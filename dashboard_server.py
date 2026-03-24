#!/usr/bin/env python3
"""Taco Trader Dashboard v3 - Accurate data, live transactions"""
from __future__ import annotations
import json, os, re, sqlite3, threading, time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path("/home/abdaltm86/.openclaw/workspace/trading")
JOURNAL_DB = ROOT / "journal.db"
PORTFOLIO_FILE = ROOT / ".portfolio.json"
POLY_POSITIONS = ROOT / ".poly_positions.json"
BTC_LOG = ROOT / ".poly_btc15m.log"
ETH_LOG = ROOT / ".poly_eth15m.log"
POLY_TRADE_LOG = ROOT / ".poly_trade_log.json"
BTC_STATE = ROOT / ".poly_btc15m_state.json"
ETH_STATE = ROOT / ".poly_eth15m_state.json"

def _db():
    return sqlite3.connect(str(JOURNAL_DB))

# ── Engine stats ──────────────────────────────────────────────────────────────
def get_engine_stats():
    conn = _db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    stats = {}
    engines = [row['engine'] for row in cur.execute("SELECT DISTINCT engine FROM trades")]
    for e in engines:
        r = cur.execute(
            "SELECT COUNT(*) total, COUNT(NULLIF(timestamp_close,'')) closed,"
            " COUNT(CASE WHEN pnl_absolute > 0 THEN 1 END) wins,"
            " SUM(pnl_absolute) tpnl, AVG(pnl_absolute) apnl,"
            " SUM(position_size_usd) vol"
            " FROM trades WHERE engine=?",
            (e,)
        ).fetchone()
        closed = r['closed'] or 0
        wins = r['wins'] or 0
        stats[e] = {
            'total_trades': r['total'] or 0,
            'closed_trades': closed,
            'wins': wins,
            'losses': max(0, closed - wins),
            'win_rate': round(wins / closed * 100, 2) if closed > 0 else 0,
            'total_pnl': round(r['tpnl'] or 0, 2),
            'avg_pnl': round(r['apnl'] or 0, 2),
            'total_volume': round(r['vol'] or 0, 2),
        }
    conn.close()
    return stats

# ── 7-day stats ───────────────────────────────────────────────────────────────
def get_7day_stats():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT pnl_absolute FROM trades
        WHERE timestamp_close >= datetime('now','-7 days')
        AND timestamp_close IS NOT NULL
        AND pnl_absolute IS NOT NULL
        ORDER BY timestamp_close DESC
    """)
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    if not rows:
        return {'win_rate': 0, 'streak': 'N/A', 'wins': 0, 'losses': 0, 'total': 0}
    wins = sum(1 for v in rows if v > 0)
    losses = len(rows) - wins
    sc, st = 0, None
    for v in rows:
        if v > 0:
            if st == 'W': sc += 1
            else: sc, st = 1, 'W'
        elif v < 0:
            if st == 'L': sc += 1
            else: sc, st = 1, 'L'
        else:
            break
    return {
        'win_rate': round(wins / len(rows) * 100, 1),
        'streak': f"{sc}{st}" if st else 'N/A',
        'wins': wins, 'losses': losses, 'total': len(rows)
    }

# ── Today stats ────────────────────────────────────────────────────────────────
def get_today_stats():
    try:
        import importlib.util as _ilu
        tr_path = ROOT / 'scripts' / 'trading_report.py'
        spec = _ilu.spec_from_file_location('dash_tr_report', tr_path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        recon = mod.reconcile_report()
        rows = recon.get('rows', [])
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        today_rows = [r for r in rows if str(r.get('window_start') or '').startswith(today)]
        resolved = [r for r in today_rows if r.get('status') in ('RESOLVED_WON', 'RESOLVED_LOST')]
        today_pnl = round(sum(float(r.get('realized_pnl') or 0) for r in resolved), 2)
        return {'today_pnl': today_pnl, 'today_trades': len(today_rows)}
    except Exception:
        conn = _db()
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(pnl_absolute),0), COUNT(*)
            FROM trades
            WHERE DATE(timestamp_close) = DATE('now')
            AND timestamp_close IS NOT NULL
        """)
        r = cur.fetchone()
        conn.close()
        return {'today_pnl': round(r[0] or 0, 2), 'today_trades': r[1] or 0}

# ── All-time stats ─────────────────────────────────────────────────────────────
def get_alltime_stats():
    try:
        import importlib.util as _ilu
        tr_path = ROOT / 'scripts' / 'trading_report.py'
        spec = _ilu.spec_from_file_location('dash_tr_report', tr_path)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        recon = mod.reconcile_report()
        poly = mod.poly_report()
        summary = recon.get('summary', {})
        return {
            'total_trades': int(summary.get('resolved', 0) + summary.get('pending', 0)),
            'total_pnl': round(float(summary.get('net_realized_pnl', 0.0)) + float(poly.get('pnl', 0.0)), 2)
        }
    except Exception:
        conn = _db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(pnl_absolute),0) FROM trades WHERE timestamp_close IS NOT NULL")
        r = cur.fetchone()
        conn.close()
        return {'total_trades': r[0] or 0, 'total_pnl': round(r[1] or 0, 2)}

