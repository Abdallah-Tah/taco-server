#!/usr/bin/env python3
"""
polymarket_sol15m_discount.py

SOL 15m discount hunter + maker rebate executor.

Modes:
  - DRY RUN (default): alerts only, simulates maker fills
  - LIVE: places real GTC maker BUY limit orders on both YES + NO sides
          at best bid price, waits for fills, tracks P&L

When combined BID < 1.00 (edge exists), the script places maker orders on
both sides. If both fill → guaranteed profit (1.00 - cost - fees).
Auto-redeem daemon handles on-chain merge after resolution.

New env vars:
  ETH15M_DISCOUNT_MAKER_EXEC (bool, default false) — enable real maker execution
  ETH15M_DISCOUNT_FILL_TIMEOUT_SEC (int, default 120) — seconds to wait for fills
  ETH15M_DISCOUNT_FILL_POLL_SEC (int, default 4) — poll interval while waiting
  ETH15M_DISCOUNT_MAKER_EXEC_THRESHOLD (float, default 0.97) — combined bid threshold for execution
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from polymarket_clob_pricing import fetch_book

WORK_DIR = Path("/home/abdaltm86/.openclaw/workspace/trading")
CREDS_FILE = Path("/home/abdaltm86/.config/openclaw/secrets.env")
STATE_F = WORK_DIR / ".poly_eth15m_discount_state.json"
LOG_F = Path("/tmp/polymarket_eth15m_discount.log")
JOURNAL_DB = WORK_DIR / "journal.db"
VENV_PY = WORK_DIR / ".polymarket-venv" / "bin" / "python3"


def _load_env():
    env = {}
    if CREDS_FILE.exists():
        for line in CREDS_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = _load_env()


try:
    _cfg = json.loads(Path("/home/abdaltm86/.openclaw/openclaw.json").read_text())
except Exception:
    _cfg = {}


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, ENV.get(key, default)))
    except Exception:
        return float(default)


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, ENV.get(key, default)))
    except Exception:
        return int(default)


def _bool(key: str, default: bool) -> bool:
    return str(os.environ.get(key, ENV.get(key, str(default)))).lower() in {"1", "true", "yes", "on"}


TELEGRAM_TOKEN = ENV.get("TELEGRAM_TOKEN") or _cfg.get("channels", {}).get("telegram", {}).get("botToken", "")
CHAT_ID = ENV.get("CHAT_ID", "-1003948211258")
TOPIC_ID = ENV.get("TOPIC_ID", "3")

WINDOW_SEC = _int("ETH15M_DISCOUNT_WINDOW_SEC", 900)
CHECK_SEC = _int("ETH15M_DISCOUNT_SCAN_SEC", 5)
ACTIVE_LAST_SEC = _int("ETH15M_DISCOUNT_ACTIVE_LAST_SEC", 180)
MIN_LEFT_SEC = _int("ETH15M_DISCOUNT_MIN_LEFT_SEC", 5)
COMBINED_THRESHOLD = _float("ETH15M_DISCOUNT_COMBINED_THRESHOLD", 0.995)
SHADOW_THRESHOLD = _float("ETH15M_DISCOUNT_SHADOW_THRESHOLD", 1.020)
MAX_SPREAD_PER_LEG = _float("ETH15M_DISCOUNT_MAX_SPREAD", 0.02)
MAX_LEG_PRICE = _float("ETH15M_DISCOUNT_MAX_LEG_PRICE", 0.75)
MIN_LIQUIDITY = _float("ETH15M_DISCOUNT_MIN_LIQUIDITY", 1000.0)
PAIR_BUDGET = _float("ETH15M_DISCOUNT_PAIR_BUDGET", 5.0)
MIN_TOP_SIZE = _float("ETH15M_DISCOUNT_MIN_TOP_SIZE", 20.0)
STRONG_EDGE_MIN = _float("ETH15M_DISCOUNT_STRONG_EDGE_MIN", 0.04)
STRONG_SPREAD_MAX = _float("ETH15M_DISCOUNT_STRONG_SPREAD_MAX", 0.01)
DRY_RUN = _bool("ETH15M_DISCOUNT_DRY_RUN", True)
NOTIFY_START = _bool("ETH15M_DISCOUNT_NOTIFY_START", True)

# ── Maker execution config ──
MAKER_EXEC = _bool("ETH15M_DISCOUNT_MAKER_EXEC", False)
MAKER_EXEC_THRESHOLD = _float("ETH15M_DISCOUNT_MAKER_EXEC_THRESHOLD", 0.97)
FILL_TIMEOUT_SEC = _int("ETH15M_DISCOUNT_FILL_TIMEOUT_SEC", 25)
FILL_POLL_SEC = _int("ETH15M_DISCOUNT_FILL_POLL_SEC", 4)

# ── Maker window-open entry config (terminal-bot style) ──
MAKER_ENTRY_WINDOW_SEC = _int("ETH15M_DISCOUNT_MAKER_ENTRY_WINDOW_SEC", 45)
MAKER_ENTRY_DELAY_SEC = _int("ETH15M_DISCOUNT_MAKER_ENTRY_DELAY_SEC", 10)
MAKER_MAX_RETRIES = _int("ETH15M_DISCOUNT_MAKER_MAX_RETRIES", 3)
MAKER_MIN_PRICE = _float("ETH15M_DISCOUNT_MAKER_MIN_PRICE", 0.42)
MAKER_MAX_PRICE = _float("ETH15M_DISCOUNT_MAKER_MAX_PRICE", 0.58)

SERIES_SLUG = "eth-up-or-down-15m"

# ── Scalp strategy config ──
SCALP_ENABLED = _bool("ETH15M_SCALP_ENABLED", False)
SCALP_SIZE = _float("ETH15M_SCALP_SIZE", 5.0)
SCALP_ENTRY_MAX = _float("ETH15M_SCALP_ENTRY_MAX", 0.92)
SCALP_TARGET = _float("ETH15M_SCALP_TARGET", 0.97)
SCALP_STOP = _float("ETH15M_SCALP_STOP", 0.88)
SCALP_SCAN_SEC = _int("ETH15M_SCALP_SCAN_SEC", 120)

# ── CLOB client (lazy init) ──
_clob_client = None


def get_clob_client():
    """Create authenticated ClobClient using executor's get_client pattern."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        log("[MAKER] py_clob_client not available — cannot place real orders")
        return None

    funder = ENV.get("POLYMARKET_FUNDER")
    private_key = ENV.get("POLYMARKET_PRIVATE_KEY")
    api_key = ENV.get("POLYMARKET_API_KEY")
    api_secret = ENV.get("POLYMARKET_API_SECRET")
    passphrase = ENV.get("POLYMARKET_PASSPHRASE")

    if not all([funder, private_key, api_key, api_secret, passphrase]):
        log("[MAKER] Missing Polymarket credentials — cannot place real orders")
        return None

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=passphrase,
    )

    _clob_client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=private_key,
        signature_type=2,  # EOA
        funder=funder,
        creds=creds,
    )
    return _clob_client


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_F, "a") as f:
        f.write(line + "\n")


def tg(msg: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log(f"[TG-SKIP] {msg}")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "message_thread_id": int(TOPIC_ID)},
            timeout=10,
        )
        if r.status_code != 200:
            log(f"[TG-ERR] code={r.status_code} response={r.text}")
    except Exception as e:
        log(f"[TG-ERR] {e}")


