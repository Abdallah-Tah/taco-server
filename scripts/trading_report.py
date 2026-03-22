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
POSITIONS_API = "https://data-api.polymarket.com/positions?user={user}"

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
    s = load_secrets()
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    cash = int(bal["balance"]) / 1e6

    trade_log = json.loads(POLY_LOG.read_text()) if POLY_LOG.exists() else []
    tor = requests.Session()
    tor.proxies = TOR_PROXY

    # Source of truth for open Polymarket positions: live positions API, not stale local tracker.
    live_positions = []
    try:
        r = tor.get(POSITIONS_API.format(user=s["POLYMARKET_FUNDER"]), timeout=20)
        api_positions = r.json() if r.status_code == 200 else []
        for p in api_positions:
            title = (p.get("title") or "").strip()
            # Keep only real long-dated/open titled positions in the portfolio summary.
            if not title:
                continue
            live_positions.append({
                "token_id": p.get("asset", ""),
                "market": title,
                "amount": float(p.get("size") or 0),
                "avg_price": float(p.get("avgPrice") or 0),
                "current_value": float(p.get("currentValue") or 0),
                "cash_pnl": float(p.get("cashPnl") or 0),
                "percent_pnl": float(p.get("percentPnl") or 0),
            })
    except Exception as e:
        logger.error("Positions API error: %s", e)

    invested = 0.0
    value = 0.0
    winners = []
    losers = []

    for pos in live_positions:
        cost = pos["avg_price"] * pos["amount"]
        val = pos["current_value"]
        pnl_pct = pos["percent_pnl"]
        invested += cost
        value += val
        item = (pnl_pct, pos["market"])
        if pnl_pct >= 2:
            winners.append(item)
        elif pnl_pct <= -2:
            losers.append(item)

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

    realized_win = 0.0
    realized_loss = 0.0
    current_markets = {p.get("market", "") for p in live_positions}
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
        if stake <= 0:
            continue
        try:
            if "Pistons vs. Raptors" in market:
                realized_win += float(entry.get("amount", 0)) * (1 - float(entry.get("price", 0)))
            else:
                realized_loss += stake
        except Exception:
            pass

    try:
        orders = client.get_orders() or []
    except Exception as e:
        logger.error("Orders API error: %s", e)
        orders = []
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
        "positions": len(live_positions),
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




