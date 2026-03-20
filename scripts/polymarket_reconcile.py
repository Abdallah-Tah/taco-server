#!/usr/bin/env python3
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

GAMMA = 'https://gamma-api.polymarket.com/markets?slug={slug}'

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
SECRETS = Path.home() / '.config' / 'openclaw' / 'secrets.env'
JOURNAL_DB = ROOT / 'journal.db'
BTC_LOG = Path('/tmp/polymarket_btc15m.log')
ETH_LOG = Path('/tmp/polymarket_eth15m.log')
VENV = ROOT / '.polymarket-venv' / 'bin' / 'python3'
EXECUTOR = ROOT / 'scripts' / 'polymarket_executor.py'
POSITIONS_API = 'https://data-api.polymarket.com/positions?user={user}'

WINDOW_SEC = 900
LOCAL_TZ = ZoneInfo('America/New_York')


def load_secrets():
    data = {}
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def dt_from_log(ts):
    # engine logs are in local ET
    return datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').replace(tzinfo=LOCAL_TZ)


def slug_for(engine, ts):
    window_ts = int(ts.timestamp()) - (int(ts.timestamp()) % WINDOW_SEC)
    prefix = 'btc' if engine == 'btc15m' else 'eth'
    return f'{prefix}-updown-15m-{window_ts}', window_ts


def title_for(engine, window_ts):
    # best-effort human title for notes only
    prefix = 'Bitcoin' if engine == 'btc15m' else 'Ethereum'
    return f'{prefix} Up or Down - {window_ts}'


def parse_log(path, engine):
    events = []
    if not path.exists():
        return events
    mode = 'DRY'
    pending = None
    lines = path.read_text(errors='ignore').splitlines()
    for line in lines:
        m = re.match(r'^\[(.*?)\] (.*)$', line)
        if not m:
            continue
        ts_s, body = m.groups()
        ts = dt_from_log(ts_s)
        if 'STARTING (DRY RUN)' in body:
            mode = 'DRY'
        elif 'STARTING (LIVE)' in body:
            mode = 'LIVE'
        if 'signal!' in body:
            side = 'UP' if ' UP signal!' in body else ('DOWN' if ' DOWN signal!' in body else None)
            pm = re.search(r'price=(\d+\.\d+)', body)
            dm = re.search(r'delta=([+-]?\d+\.\d+)%', body)
            sm = re.search(r'size=\$(\d+\.\d+)', body)
            if side and pm and sm:
                slug, window_ts = slug_for(engine, ts)
                pending = {
                    'engine': engine,
                    'time': ts.isoformat(),
                    'window_ts': window_ts,
                    'slug': slug,
                    'side': side,
                    'entry_price': float(pm.group(1)),
                    'delta_pct': float(dm.group(1)) if dm else None,
                    'size_usd': float(sm.group(1)),
                    'mode': mode,
                    'signal_line': line,
                }
        elif pending and 'Result:' in body:
            pending['result_line'] = line
            pending['placed'] = 'Result: True' in body
            events.append(pending)
            pending = None
    return events


def load_positions():
    s = load_secrets()
    user = s.get('POLYMARKET_FUNDER', '')
    if not user:
        return []
    r = requests.get(POSITIONS_API.format(user=user), timeout=20)
    if r.status_code != 200:
        return []
    return r.json()


def load_open_orders():
    try:
        res = subprocess.run([str(VENV), str(EXECUTOR), 'orders'], capture_output=True, text=True, timeout=30)
        return json.loads(res.stdout)
    except Exception:
        return []


def fetch_market_result(slug):
    try:
        r = requests.get(GAMMA.format(slug=slug), timeout=15)
        data = r.json() if r.status_code == 200 else []
        if not data:
            return None
        m = data[0]
        outcome_prices = json.loads(m.get('outcomePrices', '[]'))
        outcomes = json.loads(m.get('outcomes', '[]'))
        closed = bool(m.get('closed'))
        winner = None
        if len(outcome_prices) >= 2 and len(outcomes) >= 2:
            if str(outcome_prices[0]) in ('1', '1.0') or float(outcome_prices[0]) >= 0.999:
                winner = outcomes[0].upper()
            elif str(outcome_prices[1]) in ('1', '1.0') or float(outcome_prices[1]) >= 0.999:
                winner = outcomes[1].upper()
        return {
            'closed': closed,
            'winner': winner,
            'conditionId': m.get('conditionId'),
            'question': m.get('question'),
            'outcomes': outcomes,
            'outcomePrices': outcome_prices,
        }
    except Exception:
        return None


