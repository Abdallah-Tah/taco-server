#!/usr/bin/env python3
"""
Taco Polymarket Executor — Place real trades on Polymarket CLOB via Tor.

Functions:
  - buy_yes / buy_no: Place limit orders
  - get_positions: Check current positions
  - get_balance: Check USDC balance
  - cancel_order: Cancel open orders
  - scan_and_trade: Auto-trade from scanner output

Usage:
  python3 polymarket_executor.py balance              # Check balance
  python3 polymarket_executor.py positions             # Show positions
  python3 polymarket_executor.py orders                # Show open orders
  python3 polymarket_executor.py buy <token_id> <amount> <price>   # Buy limit order
  python3 polymarket_executor.py sell <token_id> <amount> <price>  # Sell limit order
  python3 polymarket_executor.py cancel <order_id>     # Cancel order
  python3 polymarket_executor.py auto                  # Scan + auto-trade top picks
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_API = "https://data-api.polymarket.com"

# ─── Dependencies ───

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams, AssetType
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    print("ERROR: py_clob_client not installed. Run: pip install py-clob-client", file=sys.stderr)
    sys.exit(1)

import requests

# ─── CRITICAL: Patch httpx client for Tor SOCKS proxy ───
# py_clob_client uses httpx internally, not requests.Session
# We must replace the global _http_client before any API calls
import httpx
from py_clob_client.http_helpers import helpers as _clob_helpers
_clob_helpers._http_client = httpx.Client(proxy='socks5://127.0.0.1:9050', http2=True)

# ─── Config ───

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 0  # EOA

TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
SECRETS_FILE = Path.home() / ".config" / "openclaw" / "secrets.env"

# State files
STATE_DIR = Path.home() / ".openclaw" / "workspace" / "trading"
POLY_POSITIONS_FILE = STATE_DIR / ".poly_positions.json"
POLY_TRADE_LOG = STATE_DIR / ".poly_trade_log.json"

# ─── Load config constants ───
import logging
import sys as _sys
_sys.path.insert(0, str(STATE_DIR / "scripts"))

try:
    from config import (
        POLY_MAX_SIZE as MAX_TRADE_SIZE,
        POLY_DEFAULT_SIZE as DEFAULT_TRADE_SIZE,
        POLY_MIN_SHARES as MIN_SHARES,
        POLY_MAX_POSITIONS as MAX_POSITIONS,
        POLY_MAX_ORDERS as MAX_OPEN_ORDERS,
        POLY_HIGH_CONVICTION_SIZE,
        NEWS_EDGE_MIN,
        NEWS_CONFIDENCE_MIN,
        HIGH_CONVICTION_EDGE,
        BLOCKED_CATEGORIES,
        MAX_CORRELATED_POSITIONS,
        PAUSE_THRESHOLD_USD,
    )
except ImportError:
    # Fallback defaults if config not available
    MAX_TRADE_SIZE = 10.0
    DEFAULT_TRADE_SIZE = 5.0
    MIN_SHARES = 5
    MAX_POSITIONS = 8
    MAX_OPEN_ORDERS = 10
    POLY_HIGH_CONVICTION_SIZE = 7.50
    NEWS_EDGE_MIN = 8.0
    NEWS_CONFIDENCE_MIN = 30.0
    HIGH_CONVICTION_EDGE = 15.0
    BLOCKED_CATEGORIES = ["sports", "spreads"]
    MAX_CORRELATED_POSITIONS = 2
    PAUSE_THRESHOLD_USD = 70.0

MIN_TRADE_SIZE = 1.0  # Hard floor

logger = logging.getLogger(__name__)

# ─── Helpers ───

def load_secrets():
    """Load Polymarket credentials from secrets file."""
    secrets = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                secrets[k.strip()] = v.strip().strip('"').strip("'")
    return secrets

def get_client():
    """Create authenticated ClobClient routed through Tor."""
    secrets = load_secrets()

    funder = secrets.get("POLYMARKET_FUNDER")
    private_key = secrets.get("POLYMARKET_PRIVATE_KEY")
    api_key = secrets.get("POLYMARKET_API_KEY")
    api_secret = secrets.get("POLYMARKET_API_SECRET")
    passphrase = secrets.get("POLYMARKET_PASSPHRASE")

    if not all([funder, private_key, api_key, api_secret, passphrase]):
        print("ERROR: Missing Polymarket credentials in secrets.env", file=sys.stderr)
        sys.exit(1)

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
    )

    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
        signature_type=SIGNATURE_TYPE,
        funder=funder,
        creds=creds,
    )

    # Tor routing is handled by the httpx monkey-patch at module level
    return client

def log_trade(action, token_id, amount, price, order_id=None, market_question="", extra=None):
    """Append trade to log file."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "token_id": token_id[:20] + "..." if token_id and len(token_id) > 20 else token_id,
        "amount": amount,
        "price": price,
        "order_id": order_id,
        "market": market_question[:80] if market_question else "",
    }
    if extra:
        entry.update(extra)

    log = []
    if POLY_TRADE_LOG.exists():
        try:
            log = json.loads(POLY_TRADE_LOG.read_text())
        except:
            pass
    log.append(entry)
    POLY_TRADE_LOG.write_text(json.dumps(log, indent=2))

