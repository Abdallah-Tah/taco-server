#!/usr/bin/env python3
"""
kalshi_btc_range.py — Kalshi BTC Daily Price Range Trader
==========================================================
Strategy:
  - Monitors BTC price and Kalshi daily price range brackets
  - Early in the day: buy the bracket containing current BTC price at low cost
  - Take profit when bracket price rises (BTC stays in range)
  - Stop loss if BTC moves out of bracket
  - One trade per day max

Settles at 5PM ET daily. Best entries are morning (9-11AM ET).

Config via secrets.env:
  KALSHI_BOT_DRY_RUN=true
  KALSHI_BOT_SIZE_USD=5.00        (max spend per trade)
  KALSHI_BOT_TAKE_PROFIT=0.35     (sell when bracket price hits this)
  KALSHI_BOT_STOP_LOSS=0.03       (sell when bracket price drops below this)
  KALSHI_BOT_ENTRY_MAX=0.15       (don't buy if bracket already above this)
  KALSHI_BOT_POLL_SEC=60          (check every 60s)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

WORK_DIR = Path('/home/abdaltm86/.openclaw/workspace/trading')
SECRETS = Path('/home/abdaltm86/.config/openclaw/secrets.env')
KALSHI_KEY_FILE = Path('/home/abdaltm86/.config/openclaw/keys/kalshi_private_key.pem')
JOURNAL_DB = WORK_DIR / 'journal.db'
LOG_PATH = Path('/tmp/kalshi_btc_range.log')
STATE_PATH = WORK_DIR / '.kalshi_btc_range_state.json'

API = 'https://api.elections.kalshi.com/trade-api/v2'

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('kalshi_btc')


# ── Config ────────────────────────────────────────────────────────────────────
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

def _bool(k, d): return os.environ.get(k, ENV.get(k, str(d))).lower() in ('true', '1', 'yes')
def _float(k, d):
    try: return float(os.environ.get(k, ENV.get(k, d)))
    except: return float(d)
def _int(k, d):
    try: return int(os.environ.get(k, ENV.get(k, d)))
    except: return int(d)

DRY_RUN       = _bool('KALSHI_BOT_DRY_RUN', True)
SIZE_USD      = _float('KALSHI_BOT_SIZE_USD', 5.00)
TAKE_PROFIT   = _float('KALSHI_BOT_TAKE_PROFIT', 0.35)
STOP_LOSS     = _float('KALSHI_BOT_STOP_LOSS', 0.03)
ENTRY_MAX     = _float('KALSHI_BOT_ENTRY_MAX', 0.15)
POLL_SEC      = _int('KALSHI_BOT_POLL_SEC', 60)

API_KEY = ENV.get('KALSHI_API_KEY', '')
TELEGRAM_TOKEN = ENV.get('TELEGRAM_TOKEN', '')
CHAT_ID = ENV.get('CHAT_ID', '')


# ── Telegram ──────────────────────────────────────────────────────────────────
def tg(msg):
    if DRY_RUN or not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        import subprocess
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', str(CHAT_ID),
            '--message', str(msg),
        ], capture_output=True, text=True, timeout=15)
    except:
        pass


# ── Kalshi API ────────────────────────────────────────────────────────────────
class KalshiAPI:
    def __init__(self):
        with open(KALSHI_KEY_FILE, 'rb') as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, method, path):
        ts = str(int(time.time() * 1000))
        msg = ts + method + path
        sig = base64.b64encode(self.private_key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )).decode()
        return {
            'KALSHI-ACCESS-KEY': API_KEY,
            'KALSHI-ACCESS-SIGNATURE': sig,
            'KALSHI-ACCESS-TIMESTAMP': ts,
            'Content-Type': 'application/json',
        }

    def get(self, path, params=None):
        headers = self._sign('GET', path)
        r = requests.get(API + path, headers=headers, params=params, timeout=15)
        return r

    def post(self, path, data=None):
        headers = self._sign('POST', path)
        r = requests.post(API + path, headers=headers, json=data, timeout=15)
        return r

    def get_balance(self):
        r = self.get('/portfolio/balance')
        if r.status_code == 200:
            return r.json().get('balance', 0) / 100  # cents to dollars
        return 0

    def get_btc_today_markets(self):
        """Get today's BTC price range markets."""
        r = self.get('/events', params={
            'limit': 3,
            'status': 'open',
            'series_ticker': 'KXBTC',
            'with_nested_markets': 'true',
        })
        if r.status_code != 200:
            return []

        events = r.json().get('events', [])
        # Find today's event
        today = datetime.now(timezone(timedelta(hours=-4))).strftime('%b %-d')  # ET
        for e in events:
            title = e.get('title', '')
            if today in title or 'Apr 2' in title:  # fallback
                return [m for m in e.get('markets', []) if m.get('status') == 'active']

        # If no exact match, return closest event
        if events:
            return [m for m in events[0].get('markets', []) if m.get('status') == 'active']
        return []

    def place_order(self, ticker, side, count, price_cents):
        """Place a limit order. price_cents is in cents (e.g., 10 = $0.10)."""
        if DRY_RUN:
            log.info(f'[KALSHI-DRY] ORDER {side} {count}x {ticker} @ ${price_cents/100:.2f}')
            return {'success': True, 'dry': True, 'order_id': f'dry-{int(time.time())}'}

        data = {
            'ticker': ticker,
            'client_order_id': str(uuid.uuid4()),
            'type': 'limit',
            'action': 'buy' if side == 'YES' else 'buy',
            'side': 'yes' if side == 'YES' else 'no',
            'count': count,
            'yes_price': price_cents if side == 'YES' else None,
            'no_price': price_cents if side == 'NO' else None,
        }
        # Remove None values
        data = {k: v for k, v in data.items() if v is not None}

        r = self.post('/portfolio/orders', data=data)
        log.info(f'[KALSHI] ORDER response: {r.status_code} {r.text[:200]}')
        if r.status_code in (200, 201):
            return {'success': True, 'order': r.json()}
        return {'success': False, 'error': r.text[:200]}

    def get_positions(self):
        r = self.get('/portfolio/positions')
        if r.status_code == 200:
            return r.json().get('market_positions', [])
        return []

    def sell_position(self, ticker, count, price_cents, side='yes'):
        """Sell an existing position."""
        if DRY_RUN:
            log.info(f'[KALSHI-DRY] SELL {count}x {ticker} @ ${price_cents/100:.2f}')
            return {'success': True, 'dry': True}

        data = {
            'ticker': ticker,
            'client_order_id': str(uuid.uuid4()),
            'type': 'limit',
            'action': 'sell',
            'side': side,
            'count': count,
            'yes_price': price_cents if side == 'yes' else None,
            'no_price': price_cents if side == 'no' else None,
        }
        data = {k: v for k, v in data.items() if v is not None}

        r = self.post('/portfolio/orders', data=data)
        log.info(f'[KALSHI] SELL response: {r.status_code} {r.text[:200]}')
        if r.status_code in (200, 201):
            return {'success': True, 'order': r.json()}
        return {'success': False, 'error': r.text[:200]}