def load_state() -> dict:
    try:
        return json.loads(STATE_F.read_text())
    except Exception:
        return {
            "last_window_ts": 0,
            "last_alert_key": "",
            "opportunities": [],
            "maker_trades": [],
        }


def save_state(state: dict):
    try:
        STATE_F.write_text(json.dumps(state, indent=2, sort_keys=True))
    except Exception as e:
        log(f"[STATE] save failed: {e}")


def get_current_slug() -> tuple[str, int, int]:
    now = int(time.time())
    window_ts = now - (now % WINDOW_SEC)
    secs_left = window_ts + WINDOW_SEC - now
    return f"eth-updown-15m-{window_ts}", window_ts, secs_left


def get_market(slug: str) -> dict | None:
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json() or []
        if not data:
            return None
        m = data[0]
        outcomes = json.loads(m.get("outcomes", "[]")) if m.get("outcomes") else []
        prices = json.loads(m.get("outcomePrices", "[]") or "[]")
        token_ids = json.loads(m.get("clobTokenIds", "[]")) if m.get("clobTokenIds") else []
        return {
            "id": m.get("id"),
            "slug": m.get("slug", slug),
            "question": m.get("question", slug),
            "condition_id": m.get("conditionId"),
            "outcomes": outcomes,
            "token_ids": token_ids,
            "yes_price": float(prices[0]) if len(prices) > 0 else None,
            "no_price": float(prices[1]) if len(prices) > 1 else None,
            "liquidity": float(m.get("liquidity") or 0),
            "volume": float(m.get("volume") or 0),
            "closed": bool(m.get("closed", False)),
            "end_date": m.get("endDate"),
        }
    except Exception as e:
        log(f"[GAMMA] {e}")
        return None


def _top_size(book: dict) -> float:
    asks = book.get("asks") or []
    if not asks:
        return 0.0
    return float(asks[0].get("size") or 0.0)


def _size_at_or_below(book: dict, price_cap: float) -> float:
    total = 0.0
    for level in (book.get("asks") or []):
        try:
            if float(level.get("price") or 0) <= price_cap + 1e-9:
                total += float(level.get("size") or 0)
        except Exception:
            continue
    return total


def _round_down(value: float, places: int = 2) -> float:
    factor = 10 ** places
    return math.floor(value * factor) / factor


def classify_opportunity(gross_edge: float, yes_spread: float, no_spread: float, top_ok: bool, fillable_ok: bool) -> tuple[str, list[str]]:
    warnings = []
    if not top_ok:
        warnings.append("thin_top_book")
    if not fillable_ok:
        warnings.append("pair_size_not_fully_fillable")
    if yes_spread > STRONG_SPREAD_MAX or no_spread > STRONG_SPREAD_MAX:
        warnings.append("spread_not_tight")

    if gross_edge >= STRONG_EDGE_MIN and yes_spread <= STRONG_SPREAD_MAX and no_spread <= STRONG_SPREAD_MAX and top_ok and fillable_ok:
        return "strong", warnings
    if fillable_ok:
        return "watch", warnings
    return "caution", warnings


def build_opportunity(market: dict, secs_left: int) -> dict | None:
    token_ids = market.get("token_ids") or []
    if len(token_ids) < 2:
        return None

    yes_book = fetch_book(str(token_ids[0]))
    no_book = fetch_book(str(token_ids[1]))

    yes_ask = yes_book.get("best_ask")
    no_ask = no_book.get("best_ask")
    yes_spread = yes_book.get("spread")
    no_spread = no_book.get("spread")

    if yes_ask is None or no_ask is None:
        return None
    if yes_spread is None or no_spread is None:
        return None
    if yes_spread > MAX_SPREAD_PER_LEG or no_spread > MAX_SPREAD_PER_LEG:
        return None
    if yes_ask > MAX_LEG_PRICE or no_ask > MAX_LEG_PRICE:
        return None
    if market.get("liquidity", 0.0) < MIN_LIQUIDITY:
        return None

    combined = round(float(yes_ask) + float(no_ask), 4)
    if combined > COMBINED_THRESHOLD:
        return None

    gross_edge = round(1.0 - combined, 4)
    if gross_edge <= 0:
        return None

    pair_shares = _round_down(PAIR_BUDGET / combined, 2)
    if pair_shares <= 0:
        return None

    yes_top = _top_size(yes_book)
    no_top = _top_size(no_book)
    yes_size_ok = yes_top >= max(MIN_TOP_SIZE, pair_shares)
    no_size_ok = no_top >= max(MIN_TOP_SIZE, pair_shares)

    yes_fillable = _size_at_or_below(yes_book, float(yes_ask))
    no_fillable = _size_at_or_below(no_book, float(no_ask))
    fillable_ok = yes_fillable >= pair_shares and no_fillable >= pair_shares
    top_ok = yes_size_ok and no_size_ok
    alert_level, warnings = classify_opportunity(gross_edge, float(yes_spread), float(no_spread), top_ok, fillable_ok)

    return {
        "slug": market.get("slug"),
        "question": market.get("question"),
        "secs_left": secs_left,
        "gamma_yes": market.get("yes_price"),
        "gamma_no": market.get("no_price"),
        "yes_ask": round(float(yes_ask), 4),
        "no_ask": round(float(no_ask), 4),
        "combined": combined,
        "gross_edge": gross_edge,
        "yes_spread": round(float(yes_spread), 4),
        "no_spread": round(float(no_spread), 4),
        "liquidity": round(float(market.get("liquidity") or 0), 2),
        "volume": round(float(market.get("volume") or 0), 2),
        "pair_budget": round(PAIR_BUDGET, 2),
        "pair_shares": pair_shares,
        "yes_top": round(yes_top, 2),
        "no_top": round(no_top, 2),
        "yes_fillable": round(yes_fillable, 2),
        "no_fillable": round(no_fillable, 2),
        "yes_top_ok": yes_size_ok,
        "no_top_ok": no_size_ok,
        "top_ok": top_ok,
        "fillable_ok": fillable_ok,
        "alert_level": alert_level,
        "warnings": warnings,
    }


def opportunity_key(opp: dict) -> str:
    return "|".join([
        str(opp.get("slug")),
        f"{opp.get('yes_ask', 0):.4f}",
        f"{opp.get('no_ask', 0):.4f}",
        f"{opp.get('combined', 0):.4f}",
    ])


def format_alert(opp: dict) -> str:
    level = str(opp.get("alert_level") or "watch").upper()
    icon = {"strong": "🟢", "watch": "🟡", "caution": "🟠"}.get(str(opp.get("alert_level") or "watch"), "🟡")
    top_ok = "yes" if opp.get("top_ok") else "thin"
    fillable = "yes" if opp.get("fillable_ok") else "partial-risk"
    warning_text = ", ".join(opp.get("warnings") or []) or "none"
    return (
        f"{icon} [ETH-DISCOUNT] DRY-RUN {level}\n"
        f"Market: {opp['question']}\n"
        f"Time left: {opp['secs_left']}s\n"
        f"YES ask: {opp['yes_ask']:.4f} | NO ask: {opp['no_ask']:.4f}\n"
        f"Combined ask: {opp['combined']:.4f} | Gross edge: +{opp['gross_edge']:.4f}\n"
        f"Spreads: YES {opp['yes_spread']:.4f} / NO {opp['no_spread']:.4f}\n"
        f"Top size: YES {opp['yes_top']:.2f} / NO {opp['no_top']:.2f} ({top_ok})\n"
        f"Fillable for pair size: {fillable} | Budget: ${opp['pair_budget']:.2f} | Pair shares: {opp['pair_shares']:.2f}\n"
        f"Warnings: {warning_text}\n"
        f"Liquidity: ${opp['liquidity']:.2f} | Volume: ${opp['volume']:.2f}"
    )


