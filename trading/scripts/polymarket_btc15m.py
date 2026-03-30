#!/usr/bin/env python3
"""
polymarket_btc15m.py — BTC 15-Minute Polymarket Engine
======================================================
Two strategies:
  A) Binary Arb      — buy UP+DOWN when combined < $0.98, guaranteed profit
  B) Late Snipe      — directional bet near window close based on BTC delta

Dry run by default. Set DRY_RUN=false to go live.
"""
import json
import math
import os
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
POSITIONS_F  = WORK_DIR / ".poly_btc15m_positions.json"
STATE_F       = WORK_DIR / ".poly_btc15m_state.json"
LOG_F        = WORK_DIR / ".poly_btc15m.log"

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
DRY_RUN        = os.environ.get("BTC15M_DRY_RUN",  ENV.get("BTC15M_DRY_RUN",  "true")).lower() != "false"
MAKER_ENABLED  = os.environ.get("BTC15M_MAKER_ENABLED", ENV.get("BTC15M_MAKER_ENABLED", "false")).lower() == "true"
MAKER_DRY_RUN  = os.environ.get("BTC15M_MAKER_DRY_RUN", ENV.get("BTC15M_MAKER_DRY_RUN", "true")).lower() != "false"
MAKER_START_SEC = _int("BTC15M_MAKER_START_SEC", 300)
MAKER_CANCEL_SEC = _int("BTC15M_MAKER_CANCEL_SEC", 10)
MAKER_OFFSET = _float("BTC15M_MAKER_OFFSET", 0.005)
MAKER_POLL_SEC = _int("BTC15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("BTC15M_MAKER_MIN_PRICE", 0.01)
BTC15M_GABAGOOL_ENABLED = os.environ.get("BTC15M_GABAGOOL_ENABLED", ENV.get("BTC15M_GABAGOOL_ENABLED", "false")).lower() == "true"
BTC15M_GABAGOOL_DRY_RUN = os.environ.get("BTC15M_GABAGOOL_DRY_RUN", ENV.get("BTC15M_GABAGOOL_DRY_RUN", "true")).lower() != "false"
GABAGOOL_MAX_LEG = _float("GABAGOOL_MAX_LEG", 0.48)
GABAGOOL_TARGET_COMBINED = _float("GABAGOOL_TARGET_COMBINED", 0.97)

# ── Config ────────────────────────────────────────────────────────────────────
WINDOW_SEC      = _int("BTC15M_WINDOW_SEC", 900)          # 15 minutes
ARB_THRESHOLD   = _float("BTC15M_ARB_THRESHOLD", 0.98)
ARB_SIZE        = _float("BTC15M_ARB_SIZE", 10.00)
SNIPE_DELTA_MIN = _float("BTC15M_SNIPE_DELTA_MIN", 0.025)
SNIPE_MAX_PRICE = _float("BTC15M_SNIPE_MAX_PRICE", 0.90)
# Signal confirmation (improved filtering)
SIGNAL_CONFIRM_COUNT   = _int("BTC15M_SIGNAL_CONFIRM_COUNT", 2)   # consecutive polls needed
SIGNAL_CONFIRM_SEC     = _int("BTC15M_SIGNAL_CONFIRM_SEC", 15)     # max age of confirm samples
SIGNAL_MAX_ENTRY_PRICE = _float("BTC15M_SIGNAL_MAX_ENTRY_PRICE", 0.80)  # skip if entry worse than this
SIGNAL_MIN_ENTRY_PRICE = _float("BTC15M_SIGNAL_MIN_ENTRY_PRICE", 0.45)  # skip if entry lower than this
SNIPE_DEFAULT   = _float("BTC15M_SNIPE_DEFAULT_SIZE", 5.00)
SNIPE_STRONG    = _float("BTC15M_SNIPE_STRONG_SIZE", 7.50)
SNIPE_STRONG_D  = _float("BTC15M_SNIPE_STRONG_DELTA", 0.10)  # percent
SNIPE_WINDOW    = _int("BTC15M_SNIPE_WINDOW_SEC", 30)
POLL_SEC        = _int("BTC15M_PRICE_POLL_SEC", 5)
SCAN_SEC        = _int("BTC15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS  = _float("BTC15M_MAX_DAILY_LOSS", 15.00)
SERIES_ID       = "10192"
SERIES_SLUG     = "btc-up-or-down-15m"

# ── State ─────────────────────────────────────────────────────────────────────
_state = {
    "window_ts":       0,
    "window_open_btc": 0.0,
    "arb_done":        False,
    "snipe_done":      False,
    "btc_prices":      [],   # list of (timestamp, price)
    "daily_pnl":       0.0,
    "daily_reset":     "",
    "consecutive_losses": 0,
    "cooldown_until": 0,
    "trades":          [],
    "maker_order_id":   "",
    "maker_token_id":   "",
    "maker_side":       "",
    "maker_price":      0.0,
    "maker_shares":     0.0,
    "maker_done":       False,
    "maker_last_poll":  0,
    "maker_seen_fills": [],
    "gabagool_yes_low": None,
    "gabagool_yes_ts": 0,
    "gabagool_no_low": None,
    "gabagool_no_ts": 0,
    "gabagool_window_logged": False,
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
    if DRY_RUN:
        return
    def _send():
        try:
            cp = subprocess.run([
                'openclaw', 'message', 'send',
                '--channel', 'telegram',
                '--target', str(CHAT_ID),
                '--message', str(msg),
            ], capture_output=True, text=True, timeout=15)
            if cp.returncode != 0:
                log(f"[TG-ERR] rc={cp.returncode} stderr={cp.stderr.strip()[:300]}")
        except Exception as e:
            log(f"[TG-ERR] {e}")
    threading.Thread(target=_send, daemon=True).start()


def trigger_post_resolution_tasks():
    if DRY_RUN:
        return
    try:
        subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_reconcile.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_redeem.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Check for consecutive losses after reconcile
        check_consecutive_losses()
    except Exception as e:
        log(f"[POST-RESOLUTION] task launch error: {e}")

def check_consecutive_losses():
    """Check journal for consecutive losses and trigger cooldown if needed."""
    try:
        import sqlite3
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        # Get last 3 resolved btc15m trades
        c.execute("""
            SELECT pnl_absolute FROM trades 
            WHERE engine='btc15m' AND exit_type='resolved' 
            ORDER BY timestamp_close DESC LIMIT 3
        """)
        rows = c.fetchall()
        conn.close()
        
        if len(rows) >= 3:
            # Check if all 3 are losses
            if all(float(row[0]) < 0 for row in rows):
                _state["consecutive_losses"] = 3
                _state["cooldown_until"] = time.time() + 1800  # 30 min
                save_state()
                log("[BTC-MAKER] 3 consecutive losses — 30 min cooldown")
                tg("[BTC-MAKER] ⚠️ 3 losses in a row — pausing 30 min")
            else:
                # Reset if not all losses
                if _state.get("consecutive_losses", 0) > 0:
                    _state["consecutive_losses"] = 0
                    save_state()
    except Exception as e:
        log(f"[LOSS-CHECK] error: {e}")

# ── Persistence ───────────────────────────────────────────────────────────────
def save_state():
    with open(STATE_F, "w") as f:
        json.dump(_state, f, indent=2)

def load_state():
    global _state
    if STATE_F.exists():
        with open(STATE_F) as f:
            _state = json.load(f)
    _state.setdefault('maker_seen_fills', [])

# ── BTC price from Coinbase ────────────────────────────────────────────────────
def get_btc_price():
    try:
        r = requests.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
            timeout=5,
        )
        return float(r.json()["price"])
    except Exception as e:
        log(f"BTC price error: {e}")
        return None

# ── Find current market slug ──────────────────────────────────────────────────
def get_current_slug():
    now = int(time.time())
    window_ts = now - (now % WINDOW_SEC)
    return f"btc-updown-15m-{window_ts}", window_ts

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
def log_trade(engine, direction, size_usd, entry_price, pnl, exit_type, hold_sec, notes="", sec_remaining=None, price_bucket=None):
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        
        # Build notes with sec_remaining and price_bucket if provided
        full_notes = notes
        if sec_remaining is not None:
            full_notes = f"{full_notes} sec_rem={sec_remaining}"
        if price_bucket:
            full_notes = f"{full_notes} price_bucket={price_bucket}"
        
        c.execute("""
            INSERT INTO trades (
                engine, timestamp_open, timestamp_close, asset,
                category, direction, entry_price, exit_price,
                position_size, position_size_usd, pnl_absolute, pnl_percent,
                exit_type, hold_duration_seconds, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            engine, datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat() if exit_type else None,
            "btc-15m",
            "btc-updown",
            direction, entry_price, entry_price if not exit_type else entry_price,
            size_usd / entry_price if entry_price else 0,
            size_usd, pnl or 0.0, (pnl/size_usd*100) if size_usd else 0,
            exit_type or "open", hold_sec or 0,
            "normal", full_notes
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Journal error: {e}")



def maker_place_order(side, shares, price, condition_id, token_id):
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "maker_buy", token_id, str(shares), str(price)]
    if MAKER_DRY_RUN:
        log(f"[BTC-MAKER-DRY] {side} {shares} @{price:.4f} token={token_id}")
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
    
    # Cooldown check
    if time.time() < _state.get("cooldown_until", 0):
        log(f"[BTC-MAKER] cooldown active, {int(_state['cooldown_until'] - time.time())}s remaining")
        return None
    
    oid = _state.get("maker_order_id")
    if oid and (time.time() - _state.get("maker_last_poll", 0) >= MAKER_POLL_SEC or seconds_remaining <= MAKER_CANCEL_SEC):
        _state["maker_last_poll"] = int(time.time())
        st = maker_order_status(oid)
        log(f"[BTC-MAKER] status order_id={oid} -> {st.get('status')}")
        if st.get('status') in ('filled', 'partially_filled'):
            _state['maker_done'] = True
            _state['snipe_done'] = True
            _state["consecutive_losses"] = 0  # Reset on successful fill
            save_state()
            tg(f"[BTC-MAKER] FILLED order {oid}")
            log_trade("btc15m", _state.get('maker_side', 'UP'), 
                _state.get('maker_shares', 0) * _state.get('maker_price', 0),
                _state.get('maker_price', 0), 0, 'filled',
                int(time.time()) - _state.get('maker_last_poll', int(time.time())),
                notes=f"maker_fill oid={oid}",
                sec_remaining=_state.get('maker_sec_remaining'),
                price_bucket=_state.get('maker_price_bucket'))
            return True
        if st.get('status') == 'not_found':
            vf = maker_verify_fill(_state.get('maker_token_id'), _state.get('maker_shares', 0))
            if vf.get('filled'):
                fill_key = f"{oid}:{vf.get('filled_size')}"
                seen = set(_state.get('maker_seen_fills', []))
                if fill_key not in seen:
                    log(f"[BTC-MAKER] fill verified via activity/trades size={vf.get('filled_size')}")
                    tg(f"[BTC-MAKER] FILL VERIFIED {vf.get('filled_size')} shares")
                    # Log verified fill to journal
                    log_trade("btc15m", _state.get('maker_side', 'UP'),
                        vf.get('filled_size', 0) * _state.get('maker_price', 0),
                        _state.get('maker_price', 0), 0, 'filled',
                        int(time.time()) - _state.get('maker_last_poll', int(time.time())),
                        notes=f"maker_verify oid={oid} size={vf.get('filled_size')}",
                        sec_remaining=_state.get('maker_sec_remaining'),
                        price_bucket=_state.get('maker_price_bucket'))
                    seen.add(fill_key)
                    _state['maker_seen_fills'] = list(seen)[-200:]
                _state['maker_done'] = True
                _state['snipe_done'] = True
                _state["consecutive_losses"] = 0  # Reset on successful fill
                save_state()
                return True
        if seconds_remaining <= MAKER_CANCEL_SEC and st.get('status') in ('open', 'partially_filled'):
            maker_cancel_order(oid)
            log(f"[BTC-MAKER] cancel by deadline order_id={oid} sec_rem={seconds_remaining}")
            tg(f"[BTC-MAKER] ORDER CANCELLED: {oid} | sec_rem={seconds_remaining}")
            _state['maker_done'] = True
            save_state()
            return False
    if _state.get('maker_done') or oid:
        return None
    if seconds_remaining > MAKER_START_SEC or seconds_remaining <= MAKER_CANCEL_SEC:
        return None

    base_price = get_btc_price()
    if base_price is None:
        return None
    if not _state.get('window_open_btc'):
        return None
    delta_pct = (base_price - _state['window_open_btc']) / _state['window_open_btc'] * 100
    log(f"[BTC-MAKER] now={base_price} open={_state['window_open_btc']} delta={delta_pct:+.3f}% sec_rem={seconds_remaining}")
    direction = 'UP' if delta_pct > SNIPE_DELTA_MIN else ('DOWN' if delta_pct < -SNIPE_DELTA_MIN else None)
    if not direction:
        return None

    # ── 1-HOUR TREND FILTER ──
    prices = _state.get("btc_prices", [])
    hour_ago_ts = int(time.time()) - 3600
    hour_ago_prices = [p for t, p in prices if t <= hour_ago_ts + 60 and t >= hour_ago_ts - 60]
    if hour_ago_prices:
        hour_delta = (base_price - hour_ago_prices[0]) / hour_ago_prices[0] * 100
        if direction == "UP" and hour_delta < -0.3:
            log(f"[BTC-MAKER] 1h trend DOWN ({hour_delta:+.3f}%) conflicts with UP signal, skipping")
            return None
        if direction == "DOWN" and hour_delta > 0.3:
            log(f"[BTC-MAKER] 1h trend UP ({hour_delta:+.3f}%) conflicts with DOWN signal, skipping")
            return None

    # ── Signal confirmation: require consecutive polls agreeing ──
    now_ts = int(time.time())
    cutoff_ts = now_ts - SIGNAL_CONFIRM_SEC
    recent_prices = [(t, p) for t, p in _state.get('btc_prices', []) if t >= cutoff_ts]
    if len(recent_prices) < SIGNAL_CONFIRM_COUNT:
        log(f"[BTC-MAKER] Confirm: only {len(recent_prices)}/{SIGNAL_CONFIRM_COUNT} samples, waiting")
        return None
    confirmations = 0
    for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]:
        d = (p - _state['window_open_btc']) / _state['window_open_btc'] * 100
        if (direction == 'UP' and d > SNIPE_DELTA_MIN) or (direction == 'DOWN' and d < -SNIPE_DELTA_MIN):
            confirmations += 1
    if confirmations < SIGNAL_CONFIRM_COUNT:
        log(f"[BTC-MAKER] Confirm: {confirmations}/{SIGNAL_CONFIRM_COUNT}, skipping")
        return None
    recent_avg = sum(p for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]) / len(recent_prices[-SIGNAL_CONFIRM_COUNT:])
    momentum = (recent_avg - _state['window_open_btc']) / _state['window_open_btc'] * 100
    
    # ── MOMENTUM ACCELERATION CHECK ──
    recent = [(t, p) for t, p in prices if t >= now_ts - 120]
    if len(recent) >= 6:
        mid = len(recent) // 2
        first_half = recent[:mid]
        second_half = recent[mid:]
        delta_first = (first_half[-1][1] - first_half[0][1]) / first_half[0][1] * 100
        delta_second = (second_half[-1][1] - second_half[0][1]) / second_half[0][1] * 100
        if abs(delta_second) < abs(delta_first) * 0.5:
            log(f"[BTC-MAKER] momentum decelerating first={delta_first:+.4f}% second={delta_second:+.4f}%, skipping")
            return None
    
    # Require momentum to be meaningfully above the noise floor (1.5% = 60% of delta threshold)
    MOMENTUM_MIN = SNIPE_DELTA_MIN * 1.50
    if abs(momentum) < MOMENTUM_MIN:
        log(f"[BTC-MAKER] momentum={momentum:+.3f}% < {MOMENTUM_MIN:.3f}%, skipping (weak)")
        return None
    log(f"[BTC-MAKER] CONFIRMED {confirmations}/{SIGNAL_CONFIRM_COUNT} | momentum={momentum:+.3f}%")
    token_id = market['clob_token_id'][0] if direction == 'UP' else (market['clob_token_id'][1] if len(market['clob_token_id']) > 1 else '')
    token_price = market['yes_price'] if direction == 'UP' else market['no_price']
    
    # ── NEW FILTERS: sec_remaining and entry_price ──
    # Note: maker snipe window is t<300s down to t<10s, filter only ultra-late entries
    if seconds_remaining < 15:
        log(f"[BTC-MAKER] FILTER: sec_remaining={seconds_remaining}s < 15s, skipping ultra-late entry")
        return None
    
    # Price bucket classification and filtering
    if token_price >= 0.80:
        price_bucket = "high"
        log(f"[BTC-MAKER] FILTER: entry_price={token_price:.4f} >= 0.80 (bucket={price_bucket}), skipping high price")
        return None
    elif token_price >= 0.60:
        price_bucket = "mid"
    elif token_price >= 0.40:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
    
    # Min price filter
    if token_price < SIGNAL_MIN_ENTRY_PRICE:
        log(f"[BTC-MAKER] entry={token_price:.4f} < min {SIGNAL_MIN_ENTRY_PRICE:.2f}, skipping")
        return None
    
    log(f"[BTC-MAKER] PRICE_BUCKET: {price_bucket} (price={token_price:.4f})")
    
    # Store in state for later logging
    _state['maker_price_bucket'] = price_bucket
    _state['maker_sec_remaining'] = seconds_remaining
    
    if token_price < MAKER_MIN_PRICE:
        log(f"[BTC-MAKER] token_mid={token_price:.4f} < min {MAKER_MIN_PRICE:.4f}, skipping")
        return None
    if token_price > SIGNAL_MAX_ENTRY_PRICE:
        log(f"[BTC-MAKER] entry={token_price:.4f} > max {SIGNAL_MAX_ENTRY_PRICE:.4f}, skipping {direction} signal")
        return None
    limit_price = max(0.01, round(token_price - MAKER_OFFSET, 2))
    shares = max(5.0, math.floor((SNIPE_DEFAULT / max(limit_price, 0.01)) * 100) / 100)
    log(f"[BTC-MAKER] {direction} signal token_mid={token_price:.4f} limit={limit_price:.4f} shares={shares:.2f} dry={MAKER_DRY_RUN}")
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
    # Send Telegram notification for order placement
    tg(f"[BTC-MAKER] ORDER PLACED: {direction} {shares:.2f} shares @ {limit_price:.4f} | Order: {r.get('order_id') or (r.get('posted') or {}).get('order_id') or ''}")
    return r.get('success')

# ── Strategy A: Arb check ─────────────────────────────────────────────────────
def check_gabagool(market, seconds_remaining=None):
    if not BTC15M_GABAGOOL_ENABLED:
        return None
    if market.get("closed"):
        return None

    yes_price = market.get("yes_price", 1.0)
    no_price = market.get("no_price", 1.0)
    now_ts = int(time.time())

    if yes_price < GABAGOOL_MAX_LEG:
        prev = _state.get("gabagool_yes_low")
        if prev is None or yes_price < prev:
            _state["gabagool_yes_low"] = yes_price
            _state["gabagool_yes_ts"] = now_ts
            log(f"[GABAGOOL] YES dip ts={now_ts} price={yes_price:.4f}")
            save_state()

    if no_price < GABAGOOL_MAX_LEG:
        prev = _state.get("gabagool_no_low")
        if prev is None or no_price < prev:
            _state["gabagool_no_low"] = no_price
            _state["gabagool_no_ts"] = now_ts
            log(f"[GABAGOOL] NO dip ts={now_ts} price={no_price:.4f}")
            save_state()

    if seconds_remaining is not None and seconds_remaining <= 5 and not _state.get("gabagool_window_logged"):
        y = _state.get("gabagool_yes_low")
        n = _state.get("gabagool_no_low")
        yts = _state.get("gabagool_yes_ts", 0)
        nts = _state.get("gabagool_no_ts", 0)
        if y is not None and n is not None:
            combined = y + n
            profit = 1.00 - combined
            log(f"[GABAGOOL SUMMARY] YES low=${y:.4f} at {yts}, NO low=${n:.4f} at {nts}, combined=${combined:.4f}, profit=${profit:.4f}")
        else:
            log(f"[GABAGOOL SUMMARY] incomplete yes={y} no={n}")
        _state["gabagool_window_logged"] = True
        save_state()
    return None

def check_arb(market):
    if _state["arb_done"]:
        return None
    combined = market["yes_price"] + market["no_price"]
    if combined >= ARB_THRESHOLD:
        log(f"[ARB] No arb. Combined={combined:.4f} >= {ARB_THRESHOLD}")
        return None

    log(f"[ARB] FOUND! YES={market['yes_price']:.4f} NO={market['no_price']:.4f} COMBINED={combined:.4f}")
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
    log(f"[ARB] Result: yes={r1.get('success')}, no={r2.get('success')}. {notes}")
    tg(f"[BTC-ARB] {notes}")

    _state["arb_done"] = True
    save_state()
    return True

# ── Strategy B: Snipe check ──────────────────────────────────────────────────
def check_snipe(market, seconds_remaining):
    if _state["snipe_done"]:
        return None
    if seconds_remaining > SNIPE_WINDOW or seconds_remaining < 5:
        return None

    btc_price = get_btc_price()
    if btc_price is None:
        return None

    if not _state["window_open_btc"]:
        log(f"[SNIPE] No window_open_btc recorded, skipping. BTC={btc_price}")
        return None

    delta_pct = (btc_price - _state["window_open_btc"]) / _state["window_open_btc"] * 100
    log(f"[SNIPE] BTC now={btc_price} window_open={_state['window_open_btc']} delta={delta_pct:+.3f}%")

    direction = None
    if delta_pct > SNIPE_DELTA_MIN:
        direction = "UP"
    elif delta_pct < -SNIPE_DELTA_MIN:
        direction = "DOWN"
    else:
        log(f"[SNIPE] Delta {delta_pct:+.3f}% too small, skipping")
        return None

    # Pick token
    token_id = market["clob_token_id"][0] if direction == "UP" else (market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else "")
    price    = market["yes_price"] if direction == "UP" else market["no_price"]

    # ── NEW FILTERS: sec_remaining and entry_price ──
    # Note: snipe window is t<30s, so we skip only extremely late entries (t<15s)
    if seconds_remaining < 15:
        log(f"[SNIPE] FILTER: sec_remaining={seconds_remaining}s < 15s, skipping ultra-late entry")
        return None
    
    # Price bucket classification and filtering
    if price >= 0.80:
        price_bucket = "high"
        log(f"[SNIPE] FILTER: entry_price={price:.4f} >= 0.80 (bucket={price_bucket}), skipping high price")
        return None
    elif price >= 0.60:
        price_bucket = "mid"
    elif price >= 0.40:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
    
    log(f"[SNIPE] PRICE_BUCKET: {price_bucket} (price={price:.4f})")

    if price > SNIPE_MAX_PRICE:
        log(f"[SNIPE] Price {price:.4f} > max {SNIPE_MAX_PRICE}, skipping")
        return None

    size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    shares = size / price
    # Ensure minimum 5 shares for Polymarket
    if shares < 5:
        shares = 5
        size = shares * price

    log(f"[SNIPE] {direction} signal! BTC delta={delta_pct:+.3f}% size=${size:.2f} price={price:.4f} shares={shares:.2f}")
    r = place_order("BUY", shares, price, market["condition_id"], token_id)
    # Log executor output so [ATTEMPT]/[RESULT] lines are visible
    for line in (r.get("output") or "").splitlines():
        if any(tag in line for tag in ("[ATTEMPT]", "[RESULT]", "Book best", "FILL")):
            log(f"[EXEC] {line.strip()}")
    if r.get("error"):
        for line in r["error"].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")
    notes = f"snipe {direction} delta={delta_pct:+.3f}% price={price:.4f} size=${size:.2f} sec_rem={seconds_remaining} price_bucket={price_bucket}"
    log(f"[SNIPE] Result: {r.get('success')} filled={r.get('filled')}. {notes}")
    if os.getenv("BTC15M_ONE_SHOT_TEST") == "1":
        _state["snipe_done"] = True
        save_state()
        log("[SNIPE] ONE_SHOT_TEST active — stopping after first BTC attempt in this window")

    if not r.get('filled') and not DRY_RUN:
        log(f"[SNIPE] No fill confirmed — not counting this trade as real")
        tg(f"[BTC-SNIPE] NO FILL confirmed | {notes}")
        return False
    tg(f"[BTC-SNIPE] FILL CONFIRMED {r.get('filled_size', shares):.2f} shares | {notes}")

    _state["snipe_done"]        = True
    _state["prev_snipe_side"]  = direction
    _state["prev_snipe_price"] = price
    _state["prev_snipe_size"]  = size
    save_state()
    return True

# ── New window setup ──────────────────────────────────────────────────────────
def setup_new_window(slug, window_ts):
    btc_price = get_btc_price()
    if btc_price is None:
        btc_price = _state.get("window_open_btc", 0.0)

    # Keep 70 minutes of price history for 1-hour trend filter
    cutoff = int(time.time()) - 4200  # 70 minutes
    old_prices = _state.get("btc_prices", [])
    _state["btc_prices"] = [(t, p) for t, p in old_prices if t >= cutoff]
    _state["btc_prices"].append((int(time.time()), btc_price))
    
    _state["window_ts"]       = window_ts
    _state["window_open_btc"] = btc_price
    _state["arb_done"]        = False
    _state["snipe_done"]      = False
    _state["maker_order_id"]  = ""
    _state["maker_token_id"]  = ""
    _state["maker_side"]      = ""
    _state["maker_price"]     = 0.0
    _state["maker_shares"]    = 0.0
    _state["maker_done"]      = False
    _state["maker_last_poll"] = 0
    _state["prev_slug"]       = slug
    save_state()
    trigger_post_resolution_tasks()

    log(f"[NEW WINDOW] ts={window_ts} BTC={btc_price} slug={slug}")
    # Window start — no telegram noise

# ── Daily loss check ───────────────────────────────────────────────────────────
def check_daily_limit():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _state.get("daily_reset", "") != today:
        _state["daily_pnl"]   = 0.0
        _state["daily_reset"] = today
        save_state()
    if _state["daily_pnl"] <= -MAX_DAILY_LOSS:
        log(f"[SAFETY] Daily loss ${abs(_state['daily_pnl']):.2f} >= ${MAX_DAILY_LOSS}, pausing until midnight UTC")
        return False
    return True

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    global _state
    load_state()
    log("=" * 60)
    log(f"[BTC-15M] STARTING {'(DRY RUN)' if DRY_RUN else '(LIVE)'}")
    log(f"[BTC-15M] Arb threshold=${ARB_THRESHOLD}, snipe delta>={SNIPE_DELTA_MIN}%, max daily loss=${MAX_DAILY_LOSS} | maker={MAKER_ENABLED} dry={MAKER_DRY_RUN} start=T-{MAKER_START_SEC} cancel=T-{MAKER_CANCEL_SEC} offset={MAKER_OFFSET}")
    tg("[BTC-15M] Engine started!")

    while True:
        now      = int(time.time())
        window_sec = now % WINDOW_SEC
        window_ts  = now - window_sec
        window_end = window_ts + WINDOW_SEC
        sec_rem   = window_end - now

        slug, _ = get_current_slug()

        # New window?
        if window_ts != _state["window_ts"]:
            log(f"[CYCLE] New window detected: {slug}")
            setup_new_window(slug, window_ts)

        # Safety
        if not check_daily_limit():
            time.sleep(60)
            continue

        # Get market
        market = get_market(slug)
        if not market:
            log(f"[CYCLE] No market found for {slug}, sleeping 10s...")
            time.sleep(10)
            continue

        log(f"[CYCLE] {market['question'][:60]} | YES={market['yes_price']:.3f} NO={market['no_price']:.3f} | {sec_rem}s left")

        # Record BTC price
        btc = get_btc_price()
        if btc:
            _state["btc_prices"].append((int(time.time()), btc))
            # Keep last window's prices only
            cutoff = window_ts
            _state["btc_prices"] = [(t, p) for t, p in _state["btc_prices"] if t >= cutoff]

        # Strategy A: Arb (first 14.75 min)
        if sec_rem > 15:
            check_arb(market)

        # Gabagool tracking (dry-run aware, no real orders unless explicitly enabled elsewhere)
        check_gabagool(market, sec_rem)

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
        log("[BTC-15M] Stopped by user")
