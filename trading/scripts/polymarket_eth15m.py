#!/usr/bin/env python3
"""
polymarket_eth15m.py — Ethereum 15-Minute Polymarket Engine (IMPROVED)
======================================================================
Changes from original:
  1. SIGNAL_MAX_ENTRY_PRICE raised 0.68 -> 0.80 (unblocks high-conviction signals)
  2. MAKER_OFFSET reduced 0.002 -> 0.001 (more competitive limit orders)
  3. Fixed ETH price trimming to consistently keep 70 min history
  4. Softened momentum deceleration threshold 0.5 -> 0.3
  5. Added maker retry after cancellation if time permits (>45s remaining)
  6. Added FOK taker fallback if maker order unfilled after 60s
  7. Relaxed 1h trend conflict threshold from 0.3% to 0.5%
  8. Added fill-rate tracking for diagnostics
  9. MAKER_START_SEC raised 300 -> 540 (catch signals before market fully prices them in)
 10. SNIPE_MIN_ENTRY_PRICE lowered 0.45 -> 0.38 (catch early confirmed signals with good value)
 11. SNIPE_MAX_ENTRY_PRICE lowered 0.82 -> 0.76 (skip expensive tokens, better risk/reward)
 12. FOK fallback price +0.02 premium above mid to hit actual ask instead of mid-price
 13. Asymmetric delta: DOWN requires 0.12% (was 0.05%) — DOWN was 38% win rate
 14. Correlation stagger: ETH waits 20s before entering, so BTC fills first and block can detect
 15. DOWN signals disabled by default (ETH DOWN was 38% win rate, -$9.96)
 16. Max entry capped at 0.55 (entries above 0.55 have negative expectancy)
 17. Re-enabled DOWN signals — ETH DOWN at 0.65 had 44% win rate (best signal Apr 2)

Dry run by default. Set DRY_RUN=false to go live.
"""
import json
import hashlib
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
MAKER_START_SEC = _int("ETH15M_MAKER_START_SEC", 540)
MAKER_CANCEL_SEC = _int("ETH15M_MAKER_CANCEL_SEC", 10)
# FIX #2: Reduced offset from 0.002 -> 0.001
MAKER_OFFSET = _float("ETH15M_MAKER_OFFSET", 0.001)
MAKER_POLL_SEC = _int("ETH15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("ETH15M_MAKER_MIN_PRICE", 0.50)
# FIX #5: FOK fallback after N seconds of unfilled maker order
MAKER_FOK_FALLBACK_SEC = _int("ETH15M_MAKER_FOK_FALLBACK_SEC", 60)
# FIX #5: Allow retry after cancellation if >N seconds remain
MAKER_RETRY_MIN_SEC = _int("ETH15M_MAKER_RETRY_MIN_SEC", 45)

# ── Config ────────────────────────────────────────────────────────────────────
WINDOW_SEC      = _int("ETH15M_WINDOW_SEC", 900)          # 15 minutes
ARB_THRESHOLD   = _float("ETH15M_ARB_THRESHOLD", 0.98)
ARB_SIZE        = _float("ETH15M_ARB_SIZE", 10.00)
SNIPE_DELTA_MIN = _float("ETH15M_SNIPE_DELTA_MIN", 0.05)
# FIX #14: Asymmetric delta — DOWN needs stronger signal (38% win rate was bad)
SNIPE_DELTA_MIN_DOWN = _float("ETH15M_SNIPE_DELTA_MIN_DOWN", 0.12)
# FIX #16: Re-enabled DOWN — ETH DOWN at 0.65 entry had 44% win rate, best signal today
DOWN_ENABLED = os.environ.get("ETH15M_DOWN_ENABLED", ENV.get("ETH15M_DOWN_ENABLED", "true")).lower() == "true"
SIGNAL_CONFIRM_COUNT = _int("ETH15M_SIGNAL_CONFIRM_COUNT", 2)
SIGNAL_CONFIRM_SEC   = _int("ETH15M_SIGNAL_CONFIRM_SEC", 15)
# FIX #17: Capped max entry at 0.55 — above 0.55 has negative expectancy
SNIPE_MIN_PRICE = _float("ETH15M_SIGNAL_MIN_ENTRY_PRICE", 0.38)
SNIPE_MAX_PRICE = _float("ETH15M_SIGNAL_MAX_ENTRY_PRICE", _float("ETH15M_SNIPE_MAX_PRICE", 0.55))
SNIPE_DEFAULT   = _float("ETH15M_SNIPE_DEFAULT_SIZE", 5.00)
SNIPE_STRONG    = _float("ETH15M_SNIPE_STRONG_SIZE", 7.50)
SNIPE_STRONG_D  = _float("ETH15M_SNIPE_STRONG_DELTA", 0.10)  # percent
SNIPE_WINDOW    = _int("ETH15M_SNIPE_WINDOW_SEC", 15)
POLL_SEC        = _int("ETH15M_PRICE_POLL_SEC", 5)
SCAN_SEC        = _int("ETH15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS  = _float("ETH15M_MAX_DAILY_LOSS", 15.00)
SERIES_ID       = "10216"
SERIES_SLUG     = "eth-up-or-down-15m"
# FIX #4: Softened momentum deceleration threshold
MOMENTUM_DECEL_THRESHOLD = _float("ETH15M_MOMENTUM_DECEL_THRESHOLD", 0.30)
# FIX #7: Relaxed 1h trend conflict threshold
HOUR_TREND_THRESHOLD = _float("ETH15M_HOUR_TREND_THRESHOLD", 0.50)

# ── State ─────────────────────────────────────────────────────────────────────
_state = {
    "window_ts":       0,
    "window_open_eth": 0.0,
    "arb_done":        False,
    "arb_logged":      False,
    "snipe_done":      False,
    "eth_prices":      [],
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
    "maker_attempt_count": 0,
    "maker_placed_ts":  0,
    "diag_orders_placed": 0,
    "diag_orders_filled": 0,
    "diag_orders_cancelled": 0,
    "notified_resolved_ids": [],
}
_positions = []

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


def notify_recent_resolutions():
    if DRY_RUN:
        return
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT id, asset, side, pnl_absolute
            FROM trades
            WHERE engine='eth15m' AND exit_type='resolved'
            ORDER BY timestamp_close DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log(f"[ETH-POST-RESOLUTION] notify_recent_resolutions failed: {e}")
        return

    seen = set(_state.get("notified_resolved_ids", []))
    notified = list(_state.get("notified_resolved_ids", []))
    new_rows = [row for row in reversed(rows) if row[0] and row[0] not in seen]
    for trade_id, asset, side, pnl_abs in new_rows:
        pnl_abs = float(pnl_abs or 0.0)
        outcome = "WIN" if pnl_abs >= 0 else "LOST"
        tg(f"[ETH-15M] {outcome}: {side or '?'} {asset or ''} | P&L ${pnl_abs:+.2f}")
        notified.append(trade_id)

    if new_rows:
        _state["notified_resolved_ids"] = notified[-100:]
        save_state()


def trigger_post_resolution_tasks():
    if DRY_RUN:
        return
    try:
        reconcile_proc = subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_reconcile.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            reconcile_proc.wait(timeout=30)
            notify_recent_resolutions()
            check_consecutive_losses()
        except subprocess.TimeoutExpired:
            log("[ETH-POST-RESOLUTION] WARNING: Reconcile timed out.")
        except Exception as e:
            log(f"[ETH-POST-RESOLUTION] WARNING: Reconcile failed ({e}).")

        subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_redeem.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"[ETH-POST-RESOLUTION] task launch error: {e}")


def check_consecutive_losses():
    if _state.get("cooldown_until", 0) < 0:
        if time.time() < abs(_state.get("cooldown_until", 0)):
            log("[ETH-LOSS-CHECK] cooldown manually reset, skipping check.")
            return
        else:
            _state["cooldown_until"] = 0

    try:
        import sqlite3
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("""
            SELECT pnl_absolute FROM trades
            WHERE engine='eth15m' AND exit_type='resolved'
            ORDER BY timestamp_close DESC LIMIT 3
        """)
        rows = c.fetchall()
        conn.close()

        if len(rows) >= 3:
            if all(float(row[0]) < 0 for row in rows):
                if _state.get("cooldown_until", 0) >= 0:
                    _state["consecutive_losses"] = 3
                    _state["cooldown_until"] = time.time() + 1800
                    save_state()
                    log("[ETH-MAKER] 3 consecutive losses — 30 min cooldown")
                    tg("[ETH-MAKER] ⚠️ 3 losses in a row — pausing 30 min")
            else:
                if _state.get("consecutive_losses", 0) > 0:
                    _state["consecutive_losses"] = 0
                    save_state()
    except Exception as e:
        log(f"[ETH-LOSS-CHECK] error: {e}")

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
    _state.setdefault('consecutive_losses', 0)
    _state.setdefault('cooldown_until', 0)
    _state.setdefault('maker_attempt_count', 0)
    _state.setdefault('maker_placed_ts', 0)
    _state.setdefault('arb_logged', False)
    _state.setdefault('diag_orders_placed', 0)
    _state.setdefault('diag_orders_filled', 0)
    _state.setdefault('diag_orders_cancelled', 0)

# ── ETH price from Coinbase ────────────────────────────────────────────────────
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
                payload['fill_check'] = fill
                payload['posted'] = data
                break
    except Exception:
        pass
    return payload

# ── Journal logging ───────────────────────────────────────────────────────────
def _current_trade_slug():
    window_ts = int(_state.get("window_ts") or 0)
    return f"eth-updown-15m-{window_ts}" if window_ts else "eth-15m"


def _current_trade_id(direction):
    slug = _current_trade_slug()
    stable = f"eth15m:{slug}:{direction or 'UNKNOWN'}"
    return hashlib.sha256(stable.encode()).hexdigest()[:32]


def _current_window_open_iso():
    window_ts = int(_state.get("window_ts") or 0)
    if not window_ts:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(window_ts, tz=timezone.utc).isoformat()


def log_trade(engine, direction, size_usd, entry_price, pnl, exit_type, hold_sec, notes="", sec_remaining=None, price_bucket=None):
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        trade_id = _current_trade_id(direction)
        timestamp_open = _current_window_open_iso()
        is_resolved = exit_type == "resolved"
        asset = _current_trade_slug()
        
        full_notes = notes
        if sec_remaining is not None:
            full_notes = f"{full_notes} sec_rem={sec_remaining}"
        if price_bucket:
            full_notes = f"{full_notes} price_bucket={price_bucket}"
        
        c.execute("""
            INSERT INTO trades (
                id, engine, timestamp_open, timestamp_close, asset,
                category, direction, entry_price, exit_price,
                position_size, position_size_usd, pnl_absolute, pnl_percent,
                exit_type, hold_duration_seconds, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            engine, timestamp_open,
            datetime.now(timezone.utc).isoformat() if is_resolved else None,
            asset,
            "eth-updown",
            direction, entry_price, entry_price if is_resolved else None,
            size_usd / entry_price if entry_price else 0,
            size_usd, pnl or 0.0, (pnl/size_usd*100) if size_usd else 0,
            exit_type or "open", hold_sec or 0,
            "normal", full_notes
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Journal error: {e}")


def init_active_fills_db():
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS active_fills (
                engine TEXT NOT NULL,
                window_ts INTEGER NOT NULL,
                direction TEXT NOT NULL,
                fill_timestamp TEXT NOT NULL,
                PRIMARY KEY (engine, window_ts, direction)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_active_fills_window_dir
            ON active_fills (window_ts, direction)
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[ETH] active_fills init error: {e}")


def prune_active_fills(current_window_ts):
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("DELETE FROM active_fills WHERE window_ts < ?", (int(current_window_ts),))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[ETH] active_fills prune error: {e}")


def record_active_fill(engine, window_ts, direction):
    import sqlite3
    try:
        if not window_ts or not direction:
            return
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO active_fills (engine, window_ts, direction, fill_timestamp)
            VALUES (?, ?, ?, ?)
        """, (engine, int(window_ts), direction, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[ETH] active_fills record error: {e}")


def peer_has_same_direction_fill(peer_engine, window_ts, direction):
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("""
            SELECT 1 FROM active_fills
            WHERE engine=? AND window_ts=? AND direction=?
            LIMIT 1
        """, (peer_engine, int(window_ts), direction))
        row = c.fetchone()
        conn.close()
        return bool(row)
    except Exception as e:
        log(f"[ETH] active_fills peer check error: {e}")
        return False


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


def _execution_anomaly_check(submitted_price, avg_fill_price):
    try:
        if submitted_price is None or avg_fill_price is None:
            return
        sp = float(submitted_price)
        fp = float(avg_fill_price)
        if fp > sp and abs(fp - sp) > 0.05:
            log(f"[EXECUTION_ANOMALY] ADVERSE: submitted={sp:.4f} filled={fp:.4f}")
            tg(f"[ALERT] Execution anomaly! BUY filled ABOVE limit: submitted={sp:.4f} filled={fp:.4f}")
        elif abs(fp - sp) > 0.05:
            log(f"[EXECUTION_ANOMALY] FAVORABLE: submitted={sp:.4f} filled={fp:.4f} (saved ${abs(fp-sp)*100:.1f}%)")
    except Exception:
        pass


def maker_cancel_order(order_id):
    cmd = [str(VENV_PY), str(WORK_DIR / "scripts" / "polymarket_executor.py"), "cancel", str(order_id)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {"success": result.returncode == 0, "output": result.stdout, "error": result.stderr}


def _reset_maker_state_for_retry():
    """Reset maker state to allow a retry within the same window."""
    _state['maker_order_id'] = ""
    _state['maker_token_id'] = ""
    _state['maker_side'] = ""
    _state['maker_price'] = 0.0
    _state['maker_shares'] = 0.0
    _state['maker_done'] = False
    _state['maker_last_poll'] = 0
    _state['maker_placed_ts'] = 0
    save_state()


def _handle_maker_fill(oid, source="status", vf=None):
    """Centralized fill handling for maker orders."""
    fill_side = _state.get('maker_side')
    fill_price = _state.get('maker_price', 0)
    fill_shares = _state.get('maker_shares', 0)
    fill_placed_ts = _state.get('maker_placed_ts', int(time.time()))
    fill_sec_remaining = _state.get('maker_sec_remaining')
    fill_price_bucket = _state.get('maker_price_bucket')

    _state['maker_done'] = True
    _state['snipe_done'] = True
    _state['consecutive_losses'] = 0
    _state['diag_orders_filled'] = _state.get('diag_orders_filled', 0) + 1
    record_active_fill("eth15m", _state.get("window_ts"), fill_side)
    _state['maker_order_id'] = ""
    _state['maker_token_id'] = ""
    _state['maker_side'] = ""
    _state['maker_price'] = 0.0
    _state['maker_shares'] = 0.0
    _state['maker_last_poll'] = 0
    _state['maker_placed_ts'] = 0
    save_state()
    
    if source == "status":
        tg(f"[ETH-MAKER] FILLED order {oid}")
        log_trade("eth15m", fill_side or 'UP',
            fill_shares * fill_price,
            fill_price, 0, 'filled',
            int(time.time()) - fill_placed_ts,
            notes=f"maker_fill oid={oid}",
            sec_remaining=fill_sec_remaining,
            price_bucket=fill_price_bucket)
    elif source == "verify" and vf:
        avg_fill_price = vf.get('avg_fill_price')
        effective_cost = vf.get('effective_cost')
        tx_hashes = vf.get('tx_hashes') or []
        log(f"[ETH-MAKER] fill verified via {vf.get('source')} size={vf.get('filled_size')} "
            f"submitted_limit={fill_price:.4f} "
            f"avg_fill_price={(f'{float(avg_fill_price):.4f}' if avg_fill_price is not None else 'n/a')} "
            f"effective_cost={(f'${float(effective_cost or 0):.4f}' if effective_cost is not None else 'n/a')} "
            f"tx_hashes={','.join(tx_hashes[:3]) if tx_hashes else 'n/a'}")
        _execution_anomaly_check(fill_price, avg_fill_price)
        tg(f"[ETH-MAKER] FILL VERIFIED {vf.get('filled_size')} shares")
        log_trade("eth15m", fill_side,
            vf.get('filled_size', 0) * fill_price,
            fill_price, 0, 'filled',
            int(time.time()) - fill_placed_ts,
            notes=f"maker_verify oid={oid} size={vf.get('filled_size')}",
            sec_remaining=fill_sec_remaining,
            price_bucket=fill_price_bucket)
    elif source == "fok_fallback":
        tg(f"[ETH-MAKER] FOK FALLBACK FILL")
        log_trade("eth15m", fill_side or 'UP',
            fill_shares * fill_price,
            fill_price, 0, 'filled',
            int(time.time()) - fill_placed_ts,
            notes=f"fok_fallback oid={oid}",
            sec_remaining=fill_sec_remaining,
            price_bucket=fill_price_bucket)


def check_maker_snipe(market, seconds_remaining):
    if not MAKER_ENABLED:
        return None
    if time.time() < _state.get("cooldown_until", 0):
        log(f"[ETH-MAKER] cooldown active, {int(_state['cooldown_until'] - time.time())}s remaining")
        return None

    oid = _state.get("maker_order_id")
    
    # ── Manage existing order ──
    if oid and (time.time() - _state.get("maker_last_poll", 0) >= MAKER_POLL_SEC or seconds_remaining <= MAKER_CANCEL_SEC):
        _state["maker_last_poll"] = int(time.time())
        st = maker_order_status(oid)
        log(f"[ETH-MAKER] status order_id={oid} -> {st.get('status')}")
        
        if st.get('status') in ('filled', 'partially_filled'):
            _handle_maker_fill(oid, source="status")
            return True
        
        if st.get('status') == 'not_found':
            vf = maker_verify_fill(_state.get('maker_token_id'), _state.get('maker_shares', 0))
            if vf.get('filled'):
                fill_key = f"{oid}:{vf.get('filled_size')}"
                seen = set(_state.get('maker_seen_fills', []))
                if fill_key not in seen:
                    _handle_maker_fill(oid, source="verify", vf=vf)
                    seen.add(fill_key)
                    _state['maker_seen_fills'] = list(seen)[-200:]
                else:
                    _state['maker_done'] = True
                    _state['snipe_done'] = True
                    save_state()
                return True
        
        # FIX #6: FOK fallback
        placed_ts = _state.get('maker_placed_ts', 0)
        if (st.get('status') == 'open'
            and placed_ts > 0
            and (int(time.time()) - placed_ts) >= MAKER_FOK_FALLBACK_SEC
            and seconds_remaining > MAKER_CANCEL_SEC + 5):
            
            log(f"[ETH-MAKER] FOK FALLBACK: maker open for {int(time.time()) - placed_ts}s, converting to FOK taker")
            maker_cancel_order(oid)
            tg(f"[ETH-MAKER] ORDER CANCELLED (FOK fallback): {oid} | open_for={int(time.time()) - placed_ts}s")
            
            # FIX #12: Add +0.02 premium above mid to hit actual ask (mid-price FOKs were not filling)
            FOK_PREMIUM = 0.02
            fok_price = round(min((market['yes_price'] if _state.get('maker_side') == 'UP' else market['no_price']) + FOK_PREMIUM, SNIPE_MAX_PRICE), 4)
            fok_token = _state.get('maker_token_id')
            fok_shares = _state.get('maker_shares', 5.0)
            
            r = place_order("BUY", fok_shares, fok_price, market['condition_id'], fok_token)
            for line in (r.get('output') or '').splitlines():
                if line.strip() and any(tag in line for tag in ('[ATTEMPT', '[RESULT', 'FILL')):
                    log(f"[EXEC] {line.strip()}")
            
            if r.get('filled'):
                _handle_maker_fill(oid, source="fok_fallback")
                return True
            else:
                log(f"[ETH-MAKER] FOK fallback no fill, will retry if time permits")
                _state['diag_orders_cancelled'] = _state.get('diag_orders_cancelled', 0) + 1
                if seconds_remaining > MAKER_RETRY_MIN_SEC and _state.get('maker_attempt_count', 0) < 3:
                    _state['maker_attempt_count'] = _state.get('maker_attempt_count', 0) + 1
                    log(f"[ETH-MAKER] RETRY enabled (attempt {_state['maker_attempt_count']}/3, {seconds_remaining}s remaining)")
                    _reset_maker_state_for_retry()
                    return None
                else:
                    _state['maker_done'] = True
                    save_state()
                    return False
        
        # Cancel at deadline
        if seconds_remaining <= MAKER_CANCEL_SEC and st.get('status') in ('open', 'partially_filled'):
            maker_cancel_order(oid)
            _state['diag_orders_cancelled'] = _state.get('diag_orders_cancelled', 0) + 1
            log(f"[ETH-MAKER] cancel by deadline order_id={oid} sec_rem={seconds_remaining}")
            tg(f"[ETH-MAKER] ORDER CANCELLED: {oid} | sec_rem={seconds_remaining}")
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
    # FIX #14/16: UP only by default. DOWN disabled (38% win rate).
    if delta_pct > SNIPE_DELTA_MIN:
        direction = 'UP'
    elif DOWN_ENABLED and delta_pct < -SNIPE_DELTA_MIN_DOWN:
        direction = 'DOWN'
    else:
        direction = None
    if not direction:
        return None

    # FIX #7: Relaxed 1h trend filter threshold
    prices = _state.get("eth_prices", [])
    hour_ago_ts = int(time.time()) - 3600
    hour_ago_prices = [p for t, p in prices if t <= hour_ago_ts + 60 and t >= hour_ago_ts - 60]
    if hour_ago_prices:
        hour_delta = (base_price - hour_ago_prices[0]) / hour_ago_prices[0] * 100
        if direction == "UP" and hour_delta < -HOUR_TREND_THRESHOLD:
            log(f"[ETH-MAKER] 1h trend DOWN ({hour_delta:+.3f}%) conflicts with UP signal, skipping")
            return None
        if direction == "DOWN" and hour_delta > HOUR_TREND_THRESHOLD:
            log(f"[ETH-MAKER] 1h trend UP ({hour_delta:+.3f}%) conflicts with DOWN signal, skipping")
            return None

    now_ts = int(time.time())
    cutoff_ts = now_ts - SIGNAL_CONFIRM_SEC
    recent_prices = [(t, p) for t, p in _state.get('eth_prices', []) if t >= cutoff_ts]
    if len(recent_prices) < SIGNAL_CONFIRM_COUNT:
        log(f"[ETH-MAKER] Confirm: only {len(recent_prices)}/{SIGNAL_CONFIRM_COUNT} samples, waiting")
        return None
    confirmations = 0
    for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]:
        d = (p - _state['window_open_eth']) / _state['window_open_eth'] * 100
        if (direction == 'UP' and d > SNIPE_DELTA_MIN) or (direction == 'DOWN' and d < -SNIPE_DELTA_MIN_DOWN):
            confirmations += 1
    if confirmations < SIGNAL_CONFIRM_COUNT:
        log(f"[ETH-MAKER] Confirm: {confirmations}/{SIGNAL_CONFIRM_COUNT}, skipping")
        return None
    recent_avg = sum(p for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]) / len(recent_prices[-SIGNAL_CONFIRM_COUNT:])
    momentum = (recent_avg - _state['window_open_eth']) / _state['window_open_eth'] * 100

    # FIX #4: Softened momentum deceleration threshold
    recent = [(t, p) for t, p in prices if t >= now_ts - 120]
    if len(recent) >= 6:
        mid = len(recent) // 2
        first_half = recent[:mid]
        second_half = recent[mid:]
        delta_first = (first_half[-1][1] - first_half[0][1]) / first_half[0][1] * 100
        delta_second = (second_half[-1][1] - second_half[0][1]) / second_half[0][1] * 100
        if abs(delta_second) < abs(delta_first) * MOMENTUM_DECEL_THRESHOLD:
            log(f"[ETH-MAKER] momentum decelerating first={delta_first:+.4f}% second={delta_second:+.4f}% (threshold={MOMENTUM_DECEL_THRESHOLD}), skipping")
            return None

    MOMENTUM_MIN = SNIPE_DELTA_MIN * 1.50
    if abs(momentum) < MOMENTUM_MIN:
        log(f"[ETH-MAKER] momentum={momentum:+.3f}% < {MOMENTUM_MIN:.3f}%, skipping (weak)")
        return None
    log(f"[ETH-MAKER] CONFIRMED {confirmations}/{SIGNAL_CONFIRM_COUNT} | momentum={momentum:+.3f}%")

    current_window_ts = _state.get("window_ts")
    if current_window_ts and peer_has_same_direction_fill("btc15m", current_window_ts, direction):
        log(f"[ETH-MAKER] CORRELATION BLOCK: BTC already filled {direction} in window {current_window_ts}, skipping")
        return None
    # FIX #15: Stagger ETH entry — wait 20s into maker window before placing, so BTC fills first
    # and the correlation block can detect it. Prevents both engines entering same direction simultaneously.
    CORRELATION_STAGGER_SEC = 20
    maker_window_elapsed = (MAKER_START_SEC - seconds_remaining)
    if maker_window_elapsed < CORRELATION_STAGGER_SEC:
        log(f"[ETH-MAKER] CORRELATION STAGGER: waiting {CORRELATION_STAGGER_SEC - maker_window_elapsed:.0f}s for BTC to fill first")
        return None

    token_id = market['clob_token_id'][0] if direction == 'UP' else (market['clob_token_id'][1] if len(market['clob_token_id']) > 1 else '')
    token_price = market['yes_price'] if direction == 'UP' else market['no_price']

    if seconds_remaining < 15:
        log(f"[ETH-MAKER] FILTER: sec_remaining={seconds_remaining}s < 15s, skipping ultra-late entry")
        return None

    # FIX #1: Raised max entry price
    if token_price > SNIPE_MAX_PRICE:
        price_bucket = "high"
        log(f"[ETH-MAKER] FILTER: entry_price={token_price:.4f} > max {SNIPE_MAX_PRICE:.2f} (bucket={price_bucket}), skipping high price")
        return None
    elif token_price >= 0.60:
        price_bucket = "mid"
    elif token_price >= SNIPE_MIN_PRICE:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
        log(f"[ETH-MAKER] FILTER: entry_price={token_price:.4f} < min {SNIPE_MIN_PRICE:.2f} (bucket={price_bucket}), skipping low price")
        return None

    log(f"[ETH-MAKER] PRICE_BUCKET: {price_bucket} (price={token_price:.4f})")
    _state['maker_price_bucket'] = price_bucket
    _state['maker_sec_remaining'] = seconds_remaining

    if token_price < MAKER_MIN_PRICE:
        log(f"[ETH-MAKER] token_mid={token_price:.4f} < min {MAKER_MIN_PRICE:.4f}, skipping")
        return None

    # FIX #2: More competitive limit price
    limit_price = max(0.01, round(token_price - MAKER_OFFSET, 2))
    shares = max(5.0, math.floor((SNIPE_DEFAULT / max(limit_price, 0.01)) * 100) / 100)
    log(f"[ETH-MAKER] {direction} signal token_mid={token_price:.4f} limit={limit_price:.4f} shares={shares:.2f} dry={MAKER_DRY_RUN} attempt={_state.get('maker_attempt_count', 0)+1}")
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
    _state['maker_placed_ts'] = int(time.time())
    _state['maker_done'] = False
    _state['diag_orders_placed'] = _state.get('diag_orders_placed', 0) + 1
    save_state()
    tg(f"[ETH-MAKER] ORDER PLACED: {direction} {shares:.2f} shares @ {limit_price:.4f} | Order: {r.get('order_id') or (r.get('posted') or {}).get('order_id') or ''}")
    return r.get('success')

# ── Strategy A: Arb check ─────────────────────────────────────────────────────
def check_arb(market):
    if _state["arb_done"]:
        return None
    combined = market["yes_price"] + market["no_price"]
    if combined >= ARB_THRESHOLD:
        # FIX #7: Only log arb miss once per window
        if not _state.get("arb_logged"):
            log(f"[ETH-ARB] No arb this window. Combined={combined:.4f} >= {ARB_THRESHOLD}")
            _state["arb_logged"] = True
        return None

    log(f"[ETH-ARB] FOUND! YES={market['yes_price']:.4f} NO={market['no_price']:.4f} COMBINED={combined:.4f}")
    yes_tid = market["clob_token_id"][0] if len(market["clob_token_id"]) > 0 else ""
    no_tid  = market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else ""

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
    if time.time() < _state.get("cooldown_until", 0):
        log(f"[ETH-SNIPE] cooldown active, {int(_state['cooldown_until'] - time.time())}s remaining")
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

    # FIX #14: Asymmetric delta — DOWN requires stronger signal
    # FIX #16: DOWN disabled by default
    direction = None
    if delta_pct > SNIPE_DELTA_MIN:
        direction = "UP"
    elif DOWN_ENABLED and delta_pct < -SNIPE_DELTA_MIN_DOWN:
        direction = "DOWN"
    else:
        log(f"[ETH-SNIPE] Delta {delta_pct:+.3f}% — no valid signal (DOWN_ENABLED={DOWN_ENABLED})")
        return None

    # FIX #7: Relaxed 1h trend threshold
    prices = _state.get("eth_prices", [])
    hour_ago_ts = int(time.time()) - 3600
    hour_ago_prices = [p for t, p in prices if t <= hour_ago_ts + 60 and t >= hour_ago_ts - 60]
    if hour_ago_prices:
        hour_delta = (eth_price - hour_ago_prices[0]) / hour_ago_prices[0] * 100
        if direction == "UP" and hour_delta < -HOUR_TREND_THRESHOLD:
            log(f"[ETH-SNIPE] 1h trend DOWN ({hour_delta:+.3f}%) conflicts with UP signal, skipping")
            return None
        if direction == "DOWN" and hour_delta > HOUR_TREND_THRESHOLD:
            log(f"[ETH-SNIPE] 1h trend UP ({hour_delta:+.3f}%) conflicts with DOWN signal, skipping")
            return None

    # FIX #4: Softened momentum deceleration
    recent = [(t, p) for t, p in prices if t >= int(time.time()) - 120]
    if len(recent) >= 6:
        mid = len(recent) // 2
        first_half = recent[:mid]
        second_half = recent[mid:]
        delta_first = (first_half[-1][1] - first_half[0][1]) / first_half[0][1] * 100
        delta_second = (second_half[-1][1] - second_half[0][1]) / second_half[0][1] * 100
        if abs(delta_second) < abs(delta_first) * MOMENTUM_DECEL_THRESHOLD:
            log(f"[ETH-SNIPE] momentum decelerating first={delta_first:+.4f}% second={delta_second:+.4f}% (threshold={MOMENTUM_DECEL_THRESHOLD}), skipping")
            return None

    current_window_ts = _state.get("window_ts")
    if current_window_ts and peer_has_same_direction_fill("btc15m", current_window_ts, direction):
        log(f"[ETH-SNIPE] CORRELATION BLOCK: BTC already filled {direction} in window {current_window_ts}, skipping")
        return None

    token_id = market["clob_token_id"][0] if direction == "UP" else (market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else "")
    price    = market["yes_price"] if direction == "UP" else market["no_price"]

    if seconds_remaining < 15:
        log(f"[ETH-SNIPE] FILTER: sec_remaining={seconds_remaining}s < 15s, skipping ultra-late entry")
        return None

    # FIX #1: Raised max entry
    if price > SNIPE_MAX_PRICE:
        price_bucket = "high"
        log(f"[ETH-SNIPE] FILTER: entry_price={price:.4f} > max {SNIPE_MAX_PRICE:.2f} (bucket={price_bucket}), skipping high price")
        return None
    elif price >= 0.60:
        price_bucket = "mid"
    elif price >= SNIPE_MIN_PRICE:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
        log(f"[ETH-SNIPE] FILTER: entry_price={price:.4f} < floor {SNIPE_MIN_PRICE:.2f} (bucket={price_bucket}), skipping low price")
        return None

    log(f"[ETH-SNIPE] PRICE_BUCKET: {price_bucket} (price={price:.4f})")

    size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    shares = size / price
    if shares < 5:
        shares = 5
        size = shares * price

    log(f"[ETH-SNIPE] {direction} signal! ETH delta={delta_pct:+.3f}% size=${size:.2f} price={price:.4f} shares={shares:.2f}")
    r = place_order("BUY", shares, price, market["condition_id"], token_id)
    for line in (r.get("output") or "").splitlines():
        if any(tag in line for tag in ("[ATTEMPT]", "[RESULT]", "Book best", "FILL")):
            log(f"[EXEC] {line.strip()}")
    if r.get("error"):
        for line in r["error"].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")
    notes = f"snipe {direction} delta={delta_pct:+.3f}% price={price:.4f} size=${size:.2f} sec_rem={seconds_remaining} price_bucket={price_bucket}"
    log(f"[ETH-SNIPE] Result: {r.get('success')} filled={r.get('filled')}. {notes}")
    if not r.get('filled') and not DRY_RUN:
        log(f"[ETH-SNIPE] No fill confirmed — not counting this trade as real")
        tg(f"[ETH-SNIPE] NO FILL confirmed | {notes}")
        return False
    fill = r.get('fill_check') or {}
    avg_fill_price = fill.get('avg_fill_price')
    effective_cost = fill.get('effective_cost')
    tx_hashes = fill.get('tx_hashes') or []
    if avg_fill_price is not None:
        log(f"[ETH-SNIPE] fill verified via {fill.get('source')} size={r.get('filled_size', shares)} submitted_price={price:.4f} avg_fill_price={float(avg_fill_price):.4f} effective_cost=${float(effective_cost or 0):.4f} tx_hashes={','.join(tx_hashes[:3]) if tx_hashes else 'n/a'}")
        _execution_anomaly_check(price, avg_fill_price)
    tg(f"[ETH-SNIPE] FILL CONFIRMED {r.get('filled_size', shares):.2f} shares | {notes}")
    record_active_fill("eth15m", _state.get("window_ts"), direction)

    _state["snipe_done"]        = True
    _state["prev_snipe_side"]  = direction
    _state["prev_snipe_price"] = price
    _state["prev_snipe_size"]  = size
    save_state()
    return True

# ── New window setup ──────────────────────────────────────────────────────────
def setup_new_window(slug, window_ts):
    prune_active_fills(window_ts)
    eth_price = get_eth_price()
    if eth_price is None:
        eth_price = _state.get("window_open_eth", 0.0)

    cutoff = int(time.time()) - 4200  # 70 minutes
    old_prices = _state.get("eth_prices", [])
    _state["eth_prices"] = [(t, p) for t, p in old_prices if t >= cutoff]
    if eth_price:
        _state["eth_prices"].append((int(time.time()), eth_price))

    _state["window_ts"]       = window_ts
    _state["window_open_eth"] = eth_price
    _state["arb_done"]        = False
    _state["arb_logged"]      = False
    _state["snipe_done"]      = False
    _state["maker_order_id"]  = ""
    _state["maker_token_id"]  = ""
    _state["maker_side"]      = ""
    _state["maker_price"]     = 0.0
    _state["maker_shares"]    = 0.0
    _state["maker_done"]      = False
    _state["maker_last_poll"] = 0
    _state["maker_placed_ts"] = 0
    _state["maker_attempt_count"] = 0
    _state["prev_slug"]       = slug
    save_state()

    log(f"[ETH-NEW WINDOW] ts={window_ts} ETH={eth_price} slug={slug}")

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
    init_active_fills_db()
    log("=" * 60)
    log(f"[ETH-15M] STARTING {'(DRY RUN)' if DRY_RUN else '(LIVE)'}")
    log(f"[ETH-15M] Arb threshold=${ARB_THRESHOLD}, snipe delta>={SNIPE_DELTA_MIN}%, max daily loss=${MAX_DAILY_LOSS}")
    log(f"[ETH-15M] min_entry={SNIPE_MIN_PRICE:.2f} max_entry={SNIPE_MAX_PRICE:.2f} maker={MAKER_ENABLED} dry={MAKER_DRY_RUN} start=T-{MAKER_START_SEC} cancel=T-{MAKER_CANCEL_SEC} offset={MAKER_OFFSET}")
    log(f"[ETH-15M] fok_fallback={MAKER_FOK_FALLBACK_SEC}s retry_min={MAKER_RETRY_MIN_SEC}s decel_threshold={MOMENTUM_DECEL_THRESHOLD} 1h_trend_threshold={HOUR_TREND_THRESHOLD}")
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
            # FIX #8: Log fill rate diagnostics
            placed = _state.get('diag_orders_placed', 0)
            filled = _state.get('diag_orders_filled', 0)
            cancelled = _state.get('diag_orders_cancelled', 0)
            if placed > 0:
                log(f"[DIAG] Window fill rate: {filled}/{placed} filled, {cancelled} cancelled ({filled/placed*100:.0f}% fill rate)")
            
            log(f"[ETH-CYCLE] New window detected: {slug}")
            trigger_post_resolution_tasks()
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

        # Record ETH price — FIX #3: consistent 70-min history retention
        eth = get_eth_price()
        if eth:
            _state["eth_prices"].append((int(time.time()), eth))
            cutoff = int(time.time()) - 4200  # 70 minutes
            _state["eth_prices"] = [(t, p) for t, p in _state["eth_prices"] if t >= cutoff]

        # Strategy A: Arb
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
