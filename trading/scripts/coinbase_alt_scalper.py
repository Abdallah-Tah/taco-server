#!/usr/bin/env python3
"""
coinbase_alt_scalper.py — Altcoin Momentum Dip-Reversal Scalper
================================================================
Strategy:
  - Tracks short-term price history (5-min rolling window)
  - Buys when price dips >= DIP_THRESHOLD% then starts recovering (reversal detected)
  - Sells at TAKE_PROFIT% or STOP_LOSS%
  - One position per coin at a time
  - Dry run by default. Set CB_SCALPER_DRY_RUN=false to go live.

Config via secrets.env:
  CB_SCALPER_DRY_RUN=true
  CB_SCALPER_PAIRS=SOL-USD,DOGE-USD
  CB_SCALPER_SIZE_USD=11.00
  CB_SCALPER_TAKE_PROFIT=3.0
  CB_SCALPER_STOP_LOSS=2.0
  CB_SCALPER_DIP_THRESHOLD=1.5
  CB_SCALPER_RECOVERY_PCT=0.3
  CB_SCALPER_POLL_SEC=15
  CB_SCALPER_COOLDOWN_SEC=300
  CB_SCALPER_MAX_DAILY_TRADES=10
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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORK_DIR = Path('/home/abdaltm86/.openclaw/workspace/trading')
SECRETS = Path('/home/abdaltm86/.config/openclaw/secrets.env')
CDP_KEY_FILE = Path('/home/abdaltm86/.config/openclaw/cdp_api_key.json')
JOURNAL_DB = WORK_DIR / 'journal.db'
LOG_PATH = Path('/tmp/coinbase_alt_scalper.log')
STATE_PATH = WORK_DIR / '.coinbase_alt_scalper_state.json'

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('alt_scalper')


# ── Config helpers ────────────────────────────────────────────────────────────
def _load_env():
    env = {}
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = _load_env()

def _bool(key, default):
    return os.environ.get(key, ENV.get(key, str(default))).lower() in ('true', '1', 'yes')

def _float(key, default):
    try:
        return float(os.environ.get(key, ENV.get(key, default)))
    except:
        return float(default)

def _int(key, default):
    try:
        return int(os.environ.get(key, ENV.get(key, default)))
    except:
        return int(default)

def _list(key, default):
    raw = os.environ.get(key, ENV.get(key, default))
    return [x.strip() for x in raw.split(',') if x.strip()]


# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN          = _bool('CB_SCALPER_DRY_RUN', True)
PAIRS            = _list('CB_SCALPER_PAIRS', 'SOL-USD,DOGE-USD')
SIZE_USD         = _float('CB_SCALPER_SIZE_USD', 11.00)
TAKE_PROFIT_PCT  = _float('CB_SCALPER_TAKE_PROFIT', 3.0)
STOP_LOSS_PCT    = _float('CB_SCALPER_STOP_LOSS', 2.0)
DIP_THRESHOLD    = _float('CB_SCALPER_DIP_THRESHOLD', 0.5)   # min dip % to consider (lowered from 1.5%)
RECOVERY_PCT     = _float('CB_SCALPER_RECOVERY_PCT', 0.15)   # min bounce from low to trigger buy (lowered from 0.3%)
POLL_SEC         = _int('CB_SCALPER_POLL_SEC', 15)
COOLDOWN_SEC     = _int('CB_SCALPER_COOLDOWN_SEC', 300)       # wait after a trade before next
MAX_DAILY_TRADES = _int('CB_SCALPER_MAX_DAILY_TRADES', 10)
PRICE_WINDOW_SEC = _int('CB_SCALPER_PRICE_WINDOW', 300)       # 5 min rolling window
MAX_POSITIONS    = _int('CB_SCALPER_MAX_POSITIONS', 1)         # max simultaneous positions per pair


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = ENV.get('TELEGRAM_TOKEN', '')
CHAT_ID = ENV.get('CHAT_ID', '')

def tg(msg):
    if DRY_RUN or not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        import requests
        requests.post(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': msg}, timeout=10,
        )
    except:
        pass


# ── Position tracking ─────────────────────────────────────────────────────────
@dataclass
class Position:
    pair: str
    entry_price: float
    amount: float
    size_usd: float
    entry_time: str
    trade_id: str
    take_profit: float   # price target
    stop_loss: float     # price floor


class Scalper:
    def __init__(self):
        self.running = True
        self.client = self._init_client()
        self.positions: dict[str, Position] = {}      # pair -> Position
        self.price_history: dict[str, deque] = {}     # pair -> deque of (ts, price)
        self.daily_trades = 0
        self.daily_reset = ''
        self.last_trade_time: dict[str, float] = {}   # pair -> timestamp
        self._load_state()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'running', False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'running', False))

    def _init_client(self):
        from coinbase.rest import RESTClient
        if CDP_KEY_FILE.exists():
            creds = json.loads(CDP_KEY_FILE.read_text())
            return RESTClient(api_key=creds['name'], api_secret=creds['privateKey'])
        api_key = ENV.get('COINBASE_API_KEY', '')
        api_secret = ENV.get('COINBASE_PRIVATE_KEY_JSON', '')
        if not api_key or not api_secret:
            raise RuntimeError('Missing Coinbase credentials')
        return RESTClient(api_key=api_key, api_secret=api_secret)

    # ── State persistence ─────────────────────────────────────────────────────
    def _save_state(self):
        data = {
            'positions': {k: {
                'pair': v.pair, 'entry_price': v.entry_price, 'amount': v.amount,
                'size_usd': v.size_usd, 'entry_time': v.entry_time, 'trade_id': v.trade_id,
                'take_profit': v.take_profit, 'stop_loss': v.stop_loss,
            } for k, v in self.positions.items()},
            'daily_trades': self.daily_trades,
            'daily_reset': self.daily_reset,
            'last_trade_time': self.last_trade_time,
        }
        STATE_PATH.write_text(json.dumps(data, indent=2))

    def _load_state(self):
        if not STATE_PATH.exists():
            return
        try:
            data = json.loads(STATE_PATH.read_text())
            for k, v in data.get('positions', {}).items():
                self.positions[k] = Position(**v)
            self.daily_trades = data.get('daily_trades', 0)
            self.daily_reset = data.get('daily_reset', '')
            self.last_trade_time = data.get('last_trade_time', {})
        except Exception as e:
            log.warning('[SCALPER] state load error: %s', e)

    # ── Price fetching ────────────────────────────────────────────────────────
    def get_price(self, pair: str) -> Optional[float]:
        try:
            p = self.client.get_product(pair)
            return float(p.get('price') if isinstance(p, dict) else getattr(p, 'price', 0))
        except Exception as e:
            log.warning('[SCALPER] price error %s: %s', pair, e)
            return None

    def get_usd_balance(self) -> float:
        try:
            resp = self.client.get_accounts(limit=50)
            accounts = resp.get('accounts', []) if isinstance(resp, dict) else (getattr(resp, 'accounts', []) or [])
            for acct in accounts:
                currency = acct.get('currency') if isinstance(acct, dict) else getattr(acct, 'currency', '')
                bal = acct.get('available_balance', {}) if isinstance(acct, dict) else getattr(acct, 'available_balance', {})
                val = bal.get('value') if isinstance(bal, dict) else getattr(bal, 'value', '0')
                if currency == 'USD':
                    return float(val or 0)
        except Exception as e:
            log.warning('[SCALPER] balance error: %s', e)
        return 0.0

    # ── Order execution ───────────────────────────────────────────────────────
    def market_buy(self, pair: str, usd_amount: float) -> dict:
        if DRY_RUN:
            price = self.get_price(pair)
            amount = usd_amount / price if price else 0
            log.info('[SCALPER-DRY] BUY %s $%.2f @ $%.6f = %.8f', pair, usd_amount, price, amount)
            return {'success': True, 'price': price, 'amount': amount, 'dry': True}
        try:
            order = self.client.market_order_buy(
                client_order_id=str(uuid.uuid4()),
                product_id=pair,
                quote_size=str(round(usd_amount, 2)),
            )
            log.info('[SCALPER] BUY order: %s', order)
            # Get fill price
            time.sleep(2)
            price = self.get_price(pair)
            amount = usd_amount / price if price else 0
            return {'success': True, 'price': price, 'amount': amount, 'order': order}
        except Exception as e:
            log.error('[SCALPER] BUY error %s: %s', pair, e)
            return {'success': False, 'error': str(e)}

    def market_sell(self, pair: str, amount: float) -> dict:
        if DRY_RUN:
            price = self.get_price(pair)
            log.info('[SCALPER-DRY] SELL %s %.8f @ $%.6f', pair, amount, price)
            return {'success': True, 'price': price, 'dry': True}
        try:
            # Get the base currency (e.g., SOL from SOL-USD)
            base = pair.split('-')[0]
            order = self.client.market_order_sell(
                client_order_id=str(uuid.uuid4()),
                product_id=pair,
                base_size=str(round(amount, 8)),
            )
            log.info('[SCALPER] SELL order: %s', order)
            time.sleep(2)
            price = self.get_price(pair)
            return {'success': True, 'price': price, 'order': order}
        except Exception as e:
            log.error('[SCALPER] SELL error %s: %s', pair, e)
            return {'success': False, 'error': str(e)}

    # ── Journal ───────────────────────────────────────────────────────────────
    def journal_open(self, pair, entry_price, amount, size_usd, notes=''):
        trade_id = str(uuid.uuid4())
        try:
            conn = sqlite3.connect(str(JOURNAL_DB))
            conn.execute("""
                INSERT INTO trades (engine, timestamp_open, asset, category, direction,
                    entry_price, position_size, position_size_usd, regime, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ('coinbase_scalper', datetime.now(timezone.utc).isoformat(),
                  pair, 'coinbase-scalp', 'BUY', entry_price, amount, size_usd, 'normal', notes))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning('[SCALPER] journal open error: %s', e)
        return trade_id

    def journal_close(self, trade_id, exit_price, pnl, pnl_pct, exit_type, hold_sec, notes=''):
        try:
            conn = sqlite3.connect(str(JOURNAL_DB))
            conn.execute("""
                UPDATE trades SET timestamp_close=?, exit_price=?, pnl_absolute=?,
                    pnl_percent=?, exit_type=?, hold_duration_seconds=?, notes=?
                WHERE id=?
            """, (datetime.now(timezone.utc).isoformat(), exit_price, pnl, pnl_pct,
                  exit_type, hold_sec, notes, trade_id))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning('[SCALPER] journal close error: %s', e)

    # ── Signal detection ──────────────────────────────────────────────────────
    def detect_dip_reversal(self, pair: str, current_price: float) -> bool:
        """Detect if price dipped >= DIP_THRESHOLD% and is now bouncing back."""
        history = self.price_history.get(pair, deque())
        if len(history) < 3:
            return False

        # Find the high and low in the rolling window
        prices = [p for _, p in history]
        window_high = max(prices)
        window_low = min(prices)

        if window_high == 0:
            return False

        # Calculate dip from window high to window low
        dip_pct = (window_high - window_low) / window_high * 100

        # Calculate recovery from window low to current price
        if window_low == 0:
            return False
        recovery_pct = (current_price - window_low) / window_low * 100

        # Signal: dipped enough AND recovering from the low
        if dip_pct >= DIP_THRESHOLD and recovery_pct >= RECOVERY_PCT and current_price > window_low:
            log.info('[SCALPER] DIP REVERSAL %s | high=%.6f low=%.6f now=%.6f | dip=%.2f%% recovery=%.2f%%',
                     pair, window_high, window_low, current_price, dip_pct, recovery_pct)
            return True
        return False

    # ── Main loop per pair ────────────────────────────────────────────────────
    def process_pair(self, pair: str, current_price: float):
        now = time.time()

        # Record price history
        if pair not in self.price_history:
            self.price_history[pair] = deque()
        self.price_history[pair].append((now, current_price))
        # Trim to window
        cutoff = now - PRICE_WINDOW_SEC
        while self.price_history[pair] and self.price_history[pair][0][0] < cutoff:
            self.price_history[pair].popleft()

        # ── Manage open position ──
        if pair in self.positions:
            pos = self.positions[pair]
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100

            # Take profit
            if current_price >= pos.take_profit:
                pnl = pos.amount * (current_price - pos.entry_price)
                fees = pos.size_usd * 0.012  # ~1.2% round trip
                net_pnl = pnl - fees
                r = self.market_sell(pair, pos.amount)
                if r.get('success'):
                    hold_sec = int((datetime.now(timezone.utc) - datetime.fromisoformat(pos.entry_time)).total_seconds())
                    self.journal_close(pos.trade_id, current_price, net_pnl, pnl_pct, 'take_profit', hold_sec,
                                       f'TP hit gross={pnl:.4f} fees={fees:.4f} net={net_pnl:.4f}')
                    log.info('[SCALPER] TAKE PROFIT %s | entry=%.6f exit=%.6f | pnl=$%.4f (net $%.4f)',
                             pair, pos.entry_price, current_price, pnl, net_pnl)
                    tg(f'[SCALPER] TP {pair} +${net_pnl:.2f} ({pnl_pct:+.1f}%)')
                    del self.positions[pair]
                    self.last_trade_time[pair] = now
                    self.daily_trades += 1
                    self._save_state()
                return

            # Stop loss
            if current_price <= pos.stop_loss:
                pnl = pos.amount * (current_price - pos.entry_price)
                fees = pos.size_usd * 0.012
                net_pnl = pnl - fees
                r = self.market_sell(pair, pos.amount)
                if r.get('success'):
                    hold_sec = int((datetime.now(timezone.utc) - datetime.fromisoformat(pos.entry_time)).total_seconds())
                    self.journal_close(pos.trade_id, current_price, net_pnl, pnl_pct, 'stop_loss', hold_sec,
                                       f'SL hit gross={pnl:.4f} fees={fees:.4f} net={net_pnl:.4f}')
                    log.info('[SCALPER] STOP LOSS %s | entry=%.6f exit=%.6f | pnl=$%.4f (net $%.4f)',
                             pair, pos.entry_price, current_price, pnl, net_pnl)
                    tg(f'[SCALPER] SL {pair} ${net_pnl:.2f} ({pnl_pct:+.1f}%)')
                    del self.positions[pair]
                    self.last_trade_time[pair] = now
                    self.daily_trades += 1
                    self._save_state()
                return

            # Log holding status
            log.info('[SCALPER] HOLDING %s | entry=%.6f now=%.6f | pnl=%.2f%% | TP=%.6f SL=%.6f',
                     pair, pos.entry_price, current_price, pnl_pct, pos.take_profit, pos.stop_loss)
            return

        # ── Look for entry ──
        # Cooldown check
        if now - self.last_trade_time.get(pair, 0) < COOLDOWN_SEC:
            remaining = int(COOLDOWN_SEC - (now - self.last_trade_time.get(pair, 0)))
            log.info('[SCALPER] %s cooldown %ds remaining', pair, remaining)
            return

        # Daily limit
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if self.daily_reset != today:
            self.daily_trades = 0
            self.daily_reset = today
        if self.daily_trades >= MAX_DAILY_TRADES:
            log.info('[SCALPER] daily trade limit reached (%d/%d)', self.daily_trades, MAX_DAILY_TRADES)
            return

        # Check USD balance
        # (Only check occasionally to avoid rate limits)

        # Detect signal
        if self.detect_dip_reversal(pair, current_price):
            # Confirm we have enough USD
            usd = self.get_usd_balance()
            if usd < SIZE_USD + 0.50:
                log.info('[SCALPER] insufficient USD: $%.2f < $%.2f needed', usd, SIZE_USD)
                return

            # Execute buy
            r = self.market_buy(pair, SIZE_USD)
            if r.get('success'):
                entry_price = r.get('price', current_price)
                amount = r.get('amount', SIZE_USD / current_price)
                tp_price = round(entry_price * (1 + TAKE_PROFIT_PCT / 100), 6)
                sl_price = round(entry_price * (1 - STOP_LOSS_PCT / 100), 6)
                trade_id = self.journal_open(pair, entry_price, amount, SIZE_USD,
                                              f'dip_reversal dry={DRY_RUN}')
                self.positions[pair] = Position(
                    pair=pair, entry_price=entry_price, amount=amount,
                    size_usd=SIZE_USD, entry_time=datetime.now(timezone.utc).isoformat(),
                    trade_id=trade_id, take_profit=tp_price, stop_loss=sl_price,
                )
                log.info('[SCALPER] ENTRY %s | price=%.6f | amount=%.8f | TP=%.6f (+%.1f%%) SL=%.6f (-%.1f%%)',
                         pair, entry_price, amount, tp_price, TAKE_PROFIT_PCT, sl_price, STOP_LOSS_PCT)
                tg(f'[SCALPER] BUY {pair} ${SIZE_USD:.0f} @ ${entry_price:.4f} | TP ${tp_price:.4f} SL ${sl_price:.4f}')
                self._save_state()
        else:
            prices = self.price_history.get(pair, deque())
            if prices:
                high = max(p for _, p in prices)
                low = min(p for _, p in prices)
                dip = (high - low) / high * 100 if high else 0
                log.info('[SCALPER] %s price=%.6f | 5m high=%.6f low=%.6f dip=%.2f%% (need %.1f%%)',
                         pair, current_price, high, low, dip, DIP_THRESHOLD)

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self):
        log.info('=' * 60)
        log.info('[SCALPER] Starting | dry=%s pairs=%s size=$%.2f TP=%.1f%% SL=%.1f%% dip=%.1f%% recovery=%.1f%%',
                 DRY_RUN, ','.join(PAIRS), SIZE_USD, TAKE_PROFIT_PCT, STOP_LOSS_PCT, DIP_THRESHOLD, RECOVERY_PCT)
        log.info('[SCALPER] cooldown=%ds max_daily=%d poll=%ds window=%ds',
                 COOLDOWN_SEC, MAX_DAILY_TRADES, POLL_SEC, PRICE_WINDOW_SEC)

        while self.running:
            try:
                for pair in PAIRS:
                    price = self.get_price(pair)
                    if price:
                        self.process_pair(pair, price)
            except Exception as e:
                log.exception('[SCALPER] loop error: %s', e)
            time.sleep(POLL_SEC)

        log.info('[SCALPER] Stopped')
        self._save_state()


if __name__ == '__main__':
    Scalper().run()
