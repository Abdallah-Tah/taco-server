#!/usr/bin/env python3
"""
coinbase_momentum_scalper.py — Smart Momentum Scalper with Structure
=====================================================================
Strategy (addresses all weaknesses in naive momentum approach):

  SCAN (every 5 min):
    - Pull top movers from Coinbase (24h volume + recent % change)
    - Filter: spread < 0.5%, 5min volume > $500k, price > $0.001
    - Require: price > 20 EMA (trend alignment)
    - Require: breakout of recent high (last 20 candles), not just % change

  ENTRY:
    - On breakout with volume confirmation
    - Position size = base_size * (volume_score / volatility_score)

  EXIT (trailing stop, ATR-based):
    - Initial SL: 1.5× ATR below entry
    - At +1% → move stop to breakeven (risk compression)
    - Trail: max(1%, 0.5 × ATR%) below high watermark
    - Time kill: no +1% within 10 min → exit at market

  FILTERS:
    - Max 2 positions at once
    - Cooldown: 1 hour per coin after exit
    - Skip stablecoins, wrapped tokens
    - Market regime: if BTC flat + low total volume → reduce trades
    - Daily trade limit

Dry run by default. Set CB_MSCALP_DRY_RUN=false to go live.
"""
from __future__ import annotations

import json
import logging
import math
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

import requests

WORK_DIR = Path('/home/abdaltm86/.openclaw/workspace/trading')
SECRETS = Path('/home/abdaltm86/.config/openclaw/secrets.env')
CDP_KEY_FILE = Path('/home/abdaltm86/.config/openclaw/cdp_api_key.json')
JOURNAL_DB = WORK_DIR / 'journal.db'
LOG_PATH = Path('/tmp/coinbase_momentum_scalper.log')
STATE_PATH = WORK_DIR / '.coinbase_momentum_scalper_state.json'

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('momentum_scalper')


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

DRY_RUN            = _bool('CB_MSCALP_DRY_RUN', True)
BASE_SIZE_USD      = _float('CB_MSCALP_BASE_SIZE', 11.00)
MAX_POSITIONS      = _int('CB_MSCALP_MAX_POSITIONS', 2)
MAX_DAILY_TRADES   = _int('CB_MSCALP_MAX_DAILY', 10)
COOLDOWN_SEC       = _int('CB_MSCALP_COOLDOWN', 3600)        # 1 hour per coin
POLL_SEC           = _int('CB_MSCALP_POLL_SEC', 15)
SCAN_INTERVAL_SEC  = _int('CB_MSCALP_SCAN_INTERVAL', 300)    # scan every 5 min
TIME_KILL_SEC      = _int('CB_MSCALP_TIME_KILL', 600)        # 10 min no-profit → exit
BREAKEVEN_PCT      = _float('CB_MSCALP_BREAKEVEN_PCT', 1.0)  # move stop to BE at +1%
INITIAL_SL_ATR_MULT = _float('CB_MSCALP_SL_ATR_MULT', 1.5)  # initial SL = 1.5× ATR
TRAIL_ATR_MULT     = _float('CB_MSCALP_TRAIL_ATR_MULT', 0.5) # trail = max(1%, 0.5×ATR%)
TRAIL_MIN_PCT      = _float('CB_MSCALP_TRAIL_MIN_PCT', 1.0)  # minimum trail %
MIN_5M_VOLUME_USD  = _float('CB_MSCALP_MIN_VOLUME', 500000)  # $500k 5min volume
MAX_SPREAD_PCT     = _float('CB_MSCALP_MAX_SPREAD', 0.5)     # max spread 0.5%
MIN_PRICE          = _float('CB_MSCALP_MIN_PRICE', 0.001)    # skip dust coins
EMA_PERIOD         = _int('CB_MSCALP_EMA_PERIOD', 20)        # 20-candle EMA for trend
CANDLE_LOOKBACK    = _int('CB_MSCALP_CANDLE_LOOKBACK', 20)   # breakout of last N candles