def reset_window_state(state: dict, window_ts: int):
    if int(state.get("last_window_ts") or 0) != int(window_ts):
        state["last_window_ts"] = int(window_ts)
        state["last_alert_key"] = ""
        state.pop("last_maker_exec_window", None)
        save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
# MAKER EXECUTION — place GTC buy limit orders on both YES + NO
# ══════════════════════════════════════════════════════════════════════════════

def maker_place_order(token_id: str, shares: float, price: float) -> dict:
    """Place a GTC maker BUY via executor subprocess. Returns {success, order_id}."""
    if DRY_RUN or not MAKER_EXEC:
        sim_id = f"sim-{int(time.time())}-{token_id[-6:]}"
        log(f"[SIM] BUY {shares:.2f} @ {price:.4f} token={token_id[-12:]}")
        return {"success": True, "order_id": sim_id, "sim": True}

    # Use executor subprocess for real orders (same pattern as BTC engine)
    cmd = [
        str(VENV_PY),
        str(WORK_DIR / "scripts" / "polymarket_executor.py"),
        "maker_buy",
        token_id,
        str(shares),
        str(price),
    ]
    log(f"[MAKER] placing BUY {shares:.2f} @ {price:.4f} token={token_id[-12:]}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        order_id = None
        if result.returncode == 0:
            # Parse order_id from multiple possible output formats:
            # 1. __RESULT__{"order_id": "0x...", ...}
            # 2. [MAKER-RESULT] order_id=0x... status=posted
            for line in result.stdout.splitlines():
                if "__RESULT__" in line:
                    try:
                        rjson = json.loads(line.split("__RESULT__", 1)[-1])
                        oid = rjson.get("order_id") or rjson.get("orderID")
                        if oid and oid != "None":
                            order_id = str(oid)
                    except Exception:
                        pass
                elif "order_id=" in line and order_id is None:
                    part = line.split("order_id=")[-1].split()[0].rstrip(",")
                    if part and part != "None":
                        order_id = part
            if order_id:
                log(f"[MAKER] order placed: oid={order_id[:16]}...")
            else:
                # Order may have gone through even without parseable ID
                # Check executor stderr for clues
                log(f"[MAKER] WARNING: order_id not parsed from output. stdout={result.stdout[:200]} stderr={result.stderr[:200]}")
        success = result.returncode == 0 and order_id is not None
        if not success and result.returncode != 0:
            log(f"[MAKER] order failed: rc={result.returncode} err={result.stderr[:200]}")
        return {"success": success, "order_id": order_id}
    except Exception as e:
        log(f"[MAKER] order exception: {e}")
        return {"success": False, "order_id": None}


def maker_cancel_order(order_id: str) -> bool:
    """Cancel an open maker order via executor subprocess."""
    if not order_id or order_id.startswith("sim-"):
        return True
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "cancel", order_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        ok = result.returncode == 0
        if not ok:
            log(f"[MAKER] cancel failed for {order_id[:12]}: {result.stderr[:100]}")
        return ok
    except Exception as e:
        log(f"[MAKER] cancel exception: {e}")
        return False


def executor_order_status(order_id: str) -> dict:
    if not order_id or order_id.startswith("sim-"):
        return {"status": "unknown", "order_id": order_id}
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "order_status", order_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in result.stdout.splitlines():
            if "__RESULT__" in line:
                try:
                    return json.loads(line.split("__RESULT__", 1)[-1])
                except Exception:
                    pass
    except Exception as e:
        return {"status": "error", "error": str(e), "order_id": order_id}
    return {"status": "unknown", "order_id": order_id}


def executor_verify_fill(token_id: str, min_size: float = 0.0) -> dict:
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "verify_fill", str(token_id), str(min_size)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        for line in result.stdout.splitlines():
            if "__RESULT__" in line:
                try:
                    return json.loads(line.split("__RESULT__", 1)[-1])
                except Exception:
                    pass
    except Exception as e:
        return {"filled": False, "reason": str(e)}
    return {"filled": False, "reason": "no_result"}


def maker_check_order(order_id: str) -> str:
    """Check order status via CLOB client. Returns: 'filled', 'open', 'cancelled', 'unknown'."""
    if not order_id or order_id.startswith("sim-"):
        return "unknown"

    client = get_clob_client()
    if not client:
        return "unknown"

    try:
        order = client.get_order(order_id)
        if not order:
            return "unknown"
        status = str(order.get("status") or "").upper()
        if status in ("FILLED", "FILLED_FULLY"):
            return "filled"
        if "PARTIAL" in status:
            return "partial"
        if status in ("CANCELLED", "CANCELLED_BY_USER", "EXPIRED"):
            return "cancelled"
        if status == "OPEN" or status == "LIVE":
            return "open"
        return "unknown"
    except Exception as e:
        log(f"[MAKER] check order error: {e}")
        return "unknown"


def get_token_position_size(token_id: str) -> float:
    """Return current acquired position size for a token from local position file if available."""
    try:
        pos_f = WORK_DIR / ".positions.json"
        if not pos_f.exists():
            return 0.0
        data = json.loads(pos_f.read_text())
        item = data.get(str(token_id)) or {}
        return float(item.get("shares") or item.get("size") or 0.0)
    except Exception:
        return 0.0


def verify_fill_delta(token_id: str, baseline: float, min_delta: float = 0.25) -> float:
    """Authoritative-ish local reconciliation using position delta after order placement."""
    try:
        cur = get_token_position_size(token_id)
        delta = round(cur - baseline, 8)
        if delta >= min_delta:
            return delta
        return 0.0
    except Exception:
        return 0.0


def recent_market_activity_fill_map(question: str, token_ids: list[str], max_age_sec: int = 1800) -> dict[str, float]:
    """Best-effort authority fallback using recent Polymarket activity for this exact market question."""
    out = {str(tid): 0.0 for tid in token_ids}
    try:
        funder = (ENV.get("POLYMARKET_FUNDER") or "").lower()
        if not funder or not question:
            return out
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": funder, "limit": 80, "offset": 0},
            headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        now_ts = int(time.time())
        acts = r.json() or []
        outcome_index = {0: "up", 1: "down"}
        for a in acts:
            if a.get("type") != "TRADE":
                continue
            if (a.get("title") or "") != question:
                continue
            try:
                ts = int(a.get("timestamp") or 0)
            except Exception:
                ts = 0
            if ts and (now_ts - ts) > max_age_sec:
                continue
            outcome = str(a.get("outcome") or "").strip().lower()
            size = float(a.get("size") or 0.0)
            for idx, tid in enumerate(token_ids[:2]):
                expected = outcome_index.get(idx)
                if expected and outcome == expected:
                    out[str(tid)] += size
        return out
    except Exception as e:
        log(f"[ETH-MAKER] activity reconciliation error: {e}")
        return out


def reconcile_order_fill(token_id: str, order_id: str | None, baseline: float, expected_shares: float) -> tuple[str, float, str]:
    """
    Reconcile order outcome using exchange evidence first, then local position delta.
    Returns (state, acquired_shares, basis).
    """
    ex = executor_order_status(order_id) if order_id else {"status": "unknown"}
    ex_status = str(ex.get("status") or "unknown").lower()

    vf = executor_verify_fill(token_id, min_size=expected_shares)
    vf_filled = bool(vf.get("filled"))
    vf_size = float(vf.get("filled_size") or 0.0)

    acquired = verify_fill_delta(token_id, baseline)

    if vf_filled and vf_size >= max(0.25, expected_shares * 0.9):
        return "filled", vf_size, "executor_verify_fill"
    if vf_filled and vf_size >= 0.25:
        return "partial", vf_size, "executor_verify_fill"
    if acquired >= max(0.25, expected_shares * 0.9):
        return "filled", acquired, "position_delta"
    if acquired >= 0.25:
        return "partial", acquired, "position_delta"
    if ex_status in ("filled", "partially_filled"):
        return ("filled" if ex_status == "filled" else "partial"), float(ex.get("filled") or 0.0), "executor_order_status"
    if ex_status == "open":
        return "open", 0.0, "executor_order_status"
    if ex_status == "cancelled":
        return "cancelled", 0.0, "executor_order_status"
    if ex_status == "not_found":
        # not found but no fill evidence yet -> unknown, not a hard cancel
        return "unknown", 0.0, "not_found"
    return "unknown", 0.0, "unknown"


def execute_maker_pair(market: dict, yes_bid: float, no_bid: float, combined_bid: float, secs_left: int):
    """
    Execute the maker rebate strategy: place GTC BUY on both YES and NO at best bid.
    Wait for fills up to FILL_TIMEOUT_SEC. Track result.
    """
    token_ids = market.get("token_ids") or []
    condition_id = market.get("condition_id", "")
    if len(token_ids) < 2:
        return

    # Calculate shares from budget
    shares = _round_down(PAIR_BUDGET / combined_bid, 2)
    if shares < 5.0:  # CLOB minimum
        shares = 5.0
    # Ensure budget fits
    cost_yes = round(shares * yes_bid, 2)
    cost_no = round(shares * no_bid, 2)
    total_cost = cost_yes + cost_no

    edge = round(1.0 - combined_bid, 4)
    log(
        f"[ETH-MAKER] EXECUTING | YES bid={yes_bid:.4f} NO bid={no_bid:.4f} "
        f"combined={combined_bid:.4f} edge=+{edge:.4f} | shares={shares:.2f} cost=${total_cost:.2f} "
        f"| {secs_left}s left"
    )

    # Capture baselines before placing orders
    yes_baseline = get_token_position_size(str(token_ids[0]))
    no_baseline = get_token_position_size(str(token_ids[1]))

    # Place both orders
    yes_result = maker_place_order(str(token_ids[0]), shares, yes_bid)
    no_result = maker_place_order(str(token_ids[1]), shares, no_bid)

    yes_oid = yes_result.get("order_id")
    no_oid = no_result.get("order_id")

    if not yes_result["success"] or not no_result["success"]:
        log(f"[ETH-MAKER] order placement failed — YES:{yes_result['success']} NO:{no_result['success']}")
        # Cancel any that succeeded
        if yes_result["success"] and yes_oid:
            maker_cancel_order(yes_oid)
        if no_result["success"] and no_oid:
            maker_cancel_order(no_oid)
        return

    sim_mode = DRY_RUN or not MAKER_EXEC
    tag = "[SIM]" if sim_mode else "[LIVE]"

    # Entry notification — match terminal-bot format
    sim_prefix = "🧪 [SIM] " if sim_mode else ""
    entry_msg = (
        f"{sim_prefix}📍🌮 [MAKER] [ETH] entering | "
        f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} | "
        f"edge ${edge:.4f}"
    )
    tg(entry_msg)

    # ── Wait for fills ──
    if sim_mode:
        # Simulate fill with probability
        import random
        time.sleep(5 + random.random() * 10)
        roll = random.random()
        if roll < 0.55:
            # Both fill
            pnl = round(shares * edge, 2)
            sign = "+" if pnl >= 0 else "-"
            msg = f"🧪 [SIM] 🟢🌮 [SIM] [ETH] | P&L: {sign}${abs(pnl):.2f} | YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f}"
            log(msg)
            tg(msg)
            return "both_filled"
        elif roll < 0.80:
            # One fills
            msg = f"🧪 [SIM] 🟠🌮 [SIM] [ETH] | PARTIAL | YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f}"
            log(msg)
            tg(msg)
            return "partial"
        else:
            # Neither fills
            msg = f"🧪 [SIM] ⚪🌮 [SIM] [ETH] | TIMEOUT | YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f}"
            log(msg)
            tg(msg)
            return "no_fill"

    # ── Live: poll for fills ──
    yes_filled = False
    no_filled = False
    start = time.time()

    while (time.time() - start) < FILL_TIMEOUT_SEC:
        if not yes_filled and yes_oid:
            s, acquired, basis = reconcile_order_fill(str(token_ids[0]), yes_oid, yes_baseline, shares)
            if s == "filled":
                yes_filled = True
                log(f"[ETH-MAKER] YES FILLED @ {yes_bid:.4f} basis={basis} acquired={acquired:.2f}")
            elif s == "partial":
                yes_filled = True
                log(f"[ETH-MAKER] YES PARTIAL/HAVE SHARES @ {yes_bid:.4f} basis={basis} acquired={acquired:.2f}")
            elif s == "cancelled":
                log(f"[ETH-MAKER] YES order cancelled/expired")
                yes_oid = None

        if not no_filled and no_oid:
            s, acquired, basis = reconcile_order_fill(str(token_ids[1]), no_oid, no_baseline, shares)
            if s == "filled":
                no_filled = True
                log(f"[ETH-MAKER] NO FILLED @ {no_bid:.4f} basis={basis} acquired={acquired:.2f}")
            elif s == "partial":
                no_filled = True
                log(f"[ETH-MAKER] NO PARTIAL/HAVE SHARES @ {no_bid:.4f} basis={basis} acquired={acquired:.2f}")
            elif s == "cancelled":
                log(f"[ETH-MAKER] NO order cancelled/expired")
                no_oid = None

        # Both filled → WIN
        if yes_filled and no_filled:
            pnl = round(shares * edge, 2)
            sign = "+" if pnl >= 0 else "-"
            msg = (
                f"🟢🌮 [MAKER] [ETH] | DONE | P&L: {sign}${abs(pnl):.2f} | "
                f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f}"
            )
            log(msg)
            tg(msg)
            return "both_filled"

        # Both cancelled or gone
        if not yes_oid and not no_oid:
            log("[ETH-MAKER] both orders gone (cancelled/expired)")
            break

        time.sleep(FILL_POLL_SEC)

    # ── Timeout handling ──
    if yes_filled and not no_filled:
        # Cancel the unfilled side
        if no_oid:
            maker_cancel_order(no_oid)
        msg = (
            f"🟠🌮 [MAKER] [ETH] | HELD_SIDE | P&L: $0.00 | "
            f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Holding YES"
        )
        log(msg)
        tg(msg)
        return "partial_yes"

    if no_filled and not yes_filled:
        if yes_oid:
            maker_cancel_order(yes_oid)
        msg = (
            f"🟠🌮 [MAKER] [ETH] | HELD_SIDE | P&L: $0.00 | "
            f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Holding NO"
        )
        log(msg)
        tg(msg)
        return "partial_no"

    # Final reconciliation before claiming timeout/no-fill
    question = market.get("question") or market.get("slug") or ""
    yes_final, yes_acq, yes_basis = reconcile_order_fill(str(token_ids[0]), yes_oid, yes_baseline, shares)
    no_final, no_acq, no_basis = reconcile_order_fill(str(token_ids[1]), no_oid, no_baseline, shares)

    # Authority fallback: recent activity for this exact market question
    activity_fill = recent_market_activity_fill_map(question, [str(token_ids[0]), str(token_ids[1])])
    act_yes = float(activity_fill.get(str(token_ids[0])) or 0.0)
    act_no = float(activity_fill.get(str(token_ids[1])) or 0.0)
    if act_yes >= 0.25 and yes_final not in ("filled", "partial"):
        yes_final, yes_acq, yes_basis = ("filled" if act_yes >= max(0.25, shares * 0.9) else "partial"), act_yes, "recent_activity"
    if act_no >= 0.25 and no_final not in ("filled", "partial"):
        no_final, no_acq, no_basis = ("filled" if act_no >= max(0.25, shares * 0.9) else "partial"), act_no, "recent_activity"

    if yes_final in ("filled", "partial") and no_final in ("filled", "partial"):
        pnl = round(shares * edge, 2)
        sign = "+" if pnl >= 0 else "-"
        msg = (
            f"🟢🌮 [MAKER] [ETH] | DONE | P&L: {sign}${abs(pnl):.2f} | "
            f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f}"
        )
        log(f"[ETH-MAKER] final reconciliation upgraded TIMEOUT->DONE yes={yes_basis}:{yes_acq:.2f} no={no_basis}:{no_acq:.2f}")
        log(msg)
        tg(msg)
        return "both_filled"

    if yes_final in ("filled", "partial") and no_final not in ("filled", "partial"):
        if no_oid:
            maker_cancel_order(no_oid)
        msg = (
            f"🟠🌮 [MAKER] [ETH] | HELD_SIDE | P&L: $0.00 | "
            f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Holding YES"
        )
        log(f"[ETH-MAKER] final reconciliation yes-only basis={yes_basis} acquired={yes_acq:.2f}")
        log(msg)
        tg(msg)
        return "partial_yes"

    if no_final in ("filled", "partial") and yes_final not in ("filled", "partial"):
        if yes_oid:
            maker_cancel_order(yes_oid)
        msg = (
            f"🟠🌮 [MAKER] [ETH] | HELD_SIDE | P&L: $0.00 | "
            f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Holding NO"
        )
        log(f"[ETH-MAKER] final reconciliation no-only basis={no_basis} acquired={no_acq:.2f}")
        log(msg)
        tg(msg)
        return "partial_no"

    # If either side is still open/unknown, do not claim no-fill yet.
    pending_states = {"open", "unknown"}
    if yes_final in pending_states or no_final in pending_states:
        if yes_final == "open" and yes_oid:
            maker_cancel_order(yes_oid)
        if no_final == "open" and no_oid:
            maker_cancel_order(no_oid)
        time.sleep(3)
        yes_final2, yes_acq2, yes_basis2 = reconcile_order_fill(str(token_ids[0]), yes_oid, yes_baseline, shares)
        no_final2, no_acq2, no_basis2 = reconcile_order_fill(str(token_ids[1]), no_oid, no_baseline, shares)

        if yes_final2 in ("filled", "partial") and no_final2 in ("filled", "partial"):
            pnl = round(shares * edge, 2)
            sign = "+" if pnl >= 0 else "-"
            msg = (
                f"🟢🌮 [MAKER] [ETH] | DONE | P&L: {sign}${abs(pnl):.2f} | "
                f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f}"
            )
            log(f"[ETH-MAKER] grace reconciliation upgraded PENDING->DONE yes={yes_basis2}:{yes_acq2:.2f} no={no_basis2}:{no_acq2:.2f}")
            log(msg)
            tg(msg)
            return "both_filled"

        if yes_final2 in ("filled", "partial") and no_final2 not in ("filled", "partial"):
            msg = (
                f"🟠🌮 [MAKER] [ETH] | HELD_SIDE | P&L: $0.00 | "
                f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Holding YES"
            )
            log(f"[ETH-MAKER] grace reconciliation yes-only basis={yes_basis2} acquired={yes_acq2:.2f}")
            log(msg)
            tg(msg)
            return "partial_yes"

        if no_final2 in ("filled", "partial") and yes_final2 not in ("filled", "partial"):
            msg = (
                f"🟠🌮 [MAKER] [ETH] | HELD_SIDE | P&L: $0.00 | "
                f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Holding NO"
            )
            log(f"[ETH-MAKER] grace reconciliation no-only basis={no_basis2} acquired={no_acq2:.2f}")
            log(msg)
            tg(msg)
            return "partial_no"

        msg = (
            f"🟡🌮 [MAKER] [ETH] | PENDING_RECON | "
            f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — Awaiting exchange confirmation"
        )
        log(
            f"[ETH-MAKER] pending reconciliation yes={yes_final}->{yes_final2} basis={yes_basis2} acq={yes_acq2:.2f} "
            f"no={no_final}->{no_final2} basis={no_basis2} acq={no_acq2:.2f}"
        )
        log(msg)
        tg(msg)
        return "pending_recon"

    # Truly no fill — both sides resolved non-filled without pending exchange state
    msg = (
        f"⚪🌮 [MAKER] [ETH] | NO_FILL | "
        f"YES ${yes_bid:.4f} + NO ${no_bid:.4f} = ${combined_bid:.4f} — No fill evidence after reconciliation"
    )
    log(f"[ETH-MAKER] final no-fill yes={yes_final}:{yes_basis}:{yes_acq:.2f} no={no_final}:{no_basis}:{no_acq:.2f}")
    log(msg)
    tg(msg)
    return "no_fill"


