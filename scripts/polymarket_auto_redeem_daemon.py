#!/usr/bin/env python3
"""Background auto-redeem daemon for Polymarket claimables.

- Runs independently of BTC/ETH engines.
- Every 5 minutes checks for redeemable positions.
- Claims immediately when found.
- Logs a heartbeat "Nothing to claim" at most every 30 minutes.
- Uses a lock file to avoid overlapping runs/conflicts.
- Sends a Telegram cha-ching sound on successful redeems.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from openclaw_runtime import channel_output
except Exception:
    channel_output = None
from runtime_paths import SCRIPT_ROOT, TRADING_ROOT, resolve_runtime_python

ROOT = TRADING_ROOT
VENV = resolve_runtime_python()
REDEEM = SCRIPT_ROOT / 'polymarket_redeem.py'
LOG_FILE = Path('/tmp/polymarket_auto_redeem.log')
PID_FILE = Path('/tmp/polymarket_auto_redeem.pid')
LOCK_FILE = Path('/tmp/polymarket_auto_redeem.lock')
STATE_FILE = Path('/tmp/polymarket_auto_redeem_state.json')
CHECK_EVERY_SEC = 300
NOTHING_LOG_EVERY_SEC = 1800
CHA_CHING_FILE = Path.home() / '.openclaw' / 'media' / 'inbound' / 'shopify_sale_sound---241c049a-5aef-4109-a430-38b2d21b6413.mp3'
TELEGRAM_TARGET = '7520899464'
PUSHCUT_URL = 'https://api.pushcut.io/Sp32e9ypdNreANO8BpJGG/notifications/My%20First%20Notification'


def log(msg: str):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    with LOG_FILE.open('a') as f:
        f.write(line + '\n')
    print(line, flush=True)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_nothing_log": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def acquire_lock():
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            os.kill(old_pid, 0)
            return False
        except Exception:
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


def run_redeem_check():
    dry = subprocess.run([str(VENV), str(REDEEM), '--dry-run', '--json'], capture_output=True, text=True, timeout=180)
    dry_stdout = dry.stdout.strip()
    try:
        items = json.loads(dry_stdout) if dry_stdout else []
    except Exception:
        items = []

    ready = [x for x in items if (x.get('status') == 'READY' and float(x.get('value') or 0) > 0)]
    if not ready:
        return {"claimed": False, "items": [], "claimed_total": 0.0}

    live = subprocess.run([str(VENV), str(REDEEM), '--json'], capture_output=True, text=True, timeout=300)
    live_stdout = live.stdout.strip()
    try:
        result = json.loads(live_stdout) if live_stdout else []
    except Exception:
        result = []

    claimed_total = 0.0
    claimed_items = []
    for x in result:
        if x.get('status') == 'redeemed':
            val = float(x.get('value') or 0)
            claimed_total += val
            claimed_items.append(x)
    return {"claimed": bool(claimed_items), "items": claimed_items, "claimed_total": claimed_total}


def send_telegram_cha_ching(item):
    if not CHA_CHING_FILE.exists():
        log(f"[REDEEM] Cha-ching file missing: {CHA_CHING_FILE}")
        return
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    caption = f"💰✅ Redeemed ${value:.2f} back to USDC.e 🎉\n{title}"
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', TELEGRAM_TARGET,
            '--media', str(CHA_CHING_FILE),
            '--caption', caption
        ], check=False, capture_output=True, text=True, timeout=30)
    except Exception as e:
        log(f"[REDEEM] Cha-ching send failed: {e}")


def send_pushcut_notification(item):
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    body = f"💰✅ Redeemed ${value:.2f} back to USDC.e 🎉 — {title}"
    try:
        subprocess.run([
            'curl', '-sS', '-X', 'POST',
            PUSHCUT_URL,
            '-H', 'Content-Type: application/json',
            '-d', json.dumps({'text': body})
        ], check=False, capture_output=True, text=True, timeout=20)
    except Exception as e:
        log(f"[REDEEM] Pushcut send failed: {e}")


def main():
    PID_FILE.write_text(str(os.getpid()))
    log('[REDEEM] Auto-redeem daemon started')
    state = load_state()
    while True:
        if acquire_lock():
            try:
                result = run_redeem_check()
                if result['claimed']:
                    for item in result['items']:
                        log(f"[REDEEM] Claimed ${float(item.get('value') or 0):.2f} from {item.get('title')}")
                        send_telegram_cha_ching(item)
                        send_pushcut_notification(item)
                else:
                    now = int(time.time())
                    if now - int(state.get('last_nothing_log') or 0) >= NOTHING_LOG_EVERY_SEC:
                        log('[REDEEM] Nothing to claim')
                        state['last_nothing_log'] = now
                        save_state(state)
            except Exception as e:
                log(f'[REDEEM] ERROR {e}')
            finally:
                release_lock()
        time.sleep(CHECK_EVERY_SEC)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('[REDEEM] Auto-redeem daemon stopped')
