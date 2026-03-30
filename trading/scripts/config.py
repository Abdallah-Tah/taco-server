#!/usr/bin/env python3
"""
config.py — Taco Trader configuration constants.
All values are loaded from environment variables with sensible defaults.
"""
import os


def _float(key, default):
    return float(os.environ.get(key, default))


def _int(key, default):
    return int(os.environ.get(key, default))


# ── Entry filters ──────────────────────────────────────────────────────────────
MIN_LIQUIDITY        = _float("MIN_LIQUIDITY",        80_000)
MIN_VOLUME_24H       = _float("MIN_VOLUME_24H",     2_000_000)
CHANGE_1H_MIN        = _float("CHANGE_1H_MIN",           8.0)
CHANGE_1H_MAX        = _float("CHANGE_1H_MAX",          60.0)
CHANGE_24H_FLOOR     = _float("CHANGE_24H_FLOOR",       -20.0)
MOMENTUM_SCORE_MIN   = _float("MOMENTUM_SCORE_MIN",      50.0)

# ── Exit thresholds ────────────────────────────────────────────────────────────
STOP_LOSS            = _float("STOP_LOSS",              -50.0)   # hard SL %
HARD_ROTATION        = _float("HARD_ROTATION",          -50.0)   # rotate loss %
TAKE_PROFIT_FULL     = _float("TAKE_PROFIT_FULL",       400.0)   # full TP % (used for second scale at 5x)
PARTIAL_TP_TRIGGER   = _float("PARTIAL_TP_TRIGGER",     100.0)   # partial TP arm % (2x)
TRAILING_STOP_ARM    = _float("TRAILING_STOP_ARM",      100.0)   # trail after big move
TRAILING_STOP_GIVE   = _float("TRAILING_STOP_GIVE",      35.0)   # max giveback % from peak
STALE_ROTATION_TIME  = _float("STALE_ROTATION_TIME",   7200.0)   # seconds before stale check
STALE_ROTATION_MOVE  = _float("STALE_ROTATION_MOVE",     3.0)    # % move required to avoid stale exit

# ── Position sizing ────────────────────────────────────────────────────────────
DEFAULT_RISK_SOL     = _float("DEFAULT_RISK_SOL",        0.03)
HIGH_CONVICTION_SOL  = _float("HIGH_CONVICTION_SOL",     0.05)
MAX_OPEN_POSITIONS   = _int(  "MAX_OPEN_POSITIONS",         3)
DEFENSIVE_POSITIONS  = _int(  "DEFENSIVE_POSITIONS",        2)
PARTIAL_TP_FRACTION  = _float("PARTIAL_TP_FRACTION",     0.25)
SECOND_TP_FRACTION   = _float("SECOND_TP_FRACTION",      0.25)
MIN_RESERVE_SOL      = _float("MIN_RESERVE_SOL",         0.10)
STOP_TRADING_SOL     = _float("STOP_TRADING_SOL",        0.05)

# ── High-conviction thresholds ─────────────────────────────────────────────────
HIGH_CONVICTION_SCORE     = _float("HIGH_CONVICTION_SCORE",      60.0)
HIGH_CONVICTION_VOL_ACCEL = _float("HIGH_CONVICTION_VOL_ACCEL",   2.0)

# ── Blacklist ──────────────────────────────────────────────────────────────────
BLACKLIST_FAIL_COUNT      = _int(  "BLACKLIST_FAIL_COUNT",    2)
BLACKLIST_AVG_LOSS_MAX    = _float("BLACKLIST_AVG_LOSS_MAX", -18.0)
BLACKLIST_COOLDOWN_DAYS   = _float("BLACKLIST_COOLDOWN_DAYS",  7.0)

# ── Re-entry cooldown ──────────────────────────────────────────────────────────
REENTRY_COOLDOWN_SECONDS  = _int(  "REENTRY_COOLDOWN_SECONDS", 21600)
REBUY_COOLDOWN_HOURS      = _float("REBUY_COOLDOWN_HOURS",    6.0)

# ── Loop ───────────────────────────────────────────────────────────────────────
CHECK_INTERVAL       = _int("CHECK_INTERVAL", 90)

# ── Regime switching ───────────────────────────────────────────────────────────
# Window of recent closed trades to evaluate regime
REGIME_WINDOW             = _int(  "REGIME_WINDOW",            10)
# win_rate >= AGGRESSIVE_THRESHOLD → aggressive mode
AGGRESSIVE_THRESHOLD      = _float("AGGRESSIVE_THRESHOLD",    0.55)
# win_rate <= DEFENSIVE_THRESHOLD  → defensive mode
DEFENSIVE_THRESHOLD       = _float("DEFENSIVE_THRESHOLD",     0.45)

