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
TRADES_API = 'https://data-api.polymarket.com/trades'

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
        tokens = json.loads(m.get('clobTokenIds', '[]'))
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
            'tokens': tokens,
        }
    except Exception:
        return None


def fetch_user_trades(user):
    try:
        r = requests.get(TRADES_API, params={'user': user, 'limit': 1000, 'offset': 0, 'takerOnly': 'false'}, timeout=30)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def fetch_user_activity(user):
    try:
        r = requests.get('https://data-api.polymarket.com/activity', params={'user': user, 'limit': 1000, 'offset': 0}, timeout=30)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def load_maker_log_entries():
    entries = []
    log_path = ROOT / '.poly_trade_log.json'
    if not log_path.exists():
        return entries
    try:
        data = json.loads(log_path.read_text())
    except Exception:
        return entries
    for row in data:
        if row.get('action') != 'MAKER_BUY':
            continue
        ts = row.get('timestamp')
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        token_prefix = str(row.get('token_id') or '').replace('...', '')
        entries.append({
            'timestamp': dt,
            'token_prefix': token_prefix,
            'amount': float(row.get('amount') or 0),
            'price': float(row.get('price') or 0),
            'order_id': row.get('order_id'),
            'submitted_price': float(row.get('submitted_price') or row.get('price') or 0),
        })
    return entries


def match_order_id(trade, maker_entries):
    asset = str(trade.get('asset') or '')
    price = float(trade.get('price') or 0)
    size = float(trade.get('size') or 0)
    try:
        ts = datetime.fromtimestamp(int(trade.get('timestamp') or 0), tz=timezone.utc)
    except Exception:
        ts = None
    matches = []
    for e in maker_entries:
        if e['token_prefix'] and not asset.startswith(e['token_prefix']):
            continue
        if abs(e['price'] - price) > 0.05:
            continue
        if abs(e['amount'] - size) > 0.2:
            continue
        if ts is not None:
            diff = abs((ts - e['timestamp']).total_seconds())
            if diff > 300:
                continue
        else:
            diff = 999999
        matches.append((diff, e))
    matches.sort(key=lambda x: x[0])
    return matches[0][1] if matches else None


def sync_live_positions_file(live_positions):
    clean = {}
    for p in live_positions:
        asset = str(p.get('asset') or '')
        title = (p.get('title') or '').strip()
        if not asset or not title:
            continue
        clean[asset] = {
            'amount': float(p.get('size') or 0),
            'avg_price': float(p.get('avgPrice') or 0),
            'side': 'BUY',
            'market': title,
            'condition_id': p.get('conditionId') or '',
            'opened': p.get('endDateIso') or p.get('slug') or '',
            'slug': p.get('slug') or '',
            'outcome': p.get('outcome') or '',
            'current_value': float(p.get('currentValue') or 0),
            'cash_pnl': float(p.get('cashPnl') or 0),
            'redeemable': bool(p.get('redeemable')),
        }
    (ROOT / '.poly_positions.json').write_text(json.dumps(clean, indent=2))
    return clean


