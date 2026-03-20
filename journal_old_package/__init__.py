#!/usr/bin/env python3
"""
journal/__init__.py — Taco Trade Journal package.
Provides SQLite-backed trade storage with analytics helpers.
"""
from .db import (
    init_db,
    insert_trade,
    migrate_json_logs,
    get_db_path,
)
from .analytics import (
    get_win_rate,
    get_pnl_by_category,
    get_avg_hold_duration,
    get_best_worst_trades,
    get_regime_history,
    get_correlation_exposure,
    get_streak,
)

__all__ = [
    "init_db",
    "insert_trade",
    "migrate_json_logs",
    "get_db_path",
    "get_win_rate",
    "get_pnl_by_category",
    "get_avg_hold_duration",
    "get_best_worst_trades",
    "get_regime_history",
    "get_correlation_exposure",
    "get_streak",
]
