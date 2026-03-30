#!/usr/bin/env python3
"""sentinel.py — Sentinel agent runner"""
import subprocess, time, json, sys, os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / ".openclaw/workspace/trading"
HEALTH_FILE = "/tmp/sentinel_health.json"
PID_FILE = "/tmp/sentinel.pid"

SCRIPTS = {
    "solana_sniper": "taco_trader.py",
    "btc_15m":       "polymarket_btc15m.py",
    "eth_15m":       "polymarket_eth15m.py",
    "auto_redeem":   "polymarket_auto_redeem_daemon.py",
    "whale_tracker": "polymarket_whale_tracker.py",
    "news_arb":      "polymarket_news_arb.py",
}

TOKEN = "8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4"
CHAT_ID = "7520899464"

RESTART_CWD = str(ROOT)
RESTART_VENV = str(ROOT / ".polymarket-venv/bin/python3")

RESTART_CMD = {
    "solana_sniper": [RESTART_VENV, "scripts/taco_trader.py"],
    "btc_15m":       [RESTART_VENV, "scripts/polymarket_btc15m.py"],
    "eth_15m":       [RESTART_VENV, "scripts/polymarket_eth15m.py"],
    "auto_redeem":   [RESTART_VENV, "scripts/polymarket_auto_redeem_daemon.py"],
    "whale_tracker": [RESTART_VENV, "scripts/polymarket_whale_tracker.py"],
    "news_arb":      [RESTART_VENV, "scripts/polymarket_news_arb.py", "run", "--live"],
}

def tg(msg):
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception: pass

def get_pid(script_name):
    """Find PID of running script by scanning ps aux."""
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if script_name in line and "python3" in line and "grep" not in line:
                parts = line.split()
                try:
                    return int(parts[1])
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return None

def get_all_pids():
    return {name: get_pid(script) for name, script in SCRIPTS.items()}

def get_sys_stats():
    try:
        import psutil
        return psutil.cpu_percent(interval=0.5), psutil.virtual_memory().percent
    except Exception:
        return None, None

def write_health(pids, cpu, mem):
    health = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_percent": round(cpu, 1) if cpu else 0,
        "mem_percent": round(mem, 1) if mem else 0,
        "processes": {name: {"running": pid is not None, "pid": pid, "restarts": 0}
                      for name, pid in pids.items()},
        "ok": all(v is not None for v in pids.values())
    }
    with open(HEALTH_FILE, "w") as f:
        json.dump(health, f, indent=2)

# Write PID
with open(PID_FILE, "w") as f:
    f.write(str(os.getpid()))

print(f"Sentinel started, PID={os.getpid()}", flush=True)

CYCLES = 0
while True:
    CYCLES += 1
    pids = get_all_pids()
    cpu, mem = get_sys_stats()
    write_health(pids, cpu, mem)

    # Log every 5 cycles (10 min)
    if CYCLES % 5 == 0:
        status = "ALL OK" if all(v is not None for v in pids.values()) else "ISSUES"
        print(f"[{datetime.now().strftime('%H:%M')}] Sentinel cycle {CYCLES}: {status}", flush=True)

    time.sleep(120)  # 2 min cycle
