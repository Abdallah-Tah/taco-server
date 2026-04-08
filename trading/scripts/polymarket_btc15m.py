#!/usr/bin/env python3
"""
polymarket_btc15m.py — BTC 15-Minute Polymarket Engine (IMPROVED)
=================================================================
Changes from original:
  1. SIGNAL_MAX_ENTRY_PRICE raised 0.68 -> 0.80 (unblocks high-conviction signals)
  2. MAKER_OFFSET reduced 0.005 -> 0.001 (more competitive limit orders)
  3. Fixed BTC price trimming bug in main loop (was breaking 1h trend filter)
  4. Softened momentum deceleration threshold 0.5 -> 0.3
  5. Added maker retry after cancellation if time permits (>45s remaining)
  6. Added FOK taker fallback if maker order unfilled after 60s
  7. Reduced arb log noise (logs once per window instead of every cycle)
  8. Added fill-rate tracking for diagnostics
  9. MAKER_START_SEC raised 300 -> 540 (catch signals before market fully prices them in)
 10. SIGNAL_MAX_ENTRY_PRICE lowered 0.82 -> 0.72 (skip expensive tokens, better risk/reward)
 11. SIGNAL_MIN_ENTRY_PRICE lowered 0.45 -> 0.38 (catch early confirmed signals with good value)
 12. SIGNAL_MAX_ENTRY_PRICE raised 0.72 -> 0.76 (allow late recovery entries, still blocks 0.82+)
 13. FOK fallback price +0.02 premium above mid to hit the actual ask instead of mid-price
 14. Asymmetric delta: DOWN requires 0.10% (was 0.025%) — DOWN was 25% win rate
 15. Correlation block stagger: ETH waits 20s before entering same-direction as BTC
 16. DOWN signals disabled by default (BTC DOWN was 25% win rate, -$37.96)
 17. Max entry capped at 0.55 (entries above 0.55 have negative expectancy)
 18. Max entry tightened 0.55 -> 0.50 (0.45-0.49 bucket 75% win rate, 0.50+ loses)

Two strategies:
  A) Binary Arb      — buy UP+DOWN when combined < $0.98, guaranteed profit
  B) Late Snipe      — directional bet near window close based on BTC delta

Dry run by default. Set DRY_RUN=false to go live.
"""
import json
import hashlib
import math
import os
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
SHARED_JOURNAL_DB = Path(os.environ.get("BTC15M_JOURNAL_DB", str(WORK_DIR / "btc15m_journal.db")))
CREDS_FILE    = Path("/home/abdaltm86/.config/openclaw/secrets.env")
POSITIONS_F  = WORK_DIR / ".poly_btc15m_positions.json"
STATE_F       = WORK_DIR / ".poly_btc15m_state.json"
LOG_F        = WORK_DIR / ".poly_btc15m.log"
VALIDATION_F = WORK_DIR / "validation" / "live_window_validation.jsonl"

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
USE_SHARED_JOURNAL = _bool("BTC15M_SHARED_JOURNAL", False)
DRY_RUN        = os.environ.get("BTC15M_DRY_RUN",  ENV.get("BTC15M_DRY_RUN",  "true")).lower() != "false"
MAKER_ENABLED  = os.environ.get("BTC15M_MAKER_ENABLED", ENV.get("BTC15M_MAKER_ENABLED", "false")).lower() == "true"
MAKER_DRY_RUN  = os.environ.get("BTC15M_MAKER_DRY_RUN", ENV.get("BTC15M_MAKER_DRY_RUN", "true")).lower() != "false"
MAKER_START_SEC = _int("BTC15M_MAKER_START_SEC", 540)
MAKER_CANCEL_SEC = _int("BTC15M_MAKER_CANCEL_SEC", 10)
# FIX #2: Reduced offset from 0.005 -> 0.001 for more competitive fills
MAKER_OFFSET = _float("BTC15M_MAKER_OFFSET", 0.001)
MAKER_POLL_SEC = _int("BTC15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("BTC15M_MAKER_MIN_PRICE", 0.01)
# FIX #5: FOK fallback after N seconds of unfilled maker order
MAKER_FOK_FALLBACK_SEC = _int("BTC15M_MAKER_FOK_FALLBACK_SEC", 60)
# FIX #5: Allow retry after cancellation if >N seconds remain
MAKER_RETRY_MIN_SEC = _int("BTC15M_MAKER_RETRY_MIN_SEC", 45)

BTC15M_GABAGOOL_ENABLED = os.environ.get("BTC15M_GABAGOOL_ENABLED", ENV.get("BTC15M_GABAGOOL_ENABLED", "false")).lower() == "true"
BTC15M_GABAGOOL_DRY_RUN = os.environ.get("BTC15M_GABAGOOL_DRY_RUN", ENV.get("BTC15M_GABAGOOL_DRY_RUN", "true")).lower() != "false"
GABAGOOL_MAX_LEG = _float("GABAGOOL_MAX_LEG", 0.48)
GABAGOOL_TARGET_COMBINED = _float("GABAGOOL_TARGET_COMBINED", 0.97)

# ── Config ────────────────────────────────────────────────────────────────────
WINDOW_SEC      = _int("BTC15M_WINDOW_SEC", 900)          # 15 minutes
ARB_THRESHOLD   = _float("BTC15M_ARB_THRESHOLD", 0.98)
ARB_SIZE        = _float("BTC15M_ARB_SIZE", 10.00)
SNIPE_DELTA_MIN = _float("BTC15M_SNIPE_DELTA_MIN", 0.025)
# FIX #14: Asymmetric delta — DOWN needs stronger signal (25% win rate at 0.025% was terrible)
SNIPE_DELTA_MIN_DOWN = _float("BTC15M_SNIPE_DELTA_MIN_DOWN", 0.10)
# FIX #16: Disable DOWN entirely — 25% win rate, -$37.96 over 2 days. Only trade UP.
DOWN_ENABLED = os.environ.get("BTC15M_DOWN_ENABLED", ENV.get("BTC15M_DOWN_ENABLED", "false")).lower() == "true"
UP_ENABLED = os.environ.get("BTC15M_UP_ENABLED", ENV.get("BTC15M_UP_ENABLED", "true")).lower() == "true"
SNIPE_MAX_PRICE = _float("BTC15M_SNIPE_MAX_PRICE", 0.90)
EXEC_SPREAD_CAP = _float("BTC15M_EXEC_SPREAD_CAP", 0.10)
# Signal confirmation (improved filtering)
SIGNAL_CONFIRM_COUNT   = _int("BTC15M_SIGNAL_CONFIRM_COUNT", 2)   # consecutive polls needed
SIGNAL_CONFIRM_SEC     = _int("BTC15M_SIGNAL_CONFIRM_SEC", 15)     # max age of confirm samples
# FIX #17: Capped max entry at 0.50 — 0.45-0.49 bucket is 75% win rate, above 0.50 loses money
SIGNAL_MAX_ENTRY_PRICE = _float("BTC15M_SIGNAL_MAX_ENTRY_PRICE", 0.50)
SIGNAL_MIN_ENTRY_PRICE = _float("BTC15M_SIGNAL_MIN_ENTRY_PRICE", 0.45)
SNIPE_DEFAULT   = _float("BTC15M_SNIPE_DEFAULT_SIZE", 12.00)
SNIPE_STRONG    = _float("BTC15M_SNIPE_STRONG_SIZE", 12.00)
SNIPE_STRONG_D  = _float("BTC15M_SNIPE_STRONG_DELTA", 0.10)  # percent
ROLLING_RISK_ENABLED = _bool("BTC15M_ROLLING_RISK_ENABLED", True)
ROLLING_RISK_LOOKBACK = _int("BTC15M_ROLLING_RISK_LOOKBACK", 20)
ROLLING_RISK_MIN_SAMPLE = _int("BTC15M_ROLLING_RISK_MIN_SAMPLE", 10)
ROLLING_RISK_MULTIPLIER = _float("BTC15M_ROLLING_RISK_MULTIPLIER", 0.60)
ROLLING_RISK_REFRESH_SEC = _int("BTC15M_ROLLING_RISK_REFRESH_SEC", 120)
SNIPE_WINDOW    = _int("BTC15M_SNIPE_WINDOW_SEC", 30)
POLL_SEC        = _int("BTC15M_PRICE_POLL_SEC", 5)
SCAN_SEC        = _int("BTC15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS  = _float("BTC15M_MAX_DAILY_LOSS", 15.00)
UP_MIN_DELTA = _float("BTC15M_UP_MIN_DELTA", 0.08)
DOWN_MIN_DELTA = _float("BTC15M_DOWN_MIN_DELTA", 0.05)
CONFIRM_TICKS = _int("BTC15M_CONFIRM_TICKS", 3)
CONFIRM_INTERVAL_SEC = _int("BTC15M_CONFIRM_INTERVAL_SEC", 10)
MAX_GAMMA_CLOB_DIFF = _float("BTC15M_MAX_GAMMA_CLOB_DIFF", 0.08)
MAX_SPREAD = _float("BTC15M_MAX_SPREAD", 0.015)
UP_MAX_ENTRY = _float("BTC15M_UP_MAX_ENTRY", 0.62)
DOWN_MAX_ENTRY = _float("BTC15M_DOWN_MAX_ENTRY", 0.70)
DIR_LOOKBACK = _int("BTC15M_DIR_LOOKBACK", 5)
DIR_MIN_WR = _float("BTC15M_DIR_MIN_WR", 0.40)
DIR_MAX_LOSS = _float("BTC15M_DIR_MAX_LOSS", -10.0)
DIR_THROTTLE_MULT = _float("BTC15M_DIR_THROTTLE_MULT", 0.50)
DIR_PAUSE_HOURS = _float("BTC15M_DIR_PAUSE_HOURS", 6)
ONE_TRADE_PER_WINDOW = _bool("BTC15M_ONE_TRADE_PER_WINDOW", True)
SIGNAL_CONFIRM_COUNT = max(int(SIGNAL_CONFIRM_COUNT), int(CONFIRM_TICKS))
SIGNAL_CONFIRM_SEC = max(int(SIGNAL_CONFIRM_SEC), int(CONFIRM_TICKS * CONFIRM_INTERVAL_SEC + 5))
QUOTE_JUMP_MAX = _float("BTC15M_QUOTE_JUMP_MAX", 0.23)
QUOTE_DIVERGENCE_MAX = _float("BTC15M_QUOTE_DIVERGENCE_MAX", 0.20)
QUOTE_DIVERGENCE_CYCLES = _int("BTC15M_QUOTE_DIVERGENCE_CYCLES", 3)

# ── Cooldown system (loss-based, DB-persisted) ──────────────────────────────
COOLDOWN_LOSS_THRESHOLD = _int("BTC15M_COOLDOWN_LOSS_THRESHOLD", 2)     # losses to trigger
COOLDOWN_DURATION_SEC   = _int("BTC15M_COOLDOWN_DURATION_SEC", 1800)    # 30 min pause
COOLDOWN_LOOKBACK_SEC   = _int("BTC15M_COOLDOWN_LOOKBACK_SEC", 1800)    # 30 min window

SERIES_ID       = "10192"
SERIES_SLUG     = "btc-up-or-down-15m"
# FIX #4: Softened momentum deceleration threshold (was 0.5 implicitly)
MOMENTUM_DECEL_THRESHOLD = _float("BTC15M_MOMENTUM_DECEL_THRESHOLD", 0.30)
MOMENTUM_MIN_MULTIPLIER = _float("BTC15M_MOMENTUM_MIN_MULTIPLIER", 1.0)

# ── State ─────────────────────────────────────────────────────────────────────
_state = {
    "window_ts":       0,
    "window_open_btc": 0.0,
    "arb_done":        False,
    "arb_logged":      False,   # FIX #7: only log arb miss once per window
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
    "maker_attempt_count": 0,   # FIX #5: track retry attempts per window
    "maker_placed_ts":  0,      # FIX #5: when was current order placed
    "gabagool_yes_low": None,
    "gabagool_yes_ts": 0,
    "gabagool_no_low": None,
    "gabagool_no_ts": 0,
    "gabagool_window_logged": False,
    # FIX #8: fill rate diagnostics
    "diag_orders_placed": 0,
    "diag_orders_filled": 0,
    "diag_orders_cancelled": 0,
    "confirm_direction": "",
    "confirm_count": 0,
    "confirm_last_ts": 0,
    "confirm_window_ts": 0,
    "dir_pause_until_up": 0,
    "dir_pause_until_down": 0,
    "quote_prev_window_ts": 0,
    "quote_prev_yes": None,
    "quote_prev_no": None,
    "quote_guard_window_ts": 0,
    "quote_guard_reason": "",
    "quote_divergence_count_up": 0,
    "quote_divergence_count_down": 0,
    "validation_last_finalized_ts": 0,
    "notified_resolved_ids": [],
}
_positions = []   # list of dicts for open/confirmation positions

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
    is_maker_msg = msg.startswith("[BTC-MAKER]")
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
    NOTIFIED_F = WORK_DIR / ".poly_btc15m_notified.json"
    def _load_notified():
        try:
            if NOTIFIED_F.exists(): return set(json.load(NOTIFIED_F.open()))
        except Exception: pass
        return set()
    def _save_notified(ids):
        try:
            tmp = NOTIFIED_F.with_suffix('.tmp'); tmp.write_text(json.dumps(sorted(ids)[-200:])); tmp.replace(NOTIFIED_F)
        except Exception as e: log(f"[BTC-NOTIFIED] save failed: {e}")
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT rowid, asset, direction, pnl_absolute
            FROM trades
            WHERE engine='btc15m' AND exit_type='resolved'
            ORDER BY timestamp_close DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log(f"[POST-RESOLUTION] notify_recent_resolutions failed: {e}")
        return

    seen = _load_notified()
    new_rows = [row for row in reversed(rows) if row[0] and row[0] not in seen]
    for trade_id, asset, direction, pnl_abs in new_rows:
        pnl_abs = float(pnl_abs or 0.0)
        outcome = "WIN" if pnl_abs >= 0 else "LOST"
        tg(f"[BTC-15M] {outcome}: {direction or '?'} {asset or ''} | P&L ${pnl_abs:+.2f}")
        seen.add(trade_id)
        _save_notified(seen)

    if new_rows:
        _state["notified_resolved_ids"] = sorted(seen)[-100:]
        save_state()


def trigger_post_resolution_tasks():
    # This now runs *before* the new window setup.
    if DRY_RUN:
        return
    try:
        reconcile_started = time.time()
        # Run reconcile first to update the journal
        reconcile_proc = subprocess.Popen(
            [str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_reconcile.py'), '--fast-post-resolution'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # Wait for reconcile to finish.
            reconcile_proc.wait(timeout=25)
            elapsed = time.time() - reconcile_started
            log(f"[POST-RESOLUTION] Reconcile completed rc={reconcile_proc.returncode} elapsed={elapsed:.2f}s")

            notify_recent_resolutions()

            # Updated loss check — uses the new DB-persisted cooldown system.
            check_and_manage_cooldown()

        except subprocess.TimeoutExpired:
            log("[POST-RESOLUTION] WARNING: Reconcile timed out. Skipping loss check for this cycle to avoid stale data.")
        except Exception as e:
            log(f"[POST-RESOLUTION] WARNING: Reconcile failed ({e}). Skipping loss check for this cycle.")

        # Run redeem in the background, it's not critical for the loss check.
        subprocess.Popen([str(VENV_PY), str(WORK_DIR / 'scripts' / 'polymarket_redeem.py')], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    except Exception as e:
        log(f"[POST-RESOLUTION] task launch error: {e}")


# ── Cooldown system (DB-persisted, time-window based) ─────────────────────────

def _table_exists(conn, name):
    """Check if a table exists in the given SQLite connection."""
    c = conn.cursor()
    c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return c.fetchone() is not None


def _write_cooldown_state(engine, cool_down, cool_down_until):
    """Persist cooldown state to the cooldown_state table."""
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.execute(
            """
            INSERT INTO cooldown_state (engine, cool_down, cool_down_until)
            VALUES (?, ?, ?)
            ON CONFLICT(engine) DO UPDATE SET
                cool_down=excluded.cool_down,
                cool_down_until=excluded.cool_down_until
            """,
            (engine, 1 if cool_down else 0, cool_down_until),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[COOLDOWN] Error persisting state: {e}")


def _read_cooldown_state(engine):
    """Read cooldown state from the cooldown_state table."""
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute(
            "SELECT cool_down, cool_down_until FROM cooldown_state WHERE engine=?",
            (engine,),
        )
        row = c.fetchone()
        conn.close()
        if row is None:
            return False, None
        return bool(row[0]), row[1]
    except Exception as e:
        log(f"[COOLDOWN] Error reading state: {e}")
        return False, None


def _count_recent_losses(engine):
    """Count resolved losses for `engine` within the lookback window.
    Derives count from actual trade history — no stored counters."""
    try:
        cutoff_ts = datetime.now(timezone.utc).timestamp() - COOLDOWN_LOOKBACK_SEC
        cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
        count = 0

        # Check main journal DB
        try:
            conn = sqlite3.connect(str(JOURNAL_DB))
            if _table_exists(conn, "trades"):
                c = conn.cursor()
                c.execute(
                    """SELECT COUNT(*) FROM trades
                       WHERE engine=? AND exit_type='resolved'
                         AND pnl_absolute < 0 AND timestamp_close >= ?""",
                    (engine, cutoff_iso),
                )
                row = c.fetchone()
                if row:
                    count += row[0]
            conn.close()
        except Exception as e:
            log(f"[COOLDOWN] Error querying main journal: {e}")

        # Check shared journal DB if enabled
        if USE_SHARED_JOURNAL:
            try:
                conn = sqlite3.connect(str(SHARED_JOURNAL_DB))
                if _table_exists(conn, "trades"):
                    c = conn.cursor()
                    c.execute(
                        """SELECT COUNT(*) FROM trades
                           WHERE engine=? AND exit_type='resolved'
                             AND pnl_absolute < 0 AND timestamp_close >= ?""",
                        (engine, cutoff_iso),
                    )
                    row = c.fetchone()
                    if row:
                        count += row[0]
                conn.close()
            except Exception as e:
                log(f"[COOLDOWN] Error querying shared journal: {e}")

        return count
    except Exception as e:
        log(f"[COOLDOWN] Error counting losses: {e}")
        return 0


def check_and_manage_cooldown():
    """Core cooldown logic — called every cycle.

    1. Loss detection:  count losses in lookback window from trade history
    2. Trigger:        if losses >= threshold and not in cooldown -> activate
    3. Enforce:        if in cooldown and not expired -> block trading (return False)
    4. Reset:          if in cooldown and expired -> clear and resume (return True)

    Returns True if trading is allowed, False if blocked by cooldown.
    State is persisted in cooldown_state table so it survives process restarts.
    """
    engine = "btc15m"
    cool_down, cool_down_until_str = _read_cooldown_state(engine)
    now = time.time()

    # -- 3. Enforce cooldown --
    if cool_down and cool_down_until_str is not None:
        try:
            expiry = datetime.fromisoformat(cool_down_until_str).timestamp()
        except Exception:
            expiry = 0
        if now < expiry:
            log(f"[COOLDOWN] Active -- skipping trades (market={engine}, expires_in={int(expiry - now)}s)")
            return False
        # -- 4. Reset cooldown --
        _write_cooldown_state(engine, False, None)
        log(f"[COOLDOWN] Expired -- trading resumed (market={engine})")
        tg("[BTC-15M] Cooldown expired, trading resumed")
        return True

    # -- 1. Loss detection (dynamic, no stored counter) --
    loss_count = _count_recent_losses(engine)

    # -- 2. Trigger cooldown --
    if loss_count >= COOLDOWN_LOSS_THRESHOLD:
        expire_ts = now + COOLDOWN_DURATION_SEC
        expire_iso = datetime.fromtimestamp(expire_ts, tz=timezone.utc).isoformat()
        _write_cooldown_state(engine, True, expire_iso)
        log(f"[COOLDOWN] Activated -- {loss_count} losses in {COOLDOWN_LOOKBACK_SEC // 60}min window (market={engine}, cooldown_until={expire_iso})")
        tg(f"[BTC-15M] Cooldown activated ({loss_count} losses in {COOLDOWN_LOOKBACK_SEC // 60}min window, pausing {COOLDOWN_DURATION_SEC // 60}min)")
        return False

    return True


# Legacy stub -- redirects to the new system for any external callers
def check_consecutive_losses():
    """Deprecated: replaced by check_and_manage_cooldown()."""
    check_and_manage_cooldown()

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
    _state.setdefault('maker_attempt_count', 0)
    _state.setdefault('maker_placed_ts', 0)
    _state.setdefault('arb_logged', False)
    _state.setdefault('diag_orders_placed', 0)
    _state.setdefault('diag_orders_filled', 0)
    _state.setdefault('diag_orders_cancelled', 0)
    _state.setdefault('quote_prev_window_ts', 0)
    _state.setdefault('quote_prev_yes', None)
    _state.setdefault('quote_prev_no', None)
    _state.setdefault('quote_guard_window_ts', 0)
    _state.setdefault('quote_guard_reason', '')
    _state.setdefault('quote_divergence_count_up', 0)
    _state.setdefault('quote_divergence_count_down', 0)
    _state.setdefault('validation_last_finalized_ts', 0)

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
            outcomes = json.loads(m.get("outcomes", "[]")) if m.get("outcomes") else []
            outcome_prices = json.loads(m["outcomePrices"])
            return {
                "id":           m["id"],
                "question":     m["question"],
                "condition_id": m["conditionId"],
                "slug":         m.get("slug", slug),
                "outcomes":     outcomes,
                "outcome_prices": [float(x) for x in outcome_prices],
                "clob_token_id": json.loads(m.get("clobTokenIds", "[]")),
                "yes_price":    float(outcome_prices[0]),
                "no_price":     float(outcome_prices[1]),
                "combined":     float(outcome_prices[0]) + float(outcome_prices[1]),
                "market_best_bid": float(m["bestBid"]) if m.get("bestBid") not in (None, "") else None,
                "market_best_ask": float(m["bestAsk"]) if m.get("bestAsk") not in (None, "") else None,
                "market_last_trade": float(m["lastTradePrice"]) if m.get("lastTradePrice") not in (None, "") else None,
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
                payload['fill_check'] = fill
                payload['posted'] = data
                break
    except Exception:
        pass
    return payload

# ── Journal logging ───────────────────────────────────────────────────────────
def _current_trade_slug():
    window_ts = int(_state.get("window_ts") or 0)
    return f"btc-updown-15m-{window_ts}" if window_ts else "btc-15m"


def _current_trade_id(direction):
    slug = _current_trade_slug()
    stable = f"btc15m:{slug}:{direction or 'UNKNOWN'}"
    return hashlib.sha256(stable.encode()).hexdigest()[:32]


def _current_window_open_iso():
    window_ts = int(_state.get("window_ts") or 0)
    if not window_ts:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(window_ts, tz=timezone.utc).isoformat()


def _edge_event_id(signal_type: str, side: str, seconds_remaining: int | None) -> str:
    seed = f"btc15m:{_current_trade_slug()}:{signal_type}:{side or ''}:{seconds_remaining}:{time.time_ns()}"
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
                "btc15m",
                "BTC",
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
        log(f"[BTC-EDGE] write error: {e}")


def _current_maker_family_id(direction):
    window_ts = int(_state.get("window_ts") or 0)
    if not window_ts:
        return f"btc-unknown-{direction or 'UNKNOWN'}"
    family_ts = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    return f"{family_ts}-{direction or 'UNKNOWN'}"


def shadow_bucket(price: float) -> str:
    lo = max(0.0, min(0.9, math.floor(float(price) * 10) / 10))
    hi = min(1.0, lo + 0.1)
    return f"{lo:.1f}-{hi:.1f}"


def shadow_decision_btc(price: float) -> tuple[str, str]:
    if price > 0.70:
        return "filtered", "block_gt_0.70"
    if 0.45 <= price <= 0.62:
        return "kept", "allow_0.45_0.62"
    return "filtered", "outside_0.45_0.62"


def set_shadow_state_btc(price: float, context: str):
    decision, reason = shadow_decision_btc(price)
    bucket = shadow_bucket(price)
    _state['shadow_decision'] = decision
    _state['shadow_reason'] = reason
    _state['shadow_bucket'] = bucket
    _state['shadow_price'] = price
    log(f"[BTC-SHADOW] {decision} context={context} bucket={bucket} price={price:.4f} expected_pnl=NA reason={reason}")


def shadow_live_gate_btc(price: float, context: str) -> bool:
    set_shadow_state_btc(price, context=context)
    return _state.get('shadow_decision') == 'kept'


def direction_min_delta_btc(direction: str) -> float:
    return UP_MIN_DELTA if direction == 'UP' else DOWN_MIN_DELTA


def direction_max_entry_btc(direction: str) -> float:
    return UP_MAX_ENTRY if direction == 'UP' else DOWN_MAX_ENTRY


def direction_outcome_index_btc(market: dict, direction: str) -> int:
    outcomes = market.get('outcomes') or []
    target = 'up' if direction == 'UP' else 'down'
    for idx, label in enumerate(outcomes):
        if str(label).strip().lower() == target:
            return idx
    return 0 if direction == 'UP' else 1


def direction_token_id_btc(market: dict, direction: str) -> str:
    token_ids = market.get('clob_token_id') or []
    idx = direction_outcome_index_btc(market, direction)
    return str(token_ids[idx]) if idx < len(token_ids) else ''


def direction_gamma_price_btc(market: dict, direction: str):
    prices = market.get('outcome_prices') or []
    idx = direction_outcome_index_btc(market, direction)
    if idx < len(prices):
        return float(prices[idx])
    return market.get('yes_price') if direction == 'UP' else market.get('no_price')


def direction_market_quote_btc(market: dict, direction: str) -> dict:
    base_bid = market.get('market_best_bid')
    base_ask = market.get('market_best_ask')
    idx = direction_outcome_index_btc(market, direction)
    if base_bid is None or base_ask is None:
        return {
            'best_bid': None,
            'best_ask': None,
            'midpoint': None,
            'spread': None,
            'tick': 0.01,
            'bids': [],
            'asks': [],
            'error': 'missing_gamma_market_quote',
            'source': 'gamma_market_quote',
        }
    if idx == 0:
        best_bid = float(base_bid)
        best_ask = float(base_ask)
    else:
        best_bid = max(0.01, round(1.0 - float(base_ask), 4))
        best_ask = min(0.99, round(1.0 - float(base_bid), 4))
    midpoint = round((best_bid + best_ask) / 2, 4)
    spread = round(best_ask - best_bid, 4)
    return {
        'best_bid': best_bid,
        'best_ask': best_ask,
        'midpoint': midpoint,
        'spread': spread,
        'tick': 0.01,
        'bids': [],
        'asks': [],
        'error': None,
        'source': 'gamma_market_quote',
    }


def directional_clob_price(book: dict, direction: str, fallback=None):
    if book.get('best_ask') is not None:
        return book.get('best_ask')
    if book.get('midpoint') is not None:
        return book.get('midpoint')
    if book.get('best_bid') is not None:
        return book.get('best_bid')
    return fallback


def choose_direction_book_btc(market: dict, direction: str, token_id: str, submitted_hint=0.0):
    raw_book = get_book_metrics(token_id, submitted_hint)
    raw_book['source'] = 'clob_book'
    gamma_book = direction_market_quote_btc(market, direction)
    gamma_price = direction_gamma_price_btc(market, direction)

    if gamma_book.get('best_bid') is None or gamma_book.get('best_ask') is None:
        return raw_book, None

    raw_mid = raw_book.get('midpoint')
    raw_ask = raw_book.get('best_ask')
    if raw_book.get('error'):
        reason = f"clob_error:{raw_book.get('error')}"
    elif raw_ask is None:
        reason = 'missing_best_ask'
    elif raw_mid is not None and abs(float(raw_mid) - float(gamma_price)) > 0.20:
        reason = f"mid_vs_gamma:{raw_mid:.4f}:{float(gamma_price):.4f}"
    elif abs(float(raw_ask) - float(gamma_book['best_ask'])) > 0.20:
        reason = f"ask_vs_gamma_quote:{raw_ask:.4f}:{float(gamma_book['best_ask']):.4f}"
    else:
        reason = None

    if reason:
        raw_bid_val = raw_book.get('best_bid')
        raw_ask_val = raw_book.get('best_ask')
        raw_bid_str = f"{float(raw_bid_val):.4f}" if raw_bid_val is not None else 'NA'
        raw_ask_str = f"{float(raw_ask_val):.4f}" if raw_ask_val is not None else 'NA'
        log(
            f"[BTC-BOOK] Using gamma market quote fallback direction={direction} token_id={token_id} "
            f"reason={reason} raw_bid={raw_bid_str} raw_ask={raw_ask_str} "
            f"gamma_bid={gamma_book['best_bid']:.4f} gamma_ask={gamma_book['best_ask']:.4f}"
        )
        return gamma_book, reason

    return raw_book, None


def _quote_divergence_key_btc(direction: str) -> str:
    return 'quote_divergence_count_up' if direction == 'UP' else 'quote_divergence_count_down'


def _quote_gap_from_reason_btc(reason: str):
    if not reason or ':' not in reason:
        return None
    nums = []
    for part in str(reason).split(':')[1:]:
        try:
            nums.append(float(part))
        except Exception:
            continue
    if len(nums) >= 2:
        return abs(nums[0] - nums[1])
    return None


def update_quote_guard_btc(market: dict, seconds_remaining: int):
    current_window = int(_state.get('window_ts') or 0)
    if current_window <= 0:
        return
    prev_window = int(_state.get('quote_prev_window_ts') or 0)
    prev_yes = _state.get('quote_prev_yes')
    prev_no = _state.get('quote_prev_no')
    current_yes = float(market.get('yes_price') or 0.0)
    current_no = float(market.get('no_price') or 0.0)
    changed = False

    if prev_window == current_window and prev_yes is not None and prev_no is not None:
        jump_yes = abs(current_yes - float(prev_yes))
        jump_no = abs(current_no - float(prev_no))
        jump = max(jump_yes, jump_no)
        if jump >= QUOTE_JUMP_MAX and int(_state.get('quote_guard_window_ts') or 0) != current_window:
            _state['quote_guard_window_ts'] = current_window
            _state['quote_guard_reason'] = f"gamma_quote_jump:{float(prev_yes):.3f}->{current_yes:.3f}"
            log(
                f"[BTC-GUARD] WINDOW PAUSE reason=gamma_quote_jump prev_yes={float(prev_yes):.3f} "
                f"new_yes={current_yes:.3f} prev_no={float(prev_no):.3f} new_no={current_no:.3f} sec_rem={seconds_remaining}"
            )
            changed = True

    if prev_window != current_window or prev_yes != current_yes or prev_no != current_no:
        _state['quote_prev_window_ts'] = current_window
        _state['quote_prev_yes'] = current_yes
        _state['quote_prev_no'] = current_no
        changed = True

    if changed:
        save_state()


def quote_guard_active_btc() -> bool:
    return int(_state.get('quote_guard_window_ts') or 0) == int(_state.get('window_ts') or 0)


def maybe_block_quote_guard_btc(market, signal_type: str, direction: str, seconds_remaining: int, intended_entry_price=None):
    if not quote_guard_active_btc():
        return False
    reason = _state.get('quote_guard_reason') or 'quote_guard_window_pause'
    log(f"[BTC-GUARD] BLOCK signal={signal_type} direction={direction} reason={reason} sec_rem={seconds_remaining}")
    write_edge_event(
        market,
        signal_type=signal_type,
        side=direction,
        intended_entry_price=intended_entry_price,
        decision='skip_quote_guard',
        skip_reason=reason,
        execution_status='blocked',
        seconds_remaining=seconds_remaining,
    )
    return True


def register_quote_divergence_btc(market, signal_type: str, direction: str, seconds_remaining: int, ctx: dict):
    key = _quote_divergence_key_btc(direction)
    gap = _quote_gap_from_reason_btc(ctx.get('book_fallback_reason') or '')
    if gap is None or gap < QUOTE_DIVERGENCE_MAX:
        if int(_state.get(key) or 0) != 0:
            _state[key] = 0
            save_state()
        return False

    count = int(_state.get(key) or 0) + 1
    _state[key] = count
    save_state()
    log(
        f"[BTC-GUARD] divergence signal={signal_type} direction={direction} gap={gap:.3f} "
        f"count={count}/{QUOTE_DIVERGENCE_CYCLES} source={ctx.get('book_fallback_reason')}"
    )
    current_window = int(_state.get('window_ts') or 0)
    if count >= QUOTE_DIVERGENCE_CYCLES and int(_state.get('quote_guard_window_ts') or 0) != current_window:
        _state['quote_guard_window_ts'] = current_window
        _state['quote_guard_reason'] = f"quote_divergence_{direction.lower()}:{gap:.3f}"
        save_state()
        log(
            f"[BTC-GUARD] WINDOW PAUSE reason={_state['quote_guard_reason']} "
            f"count={count}/{QUOTE_DIVERGENCE_CYCLES} sec_rem={seconds_remaining}"
        )
    return maybe_block_quote_guard_btc(
        market,
        signal_type,
        direction,
        seconds_remaining,
        intended_entry_price=ctx.get('gamma_price'),
    )


def classify_window_condition_btc(window_prices: list[tuple[int, float]], open_price: float) -> tuple[str, dict]:
    if not window_prices or not open_price:
        return 'insufficient_data', {'window_return_pct': None, 'range_pct': None, 'first_half_return_pct': None}
    prices = [float(p) for _, p in window_prices if p]
    if not prices:
        return 'insufficient_data', {'window_return_pct': None, 'range_pct': None, 'first_half_return_pct': None}
    final_price = prices[-1]
    max_price = max(prices)
    min_price = min(prices)
    window_return = ((final_price - open_price) / open_price) * 100
    range_pct = ((max_price - min_price) / open_price) * 100
    mid_idx = max(1, len(prices) // 2)
    first_half_end = prices[mid_idx - 1]
    first_half_return = ((first_half_end - open_price) / open_price) * 100
    pos_seen = any(((p - open_price) / open_price) * 100 >= UP_MIN_DELTA for p in prices)
    neg_seen = any(((p - open_price) / open_price) * 100 <= -DOWN_MIN_DELTA for p in prices)

    if abs(window_return) < 0.06 and range_pct < 0.12:
        label = 'calm'
    elif pos_seen and neg_seen and (first_half_return * window_return) < 0 and abs(window_return) >= 0.08:
        label = 'late_reversal'
    elif window_return >= 0.18:
        label = 'trend_up'
    elif window_return <= -0.18:
        label = 'trend_down'
    elif range_pct >= 0.20:
        label = 'chop'
    else:
        label = 'drift'
    return label, {
        'window_return_pct': round(window_return, 4),
        'range_pct': round(range_pct, 4),
        'first_half_return_pct': round(first_half_return, 4),
    }


def append_window_validation_btc(record: dict):
    VALIDATION_F.parent.mkdir(parents=True, exist_ok=True)
    with open(VALIDATION_F, 'a') as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def finalize_window_validation_btc():
    prev_window_ts = int(_state.get('window_ts') or 0)
    if prev_window_ts <= 0:
        return
    if prev_window_ts <= int(_state.get('validation_last_finalized_ts') or 0):
        return
    prev_slug = _state.get('prev_slug') or ''
    open_price = float(_state.get('window_open_btc') or 0.0)
    window_end = prev_window_ts + WINDOW_SEC
    window_prices = [
        (int(t), float(p)) for t, p in (_state.get('btc_prices') or [])
        if prev_window_ts <= int(t) < window_end and p is not None
    ]
    condition, metrics = classify_window_condition_btc(window_prices, open_price)
    close_price = window_prices[-1][1] if window_prices else None
    market_outcome = None
    if close_price is not None and open_price:
        market_outcome = 'UP' if close_price >= open_price else 'DOWN'

    skip_reasons, decisions, blocked_signals = [], [], []
    trades_summary = {'count': 0, 'resolved_count': 0, 'net_pnl': 0.0}
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT skip_reason, decision, execution_status, signal_type, side
            FROM edge_events
            WHERE engine='btc15m' AND market_slug=?
            ORDER BY timestamp_et ASC
            """,
            (prev_slug,),
        )
        rows = cur.fetchall()
        skip_reasons = sorted({str(r['skip_reason']) for r in rows if r['skip_reason']})
        decisions = sorted({str(r['decision']) for r in rows if r['decision']})
        blocked_signals = sorted({f"{r['signal_type']}:{r['side'] or 'NONE'}" for r in rows if str(r['execution_status'] or '') in ('blocked', 'skipped')})
        cur.execute(
            """
            SELECT COUNT(*) AS trade_count,
                   SUM(CASE WHEN exit_type='resolved' THEN 1 ELSE 0 END) AS resolved_count,
                   COALESCE(SUM(pnl_absolute), 0.0) AS net_pnl
            FROM trades
            WHERE engine='btc15m' AND asset=?
            """,
            (prev_slug,),
        )
        row = cur.fetchone()
        if row:
            trades_summary = {
                'count': int(row['trade_count'] or 0),
                'resolved_count': int(row['resolved_count'] or 0),
                'net_pnl': round(float(row['net_pnl'] or 0.0), 4),
            }
        conn.close()
    except Exception as e:
        log(f"[BTC-VALIDATION] summary query error: {e}")

    if trades_summary['resolved_count'] > 0:
        action_outcome = 'resolved_win' if trades_summary['net_pnl'] > 0 else ('resolved_loss' if trades_summary['net_pnl'] < 0 else 'resolved_flat')
    elif trades_summary['count'] > 0:
        action_outcome = 'traded_pending_resolution'
    elif any(r.startswith('gamma_quote_jump') or r.startswith('quote_divergence_') for r in skip_reasons):
        action_outcome = 'blocked_quote_guard'
    elif any('outside_' in r or 'block_gt_' in r for r in skip_reasons):
        action_outcome = 'blocked_shadow'
    elif 'entry_price_above_max' in skip_reasons:
        action_outcome = 'blocked_entry_price'
    elif 'gamma_clob_mismatch' in skip_reasons:
        action_outcome = 'blocked_gamma_mismatch'
    elif 'delta_below_threshold' in skip_reasons:
        action_outcome = 'no_signal'
    else:
        action_outcome = 'no_trade'

    record = {
        'timestamp_utc': datetime.now(timezone.utc).isoformat(),
        'engine': 'btc15m',
        'slug': prev_slug,
        'window_ts': prev_window_ts,
        'window_open_price': open_price,
        'window_close_price': close_price,
        'market_outcome': market_outcome,
        'condition': condition,
        'action_outcome': action_outcome,
        'skip_reasons': skip_reasons,
        'decisions': decisions,
        'blocked_signals': blocked_signals,
        'trade_count': trades_summary['count'],
        'resolved_count': trades_summary['resolved_count'],
        'net_pnl': trades_summary['net_pnl'],
        **metrics,
    }
    append_window_validation_btc(record)
    _state['validation_last_finalized_ts'] = prev_window_ts
    save_state()
    log(
        f"[BTC-VALIDATION] slug={prev_slug} condition={condition} market_outcome={market_outcome} "
        f"action_outcome={action_outcome} return={(metrics.get('window_return_pct'))} range={(metrics.get('range_pct'))}"
    )


def recent_direction_stats_btc(direction: str, lookback: int = DIR_LOOKBACK):
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
                        PARTITION BY asset, timestamp_open, direction
                        ORDER BY rowid DESC
                    ) AS rn
                FROM trades
                WHERE engine='btc15m'
                  AND direction=?
                  AND exit_type='resolved'
                  AND timestamp_close IS NOT NULL
            ),
            dedup AS (
                SELECT pnl_absolute, timestamp_close
                FROM ranked
                WHERE rn = 1
                ORDER BY timestamp_close DESC
                LIMIT ?
            )
            SELECT
                COUNT(*),
                SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END),
                COALESCE(SUM(pnl_absolute), 0.0)
            FROM dedup
            """,
            (direction, int(lookback)),
        )
        row = c.fetchone() or (0, 0, 0.0)
        conn.close()
        count = int(row[0] or 0)
        wins = int(row[1] or 0)
        pnl = float(row[2] or 0.0)
        return {
            'count': count,
            'wins': wins,
            'win_rate': (wins / count) if count else None,
            'pnl': pnl,
        }
    except Exception as e:
        log(f"[BTC-GATE] stats error direction={direction} err={e}")
        return {'count': 0, 'wins': 0, 'win_rate': None, 'pnl': 0.0}


def update_direction_confirmation(direction: str):
    now_ts = int(time.time())
    window_ts = int(_state.get('window_ts') or 0)
    prev_direction = _state.get('confirm_direction') or ''
    prev_count = int(_state.get('confirm_count') or 0)
    prev_last_ts = int(_state.get('confirm_last_ts') or 0)
    prev_window_ts = int(_state.get('confirm_window_ts') or 0)
    reset_reason = None

    if prev_window_ts != window_ts:
        count = 1
    elif prev_direction == direction and (now_ts - prev_last_ts) <= max(CONFIRM_INTERVAL_SEC * 2, 15):
        count = prev_count + 1
    else:
        count = 1
        if prev_direction and prev_direction != direction:
            reset_reason = 'direction_flip_reset'

    changed = (
        _state.get('confirm_direction') != direction
        or int(_state.get('confirm_count') or 0) != count
        or int(_state.get('confirm_last_ts') or 0) != now_ts
        or int(_state.get('confirm_window_ts') or 0) != window_ts
    )
    _state['confirm_direction'] = direction
    _state['confirm_count'] = count
    _state['confirm_last_ts'] = now_ts
    _state['confirm_window_ts'] = window_ts
    if changed:
        save_state()
    return count, reset_reason


def direction_pause_key(direction: str) -> str:
    return 'dir_pause_until_up' if direction == 'UP' else 'dir_pause_until_down'


def gate_block_btc(signal_type: str, direction: str, reason: str, delta_pct, token_price, gamma_price, book, extra: str = ''):
    bid = book.get('best_bid') if isinstance(book, dict) else None
    ask = book.get('best_ask') if isinstance(book, dict) else None
    mid = book.get('midpoint') if isinstance(book, dict) else None
    spread = book.get('spread') if isinstance(book, dict) else None
    wr = None
    pnl = None
    if extra:
        log(
            f"[BTC-GATE] BLOCK signal={signal_type} direction={direction} reason={reason} "
            f"delta={delta_pct:+.3f}% price={(f'{float(token_price):.4f}' if token_price is not None else 'NA')} "
            f"gamma={(f'{float(gamma_price):.4f}' if gamma_price is not None else 'NA')} "
            f"bid={(f'{float(bid):.4f}' if bid is not None else 'NA')} "
            f"ask={(f'{float(ask):.4f}' if ask is not None else 'NA')} "
            f"mid={(f'{float(mid):.4f}' if mid is not None else 'NA')} "
            f"spread={(f'{float(spread):.4f}' if spread is not None else 'NA')} {extra}"
        )
    else:
        log(
            f"[BTC-GATE] BLOCK signal={signal_type} direction={direction} reason={reason} "
            f"delta={delta_pct:+.3f}% price={(f'{float(token_price):.4f}' if token_price is not None else 'NA')} "
            f"gamma={(f'{float(gamma_price):.4f}' if gamma_price is not None else 'NA')} "
            f"bid={(f'{float(bid):.4f}' if bid is not None else 'NA')} "
            f"ask={(f'{float(ask):.4f}' if ask is not None else 'NA')} "
            f"mid={(f'{float(mid):.4f}' if mid is not None else 'NA')} "
            f"spread={(f'{float(spread):.4f}' if spread is not None else 'NA')}"
        )
    return False, reason


def passes_trade_gate_btc(signal_type: str, direction: str, delta_pct: float, token_price, gamma_price, book: dict):
    min_delta = direction_min_delta_btc(direction)
    delta_abs = abs(float(delta_pct or 0.0))
    if delta_abs < min_delta:
        return gate_block_btc(
            signal_type,
            direction,
            'up_delta_too_weak' if direction == 'UP' else 'down_delta_too_weak',
            delta_pct,
            token_price,
            gamma_price,
            book,
            extra=f"need={min_delta:.3f}%",
        )

    confirm_count, reset_reason = update_direction_confirmation(direction)
    if confirm_count < CONFIRM_TICKS:
        reason = reset_reason or 'confirmation_incomplete'
        return gate_block_btc(
            signal_type,
            direction,
            reason,
            delta_pct,
            token_price,
            gamma_price,
            book,
            extra=f"count={confirm_count}/{CONFIRM_TICKS}",
        )

    if ONE_TRADE_PER_WINDOW and _state.get('window_ts') and peer_has_same_direction_fill('btc15m', _state.get('window_ts'), direction):
        return gate_block_btc(signal_type, direction, 'window_direction_already_filled', delta_pct, token_price, gamma_price, book)

    spread = book.get('spread') if isinstance(book, dict) else None
    if spread is not None and float(spread) > MAX_SPREAD:
        return gate_block_btc(
            signal_type,
            direction,
            'spread_too_wide',
            delta_pct,
            token_price,
            gamma_price,
            book,
            extra=f"max={MAX_SPREAD:.4f}",
        )

    compare_price = directional_clob_price(book or {}, direction, fallback=token_price)
    if gamma_price is not None and compare_price is not None:
        mismatch = abs(float(gamma_price) - float(compare_price))
        if mismatch > MAX_GAMMA_CLOB_DIFF:
            return gate_block_btc(
                signal_type,
                direction,
                'gamma_clob_mismatch',
                delta_pct,
                token_price,
                gamma_price,
                book,
                extra=f"diff={mismatch:.4f} max={MAX_GAMMA_CLOB_DIFF:.4f}",
            )

    max_entry = direction_max_entry_btc(direction)
    if token_price is not None and float(token_price) > max_entry:
        return gate_block_btc(
            signal_type,
            direction,
            'up_entry_too_expensive' if direction == 'UP' else 'down_entry_too_expensive',
            delta_pct,
            token_price,
            gamma_price,
            book,
            extra=f"max={max_entry:.4f}",
        )

    pause_key = direction_pause_key(direction)
    pause_until = float(_state.get(pause_key) or 0)
    now_ts = time.time()
    if pause_until > now_ts:
        return gate_block_btc(
            signal_type,
            direction,
            'up_temporarily_paused' if direction == 'UP' else 'down_temporarily_paused',
            delta_pct,
            token_price,
            gamma_price,
            book,
            extra=f"until={datetime.fromtimestamp(pause_until, tz=timezone.utc).isoformat()}",
        )

    stats = recent_direction_stats_btc(direction, DIR_LOOKBACK)
    wr = stats.get('win_rate')
    pnl = float(stats.get('pnl') or 0.0)
    if stats.get('count', 0) >= DIR_LOOKBACK and ((wr is not None and wr < DIR_MIN_WR) or pnl <= DIR_MAX_LOSS):
        pause_until = now_ts + (DIR_PAUSE_HOURS * 3600.0)
        _state[pause_key] = pause_until
        save_state()
        return gate_block_btc(
            signal_type,
            direction,
            'recent_up_underperformance' if direction == 'UP' else 'recent_down_underperformance',
            delta_pct,
            token_price,
            gamma_price,
            book,
            extra=f"wr={(wr if wr is not None else 0):.2f} pnl={pnl:.2f} lookback={stats.get('count', 0)} mult={DIR_THROTTLE_MULT:.2f}",
        )

    return True, 'passed'


def verify_fill_via_trades(order_id, token_id, direction, min_size):
    vf = maker_verify_fill(token_id, min_size)
    filled_size = float(vf.get('filled_size') or 0.0)
    target = float(min_size or 0.0)
    if vf.get('filled'):
        vf['status'] = 'verified_filled' if target <= 0 or filled_size >= max(target * 0.99, target - 0.01) else 'verified_partial'
    else:
        vf['status'] = 'verified_unfilled'
    vf['order_id'] = order_id
    vf['direction'] = direction
    return vf


def get_book_metrics(token_id, submitted_price):
    """CLOB book snapshot used as execution-pricing truth."""
    book = fetch_book(token_id)
    distance_to_ask = round(book["best_ask"] - submitted_price, 4) if book.get("best_ask") is not None else None
    return {
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask"),
        "midpoint": book.get("midpoint"),
        "spread": book.get("spread"),
        "distance_to_ask": distance_to_ask,
        "tick": book.get("tick", 0.01),
        "bids": book.get("bids", []),
        "asks": book.get("asks", []),
        "error": book.get("error"),
    }


def get_exec_buy_context(market, direction, mode, price_cap, submitted_hint=0.0):
    token_id = direction_token_id_btc(market, direction)
    gamma_price = direction_gamma_price_btc(market, direction)
    book, book_fallback_reason = choose_direction_book_btc(market, direction, token_id, submitted_hint)
    clob_ref_price = book.get('midpoint') if book.get('midpoint') is not None else (book.get('best_ask') if book.get('best_ask') is not None else book.get('best_bid'))
    submitted_price, abort_reason = choose_buy_price(book, price_cap=price_cap, mode=mode, spread_cap=EXEC_SPREAD_CAP, maker_offset=MAKER_OFFSET)
    return {
        'token_id': token_id,
        'gamma_price': gamma_price,
        'clob_ref_price': clob_ref_price,
        'submitted_price': submitted_price,
        'abort_reason': abort_reason,
        'book': book,
        'book_source': book.get('source', 'clob_book'),
        'book_fallback_reason': book_fallback_reason,
        'side_label': 'BUY YES' if direction == 'UP' else 'BUY NO',
    }


def log_trade(engine, direction, size_usd, entry_price, pnl, exit_type, hold_sec, notes="", sec_remaining=None, price_bucket=None):
    try:
        timestamp_open = _current_window_open_iso()
        is_resolved = exit_type == "resolved"
        asset = _current_trade_slug()
        trade_id = _current_trade_id(direction)
        
        # Build notes with sec_remaining and price_bucket if provided
        full_notes = notes
        if sec_remaining is not None:
            full_notes = f"{full_notes} sec_rem={sec_remaining}"
        if price_bucket:
            full_notes = f"{full_notes} price_bucket={price_bucket}"
        if _state.get('shadow_decision'):
            full_notes = f"{full_notes} shadow={_state.get('shadow_decision')} shadow_bucket={_state.get('shadow_bucket')} shadow_price={_state.get('shadow_price')} shadow_reason={_state.get('shadow_reason')}"

        if USE_SHARED_JOURNAL:
            conn = open_journal(SHARED_JOURNAL_DB, "btc15m")
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
                        category="btc-updown",
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
            "btc-updown",
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
                WHERE engine='btc15m' AND exit_type='resolved' AND timestamp_close IS NOT NULL
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
            safe_log(f"[BTC-RISK] rolling_{ROLLING_RISK_LOOKBACK} pnl={rolling_pnl:+.2f} n={sample_n} -> size_mult={new_mult:.2f}")
        _state["risk_multiplier"] = new_mult
        _state["risk_last_check_ts"] = now
        return new_mult
    except Exception as e:
        safe_log(f"[BTC-RISK] risk multiplier check failed: {e}")
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
        # Cooldown state table (persists across restarts)
        c.execute("""
            CREATE TABLE IF NOT EXISTS cooldown_state (
                engine TEXT NOT NULL PRIMARY KEY,
                cool_down INTEGER NOT NULL DEFAULT 0,
                cool_down_until TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"[BTC] active_fills init error: {e}")


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
        log(f"[BTC] edge_events init error: {e}")


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
        log(f"[BTC] active_fills prune error: {e}")


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
        log(f"[BTC] active_fills record error: {e}")


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
        log(f"[BTC] active_fills peer check error: {e}")
        return False



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


def _execution_anomaly_check(submitted_price, avg_fill_price):
    try:
        if submitted_price is None or avg_fill_price is None:
            return
        sp = float(submitted_price)
        fp = float(avg_fill_price)
        if fp > sp and abs(fp - sp) > 0.05:
            log(f"[EXECUTION_ANOMALY] ADVERSE: submitted={sp:.4f} filled={fp:.4f} (paid more than limit)")
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
    _state["consecutive_losses"] = 0
    _state["diag_orders_filled"] = _state.get("diag_orders_filled", 0) + 1
    record_active_fill("btc15m", _state.get("window_ts"), fill_side)
    _state['maker_order_id'] = ""
    _state['maker_token_id'] = ""
    _state['maker_side'] = ""
    _state['maker_price'] = 0.0
    _state['maker_shares'] = 0.0
    _state['maker_last_poll'] = 0
    _state['maker_placed_ts'] = 0
    save_state()
    
    if source == "status":
        tg(f"[BTC-MAKER] FILLED order {oid}")
        log_trade("btc15m", fill_side or 'UP',
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
        log(f"[BTC-MAKER] fill verified via {vf.get('source')} size={vf.get('filled_size')} "
            f"submitted_limit={fill_price:.4f} "
            f"avg_fill_price={(f'{float(avg_fill_price):.4f}' if avg_fill_price is not None else 'n/a')} "
            f"effective_cost={(f'${float(effective_cost or 0):.4f}' if avg_fill_price is not None else 'n/a')} "
            f"tx_hashes={','.join(tx_hashes[:3]) if tx_hashes else 'n/a'}")
        _execution_anomaly_check(fill_price, avg_fill_price)
        tg(f"[BTC-MAKER] FILL VERIFIED {vf.get('filled_size')} shares")
        log_trade("btc15m", fill_side,
            vf.get('filled_size', 0) * fill_price,
            fill_price, 0, 'filled',
            int(time.time()) - fill_placed_ts,
            notes=f"maker_verify oid={oid} size={vf.get('filled_size')}",
            sec_remaining=fill_sec_remaining,
            price_bucket=fill_price_bucket)
    elif source == "fok_fallback":
        tg(f"[BTC-MAKER] FOK FALLBACK FILL")
        log_trade("btc15m", fill_side or 'UP',
            fill_shares * fill_price,
            fill_price, 0, 'filled',
            int(time.time()) - fill_placed_ts,
            notes=f"fok_fallback oid={oid}",
            sec_remaining=fill_sec_remaining,
            price_bucket=fill_price_bucket)


def check_maker_snipe(market, seconds_remaining):
    if not MAKER_ENABLED:
        return None
    
    if not check_and_manage_cooldown():
        return None
    
    oid = _state.get("maker_order_id")
    
    # ── Manage existing order ──
    if oid and (time.time() - _state.get("maker_last_poll", 0) >= MAKER_POLL_SEC or seconds_remaining <= MAKER_CANCEL_SEC):
        _state["maker_last_poll"] = int(time.time())
        st = maker_order_status(oid)
        log(f"[BTC-MAKER] status order_id={oid} -> {st.get('status')}")
        
        if st.get('status') in ('filled', 'partially_filled'):
            _handle_maker_fill(oid, source="status")
            return True
        
        if st.get('status') == 'not_found':
            log(f"[BTC-FILL] VERIFY order_id={oid} status=pending_check source=order_status_not_found")
            vf = verify_fill_via_trades(oid, _state.get('maker_token_id'), _state.get('maker_side'), _state.get('maker_shares', 0))
            if vf.get('filled'):
                fill_key = f"{oid}:{vf.get('filled_size')}"
                seen = set(_state.get('maker_seen_fills', []))
                if fill_key not in seen:
                    avg_fill = vf.get('avg_fill_price')
                    avg_str = f"{float(avg_fill):.4f}" if avg_fill is not None else 'NA'
                    log(f"[BTC-FILL] VERIFY order_id={oid} status={vf.get('status')} size={vf.get('filled_size')} avg={avg_str}")
                    _handle_maker_fill(oid, source="verify", vf=vf)
                    seen.add(fill_key)
                    _state['maker_seen_fills'] = list(seen)[-200:]
                else:
                    _state['maker_done'] = True
                    _state['snipe_done'] = True
                    save_state()
                return True
            log(f"[BTC-FILL] VERIFY order_id={oid} status={vf.get('status')} size={vf.get('filled_size', 0)} reason={vf.get('reason', 'no_recent_fill')}")
            if seconds_remaining > MAKER_RETRY_MIN_SEC and _state.get('maker_attempt_count', 0) < 3:
                _state['maker_attempt_count'] = _state.get('maker_attempt_count', 0) + 1
                log(f"[BTC-MAKER] VERIFY unfilled, resetting for retry (attempt {_state['maker_attempt_count']}/3, {seconds_remaining}s remaining)")
                _reset_maker_state_for_retry()
                return None
            _reset_maker_state_for_retry()
            _state['maker_done'] = True
            save_state()
            return False
        
        # FIX #6: FOK fallback — if maker order has been open > MAKER_FOK_FALLBACK_SEC, 
        # cancel and immediately try a FOK taker order at a slightly higher price
        placed_ts = _state.get('maker_placed_ts', 0)
        if (st.get('status') == 'open' 
            and placed_ts > 0
            and (int(time.time()) - placed_ts) >= MAKER_FOK_FALLBACK_SEC
            and seconds_remaining > MAKER_CANCEL_SEC + 5):
            
            log(f"[BTC-MAKER] FOK FALLBACK: maker open for {int(time.time()) - placed_ts}s, converting to FOK taker")
            maker_cancel_order(oid)
            tg(f"[BTC-MAKER] ORDER CANCELLED (FOK fallback): {oid} | open_for={int(time.time()) - placed_ts}s")
            
            fok_token = _state.get('maker_token_id')
            fok_shares = _state.get('maker_shares', 5.0)
            fok_book = get_book_metrics(fok_token, _state.get('maker_price', 0.0))
            fok_price, fok_abort = choose_buy_price(fok_book, price_cap=SIGNAL_MAX_ENTRY_PRICE, mode='taker', spread_cap=EXEC_SPREAD_CAP, maker_offset=0.0)
            bid_str = f"{fok_book['best_bid']:.4f}" if fok_book.get('best_bid') is not None else 'NA'
            ask_str = f"{fok_book['best_ask']:.4f}" if fok_book.get('best_ask') is not None else 'NA'
            mid_str = f"{fok_book['midpoint']:.4f}" if fok_book.get('midpoint') is not None else 'NA'
            spread_str = f"{fok_book['spread']:.4f}" if fok_book.get('spread') is not None else 'NA'
            if fok_abort:
                log(f"[BTC-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={_state.get('maker_side')} token_id={fok_token} gamma_price=NA clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={fok_abort} stage=fok_fallback")
                _state['diag_orders_cancelled'] = _state.get('diag_orders_cancelled', 0) + 1
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
                log(f"[BTC-MAKER] FOK fallback no fill, will retry if time permits")
                _state['diag_orders_cancelled'] = _state.get('diag_orders_cancelled', 0) + 1
                # FIX #5: Allow retry after FOK fallback fails
                if seconds_remaining > MAKER_RETRY_MIN_SEC and _state.get('maker_attempt_count', 0) < 3:
                    _state['maker_attempt_count'] = _state.get('maker_attempt_count', 0) + 1
                    log(f"[BTC-MAKER] RETRY enabled (attempt {_state['maker_attempt_count']}/3, {seconds_remaining}s remaining)")
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
            log(f"[BTC-MAKER] cancel by deadline order_id={oid} sec_rem={seconds_remaining}")
            tg(f"[BTC-MAKER] ORDER CANCELLED: {oid} | sec_rem={seconds_remaining}")
            
            # FIX #5: Allow retry after deadline cancel if enough time (shouldn't happen at deadline, 
            # but covers edge case where cancel_sec is generous)
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

    base_price = get_btc_price()
    if base_price is None:
        return None
    if not _state.get('window_open_btc'):
        return None
    delta_pct = (base_price - _state['window_open_btc']) / _state['window_open_btc'] * 100
    log(f"[BTC-MAKER] now={base_price} open={_state['window_open_btc']} delta={delta_pct:+.3f}% sec_rem={seconds_remaining}")
    # FIX #14/16: UP only by default. DOWN disabled (25% win rate).
    if UP_ENABLED and delta_pct > UP_MIN_DELTA:
        direction = 'UP'
    elif DOWN_ENABLED and delta_pct < -DOWN_MIN_DELTA:
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

    # ── 1-HOUR TREND FILTER ──
    prices = _state.get("btc_prices", [])
    hour_ago_ts = int(time.time()) - 3600
    hour_ago_prices = [p for t, p in prices if t <= hour_ago_ts + 60 and t >= hour_ago_ts - 60]
    if hour_ago_prices:
        hour_delta = (base_price - hour_ago_prices[0]) / hour_ago_prices[0] * 100
        # FIX #4b: Relaxed 1h trend threshold from 0.3% to 0.5%
        if direction == "UP" and hour_delta < -0.5:
            log(f"[BTC-MAKER] 1h trend DOWN ({hour_delta:+.3f}%) conflicts with UP signal, skipping")
            return None
        if direction == "DOWN" and hour_delta > 0.5:
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
        threshold = direction_min_delta_btc(direction)
        if (direction == 'UP' and d > threshold) or (direction == 'DOWN' and d < -threshold):
            confirmations += 1
    if confirmations < SIGNAL_CONFIRM_COUNT:
        log(f"[BTC-MAKER] Confirm: {confirmations}/{SIGNAL_CONFIRM_COUNT}, skipping")
        return None
    recent_avg = sum(p for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]) / len(recent_prices[-SIGNAL_CONFIRM_COUNT:])
    momentum = (recent_avg - _state['window_open_btc']) / _state['window_open_btc'] * 100
    
    # ── MOMENTUM ACCELERATION CHECK ── (FIX #4: softened threshold)
    recent = [(t, p) for t, p in prices if t >= now_ts - 120]
    if len(recent) >= 6:
        mid = len(recent) // 2
        first_half = recent[:mid]
        second_half = recent[mid:]
        delta_first = (first_half[-1][1] - first_half[0][1]) / first_half[0][1] * 100
        delta_second = (second_half[-1][1] - second_half[0][1]) / second_half[0][1] * 100
        if abs(delta_second) < abs(delta_first) * MOMENTUM_DECEL_THRESHOLD:
            log(f"[BTC-MAKER] momentum decelerating first={delta_first:+.4f}% second={delta_second:+.4f}% (threshold={MOMENTUM_DECEL_THRESHOLD}), skipping")
            return None
    
    # Require momentum to be meaningfully above the noise floor
    MOMENTUM_MIN = direction_min_delta_btc(direction) * MOMENTUM_MIN_MULTIPLIER
    if abs(momentum) < MOMENTUM_MIN:
        log(f"[BTC-MAKER] momentum={momentum:+.3f}% < {MOMENTUM_MIN:.3f}%, skipping (weak)")
        return None
    log(f"[BTC-MAKER] CONFIRMED {confirmations}/{SIGNAL_CONFIRM_COUNT} | momentum={momentum:+.3f}%")
    current_window_ts = _state.get("window_ts")
    if current_window_ts and peer_has_same_direction_fill("eth15m", current_window_ts, direction):
        log(f"[BTC-MAKER] CORRELATION BLOCK: ETH already filled {direction} in window {current_window_ts}, skipping")
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
    if maybe_block_quote_guard_btc(
        market,
        signal_type="maker",
        direction=direction,
        seconds_remaining=seconds_remaining,
        intended_entry_price=market['yes_price'] if direction == 'UP' else market['no_price'],
    ):
        return None
    ctx = get_exec_buy_context(market, direction, mode='maker', price_cap=SIGNAL_MAX_ENTRY_PRICE, submitted_hint=0.0)
    if register_quote_divergence_btc(market, 'maker', direction, seconds_remaining, ctx):
        return None
    token_id = ctx['token_id']
    token_price = ctx['clob_ref_price']
    gamma_price = ctx['gamma_price']
    book = ctx['book']
    
    # ── FILTERS: sec_remaining and entry_price ──
    if seconds_remaining < 15:
        log(f"[BTC-MAKER] FILTER: sec_remaining={seconds_remaining}s < 15s, skipping ultra-late entry")
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
        log(f"[BTC-MAKER] ABORT: pricing_source=CLOB abort_reason=missing_clob_reference slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price}")
        return None
    dir_clob_price = directional_clob_price(ctx['book'], direction, fallback=token_price)
    mismatch_diff = abs(gamma_price - dir_clob_price) if gamma_price is not None and dir_clob_price is not None else 0
    if gamma_price is not None and dir_clob_price is not None and mismatch_diff > MAX_GAMMA_CLOB_DIFF:
        log(f"[BTC-MAKER] ABORT: gamma/clob mismatch gamma_price={gamma_price:.4f} dir_clob={dir_clob_price:.4f} midpoint={token_price:.4f} diff={mismatch_diff:.4f}")
        return None
    
    
    if token_price > SIGNAL_MAX_ENTRY_PRICE:
        price_bucket = "high"
        log(f"[BTC-MAKER] FILTER: pricing_source=CLOB entry_price={token_price:.4f} gamma_price={gamma_price:.4f} > max {SIGNAL_MAX_ENTRY_PRICE:.2f} (bucket={price_bucket}), skipping high price")
        shadow_live_gate_btc(token_price, context='maker')
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
    elif token_price >= SIGNAL_MIN_ENTRY_PRICE:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
        log(f"[BTC-MAKER] FILTER: pricing_source=CLOB entry_price={token_price:.4f} gamma_price={gamma_price:.4f} < min {SIGNAL_MIN_ENTRY_PRICE:.2f} (bucket={price_bucket}), skipping low price")
        shadow_live_gate_btc(token_price, context='maker')
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

    if not shadow_live_gate_btc(token_price, context='maker'):
        price_bucket = "mid" if token_price >= 0.60 else "sweet_spot" if token_price >= SIGNAL_MIN_ENTRY_PRICE else "low"
        log(f"[BTC-MAKER] SHADOW FILTER: pricing_source=CLOB entry_price={token_price:.4f} gamma_price={gamma_price:.4f} shadow_reason={_state.get('shadow_reason')} (bucket={price_bucket}), skipping")
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

    gate_ok, gate_reason = passes_trade_gate_btc('maker', direction, delta_pct, token_price, gamma_price, book)
    if not gate_ok:
        write_edge_event(
            market,
            signal_type="maker",
            side=direction,
            intended_entry_price=token_price,
            decision="skip_gate",
            skip_reason=gate_reason,
            execution_status="blocked",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
            best_bid=book.get('best_bid'),
            best_ask=book.get('best_ask'),
            spread=book.get('spread'),
            midprice=token_price,
        )
        return None

    log(f"[BTC-MAKER] PRICE_BUCKET: {price_bucket} (clob_price={token_price:.4f} gamma_price={gamma_price:.4f})")
    _state['maker_price_bucket'] = price_bucket
    _state['maker_sec_remaining'] = seconds_remaining

    if token_price < MAKER_MIN_PRICE:
        log(f"[BTC-MAKER] ABORT: pricing_source=CLOB abort_reason=below_maker_min slug={market.get('slug','')} side={direction} token_id={token_id} clob_price={token_price:.4f} gamma_price={gamma_price:.4f}")
        return None

    limit_price = ctx['submitted_price']
    attempt_num = _state.get('maker_attempt_count', 0) + 1
    family_id = _current_maker_family_id(direction)
    side_label = ctx['side_label']
    token_id_mid = token_id
    token_id_book = token_id
    token_id_order = token_id
    bid_str = f"{book['best_bid']:.4f}" if book.get('best_bid') is not None else 'NA'
    ask_str = f"{book['best_ask']:.4f}" if book.get('best_ask') is not None else 'NA'
    mid_str = f"{book['midpoint']:.4f}" if book.get('midpoint') is not None else 'NA'
    spread_str = f"{book['spread']:.4f}" if book.get('spread') is not None else 'NA'
    if ctx['abort_reason']:
        log(f"[BTC-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={side_label} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={ctx['abort_reason']} attempt={attempt_num} family={family_id}")
        return None
    maker_budget = SNIPE_DEFAULT * current_risk_multiplier()
    shares = max(5.0, math.floor((maker_budget / max(limit_price, 0.01)) * 100) / 100)
    log(f"[BTC-MAKER] {direction} signal pricing_source=CLOB slug={market.get('slug','')} side={side_label} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={limit_price:.4f} spread={spread_str} shares={shares:.2f} dry={MAKER_DRY_RUN} attempt={attempt_num} family={family_id}")
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
    )
    r = maker_place_order('BUY', shares, limit_price, market['condition_id'], token_id_order)
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
    _state['maker_placed_ts'] = int(time.time())  # FIX #5: track placement time
    _state['maker_done'] = False
    _state['maker_attempt_number'] = attempt_num
    _state['maker_family_id'] = family_id
    _state['maker_side_label'] = side_label
    _state['maker_token_id_mid'] = token_id_mid
    _state['maker_token_id_book'] = token_id_book
    _state['maker_token_id_order'] = token_id_order
    _state['maker_book_bid'] = book.get('best_bid')
    _state['maker_book_ask'] = book.get('best_ask')
    _state['maker_book_spread'] = book.get('spread')
    _state['maker_distance_to_ask'] = book.get('distance_to_ask')
    _state['diag_orders_placed'] = _state.get('diag_orders_placed', 0) + 1
    save_state()
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
        # FIX #7: Only log arb miss once per window to reduce noise
        if not _state.get("arb_logged"):
            log(f"[ARB] No arb this window. Combined={combined:.4f} >= {ARB_THRESHOLD}")
            _state["arb_logged"] = True
        return None

    log(f"[ARB] FOUND! YES={market['yes_price']:.4f} NO={market['no_price']:.4f} COMBINED={combined:.4f}")
    yes_tid = direction_token_id_btc(market, 'UP')
    no_tid  = direction_token_id_btc(market, 'DOWN')

    risk_mult = current_risk_multiplier()
    arb_budget = ARB_SIZE * risk_mult
    yes_shares = arb_budget / market["yes_price"]
    no_shares  = arb_budget / market["no_price"]

    r1 = place_order("BUY", yes_shares, market["yes_price"], market["condition_id"], yes_tid)
    r2 = place_order("BUY", no_shares,  market["no_price"],  market["condition_id"], no_tid)

    arb_profit = (1.00 - combined) * min(yes_shares, no_shares)
    notes = f"arb profit=${arb_profit:.2f} yes_shares={yes_shares:.2f} no_shares={no_shares:.2f} risk_mult={risk_mult:.2f}"
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

    # FIX #14: Asymmetric delta — DOWN requires stronger signal
    direction = None
    # FIX #16: DOWN disabled by default
    if UP_ENABLED and delta_pct > UP_MIN_DELTA:
        direction = "UP"
    elif DOWN_ENABLED and delta_pct < -DOWN_MIN_DELTA:
        direction = "DOWN"
    else:
        log(f"[SNIPE] Delta {delta_pct:+.3f}% — no valid signal (DOWN_ENABLED={DOWN_ENABLED})")
        write_edge_event(
            market,
            signal_type="snipe",
            decision="skip_no_signal",
            skip_reason="delta_below_threshold",
            execution_status="skipped",
            seconds_remaining=seconds_remaining,
        )
        return None

    current_window_ts = _state.get("window_ts")
    if current_window_ts and peer_has_same_direction_fill("eth15m", current_window_ts, direction):
        log(f"[BTC-SNIPE] CORRELATION BLOCK: ETH already filled {direction} in window {current_window_ts}, skipping")
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

    if maybe_block_quote_guard_btc(
        market,
        signal_type="snipe",
        direction=direction,
        seconds_remaining=seconds_remaining,
        intended_entry_price=market['yes_price'] if direction == 'UP' else market['no_price'],
    ):
        return None

    ctx = get_exec_buy_context(market, direction, mode='taker', price_cap=SNIPE_MAX_PRICE, submitted_hint=0.0)
    if register_quote_divergence_btc(market, 'snipe', direction, seconds_remaining, ctx):
        return None
    token_id = ctx['token_id']
    gamma_price = ctx['gamma_price']
    price = ctx['clob_ref_price']
    book = ctx['book']

    if seconds_remaining < 15:
        log(f"[SNIPE] FILTER: sec_remaining={seconds_remaining}s < 15s, skipping ultra-late entry")
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
        log(f"[BTC-SNIPE] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price} abort_reason=missing_clob_reference")
        return None
    snipe_compare = directional_clob_price(ctx.get('book', {}), direction, fallback=price)
    snipe_mismatch = abs(gamma_price - snipe_compare) if gamma_price is not None and snipe_compare is not None else 0
    if gamma_price is not None and snipe_compare is not None and snipe_mismatch > MAX_GAMMA_CLOB_DIFF:
        log(f"[BTC-SNIPE] ABORT: gamma/clob mismatch gamma_price={gamma_price:.4f} dir_clob={snipe_compare:.4f} midpoint={price:.4f} diff={snipe_mismatch:.4f}")
        return None
    
    
    if price > SIGNAL_MAX_ENTRY_PRICE:
        price_bucket = "high"
        log(f"[SNIPE] FILTER: pricing_source=CLOB entry_price={price:.4f} gamma_price={gamma_price:.4f} > max {SIGNAL_MAX_ENTRY_PRICE:.2f} (bucket={price_bucket}), skipping high price")
        shadow_live_gate_btc(price, context='snipe')
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
    elif price >= SIGNAL_MIN_ENTRY_PRICE:
        price_bucket = "sweet_spot"
    else:
        price_bucket = "low"
        log(f"[SNIPE] FILTER: pricing_source=CLOB entry_price={price:.4f} gamma_price={gamma_price:.4f} < floor {SIGNAL_MIN_ENTRY_PRICE:.2f} (bucket={price_bucket}), skipping low price")
        shadow_live_gate_btc(price, context='snipe')
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

    if not shadow_live_gate_btc(price, context='snipe'):
        log(f"[SNIPE] SHADOW FILTER: pricing_source=CLOB entry_price={price:.4f} gamma_price={gamma_price:.4f} shadow_reason={_state.get('shadow_reason')} (bucket={price_bucket}), skipping")
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

    gate_ok, gate_reason = passes_trade_gate_btc('snipe', direction, delta_pct, price, gamma_price, book)
    if not gate_ok:
        write_edge_event(
            market,
            signal_type="snipe",
            side=direction,
            intended_entry_price=price,
            decision="skip_gate",
            skip_reason=gate_reason,
            execution_status="blocked",
            seconds_remaining=seconds_remaining,
            shadow_decision=_state.get('shadow_decision', ''),
            shadow_skip_reason=_state.get('shadow_reason', ''),
            best_bid=book.get('best_bid'),
            best_ask=book.get('best_ask'),
            spread=book.get('spread'),
            midprice=price,
        )
        return None

    submitted_price = ctx['submitted_price']
    bid_str = f"{book['best_bid']:.4f}" if book.get('best_bid') is not None else 'NA'
    ask_str = f"{book['best_ask']:.4f}" if book.get('best_ask') is not None else 'NA'
    mid_str = f"{book['midpoint']:.4f}" if book.get('midpoint') is not None else 'NA'
    spread_str = f"{book['spread']:.4f}" if book.get('spread') is not None else 'NA'
    if ctx['abort_reason']:
        log(f"[BTC-SNIPE] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={ctx['abort_reason']}")
        return None

    log(f"[SNIPE] PRICE_BUCKET: {price_bucket} (clob_price={price:.4f} gamma_price={gamma_price:.4f})")
    base_size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    size = base_size * current_risk_multiplier()
    shares = size / submitted_price
    if shares < 5:
        shares = 5
        size = shares * submitted_price

    log(f"[BTC-SNIPE] pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={submitted_price:.4f} spread={spread_str}")
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
    log(f"[SNIPE] Result: {r.get('success')} filled={r.get('filled')}. {notes}")
    if os.getenv("BTC15M_ONE_SHOT_TEST") == "1":
        _state["snipe_done"] = True
        save_state()
        log("[SNIPE] ONE_SHOT_TEST active — stopping after first BTC attempt in this window")

    if not r.get('filled') and not DRY_RUN:
        log(f"[SNIPE] No fill confirmed — not counting this trade as real")
        tg(f"[BTC-SNIPE] NO FILL confirmed | {notes}")
        return False
    fill = r.get('fill_check') or {}
    avg_fill_price = fill.get('avg_fill_price')
    effective_cost = fill.get('effective_cost')
    tx_hashes = fill.get('tx_hashes') or []
    if avg_fill_price is not None:
        log(f"[BTC-SNIPE] fill verified via {fill.get('source')} size={r.get('filled_size', shares)} submitted_price={price:.4f} avg_fill_price={float(avg_fill_price):.4f} effective_cost=${float(effective_cost or 0):.4f} tx_hashes={','.join(tx_hashes[:3]) if tx_hashes else 'n/a'}")
        _execution_anomaly_check(price, avg_fill_price)
    tg(f"[BTC-SNIPE] FILL CONFIRMED {r.get('filled_size', shares):.2f} shares | {notes}")
    record_active_fill("btc15m", _state.get("window_ts"), direction)

    _state["snipe_done"]        = True
    _state["prev_snipe_side"]  = direction
    _state["prev_snipe_price"] = price
    _state["prev_snipe_size"]  = size
    save_state()
    return True

# ── New window setup ──────────────────────────────────────────────────────────
def setup_new_window(slug, window_ts):
    prune_active_fills(window_ts)
    btc_price = get_btc_price()
    if btc_price is None:
        btc_price = _state.get("window_open_btc", 0.0)

    # FIX #3: Keep 70 minutes of price history for 1-hour trend filter
    # (was being trimmed to current window_ts in main loop, breaking the 1h lookback)
    cutoff = int(time.time()) - 4200  # 70 minutes
    old_prices = _state.get("btc_prices", [])
    _state["btc_prices"] = [(t, p) for t, p in old_prices if t >= cutoff]
    if btc_price:
        _state["btc_prices"].append((int(time.time()), btc_price))
    
    _state["window_ts"]       = window_ts
    _state["window_open_btc"] = btc_price
    _state["arb_done"]        = False
    _state["arb_logged"]      = False  # FIX #7: reset arb log flag
    _state["snipe_done"]      = False
    _state["maker_order_id"]  = ""
    _state["maker_token_id"]  = ""
    _state["maker_side"]      = ""
    _state["maker_price"]     = 0.0
    _state["maker_shares"]    = 0.0
    _state["maker_done"]      = False
    _state["maker_last_poll"] = 0
    _state["maker_placed_ts"] = 0
    _state["maker_attempt_count"] = 0  # FIX #5: reset retry counter
    _state["prev_slug"]       = slug
    _state['quote_prev_window_ts'] = window_ts
    _state['quote_prev_yes'] = None
    _state['quote_prev_no'] = None
    _state['quote_guard_window_ts'] = 0
    _state['quote_guard_reason'] = ''
    _state['quote_divergence_count_up'] = 0
    _state['quote_divergence_count_down'] = 0
    save_state()

    log(f"[NEW WINDOW] ts={window_ts} BTC={btc_price} slug={slug}")

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
    init_runtime_db()
    log("=" * 60)
    log(f"[BTC-15M] STARTING {'(DRY RUN)' if DRY_RUN else '(LIVE)'}")
    log(f"[BTC-15M] Arb threshold=${ARB_THRESHOLD}, up_delta>={UP_MIN_DELTA}% down_delta>={DOWN_MIN_DELTA}%, confirm={CONFIRM_TICKS} ticks, max daily loss=${MAX_DAILY_LOSS}")
    log(f"[BTC-15M] min_entry={SIGNAL_MIN_ENTRY_PRICE:.2f} generic_max={SIGNAL_MAX_ENTRY_PRICE:.2f} up_max={UP_MAX_ENTRY:.2f} down_max={DOWN_MAX_ENTRY:.2f} spread_max={MAX_SPREAD:.3f} gamma_clob_max={MAX_GAMMA_CLOB_DIFF:.3f}")
    log(f"[BTC-15M] maker={MAKER_ENABLED} dry={MAKER_DRY_RUN} start=T-{MAKER_START_SEC} cancel=T-{MAKER_CANCEL_SEC} offset={MAKER_OFFSET} fok_fallback={MAKER_FOK_FALLBACK_SEC}s retry_min={MAKER_RETRY_MIN_SEC}s")
    log(f"[BTC-15M] dir_throttle: lookback={DIR_LOOKBACK} min_wr={DIR_MIN_WR:.2f} max_loss={DIR_MAX_LOSS:.2f} pause_h={DIR_PAUSE_HOURS} one_trade_per_window={ONE_TRADE_PER_WINDOW}")
    log(f"[BTC-15M] cooldown: threshold={COOLDOWN_LOSS_THRESHOLD} losses, duration={COOLDOWN_DURATION_SEC // 60}min, lookback={COOLDOWN_LOOKBACK_SEC // 60}min decel_threshold={MOMENTUM_DECEL_THRESHOLD}")
    log(f"[BTC-15M] quote_guard: jump_max={QUOTE_JUMP_MAX:.3f} divergence_max={QUOTE_DIVERGENCE_MAX:.3f} divergence_cycles={QUOTE_DIVERGENCE_CYCLES}")
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
            # FIX #8: Log fill rate diagnostics on window transition
            placed = _state.get('diag_orders_placed', 0)
            filled = _state.get('diag_orders_filled', 0)
            cancelled = _state.get('diag_orders_cancelled', 0)
            if placed > 0:
                log(f"[DIAG] Window fill rate: {filled}/{placed} filled, {cancelled} cancelled ({filled/placed*100:.0f}% fill rate)")
            
            log(f"[CYCLE] New window detected: {slug}")
            finalize_window_validation_btc()
            trigger_post_resolution_tasks()
            setup_new_window(slug, window_ts)

        # Safety
        if not check_daily_limit():
            time.sleep(60)
            continue

        # Cooldown check (loss-based, DB-persisted)
        if not check_and_manage_cooldown():
            time.sleep(SCAN_SEC)
            continue

        # Get market
        market = get_market(slug)
        if not market:
            log(f"[CYCLE] No market found for {slug}, sleeping 10s...")
            time.sleep(10)
            continue

        update_quote_guard_btc(market, sec_rem)

        log(f"[CYCLE] {market['question'][:60]} | YES={market['yes_price']:.3f} NO={market['no_price']:.3f} | {sec_rem}s left")

        # Record BTC price
        btc = get_btc_price()
        if btc:
            _state["btc_prices"].append((int(time.time()), btc))
            # FIX #3: Keep 70 minutes of history (was trimming to window_ts, breaking 1h trend)
            cutoff = int(time.time()) - 4200  # 70 minutes
            _state["btc_prices"] = [(t, p) for t, p in _state["btc_prices"] if t >= cutoff]

        # Strategy A: Arb (first 14.75 min)
        if sec_rem > 15:
            check_arb(market)

        # Gabagool tracking
        check_gabagool(market, sec_rem)

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
        log("[BTC-15M] Stopped by user")
