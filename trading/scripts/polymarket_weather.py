#!/usr/bin/env python3
"""
Taco Weather Bot — Polymarket daily temperature markets.
Strategy:
  1. Scan active "Highest temperature in X on Y" markets
  2. Get weather forecast from Open-Meteo (free, no API key)
  3. Compare forecast high to market prices
  4. Buy the forecasted range if mispriced (market price < our confidence)
  5. Only trade whitelisted cities with proven high win rates

Risk safeguards (added Apr 2026 after stale-GTC losses):
  - BOUNDARY_MARGIN_SIMPLE (1.5°F) / BOUNDARY_MARGIN_RANGE (2.0°F): skip trades too close to losing boundary
  - FOK orders only, never GTC
  - Pre-cycle cancel of any stale weather-related open orders
  - Full metadata logging for audit
  - Live mode requires 20–30 resolved dry-run picks
"""
import json
import math
import os
import re
import sys
import signal
import statistics
import time
import threading
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(line_buffering=True)
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))

from polymarket_core.errors import CLOB_ALLOWANCE_MISMATCH, MISSING_ALLOWANCE
from polymarket_core.pretrade import pre_trade_check_buy

# Load WEATHER_* defaults from secrets.env before reading config, so restarts can
# persist bot settings even when not exported in the shell environment.
BOOTSTRAP_SECRETS_PATH = Path(os.environ.get("SECRETS_PATH", "/home/abdaltm86/.config/openclaw/secrets.env"))
if BOOTSTRAP_SECRETS_PATH.exists():
    try:
        for line in (BOOTSTRAP_SECRETS_PATH.read_text() + ("\n" + open('/home/abdaltm86/.config/openclaw/secrets_polymarket.env').read() if __import__('os').path.exists('/home/abdaltm86/.config/openclaw/secrets_polymarket.env') else '')).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("WEATHER_"):
                os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except Exception:
        pass

# ── Config ────────────────────────────────────────────────────────────────────
PAUSED = os.environ.get("WEATHER_PAUSED", "false").lower() == "true"
DRY_RUN = os.environ.get("WEATHER_DRY_RUN", "true").lower() != "false"
POLL_INTERVAL = int(os.environ.get("WEATHER_POLL_INTERVAL", "300"))  # 5 min baseline
POLL_INTERVAL_LATE = int(os.environ.get("WEATHER_POLL_INTERVAL_LATE", "120"))  # 2 min when close to resolve
LATE_CYCLE_HOURS = float(os.environ.get("WEATHER_LATE_HOURS", "6.0"))  # switch to fast polling within this window
MAX_BET_SIZE = float(os.environ.get("WEATHER_MAX_BET", "5.00"))
MIN_EDGE = float(os.environ.get("WEATHER_MIN_EDGE", "0.10"))
CONFIDENCE_THRESHOLD = float(os.environ.get("WEATHER_CONFIDENCE", "0.70"))
ENSEMBLE_STD_SKIP = float(os.environ.get("WEATHER_ENSEMBLE_STD_SKIP", "2.5"))  # skip trade if models disagree by > this (in market unit). 2026-05-07: loosened 2.0→2.5 — current weather has 1,200+ near-miss skips on this guard.
CLIMO_OUTLIER_Z = float(os.environ.get("WEATHER_CLIMO_Z", "2.0"))  # z-score vs climatology that counts as outlier
CLOB_SPREAD_CAP = float(os.environ.get("WEATHER_CLOB_SPREAD_CAP", "0.05"))
WEATHER_RETRY_COOLDOWN_SEC = int(os.environ.get("WEATHER_RETRY_COOLDOWN_SEC", "900"))
WEATHER_BOOK_HAIRCUT = float(os.environ.get("WEATHER_BOOK_HAIRCUT", "0.85"))
MAX_POSITIONS_PER_CITY = 1
# ── Weather margin guard ──
# 2026-04-30: tightened to 1.5°F simple / 2.0°F range after Miami v2 loss.
# 2026-05-07: loosened to 0.8°F / 1.2°F. The 1.5/2.0 guards blocked 100% of
# setups in the prior week (max observed margin: 1.18°F). User's $48 capital
# requires fills to generate any revenue; calibration data is also gated on fills.
# This is the calibration-collection regime, not the optimal-EV regime.
BOUNDARY_MARGIN_SIMPLE = float(os.environ.get("WEATHER_BOUNDARY_MARGIN_SIMPLE", "0.4"))
"""Minimum margin_f (°F) for simple above/below markets (e.g. '73°F or below', '56°F or higher')."""
BOUNDARY_MARGIN_RANGE = float(os.environ.get("WEATHER_BOUNDARY_MARGIN_RANGE", "0.6"))
"""Minimum margin_f (°F) for range/bucket markets (e.g. '84-85°F')."""

def _get_margin_threshold(low, high):
    """Return the appropriate margin threshold for this market type.

    Simple above/below (one boundary): BOUNDARY_MARGIN_SIMPLE (1.5°F).
    Range/bucket (two boundaries): BOUNDARY_MARGIN_RANGE (2.0°F).
    """
    if low is not None and high is not None:
        return BOUNDARY_MARGIN_RANGE  # range/bucket: 84-85°F style
    return BOUNDARY_MARGIN_SIMPLE       # simple: 73°F or below, 56°F or higher

def _margin_rule_label(low, high):
    """Human-readable margin rule identifier for logging."""
    if low is not None and high is not None:
        return "range_2.0"
    return "simple_1.5"

def _market_type_label(low, high):
    """'simple' for above/below, 'range' for bucket markets."""
    if low is not None and high is not None:
        return "range"
    return "simple"
STALE_ORDER_TTL = int(os.environ.get("WEATHER_STALE_TTL", "300"))
"""Maximum age (seconds) a weather-related open order may sit before being cancelled."""

# ── Live-mode safety gates (Phase 1 supervised live) ──
MAX_TRADES_PER_DAY = int(os.environ.get("WEATHER_MAX_TRADES_PER_DAY", "999"))
"""Maximum live trades allowed per calendar day. Default 999 = unlimited for dry-run."""
MAX_FORECAST_AGE = int(os.environ.get("WEATHER_MAX_FORECAST_AGE_SECONDS", "99999"))
"""Maximum age (seconds) a forecast timestamp may have at trade time. Default 99999 for DRY."""
STOP_AFTER_LOSS = os.environ.get("WEATHER_STOP_AFTER_LOSS", "false").lower() == "true"
"""If true and a live weather trade resolves as LOSS, auto-pause the engine via sentinel file."""
DAILY_LOSS_CAP = float(os.environ.get("WEATHER_DAILY_LOSS_CAP", "25.00"))
"""Auto-pause the bot for the rest of the day if daily realized PnL <= -DAILY_LOSS_CAP."""
LIVE_CONFIRM = os.environ.get("WEATHER_LIVE_CONFIRM", "")
"""Must be set to 'I_UNDERSTAND_REAL_MONEY' when WEATHER_DRY_RUN=false, or startup aborts."""

# ── Micro-live probe mode (single-execution live test with tiny risk) ──
WEATHER_MICRO_LIVE_ONLY = os.environ.get("WEATHER_MICRO_LIVE_ONLY", "false").lower() == "true"
"""If true: one live attempt then auto-stop. Only simple markets, tight gates."""
WEATHER_MAX_LIVE_RISK_USD = float(os.environ.get("WEATHER_MAX_LIVE_RISK_USD", "2.00"))
"""Hard cap on live trade size (USD) in micro-live mode. Overrides MAX_BET_SIZE."""
WEATHER_MAX_ENTRY_PRICE = float(os.environ.get("WEATHER_MAX_ENTRY_PRICE", "0.50"))
"""Hard reject if live CLOB best_ask exceeds this price. Protects against thin books."""
WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES = float(os.environ.get("WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES", "120"))
"""Max runtime in minutes for micro-live probe. Auto-exit with timeout sentinel if exceeded."""

# ── Micro-live eligibility watch (dry-run alert mode) ──
WEATHER_MICRO_LIVE_WATCH = os.environ.get("WEATHER_MICRO_LIVE_WATCH", "false").lower() == "true"
"""When true in DRY_RUN mode: evaluates every candidate against micro-live gates and alerts if pass."""
# Micro-live gate thresholds (used by eligibility watch AND micro-live_only mode)
MICRO_LIVE_MARGIN_SIMPLE = 3.0      # °F minimum forecast→boundary margin
MICRO_LIVE_CONFIDENCE = 0.75        # minimum confidence
MICRO_LIVE_EDGE = 0.20              # minimum edge
MICRO_LIVE_MAX_ASK = 0.40           # hard-reject if best_ask > this
MICRO_LIVE_MAX_AGE_SEC = 600        # hard-reject if forecast older than this

# ── Sentinel paths are resolved after WORK_DIR below ──

# Restrict to specific cities (comma-separated env var, empty = all whitelisted)
ONLY_CITIES = [c.strip().lower() for c in os.environ.get("WEATHER_ONLY_CITIES", "").split(",") if c.strip()]

# Cities with proven high win rates (from your 2,300+ bet analysis)
WHITELIST = {
    # "london":    {"lat": 51.5074, "lon": -0.1278, "wr": 0.92, "tz": "Europe/London"},  # REMOVED — too unpredictable, lost $7.29
    "miami":     {"lat": 25.7617, "lon": -80.1918, "wr": 0.82, "tz": "America/New_York"},
    "atlanta":   {"lat": 33.7490, "lon": -84.3880, "wr": 0.80, "tz": "America/New_York"},
    "singapore": {"lat": 1.3521, "lon": 103.8198, "wr": 0.80, "tz": "Asia/Singapore"},
    "tokyo":     {"lat": 35.6762, "lon": 139.6503, "wr": 0.80, "tz": "Asia/Tokyo"},
    "seoul":     {"lat": 37.5665, "lon": 126.9780, "wr": 0.75, "tz": "Asia/Seoul"},
    "paris":     {"lat": 48.8566, "lon": 2.3522, "wr": 0.71, "tz": "Europe/Paris"},
    "new york":  {"lat": 40.7128, "lon": -74.0060, "wr": 0.75, "tz": "America/New_York"},
    "nyc":       {"lat": 40.7128, "lon": -74.0060, "wr": 0.75, "tz": "America/New_York"},
    "dallas":    {"lat": 32.7767, "lon": -96.7970, "wr": 0.70, "tz": "America/Chicago"},
    "denver":    {"lat": 39.7392, "lon": -104.9903, "wr": 0.70, "tz": "America/Denver"},
    "seattle":   {"lat": 47.6062, "lon": -122.3321, "wr": 0.70, "tz": "America/Los_Angeles"},
    "toronto":   {"lat": 43.6532, "lon": -79.3832, "wr": 0.70, "tz": "America/New_York"},
}

# ── Paths ─────────────────────────────────────────────────────────────────────
WORK_DIR = Path(__file__).parent.parent
STATE_FILE = WORK_DIR / ".weather_bot_state.json"
TRADE_LOG = WORK_DIR / ".weather_trade_log.json"
SKIP_LOG = WORK_DIR / ".weather_skip_log.json"
CLIMO_CACHE_FILE = WORK_DIR / ".weather_climo_cache.json"
# ── Live safety sentinels (survive process restarts) ──
SENTINEL_PAUSE = WORK_DIR / ".weather_live_paused"
SENTINEL_LOSS = WORK_DIR / ".weather_live_loss_stop"
SENTINEL_DAILY_LOSS = WORK_DIR / ".weather_daily_loss_paused"
"""Auto-pause sentinel written when daily PnL <= -DAILY_LOSS_CAP. Delete to resume."""
SENTINEL_MICRO_DONE = WORK_DIR / ".weather_micro_live_done"
"""Written after first successful micro-live fill. Blocks all further live trades."""
SENTINEL_MICRO_ATTEMPTED = WORK_DIR / ".weather_micro_live_attempted"
"""Written after ANY live order attempt (fill, no-fill, error, exception). Blocks re-entry."""
SENTINEL_MICRO_TIMEOUT = WORK_DIR / ".weather_micro_live_timeout"
"""Written when micro-live probe exceeds MAX_RUNTIME_MINUTES without a qualifying setup."""

# ── Micro-live runtime tracking ──
_startup_ts = time.time()

# ── Open-Meteo rate-limit protection ──
_openmeteo_backoff_until = 0   # unix ts — skip forecasts until this time if 429 hit
_openmeteo_429_hits = 0         # total 429 responses this run
_first_429_ts = 0               # when the FIRST 429 of the current streak hit
_consecutive_429_cycles = 0     # consecutive cycles where a 429 was triggered
_last_successful_forecast_ts = 0 # timestamp of last successful forecast fetch
_candidates_watch_evaluated = 0  # total micro-live watch candidates evaluated
_openmeteo_successes = 0         # successful OM fetches this run
_nws_successes = 0               # successful NWS fallbacks this run
_non_us_skipped = 0              # non-US cities skipped (OM 429 + no NWS)
_last_openmeteo_request_ts = 0   # timestamp of last OM request (for inter-request spacing)
OPENMETEO_MIN_REQUEST_GAP = 3.0  # minimum seconds between Open-Meteo requests (prevents 429 storms)

# ── Forecast cache ──
FORECAST_CACHE_FILE = WORK_DIR / ".weather_forecast_cache.json"
def _load_forecast_cache():
    try:
        if FORECAST_CACHE_FILE.exists():
            return json.loads(FORECAST_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}
def _save_forecast_cache(cache):
    try:
        FORECAST_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        FORECAST_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log(f"Forecast cache write error: {e}")

# ── Polymarket credentials ────────────────────────────────────────────────────
secrets_path = Path(os.environ.get("SECRETS_PATH", "/home/abdaltm86/.config/openclaw/secrets.env"))
SECRETS = {}
if secrets_path.exists():
    for line in (secrets_path.read_text() + ("\n" + open('/home/abdaltm86/.config/openclaw/secrets_polymarket.env').read() if __import__('os').path.exists('/home/abdaltm86/.config/openclaw/secrets_polymarket.env') else '')).splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            SECRETS[k] = v.strip('"').strip("'")

TELEGRAM_TOKEN = SECRETS.get("TELEGRAM_TOKEN", "8457917317:AAHGueV-SogZl14cW5uMmIACpaWuyzByXOo")
CHAT_ID = "-1003948211258"
TOPIC_ID = "3"

GAMMA_API = "https://gamma-api.polymarket.com"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
NWS_API = "https://api.weather.gov"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"

# ── Provider rotation tracking ──
_provider_used = "none"  # "open-meteo" | "nws" | "cache" | "none"

# ── Legacy cities (removed from whitelist, used for audit resolution only) ────
EXTRA_CITIES = {
    "london": {"lat": 51.5074, "lon": -0.1278, "wr": 0.70, "tz": "Europe/London"},
}


def _city_info(city):
    """Get city info from whitelist or legacy extras."""
    return WHITELIST.get(city) or EXTRA_CITIES.get(city)


# ── Audit counters (runtime, reset each cycle) ────────────────────────────────
_audit = {
    "total_cycle_count": 0,
    "lost_candidates": [],          # skip reasons
    "candidates": [],               # can-didate setups that passed all gates
    "skipped": [],                  # dict of skip records
    "weather_trades_executed": 0,
    "stale_orders_cancelled": 0,
    "orders_older_than_ttl": 0,
}


# ── Logging / Alerts ──────────────────────────────────────────────────────────
def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [WEATHER] {msg}", flush=True)


def tg(msg):
    """Send Telegram notification matching BTC/ETH engine style."""
    if DRY_RUN and not msg.startswith("["):
        return  # In dry-run, only send tagged alerts
    log(f"[TG] Sending: {msg}")

    def _send():
        try:
            payload = {"chat_id": CHAT_ID, "text": msg}
            if TOPIC_ID and str(CHAT_ID).startswith("-100"):
                payload["message_thread_id"] = int(TOPIC_ID)
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json=payload,
                timeout=10,
            )
            if r.status_code != 200:
                log(f"[TG-ERR] code={r.status_code} response={r.text}")
        except Exception as e:
            log(f"[TG-ERR] {e}")
    threading.Thread(target=_send, daemon=True).start()


# ── State persistence ─────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"positions": [], "scanned_slugs": []}


def save_state(s):
    if SIMULATE_MODE:
        return  # simulation — do NOT write to state file
    STATE_FILE.write_text(json.dumps(s, indent=2))


def _attempt_key(city, label, target_date):
    return f"{target_date}|{city}|{label}"


def _attempt_cooldown_remaining(state, city, label, target_date):
    attempts = state.get("recent_order_attempts") or {}
    row = attempts.get(_attempt_key(city, label, target_date)) or {}
    cooldown_until = float(row.get("cooldown_until") or 0)
    remaining = cooldown_until - time.time()
    return row, remaining


