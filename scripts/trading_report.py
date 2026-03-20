#!/usr/bin/env python3
"""Compact local trading report for /report.

Outputs a short mobile-friendly status summary using local state + live prices.
Goal: minimal reasoning/tokens; do the heavy lifting locally.
"""
import json
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import requests
from py_clob_client.http_helpers import helpers as _clob_helpers
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

_clob_helpers._http_client = httpx.Client(proxy='socks5://127.0.0.1:9050', http2=True)

logger = logging.getLogger(__name__)

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
SECRETS = Path.home() / ".config" / "openclaw" / "secrets.env"
POLY_POS = ROOT / ".poly_positions.json"
POLY_LOG = ROOT / ".poly_trade_log.json"
SOL_POS = ROOT / ".positions.json"
BLACKLIST_FILE = ROOT / ".blacklist.json"
TRADER_LOG = Path("/tmp/taco_trader.log")
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
SIGNATURE_TYPE = 0
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}

sys.path.insert(0, str(ROOT / "scripts"))

try:
    from config import GOAL_USD, MILESTONES
except ImportError:
    GOAL_USD = 2780.0
    MILESTONES = [150, 250, 500, 1000, 2000, 2780]


def load_secrets():
    data = {}
    for line in SECRETS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def get_client():
    s = load_secrets()
    creds = ApiCreds(
        api_key=s["POLYMARKET_API_KEY"],
        api_secret=s["POLYMARKET_API_SECRET"],
        api_passphrase=s["POLYMARKET_PASSPHRASE"],
    )
    return ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=s["POLYMARKET_PRIVATE_KEY"],
        signature_type=SIGNATURE_TYPE,
        funder=s["POLYMARKET_FUNDER"],
        creds=creds,
    )


def poly_report():
    client = get_client()
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    cash = int(bal["balance"]) / 1e6

    positions = json.loads(POLY_POS.read_text()) if POLY_POS.exists() else {}
    trade_log = json.loads(POLY_LOG.read_text()) if POLY_LOG.exists() else []
    tor = requests.Session()
    tor.proxies = TOR_PROXY

    invested = 0.0
    value = 0.0
    winners = []
    losers = []

    # Unrealized/current positions
    for token_id, pos in positions.items():
        try:
            r = tor.get("https://clob.polymarket.com/price", params={"token_id": token_id, "side": "sell"}, timeout=10)
            cur = float(r.json().get("price", 0))
        except Exception:
            cur = 0.0
        cost = pos["avg_price"] * pos["amount"]
        val = cur * pos["amount"]
        pnl_pct = ((cur - pos["avg_price"]) / pos["avg_price"] * 100) if pos["avg_price"] else 0.0
        invested += cost
        value += val
        item = (pnl_pct, pos["market"])
        if pnl_pct >= 2:
            winners.append(item)
        elif pnl_pct <= -2:
            losers.append(item)

    # Historical total invested from all unique BUYs
    historical_invested = 0.0
    unique_buys = set()
    for entry in trade_log:
        if entry.get("action") != "BUY":
            continue
        key = (entry.get("market"), entry.get("amount"), entry.get("price"))
        if key in unique_buys:
            continue
        unique_buys.add(key)
        historical_invested += float(entry.get("amount", 0)) * float(entry.get("price", 0))

    # Realized totals from BUYs no longer in tracked positions
    realized_win = 0.0
    realized_loss = 0.0
    current_markets = {p.get("market", "") for p in positions.values()}
    seen = set()
    for entry in trade_log:
        if entry.get("action") != "BUY":
            continue
        market = entry.get("market", "")
        key = (market, entry.get("amount"), entry.get("price"))
        if key in seen:
            continue
        seen.add(key)
        if market in current_markets:
            continue
        stake = float(entry.get("amount", 0)) * float(entry.get("price", 0))
        # If it's no longer tracked, assume resolved/closed. Use current title clues/history.
        if stake <= 0:
            continue
        try:
            # Resolved winners on Polymarket settle near $1. Current losers near $0.
            # For removed historical positions, fetch not possible from shortened token_id in log,
            # so infer from known removed market names.
            if "Pistons vs. Raptors" in market:
                realized_win += float(entry.get("amount", 0)) * (1 - float(entry.get("price", 0)))
            else:
                realized_loss += stake
        except Exception:
            pass

    orders = client.get_orders() or []
    live_orders = [o for o in orders if o.get("status") == "live"]
    pnl = value - invested
    pnl_pct = (pnl / invested * 100) if invested else 0.0

    winners.sort(reverse=True)
    losers.sort()

    realized_net = realized_win - realized_loss
    current_total_capital = cash + value
    starting_capital = current_total_capital - realized_net

    return {
        "cash": cash,
        "positions": len(positions),
        "open_orders": len(live_orders),
        "invested": invested,
        "value": value,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "top_win": winners[0] if winners else None,
        "top_loss": losers[0] if losers else None,
        "historical_invested": historical_invested,
        "realized_win": realized_win,
        "realized_loss": realized_loss,
        "realized_net": realized_net,
        "current_total_capital": current_total_capital,
        "starting_capital": starting_capital,
    }


