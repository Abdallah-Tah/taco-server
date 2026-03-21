#!/usr/bin/env python3
"""
polymarket_eth15m.py — Ethereum 15-Minute Polymarket Engine
======================================================
Two strategies:
  A) Binary Arb      — buy UP+DOWN when combined < $0.98, guaranteed profit
  B) Late Snipe      — directional bet near window close based on BTC delta

Dry run by default. Set DRY_RUN=false to go live.
"""
import json
import os
import math
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Paths & creds ──────────────────────────────────────────────────────────────
WORK_DIR      = Path("/home/abdaltm86/.openclaw/workspace/trading")
JOURNAL_DB    = WORK_DIR / "journal.db"
CREDS_FILE    = Path("/home/abdaltm86/.config/openclaw/secrets.env")
POSITIONS_F  = WORK_DIR / ".poly_eth15m_positions.json"
STATE_F       = WORK_DIR / ".poly_eth15m_state.json"
LOG_F        = WORK_DIR / ".poly_eth15m.log"

# ── Load credentials ─────────────────────────────────────────────────────────
def _load_env():
    env = {}
    if CREDS_FILE.exists():
        for line in CREDS_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = _load_env()

def _float(key, default):
    try:
        return float(os.environ.get(key, ENV.get(key, default)))
    except Exception:
        return float(default)

def _int(key, default):
    try:
        return int(os.environ.get(key, ENV.get(key, default)))
    except Exception:
        return int(default)

