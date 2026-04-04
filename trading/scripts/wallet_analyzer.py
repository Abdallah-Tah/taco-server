#!/usr/bin/env python3
"""Simple Solana wallet trade analyzer.

What it does:
- pulls last N transactions for a wallet from public Solana RPC
- reconstructs simple round-trip trades from token balance deltas
- computes win rate / avg win / avg loss / net PnL
- buckets trades by entry price (token price in SOL, log-style buckets)
- classifies wallet A/B/C

Notes:
- start-simple heuristic; not a full DEX decoder
- best for wallets that mostly do single-token buys/sells against SOL/WSOL
- PnL is computed in SOL and also shown in rough USD using current SOL spot
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

RPC_URL = "https://api.mainnet-beta.solana.com"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYDutLCRa14Q6gttxyPjdv9w"
IGNORE_MINTS = {WSOL_MINT, USDC_MINT, USDT_MINT}
UA = {"User-Agent": "wallet-analyzer/0.1"}


@dataclass
class Trade:
    mint: str
    symbol: str
    qty: float
    entry_price_sol: float
    exit_price_sol: float
    cost_sol: float
    proceeds_sol: float
    pnl_sol: float
    entry_time: int
    exit_time: int
    entry_sig: str
    exit_sig: str


class RpcError(RuntimeError):
    pass


def rpc(method: str, params: list[Any]) -> Any:
    r = requests.post(
        RPC_URL,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RpcError(str(data["error"]))
    return data.get("result")


def get_signatures(wallet: str, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before = None
    remaining = limit
    while remaining > 0:
        batch = min(remaining, 1000)
        params = [wallet, {"limit": batch}]
        if before:
            params[1]["before"] = before
        result = rpc("getSignaturesForAddress", params) or []
        if not result:
            break
        out.extend(result)
        remaining -= len(result)
        before = result[-1].get("signature")
        if len(result) < batch:
            break
        time.sleep(0.15)
    return out[:limit]


def get_transaction(signature: str) -> dict[str, Any] | None:
    return rpc(
        "getTransaction",
        [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
    )


def get_sol_usd() -> float | None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            headers=UA,
            timeout=15,
        )
        r.raise_for_status()
        return float(r.json()["solana"]["usd"])
    except Exception:
        return None


def get_symbol(mint: str, cache: dict[str, str]) -> str:
    if mint in cache:
        return cache[mint]
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", headers=UA, timeout=12)
        r.raise_for_status()
        pairs = (r.json() or {}).get("pairs") or []
        sym = pairs[0].get("baseToken", {}).get("symbol") if pairs else None
        cache[mint] = sym or mint[:6]
    except Exception:
        cache[mint] = mint[:6]
    return cache[mint]


def _owner_token_map(items: list[dict[str, Any]], wallet: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for b in items or []:
        if b.get("owner") != wallet:
            continue
        mint = b.get("mint")
        if not mint:
            continue
        amt = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        out[mint] = amt
    return out


def _sol_delta_for_wallet(tx: dict[str, Any], wallet: str) -> float:
    try:
        msg = tx.get("transaction", {}).get("message", {})
        keys = msg.get("accountKeys") or []
        idx = None
        for i, k in enumerate(keys):
            pubkey = k.get("pubkey") if isinstance(k, dict) else k
            if pubkey == wallet:
                idx = i
                break
        if idx is None:
            return 0.0
        meta = tx.get("meta") or {}
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if idx >= len(pre) or idx >= len(post):
            return 0.0
        return (float(post[idx]) - float(pre[idx])) / 1e9
    except Exception:
        return 0.0


def parse_tx(wallet: str, tx: dict[str, Any], symbol_cache: dict[str, str]) -> dict[str, Any] | None:
    if not tx:
        return None
    meta = tx.get("meta") or {}
    sigs = tx.get("transaction", {}).get("signatures") or []
    sig = sigs[0] if sigs else ""
    ts = int(tx.get("blockTime") or 0)
    pre = _owner_token_map(meta.get("preTokenBalances") or [], wallet)
    post = _owner_token_map(meta.get("postTokenBalances") or [], wallet)
    sol_delta = _sol_delta_for_wallet(tx, wallet)

    all_deltas: dict[str, float] = {}
    for mint in (set(pre) | set(post)):
        delta = float(post.get(mint, 0.0)) - float(pre.get(mint, 0.0))
        if abs(delta) > 1e-12:
            all_deltas[mint] = delta

    if not all_deltas and abs(sol_delta) <= 1e-12:
        return None

    stable_neg = {m: -d for m, d in all_deltas.items() if m in {USDC_MINT, USDT_MINT} and d < 0}
    stable_pos = {m: d for m, d in all_deltas.items() if m in {USDC_MINT, USDT_MINT} and d > 0}
    token_pos = {m: d for m, d in all_deltas.items() if m not in IGNORE_MINTS and d > 0}
    token_neg = {m: -d for m, d in all_deltas.items() if m not in IGNORE_MINTS and d < 0}

    events = []

    # First-pass swap detection:
    # BUY  = stablecoin decreases, token increases
    # SELL = token decreases, stablecoin increases
    if len(stable_neg) == 1 and len(token_pos) == 1:
        stable_mint, stable_spent = next(iter(stable_neg.items()))
        token_mint, token_qty = next(iter(token_pos.items()))
        price = stable_spent / token_qty if token_qty > 0 else 0.0
        events.append({
            "type": "buy",
            "mint": token_mint,
            "symbol": get_symbol(token_mint, symbol_cache),
            "qty": token_qty,
            "sol_value": stable_spent,
            "price_sol": price,
            "sig": sig,
            "ts": ts,
        })
    elif len(stable_pos) == 1 and len(token_neg) == 1:
        stable_mint, stable_received = next(iter(stable_pos.items()))
        token_mint, token_qty = next(iter(token_neg.items()))
        price = stable_received / token_qty if token_qty > 0 else 0.0
        events.append({
            "type": "sell",
            "mint": token_mint,
            "symbol": get_symbol(token_mint, symbol_cache),
            "qty": token_qty,
            "sol_value": stable_received,
            "price_sol": price,
            "sig": sig,
            "ts": ts,
        })

    # Fallback to older SOL-based heuristic if no stable swap was detected.
    if not events:
        buys = {m: d for m, d in all_deltas.items() if m not in IGNORE_MINTS and d > 0}
        sells = {m: -d for m, d in all_deltas.items() if m not in IGNORE_MINTS and d < 0}
        if buys and sol_delta < 0:
            cost_total = abs(sol_delta)
            each_cost = cost_total / max(len(buys), 1)
            for mint, qty in buys.items():
                events.append({
                    "type": "buy",
                    "mint": mint,
                    "symbol": get_symbol(mint, symbol_cache),
                    "qty": qty,
                    "sol_value": each_cost,
                    "price_sol": each_cost / qty if qty > 0 else 0.0,
                    "sig": sig,
                    "ts": ts,
                })
        if sells and sol_delta > 0:
            proceeds_total = sol_delta
            each_proceeds = proceeds_total / max(len(sells), 1)
            for mint, qty in sells.items():
                events.append({
                    "type": "sell",
                    "mint": mint,
                    "symbol": get_symbol(mint, symbol_cache),
                    "qty": qty,
                    "sol_value": each_proceeds,
                    "price_sol": each_proceeds / qty if qty > 0 else 0.0,
                    "sig": sig,
                    "ts": ts,
                })

    if not events:
        return None

    return {
        "signature": sig,
        "block_time": ts,
        "sol_delta": sol_delta,
        "events": events,
    }


def reconstruct_trades(events: list[dict[str, Any]]) -> tuple[list[Trade], dict[str, list[dict[str, Any]]]]:
    open_lots: dict[str, deque] = defaultdict(deque)
    closed: list[Trade] = []

    for tx in events:
        for ev in tx["events"]:
            mint = ev["mint"]
            if ev["type"] == "buy":
                open_lots[mint].append({
                    "qty": ev["qty"],
                    "price_sol": ev["price_sol"],
                    "cost_sol": ev["sol_value"],
                    "ts": ev["ts"],
                    "sig": ev["sig"],
                    "symbol": ev["symbol"],
                })
            elif ev["type"] == "sell":
                qty_to_match = ev["qty"]
                sell_qty_total = ev["qty"]
                sell_price = ev["price_sol"]
                while qty_to_match > 1e-12 and open_lots[mint]:
                    lot = open_lots[mint][0]
                    matched = min(qty_to_match, lot["qty"])
                    entry_cost = lot["price_sol"] * matched
                    exit_proceeds = sell_price * matched
                    closed.append(Trade(
                        mint=mint,
                        symbol=lot["symbol"],
                        qty=matched,
                        entry_price_sol=lot["price_sol"],
                        exit_price_sol=sell_price,
                        cost_sol=entry_cost,
                        proceeds_sol=exit_proceeds,
                        pnl_sol=exit_proceeds - entry_cost,
                        entry_time=lot["ts"],
                        exit_time=ev["ts"],
                        entry_sig=lot["sig"],
                        exit_sig=ev["sig"],
                    ))
                    lot["qty"] -= matched
                    lot["cost_sol"] = lot["price_sol"] * lot["qty"]
                    qty_to_match -= matched
                    if lot["qty"] <= 1e-12:
                        open_lots[mint].popleft()
    return closed, open_lots


def price_bucket(price_sol: float) -> str:
    if price_sol <= 0:
        return "unknown"
    exp = math.floor(math.log10(price_sol))
    lo = 10 ** exp
    hi = 10 ** (exp + 1)
    return f"{lo:.0e}–{hi:.0e} SOL"


def classify_wallet(trades: list[Trade]) -> str:
    if not trades:
        return "C"
    wins = [t for t in trades if t.pnl_sol > 0]
    losses = [t for t in trades if t.pnl_sol < 0]
    win_rate = len(wins) / len(trades)
    net = sum(t.pnl_sol for t in trades)
    avg_win = sum(t.pnl_sol for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_sol for t in losses) / len(losses) if losses else 0.0
    if net > 0 and win_rate >= 0.55 and avg_win >= abs(avg_loss):
        return "A"
    if net > 0 or win_rate >= 0.45:
        return "B"
    return "C"


def fmt_ts(ts: int) -> str:
    if not ts:
        return "n/a"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def analyze(wallet: str, limit: int) -> int:
    sigs = get_signatures(wallet, limit)
    symbol_cache: dict[str, str] = {}
    parsed = []
    for i, item in enumerate(reversed(sigs), 1):  # oldest -> newest for FIFO
        sig = item.get("signature")
        if not sig:
            continue
        try:
            tx = get_transaction(sig)
            row = parse_tx(wallet, tx, symbol_cache)
            if row:
                parsed.append(row)
            time.sleep(0.06)
        except Exception:
            continue

    trades, open_lots = reconstruct_trades(parsed)
    wins = [t for t in trades if t.pnl_sol > 0]
    losses = [t for t in trades if t.pnl_sol < 0]
    net_pnl_sol = sum(t.pnl_sol for t in trades)
    gross_win_sol = sum(t.pnl_sol for t in wins)
    gross_loss_sol = sum(t.pnl_sol for t in losses)
    avg_win_sol = gross_win_sol / len(wins) if wins else 0.0
    avg_loss_sol = gross_loss_sol / len(losses) if losses else 0.0
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    sol_usd = get_sol_usd()
    classify = classify_wallet(trades)

    buckets = Counter()
    bucket_stats: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "wins": 0, "pnl_sol": 0.0})
    for t in trades:
        b = price_bucket(t.entry_price_sol)
        buckets[b] += 1
        bucket_stats[b]["count"] += 1
        bucket_stats[b]["wins"] += 1 if t.pnl_sol > 0 else 0
        bucket_stats[b]["pnl_sol"] += t.pnl_sol

    open_summary = []
    for mint, lots in open_lots.items():
        qty = sum(l["qty"] for l in lots)
        cost_sol = sum(l["qty"] * l["price_sol"] for l in lots)
        sym = lots[0]["symbol"] if lots else mint[:6]
        if qty > 1e-12:
            open_summary.append((sym, mint, qty, cost_sol))
    open_summary.sort(key=lambda x: -x[3])

    print(f"WALLET ANALYZER")
    print(f"Wallet: {wallet}")
    print(f"Transactions scanned: {len(sigs)}")
    print(f"Parsed trade-like txs: {len(parsed)}")
    print(f"Closed trades reconstructed: {len(trades)}")
    print(f"Classification: {classify}")
    print()
    print("SUMMARY")
    print(f"  Win rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Net PnL: {net_pnl_sol:+.4f} SOL" + (f"  (~${net_pnl_sol * sol_usd:+.2f})" if sol_usd else ""))
    print(f"  Avg win: {avg_win_sol:+.4f} SOL")
    print(f"  Avg loss: {avg_loss_sol:+.4f} SOL")
    print(f"  Gross win: {gross_win_sol:+.4f} SOL")
    print(f"  Gross loss: {gross_loss_sol:+.4f} SOL")
    print()
    print("ENTRY PRICE BUCKETS (price per token in SOL)")
    if bucket_stats:
        for bucket, stats in sorted(bucket_stats.items(), key=lambda kv: kv[0]):
            cnt = int(stats['count'])
            wr = stats['wins'] / cnt * 100 if cnt else 0
            print(f"  {bucket:<16} trades={cnt:<3} win_rate={wr:>5.1f}% pnl={stats['pnl_sol']:+.4f} SOL")
    else:
        print("  none")
    print()
    print("TOP OPEN LOTS")
    if open_summary:
        for sym, mint, qty, cost_sol in open_summary[:10]:
            print(f"  {sym:<10} qty={qty:.4f} cost={cost_sol:.4f} SOL mint={mint}")
    else:
        print("  none")
    print()
    print("LAST 10 CLOSED TRADES")
    if trades:
        for t in trades[-10:]:
            print(
                f"  {t.symbol:<10} qty={t.qty:.4f} entry={t.entry_price_sol:.8f} exit={t.exit_price_sol:.8f} "
                f"pnl={t.pnl_sol:+.4f} SOL in={fmt_ts(t.entry_time)} out={fmt_ts(t.exit_time)}"
            )
    else:
        print("  none")
    print()
    print("NOTES")
    print("  - Heuristic reconstruction only; best for single-token buy/sell flows against SOL.")
    print("  - Complex swaps, multi-leg txs, bridging, transfers, and token airdrops can reduce accuracy.")
    print("  - PnL is realized PnL on reconstructed closed lots only; open lots are listed separately.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Simple Solana wallet trade analyzer")
    ap.add_argument("wallet", help="Solana wallet address")
    ap.add_argument("--limit", type=int, default=200, help="How many recent txs to scan (default: 200)")
    args = ap.parse_args()
    return analyze(args.wallet, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
