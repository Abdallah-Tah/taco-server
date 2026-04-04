#!/usr/bin/env python3
"""Compare a Polymarket wallet's closed-trade behavior against our bot.

Analysis only.
- pulls recent wallet trades from Polymarket Data API
- reconstructs closed trades via SELL exits or market resolution
- buckets by entry price (0.1 increments)
- compares wallet bucket stats vs our bot bucket stats from journal.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com/markets"
ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
JOURNAL_DB = ROOT / "journal.db"
UA = {"User-Agent": "polymarket-wallet-compare/0.1"}


@dataclass
class ClosedTrade:
    slug: str
    market: str
    outcome: str
    entry_price: float
    size: float
    entry_ts: int
    exit_ts: int
    exit_price: float
    pnl: float
    hold_sec: int
    market_type: str
    exit_kind: str


def fetch_trades(user: str, limit: int) -> list[dict[str, Any]]:
    out = []
    offset = 0
    while len(out) < limit:
        batch = min(1000, limit - len(out))
        r = requests.get(
            f"{DATA_API}/trades",
            params={"user": user, "limit": batch, "offset": offset, "takerOnly": "false"},
            headers=UA,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json() or []
        if not rows:
            break
        out.extend(rows)
        offset += len(rows)
        if len(rows) < batch:
            break
    return out[:limit]


def fetch_market_meta(slug: str, cache: dict[str, dict[str, Any] | None]) -> dict[str, Any] | None:
    if slug in cache:
        return cache[slug]
    try:
        r = requests.get(GAMMA_API, params={"slug": slug}, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json() or []
        if not data:
            cache[slug] = None
            return None
        m = data[0]
        outcomes = m.get("outcomes")
        prices = m.get("outcomePrices")
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            import json
            prices = json.loads(prices)
        winner = None
        if outcomes and prices and len(outcomes) == len(prices):
            for o, p in zip(outcomes, prices):
                try:
                    if float(p) >= 0.999:
                        winner = str(o)
                        break
                except Exception:
                    pass
        meta = {
            "slug": slug,
            "question": m.get("question") or m.get("title") or slug,
            "closed": bool(m.get("closed")),
            "winner": winner,
            "endDate": m.get("endDate"),
        }
        cache[slug] = meta
        return meta
    except Exception:
        cache[slug] = None
        return None


def market_type(slug: str, title: str) -> str:
    s = f"{slug} {title}".lower()
    sports_terms = ["nba", "nhl", "nfl", "mlb", "soccer", "fc", "vs", "over", "under", "total", "spread"]
    crypto_terms = ["btc", "bitcoin", "eth", "ethereum", "sol", "solana", "xrp", "crypto"]
    politics_terms = ["election", "president", "regime", "ceasefire", "republican", "democrat", "iran", "ukraine"]
    if any(t in s for t in sports_terms):
        return "sports"
    if any(t in s for t in crypto_terms):
        return "crypto"
    if any(t in s for t in politics_terms):
        return "politics"
    return "other"


def price_bucket(price: float) -> str:
    if price < 0:
        return "unknown"
    lo = math.floor(price * 10) / 10
    hi = lo + 0.1
    lo = max(0.0, min(0.9, lo))
    hi = max(0.1, min(1.0, hi))
    return f"{lo:.1f}-{hi:.1f}"


def reconstruct_wallet_closed(trades: list[dict[str, Any]]) -> list[ClosedTrade]:
    # FIFO lots by (slug, outcome)
    lots: dict[tuple[str, str], deque] = defaultdict(deque)
    closed: list[ClosedTrade] = []
    meta_cache: dict[str, dict[str, Any] | None] = {}

    rows = sorted(trades, key=lambda x: int(x.get("timestamp") or 0))
    for t in rows:
        slug = str(t.get("slug") or "")
        outcome = str(t.get("outcome") or "")
        side = str(t.get("side") or "").upper()
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        ts = int(t.get("timestamp") or 0)
        title = t.get("title") or slug
        key = (slug, outcome)
        if not slug or not outcome or size <= 0:
            continue
        if side == "BUY":
            lots[key].append({
                "size": size,
                "price": price,
                "ts": ts,
                "title": title,
            })
        elif side == "SELL":
            qty = size
            while qty > 1e-12 and lots[key]:
                lot = lots[key][0]
                matched = min(qty, lot["size"])
                pnl = matched * (price - lot["price"])
                closed.append(ClosedTrade(
                    slug=slug,
                    market=title,
                    outcome=outcome,
                    entry_price=lot["price"],
                    size=matched,
                    entry_ts=lot["ts"],
                    exit_ts=ts,
                    exit_price=price,
                    pnl=pnl,
                    hold_sec=max(0, ts - lot["ts"]),
                    market_type=market_type(slug, title),
                    exit_kind="sell",
                ))
                lot["size"] -= matched
                qty -= matched
                if lot["size"] <= 1e-12:
                    lots[key].popleft()

    # Resolve remaining open lots using closed-market winner when available.
    for (slug, outcome), queue in lots.items():
        if not queue:
            continue
        meta = fetch_market_meta(slug, meta_cache)
        if not meta or not meta.get("closed") or not meta.get("winner"):
            continue
        win = str(meta.get("winner")) == outcome
        exit_price = 1.0 if win else 0.0
        for lot in list(queue):
            size = float(lot["size"])
            if size <= 1e-12:
                continue
            pnl = size * (exit_price - lot["price"])
            exit_ts = lot["ts"]
            closed.append(ClosedTrade(
                slug=slug,
                market=lot["title"],
                outcome=outcome,
                entry_price=lot["price"],
                size=size,
                entry_ts=lot["ts"],
                exit_ts=exit_ts,
                exit_price=exit_price,
                pnl=pnl,
                hold_sec=max(0, exit_ts - lot["ts"]),
                market_type=market_type(slug, lot["title"]),
                exit_kind="resolution",
            ))
    return closed


def summarize_closed(trades: list[ClosedTrade]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: {
        "trades": 0,
        "wins": 0,
        "pnl": 0.0,
        "avg_pnl": 0.0,
        "avg_hold_sec": 0.0,
    })
    for t in trades:
        b = price_bucket(t.entry_price)
        out[b]["trades"] += 1
        out[b]["wins"] += 1 if t.pnl > 0 else 0
        out[b]["pnl"] += t.pnl
        out[b]["avg_hold_sec"] += t.hold_sec
    for b, s in out.items():
        if s["trades"]:
            s["avg_pnl"] = s["pnl"] / s["trades"]
            s["win_rate"] = 100.0 * s["wins"] / s["trades"]
            s["avg_hold_sec"] = s["avg_hold_sec"] / s["trades"]
        else:
            s["win_rate"] = 0.0
    return out


def load_bot_bucket_stats(engine: str | None = None) -> dict[str, dict[str, float]]:
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    engines = ('btc15m','eth15m','sol15m','xrp15m') if engine is None else (engine,)
    placeholders = ','.join('?' for _ in engines)
    cur.execute(
        f"""
        SELECT entry_price, pnl_absolute, hold_duration_seconds, engine
        FROM trades
        WHERE engine IN ({placeholders})
          AND timestamp_close IS NOT NULL
        """,
        engines,
    )
    rows = cur.fetchall()
    conn.close()
    out: dict[str, dict[str, float]] = defaultdict(lambda: {
        "trades": 0,
        "wins": 0,
        "pnl": 0.0,
        "avg_pnl": 0.0,
        "avg_hold_sec": 0.0,
    })
    for r in rows:
        price = float(r["entry_price"] or 0)
        pnl = float(r["pnl_absolute"] or 0)
        hold = float(r["hold_duration_seconds"] or 0)
        b = price_bucket(price)
        out[b]["trades"] += 1
        out[b]["wins"] += 1 if pnl > 0 else 0
        out[b]["pnl"] += pnl
        out[b]["avg_hold_sec"] += hold
    for b, s in out.items():
        if s["trades"]:
            s["avg_pnl"] = s["pnl"] / s["trades"]
            s["win_rate"] = 100.0 * s["wins"] / s["trades"]
            s["avg_hold_sec"] = s["avg_hold_sec"] / s["trades"]
        else:
            s["win_rate"] = 0.0
    return out


def fmt_hold(sec: float) -> str:
    if sec <= 0:
        return "0m"
    if sec < 3600:
        return f"{sec/60:.1f}m"
    return f"{sec/3600:.1f}h"


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare a Polymarket wallet to our bot by entry-price bucket")
    ap.add_argument("wallet", help="Polymarket wallet address")
    ap.add_argument("--limit", type=int, default=500, help="Recent trade rows to scan (default: 500)")
    args = ap.parse_args()

    trades = fetch_trades(args.wallet, args.limit)
    closed = reconstruct_wallet_closed(trades)
    wallet_buckets = summarize_closed(closed)
    bot_buckets = load_bot_bucket_stats()

    print("POLYMARKET WALLET COMPARISON")
    print(f"Wallet: {args.wallet}")
    print(f"Recent trade rows fetched: {len(trades)}")
    print(f"Closed wallet trades reconstructed: {len(closed)}")
    print()

    print("WALLET SUMMARY")
    if closed:
        wins = sum(1 for t in closed if t.pnl > 0)
        net = sum(t.pnl for t in closed)
        over_07 = [t for t in closed if t.entry_price >= 0.7]
        over_07_net = sum(t.pnl for t in over_07)
        print(f"  Win rate: {100*wins/len(closed):.1f}% ({wins}/{len(closed)})")
        print(f"  Net PnL: {net:+.2f}")
        print(f"  Trades >=0.7 entry: {len(over_07)} | PnL: {over_07_net:+.2f}")
    else:
        print("  No closed trades reconstructed yet")
    print()

    print("WALLET BY MARKET TYPE")
    type_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"trades":0,"wins":0,"pnl":0.0,"hold":0.0})
    for t in closed:
        s = type_stats[t.market_type]
        s["trades"] += 1
        s["wins"] += 1 if t.pnl > 0 else 0
        s["pnl"] += t.pnl
        s["hold"] += t.hold_sec
    if type_stats:
        for mt, s in sorted(type_stats.items()):
            wr = 100*s['wins']/s['trades'] if s['trades'] else 0
            ah = s['hold']/s['trades'] if s['trades'] else 0
            print(f"  {mt:<10} trades={s['trades']:<4} win_rate={wr:>5.1f}% pnl={s['pnl']:+.2f} avg_hold={fmt_hold(ah)}")
    else:
        print("  none")
    print()

    print("BUCKET COMPARISON")
    print("bucket   wallet_trades wallet_wr wallet_pnl wallet_avg   bot_trades bot_wr bot_pnl bot_avg")
    buckets = [f"{i/10:.1f}-{(i+1)/10:.1f}" for i in range(10)]
    for b in buckets:
        w = wallet_buckets.get(b, {"trades":0,"win_rate":0.0,"pnl":0.0,"avg_pnl":0.0})
        bot = bot_buckets.get(b, {"trades":0,"win_rate":0.0,"pnl":0.0,"avg_pnl":0.0})
        print(f"{b:<8} {int(w['trades']):<13} {w['win_rate']:>7.1f}% {w['pnl']:>+10.2f} {w['avg_pnl']:>+10.2f}   {int(bot['trades']):<10} {bot['win_rate']:>6.1f}% {bot['pnl']:>+8.2f} {bot['avg_pnl']:>+8.2f}")
    print()

    print("LAST 10 WALLET CLOSED TRADES")
    for t in closed[-10:]:
        print(
            f"  {t.market[:48]:<48} {t.outcome:<10} entry={t.entry_price:.3f} size={t.size:.2f} "
            f"pnl={t.pnl:+.2f} exit={t.exit_kind:<10} hold={fmt_hold(t.hold_sec)}"
        )
    if not closed:
        print("  none")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
