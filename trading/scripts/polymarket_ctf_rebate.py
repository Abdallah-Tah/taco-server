#!/usr/bin/env python3
"""
polymarket_ctf_rebate.py
========================
CTF-based maker rebate bot for Polymarket BTC/ETH/SOL 15-minute markets.

PROFIT MODEL: Earn daily USDC rebates from Polymarket for providing maker liquidity.

Strategy:
  1. At window open: CTF split USDC → YES+NO tokens at $0.50 each
  2. Immediately post maker SELL orders at current best ask (front of queue)
  3. Takers fill your orders → you earn 20% of their 7.2% taker fee daily
  4. Before window close: merge unsold paired tokens → recover USDC (zero loss)
  5. Resolved unpaired tokens → auto-redeem daemon handles

Economics per fill (10 shares @ $0.70):
  Taker fee = 10 × 0.072 × 0.70 × 0.30 = $0.151
  Your rebate = 20% × $0.151 = ~$0.03 per fill
  With 50+ fills/day across 3 assets → $1.50+/day in rebates alone
  + Spread capture when both sides sell above $0.50

Config (env vars):
  CTF_REBATE_DRY_RUN=true            # default true
  CTF_REBATE_ASSETS=btc,eth,sol      # assets to trade
  CTF_REBATE_SIZE=5                  # USDC to split per window per asset
  CTF_REBATE_SELL_OFFSET=0.01        # sell at best_ask - offset (penny improvement)
  CTF_REBATE_MERGE_SEC=30            # merge N sec before window close
  CTF_REBATE_MAX_WINDOWS=0           # 0 = unlimited
"""

import os, sys, json, time, subprocess, logging
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Paths ─────────────────────────────────────────────────────────────────────
WORK_DIR  = Path("/home/abdaltm86/.openclaw/workspace/trading")
SECRETS_F = Path.home() / ".config" / "openclaw" / "secrets.env"
STATE_F   = WORK_DIR / ".poly_ctf_rebate_state.json"
LOG_F     = Path("/tmp/polymarket_ctf_rebate.log")
VENV_PY   = WORK_DIR / ".polymarket-venv" / "bin" / "python3"

sys.path.insert(0, str(WORK_DIR / "scripts"))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_F)],
    level=logging.INFO, format="%(message)s",
)
def log(msg: str):
    logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

# ── Secrets ───────────────────────────────────────────────────────────────────
def load_secrets() -> dict:
    s = {}
    if SECRETS_F.exists():
        for line in SECRETS_F.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                s[k.strip()] = v.strip()
    s.update(os.environ)
    return s

ENV = load_secrets()

def _bool(k,d): return ENV.get(k,str(d)).lower() in ("1","true","yes","on")
def _float(k,d):
    try: return float(ENV.get(k,d))
    except: return d
def _int(k,d):
    try: return int(ENV.get(k,d))
    except: return d
def _list(k,d): return [x.strip() for x in ENV.get(k,d).split(",") if x.strip()]

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN      = _bool("CTF_REBATE_DRY_RUN", True)
SCAN_ONLY    = _bool("CTF_REBATE_SCAN_ONLY", True)
ASSETS       = _list("CTF_REBATE_ASSETS", "btc,eth,sol")
SIZE         = _float("CTF_REBATE_SIZE", 5.0)
SELL_OFFSET  = _float("CTF_REBATE_SELL_OFFSET", 0.01)  # penny improvement
MERGE_SEC    = _int("CTF_REBATE_MERGE_SEC", 30)
MAX_WINDOWS  = _int("CTF_REBATE_MAX_WINDOWS", 0)
ENTRY_SEC    = _int("CTF_REBATE_ENTRY_SEC", 15)
POLL_SEC     = _int("CTF_REBATE_POLL_SEC", 10)
MIN_COMBINED = _float("CTF_REBATE_MIN_COMBINED", 1.03)  # sane version: require stronger combined edge
MIN_BID_FLOOR = _float("CTF_REBATE_MIN_BID_FLOOR", 0.05)  # skip junk books
REQUIRE_TIGHT_BOTH = _bool("CTF_REBATE_REQUIRE_TIGHT_BOTH", True)

