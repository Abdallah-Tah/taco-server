#!/usr/bin/env python3
"""
journal.py - Shared SQLite journal helpers.

This module is the single source of truth for journal bootstrap, validation,
and lifecycle persistence. Bots should treat ``trade_id`` as the only stable
external trade identifier. SQLite row ``id`` is internal only.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
DB_PATH = ROOT / "journal.db"
TRADE_LOG_JSON = ROOT / ".trade_log.json"
POLY_TRADE_LOG_JSON = ROOT / ".poly_trade_log.json"
SCHEMA_VERSION = 1

TRADE_COLUMNS = {
    "id",
    "trade_id",
    "engine",
    "timestamp_open",
    "timestamp_close",
    "asset",
    "category",
    "direction",
    "entry_price",
    "exit_price",
    "position_size",
    "position_size_usd",
    "pnl_absolute",
    "pnl_percent",
    "exit_type",
    "hold_duration_seconds",
    "momentum_score",
    "edge_percent",
    "confidence",
    "regime",
    "notes",
}

EVENT_COLUMNS = {
    "id",
    "event_id",
    "trade_id",
    "engine",
    "event_type",
    "timestamp",
    "asset",
    "direction",
    "price",
    "quantity",
    "value_usd",
    "notes",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id              TEXT NOT NULL UNIQUE,
    engine                TEXT NOT NULL,
    timestamp_open        TEXT NOT NULL,
    timestamp_close       TEXT,
    asset                 TEXT NOT NULL,
    category              TEXT,
    direction             TEXT NOT NULL,
    entry_price           REAL,
    exit_price            REAL,
    position_size         REAL,
    position_size_usd     REAL,
    pnl_absolute          REAL,
    pnl_percent           REAL,
    exit_type             TEXT,
    hold_duration_seconds INTEGER,
    momentum_score        REAL,
    edge_percent          REAL,
    confidence            REAL,
    regime                TEXT,
    notes                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_trade_id ON trades(trade_id);
CREATE INDEX IF NOT EXISTS idx_trades_engine_open ON trades(engine, timestamp_open);
CREATE INDEX IF NOT EXISTS idx_trades_engine_close ON trades(engine, timestamp_close);
CREATE INDEX IF NOT EXISTS idx_trades_open_status ON trades(timestamp_close);

CREATE TABLE IF NOT EXISTS trade_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    trade_id   TEXT,
    engine     TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    asset      TEXT,
    direction  TEXT,
    price      REAL,
    quantity   REAL,
    value_usd  REAL,
    notes      TEXT
);

CREATE INDEX IF NOT EXISTS idx_trade_events_trade_id ON trade_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_events_engine_ts ON trade_events(engine, timestamp);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def bootstrap_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO journal_meta(key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def validate_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required_tables = {"journal_meta", "trades", "trade_events"}
    missing_tables = required_tables - tables
    if missing_tables:
        raise RuntimeError(f"missing required tables: {sorted(missing_tables)}")

    version_row = conn.execute(
        "SELECT value FROM journal_meta WHERE key='schema_version'"
    ).fetchone()
    if version_row is None:
        raise RuntimeError("missing schema_version metadata")
    schema_version = int(version_row["value"])
    if schema_version != SCHEMA_VERSION:
        raise RuntimeError(
            f"schema version mismatch: expected {SCHEMA_VERSION}, got {schema_version}"
        )

    trade_columns = _table_columns(conn, "trades")
    missing_trade_columns = TRADE_COLUMNS - trade_columns
    if missing_trade_columns:
        raise RuntimeError(
            f"trades missing required columns: {sorted(missing_trade_columns)}"
        )

    event_columns = _table_columns(conn, "trade_events")
    missing_event_columns = EVENT_COLUMNS - event_columns
    if missing_event_columns:
        raise RuntimeError(
            f"trade_events missing required columns: {sorted(missing_event_columns)}"
        )

    return {
        "schema_version": schema_version,
        "trade_columns": sorted(trade_columns),
        "event_columns": sorted(event_columns),
    }


def healthcheck(conn: sqlite3.Connection, bot_name: str) -> None:
    trade_id = f"healthcheck-{bot_name}-{uuid.uuid4()}"
    timestamp_open = _utc_now()
    conn.execute("SAVEPOINT journal_healthcheck")
    try:
        cur = conn.execute(
            """
            INSERT INTO trades (
                trade_id, engine, timestamp_open, asset, category, direction,
                entry_price, position_size, position_size_usd, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                bot_name,
                timestamp_open,
                "__healthcheck__",
                "healthcheck",
                "CHECK",
                0.0,
                0.0,
                0.0,
                "healthcheck",
                "startup healthcheck",
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("healthcheck insert did not write exactly one row")

        row = conn.execute(
            "SELECT trade_id, engine FROM trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()
        if row is None or row["trade_id"] != trade_id or row["engine"] != bot_name:
            raise RuntimeError("healthcheck select verification failed")

        deleted = conn.execute(
            "DELETE FROM trades WHERE trade_id=?",
            (trade_id,),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("healthcheck delete did not remove exactly one row")
    except Exception:
        conn.execute("ROLLBACK TO journal_healthcheck")
        conn.execute("RELEASE journal_healthcheck")
        raise
    else:
        conn.execute("ROLLBACK TO journal_healthcheck")
        conn.execute("RELEASE journal_healthcheck")


def open_journal(db_path: str | Path | None = None, bot_name: str = "journal") -> sqlite3.Connection:
    resolved = Path(db_path or DB_PATH).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    preexisting = resolved.exists()
    pre_size = resolved.stat().st_size if preexisting else 0

    conn = _connect(resolved)
    try:
        bootstrap_schema(conn)
        schema_info = validate_schema(conn)
        healthcheck(conn, bot_name)
    except Exception:
        conn.close()
        raise

    post_size = resolved.stat().st_size if resolved.exists() else 0
    logger.info(
        "[JOURNAL] bot=%s path=%s size_before=%d size_after=%d schema_version=%s tables_ok=trades,trade_events writable_test=pass",
        bot_name,
        resolved,
        pre_size,
        post_size,
        schema_info["schema_version"],
    )
    return conn


def get_db() -> sqlite3.Connection:
    return open_journal(DB_PATH, "journal")


def journal_open(
    conn: sqlite3.Connection,
    *,
    trade_id: str | None = None,
    engine: str,
    asset: str,
    category: str,
    direction: str,
    entry_price: float = 0.0,
    position_size: float = 0.0,
    position_size_usd: float = 0.0,
    momentum_score: float = 0.0,
    edge_percent: float = 0.0,
    confidence: float = 0.0,
    regime: str = "normal",
    notes: str = "",
    timestamp_open: str | None = None,
) -> str:
    trade_id = trade_id or str(uuid.uuid4())
    timestamp_open = timestamp_open or _utc_now()

    existing = conn.execute(
        "SELECT id, timestamp_close FROM trades WHERE trade_id=?",
        (trade_id,),
    ).fetchone()
    if existing is not None:
        raise RuntimeError(f"trade_id already exists: {trade_id}")

    cur = conn.execute(
        """
        INSERT INTO trades (
            trade_id, engine, timestamp_open, asset, category, direction,
            entry_price, position_size, position_size_usd,
            momentum_score, edge_percent, confidence, regime, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_id,
            engine,
            timestamp_open,
            asset,
            category,
            direction,
            entry_price,
            position_size,
            position_size_usd,
            momentum_score,
            edge_percent,
            confidence,
            regime,
            notes,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"failed to insert open row for trade_id={trade_id}")

    conn.commit()
    logger.info("Trade opened: %s %s %s @ %.6f", trade_id[:8], engine, asset, entry_price)
    return trade_id


def journal_close(
    conn: sqlite3.Connection,
    *,
    trade_id: str,
    exit_price: float = 0.0,
    pnl_absolute: float = 0.0,
    pnl_percent: float = 0.0,
    exit_type: str = "MANUAL",
    hold_duration_seconds: int = 0,
    timestamp_close: str | None = None,
    notes: str | None = None,
) -> None:
    timestamp_close = timestamp_close or _utc_now()
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

    set_clause = ", ".join(f"{column}=?" for column in updates)
    values = list(updates.values()) + [trade_id]
    cur = conn.execute(
        f"""
        UPDATE trades
        SET {set_clause}
        WHERE trade_id=? AND timestamp_close IS NULL
        """,
        values,
    )
    if cur.rowcount != 1:
        conn.rollback()
        raise RuntimeError(
            f"journal_close expected exactly one open row for trade_id={trade_id}, updated={cur.rowcount}"
        )

    conn.commit()
    logger.info(
        "Trade closed: %s exit=%.6f pnl=%.2f (%+.1f%%)",
        trade_id[:8],
        exit_price,
        pnl_absolute,
        pnl_percent,
    )


def journal_event(
    conn: sqlite3.Connection,
    *,
    engine: str,
    event_type: str,
    asset: str = "",
    direction: str = "",
    price: float | None = None,
    quantity: float | None = None,
    value_usd: float | None = None,
    notes: str = "",
    trade_id: str | None = None,
    timestamp: str | None = None,
    event_id: str | None = None,
) -> str:
    event_id = event_id or str(uuid.uuid4())
    timestamp = timestamp or _utc_now()
    cur = conn.execute(
        """
        INSERT INTO trade_events (
            event_id, trade_id, engine, event_type, timestamp, asset,
            direction, price, quantity, value_usd, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            trade_id,
            engine,
            event_type,
            timestamp,
            asset,
            direction,
            price,
            quantity,
            value_usd,
            notes,
        ),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"failed to insert journal event {event_id}")
    conn.commit()
    return event_id


def log_trade_open(**kwargs: Any) -> str:
    conn = get_db()
    try:
        return journal_open(conn, **kwargs)
    finally:
        conn.close()


def log_trade_close(
    trade_id: str,
    exit_price: float = 0.0,
    pnl_absolute: float = 0.0,
    pnl_percent: float = 0.0,
    exit_type: str = "MANUAL",
    hold_duration_seconds: int = 0,
    timestamp_close: str | None = None,
    notes: str | None = None,
) -> bool:
    conn = get_db()
    try:
        journal_close(
            conn,
            trade_id=trade_id,
            exit_price=exit_price,
            pnl_absolute=pnl_absolute,
            pnl_percent=pnl_percent,
            exit_type=exit_type,
            hold_duration_seconds=hold_duration_seconds,
            timestamp_close=timestamp_close,
            notes=notes,
        )
        return True
    finally:
        conn.close()


def get_trades(
    engine: str | None = None,
    days: int | None = None,
    limit: int = 1000,
    closed_only: bool = False,
) -> list[dict[str, Any]]:
    conn = get_db()
    try:
        conditions: list[str] = []
        params: list[Any] = []

        if engine:
            conditions.append("engine = ?")
            params.append(engine)
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            conditions.append("timestamp_open >= ?")
            params.append(cutoff.isoformat())
        if closed_only:
            conditions.append("timestamp_close IS NOT NULL")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY timestamp_open DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def migrate_json_logs() -> int:
    inserted = 0
    conn = get_db()
    try:
        if TRADE_LOG_JSON.exists():
            try:
                log_entries = json.loads(TRADE_LOG_JSON.read_text())
                for entry in log_entries:
                    asset = (
                        entry.get("mint")
                        or entry.get("token_address")
                        or entry.get("sym")
                        or entry.get("asset")
                        or ""
                    )
                    direction = (
                        entry.get("event")
                        or entry.get("action")
                        or "BUY"
                    ).upper()
                    entry_price = float(
                        entry.get("entry")
                        or entry.get("entry_price")
                        or entry.get("price")
                        or 0
                    )
                    exit_price = float(entry.get("exit") or entry.get("exit_price") or 0)
                    pnl_percent = float(entry.get("pct") or entry.get("pnl_percent") or 0)
                    ts_open = entry.get("ts") or entry.get("timestamp_open") or entry.get("timestamp") or _utc_now()
                    trade_id = entry.get("trade_id") or str(uuid.uuid4())

                    try:
                        journal_open(
                            conn,
                            trade_id=trade_id,
                            engine="solana",
                            asset=asset,
                            category=entry.get("category") or "legacy",
                            direction=direction,
                            entry_price=entry_price,
                            position_size=float(entry.get("position_size") or 0),
                            position_size_usd=float(entry.get("position_size_usd") or 0),
                            momentum_score=float(entry.get("momentum_score") or entry.get("score") or 0),
                            regime=entry.get("regime") or "",
                            notes=json.dumps(entry)[:1000],
                            timestamp_open=ts_open,
                        )
                        inserted += 1
                    except RuntimeError:
                        pass

                    if exit_price:
                        try:
                            journal_close(
                                conn,
                                trade_id=trade_id,
                                exit_price=exit_price,
                                pnl_absolute=float(entry.get("pnl_absolute") or entry.get("pnl") or 0),
                                pnl_percent=pnl_percent,
                                exit_type=entry.get("exit_type") or direction,
                                hold_duration_seconds=int(entry.get("hold_duration_seconds") or 0),
                                timestamp_close=entry.get("timestamp_close") or ts_open,
                                notes=json.dumps(entry)[:1000],
                            )
                        except RuntimeError:
                            pass
            except Exception as exc:
                logger.error("Solana log migration failed: %s", exc)

        if POLY_TRADE_LOG_JSON.exists():
            try:
                log_entries = json.loads(POLY_TRADE_LOG_JSON.read_text())
                for entry in log_entries:
                    trade_id = entry.get("trade_id") or entry.get("id") or entry.get("order_id") or str(uuid.uuid4())
                    try:
                        journal_open(
                            conn,
                            trade_id=trade_id,
                            engine="polymarket",
                            asset=(entry.get("market") or "")[:80],
                            category=entry.get("category") or "legacy",
                            direction=(entry.get("action") or "BUY").upper(),
                            entry_price=float(entry.get("price") or 0),
                            position_size=float(entry.get("amount") or 0),
                            position_size_usd=float(entry.get("price") or 0) * float(entry.get("amount") or 0),
                            notes=entry.get("error") or "",
                            timestamp_open=entry.get("timestamp") or _utc_now(),
                        )
                        inserted += 1
                    except RuntimeError:
                        pass
            except Exception as exc:
                logger.error("Polymarket log migration failed: %s", exc)
        return inserted
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    conn = open_journal(DB_PATH, "journal_cli")
    conn.close()
    print(f"Journal ready at {DB_PATH}")
