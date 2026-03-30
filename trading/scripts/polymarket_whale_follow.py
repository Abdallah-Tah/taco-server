#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
SCRIPTS = ROOT / 'scripts'
import sys
sys.path.insert(0, str(SCRIPTS))

from config import (
    POLY_DEFAULT_SIZE, POLY_HIGH_CONVICTION_SIZE, POLY_MAX_SIZE,
    POLY_MIN_SHARES, NEWS_MIN_EDGE, MAX_CORRELATED_POSITIONS,
    WHALE_WATCHLIST_REFRESH, NEWS_WATCHLIST_REFRESH,
)
from journal import log_trade_open
from portfolio import _load_portfolio, _save_portfolio, check_drawdown, snapshot, is_paused
import polymarket_executor as pex
import polymarket_news_arb as newsarb
from correlation import check_correlation

DATA_API = 'https://data-api.polymarket.com'
CLOB_HOST = 'https://clob.polymarket.com'
WATCHLIST_FILE = ROOT / '.poly_whale_watchlist.json'
STATE_FILE = ROOT / '.poly_whale_follow_state.json'
POSITIONS_FILE = ROOT / '.poly_whale_follow_positions.json'
LOG_FILE = ROOT / '.poly_whale_follow_log.json'
TOR_PROXY = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}

TOP_HOLDER_LIMIT = 8
WHALE_MIN_PROFIT = 25.0
WHALE_MIN_WINRATE = 0.55
WHALE_ACTIVE_DAYS = 30
WHALE_SCAN_INTERVAL = 45
ARB_MIN_GAP = 0.03

@dataclass
class Whale:
    wallet: str
    name: str
    pseudonym: str
    win_rate: float
    profit: float
    last_active: int
    sample_trades: int
    watched_markets: list[str]

@dataclass
class WatchMarket:
    market_id: str
    condition_id: str
    title: str
    slug: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    category: str
    end_date: str


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
    save_json(LOG_FILE, data[-1000:])


def poly_price(token_id: str, side: str = 'buy') -> float | None:
    s = requests.Session(); s.proxies = TOR_PROXY
    try:
        r = s.get(f'{CLOB_HOST}/price', params={'token_id': token_id, 'side': side}, timeout=8)
        if r.status_code == 200:
            return float(r.json().get('price', 0) or 0)
    except Exception:
        return None
    return None


def get_watch_markets() -> list[WatchMarket]:
    base = newsarb.load_watchlist(force_refresh=False)
    out = []
    for m in base:
        out.append(WatchMarket(
            market_id=m.market_id,
            condition_id=m.condition_id,
            title=m.title,
            slug=m.slug,
            yes_token_id=m.yes_token_id,
            no_token_id=m.no_token_id,
            yes_price=m.current_yes_price,
            no_price=m.current_no_price,
            category=m.category,
            end_date=m.end_date,
        ))
    return out


def get_holders(condition_ids: list[str]) -> list[dict]:
    if not condition_ids:
        return []
    r = requests.get(f'{DATA_API}/holders', params={'market': ','.join(condition_ids), 'limit': TOP_HOLDER_LIMIT, 'minBalance': 1}, timeout=20)
    r.raise_for_status()
    return r.json()


def get_user_trades(wallet: str, limit: int = 200) -> list[dict]:
    r = requests.get(f'{DATA_API}/trades', params={'user': wallet, 'limit': limit, 'offset': 0}, timeout=20)
    r.raise_for_status()
    return r.json()


def profit_stats(trades: list[dict]) -> tuple[float, float, int]:
    books = defaultdict(lambda: {'BUY': [], 'SELL': []})
    for t in sorted(trades, key=lambda x: x.get('timestamp', 0)):
        books[t.get('asset')][t.get('side','BUY')].append(t)
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
    win_rate = (wins / closed) if closed else 0.0
    return realized, win_rate, closed