def check_maker_exec_opportunity(market: dict, secs_left: int, state: dict) -> bool:
    """
    Terminal-bot style maker entry at window open.
    Pricing: find cheap side (lower bid), place bids using bid+1tick logic,
    derive expensive side from MAKER_EXEC_THRESHOLD - cheapBid.
    Retries up to MAKER_MAX_RETRIES within the entry window.
    """
    token_ids = market.get("token_ids") or []
    if len(token_ids) < 2:
        return False

    # Only execute once per window
    window_ts = int(state.get("last_window_ts") or 0)
    if state.get("last_maker_exec_window") == window_ts:
        return False

    # Calculate elapsed time since window opened
    elapsed = WINDOW_SEC - secs_left

    # Must be within the maker entry window
    if elapsed < MAKER_ENTRY_DELAY_SEC or elapsed > MAKER_ENTRY_WINDOW_SEC:
        return False

    log(f"[ETH-MAKER] window-open entry mode | elapsed={elapsed}s")

    for attempt in range(1, MAKER_MAX_RETRIES + 1):
        if attempt > 1:
            time.sleep(5)

        # Re-check timing
        _, _, sl_now = get_current_slug()
        elapsed_now = WINDOW_SEC - sl_now
        if elapsed_now > MAKER_ENTRY_WINDOW_SEC:
            log(f"[ETH-MAKER] entry window expired on attempt {attempt}")
            break

        yes_book = fetch_book(str(token_ids[0]))
        no_book = fetch_book(str(token_ids[1]))

        yes_best_bid = yes_book.get("best_bid")
        no_best_bid = no_book.get("best_bid")
        yes_best_ask = yes_book.get("best_ask")
        no_best_ask = no_book.get("best_ask")

        if any(v is None for v in [yes_best_bid, no_best_bid, yes_best_ask, no_best_ask]):
            log(f"[ETH-MAKER] attempt {attempt}: incomplete book data")
            continue

        yes_best_bid = float(yes_best_bid)
        no_best_bid = float(no_best_bid)
        yes_best_ask = float(yes_best_ask)
        no_best_ask = float(no_best_ask)

        tick = 0.01

        # Determine cheap side (lower best bid)
        cheap_side = "yes" if yes_best_bid <= no_best_bid else "no"
        cheap_best_bid = yes_best_bid if cheap_side == "yes" else no_best_bid
        cheap_ask = yes_best_ask if cheap_side == "yes" else no_best_ask
        expensive_ask = no_best_ask if cheap_side == "yes" else yes_best_ask

        # Cheap bid = bestBid + 1 tick, capped below ask - 2 ticks
        cheap_bid = _round_down(cheap_best_bid + tick, 2)
        if cheap_bid >= cheap_ask - tick:
            cheap_bid = _round_down(cheap_ask - 2 * tick, 2)

        # Range check on cheap side
        if cheap_bid < MAKER_MIN_PRICE or cheap_bid > MAKER_MAX_PRICE:
            log(f"[ETH-MAKER] attempt {attempt}: cheap {cheap_side} bid ${cheap_bid:.2f} out of range [{MAKER_MIN_PRICE}-{MAKER_MAX_PRICE}]")
            continue

        # Expensive side = threshold - cheapBid, capped below ask - 2 ticks
        exp_bid = _round_down(MAKER_EXEC_THRESHOLD - cheap_bid, 2)
        if exp_bid >= expensive_ask - tick:
            exp_bid = _round_down(expensive_ask - 2 * tick, 2)

        if exp_bid <= 0 or exp_bid >= 1:
            log(f"[ETH-MAKER] attempt {attempt}: expensive bid ${exp_bid:.2f} out of bounds")
            continue

        # Map back to YES/NO
        yes_bid = cheap_bid if cheap_side == "yes" else exp_bid
        no_bid = exp_bid if cheap_side == "yes" else cheap_bid

        combined_bid = round(yes_bid + no_bid, 4)

        if combined_bid > MAKER_EXEC_THRESHOLD:
            log(f"[ETH-MAKER] attempt {attempt}: combined ${combined_bid:.4f} > threshold {MAKER_EXEC_THRESHOLD}")
            continue

        # Success — execute
        edge = round(1.0 - combined_bid, 4)
        log(f"[ETH-MAKER] attempt {attempt}: YES ${yes_bid:.2f} + NO ${no_bid:.2f} = ${combined_bid:.4f} | edge +${edge:.4f}")
        result = execute_maker_pair(market, yes_bid, no_bid, combined_bid, secs_left)

        # Mark window as executed (regardless of result)
        state["last_maker_exec_window"] = window_ts
        state.setdefault("maker_trades", []).append({
            "ts": int(time.time()),
            "window_ts": window_ts,
            "yes_bid": yes_bid,
            "no_bid": no_bid,
            "combined_bid": combined_bid,
            "result": result,
            "entry_mode": "window_open",
        })
        state["maker_trades"] = state["maker_trades"][-100:]
        save_state(state)
        return True

    log(f"[ETH-MAKER] all {MAKER_MAX_RETRIES} attempts exhausted — no entry this window")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# SCALP STRATEGY — buy cheap side in last 120s, sell at target, stop loss
