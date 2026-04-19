#!/usr/bin/env python3
"""
Polymarket 15m skip auditor.

Scans BTC15M / ETH15M bot logs for "FILTER ... < min X.XX, skipping low price"
events, correlates each with its prior [*-BOOK] line to extract direction +
token_id, then resolves each skipped window via Polymarket CLOB /prices-history
to determine whether the skipped signal would have paid out.

Writes a CSV summary and prints hit-rate + hypothetical P/L so the MIN_ENTRY
threshold becomes data-driven instead of guessed.

Usage:
    ./polymarket_skip_audit.py                    # last 24h, both assets
    ./polymarket_skip_audit.py --hours 72         # last 72h
    ./polymarket_skip_audit.py --asset btc        # btc only
    ./polymarket_skip_audit.py --since 2026-04-18 # from date
    ./polymarket_skip_audit.py --size 12          # use $12 per trade
"""
import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CLOB_HOST = "https://clob.polymarket.com"
DEFAULT_SIZE_USD = 12.00

LOGS = {
    "btc": "/tmp/polymarket_btc15m.log",
    "eth": "/tmp/polymarket_eth15m.log",
}

# [2026-04-18 11:51:41] [BTC-BOOK] ... direction=DOWN token_id=3584949...
BOOK_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(?P<asset>BTC|ETH)-BOOK\].*?"
    r"direction=(?P<direction>UP|DOWN).*?token_id=(?P<token_id>\d+)"
)
# [2026-04-18 11:51:41] [BTC-MAKER] FILTER: ... entry_price=0.4350 ... < min 0.55
FILTER_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(?P<asset>BTC|ETH)-(?:MAKER|SNIPE)\] "
    r"FILTER: .*?entry_price=(?P<entry>[0-9]+\.[0-9]+).*?"
    r"< (?:min|floor) (?P<threshold>[0-9]+\.[0-9]+).*?skipping low price"
)


def parse_ts(ts_str: str) -> int:
    """Log timestamps are local time (no tz). Return unix seconds assuming local tz."""
    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").astimezone()
    return int(dt.timestamp())


def scan_log(path: str, since_ts: int) -> list[dict]:
    """Return list of skip events {ts, asset, entry, threshold, direction, token_id}."""
    if not os.path.exists(path):
        return []
    events = []
    # Track the most recent [*-BOOK] line so we can attach its direction/token_id
    # to any FILTER line that follows within 5 seconds.
    last_book = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m_book = BOOK_RE.search(line)
            if m_book:
                last_book = {
                    "ts": parse_ts(m_book["ts"]),
                    "direction": m_book["direction"],
                    "token_id": m_book["token_id"],
                }
                continue
            m_filt = FILTER_RE.search(line)
            if not m_filt:
                continue
            ts = parse_ts(m_filt["ts"])
            if ts < since_ts:
                continue
            if not last_book or ts - last_book["ts"] > 5:
                continue  # no book context → skip the skip
            events.append({
                "ts": ts,
                "ts_str": m_filt["ts"],
                "asset": m_filt["asset"],
                "entry": float(m_filt["entry"]),
                "threshold": float(m_filt["threshold"]),
                "direction": last_book["direction"],
                "token_id": last_book["token_id"],
            })
    return events


