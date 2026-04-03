#!/usr/bin/env python3
"""
polymarket_trend_switch.py — Auto Start/Stop Polymarket Engines Based on BTC Trend
===================================================================================
Monitors BTC price trend over multiple timeframes. When all confirm UP, starts
the BTC/ETH 15m engines. When trend turns DOWN or flat, stops them.

Checks every 5 minutes. Uses Coinbase API for price data.
Logs decisions to /tmp/trend_switch.log and sends Telegram alerts.

Config via secrets.env:
  TREND_SWITCH_ENABLED=true
  TREND_SWITCH_DRY_RUN=true          (log only, don't start/stop engines)
  TREND_SWITCH_POLL_MIN=5            (check every N minutes)
  TREND_SWITCH_1H_MIN=0.10           (1h trend must be > +0.10%)
  TREND_SWITCH_4H_MIN=0.20           (4h trend must be > +0.20%)
  TREND_SWITCH_STOP_1H=-0.10         (stop if 1h trend < -0.10%)
"""
import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import requests

WORK_DIR = Path('/home/abdaltm86/.openclaw/workspace/trading')
SECRETS = Path('/home/abdaltm86/.config/openclaw/secrets.env')
VENV_PY = WORK_DIR / '.polymarket-venv' / 'bin' / 'python3'
BTC_SCRIPT = WORK_DIR / 'scripts' / 'polymarket_btc15m.py'
ETH_SCRIPT = WORK_DIR / 'scripts' / 'polymarket_eth15m.py'
STATE_PATH = WORK_DIR / '.trend_switch_state.json'
LOG_PATH = Path('/tmp/trend_switch.log')

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

ENABLED      = _bool('TREND_SWITCH_ENABLED', True)
DRY_RUN      = _bool('TREND_SWITCH_DRY_RUN', True)
POLL_MIN     = _int('TREND_SWITCH_POLL_MIN', 5)
# Trend thresholds to START engines (all must be met)
START_1H_MIN = _float('TREND_SWITCH_1H_MIN', 0.10)    # 1h must be > +0.10%
START_4H_MIN = _float('TREND_SWITCH_4H_MIN', 0.20)    # 4h must be > +0.20%
# Trend threshold to STOP engines (any triggers stop)
STOP_1H      = _float('TREND_SWITCH_STOP_1H', -0.10)   # stop if 1h < -0.10%

TELEGRAM_TOKEN = ENV.get('TELEGRAM_TOKEN', '')
CHAT_ID = ENV.get('CHAT_ID', '')


# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(LOG_PATH, 'a') as f:
        f.write(line + '\n')

def tg(msg):
    if DRY_RUN or not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', str(CHAT_ID),
            '--message', str(msg),
        ], capture_output=True, text=True, timeout=15)
    except:
        pass


# ── Price tracking ────────────────────────────────────────────────────────────
# Store prices in memory: deque of (timestamp, price) — keep 5 hours
price_history = deque()
MAX_HISTORY_SEC = 5 * 3600  # 5 hours

def get_btc_price():
    try:
        r = requests.get(
            'https://api.exchange.coinbase.com/products/BTC-USD/ticker',
            timeout=5,
        )
        return float(r.json()['price'])
    except Exception as e:
        log(f'[TREND] BTC price error: {e}')
        return None

def record_price(price):
    now = int(time.time())
    price_history.append((now, price))
    cutoff = now - MAX_HISTORY_SEC
    while price_history and price_history[0][0] < cutoff:
        price_history.popleft()

def get_trend(minutes):
    """Calculate price change % over the last N minutes."""
    if not price_history:
        return None
    now = int(time.time())
    target_ts = now - (minutes * 60)
    # Find the closest price to target_ts
    closest = None
    for ts, price in price_history:
        if ts <= target_ts + 30:  # within 30s tolerance
            closest = price
    if closest is None:
        return None
    current = price_history[-1][1]
    return (current - closest) / closest * 100


# ── Engine management ─────────────────────────────────────────────────────────
def engines_running():
    """Check if BTC or ETH 15m engines are running."""
    try:
        result = subprocess.run(['pgrep', '-f', 'polymarket_btc15m.py'], capture_output=True)
        btc = result.returncode == 0
        result = subprocess.run(['pgrep', '-f', 'polymarket_eth15m.py'], capture_output=True)
        eth = result.returncode == 0
        return btc or eth
    except:
        return False