# ── BTC Price ─────────────────────────────────────────────────────────────────
def get_btc_price():
    try:
        r = requests.get('https://api.exchange.coinbase.com/products/BTC-USD/ticker', timeout=5)
        return float(r.json()['price'])
    except:
        return None


# ── State ─────────────────────────────────────────────────────────────────────
def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2))

def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except:
            pass
    return {
        'position_ticker': '',
        'position_side': '',
        'position_count': 0,
        'entry_price': 0,
        'entry_time': '',
        'bracket_low': 0,
        'bracket_high': 0,
        'daily_traded': '',
        'daily_pnl': 0,
    }


# ── Journal ───────────────────────────────────────────────────────────────────
def journal_trade(action, ticker, price, count, pnl=0, notes=''):
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.execute("""
            INSERT INTO trades (engine, timestamp_open, asset, category, direction,
                entry_price, position_size, position_size_usd, pnl_absolute, exit_type, regime, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ('kalshi_btc', datetime.now(timezone.utc).isoformat(),
              ticker, 'kalshi-range', action, price, count, count * price,
              pnl, 'open' if action == 'BUY' else 'closed', 'normal', notes))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f'[KALSHI] journal error: {e}')


# ── Main Bot ──────────────────────────────────────────────────────────────────
class KalshiBTCBot:
    def __init__(self):
        self.running = True
        self.api = KalshiAPI()
        self.state = load_state()
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'running', False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'running', False))

    def find_current_bracket(self, markets, btc_price):
        """Find the market bracket that contains the current BTC price."""
        for m in markets:
            sub = m.get('yes_sub_title', '')
            ticker = m.get('ticker', '')
            try:
                if 'to' in sub:
                    parts = sub.replace('$', '').replace(',', '').split(' to ')
                    low = float(parts[0])
                    high = float(parts[1].replace('.99', '').strip())
                    if low <= btc_price <= high + 0.99:
                        return {
                            'ticker': ticker,
                            'subtitle': sub,
                            'low': low,
                            'high': high + 0.99,
                            'yes_bid': float(m.get('yes_bid_dollars', 0)),
                            'yes_ask': float(m.get('yes_ask_dollars', 0)),
                            'no_bid': float(m.get('no_bid_dollars', 0)),
                            'no_ask': float(m.get('no_ask_dollars', 0)),
                            'volume': float(m.get('volume_fp', 0)),
                        }
            except:
                continue
        return None

    def process(self):
        btc_price = get_btc_price()
        if not btc_price:
            log.warning('[KALSHI] Could not get BTC price')
            return

        today = datetime.now(timezone(timedelta(hours=-4))).strftime('%Y-%m-%d')

        # Check if already traded today
        if self.state.get('daily_traded') == today and self.state.get('position_ticker'):
            # Managing existing position
            self._manage_position(btc_price)
            return

        if self.state.get('daily_traded') == today and not self.state.get('position_ticker'):
            log.info(f'[KALSHI] Already traded today, no position. PnL: ${self.state.get("daily_pnl", 0):.2f}')
            return

        # Look for entry
        markets = self.api.get_btc_today_markets()
        if not markets:
            log.info(f'[KALSHI] No BTC markets open yet | BTC=${btc_price:,.2f}')
            return

        bracket = self.find_current_bracket(markets, btc_price)
        if not bracket:
            log.info(f'[KALSHI] No bracket found for BTC=${btc_price:,.2f}')
            return

        ask = bracket['yes_ask']
        bid = bracket['yes_bid']

        log.info(f'[KALSHI] BTC=${btc_price:,.2f} | Bracket: {bracket["subtitle"]} | bid=${bid:.2f} ask=${ask:.2f} | vol={bracket["volume"]:.0f}')

        # Entry conditions
        if ask <= 0:
            log.info(f'[KALSHI] No ask available')
            return

        if ask > ENTRY_MAX:
            log.info(f'[KALSHI] Ask ${ask:.2f} > max entry ${ENTRY_MAX:.2f}, waiting for cheaper')
            return

        # Check time — best entries before 2PM ET
        et_hour = datetime.now(timezone(timedelta(hours=-4))).hour
        if et_hour >= 15:  # After 3PM ET, too close to settlement
            log.info(f'[KALSHI] Too late in day ({et_hour}:00 ET), skipping entry')
            return

        # Calculate position size
        count = max(1, int(SIZE_USD / ask))
        cost = count * ask

        if not DRY_RUN:
            balance = self.api.get_balance()
            if balance < cost + 0.50:
                log.info(f'[KALSHI] Insufficient balance: ${balance:.2f} < ${cost:.2f} needed')
                return

        # Place order
        price_cents = int(ask * 100)
        log.info(f'[KALSHI] ENTRY: BUY {count}x YES {bracket["ticker"]} @ ${ask:.2f} (cost=${cost:.2f})')
        r = self.api.place_order(bracket['ticker'], 'YES', count, price_cents)

        if r.get('success'):
            self.state['position_ticker'] = bracket['ticker']
            self.state['position_side'] = 'yes'
            self.state['position_count'] = count
            self.state['entry_price'] = ask
            self.state['entry_time'] = datetime.now(timezone.utc).isoformat()
            self.state['bracket_low'] = bracket['low']
            self.state['bracket_high'] = bracket['high']
            self.state['daily_traded'] = today
            save_state(self.state)

            journal_trade('BUY', bracket['ticker'], ask, count,
                         notes=f'bracket={bracket["subtitle"]} btc={btc_price:.2f} dry={DRY_RUN}')
            tg(f'[KALSHI] BUY {count}x {bracket["subtitle"]} @ ${ask:.2f} | BTC=${btc_price:,.2f} | cost=${cost:.2f}')
            log.info(f'[KALSHI] ORDER PLACED | TP=${TAKE_PROFIT:.2f} SL=${STOP_LOSS:.2f}')

    def _manage_position(self, btc_price):
        """Monitor and manage existing position."""
        ticker = self.state['position_ticker']
        entry = self.state['entry_price']
        count = self.state['position_count']
        bracket_low = self.state['bracket_low']
        bracket_high = self.state['bracket_high']

        # Get current market price for our bracket
        markets = self.api.get_btc_today_markets()
        current_price = None
        for m in markets:
            if m.get('ticker') == ticker:
                current_price = float(m.get('yes_bid_dollars', 0))
                current_ask = float(m.get('yes_ask_dollars', 0))
                break

        if current_price is None:
            log.info(f'[KALSHI] Position {ticker} — market data unavailable')
            return

        in_bracket = bracket_low <= btc_price <= bracket_high
        pnl = (current_price - entry) * count
        pnl_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0

        log.info(f'[KALSHI] POSITION {ticker} | entry=${entry:.2f} now=${current_price:.2f} ({pnl_pct:+.1f}%) | '
                 f'BTC=${btc_price:,.2f} {"IN" if in_bracket else "OUT of"} bracket ${bracket_low:,.0f}-{bracket_high:,.0f} | '
                 f'pnl=${pnl:.2f}')

        # Take profit
        if current_price >= TAKE_PROFIT:
            log.info(f'[KALSHI] TAKE PROFIT: ${current_price:.2f} >= ${TAKE_PROFIT:.2f}')
            price_cents = int(current_price * 100)
            r = self.api.sell_position(ticker, count, price_cents)
            if r.get('success'):
                net_pnl = (current_price - entry) * count
                self.state['daily_pnl'] = net_pnl
                self.state['position_ticker'] = ''
                self.state['position_count'] = 0
                save_state(self.state)
                journal_trade('SELL_TP', ticker, current_price, count, pnl=net_pnl,
                             notes=f'take_profit entry={entry:.2f} exit={current_price:.2f}')
                tg(f'[KALSHI] TAKE PROFIT +${net_pnl:.2f} | {ticker} sold @ ${current_price:.2f}')
            return

        # Stop loss — bracket price dropped
        if current_price <= STOP_LOSS and current_price > 0:
            log.info(f'[KALSHI] STOP LOSS: ${current_price:.2f} <= ${STOP_LOSS:.2f}')
            price_cents = int(current_price * 100)
            r = self.api.sell_position(ticker, count, price_cents)
            if r.get('success'):
                net_pnl = (current_price - entry) * count
                self.state['daily_pnl'] = net_pnl
                self.state['position_ticker'] = ''
                self.state['position_count'] = 0
                save_state(self.state)
                journal_trade('SELL_SL', ticker, current_price, count, pnl=net_pnl,
                             notes=f'stop_loss entry={entry:.2f} exit={current_price:.2f}')
                tg(f'[KALSHI] STOP LOSS -${abs(net_pnl):.2f} | {ticker} sold @ ${current_price:.2f}')
            return

        # Check if close to settlement (after 4:30PM ET) — let it ride
        et_hour = datetime.now(timezone(timedelta(hours=-4))).hour
        et_min = datetime.now(timezone(timedelta(hours=-4))).minute
        if et_hour == 16 and et_min >= 30:
            log.info(f'[KALSHI] Near settlement — holding to expiry')

    def run(self):
        log.info('=' * 60)
        log.info(f'[KALSHI] BTC Range Bot Starting | dry={DRY_RUN} size=${SIZE_USD:.2f}')
        log.info(f'[KALSHI] TP=${TAKE_PROFIT:.2f} SL=${STOP_LOSS:.2f} entry_max=${ENTRY_MAX:.2f} poll={POLL_SEC}s')
        log.info(f'[KALSHI] Balance: ${self.api.get_balance():.2f}')

        while self.running:
            try:
                self.process()
            except Exception as e:
                log.exception(f'[KALSHI] loop error: {e}')
            time.sleep(POLL_SEC)

        log.info('[KALSHI] Stopped')
        save_state(self.state)


if __name__ == '__main__':
    KalshiBTCBot().run()
