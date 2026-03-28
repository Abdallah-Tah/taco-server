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
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from zoneinfo import ZoneInfo

from edge_features import build_feature_snapshot
from edge_model import score_edge
from runtime_paths import resolve_runtime_python

# ── Paths & creds ──────────────────────────────────────────────────────────────
ROOT_DIR = Path("/home/abdaltm86/.openclaw/workspace")
SCRIPT_DIR = ROOT_DIR / "scripts"
WORK_DIR = ROOT_DIR / "trading"
JOURNAL_DB = WORK_DIR / "journal.db"
CREDS_FILE = Path("/home/abdaltm86/.config/openclaw/secrets.env")
POSITIONS_F = WORK_DIR / ".poly_btc15m_positions.json"
STATE_F = WORK_DIR / ".poly_btc15m_state.json"
LOG_F = WORK_DIR / ".poly_btc15m.log"
REPORTS_DIR = WORK_DIR / "reports"
BTC_PLACE_YES_EVENTS_F = REPORTS_DIR / "btc_place_yes_events.jsonl"
BTC_PLACE_YES_SUMMARY_F = REPORTS_DIR / "btc_place_yes_summary.json"
SHADOW_EARLY_TRADES_F = REPORTS_DIR / "shadow_early_trades.jsonl"
SHADOW_EARLY_SUMMARY_F = REPORTS_DIR / "shadow_early_summary.json"
EARLY_LIVE_TRADES_F = REPORTS_DIR / "early_live_trades.jsonl"
EARLY_LIVE_SUMMARY_F = REPORTS_DIR / "early_live_summary.json"

# ── Load credentials ──────────────────────────────────────────────────────────
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


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", ENV.get("TELEGRAM_TOKEN", ""))
CHAT_ID = os.environ.get("CHAT_ID", ENV.get("CHAT_ID", ""))
POLY_WALLET = os.environ.get("POLY_WALLET", ENV.get("POLY_WALLET", ""))
VENV_PY = resolve_runtime_python()

DRY_RUN = os.environ.get("BTC15M_DRY_RUN", ENV.get("BTC15M_DRY_RUN", "true")).lower() != "false"

MAKER_ENABLED = os.environ.get(
    "BTC15M_MAKER_ENABLED", ENV.get("BTC15M_MAKER_ENABLED", "false")
).lower() == "true"
MAKER_DRY_RUN = os.environ.get(
    "BTC15M_MAKER_DRY_RUN", ENV.get("BTC15M_MAKER_DRY_RUN", "true")
).lower() != "false"
MAKER_START_SEC = _int("BTC15M_MAKER_START_SEC", 420)
MAKER_CANCEL_SEC = _int("BTC15M_MAKER_CANCEL_SEC", 10)
MAKER_OFFSET = _float("BTC15M_MAKER_OFFSET", 0.005)
MAKER_POLL_SEC = _int("BTC15M_MAKER_POLL_SEC", 10)
MAKER_MIN_PRICE = _float("BTC15M_MAKER_MIN_PRICE", 0.01)

BTC15M_GABAGOOL_ENABLED = os.environ.get(
    "BTC15M_GABAGOOL_ENABLED", ENV.get("BTC15M_GABAGOOL_ENABLED", "false")
).lower() == "true"
BTC15M_GABAGOOL_DRY_RUN = os.environ.get(
    "BTC15M_GABAGOOL_DRY_RUN", ENV.get("BTC15M_GABAGOOL_DRY_RUN", "true")
).lower() != "false"
GABAGOOL_MAX_LEG = _float("GABAGOOL_MAX_LEG", 0.48)
GABAGOOL_TARGET_COMBINED = _float("GABAGOOL_TARGET_COMBINED", 0.97)

# ── Config ────────────────────────────────────────────────────────────────────
WINDOW_SEC = _int("BTC15M_WINDOW_SEC", 900)  # 15 minutes
ARB_THRESHOLD = _float("BTC15M_ARB_THRESHOLD", 0.98)
ARB_SIZE = _float("BTC15M_ARB_SIZE", 10.00)

SNIPE_DELTA_MIN = _float("BTC15M_SNIPE_DELTA_MIN", 0.025)
SNIPE_MAX_PRICE = _float("BTC15M_SNIPE_MAX_PRICE", 0.90)

# Signal confirmation
SIGNAL_CONFIRM_COUNT = _int("BTC15M_SIGNAL_CONFIRM_COUNT", 2)
SIGNAL_CONFIRM_SEC = _int("BTC15M_SIGNAL_CONFIRM_SEC", 15)
SIGNAL_MAX_ENTRY_PRICE = _float("BTC15M_SIGNAL_MAX_ENTRY_PRICE", 0.92)

SNIPE_DEFAULT = _float("BTC15M_SNIPE_DEFAULT_SIZE", 5.00)
SNIPE_STRONG = _float("BTC15M_SNIPE_STRONG_SIZE", 7.50)
SNIPE_STRONG_D = _float("BTC15M_SNIPE_STRONG_DELTA", 0.10)  # percent
SNIPE_WINDOW = _int("BTC15M_SNIPE_WINDOW_SEC", 30)
POLL_SEC = _int("BTC15M_PRICE_POLL_SEC", 5)
SCAN_SEC = _int("BTC15M_SCAN_INTERVAL", 10)
MAX_DAILY_LOSS = _float("BTC15M_MAX_DAILY_LOSS", 15.00)
SERIES_ID = "10192"
SERIES_SLUG = "btc-up-or-down-15m"

# Early trend live execution (regime=trend, sec>300)
EARLY_LIVE_ENABLED = os.environ.get(
    "BTC15M_EARLY_LIVE_ENABLED", ENV.get("BTC15M_EARLY_LIVE_ENABLED", "false")
).lower() == "true"
EARLY_LIVE_DRY_RUN = os.environ.get(
    "BTC15M_EARLY_LIVE_DRY_RUN", ENV.get("BTC15M_EARLY_LIVE_DRY_RUN", "true")
).lower() != "false"
EARLY_LIVE_SIZE_PCT = _float("BTC15M_EARLY_LIVE_SIZE_PCT", 0.25)  # 25% of normal
EARLY_LIVE_DISABLED = False  # Runtime kill-switch if failures detected