def save_position(token_id, amount, price, side, market_question="", condition_id=""):
    """Track a position."""
    positions = load_positions()
    positions[token_id] = {
        "amount": amount,
        "avg_price": price,
        "side": side,
        "market": market_question[:80],
        "condition_id": condition_id,
        "opened": datetime.now(timezone.utc).isoformat(),
    }
    POLY_POSITIONS_FILE.write_text(json.dumps(positions, indent=2))

def load_positions():
    """Load tracked positions."""
    if POLY_POSITIONS_FILE.exists():
        try:
            return json.loads(POLY_POSITIONS_FILE.read_text())
        except:
            pass
    return {}

def remove_position(token_id):
    """Remove a closed position."""
    positions = load_positions()
    if token_id in positions:
        del positions[token_id]
        POLY_POSITIONS_FILE.write_text(json.dumps(positions, indent=2))

# ─── Core Operations ───

def check_balance(client):
    """Check USDC balance and allowances."""
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        print(f"USDC Balance: {json.dumps(bal, indent=2)}")
        return bal
    except Exception as e:
        print(f"ERROR getting balance: {e}", file=sys.stderr)
        return None

def get_open_orders(client):
    """Get all open orders."""
    try:
        orders = client.get_orders()
        return orders
    except Exception as e:
        print(f"ERROR getting orders: {e}", file=sys.stderr)
        return []


def get_order_book_raw(token_id):
    r = requests.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
    r.raise_for_status()
    return r.json()


def get_best_prices(token_id):
    ob = get_order_book_raw(token_id)
    bids = [float(x.get("price", 0) or 0) for x in (ob.get("bids") or [])]
    asks = [float(x.get("price", 0) or 0) for x in (ob.get("asks") or [])]
    best_bid = max(bids) if bids else None
    best_ask = min(asks) if asks else None
    tick = float(ob.get("tick_size") or 0.01)
    return {"best_bid": best_bid, "best_ask": best_ask, "tick_size": tick, "raw": ob}


def verify_fill(token_id, min_size=0.0, lookback_sec=120):
    """Check Data API for a recent executed BUY fill on this asset.
    Activity is usually faster than trades, so prefer that first.
    """
    def _enrich(hits, total, source):
        total_cost = 0.0
        tx_hashes = []
        for h in hits:
            try:
                sz = float(h.get("size") or 0)
                px = float(h.get("price") or 0)
                total_cost += sz * px
                txh = h.get("transactionHash")
                if txh:
                    tx_hashes.append(txh)
            except Exception:
                continue
        avg_fill_price = (total_cost / total) if total > 0 else None
        return {
            "filled": total >= max(0.0, min_size * 0.5),
            "filled_size": total,
            "trades": hits,
            "source": source,
            "avg_fill_price": avg_fill_price,
            "effective_cost": total_cost if total > 0 else None,
            "tx_hashes": tx_hashes,
        }
    try:
        user = load_secrets().get("POLYMARKET_FUNDER")
        if not user:
            return {"filled": False, "reason": "missing_funder"}
        after = int(time.time()) - lookback_sec

        # Fast path: activity feed
        ra = requests.get(f"{DATA_API}/activity", params={"user": user, "limit": 200, "offset": 0}, timeout=15)
        if ra.status_code == 200:
            acts = ra.json()
            hits = []
            total = 0.0
            for a in acts:
                if a.get("type") != "TRADE":
                    continue
                if str(a.get("asset")) != str(token_id):
                    continue
                if str(a.get("side", "")).upper() != "BUY":
                    continue
                ts = int(a.get("timestamp") or 0)
                if ts < after:
                    continue
                sz = float(a.get("size") or 0)
                total += sz
                hits.append(a)
            if total > 0:
                return _enrich(hits, total, "activity")

        # Slower fallback: trades feed
        r = requests.get(f"{DATA_API}/trades", params={"user": user, "limit": 500, "offset": 0, "takerOnly": "false"}, timeout=15)
        if r.status_code != 200:
            return {"filled": False, "reason": f"trades_http_{r.status_code}"}
        trades = r.json()
        hits = []
        total = 0.0
        for t in trades:
            if str(t.get("asset")) != str(token_id):
                continue
            if str(t.get("side", "")).upper() != "BUY":
                continue
            ts = int(t.get("timestamp") or 0)
            if ts < after:
                continue
            sz = float(t.get("size") or 0)
            total += sz
            hits.append(t)
        return _enrich(hits, total, "trades")
    except Exception as e:
        return {"filled": False, "reason": str(e)}


