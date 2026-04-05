#!/usr/bin/env python3
"""ETH shadow calibration report from journal.db."""

import argparse
import sqlite3
from pathlib import Path

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
DB_PATH = ROOT / "journal.db"

DEDUPE_TRADES_CTE = """
WITH ranked_trades AS (
    SELECT
        rowid AS _rowid,
        trades.*,
        ROW_NUMBER() OVER (
            PARTITION BY engine, asset, timestamp_open, direction
            ORDER BY rowid DESC
        ) AS lifecycle_rn
    FROM trades
),
dedup_trades AS (
    SELECT *
    FROM ranked_trades
    WHERE engine NOT IN ('btc15m', 'eth15m') OR lifecycle_rn = 1
)
"""


def run_report(hours: int) -> None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    c = conn.cursor()

    window_filter = "datetime('now', ?)"
    window_arg = f"-{int(hours)} hours"

    c.execute(
        """
        SELECT COUNT(*)
        FROM edge_events
        WHERE engine='eth15m' AND datetime(timestamp_et) >= """ + window_filter,
        (window_arg,),
    )
    total_events = int(c.fetchone()[0] or 0)

    c.execute(
        """
        SELECT
            COALESCE(shadow_decision, '') AS shadow_decision,
            COUNT(*) AS n
        FROM edge_events
        WHERE engine='eth15m' AND datetime(timestamp_et) >= """ + window_filter + """
        GROUP BY COALESCE(shadow_decision, '')
        ORDER BY n DESC
        """,
        (window_arg,),
    )
    tagged = c.fetchall()

    c.execute(
        """
        SELECT decision, COUNT(*) AS n
        FROM edge_events
        WHERE engine='eth15m' AND datetime(timestamp_et) >= """ + window_filter + """
        GROUP BY decision
        ORDER BY n DESC
        """,
        (window_arg,),
    )
    decisions = c.fetchall()

    c.execute(
        DEDUPE_TRADES_CTE
        + """
        , place_ranked AS (
            SELECT
                rowid AS edge_rowid,
                market_slug,
                shadow_decision,
                timestamp_et,
                ROW_NUMBER() OVER (
                    PARTITION BY market_slug
                    ORDER BY timestamp_et DESC, rowid DESC
                ) AS rn
            FROM edge_events
            WHERE engine='eth15m'
              AND decision='place_yes'
              AND datetime(timestamp_et) >= datetime('now', ?)
        ),
        place_events AS (
            SELECT market_slug, shadow_decision
            FROM place_ranked
            WHERE rn=1
        )
        SELECT
            COALESCE(p.shadow_decision, '') AS shadow_decision,
            COUNT(*) AS placed_markets,
            SUM(CASE WHEN t.timestamp_close IS NOT NULL THEN 1 ELSE 0 END) AS resolved_markets,
            SUM(CASE WHEN COALESCE(t.pnl_absolute, 0) > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN COALESCE(t.pnl_absolute, 0) < 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(COALESCE(SUM(t.pnl_absolute), 0), 2) AS pnl
        FROM place_events p
        LEFT JOIN dedup_trades t
            ON t.engine='eth15m'
           AND t.asset=p.market_slug
           AND t.exit_type='resolved'
        GROUP BY COALESCE(p.shadow_decision, '')
        ORDER BY placed_markets DESC
        """,
        (window_arg,),
    )
    linked = c.fetchall()
    conn.close()

    print("ETH Shadow Calibration Report")
    print(f"Window: last {hours}h")
    print(f"Total edge events: {total_events}")
    print("")
    print("Shadow Tag Distribution")
    for shadow_decision, n in tagged:
        label = shadow_decision or "<blank>"
        print(f"- {label}: {int(n)}")
    print("")
    print("Live Decision Distribution")
    for decision, n in decisions:
        print(f"- {decision}: {int(n)}")
    print("")
    print("Placed-Market Outcome Linkage (deduped trades)")
    for shadow_decision, placed, resolved, wins, losses, pnl in linked:
        label = shadow_decision or "<blank>"
        wr = (float(wins) / float(resolved) * 100.0) if resolved else 0.0
        print(
            f"- shadow={label} placed={int(placed)} resolved={int(resolved)} "
            f"wins={int(wins)} losses={int(losses)} win_rate={wr:.2f}% pnl=${float(pnl):.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="ETH shadow calibration report")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours")
    args = parser.parse_args()
    run_report(max(1, int(args.hours)))


if __name__ == "__main__":
    main()