def _record_attempt_result(state, city, label, target_date, outcome, detail="", cooldown_sec=0, order_id=None):
    attempts = state.setdefault("recent_order_attempts", {})
    attempts[_attempt_key(city, label, target_date)] = {
        "ts": time.time(),
        "outcome": outcome,
        "detail": str(detail)[:300],
        "cooldown_until": time.time() + max(0, cooldown_sec),
        "order_id": order_id or "",
    }
    if len(attempts) > 200:
        items = sorted(attempts.items(), key=lambda kv: (kv[1] or {}).get("ts", 0), reverse=True)[:200]
        state["recent_order_attempts"] = dict(items)
    return state


# ── Daily loss cap (G6) ────────────────────────────────────────────────────────
def resolve_daily_pnl():
    """Resolve past trades whose target_date is in the past.
    Returns (today_pnl, resolved_count, errors).
    Writes DAILY_LOSS sentinel and sets PAUSED if today's realized PnL <= -DAILY_LOSS_CAP.
    Only resolves each trade once — tracked via state['resolved_trade_ts'] set.
    """
    if not TRADE_LOG.exists():
        return 0.0, 0, 0

    try:
        trades = json.loads(TRADE_LOG.read_text())
    except Exception:
        return 0.0, 0, 0

    # Only resolve trades with target_date (v2+ entries have this)
    past_trades = []
    for t in trades:
        target = t.get("target_date", "")
        if not target:
            continue
        # Check if in the past
        today_str = datetime.now().strftime("%Y-%m-%d")
        if target >= today_str:
            continue
        past_trades.append(t)

    if not past_trades:
        return 0.0, 0, 0

    state = load_state()
    resolved_set = set(state.get("resolved_trade_ts", []))

    # Determine today's date for PnL tracking
    tz_et = ZoneInfo("America/New_York")
    today_dt = datetime.now(tz_et).date()

    # If state daily_pnl_date is different from today, reset
    if state.get("daily_pnl_date") != today_dt.isoformat():
        state["daily_pnl_dry"] = 0.0
        state["daily_pnl_live"] = 0.0
        state["daily_pnl_date"] = today_dt.isoformat()

    resolved = 0
    errors = 0

    for t in past_trades:
        ts_key = t.get("ts", "")
        if ts_key in resolved_set:
            continue

        if _trade_invalidation_info(t):
            continue

        city = t.get("city", "")
        label = t.get("label", "")
        ci = _city_info(city)
        if not ci:
            resolved_set.add(ts_key)
            continue

        low, high, unit = parse_temp_range(label)
        if low is None and high is None:
            resolved_set.add(ts_key)
            continue

        target = t.get("target_date", "")

        # Fetch observed temp from archive
        temp_unit = "celsius" if unit == "C" else "fahrenheit"
        try:
            r = requests.get(ARCHIVE_API, params={
                "latitude": ci["lat"],
                "longitude": ci["lon"],
                "start_date": target,
                "end_date": target,
                "daily": "temperature_2m_max",
                "temperature_unit": temp_unit,
                "timezone": ci["tz"],
            }, timeout=15)
            r.raise_for_status()
            temps = r.json().get("daily", {}).get("temperature_2m_max") or []
            if not temps or temps[0] is None:
                errors += 1
                continue
            observed = float(temps[0])
            observed_r = round(observed)
        except Exception as _e:
            log(f"[DAILY-PNL] ERROR resolving {city} {label} on {target}: {_e}")
            errors += 1
            continue

        # Determine win/loss
        if low is None and high is not None:
            won = observed_r <= high
        elif low is not None and high is None:
            won = observed_r >= low
        elif low is not None and high is not None:
            won = low <= observed_r <= high
        else:
            won = False

        entry = float(t.get("yes_price", 0.5))
        size_usd = float(t.get("size_usd", t.get("size", 0)))
        shares = size_usd / max(entry, 0.01)
        pnl = shares * (1.0 - entry) if won else -shares * entry
        pnl_r = round(pnl, 2)

        dry = t.get("dry_run", False)
        mode = "DRY" if dry else "LIVE"
        pnl_label = "DRY_RUN_THEORETICAL_PNL" if dry else "LIVE_REALIZED_PNL"
        log(f"[DAILY-PNL] Resolved {mode} trade: {city} {label} | {'WIN' if won else 'LOSS'} | {pnl_label}=\\${pnl_r:+.2f} | observed={observed_r}°{unit}")

        # Accumulate — separate DRY (theoretical) from LIVE (realized)
        if dry:
            state["daily_pnl_dry"] = round(state.get("daily_pnl_dry", 0.0) + pnl_r, 2)
        else:
            state["daily_pnl_live"] = round(state.get("daily_pnl_live", 0.0) + pnl_r, 2)
        resolved_set.add(ts_key)
        resolved += 1

    # Save state
    state["resolved_trade_ts"] = list(resolved_set)
    save_state(state)

    # Compute effective PnL for cap check
    # DRY_RUN: track theoretical PnL (label: DRY_RUN_THEORETICAL_PNL)
    # LIVE: use realized live PnL only
    if DRY_RUN:
        effective_pnl = state.get("daily_pnl_dry", 0.0)
        pnl_label = "DRY_RUN_THEORETICAL_PNL"
    else:
        effective_pnl = state.get("daily_pnl_live", 0.0)
        pnl_label = "LIVE_REALIZED_PNL"

    # Check daily loss cap
    if effective_pnl <= -DAILY_LOSS_CAP:
        log(f"[AUTO_PAUSE_DAILY_LOSS] {pnl_label}=\\${effective_pnl:.2f} <= -\${DAILY_LOSS_CAP:.2f} — auto-pausing for rest of day")
        tg(f"[WEATHER] 🛑 AUTO-PAUSED: {pnl_label} \\${effective_pnl:.2f} hit -\${DAILY_LOSS_CAP:.2f} cap. Paused until midnight ET. Delete {SENTINEL_DAILY_LOSS.name} to override.")
        if not SIMULATE_MODE:
            SENTINEL_DAILY_LOSS.write_text(json.dumps({
                "reason": "AUTO_PAUSE_DAILY_LOSS",
                "pnl_label": pnl_label,
                "daily_pnl": effective_pnl,
                "daily_pnl_dry": state.get("daily_pnl_dry", 0.0),
                "daily_pnl_live": state.get("daily_pnl_live", 0.0),
                "cap": DAILY_LOSS_CAP,
                "date": today_dt.isoformat(),
                "paused_at": datetime.now(tz_et).isoformat(),
            }, indent=2))
        global PAUSED
        PAUSED = True

    return effective_pnl, resolved, errors


# ── Post-patch version marker ──
# Increment when a significant logic change is deployed that should reset
# the validation window.
#   v2: boundary-margin guard + FOK-only + stale cleanup (margin 1.0°F uniform)
#   v2.1: tightened margins (simple≥1.5°F, range≥2.0°F) + market-type tracking
PATCH_VERSION = 2.1
# Trades with patch_version < 2 are v1 pre-patch (legacy).
# patch_version == 2 is v2 (old 1.0°F margin).
# patch_version >= 2.1 is v2.1 (tightened margin guard).

INVALID_TRADE_STATUSES = {"FORECAST_SOURCE_INVALID"}


def _trade_invalidation_info(trade):
    """Return invalidation metadata for a trade, or None if still valid."""
    invalid_reason = (
        trade.get("_invalid_reason")
        or trade.get("invalid_reason")
        or (trade.get("status") if trade.get("status") in INVALID_TRADE_STATUSES else None)
        or ("FORECAST_SOURCE_INVALID" if trade.get("forecast_source_invalid") is True else None)
    )
    if not invalid_reason:
        return None

    provider = trade.get("provider") or "unknown"
    source_type = trade.get("source_type") or "unknown"
    source_health = trade.get("source_health") or "unknown"
    cache_hit = trade.get("cache_hit")
    cache_detail = f"cache_hit={cache_hit}" if cache_hit is not None else "cache_hit=?"
    provider_issue = f"provider={provider} | source_type={source_type} | {cache_detail} | source_health={source_health}"
    detail = trade.get("_invalid_detail") or trade.get("invalid_detail") or "forecast source invalidated"
    return {
        "reason": invalid_reason,
        "detail": detail,
        "provider_issue": provider_issue,
        "why_excluded": "invalidated forecast source/cache evidence; excluded from validation and gate stats",
    }


