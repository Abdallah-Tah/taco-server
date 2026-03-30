#!/usr/bin/env python3
import json
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from runtime_paths import SCRIPT_ROOT, TRADING_ROOT, resolve_runtime_python

GAMMA = 'https://gamma-api.polymarket.com/markets?slug={slug}'
ACTIVITY_API = 'https://data-api.polymarket.com/activity'
TRADES_API = 'https://data-api.polymarket.com/trades'
POSITIONS_API = 'https://data-api.polymarket.com/positions?user={user}'

ROOT = TRADING_ROOT
SECRETS = Path.home() / '.config' / 'openclaw' / 'secrets.env'
JOURNAL_DB = ROOT / 'journal.db'
VENV = resolve_runtime_python()
EXECUTOR = SCRIPT_ROOT / 'polymarket_executor.py'
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


def load_positions(user):
    if not user:
        return []
    try:
        r = requests.get(POSITIONS_API.format(user=user), timeout=20)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


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


def fetch_user_trades(user):
    try:
        r = requests.get(TRADES_API, params={'user': user, 'limit': 1000, 'offset': 0, 'takerOnly': 'false'}, timeout=30)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def fetch_user_activity(user):
    try:
        r = requests.get(ACTIVITY_API, params={'user': user, 'limit': 1000, 'offset': 0}, timeout=30)
        return r.json() if r.status_code == 200 else []
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


def load_open_orders():
    try:
        res = subprocess.run([str(VENV), str(EXECUTOR), 'orders'], capture_output=True, text=True, timeout=30)
        return json.loads(res.stdout)
    except Exception:
        return []


def load_posted_order_stats():
    stats = {'btc15m': {'posted_orders': 0, 'filled_orders': 0}, 'eth15m': {'posted_orders': 0, 'filled_orders': 0}}
    mapping = [
        ('btc15m', Path('/tmp/polymarket_btc15m.log'), '[BTC-MAKER]'),
        ('eth15m', Path('/tmp/polymarket_eth15m.log'), '[ETH-MAKER]'),
    ]
    for engine, path, tag in mapping:
        if not path.exists():
            continue
        lines = path.read_text(errors='ignore').splitlines()
        posted = []
        filled_ids = set()
        current_order = None
        for line in lines:
            if '[EXEC] [MAKER-RESULT]' in line and 'order_id=' in line and 'status=posted' in line:
                try:
                    oid = line.split('order_id=', 1)[1].split()[0]
                    posted.append(oid)
                    current_order = oid
                except Exception:
                    pass
            elif tag in line and 'fill verified via activity/trades' in line:
                if current_order:
                    filled_ids.add(current_order)
        stats[engine]['posted_orders'] = len(set(posted))
        stats[engine]['filled_orders'] = len(filled_ids)
    return stats


def reconcile():
    s = load_secrets()
    user = s.get('POLYMARKET_FUNDER', '').lower()
    live_positions = load_positions(user)
    sync_live_positions_file(live_positions)
    open_orders = load_open_orders()
    trades = fetch_user_trades(user)
    activity = fetch_user_activity(user)

    # Deduplicate raw trade fills.
    seen = set()
    uniq_trades = []
    for t in trades:
        slug = str(t.get('slug') or '')
        if not slug.startswith(('btc-updown-15m-', 'eth-updown-15m-')):
            continue
        key = (t.get('transactionHash'), slug, str(t.get('outcome') or '').upper(), float(t.get('size') or 0), float(t.get('price') or 0))
        if key in seen:
            continue
        seen.add(key)
        uniq_trades.append(t)

    # Aggregate fills by economic position: (engine, slug, side)
    grouped = {}
    for t in uniq_trades:
        slug = str(t.get('slug') or '')
        engine = 'btc15m' if slug.startswith('btc-') else 'eth15m'
        side = str(t.get('outcome') or '').upper()
        key = (engine, slug, side)
        row = grouped.setdefault(key, {
            'engine': engine,
            'slug': slug,
            'side': side,
            'fill_count': 0,
            'total_shares': 0.0,
            'total_cost': 0.0,
            'entry_price_avg': 0.0,
            'fill_txs': [],
            'times': [],
            'status': 'OPEN',
            'winner': None,
            'redeem_received': 0.0,
            'realized_pnl': None,
            'question': t.get('title') or '',
        })
        size = float(t.get('size') or 0)
        price = float(t.get('price') or 0)
        row['fill_count'] += 1
        row['total_shares'] += size
        row['total_cost'] += size * price
        row['fill_txs'].append(t.get('transactionHash'))
        row['times'].append(int(t.get('timestamp') or 0))
        row['question'] = row['question'] or t.get('title') or ''

    # Weight-average entry price.
    for row in grouped.values():
        row['entry_price_avg'] = (row['total_cost'] / row['total_shares']) if row['total_shares'] else 0.0

    # One redeem value per market slug (do not copy it to every fill row).
    redeem_by_slug = defaultdict(float)
    redeem_tx_by_slug = {}
    for a in activity:
        slug = str(a.get('slug') or '')
        if not slug.startswith(('btc-updown-15m-', 'eth-updown-15m-')):
            continue
        if a.get('type') != 'REDEEM':
            continue
        val = float(a.get('usdcSize') or 0)
        if val > redeem_by_slug[slug]:
            redeem_by_slug[slug] = val
            redeem_tx_by_slug[slug] = a.get('transactionHash')

    market_cache = {}
    live_positions_by_slug_side = {}
    for p in live_positions:
        slug = str(p.get('slug') or '')
        outcome = str(p.get('outcome') or '').upper()
        if slug.startswith(('btc-updown-15m-', 'eth-updown-15m-')):
            live_positions_by_slug_side[(slug, outcome)] = p

    live_order_keys = set()
    for o in open_orders:
        if (o.get('status') or '').upper() != 'LIVE':
            continue
        try:
            market_slug = str(o.get('market_slug') or '')
            outcome = str(o.get('outcome') or '').upper()
            if market_slug:
                live_order_keys.add((market_slug, outcome))
        except Exception:
            pass

    rows = []
    for key, row in sorted(grouped.items(), key=lambda kv: max(kv[1]['times'])):
        engine, slug, side = key
        market = market_cache.get(slug)
        if market is None:
            market = fetch_market_result(slug)
            market_cache[slug] = market
        if market:
            row['question'] = row['question'] or market.get('question') or ''
            row['winner'] = market.get('winner')
        row['time'] = datetime.fromtimestamp(max(row['times']), tz=timezone.utc).astimezone(LOCAL_TZ).isoformat()
        row['redeem_received'] = redeem_by_slug.get(slug, 0.0)
        row['redeem_tx'] = redeem_tx_by_slug.get(slug)
        row['live_position'] = bool(live_positions_by_slug_side.get((slug, side)))
        row['open_order'] = (slug, side) in live_order_keys

        if market and market.get('closed') and row['winner'] in ('UP', 'DOWN'):
            if row['winner'] == side:
                row['status'] = 'RESOLVED_WON'
                payout = row['redeem_received'] if row['redeem_received'] > 0 else row['total_shares']
            else:
                row['status'] = 'RESOLVED_LOST'
                payout = 0.0
            row['realized_pnl'] = payout - row['total_cost']
        else:
            row['status'] = 'OPEN' if row['live_position'] or row['open_order'] else 'PENDING'
            row['realized_pnl'] = None
        rows.append(row)

    return rows


