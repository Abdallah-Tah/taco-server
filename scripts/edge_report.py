#!/usr/bin/env python3
"""Read-only edge telemetry comparison report."""
from __future__ import annotations

import argparse
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from journal import DB_PATH


def is_live_place(decision):
    return isinstance(decision, str) and decision.startswith("place_")


def is_live_skip(decision):
    return isinstance(decision, str) and decision.startswith("skip_")


def _as_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _as_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def infer_shadow_decision(row, has_shadow_decision=False):
    if has_shadow_decision:
        decision = row.get("shadow_decision")
        if isinstance(decision, str) and (decision.startswith("place_") or decision.startswith("skip_")):
            return decision, row.get("shadow_skip_reason")

    regime_ok = _as_int(row.get("regime_ok"))
    net_edge = _as_float(row.get("net_edge"))
    if regime_ok == 0:
        spread = _as_float(row.get("spread"))
        if spread is not None and spread > 0.20:
            return "skip_spread", "spread_too_wide"
        return "skip_regime", "regime_not_ok"
    if net_edge is None:
        if _as_float(row.get("model_p_yes")) is not None or _as_float(row.get("model_p_no")) is not None:
            return "skip_data", "insufficient_features"
        return None, None
    if abs(net_edge) < 0.01:
        return "skip_no_edge", "net_edge_below_floor"
    if net_edge > 0:
        return "place_yes", None
    return "place_no", None


def shadow_is_trade(shadow_decision):
    return isinstance(shadow_decision, str) and shadow_decision.startswith("place_")


def shadow_is_skip(shadow_decision):
    return isinstance(shadow_decision, str) and shadow_decision.startswith("skip_")


def disagreement_bucket(live_decision, shadow_decision):
    live_place = is_live_place(live_decision)
    live_skip = is_live_skip(live_decision)
    shadow_place = shadow_is_trade(shadow_decision)
    shadow_skip = shadow_is_skip(shadow_decision)
    if live_place and shadow_skip:
        return "live_place_shadow_skip"
    if live_skip and shadow_place:
        return "live_skip_shadow_trade"
    if live_place and shadow_place:
        return "both_place"
    if live_skip and shadow_skip:
        return "both_skip"
    return "unknown"


def net_edge_bucket(net_edge):
    value = _as_float(net_edge)
    if value is None:
        return "null"
    if value < 0:
        return "<0"
    if value < 0.01:
        return "0_to_0.01"
    if value < 0.02:
        return "0.01_to_0.02"
    if value <= 0.05:
        return "0.02_to_0.05"
    return ">0.05"


def hour_et(timestamp_et):
    if not timestamp_et:
        return None
    try:
        return datetime.fromisoformat(str(timestamp_et)).hour
    except Exception:
        return None


def _print_section(title):
    print(f"\n{title}")
    print("-" * len(title))


def _open_read_conn(db_path=None):
    if db_path:
        resolved = Path(db_path).expanduser()
    else:
        env_path = os.environ.get("_JOURNAL_TEST_DB") or os.environ.get("EDGE_REPORT_DB")
        resolved = Path(env_path).expanduser() if env_path else Path(DB_PATH)
    if not resolved.exists():
        raise FileNotFoundError(f"journal DB not found: {resolved}")
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    return conn


