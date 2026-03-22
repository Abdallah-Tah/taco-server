#!/usr/bin/env python3
"""Coinbase BTC/ETH momentum spot bot.

Completely separate from Polymarket/Solana systems.
- Own process
- Own log file (/tmp/coinbase_momentum.log)
- Own state files
- Reads only Coinbase creds + CB_ config from env/secrets
- Writes journal rows with engine='coinbase_momentum'

Starts in DRY RUN by default.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
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
    # First check secrets.env
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
    # First check secrets.env
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
    # First check secrets.env
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
    """Parse comma-separated pairs from secrets.env or env."""
    # First try to read from secrets.env file
    if SECRETS.exists():
        try:
            for line in SECRETS.read_text().splitlines():
                line = line.strip()
                if line.startswith(key) and '=' in line:
                    value = line.split('=', 1)[1].strip()
                    return [x.strip() for x in value.split(',') if x.strip()]
        except Exception:
            pass
    # Fallback to environment variable
    raw = os.environ.get(key, default)
    return [x.strip() for x in raw.split(',') if x.strip()]


# Coinbase Momentum Bot (isolated defaults; NOT using shared config.py to honor isolation)
# Note: CB_PAIRS is loaded from secrets.env if available, otherwise from env
CB_ENABLED = env_bool('CB_ENABLED', True)
CB_POLL_INTERVAL = env_int('CB_POLL_INTERVAL', 30)
CB_PAIRS = env_pairs('CB_PAIRS', 'BTC-USD,ETH-USD')
CB_MA_WINDOW = env_int('CB_MA_WINDOW', 30)
CB_CONSECUTIVE_MIN = env_int('CB_CONSECUTIVE_MIN', 2)
CB_MOMENTUM_MIN = env_float('CB_MOMENTUM_MIN', 0.02)
CB_TAKE_PROFIT = env_float('CB_TAKE_PROFIT', 0.8)
CB_STOP_LOSS = env_float('CB_STOP_LOSS', -0.5)
CB_TIME_EXIT_HOURS = env_float('CB_TIME_EXIT_HOURS', 1)
CB_USD_BUFFER = env_float('CB_USD_BUFFER', 10.00)
CB_MAX_POSITIONS = env_int('CB_MAX_POSITIONS', 2)
CB_DRY_RUN = env_bool('CB_DRY_RUN', True)
CB_SIGNAL_LOG_EVERY = env_int('CB_SIGNAL_LOG_EVERY', 1)


logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('coinbase_momentum')


# Telegram configuration (shared with other systems)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4')
CHAT_ID = os.environ.get('CHAT_ID', '7520899464')


def tg(msg: str):
    """Send Telegram message if not in dry run mode."""
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
class Position:
    asset: str
    entry_price: float
    amount: float
    entry_time: str
    high_watermark: float
    status: str = 'open'
    trade_id: Optional[str] = None
    entry_value_usd: float = 0.0


class Bot:
    def __init__(self):
        self.running = True
        self.secrets = self._load_secrets()
        self.client = self._client()
        self.price_history: dict[str, deque[float]] = {pair: deque(maxlen=CB_MA_WINDOW) for pair in CB_PAIRS}
        self.positions: dict[str, Position] = {}
        self.counters = {pair: {'up': 0, 'down': 0, 'ticks': 0} for pair in CB_PAIRS}
        self.signal_counts = {pair: {'buy_signals': 0, 'sell_signals': 0} for pair in CB_PAIRS}
        self._load_state()
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

    def _handle_stop(self, *_):
        self.running = False
        log.info('[CB] stop requested')

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
        private_json = self.secrets.get('COINBASE_PRIVATE_KEY_JSON')
        if not api_key or not private_json:
            raise RuntimeError('Missing Coinbase credentials in secrets.env')
        private_key = json.loads(private_json)
        return RESTClient(api_key=api_key, api_secret=private_key)

    def _load_state(self):
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text())
            for pair, prices in data.get('price_history', {}).items():
                self.price_history[pair] = deque(prices, maxlen=CB_MA_WINDOW)
            for pair, pos in data.get('positions', {}).items():
                self.positions[pair] = Position(**pos)
            self.counters.update(data.get('counters', {}))
        except Exception as e:
            log.warning('[CB] state load failed: %s', e)

    def _save_state(self):
        data = {
            'price_history': {k: list(v) for k, v in self.price_history.items()},
            'positions': {k: asdict(v) for k, v in self.positions.items()},
            'counters': self.counters,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        STATE_PATH.write_text(json.dumps(data, indent=2))

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
                'coinbase-spot',
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
            log.warning('[CB] get_available_usd error: %s', e)
        return 0.0

    def market_buy(self, pair: str, usd_budget: float):
        """Execute a market buy order. Returns (price, amount)."""
        product_price = self.get_product_price(pair)
        base_amount = max(0.0, usd_budget / product_price)
        if not CB_DRY_RUN:
            try:
                # Use small tolerance for market order
                tolerance = 0.01  # 1% tolerance for market order
                self.client.market_buy(
                    product_id=pair,
                    quote_size=str(usd_budget),
                    funds=str(usd_budget),
                )
                log.info('[CB-BUY] Market buy submitted for $%.2f of %s @ ~$%.2f', usd_budget, pair, product_price)
                tg(f'[CB] BUY {pair} $usd_budget:.2f @ ${product_price:.2f}')
            except Exception as e:
                log.warning('[CB-BUY] Submit error: %s', e)
        return product_price, base_amount

    def market_sell(self, pair: str, amount: float):
        """Execute a market sell order. Returns (price, amount)."""
        product_price = self.get_product_price(pair)
        if not CB_DRY_RUN:
            try:
                self.client.market_sell(
                    product_id=pair,
                    size=str(amount),
                )
                log.info('[CB-SELL] Market sell submitted for %.8f %s @ ~$%.2f', amount, pair, product_price)
                tg(f'[CB] SELL {pair} {amount:.4f} @ ${product_price:.2f}')
            except Exception as e:
                log.warning('[CB-SELL] Submit error: %s', e)
        return product_price, amount

    def trend_metrics(self, pair: str):
        prices = list(self.price_history[pair])
        if not prices:
            return None
        ma = sum(prices) / len(prices)
        five_min_ago = prices[-10] if len(prices) >= 10 else prices[0]
        current = prices[-1]
        change_5m = ((current - five_min_ago) / five_min_ago * 100) if five_min_ago else 0.0
        cnt = self.counters[pair]
        trend = 'UP' if cnt['up'] >= CB_CONSECUTIVE_MIN else ('DOWN' if cnt['down'] >= CB_CONSECUTIVE_MIN else 'FLAT')
        return {
            'current': current,
            'ma': ma,
            'change_5m_pct': change_5m,
            'up_count': cnt['up'],
            'down_count': cnt['down'],
            'trend': trend,
        }

    def process_tick(self, pair: str):
        price = self.get_product_price(pair)
        prices = self.price_history[pair]
        prev = prices[-1] if prices else None
        prices.append(price)
        ctr = self.counters[pair]
        ctr['ticks'] += 1
        if prev is not None:
            if price > prev:
                ctr['up'] += 1
                ctr['down'] = 0
            elif price < prev:
                ctr['down'] += 1
                ctr['up'] = 0
        metrics = self.trend_metrics(pair)
        if not metrics:
            return
        log.info('[CB] %s price=%.2f ma=%.2f trend=%s up=%s down=%s chg5m=%+.3f%% pos=%s dry=%s',
                 pair, metrics['current'], metrics['ma'], metrics['trend'], metrics['up_count'], metrics['down_count'],
                 metrics['change_5m_pct'], 'YES' if pair in self.positions else 'NO', CB_DRY_RUN)
        self.check_exit(pair, metrics)
        self.check_entry(pair, metrics)

    def check_entry(self, pair: str, m):
        if pair in self.positions:
            return
        if len(self.positions) >= CB_MAX_POSITIONS:
            return
        conds = [
            m['current'] > m['ma'],
            m['up_count'] >= CB_CONSECUTIVE_MIN,
            m['change_5m_pct'] >= CB_MOMENTUM_MIN,
        ]
        if not all(conds):
            return
        self.signal_counts[pair]['buy_signals'] += 1
        usd_available = self.get_available_usd()
        usd_budget = max(0.0, usd_available - CB_USD_BUFFER)
        log.info('[CB] %s signal funds check usd_available=%.2f usd_budget=%.2f buffer=%.2f', pair, usd_available, usd_budget, CB_USD_BUFFER)
        if usd_budget < 1.0:
            log.info('[CB] %s buy signal but usd_budget=%.2f < 1.00, skipping', pair, usd_budget)
            return

        # Telegram notification for entry signal
        tg(f'[CB] BUY SIGNAL: {pair} | chg5m={m["change_5m_pct"]:+.2f}% | up={m["up_count"]}')

        entry_price, amount = self.market_buy(pair, usd_budget)
        if CB_DRY_RUN:
            log.info('[CB-BUY][DRY] Bought %.8f %s at $%.2f | budget=$%.2f', amount, pair.split('-')[0], entry_price, usd_budget)
            trade_id = self._journal_open(pair, 'BUY', entry_price, amount, usd_budget, 'DRY RUN entry')
        else:
            log.info('[CB-BUY] Bought %.8f %s at $%.2f', amount, pair.split('-')[0], entry_price)
            trade_id = self._journal_open(pair, 'BUY', entry_price, amount, usd_budget, 'LIVE entry')
        self.positions[pair] = Position(
            asset=pair,
            entry_price=entry_price,
            amount=amount,
            entry_time=datetime.now(timezone.utc).isoformat(),
            high_watermark=entry_price,
            trade_id=trade_id,
            entry_value_usd=usd_budget,
        )
        self._save_state()

    def check_exit(self, pair: str, m):
        pos = self.positions.get(pair)
        if not pos:
            return
        pos.high_watermark = max(pos.high_watermark, m['current'])
        pnl_pct = ((m['current'] - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else 0.0
        held = datetime.now(timezone.utc) - datetime.fromisoformat(pos.entry_time)
        reason = None
        if pnl_pct >= CB_TAKE_PROFIT:
            reason = 'take_profit'
        elif pnl_pct <= CB_STOP_LOSS:
            reason = 'stop_loss'
        elif m['current'] < m['ma'] and m['down_count'] >= CB_CONSECUTIVE_MIN:
            reason = 'trend_break'
        elif held >= timedelta(hours=CB_TIME_EXIT_HOURS):
            reason = 'time_exit'
        if not reason:
            self._save_state()
            return
        self.signal_counts[pair]['sell_signals'] += 1
        exit_price, amount = self.market_sell(pair, pos.amount)
        pnl_abs = amount * (exit_price - pos.entry_price)
        if CB_DRY_RUN:
            log.info('[CB-SELL][DRY] Sold %.8f %s at $%.2f | P&L: $%.2f | Reason: %s', amount, pair.split('-')[0], exit_price, pnl_abs, reason)
        else:
            log.info('[CB-SELL] Sold %.8f %s at $%.2f | P&L: $%.2f | Reason: %s', amount, pair.split('-')[0], exit_price, pnl_abs, reason)
        # Telegram notification for exit
        pnl_sign = '+' if pnl_abs >= 0 else ''
        tg(f'[CB] SELL: {pair} | P&L: ${pnl_sign}{pnl_abs:.2f} ({pnl_pct:+.1f}%) | {reason}')
        if pos.trade_id:
            self._journal_close(
                pos.trade_id,
                exit_price=exit_price,
                pnl_abs=pnl_abs,
                pnl_pct=pnl_pct,
                exit_type=reason,
                hold_seconds=int(held.total_seconds()),
                notes=f'{"DRY" if CB_DRY_RUN else "LIVE"} exit',
            )
        del self.positions[pair]
        self._save_state()

    def run(self):
        PID_PATH.write_text(str(os.getpid()))
        log.info('[CB] Coinbase momentum bot starting | dry=%s poll=%ss pairs=%s', CB_DRY_RUN, CB_POLL_INTERVAL, ','.join(CB_PAIRS))
        startup_usd = self.get_available_usd()
        startup_budget = max(0.0, startup_usd - CB_USD_BUFFER)
        log.info('[CB] USD budget: $%.2f (usd_available=%.2f, buffer=%.2f)', startup_budget, startup_usd, CB_USD_BUFFER)
        if not CB_ENABLED:
            log.info('[CB] disabled via CB_ENABLED=false')
            return
        while self.running:
            try:
                for pair in CB_PAIRS:
                    self.process_tick(pair)
                self._save_state()
            except Exception as e:
                log.exception('[CB] loop error: %s', e)
            time.sleep(CB_POLL_INTERVAL)


if __name__ == '__main__':
    Bot().run()