def build_watchlist() -> list[Whale]:
    markets = get_watch_markets()
    holders = get_holders([m.condition_id for m in markets[:20]])
    by_wallet = {}
    wallet_markets = defaultdict(set)
    for bucket in holders:
        token = bucket.get('token')
        for h in bucket.get('holders', []):
            w = (h.get('proxyWallet') or '').lower()
            if not w:
                continue
            by_wallet.setdefault(w, {'wallet': w, 'name': h.get('name',''), 'pseudonym': h.get('pseudonym','')})
            wallet_markets[w].add(token)
    whales = []
    cutoff = int(time.time()) - WHALE_ACTIVE_DAYS * 86400
    for wallet, meta in by_wallet.items():
        try:
            trades = get_user_trades(wallet, limit=200)
        except Exception:
            continue
        realized, win_rate, closed = profit_stats(trades)
        last_active = max([int(t.get('timestamp', 0) or 0) for t in trades], default=0)
        if last_active < cutoff:
            continue
        if realized < WHALE_MIN_PROFIT:
            continue
        if win_rate < WHALE_MIN_WINRATE:
            continue
        whales.append(Whale(
            wallet=wallet,
            name=meta.get('name',''),
            pseudonym=meta.get('pseudonym',''),
            win_rate=win_rate,
            profit=realized,
            last_active=last_active,
            sample_trades=closed,
            watched_markets=sorted(wallet_markets[wallet])[:12],
        ))
    whales.sort(key=lambda x: (x.profit, x.win_rate, x.last_active), reverse=True)
    save_json(WATCHLIST_FILE, [asdict(w) for w in whales])
    return whales


def load_watchlist(force_refresh: bool = False) -> list[Whale]:
    if force_refresh or not WATCHLIST_FILE.exists() or (time.time() - WATCHLIST_FILE.stat().st_mtime) > WHALE_WATCHLIST_REFRESH:
        return build_watchlist()
    return [Whale(**x) for x in load_json(WATCHLIST_FILE, [])]


def market_map() -> dict[str, WatchMarket]:
    return {m.condition_id: m for m in get_watch_markets()}


def drawdown_paused() -> tuple[bool, str]:
    portfolio = _load_portfolio()
    snap = snapshot()
    total = snap.get('total_usd') or 100.0
    portfolio = check_drawdown(portfolio, total)
    _save_portfolio(portfolio)
    return is_paused(portfolio), f'capital=${total:.2f}'


def same_market_open(condition_id: str, token_id: str) -> bool:
    shared = load_json(ROOT / '.poly_positions.json', {})
    local = load_json(POSITIONS_FILE, {})
    if token_id in shared or token_id in local:
        return True
    return False


def choose_size_usd(gap: float) -> float:
    usd = POLY_HIGH_CONVICTION_SIZE if gap >= 0.06 else POLY_DEFAULT_SIZE
    return min(usd, POLY_MAX_SIZE)