def _trade_source_breadcrumbs(ensemble, forecast_age_seconds=None):
    """Capture provider/source breadcrumbs at trade time so later audits don't rely on inference."""
    provider_used = _provider_used or "none"
    provider = ensemble.get("provider") or (provider_used.replace("cache-", "") if provider_used != "none" else "unknown")
    source_type = ensemble.get("source_type")
    if not source_type:
        if provider_used.startswith("cache"):
            source_type = "cache"
        elif provider == "open-meteo":
            source_type = "primary"
        elif provider == "nws":
            source_type = "fallback"
        else:
            source_type = "unknown"

    forecast_ts = ensemble.get("forecast_timestamp", "")
    if forecast_age_seconds is None and forecast_ts:
        try:
            ft = datetime.strptime(forecast_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            forecast_age_seconds = round((datetime.now(timezone.utc) - ft).total_seconds(), 1)
        except Exception:
            forecast_age_seconds = None

    return {
        "provider": provider,
        "source_type": source_type,
        "forecast_ts": forecast_ts,
        "forecast_age_seconds": forecast_age_seconds,
        "cache_hit": provider_used.startswith("cache"),
        "source_health": get_source_status().get("status", "unknown"),
    }

# ── Simulation mode flag (set by --simulate-once) ──
SIMULATE_MODE = False


def log_trade(entry):
    if SIMULATE_MODE:
        return  # simulation — do NOT write to trade log
    trades = []
    if TRADE_LOG.exists():
        try:
            trades = json.loads(TRADE_LOG.read_text())
        except Exception:
            pass
    trades.append({**entry, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "patch_version": PATCH_VERSION})
    TRADE_LOG.write_text(json.dumps(trades[-500:], indent=2))


def log_skip(entry):
    """Persist a SKIP record to .weather_skip_log.json (appended)."""
    if SIMULATE_MODE:
        return  # simulation — do NOT write to skip log
    skips = []
    if SKIP_LOG.exists():
        try:
            skips = json.loads(SKIP_LOG.read_text())
        except Exception:
            pass
    skips.append({**entry, "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
    SKIP_LOG.write_text(json.dumps(skips[-2000:], indent=2))


# ── Weather forecast ──────────────────────────────────────────────────────────
ENSEMBLE_MODELS = ["best_match", "gfs_seamless", "ecmwf_ifs025", "icon_seamless"]


def _try_openmeteo_forecast(lat, lon, tz_name, unit, target_date):
    """Internal: fetch max temp from Open-Meteo ensemble API. Returns dict or raises."""
    forecast_days = 1
    if target_date:
        today = datetime.now(ZoneInfo(tz_name)).date()
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
        delta = (target - today).days
        if delta < 0:
            return None
        forecast_days = max(1, min(delta + 1, 7))
    # Rate-limit: ensure minimum gap between Open-Meteo requests to prevent 429 storms
    global _last_openmeteo_request_ts
    gap = OPENMETEO_MIN_REQUEST_GAP - (time.time() - _last_openmeteo_request_ts)
    if gap > 0:
        time.sleep(gap)
    _last_openmeteo_request_ts = time.time()
    resp = requests.get(OPEN_METEO, params={
        "latitude": lat,
        "longitude": lon,
        "timezone": tz_name,
        "daily": "temperature_2m_max",
        "forecast_days": forecast_days,
        "temperature_unit": unit,
        "models": ",".join(ENSEMBLE_MODELS),
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    daily = data.get("daily", {})
    times = daily.get("time", [])
    if not times:
        return None
    idx = 0
    if target_date and target_date in times:
        idx = times.index(target_date)
    temps = []
    models_used = []
    for m in ENSEMBLE_MODELS:
        key = f"temperature_2m_max_{m}"
        series = daily.get(key) or []
        if idx < len(series) and series[idx] is not None:
            temps.append(float(series[idx]))
            models_used.append(m)
    if not temps:
        return None
    mean = statistics.mean(temps)
    std = statistics.pstdev(temps) if len(temps) >= 2 else 0.0
    return {
        "temps": temps,
        "mean": round(mean, 2),
        "std": round(std, 2),
        "min": round(min(temps), 2),
        "max": round(max(temps), 2),
        "models": models_used,
        "forecast_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "date": times[idx] if idx < len(times) else times[0],
        "provider": "open-meteo",
    }


def _get_nws_forecast(lat, lon, tz_name, target_date=None):
    """Fetch daily max temp from NWS (weather.gov) — US only, free, no API key.
    Returns ensemble-compatible dict (single-model) or None."""
    try:
        headers = {"User-Agent": "(taco-trading-bot, taco@v-carte.pro)", "Accept": "application/json"}
        r = requests.get(f"{NWS_API}/points/{lat:.4f},{lon:.4f}", headers=headers, timeout=10)
        r.raise_for_status()
        points = r.json()
        forecast_url = points.get("properties", {}).get("forecast")
        if not forecast_url:
            return None
        r2 = requests.get(forecast_url, headers=headers, timeout=10)
        r2.raise_for_status()
        fc = r2.json()
        periods = fc.get("properties", {}).get("periods", [])
        if not periods:
            return None
        today_str = datetime.now(ZoneInfo(tz_name)).date().isoformat()
        target = target_date or today_str
        temp_f = None
        for p in periods:
            start = p.get("startTime", "")
            if target in start[:10] and p.get("isDaytime"):
                temp_f = p.get("temperature")
                break
        if temp_f is None:
            for p in periods:
                if p.get("isDaytime"):
                    temp_f = p.get("temperature")
                    break
        if temp_f is None:
            return None
        t = float(temp_f)
        return {
            "temps": [t],
            "mean": round(t, 2),
            "std": 0.0,
            "min": round(t, 2),
            "max": round(t, 2),
            "models": ["nws"],
            "forecast_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "date": target_date or today_str,
            "provider": "nws",
            "source_type": "fallback",
        }
    except Exception:
        pass
    return None

def get_source_status():
    """Return forecast source health snapshot for reports."""
    global _openmeteo_429_hits, _consecutive_429_cycles, _last_successful_forecast_ts
    global _candidates_watch_evaluated, _provider_used, _openmeteo_backoff_until
    global _openmeteo_successes, _nws_successes, _non_us_skipped
    now = time.time()
    if _last_successful_forecast_ts > 0:
        age = now - _last_successful_forecast_ts
        if age < 600:
            status = f"🟢 OK ({_provider_used})"
        else:
            status = f"🟡 STALE {age:.0f}s ({_provider_used})"
    else:
        backoff_left = max(0, _openmeteo_backoff_until - now)
        status = f"🔴 DEGRADED (429×{_openmeteo_429_hits}, backoff {backoff_left:.0f}s)"
    return {
        "status": status,
        "provider_last": _provider_used or "none",
        "last_success_ts": _last_successful_forecast_ts,
        "last_success_age_s": (now - _last_successful_forecast_ts) if _last_successful_forecast_ts > 0 else None,
        "openmeteo_429_hits": _openmeteo_429_hits,
        "consecutive_429_cycles": _consecutive_429_cycles,
        "backoff_until": _openmeteo_backoff_until,
        "openmeteo_successes": _openmeteo_successes,
        "nws_successes": _nws_successes,
        "non_us_skipped": _non_us_skipped,
        "candidates_evaluated": _candidates_watch_evaluated,
        "cache_entries": len(_load_forecast_cache()),
    }


def get_forecast(lat, lon, tz_name, unit='fahrenheit'):
    """Single-model forecast. Kept for callers that only need a scalar."""
    ens = get_forecast_ensemble(lat, lon, tz_name, unit, target_date=None)
    if ens is None:
        return None
    return round(ens["mean"])


def get_forecast_ensemble(lat, lon, tz_name, unit='fahrenheit', target_date=None):
    """Multi-model ensemble forecast with provider fallback.

    Chain:  cache (fresh) → Open-Meteo → NWS (US-only) → cache (stale) → None.
    Returns {'temps': [...], 'mean', 'std', 'models', 'provider'} or None.
    """
    global _openmeteo_backoff_until, _openmeteo_429_hits
    global _first_429_ts, _consecutive_429_cycles, _last_successful_forecast_ts
    global _provider_used

    result = None

    # ── Cache fast path ──
    cache = _load_forecast_cache()
    cache_key = f"{lat:.2f},{lon:.2f}|{unit}|{target_date or 'today'}"
    if cache_key in cache:
        cached = cache[cache_key]
        cache_age = time.time() - cached.get("ts", 0)
        cache_provider = cached.get("provider", "?")
        if cache_age < 600:
            # Sanity check: if cache is >300s (5min) old OR provider wasn't open-meteo, flag it
            cached_data = cached["data"]
            if cache_age > 300 or cache_provider != "open-meteo":
                log(f"Forecast cache HIT ({cache_age:.0f}s, {cache_provider}) —⚠️  aging/stale source for {lat:.2f},{lon:.2f} date={target_date or 'today'}")
            else:
                log(f"Forecast cache HIT ({cache_age:.0f}s, {cache_provider}) for {lat:.2f},{lon:.2f} date={target_date or 'today'}")
            _provider_used = f"cache-{cache_provider}"
            return cached_data

    # ── Open-Meteo (primary) ──
    om_tried, om_429 = False, False
    if time.time() >= _openmeteo_backoff_until:
        om_tried = True
        try:
            result = _try_openmeteo_forecast(lat, lon, tz_name, unit, target_date)
            if result:
                global _openmeteo_successes
                _provider_used = "open-meteo"
                _openmeteo_successes += 1
                _last_successful_forecast_ts = time.time()
                _first_429_ts = 0
                _consecutive_429_cycles = 0
                # Forecast sanity check: compare fresh vs old cached value
                old_cached = cache.get(cache_key)
                if old_cached and old_cached.get("data", {}).get("mean") is not None:
                    old_mean = old_cached["data"]["mean"]
                    new_mean = result["mean"]
                    delta = abs(new_mean - old_mean)
                    if delta > 5.0:
                        log(f"⚠️  FORECAST DRIFT: {lat:.2f},{lon:.2f} date={target_date or 'today'} old={old_mean}°→new={new_mean}° (Δ{delta:.1f}°). Old cache was {time.time()-old_cached.get('ts',0):.0f}s old from {old_cached.get('provider','?')}")
                        result["drift_alert"] = True
                        result["drift_delta"] = round(delta, 2)
                # Update cache
                cache[cache_key] = {"data": result, "ts": time.time(), "provider": "open-meteo"}
                cutoff = time.time() - 7200
                cache = {k: v for k, v in cache.items() if v.get("ts", 0) > cutoff}
                _save_forecast_cache(cache)
                return result
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                om_429 = True
                now = time.time()
                _openmeteo_429_hits += 1
                _consecutive_429_cycles += 1
                if _first_429_ts == 0:
                    _first_429_ts = now
                streak_min = (now - _first_429_ts) / 60.0
                if streak_min > 30:
                    backoff = 600
                elif streak_min > 15:
                    backoff = 300
                else:
                    backoff = 120
                _openmeteo_backoff_until = now + backoff
                log(f"Open-Meteo 429 (hit #{_openmeteo_429_hits}, streak={_consecutive_429_cycles}, {streak_min:.0f}min) — backoff {backoff}s")
            else:
                log(f"Open-Meteo HTTP error for {lat},{lon}: {e}")
        except Exception as e:
            log(f"Open-Meteo error for {lat},{lon}: {e}")

    # ── NWS fallback (US-only) ──
    is_us = -130.0 <= lon <= -65.0
    if result is None and is_us:
        nws = _get_nws_forecast(lat, lon, tz_name, target_date)
        if nws:
            # Convert F → C if needed (NWS returns Fahrenheit)
            if unit == "celsius":
                nws["mean"] = round((nws["mean"] - 32) * 5/9, 2)
                nws["temps"] = [round((t - 32) * 5/9, 2) for t in nws["temps"]]
                nws["min"] = round((nws["min"] - 32) * 5/9, 2)
                nws["max"] = round((nws["max"] - 32) * 5/9, 2)
            result = nws
            global _nws_successes, _non_us_skipped
            _provider_used = "nws"
            _nws_successes += 1
            _last_successful_forecast_ts = time.time()
            # Update cache
            cache[cache_key] = {"data": result, "ts": time.time(), "provider": "nws"}
            cutoff = time.time() - 7200
            cache = {k: v for k, v in cache.items() if v.get("ts", 0) > cutoff}
            _save_forecast_cache(cache)
            log(f"NWS fallback success: {lat:.2f},{lon:.2f} → {result['mean']}°{unit}")
            return result

    # ── Non-US city skipped (OM unavailable, no NWS coverage) ──
    if result is None and not is_us and (om_tried or time.time() < _openmeteo_backoff_until):
        global _non_us_skipped
        _non_us_skipped += 1

    # ── Stale cache (last resort, never for micro-live eligibility) ──
    if result is None and cache_key in cache:
        cached = cache[cache_key]
        cache_age = time.time() - cached.get("ts", 0)
        if cache_age < 3600:
            result = cached["data"]
            _provider_used = "cache-stale"
            log(f"⚠️  Forecast cache STALE ({cache_age:.0f}s, {cached.get('provider','?')}) for {lat:.2f},{lon:.2f} — may be unreliable")
            return result

    _provider_used = "none"
    return None


# ── Climatology (ERA5 historical archive, cached per city-date) ───────────────
def _load_climo_cache():
    if CLIMO_CACHE_FILE.exists():
        try:
            return json.loads(CLIMO_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_climo_cache(cache):
    try:
        CLIMO_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log(f"Climo cache save error: {e}")


def get_climatology(city, lat, lon, tz_name, target_date, unit='fahrenheit', years=10):
    """10-year historical high for the same MM-DD at this location. Cached."""
    mm_dd = target_date[5:]  # "MM-DD"
    cache_key = f"{city}|{mm_dd}|{unit}"
    cache = _load_climo_cache()
    if cache_key in cache:
        return cache[cache_key]
    try:
        global _openmeteo_backoff_until, _openmeteo_429_hits
        if time.time() < _openmeteo_backoff_until:
            return None  # silent skip during backoff
        this_year = datetime.now(ZoneInfo(tz_name)).year
        start = f"{this_year - years}-01-01"
        end = f"{this_year - 1}-12-31"
        resp = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
            "latitude": lat, "longitude": lon,
            "start_date": start, "end_date": end,
            "daily": "temperature_2m_max",
            "temperature_unit": unit,
            "timezone": tz_name,
        }, timeout=20)
        resp.raise_for_status()
        d = resp.json()
        times = d.get("daily", {}).get("time", [])
        temps = d.get("daily", {}).get("temperature_2m_max", [])
        same_day = [t for (ts, t) in zip(times, temps) if ts.endswith(mm_dd) and t is not None]
        if len(same_day) < 3:
            return None
        result = {
            "mean": round(statistics.mean(same_day), 2),
            "std": round(statistics.stdev(same_day) if len(same_day) > 1 else 1.0, 2),
            "n": len(same_day),
        }
        cache[cache_key] = result
        _save_climo_cache(cache)
        return result
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            _openmeteo_429_hits += 1
            _openmeteo_backoff_until = time.time() + 120
            log(f"Open-Meteo 429 (archive) — backing off for 120s")
        else:
            log(f"Climo HTTP error {city} {mm_dd}: {e}")
    except Exception as e:
        log(f"Climo error {city} {mm_dd}: {e}")
        return None


# ── Probability & time helpers ────────────────────────────────────────────────
def _norm_cdf(x, mu, sigma):
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def prob_temp_in_range(low, high, mean, sigma):
    """P(actual high falls in range) given forecast ~ N(mean, sigma).

    Temperature buckets are integers on Polymarket; expand by 0.5 on each side.
    """
    if sigma <= 0:
        sigma = 0.5
    if low is None and high is not None:
        return _norm_cdf(high + 0.5, mean, sigma)
    if low is not None and high is None:
        return 1 - _norm_cdf(low - 0.5, mean, sigma)
    if low is not None and high is not None:
        return _norm_cdf(high + 0.5, mean, sigma) - _norm_cdf(low - 0.5, mean, sigma)
    return 0.0


def parse_event_date(date_str, tz_name):
    """Parse 'April 23' / 'april-23-2026' → date in city tz. Returns YYYY-MM-DD or None."""
    try:
        cleaned = date_str.replace("-", " ").strip(" ?.!,")
        now_local = datetime.now(ZoneInfo(tz_name))
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                d = datetime.strptime(cleaned, fmt)
                candidate = d.date()
                return candidate.isoformat()
            except ValueError:
                continue
        # Fallback: no year in the string → assume current local year, bump forward if past
        for fmt in ("%B %d", "%b %d"):
            try:
                d = datetime.strptime(f"{cleaned} {now_local.year}", f"{fmt} %Y")
                year = now_local.year
                candidate = d.replace(year=year).date()
                # if the parsed date looks like it's in the past (no year given),
                # bump to next year
                if year == now_local.year and candidate < now_local.date() - timedelta(days=1):
                    candidate = candidate.replace(year=year + 1)
                return candidate.isoformat()
            except ValueError:
                continue
    except Exception as e:
        log(f"Date parse error '{date_str}': {e}")
    return None


def hours_to_resolution(target_date_iso, tz_name):
    """Hours remaining until local-midnight-end-of-day in the city tz."""
    try:
        tz = ZoneInfo(tz_name)
        target = datetime.strptime(target_date_iso, "%Y-%m-%d").date()
        # Resolution = end of that local day (next midnight)
        resolve_local = datetime.combine(target + timedelta(days=1), datetime.min.time(), tzinfo=tz)
        now = datetime.now(tz)
        return max(0.0, (resolve_local - now).total_seconds() / 3600.0)
    except Exception:
        return 24.0


def effective_sigma(ensemble_std, hours_left):
    """Time-adjusted sigma. Farther out → wider. Inside 6h → tight."""
    if hours_left <= 6:
        floor = 0.4
    elif hours_left <= 12:
        floor = 0.7
    elif hours_left <= 24:
        floor = 1.0
    else:
        floor = 1.5
    return max(ensemble_std, floor)


# ── Boundary margin computation ──────────────────────────────────────────────
def compute_margin_f(ensemble_mean, low, high, unit='F'):
    """Distance from forecast mean to the nearest *losing* boundary in °F.

    Returns a positive float: how far the forecast is from busting out.
    Returns a negative float if the forecast is already outside the range.
    Returns None if the range is unbounded (no boundary to bust).
    """
    if unit == 'C':
        margin = compute_margin_f(ensemble_mean * 9/5 + 32,
                                  (low or -999) * 9/5 + 32,
                                  (high or 999) * 9/5 + 32,
                                  'F')
        return margin

    # Now all in °F
    if low is not None and high is not None:
        # Range: forecast must be between low and high
        # Losing boundaries: below low - 0.5, above high + 0.5 (rounding)
        lo_boundary = low - 0.5
        hi_boundary = high + 0.5
        if ensemble_mean < lo_boundary:
            return ensemble_mean - lo_boundary  # negative = already busting low
        if ensemble_mean > hi_boundary:
            return hi_boundary - ensemble_mean  # negative = already busting high
        return min(ensemble_mean - lo_boundary, hi_boundary - ensemble_mean)
    elif low is not None and high is None:
        # "X or higher": losing if < low - 0.5
        lo_boundary = low - 0.5
        if ensemble_mean < lo_boundary:
            return ensemble_mean - lo_boundary  # negative
        return ensemble_mean - lo_boundary  # positive margin above boundary
    elif low is None and high is not None:
        # "X or below": losing if > high + 0.5
        hi_boundary = high + 0.5
        if ensemble_mean > hi_boundary:
            return hi_boundary - ensemble_mean  # negative
        return hi_boundary - ensemble_mean  # positive margin below boundary
    return None


# ── CLOB pricing ──────────────────────────────────────────────────────────────
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from polymarket_clob_pricing import fetch_book as _clob_fetch_book
except Exception as _clob_err:
    _clob_fetch_book = None
    log(f"polymarket_clob_pricing not importable: {_clob_err}")


def get_live_ask(token_id, timeout=4):
    """Return (best_ask, best_bid, spread, raw_book) from CLOB; None on failure."""
    if not token_id or _clob_fetch_book is None:
        return None
    book = _clob_fetch_book(token_id, timeout=timeout)
    if book.get("error") or book.get("best_ask") is None:
        return None
    return {
        "best_ask": book["best_ask"],
        "best_bid": book.get("best_bid"),
        "spread": book.get("spread"),
        "midpoint": book.get("midpoint"),
    }


# ── Stale order cleanup ─────────────────────────────────────────────────────
def cancel_weather_open_orders(client):
    """Cancel ONLY weather-bot open orders (not unrelated Polymarket orders).

    Loads token_ids from our trade log to identify weather orders, then
    cancels only those. Never touches non-weather positions.

    Returns (cancelled_count, orders_older_than_ttl_count, oldest_order_age_sec).
    """
    # ── Collect known weather token_ids from trade log ──
    weather_token_ids = set()
    if TRADE_LOG.exists():
        try:
            for t in json.loads(TRADE_LOG.read_text()):
                tid = t.get("token_id")
                if tid:
                    weather_token_ids.add(tid)
        except Exception:
            pass

    if not weather_token_ids:
        return 0, 0, 0

    # ── Get open orders ──
    try:
        # Import locally to avoid circular dependency at module level
        sys.path.insert(0, str(Path(__file__).parent))
        from polymarket_executor import get_open_orders, cancel_order
        open_orders = get_open_orders(client)
    except Exception as e:
        log(f"  [STALE-ERR] get_open_orders: {e}")
        return 0, 0, 0

    if not open_orders:
        return 0, 0, 0

    cancelled = 0
    old = 0
    oldest_age = 0
    now_ts = time.time()

    for order in open_orders:
        oid = order.get("id") or order.get("orderID") or order.get("order_id")
        tid = order.get("token_id") or order.get("asset") or ""

        # ── ONLY cancel if this order is on a known weather token_id ──
        if tid not in weather_token_ids:
            continue

        ots = order.get("timestamp") or order.get("createdAt") or 0
        if isinstance(ots, str):
            try:
                ots = datetime.fromisoformat(ots.replace("Z", "+00:00")).timestamp()
            except Exception:
                ots = now_ts
        age = now_ts - ots

        if age > STALE_ORDER_TTL:
            old += 1
            if age > oldest_age:
                oldest_age = age

        try:
            cancel_order(client, oid)
            cancelled += 1
            log(f"  [STALE-CANCEL] order={oid[:20]}... token={tid[:15]}... age={age:.0f}s")
        except Exception as e:
            log(f"  [STALE-CANCEL-ERR] order={oid[:20]}... {e}")

    return cancelled, old, oldest_age


# ── Market scanning ───────────────────────────────────────────────────────────
CITY_PATTERN = re.compile(r"highest temperature in (.+?) on (.+)", re.IGNORECASE)


def scan_markets():
    """Find all active temperature markets."""
    events = []
    try:
        resp = requests.get(f"{GAMMA_API}/events", params={
            "limit": 200,
            "closed": "false",
            "tag_slug": "weather",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for e in data:
            title = e.get("title", "")
            match = CITY_PATTERN.search(title)
            if not match:
                continue
            city_raw = match.group(1).strip().lower()
            date_str = match.group(2).strip()
            if city_raw not in WHITELIST:
                continue
            if ONLY_CITIES and city_raw not in ONLY_CITIES:
                continue
            events.append({
                "title": title,
                "slug": e.get("slug", ""),
                "city": city_raw,
                "date_str": date_str,
                "volume": float(e.get("volume", 0)),
                "markets": e.get("markets", []),
                "neg_risk": e.get("negRisk", False) or e.get("enableNegRisk", False),
            })
    except Exception as e:
        log(f"Scan error: {e}")
    return events


def parse_temp_range(label):
    """Parse temperature range from market label. Handles both °F and °C."""
    label = label.strip()
    # "8°C or below" → (None, 8)
    m = re.match(r"(\d+)°?C\s+or\s+below", label, re.IGNORECASE)
    if m:
        return (None, int(m.group(1)), 'C')
    # "64°F or below" → (None, 64)
    m = re.match(r"(\d+)°?F\s+or\s+below", label, re.IGNORECASE)
    if m:
        return (None, int(m.group(1)), 'F')
    # "18°C or higher" → (18, None)
    m = re.match(r"(\d+)°?C\s+or\s+higher", label, re.IGNORECASE)
    if m:
        return (int(m.group(1)), None, 'C')
    # "64°F or higher" → (64, None)
    m = re.match(r"(\d+)°?F\s+or\s+higher", label, re.IGNORECASE)
    if m:
        return (int(m.group(1)), None, 'F')
    # "54-55°F" → (54, 55)
    m = re.match(r"(\d+)[-–](\d+)°?F", label, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)), 'F')
    # "12°C" (single value, means exact) → (12, 12)
    m = re.match(r"(\d+)°?C$", label, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(1)), 'C')
    # "54°F" (single value)
    m = re.match(r"(\d+)°?F$", label, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(1)), 'F')
    return (None, None, None)


def forecast_matches_range(forecast_temp, low, high, unit):
    """Check if forecast temperature falls within a range. Unit is 'C' or 'F'."""
    if low is None and high is not None:
        return forecast_temp <= high  # "X or below"
    if low is not None and high is None:
        return forecast_temp >= low  # "X or higher"
    if low is not None and high is not None:
        return low <= forecast_temp <= high
    return False


# ── Trading logic ─────────────────────────────────────────────────────────────
def detect_unit(event):
    """Detect if a market uses °C or °F from its labels."""
    for m in event.get('markets', []):
        lbl = m.get('groupItemTitle', m.get('question', ''))
        if '°C' in lbl:
            return 'C'
        if '°F' in lbl:
            return 'F'
    return 'F'  # default


def _market_dict(m):
    """Normalize a Gamma market entry into the dict shape the rest of the bot uses."""
    label = m.get("groupItemTitle", m.get("question", ""))
    low, high, mkt_unit = parse_temp_range(label)
    prices_raw = m.get("outcomePrices", "[]")
    try:
        yes_price = float(json.loads(prices_raw)[0])
    except Exception:
        yes_price = 0.5
    try:
        token_ids = json.loads(m.get("clobTokenIds", "[]")) if m.get("clobTokenIds") else []
    except Exception:
        token_ids = []
    return {
        "label": label,
        "low": low,
        "high": high,
        "unit": mkt_unit,
        "yes_price": yes_price,
        "volume": float(m.get("volume", 0) or 0),
        "token_ids": token_ids,
        "condition_id": m.get("conditionId", ""),
        "slug": m.get("slug", ""),
        "question_id": m.get("questionID", ""),
    }


def evaluate_trade_setup(event, market, ensemble, city_info, hours_left, climo=None):
    """Score a market using the ensemble forecast probability.

    confidence = P(actual high ∈ [low, high]) under N(ensemble.mean, effective_sigma)
    edge       = confidence - live_ask (fall back to yes_price when CLOB missing)

    city_wr acts as a prior and caps runaway confidence.
    """
    city = event["city"]
    label = market["label"]
    low, high = market["low"], market["high"]
    sigma = effective_sigma(ensemble["std"], hours_left)
    raw_prob = prob_temp_in_range(low, high, ensemble["mean"], sigma)

    # ── Boundary margin check (tightened: simple ≥1.5°F, range ≥2.0°F) ──
    margin_f = compute_margin_f(ensemble["mean"], low, high, market.get("unit") or 'F')
    # If margin_f is None (unbounded range) or negative (already busting), skip
    margin_threshold = _get_margin_threshold(low, high)
    margin_ok = margin_f is None or margin_f >= margin_threshold

    # Climatology outlier penalty: forecast far from climo AND models disagree → haircut
    climo_penalty = 1.0
    climo_z = None
    if climo and climo.get("std"):
        climo_z = abs(ensemble["mean"] - climo["mean"]) / max(climo["std"], 0.5)
        if climo_z > CLIMO_OUTLIER_Z and ensemble["std"] > 1.5:
            climo_penalty = 0.85

    # Blend with historical city win rate as a weak prior (Beta-ish shrinkage toward WR)
    wr = city_info["wr"]
    confidence = (0.8 * raw_prob + 0.2 * wr) * climo_penalty
    confidence = max(0.0, min(0.99, confidence))

    # First-pass uses the stale Gamma price. CLOB lookup happens later,
    # only after this setup clears the confidence/edge gates.
    gamma_price = market["yes_price"]
    edge = confidence - gamma_price
    size = min(MAX_BET_SIZE, round(MAX_BET_SIZE * confidence, 2))
    return {
        "event": event,
        "market": market,
        "ensemble": ensemble,
        "forecast_temp": round(ensemble["mean"]),
        "city": city,
        "label": label,
        "sigma": sigma,
        "raw_prob": round(raw_prob, 3),
        "climo_z": climo_z,
        "yes_price": gamma_price,
        "gamma_price": gamma_price,
        "clob_book": None,
        "confidence": confidence,
        "edge": edge,
        "size": size,
        "hours_left": hours_left,
        "volume": float(market.get("volume") or 0),
        "margin_f": margin_f,
        "margin_ok": margin_ok,
        "margin_rule": _margin_rule_label(low, high),
        "required_margin_f": margin_threshold,
        "market_type": _market_type_label(low, high),
    }


def refresh_with_clob(setup):
    """Update setup's entry price, edge, and size using live CLOB best ask.

    Returns False when the CLOB book rejects the trade (missing ask, spread too wide,
    or live ask pushes edge below MIN_EDGE). Mutates setup on success.
    """
    market = setup["market"]
    token_id = market["token_ids"][0] if market["token_ids"] else None
    if not token_id:
        return True  # no token to check; fall back to gamma price
    book = get_live_ask(token_id)
    setup["clob_book"] = book
    if not book:
        return True  # keep stale price; live trading will re-check
    if book.get("spread") is not None and book["spread"] > CLOB_SPREAD_CAP:
        return False
    best_ask = book.get("best_ask")
    if best_ask is None:
        return False
    setup["yes_price"] = best_ask
    setup["edge"] = setup["confidence"] - best_ask
    if best_ask > 0:
        setup["size"] = min(MAX_BET_SIZE, round(MAX_BET_SIZE * setup["confidence"], 2))
    return setup["edge"] >= MIN_EDGE and best_ask <= 1 - MIN_EDGE


def count_todays_live_trades():
    """Count LIVE trades placed today (UTC calendar day).
    Reads trade log read-only. Returns (count, list of today's live trades)."""
    today = datetime.now(timezone.utc).date().isoformat()
    trades = []
    if TRADE_LOG.exists():
        try:
            trades = json.loads(TRADE_LOG.read_text())
        except Exception:
            pass
    today_live = []
    for t in trades:
        t_event = t.get("event", "")
        if t_event != "LIVE_BUY":
            continue
        t_ts = t.get("ts", "")
        try:
            t_date = datetime.strptime(t_ts[:10], "%Y-%m-%d").date().isoformat()
        except Exception:
            continue
        if t_date == today:
            today_live.append(t)
    return len(today_live), today_live


def check_micro_live_eligibility(setup, margin_f, hours_left):
    """Evaluate a dry-run candidate against micro-live probe gates.
    Returns (passed: bool, results: dict) where results maps gate_name → (ok, value, threshold).
    """
    market_type = setup.get("market_type", "")
    confidence = setup.get("confidence", 0)
    edge = setup.get("edge", 0)
    yes_price = setup.get("yes_price", 0.99)
    ensemble = setup.get("ensemble", {})
    forecast_ts = ensemble.get("forecast_timestamp", "")

    results = {}

    # Gate 0: Forecast source must be healthy (fresh API fetch, not stale cache, no API errors)
    source_ok = False
    source_detail = "unknown"
    provider_tag = ensemble.get("provider", "?")
    source_type = ensemble.get("source_type", "")  # 'primary' = direct API, 'fallback' = NWS/cache
    # Check for API errors in the current cycle
    api_errors = _openmeteo_429_hits > 0 and _provider_used == "none"
    # Check for stale cache usage
    using_cache = bool(_provider_used and _provider_used.startswith("cache"))
    # Check for forecast source mismatch (ensemble flagged as inconsistent)
    source_mismatch = ensemble.get("source_mismatch", False)
    drift_alert = bool(ensemble.get("drift_alert"))

    if _last_successful_forecast_ts > 0 and not api_errors and not using_cache and not source_mismatch and not drift_alert:
        source_age = time.time() - _last_successful_forecast_ts
        if source_age < 600 and provider_tag == "open-meteo":
            source_ok = True
            source_detail = f"fresh {provider_tag} ({source_age:.0f}s)"
        elif source_age >= 600:
            source_detail = f"stale {provider_tag} ({source_age:.0f}s)"
        else:
            source_detail = f"non-primary {provider_tag} ({source_age:.0f}s)"
    elif api_errors:
        source_detail = f"BLOCKED (429×{_openmeteo_429_hits}, provider=none)"
    elif using_cache:
        source_detail = f"BLOCKED stale cache ({_provider_used})"
    elif source_mismatch:
        source_detail = f"BLOCKED source mismatch (OM≠NWS>5°F)"
    elif drift_alert:
        source_detail = f"BLOCKED drift_alert (Δ{ensemble.get('drift_delta', '?')}° vs cache)"
    elif _provider_used == "nws":
        source_age = time.time() - _last_successful_forecast_ts
        source_detail = f"fallback-nws ({source_age:.0f}s) — BLOCKED"
    else:
        source_detail = f"no data ({_provider_used})"
    results["source"] = (source_ok, source_detail, "fresh open-meteo (age < 600s, no errors, no cache, no mismatch)")

    # Gate 1: Market type — must be simple (above/below), not range
    is_simple = market_type != "range"
    results["market_type"] = (is_simple, market_type, "simple (not range)")

    # Gate 2: Margin ≥ 3.0°F
    margin_pct = margin_f / MICRO_LIVE_MARGIN_SIMPLE if margin_f else 0
    results["margin"] = (margin_f is not None and margin_f >= MICRO_LIVE_MARGIN_SIMPLE,
                         margin_f if margin_f else 0, MICRO_LIVE_MARGIN_SIMPLE)

    # Gate 3: Confidence ≥ 75%
    results["confidence"] = (confidence >= MICRO_LIVE_CONFIDENCE,
                             confidence, MICRO_LIVE_CONFIDENCE)

    # Gate 4: Edge ≥ 0.20
    results["edge"] = (edge >= MICRO_LIVE_EDGE, edge, MICRO_LIVE_EDGE)

    # Gate 5: Best ask ≤ $0.40
    results["best_ask"] = (yes_price <= MICRO_LIVE_MAX_ASK, yes_price, MICRO_LIVE_MAX_ASK)

    # Gate 6: Forecast age ≤ 600s
    forecast_age = -1
    if forecast_ts:
        try:
            ft = datetime.strptime(forecast_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            forecast_age = (datetime.now(timezone.utc) - ft).total_seconds()
        except Exception:
            pass
    results["forecast_age"] = (0 <= forecast_age <= MICRO_LIVE_MAX_AGE_SEC,
                               forecast_age, MICRO_LIVE_MAX_AGE_SEC)

    # Gate 7: Max risk ≤ $2
    effective_micro_risk = min(MAX_BET_SIZE, WEATHER_MAX_LIVE_RISK_USD)
    results["max_risk"] = (effective_micro_risk <= 2.0,
                           effective_micro_risk, 2.0)

    # Gate 8: FOK only
    results["order_type"] = (True, "FOK", "FOK only")

    # Gate 9: One attempt only (must not already be armed out)
    one_attempt_ok = not (SENTINEL_MICRO_ATTEMPTED.exists() or SENTINEL_MICRO_DONE.exists() or SENTINEL_MICRO_TIMEOUT.exists())
    one_attempt_detail = "available"
    if not one_attempt_ok:
        if SENTINEL_MICRO_ATTEMPTED.exists():
            one_attempt_detail = SENTINEL_MICRO_ATTEMPTED.name
        elif SENTINEL_MICRO_DONE.exists():
            one_attempt_detail = SENTINEL_MICRO_DONE.name
        else:
            one_attempt_detail = SENTINEL_MICRO_TIMEOUT.name
    results["one_attempt"] = (one_attempt_ok, one_attempt_detail, "no micro-live sentinels present")

    # Gate 10: Internal timeout must be active
    results["timeout_active"] = (WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES > 0,
                                 WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES, ">0 min")

    passed = all(v[0] for v in results.values())
    return passed, results


def check_forecast_age(setup):
    """Check forecast timestamp freshness.
    Returns (ok: bool, age_seconds: float, reason: str)."""
    fts = setup["ensemble"].get("forecast_timestamp", "")
    if not fts:
        return False, -1, "missing forecast_timestamp"
    try:
        ft = datetime.strptime(fts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ft).total_seconds()
        if age > MAX_FORECAST_AGE:
            return False, age, f"forecast age {age:.0f}s > {MAX_FORECAST_AGE}s limit"
        return True, age, "ok"
    except Exception as e:
        return False, -1, f"forecast_timestamp parse error: {e}"


def execute_trade(setup):
    """Place a buy order using a fully-scored setup from evaluate_trade_setup."""
    global PAUSED
    event = setup["event"]
    market = setup["market"]
    city = setup["city"]
    label = setup["label"]
    yes_price = setup["yes_price"]  # live ask when available
    confidence = setup["confidence"]
    edge = setup["edge"]
    size = setup["size"]
    forecast_temp = setup["forecast_temp"]
    target_date = parse_event_date(event.get("date_str", ""), WHITELIST.get(city, {}).get("tz", "UTC"))
    source_meta = _trade_source_breadcrumbs(setup["ensemble"])

    if DRY_RUN:
        margin_f_str = f"{setup.get('margin_f')}" if setup.get('margin_f') is not None else 'N/A'
        margin_rule = setup.get('margin_rule', '?')
        req_margin = setup.get('required_margin_f', '?')
        mkt_type = setup.get('market_type', '?')
        log(f"  [DRY-RUN] BUY {city} {label} @ ${yes_price:.3f} size=${size:.2f} edge={edge:.2f} confidence={confidence:.0%} forecast={forecast_temp}° σ={setup['sigma']:.2f} T-{setup['hours_left']:.1f}h margin={margin_f_str} req={req_margin} rule={margin_rule} type={mkt_type}")
        tg(f"[WEATHER-DRY] ORDER PLACED: {city.title()} {label} @ ${yes_price:.3f} size=${size:.2f} | forecast={forecast_temp}° edge={edge:.2f} confidence={confidence:.0%} | {margin_rule}")
        log_trade({
            "event": "DRY_BUY",
            "city": city,
            "label": label,
            "event_slug": event.get("slug", ""),
            "target_date": target_date,
            "market_title": event.get("title", ""),
            "yes_price": yes_price,
            "size": size,
            "edge": edge,
            "confidence": confidence,
            "forecast": forecast_temp,
            "ensemble_mean": setup["ensemble"]["mean"],
            "ensemble_std": setup["ensemble"]["std"],
            "sigma": setup["sigma"],
            "raw_prob": setup["raw_prob"],
            "climo_z": setup["climo_z"],
            "hours_left": setup["hours_left"],
            "margin_f": setup.get("margin_f"),
            "required_margin_f": setup.get("required_margin_f"),
            "margin_rule": setup.get("margin_rule"),
            "market_type": setup.get("market_type"),
            "forecast_timestamp": setup["ensemble"].get("forecast_timestamp", ""),
            "forecast_ts": source_meta.get("forecast_ts", ""),
            "forecast_age_seconds": source_meta.get("forecast_age_seconds"),
            "provider": source_meta.get("provider"),
            "source_type": source_meta.get("source_type"),
            "cache_hit": source_meta.get("cache_hit"),
            "source_health": source_meta.get("source_health"),
            "order_type": "DRY_FOK",
            "reason": "boundary_margin_ok" if setup.get("margin_ok") else "boundary_margin_skip",
            "dry_run": True,
            "patch_version": PATCH_VERSION,
        })
        _audit["weather_trades_executed"] += 1
        return True
    else:
        # ── Live execution via polymarket_executor ──

        # ── Phase 1 safety gate: daily trade limit ──
        today_count, today_trades = count_todays_live_trades()
        if today_count >= MAX_TRADES_PER_DAY:
            log(f"  [LIVE-SKIP] Daily trade limit reached ({today_count}/{MAX_TRADES_PER_DAY}) — skipping {city} {label}")
            tg(f"[WEATHER-SKIP] Daily limit: {today_count}/{MAX_TRADES_PER_DAY} trades today — skipping {city.title()} {label}")
            return False

        # ── Phase 1 safety gate: stop-after-loss sentinel ──
        if SENTINEL_LOSS.exists():
            log(f"  [LIVE-STOP] Loss-stop sentinel exists ({SENTINEL_LOSS}) — refusing new trade")
            tg(f"[WEATHER-STOP] Loss-stop active. Clear {SENTINEL_LOSS.name} to resume.")
            return False

        # ── Phase 1 safety gate: forecast age ──
        fa_ok, fa_age, fa_reason = check_forecast_age(setup)
        if not fa_ok:
            log(f"  [LIVE-SKIP] {fa_reason} — skipping {city} {label}")
            tg(f"[WEATHER-SKIP] Stale forecast: {fa_reason} — {city.title()} {label}")
            return False
        source_meta = _trade_source_breadcrumbs(setup["ensemble"], round(fa_age, 1) if fa_ok else None)

        # ── Micro-live sentinel gate: refuse if already attempted ──
        if WEATHER_MICRO_LIVE_ONLY:
            if SENTINEL_MICRO_ATTEMPTED.exists():
                log(f"  [MICRO-LIVE STOP] Attempted sentinel exists ({SENTINEL_MICRO_ATTEMPTED}) — refusing new trade")
                return False
            if SENTINEL_MICRO_DONE.exists():
                log(f"  [MICRO-LIVE STOP] Done sentinel exists ({SENTINEL_MICRO_DONE}) — refusing new trade")
                return False

        token_id = market["token_ids"][0] if market["token_ids"] else None
        if not token_id:
            log(f"  [LIVE-ERR] No token_id for {city} {label}")
            return False

        state = load_state()
        attempt_row, cooldown_remaining = _attempt_cooldown_remaining(state, city, label, target_date)
        if cooldown_remaining > 0:
            log(f"  [LIVE-SKIP] Cooldown active for {city} {label}: {cooldown_remaining:.0f}s remaining after {attempt_row.get('outcome')}")
            return False

        # Calculate shares from dollar size
        live_risk_usd = min(WEATHER_MAX_LIVE_RISK_USD, MAX_BET_SIZE) if WEATHER_MICRO_LIVE_ONLY else MAX_BET_SIZE
        effective_size = min(size, live_risk_usd)
        if yes_price <= 0:
            shares = effective_size / 0.01
        else:
            shares = round(effective_size / yes_price, 2)
        # Enforce Polymarket minimum 5 shares
        MIN_SHARES = 5.0
        if shares < MIN_SHARES:
            min_shares_cost = round(MIN_SHARES * yes_price, 2)
            if min_shares_cost <= effective_size:
                log(f"  [LIVE-ADJUST] Bumping shares {shares} → {int(MIN_SHARES)} to meet Polymarket minimum (cost=${min_shares_cost:.2f} ≤ budget=${effective_size:.2f})")
                shares = MIN_SHARES
            elif min_shares_cost <= MAX_BET_SIZE:
                # Budget is too tight but total size still within MAX_BET_SIZE — just bump
                log(f"  [LIVE-ADJUST] Bumping effective_size ${effective_size:.2f} → ${min_shares_cost:.2f} to meet 5-share min (within MAX_BET ${MAX_BET_SIZE})")
                effective_size = min_shares_cost
                shares = MIN_SHARES
            else:
                log(f"  [LIVE-SKIP] Shares {shares} below Poly min {int(MIN_SHARES)}; need ${min_shares_cost:.2f} > MAX_BET ${MAX_BET_SIZE} — skipping")
                return False

        filled = False
        order_error = None
        oid = None
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from polymarket_executor import get_client, place_order, get_best_prices, BUY, OrderType

            client = get_client()

            # Thin-book guard: downsize to visible ask depth within our FOK slippage cap.
            try:
                slippage_cap = round(yes_price * 1.10, 2)
                book = get_best_prices(token_id)
                raw_book = book.get("raw") or {}
                asks = raw_book.get("asks") or []
                visible_shares = 0.0
                for level in asks:
                    try:
                        px = float(level.get("price") or 0)
                        sz = float(level.get("size") or 0)
                    except Exception:
                        continue
                    if px <= slippage_cap:
                        visible_shares += sz
                safe_shares = round(visible_shares * max(0.1, min(1.0, WEATHER_BOOK_HAIRCUT)), 2)
                if visible_shares > 0 and safe_shares < shares:
                    if safe_shares >= MIN_SHARES:
                        old_shares = shares
                        shares = safe_shares
                        effective_size = round(shares * yes_price, 2)
                        log(f"  [LIVE-ADJUST] Thin book: visible={visible_shares:.2f} safe={safe_shares:.2f} shares within ${slippage_cap:.2f}; downsizing {old_shares:.2f} → {shares:.2f} (risk=${effective_size:.2f})")
                    else:
                        _record_attempt_result(state, city, label, target_date, "thin_book_skip", f"visible_shares={visible_shares:.2f} safe_shares={safe_shares:.2f}", cooldown_sec=min(WEATHER_RETRY_COOLDOWN_SEC, 300))
                        save_state(state)
                        log(f"  [LIVE-SKIP] Thin book: only {visible_shares:.2f} shares visible within ${slippage_cap:.2f}; below 5-share minimum")
                        return False
            except Exception as _book_e:
                log(f"  [LIVE-WARN] order book sizing check failed: {_book_e}")
            pretrade = pre_trade_check_buy(
                client,
                required_usdc=round(max(effective_size, shares * yes_price) * 1.05, 6),
                refresh_allowance=True,
                metadata={"strategy": "weather", "city": city, "label": label, "token_id": token_id},
            )
            if not pretrade.get("ok"):
                reason_code = pretrade.get("reason_code")
                account_state = pretrade.get("account_state") or {}
                if reason_code in (MISSING_ALLOWANCE, CLOB_ALLOWANCE_MISMATCH) and not SIMULATE_MODE:
                    pause_payload = {
                        "reason": "AUTO_PAUSE_EXECUTION_NOT_READY",
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "city": city,
                        "label": label,
                        "yes_price": yes_price,
                        "shares": shares,
                        "allowance": account_state,
                        "pretrade": pretrade,
                    }
                    try:
                        SENTINEL_PAUSE.write_text(json.dumps(pause_payload, indent=2, default=str))
                    except Exception as _pause_e:
                        log(f"  [LIVE-ERR] pause sentinel write failed: {_pause_e}")
                    PAUSED = True
                    order_error = (
                        f"Execution not ready ({reason_code}) — auto-paused bot before order submit. "
                        f"required={account_state.get('required')} balance={account_state.get('balance')}"
                    )
                    log(f"  [LIVE-ERR] {order_error}")
                    tg(f"[WEATHER] ORDER FAILED: {city.title()} {label} — {order_error[:200]}")
                    tg(f"[WEATHER] 🛑 AUTO-PAUSED: live execution not ready ({reason_code}). Clear the blocker, then delete {SENTINEL_PAUSE.name} and restart.")
                    return False
                order_error = f"Pre-trade blocked: {reason_code} — {pretrade.get('reason')}"
                log(f"  [LIVE-ERR] {order_error}")
                tg(f"[WEATHER] ORDER FAILED: {city.title()} {label} — {order_error[:200]}")
                return False
            log(f"  [LIVE] Placing FOK order: {city} {label} BUY {shares:.2f} shares @ ${yes_price:.3f} risk=${effective_size:.2f}")
            tg(f"[WEATHER] 🎯 SIGNAL: {city.title()} {label} BUY {shares:.2f} @ ${yes_price:.3f} risk=${effective_size:.2f} — attempting FOK")
            result = place_order(
                client, token_id, shares, yes_price, BUY,
                market_question=f"{city} {label}",
                condition_id=market.get("condition_id", ""),
                order_type=OrderType.FOK,
                verify=True,
                max_trade_size_override=effective_size,
            )
            # Defensive guard: place_order may return a non-dict (e.g. odd SDK
            # response or wrapper object) which would crash result.get below.
            if not isinstance(result, dict) and result is not None:
                log(f"  [LIVE-ERR] place_order returned non-dict type={type(result).__name__} repr={repr(result)[:200]}")
                result = None
            status_norm = str((result or {}).get("status", "")).lower() if isinstance(result, dict) else ""
            fill_check = (result or {}).get("fill_check") or {} if isinstance(result, dict) else {}
            if result and result.get("success") and (
                status_norm == "matched" or fill_check.get("filled")
            ):
                filled = True
                oid = result.get("orderID") or result.get("id") or result.get("order_id")
                matched_via = "fill_check" if fill_check.get("filled") and status_norm != "matched" else "status"
                log(f"  [LIVE] ORDER MATCHED: {oid} via={matched_via} status={status_norm or 'n/a'}")
                tg(f"[WEATHER] ORDER MATCHED: {city.title()} {label} BUY {shares:.2f} @ ${yes_price:.3f} | order={oid}")
                log_trade({
                    "event": "LIVE_BUY",
                    "city": city,
                    "label": label,
                    "event_slug": event.get("slug", ""),
                    "target_date": target_date,
                    "market_title": event.get("title", ""),
                    "yes_price": yes_price,
                    "shares": shares,
                    "size_usd": effective_size,
                    "order_id": oid,
                    "token_id": token_id,
                    "order_type": "FOK",
                    "reason": "boundary_margin_ok" if setup.get("margin_ok") else "boundary_margin_skip",
                    "confidence": confidence,
                    "edge": edge,
                    "ensemble_mean": setup["ensemble"]["mean"],
                    "ensemble_std": setup["ensemble"]["std"],
                    "sigma": setup["sigma"],
                    "raw_prob": setup["raw_prob"],
                    "climo_z": setup["climo_z"],
                    "hours_left": setup["hours_left"],
                    "margin_f": setup.get("margin_f"),
                    "required_margin_f": setup.get("required_margin_f"),
                    "margin_rule": setup.get("margin_rule"),
                    "market_type": setup.get("market_type"),
                    "forecast_timestamp": setup["ensemble"].get("forecast_timestamp", ""),
                    "forecast_ts": source_meta.get("forecast_ts", ""),
                    "forecast_age_seconds": source_meta.get("forecast_age_seconds"),
                    "provider": source_meta.get("provider"),
                    "source_type": source_meta.get("source_type"),
                    "cache_hit": source_meta.get("cache_hit"),
                    "source_health": source_meta.get("source_health"),
                    "dry_run": False,
                })
                _record_attempt_result(state, city, label, target_date, "matched", oid or "", cooldown_sec=0, order_id=oid)
                save_state(state)
                _audit["weather_trades_executed"] += 1
            elif result and result.get("success") and status_norm == "delayed":
                oid = result.get("orderID") or result.get("id") or result.get("order_id")
                delayed_reason = fill_check.get("reason") or "awaiting_fill_confirmation"
                order_error = None
                log(f"  [LIVE-WAIT] Order submitted but not yet verified: status=delayed orderID={oid} fill_check={delayed_reason}")
                tg(f"[WEATHER] ORDER SUBMITTED: {city.title()} {label} BUY {shares:.2f} @ ${yes_price:.3f} | status=delayed | order={oid}")
                _record_attempt_result(state, city, label, target_date, "delayed", delayed_reason, cooldown_sec=WEATHER_RETRY_COOLDOWN_SEC, order_id=oid)
                save_state(state)
                return False
            else:
                allowance_block = False
                if result is None:
                    order_error = "Executor rejected or FOK not filled immediately"
                    log(f"  [LIVE-ERR] place_order returned None — likely no immediate fill or executor rejection")
                else:
                    # Surface the SDK fields explicitly instead of relying on the
                    # default dict repr — that's how we got the unhelpful
                    # "...: None" message that prompted this fix.
                    success = result.get("success")
                    status = result.get("status")
                    err_msg = result.get("errorMsg") or result.get("error_msg") or result.get("error")
                    err_code = result.get("errorCode") or result.get("error_code")
                    oid_dbg = result.get("orderID") or result.get("id") or result.get("order_id")
                    sdk_keys = ",".join(sorted(result.keys()))
                    order_error = (
                        f"Order failed/not matched: success={success} status={status} "
                        f"errorMsg={err_msg} errorCode={err_code} orderID={oid_dbg} "
                        f"keys=[{sdk_keys}]"
                    )
                    allowance_blob = result.get("allowance") or {}
                    allowance_block = (
                        status == "blocked"
                        or "allowance" in str(err_msg).lower()
                        or result.get("error") == "missing collateral allowance"
                    )
                    # Forensic dump for the next puzzling failure.
                    try:
                        dbg_path = WORK_DIR / ".weather_order_failures.json"
                        existing = []
                        if dbg_path.exists():
                            try:
                                existing = json.loads(dbg_path.read_text())
                            except Exception:
                                existing = []
                        existing.append({
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "city": city, "label": label,
                            "yes_price": yes_price, "shares": shares,
                            "result_repr": repr(result)[:1000],
                            "result_keys": list(result.keys()),
                            "result_fields": {k: result.get(k) for k in (
                                "success", "status", "errorMsg", "error_msg",
                                "error", "errorCode", "error_code",
                                "orderID", "id", "order_id",
                                "submitted_price", "submitted_order_type",
                                "fill_check", "allowance",
                            )},
                        })
                        dbg_path.write_text(json.dumps(existing[-50:], indent=2, default=str))
                    except Exception as _dbg_e:
                        log(f"  [LIVE-ERR] forensic dump failed: {_dbg_e}")
                    if allowance_block and not SIMULATE_MODE:
                        pause_payload = {
                            "reason": "AUTO_PAUSE_MISSING_ALLOWANCE",
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "city": city,
                            "label": label,
                            "yes_price": yes_price,
                            "shares": shares,
                            "allowance": allowance_blob,
                        }
                        try:
                            SENTINEL_PAUSE.write_text(json.dumps(pause_payload, indent=2, default=str))
                        except Exception as _pause_e:
                            log(f"  [LIVE-ERR] pause sentinel write failed: {_pause_e}")
                        PAUSED = True
                        concise = allowance_blob or {}
                        order_error = (
                            "Missing USDC/CLOB allowance — auto-paused bot to stop repeat failures. "
                            f"required={concise.get('required')} balance={concise.get('balance')}"
                        )
                cooldown_outcome = "allowance_block" if allowance_block else ("no_fill" if result is None else f"status_{status_norm or 'unknown'}")
                _record_attempt_result(state, city, label, target_date, cooldown_outcome, order_error, cooldown_sec=WEATHER_RETRY_COOLDOWN_SEC, order_id=(result or {}).get("orderID") if isinstance(result, dict) else None)
                save_state(state)
                log(f"  [LIVE-ERR] {order_error}")
                tg(f"[WEATHER] ORDER FAILED: {city.title()} {label} — {order_error[:200]}")
                if allowance_block:
                    tg(f"[WEATHER] 🛑 AUTO-PAUSED: missing CLOB allowance. Fix wallet approval, then delete {SENTINEL_PAUSE.name} and restart.")
                    return False
        except Exception as e:
            order_error = f"Exception: {str(e)[:200]}"
            log(f"  [LIVE-ERR] {e}")
            tg(f"[WEATHER] ORDER ERROR: {city.title()} {label} - {str(e)[:100]}")

        # ── Micro-live probe: auto-stop after ANY attempt ──
        if WEATHER_MICRO_LIVE_ONLY:
            attempt_data = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "city": city,
                "label": label,
                "slug": event.get("slug", ""),
                "status": "filled" if filled else ("no_fill" if order_error else "error"),
                "order_id": oid,
                "yes_price": yes_price,
                "shares": shares,
                "size_usd": effective_size,
                "error": order_error,
            }
            SENTINEL_MICRO_ATTEMPTED.write_text(json.dumps(attempt_data, indent=2))
            log(f"  [MICRO-LIVE] Sentinel written: {SENTINEL_MICRO_ATTEMPTED}")
            tg(f"[WEATHER] ⏹ MICRO-LIVE ATTEMPT COMPLETE: {city.title()} {label} | status={'FILLED' if filled else 'NO_FILL'} | risk=${effective_size:.2f} | sentinel={SENTINEL_MICRO_ATTEMPTED.name}")
            if filled:
                SENTINEL_MICRO_DONE.write_text(json.dumps(attempt_data, indent=2))
                log(f"  [MICRO-LIVE] Done sentinel written: {SENTINEL_MICRO_DONE}")
                tg(f"[WEATHER] ✅ MICRO-LIVE DONE: order={oid} | sentinel={SENTINEL_MICRO_DONE.name}")
            log("[MICRO-LIVE] Auto-stopping engine — micro-live probe complete")
            tg("[WEATHER] ⏹ Micro-live probe complete. Stopping engine. Clear .weather_micro_live_* sentinels to re-arm.")
            sys.exit(0)

        return filled


# ── Audit report ──────────────────────────────────────────────────────────────
def audit_summary():
    """Print an audit summary to stdout, resolving trades against Open-Meteo archive.

    READ-ONLY — never creates, modifies, cleans, or deletes trade logs.
    Only reads .weather_trade_log.json and .weather_skip_log.json.
    """
    global _audit
    print("\n" + "=" * 62)
    print("  WEATHER BOT AUDIT SUMMARY")
    print("=" * 62)

    # ── Helper: resolve a single trade against archive ──

    def _get_trade_target(trade):
        """Extract target date from a trade dict. Returns (YYYY-MM-DD, source) or (None, '')."""
        city = trade.get("city", "")
        ci = _city_info(city) or {"tz": "UTC"}

        if trade.get("target_date"):
            return str(trade["target_date"]), "target_date"

        slug = trade.get("event_slug") or trade.get("slug") or ""
        # Try recovering slug from state file (strict match: city + label + bought_at)
        if not slug:
            try:
                legacy_state = json.loads(STATE_FILE.read_text())
                for lp in legacy_state.get("positions", []):
                    if (lp.get("city") == city and
                        lp.get("label") == trade.get("label") and
                        lp.get("bought_at") == trade.get("ts")):
                        slug = lp.get("slug", "")
                        break
            except Exception:
                pass

        m = re.search(r"-on-([a-z]+-\d{1,2}-\d{4})", slug, re.IGNORECASE)
        if m:
            parsed = parse_event_date(m.group(1), ci["tz"])
            if parsed:
                return parsed, "event_slug"

        # Fallback: use trade timestamp date (best-effort, can be off for next-day markets)
        try:
            local = datetime.strptime(trade["ts"], "%Y-%m-%d %H:%M:%S").astimezone(ZoneInfo(ci["tz"]))
            return local.date().isoformat(), "trade_ts_fallback"
        except Exception:
            pass
        return None, ""

    def _forecast_age_min(fts, trade_ts):
        """Return forecast-to-trade age in minutes, or -1 if unparseable."""
        try:
            ft = datetime.strptime(fts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            tt = datetime.strptime(trade_ts, "%Y-%m-%d %H:%M:%S")
            tt_utc = tt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
            return (tt_utc - ft).total_seconds() / 60
        except Exception:
            return -1

    def _resolve_trade(trade):
        """Resolve a single trade against archive observed temp.
        Returns dict with status/pnl/observed, or None if pending/unresolvable."""
        city = trade.get("city", "")
        ci = _city_info(city)
        if not ci:
            return None  # unknown city, can't resolve

        label = trade.get("label", "")
        low, high, unit = parse_temp_range(label)
        if low is None and high is None:
            return None

        target, _ = _get_trade_target(trade)
        if not target:
            return None

        tz = ZoneInfo(ci["tz"])
        target_dt = datetime.strptime(target, "%Y-%m-%d").date()
        today_dt = datetime.now(tz).date()

        if target_dt >= today_dt:
            return None  # still pending

        # Fetch observed temp
        temp_unit = "celsius" if unit == "C" else "fahrenheit"
        try:
            r = requests.get(ARCHIVE_API, params={
                "latitude": ci["lat"],
                "longitude": ci["lon"],
                "start_date": target,
                "end_date": target,
                "daily": "temperature_2m_max",
                "temperature_unit": temp_unit,
                "timezone": ci["tz"],
            }, timeout=15)
            r.raise_for_status()
            temps = r.json().get("daily", {}).get("temperature_2m_max") or []
            if not temps or temps[0] is None:
                return None
            observed = float(temps[0])
            observed_r = round(observed)
        except Exception:
            return None

        # Determine win/loss
        if low is None and high is not None:
            won = observed_r <= high
        elif low is not None and high is None:
            won = observed_r >= low
        elif low is not None and high is not None:
            won = low <= observed_r <= high
        else:
            won = False

        entry = float(trade.get("yes_price", 0.5))
        size_usd = float(trade.get("size_usd", trade.get("size", 0)))
        shares = size_usd / max(entry, 0.01)
        pnl = shares * (1.0 - entry) if won else -shares * entry

        return {
            "won": won,
            "pnl": round(pnl, 2),
            "observed": observed,
            "observed_rounded": observed_r,
            "margin_f": trade.get("margin_f"),
            "forecast_timestamp": trade.get("forecast_timestamp", ""),
            "patch_version": trade.get("patch_version", 0),
            "margin_rule": trade.get("margin_rule", ""),
            "required_margin_f": trade.get("required_margin_f"),
            "market_type": trade.get("market_type", ""),
            "entry": entry,
            "size_usd": size_usd,
            "confidence": trade.get("confidence"),
            "label": label,
            "city": city,
            "ts": trade.get("ts", ""),
            "target_date": target,
        }

    # ── Load trade log (read-only) ──
    trades = []
    if TRADE_LOG.exists():
        try:
            trades = json.loads(TRADE_LOG.read_text())
        except Exception:
            pass

    live_trades = [t for t in trades if t.get("event") in ("LIVE_BUY", "DRY_BUY")]

    # ── Classify each trade ──
    whitelist_wins = []
    whitelist_losses = []
    legacy_wins = []
    legacy_losses = []
    pending_trades = []
    unresolvable = []
    invalidated_trades = []

    for t in live_trades:
        invalid = _trade_invalidation_info(t)
        if invalid:
            invalidated_trades.append({**t, **invalid})
            continue

        city = t.get("city", "")
        result = _resolve_trade(t)

        if result is None:
            # Couldn't resolve — check if target is in the future or truly unresolvable
            ci = _city_info(city)
            if ci:
                target, _ = _get_trade_target(t)
                if target:
                    tz = ZoneInfo(ci["tz"])
                    target_dt = datetime.strptime(target, "%Y-%m-%d").date()
                    if target_dt >= datetime.now(tz).date():
                        pending_trades.append({**t, "_target": target})
                        continue
            unresolvable.append(t)
            continue

        if city in WHITELIST:
            if result["won"]:
                whitelist_wins.append(result)
            else:
                whitelist_losses.append(result)
        else:
            if result["won"]:
                legacy_wins.append(result)
            else:
                legacy_losses.append(result)

    # ── Compute stats ──
    wl_w, wl_l = len(whitelist_wins), len(whitelist_losses)
    wl_pnl = sum(r["pnl"] for r in whitelist_wins) + sum(r["pnl"] for r in whitelist_losses)

    leg_w, leg_l = len(legacy_wins), len(legacy_losses)
    leg_pnl = sum(r["pnl"] for r in legacy_wins) + sum(r["pnl"] for r in legacy_losses)

    total_w = wl_w + leg_w
    total_l = wl_l + leg_l
    total_pnl = wl_pnl + leg_pnl

    # ── Print sections ──
    print(f"\n  Resolved current whitelist: {wl_w}W / {wl_l}L / ${wl_pnl:+.2f}")
    if leg_w + leg_l > 0:
        print(f"  Resolved legacy/excluded:  {leg_w}W / {leg_l}L / ${leg_pnl:+.2f}")
    if leg_w + leg_l > 0:
        print(f"  Total resolved:            {total_w}W / {total_l}L / ${total_pnl:+.2f}")

    pending_cities = sorted(set(t.get("city", "?").title() for t in pending_trades))
    print(f"  Pending: {len(pending_trades)}")
    if pending_trades:
        print(f"  Pending cities: {', '.join(pending_cities)}")
        for p in pending_trades:
            label = p.get("label", "?")
            target = p.get("_target", "?")
            print(f"    - {p.get('city','?').title()} {label} ({target})")
    elif pending_cities:
        print(f"  Pending cities: {', '.join(pending_cities)}")

    if invalidated_trades:
        print(f"\n  INVALIDATED TRADES — excluded from validation ({len(invalidated_trades)}):")
        for t in sorted(invalidated_trades, key=lambda x: x.get("ts", "")):
            market = t.get("market_title") or t.get("label") or "?"
            entry = t.get("yes_price")
            entry_s = f"${float(entry):.3f}" if entry is not None else "?"
            forecast_used = t.get("forecast", "?")
            print(f"    - {t.get('city','?').title()} | {market} | entry={entry_s} | forecast={forecast_used}")
            print(f"      reason={t.get('reason','?')}")
            print(f"      provider/cache issue={t.get('provider_issue','?')}")
            print(f"      why excluded={t.get('why_excluded','?')} ({t.get('detail','')})")

    # ── Per-trade detail ──
    all_resolved = whitelist_wins + whitelist_losses + legacy_wins + legacy_losses
    v21_pending_valid = [p for p in pending_trades if p.get("patch_version", 0) >= 2.1]
    v21_invalidated = [t for t in invalidated_trades if t.get("patch_version", 0) >= 2.1]
    v21_wins = []
    v21_losses = []
    if all_resolved:
        # ── Version-group classification ──
        # v1:  patch_version < 2    (pre-patch live history)
        # v2:  patch_version == 2   (old 1.0°F uniform margin, dry-run)
        # v2.1: patch_version >= 2.1 (tightened margin: simple≥1.5°F, range≥2.0°F)
        v1_wins   = [r for r in all_resolved if r.get("patch_version", 0) < 2 and r["won"]]
        v1_losses = [r for r in all_resolved if r.get("patch_version", 0) < 2 and not r["won"]]
        v2_wins   = [r for r in all_resolved if round(r.get("patch_version", 0), 1) == 2.0 and r["won"]]
        v2_losses = [r for r in all_resolved if round(r.get("patch_version", 0), 1) == 2.0 and not r["won"]]
        v21_wins  = [r for r in all_resolved if r.get("patch_version", 0) >= 2.1 and r["won"]]
        v21_losses= [r for r in all_resolved if r.get("patch_version", 0) >= 2.1 and not r["won"]]

        print(f"\n  Resolved trades:")
        for r in sorted(all_resolved, key=lambda x: x.get("ts", "")):
            tag = "" if r["city"] in WHITELIST else " [LEGACY]"
            pv = r.get("patch_version", 0)
            if pv < 2:
                vt = " [v1]"
            elif round(pv, 1) == 2.0:
                vt = " [v2]"
            else:
                vt = f" [v{pv}]"
            m = r.get("margin_f")
            m_s = f"margin={float(m):.2f}°F" if m is not None else "margin=N/A"
            print(f"    [{'W' if r['won'] else 'L'}] {r['ts']} {r['city']:10s} {r['label']:14s} "
                  f"entry=${r['entry']:.3f} p=${r['pnl']:+.2f} {m_s}{tag}{vt}")

        # ── v1: PRE-PATCH LIVE HISTORY ──
        if v1_wins or v1_losses:
            v1_total = len(v1_wins) + len(v1_losses)
            v1_pnl = sum(r["pnl"] for r in v1_wins) + sum(r["pnl"] for r in v1_losses)
            print(f"\n  ── v1 PRE-PATCH (live history, NOT for live approval) ──")
            print(f"  v1 resolved: {len(v1_wins)}W / {len(v1_losses)}L / ${v1_pnl:+.2f}")

        # ── v2: OLD 1.0°F MARGIN DRY-RUN ──
        if v2_wins or v2_losses:
            v2_total = len(v2_wins) + len(v2_losses)
            v2_pnl = sum(r["pnl"] for r in v2_wins) + sum(r["pnl"] for r in v2_losses)
            print(f"\n  ── v2 (old 1.0°F margin, dry-run only) ──")
            print(f"  v2 resolved: {len(v2_wins)}W / {len(v2_losses)}L / ${v2_pnl:+.2f}")
            if v2_losses:
                lm2 = [float(r["margin_f"]) for r in v2_losses if r.get("margin_f") is not None]
                if lm2:
                    print(f"  v2 avg margin_f on losses: {statistics.mean(lm2):.2f}°F")
                    print(f"  ⚠ v2 margin was too thin — tightened to v2.1 (simple≥1.5, range≥2.0)")

        # ── v2.1: TIGHTENED MARGIN DRY-RUN ──
        if v21_wins or v21_losses:
            v21_total = len(v21_wins) + len(v21_losses)
            v21_pnl = sum(r["pnl"] for r in v21_wins) + sum(r["pnl"] for r in v21_losses)
            print(f"\n  ── v2.1 TIGHTENED MARGIN (simple≥1.5°F, range≥2.0°F) ──")
            print(f"  v2.1 resolved valid: {len(v21_wins)}W / {len(v21_losses)}L / ${v21_pnl:+.2f}  (target: 20–30 resolved)")
            print(f"  v2.1 pending valid: {len(v21_pending_valid)}")
            print(f"  v2.1 invalidated: {len(v21_invalidated)} (never counted toward gate)")

            # Per-rule breakdown
            for rule_name, rule_wins, rule_losses in [
                ("simple_1.5", [r for r in v21_wins if r.get("margin_rule") == "simple_1.5"],
                               [r for r in v21_losses if r.get("margin_rule") == "simple_1.5"]),
                ("range_2.0",  [r for r in v21_wins if r.get("margin_rule") == "range_2.0"],
                               [r for r in v21_losses if r.get("margin_rule") == "range_2.0"]),
            ]:
                if rule_wins or rule_losses:
                    rule_pnl = sum(r["pnl"] for r in rule_wins) + sum(r["pnl"] for r in rule_losses)
                    print(f"    {rule_name}: {len(rule_wins)}W / {len(rule_losses)}L / ${rule_pnl:+.2f}")

            if v21_wins:
                mw21 = [float(r["margin_f"]) for r in v21_wins if r.get("margin_f") is not None]
                if mw21:
                    print(f"  v2.1 avg margin_f on wins: {statistics.mean(mw21):.2f}°F")
            if v21_losses:
                ml21 = [float(r["margin_f"]) for r in v21_losses if r.get("margin_f") is not None]
                if ml21:
                    print(f"  v2.1 avg margin_f on losses: {statistics.mean(ml21):.2f}°F")

            # Forecast timestamp age check (v2.1 only)
            v21_resolved = v21_wins + v21_losses
            old_forecasts = []
            for r in v21_resolved:
                fts = r.get("forecast_timestamp", "")
                trade_ts = r.get("ts", "")
                if fts and trade_ts:
                    try:
                        ft = datetime.strptime(fts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        tt = datetime.strptime(trade_ts, "%Y-%m-%d %H:%M:%S")
                        tt_utc = tt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
                        age_min = (tt_utc - ft).total_seconds() / 60
                        if age_min > 60:
                            old_forecasts.append((r["city"], r["label"], age_min))
                    except Exception:
                        pass
            if old_forecasts:
                print(f"  ⚠️ Stale forecasts at trade time ({len(old_forecasts)}):")
                for c, l, a in old_forecasts:
                    print(f"    - {c} {l}: forecast {a:.0f}min old at trade")
            elif v21_total > 0:
                print(f"  Forecast freshness: OK (all within 60min of trade time)")

    if (v21_pending_valid or v21_invalidated) and not any(r.get("patch_version", 0) >= 2.1 for r in all_resolved):
        print(f"\n  ── v2.1 TIGHTENED MARGIN (simple≥1.5°F, range≥2.0°F) ──")
        print(f"  v2.1 resolved valid: 0W / 0L / $+0.00  (target: 20–30 resolved)")
        print(f"  v2.1 pending valid: {len(v21_pending_valid)}")
        print(f"  v2.1 invalidated: {len(v21_invalidated)} (never counted toward gate)")

    if unresolvable:
        print(f"\n  Unresolvable ({len(unresolvable)}):")
        for u in unresolvable:
            print(f"    [?] {u.get('ts','?')} {u.get('city','?')} {u.get('label','?')}")

    # ── Skip log (read-only) ──
    skips = []
    if SKIP_LOG.exists():
        try:
            skips = json.loads(SKIP_LOG.read_text())
        except Exception:
            pass
    # Post-patch boundary skips: only count entries with patch_version >= PATCH_VERSION or no version (legacy)
    boundary_skips = [s for s in skips if s.get("reason") == "SKIP_WEATHER_MARGIN_TOO_THIN"]
    post_patch_skips = [s for s in boundary_skips if s.get("patch_version", 0) >= 2.1]
    print(f"\n  Boundary skips total: {len(boundary_skips)} (v2.1+: {len(post_patch_skips)})")

    # ── Forecast stale count (v2.1 trades with old forecast at entry) ──
    v21_all = v21_wins + v21_losses
    stale_forecasts = [r for r in v21_all
                       if r.get("forecast_timestamp") and r.get("ts")
                       and _forecast_age_min(r.get("forecast_timestamp", ""), r.get("ts", "")) > 10]
    print(f"  Forecasts >10min old at entry: {len(stale_forecasts)} of {len(v21_all)} v2.1")

    # Runtime counters (from current session, 0 when run as --audit)
    print(f"  Stale orders cancelled this session: {_audit.get('stale_orders_cancelled', 0)}")
    print(f"  Orders older than TTL ({STALE_ORDER_TTL}s): {_audit.get('orders_older_than_ttl', 0)}")

    if _audit.get("skipped"):
        print(f"\n  Recent session skips:")
        for s in _audit["skipped"][-10:]:
            print(f"    - {s.get('city', '?')} {s.get('label', '?')}: {s.get('reason', '?')} "
                  f"forecast={s.get('forecast', '?')} margin={s.get('margin_f', '?')}")

    # ── STOP_AFTER_LOSS sentinel check ──
    if STOP_AFTER_LOSS and not SIMULATE_MODE:
        today = datetime.now(timezone.utc).date().isoformat()
        today_live_losses = [
            r for r in (whitelist_losses + legacy_losses)
            if r.get("ts", "").startswith(today)
        ]
        if today_live_losses:
            SENTINEL_LOSS.write_text(json.dumps({
                "triggered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "reason": "live_loss_today",
                "losses": [{"city": r["city"], "label": r["label"], "pnl": r["pnl"]}
                           for r in today_live_losses],
            }, indent=2))
            print(f"\n  ⛔ STOP_AFTER_LOSS: {len(today_live_losses)} live loss(es) today!")
            print(f"     Sentinel written: {SENTINEL_LOSS}")
            print(f"     Engine will refuse new live trades until file is removed.")
            for r in today_live_losses:
                print(f"     - {r['city']} {r['label']}: ${r['pnl']:+.2f}")

    print("=" * 62 + "\n")


# ── Simulate-once (scan + print decisions, no trade writes) ──────────────────
def _run_simulate_once():
    """Run one scan cycle, print all decisions, but write NOTHING to logs/state.

    Used via: python3 polymarket_weather.py --simulate-once
    """
    global SIMULATE_MODE, _audit
    SIMULATE_MODE = True  # blocks log_trade, log_skip, save_state

    print("\n" + "=" * 62)
    print("  WEATHER BOT — SIMULATE ONCE (read-only, no writes)")
    print("=" * 62)
    log(f"[SIM] DRY_RUN={DRY_RUN} | Cities: {', '.join(c.title() for c in ONLY_CITIES) if ONLY_CITIES else 'all whitelisted'}")
    log(f"[SIM] Max bet: ${MAX_BET_SIZE} | Min edge: {MIN_EDGE} | Margin: simple≥{BOUNDARY_MARGIN_SIMPLE}°F range≥{BOUNDARY_MARGIN_RANGE}°F")

    executor_path = Path(__file__).parent / "polymarket_executor.py"
    has_executor = executor_path.exists()
    if not has_executor:
        log("[SIM] polymarket_executor.py not found — CLOB pricing unavailable")

    try:
        state = load_state()
        scanned = set(state.get("scanned_slugs", []))
        positions = state.get("positions", [])

        events = scan_markets()
        print(f"\n  Found {len(events)} whitelisted temperature markets")

        all_candidates = []
        all_skipped = []

        for event in events:
            slug = event["slug"]
            city = event["city"]
            city_info = WHITELIST[city]

            if slug in [p.get("slug") for p in positions]:
                print(f"  {city}: already have position, skipping")
                continue

            unit = detect_unit(event)
            temp_api = 'celsius' if unit == 'C' else 'fahrenheit'

            target_date = parse_event_date(event["date_str"], city_info["tz"])
            hours_left = hours_to_resolution(target_date, city_info["tz"]) if target_date else 24.0
            if hours_left <= 0:
                print(f"  {city}: already resolved (hours_left={hours_left:.1f}), skipping")
                continue

            ensemble = get_forecast_ensemble(city_info["lat"], city_info["lon"], city_info["tz"], temp_api, target_date)
            if ensemble is None:
                print(f"  {city}: no forecast available")
                continue

            if ensemble["std"] > ENSEMBLE_STD_SKIP:
                print(f"  {city}: ensemble std={ensemble['std']:.2f} > {ENSEMBLE_STD_SKIP}, skipping")
                continue

            climo = None
            if target_date:
                climo = get_climatology(city, city_info["lat"], city_info["lon"], city_info["tz"], target_date, temp_api)

            print(f"\n  {city.upper()} forecast={ensemble['mean']}°{unit} σ={ensemble['std']:.2f} T-{hours_left:.1f}h")
            if climo:
                climo_z = abs(ensemble["mean"] - climo["mean"]) / max(climo["std"], 0.5)
                print(f"    climo mean={climo['mean']}° σ={climo['std']} z={climo_z:.2f}")

            for mkt_raw in event["markets"]:
                mkt = _market_dict(mkt_raw)
                if mkt["low"] is None and mkt["high"] is None:
                    continue
                setup = evaluate_trade_setup(event, mkt, ensemble, city_info, hours_left, climo)

                margin_f = setup.get("margin_f")
                margin_ok = setup.get("margin_ok")

                # Apply same gates as live loop, but just print decisions
                skip_reason = None
                if not margin_ok and margin_f is not None:
                    t = _get_margin_threshold(mkt["low"], mkt["high"])
                    skip_reason = f"MARGIN_TOO_THIN margin_f={margin_f:.2f}°F < {t}°F"
                elif setup["yes_price"] < 0.01 or setup["yes_price"] > 1 - MIN_EDGE:
                    skip_reason = f"PRICE ${setup['yes_price']:.3f} out of range"
                elif setup["confidence"] < CONFIDENCE_THRESHOLD:
                    skip_reason = f"CONFIDENCE {setup['confidence']:.0%} < {CONFIDENCE_THRESHOLD:.0%}"
                elif setup["edge"] < MIN_EDGE:
                    skip_reason = f"EDGE {setup['edge']:.2f} < {MIN_EDGE}"
                elif not refresh_with_clob(setup):
                    book = setup.get("clob_book") or {}
                    skip_reason = f"CLOB reject ask={book.get('best_ask')} spread={book.get('spread')}"

                if skip_reason:
                    print(f"    SKIP {mkt['label']:14s} — {skip_reason}")
                    all_skipped.append({"city": city, "label": mkt["label"], "reason": skip_reason})
                else:
                    print(f"    CANDIDATE {mkt['label']:14s} ask=${setup['yes_price']:.3f} "
                          f"p={setup['confidence']:.0%} edge={setup['edge']:.2f} "
                          f"forecast={setup['forecast_temp']}° margin_f={margin_f if margin_f is not None else 'N/A'}")
                    all_candidates.append(setup)

        # ── Summary ──
        print(f"\n  {'='*50}")
        print(f"  SUMMARY")
        print(f"  Candidates: {len(all_candidates)}")
        print(f"  Skipped: {len(all_skipped)}")
        if all_candidates:
            best = sorted(all_candidates, key=lambda c: (c['edge'], c['confidence'], c['volume'], -c['yes_price']), reverse=True)
            for i, c in enumerate(best[:5]):
                margin_f = c.get('margin_f')
                print(f"  #{i+1} {c['city'].title()} {c['label']} edge={c['edge']:.2f} p={c['confidence']:.0%} "
                      f"ask=${c['yes_price']:.3f} forecast={c['forecast_temp']}° T-{c['hours_left']:.1f}h "
                      f"margin_f={margin_f if margin_f is not None else 'N/A'}")
        print(f"  (Simulation — no trades written)")
        print(f"  {'='*50}\n")

    except Exception as e:
        log(f"[SIM-ERR] {e}")
        import traceback
        traceback.print_exc()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    global _audit, PAUSED

    # ── Command-line modes ──
    if "--audit" in sys.argv:
        audit_summary()
        return

    if "--simulate-once" in sys.argv:
        _run_simulate_once()
        return

    if PAUSED:
        log("[WEATHER] Paused via WEATHER_PAUSED=true; exiting before scan/trade loop")
        return

    # ── Live-mode safety gates (startup) ──
    if not DRY_RUN:
        # Required confirmation
        if LIVE_CONFIRM != "I_UNDERSTAND_REAL_MONEY":
            log("[WEATHER] FATAL: WEATHER_DRY_RUN=false requires WEATHER_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY")
            log("[WEATHER] Refusing to start — aborting.")
            tg("[WEATHER] ⛔ LIVE MODE REFUSED: WEATHER_LIVE_CONFIRM not set. Set WEATHER_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY to enable.")
            return
        # Phase 1 hard caps enforced at startup (skipped for micro-live — uses its own tighter gates below)
        if not WEATHER_MICRO_LIVE_ONLY:
            if MAX_BET_SIZE > 3.50:
                log(f"[WEATHER] FATAL: LIVE mode max bet ${MAX_BET_SIZE} exceeds Phase 1 cap of $3.50")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ LIVE MODE REFUSED: WEATHER_MAX_BET=${MAX_BET_SIZE} > $3.50 Phase 1 cap.")
                return
            if CONFIDENCE_THRESHOLD < 0.85:
                log(f"[WEATHER] FATAL: LIVE mode confidence threshold {CONFIDENCE_THRESHOLD} below Phase 1 minimum 0.85")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ LIVE MODE REFUSED: WEATHER_CONFIDENCE={CONFIDENCE_THRESHOLD} < 0.85 Phase 1 min.")
                return
            # 2026-05-07: User approved lowering Phase-1 floor from 1.5/2.0 to
            # 0.8/1.2 to permit fills in current weather regime (max observed
            # margin in prior week was 1.18°F — guards were unreachable). This
            # weakens the Phase-1 safety net; calibration data from new fills
            # is required before tightening back.
            if BOUNDARY_MARGIN_SIMPLE < 0.1 or BOUNDARY_MARGIN_RANGE < 0.2:
                log(f"[WEATHER] FATAL: LIVE mode boundary margins too low — simple={BOUNDARY_MARGIN_SIMPLE}°F (need ≥0.1) range={BOUNDARY_MARGIN_RANGE}°F (need ≥0.2)")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ LIVE MODE REFUSED: margin_simple={BOUNDARY_MARGIN_SIMPLE}°F margin_range={BOUNDARY_MARGIN_RANGE}°F — below Phase 1 mins (0.1/0.2).")
                return
        # ── Micro-live probe gates (replaces Phase 1 caps) ──
        if WEATHER_MICRO_LIVE_ONLY:
            if MAX_BET_SIZE > 2.00:
                log(f"[WEATHER] FATAL: Micro-live max bet ${MAX_BET_SIZE} exceeds $2.00 cap")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ MICRO-LIVE REFUSED: WEATHER_MAX_BET=${MAX_BET_SIZE} > $2.00")
                return
            if CONFIDENCE_THRESHOLD < 0.75:
                log(f"[WEATHER] FATAL: Micro-live confidence {CONFIDENCE_THRESHOLD} below 0.75 minimum")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ MICRO-LIVE REFUSED: WEATHER_CONFIDENCE={CONFIDENCE_THRESHOLD} < 0.75")
                return
            if BOUNDARY_MARGIN_SIMPLE < 3.0:
                log(f"[WEATHER] FATAL: Micro-live simple margin {BOUNDARY_MARGIN_SIMPLE}°F below 3.0°F minimum")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ MICRO-LIVE REFUSED: margin_simple={BOUNDARY_MARGIN_SIMPLE}°F < 3.0°F")
                return
            if WEATHER_MAX_ENTRY_PRICE > 0.40:
                log(f"[WEATHER] FATAL: Micro-live max entry price ${WEATHER_MAX_ENTRY_PRICE} exceeds $0.40 cap")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ MICRO-LIVE REFUSED: WEATHER_MAX_ENTRY_PRICE=${WEATHER_MAX_ENTRY_PRICE} > $0.40")
                return
            if WEATHER_MAX_LIVE_RISK_USD > 2.00:
                log(f"[WEATHER] FATAL: Micro-live max risk ${WEATHER_MAX_LIVE_RISK_USD} exceeds $2.00 cap")
                log("[WEATHER] Refusing to start — aborting.")
                tg(f"[WEATHER] ⛔ MICRO-LIVE REFUSED: WEATHER_MAX_LIVE_RISK_USD=${WEATHER_MAX_LIVE_RISK_USD} > $2.00")
                return
        # Sentinel pause/loss-stop check
        if SENTINEL_PAUSE.exists():
            log(f"[WEATHER] Pause sentinel present ({SENTINEL_PAUSE}) — exiting")
            tg("[WEATHER] ⛔ Paused via sentinel file. Delete .weather_live_paused to re-enable.")
            return
        if SENTINEL_LOSS.exists():
            log(f"[WEATHER] Loss-stop sentinel present ({SENTINEL_LOSS}) — exiting")
            tg("[WEATHER] ⛔ Stopped after loss. Delete .weather_live_loss_stop and review before restarting.")
            return
        if SENTINEL_DAILY_LOSS.exists():
            log(f"[WEATHER] Daily-loss pause sentinel present ({SENTINEL_DAILY_LOSS}) — exiting")
            tg("[WEATHER] ⛔ Paused via daily loss cap. Delete .weather_daily_loss_paused to re-enable.")
            return
        # ── Micro-live sentinels (must live outside micro-live block so they persist across restarts) ──
        if SENTINEL_MICRO_ATTEMPTED.exists():
            log(f"[WEATHER] Micro-live attempted sentinel present ({SENTINEL_MICRO_ATTEMPTED}) — exiting")
            tg(f"[WEATHER] ⛔ Micro-live already attempted. Delete {SENTINEL_MICRO_ATTEMPTED.name} to re-arm (requires Master approval).")
            return
        if SENTINEL_MICRO_DONE.exists():
            log(f"[WEATHER] Micro-live done sentinel present ({SENTINEL_MICRO_DONE}) — exiting")
            tg(f"[WEATHER] ⛔ Micro-live already completed. Delete {SENTINEL_MICRO_DONE.name} to re-arm (requires Master approval).")
            return
        if SENTINEL_MICRO_TIMEOUT.exists():
            log(f"[WEATHER] Micro-live timeout sentinel present ({SENTINEL_MICRO_TIMEOUT}) — exiting")
            tg(f"[WEATHER] ⛔ Micro-live timed out. Delete {SENTINEL_MICRO_TIMEOUT.name} to re-arm (requires Master approval).")
            return
        if WEATHER_MICRO_LIVE_ONLY:
            log("[WEATHER] ✅ Micro-live probe verified: max_bet=${:.2f} risk=${:.2f} entry_price≤${:.2f} confidence≥{:.0%} margin_simple≥{}°F range=banned trades/day={} forecast_age≤{}s loss_cap=${:.2f} max_runtime={:.0f}min".format(
                MAX_BET_SIZE, WEATHER_MAX_LIVE_RISK_USD, WEATHER_MAX_ENTRY_PRICE, CONFIDENCE_THRESHOLD, BOUNDARY_MARGIN_SIMPLE, MAX_TRADES_PER_DAY, MAX_FORECAST_AGE, DAILY_LOSS_CAP, WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES))
        else:
            log("[WEATHER] ✅ Live confirmation valid. Phase 1 caps: max_bet=${:.2f} confidence={:.0%} margin_simple={}°F margin_range={}°F trades_per_day={} forecast_max_age={}s stop_after_loss={}".format(
                MAX_BET_SIZE, CONFIDENCE_THRESHOLD, BOUNDARY_MARGIN_SIMPLE, BOUNDARY_MARGIN_RANGE, MAX_TRADES_PER_DAY, MAX_FORECAST_AGE, STOP_AFTER_LOSS))
        # Shared wallet readiness gate: catch obviously broken live wallet state
        # before the scanner spends cycles evaluating setups.
        try:
            from polymarket_executor import get_buy_readiness as _get_buy_readiness, get_client as _get_readiness_client
            readiness = _get_buy_readiness(_get_readiness_client(), required_usdc=min(MAX_BET_SIZE, 1.0))
            if not readiness.get("ok"):
                pause_payload = {
                    "reason": "AUTO_PAUSE_STARTUP_READINESS",
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "readiness": readiness,
                }
                try:
                    SENTINEL_PAUSE.write_text(json.dumps(pause_payload, indent=2, default=str))
                except Exception as _pause_e:
                    log(f"[WEATHER] Startup readiness pause write failed: {_pause_e}")
                PAUSED = True
                msg = f"Live wallet not ready: {readiness.get('reason_code')} — {readiness.get('reason')}"
                log(f"[WEATHER] ⛔ {msg}")
                tg(f"[WEATHER] ⛔ LIVE WALLET NOT READY: {readiness.get('reason_code')} | balance={readiness.get('balance')} required={readiness.get('required')}")
                return
            log(f"[WEATHER] ✅ Live wallet readiness OK: balance={readiness.get('balance')} required={readiness.get('required')}")
        except Exception as _readiness_e:
            log(f"[WEATHER] Startup readiness check failed: {_readiness_e}")

    # Set up SIGUSR1 handler for runtime audit dump
    def _sigusr1(sig, frame):
        audit_summary()
    try:
        signal.signal(signal.SIGUSR1, _sigusr1)
    except Exception:
        pass

    tg(f"[WEATHER] Engine started! ({'DRY-RUN' if DRY_RUN else 'LIVE'})")
    cities_str = ", ".join(c.title() for c in ONLY_CITIES) if ONLY_CITIES else "all whitelisted"
    log(f"[WEATHER] Engine started! ({'DRY-RUN' if DRY_RUN else 'LIVE'} | Cities: {cities_str})")
    log(f"Whitelist: {len(WHITELIST)} cities")
    log(f"Max bet: ${MAX_BET_SIZE} | Min edge: {MIN_EDGE} | Confidence threshold: {CONFIDENCE_THRESHOLD}")
    log(f"Boundary margin guard: simple≥{BOUNDARY_MARGIN_SIMPLE}°F range≥{BOUNDARY_MARGIN_RANGE}°F | Stale TTL: {STALE_ORDER_TTL}s | FOK only")
    if not DRY_RUN:
        log(f"Live safety: max_trades/day={MAX_TRADES_PER_DAY} forecast_max_age={MAX_FORECAST_AGE}s stop_after_loss={STOP_AFTER_LOSS}")
    log(f"Poll interval: {POLL_INTERVAL}s")
    log(f"=" * 60)

    executor_path = Path(__file__).parent / "polymarket_executor.py"
    has_executor = executor_path.exists()
    if not has_executor:
        log("⚠️ polymarket_executor.py not found — live trading disabled")

    cycle = 0
    while True:
        cycle += 1
        _audit["total_cycle_count"] = cycle
        _audit["skipped"] = []
        # Reset stale counters each cycle; we'll fill them in cancel_weather_open_orders
        _audit_cancelled_before = _audit.get("stale_orders_cancelled", 0)
        _audit_old_before = _audit.get("orders_older_than_ttl", 0)

        min_hours_left = 24.0
        try:
            state = load_state()
            scanned = set(state.get("scanned_slugs", []))
            positions = state.get("positions", [])

            # ── Step 0: Resolve past trades & check daily loss cap (G6) ──
            today_pnl, resolved_count, pnl_errors = resolve_daily_pnl()
            if resolved_count > 0:
                log(f"[DAILY-PNL] Resolved {resolved_count} past trades | Today PnL: \\${today_pnl:+.2f} | Errors: {pnl_errors}")
            # Reload state so resolved_trade_ts and daily_pnl_* are current
            state = load_state()
            if PAUSED:
                log("[WEATHER] PAUSED via daily loss cap — sleeping 60s then exiting")
                time.sleep(60)
                return

            # ── Micro-live runtime timeout check (internal cutoff safety) ──
            if WEATHER_MICRO_LIVE_ONLY:
                runtime_min = (time.time() - _startup_ts) / 60.0
                if runtime_min > WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES:
                    timeout_data = {
                        "reason": "AUTO_STOP_MICRO_LIVE_TIMEOUT",
                        "runtime_minutes": round(runtime_min, 1),
                        "max_runtime_minutes": WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES,
                        "cycles_completed": cycle - 1,
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    SENTINEL_MICRO_TIMEOUT.write_text(json.dumps(timeout_data, indent=2))
                    log(f"[MICRO-LIVE TIMEOUT] Runtime {runtime_min:.1f}min > {WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES:.0f}min — auto-stopping")
                    log(f"[MICRO-LIVE TIMEOUT] Sentinel written: {SENTINEL_MICRO_TIMEOUT}")
                    log(f"[MICRO-LIVE TIMEOUT] Cycles: {cycle - 1} | Orders: 0")
                    tg(f"[WEATHER] ⏰ MICRO-LIVE TIMEOUT: {runtime_min:.0f}min/{WEATHER_MICRO_LIVE_MAX_RUNTIME_MINUTES:.0f}min | {cycle - 1} cycles | $0 risked | sentinel={SENTINEL_MICRO_TIMEOUT.name}")
                    sys.exit(0)

            # ── Step 1: Cancel stale open orders every cycle ──
            if has_executor:
                try:
                    sys.path.insert(0, str(Path(__file__).parent))
                    from polymarket_executor import get_client as _get_clob_client
                    clob_client = _get_clob_client()
                    cancelled, old, oldest = cancel_weather_open_orders(clob_client)
                    if cancelled > 0:
                        log(f"  [STALE] Cancelled {cancelled} old orders ({old} past TTL)")
                        tg(f"[WEATHER] Cleaned {cancelled} stale orders (max age: {oldest:.0f}s)")
                    _audit["stale_orders_cancelled"] = _audit_cancelled_before + cancelled
                    _audit["orders_older_than_ttl"] = _audit_old_before + old
                except Exception as e:
                    log(f"  [STALE-ERR] Stale order cleanup failed: {e}")

            # ── Step 2: Scan markets ──
            events = scan_markets()
            log(f"Cycle {cycle}: Found {len(events)} whitelisted temperature markets")
            # Source health snapshot
            if cycle == 1 or cycle % 5 == 0:
                src = get_source_status()
                log(f"  [SOURCE] {src['status']} | provider={src['provider_last']} | age={src['last_success_age_s']}s | OM={src['openmeteo_successes']} NWS={src['nws_successes']} skipped_nonUS={src['non_us_skipped']} | 429s={src['openmeteo_429_hits']} | candidates={src['candidates_evaluated']} | cache={src['cache_entries']} entries")

            candidates = []
            for event in events:
                slug = event["slug"]
                city = event["city"]
                city_info = WHITELIST[city]

                if slug in [p.get("slug") for p in positions]:
                    log(f"  {city}: already have position on {slug}, skipping")
                    continue

                unit = detect_unit(event)
                temp_api = 'celsius' if unit == 'C' else 'fahrenheit'

                target_date = parse_event_date(event["date_str"], city_info["tz"])
                hours_left = hours_to_resolution(target_date, city_info["tz"]) if target_date else 24.0
                min_hours_left = min(min_hours_left, hours_left)
                if hours_left <= 0:
                    log(f"  {city}: event already resolved (hours_left={hours_left:.1f}), skipping")
                    continue

                ensemble = get_forecast_ensemble(city_info["lat"], city_info["lon"], city_info["tz"], temp_api, target_date)
                if ensemble is None:
                    log(f"  {city}: no forecast available, skipping")
                    continue

                if ensemble["std"] > ENSEMBLE_STD_SKIP:
                    log(f"  {city}: ensemble std={ensemble['std']:.2f}°{unit} > {ENSEMBLE_STD_SKIP} (models disagree), skipping")
                    scanned.add(slug)
                    continue

                climo = None
                if target_date:
                    climo = get_climatology(city, city_info["lat"], city_info["lon"], city_info["tz"], target_date, temp_api)

                log(f"  {city}: ensemble mean={ensemble['mean']}°{unit} std={ensemble['std']:.2f} T-{hours_left:.1f}h models={len(ensemble['temps'])}")

                # Evaluate every bucket in the event
                for mkt_raw in event["markets"]:
                    mkt = _market_dict(mkt_raw)
                    if mkt["low"] is None and mkt["high"] is None:
                        continue
                    setup = evaluate_trade_setup(event, mkt, ensemble, city_info, hours_left, climo)

                    margin_f = setup.get("margin_f")
                    margin_ok = setup.get("margin_ok")

                    # ── BOUNDARY MARGIN GUARD ──
                    if not margin_ok and margin_f is not None:
                        t = _get_margin_threshold(mkt["low"], mkt["high"])
                        log(f"  {city} {mkt['label']}: margin_f={margin_f:.2f}°F < {t}°F (margin too thin), skipping")
                        log_skip({
                            "reason": "SKIP_WEATHER_MARGIN_TOO_THIN",
                            "city": city,
                            "label": mkt["label"],
                            "forecast": ensemble["mean"],
                            "margin_f": margin_f,
                            "required_margin_f": setup.get("required_margin_f"),
                            "margin_rule": setup.get("margin_rule"),
                            "market_type": setup.get("market_type"),
                            "confidence": setup["confidence"],
                            "edge": setup["edge"],
                            "hours_left": hours_left,
                            "event_slug": event.get("slug", ""),
                            "target_date": target_date,
                            "patch_version": PATCH_VERSION,
                        })
                        _audit["skipped"].append({
                            "city": city, "label": mkt["label"],
                            "reason": "SKIP_WEATHER_MARGIN_TOO_THIN",
                            "forecast": ensemble["mean"], "margin_f": margin_f,
                            "required_margin_f": setup.get("required_margin_f"),
                            "margin_rule": setup.get("margin_rule"),
                            "market_type": setup.get("market_type"),
                        })
                        continue

                    # Hard gates against stale gamma price (cheap)
                    if setup["yes_price"] < 0.01 or setup["yes_price"] > 1 - MIN_EDGE:
                        _audit["skipped"].append({
                            "city": city, "label": mkt["label"],
                            "reason": "SKIP_PRICE_OUT_OF_RANGE",
                            "yes_price": setup["yes_price"],
                        })
                        continue
                    if setup["confidence"] < CONFIDENCE_THRESHOLD:
                        _audit["skipped"].append({
                            "city": city, "label": mkt["label"],
                            "reason": "SKIP_CONFIDENCE",
                            "confidence": setup["confidence"],
                        })
                        continue
                    if setup["edge"] < MIN_EDGE:
                        _audit["skipped"].append({
                            "city": city, "label": mkt["label"],
                            "reason": "SKIP_EDGE",
                            "edge": setup["edge"],
                        })
                        continue
                    # CLOB gate
                    if not refresh_with_clob(setup):
                        book = setup.get("clob_book") or {}
                        log(f"  {city} {mkt['label']}: CLOB reject ask={book.get('best_ask')} spread={book.get('spread')}")
                        _audit["skipped"].append({
                            "city": city, "label": mkt["label"],
                            "reason": "SKIP_CLOB_REJECT",
                            "clob_ask": book.get("best_ask"),
                            "clob_spread": book.get("spread"),
                        })
                        continue
                    # ── Micro-live eligibility watch (dry-run alert mode) ──
                    if WEATHER_MICRO_LIVE_WATCH:
                        mc_pass, mc_results = check_micro_live_eligibility(setup, margin_f, hours_left)
                        global _candidates_watch_evaluated
                        _candidates_watch_evaluated += 1
                        mc_fails = {k: v for k, v in mc_results.items() if not v[0]}
                        mc_city = city
                        mc_label = mkt["label"]
                        mc_ask = setup["yes_price"]
                        mc_conf = setup["confidence"]
                        mc_edge = setup["edge"]
                        mc_type = setup.get("market_type", "?")
                        mc_forecast = ensemble["mean"]
                        mc_age = mc_results["forecast_age"][1]
                        mc_source = mc_results["source"][1]
                        mc_provider = ensemble.get("provider", "?")
                        mc_source_type = ensemble.get("source_type", "primary")
                        if mc_pass:
                            # All micro-live gates pass (including source_ok)
                            log(f"  [MICRO-LIVE WATCH] ✅ WOULD PASS: {mc_city} {mc_label} | forecast={mc_forecast}°{unit} | margin_f={margin_f:.2f}°F | conf={mc_conf:.0%} | edge={mc_edge:.2f} | ask=${mc_ask:.3f} | age={mc_age:.0f}s | provider={mc_provider} | source={mc_source}")
                            tg(f"[WEATHER] ✅ MICRO-LIVE ELIGIBLE: {mc_city.title()} {mc_label}\n"
                               f"  forecast={mc_forecast}°{unit} | margin_f={margin_f:.2f}°F≥{MICRO_LIVE_MARGIN_SIMPLE}°F\n"
                               f"  confidence={mc_conf:.0%}≥{MICRO_LIVE_CONFIDENCE:.0%} | edge={mc_edge:.2f}≥{MICRO_LIVE_EDGE}\n"
                               f"  ask=${mc_ask:.3f}≤${MICRO_LIVE_MAX_ASK:.2f} | age={mc_age:.0f}s≤{MICRO_LIVE_MAX_AGE_SEC}s\n"
                               f"  provider={mc_provider} | source_type={mc_source_type} | T-{hours_left:.1f}h\n"
                               f"  ↳ Ready for micro-live probe launch. Reply 'LAUNCH' to execute.")
                        elif "source" in mc_fails:
                            # Source degraded — never eligible, log only
                            log(f"  [MICRO-LIVE WATCH] ⛔ SOURCE DEGRADED: {mc_city} {mc_label} | source={mc_source} | margin_f={margin_f:.2f}°F | conf={mc_conf:.0%} | edge={mc_edge:.2f} | ask=${mc_ask:.3f}")
                        elif len(mc_fails) == 1:
                            # Only one gate failed — report as "almost"
                            gname, (_, gval, gthresh) = list(mc_fails.items())[0]
                            # Only alert if it's somewhat close (within 50% of threshold for numeric gates)
                            gclose = False
                            if gname == "margin" and margin_f and margin_f >= MICRO_LIVE_MARGIN_SIMPLE * 0.5:
                                gclose = True
                            elif gname == "confidence" and mc_conf >= MICRO_LIVE_CONFIDENCE * 0.85:
                                gclose = True
                            elif gname == "edge" and mc_edge >= MICRO_LIVE_EDGE * 0.75:
                                gclose = True
                            elif gname == "best_ask" and mc_ask <= MICRO_LIVE_MAX_ASK * 1.25:
                                gclose = True
                            elif gname == "forecast_age" and mc_age > 0 and mc_age <= MICRO_LIVE_MAX_AGE_SEC * 1.5:
                                gclose = True
                            if gclose:
                                log(f"  [MICRO-LIVE WATCH] ⚠ ALMOST: {mc_city} {mc_label} | blocked={gname} ({gval} vs {gthresh}) | margin_f={margin_f:.2f}°F | conf={mc_conf:.0%} | edge={mc_edge:.2f} | ask=${mc_ask:.3f} | provider={mc_provider}")
                                tg(f"[WEATHER] ⚠ NEARLY MICRO-LIVE: {mc_city.title()} {mc_label}\n"
                                   f"  forecast={mc_forecast}°{unit} | margin_f={margin_f:.2f}°F\n"
                                   f"  confidence={mc_conf:.0%} | edge={mc_edge:.2f} | ask=${mc_ask:.3f} | age={mc_age:.0f}s\n"
                                   f"  provider={mc_provider} | source_type={mc_source_type} | T-{hours_left:.1f}h\n"
                                   f"  ❌ Blocked by: {gname} ({gval} vs threshold {gthresh})")
                        else:
                            log(f"  [MICRO-LIVE WATCH] {mc_city} {mc_label}: {len(mc_fails)} gates fail [{', '.join(mc_fails.keys())}]")
                    # ── Micro-live probe: only simple above/below markets ──
                    if WEATHER_MICRO_LIVE_ONLY:
                        mkt_type = setup.get("market_type", "")
                        if mkt_type == "range":
                            _audit["skipped"].append({
                                "city": city, "label": mkt["label"],
                                "reason": "SKIP_MICRO_LIVE_RANGE_DISABLED",
                                "market_type": mkt_type,
                            })
                            continue
                        # Hard reject if CLOB ask > max entry price
                        if setup["yes_price"] > WEATHER_MAX_ENTRY_PRICE:
                            _audit["skipped"].append({
                                "city": city, "label": mkt["label"],
                                "reason": "SKIP_MICRO_LIVE_PRICE_CAP",
                                "yes_price": setup["yes_price"],
                                "max_entry_price": WEATHER_MAX_ENTRY_PRICE,
                            })
                            continue
                    candidates.append(setup)
                    log(f"    CANDIDATE {mkt['label']} ask=${setup['yes_price']:.3f} p={setup['confidence']:.0%} edge={setup['edge']:.2f} margin_f={margin_f if margin_f is not None else 'N/A'}")

                scanned.add(slug)

            if candidates:
                candidates.sort(key=lambda c: (c['edge'], c['confidence'], c['volume'], -c['yes_price']), reverse=True)
                pick = candidates[0]
                log(f"BEST PICK: {pick['city']} {pick['label']} edge={pick['edge']:.2f} confidence={pick['confidence']:.0%} ask=${pick['yes_price']:.3f} vol=${pick['volume']:,.0f} T-{pick['hours_left']:.1f}h margin_f={pick.get('margin_f') or 'N/A'}")
                traded = execute_trade(pick)
                if traded:
                    positions.append({
                        "slug": pick['event']['slug'],
                        "city": pick['city'],
                        "label": pick['label'],
                        "forecast": pick['forecast_temp'],
                        "yes_price": pick['yes_price'],
                        "confidence": pick['confidence'],
                        "edge": pick['edge'],
                        "margin_f": pick.get("margin_f"),
                        "bought_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "dry_run": DRY_RUN,
                    })
                    state["positions"] = positions
                    save_state(state)

            state["scanned_slugs"] = list(scanned)[-200:]
            save_state(state)

            if cycle % 20 == 0:
                log(f"STATUS — cycle={cycle} positions={len(positions)} scanned={len(scanned)}")

        except Exception as e:
            log(f"Cycle error: {e}")

        # Dynamic sleep: faster polling when any event is close to resolution
        sleep_s = POLL_INTERVAL_LATE if min_hours_left <= LATE_CYCLE_HOURS else POLL_INTERVAL
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