def engine_report():
    """Summarize BTC/ETH 15m engine status and resolved trade results."""
    import sqlite3
    import subprocess
    result = {"btc": {}, "eth": {}}
    engine_specs = {
        "btc": {
            "label": "BTC-15m",
            "engine": "btc15m",
            "pid": "/tmp/polymarket_btc15m.pid",
            "log": "/tmp/polymarket_btc15m.log",
            "script": ROOT / 'scripts' / 'polymarket_btc15m.py',
        },
        "eth": {
            "label": "ETH-15m",
            "engine": "eth15m",
            "pid": "/tmp/polymarket_eth15m.pid",
            "log": "/tmp/polymarket_eth15m.log",
            "script": ROOT / 'scripts' / 'polymarket_eth15m.py',
        },
    }

    conn = sqlite3.connect(str(ROOT / 'journal.db'))
    c = conn.cursor()
    for key, spec in engine_specs.items():
        info = {
            'label': spec['label'], 'running': False, 'pid': None,
            'resolved': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0,
            'threshold': '?', 'recent_error': None,
        }
        pid_path = Path(spec['pid'])
        if pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                info['pid'] = pid
                subprocess.run(['kill', '-0', str(pid)], check=True, capture_output=True)
                info['running'] = True
            except Exception:
                pass

        try:
            c.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN pnl_percent <= 0 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(pnl_absolute), 0)
                FROM trades
                WHERE engine = ? AND timestamp_close IS NOT NULL AND exit_price > 0
            """, (spec['engine'],))
            row = c.fetchone()
            info['resolved'], info['wins'], info['losses'], info['pnl'] = row
        except Exception:
            pass

        try:
            import re
            for line in Path(spec['script']).read_text().splitlines():
                if 'SNIPE_DELTA_MIN' in line and '=' in line:
                    rhs = line.split('=', 1)[1].split('#', 1)[0].strip()
                    m = re.search(r'_float\([^,]+,\s*([0-9.]+)\)', rhs)
                    info['threshold'] = m.group(1) if m else rhs
                    break
        except Exception:
            pass

        try:
            lines = Path(spec['log']).read_text(errors='ignore').splitlines()[-200:]
            info['last_signal'] = None
            info['last_skip'] = None
            info['last_order'] = None
            info['last_price_skip'] = None
            info['last_delta_skip'] = None
            info['last_arb_skip'] = None
            info['last_activity'] = lines[-1].strip() if lines else None
            for line in reversed(lines):
                low = line.lower()
                if info['recent_error'] is None and ('traceback' in low or 'nameerror' in low or 'error:' in low or 'failed' in low):
                    info['recent_error'] = line.strip()
                if info['last_order'] is None and ('Result:' in line or '[DRY]' in line):
                    info['last_order'] = line.strip()
                if info['last_signal'] is None and 'signal!' in line:
                    info['last_signal'] = line.strip()
                if info['last_price_skip'] is None and ('> max' in line and 'skipping' in line):
                    info['last_price_skip'] = line.strip()
                if info['last_delta_skip'] is None and ('too small, skipping' in line):
                    info['last_delta_skip'] = line.strip()
                if info['last_arb_skip'] is None and ('No arb.' in line):
                    info['last_arb_skip'] = line.strip()
                if info['last_skip'] is None and (('too small, skipping' in line) or ('> max' in line and 'skipping' in line) or ('No arb.' in line)):
                    info['last_skip'] = line.strip()
        except Exception:
            pass

        result[key] = info
    conn.close()
    return result



def reconcile_report():
    """Load corrected BTC/ETH 15m market-position reconciliation."""
    import json as _json
    import subprocess as _sp
    out = {
        'rows': [],
        'summary': {'market_positions': 0, 'resolved': 0, 'wins': 0, 'losses': 0, 'pending': 0, 'win_rate': 0.0, 'net_realized_pnl': 0.0},
        'posted_stats': {'btc15m': {'posted_orders': 0, 'filled_orders': 0}, 'eth15m': {'posted_orders': 0, 'filled_orders': 0}},
        'by_engine': {
            'btc15m': {'market_positions': 0, 'resolved': 0, 'wins': 0, 'losses': 0, 'pending': 0, 'net_realized_pnl': 0.0, 'posted_orders': 0, 'filled_orders': 0, 'true_fill_rate': 0.0},
            'eth15m': {'market_positions': 0, 'resolved': 0, 'wins': 0, 'losses': 0, 'pending': 0, 'net_realized_pnl': 0.0, 'posted_orders': 0, 'filled_orders': 0, 'true_fill_rate': 0.0},
        }
    }
    try:
        cp = _sp.run([str(ROOT / '.polymarket-venv' / 'bin' / 'python3'), str(ROOT / 'scripts' / 'polymarket_reconcile.py'), '--json', '--no-sync'], capture_output=True, text=True, timeout=60)
        data = _json.loads(cp.stdout)
        out['rows'] = data.get('rows', [])
        out['summary'] = data.get('summary', out['summary'])
        out['posted_stats'] = data.get('posted_stats', out['posted_stats'])
        for row in out['rows']:
            eng = row.get('engine')
            if eng not in out['by_engine']:
                continue
            bucket = out['by_engine'][eng]
            bucket['market_positions'] += 1
            if row.get('status') in ('RESOLVED_WON', 'RESOLVED_LOST'):
                bucket['resolved'] += 1
                if row.get('status') == 'RESOLVED_WON':
                    bucket['wins'] += 1
                else:
                    bucket['losses'] += 1
                bucket['net_realized_pnl'] += float(row.get('realized_pnl') or 0)
            else:
                bucket['pending'] += 1
        for eng, bucket in out['by_engine'].items():
            ps = out['posted_stats'].get(eng, {})
            bucket['posted_orders'] = int(ps.get('posted_orders') or 0)
            bucket['filled_orders'] = int(ps.get('filled_orders') or 0)
            if bucket['posted_orders']:
                bucket['true_fill_rate'] = bucket['filled_orders'] / bucket['posted_orders'] * 100.0
    except Exception as e:
        logger.error('Reconcile report error: %s', e)
    return out

def fmt_money(x):
    return f"${x:.2f}"


def main():
    poly = poly_report()
    sol = sol_report()
    an = analytics_report()
    engines = engine_report()
    recon = reconcile_report()
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

    open_15m_value = sum(float(r.get('redeem_received') or 0) if r.get('status') == 'OPEN' else 0.0 for r in recon.get('rows', []))
    non_15m_value = max(0.0, poly['value'] - open_15m_value)

    lines.append("CAPITAL")
    lines.append(f"• Current: {fmt_money(poly['current_total_capital'])}")
    lines.append(f"• Free cash: {fmt_money(poly['cash'])}")
    lines.append(f"• Open position value: {fmt_money(poly['value'])}")
    lines.append(f"• Non-15m holdings in open value: {fmt_money(non_15m_value)}")
    lines.append("")

    btc_rs = recon['by_engine'].get('btc15m', {})
    eth_rs = recon['by_engine'].get('eth15m', {})
    open_15m_count = btc_rs.get('pending', 0) + eth_rs.get('pending', 0)

    lines.append("RESULTS")
    lines.append(f"• Corrected BTC realized PnL: {fmt_money(btc_rs.get('net_realized_pnl', 0.0))}")
    lines.append(f"• Corrected ETH realized PnL: {fmt_money(eth_rs.get('net_realized_pnl', 0.0))}")
    lines.append(f"• Corrected combined 15m realized PnL: {fmt_money(recon['summary'].get('net_realized_pnl', 0.0))}")
    lines.append(f"• 15m positions currently open: {open_15m_count}")
    lines.append(f"• Portfolio open/unrealized PnL: {fmt_money(poly['pnl'])} ({poly['pnl_pct']:+.1f}%)")
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

    lines.append("ENGINES")
    for key in ['btc', 'eth']:
        e = engines[key]
        status = 'LIVE' if e['running'] else 'DOWN'
        lines.append(f"• {e['label']}: {status} | delta>={e['threshold']}")
        rs = recon['by_engine'].get(key + '15m', {}) if key in ('btc','eth') else {}
        lines.append(f"  Posted orders: {rs.get('posted_orders', 0)} | Filled orders: {rs.get('filled_orders', 0)} | True fill rate: {rs.get('true_fill_rate', 0.0):.1f}%")
        lines.append(f"  Resolved markets: {rs.get('resolved', 0)} | W: {rs.get('wins', 0)} L: {rs.get('losses', 0)} | Pending/Open: {rs.get('pending', 0)}")
        lines.append(f"  Corrected realized P&L: {fmt_money(rs.get('net_realized_pnl', 0.0))}")
        if e.get('last_signal'):
            lines.append(f"  Last signal: {e['last_signal'].split('] ',1)[-1][:100]}")
        if e.get('last_order'):
            lines.append(f"  Last order: {e['last_order'].split('] ',1)[-1][:100]}")
        if e.get('last_skip'):
            lines.append(f"  Last skip: {e['last_skip'].split('] ',1)[-1][:100]}")
        if e.get('last_price_skip'):
            lines.append(f"  Last price skip: {e['last_price_skip'].split('] ',1)[-1][:100]}")
        if e.get('last_delta_skip'):
            lines.append(f"  Last delta skip: {e['last_delta_skip'].split('] ',1)[-1][:100]}")
        if e.get('last_arb_skip'):
            lines.append(f"  Last arb skip: {e['last_arb_skip'].split('] ',1)[-1][:100]}")
        if e.get('last_activity'):
            lines.append(f"  Last activity: {e['last_activity'].split('] ',1)[-1][:100]}")
        if e.get('recent_error'):
            lines.append(f"  Last error: {e['recent_error'][:100]}")
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
    lines.append("• Auto-redeem: enabled via polymarket_redeem.py after window rollover")
    if redeem['value'] > 0:
        lines.append("• Action: redeem/claim now")
    else:
        lines.append("• Action: nothing to redeem")
    lines.append("")

    # BTC/ETH Detailed Trades Section
    lines.append("=" * 40)
    lines.append("BTC/ETH DETAILED TRADES")
    lines.append("=" * 40)

    try:
        import sqlite3
        conn = sqlite3.connect(str(ROOT / 'journal.db'))
        c = conn.cursor()

        # BTC trades - last 20
        lines.append("")
        lines.append("✅ BTC 15m - LAST 20 TRADES")
        c.execute("""
            SELECT timestamp_open, direction, position_size_usd, pnl_absolute, pnl_percent, exit_type
            FROM trades WHERE engine = 'btc15m'
            ORDER BY timestamp_open DESC LIMIT 20
        """)
        for t in c.fetchall():
            ts, direction, size, pnl, pnl_pct, status = t
            ts_str = ts[:16].replace('T', ' ')
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_color = "✅" if pnl > 0 else "❌"
            lines.append(f"  {ts_str} {direction} {pnl_color} ${pnl:.2f} ({pnl_sign}{pnl_pct:.1f}%) [{status}]")

        # ETH trades - last 20
        lines.append("")
        lines.append("✅ ETH 15m - LAST 20 TRADES")
        c.execute("""
            SELECT timestamp_open, direction, position_size_usd, pnl_absolute, pnl_percent, exit_type
            FROM trades WHERE engine = 'eth15m'
            ORDER BY timestamp_open DESC LIMIT 20
        """)
        for t in c.fetchall():
            ts, direction, size, pnl, pnl_pct, status = t
            ts_str = ts[:16].replace('T', ' ')
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_color = "✅" if pnl > 0 else "❌"
            lines.append(f"  {ts_str} {direction} {pnl_color} ${pnl:.2f} ({pnl_sign}{pnl_pct:.1f}%) [{status}]")

        # BTC daily summary
        lines.append("")
        lines.append("📊 BTC 15m - DAILY SUMMARY")
        c.execute("""
            SELECT DATE(timestamp_open) as date, COUNT(*) as trades,
                   SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_absolute < 0 THEN 1 ELSE 0 END) as losses,
                   SUM(pnl_absolute) as total_pnl
            FROM trades WHERE engine = 'btc15m'
            GROUP BY DATE(timestamp_open)
            ORDER BY date DESC LIMIT 7
        """)
        for row in c.fetchall():
            date, trades, wins, losses, total_pnl = row
            win_rate = (wins / trades * 100) if trades > 0 else 0
            pnl_sign = "+" if total_pnl >= 0 else ""
            lines.append(f"  {date}: {trades} trades | W:{wins} L:{losses} ({win_rate:.1f}%) | {pnl_sign}${total_pnl:.2f}")

        # ETH daily summary
        lines.append("")
        lines.append("📊 ETH 15m - DAILY SUMMARY")
        c.execute("""
            SELECT DATE(timestamp_open) as date, COUNT(*) as trades,
                   SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_absolute < 0 THEN 1 ELSE 0 END) as losses,
                   SUM(pnl_absolute) as total_pnl
            FROM trades WHERE engine = 'eth15m'
            GROUP BY DATE(timestamp_open)
            ORDER BY date DESC LIMIT 7
        """)
        for row in c.fetchall():
            date, trades, wins, losses, total_pnl = row
            win_rate = (wins / trades * 100) if trades > 0 else 0
            pnl_sign = "+" if total_pnl >= 0 else ""
            lines.append(f"  {date}: {trades} trades | W:{wins} L:{losses} ({win_rate:.1f}%) | {pnl_sign}${total_pnl:.2f}")

        conn.close()

    except Exception as e:
        lines.append(f"  Error fetching detailed trades: {e}")

    lines.append("")
    lines.append("=" * 40)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