# Skip stablecoins, wrapped tokens, and illiquid pairs
SKIP_PATTERNS = {'USDT', 'USDC', 'DAI', 'BUSD', 'TUSD', 'WBTC', 'WETH', 'STETH',
                 'CBETH', 'PYUSD', 'GUSD', 'PAX', 'USDP', 'FRAX', 'EURC', 'GBP', 'EUR'}

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


# ── Position tracking ─────────────────────────────────────────────────────────
@dataclass
class Position:
    pair: str
    entry_price: float
    amount: float
    size_usd: float
    entry_time: float         # unix timestamp
    trade_id: str
    initial_sl: float         # initial stop loss price
    current_sl: float         # current trailing stop price
    high_watermark: float     # highest price since entry
    atr_pct: float            # ATR as % of price at entry
    breakeven_hit: bool       # has +1% been reached?

    def to_dict(self):
        return {
            'pair': self.pair, 'entry_price': self.entry_price, 'amount': self.amount,
            'size_usd': self.size_usd, 'entry_time': self.entry_time, 'trade_id': self.trade_id,
            'initial_sl': self.initial_sl, 'current_sl': self.current_sl,
            'high_watermark': self.high_watermark, 'atr_pct': self.atr_pct,
            'breakeven_hit': self.breakeven_hit,
        }


# ── Scanner Result ────────────────────────────────────────────────────────────
@dataclass
class ScanResult:
    pair: str
    price: float
    change_15m_pct: float
    volume_5m_usd: float
    spread_pct: float
    atr_pct: float
    ema_20: float
    recent_high: float
    volume_score: float      # normalized volume (higher = more active)
    volatility_score: float  # normalized ATR (higher = more volatile)