def place_maker_order(client, token_id, amount, price, side=BUY, market_question="", condition_id="", verify=False):
    """Place a maker GTC order; caller is responsible for later status polling/cancel."""
    import math
    if price <= 0 or price >= 1:
        print(f"ERROR: Price must be between 0.01 and 0.99, got {price}", file=sys.stderr)
        return None
    side_str = "BUY" if side == BUY else "SELL"
    price = round(float(price), 2)
    amount = math.floor(float(amount) * 100) / 100
    if amount < MIN_SHARES:
        amount = float(MIN_SHARES)
    dollar_cost = round(amount * price, 2)
    print(f"[MAKER-ATTEMPT] side={side_str} submitted_price={price:.4f} shares={amount} size=${dollar_cost:.2f}")
    try:
        order_args = OrderArgs(token_id=token_id, price=price, size=amount, side=side)
        signed = client.create_order(order_args)
        posted = client.post_order(signed, orderType=OrderType.GTC)
        order_id = None
        if isinstance(posted, dict):
            order_id = posted.get("orderID") or posted.get("id")
        print(f"[MAKER-RESULT] order_id={order_id} status=posted")
        log_trade(
            action=f"MAKER_{side_str}", token_id=token_id, amount=amount, price=price,
            order_id=order_id, market_question=market_question,
            extra={"order_type": "GTC", "submitted_price": price}
        )
        if isinstance(posted, dict):
            posted["order_id"] = order_id
            posted["submitted_price"] = price
            posted["submitted_order_type"] = "GTC"
        return posted
    except Exception as e:
        error_msg = str(e)
        print(f"❌ Maker order failed: {error_msg}", file=sys.stderr)
        log_trade(
            action=f"FAILED_MAKER_{side_str}", token_id=token_id, amount=amount, price=price,
            market_question=market_question, extra={"error": error_msg[:200]}
        )
        return None


def check_order_status(client, order_id):
    """Normalize order status using open orders list; not_found means not currently open."""
    try:
        orders = client.get_orders() or []
        found = None
        for o in orders:
            oid = o.get("orderID") or o.get("id")
            if str(oid) == str(order_id):
                found = o
                break
        if not found:
            return {"status": "not_found", "order_id": order_id}
        status = str(found.get("status") or found.get("state") or "open").lower()
        size = float(found.get("original_size") or found.get("size") or 0)
        filled = float(found.get("filled") or found.get("sizeFilled") or found.get("matched_size") or 0)
        if status in ("live", "active"):
            status = "open"
        if filled > 0 and filled < size:
            status = "partially_filled"
        elif filled >= size and size > 0:
            status = "filled"
        return {"status": status, "order_id": order_id, "size": size, "filled": filled, "raw": found}
    except Exception as e:
        return {"status": "error", "order_id": order_id, "error": str(e)}


