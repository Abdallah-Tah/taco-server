#!/usr/bin/env python3
import json
import hashlib
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from journal import journal_close as shared_journal_close
from journal import journal_open as shared_journal_open
from journal import open_journal

GAMMA = 'https://gamma-api.polymarket.com/markets?slug={slug}'
ACTIVITY_API = 'https://data-api.polymarket.com/activity'
TRADES_API = 'https://data-api.polymarket.com/trades'
POSITIONS_API = 'https://data-api.polymarket.com/positions?user={user}'

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
SECRETS = Path.home() / '.config' / 'openclaw' / 'secrets.env'
JOURNAL_DB = ROOT / 'journal.db'
REDEEM_NOTIFY_STATE = ROOT / '.redeem_notify_state.json'
BTC_SHARED_JOURNAL = str(os.environ.get('BTC15M_SHARED_JOURNAL', 'false')).lower() in ('1', 'true', 'yes', 'on')
ETH_SHARED_JOURNAL = str(os.environ.get('ETH15M_SHARED_JOURNAL', 'false')).lower() in ('1', 'true', 'yes', 'on')
SOL_SHARED_JOURNAL = str(os.environ.get('SOL15M_SHARED_JOURNAL', 'false')).lower() in ('1', 'true', 'yes', 'on')
BTC_SHARED_DB = Path(os.environ.get('BTC15M_JOURNAL_DB', str(ROOT / 'btc15m_journal.db')))
ETH_SHARED_DB = Path(os.environ.get('ETH15M_JOURNAL_DB', str(ROOT / 'eth15m_journal.db')))
SOL_SHARED_DB = Path(os.environ.get('SOL15M_JOURNAL_DB', str(ROOT / 'sol15m_journal.db')))
VENV = ROOT / '.polymarket-venv' / 'bin' / 'python3'
EXECUTOR = ROOT / 'scripts' / 'polymarket_executor.py'
WINDOW_SEC = 900
LOCAL_TZ = ZoneInfo('America/New_York')
REQUEST_TIMEOUT_POSITIONS = 8
REQUEST_TIMEOUT_TRADES = 8
REQUEST_TIMEOUT_ACTIVITY = 8
REQUEST_TIMEOUT_MARKET = 5
SUBPROCESS_TIMEOUT_ORDERS = 10
REDEEM_NOTIFY_MAX_AGE_SEC = int(os.environ.get('REDEEM_NOTIFY_MAX_AGE_SEC', '1800'))


def _normalize_tx_hash(tx):
    t = str(tx or '').strip().lower()
    if t.startswith('0x'):
        t = t[2:]
    return t


def _shared_db_for_engine(engine: str) -> Path | None:
    if engine == 'btc15m' and BTC_SHARED_JOURNAL:
        return BTC_SHARED_DB
    if engine == 'eth15m' and ETH_SHARED_JOURNAL:
        return ETH_SHARED_DB
    if engine == 'sol15m' and SOL_SHARED_JOURNAL:
        return SOL_SHARED_DB
    return None


def _trade_id_for_row(engine: str, slug: str, side: str) -> str | None:
    if engine not in ('btc15m', 'eth15m', 'sol15m'):
        return None
    stable = f"{engine}:{slug}:{side or 'UNKNOWN'}"
    return hashlib.sha256(stable.encode()).hexdigest()[:32]


def load_secrets():
    data = {}
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def load_positions(user, timeout=REQUEST_TIMEOUT_POSITIONS):
    if not user:
        return []
    try:
        r = requests.get(POSITIONS_API.format(user=user), timeout=timeout)
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
    try:
        (ROOT / '.poly_positions.json').write_text(json.dumps(clean, indent=2))
    except Exception:
        pass
    return clean


def fetch_user_trades(user, timeout=REQUEST_TIMEOUT_TRADES):
    try:
        r = requests.get(TRADES_API, params={'user': user, 'limit': 1000, 'offset': 0, 'takerOnly': 'false'}, timeout=timeout)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def fetch_user_activity(user, timeout=REQUEST_TIMEOUT_ACTIVITY):
    try:
        r = requests.get(ACTIVITY_API, params={'user': user, 'limit': 1000, 'offset': 0}, timeout=timeout)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def fetch_market_result(slug, timeout=REQUEST_TIMEOUT_MARKET):
    try:
        r = requests.get(GAMMA.format(slug=slug), timeout=timeout)
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


