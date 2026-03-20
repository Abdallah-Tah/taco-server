#!/usr/bin/env python3
"""
journal/db.py — SQLite persistence layer for Taco Trade Journal.

Schema (trades table):
  id, engine, timestamp_open, timestamp_close, asset, category,
  direction, entry_price, exit_price, position_size, position_size_usd,
  pnl_absolute, pnl_percent, exit_type, hold_duration_seconds,
  momentum_score, edge_percent, confidence, regime, notes

Idempotent: init_db() safe to call on every startup.
migrate_json_logs() is safe to run multiple times (deduplicates by engine+asset+timestamp_open).
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_TRADING_DIR = Path(__file__).parent.parent
DB_PATH = _TRADING_DIR / "journal.db"


def get_db_path() -> Path:
    return DB_PATH


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    engine                TEXT    NOT NULL DEFAULT 'unknown',
    timestamp_open        TEXT    NOT NULL,
    timestamp_close       TEXT,
    asset                 TEXT    NOT NULL DEFAULT '',
    category              TEXT    DEFAULT 'unknown',
    direction             TEXT    DEFAULT 'long',
    entry_price           REAL    DEFAULT 0,
    exit_price            REAL    DEFAULT 0,
    position_size         REAL    DEFAULT 0,
    position_size_usd     REAL    DEFAULT 0,
    pnl_absolute          REAL    DEFAULT 0,
    pnl_percent           REAL    DEFAULT 0,
    exit_type             TEXT    DEFAULT '',
    hold_duration_seconds REAL    DEFAULT 0,
    momentum_score        REAL    DEFAULT 0,
    edge_percent          REAL    DEFAULT 0,
    confidence            REAL    DEFAULT 0,
    regime                TEXT    DEFAULT 'normal',
    notes                 TEXT    DEFAULT '',
    UNIQUE(engine, asset, timestamp_open)
);

CREATE TABLE IF NOT EXISTS regime_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    from_regime TEXT    NOT NULL,
    to_regime   TEXT    NOT NULL,
    win_rate    REAL    DEFAULT 0,
    window      INTEGER DEFAULT 0,
    notes       TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS milestones (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    amount      REAL    NOT NULL,
    days_since_start INTEGER DEFAULT 0,
    total_trades INTEGER DEFAULT 0,
    win_rate    REAL    DEFAULT 0,
    avg_return  REAL    DEFAULT 0,
    std_return  REAL    DEFAULT 0,
    best_trade  REAL    DEFAULT 0,
    worst_trade REAL    DEFAULT 0,
    UNIQUE(amount)
);

CREATE INDEX IF NOT EXISTS idx_trades_engine ON trades(engine);
CREATE INDEX IF NOT EXISTS idx_trades_ts     ON trades(timestamp_open);
CREATE INDEX IF NOT EXISTS idx_trades_asset  ON trades(asset);
"""


def init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
    with _connect() as conn:
        conn.executescript(CREATE_SQL)
    logger.info("Journal DB initialised at %s", DB_PATH)


def insert_trade(trade: dict) -> int | None:
    """
    Insert a trade record. Returns rowid, or None if duplicate.
    
    Required fields: engine, timestamp_open, asset
    Optional: all other schema columns
    """
    cols = [
        "engine", "timestamp_open", "timestamp_close", "asset", "category",
        "direction", "entry_price", "exit_price", "position_size",
        "position_size_usd", "pnl_absolute", "pnl_percent", "exit_type",
        "hold_duration_seconds", "momentum_score", "edge_percent",
        "confidence", "regime", "notes",
    ]
    row = {c: trade.get(c) for c in cols}
    placeholders = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    sql = f"""
        INSERT OR IGNORE INTO trades ({col_list})
        VALUES ({placeholders})
    """
    try:
        with _connect() as conn:
            cur = conn.execute(sql, row)
            if cur.lastrowid and cur.rowcount:
                return cur.lastrowid
            return None  # duplicate
    except Exception as exc:
        logger.error("insert_trade error: %s", exc)
        return None


def log_regime_change(
    from_regime: str,
    to_regime: str,
    win_rate: float = 0.0,
    window: int = 0,
    notes: str = "",
) -> None:
    msg = f"REGIME SWITCH: {from_regime} → {to_regime} (win_rate={win_rate:.2f}, window={window})"
    logger.info(msg)
    print(msg)
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO regime_log (timestamp, from_regime, to_regime, win_rate, window, notes) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    from_regime, to_regime, win_rate, window, notes,
                ),
            )
    except Exception as exc:
        logger.error("log_regime_change error: %s", exc)


