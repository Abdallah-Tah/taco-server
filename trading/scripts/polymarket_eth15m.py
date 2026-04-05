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
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from journal import journal_close as shared_journal_close
from journal import journal_open as shared_journal_open
from journal import open_journal
from polymarket_clob_pricing import fetch_book, choose_buy_price

# ── Paths & creds ──────────────────────────────────────────────────────────────
WORK_DIR      = Path("/home/abdaltm86/.openclaw/workspace/trading")
JOURNAL_DB    = WORK_DIR / "journal.db"
SHARED_JOURNAL_DB = Path(os.environ.get("ETH15M_JOURNAL_DB", str(WORK_DIR / "eth15m_journal.db")))
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


def _bool(key, default):
    return str(os.environ.get(key, ENV.get(key, str(default)))).lower() in ("1", "true", "yes", "on")

TELEGRAM_TOKEN = ENV.get("TELEGRAM_TOKEN", "8457917317:AAHGueV-SogZl14cW5uMmIACpaWuyzByXOo")
CHAT_ID        = ENV.get("CHAT_ID",        "7520899464")
POLY_WALLET    = ENV.get("POLY_WALLET",    "0x1a4c163a134D7154ebD5f7359919F9c439424f00")
VENV_PY        = Path("/home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3")
DRY_RUN        = os.environ.get("ETH15M_DRY_RUN", ENV.get("ETH15M_DRY_RUN", "true")).lower() != "false"
USE_SHARED_JOURNAL = _bool("ETH15M_SHARED_JOURNAL", False)
MAKER_ENABLED  = os.environ.get("ETH15M_MAKER_ENABLED", ENV.get("ETH15M_MAKER_ENABLED", "false")).lower() == "true"
MAKER_DRY_RUN  = os.environ.get("ETH15M_MAKER_DRY_RUN", ENV.get("ETH15M_MAKER_DRY_RUN", "true")).lower() != "false"
MAKER_START_SEC = _int("ETH15M_MAKER_START_SEC", 540)
MAKER_CANCEL_SEC = _int("ETH15M_MAKER_CANCEL_SEC", 10)
# FIX #2: Reduced offset from 0.002 -> 0.001
MAKER_OFFSET = _float("ETH15M_MAKER_OFFSET", 0.001)
MAKER_POLL_SEC = _int("ETH15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("ETH15M_MAKER_MIN_PRICE", 0.38)
# FIX #5: FOK fallback after N seconds of unfilled maker order
MAKER_FOK_FALLBACK_SEC = _int("ETH15M_MAKER_FOK_FALLBACK_SEC", 60)
# FIX #5: Allow retry after cancellation if >N seconds remain
MAKER_RETRY_MIN_SEC = _int("ETH15M_MAKER_RETRY_MIN_SEC", 45)
MAKER_MAX_RETRIES = _int("ETH15M_MAKER_MAX_RETRIES", 1)

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
EXEC_SPREAD_CAP = _float("ETH15M_EXEC_SPREAD_CAP", 0.10)
SNIPE_DEFAULT   = _float("ETH15M_SNIPE_DEFAULT_SIZE", 5.00)
SNIPE_STRONG    = _float("ETH15M_SNIPE_STRONG_SIZE", 7.50)
SNIPE_STRONG_D  = _float("ETH15M_SNIPE_STRONG_DELTA", 0.10)  # percent
ROLLING_RISK_ENABLED = _bool("ETH15M_ROLLING_RISK_ENABLED", True)
ROLLING_RISK_LOOKBACK = _int("ETH15M_ROLLING_RISK_LOOKBACK", 20)
ROLLING_RISK_MIN_SAMPLE = _int("ETH15M_ROLLING_RISK_MIN_SAMPLE", 10)
ROLLING_RISK_MULTIPLIER = _float("ETH15M_ROLLING_RISK_MULTIPLIER", 0.60)
ROLLING_RISK_REFRESH_SEC = _int("ETH15M_ROLLING_RISK_REFRESH_SEC", 120)
SNIPE_WINDOW    = _int("ETH15M_SNIPE_WINDOW_SEC", 45)
MIN_ENTRY_SEC   = _int("ETH15M_MIN_ENTRY_SEC", 8)
LATE_REVERSAL_SEC = _int("ETH15M_LATE_REVERSAL_SEC", 45)
LATE_REVERSAL_MAX_PRICE = _float("ETH15M_LATE_REVERSAL_MAX_PRICE", 0.35)
LATE_REVERSAL_CONFIRM_COUNT = _int("ETH15M_LATE_REVERSAL_CONFIRM_COUNT", 1)
POLL_SEC        = _int("ETH15M_PRICE_POLL_SEC", 5)
SCAN_SEC        = _int("ETH15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS  = _float("ETH15M_MAX_DAILY_LOSS", 15.00)
SERIES_ID       = "10216"
SERIES_SLUG     = "eth-up-or-down-15m"
# FIX #4: Softened momentum deceleration threshold
MOMENTUM_DECEL_THRESHOLD = _float("ETH15M_MOMENTUM_DECEL_THRESHOLD", 0.30)
MOMENTUM_MIN_MULTIPLIER = _float("ETH15M_MOMENTUM_MIN_MULTIPLIER", 1.0)
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
    "maker_book_bid":   None,
    "maker_book_ask":   None,
    "maker_book_spread": None,
    "maker_book_tick":  0.01,
    "maker_distance_to_ask": None,
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


def safe_log(msg):
    try:
        log(msg)
    except Exception:
        print(msg, flush=True)

def tg(msg):
    if DRY_RUN:
        return
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log("[TG-SKIP] Missing TELEGRAM_TOKEN or CHAT_ID")
        return
    log(f"[TG] Sending: {msg}")
    is_maker_msg = msg.startswith("[ETH-MAKER]")
    def _send():
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            if r.status_code != 200:
                log(f"[TG-ERR] code={r.status_code} response={r.text}")
            elif is_maker_msg:
                log("[TG] Delivered maker alert")
        except Exception as e:
            log(f"[TG-ERR] {e}")
    threading.Thread(target=_send, daemon=not is_maker_msg).start()


def notify_recent_resolutions():
    if DRY_RUN:
        return
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT id, asset, direction, pnl_absolute
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
    for trade_id, asset, direction, pnl_abs in new_rows:
        pnl_abs = float(pnl_abs or 0.0)
        outcome = "WIN" if pnl_abs >= 0 else "LOST"
        tg(f"[ETH-15M] {outcome}: {direction or '?'} {asset or ''} | P&L ${pnl_abs:+.2f}")
        notified.append(trade_id)

    if new_rows:
        _state["notified_resolved_ids"] = notified[-100:]
        save_state()


def trigger_post_resolution_tasks():
    if DRY_RUN:
        return
    try:
        reconcile_started = time.time()
        reconcile_proc = subprocess.Popen(
            [str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_reconcile.py'), '--fast-post-resolution'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            reconcile_proc.wait(timeout=25)
            elapsed = time.time() - reconcile_started
            log(f"[ETH-POST-RESOLUTION] Reconcile completed rc={reconcile_proc.returncode} elapsed={elapsed:.2f}s")
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
    _state.setdefault('maker_book_bid', None)
    _state.setdefault('maker_book_ask', None)
    _state.setdefault('maker_book_spread', None)
    _state.setdefault('maker_book_tick', 0.01)
    _state.setdefault('maker_distance_to_ask', None)
    _state.setdefault('arb_logged', False)
    _state.setdefault('diag_orders_placed', 0)
    _state.setdefault('diag_orders_filled', 0)
    _state.setdefault('diag_orders_cancelled', 0)

def detect_late_reversal(prices, window_open, direction, seconds_remaining):
    if direction != "UP" or seconds_remaining > LATE_REVERSAL_SEC:
        return False
    recent = [(t, p) for t, p in prices if t >= int(time.time()) - 90]
    if len(recent) < 3 or not window_open:
        return False
    deltas = [((p - window_open) / window_open) * 100 for _, p in recent]
    current_delta = deltas[-1]
    min_delta = min(deltas)
    if current_delta < (SNIPE_DELTA_MIN * 0.65):
        return False
    if min_delta > -(SNIPE_DELTA_MIN * 0.10):
        return False
    return (current_delta - min_delta) >= (SNIPE_DELTA_MIN * 0.90)

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


def _edge_event_id(signal_type: str, side: str, seconds_remaining: int | None) -> str:
    seed = f"eth15m:{_current_trade_slug()}:{signal_type}:{side or ''}:{seconds_remaining}:{time.time_ns()}"
    return hashlib.sha256(seed.encode()).hexdigest()


def write_edge_event(
    market,
    *,
    signal_type: str,
    side: str = "",
    intended_entry_price: float | None = None,
    decision: str = "",
    skip_reason: str = "",
    execution_status: str = "",
    seconds_remaining: int | None = None,
    shadow_decision: str = "",
    shadow_skip_reason: str = "",
    actual_fill_price: float | None = None,
    best_bid: float | None = None,
    best_ask: float | None = None,
    spread: float | None = None,
    midprice: float | None = None,
):
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute(
            """
            INSERT INTO edge_events (
                id, engine, asset, timestamp_et, market_slug, market_id, side, signal_type,
                seconds_remaining, best_bid, best_ask, spread, midprice,
                model_p_yes, model_p_no, regime_ok, skip_reason,
                intended_entry_price, actual_fill_price, slippage, decision, shadow_decision,
                shadow_skip_reason, execution_status, regime
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _edge_event_id(signal_type, side, seconds_remaining),
                "eth15m",
                "ETH",
                datetime.now(timezone.utc).astimezone().isoformat(),
                _current_trade_slug(),
                str(market.get("id") or ""),
                side,
                signal_type,
                seconds_remaining,
                best_bid,
                best_ask,
                spread,
                midprice,
                float(market.get("yes_price") or 0.0),
                float(market.get("no_price") or 0.0),
                1,
                skip_reason or None,
                float(intended_entry_price or 0.0) if intended_entry_price is not None else None,
                float(actual_fill_price or 0.0) if actual_fill_price is not None else None,
                (
                    float(actual_fill_price) - float(intended_entry_price)
                    if actual_fill_price is not None and intended_entry_price is not None
                    else None
                ),
                decision or None,
                shadow_decision or None,
                shadow_skip_reason or None,
                execution_status or None,
                "normal",
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[ETH-EDGE] write error: {e}")


def shadow_bucket(price: float) -> str:
    lo = max(0.0, min(0.9, math.floor(float(price) * 10) / 10))
    hi = min(1.0, lo + 0.1)
    return f"{lo:.1f}-{hi:.1f}"


def shadow_decision_eth(price: float) -> tuple[str, str]:
    if price > 0.80:
        return "filtered", "block_gt_0.80"
    if 0.55 <= price <= 0.70:
        return "kept", "allow_0.55_0.70"
    return "filtered", "outside_0.55_0.70"


def set_shadow_state_eth(price: float, context: str):
    decision, reason = shadow_decision_eth(price)
    bucket = shadow_bucket(price)
    _state['shadow_decision'] = decision
    _state['shadow_reason'] = reason
    _state['shadow_bucket'] = bucket
    _state['shadow_price'] = price
    log(f"[ETH-SHADOW] {decision} context={context} bucket={bucket} price={price:.4f} expected_pnl=NA reason={reason}")


def shadow_live_gate_eth(price: float, context: str) -> bool:
    set_shadow_state_eth(price, context=context)
    return _state.get('shadow_decision') == 'kept'


def get_book_metrics(token_id, submitted_price):
    """CLOB book snapshot used as execution-pricing truth."""
    book = fetch_book(token_id)
    distance_to_ask = round(book["best_ask"] - submitted_price, 4) if book.get("best_ask") is not None else None
    return {
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask"),
        "midpoint": book.get("midpoint"),
        "tick": book.get("tick", 0.01),
        "spread": book.get("spread"),
        "distance_to_ask": distance_to_ask,
        "bids": book.get("bids", []),
        "asks": book.get("asks", []),
        "error": book.get("error"),
    }


def get_exec_buy_context(market, direction, mode, price_cap, submitted_hint=0.0):
    token_id = market['clob_token_id'][0] if direction == 'UP' else (market['clob_token_id'][1] if len(market['clob_token_id']) > 1 else '')
    gamma_price = market['yes_price'] if direction == 'UP' else market['no_price']
    book = get_book_metrics(token_id, submitted_hint)
    clob_ref_price = book.get('midpoint') if book.get('midpoint') is not None else (book.get('best_ask') if book.get('best_ask') is not None else book.get('best_bid'))
    submitted_price, abort_reason = choose_buy_price(book, price_cap=price_cap, mode=mode, spread_cap=EXEC_SPREAD_CAP, maker_offset=MAKER_OFFSET)
    return {
        'token_id': token_id,
        'gamma_price': gamma_price,
        'clob_ref_price': clob_ref_price,
        'submitted_price': submitted_price,
        'abort_reason': abort_reason,
        'book': book,
        'side_label': 'BUY YES' if direction == 'UP' else 'BUY NO',
    }


def log_trade(engine, direction, size_usd, entry_price, pnl, exit_type, hold_sec, notes="", sec_remaining=None, price_bucket=None):
    try:
        timestamp_open = _current_window_open_iso()
        is_resolved = exit_type == "resolved"
        asset = _current_trade_slug()
        trade_id = _current_trade_id(direction)
        
        full_notes = notes
        if sec_remaining is not None:
            full_notes = f"{full_notes} sec_rem={sec_remaining}"
        if price_bucket:
            full_notes = f"{full_notes} price_bucket={price_bucket}"
        if _state.get('shadow_decision'):
            full_notes = f"{full_notes} shadow={_state.get('shadow_decision')} shadow_bucket={_state.get('shadow_bucket')} shadow_price={_state.get('shadow_price')} shadow_reason={_state.get('shadow_reason')}"

        if USE_SHARED_JOURNAL:
            conn = open_journal(SHARED_JOURNAL_DB, "eth15m")
            try:
                if is_resolved:
                    shared_journal_close(
                        conn,
                        trade_id=trade_id,
                        exit_price=entry_price,
                        pnl_absolute=pnl or 0.0,
                        pnl_percent=(pnl / size_usd * 100) if size_usd else 0.0,
                        exit_type=exit_type,
                        hold_duration_seconds=hold_sec or 0,
                        timestamp_close=datetime.now(timezone.utc).isoformat(),
                        notes=full_notes,
                    )
                else:
                    shared_journal_open(
                        conn,
                        trade_id=trade_id,
                        engine=engine,
                        asset=asset,
                        category="eth-updown",
                        direction=direction,
                        entry_price=entry_price,
                        position_size=size_usd / entry_price if entry_price else 0,
                        position_size_usd=size_usd,
                        regime="normal",
                        notes=full_notes,
                        timestamp_open=timestamp_open,
                    )
            finally:
                conn.close()
            return

        import sqlite3
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.execute("PRAGMA busy_timeout = 5000")
        c = conn.cursor()
        close_ts = datetime.now(timezone.utc).isoformat() if is_resolved else None
        base_params = (
            engine,
            timestamp_open,
            close_ts,
            asset,
            "eth-updown",
            direction,
            entry_price,
            entry_price if is_resolved else None,
            size_usd / entry_price if entry_price else 0,
            size_usd,
            pnl or 0.0,
            (pnl / size_usd * 100) if size_usd else 0,
            exit_type or "open",
            hold_sec or 0,
            "normal",
            full_notes,
        )

        if is_resolved:
            c.execute(
                """
                SELECT rowid
                FROM trades
                WHERE engine=? AND asset=? AND timestamp_open=? AND direction=?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (engine, asset, timestamp_open, direction),
            )
            existing = c.fetchone()
            if existing:
                c.execute(
                    """
                    UPDATE trades
                    SET timestamp_close=?, entry_price=?, exit_price=?, position_size=?, position_size_usd=?,
                        pnl_absolute=?, pnl_percent=?, exit_type=?, hold_duration_seconds=?, regime=?, notes=?
                    WHERE rowid=?
                    """,
                    (
                        close_ts,
                        entry_price,
                        entry_price,
                        size_usd / entry_price if entry_price else 0,
                        size_usd,
                        pnl or 0.0,
                        (pnl / size_usd * 100) if size_usd else 0,
                        exit_type or "open",
                        hold_sec or 0,
                        "normal",
                        full_notes,
                        existing[0],
                    ),
                )
            else:
                c.execute(
                    """
                    INSERT INTO trades (
                        engine, timestamp_open, timestamp_close, asset,
                        category, direction, entry_price, exit_price,
                        position_size, position_size_usd, pnl_absolute, pnl_percent,
                        exit_type, hold_duration_seconds, regime, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    base_params,
                )
        else:
            c.execute(
                """
                SELECT rowid
                FROM trades
                WHERE engine=? AND asset=? AND timestamp_open=? AND direction=? AND timestamp_close IS NULL
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (engine, asset, timestamp_open, direction),
            )
            existing_open = c.fetchone()
            if existing_open:
                c.execute(
                    """
                    UPDATE trades
                    SET entry_price=?, position_size=?, position_size_usd=?, pnl_absolute=?, pnl_percent=?,
                        exit_type=?, hold_duration_seconds=?, regime=?, notes=?
                    WHERE rowid=?
                    """,
                    (
                        entry_price,
                        size_usd / entry_price if entry_price else 0,
                        size_usd,
                        pnl or 0.0,
                        (pnl / size_usd * 100) if size_usd else 0,
                        exit_type or "open",
                        hold_sec or 0,
                        "normal",
                        full_notes,
                        existing_open[0],
                    ),
                )
            else:
                c.execute(
                    """
                    INSERT INTO trades (
                        engine, timestamp_open, timestamp_close, asset,
                        category, direction, entry_price, exit_price,
                        position_size, position_size_usd, pnl_absolute, pnl_percent,
                        exit_type, hold_duration_seconds, regime, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    base_params,
                )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Journal error: {e}")


def current_risk_multiplier():
    if not ROLLING_RISK_ENABLED:
        return 1.0
    now = time.time()
    last_ts = float(_state.get("risk_last_check_ts", 0) or 0)
    cached = float(_state.get("risk_multiplier", 1.0) or 1.0)
    if now - last_ts < ROLLING_RISK_REFRESH_SEC:
        return cached
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute(
            """
            WITH ranked AS (
                SELECT
                    pnl_absolute,
                    timestamp_close,
                    ROW_NUMBER() OVER (
                        PARTITION BY engine, asset, timestamp_open, direction
                        ORDER BY rowid DESC
                    ) AS rn
                FROM trades
                WHERE engine='eth15m' AND exit_type='resolved' AND timestamp_close IS NOT NULL
            ),
            dedup AS (
                SELECT pnl_absolute, timestamp_close
                FROM ranked
                WHERE rn=1
                ORDER BY timestamp_close DESC
                LIMIT ?
            )
            SELECT COALESCE(SUM(COALESCE(pnl_absolute, 0)), 0), COUNT(*)
            FROM dedup
            """,
            (ROLLING_RISK_LOOKBACK,),
        )
        row = c.fetchone()
        conn.close()
        rolling_pnl = float((row or [0, 0])[0] or 0.0)
        sample_n = int((row or [0, 0])[1] or 0)
        new_mult = 1.0
        if sample_n >= ROLLING_RISK_MIN_SAMPLE and rolling_pnl < 0:
            new_mult = max(0.1, float(ROLLING_RISK_MULTIPLIER))
        if abs(new_mult - cached) > 1e-9:
            safe_log(f"[ETH-RISK] rolling_{ROLLING_RISK_LOOKBACK} pnl={rolling_pnl:+.2f} n={sample_n} -> size_mult={new_mult:.2f}")
        _state["risk_multiplier"] = new_mult
        _state["risk_last_check_ts"] = now
        return new_mult
    except Exception as e:
        safe_log(f"[ETH-RISK] risk multiplier check failed: {e}")
        _state["risk_last_check_ts"] = now
        _state["risk_multiplier"] = cached
        return cached


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


def init_edge_events_db():
    import sqlite3
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS edge_events (
                id TEXT PRIMARY KEY,
                engine TEXT,
                asset TEXT,
                timestamp_et TEXT,
                market_slug TEXT,
                market_id TEXT,
                side TEXT,
                signal_type TEXT,
                seconds_remaining INTEGER,
                best_bid REAL,
                best_ask REAL,
                spread REAL,
                midprice REAL,
                microprice REAL,
                price_now REAL,
                price_1s_ago REAL,
                price_3s_ago REAL,
                price_5s_ago REAL,
                price_10s_ago REAL,
                price_30s_ago REAL,
                ret_1s REAL,
                ret_3s REAL,
                ret_5s REAL,
                ret_10s REAL,
                ret_30s REAL,
                vol_10s REAL,
                vol_30s REAL,
                vol_60s REAL,
                imbalance_1 REAL,
                imbalance_3 REAL,
                model_p_yes REAL,
                model_p_no REAL,
                edge_yes REAL,
                edge_no REAL,
                net_edge REAL,
                confidence REAL,
                regime TEXT,
                regime_ok INTEGER,
                adaptive_net_edge_floor REAL,
                adaptive_confidence_floor REAL,
                skip_reason TEXT,
                intended_entry_price REAL,
                actual_fill_price REAL,
                slippage REAL,
                shadow_decision TEXT,
                shadow_skip_reason TEXT,
                execution_status TEXT,
                decision TEXT
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_edge_events_timestamp
            ON edge_events (timestamp_et)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_edge_events_engine_asset
            ON edge_events (engine, asset)
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[ETH] edge_events init error: {e}")


def init_runtime_db():
    init_edge_events_db()
    init_active_fills_db()


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
    _state['maker_book_bid'] = None
    _state['maker_book_ask'] = None
    _state['maker_book_spread'] = None
    _state['maker_book_tick'] = 0.01
    _state['maker_distance_to_ask'] = None
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
    _state['maker_book_bid'] = None
    _state['maker_book_ask'] = None
    _state['maker_book_spread'] = None
    _state['maker_book_tick'] = 0.01
    _state['maker_distance_to_ask'] = None
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
            
            fok_token = _state.get('maker_token_id')
            fok_shares = _state.get('maker_shares', 5.0)
            current_book = get_book_metrics(fok_token, _state.get('maker_price', 0.0))
            bid_str = f"{current_book['best_bid']:.4f}" if current_book.get('best_bid') is not None else 'NA'
            ask_str = f"{current_book['best_ask']:.4f}" if current_book.get('best_ask') is not None else 'NA'
            mid_str = f"{current_book['midpoint']:.4f}" if current_book.get('midpoint') is not None else 'NA'
            spread_str = f"{current_book['spread']:.4f}" if current_book.get('spread') is not None else 'NA'
            fok_price, fok_abort = choose_buy_price(current_book, price_cap=SNIPE_MAX_PRICE, mode='taker', spread_cap=EXEC_SPREAD_CAP, maker_offset=0.0)
            if fok_abort:
                log(f"[ETH-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={_state.get('maker_side')} token_id={fok_token} gamma_price=NA clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={fok_abort} stage=fok_fallback")
                _state['diag_orders_cancelled'] = _state.get('diag_orders_cancelled', 0) + 1
                if seconds_remaining > MAKER_RETRY_MIN_SEC and _state.get('maker_attempt_count', 0) < MAKER_MAX_RETRIES:
                    _state['maker_attempt_count'] = _state.get('maker_attempt_count', 0) + 1
                    log(f"[ETH-MAKER] RETRY enabled (attempt {_state['maker_attempt_count']}/{MAKER_MAX_RETRIES}, {seconds_remaining}s remaining)")
                    _reset_maker_state_for_retry()
                    return None
                else:
                    _state['maker_done'] = True
                    save_state()
                    return False
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
                if seconds_remaining > MAKER_RETRY_MIN_SEC and _state.get('maker_attempt_count', 0) < MAKER_MAX_RETRIES:
                    _state['maker_attempt_count'] = _state.get('maker_attempt_count', 0) + 1
                    log(f"[ETH-MAKER] RETRY enabled (attempt {_state['maker_attempt_count']}/{MAKER_MAX_RETRIES}, {seconds_remaining}s remaining)")
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
        write_edge_event(
            market,
            signal_type="maker",
            decision="skip_time",
            skip_reason="outside_maker_window",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
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
        write_edge_event(
            market,
            signal_type="maker",
            decision="skip_no_signal",
            skip_reason="delta_below_threshold",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
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
    late_reversal = detect_late_reversal(prices, _state['window_open_eth'], direction, seconds_remaining)
    confirm_target = SIGNAL_CONFIRM_COUNT
    if late_reversal:
        confirm_target = min(confirm_target, LATE_REVERSAL_CONFIRM_COUNT)
    if len(recent_prices) < confirm_target:
        log(f"[ETH-MAKER] Confirm: only {len(recent_prices)}/{confirm_target} samples, waiting")
        return None
    confirmations = 0
    for _, p in recent_prices[-confirm_target:]:
        d = (p - _state['window_open_eth']) / _state['window_open_eth'] * 100
        if (direction == 'UP' and d > SNIPE_DELTA_MIN) or (direction == 'DOWN' and d < -SNIPE_DELTA_MIN_DOWN):
            confirmations += 1
    if confirmations < confirm_target:
        log(f"[ETH-MAKER] Confirm: {confirmations}/{confirm_target}, skipping")
        return None
    recent_avg = sum(p for _, p in recent_prices[-confirm_target:]) / len(recent_prices[-confirm_target:])
    momentum = (recent_avg - _state['window_open_eth']) / _state['window_open_eth'] * 100

    # FIX #4: Softened momentum deceleration threshold
    recent = [(t, p) for t, p in prices if t >= now_ts - 120]
    if len(recent) >= 6:
        mid = len(recent) // 2
        first_half = recent[:mid]
        second_half = recent[mid:]
        delta_first = (first_half[-1][1] - first_half[0][1]) / first_half[0][1] * 100
        delta_second = (second_half[-1][1] - second_half[0][1]) / second_half[0][1] * 100
        if abs(delta_second) < abs(delta_first) * MOMENTUM_DECEL_THRESHOLD and not late_reversal:
            log(f"[ETH-MAKER] momentum decelerating first={delta_first:+.4f}% second={delta_second:+.4f}% (threshold={MOMENTUM_DECEL_THRESHOLD}), skipping")
            return None

    MOMENTUM_MIN = SNIPE_DELTA_MIN * MOMENTUM_MIN_MULTIPLIER
    if abs(momentum) < MOMENTUM_MIN and not late_reversal:
        log(f"[ETH-MAKER] momentum={momentum:+.3f}% < {MOMENTUM_MIN:.3f}%, skipping (weak)")
        return None
    if late_reversal:
        log(f"[ETH-MAKER] LATE REVERSAL detected | confirmed={confirmations}/{confirm_target} momentum={momentum:+.3f}%")
    else:
        log(f"[ETH-MAKER] CONFIRMED {confirmations}/{confirm_target} | momentum={momentum:+.3f}%")

    current_window_ts = _state.get("window_ts")
    if current_window_ts and peer_has_same_direction_fill("btc15m", current_window_ts, direction):
        log(f"[ETH-MAKER] CORRELATION BLOCK: BTC already filled {direction} in window {current_window_ts}, skipping")
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            intended_entry_price=market['yes_price'] if direction == 'UP' else market['no_price'],
            decision="skip_correlation",
            skip_reason="peer_same_direction_fill",
            execution_status="blocked",
            seconds_remaining=seconds_remaining,
        )
        return None
    # FIX #15: Stagger ETH entry — wait 20s into maker window before placing, so BTC fills first
    # and the correlation block can detect it. Prevents both engines entering same direction simultaneously.
    CORRELATION_STAGGER_SEC = 20
    maker_window_elapsed = (MAKER_START_SEC - seconds_remaining)
    if maker_window_elapsed < CORRELATION_STAGGER_SEC:
        log(f"[ETH-MAKER] CORRELATION STAGGER: waiting {CORRELATION_STAGGER_SEC - maker_window_elapsed:.0f}s for BTC to fill first")
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            decision="skip_time",
            skip_reason="correlation_stagger",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
        return None

    price_cap = LATE_REVERSAL_MAX_PRICE if late_reversal else SNIPE_MAX_PRICE
    ctx = get_exec_buy_context(market, direction, mode='maker', price_cap=price_cap, submitted_hint=0.0)
    token_id = ctx['token_id']
    token_price = ctx['clob_ref_price']
    gamma_price = ctx['gamma_price']
    book = ctx['book']

    if seconds_remaining < MIN_ENTRY_SEC:
        log(f"[ETH-MAKER] FILTER: sec_remaining={seconds_remaining}s < {MIN_ENTRY_SEC}s, skipping ultra-late entry")
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            intended_entry_price=token_price,
            decision="skip_time",
            skip_reason="late_entry",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
        return None

    if token_price is None:
        log(f"[ETH-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price} abort_reason=missing_clob_reference")
        return None
    # ABORT: gamma/clob mismatch (direction source vs execution source disagree)
    if gamma_price is not None and token_price is not None and abs(gamma_price - token_price) > 0.10:
        log(f"[ETH-MAKER] ABORT: gamma/clob mismatch gamma_price={gamma_price:.4f} clob_price={token_price:.4f} diff={abs(gamma_price - token_price):.4f}")
        return None
    

    if token_price > price_cap:
        price_bucket = "high"
        log(f"[ETH-MAKER] FILTER: pricing_source=CLOB entry_price={token_price:.4f} gamma_price={gamma_price:.4f} > max {price_cap:.2f} (bucket={price_bucket}), skipping high price")
        shadow_live_gate_eth(token_price, context='maker')
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            intended_entry_price=token_price,
            decision="skip_no_edge",
            skip_reason="entry_price_above_max",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
        )
        return None
    elif token_price >= 0.60:
        price_bucket = "mid"
    elif token_price >= SNIPE_MIN_PRICE:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
        log(f"[ETH-MAKER] FILTER: pricing_source=CLOB entry_price={token_price:.4f} gamma_price={gamma_price:.4f} < min {SNIPE_MIN_PRICE:.2f} (bucket={price_bucket}), skipping low price")
        shadow_live_gate_eth(token_price, context='maker')
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            intended_entry_price=token_price,
            decision="skip_no_edge",
            skip_reason="entry_price_below_min",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
        )
        return None

    if not shadow_live_gate_eth(token_price, context='maker'):
        log(f"[ETH-MAKER] SHADOW FILTER: pricing_source=CLOB entry_price={token_price:.4f} gamma_price={gamma_price:.4f} shadow_reason={_state.get('shadow_reason')} (bucket={price_bucket}), skipping")
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            intended_entry_price=token_price,
            decision="skip_no_edge",
            skip_reason=_state.get('shadow_reason', 'shadow_filtered'),
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
        )
        return None

    _state['maker_price_bucket'] = price_bucket
    _state['maker_sec_remaining'] = seconds_remaining

    if token_price < MAKER_MIN_PRICE:
        log(f"[ETH-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} clob_price={token_price:.4f} gamma_price={gamma_price:.4f} abort_reason=below_maker_min")
        return None

    limit_price = ctx['submitted_price']
    bid_str = f"{book['best_bid']:.4f}" if book.get('best_bid') is not None else 'NA'
    ask_str = f"{book['best_ask']:.4f}" if book.get('best_ask') is not None else 'NA'
    mid_str = f"{book['midpoint']:.4f}" if book.get('midpoint') is not None else 'NA'
    spread_str = f"{book['spread']:.4f}" if book.get('spread') is not None else 'NA'
    if ctx['abort_reason']:
        log(f"[ETH-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={ctx['abort_reason']}")
        return None
    shares = max(5.0, math.floor(((SNIPE_DEFAULT * current_risk_multiplier()) / max(limit_price, 0.01)) * 100) / 100)
    log(f"[ETH-MAKER] pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={limit_price:.4f} spread={spread_str} shares={shares:.2f} dry={MAKER_DRY_RUN} attempt={_state.get('maker_attempt_count', 0)+1}")
    write_edge_event(
        market,
        signal_type="maker",
        side=direction,
        intended_entry_price=token_price,
        decision="place_yes",
        execution_status="pending" if DRY_RUN else "posted",
        seconds_remaining=seconds_remaining,
        shadow_decision=_state.get('shadow_decision', ''),
        shadow_skip_reason=_state.get('shadow_reason', ''),
        best_bid=book.get('best_bid'),
        best_ask=book.get('best_ask'),
        spread=book.get('spread'),
        midprice=token_price,
    )
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
    _state['maker_book_bid'] = book.get('best_bid')
    _state['maker_book_ask'] = book.get('best_ask')
    _state['maker_book_spread'] = book.get('spread')
    _state['maker_book_tick'] = float(book.get('tick') or 0.01)
    _state['maker_distance_to_ask'] = (
        round(float(book['best_ask']) - float(limit_price), 4)
        if book.get('best_ask') is not None else None
    )
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

    risk_mult = current_risk_multiplier()
    arb_budget = ARB_SIZE * risk_mult
    yes_shares = arb_budget / market["yes_price"]
    no_shares  = arb_budget / market["no_price"]

    r1 = place_order("BUY", yes_shares, market["yes_price"], market["condition_id"], yes_tid)
    r2 = place_order("BUY", no_shares,  market["no_price"],  market["condition_id"], no_tid)

    arb_profit = (1.00 - combined) * min(yes_shares, no_shares)
    notes = f"arb profit=${arb_profit:.2f} yes_shares={yes_shares:.2f} no_shares={no_shares:.2f} risk_mult={risk_mult:.2f}"
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
        write_edge_event(
            market,
            signal_type="snipe",
            decision="skip_no_signal",
            skip_reason="delta_below_threshold",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
        return None

    # FIX #7: Relaxed 1h trend threshold
    prices = _state.get("eth_prices", [])
    late_reversal = detect_late_reversal(prices, _state["window_open_eth"], direction, seconds_remaining)
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
        if abs(delta_second) < abs(delta_first) * MOMENTUM_DECEL_THRESHOLD and not late_reversal:
            log(f"[ETH-SNIPE] momentum decelerating first={delta_first:+.4f}% second={delta_second:+.4f}% (threshold={MOMENTUM_DECEL_THRESHOLD}), skipping")
            return None

    current_window_ts = _state.get("window_ts")
    if current_window_ts and peer_has_same_direction_fill("btc15m", current_window_ts, direction):
        log(f"[ETH-SNIPE] CORRELATION BLOCK: BTC already filled {direction} in window {current_window_ts}, skipping")
        write_edge_event(
            market,
            signal_type="snipe",
            side=direction,
            intended_entry_price=market["yes_price"] if direction == "UP" else market["no_price"],
            decision="skip_correlation",
            skip_reason="peer_same_direction_fill",
            execution_status="blocked",
            seconds_remaining=seconds_remaining,
        )
        return None

    price_cap = LATE_REVERSAL_MAX_PRICE if late_reversal else SNIPE_MAX_PRICE
    ctx = get_exec_buy_context(market, direction, mode='taker', price_cap=price_cap, submitted_hint=0.0)
    token_id = ctx['token_id']
    price = ctx['clob_ref_price']
    gamma_price = ctx['gamma_price']
    book = ctx['book']

    if seconds_remaining < MIN_ENTRY_SEC:
        log(f"[ETH-SNIPE] FILTER: sec_remaining={seconds_remaining}s < {MIN_ENTRY_SEC}s, skipping ultra-late entry")
        write_edge_event(
            market,
            signal_type="snipe",
            side=direction,
            intended_entry_price=price,
            decision="skip_time",
            skip_reason="late_entry",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
        return None

    if price is None:
        log(f"[ETH-SNIPE] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price} abort_reason=missing_clob_reference")
        return None
    # ABORT: gamma/clob mismatch (direction source vs execution source disagree)
    if gamma_price is not None and price is not None and abs(gamma_price - price) > 0.10:
        log(f"[ETH-SNIPE] ABORT: gamma/clob mismatch gamma_price={gamma_price:.4f} clob_price={price:.4f} diff={abs(gamma_price - price):.4f}")
        return None
    

    if price > price_cap:
        price_bucket = "high"
        log(f"[ETH-SNIPE] FILTER: pricing_source=CLOB entry_price={price:.4f} gamma_price={gamma_price:.4f} > max {price_cap:.2f} (bucket={price_bucket}), skipping high price")
        shadow_live_gate_eth(price, context='snipe')
        write_edge_event(
            market,
            signal_type="snipe",
            side=direction,
            intended_entry_price=price,
            decision="skip_no_edge",
            skip_reason="entry_price_above_max",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
        )
        return None
    elif price >= 0.60:
        price_bucket = "mid"
    elif price >= SNIPE_MIN_PRICE:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
        log(f"[ETH-SNIPE] FILTER: pricing_source=CLOB entry_price={price:.4f} gamma_price={gamma_price:.4f} < floor {SNIPE_MIN_PRICE:.2f} (bucket={price_bucket}), skipping low price")
        shadow_live_gate_eth(price, context='snipe')
        write_edge_event(
            market,
            signal_type="snipe",
            side=direction,
            intended_entry_price=price,
            decision="skip_no_edge",
            skip_reason="entry_price_below_min",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
        )
        return None

    if not shadow_live_gate_eth(price, context='snipe'):
        log(f"[ETH-SNIPE] SHADOW FILTER: pricing_source=CLOB entry_price={price:.4f} gamma_price={gamma_price:.4f} shadow_reason={_state.get('shadow_reason')} (bucket={price_bucket}), skipping")
        write_edge_event(
            market,
            signal_type="snipe",
            side=direction,
            intended_entry_price=price,
            decision="skip_no_edge",
            skip_reason=_state.get('shadow_reason', 'shadow_filtered'),
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
        )
        return None

    submitted_price = ctx['submitted_price']
    bid_str = f"{book['best_bid']:.4f}" if book.get('best_bid') is not None else 'NA'
    ask_str = f"{book['best_ask']:.4f}" if book.get('best_ask') is not None else 'NA'
    mid_str = f"{book['midpoint']:.4f}" if book.get('midpoint') is not None else 'NA'
    spread_str = f"{book['spread']:.4f}" if book.get('spread') is not None else 'NA'
    if ctx['abort_reason']:
        log(f"[ETH-SNIPE] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={ctx['abort_reason']}")
        return None

    base_size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    size = base_size * current_risk_multiplier()
    shares = size / submitted_price
    if shares < 5:
        shares = 5
        size = shares * submitted_price

    log(f"[ETH-SNIPE] pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={submitted_price:.4f} spread={spread_str}")
    write_edge_event(
        market,
        signal_type="snipe",
        side=direction,
        intended_entry_price=price,
        decision="place_yes",
        execution_status="pending" if DRY_RUN else "posted",
        seconds_remaining=seconds_remaining,
        shadow_decision=_state.get('shadow_decision', ''),
        shadow_skip_reason=_state.get('shadow_reason', ''),
    )
    r = place_order("BUY", shares, submitted_price, market["condition_id"], token_id)
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
    _state["maker_book_bid"] = None
    _state["maker_book_ask"] = None
    _state["maker_book_spread"] = None
    _state["maker_book_tick"] = 0.01
    _state["maker_distance_to_ask"] = None
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
    init_runtime_db()
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
        if sec_rem <= SNIPE_WINDOW and sec_rem > 5:
            check_snipe(market, sec_rem)

        # Sleep
        time.sleep(SCAN_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[ETH-15M] Stopped by user")