def sol_report():
    status = {
        "running": False,
        "cycle_line": "unknown",
        "positions": 0,
        "sol_positions": [],
    }
    if TRADER_LOG.exists():
        lines = TRADER_LOG.read_text(errors="ignore").splitlines()
        for line in reversed(lines[-30:]):
            if "Cycle" in line:
                status["cycle_line"] = line.strip()
                status["running"] = True
                break
        for line in reversed(lines[-30:]):
            if "SOL balance:" in line:
                status["balance_line"] = line.strip()
                break
    if SOL_POS.exists():
        try:
            positions = json.loads(SOL_POS.read_text())
            status["positions"] = len(positions)
            # Enrich with live prices from DexScreener
            tor = requests.Session()
            for mint, pos in positions.items():
                sym = pos.get("sym", mint[:6])
                entry = float(pos.get("entry") or pos.get("entry_price") or 0)
                amount = float(pos.get("amount") or 0)
                cur_price = 0.0
                try:
                    r = tor.get(
                        f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                        timeout=8,
                    )
                    pairs = r.json().get("pairs") or []
                    if pairs:
                        cur_price = float(pairs[0].get("priceUsd") or 0)
                except Exception:
                    pass
                pnl_pct = ((cur_price - entry) / entry * 100) if entry > 0 else 0.0
                cost_usd = entry * amount
                cur_usd = cur_price * amount if cur_price else 0.0
                status["sol_positions"].append({
                    "sym": sym,
                    "entry": entry,
                    "cur_price": cur_price,
                    "pnl_pct": pnl_pct,
                    "cost_usd": cost_usd,
                    "cur_usd": cur_usd,
                })
        except Exception:
            pass
    return status


def analytics_report():
    """Pull 7-day stats from journal.db."""
    result = {
        "win_rate_7d": 0.0,
        "win_rate_all": 0.0,
        "pnl_7d": 0.0,
        "streak": {},
        "regime_history": [],
    }
    try:
        from analytics import get_win_rate, get_pnl_by_category, get_streak, get_regime_history
        result["win_rate_7d"] = get_win_rate(days=7)
        result["win_rate_all"] = get_win_rate()
        pnl_cat = get_pnl_by_category(days=7)
        result["pnl_7d"] = sum(pnl_cat.values())
        result["streak"] = get_streak()
        result["regime_history"] = get_regime_history()[:3]
    except Exception as e:
        logger.error("Analytics error: %s", e)
    return result


def milestone_report(total_capital: float):
    """Get milestone progress."""
    result = {
        "total": total_capital,
        "goal": GOAL_USD,
        "percent": (total_capital / GOAL_USD * 100) if GOAL_USD else 0,
        "reached": [],
        "next": None,
    }
    try:
        from milestones import get_progress, check_milestones
        check_milestones(total_capital)  # Log any newly reached
        progress = get_progress()
        result["reached"] = progress["reached"]
        result["next"] = progress["next"]
    except Exception as e:
        logger.error("Milestone error: %s", e)
    return result


def blacklist_count():
    """Count blacklisted tokens."""
    if BLACKLIST_FILE.exists():
        try:
            bl = json.loads(BLACKLIST_FILE.read_text())
            return len(bl)
        except Exception:
            pass
    return 0


def redeemable_report():
    """Get redeemable settled Polymarket value from public data API."""
    result = {"value": 0.0, "count": 0, "markets": []}
    try:
        s = load_secrets()
        addr = s.get("POLYMARKET_FUNDER", "")
        if not addr:
            return result
        r = requests.get(f"https://data-api.polymarket.com/positions?user={addr}", timeout=20)
        positions = r.json() if r.status_code == 200 else []
        for p in positions:
            if p.get("redeemable") is True:
                val = float(p.get("currentValue", 0) or 0)
                title = p.get("title", "")
                result["value"] += val
                result["count"] += 1
                result["markets"].append({"title": title, "value": val})
    except Exception as e:
        logger.error("Redeemable report error: %s", e)
    return result