class MomentumScalper:
    def __init__(self):
        self.running = True
        self.client = self._init_client()
        self.positions: dict[str, Position] = {}
        self.daily_trades = 0
        self.daily_reset = ''
        self.last_trade_time: dict[str, float] = {}  # pair -> unix timestamp
        self.watchlist: list[str] = []                # dynamically updated
        self.last_scan_time = 0
        self.btc_baseline: Optional[float] = None     # for regime detection
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

    # ── State ─────────────────────────────────────────────────────────────────
    def _save_state(self):
        data = {
            'positions': {k: v.to_dict() for k, v in self.positions.items()},
            'daily_trades': self.daily_trades,
            'daily_reset': self.daily_reset,
            'last_trade_time': self.last_trade_time,
            'watchlist': self.watchlist,
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
            self.watchlist = data.get('watchlist', [])
        except Exception as e:
            log.warning('[MSCALP] state load error: %s', e)

    # ── Price / Market Data ───────────────────────────────────────────────────
    def get_price(self, pair: str) -> Optional[float]:
        try:
            p = self.client.get_product(pair)
            return float(p.get('price') if isinstance(p, dict) else getattr(p, 'price', 0))
        except:
            return None

    def get_product_book(self, pair: str) -> Optional[dict]:
        """Get order book for spread + liquidity check."""
        try:
            book = self.client.get_product_book(pair, level=1)
            if isinstance(book, dict):
                pricebook = book.get('pricebook', book)
            else:
                pricebook = getattr(book, 'pricebook', book)

            if isinstance(pricebook, dict):
                bids = pricebook.get('bids', [])
                asks = pricebook.get('asks', [])
            else:
                bids = getattr(pricebook, 'bids', [])
                asks = getattr(pricebook, 'asks', [])

            if not bids or not asks:
                return None

            def extract(entry):
                if isinstance(entry, dict):
                    return float(entry.get('price', 0)), float(entry.get('size', 0))
                return float(getattr(entry, 'price', 0)), float(getattr(entry, 'size', 0))

            best_bid_price, best_bid_size = extract(bids[0])
            best_ask_price, best_ask_size = extract(asks[0])

            if best_bid_price <= 0 or best_ask_price <= 0:
                return None

            spread_pct = (best_ask_price - best_bid_price) / best_bid_price * 100
            mid = (best_bid_price + best_ask_price) / 2

            return {
                'bid': best_bid_price,
                'ask': best_ask_price,
                'bid_size': best_bid_size,
                'ask_size': best_ask_size,
                'spread_pct': spread_pct,
                'mid': mid,
            }
        except:
            return None

    def get_candles(self, pair: str, granularity: str = 'FIVE_MINUTE', limit: int = 30) -> list:
        """Get recent candles for EMA, ATR, breakout detection."""
        try:
            now = int(time.time())
            start = now - (limit * 300 + 300)  # 5 min candles
            resp = self.client.get_candles(
                pair, start=str(start), end=str(now), granularity=granularity
            )
            candles = resp.get('candles', []) if isinstance(resp, dict) else getattr(resp, 'candles', [])
            result = []
            for c in candles:
                if isinstance(c, dict):
                    result.append({
                        'ts': int(c.get('start', 0)),
                        'open': float(c.get('open', 0)),
                        'high': float(c.get('high', 0)),
                        'low': float(c.get('low', 0)),
                        'close': float(c.get('close', 0)),
                        'volume': float(c.get('volume', 0)),
                    })
                else:
                    result.append({
                        'ts': int(getattr(c, 'start', 0)),
                        'open': float(getattr(c, 'open', 0)),
                        'high': float(getattr(c, 'high', 0)),
                        'low': float(getattr(c, 'low', 0)),
                        'close': float(getattr(c, 'close', 0)),
                        'volume': float(getattr(c, 'volume', 0)),
                    })
            # Sort oldest first
            result.sort(key=lambda x: x['ts'])
            return result
        except Exception as e:
            log.warning('[MSCALP] candles error %s: %s', pair, e)
            return []

    def compute_ema(self, closes: list[float], period: int) -> float:
        """Compute EMA from a list of close prices."""
        if len(closes) < period:
            return closes[-1] if closes else 0
        k = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for price in closes[period:]:
            ema = price * k + ema * (1 - k)
        return ema

    def compute_atr(self, candles: list[dict], period: int = 14) -> float:
        """Compute ATR (Average True Range) from candles."""
        if len(candles) < 2:
            return 0
        trs = []
        for i in range(1, len(candles)):
            h = candles[i]['high']
            l = candles[i]['low']
            pc = candles[i-1]['close']
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
        if not trs:
            return 0
        # Use last `period` TRs or all available
        recent = trs[-period:] if len(trs) >= period else trs
        return sum(recent) / len(recent)

    # ── Scanner ───────────────────────────────────────────────────────────────
    def scan_for_candidates(self) -> list[ScanResult]:
        """Scan Coinbase for momentum candidates with structure confirmation."""
        candidates = []

        try:
            # Get all USD trading pairs
            resp = self.client.get_products(product_type='SPOT')
            products = resp.get('products', []) if isinstance(resp, dict) else getattr(resp, 'products', [])
        except Exception as e:
            log.warning('[MSCALP] scan products error: %s', e)
            return []

        usd_pairs = []
        for p in products:
            pid = p.get('product_id', '') if isinstance(p, dict) else getattr(p, 'product_id', '')
            status = p.get('status', '') if isinstance(p, dict) else getattr(p, 'status', '')
            quote = p.get('quote_currency_id', '') if isinstance(p, dict) else getattr(p, 'quote_currency_id', '')
            base = p.get('base_currency_id', '') if isinstance(p, dict) else getattr(p, 'base_currency_id', '')
            price_raw = p.get('price', 0) if isinstance(p, dict) else getattr(p, 'price', 0)
            price = float(price_raw) if price_raw not in (None, '') else 0.0
            pct_24h_raw = p.get('price_percentage_change_24h', 0) if isinstance(p, dict) else getattr(p, 'price_percentage_change_24h', 0)
            pct_24h = float(pct_24h_raw) if pct_24h_raw not in (None, '') else 0.0
            vol_24h_raw = p.get('volume_24h', 0) if isinstance(p, dict) else getattr(p, 'volume_24h', 0)
            vol_24h = float(vol_24h_raw) if vol_24h_raw not in (None, '') else 0.0

            if quote != 'USD' or status != 'online':
                continue
            if base in SKIP_PATTERNS:
                continue
            if price < MIN_PRICE:
                continue
            # Pre-filter: only look at coins with some activity
            if vol_24h * price < 100000:  # < $100k daily volume → skip
                continue
            usd_pairs.append({
                'pair': pid, 'price': price, 'base': base,
                'pct_24h': pct_24h, 'vol_24h_usd': vol_24h * price,
            })

        # Sort by 24h volume (most liquid first), take top 30 to analyze
        usd_pairs.sort(key=lambda x: x['vol_24h_usd'], reverse=True)
        top_pairs = usd_pairs[:30]

        log.info('[MSCALP] SCAN: %d USD pairs, analyzing top %d by volume', len(usd_pairs), len(top_pairs))

        for pp in top_pairs:
            pair = pp['pair']

            # Skip if in cooldown or already holding
            if pair in self.positions:
                continue
            if time.time() - self.last_trade_time.get(pair, 0) < COOLDOWN_SEC:
                continue

            try:
                # Get candles for structure analysis
                candles = self.get_candles(pair, limit=30)
                if len(candles) < EMA_PERIOD:
                    continue

                closes = [c['close'] for c in candles]
                current_price = closes[-1]
                if current_price <= 0:
                    continue

                # ── Trend filter: price > 20 EMA ──
                ema = self.compute_ema(closes, EMA_PERIOD)
                if current_price <= ema:
                    continue

                # ── ATR for volatility ──
                atr = self.compute_atr(candles)
                atr_pct = (atr / current_price * 100) if current_price > 0 else 0

                # ── Breakout: price > recent high (last N candles, excluding current) ──
                lookback_candles = candles[-(CANDLE_LOOKBACK + 1):-1] if len(candles) > CANDLE_LOOKBACK else candles[:-1]
                recent_high = max(c['high'] for c in lookback_candles) if lookback_candles else current_price
                if current_price <= recent_high:
                    continue  # no breakout

                # ── Volume: 5-min volume check ──
                last_candle = candles[-1]
                vol_5m_usd = last_candle['volume'] * current_price
                if vol_5m_usd < MIN_5M_VOLUME_USD:
                    continue

                # ── Spread check ──
                book = self.get_product_book(pair)
                if not book:
                    continue
                if book['spread_pct'] > MAX_SPREAD_PCT:
                    continue

                # ── 15 min change ──
                if len(candles) >= 3:
                    price_15m_ago = candles[-3]['open']
                    change_15m = (current_price - price_15m_ago) / price_15m_ago * 100 if price_15m_ago > 0 else 0
                else:
                    change_15m = 0

                # ── Volume score: current vol vs avg vol ──
                avg_vol = sum(c['volume'] for c in candles[:-1]) / max(len(candles) - 1, 1)
                volume_score = last_candle['volume'] / avg_vol if avg_vol > 0 else 1.0

                # ── Volatility score ──
                volatility_score = max(atr_pct, 0.1)  # floor to avoid div/0

                candidates.append(ScanResult(
                    pair=pair,
                    price=current_price,
                    change_15m_pct=change_15m,
                    volume_5m_usd=vol_5m_usd,
                    spread_pct=book['spread_pct'],
                    atr_pct=atr_pct,
                    ema_20=ema,
                    recent_high=recent_high,
                    volume_score=volume_score,
                    volatility_score=volatility_score,
                ))

                # Rate limit: don't hammer API
                time.sleep(0.3)

            except Exception as e:
                log.warning('[MSCALP] scan error %s: %s', pair, e)
                continue

        # Sort by volume_score / volatility_score (best edge first)
        candidates.sort(key=lambda x: x.volume_score / x.volatility_score, reverse=True)

        for c in candidates[:5]:
            log.info('[MSCALP] CANDIDATE %s | $%.4f | 15m=%+.1f%% | vol=$%.0f | spread=%.2f%% | ATR=%.2f%% | volScore=%.1f | breakout above %.4f',
                     c.pair, c.price, c.change_15m_pct, c.volume_5m_usd, c.spread_pct, c.atr_pct, c.volume_score, c.recent_high)

        return candidates

    # ── Market Regime ─────────────────────────────────────────────────────────
    def check_regime(self) -> str:
        """Check if market is active enough to trade."""
        btc_price = self.get_price('BTC-USD')
        if not btc_price:
            return 'unknown'

        if self.btc_baseline is None:
            self.btc_baseline = btc_price
            return 'normal'

        btc_change = abs(btc_price - self.btc_baseline) / self.btc_baseline * 100

        # Update baseline slowly (EMA-style)
        self.btc_baseline = self.btc_baseline * 0.95 + btc_price * 0.05

        if btc_change < 0.1:
            return 'flat'  # BTC barely moving → alts probably dead too
        return 'normal'

    # ── Order Execution ───────────────────────────────────────────────────────
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
            log.warning('[MSCALP] balance error: %s', e)
        return 0.0

    def market_buy(self, pair: str, usd_amount: float) -> dict:
        if DRY_RUN:
            price = self.get_price(pair)
            amount = usd_amount / price if price else 0
            log.info('[MSCALP-DRY] BUY %s $%.2f @ $%.6f = %.8f', pair, usd_amount, price, amount)
            return {'success': True, 'price': price, 'amount': amount, 'dry': True}
        try:
            order = self.client.market_order_buy(
                client_order_id=str(uuid.uuid4()),
                product_id=pair,
                quote_size=str(round(usd_amount, 2)),
            )
            log.info('[MSCALP] BUY order: %s', order)
            time.sleep(2)
            price = self.get_price(pair)
            amount = usd_amount / price if price else 0
            return {'success': True, 'price': price, 'amount': amount, 'order': order}
        except Exception as e:
            log.error('[MSCALP] BUY error %s: %s', pair, e)
            return {'success': False, 'error': str(e)}

    def market_sell(self, pair: str, amount: float) -> dict:
        if DRY_RUN:
            price = self.get_price(pair)
            log.info('[MSCALP-DRY] SELL %s %.8f @ $%.6f', pair, amount, price)
            return {'success': True, 'price': price, 'dry': True}
        try:
            order = self.client.market_order_sell(
                client_order_id=str(uuid.uuid4()),
                product_id=pair,
                base_size=str(round(amount, 8)),
            )
            log.info('[MSCALP] SELL order: %s', order)
            time.sleep(2)
            price = self.get_price(pair)
            return {'success': True, 'price': price, 'order': order}
        except Exception as e:
            log.error('[MSCALP] SELL error %s: %s', pair, e)
            return {'success': False, 'error': str(e)}

    # ── Journal ───────────────────────────────────────────────────────────────
    def journal_open(self, pair, entry_price, amount, size_usd, notes=''):
        try:
            conn = sqlite3.connect(str(JOURNAL_DB))
            cur = conn.execute("""
                INSERT INTO trades (engine, timestamp_open, asset, category, direction,
                    entry_price, position_size, position_size_usd, regime, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, ('coinbase_momentum', datetime.now(timezone.utc).isoformat(),
                  pair, 'coinbase-momentum', 'BUY', entry_price, amount, size_usd, 'normal', notes))
            trade_id = str(cur.lastrowid)
            conn.commit()
            conn.close()
            return trade_id
        except Exception as e:
            log.warning('[MSCALP] journal open error: %s', e)
            return str(uuid.uuid4())

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
            log.warning('[MSCALP] journal close error: %s', e)

    # ── Position Management ───────────────────────────────────────────────────
    def manage_position(self, pair: str):
        """Manage open position with ATR trailing stop + time kill."""
        pos = self.positions[pair]
        price = self.get_price(pair)
        if not price:
            return

        now = time.time()
        hold_sec = now - pos.entry_time
        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
        pnl_usd = pos.amount * (price - pos.entry_price)
        fees = pos.size_usd * 0.012  # ~1.2% round trip

        # Update high watermark
        if price > pos.high_watermark:
            pos.high_watermark = price

        # ── Breakeven shift: at +BREAKEVEN_PCT%, move stop to entry ──
        if not pos.breakeven_hit and pnl_pct >= BREAKEVEN_PCT:
            pos.breakeven_hit = True
            pos.current_sl = pos.entry_price
            log.info('[MSCALP] BREAKEVEN %s | moved SL to entry $%.6f | pnl=%+.2f%%',
                     pair, pos.entry_price, pnl_pct)

        # ── Trailing stop: trail at max(TRAIL_MIN_PCT%, TRAIL_ATR_MULT × ATR%) below HWM ──
        if pos.breakeven_hit:
            trail_pct = max(TRAIL_MIN_PCT, TRAIL_ATR_MULT * pos.atr_pct)
            trail_stop = pos.high_watermark * (1 - trail_pct / 100)
            if trail_stop > pos.current_sl:
                pos.current_sl = trail_stop
                log.info('[MSCALP] TRAIL %s | SL → $%.6f (%.1f%% below HWM $%.6f)',
                         pair, trail_stop, trail_pct, pos.high_watermark)

        # ── Time kill: no +BREAKEVEN_PCT% within TIME_KILL_SEC → exit ──
        if not pos.breakeven_hit and hold_sec >= TIME_KILL_SEC:
            log.info('[MSCALP] TIME KILL %s | held %.0fs, never hit +%.1f%% | pnl=%+.2f%%',
                     pair, hold_sec, BREAKEVEN_PCT, pnl_pct)
            self._close_position(pair, price, pnl_usd - fees, pnl_pct, 'time_kill', hold_sec)
            return

        # ── Stop loss hit ──
        if price <= pos.current_sl:
            exit_type = 'trailing_stop' if pos.breakeven_hit else 'stop_loss'
            log.info('[MSCALP] %s %s | entry=%.6f exit=%.6f SL=%.6f | pnl=$%.4f (%+.2f%%)',
                     exit_type.upper(), pair, pos.entry_price, price, pos.current_sl, pnl_usd - fees, pnl_pct)
            self._close_position(pair, price, pnl_usd - fees, pnl_pct, exit_type, hold_sec)
            return

        # Log status
        sl_type = 'trail' if pos.breakeven_hit else 'initial'
        log.info('[MSCALP] HOLD %s | $%.4f (%+.2f%%) | HWM=$%.4f | SL=$%.4f (%s) | %.0fs',
                 pair, price, pnl_pct, pos.high_watermark, pos.current_sl, sl_type, hold_sec)

        self._save_state()

    def _close_position(self, pair: str, price: float, net_pnl: float, pnl_pct: float,
                        exit_type: str, hold_sec: float):
        pos = self.positions[pair]
        r = self.market_sell(pair, pos.amount)
        if r.get('success'):
            actual_price = r.get('price', price)
            self.journal_close(pos.trade_id, actual_price, net_pnl, pnl_pct, exit_type, int(hold_sec),
                               f'hwm={pos.high_watermark:.6f} atr={pos.atr_pct:.2f}% be={pos.breakeven_hit}')
            tg(f'[MOMENTUM] {exit_type.upper()} {pair} ${net_pnl:+.2f} ({pnl_pct:+.1f}%) in {hold_sec:.0f}s')
            del self.positions[pair]
            self.last_trade_time[pair] = time.time()
            self.daily_trades += 1
            self._save_state()

    # ── Entry Logic ───────────────────────────────────────────────────────────
    def try_enter(self, candidate: ScanResult):
        """Enter a momentum trade with volatility-adjusted sizing."""
        pair = candidate.pair

        # Position sizing: base × (volume_score / volatility_score), capped
        size_mult = min(2.0, max(0.5, candidate.volume_score / candidate.volatility_score))
        size_usd = round(BASE_SIZE_USD * size_mult, 2)
        size_usd = min(size_usd, BASE_SIZE_USD * 2)  # hard cap at 2× base

        # Balance check
        if not DRY_RUN:
            usd = self.get_usd_balance()
            if usd < size_usd + 0.50:
                log.info('[MSCALP] insufficient USD: $%.2f < $%.2f needed', usd, size_usd)
                return

        # Initial stop loss: INITIAL_SL_ATR_MULT × ATR below entry
        atr_sl_pct = INITIAL_SL_ATR_MULT * candidate.atr_pct
        atr_sl_pct = max(atr_sl_pct, 1.0)   # floor at 1%
        atr_sl_pct = min(atr_sl_pct, 5.0)    # cap at 5%
        initial_sl = candidate.price * (1 - atr_sl_pct / 100)

        r = self.market_buy(pair, size_usd)
        if r.get('success'):
            entry_price = r.get('price', candidate.price)
            amount = r.get('amount', size_usd / candidate.price)
            trade_id = self.journal_open(pair, entry_price, amount, size_usd,
                                          f'momentum breakout vol_score={candidate.volume_score:.1f} '
                                          f'atr={candidate.atr_pct:.2f}% spread={candidate.spread_pct:.3f}% '
                                          f'size_mult={size_mult:.2f} dry={DRY_RUN}')

            self.positions[pair] = Position(
                pair=pair, entry_price=entry_price, amount=amount,
                size_usd=size_usd, entry_time=time.time(), trade_id=trade_id,
                initial_sl=initial_sl, current_sl=initial_sl,
                high_watermark=entry_price, atr_pct=candidate.atr_pct,
                breakeven_hit=False,
            )

            log.info('[MSCALP] ENTRY %s | $%.4f | size=$%.2f (×%.1f) | SL=$%.4f (%.1f%% ATR) | vol_score=%.1f | spread=%.3f%%',
                     pair, entry_price, size_usd, size_mult, initial_sl, atr_sl_pct, candidate.volume_score, candidate.spread_pct)
            tg(f'[MOMENTUM] BUY {pair} ${size_usd:.0f} @ ${entry_price:.4f} | SL ${initial_sl:.4f} | vol={candidate.volume_score:.1f}×')
            self._save_state()

    # ── Main Loop ─────────────────────────────────────────────────────────────
    def run(self):
        log.info('=' * 60)
        log.info('[MSCALP] Momentum Scalper Starting | dry=%s base=$%.2f max_pos=%d',
                 DRY_RUN, BASE_SIZE_USD, MAX_POSITIONS)
        log.info('[MSCALP] BE=+%.1f%% | SL=%.1f×ATR | trail=max(%.1f%%, %.1f×ATR) | time_kill=%ds',
                 BREAKEVEN_PCT, INITIAL_SL_ATR_MULT, TRAIL_MIN_PCT, TRAIL_ATR_MULT, TIME_KILL_SEC)
        log.info('[MSCALP] min_vol=$%.0f | max_spread=%.1f%% | cooldown=%ds | scan_interval=%ds',
                 MIN_5M_VOLUME_USD, MAX_SPREAD_PCT, COOLDOWN_SEC, SCAN_INTERVAL_SEC)

        while self.running:
            try:
                now = time.time()

                # Daily reset
                today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                if self.daily_reset != today:
                    self.daily_trades = 0
                    self.daily_reset = today
                    self.btc_baseline = None

                # ── Manage open positions (every poll) ──
                for pair in list(self.positions.keys()):
                    self.manage_position(pair)

                # ── Scan for new candidates (every scan interval) ──
                if now - self.last_scan_time >= SCAN_INTERVAL_SEC:
                    self.last_scan_time = now

                    if self.daily_trades >= MAX_DAILY_TRADES:
                        log.info('[MSCALP] daily limit reached (%d/%d)', self.daily_trades, MAX_DAILY_TRADES)
                    elif len(self.positions) >= MAX_POSITIONS:
                        log.info('[MSCALP] max positions reached (%d/%d)', len(self.positions), MAX_POSITIONS)
                    else:
                        # Regime check
                        regime = self.check_regime()
                        if regime == 'flat':
                            log.info('[MSCALP] REGIME: flat — BTC barely moving, reducing scan')
                        else:
                            candidates = self.scan_for_candidates()
                            slots = MAX_POSITIONS - len(self.positions)
                            for c in candidates[:slots]:
                                self.try_enter(c)

                    self._save_state()

            except Exception as e:
                log.exception('[MSCALP] loop error: %s', e)

            time.sleep(POLL_SEC)

        log.info('[MSCALP] Stopped')
        self._save_state()


if __name__ == '__main__':
    MomentumScalper().run()
