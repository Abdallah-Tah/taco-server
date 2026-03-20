#!/usr/bin/env python3
"""
journal/analytics.py — Query helpers for the Taco Trade Journal.

All functions are safe to call even if the DB has no data yet.
"""
import logging
import math
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from .db import _connect, init_db

logger = logging.getLogger(__name__)


def _safe(fn):
    """Decorator: init DB if needed, return default on any error."""
    def wrapper(*args, **kwargs):
        try:
            init_db()
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.error("%s error: %s", fn.__name__, exc)
            return None
    return wrapper


@_safe
def get_win_rate(engine: Optional[str] = None, days: Optional[int] = None) -> dict:
    """
    Compute win rate for closed trades.
    Returns {'win_rate': float, 'wins': int, 'losses': int, 'total': int}
    """
    params: list = []
    clauses = ["exit_type NOT IN ('open','')"]

    if engine:
        clauses.append("engine = ?")
        params.append(engine)

    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        clauses.append("timestamp_open >= ?")
        params.append(since)

    where = " AND ".join(clauses)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) total, "
            f"SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) wins, "
            f"SUM(CASE WHEN pnl_percent <= 0 THEN 1 ELSE 0 END) losses "
            f"FROM trades WHERE {where}",
            params,
        ).fetchone()

    total  = row["total"] or 0
    wins   = row["wins"]  or 0
    losses = row["losses"] or 0
    return {
        "win_rate": wins / total if total else 0.0,
        "wins":     wins,
        "losses":   losses,
        "total":    total,
    }


@_safe
def get_pnl_by_category(engine: Optional[str] = None, days: Optional[int] = None) -> list:
    """
    PnL summary grouped by category.
    Returns list of {category, avg_pnl, total_pnl, count, win_rate}
    """
    params: list = []
    clauses = ["exit_type NOT IN ('open','')"]

    if engine:
        clauses.append("engine = ?")
        params.append(engine)
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        clauses.append("timestamp_open >= ?")
        params.append(since)

    where = " AND ".join(clauses)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT category, AVG(pnl_percent) avg_pnl, SUM(pnl_percent) total_pnl, "
            f"COUNT(*) cnt, "
            f"SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END) wins "
            f"FROM trades WHERE {where} GROUP BY category ORDER BY total_pnl DESC",
            params,
        ).fetchall()

    return [
        {
            "category":  r["category"],
            "avg_pnl":   round(r["avg_pnl"] or 0, 3),
            "total_pnl": round(r["total_pnl"] or 0, 3),
            "count":     r["cnt"],
            "win_rate":  round((r["wins"] or 0) / r["cnt"], 3) if r["cnt"] else 0,
        }
        for r in rows
    ]


@_safe
def get_avg_hold_duration(engine: Optional[str] = None) -> dict:
    """Average hold duration in seconds for closed trades."""
    params: list = []
    clauses = ["exit_type NOT IN ('open','')", "hold_duration_seconds > 0"]

    if engine:
        clauses.append("engine = ?")
        params.append(engine)

    where = " AND ".join(clauses)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT AVG(hold_duration_seconds) avg_s, MAX(hold_duration_seconds) max_s, "
            f"MIN(hold_duration_seconds) min_s FROM trades WHERE {where}",
            params,
        ).fetchone()

    avg_s = row["avg_s"] or 0
    return {
        "avg_seconds": round(avg_s, 1),
        "avg_hours":   round(avg_s / 3600, 2),
        "max_seconds": round(row["max_s"] or 0, 1),
        "min_seconds": round(row["min_s"] or 0, 1),
    }


@_safe
def get_best_worst_trades(n: int = 5, engine: Optional[str] = None) -> dict:
    """Return top-n best and worst trades by pnl_percent."""
    params: list = []
    clauses = ["exit_type NOT IN ('open','')"]

    if engine:
        clauses.append("engine = ?")
        params.append(engine)

    where = " AND ".join(clauses)
    with _connect() as conn:
        best = conn.execute(
            f"SELECT asset, category, pnl_percent, exit_type, timestamp_close "
            f"FROM trades WHERE {where} ORDER BY pnl_percent DESC LIMIT ?",
            params + [n],
        ).fetchall()
        worst = conn.execute(
            f"SELECT asset, category, pnl_percent, exit_type, timestamp_close "
            f"FROM trades WHERE {where} ORDER BY pnl_percent ASC LIMIT ?",
            params + [n],
        ).fetchall()

    def _fmt(rows):
        return [dict(r) for r in rows]

    return {"best": _fmt(best), "worst": _fmt(worst)}


