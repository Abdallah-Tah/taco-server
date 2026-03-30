#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
SCRIPTS = ROOT / 'scripts'
import sys
sys.path.insert(0, str(SCRIPTS))

from config import (
    WHALE_SCAN_INTERVAL, WHALE_MIN_WIN_RATE, WHALE_MIN_TRADE_SIZE,
    WHALE_FOLLOW_DEFAULT_SIZE, WHALE_FOLLOW_HIGH_SIZE, WHALE_FOLLOW_MAX_SIZE,
    WHALE_FOLLOW_TP, WHALE_FOLLOW_SL, WHALE_FOLLOW_TIME_EXIT,
    WHALE_FOLLOW_MAX_POSITIONS, WHALE_WATCHLIST_REFRESH,
)
from journal import log_trade_open, log_trade_close
from portfolio import _load_portfolio, _save_portfolio, check_drawdown, snapshot, is_paused
from correlation import check_correlation
import polymarket_executor as pex

DATA_API = 'https://data-api.polymarket.com'
GAMMA_API = 'https://gamma-api.polymarket.com/markets'
CLOB_HOST = 'https://clob.polymarket.com'
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
WATCHLIST_FILE = ROOT / '.poly_whale_tracker_watchlist.json'
STATE_FILE = ROOT / '.poly_whale_tracker_state.json'
POSITIONS_FILE = ROOT / '.poly_whale_tracker_positions.json'
LOG_FILE = ROOT / '.poly_whale_tracker_log.json'

LEADERBOARD_URL = 'https://polymarket.com/leaderboard'
MIN_PROFIT = 5000.0
MAX_WHALES = 20
LARGE_TRADE_DISCOVERY = 500.0
ARB_THRESHOLD = 0.97
ALLOWED_THEME_TERMS = {
    'trump', 'biden', 'republican', 'democratic', 'democrat', 'senate', 'house', 'congress',
    'president', 'presidential', 'election', 'primary', 'impeach', 'pardon', 'court', 'judge',
    'sentenced', 'indicted', 'ruling', 'sec', 'etf', 'crypto', 'bitcoin', 'ethereum', 'solana',
    'regulation', 'ban', 'approval', 'ukraine', 'russia', 'ceasefire', 'putin', 'zelensky',
    'zelenskyy', 'china', 'taiwan', 'iran', 'israel', 'gaza', 'war', 'military', 'missile',
    'attack', 'conflict', 'sanctions', 'ufo', 'uap', 'aliens', 'disclosure', 'maxwell',
    'weinstein', 'texas'
}
BLOCKED_THEME_TERMS = {
    'nba', 'nfl', 'nhl', 'mlb', 'fifa', 'world cup', 'stanley cup', 'super bowl', 'uefa',
    'champions league', 'edmonton oilers', 'brazil win', 'movie', 'album', 'song', 'box office',
    'celebrity', 'oscars', 'grammys', 'emmys', 'wrestlemania', 'ufc', 'fight night'
}

@dataclass
class WhaleRecord:
    address: str
    win_rate: float
    total_profit: float
    last_active: str
    tracked_since: str
    sample_trades: int = 0
    source: str = 'data_api'
    name: str = ''

@dataclass
class TrackedPosition:
    trade_id: str
    token_id: str
    condition_id: str
    market: str
    side: str
    whale_address: str
    entry_price: float
    peak_price: float
    shares: float
    usd_size: float
    opened: str


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2))


def append_log(kind: str, payload: dict):
    data = load_json(LOG_FILE, [])
    data.append({'ts': datetime.now(timezone.utc).isoformat(), 'kind': kind, **payload})
    save_json(LOG_FILE, data[-2000:])


def gamma_active_markets(limit: int = 200) -> list[dict]:
    r = requests.get(GAMMA_API, params={'limit': limit, 'active': True, 'closed': False, 'archived': False}, timeout=20)
    r.raise_for_status()
    data = r.json()
    return data.get('data', []) if isinstance(data, dict) else data


def parse_json_field(val, default):
    if val is None:
        return default
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return default


def allowed_market_title(title: str) -> bool:
    t = (title or '').lower()
    if any(term in t for term in BLOCKED_THEME_TERMS):
        return False
    return any(term in t for term in ALLOWED_THEME_TERMS)


