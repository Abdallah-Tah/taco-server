#!/usr/bin/env python3
"""Shadow outcome tracking report.

Classifies actual executed trades by entry price band:
  kept:     0.45 <= price <= 0.62  (the band we'd KEEP)
  filtered: outside that band       (the band we'd FILTER)

Reports realized PnL for each group — only actual trades, no simulation.

Usage:
  python3 scripts/shadow_report.py --btc
  python3 scripts/shadow_report.py --eth
  python3 scripts/shadow_report.py --all
  python3 scripts/shadow_report.py --all --json
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "journal.db"

KEPT_BAND = (0.45, 0.62)


def classify(entry_price: float) -> tuple[str, str]:
    if KEPT_BAND[0] <= entry_price <= KEPT_BAND[1]:
        return "kept", "allow_0.45_0.62"
    elif entry_price > 0.70:
        return "filtered", "block_gt_0.70"
    else:
        return "filtered", "outside_0.45_0.62"


def bucket(entry_price: float) -> str:
    lo = int(entry_price * 10) / 10
    return f"{lo:.1f}-{lo + 0.1:.1f}"


def report(engine_filter: str, fmt: str = "text"):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    where = "exit_type != ''"
    params: list = []
    if engine_filter == "btc":
        where += " AND engine LIKE '%btc%15m%'"
    elif engine_filter == "eth":
        where += " AND engine LIKE '%eth%15m%'"
    else:
        where += " AND (engine LIKE '%btc%15m%' OR engine LIKE '%eth%15m%')"

    rows = conn.execute(
        f"SELECT engine, entry_price, pnl_absolute, notes FROM trades WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    conn.close()

    # Classify each row
    results: dict[str, dict] = {}
    for r in rows:
        eng = "BTC" if "btc" in r["engine"] else "ETH"
        ep = r["entry_price"] or 0.5
        sc, _ = classify(ep)

        # Prefer note-level shadow tag if present
        if "shadow=kept" in (r["notes"] or ""):
            sc = "kept"
        elif "shadow=filtered" in (r["notes"] or ""):
            sc = "filtered"

        key = f"{eng}_{sc}"
        if key not in results:
            results[key] = {"engine": eng, "class": sc, "count": 0, "wins": 0, "pnl": 0.0}
        results[key]["count"] += 1
        results[key]["pnl"] += r["pnl_absolute"] or 0.0
        if (r["pnl_absolute"] or 0) > 0:
            results[key]["wins"] += 1

    if fmt == "json":
        print(json.dumps(list(results.values()), indent=2))
        return

    engines = sorted(set(k.split("_")[0] for k in results))
    for eng in engines:
        kept = results.get(f"{eng}_kept", {"count": 0, "wins": 0, "pnl": 0.0})
        filt = results.get(f"{eng}_filtered", {"count": 0, "wins": 0, "pnl": 0.0})

        kr = (kept["wins"] / kept["count"] * 100) if kept["count"] else 0
        fr = (filt["wins"] / filt["count"] * 100) if filt["count"] else 0
        ka = (kept["pnl"] / kept["count"]) if kept["count"] else 0
        fa = (filt["pnl"] / filt["count"]) if filt["count"] else 0

        print(f"\n{'='*40}")
        print(f"  {eng}-15m SHADOW OUTCOME TRACKING")
        print(f"{'='*40}")
        print(f"  KEPT (0.45-0.62 band):")
        print(f"    Count:      {kept['count']}")
        print(f"    Win rate:   {kr:.1f}%")
        print(f"    Avg PnL:   ${ka:.4f}")
        print(f"    Total PnL: ${kept['pnl']:.2f}")
        print(f"  FILTERED (outside band):")
        print(f"    Count:      {filt['count']}")
        print(f"    Win rate:   {fr:.1f}%")
        print(f"    Avg PnL:   ${fa:.4f}")
        print(f"    Total PnL: ${filt['pnl']:.2f}")
        print(f"  COMPARISON:")
        print(f"    Kept PnL - Filtered PnL = ${kept['pnl'] - filt['pnl']:.2f}")
        print(f"    Kept WR  - Filtered WR  = {kr - fr:+.1f}pp")


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--btc", action="store_true")
    g.add_argument("--eth", action="store_true")
    g.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    eng = "btc" if args.btc else "eth" if args.eth else "all"
    fmt = "json" if args.json else "text"
    report(eng, fmt)


if __name__ == "__main__":
    main()