PRIVATE_KEY  = ENV.get("POLYMARKET_PRIVATE_KEY", "")
FUNDER       = ENV.get("POLYMARKET_FUNDER", "")
WINDOW_SEC   = 900

# ── Contracts (Polygon) ──────────────────────────────────────────────────────
CTF_ADDRESS  = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPCS = ["https://polygon-bor-rpc.publicnode.com","https://polygon.drpc.org","https://rpc.ankr.com/polygon"]

CTF_ABI = [
    {"name":"splitPosition","type":"function","inputs":[
        {"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},
        {"name":"conditionId","type":"bytes32"},{"name":"partition","type":"uint256[]"},
        {"name":"amount","type":"uint256"}],"outputs":[],"stateMutability":"nonpayable"},
    {"name":"mergePositions","type":"function","inputs":[
        {"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},
        {"name":"conditionId","type":"bytes32"},{"name":"partition","type":"uint256[]"},
        {"name":"amount","type":"uint256"}],"outputs":[],"stateMutability":"nonpayable"},
    {"name":"balanceOf","type":"function","inputs":[
        {"name":"account","type":"address"},{"name":"id","type":"uint256"}],
        "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
]
ERC20_ABI = [
    {"name":"approve","type":"function","inputs":[
        {"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
        "outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable"},
    {"name":"allowance","type":"function","inputs":[
        {"name":"owner","type":"address"},{"name":"spender","type":"address"}],
        "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
]

# ── Telegram ──────────────────────────────────────────────────────────────────
try: _cfg = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text())
except: _cfg = {}
TG_TOKEN  = ENV.get("TELEGRAM_TOKEN") or _cfg.get("channels",{}).get("telegram",{}).get("botToken","")
CHAT_ID   = ENV.get("CHAT_ID", "-1003948211258")
TOPIC_ID  = ENV.get("TOPIC_ID", "3")

def tg(msg: str):
    if not TG_TOKEN or not CHAT_ID: return
    try:
        import urllib.request
        data = json.dumps({"chat_id":CHAT_ID,"text":msg,"message_thread_id":int(TOPIC_ID)}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",data=data,headers={"Content-Type":"application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"[TG] failed: {e}")

# ── Web3 helpers ──────────────────────────────────────────────────────────────
_w3 = None
def get_web3():
    global _w3
    if _w3 and _w3.is_connected(): return _w3
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    for url in POLYGON_RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout":30}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected(): _w3 = w3; return w3
        except: pass
    raise RuntimeError("No Polygon RPC")

def send_tx(fn, desc: str) -> str|None:
    if DRY_RUN:
        log(f"[SIM] {desc}")
        return f"sim-{int(time.time())}"
    try:
        from web3 import Web3
        w3 = get_web3()
        acct = w3.eth.account.from_key(PRIVATE_KEY)
        # Always fetch fresh nonce from pending state
        nonce = w3.eth.get_transaction_count(acct.address, 'pending')
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": nonce,
            "gasPrice": int(w3.eth.gas_price * 1.5),
            "gas": 500_000,
        })
        signed = acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            log(f"[CTF] TX reverted: {desc}"); return None
        log(f"[CTF] TX ok: {desc} | {tx_hash.hex()}")
        return tx_hash.hex()
    except Exception as e:
        log(f"[CTF] TX err ({desc}): {e}"); return None

def ensure_approval(amount: float) -> bool:
    if DRY_RUN: return True
    from web3 import Web3
    w3 = get_web3()
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    if usdc.functions.allowance(Web3.to_checksum_address(FUNDER), Web3.to_checksum_address(CTF_ADDRESS)).call() >= int(amount * 1e6):
        return True
    return send_tx(usdc.functions.approve(Web3.to_checksum_address(CTF_ADDRESS), 2**256-1), "approve USDC→CTF") is not None

def ensure_erc1155_approval() -> bool:
    """Approve CTF_EXCHANGE to transfer our ERC1155 tokens (needed for CLOB sells)."""
    if DRY_RUN: return True
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    from web3 import Web3
    w3 = get_web3()
    ERC1155_ABI_FULL = [
        {"name":"isApprovedForAll","type":"function","inputs":[
            {"name":"account","type":"address"},{"name":"operator","type":"address"}],
            "outputs":[{"name":"","type":"bool"}],"stateMutability":"view"},
        {"name":"setApprovalForAll","type":"function","inputs":[
            {"name":"operator","type":"address"},{"name":"approved","type":"bool"}],
            "outputs":[],"stateMutability":"nonpayable"},
    ]
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_ABI_FULL)
    if ctf.functions.isApprovedForAll(Web3.to_checksum_address(FUNDER), Web3.to_checksum_address(CTF_EXCHANGE)).call():
        return True
    return send_tx(ctf.functions.setApprovalForAll(Web3.to_checksum_address(CTF_EXCHANGE), True), "approve ERC1155→Exchange") is not None

def ctf_split(condition_id: str, amount: float) -> bool:
    if DRY_RUN:
        log(f"[SIM] splitPosition ${amount:.2f} → YES+NO"); return True
    from web3 import Web3
    w3 = get_web3()
    if not ensure_approval(amount): return False
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    cid = bytes.fromhex(condition_id.replace("0x","").zfill(64))
    return send_tx(ctf.functions.splitPosition(
        Web3.to_checksum_address(USDC_ADDRESS), b"\x00"*32, cid, [1,2], int(amount*1e6)
    ), f"split ${amount:.2f}") is not None

def ctf_merge(condition_id: str, shares: float) -> bool:
    if DRY_RUN:
        log(f"[SIM] mergePositions {shares:.2f} YES+NO → USDC"); return True
    from web3 import Web3
    w3 = get_web3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    cid = bytes.fromhex(condition_id.replace("0x","").zfill(64))
    floor = int(shares * 1e6) / 1e6  # floor to 6 decimals
    return send_tx(ctf.functions.mergePositions(
        Web3.to_checksum_address(USDC_ADDRESS), b"\x00"*32, cid, [1,2], int(floor*1e6)
    ), f"merge {floor:.2f}") is not None

def get_token_balance(token_id: str) -> float:
    if DRY_RUN: return SIZE  # simulate full balance after split
    from web3 import Web3
    w3 = get_web3()
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    raw = ctf.functions.balanceOf(Web3.to_checksum_address(FUNDER), int(token_id)).call()
    return raw / 1e6

# ── CLOB helpers ──────────────────────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
HEARTBEAT_ID = ""

def get_market(asset, window_ts):
    slug = f"{asset}-updown-15m-{window_ts}"
    try:
        r = requests.get(f"{GAMMA_API}/markets?slug={slug}", timeout=10)
        if r.ok:
            data = r.json()
            items = data if isinstance(data, list) else data.get("markets",[])
            if items:
                m = items[0]
                tids = m.get("clobTokenIds") or m.get("token_ids") or []
                if isinstance(tids, str):
                    try: tids = json.loads(tids)
                    except: tids = []
                m["token_ids"] = tids
                return m
    except: pass
    return None

def get_books(token_ids):
    """Return live CLOB book snapshots for YES and NO."""
    try:
        from polymarket_clob_pricing import fetch_book
        yb = fetch_book(str(token_ids[0])) or {}
        nb = fetch_book(str(token_ids[1])) or {}
        return yb, nb
    except:
        return {}, {}


def choose_post_only_sell_price(book: dict, floor_price: float, tick_size: float) -> float | None:
    """Choose a post-only SELL price that will not cross the best bid.
    For a split YES+NO pair, profit should be evaluated on the combined pair,
    not by forcing each individual side above $0.50.
    """
    try:
        best_bid = float(book.get("best_bid") or 0)
        best_ask = float(book.get("best_ask") or 1)
        tick = float(book.get("tick_size") or tick_size or 0.01)
        candidate = round(best_ask - tick, 2)
        min_post_only = round(best_bid + tick, 2)
        price = max(floor_price, candidate, min_post_only)
        price = round(min(0.99, price), 2)
        if price <= round(best_bid, 2):
            return None
        if price > 0.99:
            return None
        return price
    except:
        return None

def clob_maker_sell(token_id: str, shares: float, price: float) -> dict:
    """Place post-only GTC maker SELL via executor (Tor), per Polymarket docs."""
    if DRY_RUN:
        log(f"[SIM] SELL {shares:.2f} @ {price:.4f} token=...{token_id[-8:]}")
        return {"success": True, "order_id": f"sim-s-{int(time.time())}", "sim": True, "status": "live"}
    try:
        # CTF rebate path uses its own supervised size; do not inherit the normal engine $10 cap.
        order_notional = round(float(shares) * float(price), 2)
        max_override = max(10.0, order_notional + 5.0)
        cmd = [str(VENV_PY), str(WORK_DIR/"scripts"/"polymarket_executor.py"),
               "sell", token_id, str(shares), f"{price:.4f}", "--post-only", f"--max-trade-size={max_override:.2f}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        order_id = ""
        posted = None
        for line in r.stdout.splitlines():
            for part in line.split():
                if part.startswith("order_id="):
                    candidate = part.split("=",1)[1]
                    if candidate and candidate not in ("none", "null"):
                        order_id = candidate
                        break
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    posted = json.loads(s)
                except:
                    pass
        if isinstance(posted, dict) and not order_id:
            order_id = posted.get("order_id") or posted.get("orderID") or ""
        success = bool(order_id)
        status = (posted or {}).get("status", "") if isinstance(posted, dict) else ""
        log(f"[CLOB] SELL {shares:.2f}@{price:.4f} order_id={order_id or 'FAILED'} status={status or 'unknown'} rc={r.returncode}")
        if not success:
            log(f"[CLOB] sell stderr: {r.stderr[:300]}")
        return {"success": success, "order_id": order_id, "status": status, "raw": posted}
    except Exception as e:
        log(f"[CLOB] sell error: {e}")
        return {"success": False, "order_id": "", "status": "error"}

def clob_cancel(order_id: str) -> bool:
    if not order_id or order_id.startswith("sim-"): return True
    try:
        cmd = [str(VENV_PY), str(WORK_DIR/"scripts"/"polymarket_executor.py"), "cancel", order_id]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode == 0
    except:
        return False


def get_clob_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    import httpx
    from py_clob_client.http_helpers import helpers as _h
    _h._http_client = httpx.Client(proxy='socks5://127.0.0.1:9050', http2=True, timeout=30)
    creds = ApiCreds(
        api_key=ENV.get("POLYMARKET_API_KEY", ""),
        api_secret=ENV.get("POLYMARKET_API_SECRET", ""),
        api_passphrase=ENV.get("POLYMARKET_PASSPHRASE", "") or ENV.get("POLYMARKET_API_PASSPHRASE", ""),
    )
    return ClobClient(
        host=CLOB_HOST,
        chain_id=137,
        key=PRIVATE_KEY,
        signature_type=0,
        funder=FUNDER,
        creds=creds,
    )


def send_heartbeat(client):
    global HEARTBEAT_ID
    try:
        resp = client.post_heartbeat(HEARTBEAT_ID)
        if isinstance(resp, dict):
            HEARTBEAT_ID = resp.get("heartbeat_id", HEARTBEAT_ID)
        return True
    except Exception as e:
        msg = str(e)
        if "heartbeat_id" in msg:
            try:
                import re
                m = re.search(r'heartbeat_id[^A-Za-z0-9_-]*([A-Za-z0-9_-]+)', msg)
                if m:
                    HEARTBEAT_ID = m.group(1)
                    resp = client.post_heartbeat(HEARTBEAT_ID)
                    if isinstance(resp, dict):
                        HEARTBEAT_ID = resp.get("heartbeat_id", HEARTBEAT_ID)
                    return True
            except:
                pass
        log(f"[CLOB] heartbeat error: {e}")
        return False


def get_order_snapshot(client, order_id: str):
    try:
        if not order_id:
            return None
        return client.get_order(order_id)
    except Exception as e:
        log(f"[CLOB] get_order error {order_id}: {e}")
        return None


def get_order_filled_size(client, order_id: str, asset_id: str, market: str, placed_after: int):
    """Doc-aligned fill confirmation: order snapshot + trade history, not open-order absence."""
    filled = 0.0
    status = "unknown"
    order = get_order_snapshot(client, order_id)
    if isinstance(order, dict):
        status = str(order.get("status") or "unknown").lower()
        try:
            filled = max(filled, float(order.get("size_matched") or order.get("filled") or 0))
        except:
            pass
    try:
        from py_clob_client.clob_types import TradeParams
        trades = client.get_trades(TradeParams(market=market, asset_id=asset_id, maker_address=FUNDER, after=placed_after)) or []
        for tr in trades:
            tstatus = str(tr.get("status") or "").upper()
            if tstatus not in ("MATCHED", "MINED", "CONFIRMED"):
                continue
            for mo in (tr.get("maker_orders") or []):
                if str(mo.get("order_id")) == str(order_id):
                    try:
                        filled += float(mo.get("matched_amount") or 0)
                    except:
                        pass
    except Exception as e:
        log(f"[CLOB] get_trades error {order_id}: {e}")
    return max(0.0, filled), status


def check_scoring(client, order_ids: list[str]) -> dict:
    try:
        from py_clob_client.clob_types import OrdersScoringParams
        ids = [x for x in order_ids if x]
        if not ids:
            return {}
        return client.are_orders_scoring(OrdersScoringParams(orderIds=ids)) or {}
    except Exception as e:
        log(f"[CLOB] scoring check error: {e}")
        return {}

# ── Single-instance guard ────────────────────────────────────────────────────
PID_F = Path("/tmp/polymarket_ctf_rebate.pid")

def ensure_single_instance():
    if PID_F.exists():
        try:
            old_pid = int(PID_F.read_text().strip())
            os.kill(old_pid, 0)
            log(f"[CTF-REBATE] Already running (PID {old_pid}) — exiting")
            sys.exit(1)
        except (ProcessLookupError, ValueError, OSError):
            pass
    PID_F.write_text(str(os.getpid()))

def cleanup_pid():
    try: PID_F.unlink()
    except: pass

import atexit


def load_state() -> dict:
    if STATE_F.exists():
        try: return json.loads(STATE_F.read_text())
        except: pass
    return {"windows":0,"splits":0,"sells_placed":0,"fills":0,"merges":0,
            "est_rebates":0.0,"pnl":0.0,"history":[]}

def save_state(state: dict):
    # Keep history last 100
    state["history"] = state.get("history",[])[-100:]
    STATE_F.write_text(json.dumps(state, indent=2, sort_keys=True))

# ── Core cycle ────────────────────────────────────────────────────────────────
def run_cycle(asset: str, market: dict, window_ts: int, state: dict):
    tag = f"[CTF-{asset.upper()}]"
    token_ids = market.get("token_ids") or []
    condition_id = market.get("conditionId") or market.get("condition_id") or ""
    question = market.get("question","")[:60]

    if not condition_id or len(token_ids) < 2:
        log(f"{tag} Missing conditionId or token_ids — skip"); return

    shares = SIZE  # scanner uses intended live size for eligibility checks

    # ── Scanner-first structural evaluation (no split, no orders, no gas) ─────

    rewards_min_size = float(market.get("rewardsMinSize") or 0)
    rewards_max_spread_c = float(market.get("rewardsMaxSpread") or 0)
    tick_size = float(market.get("orderPriceMinTickSize") or 0.01)
    order_min_size = float(market.get("orderMinSize") or 5)

    # Hard guard: if we're below order min or reward min, don't pretend this is a rebate cycle.
    if shares < order_min_size:
        log(f"{tag} SKIP reason=below_order_min shares={shares} orderMinSize={order_min_size}")
        tg(f"⚠️ {tag} SKIP\nReason: below orderMinSize ({shares:.0f} < {order_min_size:.0f})")
        return
    if rewards_min_size and shares < rewards_min_size:
        log(f"{tag} SKIP reason=below_rewards_min shares={shares} rewardsMinSize={rewards_min_size}")
        tg(f"⚠️ {tag} SKIP\nReason: below rewardsMinSize ({shares:.0f} < {rewards_min_size:.0f})")
        return

    yes_book, no_book = get_books(token_ids)
    yes_bid = float(yes_book.get("best_bid") or 0)
    no_bid  = float(no_book.get("best_bid") or 0)
    yes_ask = float(yes_book.get("best_ask") or 1)
    no_ask  = float(no_book.get("best_ask") or 1)
    yes_book_spread_c = round((yes_ask - yes_bid) * 100, 2)
    no_book_spread_c  = round((no_ask - no_bid) * 100, 2)

    # Conservative pre-filter: if the live book is already wider than the reward spread threshold,
    # skip before splitting. This reduces gas burn on structurally weak windows.
    if rewards_max_spread_c and (yes_book_spread_c > rewards_max_spread_c or no_book_spread_c > rewards_max_spread_c):
        log(f"{tag} SKIP reason=spread_too_wide yesSpread={yes_book_spread_c}c noSpread={no_book_spread_c}c rewardsMaxSpread={rewards_max_spread_c}c")
        tg(f"⚠️ {tag} SKIP\nReason: spread too wide (YES={yes_book_spread_c}c NO={no_book_spread_c}c)")
        return

    # Sane profile: don't split into junk books with tiny bids or obviously imbalanced quality.
    if yes_bid < MIN_BID_FLOOR or no_bid < MIN_BID_FLOOR:
        log(f"{tag} SKIP reason=weak_bid_floor yesBid={yes_bid:.2f} noBid={no_bid:.2f} minBidFloor={MIN_BID_FLOOR:.2f}")
        tg(f"⚠️ {tag} SKIP\nReason: weak bid floor (YES={yes_bid:.2f} NO={no_bid:.2f})")
        return

    if REQUIRE_TIGHT_BOTH:
        # Require both sides to have at least modest structure; avoid one strong / one junk side.
        if abs(yes_bid - no_bid) > 0.35:
            log(f"{tag} SKIP reason=pair_imbalanced yesBid={yes_bid:.2f} noBid={no_bid:.2f}")
            tg(f"⚠️ {tag} SKIP\nReason: pair too imbalanced")
            return

    # Build pair-level quotes. For split YES+NO, the meaningful floor is combined YES+NO revenue.
    floor_price = tick_size
    yes_sell_price = choose_post_only_sell_price(yes_book, floor_price, tick_size)
    no_sell_price  = choose_post_only_sell_price(no_book, floor_price, tick_size)

    if yes_sell_price is None or no_sell_price is None:
        log(f"{tag} SKIP reason=post_only_not_quoteable yesBid={yes_bid:.2f} yesAsk={yes_ask:.2f} noBid={no_bid:.2f} noAsk={no_ask:.2f}")
        tg(f"⚠️ {tag} SKIP\nReason: cannot build safe post-only two-sided quotes")
        return

    combined_pair = round(yes_sell_price + no_sell_price, 2)
    if combined_pair < MIN_COMBINED:
        log(f"{tag} SKIP reason=combined_below_min combined={combined_pair:.2f} minCombined={MIN_COMBINED:.2f}")
        tg(f"⚠️ {tag} SKIP\nReason: combined YES+NO price {combined_pair:.2f} below minimum {MIN_COMBINED:.2f}")
        return

    # Scanner verdict only: no split, no orders, no gas.
    state["windows"] = state.get("windows",0) + 1
    state.setdefault("scan_history", []).append({
        "ts": int(time.time()),
        "asset": asset,
        "window": window_ts,
        "question": question,
        "yes_bid": round(yes_bid, 4),
        "yes_ask": round(yes_ask, 4),
        "no_bid": round(no_bid, 4),
        "no_ask": round(no_ask, 4),
        "yes_spread_c": yes_book_spread_c,
        "no_spread_c": no_book_spread_c,
        "yes_quote": yes_sell_price,
        "no_quote": no_sell_price,
        "combined": combined_pair,
        "rewards_min_size": rewards_min_size,
        "rewards_max_spread_c": rewards_max_spread_c,
        "verdict": "TRADEABLE",
    })
    msg = (
        f"🧪 {tag} TRADEABLE\n"
        f"Market: {question}\n"
        f"Planned size: {shares:.0f}\n"
        f"YES bid/ask: {yes_bid:.2f}/{yes_ask:.2f} | quote {yes_sell_price:.2f}\n"
        f"NO bid/ask: {no_bid:.2f}/{no_ask:.2f} | quote {no_sell_price:.2f}\n"
        f"Combined: {combined_pair:.2f} | rewardsMinSize={rewards_min_size:.0f} | rewardsMaxSpread={rewards_max_spread_c}c"
    )
    log(msg.replace("\n", " | "))
    tg(msg)
    save_state(state)
    return

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    ensure_single_instance()
    atexit.register(cleanup_pid)
    if not PRIVATE_KEY and not DRY_RUN:
        log("[CTF-REBATE] POLYMARKET_PRIVATE_KEY not set"); sys.exit(1)
    if not FUNDER and not DRY_RUN:
        log("[CTF-REBATE] POLYMARKET_FUNDER not set"); sys.exit(1)

    mode = "DRY-RUN" if DRY_RUN else "LIVE"
    profile = "SCAN-ONLY" if SCAN_ONLY else "EXECUTION"
    log(f"[CTF-REBATE] STARTING mode={mode} profile={profile} assets={ASSETS} size=${SIZE} "
        f"sell_offset={SELL_OFFSET} entry_window=first_{ENTRY_SEC}s merge_at={MERGE_SEC}s")
    log(f"[CTF-REBATE] SANE PROFILE min_combined={MIN_COMBINED:.2f} min_bid_floor={MIN_BID_FLOOR:.2f} require_tight_both={REQUIRE_TIGHT_BOTH}")
    log("[CTF-REBATE] NOTE: reward eligibility depends on market rewardsMinSize/rewardsMaxSpread and order scoring per Polymarket docs")

    state = load_state()
    save_state(state)

    active: dict[str,int] = {}
    prefetched: dict[str,dict] = {}

    while True:
        try:
            now = int(time.time())
            w = (now // WINDOW_SEC) * WINDOW_SEC
            secs_into = now - w
            next_w = w + WINDOW_SEC

            # Pre-fetch next window markets
            if secs_into >= WINDOW_SEC - 60:
                for asset in ASSETS:
                    if prefetched.get(asset,{}).get("_w") != next_w:
                        m = get_market(asset, next_w)
                        if m:
                            m["_w"] = next_w
                            prefetched[asset] = m
                            log(f"[CTF-REBATE] Pre-fetched {asset.upper()} window {next_w}")

            # Enter within first ENTRY_SEC seconds
            if secs_into <= ENTRY_SEC:
                for asset in ASSETS:
                    if active.get(asset) == w: continue
                    m = prefetched.pop(asset, None)
                    if not m or m.get("_w") != w:
                        m = get_market(asset, w)
                    if not m: continue
                    active[asset] = w
                    log(f"[CTF-REBATE] >>> {asset.upper()} entering window {w}")
                    run_cycle(asset, m, w, state)

                    if MAX_WINDOWS > 0 and state.get("windows",0) >= MAX_WINDOWS:
                        log(f"[CTF-REBATE] Max windows reached ({MAX_WINDOWS}) — stopping")
                        return

            time.sleep(5)

        except KeyboardInterrupt:
            log("[CTF-REBATE] Stopped"); break
        except Exception as e:
            log(f"[CTF-REBATE] Loop error: {e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
