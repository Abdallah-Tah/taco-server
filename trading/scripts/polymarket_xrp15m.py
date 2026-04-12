#!/usr/bin/env python3
"""
polymarket_xrp15m.py — XRP 15-Minute Polymarket Engine
======================================================
Two strategies:
  A) Binary Arb      — buy UP+DOWN when combined < $0.98, guaranteed profit
  B) Late Snipe      — directional bet near window close based on XRP delta

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

from polymarket_clob_pricing import fetch_book, choose_buy_price
from scalp_exit import ScalpExitManager

# ── Paths & creds ──────────────────────────────────────────────────────────────
WORK_DIR      = Path("/home/abdaltm86/.openclaw/workspace/trading")
JOURNAL_DB    = WORK_DIR / "journal.db"
CREDS_FILE    = Path("/home/abdaltm86/.config/openclaw/secrets.env")
POSITIONS_F  = WORK_DIR / ".poly_xrp15m_positions.json"
STATE_F       = WORK_DIR / ".poly_xrp15m_state.json"
LOG_F        = WORK_DIR / ".poly_xrp15m.log"

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
CHAT_ID        = ENV.get("CHAT_ID",        "-1003948211258")
TOPIC_ID       = ENV.get("TOPIC_ID",       "3")
POLY_WALLET    = ENV.get("POLY_WALLET",    "0x1a4c163a134D7154ebD5f7359919F9c439424f00")
VENV_PY        = Path("/home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3")
DRY_RUN        = os.environ.get("XRP15M_DRY_RUN",  ENV.get("XRP15M_DRY_RUN",  "true")).lower() != "false"
MAKER_ENABLED  = os.environ.get("XRP15M_MAKER_ENABLED", ENV.get("XRP15M_MAKER_ENABLED", "false")).lower() == "true"
MAKER_DRY_RUN  = os.environ.get("XRP15M_MAKER_DRY_RUN", ENV.get("XRP15M_MAKER_DRY_RUN", "true")).lower() != "false"
MAKER_START_SEC = _int("XRP15M_MAKER_START_SEC", 300)
MAKER_CANCEL_SEC = _int("XRP15M_MAKER_CANCEL_SEC", 30)
MAKER_OFFSET = _float("XRP15M_MAKER_OFFSET", 0.005)
MAKER_POLL_SEC = _int("XRP15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("XRP15M_MAKER_MIN_PRICE", 0.01)

# ── Config ────────────────────────────────────────────────────────────────────
WINDOW_SEC      = _int("XRP15M_WINDOW_SEC", 900)          # 15 minutes
ARB_THRESHOLD   = _float("XRP15M_ARB_THRESHOLD", 0.98)
ARB_SIZE        = _float("XRP15M_ARB_SIZE", 10.00)
SNIPE_DELTA_MIN = _float("XRP15M_SNIPE_DELTA_MIN", 0.025)
SNIPE_MAX_PRICE = _float("XRP15M_SNIPE_MAX_PRICE", 0.90)
EXEC_SPREAD_CAP = _float("XRP15M_EXEC_SPREAD_CAP", 0.10)
XRP_EXEC_BOOK_SPREAD_CAP = _float("XRP15M_BOOK_SPREAD_CAP", 0.03)
XRP_EXEC_GAMMA_CLOB_MAX = _float("XRP15M_GAMMA_CLOB_MAX", 0.05)
# Signal confirmation (improved filtering)
SIGNAL_CONFIRM_COUNT   = _int("XRP15M_SIGNAL_CONFIRM_COUNT", 2)   # consecutive polls needed
SIGNAL_CONFIRM_SEC     = _int("XRP15M_SIGNAL_CONFIRM_SEC", 15)     # max age of confirm samples
SIGNAL_MAX_ENTRY_PRICE = _float("XRP15M_SIGNAL_MAX_ENTRY_PRICE", 0.85)  # skip if entry worse than this
SNIPE_DEFAULT   = _float("XRP15M_SNIPE_DEFAULT_SIZE", 5.00)
SNIPE_STRONG    = _float("XRP15M_SNIPE_STRONG_SIZE", 7.50)
SNIPE_STRONG_D  = _float("XRP15M_SNIPE_STRONG_DELTA", 0.10)  # percent
SNIPE_WINDOW    = _int("XRP15M_SNIPE_WINDOW_SEC", 30)
POLL_SEC        = _int("XRP15M_PRICE_POLL_SEC", 5)
SCAN_SEC        = _int("XRP15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS  = _float("XRP15M_MAX_DAILY_LOSS", 15.00)
SERIES_ID       = "10192"
SERIES_SLUG     = "xrp-updown-15m"

# ── State ─────────────────────────────────────────────────────────────────────
_state = {
    "window_ts":       0,
    "window_open_xrp": 0.0,
    "arb_done":        False,
    "snipe_done":      False,
    "xrp_prices":      [],   # list of (timestamp, price)
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
    "maker_placed_ts":  0,
    "maker_seen_fills": [],
    "diag_orders_placed": 0,
    "diag_orders_cancelled": 0,
}
_positions = []   # list of dicts for open/confirmation positions

scalp_mgr = None  # initialized in main()

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
                json={"chat_id": CHAT_ID, "text": msg, "message_thread_id": int(TOPIC_ID)},
                timeout=10,
            )
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


def _book_quality_abort(ctx, mode='maker'):
    book = ctx.get('book') or {}
    best_bid = book.get('best_bid')
    best_ask = book.get('best_ask')
    midpoint = book.get('midpoint')
    spread = book.get('spread')
    gamma_price = ctx.get('gamma_price')
    submitted_price = ctx.get('submitted_price')

    if best_ask is None:
        return 'missing_best_ask'
    if mode == 'maker' and best_bid is None:
        return 'missing_best_bid'
    if spread is None:
        return 'missing_spread'
    if spread > XRP_EXEC_BOOK_SPREAD_CAP:
        return f'spread_too_wide:{spread:.4f}>{XRP_EXEC_BOOK_SPREAD_CAP:.4f}'
    if gamma_price is not None and midpoint is not None and abs(midpoint - gamma_price) > XRP_EXEC_GAMMA_CLOB_MAX:
        return f'mid_vs_gamma_too_wide:{midpoint:.4f}:{gamma_price:.4f}'
    if gamma_price is not None and submitted_price is not None and abs(submitted_price - gamma_price) > XRP_EXEC_GAMMA_CLOB_MAX:
        return f'submitted_vs_gamma_too_wide:{submitted_price:.4f}:{gamma_price:.4f}'
    return None


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
    _state.setdefault('maker_seen_fills', [])
    _state.setdefault('maker_placed_ts', 0)
    _state.setdefault('diag_orders_placed', 0)
    _state.setdefault('diag_orders_cancelled', 0)

# ── XRP price from Coinbase ────────────────────────────────────────────────────
def get_xrp_price():
    try:
        r = requests.get(
            "https://api.exchange.coinbase.com/products/XRP-USD/ticker",
            timeout=5,
        )
        return float(r.json()["price"])
    except Exception as e:
        log(f"XRP price error: {e}")
        return None


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

# ── Find current market slug ──────────────────────────────────────────────────
def get_current_slug():
    now = int(time.time())
    window_ts = now - (now % WINDOW_SEC)
    return f"xrp-updown-15m-{window_ts}", window_ts

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
                "slug":         m.get("slug") or slug,
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
            f"xrp15m_{int(time.time()*1000)}",
            engine, datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat() if exit_type else None,
            "btc-15m",
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
        log(f"[XRP-MAKER-DRY] {side} {shares} @{price:.4f} token={token_id}")
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
        log(f"[XRP-MAKER] status order_id={oid} -> {st.get('status')}")
        if st.get('status') in ('filled', 'partially_filled'):
            _state['maker_done'] = True
            _state['snipe_done'] = True
            save_state()
            tg(f"[XRP-MAKER] FILLED order {oid}")
            log_trade("xrp15m", _state.get('maker_side', 'UP'), 
                _state.get('maker_shares', 0) * _state.get('maker_price', 0),
                _state.get('maker_price', 0), 0, 'filled',
                int(time.time()) - _state.get('maker_last_poll', int(time.time())),
                notes=f"maker_fill oid={oid}")
            return True
        if st.get('status') == 'not_found':
            vf = maker_verify_fill(_state.get('maker_token_id'), _state.get('maker_shares', 0))
            if vf.get('filled'):
                fill_key = f"{oid}:{vf.get('filled_size')}"
                seen = set(_state.get('maker_seen_fills', []))
                if fill_key not in seen:
                    log(f"[XRP-MAKER] fill verified via activity/trades size={vf.get('filled_size')}")
                    tg(f"[XRP-MAKER] FILL VERIFIED {vf.get('filled_size')} shares")
                    # Log verified fill to journal
                    log_trade("xrp15m", _state.get('maker_side', 'UP'),
                        vf.get('filled_size', 0) * _state.get('maker_price', 0),
                        _state.get('maker_price', 0), 0, 'filled',
                        int(time.time()) - _state.get('maker_last_poll', int(time.time())),
                        notes=f"maker_verify oid={oid} size={vf.get('filled_size')}")
                    seen.add(fill_key)
                    _state['maker_seen_fills'] = list(seen)[-200:]
                _state['maker_done'] = True
                _state['snipe_done'] = True
                save_state()
                return True
            no_fill_reason = vf.get('reason', 'no_recent_fill')
            open_for = max(0, int(time.time()) - int(_state.get('maker_placed_ts', 0) or 0))
            log(f"[XRP-MAKER] VERIFY NO FILL: {oid} | open_for={open_for}s | reason={no_fill_reason}")
            tg(f"[XRP-MAKER] ORDER CANCELLED (verify no fill): {oid} | open_for={open_for}s")
            _state['diag_orders_cancelled'] = _state.get('diag_orders_cancelled', 0) + 1
            _state['maker_done'] = True
            save_state()
            return False
        if seconds_remaining <= MAKER_CANCEL_SEC and st.get('status') in ('open', 'partially_filled'):
            maker_cancel_order(oid)
            log(f"[XRP-MAKER] cancel by deadline order_id={oid} sec_rem={seconds_remaining}")
            tg(f"[XRP-MAKER] ORDER CANCELLED: {oid} | sec_rem={seconds_remaining}")
            _state['maker_done'] = True
            save_state()
            return False
    if _state.get('maker_done') or oid:
        return None
    if seconds_remaining > MAKER_START_SEC or seconds_remaining <= MAKER_CANCEL_SEC:
        return None

    base_price = get_xrp_price()
    if base_price is None:
        return None
    if not _state.get('window_open_xrp'):
        return None
    delta_pct = (base_price - _state['window_open_xrp']) / _state['window_open_xrp'] * 100
    log(f"[XRP-MAKER] now={base_price} open={_state['window_open_xrp']} delta={delta_pct:+.3f}% sec_rem={seconds_remaining}")
    direction = 'UP' if delta_pct > SNIPE_DELTA_MIN else ('DOWN' if delta_pct < -SNIPE_DELTA_MIN else None)
    if not direction:
        return None

    # ── Signal confirmation: require consecutive polls agreeing ──
    now_ts = int(time.time())
    cutoff_ts = now_ts - SIGNAL_CONFIRM_SEC
    recent_prices = [(t, p) for t, p in _state.get('xrp_prices', []) if t >= cutoff_ts]
    if len(recent_prices) < SIGNAL_CONFIRM_COUNT:
        log(f"[XRP-MAKER] Confirm: only {len(recent_prices)}/{SIGNAL_CONFIRM_COUNT} samples, waiting")
        return None
    confirmations = 0
    for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]:
        d = (p - _state['window_open_xrp']) / _state['window_open_xrp'] * 100
        if (direction == 'UP' and d > SNIPE_DELTA_MIN) or (direction == 'DOWN' and d < -SNIPE_DELTA_MIN):
            confirmations += 1
    if confirmations < SIGNAL_CONFIRM_COUNT:
        log(f"[XRP-MAKER] Confirm: {confirmations}/{SIGNAL_CONFIRM_COUNT}, skipping")
        return None
    recent_avg = sum(p for _, p in recent_prices[-SIGNAL_CONFIRM_COUNT:]) / len(recent_prices[-SIGNAL_CONFIRM_COUNT:])
    momentum = (recent_avg - _state['window_open_xrp']) / _state['window_open_xrp'] * 100
    # Require momentum to be meaningfully above the noise floor (1.5% = 60% of delta threshold)
    MOMENTUM_MIN = SNIPE_DELTA_MIN * 1.50
    if abs(momentum) < MOMENTUM_MIN:
        log(f"[XRP-MAKER] momentum={momentum:+.3f}% < {MOMENTUM_MIN:.3f}%, skipping (weak)")
        return None
    log(f"[XRP-MAKER] CONFIRMED {confirmations}/{SIGNAL_CONFIRM_COUNT} | momentum={momentum:+.3f}%")
    ctx = get_exec_buy_context(market, direction, mode='maker', price_cap=SIGNAL_MAX_ENTRY_PRICE, submitted_hint=0.0)
    token_id = ctx['token_id']
    token_price = ctx['clob_ref_price']
    gamma_price = ctx['gamma_price']
    book = ctx['book']
    bid_str = f"{book['best_bid']:.4f}" if book.get('best_bid') is not None else 'NA'
    ask_str = f"{book['best_ask']:.4f}" if book.get('best_ask') is not None else 'NA'
    mid_str = f"{book['midpoint']:.4f}" if book.get('midpoint') is not None else 'NA'
    spread_str = f"{book['spread']:.4f}" if book.get('spread') is not None else 'NA'
    if token_price is None:
        log(f"[XRP-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason=missing_clob_reference")
        return None
    if token_price < MAKER_MIN_PRICE:
        log(f"[XRP-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason=below_maker_min")
        return None
    if token_price > SIGNAL_MAX_ENTRY_PRICE:
        log(f"[XRP-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={direction} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason=slippage_cap_exceeded:{token_price:.4f}>{SIGNAL_MAX_ENTRY_PRICE:.4f}")
        return None
    limit_price = ctx['submitted_price']
    if ctx['abort_reason']:
        log(f"[XRP-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={ctx['abort_reason']}")
        return None
    quality_abort = _book_quality_abort(ctx, mode='maker')
    if quality_abort:
        log(f"[XRP-MAKER] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={limit_price:.4f} spread={spread_str} abort_reason={quality_abort}")
        return None
    shares = max(5.0, math.floor((SNIPE_DEFAULT / max(limit_price, 0.01)) * 100) / 100)
    log(f"[XRP-MAKER] pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={limit_price:.4f} spread={spread_str} shares={shares:.2f} dry={MAKER_DRY_RUN}")
    r = maker_place_order('BUY', shares, limit_price, market['condition_id'], token_id)
    for line in (r.get('output') or '').splitlines():
        if line.strip() and ('[MAKER' in line or '[RESULT' in line or '[ATTEMPT' in line):
            log(f"[EXEC] {line.strip()}")
    if r.get('error'):
        for line in r['error'].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")
    posted_oid = str(r.get('order_id') or (r.get('posted') or {}).get('order_id') or '')
    if not r.get('success') or not posted_oid:
        fail_reason = 'missing_order_id' if r.get('success') and not posted_oid else 'executor_error'
        log(f"[XRP-MAKER] submit failed reason={fail_reason} sec_rem={seconds_remaining} output_oid={posted_oid or 'none'}")
        _state['maker_done'] = True
        save_state()
        return False
    _state['maker_order_id'] = posted_oid
    _state['maker_token_id'] = token_id
    _state['maker_side'] = direction
    _state['maker_price'] = limit_price
    _state['maker_shares'] = shares
    _state['maker_last_poll'] = int(time.time())
    _state['maker_placed_ts'] = int(time.time())
    _state['maker_done'] = False
    _state['diag_orders_placed'] = _state.get('diag_orders_placed', 0) + 1
    save_state()
    # Send Telegram notification for order placement
    tg(f"[XRP-MAKER] ORDER PLACED: {direction} {shares:.2f} shares @ {limit_price:.4f} | Order: {posted_oid}")
    return True

# ── Strategy A: Arb check ─────────────────────────────────────────────────────
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
    tg(f"[XRP-ARB] {notes}")

    _state["arb_done"] = True
    save_state()
    return True

# ── Strategy B: Snipe check ──────────────────────────────────────────────────
def check_snipe(market, seconds_remaining):
    if _state["snipe_done"]:
        return None
    if seconds_remaining > SNIPE_WINDOW or seconds_remaining < 5:
        return None

    xrp_price = get_xrp_price()
    if xrp_price is None:
        return None

    if not _state["window_open_xrp"]:
        log(f"[SNIPE] No window_open_xrp recorded, skipping. XRP={xrp_price}")
        return None

    delta_pct = (xrp_price - _state["window_open_xrp"]) / _state["window_open_xrp"] * 100
    log(f"[SNIPE] XRP now={xrp_price} window_open={_state['window_open_xrp']} delta={delta_pct:+.3f}%")

    direction = None
    if delta_pct > SNIPE_DELTA_MIN:
        direction = "UP"
    elif delta_pct < -SNIPE_DELTA_MIN:
        direction = "DOWN"
    else:
        log(f"[SNIPE] Delta {delta_pct:+.3f}% too small, skipping")
        return None

    ctx = get_exec_buy_context(market, direction, mode='taker', price_cap=SNIPE_MAX_PRICE, submitted_hint=0.0)
    token_id = ctx['token_id']
    price = ctx['clob_ref_price']
    submitted_price = ctx['submitted_price']
    gamma_price = ctx['gamma_price']
    book = ctx['book']
    bid_str = f"{book['best_bid']:.4f}" if book.get('best_bid') is not None else 'NA'
    ask_str = f"{book['best_ask']:.4f}" if book.get('best_ask') is not None else 'NA'
    mid_str = f"{book['midpoint']:.4f}" if book.get('midpoint') is not None else 'NA'
    spread_str = f"{book['spread']:.4f}" if book.get('spread') is not None else 'NA'
    if price is None or ctx['abort_reason']:
        log(f"[XRP-SNIPE] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price=NA spread={spread_str} abort_reason={ctx['abort_reason'] or 'missing_clob_reference'}")
        return None
    quality_abort = _book_quality_abort(ctx, mode='taker')
    if quality_abort:
        log(f"[XRP-SNIPE] ABORT: pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={submitted_price:.4f} spread={spread_str} abort_reason={quality_abort}")
        return None

    size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    shares = size / submitted_price
    # Ensure minimum 5 shares for Polymarket
    if shares < 5:
        shares = 5
        size = shares * submitted_price

    log(f"[XRP-SNIPE] pricing_source=CLOB slug={market.get('slug','')} side={ctx['side_label']} token_id={token_id} gamma_price={gamma_price:.4f} clob_best_bid={bid_str} clob_best_ask={ask_str} clob_midpoint={mid_str} submitted_price={submitted_price:.4f} spread={spread_str}")
    r = place_order("BUY", shares, submitted_price, market["condition_id"], token_id)
    # Log executor output so [ATTEMPT]/[RESULT] lines are visible
    for line in (r.get("output") or "").splitlines():
        if any(tag in line for tag in ("[ATTEMPT]", "[RESULT]", "Book best", "FILL")):
            log(f"[EXEC] {line.strip()}")
    if r.get("error"):
        for line in r["error"].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")
    notes = f"snipe {direction} delta={delta_pct:+.3f}% submitted_price={submitted_price:.4f} clob_midpoint={price:.4f} gamma_price={gamma_price:.4f} size=${size:.2f}"
    log(f"[SNIPE] Result: {r.get('success')} filled={r.get('filled')}. {notes}")
    if os.getenv("XRP15M_ONE_SHOT_TEST") == "1":
        _state["snipe_done"] = True
        save_state()
        log("[SNIPE] ONE_SHOT_TEST active — stopping after first XRP attempt in this window")

    if not r.get('filled') and not DRY_RUN:
        log(f"[SNIPE] No fill confirmed — not counting this trade as real")
        tg(f"[XRP-SNIPE] NO FILL confirmed | {notes}")
        return False
    tg(f"[XRP-SNIPE] FILL CONFIRMED {r.get('filled_size', shares):.2f} shares | {notes}")

    _state["snipe_done"]        = True
    _state["prev_snipe_side"]  = direction
    _state["prev_snipe_price"] = submitted_price
    _state["prev_snipe_size"]  = size
    save_state()
    return True

# ── New window setup ──────────────────────────────────────────────────────────
def setup_new_window(slug, window_ts):
    xrp_price = get_xrp_price()
    if xrp_price is None:
        xrp_price = _state.get("window_open_xrp", 0.0)

    _state["window_ts"]       = window_ts
    _state["window_open_xrp"] = xrp_price
    _state["arb_done"]        = False
    _state["snipe_done"]      = False
    _state["maker_order_id"]  = ""
    _state["maker_token_id"]  = ""
    _state["maker_side"]      = ""
    _state["maker_price"]     = 0.0
    _state["maker_shares"]    = 0.0
    _state["maker_done"]      = False
    _state["maker_last_poll"] = 0
    _state["xrp_prices"]      = [(int(time.time()), xrp_price)]
    _state["prev_slug"]       = slug
    save_state()
    trigger_post_resolution_tasks()

    # Reset scalp exit for new window
    if scalp_mgr is not None:
        scalp_mgr.reset()

    log(f"[NEW WINDOW] ts={window_ts} XRP={xrp_price} slug={slug}")
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
    global _state, scalp_mgr
    load_state()
    log("=" * 60)
    scalp = ScalpExitManager(
        engine="xrp15m",
        venv_py=VENV_PY,
        work_dir=WORK_DIR,
        log_fn=log,
        tg_fn=tg,
        dry_run=DRY_RUN,
        env_dict=ENV,
    )
    scalp_mgr = scalp
    log(f"[XRP-15M] STARTING {'(DRY RUN)' if DRY_RUN else '(LIVE)'}")
    log(f"[XRP-15M] Arb threshold=${ARB_THRESHOLD}, snipe delta>={SNIPE_DELTA_MIN}%, max daily loss=${MAX_DAILY_LOSS} | maker={MAKER_ENABLED} dry={MAKER_DRY_RUN} start=T-{MAKER_START_SEC} cancel=T-{MAKER_CANCEL_SEC} offset={MAKER_OFFSET}")
    log(f"[XRP-15M] scalp_exit={scalp.is_enabled()} target=+{scalp.target_cents} stop=-{scalp.stop_cents}")
    tg("[XRP-15M] Engine started!")

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

        # Record XRP price
        xrp = get_xrp_price()
        if xrp:
            _state["xrp_prices"].append((int(time.time()), xrp))
            # Keep last window's prices only
            cutoff = window_ts
            _state["xrp_prices"] = [(t, p) for t, p in _state["xrp_prices"] if t >= cutoff]

        # Strategy A: Arb (first 14.75 min)
        if sec_rem > 15:
            check_arb(market)

        # Strategy B: Snipe / Maker Snipe
        if MAKER_ENABLED:
            maker_result = check_maker_snipe(market, sec_rem)
            # Activate scalp exit on fresh maker fill
            if maker_result is True and scalp.is_enabled() and not scalp.active:
                scalp.on_fill(
                    token_id=_state.get('maker_token_id', ''),
                    entry_price=_state.get('maker_price', 0),
                    shares=_state.get('maker_shares', 0),
                    direction=_state.get('maker_side', 'UP'),
                    market_label=_state.get('maker_market_ref', ''),
                )
        elif sec_rem <= SNIPE_WINDOW and sec_rem > 5:
            check_snipe(market, sec_rem)

        # Scalp exit monitoring
        if scalp.active:
            scalp.tick(market, sec_rem)

        # Sleep
        time.sleep(SCAN_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[XRP-15M] Stopped by user")