def window_bounds(ts: int) -> tuple[int, int]:
    """Return (start, end) unix of the 15m window containing ts."""
    start = (ts // 900) * 900
    return start, start + 900


def prices_history(token_id: str, start: int, end: int) -> list[dict]:
    """CLOB /prices-history. fidelity=1 gives per-minute; we want the close."""
    try:
        r = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={"market": token_id, "startTs": start, "endTs": end + 60, "fidelity": 1},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        return r.json().get("history", [])
    except requests.RequestException:
        return []


def resolve_skip(ev: dict) -> dict:
    """Fetch the final price of the skipped token in its window."""
    start, end = window_bounds(ev["ts"])
    hist = prices_history(ev["token_id"], start, end)
    if not hist:
        ev["resolution"] = None
        ev["final_price"] = None
        ev["hypothetical_pnl"] = None
        return ev
    # Final price is last sample in window. Market resolves at end → 1.0 or 0.0.
    final = float(hist[-1]["p"])
    ev["final_price"] = final
    # Heuristic: >=0.95 = WIN, <=0.05 = LOSS, else unresolved (in-flight)
    if final >= 0.95:
        ev["resolution"] = "WIN"
    elif final <= 0.05:
        ev["resolution"] = "LOSS"
    else:
        ev["resolution"] = "UNRESOLVED"
    return ev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--since", help="ISO date, overrides --hours")
    ap.add_argument("--asset", choices=["btc", "eth", "both"], default="both")
    ap.add_argument("--size", type=float, default=DEFAULT_SIZE_USD,
                    help="Hypothetical position size USD per trade (default 12)")
    ap.add_argument("--out", default="/tmp/polymarket_skip_audit.csv")
    args = ap.parse_args()

    if args.since:
        since_ts = int(datetime.fromisoformat(args.since).astimezone().timestamp())
    else:
        since_ts = int(time.time() - args.hours * 3600)

    assets = ["btc", "eth"] if args.asset == "both" else [args.asset]

    # Scan
    all_events = []
    for a in assets:
        events = scan_log(LOGS[a], since_ts)
        print(f"[{a.upper()}] {len(events)} skipped signals in window", file=sys.stderr)
        all_events.extend(events)

    if not all_events:
        print("No skipped signals found in window.")
        return 0

    # Resolve each via CLOB API. Dedupe by (token_id, window_start) — same window
    # fires the skip many times per 15m. We only need ONE resolution per window.
    seen = {}
    for ev in all_events:
        key = (ev["token_id"], window_bounds(ev["ts"])[0])
        if key in seen:
            seen[key]["skip_count"] += 1
            continue
        ev["skip_count"] = 1
        seen[key] = ev

    unique = list(seen.values())
    print(f"Resolving {len(unique)} unique windows...", file=sys.stderr)

    for i, ev in enumerate(unique, 1):
        resolve_skip(ev)
        # Be polite to the API; /prices-history is 1000 req/10s so 50ms is plenty
        time.sleep(0.05)
        if i % 20 == 0:
            print(f"  {i}/{len(unique)}", file=sys.stderr)

    # P/L math: shares = size / entry. On WIN each share pays $1 → pnl = shares * (1 - entry).
    # On LOSS shares pay $0 → pnl = -shares * entry. Multiply by skip_count is NOT
    # right (same window fires many times but one bet is one bet), so we count each
    # unique window once at the chosen size.
    for ev in unique:
        if ev["resolution"] == "WIN":
            shares = args.size / ev["entry"]
            ev["hypothetical_pnl"] = round(shares * (1 - ev["entry"]), 2)
        elif ev["resolution"] == "LOSS":
            shares = args.size / ev["entry"]
            ev["hypothetical_pnl"] = round(-shares * ev["entry"], 2)
        else:
            ev["hypothetical_pnl"] = None

    # Write CSV
    fields = ["ts_str", "asset", "direction", "entry", "threshold",
              "skip_count", "final_price", "resolution", "hypothetical_pnl",
              "token_id"]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for ev in sorted(unique, key=lambda e: e["ts"]):
            w.writerow(ev)
    print(f"Wrote {args.out}", file=sys.stderr)

    # Summary
    by_asset_bucket = defaultdict(lambda: {"WIN": 0, "LOSS": 0, "UNRESOLVED": 0, "pnl": 0.0})
    for ev in unique:
        if ev["resolution"] is None:
            continue
        # Bucket by 5¢ band
        band_lo = round(ev["entry"] - (ev["entry"] % 0.05), 2)
        key = (ev["asset"], f"{band_lo:.2f}-{band_lo+0.05:.2f}")
        by_asset_bucket[key][ev["resolution"]] += 1
        if ev["hypothetical_pnl"] is not None:
            by_asset_bucket[key]["pnl"] += ev["hypothetical_pnl"]

    print("\n=== Skip audit summary ===")
    print(f"{'Asset':<6} {'Band':<12} {'Wins':>5} {'Losses':>7} {'Unres':>6} {'WR':>7} {'Hypo P/L':>10}")
    for key in sorted(by_asset_bucket):
        asset, band = key
        d = by_asset_bucket[key]
        total_res = d["WIN"] + d["LOSS"]
        wr = (d["WIN"] / total_res * 100) if total_res else 0
        print(f"{asset:<6} {band:<12} {d['WIN']:>5} {d['LOSS']:>7} {d['UNRESOLVED']:>6} "
              f"{wr:>6.1f}% ${d['pnl']:>+9.2f}")

    # Overall
    total_win = sum(d["WIN"] for d in by_asset_bucket.values())
    total_loss = sum(d["LOSS"] for d in by_asset_bucket.values())
    total_pnl = sum(d["pnl"] for d in by_asset_bucket.values())
    resolved = total_win + total_loss
    wr = (total_win / resolved * 100) if resolved else 0
    print(f"\nTotal resolved: {resolved} ({total_win}W / {total_loss}L) — WR {wr:.1f}%")
    print(f"Total hypothetical P/L at ${args.size:.2f}/trade: ${total_pnl:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