# ── State ─────────────────────────────────────────────────────────────────────
_state = {
    "window_ts": 0,
    "window_open_btc": 0.0,
    "arb_done": False,
    "snipe_done": False,
    "btc_prices": [],
    "daily_pnl": 0.0,
    "daily_reset": "",
    "trades": [],
    "maker_order_id": "",
    "maker_token_id": "",
    "maker_side": "",
    "maker_price": 0.0,
    "maker_shares": 0.0,
    "maker_done": False,
    "maker_last_poll": 0,
    "maker_event_id": "",
    "maker_seen_fills": [],
    "gabagool_yes_low": None,
    "gabagool_yes_ts": 0,
    "gabagool_no_low": None,
    "gabagool_no_ts": 0,
    "gabagool_window_logged": False,
    "early_live_first_trade_notified": False,  # Track if first trade full lifecycle sent
    "early_live_first_order_id": None,  # Track first trade order_id for resolution notification
    "early_live_done": False,  # One early_live trade per window max
}
_positions = []
ET_TZ = ZoneInfo("America/New_York")

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
            cp = subprocess.run(
                [
                    "openclaw",
                    "message",
                    "send",
                    "--channel",
                    "telegram",
                    "--target",
                    str(CHAT_ID or "7520899464"),
                    "--message",
                    str(msg),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if cp.returncode != 0:
                log(f"[TG-ERR] rc={cp.returncode} stderr={cp.stderr.strip()[:300]}")
        except Exception as e:
            log(f"[TG-ERR] {e}")

    threading.Thread(target=_send, daemon=True).start()


def trigger_post_resolution_tasks():
    if DRY_RUN:
        return
    try:
        subprocess.Popen(
            [str(VENV_PY), str(SCRIPT_DIR / "polymarket_reconcile.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.Popen(
            [str(VENV_PY), str(SCRIPT_DIR / "polymarket_redeem.py")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
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
    _state.setdefault("maker_seen_fills", [])
    _state.setdefault("maker_event_id", "")


# ── BTC price from Coinbase ───────────────────────────────────────────────────
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
                "id": m["id"],
                "question": m["question"],
                "condition_id": m["conditionId"],
                "clob_token_id": json.loads(m.get("clobTokenIds", "[]")),
                "yes_price": float(outcome_prices[0]),
                "no_price": float(outcome_prices[1]),
                "combined": float(outcome_prices[0]) + float(outcome_prices[1]),
                "liquidity": float(m["liquidity"]),
                "volume": float(m["volume"]),
                "end_date": m["endDate"],
                "closed": m.get("closed", False),
            }
    except Exception as e:
        log(f"Gamma error: {e}")
    return None


# ── CLOB: get order book (currently unused, kept for future debugging) ───────
def get_clob_prices(condition_id):
    try:
        r = requests.get(
            f"https://clob.polymarket.com/orders?condition_id={condition_id}",
            timeout=10,
        )
        return r.json()
    except Exception as e:
        log(f"CLOB error: {e}")
    return {}


# ── Place order via polymarket_executor ───────────────────────────────────────
def place_order(side, shares, price, condition_id, token_id):
    """Use aggressive FOK buy orders for immediate 15m fills and verify them."""
    cmd = [
        str(VENV_PY),
        str(SCRIPT_DIR / "polymarket_executor.py"),
        "buy_fok" if side.upper() == "BUY" else "sell",
        token_id,
        str(shares),
        str(price),
    ]
    if DRY_RUN:
        log(f"[DRY] {'BUY' if side.upper() == 'BUY' else 'SELL'} {shares} @{price:.4f} token={token_id}")
        return {"success": True, "dry": True, "filled": True}

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    payload = {
        "success": result.returncode == 0,
        "output": result.stdout,
        "error": result.stderr,
        "filled": False,
    }
    try:
        marker = "__RESULT__"
        for line in result.stdout.splitlines():
            if line.startswith(marker):
                data = json.loads(line[len(marker):])
                fill = data.get("fill_check") or {}
                payload["filled"] = bool(fill.get("filled"))
                payload["filled_size"] = fill.get("filled_size", 0)
                payload["posted"] = data
                break
    except Exception:
        pass
    return payload


# ── Journal logging ───────────────────────────────────────────────────────────
def log_trade(engine, direction, size_usd, entry_price, pnl, exit_type, hold_sec, notes=""):
    import sqlite3

    try:
        timestamp_open = datetime.now(timezone.utc).isoformat()
        timestamp_close = timestamp_open if exit_type else None
        exit_price = entry_price if exit_type else 0.0
        position_size = (size_usd / entry_price) if entry_price else 0.0
        pnl_absolute = float(pnl or 0.0)
        pnl_percent = (pnl_absolute / size_usd * 100) if size_usd else 0.0
        hold_duration_seconds = int(hold_sec or 0)

        conn = sqlite3.connect(str(JOURNAL_DB))
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO trades (
                engine, timestamp_open, timestamp_close, asset,
                category, direction, entry_price, exit_price,
                position_size, position_size_usd, pnl_absolute, pnl_percent,
                exit_type, hold_duration_seconds, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                engine,
                timestamp_open,
                timestamp_close,
                "btc-15m",
                "btc-updown",
                direction,
                float(entry_price or 0.0),
                float(exit_price or 0.0),
                float(position_size),
                float(size_usd or 0.0),
                pnl_absolute,
                float(pnl_percent),
                exit_type or "open",
                hold_duration_seconds,
                "normal",
                str(notes or ""),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log(f"Journal error: {e}")


def _timestamp_et():
    return datetime.now(ET_TZ).isoformat()


def _normalize_side(side):
    if side in ("UP", "YES"):
        return "YES"
    if side in ("DOWN", "NO"):
        return "NO"
    return None


def _valid_edge_value(net_edge):
    if net_edge is None:
        return None
    try:
        v = float(net_edge)
    except Exception:
        return None
    if abs(v) >= 1.0:
        return None
    return v


def build_edge_event_payload(
    market,
    signal_type,
    decision,
    side=None,
    seconds_remaining=None,
    skip_reason=None,
    intended_entry_price=None,
    actual_fill_price=None,
    slippage=None,
    market_slug=None,
    now_ts=None,
    execution_status=None,
):
    if now_ts is None:
        now_ts = int(time.time())

    price_points = [(int(t), float(p)) for t, p in _state.get("btc_prices", []) if p is not None]
    snapshot = build_feature_snapshot(
        price_points=price_points,
        now_ts=now_ts,
        best_bid=market.get("best_bid") if market else None,
        best_ask=market.get("best_ask") if market else None,
        depth_bids=market.get("bids") if market else None,
        depth_asks=market.get("asks") if market else None,
        seconds_remaining=seconds_remaining,
    )
    shadow = score_edge(snapshot, asset="BTC", engine="btc15m")

    raw_net_edge = shadow.get("net_edge")
    clean_net_edge = _valid_edge_value(raw_net_edge)
    edge_valid = clean_net_edge is not None
    effective_skip_reason = skip_reason if skip_reason is not None else shadow.get("shadow_skip_reason")
    if not edge_valid and effective_skip_reason is None:
        effective_skip_reason = "edge_not_computed"

    market_price_at_decision = None
    if market:
        normalized_side = _normalize_side(side)
        if normalized_side == "YES":
            market_price_at_decision = market.get("yes_price")
        elif normalized_side == "NO":
            market_price_at_decision = market.get("no_price")

    return {
        "event_id": str(uuid.uuid4()),
        "engine": "btc15m",
        "asset": "BTC",
        "timestamp_et": _timestamp_et(),
        "market_slug": market_slug,
        "market_id": market.get("id") if market else None,
        "side": _normalize_side(side),
        "signal_type": signal_type,
        "seconds_remaining": seconds_remaining,
        "best_bid": snapshot.get("best_bid"),
        "best_ask": snapshot.get("best_ask"),
        "spread": snapshot.get("spread"),
        "midprice": snapshot.get("midprice"),
        "microprice": snapshot.get("microprice"),
        "price_now": snapshot.get("price_now"),
        "price_1s_ago": snapshot.get("price_1s_ago"),
        "price_3s_ago": snapshot.get("price_3s_ago"),
        "price_5s_ago": snapshot.get("price_5s_ago"),
        "price_10s_ago": snapshot.get("price_10s_ago"),
        "price_30s_ago": snapshot.get("price_30s_ago"),
        "ret_1s": snapshot.get("ret_1s"),
        "ret_3s": snapshot.get("ret_3s"),
        "ret_5s": snapshot.get("ret_5s"),
        "ret_10s": snapshot.get("ret_10s"),
        "ret_30s": snapshot.get("ret_30s"),
        "vol_10s": snapshot.get("vol_10s"),
        "vol_30s": snapshot.get("vol_30s"),
        "vol_60s": snapshot.get("vol_60s"),
        "imbalance_1": snapshot.get("imbalance_1"),
        "imbalance_3": snapshot.get("imbalance_3"),
        "model_p_yes": shadow.get("model_p_yes"),
        "model_p_no": shadow.get("model_p_no"),
        "edge_yes": shadow.get("edge_yes"),
        "edge_no": shadow.get("edge_no"),
        "net_edge": clean_net_edge,
        "confidence": shadow.get("confidence"),
        "regime": shadow.get("regime"),
        "regime_ok": shadow.get("regime_ok"),
        "adaptive_net_edge_floor": shadow.get("adaptive_net_edge_floor"),
        "adaptive_confidence_floor": shadow.get("adaptive_confidence_floor"),
        "edge_valid": edge_valid,
        "shadow_skip_reason": shadow.get("shadow_skip_reason"),
        "skip_reason": effective_skip_reason,
        "market_price_at_decision": market_price_at_decision,
        "intended_entry_price": intended_entry_price,
        "actual_fill_price": actual_fill_price,
        "slippage": slippage,
        "shadow_decision": shadow.get("shadow_decision"),
        "execution_status": execution_status,
        "decision": decision,
    }


def log_edge_decision(**kwargs):
    requested_live_decision = kwargs.get("decision")
    try:
        from journal import log_edge_event
    except Exception:
        return None

    try:
        payload = build_edge_event_payload(**kwargs)
        log_edge_event(**payload)
        log(
            "[BTC-GATE] ts={timestamp} net_edge={net_edge} conf={confidence} "
            "regime={regime} edge_floor={edge_floor} conf_floor={conf_floor} "
            "shadow={shadow} live={live} execution_status={execution_status} skip_reason={skip_reason}".format(
                timestamp=payload.get("timestamp_et"),
                net_edge=payload.get("net_edge"),
                confidence=payload.get("confidence"),
                regime=payload.get("regime"),
                edge_floor=payload.get("adaptive_net_edge_floor"),
                conf_floor=payload.get("adaptive_confidence_floor"),
                shadow=payload.get("shadow_decision"),
                live=requested_live_decision,
                execution_status=payload.get("execution_status"),
                skip_reason=payload.get("skip_reason"),
            )
        )
        return payload
    except Exception as e:
        log(f"Edge telemetry error: {e}")
        return None


def should_execute_btc_trade(payload, requested_live_decision):
    return requested_live_decision == "place_yes" and payload.get("shadow_decision") == "place_yes"


def _bucket(ep):
    if ep is None:
        return "unknown"
    if ep < 0.30:
        return "<0.30"
    if ep < 0.50:
        return "0.30-0.50"
    if ep < 0.70:
        return "0.50-0.70"
    return "0.70+"


def _classify_place_yes_result(payload):
    execution_status = payload.get("execution_status")
    skip_reason = payload.get("skip_reason")
    if execution_status in ("filled", "executed"):
        return "filled"
    if execution_status in ("pending", "posted"):
        return "posted"
    if execution_status == "cancelled" or skip_reason == "maker_cancelled_by_deadline":
        return "cancelled"
    if skip_reason == "below_min_price":
        return "below_min_price"
    if skip_reason == "above_max_entry_price":
        return "above_max_entry_price"
    if execution_status == "failed":
        return "other_failure"
    return "blocked"


def _compute_bucket_stats(events):
    from collections import defaultdict
    buckets = defaultdict(list)
    for e in events:
        buckets[_bucket(e.get("entry_price"))].append(e)

    def make_bucket(bucket_name, evts):
        n = len(evts)
        if n == 0:
            return None
        net_edges = [e.get("net_edge") for e in evts if e.get("net_edge") is not None]
        avg_net_edge = sum(net_edges) / len(net_edges) if net_edges else None
        posted = sum(1 for e in evts if e.get("execution_status") in ("posted",))
        filled = sum(1 for e in evts if e.get("execution_status") in ("filled", "executed"))
        cancelled = sum(1 for e in evts if e.get("execution_status") == "cancelled")
        below_min = sum(1 for e in evts if e.get("execution_status") == "below_min_price")
        above_max = sum(1 for e in evts if e.get("execution_status") == "above_max_entry_price")
        blocked = sum(1 for e in evts if e.get("execution_status") == "blocked")
        other_fail = sum(1 for e in evts if e.get("execution_status") in ("failed", "other_failure"))
        # blocked_reason breakdown
        blocked_reasons = defaultdict(int)
        for e in evts:
            if e.get("execution_status") == "blocked":
                br = e.get("blocked_reason") or "unknown"
                blocked_reasons[br] += 1
        return {
            "count": n,
            "avg_net_edge": avg_net_edge,
            "status_breakdown": {
                "posted": posted,
                "filled": filled,
                "cancelled": cancelled,
                "below_min_price": below_min,
                "above_max_entry_price": above_max,
                "blocked": blocked,
                "other_failure": other_fail,
            },
            "blocked_reason_breakdown": dict(blocked_reasons),
            "execution_rate": posted / n if n else None,
            "fill_rate": filled / posted if posted else None,
        }

    report = {}
    for bucket_name in ["<0.30", "0.30-0.50", "0.50-0.70", "0.70+", "unknown"]:
        bv = make_bucket(bucket_name, buckets.get(bucket_name, []))
        if bv:
            report[bucket_name] = bv
    return report


def _load_edge_event_payload(event_id):
    if not event_id:
        return None
    try:
        import sqlite3
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM edge_events WHERE id = ?", (event_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _write_place_yes_analytics(payload):
    payload = dict(payload or {})
    event_id = payload.get("id") or payload.get("event_id")
    if payload.get("shadow_decision") != "place_yes":
        hydrated = _load_edge_event_payload(event_id)
        if hydrated and hydrated.get("shadow_decision") == "place_yes":
            merged_payload = dict(hydrated)
            merged_payload.update({k: v for k, v in payload.items() if v is not None})
            payload = merged_payload
        else:
            return
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        events = []
        if BTC_PLACE_YES_EVENTS_F.exists():
            for line in BTC_PLACE_YES_EVENTS_F.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        entry_price = payload.get("intended_entry_price")
        net_edge = payload.get("net_edge")
        estimated_probability = None
        expected_value = None
        if entry_price is not None and net_edge is not None:
            estimated_probability = float(entry_price) + float(net_edge)
            expected_value = estimated_probability - float(entry_price)

        market_price = payload.get("market_price_at_decision")
        side = payload.get("side")
        execution_status = _classify_place_yes_result(payload)
        # Capture actual DB skip_reason as blocked_reason for non-executed events
        if execution_status in ("blocked", "other_failure", None):
            blocked_reason = payload.get("skip_reason")
        else:
            blocked_reason = None

        record = {
            "event_id": event_id,
            "timestamp": payload.get("timestamp_et"),
            "entry_price": entry_price,
            "net_edge": net_edge,
            "estimated_probability": estimated_probability,
            "expected_value": expected_value,
            "confidence": payload.get("confidence"),
            "regime": payload.get("regime"),
            "yes_price": market_price if side == "YES" else payload.get("yes_price"),
            "no_price": market_price if side == "NO" else payload.get("no_price"),
            "seconds_remaining": payload.get("seconds_remaining"),
            "market_slug": payload.get("market_slug"),
            "execution_status": execution_status,
            "blocked_reason": blocked_reason,
            "outcome": payload.get("outcome"),
            "pnl_absolute": payload.get("pnl_absolute"),
            "pnl_percent": payload.get("pnl_percent"),
            "order_id": payload.get("order_id"),
        }

        replaced = False
        for i, existing in enumerate(events):
            if existing.get("event_id") and existing.get("event_id") == record["event_id"]:
                merged = dict(existing)
                merged.update({k: v for k, v in record.items() if v is not None})
                events[i] = merged
                replaced = True
                break
        if not replaced:
            events.append(record)

        with BTC_PLACE_YES_EVENTS_F.open("w") as f:
            for row in events:
                f.write(json.dumps(row) + "\n")

        valid_ev = [e for e in events if e.get("expected_value") is not None]
        executed = [e for e in events if e.get("outcome") in ("win", "loss") and e.get("pnl_absolute") is not None]
        avg_ev = sum(float(e.get("expected_value") or 0.0) for e in valid_ev) / len(valid_ev) if valid_ev else 0.0
        avg_realized = sum(float(e.get("pnl_absolute") or 0.0) for e in executed) / len(executed) if executed else None
        summary = {
            "total_place_yes_count": len(events),
            "average_expected_value": avg_ev,
            "average_realized_pnl": avg_realized,
            "ev_vs_realized_comparison": {
                "average_expected_value": avg_ev,
                "average_realized_pnl": avg_realized,
                "realized_minus_expected": (avg_realized - avg_ev) if avg_realized is not None else None,
            },
            "price_bucket_breakdown": _compute_bucket_stats(events),
        }
        BTC_PLACE_YES_SUMMARY_F.write_text(json.dumps(summary, indent=2))
    except Exception as e:
        log(f"[BTC-ANALYTICS] {e}")


def _write_shadow_early_trade(payload, resolved_outcome=None):
    """Write or update a shadow early trade record (regime=trend, sec_remaining > 300).
    
    resolved_outcome: 'win', 'loss', or None (if market not yet resolved)
    When resolved_outcome is set, also computes PnL based on entry_price.
    """
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        events = []
        if SHADOW_EARLY_TRADES_F.exists():
            for line in SHADOW_EARLY_TRADES_F.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        entry_price = payload.get("market_price") or payload.get("intended_entry_price")
        net_edge = payload.get("net_edge")
        market_slug = payload.get("market_slug")
        regime = payload.get("regime")
        confidence = payload.get("confidence")
        seconds_remaining = payload.get("seconds_remaining")
        timestamp_et = payload.get("timestamp_et")

        # Build record
        record = {
            "timestamp": timestamp_et,
            "market_slug": market_slug,
            "regime": regime,
            "confidence": confidence,
            "seconds_remaining": seconds_remaining,
            "net_edge": net_edge,
            "entry_price": entry_price,
            "market_price_at_decision": payload.get("market_price"),
            "shadow_decision": payload.get("shadow_decision"),
            "live_decision": payload.get("decision"),
            "live_skip_reason": payload.get("skip_reason"),  # why real trade didn't happen
            "resolved": resolved_outcome is not None,
            "outcome": resolved_outcome,
        }

        # Compute PnL if resolved
        if resolved_outcome in ("win", "loss") and entry_price is not None:
            # Assume $1 notional: win pays (1/entry_price - 1), loss pays -(1 - entry_price)
            if resolved_outcome == "win":
                # paid entry_price to buy YES, received $1 on resolve
                pnl = 1.0 - float(entry_price)
            else:
                pnl = -float(entry_price)
            record["pnl_absolute"] = round(pnl, 4)
            record["pnl_percent"] = round(pnl / 1.0 * 100, 2) if entry_price else 0.0
        else:
            record["pnl_absolute"] = None
            record["pnl_percent"] = None

        # Merge if same market_slug already exists (don't double-count)
        replaced = False
        for i, existing in enumerate(events):
            if existing.get("market_slug") == market_slug and existing.get("timestamp") == timestamp_et:
                merged = dict(existing)
                merged.update({k: v for k, v in record.items() if v is not None})
                events[i] = merged
                replaced = True
                break
        if not replaced:
            events.append(record)

        with SHADOW_EARLY_TRADES_F.open("w") as f:
            for row in events:
                f.write(json.dumps(row) + "\n")

        # Build summary
        total = len(events)
        resolved = [e for e in events if e.get("resolved")]
        wins = [e for e in resolved if e.get("outcome") == "win"]
        losses = [e for e in resolved if e.get("outcome") == "loss"]
        pending = [e for e in events if not e.get("resolved")]
        win_rate = len(wins) / len(resolved) if resolved else None
        avg_net_edge = sum(e["net_edge"] for e in events if e.get("net_edge")) / max(1, len([e for e in events if e.get("net_edge")]))
        avg_pnl = sum(e["pnl_absolute"] for e in resolved if e.get("pnl_absolute") is not None) / max(1, len(resolved))
        avg_pnl_pct = sum(e["pnl_percent"] for e in resolved if e.get("pnl_percent") is not None) / max(1, len(resolved))

        summary = {
            "total_shadow_trades": total,
            "resolved": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "pending": len(pending),
            "win_rate": round(win_rate, 4) if win_rate else None,
            "win_rate_pct": f"{win_rate:.1%}" if win_rate else "N/A",
            "average_net_edge": round(avg_net_edge, 6) if avg_net_edge else None,
            "average_pnl": round(avg_pnl, 4) if avg_pnl else None,
            "average_pnl_percent": round(avg_pnl_pct, 2) if avg_pnl_pct else None,
            "ev_per_trade": round(avg_net_edge, 6) if avg_net_edge else None,  # net_edge ≈ expected value
            "live_skip_reasons": {
                sr: sum(1 for e in events if e.get("live_skip_reason") == sr)
                for sr in set(e.get("live_skip_reason") for e in events if e.get("live_skip_reason"))
            },
        }
        SHADOW_EARLY_SUMMARY_F.write_text(json.dumps(summary, indent=2))
        log(f"[BTC-SHADOW-EARLY] recorded slug={market_slug} sec={seconds_remaining} regime={regime} outcome={resolved_outcome}")
    except Exception as e:
        log(f"[BTC-SHADOW-ANALYTICS] {e}")


def _write_early_live_trade(payload, execution_result=None, resolved_outcome=None):
    """Write or update an early live trade record (regime=trend, sec_remaining > 300, LIVE EXECUTION).
    
    This is the LIVE execution path — records actual orders placed, not shadow.
    execution_result: dict from executor with order_id, filled, shares, etc.
    resolved_outcome: 'win', 'loss', or None (if market not yet resolved)
    """
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        events = []
        if EARLY_LIVE_TRADES_F.exists():
            for line in EARLY_LIVE_TRADES_F.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue

        entry_price = payload.get("market_price") or payload.get("intended_entry_price")
        net_edge = payload.get("net_edge")
        market_slug = payload.get("market_slug")
        regime = payload.get("regime")
        confidence = payload.get("confidence")
        seconds_remaining = payload.get("seconds_remaining")
        timestamp_et = payload.get("timestamp_et")
        yes_price = payload.get("yes_price")
        no_price = payload.get("no_price")
        
        # Execution results
        exec_result = execution_result or {}
        order_id = exec_result.get("order_id")
        filled = exec_result.get("filled", False)
        shares = exec_result.get("shares")
        size_usd = exec_result.get("size_usd")
        actual_fill_price = exec_result.get("actual_fill_price") or entry_price
        execution_outcome = "filled" if filled else ("cancelled" if exec_result.get("cancelled") else "failed")

        # Build record
        record = {
            "timestamp": timestamp_et,
            "market_slug": market_slug,
            "regime": regime,
            "confidence": round(confidence, 4) if confidence else None,
            "seconds_remaining": seconds_remaining,
            "net_edge": round(net_edge, 6) if net_edge else None,
            "yes_price": round(yes_price, 4) if yes_price else None,
            "no_price": round(no_price, 4) if no_price else None,
            "entry_price": round(entry_price, 4) if entry_price else None,
            "size_usd": round(size_usd, 2) if size_usd else None,
            "shares": shares,
            "order_id": order_id,
            "execution_outcome": execution_outcome,
            "resolved": resolved_outcome is not None,
            "outcome": resolved_outcome,
        }

        # Compute PnL if resolved
        if resolved_outcome in ("win", "loss") and actual_fill_price is not None and size_usd is not None:
            # PnL = size_usd * (1/entry_price - 1) for win, -size_usd for loss
            if resolved_outcome == "win":
                pnl = size_usd * (1.0 / float(actual_fill_price) - 1.0)
            else:
                pnl = -size_usd
            record["pnl_absolute"] = round(pnl, 4)
            record["pnl_percent"] = round(pnl / size_usd * 100, 2) if size_usd else 0.0
        else:
            record["pnl_absolute"] = None
            record["pnl_percent"] = None

        # Merge if same market_slug + timestamp already exists
        replaced = False
        for i, existing in enumerate(events):
            if existing.get("market_slug") == market_slug and existing.get("timestamp") == timestamp_et:
                merged = dict(existing)
                merged.update({k: v for k, v in record.items() if v is not None})
                events[i] = merged
                replaced = True
                break
        if not replaced:
            events.append(record)

        with EARLY_LIVE_TRADES_F.open("w") as f:
            for row in events:
                f.write(json.dumps(row) + "\n")

        # FIRST TRADE FULL LIFECYCLE NOTIFICATION
        # Send complete lifecycle for the very first early_live trade only
        if (not _state.get("early_live_first_trade_notified") and 
            order_id and 
            execution_outcome == "filled" and
            regime == "trend"):
            
            _state["early_live_first_trade_notified"] = True
            _state["early_live_first_order_id"] = order_id
            
            # Build full lifecycle message
            lifecycle_msg = (
                f"🌮 [EARLY_LIVE] FIRST TRADE — FULL LIFECYCLE\n\n"
                f"• timestamp: {timestamp_et}\n"
                f"• sec_remaining: {seconds_remaining}\n"
                f"• regime: {regime}\n"
                f"• net_edge: {round(net_edge, 6) if net_edge else 'N/A'}\n"
                f"• confidence: {round(confidence, 4) if confidence else 'N/A'}\n"
                f"• yes_price: {round(yes_price, 4) if yes_price else 'N/A'}\n"
                f"• no_price: {round(no_price, 4) if no_price else 'N/A'}\n"
                f"• entry_price: {round(entry_price, 4) if entry_price else 'N/A'}\n"
                f"• size_usd: ${round(size_usd, 2) if size_usd else 'N/A'}\n"
                f"• shares: {shares:.2f}\n"
                f"• order_id: {order_id}\n"
                f"• status: posted → filled\n"
                f"• final_outcome: PENDING (will update on resolution)\n\n"
                f"PATH: early_live (NOT normal BTC-MAKER)"
            )
            tg(lifecycle_msg)
            log(f"[BTC-EARLY-LIVE] First trade full lifecycle notification sent for order_id={order_id}")

        # Build summary
        total = len(events)
        resolved = [e for e in events if e.get("resolved")]
        wins = [e for e in resolved if e.get("outcome") == "win"]
        losses = [e for e in resolved if e.get("outcome") == "loss"]
        pending = [e for e in events if not e.get("resolved")]
        filled_count = sum(1 for e in events if e.get("execution_outcome") == "filled")
        win_rate = len(wins) / len(resolved) if resolved else None
        avg_net_edge = sum(e["net_edge"] for e in events if e.get("net_edge")) / max(1, len([e for e in events if e.get("net_edge")]))
        avg_pnl = sum(e["pnl_absolute"] for e in resolved if e.get("pnl_absolute") is not None) / max(1, len(resolved))
        avg_pnl_pct = sum(e["pnl_percent"] for e in resolved if e.get("pnl_percent") is not None) / max(1, len(resolved))

        summary = {
            "total_early_live_trades": total,
            "filled": filled_count,
            "resolved": len(resolved),
            "wins": len(wins),
            "losses": len(losses),
            "pending": len(pending),
            "win_rate": round(win_rate, 4) if win_rate else None,
            "win_rate_pct": f"{win_rate:.1%}" if win_rate else "N/A",
            "average_net_edge": round(avg_net_edge, 6) if avg_net_edge else None,
            "average_pnl": round(avg_pnl, 4) if avg_pnl else None,
            "average_pnl_percent": round(avg_pnl_pct, 2) if avg_pnl_pct else None,
            "ev_per_trade": round(avg_net_edge, 6) if avg_net_edge else None,
        }
        EARLY_LIVE_SUMMARY_F.write_text(json.dumps(summary, indent=2))
        log(f"[BTC-EARLY-LIVE] recorded slug={market_slug} sec={seconds_remaining} regime={regime} outcome={resolved_outcome} pnl={record['pnl_absolute']}")
    except Exception as e:
        log(f"[BTC-EARLY-LIVE-ANALYTICS] {e}")


def _resolve_early_live_trades():
    """Resolve pending early live trades against market resolutions."""
    try:
        if not EARLY_LIVE_TRADES_F.exists():
            return
        import sqlite3
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT DISTINCT market_slug FROM edge_events WHERE engine='btc15m' AND regime='trend' AND seconds_remaining > 300").fetchall()
        conn.close()
        slug_set = {r["market_slug"] for r in rows}
        if not slug_set:
            return
        # Load resolutions
        res_file = Path("/tmp/market_resolutions.json")
        if not res_file.exists():
            return
        resolutions = json.loads(res_file.read_text())
        updated = False
        events = []
        for line in EARLY_LIVE_TRADES_F.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
        
        # Resolve each event
        for e in events:
            slug = e.get("market_slug")
            if not slug or slug not in resolutions:
                continue
            if e.get("resolved"):
                continue
            res = resolutions[slug]
            if not res.get("closed"):
                continue
            yes_r = res.get("yes_resolved")
            if yes_r is None:
                continue
            # Determine outcome
            outcome = "win" if yes_r == "1" else "loss"
            e["resolved"] = True
            e["outcome"] = outcome
            # Recompute PnL
            entry_price = e.get("entry_price")
            size_usd = e.get("size_usd")
            if entry_price is not None and size_usd is not None:
                if outcome == "win":
                    pnl = size_usd * (1.0 / float(entry_price) - 1.0)
                else:
                    pnl = -size_usd
                e["pnl_absolute"] = round(pnl, 4)
                e["pnl_percent"] = round(pnl / size_usd * 100, 2) if size_usd else 0.0
            updated = True
        
        if updated:
            with EARLY_LIVE_TRADES_F.open("w") as f:
                for row in events:
                    f.write(json.dumps(row) + "\n")
            
            # FIRST TRADE FINAL OUTCOME NOTIFICATION
            # Check if the first tracked trade just resolved
            first_order_id = _state.get("early_live_first_order_id")
            if first_order_id:
                for e in events:
                    if e.get("order_id") == first_order_id and e.get("resolved") and e.get("outcome"):
                        outcome = e.get("outcome")
                        pnl_abs = e.get("pnl_absolute")
                        pnl_pct = e.get("pnl_percent")
                        
                        final_msg = (
                            f"🌮 [EARLY_LIVE] FIRST TRADE — FINAL OUTCOME\n\n"
                            f"• order_id: {first_order_id}\n"
                            f"• final_outcome: {outcome.upper()}\n"
                            f"• pnl_absolute: ${pnl_abs:.4f}\n"
                            f"• pnl_percent: {pnl_pct:+.2f}%\n\n"
                            f"PATH: early_live (NOT normal BTC-MAKER)\n\n"
                            f"✅ Full lifecycle complete!"
                        )
                        tg(final_msg)
                        log(f"[BTC-EARLY-LIVE] First trade final outcome notification sent: {outcome} pnl={pnl_abs}")
                        break
            
            # Regenerate summary
            _write_early_live_trade({"market_slug": "refresh"}, resolved_outcome=None)
            log(f"[BTC-EARLY-LIVE-RESOLVE] resolved {sum(1 for e in events if e.get('resolved'))}/{len(events)} early live trades")
    except Exception as e:
        log(f"[BTC-EARLY-LIVE-RESOLVE] error: {e}")


def _resolve_shadow_early_trades():
    """Resolve pending shadow early trades against market resolutions."""
    try:
        if not SHADOW_EARLY_TRADES_F.exists():
            return
        import sqlite3
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT DISTINCT market_slug FROM edge_events WHERE engine='btc15m' AND regime='trend' AND seconds_remaining > 300").fetchall()
        conn.close()
        slug_set = {r["market_slug"] for r in rows}
        if not slug_set:
            return
        # Load resolutions
        res_file = Path("/tmp/market_resolutions.json")
        if not res_file.exists():
            return
        resolutions = json.loads(res_file.read_text())
        updated = False
        events = []
        for line in SHADOW_EARLY_TRADES_F.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
        for i, e in enumerate(events):
            if e.get("resolved"):
                continue
            slug = e.get("market_slug")
            m = resolutions.get(slug, {})
            yr = m.get("yes_resolved")
            if yr is not None:
                outcome = "win" if yr == 1.0 else "loss"
                events[i]["resolved"] = True
                events[i]["outcome"] = outcome
                entry_price = e.get("entry_price")
                if entry_price:
                    events[i]["pnl_absolute"] = round(1.0 - float(entry_price), 4) if outcome == "win" else round(-float(entry_price), 4)
                    events[i]["pnl_percent"] = round(events[i]["pnl_absolute"] / 1.0 * 100, 2)
                updated = True
        if updated:
            with SHADOW_EARLY_TRADES_F.open("w") as f:
                for row in events:
                    f.write(json.dumps(row) + "\n")
            log(f"[BTC-SHADOW-RESOLVE] resolved {sum(1 for e in events if e.get('resolved'))}/{len(events)} shadow early trades")
    except Exception as e:
        log(f"[BTC-SHADOW-RESOLVE] error: {e}")


def log_btc_trade_decision(
    market,
    signal_type,
    requested_live_decision,
    side=None,
    seconds_remaining=None,
    skip_reason=None,
    intended_entry_price=None,
    actual_fill_price=None,
    slippage=None,
    market_slug=None,
    now_ts=None,
    execution_status=None,
):
    base_payload = build_edge_event_payload(
        market=market,
        signal_type=signal_type,
        decision=requested_live_decision,
        side=side,
        seconds_remaining=seconds_remaining,
        skip_reason=skip_reason,
        intended_entry_price=intended_entry_price,
        actual_fill_price=actual_fill_price,
        slippage=slippage,
        market_slug=market_slug,
        now_ts=now_ts,
        execution_status=execution_status,
    )
    payload = dict(base_payload)

    if execution_status is None:
        if signal_type == "arb":
            if requested_live_decision.startswith("place_"):
                payload["execution_status"] = "pending"
            else:
                payload["execution_status"] = "skipped"
        elif requested_live_decision.startswith("place_"):
            can_execute = should_execute_btc_trade(base_payload, requested_live_decision)
            payload["execution_status"] = "pending" if can_execute else "skipped"
            if not can_execute:
                payload["decision"] = "skip_shadow_gate"
                payload["skip_reason"] = f"shadow_gate_{base_payload.get('shadow_decision') or 'unknown'}"
        else:
            payload["execution_status"] = "skipped"

    try:
        from journal import log_edge_event

        payload["id"] = log_edge_event(**payload)
        log(
            "[BTC-GATE] ts={timestamp} net_edge={net_edge} conf={confidence} "
            "regime={regime} edge_floor={edge_floor} conf_floor={conf_floor} "
            "shadow={shadow} live={live} execution_status={execution_status} skip_reason={skip_reason}".format(
                timestamp=payload.get("timestamp_et"),
                net_edge=payload.get("net_edge"),
                confidence=payload.get("confidence"),
                regime=payload.get("regime"),
                edge_floor=payload.get("adaptive_net_edge_floor"),
                conf_floor=payload.get("adaptive_confidence_floor"),
                shadow=payload.get("shadow_decision"),
                live=requested_live_decision,
                execution_status=payload.get("execution_status"),
                skip_reason=payload.get("skip_reason"),
            )
        )
        _write_place_yes_analytics(payload)
        # Shadow early trades: regime=trend AND sec_remaining > 300 AND shadow=place_yes
        regime = payload.get("regime")
        sec_rem = payload.get("seconds_remaining")
        shadow = payload.get("shadow_decision")
        if regime == "trend" and sec_rem is not None and sec_rem > 300 and shadow == "place_yes":
            _write_shadow_early_trade(payload)
    except Exception as e:
        log(f"Edge telemetry error: {e}")
    return payload


def finalize_btc_trade_decision(payload, result=None, *, fill_required=False, success_status="executed"):
    payload = dict(payload or {})
    event_id = payload.get("id")
    result = result or {}

    error_lines = [line.strip() for line in (result.get("error") or "").splitlines() if line.strip()]
    skip_reason = error_lines[0] if error_lines else None
    success = bool(result.get("success")) and (not fill_required or DRY_RUN or bool(result.get("filled")))
    execution_status = success_status if success else "failed"
    if not success and not skip_reason:
        skip_reason = "execution_failed"

    payload["execution_status"] = execution_status
    payload["skip_reason"] = None if success else skip_reason

    try:
        from journal import update_edge_event_status

        if event_id:
            update_edge_event_status(
                event_id,
                execution_status=execution_status,
                skip_reason=payload.get("skip_reason"),
            )
        log(
            "[BTC-EXEC] ts={timestamp} shadow={shadow} live={live} execution_status={execution_status} skip_reason={skip_reason}".format(
                timestamp=payload.get("timestamp_et"),
                shadow=payload.get("shadow_decision"),
                live=payload.get("decision"),
                execution_status=payload.get("execution_status"),
                skip_reason=payload.get("skip_reason"),
            )
        )
        _write_place_yes_analytics(payload)
    except Exception as e:
        log(f"Edge telemetry finalize error: {e}")
    return payload


def update_btc_trade_decision_status(event_id, execution_status, skip_reason=None):
    if not event_id:
        return
    try:
        from journal import update_edge_event_status

        update_edge_event_status(
            event_id,
            execution_status=execution_status,
            skip_reason=skip_reason,
        )
        _write_place_yes_analytics({
            "id": event_id,
            "shadow_decision": "place_yes",
            "execution_status": execution_status,
            "skip_reason": skip_reason,
        })
        log(f"[BTC-EXEC] event_id={event_id} execution_status={execution_status} skip_reason={skip_reason}")
    except Exception as e:
        log(f"Edge telemetry status update error: {e}")


def maker_place_order(side, shares, price, condition_id, token_id):
    cmd = [str(VENV_PY), str(SCRIPT_DIR / "polymarket_executor.py"), "maker_buy", token_id, str(shares), str(price)]
    if MAKER_DRY_RUN:
        log(f"[BTC-MAKER-DRY] {side} {shares} @{price:.4f} token={token_id}")
        return {"success": True, "dry": True, "order_id": f"dry-{int(time.time())}"}

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    payload = {"success": result.returncode == 0, "output": result.stdout, "error": result.stderr}
    for line in result.stdout.splitlines():
        if line.startswith("__RESULT__"):
            payload["posted"] = json.loads(line[len("__RESULT__"):])
            payload["order_id"] = (
                payload["posted"].get("order_id")
                or payload["posted"].get("orderID")
                or payload["posted"].get("id")
            )
            break
    return payload


def maker_order_status(order_id):
    cmd = [str(VENV_PY), str(SCRIPT_DIR / "polymarket_executor.py"), "order_status", str(order_id)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    return {"status": "error", "error": result.stderr or result.stdout or "no_status"}


def maker_verify_fill(token_id, min_size):
    cmd = [str(VENV_PY), str(SCRIPT_DIR / "polymarket_executor.py"), "verify_fill", str(token_id), str(min_size)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    return {"filled": False, "reason": result.stderr or result.stdout or "no_verify"}


def maker_cancel_order(order_id):
    cmd = [str(VENV_PY), str(SCRIPT_DIR / "polymarket_executor.py"), "cancel", str(order_id)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return {"success": result.returncode == 0, "output": result.stdout, "error": result.stderr}


def check_early_trend_live(market, seconds_remaining, market_slug=None):
    """Early trend live execution path (regime=trend, sec_remaining > 300).
    
    This is an ADDITIVE path — does NOT replace the maker window logic.
    Executes at 25% of normal size for highest-quality early signals.
    
    Returns:
        True if early live trade executed
        False if evaluated but skipped
        None if conditions not met (continue to normal maker path)
    """
    global EARLY_LIVE_DISABLED
    
    if not EARLY_LIVE_ENABLED or EARLY_LIVE_DISABLED:
        return None
    if seconds_remaining <= 300:
        return None  # Not an early signal — let normal maker path handle it
    
    # One early_live trade per window max
    if _state.get("early_live_done", False):
        log(f"[BTC-EARLY-LIVE] early_live_done=True for this window — skipping")
        return False
    
    # Build feature snapshot and score
    try:
        now_ts = int(time.time())
        price_points = [(int(t), float(p)) for t, p in _state.get("btc_prices", []) if p is not None]
        features = build_feature_snapshot(
            price_points=price_points,
            now_ts=now_ts,
            best_bid=market.get("best_bid"),
            best_ask=market.get("best_ask"),
            depth_bids=market.get("bids"),
            depth_asks=market.get("asks"),
            seconds_remaining=seconds_remaining,
        )
        result = score_edge(features, asset="BTC", engine="btc15m")
    except Exception as e:
        log(f"[BTC-EARLY-LIVE] feature/score error: {e}")
        return False
    
    regime = result.get("regime")
    shadow_decision = result.get("shadow_decision")
    net_edge = result.get("net_edge")
    confidence = result.get("confidence")
    yes_price = market.get("yes_price")
    no_price = market.get("no_price")
    
    # Check early live conditions
    if regime != "trend" or shadow_decision != "place_yes":
        return False  # Evaluated but doesn't meet criteria
    
    # EARLY_LIVE CONFIDENCE OVERRIDE (trend regime only)
    # Lower confidence floor from 0.20 to 0.18 for early_live path
    EARLY_LIVE_CONF_FLOOR = 0.18
    if confidence is not None and confidence < EARLY_LIVE_CONF_FLOOR:
        log(f"[BTC-EARLY-LIVE] confidence {confidence:.4f} < {EARLY_LIVE_CONF_FLOOR} — skipping")
        return False
    
    # Safety checks — same as maker path
    token_price = yes_price
    if token_price is None or token_price < 0.01:
        log(f"[BTC-EARLY-LIVE] invalid price {token_price}, skipping")
        return False
    
    if token_price > SIGNAL_MAX_ENTRY_PRICE:
        log(f"[BTC-EARLY-LIVE] price {token_price:.4f} > max {SIGNAL_MAX_ENTRY_PRICE}, skipping")
        return False
    
    # Calculate size (25% of normal snipe size)
    # Polymarket API precision: maker=2 decimals, taker=4 decimals
    # Round shares to 4 decimals (taker precision), size_usd to 2 decimals
    base_size = SNIPE_DEFAULT if net_edge >= 0.02 else SNIPE_DEFAULT * 0.5
    size_usd = round(base_size * EARLY_LIVE_SIZE_PCT, 2)
    shares = round(size_usd / token_price, 4)  # 4 decimals for taker amount
    
    log(f"[BTC-EARLY-LIVE] regime={regime} sec={seconds_remaining} edge={net_edge:.4f} conf={confidence:.4f} price={token_price:.4f} size=${size_usd:.2f} shares={shares:.4f} dry={EARLY_LIVE_DRY_RUN}")
    
    # Gate decision
    gate_payload = log_btc_trade_decision(
        market=market,
        signal_type="early_trend",
        requested_live_decision="place_yes",
        side="UP",
        seconds_remaining=seconds_remaining,
        intended_entry_price=token_price,
        market_slug=market_slug,
    )
    
    if gate_payload.get("execution_status") != "pending":
        log(f"[BTC-EARLY-LIVE] shadow gate blocked — shadow={gate_payload.get('shadow_decision')}")
        _state["early_live_done"] = True  # Count as attempt
        save_state()
        return False
    
    # Execute order
    token_id = market["clob_token_id"][0] if len(market["clob_token_id"]) > 0 else ""
    
    if EARLY_LIVE_DRY_RUN:
        r = {"success": True, "filled": True, "order_id": f"early_dry_{int(time.time())}", "shares": shares, "size_usd": size_usd, "actual_fill_price": token_price}
        log(f"[BTC-EARLY-LIVE] DRY RUN — would buy {shares:.4f} shares @ {token_price:.4f}")
    else:
        r = place_order("BUY", shares, token_price, market["condition_id"], token_id)
    
    # Mark window as done after execution attempt (regardless of outcome)
    _state["early_live_done"] = True
    save_state()
    
    # Finalize
    gate_payload = finalize_btc_trade_decision(gate_payload, r, fill_required=True, success_status="executed")
    
    if not r.get("success"):
        log(f"[BTC-EARLY-LIVE] execution FAILED: {r.get('error')}")
        # Record failure
        gate_payload["yes_price"] = yes_price
        gate_payload["no_price"] = no_price
        _write_early_live_trade(gate_payload, execution_result=r, resolved_outcome=None)
        tg(f"[BTC-EARLY-LIVE] ❌ FAILED | {shares:.4f} shares @ {token_price:.4f} | error={r.get('error', 'unknown')[:100]}")
        # Safety: disable early path on failure, keep maker path running
        EARLY_LIVE_DISABLED = True
        log("[BTC-EARLY-LIVE] DISABLED due to execution failure — maker path still active")
        return False
    
    # Record to early live analytics
    gate_payload["yes_price"] = yes_price
    gate_payload["no_price"] = no_price
    _write_early_live_trade(gate_payload, execution_result=r, resolved_outcome=None)
    
    # Telegram notification — only announce FILLED if order_id present and success confirmed
    order_id = r.get("order_id")
    if order_id and r.get("filled"):
        tg(f"[BTC-EARLY-LIVE] ✅ FILLED: {shares:.4f} shares @ {token_price:.4f} (${size_usd:.2f}) | order={order_id} | sec={seconds_remaining}s | edge={net_edge:.4f}")
        log(f"[BTC-EARLY-LIVE] FILLED order_id={order_id} shares={shares:.4f} price={token_price:.4f} size=${size_usd:.2f}")
    elif order_id:
        tg(f"[BTC-EARLY-LIVE] 📋 POSTED (pending fill): {shares:.4f} shares @ {token_price:.4f} | order={order_id}")
        log(f"[BTC-EARLY-LIVE] POSTED order_id={order_id} shares={shares:.4f} price={token_price:.4f} size=${size_usd:.2f}")
    else:
        log(f"[BTC-EARLY-LIVE] SUCCESS but no order_id returned")
    
    return True


def check_maker_snipe(market, seconds_remaining, market_slug=None):
    if not MAKER_ENABLED:
        return None

    oid = _state.get("maker_order_id")
    maker_event_id = _state.get("maker_event_id")

    if oid and (time.time() - _state.get("maker_last_poll", 0) >= MAKER_POLL_SEC or seconds_remaining <= MAKER_CANCEL_SEC):
        _state["maker_last_poll"] = int(time.time())
        st = maker_order_status(oid)
        log(f"[BTC-MAKER] status order_id={oid} -> {st.get('status')}")

        if st.get("status") in ("filled", "partially_filled"):
            _state["maker_done"] = True
            _state["snipe_done"] = True
            update_btc_trade_decision_status(maker_event_id, "filled", None)
            save_state()
            tg(f"[BTC-MAKER] FILLED order {oid}")
            log_trade(
                "btc15m",
                _state.get("maker_side", "UP"),
                _state.get("maker_shares", 0) * _state.get("maker_price", 0),
                _state.get("maker_price", 0),
                0,
                "filled",
                int(time.time()) - _state.get("maker_last_poll", int(time.time())),
                notes=f"maker_fill oid={oid}",
            )
            return True

        if st.get("status") == "not_found":
            vf = maker_verify_fill(_state.get("maker_token_id"), _state.get("maker_shares", 0))
            if vf.get("filled"):
                fill_key = f"{oid}:{vf.get('filled_size')}"
                seen = set(_state.get("maker_seen_fills", []))
                if fill_key not in seen:
                    log(f"[BTC-MAKER] fill verified via activity/trades size={vf.get('filled_size')}")
                    tg(f"[BTC-MAKER] FILL VERIFIED {vf.get('filled_size')} shares")
                    log_trade(
                        "btc15m",
                        _state.get("maker_side", "UP"),
                        vf.get("filled_size", 0) * _state.get("maker_price", 0),
                        _state.get("maker_price", 0),
                        0,
                        "filled",
                        int(time.time()) - _state.get("maker_last_poll", int(time.time())),
                        notes=f"maker_verify oid={oid} size={vf.get('filled_size')}",
                    )
                    seen.add(fill_key)
                    _state["maker_seen_fills"] = list(seen)[-200:]

                _state["maker_done"] = True
                _state["snipe_done"] = True
                update_btc_trade_decision_status(maker_event_id, "filled", None)
                save_state()
                return True

        if seconds_remaining <= MAKER_CANCEL_SEC and st.get("status") in ("open", "partially_filled"):
            maker_cancel_order(oid)
            log(f"[BTC-MAKER] cancel by deadline order_id={oid} sec_rem={seconds_remaining}")
            tg(f"[BTC-MAKER] ORDER CANCELLED: {oid} | sec_rem={seconds_remaining}")
            _state["maker_done"] = True
            update_btc_trade_decision_status(maker_event_id, "cancelled", "maker_cancelled_by_deadline")
            save_state()
            return False

    if _state.get("maker_done") or oid:
        return None

    # Early trend live execution path (regime=trend, sec>300)
    early_result = check_early_trend_live(market, seconds_remaining, market_slug)
    if early_result is not None:
        return early_result

    if seconds_remaining > MAKER_START_SEC or seconds_remaining <= MAKER_CANCEL_SEC:
        log_btc_trade_decision(
            market=market,
            signal_type="maker_snipe",
            requested_live_decision="skip_time",
            seconds_remaining=seconds_remaining,
            skip_reason="outside_maker_window",
            market_slug=market_slug,
        )
        return None

    base_price = get_btc_price()
    if base_price is None:
        log_btc_trade_decision(
            market=market,
            signal_type="maker_snipe",
            requested_live_decision="skip_data",
            seconds_remaining=seconds_remaining,
            skip_reason="missing_btc_price",
            market_slug=market_slug,
        )
        return None

    if not _state.get("window_open_btc"):
        log_btc_trade_decision(
            market=market,
            signal_type="maker_snipe",
            requested_live_decision="skip_data",
            seconds_remaining=seconds_remaining,
            skip_reason="missing_window_open_btc",
            market_slug=market_slug,
        )
        return None

    delta_pct = (base_price - _state["window_open_btc"]) / _state["window_open_btc"] * 100
    log(f"[BTC-MAKER] now={base_price} open={_state['window_open_btc']} delta={delta_pct:+.3f}% sec_rem={seconds_remaining}")

    preview_payload = build_edge_event_payload(
        market=market,
        signal_type="maker_snipe",
        decision="preview",
        seconds_remaining=seconds_remaining,
        market_slug=market_slug,
        now_ts=int(time.time()),
    )
    shadow_decision = preview_payload.get("shadow_decision")
    if shadow_decision == "place_yes":
        direction = "UP"
    elif shadow_decision == "place_no":
        direction = "DOWN"
    else:
        log_btc_trade_decision(
            market=market,
            signal_type="maker_snipe",
            requested_live_decision="skip_no_edge",
            seconds_remaining=seconds_remaining,
            market_slug=market_slug,
        )
        return None

    log(f"[BTC-MAKER] model-approved direction={direction} shadow={shadow_decision} net_edge={preview_payload.get('net_edge')} conf={preview_payload.get('confidence')}")

    token_id = market["clob_token_id"][0] if direction == "UP" else (market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else "")
    token_price = market["yes_price"] if direction == "UP" else market["no_price"]

    if token_price < MAKER_MIN_PRICE:
        log(f"[BTC-MAKER] token_mid={token_price:.4f} < min {MAKER_MIN_PRICE:.4f}, skipping")
        log_btc_trade_decision(
            market=market,
            signal_type="maker_snipe",
            requested_live_decision="skip_spread",
            side=direction,
            seconds_remaining=seconds_remaining,
            skip_reason="below_min_price",
            intended_entry_price=token_price,
            market_slug=market_slug,
        )
        return None

    if token_price > SIGNAL_MAX_ENTRY_PRICE:
        log(f"[BTC-MAKER] entry={token_price:.4f} > max {SIGNAL_MAX_ENTRY_PRICE:.4f}, skipping {direction} signal")
        log_btc_trade_decision(
            market=market,
            signal_type="maker_snipe",
            requested_live_decision="skip_spread",
            side=direction,
            seconds_remaining=seconds_remaining,
            skip_reason="above_max_entry_price",
            intended_entry_price=token_price,
            market_slug=market_slug,
        )
        return None

    limit_price = max(0.01, round(token_price - MAKER_OFFSET, 2))
    shares = max(5.0, math.floor((SNIPE_DEFAULT / max(limit_price, 0.01)) * 100) / 100)

    gate_payload = log_btc_trade_decision(
        market=market,
        signal_type="maker_snipe",
        requested_live_decision="place_yes" if direction == "UP" else "place_no",
        side=direction,
        seconds_remaining=seconds_remaining,
        intended_entry_price=limit_price,
        market_slug=market_slug,
    )
    if gate_payload.get("execution_status") != "pending":
        log(
            f"[BTC-MAKER] shadow gate blocked {direction} signal "
            f"shadow={gate_payload.get('shadow_decision')} "
            f"net_edge={gate_payload.get('net_edge')} conf={gate_payload.get('confidence')}"
        )
        return False

    log(f"[BTC-MAKER] {direction} signal token_mid={token_price:.4f} limit={limit_price:.4f} shares={shares:.2f} dry={MAKER_DRY_RUN}")
    r = maker_place_order("BUY", shares, limit_price, market["condition_id"], token_id)
    gate_payload["order_id"] = r.get("order_id")

    for line in (r.get("output") or "").splitlines():
        if line.strip() and ("[MAKER" in line or "[RESULT" in line or "[ATTEMPT" in line):
            log(f"[EXEC] {line.strip()}")
    if r.get("error"):
        for line in r["error"].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")

    gate_payload = finalize_btc_trade_decision(
        gate_payload,
        r,
        fill_required=False,
        success_status="posted",
    )
    if gate_payload.get("execution_status") != "posted":
        log(f"[BTC-MAKER] order placement failed for {direction} signal")
        return False

    _state["maker_order_id"] = str(r.get("order_id") or (r.get("posted") or {}).get("order_id") or "")
    _state["maker_token_id"] = token_id
    _state["maker_side"] = direction
    _state["maker_price"] = limit_price
    _state["maker_shares"] = shares
    _state["maker_last_poll"] = int(time.time())
    _state["maker_done"] = False
    _state["maker_event_id"] = gate_payload.get("id") or ""
    save_state()

    tg(
        f"[BTC-MAKER] ORDER PLACED: {direction} {shares:.2f} shares @ {limit_price:.4f} | "
        f"Order: {_state['maker_order_id']}"
    )
    return r.get("success")


# ── Strategy A: Gabagool ──────────────────────────────────────────────────────
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


# ── Strategy B: Arb check ─────────────────────────────────────────────────────
def check_arb(market, seconds_remaining=None, market_slug=None):
    if _state["arb_done"]:
        return None

    combined = market["yes_price"] + market["no_price"]
    if combined >= ARB_THRESHOLD:
        log(f"[ARB] No arb. Combined={combined:.4f} >= {ARB_THRESHOLD}")
        return None

    log(f"[ARB] FOUND! YES={market['yes_price']:.4f} NO={market['no_price']:.4f} COMBINED={combined:.4f}")

    yes_tid = market["clob_token_id"][0] if len(market["clob_token_id"]) > 0 else ""
    no_tid = market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else ""

    yes_shares = ARB_SIZE / market["yes_price"]
    no_shares = ARB_SIZE / market["no_price"]

    gate_yes = log_btc_trade_decision(
        market=market,
        signal_type="arb",
        requested_live_decision="place_yes",
        side="YES",
        seconds_remaining=seconds_remaining,
        intended_entry_price=market["yes_price"],
        market_slug=market_slug,
    )
    gate_no = log_btc_trade_decision(
        market=market,
        signal_type="arb",
        requested_live_decision="place_no",
        side="NO",
        seconds_remaining=seconds_remaining,
        intended_entry_price=market["no_price"],
        market_slug=market_slug,
    )

    r1 = place_order("BUY", yes_shares, market["yes_price"], market["condition_id"], yes_tid)
    gate_yes = finalize_btc_trade_decision(gate_yes, r1, fill_required=True, success_status="executed")

    r2 = place_order("BUY", no_shares, market["no_price"], market["condition_id"], no_tid)
    gate_no = finalize_btc_trade_decision(gate_no, r2, fill_required=True, success_status="executed")

    arb_profit = (1.00 - combined) * min(yes_shares, no_shares)
    notes = f"arb profit=${arb_profit:.2f} yes_shares={yes_shares:.2f} no_shares={no_shares:.2f}"
    log(f"[ARB] Result: yes={r1.get('success')}, no={r2.get('success')}. {notes}")
    tg(f"[BTC-ARB] {notes}")

    _state["arb_done"] = True
    save_state()
    return True


# ── Strategy C: Late snipe ────────────────────────────────────────────────────
def check_snipe(market, seconds_remaining, market_slug=None):
    if _state["snipe_done"]:
        return None
    if seconds_remaining > SNIPE_WINDOW or seconds_remaining < 5:
        return None

    btc_price = get_btc_price()
    if btc_price is None:
        log_btc_trade_decision(
            market=market,
            signal_type="snipe",
            requested_live_decision="skip_data",
            seconds_remaining=seconds_remaining,
            skip_reason="missing_btc_price",
            market_slug=market_slug,
        )
        return None

    if not _state["window_open_btc"]:
        log(f"[SNIPE] No window_open_btc recorded, skipping. BTC={btc_price}")
        log_btc_trade_decision(
            market=market,
            signal_type="snipe",
            requested_live_decision="skip_data",
            seconds_remaining=seconds_remaining,
            skip_reason="missing_window_open_btc",
            market_slug=market_slug,
        )
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
        log_btc_trade_decision(
            market=market,
            signal_type="snipe",
            requested_live_decision="skip_no_edge",
            seconds_remaining=seconds_remaining,
            skip_reason="delta_below_threshold",
            market_slug=market_slug,
        )
        return None

    token_id = market["clob_token_id"][0] if direction == "UP" else (market["clob_token_id"][1] if len(market["clob_token_id"]) > 1 else "")
    price = market["yes_price"] if direction == "UP" else market["no_price"]

    if price > SNIPE_MAX_PRICE:
        log(f"[SNIPE] Price {price:.4f} > max {SNIPE_MAX_PRICE}, skipping")
        log_btc_trade_decision(
            market=market,
            signal_type="snipe",
            requested_live_decision="skip_spread",
            side=direction,
            seconds_remaining=seconds_remaining,
            skip_reason="above_max_entry_price",
            intended_entry_price=price,
            market_slug=market_slug,
        )
        return None

    size = SNIPE_STRONG if abs(delta_pct) >= SNIPE_STRONG_D * 100 else SNIPE_DEFAULT
    shares = size / price
    if shares < 5:
        shares = 5
        size = shares * price

    gate_payload = log_btc_trade_decision(
        market=market,
        signal_type="snipe",
        requested_live_decision="place_yes" if direction == "UP" else "place_no",
        side=direction,
        seconds_remaining=seconds_remaining,
        intended_entry_price=price,
        market_slug=market_slug,
    )
    if gate_payload.get("execution_status") != "pending":
        log(
            f"[SNIPE] shadow gate blocked {direction} signal "
            f"shadow={gate_payload.get('shadow_decision')} "
            f"net_edge={gate_payload.get('net_edge')} conf={gate_payload.get('confidence')}"
        )
        return False

    log(f"[SNIPE] {direction} signal! BTC delta={delta_pct:+.3f}% size=${size:.2f} price={price:.4f} shares={shares:.2f}")
    r = place_order("BUY", shares, price, market["condition_id"], token_id)
    gate_payload = finalize_btc_trade_decision(gate_payload, r, fill_required=True, success_status="executed")

    for line in (r.get("output") or "").splitlines():
        if any(tag in line for tag in ("[ATTEMPT]", "[RESULT]", "Book best", "FILL")):
            log(f"[EXEC] {line.strip()}")
    if r.get("error"):
        for line in r["error"].splitlines():
            if line.strip():
                log(f"[EXEC-ERR] {line.strip()}")

    notes = f"snipe {direction} delta={delta_pct:+.3f}% price={price:.4f} size=${size:.2f}"
    log(f"[SNIPE] Result: {r.get('success')} filled={r.get('filled')}. {notes}")

    if os.getenv("BTC15M_ONE_SHOT_TEST") == "1":
        _state["snipe_done"] = True
        save_state()
        log("[SNIPE] ONE_SHOT_TEST active — stopping after first BTC attempt in this window")

    if gate_payload.get("execution_status") != "executed":
        log("[SNIPE] Execution failed — not counting this trade as real")
        tg(f"[BTC-SNIPE] EXECUTION FAILED | {notes}")
        return False

    if not r.get("filled") and not DRY_RUN:
        log("[SNIPE] No fill confirmed — not counting this trade as real")
        tg(f"[BTC-SNIPE] NO FILL confirmed | {notes}")
        return False

    tg(f"[BTC-SNIPE] FILL CONFIRMED {r.get('filled_size', shares):.2f} shares | {notes}")

    _state["snipe_done"] = True
    _state["prev_snipe_side"] = direction
    _state["prev_snipe_price"] = price
    _state["prev_snipe_size"] = size
    save_state()
    return True


# ── New window setup ──────────────────────────────────────────────────────────
def setup_new_window(slug, window_ts):
    btc_price = get_btc_price()
    if btc_price is None:
        btc_price = _state.get("window_open_btc", 0.0)

    _state["window_ts"] = window_ts
    _state["window_open_btc"] = btc_price
    _state["arb_done"] = False
    _state["snipe_done"] = False
    _state["maker_order_id"] = ""
    _state["maker_token_id"] = ""
    _state["maker_side"] = ""
    _state["maker_price"] = 0.0
    _state["maker_shares"] = 0.0
    _state["maker_done"] = False
    _state["maker_last_poll"] = 0
    _state["maker_event_id"] = ""
    _state["btc_prices"] = [(int(time.time()), btc_price)]
    _state["prev_slug"] = slug
    _state["gabagool_yes_low"] = None
    _state["gabagool_yes_ts"] = 0
    _state["gabagool_no_low"] = None
    _state["gabagool_no_ts"] = 0
    _state["gabagool_window_logged"] = False
    _state["early_live_done"] = False
    save_state()

    trigger_post_resolution_tasks()
    log(f"[NEW WINDOW] ts={window_ts} BTC={btc_price} slug={slug}")


# ── Daily loss check ───────────────────────────────────────────────────────────
def check_daily_limit():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _state.get("daily_reset", "") != today:
        _state["daily_pnl"] = 0.0
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
    log(
        f"[BTC-15M] Arb threshold=${ARB_THRESHOLD}, "
        f"snipe delta>={SNIPE_DELTA_MIN}%, max daily loss=${MAX_DAILY_LOSS} | "
        f"maker={MAKER_ENABLED} dry={MAKER_DRY_RUN} start=T-{MAKER_START_SEC} "
        f"cancel=T-{MAKER_CANCEL_SEC} offset={MAKER_OFFSET}"
    )
    tg("[BTC-15M] Engine started!")

    while True:
        now = int(time.time())
        window_sec = now % WINDOW_SEC
        window_ts = now - window_sec
        window_end = window_ts + WINDOW_SEC
        sec_rem = window_end - now

        slug, _ = get_current_slug()

        if window_ts != _state["window_ts"]:
            log(f"[CYCLE] New window detected: {slug}")
            setup_new_window(slug, window_ts)

        if not check_daily_limit():
            time.sleep(60)
            continue

        market = get_market(slug)
        if not market:
            log(f"[CYCLE] No market found for {slug}, sleeping 10s...")
            time.sleep(10)
            continue

        log(f"[CYCLE] {market['question'][:60]} | YES={market['yes_price']:.3f} NO={market['no_price']:.3f} | {sec_rem}s left")

        btc = get_btc_price()
        if btc:
            _state["btc_prices"].append((int(time.time()), btc))
            cutoff = window_ts
            _state["btc_prices"] = [(t, p) for t, p in _state["btc_prices"] if t >= cutoff]

        if sec_rem > 15:
            check_arb(market, sec_rem, slug)

        check_gabagool(market, sec_rem)

        # Early trend live execution (checked first, before maker window)
        if EARLY_LIVE_ENABLED and not EARLY_LIVE_DISABLED and sec_rem > 300:
            check_early_trend_live(market, sec_rem, slug)

        if MAKER_ENABLED:
            check_maker_snipe(market, sec_rem, slug)
        elif sec_rem <= SNIPE_WINDOW and sec_rem > 5:
            check_snipe(market, sec_rem, slug)

        # Resolve pending shadow early trades every ~5 min
        _state["_shadow_resolve_counter"] = _state.get("_shadow_resolve_counter", 0) + 1
        if _state["_shadow_resolve_counter"] % 30 == 0:
            _resolve_shadow_early_trades()
            _resolve_early_live_trades()

        time.sleep(SCAN_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("[BTC-15M] Stopped by user")