# Aggressive regime overrides
AGGRESSIVE_MAX_POSITIONS  = _int(  "AGGRESSIVE_MAX_POSITIONS",  4)
# (aggressive uses HIGH_CONVICTION_SOL and normal STALE_ROTATION_TIME)

# Defensive regime overrides
DEFENSIVE_MAX_POSITIONS   = _int(  "DEFENSIVE_MAX_POSITIONS",   2)
DEFENSIVE_STOP_LOSS       = _float("DEFENSIVE_STOP_LOSS",     -25.0)  # tighter SL
DEFENSIVE_HARD_ROTATION   = _float("DEFENSIVE_HARD_ROTATION", -10.0)
DEFENSIVE_STALE_TIME      = _float("DEFENSIVE_STALE_TIME",   3600.0)  # 1h
# Defensive mode forces DEFAULT_RISK_SOL only (no high-conviction sizing)

# Legacy regime compat (used by old taco_trader paths)
DEFENSIVE_SCORE_BUMP      = _float("DEFENSIVE_SCORE_BUMP",    8.0)
REGIME_SAMPLE_SIZE        = REGIME_WINDOW
REGIME_DEFENSIVE_AVG      = _float("REGIME_DEFENSIVE_AVG",   -8.0)
REGIME_DEFENSIVE_WINS     = _int(  "REGIME_DEFENSIVE_WINS",    2)
REGIME_AGGRESSIVE_AVG     = _float("REGIME_AGGRESSIVE_AVG",  12.0)
REGIME_AGGRESSIVE_WINS    = _int(  "REGIME_AGGRESSIVE_WINS",   2)

# ── Polymarket config ──────────────────────────────────────────────────────────
# Edge thresholds (percent, not decimal)
NEWS_EDGE_MIN             = _float("NEWS_EDGE_MIN",            8.0)
NEWS_CONFIDENCE_MIN       = _float("NEWS_CONFIDENCE_MIN",     30.0)
HIGH_CONVICTION_EDGE      = _float("HIGH_CONVICTION_EDGE",    15.0)
NEWS_SOURCES              = ["brave_api", "duckduckgo", "google_rss"]

# Position sizing (USD)
POLY_DEFAULT_SIZE         = _float("POLY_DEFAULT_SIZE",        8.00)
POLY_HIGH_CONVICTION_SIZE = _float("POLY_HIGH_CONVICTION_SIZE",10.00)
POLY_MAX_SIZE             = _float("POLY_MAX_SIZE",           15.00)

# Safety: Daily loss limit triggers size reduction
DAILY_LOSS_LIMIT          = _float("DAILY_LOSS_LIMIT",        50.00)  # Stop trading if daily loss exceeds this
FALLBACK_SIZE             = _float("FALLBACK_SIZE",            5.00)  # Reduce to this size after hitting loss limit
POLY_MIN_SHARES           = _int(  "POLY_MIN_SHARES",            5)
POLY_MAX_POSITIONS        = _int(  "POLY_MAX_POSITIONS",          8)
POLY_MAX_TOTAL_ORDERS     = _int(  "POLY_MAX_TOTAL_ORDERS",      10)

# Blocked categories (configurable list)
BLOCKED_CATEGORIES        = os.environ.get(
    "BLOCKED_CATEGORIES", "sports,spreads"
).split(",")

