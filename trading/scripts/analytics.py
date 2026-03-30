#!/usr/bin/env python3
"""
analytics.py — Trade analytics engine for Taco Trader.

Queries journal.db to produce win rates, PnL breakdowns, streaks, etc.
"""
import importlib.util
import logging
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# Load sibling scripts/journal.py directly to avoid collision with trading/journal package
_JOURNAL_PATH = Path(__file__).with_name('journal.py')
_spec = importlib.util.spec_from_file_location('scripts_journal_file', _JOURNAL_PATH)
_journal_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_journal_mod)
get_db = _journal_mod.get_db


def _get_closed_trades(engine: str = None, days: int = None) -> list:
    """Helper: fetch closed trades with optional filters."""
    try:
        conn = get_db()
        conditions = ["timestamp_close IS NOT NULL", "exit_price IS NOT NULL", "exit_price > 0"]
        params = []

        if engine:
            conditions.append("engine = ?")
            params.append(engine)
        if days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            conditions.append("timestamp_open >= ?")
            params.append(cutoff)

        where = "WHERE " + " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY timestamp_close DESC",
            params,
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to fetch closed trades: %s", e)
        return []


def get_win_rate(engine: str = None, days: int = None) -> float:
    """
    Calculate win rate (% of profitable closed trades).

    Args:
        engine: Filter by engine or None for all.
        days: Last N days or None for all time.

    Returns:
        Win rate as a float 0.0–100.0.
    """
    trades = _get_closed_trades(engine=engine, days=days)
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("pnl_absolute") or t.get("pnl_percent") or 0) > 0)
    return (wins / len(trades)) * 100.0


def get_pnl_by_category(engine: str = None, days: int = None) -> dict:
    """
    Get total PnL grouped by category.

    Returns:
        Dict of {category: total_pnl_usd}
    """
    trades = _get_closed_trades(engine=engine, days=days)
    result = {}
    for t in trades:
        cat = t.get("category") or "unknown"
        pnl = float(t.get("pnl_absolute") or 0)
        result[cat] = result.get(cat, 0.0) + pnl
    return result


def get_avg_hold_duration(engine: str = None) -> float:
    """
    Get average hold duration in seconds.

    Returns:
        Average seconds held, or 0.0 if no data.
    """
    trades = _get_closed_trades(engine=engine)
    durations = [t["hold_duration_seconds"] for t in trades if t.get("hold_duration_seconds")]
    if not durations:
        return 0.0
    return sum(durations) / len(durations)


def get_best_worst_trades(n: int = 5) -> tuple:
    """
    Get the N best and N worst closed trades by pnl_percent.

    Returns:
        (best_trades: list, worst_trades: list)
    """
    try:
        conn = get_db()
        where = "WHERE timestamp_close IS NOT NULL AND pnl_percent IS NOT NULL"
        best = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY pnl_percent DESC LIMIT ?", (n,)
        ).fetchall()
        worst = conn.execute(
            f"SELECT * FROM trades {where} ORDER BY pnl_percent ASC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        return ([dict(r) for r in best], [dict(r) for r in worst])
    except Exception as e:
        logger.error("Failed to get best/worst trades: %s", e)
        return ([], [])


def get_regime_history() -> list:
    """
    Get list of unique regime periods from trade history.

    Returns:
        List of {regime, count, win_rate, avg_pnl, first_seen, last_seen}
    """
    try:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT
                regime,
                COUNT(*) as count,
                SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
                AVG(pnl_absolute) as avg_pnl,
                MIN(timestamp_open) as first_seen,
                MAX(timestamp_open) as last_seen
            FROM trades
            WHERE regime IS NOT NULL AND regime != ''
            GROUP BY regime
            ORDER BY last_seen DESC
            """
        ).fetchall()
        conn.close()
        result = []
        for r in rows:
            d = dict(r)
            total = d["count"]
            d["win_rate"] = (d["wins"] / total * 100) if total > 0 else 0.0
            result.append(d)
        return result
    except Exception as e:
        logger.error("Failed to get regime history: %s", e)
        return []


def get_correlation_exposure() -> dict:
    """
    Get current correlation exposure — open positions grouped by inferred thesis.

    Returns:
        Dict of {thesis_group: [asset names]}
    """
    try:
        conn = get_db()
        # Open positions: no close timestamp
        rows = conn.execute(
            "SELECT asset, category, engine FROM trades WHERE timestamp_close IS NULL"
        ).fetchall()
        conn.close()

        groups = {}
        for r in rows:
            cat = r["category"] or r["engine"] or "unknown"
            groups.setdefault(cat, []).append(r["asset"] or "?")
        return groups
    except Exception as e:
        logger.error("Failed to get correlation exposure: %s", e)
        return {}


def get_streak() -> dict:
    """
    Get current win/loss streak.

    Returns:
        {current_streak: int (positive=wins, negative=losses),
         max_win_streak: int,
         max_loss_streak: int}
    """
    try:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT pnl_absolute FROM trades
            WHERE timestamp_close IS NOT NULL AND pnl_absolute IS NOT NULL
            ORDER BY timestamp_close DESC
            LIMIT 100
            """
        ).fetchall()
        conn.close()

        if not rows:
            return {"current_streak": 0, "max_win_streak": 0, "max_loss_streak": 0}

        results = [1 if r["pnl_absolute"] > 0 else -1 for r in rows]

        # Current streak
        current = 0
        sign = results[0]
        for r in results:
            if r == sign:
                current += sign
            else:
                break

        # Max streaks
        max_win = max_loss = cur_win = cur_loss = 0
        for r in reversed(results):
            if r > 0:
                cur_win += 1
                cur_loss = 0
            else:
                cur_loss += 1
                cur_win = 0
            max_win = max(max_win, cur_win)
            max_loss = max(max_loss, cur_loss)

        return {
            "current_streak": current,
            "max_win_streak": max_win,
            "max_loss_streak": max_loss,
        }
    except Exception as e:
        logger.error("Failed to get streak: %s", e)
        return {"current_streak": 0, "max_win_streak": 0, "max_loss_streak": 0}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(f"Win rate (all time): {get_win_rate():.1f}%")
    print(f"Win rate (7d): {get_win_rate(days=7):.1f}%")
    print(f"PnL by category: {get_pnl_by_category()}")
    print(f"Avg hold: {get_avg_hold_duration()/3600:.1f}h")
    print(f"Streak: {get_streak()}")
    best, worst = get_best_worst_trades(3)
    print(f"Best trades: {[(t['asset'], t['pnl_absolute']) for t in best]}")
    print(f"Worst trades: {[(t['asset'], t['pnl_absolute']) for t in worst]}")