def reconcile():
    positions = load_positions()
    orders = load_open_orders()
    positions_by_slug = {}
    for p in positions:
        slug = p.get('slug')
        if slug:
            positions_by_slug.setdefault(slug, []).append(p)

    market_cache = {}
    rows = []
    for event in parse_log(BTC_LOG, 'btc15m') + parse_log(ETH_LOG, 'eth15m'):
        if event['mode'] != 'LIVE' or not event.get('placed'):
            continue
        status = 'PLACED'
        pnl = None
        matched = None
        market = market_cache.get(event['slug'])
        if market is None:
            market = fetch_market_result(event['slug'])
            market_cache[event['slug']] = market

        # Prefer positions match by slug + outcome
        plist = positions_by_slug.get(event['slug'], [])
        for p in plist:
            outcome = (p.get('outcome') or '').upper()
            if outcome == event['side']:
                matched = p
                break

        if matched:
            redeemable = bool(matched.get('redeemable'))
            cur_val = float(matched.get('currentValue', 0) or 0)
            cash_pnl = float(matched.get('cashPnl', 0) or 0)
            pnl = cash_pnl
            if redeemable:
                status = 'WON' if cur_val > 0 else 'LOST'
            else:
                status = 'PENDING'
        else:
            # Then try open orders by time proximity / price / outcome
            for o in orders:
                if o.get('status') != 'LIVE':
                    continue
                try:
                    created = datetime.fromtimestamp(int(o.get('created_at')), tz=timezone.utc).astimezone(LOCAL_TZ)
                except Exception:
                    continue
                price = float(o.get('price', 0) or 0)
                outcome = (o.get('outcome') or '').upper()
                if abs((created - datetime.fromisoformat(event['time'])).total_seconds()) < 180 and abs(price - event['entry_price']) < 0.02 and outcome == event['side']:
                    status = 'OPEN_ORDER'
                    matched = o
                    break

            # If market resolved, use market outcome directly
            window_end = datetime.fromtimestamp(event['window_ts'] + WINDOW_SEC, tz=LOCAL_TZ)
            now = datetime.now(LOCAL_TZ)
            if market and market.get('closed') and market.get('winner') in ('UP','DOWN'):
                win = market['winner'] == event['side']
                status = 'WON' if win else 'LOST'
                shares = (event['size_usd'] / event['entry_price']) if event['entry_price'] else 0.0
                cur_val = shares * (1.0 if win else 0.0)
                pnl = cur_val - event['size_usd']
            elif matched is None:
                if now < window_end:
                    status = 'PENDING'
                else:
                    status = 'UNKNOWN'

        rows.append({
            'time': event['time'],
            'engine': event['engine'],
            'side': event['side'],
            'entry_price': event['entry_price'],
            'size_usd': event['size_usd'],
            'slug': event['slug'],
            'resolved': bool(market.get('closed')) if market else False,
            'winner': market.get('winner') if market else None,
            'result': status,
            'pnl': pnl,
        })
    rows.sort(key=lambda r: r['time'])
    return rows


def sync_journal(rows):
    conn = sqlite3.connect(str(JOURNAL_DB))
    c = conn.cursor()
    for r in rows:
        if r['result'] not in ('WON', 'LOST'):
            continue
        trade_id = f"{r['engine']}_{r['slug']}_{r['side'].lower()}"
        ts_open = r['time']
        ts_close = datetime.fromtimestamp((int(datetime.fromisoformat(ts_open).timestamp()) - (int(datetime.fromisoformat(ts_open).timestamp()) % WINDOW_SEC)) + WINDOW_SEC, tz=timezone.utc).isoformat()
        exit_price = 1.0 if r['result'] == 'WON' else 0.0
        pnl = float(r['pnl'] or 0.0)
        pnl_pct = (pnl / r['size_usd'] * 100) if r['size_usd'] else 0.0
        pos_size = (r['size_usd'] / r['entry_price']) if r['entry_price'] else 0.0
        c.execute("SELECT 1 FROM trades WHERE id = ?", (trade_id,))
        exists = c.fetchone() is not None
        vals = (
            trade_id, r['engine'], ts_open, ts_close, r['slug'],
            'poly-15m', r['side'], r['entry_price'], exit_price,
            pos_size, r['size_usd'], pnl, pnl_pct,
            'resolved', WINDOW_SEC, 'normal', f"auto-reconciled {r['result']}"
        )
        if exists:
            c.execute("""
                UPDATE trades SET timestamp_close=?, exit_price=?, position_size=?, position_size_usd=?,
                    pnl_absolute=?, pnl_percent=?, exit_type=?, hold_duration_seconds=?, notes=?
                WHERE id=?
            """, (ts_close, exit_price, pos_size, r['size_usd'], pnl, pnl_pct, 'resolved', WINDOW_SEC, f"auto-reconciled {r['result']}", trade_id))
        else:
            c.execute("""
                INSERT INTO trades (
                    id, engine, timestamp_open, timestamp_close, asset, category, direction,
                    entry_price, exit_price, position_size, position_size_usd, pnl_absolute,
                    pnl_percent, exit_type, hold_duration_seconds, regime, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, vals)
    conn.commit()
    conn.close()


def summarize(rows):
    resolved = [r for r in rows if r['result'] in ('WON', 'LOST')]
    wins = sum(1 for r in resolved if r['result'] == 'WON')
    losses = sum(1 for r in resolved if r['result'] == 'LOST')
    pending = sum(1 for r in rows if r['result'] in ('PENDING', 'OPEN_ORDER', 'PLACED', 'UNKNOWN'))
    net = sum(float(r['pnl'] or 0.0) for r in resolved)
    return {
        'placed': len(rows),
        'resolved': len(resolved),
        'wins': wins,
        'losses': losses,
        'pending': pending,
        'win_rate': (wins / len(resolved) * 100.0) if resolved else 0.0,
        'net_pnl': net,
    }


def main():
    json_mode = '--json' in sys.argv
    no_sync = '--no-sync' in sys.argv
    rows = reconcile()
    if not no_sync:
        sync_journal(rows)
    summary = summarize(rows)
    if json_mode:
        print(json.dumps({'rows': rows, 'summary': summary}, indent=2))
        return

    print('| Time | Engine | Side | Entry Price | Result | P&L |')
    print('|---|---|---:|---:|---|---:|')
    for r in rows:
        print(f"| {r['time']} | {r['engine']} | {r['side']} | {r['entry_price']:.4f} | {r['result']} | {float(r['pnl'] or 0):+.2f} |")
    print()
    print(f"Placed: {summary['placed']} | Resolved: {summary['resolved']} | Wins: {summary['wins']} | Losses: {summary['losses']} | Pending: {summary['pending']} | Win rate: {summary['win_rate']:.1f}% | Net P&L: ${summary['net_pnl']:.2f}")

if __name__ == '__main__':
    main()
