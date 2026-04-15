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
import sqlite3
import subprocess
import sys
import time
import requests as _requests
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
TELEGRAM_TARGET = '-1003948211258'
TELEGRAM_TOPIC  = 3

# Load bot token from secrets, fallback to OpenClaw config
_SECRETS = {}
_secrets_path = Path('/home/abdaltm86/.config/openclaw/secrets.env')
if _secrets_path.exists():
    for line in _secrets_path.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            _SECRETS[k.strip()] = v.strip().strip("'").strip('"')

TG_TOKEN = _SECRETS.get('TELEGRAM_TOKEN', os.environ.get('TELEGRAM_TOKEN', ''))
if not TG_TOKEN:
    try:
        _cfg = json.loads(Path('/home/abdaltm86/.openclaw/openclaw.json').read_text())
        TG_TOKEN = _cfg.get('channels', {}).get('telegram', {}).get('botToken', '')
    except Exception:
        TG_TOKEN = ''
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


def ensure_single_instance():
    """Exit if another daemon instance is already alive."""
    try:
        if PID_FILE.exists():
            old_pid = int(PID_FILE.read_text().strip())
            if old_pid and old_pid != os.getpid():
                try:
                    os.kill(old_pid, 0)
                    log(f'[REDEEM] Another daemon already running (pid={old_pid}), exiting')
                    return False
                except Exception:
                    pass
        PID_FILE.write_text(str(os.getpid()))
        return True
    except Exception as e:
        log(f'[REDEEM] PID guard error: {e}')
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

    ready = [x for x in items if x.get('status') == 'READY']
    if not ready:
        return {"claimed": False, "items": [], "claimed_total": 0.0, "errors": []}

    live = subprocess.run([str(VENV), str(REDEEM), '--json'], capture_output=True, text=True, timeout=300)
    live_stdout = live.stdout.strip()
    try:
        result = json.loads(live_stdout) if live_stdout else []
    except Exception:
        result = []

    claimed_total = 0.0
    claimed_items = []
    errors = []
    for x in result:
        if x.get('status') == 'redeemed':
            val = float(x.get('value') or 0)
            claimed_total += val
            claimed_items.append(x)
        elif x.get('status') in {'error', 'insufficient_gas', 'redeem_failed'}:
            errors.append(x)
    return {"claimed": bool(claimed_items), "items": claimed_items, "claimed_total": claimed_total, "errors": errors}


def _redeem_prefix(title: str) -> str:
    t = (title or '').lower()
    if 'solana up or down' in t:
        return '[SOL-REDEEM]'
    if 'bitcoin up or down' in t:
        return '[BTC-REDEEM]'
    if 'ethereum up or down' in t:
        return '[ETH-REDEEM]'
    return '[REDEEM]'


def _normalize_tx_hash(tx_hash):
    tx = str(tx_hash or '').strip().lower()
    if tx.startswith('0x'):
        tx = tx[2:]
    if not tx:
        return ''
    return f'0x{tx}'


def _get_trade_details(title: str):
    """Look up direction, entry price, and pnl from journal for this market."""
    try:
        conn = sqlite3.connect(str(ROOT / 'journal.db'))
        c = conn.cursor()
        # Match by asset title, most recent resolved trade
        c.execute("""
            SELECT direction, entry_price, pnl_absolute, position_size_usd
            FROM trades
            WHERE asset LIKE ? AND exit_type='resolved' AND direction NOT IN ('REDEEM')
            ORDER BY timestamp_close DESC LIMIT 1
        """, (f'%{title[:40]}%',))
        row = c.fetchone()
        conn.close()
        return row  # (direction, entry_price, pnl_absolute, position_size_usd) or None
    except Exception:
        return None


def send_telegram_cha_ching(item):
    """Send celebration notification for winning redeems."""
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    tx = _normalize_tx_hash(item.get('txHash') or item.get('transactionHash')) or 'n/a'
    prefix = _redeem_prefix(title)

    # Look up trade details from journal
    details = _get_trade_details(title)
    if details:
        direction, entry_price, pnl_abs, size_usd = details
        if direction == 'UP':
            market_label = title.replace('Up or Down', 'UP ⬆️')
        elif direction == 'DOWN':
            market_label = title.replace('Up or Down', 'DOWN ⬇️')
        else:
            market_label = title
        cost = float(size_usd or 0)
        profit = value - cost
        message = (
            f"💰 CHA-CHING! {prefix}\n"
            f"Market: {market_label}\n"
            f"Entry: ${float(entry_price or 0):.2f} | Paid: ${cost:.2f}\n"
            f"Redeemed: ${value:.2f} | Profit: +${profit:.2f}\n"
            f"tx={tx}"
        )
    else:
        message = f"💰 CHA-CHING! {prefix} Redeemed ${value:.2f} from {title} | pnl=${value:.2f} tx={tx}"
    try:
        r = _requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_TARGET, "text": message, "message_thread_id": TELEGRAM_TOPIC},
            timeout=10)
        if r.status_code != 200:
            log(f"[REDEEM] Telegram send failed code={r.status_code} body={r.text}")
    except Exception as e:
        log(f"[REDEEM] Telegram send failed: {e}")


def send_telegram_loss(item):
    """Send quiet notification for losing redeems (no cha-ching)."""
    title = item.get('title') or 'Polymarket redeem'
    value = float(item.get('value') or 0)
    tx = _normalize_tx_hash(item.get('txHash') or item.get('transactionHash')) or 'n/a'
    prefix = _redeem_prefix(title)
    message = f"❌ {prefix} Lost position: {title} ($0.00) tx={tx}"
    try:
        r = _requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_TARGET, "text": message, "message_thread_id": TELEGRAM_TOPIC},
            timeout=10)
        if r.status_code != 200:
            log(f"[REDEEM] Telegram loss send failed code={r.status_code} body={r.text}")
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
    if not ensure_single_instance():
        return
    log('[REDEEM] Auto-redeem daemon started')
    state = load_state()
    try:
        while True:
            if acquire_lock():
                try:
                    result = run_redeem_check()
                    if result['claimed']:
                        for item in result['items']:
                            val = float(item.get('value') or 0)
                            title = item.get('title') or ''
                            if val > 0:
                                log(f"[REDEEM] Claimed ${val:.2f} from {title}")
                                send_telegram_cha_ching(item)
                                # send_redeem_email(item)  # Disabled per user request
                                send_pushcut_notification(item)
                            else:
                                log(f"[REDEEM] Zero-value redeem for {title} — suppressing loss alert")
                    elif result.get('errors'):
                        for item in result['errors']:
                            status = item.get('status') or 'error'
                            err = item.get('error') or ''
                            if status == 'insufficient_gas':
                                log(
                                    f"[REDEEM] Insufficient POL gas for {item.get('title')} "
                                    f"(need {float(item.get('requiredPol') or 0):.6f}, have {float(item.get('availablePol') or 0):.6f})"
                                )
                            else:
                                log(f"[REDEEM] {status} for {item.get('title')}: {err}")
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
    finally:
        try:
            if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log('[REDEEM] Auto-redeem daemon stopped')