def scan_once(whales: list[Whale], debug: bool = False) -> tuple[list[dict], dict]:
    state = load_json(STATE_FILE, {'last_seen_ts': {}, 'cycles': 0})
    mm = market_map()
    detected = []
    arb_checks = []
    for whale in whales[:10]:
        try:
            trades = get_user_trades(whale.wallet, limit=50)
        except Exception:
            continue
        last_seen = int(state['last_seen_ts'].get(whale.wallet, 0) or 0)
        fresh = [t for t in trades if int(t.get('timestamp', 0) or 0) > last_seen]
        newest = max([int(t.get('timestamp', 0) or 0) for t in trades], default=last_seen)
        state['last_seen_ts'][whale.wallet] = max(last_seen, newest)
        for t in fresh:
            cid = t.get('conditionId')
            if cid not in mm:
                continue
            market = mm[cid]
            side = 'YES' if int(t.get('outcomeIndex', 0) or 0) == 0 else 'NO'
            whale_price = float(t.get('price', 0) or 0)
            token_id = market.yes_token_id if side == 'YES' else market.no_token_id
            current = poly_price(token_id, side='buy') or (market.yes_price if side == 'YES' else market.no_price)
            gap = whale_price - current
            arb = {
                'wallet': whale.wallet,
                'name': whale.name or whale.pseudonym,
                'market': market.title,
                'side': side,
                'whale_price': whale_price,
                'current_price': current,
                'gap': round(gap, 4),
                'tx_ts': int(t.get('timestamp', 0) or 0),
            }
            arb_checks.append(arb)
            if gap < ARB_MIN_GAP:
                arb['skip'] = f'gap too small ({gap:.3f} < {ARB_MIN_GAP:.3f})'
                continue
            paused, reason = drawdown_paused()
            if paused:
                arb['skip'] = f'drawdown paused: {reason}'
                continue
            if same_market_open(market.condition_id, token_id):
                arb['skip'] = 'market already open elsewhere'
                continue
            existing = load_json(ROOT / '.poly_positions.json', {})
            allowed, thesis, corr_count = check_correlation(market.title, existing)
            if not allowed:
                arb['skip'] = f'correlation guard thesis={thesis} count={corr_count}'
                continue
            usd = choose_size_usd(gap)
            shares = max(POLY_MIN_SHARES, round(usd / max(current, 0.01), 2))
            detected.append({
                'wallet': whale.wallet,
                'name': whale.name or whale.pseudonym,
                'market': market.title,
                'condition_id': market.condition_id,
                'token_id': token_id,
                'side': side,
                'price': round(current, 4),
                'whale_price': whale_price,
                'gap': round(gap * 100, 2),
                'edge_pct': round(gap * 100, 2),
                'shares': shares,
                'usd_size': usd,
                'headline': f"whale {whale.wallet[:8]} copied in {market.title}",
            })
    state['cycles'] = int(state.get('cycles', 0)) + 1
    save_json(STATE_FILE, state)
    stats = {
        'cycle': state['cycles'],
        'whales_checked': min(len(whales), 10),
        'activity_detected': len(arb_checks),
        'arb_checks': arb_checks,
    }
    append_log('scan', {'stats': stats, 'would_trades': detected})
    return detected, stats


def execute_trade(candidate: dict, live: bool = False) -> dict:
    payload = dict(candidate)
    payload['status'] = 'DRY_RUN' if not live else 'LIVE'
    if not live:
        return payload
    client = pex.get_client()
    result = pex.place_order(client, candidate['token_id'], candidate['shares'], candidate['price'], pex.BUY, candidate['market'], candidate['condition_id'])
    if not result:
        return {'status': 'FAILED', **candidate}
    trade_id = log_trade_open(
        trade_id=None,
        engine='whale_follow',
        asset=candidate['token_id'],
        category='whale_follow',
        direction=f"BUY_{candidate['side']}",
        entry_price=candidate['price'],
        position_size=candidate['shares'],
        position_size_usd=candidate['usd_size'],
        edge_percent=candidate['edge_pct'],
        confidence=0.8,
        regime='whale',
        notes=json.dumps(candidate)[:1000],
    )
    return {'status': 'LIVE', 'trade_id': trade_id, **candidate}


def cmd_watchlist():
    whales = load_watchlist(force_refresh=True)
    print(f'WHALES_FOUND {len(whales)}')
    for i, w in enumerate(whales[:10], 1):
        last = datetime.fromtimestamp(w.last_active, tz=timezone.utc).isoformat() if w.last_active else 'n/a'
        print(f'{i:02d}. {w.wallet} | win_rate={w.win_rate*100:.1f}% | profit=${w.profit:.2f} | last_active={last} | name={w.name or w.pseudonym}')
    return 0


def cmd_scan(cycles: int = 1, live: bool = False):
    whales = load_watchlist(force_refresh=False)
    for i in range(cycles):
        found, stats = scan_once(whales, debug=True)
        print(f'=== SCAN CYCLE {stats["cycle"]} ===')
        print(f'whales_checked={stats["whales_checked"]} activity_detected={stats["activity_detected"]}')
        for arb in stats['arb_checks'][:20]:
            print(json.dumps(arb, ensure_ascii=False))
        if found:
            print('WOULD_TRADE')
            for c in found:
                print(json.dumps(execute_trade(c, live=live), ensure_ascii=False))
        else:
            print('WOULD_TRADE none')
        if i < cycles - 1:
            time.sleep(WHALE_SCAN_INTERVAL)
    return 0


def main():
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