# ── P&L by asset ──────────────────────────────────────────────────────────────
def get_asset_pnl():
    conn = _db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT asset, COUNT(*) c, ROUND(SUM(pnl_absolute),2) p
        FROM trades WHERE timestamp_close IS NOT NULL
        GROUP BY asset ORDER BY p DESC
    """)
    result = [{'asset': r['asset'], 'trades': r['c'], 'pnl': round(r['p'] or 0, 2)} for r in cur.fetchall()]
    conn.close()
    return result

# ── Recent trades ──────────────────────────────────────────────────────────────
def get_recent_trades(limit=100):
    conn = _db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades ORDER BY timestamp_open DESC LIMIT ?", (limit,))
    trades = []
    for row in cur.fetchall():
        t = dict(row)
        t['open_time'] = (t['timestamp_open'] or '')[:16].replace('T', ' ') if t.get('timestamp_open') else '-'
        t['duration_mins'] = int(t.get('hold_duration_seconds', 0) / 60) if t.get('hold_duration_seconds') else 0
        trades.append(t)
    conn.close()
    return trades

# ── Polymarket positions ───────────────────────────────────────────────────────
def get_poly_positions():
    if not POLY_POSITIONS.exists():
        return []
    try:
        data = json.loads(POLY_POSITIONS.read_text())
        return [pos for pos in data.values() if isinstance(pos, dict) and pos.get('amount')]
    except Exception:
        return []

# ── Capital ───────────────────────────────────────────────────────────────────
def get_capital():
    # Read portfolio config
    try:
        pf = json.loads(PORTFOLIO_FILE.read_text()) if PORTFOLIO_FILE.exists() else {}
    except Exception:
        pf = {}

    # Try to fetch live USDC balance from Polymarket CLOB
    clob_balance = None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
        from py_clob_client import constants

        secrets = {}
        secrets_file = Path.home() / ".config" / "openclaw" / "secrets.env"
        if secrets_file.exists():
            for line in secrets_file.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    secrets[k.strip()] = v.strip().strip('"').strip("'")

        if all([secrets.get("POLYMARKET_FUNDER"), secrets.get("POLYMARKET_PRIVATE_KEY"),
                secrets.get("POLYMARKET_API_KEY"), secrets.get("POLYMARKET_API_SECRET"),
                secrets.get("POLYMARKET_PASSPHRASE")]):
            creds = ApiCreds(
                api_key=secrets.get("POLYMARKET_API_KEY"),
                api_secret=secrets.get("POLYMARKET_API_SECRET"),
                api_passphrase=secrets.get("POLYMARKET_PASSPHRASE"),
            )
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=constants.POLYGON,
                key=secrets.get("POLYMARKET_PRIVATE_KEY"),
                signature_type=constants.L2,
                funder=secrets.get("POLYMARKET_FUNDER"),
                creds=creds,
            )
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = client.get_balance_allowance(params)
            raw_bal = bal.get('balance', '0')
            clob_balance = float(raw_bal) if raw_bal else 0.0
    except Exception:
        pass

    # Use CLOB balance if available, else manual override, else calculated
    if clob_balance is not None and clob_balance > 0:
        # Live CLOB balance (deposited funds)
        poly_positions = get_poly_positions()
        open_value = sum(p.get('current_value', 0) for p in poly_positions)
        current = round(clob_balance + open_value, 2)  # Total = CLOB cash + position value
        free = round(clob_balance, 2)
        goal = pf.get('capital_goal_usd', 2780)
        return {'current': current, 'free': free, 'open_value': round(open_value, 2), 'goal': goal}

    if pf.get('wallet_balance_usd') is not None:
        # Manual override - update to match report's wallet balance
        poly_positions = get_poly_positions()
        open_value = sum(p.get('current_value', 0) for p in poly_positions)
        current = round(float(pf.get('wallet_balance_usd')), 2)
        free = round(current - open_value, 2)
        goal = pf.get('capital_goal_usd', 2780)
        return {'current': current, 'free': free, 'open_value': round(open_value, 2), 'goal': goal}

    # Fallback: calculated from start + realized P&L
    start = pf.get('start_capital_usd', 94.69)
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(pnl_absolute),0) FROM trades WHERE timestamp_close IS NOT NULL")
    realized = (cur.fetchone()[0] or 0)
    conn.close()
    current = round(start + realized, 2)

    poly_positions = get_poly_positions()
    open_value = sum(p.get('current_value', 0) for p in poly_positions)
    free = round(current - open_value, 2)
    goal = pf.get('capital_goal_usd', 2780)
    return {'current': current, 'free': free, 'open_value': round(open_value, 2), 'goal': goal}

# ── Bot status ────────────────────────────────────────────────────────────────
def get_bot_status():
    import subprocess
    status = {}
    proc_map = {
        'btc15m': 'polymarket_btc15m.py',
        'eth15m': 'polymarket_eth15m.py',
        'sol15m': 'polymarket_sol15m.py',
        'xrp15m': 'polymarket_xrp15m.py',
    }
    for state_file, key in [(BTC_STATE, 'btc15m'), (ETH_STATE, 'eth15m')]:
        running = subprocess.run(f"pgrep -f '{proc_map.get(key,'')}' >/dev/null", shell=True).returncode == 0
        if state_file.exists():
            try:
                d = json.loads(state_file.read_text())
                status[key] = {
                    'running': running,
                    'window_ts': d.get('window_ts', 0),
                    'maker_done': d.get('maker_done', False),
                    'snipe_done': d.get('snipe_done', False),
                    'arb_done': d.get('arb_done', False),
                    'maker_order_id': d.get('maker_order_id', ''),
                    'daily_pnl': d.get('daily_pnl', 0),
                }
            except Exception:
                status[key] = {'running': running}
        else:
            status[key] = {'running': running}
    # Coinbase
    try:
        secrets = {}
        for line in Path("/home/abdaltm86/.config/openclaw/secrets.env").read_text().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                secrets[k.strip()] = v.strip().strip('"').strip("'")
        dry = secrets.get('CB_DRY_RUN', 'true').lower() != 'false'
        running = subprocess.run("pgrep -f 'coinbase_momentum.py' >/dev/null", shell=True).returncode == 0
        status['coinbase'] = {
            'running': running,
            'dry_run': dry,
            'pairs': secrets.get('CB_PAIRS', 'BTC-USD,ETH-USD').split(','),
        }
    except Exception:
        status['coinbase'] = {'running': False}
    return status

# ── Live transactions from log files ─────────────────────────────────────────
def get_live_transactions():
    transactions = []
    seen = set()
    for log_file, label in [(BTC_LOG, 'BTC-15m'), (ETH_LOG, 'ETH-15m')]:
        if not log_file.exists():
            continue
        try:
            lines = log_file.read_text(errors='ignore').splitlines()
            for line in lines[-500:]:
                # Fill events
                if 'fill verified' in line:
                    m = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                    ts = m.group(1) if m else ''
                    size_m = re.search(r'size=([\d.]+)', line)
                    size = size_m.group(1) if size_m else ''
                    key = f"{ts}:FILL:{size}"
                    if key not in seen:
                        seen.add(key)
                        transactions.append({
                            'time': ts, 'engine': label, 'event': 'Fill Verified',
                            'size': size, 'pnl': '', 'type': 'fill',
                            'is_error': False, 'market': label,
                        })
                # Cancel events
                elif 'cancel by deadline' in line:
                    m = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                    ts = m.group(1) if m else ''
                    sec_m = re.search(r'sec_rem=(\d+)', line)
                    sec = sec_m.group(1) if sec_m else ''
                    key = f"{ts}:CANCEL:{sec}"
                    if key not in seen:
                        seen.add(key)
                        transactions.append({
                            'time': ts, 'engine': label, 'event': f'Cancelled (T-{sec}s)',
                            'size': '', 'pnl': '', 'type': 'cancel',
                            'is_error': True, 'market': label,
                        })
                # Error events
                elif re.search(r'ERROR|error|submission failed|Submit error', line):
                    m = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                    ts = m.group(1) if m else ''
                    key = f"{ts}:ERROR"
                    if key not in seen:
                        seen.add(key)
                        transactions.append({
                            'time': ts, 'engine': label, 'event': 'Error',
                            'size': '', 'pnl': '', 'type': 'error',
                            'is_error': True, 'market': label,
                        })
        except Exception:
            pass

    transactions.sort(key=lambda x: x['time'], reverse=True)
    return transactions[:40]

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Taco Trader Dashboard</title>
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<style>
:root{--bg:#080c14;--surf:#0f1623;--card:#141e2e;--card2:#1a2740;--bd:#1e3050;--txt:#e2e8f0;--mut:#5a6a8a;--acc:#3b82f6;--grn:#10b981;--red:#ef4444;--org:#f59e0b;--pur:#8b5cf6;--cyn:#06b6d4}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--txt);font-size:13px;line-height:1.5}
.container{max-width:1500px;margin:0 auto;padding:16px 20px}

/* header */
.hdr{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:18px}
.logo{font-size:1.4rem;font-weight:800;background:linear-gradient(135deg,var(--acc),var(--pur));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:20px;font-size:0.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.reg-def{background:rgba(59,130,246,.15);color:var(--acc);border:1px solid rgba(59,130,246,.3)}
.reg-nrm{background:rgba(16,185,129,.12);color:var(--grn);border:1px solid rgba(16,185,129,.25)}
.reg-ag{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.25)}
.sk{background:rgba(16,185,129,.12);color:var(--grn);border:1px solid rgba(16,185,129,.25)}
.sk-l{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.25)}
.sk-n{background:rgba(90,106,133,.1);color:var(--mut);border:1px solid rgba(90,106,133,.2)}
.ts{color:var(--mut);font-size:.75rem}

/* metrics */
.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:16px}
.m{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:13px 15px}
.ml{font-size:.63rem;color:var(--mut);text-transform:uppercase;letter-spacing:.08em;font-weight:600;margin-bottom:4px}
.mv{font-size:1.3rem;font-weight:700;line-height:1.2}
.prog{height:3px;background:var(--surf);border-radius:2px;margin-top:7px;overflow:hidden}
.pfill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--acc),var(--pur));transition:width .6s}
.ms{font-size:.68rem;color:var(--mut);margin-top:2px}

/* engines */
.engs{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.ec{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px}
.ecs{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.ecname{display:flex;align-items:center;gap:7px;font-size:.88rem;font-weight:700}
.dot{width:7px;height:7px;border-radius:50%}
.db{background:var(--org);box-shadow:0 0 6px rgba(245,158,11,.4)}
.dp{background:var(--pur);box-shadow:0 0 6px rgba(139,92,246,.4)}
.dc{background:var(--cyn);box-shadow:0 0 6px rgba(6,182,212,.4)}
.st{padding:2px 8px;border-radius:20px;font-size:.63rem;font-weight:700;text-transform:uppercase}
.st-r{background:rgba(16,185,129,.12);color:var(--grn);border:1px solid rgba(16,185,129,.25)}
.st-l{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.25)}
.st-d{background:rgba(245,158,11,.12);color:var(--org);border:1px solid rgba(245,158,11,.25)}
.st-s{background:rgba(90,106,133,.1);color:var(--mut);border:1px solid rgba(90,106,133,.2)}
.er{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.erow{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid rgba(30,48,80,.6);font-size:.78rem}
.erow:last-child{border-bottom:none}
.el{color:var(--mut)}
.ev{font-weight:600}
.mk{margin-top:8px;font-size:.7rem;padding:4px 8px;border-radius:5px}
.mk-a{background:rgba(59,130,246,.08);color:var(--acc);border:1px solid rgba(59,130,246,.15)}
.mk-d{background:rgba(16,185,129,.08);color:var(--grn);border:1px solid rgba(16,185,129,.15)}

/* main grid */
.main{display:grid;grid-template-columns:1fr 370px;gap:14px;margin-bottom:16px}
.panel{background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.phdr{display:flex;justify-content:space-between;align-items:center;padding:11px 14px;border-bottom:1px solid var(--bd)}
.ptitle{font-size:.82rem;font-weight:700}
.pmeta{font-size:.68rem;color:var(--mut)}

/* transactions */
.txlist{max-height:380px;overflow-y:auto}
.txi{display:flex;align-items:center;gap:9px;padding:8px 14px;border-bottom:1px solid rgba(30,48,80,.4);font-size:.76rem}
.txi:last-child{border-bottom:none}
.txd{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.txf{background:var(--grn);box-shadow:0 0 5px rgba(16,185,129,.5)}
.txc{background:var(--red);box-shadow:0 0 5px rgba(239,68,68,.5)}
.txe{background:var(--org);box-shadow:0 0 5px rgba(245,158,11,.5)}
.tt{color:var(--mut);font-size:.68rem;min-width:65px}
.te{font-weight:700;font-size:.7rem;min-width:50px}
.te-b{color:var(--org)}
.te-e{color:var(--pur)}
.te-c{color:var(--cyn)}
.tc{flex:1}
.tp{font-weight:600;font-size:.75rem;min-width:55px;text-align:right}
.tp-p{color:var(--grn)}
.tp-n{color:var(--red)}

/* positions */
.posgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;padding:12px 14px}
.pos{background:var(--surf);border:1px solid var(--bd);border-radius:8px;padding:11px}
.posn{font-size:.76rem;font-weight:600;line-height:1.3;margin-bottom:5px;word-break:break-word}
.posr{display:flex;justify-content:space-between;align-items:center;margin-bottom:5px}
.pnl{font-size:.8rem;font-weight:700;padding:2px 7px;border-radius:5px}
.pnlp{background:rgba(16,185,129,.1);color:var(--grn)}
.pnln{background:rgba(239,68,68,.1);color:var(--red)}
.pd{display:grid;grid-template-columns:1fr 1fr;gap:2px;font-size:.68rem}
.pdi{display:flex;justify-content:space-between;color:var(--mut)}
.pdi span:last-child{font-weight:600;color:var(--txt)}
.pbadges{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.pb{font-size:.6rem;padding:1px 5px;border-radius:3px;font-weight:700;text-transform:uppercase}
.pb15{background:rgba(245,158,11,.1);color:var(--org)}
.pbr{background:rgba(59,130,246,.1);color:var(--acc)}

/* pnl bars */
.pbars{padding:12px 14px}
.pb2{margin-bottom:9px}
.pbh{display:flex;justify-content:space-between;font-size:.76rem;margin-bottom:3px}
.pbl{color:var(--mut)}
.pbv{font-weight:600}
.pbt{height:5px;background:var(--surf);border-radius:3px;overflow:hidden}
.pbf{height:100%;border-radius:3px;transition:width .5s}
.pf-g{background:linear-gradient(90deg,var(--grn),#34d399)}
.pf-r{background:linear-gradient(90deg,var(--red),#f87171)}

/* tabs */
.tabs{display:flex;border-bottom:2px solid var(--bd);margin-bottom:14px}
.tab{padding:8px 18px;cursor:pointer;font-size:.83rem;font-weight:600;color:var(--mut);border-bottom:2px solid transparent;margin-bottom:-2px;transition:all .15s}
.tab:hover{color:var(--txt)}
.tab.on{color:var(--acc);border-bottom-color:var(--acc)}
.tp{display:none}
.tp.on{display:block}

/* trade table */
.ttable{width:100%;border-collapse:collapse;font-size:.78rem}
.ttable th{text-align:left;padding:7px 10px;color:var(--mut);font-size:.67rem;text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--bd);white-space:nowrap}
.ttable td{padding:8px 10px;border-bottom:1px solid rgba(30,48,80,.35);vertical-align:middle}
.ttable tr:last-child td{border-bottom:none}
.ttable tr:hover td{background:rgba(26,37,64,.4)}
.eb{display:inline-block;padding:2px 6px;border-radius:4px;font-size:.63rem;font-weight:700;text-transform:uppercase}
.eb-b{background:rgba(245,158,11,.12);color:var(--org)}
.eb-e{background:rgba(139,92,246,.12);color:var(--pur)}
.eb-c{background:rgba(6,182,212,.12);color:var(--cyn)}
.eb-p{background:rgba(59,130,246,.12);color:var(--acc)}
.fbtn{background:var(--surf);border:1px solid var(--bd);color:var(--mut);padding:3px 11px;border-radius:5px;cursor:pointer;font-size:.73rem;font-weight:600;transition:all .12s}
.fbtn:hover{border-color:var(--acc);color:var(--txt)}
.fbtn.on{background:var(--acc);border-color:var(--acc);color:#fff}
.frow{display:flex;gap:5px;margin-bottom:11px;flex-wrap:wrap}

@media(max-width:1100px){.main{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(3,1fr)}}
@media(max-width:700px){.metrics{grid-template-columns:repeat(2,1fr)}.engs{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">

<div class="hdr">
  <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
    <div class="logo">Taco Trader</div>
    <span class="badge reg-ag" id="regime">AGGRESSIVE</span>
    <span class="badge sk" id="streak">3W</span>
  </div>
  <span class="ts" id="lastupd">—</span>
</div>

<!-- Metrics -->
<div class="metrics">
  <div class="m">
    <div class="ml">Current Capital</div>
    <div class="mv" id="cap-cur">—</div>
    <div class="prog"><div class="pfill" id="cap-bar" style="width:0%"></div></div>
    <div class="ms" id="cap-goal">Goal: $2,780</div>
  </div>
  <div class="m">
    <div class="ml">Free Cash</div>
    <div class="mv" id="cap-free">—</div>
    <div class="ms" id="cap-open">— in open</div>
  </div>
  <div class="m">
    <div class="ml">Open Unrealized</div>
    <div class="mv" id="open-pnl">—</div>
    <div class="ms" id="open-cnt">—</div>
  </div>
  <div class="m">
    <div class="ml">Today's Realized</div>
    <div class="mv" id="today-pnl">—</div>
    <div class="ms" id="today-cnt">—</div>
  </div>
  <div class="m">
    <div class="ml">7-Day Win Rate</div>
    <div class="mv" id="wr">—</div>
    <div class="ms" id="wr-detail">—</div>
  </div>
  <div class="m">
    <div class="ml">All-Time P&L</div>
    <div class="mv" id="all-pnl">—</div>
    <div class="ms" id="all-trades">— trades</div>
  </div>
</div>

<!-- Engine Cards -->
<div class="engs">
  <div class="ec">
    <div class="ecs">
      <div class="ecname"><span class="dot db"></span>BTC-15m</div>
      <span class="st st-r" id="btc-st">Running</span>
    </div>
    <div class="er">
      <div class="erow"><span class="el">Closed</span><span class="ev" id="btc-t">—</span></div>
      <div class="erow"><span class="el">Win Rate</span><span class="ev" id="btc-wr">—</span></div>
      <div class="erow"><span class="el">P&L</span><span class="ev" id="btc-pnl">—</span></div>
      <div class="erow"><span class="el">Fill Rate</span><span class="ev" id="btc-fr">—</span></div>
    </div>
    <div class="mk mk-a" id="btc-mk">○ Waiting</div>
  </div>
  <div class="ec">
    <div class="ecs">
      <div class="ecname"><span class="dot dp"></span>ETH-15m</div>
      <span class="st st-r" id="eth-st">Running</span>
    </div>
    <div class="er">
      <div class="erow"><span class="el">Closed</span><span class="ev" id="eth-t">—</span></div>
      <div class="erow"><span class="el">Win Rate</span><span class="ev" id="eth-wr">—</span></div>
      <div class="erow"><span class="el">P&L</span><span class="ev" id="eth-pnl">—</span></div>
      <div class="erow"><span class="el">Fill Rate</span><span class="ev" id="eth-fr">—</span></div>
    </div>
    <div class="mk mk-a" id="eth-mk">○ Waiting</div>
  </div>
  <div class="ec">
    <div class="ecs">
      <div class="ecname"><span class="dot dc"></span>Coinbase</div>
      <span class="st st-d" id="cb-st">Dry Run</span>
    </div>
    <div class="er">
      <div class="erow"><span class="el">Closed</span><span class="ev" id="cb-t">—</span></div>
      <div class="erow"><span class="el">Win Rate</span><span class="ev" id="cb-wr">—</span></div>
      <div class="erow"><span class="el">P&L</span><span class="ev" id="cb-pnl">—</span></div>
      <div class="erow"><span class="el">Pairs</span><span class="ev" id="cb-pr">—</span></div>
    </div>
  </div>
</div>

<!-- Main Grid: Transactions + Positions -->
<div class="main">
  <div class="panel">
    <div class="phdr">
      <div class="ptitle">Live Transactions</div>
      <div class="pmeta">BTC · ETH · auto-updated</div>
    </div>
    <div class="txlist" id="txlist">
      <div style="text-align:center;padding:30px;color:var(--mut)">Loading…</div>
    </div>
  </div>
  <div>
    <div class="panel" style="margin-bottom:14px">
      <div class="phdr">
        <div class="ptitle">Open Positions</div>
        <div class="pmeta" id="pos-cnt">—</div>
      </div>
      <div class="posgrid" id="pos-grid">
        <div style="text-align:center;padding:20px;color:var(--mut);font-size:.85rem;grid-column:1/-1">No open positions</div>
      </div>
    </div>
    <div class="panel">
      <div class="phdr">
        <div class="ptitle">P&L by Asset</div>
        <div class="pmeta">all time</div>
      </div>
      <div class="pbars" id="pnl-bars"></div>
    </div>
  </div>
</div>

<!-- Tabs -->
<div class="tabs">
  <div class="tab on" onclick="showTab('db')">Dashboard</div>
  <div class="tab" onclick="showTab('trades')">Trades</div>
  <div class="tab" onclick="showTab('positions')">Positions</div>
</div>

<!-- Dashboard tab -->
<div class="tp on" id="tab-db">
  <div class="panel">
    <div class="phdr"><div class="ptitle">Recent Trades</div><div class="pmeta">last 30</div></div>
    <div style="overflow-x:auto;padding:0 14px 14px">
      <table class="ttable">
        <thead><tr><th>Engine</th><th>Asset</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L $</th><th>P&L %</th><th>Hold</th><th>Type</th><th>Time</th></tr></thead>
        <tbody id="recent-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Trades tab -->
<div class="tp" id="tab-trades">
  <div class="panel">
    <div class="phdr"><div class="ptitle">All Trades</div><div></div></div>
    <div style="padding:12px 14px">
      <div class="frow">
        <button class="fbtn on" data-f="all" onclick="filter(this,'all')">All</button>
        <button class="fbtn" data-f="btc" onclick="filter(this,'btc')">BTC-15m</button>
        <button class="fbtn" data-f="eth" onclick="filter(this,'eth')">ETH-15m</button>
        <button class="fbtn" data-f="poly" onclick="filter(this,'poly')">Polymarket</button>
        <button class="fbtn" data-f="cb" onclick="filter(this,'cb')">Coinbase</button>
      </div>
      <div style="overflow-x:auto">
        <table class="ttable">
          <thead><tr><th>Engine</th><th>Asset</th><th>Side</th><th>Entry</th><th>Exit</th><th>P&L $</th><th>P&L %</th><th>Hold</th><th>Type</th><th>Time</th></tr></thead>
          <tbody id="all-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- Positions tab -->
<div class="tp" id="tab-positions">
  <div class="panel">
    <div class="phdr"><div class="ptitle">Open Positions</div><div class="pmeta">live</div></div>
    <div style="padding:12px 14px"><div class="posgrid" id="pos-full"></div></div>
  </div>
</div>

</div>

<script>
let all = [];
let curF = 'all';

async function api(u){try{return await fetch(u).then(r=>r.json());}catch(e){console.error(e);return null;}}

function $(id){return document.getElementById(id)}
function pcls(v){return (v==null||v>=0)?'pnlp':'pnln'}
function vc(v){return (v==null||v>=0)?'var(--grn)':'var(--red)'}
function vc2(v){return (v==null||v>=0)?'pnlp':'pnln'}
function fmt$(v,d=2){return (v==null||isNaN(v))?'—':'$'+Math.abs(v).toFixed(d)}
function fpnl(v){return(v==null||isNaN(v))?'—':((v>=0?'+$':'-$')+Math.abs(v).toFixed(2))}
function fpct(v){return(v==null||isNaN(v))?'—':((v>=0?'+':'')+v.toFixed(1)+'%')}
function ecls(e){if(!e)return'eb-p';if(e.includes('btc'))return'eb-b';if(e.includes('eth'))return'eb-e';if(e.includes('coinbase'))return'eb-c';return'eb-p'}
function eclean(e){return((e||'').replace('polymarket_','').replace('polymarket','POLY').replace('_','-')||'POLY').toUpperCase()}

async function update(){
  const [s,sd,tx] = await Promise.all([api('/api/summary'),api('/api/7day'),api('/api/transactions')]);
  if(!s)return;
  const st=s.stats||{}, btc=st.btc15m||{}, eth=st.eth15m||{}, cb=st.coinbase_momentum||{}, pos=s.positions||[];
  const cap=s.capital||{}, tod=s.today_pnl||{}, at=s.all_pnl||{};
  const wr7=sd.win_rate||0, op=s.open_pnl||0;

  // capital
  $('cap-cur').textContent='$'+((cap.current||0).toFixed(2));
  const goal=cap.goal||2780;
  const pct2=Math.min(100,((cap.current||0)/goal*100));
  $('cap-bar').style.width=pct2+'%';
  $('cap-goal').textContent='$'+((cap.current||0).toFixed(0))+' / $'+goal+' ('+pct2.toFixed(1)+'%)';
  $('cap-free').textContent='$'+((cap.free||0).toFixed(2));
  $('cap-open').textContent='$'+((cap.open_value||0).toFixed(2))+' in open';

  // open pnl
  $('open-pnl').textContent=fpnl(op);
  $('open-pnl').style.color=vc(op);
  $('open-cnt').textContent=pos.length+' open | '+fpnl(op)+' unrealized';

  // today
  $('today-pnl').textContent=fpnl(tod.today_pnl||0);
  $('today-pnl').style.color=vc(tod.today_pnl||0);
  $('today-cnt').textContent=(tod.today_trades||0)+' trades closed today';

  // 7d
  $('wr').textContent=(wr7.toFixed(1))+'%';
  $('wr').style.color=vc2(wr7-50);
  $('wr-detail').textContent=(sd.wins||0)+'W / '+(sd.losses||0)+'L / '+(sd.total||0)+' total';

  // all-time
  $('all-pnl').textContent=fpnl(at.total_pnl||0);
  $('all-pnl').style.color=vc(at.total_pnl||0);
  $('all-trades').textContent=(at.total_trades||0)+' closed trades';

  // regime
  const rEl=$('regime');
  if(wr7<45){rEl.textContent='DEFENSIVE';rEl.className='badge reg-def';}
  else if(wr7<=55){rEl.textContent='NORMAL';rEl.className='badge reg-nrm';}
  else{rEl.textContent='AGGRESSIVE';rEl.className='badge reg-ag';}

  // streak
  const skEl=$('sk');
  if(sd.streak&&sd.streak!=='N/A'){
    skEl.textContent=sd.streak+' Streak';
    skEl.className='badge '+(sd.streak.includes('W')?'sk':'sk-l');
  }else{skEl.textContent='No Streak';skEl.className='badge sk-n';}

  // engines
  function fillEng(stEl,wrEl,pnlEl,tEl,frEl,mkEl,makerDone,makerOrder,s,label){
    if(stEl){stEl.textContent=s.running?'Running':'Stopped';stEl.className='st '+(s.running?'st-r':'st-s');}
    tEl.textContent=(s.closed_trades||0)+'';
    wrEl.textContent=(s.win_rate||0)+'%';
    const pv=s.total_pnl||0;
    pnlEl.textContent=fpnl(pv);
    pnlEl.style.color=vc(pv);
    // fill rate from total/closed
    const fr=s.total_trades?(s.closed_trades||0)/s.total_trades*100:0;
    if(frEl)frEl.textContent=fr.toFixed(0)+'%';
    if(mkEl){
      if(makerDone){mkEl.textContent='✓ Done for window';mkEl.className='mk mk-d';}
      else if(makerOrder){mkEl.textContent='⚡ Order active';mkEl.className='mk mk-a';}
      else{mkEl.textContent='○ Waiting for signal';mkEl.className='mk mk-a';}
    }
  }
  const btcSt=s.status?.btc15m||{}, ethSt=s.status?.eth15m||{}, cbSt=s.status?.coinbase||{};
  fillEng($('btc-st'),$('btc-wr'),$('btc-pnl'),$('btc-t'),$('btc-fr'),$('btc-mk'),btcSt.maker_done,btcSt.maker_order_id,btc,'btc');
  fillEng($('eth-st'),$('eth-wr'),$('eth-pnl'),$('eth-t'),$('eth-fr'),$('eth-mk'),ethSt.maker_done,ethSt.maker_order_id,eth,'eth');
  const cbPv=cb.total_pnl||0;
  $('cb-t').textContent=(cb.closed_trades||0)+'';
  $('cb-wr').textContent=(cb.win_rate||0)+'%';
  $('cb-pnl').textContent=fpnl(cbPv);
  $('cb-pnl').style.color=vc(cbPv);
  $('cb-pr').textContent=((cbSt.pairs||[]).join(', ')||'-');
  if(cbSt.running){$('cb-st').textContent=cbSt.dry_run?'Dry Run':'LIVE';$('cb-st').className='st '+(cbSt.dry_run?'st-d':'st-l');}
  else{$('cb-st').textContent='Stopped';$('cb-st').className='st st-s';}

  // transactions
  renderTx(tx||[]);

  // positions
  renderPos(pos,'pos-grid');
  renderPos(pos,'pos-full');
  $('pos-cnt').textContent=pos.length+' open';

  // pnl bars
  const assets=st.assets||[];
  const total=assets.reduce((s,a)=>s+Math.abs(a.pnl||0),1);
  $('pnl-bars').innerHTML=assets.length===0
    ?'<div style="padding:14px;color:var(--mut);text-align:center">No P&L data yet</div>'
    :assets.slice(0,10).map(a=>{
      const w=(Math.abs(a.pnl||0)/total*100).toFixed(1);
      const pos2=a.pnl>=0;
      return`<div class="pb2">
        <div class="pbh"><span class="pbl">${((a.asset||'-')||'').substring(0,40)}</span><span class="pbv" style="color:${vc(a.pnl)}">${fpnl(a.pnl)}</span></div>
        <div class="pbt"><div class="pbf ${pos2?'pf-g':'pf-r'}" style="width:${w}%"></div></div>
      </div>`;
    }).join('');

  // trades
  all=s.recent_trades||[];
  renderT(all.slice(0,30),'recent-tbody');
  renderT(all,'all-tbody');

  const now=new Date().toLocaleTimeString('en-US',{hour12:false,timeZone:'America/New_York'});
  $('lastupd').textContent='Updated '+now+' ET';
}

function renderTx(txns){
  const el=$('txlist');
  if(!txns||txns.length===0){el.innerHTML='<div style="text-align:center;padding:30px;color:var(--mut)">No recent transactions</div>';return;}
  el.innerHTML=txns.map(t=>{
    const dc=t.type==='fill'?'txf':t.type==='cancel'?'txc':'txe';
    const ec=t.engine.includes('BTC')?'te-b':t.engine.includes('ETH')?'te-e':'te-c';
    const p=t.pnl?((parseFloat(t.pnl)>=0)?'tp-p':'tp-n'):'';
    const ev=t.event.length>28?t.event.substring(0,28)+'…':t.event;
    return`<div class="txi">
      <span class="txd ${dc}"></span>
      <span class="tt">${(t.time||'').split(' ')[1]||'—'}</span>
      <span class="te ${ec}">${t.engine}</span>
      <span class="tc">${ev}</span>
      <span class="tp ${p}">${t.pnl?fpnl(parseFloat(t.pnl)):''}</span>
    </div>`;
  }).join('');
}

function renderPos(pos,id){
  const el=$(id);
  if(!pos||pos.length===0){
    el.innerHTML='<div style="text-align:center;padding:20px;color:var(--mut);font-size:.85rem;grid-column:1/-1">No open positions</div>';return;
  }
  el.innerHTML=pos.map(p=>{
    const cp=p.cash_pnl||0;
    const is15m=(p.slug||'').includes('15m')||(p.market||'').includes('Up or Down');
    const mkt=((p.market||p.asset||'-')||'').substring(0,55);
    return`<div class="pos">
      <div class="posn">${mkt}</div>
      <div class="posr"><span class="pnl ${pcls(cp)}">${fpnl(cp)}</span></div>
      <div class="pd">
        <div class="pdi"><span>Avg price</span><span>$${((p.avg_price||0)).toFixed(3)}</span></div>
        <div class="pdi"><span>Amount</span><span>${((p.amount||0)).toFixed(2)}</span></div>
        <div class="pdi"><span>Value</span><span>$${((p.current_value||0)).toFixed(2)}</span></div>
        <div class="pdi"><span>Direction</span><span>${p.outcome||'-'}</span></div>
      </div>
      <div class="pbadges">
        ${is15m?'<span class="pb pb15">15m</span>':''}
        ${p.redeemable?'<span class="pb pbr">Redeemable</span>':''}
      </div>
    </div>`;
  }).join('');
}

function renderT(trades,tbody){
  const el=$(tbody);
  if(!trades||trades.length===0){el.innerHTML='<tr><td colspan="10" style="text-align:center;padding:20px;color:var(--mut)">No trades</td></tr>';return;}
  el.innerHTML=trades.map(t=>`<tr>
    <td><span class="eb ${ecls(t.engine)}">${eclean(t.engine)}</span></td>
    <td>${((t.asset||'-')||'').substring(0,22)}</td>
    <td>${t.direction||'-'}</td>
    <td>${t.entry_price!=null?'$'+t.entry_price.toFixed(3):'—'}</td>
    <td>${t.exit_price!=null?'$'+t.exit_price.toFixed(3):'—'}</td>
    <td class="${t.pnl_absolute!=null?vc2(t.pnl_absolute):''}">${fpnl(t.pnl_absolute)}</td>
    <td class="${t.pnl_percent!=null?vc2(t.pnl_percent):''}">${fpct(t.pnl_percent)}</td>
    <td>${(t.duration_mins||0)}m</td>
    <td>${t.exit_type||(t.timestamp_close?'closed':'open')}</td>
    <td style="white-space:nowrap;color:var(--mut);font-size:.73rem">${t.open_time||'-'}</td>
  </tr>`).join('');
}

function filter(btn,type){
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
  curF=type;
  let f=all;
  if(type==='btc')f=all.filter(t=>(t.engine||'').includes('btc'));
  else if(type==='eth')f=all.filter(t=>(t.engine||'').includes('eth'));
  else if(type==='poly')f=all.filter(t=>(t.engine||'').includes('polymarket')&&!(t.engine||'').includes('btc')&&!(t.engine||'').includes('eth'));
  else if(type==='cb')f=all.filter(t=>(t.engine||'').includes('coinbase'));
  renderT(f,'all-tbody');
}

function showTab(tab){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.textContent.toLowerCase()===tab));
  document.querySelectorAll('.tp').forEach(p=>p.classList.toggle('on',p.id==='tab-'+tab));
}

update();setInterval(update,8000);
</script>
</body>
</html>
'''

# ── HTTP Server ────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def send_json(self,d,c=200):
        self.send_response(c);self.send_header('Content-Type','application/json');self.send_header('Access-Control-Allow-Origin','*');self.end_headers();self.wfile.write(json.dumps(d).encode())
    def send_html(self,c):
        self.send_response(200);self.send_header('Content-Type','text/html;charset=utf-8');self.send_header('Access-Control-Allow-Origin','*');self.end_headers();self.wfile.write(c.encode())
    def do_OPTIONS(self):
        self.send_response(200);self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Methods','GET,OPTIONS');self.end_headers()
    def do_GET(self):
        p=self.path.split('?')[0]
        if p=='/api/summary':
            stats=get_engine_stats();stats['assets']=get_asset_pnl()
            self.send_json({
                'stats':stats,'positions':get_poly_positions(),
                'open_pnl':round(sum(p.get('cash_pnl',0)for p in get_poly_positions()),2),
                'capital':get_capital(),'status':get_bot_status(),
                'recent_trades':get_recent_trades(100),
                'today_pnl':get_today_stats(),'all_pnl':get_alltime_stats(),
                'timestamp':datetime.now(timezone.utc).isoformat(),
            })
        elif p=='/api/7day': self.send_json(get_7day_stats())
        elif p=='/api/transactions': self.send_json(get_live_transactions())
        elif p=='/api/trades': self.send_json(get_recent_trades(200))
        elif p in('/','/index.html'): self.send_html(HTML)
        else: self.send_error(404)

class S:
    def __init__(self,host='0.0.0.0',port=8080): self.host=host;self.port=port
    def start(self):
        self.server=HTTPServer((self.host,self.port),H)
        print(f"[Dashboard] http://{self.host}:{self.port}")
        try:
            import subprocess;ip=subprocess.check_output(['hostname','-I'],text=True).strip().split()[0];print(f"[Dashboard] http://{ip}:{self.port}")
        except:pass
        self.server.serve_forever()
    def stop(self): self.server.shutdown()

if __name__=='__main__':
    import argparse
    a=argparse.ArgumentParser();a.add_argument('--host',default='0.0.0.0');a.add_argument('--port',default=8080,type=int)
    args=a.parse_args()
    S(args.host,args.port).start()