TELEGRAM_TOKEN = ENV.get("TELEGRAM_TOKEN", "8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4")
CHAT_ID        = ENV.get("CHAT_ID",        "7520899464")
POLY_WALLET    = ENV.get("POLY_WALLET",    "0x1a4c163a134D7154ebD5f7359919F9c439424f00")
VENV_PY        = Path("/home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3")
DRY_RUN        = os.environ.get("ETH15M_DRY_RUN", ENV.get("ETH15M_DRY_RUN", "true")).lower() != "false"
MAKER_ENABLED  = os.environ.get("ETH15M_MAKER_ENABLED", ENV.get("ETH15M_MAKER_ENABLED", "false")).lower() == "true"
MAKER_DRY_RUN  = os.environ.get("ETH15M_MAKER_DRY_RUN", ENV.get("ETH15M_MAKER_DRY_RUN", "true")).lower() != "false"
MAKER_START_SEC = _int("ETH15M_MAKER_START_SEC", 300)
MAKER_CANCEL_SEC = _int("ETH15M_MAKER_CANCEL_SEC", 60)
MAKER_OFFSET = _float("ETH15M_MAKER_OFFSET", 0.02)
MAKER_POLL_SEC = _int("ETH15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("ETH15M_MAKER_MIN_PRICE", 0.01)

# ── Config ────────────────────────────────────────────────────────────────────
WINDOW_SEC      = _int("ETH15M_WINDOW_SEC", 900)          # 15 minutes
ARB_THRESHOLD   = _float("ETH15M_ARB_THRESHOLD", 0.98)
ARB_SIZE        = _float("ETH15M_ARB_SIZE", 10.00)
SNIPE_DELTA_MIN = _float("ETH15M_SNIPE_DELTA_MIN", 0.035)
SNIPE_MAX_PRICE = _float("ETH15M_SNIPE_MAX_PRICE", 0.90)
SNIPE_DEFAULT   = _float("ETH15M_SNIPE_DEFAULT_SIZE", 5.00)
SNIPE_STRONG    = _float("ETH15M_SNIPE_STRONG_SIZE", 7.50)
SNIPE_STRONG_D  = _float("ETH15M_SNIPE_STRONG_DELTA", 0.10)  # percent
SNIPE_WINDOW    = _int("ETH15M_SNIPE_WINDOW_SEC", 15)
POLL_SEC        = _int("ETH15M_PRICE_POLL_SEC", 5)
SCAN_SEC        = _int("ETH15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS  = _float("ETH15M_MAX_DAILY_LOSS", 15.00)
SERIES_ID       = "10216"
SERIES_SLUG     = "eth-up-or-down-15m"

# ── State ─────────────────────────────────────────────────────────────────────
_state = {
    "window_ts":       0,
    "window_open_eth": 0.0,
    "arb_done":        False,
    "snipe_done":      False,
    "eth_prices":      [],   # list of (timestamp, price)
    "daily_pnl":       0.0,
    "daily_reset":     "",
    "trades":          [],
    "maker_order_id":   "",
    "maker_token_id":   "",
    "maker_side":       "",
    "maker_price":      0.0,
    "maker_shares":     0.0,
    "maker_done":       False,
    "maker_last_poll":  0,
}
_positions = []   # list of dicts for open/confirmation positions

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_F, "a") as f:
        f.write(line + "\n")

def tg(msg):
    # In DRY RUN mode, all Telegram alerts are silenced — log only
    if DRY_RUN:
        return
    def _send():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def trigger_post_resolution_tasks():
    if DRY_RUN:
        return
    try:
        subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_reconcile.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_redeem.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[POST-RESOLUTION] task launch error: {e}")

# ── Persistence ───────────────────────────────────────────────────────────────
def save_state():
    with open(STATE_F, "w") as f:
        json.dump(_state, f, indent=2)

def load_state():
    global _state
    if STATE_F.exists():
        with open(STATE_F) as f:
            _state = json.load(f)

# ── BTC price from Coinbase ────────────────────────────────────────────────────
def get_eth_price():
    try:
        r = requests.get(
            "https://api.exchange.coinbase.com/products/ETH-USD/ticker",
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        price = data.get("price")
        if price is not None:
            return float(price)
    except Exception as e:
        log(f"ETH Coinbase price error: {e}")

    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        price = data.get("ethereum", {}).get("usd")
        if price is not None:
            log("ETH price fallback: CoinGecko")
            return float(price)
    except Exception as e:
        log(f"ETH CoinGecko fallback error: {e}")
        return None

    return None

# ── Find current market slug ──────────────────────────────────────────────────
def get_current_slug():
    now = int(time.time())
    window_ts = now - (now % WINDOW_SEC)
    return f"eth-updown-15m-{window_ts}", window_ts

# ── Gamma API: get market info ────────────────────────────────────────────────
def get_market(slug):
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/markets?slug={slug}",
            timeout=10,
        )
        data = r.json()
        if data:
            m = data[0]
            outcome_prices = json.loads(m["outcomePrices"])
            return {
                "id":           m["id"],
                "question":     m["question"],
                "condition_id": m["conditionId"],
                "clob_token_id": json.loads(m.get("clobTokenIds", "[]")),
                "yes_price":    float(outcome_prices[0]),
                "no_price":     float(outcome_prices[1]),
                "combined":     float(outcome_prices[0]) + float(outcome_prices[1]),
                "liquidity":    float(m["liquidity"]),
                "volume":       float(m["volume"]),
                "end_date":     m["endDate"],
                "closed":       m.get("closed", False),
            }
    except Exception as e:
        log(f"Gamma error: {e}")
    return None

# ── CLOB: get order book ──────────────────────────────────────────────────────
def get_clob_prices(condition_id):
    try:
        r = requests.get(
            f"https://clob.polymarket.com/orders?condition_id={condition_id}&喝着=1",
            timeout=10,
        )
        return r.json()
    except Exception as e:
        log(f"CLOB error: {e}")
    return {}

# ── Place order via polymarket_executor ───────────────────────────────────────
def place_order(side, shares, price, condition_id, token_id):
    """Use aggressive FOK buy orders for immediate 15m fills and verify them."""
    import subprocess
    cmd = [
        str(VENV_PY),
        str(WORK_DIR / "scripts" / "polymarket_executor.py"),
        "buy_fok" if side.upper() == "BUY" else "sell",
        token_id,
        str(shares),
        str(price),
    ]
    if DRY_RUN:
        log(f"[DRY] {'BUY' if side.upper()=='BUY' else 'SELL'} {shares} @{price:.4f} token={token_id}")
        return {"success": True, "dry": True, "filled": True}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    payload = {"success": result.returncode == 0, "output": result.stdout, "error": result.stderr, "filled": False}
    try:
        marker = '__RESULT__'
        for line in result.stdout.splitlines():
            if line.startswith(marker):
                import json as _json
                data = _json.loads(line[len(marker):])
                fill = (data.get('fill_check') or {})
                payload['filled'] = bool(fill.get('filled'))
                payload['filled_size'] = fill.get('filled_size', 0)
                payload['posted'] = data
                break
    except Exception:
        pass
    return payload

# ── Journal logging ───────────────────────────────────────────────────────────
def log_trade(engine, direction, size_usd, entry_price, pnl, exit_type, hold_sec, notes=""):
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades (
                id, engine, timestamp_open, timestamp_close, asset,
                category, direction, entry_price, exit_price,
                position_size, position_size_usd, pnl_absolute, pnl_percent,
                exit_type, hold_duration_seconds, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"eth15m_{int(time.time()*1000)}",
            engine, datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat() if exit_type else None,
            "eth-15m",
            "btc-updown",
            direction, entry_price, entry_price if not exit_type else entry_price,
            size_usd / entry_price if entry_price else 0,
            size_usd, pnl or 0.0, (pnl/size_usd*100) if size_usd else 0,
            exit_type or "open", hold_sec or 0,
            "normal", notes
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Journal error: {e}")



def maker_place_order(side, shares, price, condition_id, token_id):
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "maker_buy", token_id, str(shares), str(price)]
    if MAKER_DRY_RUN:
        log(f"[ETH-MAKER-DRY] {side} {shares} @{price:.4f} token={token_id}")
        return {"success": True, "dry": True, "order_id": f"dry-{int(time.time())}"}
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    payload = {"success": result.returncode == 0, "output": result.stdout, "error": result.stderr}
    for line in result.stdout.splitlines():
        if line.startswith('__RESULT__'):
            import json as _json
            payload['posted'] = _json.loads(line[len('__RESULT__'):])
            payload['order_id'] = payload['posted'].get('order_id') or payload['posted'].get('orderID') or payload['posted'].get('id')
            break
    return payload


def maker_order_status(order_id):
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "order_status", str(order_id)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        if line.startswith('__RESULT__'):
            import json as _json
            return _json.loads(line[len('__RESULT__'):])
    return {"status": "error", "error": result.stderr or result.stdout or 'no_status'}


def maker_verify_fill(token_id, min_size):
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "verify_fill", str(token_id), str(min_size)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        if line.startswith('__RESULT__'):
            import json as _json
            return _json.loads(line[len('__RESULT__'):])
    return {"filled": False, "reason": result.stderr or result.stdout or 'no_verify'}


def maker_cancel_order(order_id):
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "cancel", str(order_id)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {"success": result.returncode == 0, "output": result.stdout, "error": result.stderr}


def check_maker_snipe(market, seconds_remaining):
    if not MAKER_ENABLED:
        return None
    oid = _state.get("maker_order_id")
    if oid and (time.time() - _state.get("maker_last_poll", 0) >= MAKER_POLL_SEC or seconds_remaining <= MAKER_CANCEL_SEC):
        _state["maker_last_poll"] = int(time.time())
        st = maker_order_status(oid)
        log(f"[ETH-MAKER] status order_id={oid} -> {st.get('status')}")
        if st.get('status') in ('filled', 'partially_filled'):
            _state['maker_done'] = True
            _state['snipe_done'] = True
            save_state()
            tg(f"[ETH-MAKER] FILLED order {oid}")
            return True
        if st.get('status') == 'not_found':
            vf = maker_verify_fill(_state.get('maker_token_id'), _state.get('maker_shares', 0))
            if vf.get('filled'):
                log(f"[ETH-MAKER] fill verified via activity/trades size={vf.get('filled_size')}")
                _state['maker_done'] = True
                _state['snipe_done'] = True
                save_state()
                tg(f"[ETH-MAKER] FILL VERIFIED {vf.get('filled_size')} shares")
                return True
        if seconds_remaining <= MAKER_CANCEL_SEC and st.get('status') in ('open', 'partially_filled'):
            maker_cancel_order(oid)
            log(f"[ETH-MAKER] cancel by deadline order_id={oid} sec_rem={seconds_remaining}")
            _state['maker_done'] = True
            save_state()
            return False
    if _state.get('maker_done') or oid:
        return None
    if seconds_remaining > MAKER_START_SEC or seconds_remaining <= MAKER_CANCEL_SEC:
        return None

    base_price = get_eth_price()
    if base_price is None:
        return None
    if not _state.get('window_open_eth'):
        return None
    delta_pct = (base_price - _state['window_open_eth']) / _state['window_open_eth'] * 100
    log(f"[ETH-MAKER] now={base_price} open={_state['window_open_eth']} delta={delta_pct:+.3f}% sec_rem={seconds_remaining}")
    direction = 'UP' if delta_pct > SNIPE_DELTA_MIN else ('DOWN' if delta_pct < -SNIPE_DELTA_MIN else None)
    if not direction:
        return None
    token_id = market['clob_token_id'][0] if direction == 'UP' else (market['clob_token_id'][1] if len(market['clob_token_id']) > 1 else '')
    token_price = market['yes_price'] if direction == 'UP' else market['no_price']
    if token_price < MAKER_MIN_PRICE:
        log(f"[ETH-MAKER] token_mid={token_price:.4f} < min {MAKER_MIN_PRICE:.4f}, skipping")
        return None
    limit_price = max(0.01, round(token_price - MAKER_OFFSET, 2))
    shares = max(5.0, math.floor((SNIPE_DEFAULT / max(limit_price, 0.01)) * 100) / 100)
    log(f"[ETH-MAKER] {direction} signal token_mid={token_price:.4f} limit={limit_price:.4f} shares={shares:.2f} dry={MAKER_DRY_RUN}")
    r = maker_place_order('BUY', shares, limit_price, market['condition_id'], token_id)
    for line in (r.get('output') or '').splitlines():
        if line.strip() and ('[MAKER' in line or '[RESULT' in line or '[ATTEMPT' in line):
            log(f"[EXEC] {line.strip()}")
    if r.get('error'):
        for line in r['error'].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")
    _state['maker_order_id'] = str(r.get('order_id') or (r.get('posted') or {}).get('order_id') or '')
    _state['maker_token_id'] = token_id
    _state['maker_side'] = direction
    _state['maker_price'] = limit_price
    _state['maker_shares'] = shares
    _state['maker_last_poll'] = int(time.time())
    _state['maker_done'] = False
    save_state()
    return r.get('success')

# ── Strategy A: Arb check ─────────────────────────────────────────────────────
def check_arb(market):
    if _state["arb_done"]:
        return None
    combined = market["yes_price"] + market["no_price"]
    if combined >= ARB_THRESHOLD:
        log(f"[ETH-ARB] No arb. Combined={combined:.4f} >= {ARB_THRESHOLD}")
        return None

    log(f"[ETH-ARB] FOUND! YES={market['yes_price']:.4f} NO={market['no_price']:.4f} COMBINED={combined:.4f}")
    # Buy both legs
    yes_tid = market["clob_token_id"][0] if len(market["clob_token_id"]) > 0 else ""
    no_tid  = market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else ""

    # FOK orders — buy equal dollar value of each
    yes_shares = ARB_SIZE / market["yes_price"]
    no_shares  = ARB_SIZE / market["no_price"]

    r1 = place_order("BUY", yes_shares, market["yes_price"], market["condition_id"], yes_tid)
    r2 = place_order("BUY", no_shares,  market["no_price"],  market["condition_id"], no_tid)

    arb_profit = (1.00 - combined) * min(yes_shares, no_shares)
    notes = f"arb profit=${arb_profit:.2f} yes_shares={yes_shares:.2f} no_shares={no_shares:.2f}"
    log(f"[ETH-ARB] Result: yes={r1.get('success')}, no={r2.get('success')}. {notes}")
    tg(f"[ETH-ARB] {notes}")

    _state["arb_done"] = True
    save_state()
    return True

# ── Strategy B: Snipe check ──────────────────────────────────────────────────
def check_snipe(market, seconds_remaining):
    if _state["snipe_done"]:
        return None
    if seconds_remaining > SNIPE_WINDOW or seconds_remaining < 5:
        return None

    eth_price = get_eth_price()
    if eth_price is None:
        return None

    if not _state["window_open_eth"]:
        log(f"[ETH-SNIPE] No window_open_eth recorded, skipping. ETH={eth_price}")
        return None

    delta_pct = (eth_price - _state["window_open_eth"]) / _state["window_open_eth"] * 100
    log(f"[ETH-SNIPE] ETH now={eth_price} window_open={_state['window_open_eth']} delta={delta_pct:+.3f}%")

    direction = None
    if delta_pct > SNIPE_DELTA_MIN:
        direction = "UP"
    elif delta_pct < -SNIPE_DELTA_MIN:
        direction = "DOWN"
    else:
        log(f"[ETH-SNIPE] Delta {delta_pct:+.3f}% too small, skipping")
        return None

    # Pick token
    token_id = market["clob_token_id"][0] if direction == "UP" else (market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else "")
    price    = market["yes_price"] if direction == "UP" else market["no_price"]

    if price > SNIPE_MAX_PRICE:
        log(f"[ETH-SNIPE] Price {price:.4f} > max {SNIPE_MAX_PRICE}, skipping")
        return None

    size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    shares = size / price
    # Ensure minimum 5 shares for Polymarket
    if shares < 5:
        shares = 5
        size = shares * price

    log(f"[ETH-SNIPE] {direction} signal! ETH delta={delta_pct:+.3f}% size=${size:.2f} price={price:.4f} shares={shares:.2f}")
    r = place_order("BUY", shares, price, market["condition_id"], token_id)
    # Log executor output so [ATTEMPT]/[RESULT] lines are visible
    for line in (r.get("output") or "").splitlines():
        if any(tag in line for tag in ("[ATTEMPT]", "[RESULT]", "Book best", "FILL")):
            log(f"[EXEC] {line.strip()}")
    if r.get("error"):
        for line in r["error"].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")
    notes = f"snipe {direction} delta={delta_pct:+.3f}% price={price:.4f} size=${size:.2f}"
    log(f"[ETH-SNIPE] Result: {r.get('success')} filled={r.get('filled')}. {notes}")
    if not r.get('filled') and not DRY_RUN:
        log(f"[ETH-SNIPE] No fill confirmed — not counting this trade as real")
        tg(f"[ETH-SNIPE] NO FILL confirmed | {notes}")
        return False
    tg(f"[ETH-SNIPE] FILL CONFIRMED {r.get('filled_size', shares):.2f} shares | {notes}")

    _state["snipe_done"]        = True
    _state["prev_snipe_side"]  = direction
    _state["prev_snipe_price"] = price
    _state["prev_snipe_size"]  = size
    save_state()
    return True

# ── New window setup ──────────────────────────────────────────────────────────
def setup_new_window(slug, window_ts):
    eth_price = get_eth_price()
    if eth_price is None:
        eth_price = _state.get("window_open_eth", 0.0)

    _state["window_ts"]       = window_ts
    _state["window_open_eth"] = eth_price
    _state["arb_done"]        = False
    _state["snipe_done"]      = False
    _state["maker_order_id"]  = ""
    _state["maker_token_id"]  = ""
    _state["maker_side"]      = ""
    _state["maker_price"]     = 0.0
    _state["maker_shares"]    = 0.0
    _state["maker_done"]      = False
    _state["maker_last_poll"] = 0
    _state["eth_prices"]      = [(int(time.time()), eth_price)]
    save_state()
    trigger_post_resolution_tasks()

    log(f"[ETH-NEW WINDOW] ts={window_ts} ETH={eth_price} slug={slug}")
    # Window start — no telegram noise

# ── Daily loss check ───────────────────────────────────────────────────────────
def check_daily_limit():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _state.get("daily_reset", "") != today:
        _state["daily_pnl"]   = 0.0
        _state["daily_reset"] = today
        save_state()
    if _state["daily_pnl"] <= -MAX_DAILY_LOSS:
        log(f"[ETH-SAFETY] Daily loss ${abs(_state['daily_pnl']):.2f} >= ${MAX_DAILY_LOSS}, pausing until midnight UTC")
        return False
    return True

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    global _state
    load_state()
    log("=" * 60)
    log(f"[ETH-15M] STARTING {'(DRY RUN)' if DRY_RUN else '(LIVE)'}")
    log(f"[ETH-15M] Arb threshold=${ARB_THRESHOLD}, snipe delta>={SNIPE_DELTA_MIN}%, max daily loss=${MAX_DAILY_LOSS} | maker={MAKER_ENABLED} dry={MAKER_DRY_RUN} start=T-{MAKER_START_SEC} cancel=T-{MAKER_CANCEL_SEC} offset={MAKER_OFFSET}")
    tg("[ETH-15M] Engine started!")

    while True:
        now      = int(time.time())
        window_sec = now % WINDOW_SEC
        window_ts  = now - window_sec
        window_end = window_ts + WINDOW_SEC
        sec_rem   = window_end - now

        slug, _ = get_current_slug()

        # New window?
        if window_ts != _state["window_ts"]:
            log(f"[ETH-CYCLE] New window detected: {slug}")
            setup_new_window(slug, window_ts)

        # Safety
        if not check_daily_limit():
            time.sleep(60)
            continue

        # Get market
        market = get_market(slug)
        if not market:
            log(f"[ETH-CYCLE] No market found for {slug}, sleeping 10s...")
            time.sleep(10)
            continue

        log(f"[ETH-CYCLE] {market['question'][:60]} | YES={market['yes_price']:.3f} NO={market['no_price']:.3f} | {sec_rem}s left")

        # Record BTC price
        btc = get_eth_price()
        if btc:
            _state["eth_prices"].append((int(time.time()), btc))
            # Keep last window's prices only
            cutoff = window_ts
            _state["eth_prices"] = [(t, p) for t, p in _state["eth_prices"] if t >= cutoff]

        # Strategy A: Arb (first 14.75 min)
        if sec_rem > 15:
            check_arb(market)

        # Strategy B: Snipe / Maker Snipe
        if MAKER_ENABLED:
            check_maker_snipe(market, sec_rem)
        elif sec_rem <= SNIPE_WINDOW and sec_rem > 5:
            check_snipe(market, sec_rem)

        # Sleep
        time.sleep(SCAN_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[ETH-15M] Stopped by user")