# ══════════════════════════════════════════════════════════════════════════════

def check_scalp_opportunity(market: dict, secs_left: int, state: dict):
    """
    Monitor the last SCALP_SCAN_SEC seconds of each 15-min window.
    If any side's best ask <= SCALP_ENTRY_MAX, BUY and place SELL limit at SCALP_TARGET.
    Stop loss at SCALP_STOP. Time stop at market close.
    """
    if not SCALP_ENABLED:
        return

    window_ts = int(state.get("last_window_ts") or 0)

    # Only trade once per window
    if state.get("last_scalp_window") == window_ts:
        return

    # Only active in last SCALP_SCAN_SEC seconds
    if secs_left > SCALP_SCAN_SEC or secs_left < 2:
        return

    token_ids = market.get("token_ids") or []
    if len(token_ids) < 2:
        return

    outcomes = market.get("outcomes") or ["Up", "Down"]
    sim_mode = DRY_RUN or not MAKER_EXEC

    # Check both sides for cheap asks
    for i, tid in enumerate(token_ids[:2]):
        side_name = outcomes[i] if i < len(outcomes) else ("YES" if i == 0 else "NO")
        book = fetch_book(str(tid))
        best_ask = book.get("best_ask")
        if best_ask is None:
            continue
        best_ask = float(best_ask)

        if best_ask > SCALP_ENTRY_MAX:
            continue
        if best_ask < 0.10:
            log(f"[SCALP] skipping {side_name} ask={best_ask:.4f} — below minimum 0.10 (junk price)")
            continue

        # Found a cheap side — calculate shares
        shares = _round_down(SCALP_SIZE / best_ask, 2)
        if shares < 5.0:
            shares = 5.0
        entry_price = best_ask
        entry_cost = round(shares * entry_price, 2)

        # Mark window as traded
        state["last_scalp_window"] = window_ts
        save_state(state)

        entry_label = f"BUY {side_name}"
        sim_prefix = "🧪 [SIM] " if sim_mode else ""

        # Entry notification
        entry_msg = (
            f"{sim_prefix}📍🌮 [SCALP] [ETH] entering {entry_label} @ ${entry_price:.2f} "
            f"| target ${SCALP_TARGET:.2f} | stop ${SCALP_STOP:.2f} | size ${SCALP_SIZE:.0f}"
        )
        log(entry_msg)
        tg(entry_msg)

        if sim_mode:
            # Simulate outcome
            import random
            time.sleep(3 + random.random() * 8)
            roll = random.random()
            if roll < 0.55:
                # Win — sold at target
                sell_price = SCALP_TARGET
                pnl = round(shares * (sell_price - entry_price), 2)
                pct = round((pnl / entry_cost) * 100, 2)
                result_msg = (
                    f"🧪 [SIM] 🟢🌮 [SIM] [ETH] SCALP WIN | {entry_label} ${entry_price:.2f} → "
                    f"SELL ${sell_price:.2f} | P&L: +${pnl:.2f} | +{pct:.2f}%"
                )
            elif roll < 0.80:
                # Stop loss hit
                sell_price = SCALP_STOP
                pnl = round(shares * (sell_price - entry_price), 2)
                pct = round((pnl / entry_cost) * 100, 2)
                result_msg = (
                    f"🧪 [SIM] 🔴🌮 [SIM] [ETH] SCALP STOP_LOSS | {entry_label} ${entry_price:.2f} → "
                    f"SELL ${sell_price:.2f} | P&L: -${abs(pnl):.2f} | {pct:.2f}%"
                )
            else:
                # Time stop — market price at close (simulated between stop and entry)
                sell_price = round(entry_price + random.uniform(-0.03, 0.02), 2)
                pnl = round(shares * (sell_price - entry_price), 2)
                sign = "+" if pnl >= 0 else "-"
                pct = round((pnl / entry_cost) * 100, 2)
                result_msg = (
                    f"🧪 [SIM] 🟠🌮 [SIM] [ETH] SCALP TIME_STOP | {entry_label} ${entry_price:.2f} → "
                    f"SELL ${sell_price:.2f} | P&L: {sign}${abs(pnl):.2f} | {pct:.2f}%"
                )
            log(result_msg)
            tg(result_msg)

            # Record to state
            state.setdefault("scalp_trades", []).append({
                "ts": int(time.time()),
                "window_ts": window_ts,
                "side": side_name,
                "entry": entry_price,
                "exit": sell_price,
                "pnl": pnl,
                "sim": True,
            })
            state["scalp_trades"] = state["scalp_trades"][-100:]
            save_state(state)
            return

        # ── LIVE execution ──
        # Step 1: Place BUY limit at entry price
        baseline = get_token_position_size(str(tid))
        buy_result = maker_place_order(str(tid), shares, entry_price)
        if not buy_result.get("success"):
            log(f"[SCALP] BUY failed — aborting")
            state["last_scalp_window"] = None  # Allow retry
            save_state(state)
            return

        buy_oid = buy_result.get("order_id")

        # Step 2: Wait for fill with reconciliation
        filled = False
        poll_start = time.time()
        while (time.time() - poll_start) < min(secs_left - 2, FILL_TIMEOUT_SEC):
            s, acquired, basis = reconcile_order_fill(str(tid), buy_oid, baseline, shares)
            if s in ("filled", "partial"):
                filled = True
                shares = max(shares, acquired or shares)
                log(f"[SCALP] BUY reconciled basis={basis} acquired={acquired:.2f}")
                break
            if s == "cancelled":
                break
            time.sleep(FILL_POLL_SEC)

        if not filled:
            # Final reconciliation before calling it no-fill
            s, acquired, basis = reconcile_order_fill(str(tid), buy_oid, baseline, shares)
            if s in ("filled", "partial"):
                filled = True
                shares = max(shares, acquired or shares)
                log(f"[SCALP] BUY final reconciliation rescued no-fill basis={basis} acquired={acquired:.2f}")
            else:
                maker_cancel_order(buy_oid)
                log(f"[SCALP] BUY not filled — no confirmed shares")
                state["last_scalp_window"] = None
                save_state(state)
                return

        log(f"[SCALP] BUY filled @ ${entry_price:.2f} for {shares:.2f} shares {side_name}")

        # Step 3: Place SELL limit at target
        sell_result = maker_place_order(str(tid), shares, SCALP_TARGET)
        sell_oid = sell_result.get("order_id") if sell_result.get("success") else None

        if not sell_oid:
            log(f"[SCALP] SELL order failed — will use time stop")

        # Step 4: Monitor for target fill or stop loss
        remaining = max(2, secs_left - (time.time() - poll_start) - 2)
        monitor_start = time.time()
        exited = False

        while (time.time() - monitor_start) < remaining:
            # Check if sell filled at target
            if sell_oid:
                ss = maker_check_order(sell_oid)
                if ss == "filled":
                    pnl = round(shares * (SCALP_TARGET - entry_price), 2)
                    pct = round((pnl / entry_cost) * 100, 2)
                    msg = (
                        f"🟢🌮 [SCALP] [ETH] WIN | {entry_label} ${entry_price:.2f} → "
                        f"SELL ${SCALP_TARGET:.2f} | P&L: +${pnl:.2f} | +{pct:.2f}%"
                    )
                    log(msg)
                    tg(msg)
                    exited = True
                    break
                if ss == "cancelled":
                    sell_oid = None

            # Check stop loss — re-fetch book
            if (time.time() - monitor_start) % 5 < CHECK_SEC + 0.5:
                book_now = fetch_book(str(tid))
                current_ask = book_now.get("best_ask")
                current_bid = book_now.get("best_bid")
                mid = None
                if current_ask and current_bid:
                    mid = (float(current_ask) + float(current_bid)) / 2
                elif current_ask:
                    mid = float(current_ask)

                if mid is not None and mid <= SCALP_STOP:
                    # Stop loss triggered — cancel sell, market sell
                    if sell_oid:
                        maker_cancel_order(sell_oid)
                    # Market sell via executor
                    cmd = [
                        str(VENV_PY),
                        str(WORK_DIR / "scripts" / "polymarket_executor.py"),
                        "sell", str(tid), str(shares), str(SCALP_STOP),
                    ]
                    try:
                        subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    except Exception as e:
                        log(f"[SCALP] stop-loss sell error: {e}")
                    pnl = round(shares * (SCALP_STOP - entry_price), 2)
                    pct = round((pnl / entry_cost) * 100, 2)
                    msg = (
                        f"🔴🌮 [SCALP] [ETH] STOP_LOSS | {entry_label} ${entry_price:.2f} → "
                        f"SELL ${SCALP_STOP:.2f} | P&L: -${abs(pnl):.2f} | {pct:.2f}%"
                    )
                    log(msg)
                    tg(msg)
                    exited = True
                    break

            time.sleep(CHECK_SEC)

        # Time stop — if still holding, cancel sell and market sell
        if not exited:
            if sell_oid:
                maker_cancel_order(sell_oid)
            # Get current best bid for exit price estimate
            book_end = fetch_book(str(tid))
            exit_bid = book_end.get("best_bid")
            exit_price = float(exit_bid) if exit_bid else entry_price
            # Market sell
            cmd = [
                str(VENV_PY),
                str(WORK_DIR / "scripts" / "polymarket_executor.py"),
                "sell", str(tid), str(shares), str(exit_price),
            ]
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            except Exception as e:
                log(f"[SCALP] time-stop sell error: {e}")
            pnl = round(shares * (exit_price - entry_price), 2)
            sign = "+" if pnl >= 0 else "-"
            pct = round((pnl / entry_cost) * 100, 2) if entry_cost else 0
            msg = (
                f"🟠🌮 [SCALP] [ETH] TIME_STOP | {entry_label} ${entry_price:.2f} → "
                f"SELL ${exit_price:.2f} | P&L: {sign}${abs(pnl):.2f} | {pct:.2f}%"
            )
            log(msg)
            tg(msg)
            exit_price_used = exit_price
        else:
            exit_price_used = SCALP_TARGET

        # Record trade
        state.setdefault("scalp_trades", []).append({
            "ts": int(time.time()),
            "window_ts": window_ts,
            "side": side_name,
            "entry": entry_price,
            "exit": exit_price_used,
            "pnl": pnl,
            "sim": False,
        })
        state["scalp_trades"] = state["scalp_trades"][-100:]
        save_state(state)
        return  # Only one scalp per window