def log_milestone(amount: float, stats: dict) -> None:
    """Record a capital milestone hit."""
    import math
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO milestones
                   (timestamp, amount, days_since_start, total_trades, win_rate,
                    avg_return, std_return, best_trade, worst_trade)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    amount,
                    stats.get("days_since_start", 0),
                    stats.get("total_trades", 0),
                    stats.get("win_rate", 0.0),
                    stats.get("avg_return", 0.0),
                    stats.get("std_return", 0.0),
                    stats.get("best_trade", 0.0),
                    stats.get("worst_trade", 0.0),
                ),
            )
            logger.info("🏆 MILESTONE HIT: $%.2f", amount)
            print(f"🏆 MILESTONE HIT: ${amount:.2f} | {stats}")
    except Exception as exc:
        logger.error("log_milestone error: %s", exc)


# ── Migration helpers ──────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> str:
    """Normalize timestamp to ISO format string."""
    if not ts:
        return datetime.now(timezone.utc).isoformat()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(ts, fmt).isoformat()
        except ValueError:
            continue
    return ts  # Return as-is if nothing matches


def migrate_json_logs() -> dict:
    """
    Migrate existing .trade_log.json (Solana) and .poly_trade_log.json (Polymarket)
    into the SQLite journal. Idempotent — duplicates are silently ignored.
    
    Returns {'sol_migrated': int, 'poly_migrated': int}
    """
    init_db()
    sol_log  = _TRADING_DIR / ".trade_log.json"
    poly_log = _TRADING_DIR / ".poly_trade_log.json"

    sol_migrated  = _migrate_sol_log(sol_log)
    poly_migrated = _migrate_poly_log(poly_log)

    logger.info(
        "Migration complete — SOL: %d, Poly: %d",
        sol_migrated, poly_migrated,
    )
    return {"sol_migrated": sol_migrated, "poly_migrated": poly_migrated}


def _migrate_sol_log(path: Path) -> int:
    """Migrate Solana .trade_log.json entries that are close events."""
    if not path.exists():
        return 0
    try:
        entries = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return 0

    count = 0
    exit_events = {"SL", "ROTATE", "TRAIL", "TP", "PARTIAL_TP"}
    buy_map: dict[str, dict] = {}

    # Build buy index first
    for e in entries:
        if e.get("event") == "BUY":
            buy_map[e.get("mint", "")] = e

    for e in entries:
        event = e.get("event", "")
        if event not in exit_events:
            continue

        mint = e.get("mint", "")
        buy  = buy_map.get(mint, {})

        entry_price = float(e.get("entry_price", e.get("entry", buy.get("entry", 0))) or 0)
        exit_price  = float(e.get("exit_price",  e.get("exit",  0)) or 0)
        pnl_pct     = float(e.get("pnl_percent", e.get("pct",   0)) or 0)
        hold_s      = float(e.get("hold_duration_seconds", 0) or 0)
        ts_open     = _parse_ts(buy.get("ts", e.get("ts", "")))
        ts_close    = _parse_ts(e.get("ts", ""))

        trade = {
            "engine":                "solana",
            "timestamp_open":        ts_open,
            "timestamp_close":       ts_close,
            "asset":                 mint or e.get("sym", "?"),
            "category":              "crypto",
            "direction":             "long",
            "entry_price":           entry_price,
            "exit_price":            exit_price,
            "position_size":         float(buy.get("sol", 0) or 0),
            "position_size_usd":     0.0,
            "pnl_absolute":          0.0,
            "pnl_percent":           pnl_pct,
            "exit_type":             event,
            "hold_duration_seconds": hold_s,
            "momentum_score":        float(e.get("score", buy.get("score", 0)) or 0),
            "edge_percent":          0.0,
            "confidence":            0.0,
            "regime":                "unknown",
            "notes":                 e.get("reason", ""),
        }
        if insert_trade(trade) is not None:
            count += 1

    return count


def _migrate_poly_log(path: Path) -> int:
    """Migrate Polymarket .poly_trade_log.json into journal."""
    if not path.exists():
        return 0
    try:
        entries = json.loads(path.read_text())
    except Exception as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return 0

    count = 0
    # Only migrate BUY entries; sells/cancels lack enough data
    for e in entries:
        if e.get("action") not in ("BUY",):
            continue
        ts_open = _parse_ts(e.get("timestamp", ""))
        trade = {
            "engine":                "polymarket",
            "timestamp_open":        ts_open,
            "timestamp_close":       None,
            "asset":                 e.get("market", e.get("token_id", ""))[:120],
            "category":              "prediction",
            "direction":             "long",
            "entry_price":           float(e.get("price", 0) or 0),
            "exit_price":            0.0,
            "position_size":         float(e.get("amount", 0) or 0),
            "position_size_usd":     float(e.get("amount", 0) or 0) * float(e.get("price", 0) or 0),
            "pnl_absolute":          0.0,
            "pnl_percent":           0.0,
            "exit_type":             "open",
            "hold_duration_seconds": 0.0,
            "momentum_score":        0.0,
            "edge_percent":          float(e.get("edge", 0) or 0),
            "confidence":            float(e.get("confidence", 0) or 0),
            "regime":                "unknown",
            "notes":                 e.get("error", ""),
        }
        if insert_trade(trade) is not None:
            count += 1

    return count
