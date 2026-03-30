#!/usr/bin/env python3
"""
milestones.py — Portfolio milestone tracker.

Tracks progress toward capital milestones. Logs and stores reached milestones.
"""
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
try:
    from config import MILESTONES, GOAL_USD
except ImportError:
    MILESTONES = [150, 250, 500, 1000, 2000, 2780]
    GOAL_USD = 2780.0

logger = logging.getLogger(__name__)

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
MILESTONES_FILE = ROOT / ".milestones.json"
TRADE_LOG_FILE = ROOT / ".trade_log.json"
POLY_TRADE_LOG_FILE = ROOT / ".poly_trade_log.json"


def _load_milestones_state() -> dict:
    """Load milestone tracking state from disk."""
    if MILESTONES_FILE.exists():
        try:
            return json.loads(MILESTONES_FILE.read_text())
        except Exception:
            pass
    return {"reached": [], "start_time": datetime.now(timezone.utc).isoformat()}


def _save_milestones_state(state: dict):
    MILESTONES_FILE.write_text(json.dumps(state, indent=2))


def _count_trades() -> int:
    """Count total trades across both log files."""
    total = 0
    for f in [TRADE_LOG_FILE, POLY_TRADE_LOG_FILE]:
        if f.exists():
            try:
                log = json.loads(f.read_text())
                total += len(log)
            except Exception:
                pass
    return total


def _calc_win_rate() -> float:
    """Rough win rate from trade logs (wins / total closed trades)."""
    wins = 0
    total = 0
    for f in [TRADE_LOG_FILE, POLY_TRADE_LOG_FILE]:
        if f.exists():
            try:
                log = json.loads(f.read_text())
                for entry in log:
                    pnl = entry.get("pnl_absolute") or entry.get("pnl") or 0
                    action = entry.get("action", "")
                    if action in ("SELL", "CLOSE", "PARTIAL_TP", "TAKE_PROFIT") or pnl != 0:
                        total += 1
                        if float(pnl) > 0:
                            wins += 1
            except Exception:
                pass
    return (wins / total * 100) if total > 0 else 0.0


def check_milestones(current_capital: float, reached_milestones: list = None) -> list:
    """
    Check for newly reached milestones.

    Args:
        current_capital: Current total portfolio value in USD.
        reached_milestones: List of already-reached milestone amounts (or None to load from file).

    Returns:
        List of newly reached milestone amounts.
    """
    state = _load_milestones_state()
    if reached_milestones is None:
        reached_milestones = state.get("reached", [])

    newly_reached = []
    for milestone in MILESTONES:
        if milestone not in reached_milestones and current_capital >= milestone:
            newly_reached.append(milestone)

    if newly_reached:
        # Calculate stats for logging
        start_time_str = state.get("start_time", datetime.now(timezone.utc).isoformat())
        try:
            start_time = datetime.fromisoformat(start_time_str)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=timezone.utc)
            days_elapsed = (datetime.now(timezone.utc) - start_time).days
        except Exception:
            days_elapsed = 0

        trade_count = _count_trades()
        win_rate = _calc_win_rate()

        for milestone in newly_reached:
            msg = (
                f"MILESTONE: ${milestone} reached! "
                f"Days: {days_elapsed}, "
                f"Trades: {trade_count}, "
                f"Win rate: {win_rate:.1f}%"
            )
            logger.info(msg)
            print(f"🏆 {msg}")

            # Append to state
            state.setdefault("reached", []).append(milestone)
            state[f"reached_{milestone}"] = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "capital": current_capital,
                "days": days_elapsed,
                "trades": trade_count,
                "win_rate": win_rate,
            }

        _save_milestones_state(state)

    return newly_reached


def get_progress() -> dict:
    """Get milestone progress summary."""
    state = _load_milestones_state()
    reached = state.get("reached", [])
    next_milestone = next((m for m in MILESTONES if m not in reached), None)
    return {
        "reached": reached,
        "next": next_milestone,
        "goal": GOAL_USD,
        "all_milestones": MILESTONES,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    capital = float(sys.argv[1]) if len(sys.argv) > 1 else 100.0
    newly = check_milestones(capital)
    progress = get_progress()
    print(f"Reached: {progress['reached']}")
    print(f"Next milestone: ${progress['next']}")
    print(f"Goal: ${progress['goal']}")
