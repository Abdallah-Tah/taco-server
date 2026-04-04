#!/usr/bin/env python3
"""Aggregate bucket comparison across multiple large Polymarket wallets.

Analysis only.
- discovers or loads large wallets from local whale tracker
- runs the same closed-trade bucket analysis per wallet
- aggregates bucket stats across 5-10 wallets
- compares aggregate wallet behavior vs our bot buckets
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

# local script imports
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import requests
from polymarket_wallet_compare import fetch_trades, reconstruct_wallet_closed, summarize_closed, load_bot_bucket_stats  # type: ignore

DATA_API = 'https://data-api.polymarket.com'
UA = {'User-Agent': 'polymarket-wallet-aggregate/0.1'}


def discover_large_wallets(count: int = 5, pages: int = 8) -> list[str]:
    """Discover active large wallets from recent trade flow by notional.
    Simple heuristic: rank wallets by cumulative recent notional.
    """
    scores = {}
    for page in range(pages):
        try:
            r = requests.get(
                f'{DATA_API}/trades',
                params={'limit': 1000, 'offset': page * 1000},
                headers=UA,
                timeout=30,
            )
            r.raise_for_status()
            rows = r.json() or []
        except Exception:
            rows = []
        if not rows:
            break
        for t in rows:
            wallet = str(t.get('proxyWallet') or '').lower()
            if not wallet:
                continue
            notional = float(t.get('size') or 0) * float(t.get('price') or 0)
            scores[wallet] = scores.get(wallet, 0.0) + notional
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [w for w, _ in ranked[: max(1, min(count, 10))]]


def main() -> int:
    ap = argparse.ArgumentParser(description="Aggregate large Polymarket wallet bucket stats")
    ap.add_argument("--wallets", type=int, default=5, help="How many whale wallets to analyze (default: 5)")
    ap.add_argument("--limit", type=int, default=300, help="Recent trade rows per wallet (default: 300)")
    args = ap.parse_args()

    whale_addresses = discover_large_wallets(count=args.wallets)
    bot_all = load_bot_bucket_stats()
    bot_btc = load_bot_bucket_stats('btc15m')
    bot_eth = load_bot_bucket_stats('eth15m')

    agg = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "avg_pnl": 0.0, "wallets": 0})
    per_wallet_bucket = defaultdict(list)
    per_wallet_bucket_wr = defaultdict(list)
    wallet_summaries = []

    for address in whale_addresses:
        try:
            rows = fetch_trades(address, args.limit)
            closed = reconstruct_wallet_closed(rows)
            buckets = summarize_closed(closed)
            wallet_net = sum(t.pnl for t in closed)
            wallet_wins = sum(1 for t in closed if t.pnl > 0)
            wallet_summary = {
                "address": address,
                "closed": len(closed),
                "win_rate": (100 * wallet_wins / len(closed)) if closed else 0.0,
                "pnl": wallet_net,
            }
            wallet_summaries.append(wallet_summary)
            wallet_summary['buckets'] = buckets
            for b, s in buckets.items():
                agg[b]["trades"] += int(s["trades"])
                agg[b]["wins"] += int(s["wins"])
                agg[b]["pnl"] += float(s["pnl"])
                agg[b]["wallets"] += 1
                per_wallet_bucket[b].append(float(s['pnl']))
                per_wallet_bucket_wr[b].append(float(s.get('win_rate', 0.0)))
        except Exception as e:
            wallet_summaries.append({"address": address, "closed": 0, "win_rate": 0.0, "pnl": 0.0, "error": str(e)})

    for b, s in agg.items():
        if s["trades"]:
            s["win_rate"] = 100.0 * s["wins"] / s["trades"]
            s["avg_pnl"] = s["pnl"] / s["trades"]
        else:
            s["win_rate"] = 0.0
            s["avg_pnl"] = 0.0

    print("POLYMARKET LARGE WALLET AGGREGATE")
    print(f"Wallets analyzed: {len(whale_addresses)}")
    print(f"Trade rows per wallet: {args.limit}")
    print()

    buckets = [f"{i/10:.1f}-{(i+1)/10:.1f}" for i in range(10)]

    print("WALLET-BY-WALLET")
    print("wallet                                     closed   wr      pnl       b0.4-0.5   b0.5-0.6   b0.7+")
    for w in wallet_summaries:
        b = w.get('buckets', {})
        b45 = float((b.get('0.4-0.5') or {}).get('pnl', 0.0))
        b56 = float((b.get('0.5-0.6') or {}).get('pnl', 0.0))
        b7 = sum(float((b.get(x) or {}).get('pnl', 0.0)) for x in buckets if float(x.split('-')[0]) >= 0.7)
        extra = f" error={w['error']}" if 'error' in w else ''
        print(f"{w['address']:<42} {w['closed']:<7} {w['win_rate']:>5.1f}% {w['pnl']:>+10.2f} {b45:>+10.2f} {b56:>+10.2f} {b7:>+10.2f}{extra}")
    print()

    print("MEDIAN WALLET BEHAVIOR BY BUCKET")
    print("bucket   median_pnl   median_wr")
    for b in buckets:
        pnls = per_wallet_bucket.get(b, [])
        wrs = per_wallet_bucket_wr.get(b, [])
        mp = median(pnls) if pnls else 0.0
        mw = median(wrs) if wrs else 0.0
        print(f"{b:<8} {mp:>+10.2f} {mw:>10.1f}%")
    print()

    print("OUTLIER-TRIMMED AGGREGATE")
    print("bucket   trim_trades trim_wr trim_pnl trim_avg")
    for b in buckets:
        pnl_list = sorted(per_wallet_bucket.get(b, []))
        wr_list = sorted(per_wallet_bucket_wr.get(b, []))
        if len(pnl_list) >= 5:
            pnl_used = pnl_list[1:-1]
            wr_used = wr_list[1:-1]
        else:
            pnl_used = pnl_list
            wr_used = wr_list
        trim_pnl = sum(pnl_used)
        trim_avg = (sum(pnl_used) / len(pnl_used)) if pnl_used else 0.0
        trim_wr = (sum(wr_used) / len(wr_used)) if wr_used else 0.0
        trim_trades = sum(int((w.get('buckets', {}).get(b) or {}).get('trades', 0)) for w in wallet_summaries)
        print(f"{b:<8} {trim_trades:<10} {trim_wr:>6.1f}% {trim_pnl:>+8.2f} {trim_avg:>+8.2f}")
    print()

    print("BOT BUCKETS — BTC ONLY")
    print("bucket   trades      wr      pnl      avg")
    for b in buckets:
        s = bot_btc.get(b, {"trades":0,"win_rate":0.0,"pnl":0.0,"avg_pnl":0.0})
        print(f"{b:<8} {int(s['trades']):<10} {s['win_rate']:>6.1f}% {s['pnl']:>+8.2f} {s['avg_pnl']:>+8.2f}")
    print()

    print("BOT BUCKETS — ETH ONLY")
    print("bucket   trades      wr      pnl      avg")
    for b in buckets:
        s = bot_eth.get(b, {"trades":0,"win_rate":0.0,"pnl":0.0,"avg_pnl":0.0})
        print(f"{b:<8} {int(s['trades']):<10} {s['win_rate']:>6.1f}% {s['pnl']:>+8.2f} {s['avg_pnl']:>+8.2f}")
    print()

    print("RAW AGGREGATE VS BOT (ALL)")
    print("bucket   agg_trades agg_wr agg_pnl agg_avg   bot_trades bot_wr bot_pnl bot_avg")
    for b in buckets:
        a = agg.get(b, {"trades": 0, "win_rate": 0.0, "pnl": 0.0, "avg_pnl": 0.0})
        bot = bot_all.get(b, {"trades": 0, "win_rate": 0.0, "pnl": 0.0, "avg_pnl": 0.0})
        print(f"{b:<8} {int(a['trades']):<10} {a['win_rate']:>6.1f}% {a['pnl']:>+8.2f} {a['avg_pnl']:>+8.2f}   {int(bot['trades']):<10} {bot['win_rate']:>6.1f}% {bot['pnl']:>+8.2f} {bot['avg_pnl']:>+8.2f}")
    print()

    over_07_trades = sum(int(agg[b]['trades']) for b in buckets if float(b.split('-')[0]) >= 0.7)
    over_07_pnl = sum(float(agg[b]['pnl']) for b in buckets if float(b.split('-')[0]) >= 0.7)
    sweet_trades = sum(int(agg[b]['trades']) for b in ('0.4-0.5', '0.5-0.6'))
    sweet_pnl = sum(float(agg[b]['pnl']) for b in ('0.4-0.5', '0.5-0.6'))

    print("HEADLINES")
    print(f"  Aggregate 0.4-0.6 trades: {sweet_trades} | pnl={sweet_pnl:+.2f}")
    print(f"  Aggregate 0.7+ trades:    {over_07_trades} | pnl={over_07_pnl:+.2f}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
