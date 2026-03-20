#!/usr/bin/env python3
"""
journal.py — SQLite trade journal for Taco Trader.

Tracks all trades from both Solana and Polymarket engines in a unified DB.
Provides migration from legacy JSON logs.
"""
import hashlib
import json
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
DB_PATH = ROOT / "journal.db"
TRADE_LOG_JSON = ROOT / ".trade_log.json"
POLY_TRADE_LOG_JSON = ROOT / ".poly_trade_log.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id                   TEXT PRIMARY KEY,
    engine               TEXT,
    timestamp_open       TEXT,
    timestamp_close      TEXT,
    asset                TEXT,
    category             TEXT,
    direction            TEXT,
    entry_price          REAL,
    exit_price           REAL,
    position_size        REAL,
    position_size_usd    REAL,
    pnl_absolute         REAL,
    pnl_percent          REAL,
    exit_type            TEXT,
    hold_duration_seconds INTEGER,
    momentum_score       REAL,
    edge_percent         REAL,
    confidence           REAL,
    regime               TEXT,
    notes                TEXT
);
"""


def get_db() -> sqlite3.Connection:
    """Get a database connection, creating the schema if needed."""
    db_path = Path(DB_PATH)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log_trade_open(
    trade_id: str = None,
    engine: str = "solana",
    asset: str = "",
    category: str = "",
    direction: str = "BUY",
    entry_price: float = 0.0,
    position_size: float = 0.0,
    position_size_usd: float = 0.0,
    momentum_score: float = 0.0,
    edge_percent: float = 0.0,
    confidence: float = 0.0,
    regime: str = "normal",
    notes: str = "",
    timestamp_open: str = None,
) -> str:
    """
    Log a trade open event. Returns the trade ID.
    """
    if not trade_id:
        trade_id = str(uuid.uuid4())
    if not timestamp_open:
        timestamp_open = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_db()
        conn.execute(
            """
            INSERT OR IGNORE INTO trades
                (id, engine, timestamp_open, asset, category, direction,
                 entry_price, position_size, position_size_usd,
                 momentum_score, edge_percent, confidence, regime, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (trade_id, engine, timestamp_open, asset, category, direction,
             entry_price, position_size, position_size_usd,
             momentum_score, edge_percent, confidence, regime, notes),
        )
        conn.commit()
        conn.close()
        logger.info("Trade opened: %s %s %s @ %.6f", trade_id[:8], engine, asset, entry_price)
    except Exception as e:
        logger.error("Failed to log trade open: %s", e)

    return trade_id


def log_trade_close(
    trade_id: str,
    exit_price: float = 0.0,
    pnl_absolute: float = 0.0,
    pnl_percent: float = 0.0,
    exit_type: str = "MANUAL",
    hold_duration_seconds: int = 0,
    timestamp_close: str = None,
    notes: str = None,
) -> bool:
    """
    Update a trade with close details. Returns True on success.
    """
    if not timestamp_close:
        timestamp_close = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_db()
        updates = {
            "timestamp_close": timestamp_close,
            "exit_price": exit_price,
            "pnl_absolute": pnl_absolute,
            "pnl_percent": pnl_percent,
            "exit_type": exit_type,
            "hold_duration_seconds": hold_duration_seconds,
        }
        if notes is not None:
            updates["notes"] = notes

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [trade_id]
        conn.execute(f"UPDATE trades SET {set_clause} WHERE id = ?", values)
        conn.commit()
        conn.close()
        logger.info("Trade closed: %s exit=%.6f pnl=%.2f (%+.1f%%)", trade_id[:8], exit_price, pnl_absolute, pnl_percent)
        return True
    except Exception as e:
        logger.error("Failed to log trade close: %s", e)
        return False