def active_market_map() -> dict[str, dict]:
    out = {}
    for m in gamma_active_markets(limit=400):
        try:
            title = m.get('question') or ''
            if not allowed_market_title(title):
                continue
            token_ids = parse_json_field(m.get('clobTokenIds'), [])
            prices = [float(x) for x in parse_json_field(m.get('outcomePrices'), [])]
            liquidity = float(m.get('liquidity') or m.get('liquidityNum') or 0)
            if len(token_ids) != 2 or len(prices) != 2:
                continue
            out[str(m.get('conditionId'))] = {
                'market_id': str(m.get('id')),
                'condition_id': str(m.get('conditionId')),
                'title': title,
                'slug': m.get('slug') or '',
                'yes_token_id': str(token_ids[0]),
                'no_token_id': str(token_ids[1]),
                'yes_price': prices[0],
                'no_price': prices[1],
                'liquidity': liquidity,
                'end_date': m.get('endDate') or m.get('endDateIso') or '',
            }
        except Exception:
            continue
    return out


def get_trades(**params) -> list[dict]:
    r = requests.get(f'{DATA_API}/trades', params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def get_holders(condition_ids: list[str], limit: int = 20) -> list[dict]:
    if not condition_ids:
        return []
    r = requests.get(f'{DATA_API}/holders', params={'market': ','.join(condition_ids), 'limit': limit, 'minBalance': 1}, timeout=20)
    r.raise_for_status()
    return r.json()


def trade_notional(t: dict) -> float:
    return float(t.get('size', 0) or 0) * float(t.get('price', 0) or 0)


def calc_profit_stats(trades: list[dict]) -> tuple[float, float, int, int]:
    books = defaultdict(lambda: {'BUY': [], 'SELL': []})
    large_trades = 0
    for t in sorted(trades, key=lambda x: x.get('timestamp', 0)):
        if trade_notional(t) >= LARGE_TRADE_DISCOVERY:
            large_trades += 1
        books[str(t.get('asset'))][t.get('side', 'BUY')].append(t)
    realized = 0.0
    wins = 0
    closed = 0
    for asset, sides in books.items():
        buy_qty = sum(float(x.get('size', 0) or 0) for x in sides['BUY'])
        sell_qty = sum(float(x.get('size', 0) or 0) for x in sides['SELL'])
        if buy_qty <= 0 or sell_qty <= 0:
            continue
        avg_buy = sum(float(x.get('size', 0) or 0) * float(x.get('price', 0) or 0) for x in sides['BUY']) / max(buy_qty, 1e-9)
        avg_sell = sum(float(x.get('size', 0) or 0) * float(x.get('price', 0) or 0) for x in sides['SELL']) / max(sell_qty, 1e-9)
        qty = min(buy_qty, sell_qty)
        pnl = (avg_sell - avg_buy) * qty
        realized += pnl
        closed += 1
        if pnl > 0:
            wins += 1
    win_rate = wins / closed if closed else 0.0
    return realized, win_rate, closed, large_trades


def build_watchlist() -> list[WhaleRecord]:
    now = datetime.now(timezone.utc)
    tracked_since = now.date().isoformat()
    cutoff_ts = int(time.time()) - 30 * 86400
    market_map = active_market_map()
    holders = get_holders(list(market_map.keys())[:40], limit=12)
    candidates = {}

    # A) public top-holder / equivalent public data
    for bucket in holders:
        for h in bucket.get('holders', []):
            addr = (h.get('proxyWallet') or '').lower()
            if addr:
                candidates.setdefault(addr, {'name': h.get('name', '')})

    # B) recent large trades discovery
    for cid in list(market_map.keys())[:60]:
        try:
            trades = get_trades(market=cid, limit=80)
        except Exception:
            continue
        for t in trades:
            if trade_notional(t) >= LARGE_TRADE_DISCOVERY:
                addr = (t.get('proxyWallet') or '').lower()
                if addr:
                    candidates.setdefault(addr, {'name': t.get('name', '') or t.get('pseudonym', '')})

    whales = []
    for addr, meta in candidates.items():
        try:
            trades = get_trades(user=addr, limit=250)
        except Exception:
            continue
        realized, win_rate, closed, large_trades = calc_profit_stats(trades)
        last_ts = max([int(t.get('timestamp', 0) or 0) for t in trades], default=0)
        if last_ts < cutoff_ts:
            continue
        if realized < MIN_PROFIT:
            continue
        if win_rate <= WHALE_MIN_WIN_RATE:
            continue
        if large_trades < 3:
            continue
        whales.append(WhaleRecord(
            address=addr,
            win_rate=win_rate,
            total_profit=realized,
            last_active=datetime.fromtimestamp(last_ts, tz=timezone.utc).date().isoformat(),
            tracked_since=tracked_since,
            sample_trades=closed,
            source='public_data',
            name=meta.get('name', ''),
        ))
    whales.sort(key=lambda x: (x.total_profit, x.win_rate), reverse=True)
    whales = whales[:MAX_WHALES]
    save_json(WATCHLIST_FILE, [asdict(w) for w in whales])
    return whales


def load_watchlist(force_refresh: bool = False) -> list[WhaleRecord]:
    if force_refresh or not WATCHLIST_FILE.exists() or (time.time() - WATCHLIST_FILE.stat().st_mtime) > WHALE_WATCHLIST_REFRESH:
        return build_watchlist()
    return [WhaleRecord(**x) for x in load_json(WATCHLIST_FILE, [])]


def poly_price(token_id: str, side: str = 'buy') -> float | None:
    s = requests.Session(); s.proxies = TOR_PROXY
    try:
        r = s.get(f'{CLOB_HOST}/price', params={'token_id': token_id, 'side': side}, timeout=8)
        if r.status_code == 200:
            return float(r.json().get('price', 0) or 0)
    except Exception:
        return None
    return None


def drawdown_paused() -> tuple[bool, str]:
    portfolio = _load_portfolio()
    snap = snapshot()
    total = snap.get('total_usd') or 100.0
    portfolio = check_drawdown(portfolio, total)
    _save_portfolio(portfolio)
    return is_paused(portfolio), f'capital=${total:.2f}'


def available_cash() -> float:
    try:
        client = pex.get_client()
        bal = client.get_balance_allowance(pex.BalanceAllowanceParams(asset_type=pex.AssetType.COLLATERAL))
        return int(bal.get('balance', 0)) / 1e6
    except Exception:
        return 0.0


def current_positions() -> dict:
    return load_json(POSITIONS_FILE, {})


def same_market_open(condition_id: str, token_id: str) -> bool:
    shared = load_json(ROOT / '.poly_positions.json', {})
    local = current_positions()
    return token_id in shared or token_id in local


def choose_size_usd(win_rate: float, whale_trade_usd: float, cash: float) -> float:
    usd = WHALE_FOLLOW_HIGH_SIZE if (win_rate > 0.75 and whale_trade_usd > 500) else WHALE_FOLLOW_DEFAULT_SIZE
    usd = min(usd, WHALE_FOLLOW_MAX_SIZE)
    return min(usd, cash * 0.10 if cash > 0 else usd)


def scan_whale_activity(whales: list[WhaleRecord], debug: bool = False) -> tuple[list[dict], dict]:
    state = load_json(STATE_FILE, {'last_seen_ts': {}, 'cycles': 0})
    market_map = active_market_map()
    alerts = []
    would_trades = []
    for whale in whales:
        try:
            trades = get_trades(user=whale.address, limit=80)
        except Exception:
            continue
        last_seen = int(state['last_seen_ts'].get(whale.address, 0) or 0)
        newest = max([int(t.get('timestamp', 0) or 0) for t in trades], default=last_seen)
        fresh = [t for t in trades if int(t.get('timestamp', 0) or 0) > last_seen]
        state['last_seen_ts'][whale.address] = max(last_seen, newest)
        for t in fresh:
            cid = str(t.get('conditionId') or '')
            if cid not in market_map:
                continue
            market = market_map[cid]
            trade_usd = trade_notional(t)
            if t.get('side') != 'BUY' or trade_usd <= WHALE_MIN_TRADE_SIZE:
                continue
            side = 'YES' if int(t.get('outcomeIndex', 0) or 0) == 0 else 'NO'
            whale_price = float(t.get('price', 0) or 0)
            token_id = market['yes_token_id'] if side == 'YES' else market['no_token_id']
            current = poly_price(token_id, side='buy') or (market['yes_price'] if side == 'YES' else market['no_price'])
            alert = {
                'whale_address': whale.address,
                'whale_win_rate': whale.win_rate,
                'market_id': market['market_id'],
                'condition_id': cid,
                'market': market['title'],
                'side': side,
                'size': round(trade_usd, 2),
                'price': whale_price,
                'current_price': current,
                'detected_ts': int(time.time()),
                'trade_ts': int(t.get('timestamp', 0) or 0),
            }
            alerts.append(alert)
            # entry rules
            skip = None
            if whale.win_rate <= WHALE_MIN_WIN_RATE:
                skip = 'whale win rate too low'
            elif market['liquidity'] <= 5000:
                skip = 'insufficient liquidity'
            elif not (0.10 <= current <= 0.90):
                skip = 'current price out of range'
            elif same_market_open(cid, token_id):
                skip = 'market already open'
            elif len(current_positions()) >= WHALE_FOLLOW_MAX_POSITIONS:
                skip = 'max whale-follow positions reached'
            elif (int(time.time()) - alert['trade_ts']) > 30:
                skip = 'detection too stale'
            else:
                shared = load_json(ROOT / '.poly_positions.json', {})
                allowed, thesis, corr_count = check_correlation(market['title'], shared)
                if not allowed:
                    skip = f'correlation guard thesis={thesis} count={corr_count}'
            if skip:
                alert['skip'] = skip
                continue
            cash = available_cash()
            usd = choose_size_usd(whale.win_rate, trade_usd, cash)
            shares = max(5.0, round(usd / max(current, 0.01), 2))
            would_trades.append({
                'whale_address': whale.address,
                'whale_win_rate': whale.win_rate,
                'market': market['title'],
                'condition_id': cid,
                'token_id': token_id,
                'side': side,
                'price': round(current, 4),
                'shares': shares,
                'usd_size': round(usd, 2),
                'trade_ts': alert['trade_ts'],
            })
    state['cycles'] = int(state.get('cycles', 0)) + 1
    save_json(STATE_FILE, state)
    stats = {'cycle': state['cycles'], 'alerts': alerts}
    append_log('scan', {'stats': stats, 'would_trades': would_trades})
    return would_trades, stats


def monitor_positions(live: bool = False) -> list[dict]:
    positions = current_positions()
    exits = []
    for token_id, pos in list(positions.items()):
        current = poly_price(token_id, side='sell') or float(pos['entry_price'])
        entry = float(pos['entry_price'])
        peak = max(float(pos.get('peak_price', entry)), current)
        pos['peak_price'] = peak
        move_pct = ((current - entry) / max(entry, 1e-9)) * 100.0
        hold_h = (datetime.now(timezone.utc) - datetime.fromisoformat(pos['opened'])).total_seconds() / 3600.0
        exit_reason = None
        if move_pct >= WHALE_FOLLOW_TP:
            exit_reason = 'TP'
        elif move_pct <= WHALE_FOLLOW_SL:
            exit_reason = 'SL'
        elif hold_h >= WHALE_FOLLOW_TIME_EXIT:
            exit_reason = 'TIME'
        elif ((peak - entry) / max(entry, 1e-9)) * 100.0 >= 8.0 and ((current - peak) / max(peak, 1e-9)) * 100.0 <= -4.0:
            exit_reason = 'MOMENTUM'
        else:
            try:
                recent = get_trades(user=pos['whale_address'], market=pos['condition_id'], limit=30)
                if any(t.get('side') == 'SELL' and int(t.get('outcomeIndex', 0) or 0) == (0 if pos['side'] == 'YES' else 1) for t in recent):
                    exit_reason = 'WHALE_EXIT'
            except Exception:
                pass
        positions[token_id] = pos
        if not exit_reason:
            continue
        pnl_pct = move_pct
        pnl_abs = (current - entry) * float(pos['shares'])
        exits.append({'market': pos['market'], 'entry': entry, 'exit': current, 'pnl': pnl_abs, 'pnl_pct': pnl_pct, 'hold_hours': round(hold_h, 2), 'whale_address': pos['whale_address'], 'exit_reason': exit_reason})
        if live:
            client = pex.get_client()
            pex.place_order(client, token_id, float(pos['shares']), current, pex.SELL, pos['market'], pos['condition_id'])
            log_trade_close(pos['trade_id'], exit_price=current, pnl_absolute=pnl_abs, pnl_percent=pnl_pct, exit_type=exit_reason, hold_duration_seconds=int(hold_h*3600), notes=json.dumps({'whale_address': pos['whale_address'], 'market': pos['market']})[:1000])
            del positions[token_id]
    save_json(POSITIONS_FILE, positions)
    if exits:
        append_log('exits', {'items': exits})
    return exits


def arb_checks() -> list[dict]:
    out = []
    for cid, m in list(active_market_map().items())[:120]:
        yes = poly_price(m['yes_token_id'], side='buy') or m['yes_price']
        no = poly_price(m['no_token_id'], side='buy') or m['no_price']
        s = yes + no
        if s < ARB_THRESHOLD:
            out.append({'market': m['title'], 'yes': round(yes, 4), 'no': round(no, 4), 'sum': round(s, 4), 'gap': round(1.0 - s, 4)})
    append_log('arb_checks', {'items': out[:50]})
    return out


def execute_follow(candidate: dict, live: bool = False) -> dict:
    result = {'status': 'DRY_RUN' if not live else 'LIVE', **candidate}
    if not live:
        return result
    client = pex.get_client()
    order = pex.place_order(client, candidate['token_id'], candidate['shares'], candidate['price'], pex.BUY, candidate['market'], candidate['condition_id'])
    if not order:
        return {'status': 'FAILED', **candidate}
    trade_id = log_trade_open(trade_id=str(uuid.uuid4()), engine='whale_follow', asset=candidate['token_id'], category='whale_follow', direction=f"BUY_{candidate['side']}", entry_price=candidate['price'], position_size=candidate['shares'], position_size_usd=candidate['usd_size'], edge_percent=0.0, confidence=candidate['whale_win_rate'], regime='whale_follow', notes=json.dumps(candidate)[:1000])
    positions = current_positions()
    positions[candidate['token_id']] = asdict(TrackedPosition(trade_id=trade_id, token_id=candidate['token_id'], condition_id=candidate['condition_id'], market=candidate['market'], side=candidate['side'], whale_address=candidate['whale_address'], entry_price=candidate['price'], peak_price=candidate['price'], shares=candidate['shares'], usd_size=candidate['usd_size'], opened=datetime.now(timezone.utc).isoformat()))
    save_json(POSITIONS_FILE, positions)
    return {'status': 'LIVE', 'trade_id': trade_id, **candidate}


def cmd_watchlist() -> int:
    whales = load_watchlist(force_refresh=True)
    print(f'WHALE_WATCHLIST {len(whales)}')
    for i, w in enumerate(whales[:30], 1):
        print(json.dumps(asdict(w), ensure_ascii=False))
    return 0


def cmd_scan(cycles: int = 1, live: bool = False) -> int:
    whales = load_watchlist(force_refresh=False)
    for i in range(cycles):
        monitor_positions(live=live)
        found, stats = scan_whale_activity(whales, debug=True)
        arbs = arb_checks()
        print(f'=== WHALE SCAN CYCLE {stats["cycle"]} ===')
        for a in stats['alerts'][:50]:
            print(f"WHALE ALERT: {a['whale_address'][:8]} bought {a['side']} on '{a['market'][:80]}' at ${a['price']:.3f} size=${a['size']:.2f}")
            if a.get('skip'):
                print(f"  skip={a['skip']}")
        if found:
            for f in found:
                print('FOLLOW TRADE:', json.dumps(execute_follow(f, live=live), ensure_ascii=False))
        else:
            print('FOLLOW TRADE: none')
        if arbs:
            for a in arbs[:20]:
                print(f"ARB OPPORTUNITY: {a['market'][:80]} YES=${a['yes']:.3f} + NO=${a['no']:.3f} = ${a['sum']:.3f} (gap=${a['gap']:.3f})")
        else:
            print('ARB OPPORTUNITY: none')
        if i < cycles - 1:
            time.sleep(WHALE_SCAN_INTERVAL)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('command', nargs='?', default='scan', choices=['watchlist', 'scan'])
    ap.add_argument('--cycles', type=int, default=1)
    ap.add_argument('--live', action='store_true')
    args = ap.parse_args()
    if args.command == 'watchlist':
        return cmd_watchlist()
    return cmd_scan(cycles=args.cycles, live=args.live)

if __name__ == '__main__':
    raise SystemExit(main())
