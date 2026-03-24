#!/usr/bin/env python3
"""Coinbase BTC grid trader (DRY RUN first).

Reuses the existing coinbase_momentum.py entrypoint so surrounding tooling keeps working,
but switches strategy logic to a spot grid system.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from coinbase.rest import RESTClient

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
SECRETS = Path.home() / '.config' / 'openclaw' / 'secrets.env'
JOURNAL_DB = ROOT / 'journal.db'
LOG_PATH = Path('/tmp/coinbase_momentum.log')
PID_PATH = Path('/tmp/coinbase_momentum.pid')
STATE_PATH = ROOT / '.coinbase_momentum_state.json'


def env_bool(key: str, default: bool) -> bool:
    if SECRETS.exists():
        try:
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line.startswith(key) and '=' in line:
                    value = line.split('=', 1)[1].strip().lower()
                    return value in ('1', 'true', 'yes', 'on')
        except Exception:
            pass
    return os.environ.get(key, str(default).lower()).lower() in ('1', 'true', 'yes', 'on')


def env_float(key: str, default: float) -> float:
    if SECRETS.exists():
        try:
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line.startswith(key) and '=' in line:
                    return float(line.split('=', 1)[1].strip())
        except Exception:
            pass
    return float(os.environ.get(key, default))


def env_int(key: str, default: int) -> int:
    if SECRETS.exists():
        try:
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line.startswith(key) and '=' in line:
                    return int(line.split('=', 1)[1].strip())
        except Exception:
            pass
    return int(os.environ.get(key, default))


def env_pairs(key: str, default: str) -> list[str]:
    if SECRETS.exists():
        try:
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line.startswith(key) and '=' in line:
                    value = line.split('=', 1)[1].strip()
                    return [x.strip() for x in value.split(',') if x.strip()]
        except Exception:
            pass
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(',') if x.strip()]


CB_ENABLED = env_bool('CB_ENABLED', True)
CB_POLL_INTERVAL = env_int('CB_POLL_INTERVAL', 30)
CB_PAIRS = env_pairs('CB_PAIRS', 'BTC-USD')
CB_USD_BUFFER = env_float('CB_USD_BUFFER', 0.50)
CB_DRY_RUN = env_bool('CB_DRY_RUN', True)
CB_GRID_ENABLED = env_bool('CB_GRID_ENABLED', True)
CB_GRID_SPACING = env_float('CB_GRID_SPACING', 200)
CB_GRID_PROFIT = env_float('CB_GRID_PROFIT', 300)
CB_GRID_LEVELS = env_int('CB_GRID_LEVELS', 5)
CB_GRID_SIZE_USD = env_float('CB_GRID_SIZE_USD', 4.00)
CB_GRID_RESET_HOURS = env_int('CB_GRID_RESET_HOURS', 4)

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('coinbase_grid')

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4')
CHAT_ID = os.environ.get('CHAT_ID', '7520899464')


def tg(msg: str):
    if CB_DRY_RUN:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': msg},
            timeout=10,
        )
    except Exception:
        pass


@dataclass
class GridLevel:
    level: int
    buy_price: float
    sell_price: float
    size_usd: float
    amount: float
    status: str = 'waiting_buy'  # waiting_buy | holding | completed
    bought_at: Optional[str] = None
    sold_at: Optional[str] = None
    trade_id: Optional[str] = None
    sell_trade_id: Optional[str] = None


class Bot:
    def __init__(self):
        self.running = True
        self.secrets = self._load_secrets()
        self.client = self._client()
        self.grid = {}
        self.grid_anchor = {}
        self.last_reset = {}
        self._load_state()
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

    def _handle_stop(self, *_):
        self.running = False
        log.info('[CB-GRID] stop requested')

    def _load_secrets(self):
        data = {}
        if SECRETS.exists():
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    data[k.strip()] = v.strip()
        return data

    def _client(self):
        api_key = self.secrets.get('COINBASE_API_KEY')
        private_key = self.secrets.get('COINBASE_PRIVATE_KEY_JSON')
        if not api_key or not private_key:
            raise RuntimeError('Missing Coinbase credentials in secrets.env')
        return RESTClient(api_key=api_key, api_secret=private_key)

    def _save_state(self):
        data = {
            'grid': {pair: [asdict(x) for x in levels] for pair, levels in self.grid.items()},
            'grid_anchor': self.grid_anchor,
            'last_reset': {k: v.isoformat() if isinstance(v, datetime) else v for k, v in self.last_reset.items()},
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        STATE_PATH.write_text(json.dumps(data, indent=2))

    def _load_state(self):
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text())
            self.grid = {pair: [GridLevel(**lvl) for lvl in levels] for pair, levels in data.get('grid', {}).items()}
            self.grid_anchor = data.get('grid_anchor', {})
            self.last_reset = {
                k: datetime.fromisoformat(v) if isinstance(v, str) else datetime.now(timezone.utc)
                for k, v in data.get('last_reset', {}).items()
            }
        except Exception as e:
            log.warning('[CB-GRID] state load failed: %s', e)

    def _journal_open(self, pair: str, side: str, entry_price: float, amount: float, usd_size: float, notes: str) -> str:
        trade_id = str(uuid.uuid4())
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.execute(
            """
            INSERT INTO trades (
                id, engine, timestamp_open, asset, category, direction,
                entry_price, position_size, position_size_usd, regime, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade_id,
                'coinbase_momentum',
                datetime.now(timezone.utc).isoformat(),
                pair,
                'coinbase-grid',
                side,
                entry_price,
                amount,
                usd_size,
                'normal',
                notes,
            ),
        )
        conn.commit()
        conn.close()
        return trade_id

    def _journal_close(self, trade_id: str, exit_price: float, pnl_abs: float, pnl_pct: float, exit_type: str, hold_seconds: int, notes: str):
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.execute(
            """
            UPDATE trades
            SET timestamp_close=?, exit_price=?, pnl_absolute=?, pnl_percent=?, exit_type=?, hold_duration_seconds=?, notes=?
            WHERE id=?
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                exit_price,
                pnl_abs,
                pnl_pct,
                exit_type,
                hold_seconds,
                notes,
                trade_id,
            ),
        )
        conn.commit()
        conn.close()

    def get_product_price(self, pair: str) -> float:
        product = self.client.get_product(pair)
        if isinstance(product, dict):
            return float(product['price'])
        return float(product.price)

    def get_available_usd(self) -> float:
        try:
            resp = self.client.get_accounts(limit=250)
            accounts = resp.get('accounts', []) if isinstance(resp, dict) else (getattr(resp, 'accounts', []) or [])
            for acct in accounts:
                try:
                    currency = acct.get('currency')
                    bal = acct.get('available_balance', {}) or {}
                    val = bal.get('value')
                except Exception:
                    currency = getattr(acct, 'currency', None)
                    bal = getattr(acct, 'available_balance', None)
                    if isinstance(bal, dict):
                        val = bal.get('value')
                    else:
                        val = getattr(bal, 'value', None) if bal else None
                if currency == 'USD':
                    return float(val or 0)
        except Exception as e:
            log.warning('[CB-GRID] get_available_usd error: %s', e)
        return 0.0

    def ensure_grid(self, pair: str, current_price: float):
        now = datetime.now(timezone.utc)
        last = self.last_reset.get(pair)
        levels = self.grid.get(pair, [])
        active_holding = any(l.status == 'holding' for l in levels)
        need_reset = (
            pair not in self.grid or
            not levels or
            last is None or
            (now - last) >= timedelta(hours=CB_GRID_RESET_HOURS)
        )
        if need_reset and not active_holding:
            self.grid_anchor[pair] = current_price
            self.last_reset[pair] = now
            new_levels = []
            for i in range(1, CB_GRID_LEVELS + 1):
                buy_price = round(current_price - (CB_GRID_SPACING * i), 2)
                sell_price = round(buy_price + CB_GRID_PROFIT, 2)
                amount = round(CB_GRID_SIZE_USD / buy_price, 8)
                new_levels.append(GridLevel(
                    level=i,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    size_usd=CB_GRID_SIZE_USD,
                    amount=amount,
                ))
            self.grid[pair] = new_levels
            log.info('[CB-GRID] Reset grid for %s around $%.2f | levels=%s', pair, current_price, [(l.buy_price, l.sell_price) for l in new_levels])
            self._save_state()

    def process_pair(self, pair: str):
        current = self.get_product_price(pair)
        self.ensure_grid(pair, current)
        levels = self.grid.get(pair, [])
        usd_available = self.get_available_usd()
        for lvl in levels:
            if lvl.status == 'waiting_buy':
                if current <= lvl.buy_price and usd_available >= (lvl.size_usd + CB_USD_BUFFER):
                    lvl.trade_id = self._journal_open(pair, 'BUY', lvl.buy_price, lvl.amount, lvl.size_usd, 'GRID DRY entry' if CB_DRY_RUN else 'GRID LIVE entry')
                    lvl.status = 'holding'
                    lvl.bought_at = datetime.now(timezone.utc).isoformat()
                    log.info('[CB-GRID-BUY][%s] level=%s buy=%.2f sell=%.2f amount=%.8f dry=%s', pair, lvl.level, lvl.buy_price, lvl.sell_price, lvl.amount, CB_DRY_RUN)
                    tg(f'[CB-GRID] BUY {pair} level {lvl.level} @ ${lvl.buy_price:.2f}')
            elif lvl.status == 'holding':
                if current >= lvl.sell_price:
                    pnl_abs = lvl.amount * (lvl.sell_price - lvl.buy_price)
                    pnl_pct = ((lvl.sell_price - lvl.buy_price) / lvl.buy_price * 100) if lvl.buy_price else 0.0
                    hold_seconds = 0
                    if lvl.bought_at:
                        hold_seconds = int((datetime.now(timezone.utc) - datetime.fromisoformat(lvl.bought_at)).total_seconds())
                    if lvl.trade_id:
                        self._journal_close(lvl.trade_id, lvl.sell_price, pnl_abs, pnl_pct, 'grid_take_profit', hold_seconds, 'GRID DRY exit' if CB_DRY_RUN else 'GRID LIVE exit')
                    lvl.status = 'completed'
                    lvl.sold_at = datetime.now(timezone.utc).isoformat()
                    log.info('[CB-GRID-SELL][%s] level=%s sold=%.2f pnl=$%.2f dry=%s', pair, lvl.level, lvl.sell_price, pnl_abs, CB_DRY_RUN)
                    tg(f'[CB-GRID] SELL {pair} level {lvl.level} @ ${lvl.sell_price:.2f} | P&L ${pnl_abs:.2f}')
        self._save_state()
        waiting = sum(1 for l in levels if l.status == 'waiting_buy')
        holding = sum(1 for l in levels if l.status == 'holding')
        completed = sum(1 for l in levels if l.status == 'completed')
        log.info('[CB-GRID] %s current=%.2f waiting=%s holding=%s completed=%s dry=%s', pair, current, waiting, holding, completed, CB_DRY_RUN)

    def run(self):
        PID_PATH.write_text(str(os.getpid()))
        log.info('[CB-GRID] Coinbase grid bot starting | dry=%s poll=%ss pairs=%s spacing=%s profit=%s levels=%s size=$%.2f reset=%sh',
                 CB_DRY_RUN, CB_POLL_INTERVAL, ','.join(CB_PAIRS), CB_GRID_SPACING, CB_GRID_PROFIT, CB_GRID_LEVELS, CB_GRID_SIZE_USD, CB_GRID_RESET_HOURS)
        if not CB_ENABLED or not CB_GRID_ENABLED:
            log.info('[CB-GRID] disabled via config')
            return
        while self.running:
            try:
                for pair in CB_PAIRS:
                    self.process_pair(pair)
            except Exception as e:
                log.exception('[CB-GRID] loop error: %s', e)
            time.sleep(CB_POLL_INTERVAL)


if __name__ == '__main__':
    Bot().run()
