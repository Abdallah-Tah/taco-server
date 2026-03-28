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

CREATE TABLE IF NOT EXISTS edge_events (
    id                   TEXT PRIMARY KEY,
    engine               TEXT,
    asset                TEXT,
    timestamp_et         TEXT,
    market_slug          TEXT,
    market_id            TEXT,
    side                 TEXT,
    signal_type          TEXT,
    seconds_remaining    INTEGER,
    best_bid             REAL,
    best_ask             REAL,
    spread               REAL,
    midprice             REAL,
    microprice           REAL,
    price_now            REAL,
    price_1s_ago         REAL,
    price_3s_ago         REAL,
    price_5s_ago         REAL,
    price_10s_ago        REAL,
    price_30s_ago        REAL,
    ret_1s               REAL,
    ret_3s               REAL,
    ret_5s               REAL,
    ret_10s              REAL,
    ret_30s              REAL,
    vol_10s              REAL,
    vol_30s              REAL,
    vol_60s              REAL,
    imbalance_1          REAL,
    imbalance_3          REAL,
    model_p_yes          REAL,
    model_p_no           REAL,
    edge_yes             REAL,
    edge_no              REAL,
    net_edge             REAL,
    confidence           REAL,
    regime               TEXT,
    regime_ok            INTEGER,
    adaptive_net_edge_floor REAL,
    adaptive_confidence_floor REAL,
    skip_reason          TEXT,
    intended_entry_price REAL,
    actual_fill_price    REAL,
    slippage             REAL,
    shadow_decision      TEXT,
    shadow_skip_reason   TEXT,
    execution_status     TEXT,
    decision             TEXT
);
"""

EDGE_EVENT_COLUMNS = [
    "id", "engine", "asset", "timestamp_et", "market_slug", "market_id", "side", "signal_type",
    "seconds_remaining", "best_bid", "best_ask", "spread", "midprice", "microprice",
    "price_now", "price_1s_ago", "price_3s_ago", "price_5s_ago", "price_10s_ago", "price_30s_ago",
    "ret_1s", "ret_3s", "ret_5s", "ret_10s", "ret_30s", "vol_10s", "vol_30s", "vol_60s",
    "imbalance_1", "imbalance_3", "model_p_yes", "model_p_no", "edge_yes", "edge_no",
    "net_edge", "confidence", "regime", "regime_ok", "adaptive_net_edge_floor", "adaptive_confidence_floor", "skip_reason", "intended_entry_price",
    "actual_fill_price", "slippage", "shadow_decision", "shadow_skip_reason", "execution_status", "decision",
]

SOLANA_MIGRATION_COLUMNS = [
    "id", "engine", "timestamp_open", "timestamp_close", "asset", "direction",
    "entry_price", "exit_price", "pnl_absolute", "pnl_percent", "exit_type",
    "hold_duration_seconds", "momentum_score", "regime",
]


def get_db() -> sqlite3.Connection:
    """Get a database connection, creating the schema if needed."""
    db_path = Path(DB_PATH)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)

    # Backward-compatible migrations for previously created edge_events tables.
    edge_cols = {row["name"] for row in conn.execute("PRAGMA table_info(edge_events)").fetchall()}
    if "regime" not in edge_cols:
        conn.execute("ALTER TABLE edge_events ADD COLUMN regime TEXT")
    if "adaptive_net_edge_floor" not in edge_cols:
        conn.execute("ALTER TABLE edge_events ADD COLUMN adaptive_net_edge_floor REAL")
    if "adaptive_confidence_floor" not in edge_cols:
        conn.execute("ALTER TABLE edge_events ADD COLUMN adaptive_confidence_floor REAL")
    if "shadow_decision" not in edge_cols:
        conn.execute("ALTER TABLE edge_events ADD COLUMN shadow_decision TEXT")
    if "shadow_skip_reason" not in edge_cols:
        conn.execute("ALTER TABLE edge_events ADD COLUMN shadow_skip_reason TEXT")
    if "execution_status" not in edge_cols:
        conn.execute("ALTER TABLE edge_events ADD COLUMN execution_status TEXT")

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


def log_edge_event(
    event_id: str = None,
    engine: str = None,
    asset: str = None,
    timestamp_et: str = None,
    market_slug: str = None,
    market_id: str = None,
    side: str = None,
    signal_type: str = None,
    seconds_remaining: int = None,
    best_bid: float = None,
    best_ask: float = None,
    spread: float = None,
    midprice: float = None,
    microprice: float = None,
    price_now: float = None,
    price_1s_ago: float = None,
    price_3s_ago: float = None,
    price_5s_ago: float = None,
    price_10s_ago: float = None,
    price_30s_ago: float = None,
    ret_1s: float = None,
    ret_3s: float = None,
    ret_5s: float = None,
    ret_10s: float = None,
    ret_30s: float = None,
    vol_10s: float = None,
    vol_30s: float = None,
    vol_60s: float = None,
    imbalance_1: float = None,
    imbalance_3: float = None,
    model_p_yes: float = None,
    model_p_no: float = None,
    edge_yes: float = None,
    edge_no: float = None,
    net_edge: float = None,
    confidence: float = None,
    regime_ok=None,
    skip_reason: str = None,
    intended_entry_price: float = None,
    actual_fill_price: float = None,
    slippage: float = None,
    execution_status: str = None,
    decision: str = None,
    **extra_fields,
) -> str:
    """
    Log one edge event row. Returns the event ID.
    Extra keys are safely ignored.
    """
    if not event_id:
        event_id = str(uuid.uuid4())
    if isinstance(regime_ok, bool):
        regime_ok = int(regime_ok)

    raw_payload = {
        "id": event_id,
        "engine": engine,
        "asset": asset,
        "timestamp_et": timestamp_et,
        "market_slug": market_slug,
        "market_id": market_id,
        "side": side,
        "signal_type": signal_type,
        "seconds_remaining": seconds_remaining,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "midprice": midprice,
        "microprice": microprice,
        "price_now": price_now,
        "price_1s_ago": price_1s_ago,
        "price_3s_ago": price_3s_ago,
        "price_5s_ago": price_5s_ago,
        "price_10s_ago": price_10s_ago,
        "price_30s_ago": price_30s_ago,
        "ret_1s": ret_1s,
        "ret_3s": ret_3s,
        "ret_5s": ret_5s,
        "ret_10s": ret_10s,
        "ret_30s": ret_30s,
        "vol_10s": vol_10s,
        "vol_30s": vol_30s,
        "vol_60s": vol_60s,
        "imbalance_1": imbalance_1,
        "imbalance_3": imbalance_3,
        "model_p_yes": model_p_yes,
        "model_p_no": model_p_no,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "net_edge": net_edge,
        "confidence": confidence,
        "regime_ok": regime_ok,
        "skip_reason": skip_reason,
        "intended_entry_price": intended_entry_price,
        "actual_fill_price": actual_fill_price,
        "slippage": slippage,
        "execution_status": execution_status,
        "decision": decision,
    }
    raw_payload.update(extra_fields or {})

    filtered_payload = {col: raw_payload.get(col) for col in EDGE_EVENT_COLUMNS}
    placeholders = ", ".join("?" for _ in EDGE_EVENT_COLUMNS)
    columns_csv = ", ".join(EDGE_EVENT_COLUMNS)
    values = [filtered_payload[col] for col in EDGE_EVENT_COLUMNS]

    try:
        conn = get_db()
        conn.execute(
            f"INSERT OR REPLACE INTO edge_events ({columns_csv}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        conn.close()
        logger.info("Edge event logged: %s %s %s", event_id[:8], engine or "", asset or "")
    except Exception as e:
        logger.error("Failed to log edge event: %s", e)

    return event_id


def update_edge_event_status(
    event_id: str,
    execution_status: str = None,
    skip_reason: str = None,
    actual_fill_price: float = None,
    slippage: float = None,
    decision: str = None,
) -> bool:
    """
    Update execution-related fields for an existing edge event.
    Returns True when a row was updated.
    """
    updates = []
    values = []

    if execution_status is not None:
        updates.append("execution_status = ?")
        values.append(execution_status)
    if skip_reason is not None:
        updates.append("skip_reason = ?")
        values.append(skip_reason)
    if actual_fill_price is not None:
        updates.append("actual_fill_price = ?")
        values.append(actual_fill_price)
    if slippage is not None:
        updates.append("slippage = ?")
        values.append(slippage)
    if decision is not None:
        updates.append("decision = ?")
        values.append(decision)

    if not updates or not event_id:
        return False

    values.append(event_id)
    try:
        conn = get_db()
        cur = conn.execute(
            f"UPDATE edge_events SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        conn.commit()
        updated = cur.rowcount > 0
        conn.close()
        return updated
    except Exception as e:
        logger.error("Failed to update edge event status: %s", e)
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
                    solana_values = {
                        "id": trade_id,
                        "engine": "solana",
                        "timestamp_open": ts_open,
                        "timestamp_close": ts_close or None,
                        "asset": asset,
                        "direction": direction,
                        "entry_price": entry_price,
                        "exit_price": exit_price or None,
                        "pnl_absolute": pnl_absolute,
                        "pnl_percent": pnl_percent,
                        "exit_type": exit_type or None,
                        "hold_duration_seconds": hold_s,
                        "momentum_score": score,
                        "regime": regime,
                    }
                    cols = ", ".join(SOLANA_MIGRATION_COLUMNS)
                    placeholders = ", ".join("?" for _ in SOLANA_MIGRATION_COLUMNS)
                    values = [solana_values.get(c) for c in SOLANA_MIGRATION_COLUMNS]
                    conn.execute(
                        f"INSERT OR IGNORE INTO trades ({cols}) VALUES ({placeholders})",
                        values,
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