def reconcile():
    s = load_secrets()
    user = s.get('POLYMARKET_FUNDER', '').lower()
    live_positions = load_positions()
    positions_by_asset = {str(p.get('asset') or ''): p for p in live_positions if p.get('asset')}
    sync_live_positions_file(live_positions)
    open_orders = load_open_orders()
    maker_entries = load_maker_log_entries()
    trades = fetch_user_trades(user)
    activity = fetch_user_activity(user)

    target_trades = []
    seen = set()
    for t in trades:
        slug = str(t.get('slug') or '')
        if not (slug.startswith('btc-updown-15m-') or slug.startswith('eth-updown-15m-')):
            continue
        key = (t.get('transactionHash'), str(t.get('asset')), float(t.get('size') or 0), float(t.get('price') or 0))
        if key in seen:
            continue
        seen.add(key)
        target_trades.append(t)

    market_cache = {}
    redeem_by_slug = {}
    for a in activity:
        if a.get('type') != 'REDEEM':
            continue
        slug = str(a.get('slug') or '')
        if not slug:
            continue
        redeem_by_slug.setdefault(slug, []).append(a)

    rows = []
    for t in sorted(target_trades, key=lambda x: int(x.get('timestamp') or 0)):
        slug = str(t.get('slug') or '')
        engine = 'btc15m' if slug.startswith('btc-') else 'eth15m'
        side = str(t.get('outcome') or '').upper()
        asset_id = str(t.get('asset') or '')
        size = float(t.get('size') or 0)
        price = float(t.get('price') or 0)
        size_usd = size * price
        ts = datetime.fromtimestamp(int(t.get('timestamp') or 0), tz=timezone.utc).astimezone(LOCAL_TZ).isoformat()
        market = market_cache.get(slug)
        if market is None:
            market = fetch_market_result(slug)
            market_cache[slug] = market
        pos = positions_by_asset.get(asset_id)
        redeem_events = sorted(redeem_by_slug.get(slug, []), key=lambda x: int(x.get('timestamp') or 0))
        redeem_usdc = 0.0
        redeem_tx = None
        if redeem_events:
            redeem_usdc = max(float(a.get('usdcSize') or 0) for a in redeem_events)
            for a in redeem_events:
                if float(a.get('usdcSize') or 0) > 0:
                    redeem_tx = a.get('transactionHash')
                    break
        order = match_order_id(t, maker_entries)
        is_open_order = False
        if not pos:
            for o in open_orders:
                if str(o.get('asset_id') or '') == asset_id and (o.get('status') or '').upper() == 'LIVE':
                    is_open_order = True
                    break

        result = 'PENDING'
        pnl = None
        if market and market.get('closed'):
            winner = str(market.get('winner') or '').upper()
            if winner in ('UP', 'DOWN'):
                win = winner == side
                result = 'WON' if win else 'LOST'
                payout = redeem_usdc if redeem_usdc > 0 else (size if win else 0.0)
                pnl = payout - size_usd
            else:
                result = 'UNKNOWN'
        elif is_open_order:
            result = 'OPEN_ORDER'

        rows.append({
            'time': ts,
            'engine': engine,
            'side': side,
            'entry_price': price,
            'size_usd': size_usd,
            'shares': size,
            'slug': slug,
            'asset_id': asset_id,
            'order_id': order.get('order_id') if order else None,
            'submitted_price': order.get('submitted_price') if order else price,
            'filled': True,
            'resolved': bool(market.get('closed')) if market else False,
            'winner': market.get('winner') if market else None,
            'result': result,
            'pnl': pnl,
            'question': t.get('title') or (market.get('question') if market else ''),
            'redeemed': redeem_usdc > 0,
            'redeem_usdc': redeem_usdc,
            'redeem_tx': redeem_tx,
            'fill_tx': t.get('transactionHash'),
            'live_position': bool(pos),
        })
    rows.sort(key=lambda r: r['time'])
    return rows

def sync_journal(rows):
    conn = sqlite3.connect(str(JOURNAL_DB))
    c = conn.cursor()
    for r in rows:
        if r['result'] not in ('WON', 'LOST'):
            continue
        trade_id = f"{r['engine']}_{r['slug']}_{r['side'].lower()}_{str(r.get('fill_tx') or 'nofill')[:12]}"
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
    attempted = len(rows)
    filled_rows = [r for r in rows if r.get('filled')]
    filled = len(filled_rows)
    resolved = [r for r in filled_rows if r['result'] in ('WON', 'LOST')]
    wins = sum(1 for r in resolved if r['result'] == 'WON')
    losses = sum(1 for r in resolved if r['result'] == 'LOST')
    pending = sum(1 for r in filled_rows if r['result'] in ('PENDING', 'OPEN_ORDER', 'PLACED', 'UNKNOWN'))
    unfilled = sum(1 for r in rows if r['result'] == 'UNFILLED')
    net = sum(float(r['pnl'] or 0.0) for r in resolved)
    return {
        'attempted': attempted,
        'filled': filled,
        'fill_rate': (filled / attempted * 100.0) if attempted else 0.0,
        'placed': attempted,
        'resolved': len(resolved),
        'wins': wins,
        'losses': losses,
        'pending': pending,
        'unfilled': unfilled,
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