def fmt_money(x):
    return f"${x:.2f}"


def main():
    poly = poly_report()
    sol = sol_report()
    an = analytics_report()
    milestones = milestone_report(poly['current_total_capital'])
    bl_count = blacklist_count()
    redeem = redeemable_report()

    # Regime from rolling window (matches live trader)
    regime_label = "normal"
    regime_wr = an["win_rate_all"]
    try:
        import importlib.util as _ilu
        from pathlib import Path as _P
        _jspec = _ilu.spec_from_file_location('sj', _P(__file__).with_name('journal.py'))
        _jmod = _ilu.module_from_spec(_jspec); _jspec.loader.exec_module(_jmod)
        _jmod.migrate_json_logs()
        _aspec = _ilu.spec_from_file_location('sa', _P(__file__).with_name('analytics.py'))
        _amod = _ilu.module_from_spec(_aspec); _aspec.loader.exec_module(_amod)
        closed = _amod._get_closed_trades(engine="solana")
        from config import REGIME_WINDOW, AGGRESSIVE_THRESHOLD, DEFENSIVE_THRESHOLD
        recent = closed[:REGIME_WINDOW]
        if len(recent) >= REGIME_WINDOW:
            wins = sum(1 for t in recent if (t.get("pnl_percent") or 0) > 0)
            wr = wins / len(recent)
            regime_wr = wr * 100.0
            if wr > AGGRESSIVE_THRESHOLD:
                regime_label = "aggressive"
            elif wr < DEFENSIVE_THRESHOLD:
                regime_label = "defensive"
    except Exception:
        if an.get("regime_history"):
            latest = an["regime_history"][0]
            regime_label = latest.get("regime", "normal")
            regime_wr = latest.get("win_rate", regime_wr)

    lines = []
    lines.append("📊 REPORT")
    lines.append("")
    lines.append(f"Goal: ${milestones['total']:.2f} / ${milestones['goal']:.2f} ({milestones['percent']:.1f}%)")
    next_m = milestones.get("next")
    reached = milestones.get("reached", [])
    lines.append("")

    lines.append("CAPITAL")
    lines.append(f"• Start: {fmt_money(poly['starting_capital'])}")
    lines.append(f"• Current: {fmt_money(poly['current_total_capital'])}")
    lines.append(f"• Cash: {fmt_money(poly['cash'])}")
    lines.append(f"• Open value: {fmt_money(poly['value'])}")
    lines.append("")

    lines.append("RESULTS")
    lines.append(f"• Won: {fmt_money(poly['realized_win'])} | Lost: {fmt_money(poly['realized_loss'])}")
    lines.append(f"• Realized net: {fmt_money(poly['realized_net'])}")
    lines.append(f"• Open PnL: {fmt_money(poly['pnl'])} ({poly['pnl_pct']:+.1f}%)")
    lines.append("")

    lines.append("7-DAY")
    lines.append(f"• Win rate: {an['win_rate_7d']:.1f}%")
    streak = an.get("streak", {})
    cur_streak = streak.get("current_streak", 0)
    if cur_streak > 0:
        lines.append(f"• Streak: {cur_streak}W")
    elif cur_streak < 0:
        lines.append(f"• Streak: {abs(cur_streak)}L")
    lines.append(f"• Regime: {regime_label} ({regime_wr:.1f}%)")
    lines.append("")

    lines.append("SOLANA")
    sol_status = "Running" if sol['running'] else "Stopped"
    lines.append(f"• {sol_status}, {sol['positions']} positions, {sol.get('balance_line', '').split('SOL balance: ')[-1].split(' |')[0] if sol.get('balance_line') else '?'} SOL")

    if sol.get("sol_positions"):
        for sp in sol["sol_positions"]:
            sym = sp["sym"]
            pnl_pct = sp["pnl_pct"]
            sign = "+" if pnl_pct >= 0 else ""
            lines.append(f"  {sym}: {sign}{pnl_pct:.1f}%")

    lines.append(f"• Blacklist: {bl_count} tokens")
    lines.append("")

    lines.append("REDEEM")
    lines.append(f"• Redeemable: {fmt_money(redeem['value'])} across {redeem['count']} market(s)")
    if redeem['value'] > 0:
        lines.append("• Action: redeem/claim now")
    else:
        lines.append("• Action: nothing to redeem")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