def start_engines():
    """Start BTC and ETH 15m engines."""
    if DRY_RUN:
        log('[TREND] DRY RUN: would START engines')
        return
    log('[TREND] STARTING BTC and ETH engines')
    subprocess.Popen(
        [str(VENV_PY), str(BTC_SCRIPT)],
        stdout=open('/tmp/btc15m.log', 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(2)
    subprocess.Popen(
        [str(VENV_PY), str(ETH_SCRIPT)],
        stdout=open('/tmp/eth15m.log', 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    tg('[TREND SWITCH] Engines STARTED — BTC trend is UP')

def stop_engines():
    """Stop BTC and ETH 15m engines."""
    if DRY_RUN:
        log('[TREND] DRY RUN: would STOP engines')
        return
    log('[TREND] STOPPING BTC and ETH engines')
    subprocess.run(['pkill', '-f', 'polymarket_btc15m.py'], capture_output=True)
    subprocess.run(['pkill', '-f', 'polymarket_eth15m.py'], capture_output=True)
    tg('[TREND SWITCH] Engines STOPPED — BTC trend turned DOWN')


# ── State persistence ─────────────────────────────────────────────────────────
def save_state(engines_on, prices):
    data = {
        'engines_on': engines_on,
        'prices': list(prices)[-360:],  # keep last 360 samples (30 hours at 5min intervals)
        'updated': datetime.now(timezone.utc).isoformat(),
    }
    STATE_PATH.write_text(json.dumps(data))

def load_state():
    if not STATE_PATH.exists():
        return False, deque()
    try:
        data = json.loads(STATE_PATH.read_text())
        prices = deque(data.get('prices', []))
        return data.get('engines_on', False), prices
    except:
        return False, deque()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    global price_history

    running = True
    def handle_stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    engines_on, saved_prices = load_state()
    if saved_prices:
        price_history = saved_prices
        log(f'[TREND] Loaded {len(price_history)} price samples from state')

    log('=' * 60)
    log(f'[TREND] Starting trend switch | dry={DRY_RUN} poll={POLL_MIN}min')
    log(f'[TREND] START conditions: 1h > +{START_1H_MIN}% AND 4h > +{START_4H_MIN}%')
    log(f'[TREND] STOP condition: 1h < {STOP_1H}%')
    log(f'[TREND] Engines currently: {"ON" if engines_on else "OFF"}')

    # Need warmup period before making decisions
    warmup_samples = max(12, int(60 / POLL_MIN))  # at least 1 hour of data
    samples_collected = len(price_history)

    while running:
        price = get_btc_price()
        if price is None:
            time.sleep(30)
            continue

        record_price(price)
        samples_collected += 1

        trend_1h = get_trend(60)
        trend_4h = get_trend(240)

        # Status log
        t1 = f'{trend_1h:+.3f}%' if trend_1h is not None else 'n/a'
        t4 = f'{trend_4h:+.3f}%' if trend_4h is not None else 'n/a'
        actual_running = engines_running()
        log(f'[TREND] BTC=${price:,.2f} | 1h={t1} 4h={t4} | engines={"ON" if actual_running else "OFF"} state={"ON" if engines_on else "OFF"}')

        # Warmup check
        if samples_collected < warmup_samples:
            log(f'[TREND] Warming up: {samples_collected}/{warmup_samples} samples')
            save_state(engines_on, price_history)
            time.sleep(POLL_MIN * 60)
            continue

        # ── Decision logic ──
        if not engines_on:
            # Check if we should START
            if trend_1h is not None and trend_4h is not None:
                if trend_1h > START_1H_MIN and trend_4h > START_4H_MIN:
                    log(f'[TREND] START SIGNAL: 1h={t1} > +{START_1H_MIN}% AND 4h={t4} > +{START_4H_MIN}%')
                    engines_on = True
                    start_engines()
                else:
                    reasons = []
                    if trend_1h <= START_1H_MIN:
                        reasons.append(f'1h {t1} <= +{START_1H_MIN}%')
                    if trend_4h <= START_4H_MIN:
                        reasons.append(f'4h {t4} <= +{START_4H_MIN}%')
                    log(f'[TREND] STAY OFF: {", ".join(reasons)}')
        else:
            # Check if we should STOP
            if trend_1h is not None and trend_1h < STOP_1H:
                log(f'[TREND] STOP SIGNAL: 1h={t1} < {STOP_1H}%')
                engines_on = False
                stop_engines()
            else:
                log(f'[TREND] KEEP RUNNING: 1h={t1} still above {STOP_1H}%')

        # Sync state with reality
        if engines_on and not actual_running and not DRY_RUN:
            log('[TREND] Engines should be ON but not running — restarting')
            start_engines()
        elif not engines_on and actual_running and not DRY_RUN:
            log('[TREND] Engines should be OFF but still running — stopping')
            stop_engines()

        save_state(engines_on, price_history)
        time.sleep(POLL_MIN * 60)

    log('[TREND] Stopped')
    save_state(engines_on, price_history)


if __name__ == '__main__':
    main()