def place_order(client, token_id, amount, price, side=BUY, market_question="", condition_id="", order_type=None, verify=False):
    """Place an order. For 15m snipes we prefer FOK at/through the current best ask."""
    if price <= 0 or price >= 1:
        print(f"ERROR: Price must be between 0.01 and 0.99, got {price}", file=sys.stderr)
        return None

    dollar_cost = amount * price
    if dollar_cost > MAX_TRADE_SIZE:
        print(f"ERROR: Trade cost ${dollar_cost:.2f} exceeds max ${MAX_TRADE_SIZE}", file=sys.stderr)
        return None
    if amount < MIN_SHARES:
        print(f"ERROR: Shares {amount} below Polymarket minimum {MIN_SHARES}", file=sys.stderr)
        return None

    side_str = "BUY" if side == BUY else "SELL"
    # --- PRECISION FIX: Polymarket decimal constraints ---
    # maker_amount (USDC): max 2 decimals | taker_amount (shares): max 4 decimals
    # We must ensure: shares * price rounds cleanly to 2 decimals
    import math
    price = round(price, 2)
    # Floor shares to 2 decimals so shares*price stays within 2-decimal USDC precision
    amount = math.floor(amount * 100) / 100
    if amount < 5:
        amount = 5.0  # Polymarket minimum
    dollar_cost = round(amount * price, 2)

    print(f"Placing {side_str} order: {amount} shares @ ${price:.3f} = ${dollar_cost:.2f}")
    print(f"  Market: {market_question[:80]}")
    print(f"  Token: {token_id[:40]}...")

    try:
        actual_price = price
        actual_order_type = order_type or OrderType.GTC
        best_ask = None
        tick = 0.01
        if side == BUY and order_type in (OrderType.FOK, OrderType.FAK):
            book = get_best_prices(token_id)
            best_ask = book.get("best_ask")
            tick = book.get("tick_size") or 0.01
            if best_ask is not None:
                # Cap slippage: never pay more than 10% above model price
                slippage_cap = round(price * 1.10, 2)
                aggressive_price = round(best_ask + tick, 2)
                if aggressive_price > slippage_cap:
                    print(f"[ATTEMPT] side={side_str} best_ask={best_ask:.4f} aggressive={aggressive_price:.4f} slippage_cap={slippage_cap:.4f} — SKIP (spread too wide)", file=sys.stderr)
                    return None
                actual_price = min(0.99, max(price, aggressive_price))
                actual_price = round(actual_price, 2)
                # Re-floor shares for the actual submitted price
                amount = math.floor(amount * 100) / 100
                if amount < 5:
                    amount = 5.0
                dollar_cost = round(amount * actual_price, 2)
                print(f"[ATTEMPT] side={side_str} best_ask={best_ask:.4f} submitted_price={actual_price:.4f} slippage_cap={slippage_cap:.4f} shares={amount} size=${dollar_cost:.2f}")
            else:
                print(f"[ATTEMPT] side={side_str} best_ask=NONE -- skipping FOK order", file=sys.stderr)
                return None
        else:
            print(f"[ATTEMPT] side={side_str} price={actual_price:.4f} shares={amount} size=${dollar_cost:.2f} order_type={actual_order_type}")

        order_args = OrderArgs(
            token_id=token_id,
            price=round(actual_price, 2),
            size=amount,
            side=side,
        )
        signed = client.create_order(order_args)
        posted = client.post_order(signed, orderType=actual_order_type)

        order_id = None
        if isinstance(posted, dict):
            order_id = posted.get("orderID") or posted.get("id")
            print(f"[RESULT] filled=pending order_id={order_id} reason=posted_ok")
            print(f"   Response: {json.dumps(posted, indent=2)[:500]}")
        else:
            print(f"[RESULT] filled=pending order_id=none reason=non_dict_response")

        log_trade(
            action=side_str,
            token_id=token_id,
            amount=amount,
            price=actual_price,
            order_id=order_id,
            market_question=market_question,
            extra={"order_type": str(actual_order_type), "best_ask": best_ask, "submitted_price": actual_price, "tick_size": tick},
        )

        fill = None
        if verify and side == BUY:
            time.sleep(2)
            fill = verify_fill(token_id, min_size=amount, lookback_sec=180)
            if fill.get("filled"):
                print(f"[RESULT] filled=true order_id={order_id} reason=fill_confirmed shares={fill.get('filled_size', 0):.4f}")
                save_position(token_id, fill.get("filled_size", amount), actual_price, side_str, market_question, condition_id)
            else:
                print(f"[RESULT] filled=false order_id={order_id} reason=no_fill_confirmed", file=sys.stderr)

        if isinstance(posted, dict):
            posted["fill_check"] = fill
            posted["submitted_price"] = actual_price
            posted["submitted_order_type"] = str(actual_order_type)
        return posted

    except Exception as e:
        error_msg = str(e)
        print(f"❌ Order failed: {error_msg}", file=sys.stderr)
        log_trade(
            action=f"FAILED_{side_str}",
            token_id=token_id,
            amount=amount,
            price=price,
            market_question=market_question,
            extra={"error": error_msg[:200]},
        )
        return None