@_safe
def get_regime_history(limit: int = 50) -> list:
    """Return recent regime change log entries."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM regime_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


@_safe
def get_correlation_exposure(engine: str = "polymarket") -> dict:
    """
    Show currently open positions grouped by shared keywords.
    Returns {'groups': [{keywords, positions, count}], 'max_group': int}
    """
    from pathlib import Path
    import json

    # Pull open positions from JSON file (real-time state)
    trading_dir = Path(__file__).parent.parent
    pos_file = trading_dir / (".poly_positions.json" if engine == "polymarket" else ".positions.json")

    if not pos_file.exists():
        return {"groups": [], "max_group": 0}

    try:
        positions = json.loads(pos_file.read_text())
    except Exception:
        return {"groups": [], "max_group": 0}

    # Simple keyword extraction: split question into significant words
    import re
    stop_words = {
        "will", "the", "a", "an", "in", "of", "to", "be", "by", "on",
        "at", "for", "is", "are", "was", "were", "and", "or", "not",
        "2025", "2026", "2027", "end", "before", "after", "than", "by",
        "with", "from", "its", "it", "this", "that", "which", "who",
    }

    def keywords(text: str) -> set:
        words = re.findall(r"[a-z]+", text.lower())
        return {w for w in words if len(w) > 3 and w not in stop_words}

    items = []
    for token_id, pos in positions.items():
        market = pos.get("market", token_id)
        items.append({"id": token_id, "market": market, "kws": keywords(market)})

    groups: list = []
    used: set = set()

    for i, a in enumerate(items):
        if i in used:
            continue
        group = [a]
        used.add(i)
        for j, b in enumerate(items):
            if j <= i or j in used:
                continue
            shared = a["kws"] & b["kws"]
            if len(shared) >= 2:  # CORRELATION_MIN_KEYWORDS default
                group.append(b)
                used.add(j)

        if len(group) > 1:
            shared_all = group[0]["kws"]
            for g in group[1:]:
                shared_all = shared_all & g["kws"]
            groups.append({
                "keywords": sorted(shared_all)[:5],
                "positions": [g["market"] for g in group],
                "count": len(group),
            })

    max_group = max((g["count"] for g in groups), default=0)
    return {"groups": groups, "max_group": max_group}


@_safe
def get_streak() -> dict:
    """Current win/loss streak based on most recent closed trades."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pnl_percent FROM trades "
            "WHERE exit_type NOT IN ('open','') "
            "ORDER BY timestamp_close DESC LIMIT 20"
        ).fetchall()

    if not rows:
        return {"streak": 0, "type": "none"}

    streak = 1
    first = rows[0]["pnl_percent"] > 0

    for row in rows[1:]:
        win = row["pnl_percent"] > 0
        if win == first:
            streak += 1
        else:
            break

    return {"streak": streak, "type": "win" if first else "loss"}


@_safe
def check_milestones(current_capital: float, start_ts: Optional[str] = None) -> list:
    """
    Check if any new milestones have been hit.
    Returns list of newly-hit milestone amounts.
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from config import MILESTONE_AMOUNTS
    from .db import log_milestone

    with _connect() as conn:
        already = {
            row["amount"]
            for row in conn.execute("SELECT amount FROM milestones").fetchall()
        }

    newly_hit = []
    for m in MILESTONE_AMOUNTS:
        if current_capital >= m and m not in already:
            # Gather stats
            wr    = get_win_rate()
            bw    = get_best_worst_trades(n=1)
            durations = []

            with _connect() as conn:
                rows = conn.execute("SELECT pnl_percent FROM trades WHERE exit_type NOT IN ('open','')").fetchall()
            pcts = [r["pnl_percent"] for r in rows]

            avg_r  = sum(pcts) / len(pcts) if pcts else 0.0
            std_r  = math.sqrt(sum((p - avg_r) ** 2 for p in pcts) / len(pcts)) if len(pcts) > 1 else 0.0
            best   = bw["best"][0]["pnl_percent"]  if bw and bw["best"]  else 0.0
            worst  = bw["worst"][0]["pnl_percent"] if bw and bw["worst"] else 0.0

            # Days since start
            days_since = 0
            if start_ts:
                try:
                    t0 = datetime.fromisoformat(start_ts)
                    days_since = max(0, (datetime.now(timezone.utc) - t0.replace(tzinfo=timezone.utc)).days)
                except Exception:
                    pass

            log_milestone(m, {
                "days_since_start": days_since,
                "total_trades":     len(pcts),
                "win_rate":         wr["win_rate"] if wr else 0.0,
                "avg_return":       avg_r,
                "std_return":       std_r,
                "best_trade":       best,
                "worst_trade":      worst,
            })
            newly_hit.append(m)

    return newly_hit