def sync_journal(rows):
    conn = sqlite3.connect(str(JOURNAL_DB))
    c = conn.cursor()
    for r in rows:
        if r['status'] not in ('RESOLVED_WON', 'RESOLVED_LOST'):
            continue
        trade_id = f"{r['engine']}_{r['slug']}_{r['side'].lower()}"
        ts_open = r['time']
        ts_close = datetime.fromtimestamp((int(datetime.fromisoformat(ts_open).timestamp()) - (int(datetime.fromisoformat(ts_open).timestamp()) % WINDOW_SEC)) + WINDOW_SEC, tz=timezone.utc).isoformat()
        exit_price = 1.0 if r['status'] == 'RESOLVED_WON' else 0.0
        pnl = float(r['realized_pnl'] or 0.0)
        pnl_pct = (pnl / r['total_cost'] * 100) if r['total_cost'] else 0.0
        c.execute("SELECT 1 FROM trades WHERE id = ?", (trade_id,))
        exists = c.fetchone() is not None
        vals = (
            trade_id, r['engine'], ts_open, ts_close, r['slug'],
            'poly-15m', r['side'], r['entry_price_avg'], exit_price,
            r['total_shares'], r['total_cost'], pnl, pnl_pct,
            'resolved', WINDOW_SEC, 'normal', f"market-reconciled {r['status']} fill_count={r['fill_count']}"
        )
        if exists:
            c.execute("""
                UPDATE trades SET timestamp_open=?, timestamp_close=?, entry_price=?, exit_price=?, position_size=?, position_size_usd=?,
                    pnl_absolute=?, pnl_percent=?, exit_type=?, hold_duration_seconds=?, notes=?
                WHERE id=?
            """, (ts_open, ts_close, r['entry_price_avg'], exit_price, r['total_shares'], r['total_cost'], pnl, pnl_pct, 'resolved', WINDOW_SEC, f"market-reconciled {r['status']} fill_count={r['fill_count']}", trade_id))
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
    resolved = [r for r in rows if r['status'] in ('RESOLVED_WON', 'RESOLVED_LOST')]
    wins = sum(1 for r in resolved if r['status'] == 'RESOLVED_WON')
    losses = sum(1 for r in resolved if r['status'] == 'RESOLVED_LOST')
    pending = sum(1 for r in rows if r['status'] in ('OPEN', 'PENDING'))
    net = sum(float(r['realized_pnl'] or 0.0) for r in resolved)
    return {
        'market_positions': len(rows),
        'resolved': len(resolved),
        'wins': wins,
        'losses': losses,
        'pending': pending,
        'win_rate': (wins / len(resolved) * 100.0) if resolved else 0.0,
        'net_realized_pnl': net,
    }


def main():
    json_mode = '--json' in sys.argv
    no_sync = '--no-sync' in sys.argv
    rows = reconcile()
    if not no_sync:
        sync_journal(rows)
    summary = summarize(rows)
    posted_stats = load_posted_order_stats()
    if json_mode:
        print(json.dumps({'rows': rows, 'summary': summary, 'posted_stats': posted_stats}, indent=2))
        return

    print('| Time | Engine | Slug | Side | Fill Count | Shares | Cost | Redeem | Realized P&L | Status |')
    print('|---|---|---|---|---:|---:|---:|---:|---:|---|')
    for r in rows:
        print(f"| {r['time']} | {r['engine']} | {r['slug']} | {r['side']} | {r['fill_count']} | {r['total_shares']:.4f} | {r['total_cost']:.4f} | {float(r['redeem_received'] or 0):.4f} | {float(r['realized_pnl'] or 0):+.4f} | {r['status']} |")
    print()
    print(json.dumps({'summary': summary, 'posted_stats': posted_stats}, indent=2))


if __name__ == '__main__':
    main()