def load_open_orders(timeout=SUBPROCESS_TIMEOUT_ORDERS):
    try:
        res = subprocess.run([str(VENV), str(EXECUTOR), 'orders'], capture_output=True, text=True, timeout=timeout)
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


def _load_redeem_notify_state():
    if REDEEM_NOTIFY_STATE.exists():
        try:
            data = json.loads(REDEEM_NOTIFY_STATE.read_text())
            sent_txs = [_normalize_tx_hash(x) for x in (data.get('sent_txs') or [])]
            sent_txs = [x for x in sent_txs if x]
            return {'sent_txs': list(sent_txs)[-5000:]}
        except Exception:
            pass
    return {'sent_txs': []}


def _save_redeem_notify_state(state):
    try:
        sent_txs = [_normalize_tx_hash(x) for x in (state.get('sent_txs') or [])]
        sent_txs = [x for x in sent_txs if x]
        REDEEM_NOTIFY_STATE.write_text(json.dumps({'sent_txs': list(sent_txs)[-5000:]}))
    except Exception:
        pass


def reconcile(fast_post_resolution=False):
    s = load_secrets()
    user = s.get('POLYMARKET_FUNDER', '').lower()
    live_positions = [] if fast_post_resolution else load_positions(user)
    if not fast_post_resolution:
        sync_live_positions_file(live_positions)
    open_orders = [] if fast_post_resolution else load_open_orders()
    trades = fetch_user_trades(user)
    activity = fetch_user_activity(user)
    if fast_post_resolution:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        cutoff_ts = now_ts - 86400
        trades = [t for t in trades if int(t.get('timestamp') or 0) >= cutoff_ts]
        activity = [a for a in activity if int(a.get('timestamp') or 0) >= cutoff_ts]

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
    if fast_post_resolution:
        recent_slugs = []
        seen_recent = set()
        for t in sorted(uniq_trades, key=lambda x: int(x.get('timestamp') or 0), reverse=True):
            slug = str(t.get('slug') or '')
            if slug in seen_recent:
                continue
            seen_recent.add(slug)
            recent_slugs.append(slug)
            if len(recent_slugs) >= 32:
                break
        recent_slug_set = set(recent_slugs)
        uniq_trades = [t for t in uniq_trades if str(t.get('slug') or '') in recent_slug_set]
        activity = [a for a in activity if str(a.get('slug') or '') in recent_slug_set]

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
        ts_open = r['time']
        ts_close = datetime.fromtimestamp((int(datetime.fromisoformat(ts_open).timestamp()) - (int(datetime.fromisoformat(ts_open).timestamp()) % WINDOW_SEC)) + WINDOW_SEC, tz=timezone.utc).isoformat()
        exit_price = 1.0 if r['status'] == 'RESOLVED_WON' else 0.0
        pnl = float(r['realized_pnl'] or 0.0)
        pnl_pct = (pnl / r['total_cost'] * 100) if r['total_cost'] else 0.0
        # --- Shadow classification (classify actual trade by entry price) ---
        entry_price = r.get('entry_price_avg') or r.get('entry_price', 0.5)
        if 0.45 <= entry_price <= 0.62:
            shadow_class = "kept"
            shadow_reason = "allow_0.45_0.62"
        elif entry_price > 0.70:
            shadow_class = "filtered"
            shadow_reason = "block_gt_0.70"
        else:
            shadow_class = "filtered"
            shadow_reason = "outside_0.45_0.62"
        lo = int(entry_price * 10) / 10
        shadow_bucket = f"{lo:.1f}-{lo+0.1:.1f}"
        notes = f"market-reconciled {r['status']} fill_count={r['fill_count']} shadow={shadow_class} shadow_bucket={shadow_bucket} shadow_price={entry_price:.4f} shadow_reason={shadow_reason}"
        if r.get('redeem_tx'):
            notes += f" redeem_tx={r['redeem_tx']}"
        shared_db = _shared_db_for_engine(r['engine'])
        trade_id = _trade_id_for_row(r['engine'], r['slug'], r['side'])
        if shared_db and trade_id:
            sconn = open_journal(shared_db, f"{r['engine']}_reconcile")
            try:
                try:
                    shared_journal_close(
                        sconn,
                        trade_id=trade_id,
                        exit_price=exit_price,
                        pnl_absolute=pnl,
                        pnl_percent=pnl_pct,
                        exit_type='resolved',
                        hold_duration_seconds=WINDOW_SEC,
                        timestamp_close=ts_close,
                        notes=notes,
                    )
                except RuntimeError:
                    shared_journal_open(
                        sconn,
                        trade_id=trade_id,
                        engine=r['engine'],
                        asset=r['slug'],
                        category='poly-15m',
                        direction=r['side'],
                        entry_price=r['entry_price_avg'],
                        position_size=r['total_shares'],
                        position_size_usd=r['total_cost'],
                        regime='normal',
                        notes=notes,
                        timestamp_open=ts_open,
                    )
                    shared_journal_close(
                        sconn,
                        trade_id=trade_id,
                        exit_price=exit_price,
                        pnl_absolute=pnl,
                        pnl_percent=pnl_pct,
                        exit_type='resolved',
                        hold_duration_seconds=WINDOW_SEC,
                        timestamp_close=ts_close,
                        notes=notes,
                    )
            finally:
                sconn.close()
            continue
        c.execute(
            """
            SELECT rowid
            FROM trades
            WHERE engine=? AND asset=? AND timestamp_open=? AND direction=?
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (r['engine'], r['slug'], ts_open, r['side']),
        )
        existing = c.fetchone()
        if existing:
            c.execute("""
                UPDATE trades SET timestamp_close=?, entry_price=?, exit_price=?, position_size=?, position_size_usd=?,
                    pnl_absolute=?, pnl_percent=?, exit_type=?, hold_duration_seconds=?, notes=?
                WHERE rowid=?
            """, (ts_close, r['entry_price_avg'], exit_price, r['total_shares'], r['total_cost'], pnl, pnl_pct, 'resolved', WINDOW_SEC, notes, existing[0]))
        else:
            c.execute("""
                INSERT INTO trades (
                    engine, timestamp_open, timestamp_close, asset, category, direction,
                    entry_price, exit_price, position_size, position_size_usd, pnl_absolute,
                    pnl_percent, exit_type, hold_duration_seconds, regime, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (r['engine'], ts_open, ts_close, r['slug'], 'poly-15m', r['side'], r['entry_price_avg'], exit_price, r['total_shares'], r['total_cost'], pnl, pnl_pct, 'resolved', WINDOW_SEC, 'normal', notes))
    conn.commit()
    conn.close()


def _send_redeem_notification(title, value, tx, slug=None):
    prefix = '[REDEEM]'
    tl = (title or '').lower()
    if 'solana up or down' in tl:
        prefix = '[SOL-REDEEM]'
    elif 'bitcoin up or down' in tl:
        prefix = '[BTC-REDEEM]'
    elif 'ethereum up or down' in tl:
        prefix = '[ETH-REDEEM]'

    extra = ''
    if slug:
        try:
            row = None
            for db_path in [JOURNAL_DB, BTC_SHARED_DB if BTC_SHARED_JOURNAL else None, ETH_SHARED_DB if ETH_SHARED_JOURNAL else None, SOL_SHARED_DB if SOL_SHARED_JOURNAL else None]:
                if not db_path:
                    continue
                conn = sqlite3.connect(str(db_path))
                c = conn.cursor()
                c.execute("SELECT engine, direction, entry_price, position_size_usd, position_size, pnl_absolute FROM trades WHERE asset=? AND engine IN ('btc15m','eth15m','sol15m','xrp15m') ORDER BY timestamp_open DESC LIMIT 1", (slug,))
                row = c.fetchone()
                conn.close()
                if row:
                    break
            if row:
                engine, direction, entry_price, size_usd, shares, pnl = row
                outcome = 'WIN' if float(pnl or 0) > 0 else 'LOSS' if float(pnl or 0) < 0 else 'FLAT'
                extra = f" engine={str(engine).upper()} dir={direction} entry={float(entry_price or 0):.4f} size=${float(size_usd or 0):.2f} shares={float(shares or 0):.2f} outcome={outcome} pnl=${float(pnl or 0):.2f}"
        except Exception:
            pass
    tx_norm = _normalize_tx_hash(tx)
    tx_display = f"0x{tx_norm}" if tx_norm else (tx or 'n/a')
    message = f"💰 CHA-CHING! {prefix} Redeemed ${float(value or 0):.2f} from {title}{extra} tx={tx_display}"
    try:
        subprocess.run([
            '/home/abdaltm86/.local/bin/openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', str(load_secrets().get('CHAT_ID', '7520899464')),
            '--message', message,
        ], check=False, capture_output=True, text=True, timeout=20)
    except Exception:
        pass


def backfill_redeem_rows(user):
    activity = fetch_user_activity(user)
    conn = sqlite3.connect(str(JOURNAL_DB))
    c = conn.cursor()
    inserted = 0
    notify_state = _load_redeem_notify_state()
    sent_txs = set(notify_state.get('sent_txs') or [])
    now_ts = int(datetime.now(timezone.utc).timestamp())
    for a in activity:
        if a.get('type') != 'REDEEM':
            continue
        tx_norm = _normalize_tx_hash(a.get('transactionHash') or '')
        if not tx_norm:
            continue
        tx = f"0x{tx_norm}"
        event_ts = int(a.get('timestamp') or 0)
        is_fresh = bool(event_ts) and (now_ts - event_ts) <= REDEEM_NOTIFY_MAX_AGE_SEC
        c.execute(
            "SELECT 1 FROM trades WHERE engine='polymarket_redeem' AND (notes LIKE ? OR notes LIKE ?) LIMIT 1",
            (f'%tx={tx}%', f'%tx={tx_norm}%'),
        )
        already_in_journal = bool(c.fetchone())
        if already_in_journal:
            sent_txs.add(tx_norm)
            continue
        ts = datetime.fromtimestamp(int(a.get('timestamp') or 0), tz=timezone.utc).isoformat()
        title = (a.get('title') or '')[:200]
        val = float(a.get('usdcSize') or 0)
        condition = a.get('conditionId') or ''
        notes = f"condition={condition} tx={tx} slug={a.get('slug') or ''}"
        c.execute("""
            INSERT INTO trades (
                engine, timestamp_open, timestamp_close, asset, category, direction,
                entry_price, exit_price, position_size, position_size_usd, pnl_absolute,
                pnl_percent, exit_type, hold_duration_seconds, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ('polymarket_redeem', ts, ts, title, 'redeem', 'REDEEM', 0.0, 0.0, 0.0, 0.0, val, 0.0, 'redeemed', 0, 'normal', notes))
        if tx_norm not in sent_txs and is_fresh:
            _send_redeem_notification(title, val, tx, slug=a.get('slug') or '')
        sent_txs.add(tx_norm)
        inserted += 1
    conn.commit()
    conn.close()
    notify_state['sent_txs'] = list(sent_txs)[-5000:]
    _save_redeem_notify_state(notify_state)
    return inserted


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
    fast_post_resolution = '--fast-post-resolution' in sys.argv
    rows = reconcile(fast_post_resolution=fast_post_resolution)
    inserted_redeems = 0
    if not no_sync:
        sync_journal(rows)
        s = load_secrets()
        user = s.get('POLYMARKET_FUNDER', '').lower()
        if user:
            inserted_redeems = backfill_redeem_rows(user)
    summary = summarize(rows)
    posted_stats = {'btc15m': {'posted_orders': 0, 'filled_orders': 0}, 'eth15m': {'posted_orders': 0, 'filled_orders': 0}}
    if not fast_post_resolution:
        posted_stats = load_posted_order_stats()
    if json_mode:
        print(json.dumps({'rows': rows, 'summary': summary, 'posted_stats': posted_stats, 'inserted_redeems': inserted_redeems}, indent=2))
        return

    print('| Time | Engine | Slug | Side | Fill Count | Shares | Cost | Redeem | Realized P&L | Status |')
    print('|---|---|---|---|---:|---:|---:|---:|---:|---|')
    for r in rows:
        print(f"| {r['time']} | {r['engine']} | {r['slug']} | {r['side']} | {r['fill_count']} | {r['total_shares']:.4f} | {r['total_cost']:.4f} | {float(r['redeem_received'] or 0):.4f} | {float(r['realized_pnl'] or 0):+.4f} | {r['status']} |")
    print()
    print(json.dumps({'summary': summary, 'posted_stats': posted_stats, 'inserted_redeems': inserted_redeems}, indent=2))


if __name__ == '__main__':
    main()
