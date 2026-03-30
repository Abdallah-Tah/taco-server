#!/usr/bin/env python3
"""
WTI Price Monitor — alerts via Telegram when TP or SL hit
"""
import requests, time, subprocess

MINT = "G2egjtmfDed6ji7qYqiCJHYC1KwiaCrZYHALKKipump"
ENTRY = 0.0003131
TP = ENTRY * 2       # 2x = $0.0006262
SL = ENTRY * 0.5     # -50% = $0.00015655
TELEGRAM_TOKEN = "8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4"
CHAT_ID = "7520899464"
CHECK_INTERVAL = 60  # seconds

def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg}, timeout=10)

def get_price():
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{MINT}", timeout=10)
    pairs = r.json().get("pairs", [])
    if pairs:
        return float(pairs[0].get("priceUsd", 0) or 0)
    return 0

print(f"🌮 WTI Monitor started | Entry=${ENTRY} | TP=${TP:.8f} | SL=${SL:.8f}")
send_telegram(f"🌮 WTI Monitor started\nEntry: ${ENTRY}\nTP: ${TP:.8f} (+100%)\nSL: ${SL:.8f} (-50%)")

while True:
    try:
        price = get_price()
        if price <= 0:
            time.sleep(CHECK_INTERVAL)
            continue

        pct = ((price - ENTRY) / ENTRY) * 100
        print(f"WTI: ${price:.8f} ({pct:+.1f}%)")

        if price >= TP:
            send_telegram(f"🚀 WTI HIT TAKE PROFIT!\nPrice: ${price:.8f}\nGain: +{pct:.1f}%\n\n⚡ SELL 50% NOW!")
            time.sleep(300)  # wait 5 min then keep monitoring remainder
        elif price <= SL:
            send_telegram(f"🛑 WTI HIT STOP LOSS!\nPrice: ${price:.8f}\nLoss: {pct:.1f}%\n\n⚡ SELL ALL NOW!")
            break
        elif pct > 50:
            send_telegram(f"📈 WTI UPDATE: +{pct:.1f}%\nPrice: ${price:.8f}\nGetting close to TP!")

    except Exception as e:
        print(f"Error: {e}")

    time.sleep(CHECK_INTERVAL)