def get_trades(
    engine: str = None,
    days: int = None,
    limit: int = 1000,
    closed_only: bool = False,
) -> list:
    """
    Fetch trades from journal.

    Args:
        engine: Filter by engine ("solana" or "polymarket").
        days: Only trades from last N days.
        limit: Max results.
        closed_only: Only return closed trades.

    Returns:
        List of dicts.
    """
    try:
        conn = get_db()
        conditions = []
        params = []

        if engine:
            conditions.append("engine = ?")
            params.append(engine)
        if days:
            cutoff = datetime.now(timezone.utc)
            from datetime import timedelta
            cutoff -= timedelta(days=days)
            conditions.append("timestamp_open >= ?")
            params.append(cutoff.isoformat())
        if closed_only:
            conditions.append("timestamp_close IS NOT NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY timestamp_open DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to get trades: %s", e)
        return []


def migrate_json_logs() -> int:
    """
    Migrate legacy JSON trade logs into journal.db.
    Reads .trade_log.json (Solana) and .poly_trade_log.json (Polymarket).
    Returns count of records inserted.
    """
    inserted = 0
    conn = get_db()

    # Migrate Solana log
    if TRADE_LOG_JSON.exists():
        try:
            log = json.loads(TRADE_LOG_JSON.read_text())
            for entry in log:
                asset = entry.get("mint") or entry.get("token_address") or entry.get("sym") or entry.get("asset") or ""
                direction = entry.get("event") or entry.get("action", "BUY")
                direction = direction.upper()
                entry_price = float(entry.get("entry") or entry.get("entry_price") or entry.get("price") or 0)
                exit_price = float(entry.get("exit") or entry.get("exit_price") or 0)
                pnl_percent = float(entry.get("pct") or entry.get("pnl_percent") or 0)
                ts_open = entry.get("ts") or entry.get("timestamp_open") or entry.get("timestamp") or ""
                token_address = entry.get("token_address") or entry.get("mint") or asset
                stable = f"{token_address}:{ts_open}:{entry_price}:{exit_price}:{pnl_percent}"
                trade_id = hashlib.sha256(stable.encode()).hexdigest()[:16]
                pnl_absolute = float(entry.get("pnl_absolute") or entry.get("pnl") or 0)
                exit_type = entry.get("exit_type") or entry.get("event") or ("CLOSE" if exit_price else "")
                hold_s = int(entry.get("hold_duration_seconds") or 0)
                ts_close = entry.get("timestamp_close") or (ts_open if exit_price else "")
                regime = entry.get("regime") or ""
                score = float(entry.get("momentum_score") or entry.get("score") or 0)

                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO trades
                            (id, engine, timestamp_open, timestamp_close, asset, direction,
                             entry_price, exit_price, pnl_absolute, pnl_percent, exit_type,
                             hold_duration_seconds, momentum_score, regime)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (trade_id, "solana", ts_open, ts_close or None, asset, direction,
                         entry_price, exit_price or None, pnl_absolute, pnl_percent, exit_type or None,
                         hold_s, score, regime),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass  # Already exists
        except Exception as e:
            logger.error("Solana log migration failed: %s", e)

    # Migrate Polymarket log
    if POLY_TRADE_LOG_JSON.exists():
        try:
            log = json.loads(POLY_TRADE_LOG_JSON.read_text())
            for entry in log:
                trade_id = entry.get("id") or entry.get("order_id") or str(uuid.uuid4())
                market = entry.get("market") or ""
                direction = entry.get("action", "BUY").upper()
                price = float(entry.get("price") or 0)
                amount = float(entry.get("amount") or 0)
                cost_usd = price * amount
                ts = entry.get("timestamp") or ""

                try:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO trades
                            (id, engine, timestamp_open, asset, direction,
                             entry_price, position_size, position_size_usd, notes)
                        VALUES (?,?,?,?,?,?,?,?,?)
                        """,
                        (trade_id, "polymarket", ts, market[:80], direction,
                         price, amount, cost_usd, entry.get("error") or ""),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
        except Exception as e:
            logger.error("Polymarket log migration failed: %s", e)

    conn.commit()
    conn.close()
    logger.info("Migration complete: %d records inserted into journal.db", inserted)
    return inserted


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = migrate_json_logs()
    print(f"Migrated {n} records")
    trades = get_trades(limit=5)
    print(f"Recent trades: {len(trades)}")
    for t in trades:
        print(f"  {t['engine']:12} | {t['asset'][:30]:30} | {t['direction']:4} | {t['timestamp_open'][:19]}")