def load_edge_events(asset=None, limit=None, db_path=None):
    conn = _open_read_conn(db_path=db_path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(edge_events)").fetchall()}
    has_shadow_decision = "shadow_decision" in columns
    has_shadow_skip_reason = "shadow_skip_reason" in columns

    base_cols = [
        "id",
        "asset",
        "timestamp_et",
        "decision",
        "skip_reason",
        "spread",
        "net_edge",
        "model_p_yes",
        "model_p_no",
        "regime_ok",
    ]
    if has_shadow_decision:
        base_cols.append("shadow_decision")
    if has_shadow_skip_reason:
        base_cols.append("shadow_skip_reason")

    sql = f"SELECT {', '.join(base_cols)} FROM edge_events"
    params = []
    where = []
    if asset:
        where.append("asset = ?")
        params.append(asset.upper())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY rowid DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    conn.close()
    return rows, has_shadow_decision


def build_report(rows, has_shadow_decision=False):
    totals = Counter()
    disagreement = Counter()
    by_asset = {key: Counter() for key in ("BTC", "ETH")}
    by_hour = defaultdict(Counter)
    shadow_skip_reasons = Counter()
    live_skip_reasons = Counter()
    quality = {k: Counter() for k in ("<0", "0_to_0.01", "0.01_to_0.02", "0.02_to_0.05", ">0.05", "null")}

    for row in rows:
        asset = (row.get("asset") or "").upper()
        live_decision = row.get("decision")
        shadow_decision, inferred_shadow_reason = infer_shadow_decision(
            row, has_shadow_decision=has_shadow_decision
        )
        live_place = is_live_place(live_decision)
        live_skip = is_live_skip(live_decision)
        shadow_trade = shadow_is_trade(shadow_decision)
        shadow_skip = shadow_is_skip(shadow_decision)
        bucket = disagreement_bucket(live_decision, shadow_decision)

        totals["events"] += 1
        if live_place:
            totals["live_place"] += 1
        if live_skip:
            totals["live_skip"] += 1
        if shadow_trade:
            totals["shadow_trade"] += 1
        if shadow_skip:
            totals["shadow_skip"] += 1
        disagreement[bucket] += 1

        if asset in by_asset:
            by_asset[asset]["events"] += 1
            if live_place:
                by_asset[asset]["live_place"] += 1
            if shadow_trade:
                by_asset[asset]["shadow_trade"] += 1
            if bucket in ("live_place_shadow_skip", "live_skip_shadow_trade"):
                by_asset[asset]["disagreement"] += 1

        hr = hour_et(row.get("timestamp_et"))
        if hr is not None:
            by_hour[hr]["events"] += 1
            if live_place:
                by_hour[hr]["live_place"] += 1
            if shadow_trade:
                by_hour[hr]["shadow_trade"] += 1
            if bucket in ("live_place_shadow_skip", "live_skip_shadow_trade"):
                by_hour[hr]["disagreement"] += 1

        if live_skip:
            live_skip_reasons[row.get("skip_reason") or "unknown"] += 1
        if shadow_skip:
            shadow_reason = row.get("shadow_skip_reason") or inferred_shadow_reason
            if shadow_reason is None:
                shadow_reason = "unknown"
            shadow_skip_reasons[shadow_reason] += 1

        q = net_edge_bucket(row.get("net_edge"))
        quality[q]["events"] += 1
        if live_place:
            quality[q]["live_place"] += 1
        if shadow_trade:
            quality[q]["shadow_trade"] += 1

    _print_section("A. Event Counts")
    print(f"total edge events: {totals['events']}")
    print(f"total live place_* decisions: {totals['live_place']}")
    print(f"total live skip_* decisions: {totals['live_skip']}")
    print(f"total shadow trade decisions: {totals['shadow_trade']}")
    print(f"total shadow skip decisions: {totals['shadow_skip']}")

    _print_section("B. Live vs Shadow Disagreement")
    print(f"live place, shadow skip: {disagreement['live_place_shadow_skip']}")
    print(f"live skip, shadow trade: {disagreement['live_skip_shadow_trade']}")
    print(f"both place: {disagreement['both_place']}")
    print(f"both skip: {disagreement['both_skip']}")

    _print_section("C. Breakdown by Asset")
    for asset in ("BTC", "ETH"):
        print(
            f"{asset}: events={by_asset[asset]['events']} "
            f"live_place={by_asset[asset]['live_place']} "
            f"shadow_trade={by_asset[asset]['shadow_trade']} "
            f"disagreement={by_asset[asset]['disagreement']}"
        )

    _print_section("D. Breakdown by Hour ET")
    if not by_hour:
        print("no timestamp_et rows")
    else:
        for hr in sorted(by_hour):
            print(
                f"{hr:02d}:00 events={by_hour[hr]['events']} "
                f"live_place={by_hour[hr]['live_place']} "
                f"shadow_trade={by_hour[hr]['shadow_trade']} "
                f"disagreement={by_hour[hr]['disagreement']}"
            )

    _print_section("E. Breakdown by Shadow Skip Reason")
    if not shadow_skip_reasons:
        print("none")
    else:
        for reason, count in shadow_skip_reasons.most_common(12):
            print(f"{reason}: {count}")

    _print_section("F. Breakdown by Live Skip Reason")
    if not live_skip_reasons:
        print("none")
    else:
        for reason, count in live_skip_reasons.most_common(12):
            print(f"{reason}: {count}")

    _print_section("G. Quality Buckets (net_edge)")
    for key in ("<0", "0_to_0.01", "0.01_to_0.02", "0.02_to_0.05", ">0.05"):
        print(
            f"{key}: events={quality[key]['events']} "
            f"live_place={quality[key]['live_place']} "
            f"shadow_trade={quality[key]['shadow_trade']}"
        )


def main():
    parser = argparse.ArgumentParser(description="Edge telemetry live-vs-shadow report")
    parser.add_argument("--asset", choices=["BTC", "ETH"], help="Filter by asset")
    parser.add_argument("--limit", type=int, help="Limit to most recent N events")
    parser.add_argument("--db", help="Optional path to journal SQLite DB")
    args = parser.parse_args()

    try:
        rows, has_shadow_decision = load_edge_events(asset=args.asset, limit=args.limit, db_path=args.db)
    except FileNotFoundError as e:
        print(str(e))
        raise SystemExit(2)
    except sqlite3.Error as e:
        print(f"failed to read edge events: {e}")
        raise SystemExit(2)
    build_report(rows, has_shadow_decision=has_shadow_decision)


if __name__ == "__main__":
    main()