def cancel_order(client, order_id):
    """Cancel an open order."""
    try:
        result = client.cancel(order_id)
        print(f"✅ Cancelled order {order_id}: {result}")
        log_trade("CANCEL", "", 0, 0, order_id=order_id)
        return result
    except Exception as e:
        print(f"❌ Cancel failed: {e}", file=sys.stderr)
        return None

def cancel_all_orders(client):
    """Cancel all open orders."""
    try:
        result = client.cancel_all()
        print(f"✅ Cancelled all orders: {result}")
        return result
    except Exception as e:
        print(f"❌ Cancel all failed: {e}", file=sys.stderr)
        return None

# ─── Auto-Trade ───

def auto_trade(client, scanner_json=None):
    """
    Scan markets and place trades on top opportunities.
    Uses polymarket_scanner_v2.py output or runs it fresh.
    """
    import subprocess

    # Check current positions
    positions = load_positions()
    if len(positions) >= MAX_POSITIONS:
        print(f"Already at max positions ({MAX_POSITIONS}). Skipping auto-trade.")
        return

    # Check open orders
    open_orders = get_open_orders(client)
    if isinstance(open_orders, list) and len(open_orders) >= MAX_OPEN_ORDERS:
        print(f"Already at max open orders ({MAX_OPEN_ORDERS}). Skipping.")
        return

    # Run scanner
    if not scanner_json:
        print("Running scanner...")
        scanner_path = STATE_DIR / "scripts" / "polymarket_scanner_v2.py"
        venv_python = STATE_DIR / ".polymarket-venv" / "bin" / "python3"
        
        result = subprocess.run(
            [str(venv_python), str(scanner_path), "--json"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"Scanner error: {result.stderr[:300]}", file=sys.stderr)
            return
        
        try:
            opportunities = json.loads(result.stdout)
        except json.JSONDecodeError:
            print("Scanner output not valid JSON", file=sys.stderr)
            return
    else:
        opportunities = scanner_json

    if not opportunities:
        print("No opportunities found.")
        return

    print(f"\nFound {len(opportunities)} opportunities above threshold")

    # Filter: skip markets we already have positions in
    existing_tokens = set(positions.keys())
    candidates = []

    # Load correlation guard
    try:
        from correlation import check_correlation
        has_correlation = True
    except ImportError:
        has_correlation = False

    for opp in opportunities:
        token_ids = opp.get("token_ids", [])
        if not token_ids:
            continue
        if any(t in existing_tokens for t in token_ids):
            continue

        # BLOCKED CATEGORIES check (from config)
        cat = opp.get("category", "").lower()
        q = opp.get("question", "").lower()
        blocked = False
        for bc in BLOCKED_CATEGORIES:
            if bc == cat or bc in q:
                print(f"BLOCKED CATEGORY: {bc} — skipping '{opp.get('question','')[:60]}'")
                logger.info("BLOCKED CATEGORY: %s for market '%s'", bc, opp.get('question','')[:60])
                blocked = True
                break
        if blocked:
            continue

        # Correlation guard
        if has_correlation:
            allowed, thesis, corr_count = check_correlation(opp.get("question", ""), positions)
            if not allowed:
                msg = f"CORRELATION GUARD: blocked '{opp.get('question','')[:60]}' (thesis={thesis}, count={corr_count})"
                print(msg)
                logger.warning(msg)
                continue

        # Need a tight spread to trade
        spread = opp.get("spread")
        if spread is not None and spread > 0.05:
            continue
        candidates.append(opp)

    if not candidates:
        print("No new candidates after filtering.")
        return

    # ── Drawdown / pause check ──
    try:
        from portfolio import check_drawdown, _load_portfolio, _save_portfolio, snapshot, is_paused
        _portfolio = _load_portfolio()
        _snap = snapshot()
        _total_usd = _snap.get("total_usd", None)
        if _total_usd is None:
            print("⚠️ snapshot() returned no total_usd, using fallback $100")
            _total_usd = 100.0
        _portfolio = check_drawdown(_portfolio, _total_usd)
        _save_portfolio(_portfolio)
        if is_paused(_portfolio):
            print(f"⛔ DRAWDOWN PAUSE: capital=${_total_usd:.2f} — skipping auto-trade.")
            return
    except ImportError:
        pass

    # Pick top candidates — but validate with news first
    slots = MAX_POSITIONS - len(positions)
    
    # Import news edge analyzer
    try:
        sys.path.insert(0, str(STATE_DIR / "scripts"))
        from polymarket_news_edge import analyze_opportunity
        has_news = True
        print("\n📰 News edge validation enabled")
    except ImportError:
        has_news = False
        print("\n⚠️ News edge module not available, trading without validation")

    validated = []
    for opp in candidates[:slots * 3]:  # Check 3x candidates to find enough valid ones
        if len(validated) >= slots:
            break

        question = opp.get("question", "?")
        yes_price = opp.get("yes", 0.5)
        token_ids = opp.get("token_ids", [])
        
        if has_news:
            print(f"\n  📰 Checking news for: {question[:60]}...")
            analysis = analyze_opportunity(question, yes_price)
            rec = analysis.get("recommendation", "SKIP")
            edge = analysis.get("edge", 0)
            confidence = analysis.get("confidence", 0)
            
            if rec == "SKIP":
                print(f"     ⏭️ SKIP — {analysis.get('reason', 'no edge')}")
                continue
            
            print(f"     ✅ {rec} — edge:{edge:+.3f} conf:{confidence:.0%}")
            
            # Override direction based on news
            if edge > 0:
                opp["_direction"] = "YES"
                opp["_token"] = token_ids[0] if token_ids else None
                opp["_price"] = yes_price
            else:
                opp["_direction"] = "NO"
                opp["_token"] = token_ids[1] if len(token_ids) > 1 else None
                opp["_price"] = 1 - yes_price
            
            # Size based on conviction
            if rec == "STRONG_BUY":
                opp["_size_mult"] = 1.5
            else:
                opp["_size_mult"] = 1.0
            
            validated.append(opp)
            time.sleep(0.5)
        else:
            validated.append(opp)

    picks = validated

    print(f"\n🎯 Trading {len(picks)} news-validated markets:")
    for opp in picks:
        token_ids = opp.get("token_ids", [])
        yes_token = token_ids[0] if token_ids else None
        no_token = token_ids[1] if len(token_ids) > 1 else None
        yes_price = opp.get("yes", 0.5)
        question = opp.get("question", "?")
        condition_id = opp.get("condition_id", "")
        score = opp.get("score", 0)

        # Use news-validated direction if available
        if "_direction" in opp:
            direction = opp["_direction"]
            token = opp["_token"]
            price = opp["_price"]
            size_mult = opp.get("_size_mult", 1.0)
        else:
            if yes_price <= 0.5:
                token = yes_token
                price = yes_price
                direction = "YES"
            else:
                token = no_token
                price = 1 - yes_price
                direction = "NO"
            size_mult = 1.0

        if not token:
            continue

        # Size: scale by score and conviction
        if score >= 80:
            size_usd = DEFAULT_TRADE_SIZE * 1.5 * size_mult
        elif score >= 70:
            size_usd = DEFAULT_TRADE_SIZE * size_mult
        else:
            size_usd = DEFAULT_TRADE_SIZE * 0.75

        size_usd = min(size_usd, MAX_TRADE_SIZE)
        shares = round(size_usd / price, 1) if price > 0 else 0

        if shares <= 0:
            continue

        print(f"\n  [{direction}] {question[:70]}")
        print(f"  Score: {score} | Price: {price:.3f} | Shares: {shares} | Cost: ~${shares * price:.2f}")

        result = place_order(
            client,
            token_id=token,
            amount=shares,
            price=round(price, 2),
            side=BUY,
            market_question=question,
            condition_id=condition_id,
        )

        if result:
            print(f"  ✅ Order placed")
        else:
            print(f"  ❌ Order failed")

        time.sleep(1)  # Rate limit between orders

# ─── CLI ───

def show_positions():
    """Display tracked positions."""
    positions = load_positions()
    if not positions:
        print("No tracked positions.")
        return
    print(f"\n📊 Polymarket Positions ({len(positions)}):")
    print("-" * 60)
    for token_id, pos in positions.items():
        print(f"  {pos.get('side', '?')} | ${pos.get('avg_price', 0):.3f} x {pos.get('amount', 0)}")
        print(f"  {pos.get('market', '?')}")
        print(f"  Token: {token_id[:40]}...")
        print(f"  Opened: {pos.get('opened', '?')}")
        print()

def show_trade_log():
    """Display trade history."""
    if not POLY_TRADE_LOG.exists():
        print("No trade history.")
        return
    try:
        log = json.loads(POLY_TRADE_LOG.read_text())
    except:
        print("Trade log corrupted.")
        return
    print(f"\n📜 Trade Log ({len(log)} entries):")
    print("-" * 60)
    for entry in log[-10:]:  # Last 10
        print(f"  {entry.get('timestamp', '?')[:19]} | {entry.get('action', '?')} | ${entry.get('price', 0):.3f} x {entry.get('amount', 0)}")
        if entry.get('market'):
            print(f"    {entry['market']}")
        if entry.get('error'):
            print(f"    ❌ {entry['error']}")
        print()

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0].lower()

    if cmd == "balance":
        client = get_client()
        check_balance(client)

    elif cmd == "positions":
        show_positions()

    elif cmd == "orders":
        client = get_client()
        orders = get_open_orders(client)
        if orders:
            print(json.dumps(orders, indent=2, default=str)[:2000])
        else:
            print("No open orders.")

    elif cmd == "log":
        show_trade_log()

    elif cmd == "buy" and len(args) >= 4:
        token_id = args[1]
        amount = float(args[2])
        price = float(args[3])
        question = " ".join(args[4:]) if len(args) > 4 else ""
        client = get_client()
        place_order(client, token_id, amount, price, BUY, question)

    elif cmd == "buy_fok" and len(args) >= 4:
        token_id = args[1]
        amount = float(args[2])
        price = float(args[3])
        question = " ".join(args[4:]) if len(args) > 4 else ""
        client = get_client()
        result = place_order(client, token_id, amount, price, BUY, question, order_type=OrderType.FOK, verify=True)
        if isinstance(result, dict):
            print("__RESULT__" + json.dumps(result, default=str))

    elif cmd == "maker_buy" and len(args) >= 4:
        token_id = args[1]
        amount = float(args[2])
        price = float(args[3])
        question = " ".join(args[4:]) if len(args) > 4 else ""
        client = get_client()
        result = place_maker_order(client, token_id, amount, price, BUY, question)
        if isinstance(result, dict):
            print("__RESULT__" + json.dumps(result, default=str))

    elif cmd == "order_status" and len(args) >= 2:
        order_id = args[1]
        client = get_client()
        result = check_order_status(client, order_id)
        print("__RESULT__" + json.dumps(result, default=str))

    elif cmd == "verify_fill" and len(args) >= 2:
        token_id = args[1]
        min_size = float(args[2]) if len(args) >= 3 else 0.0
        result = verify_fill(token_id, min_size=min_size, lookback_sec=600)
        print("__RESULT__" + json.dumps(result, default=str))

    elif cmd == "sell" and len(args) >= 4:
        token_id = args[1]
        amount = float(args[2])
        price = float(args[3])
        client = get_client()
        place_order(client, token_id, amount, price, SELL)

    elif cmd == "cancel" and len(args) >= 2:
        order_id = args[1]
        client = get_client()
        if order_id == "all":
            cancel_all_orders(client)
        else:
            cancel_order(client, order_id)

    elif cmd == "auto":
        client = get_client()
        auto_trade(client)

    elif cmd == "test":
        # Test connectivity only
        print("Testing Polymarket connection via Tor...")
        client = get_client()
        bal = check_balance(client)
        if bal:
            print("✅ Connection working!")
        else:
            print("❌ Connection failed")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)

if __name__ == "__main__":
    main()
