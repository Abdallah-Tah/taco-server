#!/usr/bin/env python3
import time, json, requests
from pathlib import Path

SECRETS=Path('/home/abdaltm86/.config/openclaw/secrets.env')
LOG=Path('/tmp/polymarket_btc15m.live.log')
STATE=Path('/tmp/btc_maker_notify.state')

def load_env(path):
    env={}
    if path.exists():
        for ln in path.read_text().splitlines():
            if '=' in ln and not ln.strip().startswith('#'):
                k,v=ln.split('=',1)
                env[k.strip()]=v.strip().strip('"').strip("'")
    return env

env=load_env(SECRETS)
TOKEN=env.get('TELEGRAM_TOKEN','8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4')
CHAT=env.get('CHAT_ID','7520899464')

def send(msg):
    try:
        requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT,'text':msg},timeout=8)
    except Exception:
        pass

last=0
if STATE.exists():
    try:last=int(STATE.read_text().strip())
    except: last=0

while True:
    try:
        size=LOG.stat().st_size
        if last>size: last=0
        with LOG.open('r',errors='ignore') as f:
            f.seek(last)
            for line in f:
                if '[BTC-MAKER]' in line:
                    send(line.strip())
            last=f.tell()
        STATE.write_text(str(last))
    except Exception:
        pass
    time.sleep(2)