# ── News arb config ───────────────────────────────────────────────────────────
NEWS_SCAN_INTERVAL        = _int(  "NEWS_SCAN_INTERVAL",          45)
NEWS_WATCHLIST_REFRESH    = _int(  "NEWS_WATCHLIST_REFRESH",    1800)
NEWS_ARTICLE_MAX_AGE      = _int(  "NEWS_ARTICLE_MAX_AGE",       900)
NEWS_MIN_KEYWORD_MATCH    = _int(  "NEWS_MIN_KEYWORD_MATCH",       2)
NEWS_MIN_EDGE             = _float("NEWS_MIN_EDGE",              8.0)
NEWS_HIGH_CONVICTION_EDGE = _float("NEWS_HIGH_CONVICTION_EDGE", 15.0)
NEWS_DEFAULT_SIZE         = _float("NEWS_DEFAULT_SIZE",          5.00)
NEWS_HIGH_SIZE            = _float("NEWS_HIGH_SIZE",             7.50)
NEWS_MAX_SIZE             = _float("NEWS_MAX_SIZE",             10.00)
NEWS_TP                   = _float("NEWS_TP",                    8.0)
NEWS_SL                   = _float("NEWS_SL",                   -5.0)
NEWS_TIME_EXIT_HOURS      = _float("NEWS_TIME_EXIT_HOURS",       4.0)
NEWS_MAX_POSITIONS        = _int(  "NEWS_MAX_POSITIONS",           3)
WHALE_WATCHLIST_REFRESH   = _int(  "WHALE_WATCHLIST_REFRESH",   86400)
WHALE_SCAN_INTERVAL       = _int(  "WHALE_SCAN_INTERVAL",          15)
WHALE_MIN_WIN_RATE        = _float("WHALE_MIN_WIN_RATE",         0.60)
WHALE_MIN_TRADE_SIZE      = _float("WHALE_MIN_TRADE_SIZE",      100.0)
WHALE_FOLLOW_DEFAULT_SIZE = _float("WHALE_FOLLOW_DEFAULT_SIZE",   5.00)
WHALE_FOLLOW_HIGH_SIZE    = _float("WHALE_FOLLOW_HIGH_SIZE",      7.50)
WHALE_FOLLOW_MAX_SIZE     = _float("WHALE_FOLLOW_MAX_SIZE",      10.00)
WHALE_FOLLOW_TP           = _float("WHALE_FOLLOW_TP",            12.0)
WHALE_FOLLOW_SL           = _float("WHALE_FOLLOW_SL",            -8.0)
WHALE_FOLLOW_TIME_EXIT    = _float("WHALE_FOLLOW_TIME_EXIT",     48.0)
WHALE_FOLLOW_MAX_POSITIONS= _int(  "WHALE_FOLLOW_MAX_POSITIONS",    3)

# Correlation guard
MAX_CORRELATED_POSITIONS  = _int(  "MAX_CORRELATED_POSITIONS",   2)
CORRELATION_MIN_KEYWORDS  = _int(  "CORRELATION_MIN_KEYWORDS",   2)

# ── Global risk / portfolio ────────────────────────────────────────────────────
MONTHLY_DRAWDOWN_LIMIT    = _float("MONTHLY_DRAWDOWN_LIMIT",   0.30)
PAUSE_THRESHOLD_USD       = _float("PAUSE_THRESHOLD_USD",     70.00)
PAUSE_DURATION_HOURS      = _float("PAUSE_DURATION_HOURS",   168.0)   # 7 days
MILESTONE_AMOUNTS         = [float(x) for x in os.environ.get(
    "MILESTONE_AMOUNTS", "150,250,500,1000,2000,2780"
).split(",")]

# ── BTC 15-Minute Engine ──────────────────────────────────────────────────────
BTC15M_ENABLED            = os.environ.get("BTC15M_ENABLED",      "true").lower() == "true"
BTC15M_DRY_RUN            = os.environ.get("BTC15M_DRY_RUN",      "true").lower() != "false"
BTC15M_ARB_THRESHOLD      = _float("BTC15M_ARB_THRESHOLD",        0.98)
BTC15M_ARB_SIZE           = _float("BTC15M_ARB_SIZE",            10.00)
BTC15M_SNIPE_DELTA_MIN    = _float("BTC15M_SNIPE_DELTA_MIN",     0.025)
BTC15M_SNIPE_MAX_PRICE    = _float("BTC15M_SNIPE_MAX_PRICE",    0.92)
BTC15M_SNIPE_DEFAULT_SIZE = _float("BTC15M_SNIPE_DEFAULT_SIZE",   8.00)
BTC15M_SNIPE_STRONG_SIZE  = _float("BTC15M_SNIPE_STRONG_SIZE",   12.00)
BTC15M_SNIPE_STRONG_DELTA = _float("BTC15M_SNIPE_STRONG_DELTA",  0.10)
BTC15M_SNIPE_WINDOW_SEC  = _int(  "BTC15M_SNIPE_WINDOW_SEC",       15)
BTC15M_PRICE_POLL_SEC    = _int(  "BTC15M_PRICE_POLL_SEC",         5)
BTC15M_SCAN_INTERVAL      = _int(  "BTC15M_SCAN_INTERVAL",        10)
BTC15M_MAX_DAILY_LOSS     = _float("BTC15M_MAX_DAILY_LOSS",      50.00)  # Increased to match global DAILY_LOSS_LIMIT
BTC15M_SERIES_ID          = "10192"
BTC15M_SERIES_SLUG        = "btc-up-or-down-15m"
