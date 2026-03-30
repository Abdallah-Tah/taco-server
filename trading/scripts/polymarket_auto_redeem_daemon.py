#!/usr/bin/env python3
"""Background auto-redeem daemon for Polymarket claimables.

- Runs independently of BTC/ETH engines.
- Every 5 minutes checks for redeemable positions.
- Claims immediately when found.
- Logs a heartbeat "Nothing to claim" at most every 30 minutes.
- Uses a lock file to avoid overlapping runs/conflicts.
- Sends a Telegram redeem notification on successful redeems.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from openclaw_runtime import channel_output
except Exception:
    channel_output = None

ROOT = Path.home() / '.openclaw' / 'workspace' / 'trading'
VENV = ROOT / '.polymarket-venv' / 'bin' / 'python3'
REDEEM = ROOT / 'scripts' / 'polymarket_redeem.py'
LOG_FILE = Path('/tmp/polymarket_auto_redeem.log')
PID_FILE = Path('/tmp/polymarket_auto_redeem.pid')
LOCK_FILE = Path('/tmp/polymarket_auto_redeem.lock')
STATE_FILE = Path('/tmp/polymarket_auto_redeem_state.json')
CHECK_EVERY_SEC = 300
NOTHING_LOG_EVERY_SEC = 1800
TELEGRAM_TARGET = '7520899464'
REDEEM_EMAIL_TO = 'abdallahtmohamed86@gmail.com'
REDEEM_EMAIL_SUBJECT = 'TacoTrader: REDEEMED 🤑'
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
    """Send celebration notification for winning redeems."""
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    message = f"💰 CHA-CHING! Redeemed ${value:.2f} from {title}"
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', TELEGRAM_TARGET,
            '--message', message,
        ], check=False, capture_output=True, text=True, timeout=30)
    except Exception as e:
        log(f"[REDEEM] Telegram send failed: {e}")


def send_telegram_loss(item):
    """Send quiet notification for losing redeems (no cha-ching)."""
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    message = f"❌ Lost position: {title} ($0.00)"
    try:
        subprocess.run([
            'openclaw', 'message', 'send',
            '--channel', 'telegram',
            '--target', TELEGRAM_TARGET,
            '--message', message,
        ], check=False, capture_output=True, text=True, timeout=30)
    except Exception as e:
        log(f"[REDEEM] Telegram loss send failed: {e}")


def send_redeem_email(item):
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    body = f"💰 CHA-CHING! Redeemed ${value:.2f} from {title}\n"
    # NOTE: When running under cron/nohup, PATH may not include /usr/sbin.
    # Try PATH lookup first, then common absolute locations.
    sendmail = shutil.which('sendmail')
    if not sendmail:
        for candidate in ('/usr/sbin/sendmail', '/usr/bin/sendmail'):
            if Path(candidate).exists():
                sendmail = candidate
                break
    if not sendmail:
        log('[REDEEM] Email skipped: sendmail not available (PATH missing /usr/sbin?)')
        return
    raw = f"To: {REDEEM_EMAIL_TO}\nSubject: {REDEEM_EMAIL_SUBJECT}\n\n{body}"
    try:
        subprocess.run([sendmail, '-t', '-oi'], input=raw, text=True, check=False, capture_output=True, timeout=30)
    except Exception as e:
        log(f"[REDEEM] Email send failed: {e}")


def send_pushcut_notification(item):
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    body = f"Redeemed ${value:.2f} back to USDC.e — {title}"
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
                        val = float(item.get('value') or 0)
                        if val <= 0:
                            # Loss - notify but without celebration
                            log(f"[REDEEM] Lost position: {item.get('title')} ($0.00)")
                            send_telegram_loss(item)
                        else:
                            # Win - full celebration
                            log(f"[REDEEM] Claimed ${val:.2f} from {item.get('title')}")
                            send_telegram_cha_ching(item)
                            # send_redeem_email(item)  # Disabled per user request
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