def main():
    state = load_state()
    if NOTIFY_START:
        mode = "DRY-RUN" if DRY_RUN else "LIVE"
        exec_status = "ON" if MAKER_EXEC else "OFF"
        scalp_status = "ON" if SCALP_ENABLED else "OFF"
        tg(
            f"🔔 [ETH-DISCOUNT] {mode} watcher started | threshold<={COMBINED_THRESHOLD:.3f}\n"
            f"Maker entry: first {MAKER_ENTRY_WINDOW_SEC}s (delay {MAKER_ENTRY_DELAY_SEC}s) | retries={MAKER_MAX_RETRIES}\n"
            f"Scalp zone: last {ACTIVE_LAST_SEC}s | maker_exec={exec_status} | scalp={scalp_status}"
        )
    log(
        f"[ETH-DISCOUNT] STARTING dry={DRY_RUN} maker_exec={MAKER_EXEC} "
        f"threshold<={COMBINED_THRESHOLD:.3f} maker_threshold<{MAKER_EXEC_THRESHOLD:.3f} "
        f"maker_entry=first_{MAKER_ENTRY_WINDOW_SEC}s_delay_{MAKER_ENTRY_DELAY_SEC}s "
        f"last={ACTIVE_LAST_SEC}s min_left={MIN_LEFT_SEC}s"
    )

    while True:
        try:
            slug, window_ts, secs_left = get_current_slug()
            reset_window_state(state, window_ts)

            # Determine which phase we're in
            elapsed = WINDOW_SEC - secs_left  # seconds since window opened
            in_maker_entry = (MAKER_ENTRY_DELAY_SEC <= elapsed <= MAKER_ENTRY_WINDOW_SEC)
            in_scalp_zone = (secs_left <= ACTIVE_LAST_SEC and secs_left >= MIN_LEFT_SEC)

            if not in_maker_entry and not in_scalp_zone:
                time.sleep(CHECK_SEC)
                continue

            market = get_market(slug)
            if not market or market.get("closed"):
                time.sleep(CHECK_SEC)
                continue

            # ── Window-open maker entry (first 45s) ──
            if in_maker_entry:
                check_maker_exec_opportunity(market, secs_left, state)
                if not in_scalp_zone:
                    time.sleep(CHECK_SEC)
                    continue

            # ── Scalp + shadow zone (last 180s) ──
            if in_scalp_zone:
                token_ids = market.get("token_ids") or []
                if len(token_ids) >= 2:
                    from polymarket_clob_pricing import fetch_book as _fb
                    yb = _fb(str(token_ids[0]))
                    nb = _fb(str(token_ids[1]))
                    ya = yb.get("best_ask")
                    na = nb.get("best_ask")
                    gy = market.get("yes_price")
                    gn = market.get("no_price")
                    ybid = yb.get("best_bid")
                    nbid = nb.get("best_bid")
                    if ya is not None and na is not None:
                        clob_comb_ask = round(float(ya) + float(na), 4)
                        clob_comb_bid = round((float(ybid) if ybid else 0) + (float(nbid) if nbid else 0), 4)
                        gamma_comb = round((gy or 0) + (gn or 0), 4)
                        log(f"[SHADOW] secs_left={secs_left} gamma_comb={gamma_comb:.4f} ask_comb={clob_comb_ask:.4f} bid_comb={clob_comb_bid:.4f} YES_bid={ybid} YES_ask={ya} NO_bid={nbid} NO_ask={na}")
                        # Maker-side shadow
                        if clob_comb_bid < 1.0 and clob_comb_bid > 0:
                            maker_key = f"maker|{slug}|{clob_comb_bid:.4f}"
                            if maker_key != state.get("last_maker_shadow_key"):
                                state["last_maker_shadow_key"] = maker_key
                                state.setdefault("maker_shadows", []).append({
                                    "ts": int(time.time()),
                                    "window_ts": window_ts,
                                    "secs_left": secs_left,
                                    "gamma_combined": gamma_comb,
                                    "bid_combined": clob_comb_bid,
                                    "ask_combined": clob_comb_ask,
                                    "yes_bid": round(float(ybid), 4) if ybid else None,
                                    "yes_ask": round(float(ya), 4),
                                    "no_bid": round(float(nbid), 4) if nbid else None,
                                    "no_ask": round(float(na), 4),
                                })
                                state["maker_shadows"] = state["maker_shadows"][-100:]
                                save_state(state)
                                maker_edge = round(1.0 - clob_comb_bid, 4)
                                log(f"[MAKER-SHADOW] bid_comb={clob_comb_bid:.4f} edge=+{maker_edge:.4f} | YES bid={ybid} ask={ya} | NO bid={nbid} ask={na} | {secs_left}s left")
                                tg(
                                    f"🔬 [ETH-DISCOUNT] MAKER shadow\n"
                                    f"Combined BID: {clob_comb_bid:.4f} | Edge: +{maker_edge:.4f}\n"
                                    f"YES bid/ask: {ybid}/{ya} | NO bid/ask: {nbid}/{na}\n"
                                    f"Time left: {secs_left}s | Zero fees" f"\n"
                                    f"Combined ASK: {clob_comb_ask:.4f} (taker would lose)"
                                )
                        # Taker-side shadow
                        if clob_comb_ask <= SHADOW_THRESHOLD and clob_comb_ask > COMBINED_THRESHOLD:
                            shadow_key = f"{slug}|{clob_comb_ask:.4f}"
                            if shadow_key != state.get("last_shadow_key"):
                                state["last_shadow_key"] = shadow_key
                                state.setdefault("shadows", []).append({
                                    "ts": int(time.time()),
                                    "window_ts": window_ts,
                                    "secs_left": secs_left,
                                    "gamma_combined": gamma_comb,
                                    "clob_combined": clob_comb_ask,
                                    "yes_ask": round(float(ya), 4),
                                    "no_ask": round(float(na), 4),
                                })
                                state["shadows"] = state["shadows"][-100:]
                                save_state(state)
                                log(f"[SHADOW-NEAR] combined={clob_comb_ask:.4f} edge={1.0 - clob_comb_ask:.4f} | YES={ya} NO={na} | {secs_left}s left")

                        # ── Scalp: check for cheap asks in last 120s ──
                        check_scalp_opportunity(market, secs_left, state)

                # Discount alerts
                opp = build_opportunity(market, secs_left)
                if opp:
                    key = opportunity_key(opp)
                    if key != state.get("last_alert_key"):
                        state["last_alert_key"] = key
                        state.setdefault("opportunities", []).append(
                            {"ts": int(time.time()), "window_ts": window_ts, **opp}
                        )
                        state["opportunities"] = state["opportunities"][-50:]
                        save_state(state)
                        msg = format_alert(opp)
                        log(msg.replace("\n", " | "))
                        tg(msg)

            time.sleep(CHECK_SEC)
        except KeyboardInterrupt:
            log("[ETH-DISCOUNT] stopped by user")
            raise
        except Exception as e:
            log(f"[ETH-DISCOUNT] loop error: {e}")
            time.sleep(max(3, CHECK_SEC))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
