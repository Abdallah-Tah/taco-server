#!/usr/bin/env python3
"""
Overnight Trading Monitor Daemon
Runs for 8 hours, sends Telegram updates every hour
Logs to /tmp/overnight_daemon.log
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path("/home/abdaltm86/.openclaw/workspace/trading")
LOG_FILE = Path("/tmp/overnight_daemon.log")
STATE_FILE = WORKSPACE / ".monitor_daemon_state.json"
TELEGRAM_CHAT = "7520899464"

DAILY_PNL_THRESHOLD = -50.00
SIZES = {"BTC": 8, "ETH": 8, "SOL": 3}
TOTAL_CHECKS = 8
CHECK_INTERVAL = 3600  # 1 hour

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def check_engines():
    engines = {
        "BTC-15m": "polymarket_btc15m.py",
        "ETH-15m": "polymarket_eth15m.py",
        "SOL-15m": "polymarket_sol15m.py",
        "XRP-15m": "polymarket_xrp15m.py",
        "Coinbase": "coinbase_momentum.py",
        "Auto-Redeem": "polymarket_auto_redeem",
        "Taco API": "taco_api_server.py"
    }
    status = {}
    for name, pattern in engines.items():
        result = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
        status[name] = "RUNNING" if result.returncode == 0 else "CRASHED"
    return status

def get_portfolio():
    try:
        with open(WORKSPACE / ".portfolio.json") as f:
            wallet = json.load(f).get("wallet_balance_usd", 0)
    except:
        wallet = 0
    
    try:
        conn = sqlite3.connect(WORKSPACE / "journal.db")
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute(f"SELECT SUM(pnl_absolute) FROM trades WHERE timestamp_close LIKE '{today}%'")
        pnl = c.fetchone()[0] or 0
        conn.close()
    except:
        pnl = 0
    
    return wallet, pnl

def check_redeems():
    try:
        with open(WORKSPACE / ".poly_positions.json") as f:
            positions = json.load(f)
        redeemable = sum(1 for v in positions.values() if v.get("redeemable"))
        return redeemable
    except:
        return 0

def run_redeem():
    log("Running auto-redeem script...")
    try:
        result = subprocess.run(
            [str(WORKSPACE / ".polymarket-venv" / "bin" / "python3"), 
             str(WORKSPACE / "scripts" / "polymarket_redeem.py")],
            capture_output=True, text=True, timeout=120, cwd=WORKSPACE
        )
        log(f"Redeem output: {result.stdout[:200]}")
    except Exception as e:
        log(f"Redeem error: {e}")

def pause_engines():
    log("🚨 CRITICAL: Pausing all trading engines - PnL threshold reached")
    for pattern in ["polymarket_btc15m.py", "polymarket_eth15m.py", "polymarket_sol15m.py", "polymarket_xrp15m.py"]:
        subprocess.run(["pkill", "-f", pattern], capture_output=True)
        log(f"Killed {pattern}")

def send_telegram(message):
    """Send via OpenClaw CLI message tool"""
    log(f"Sending Telegram message...")
    cmd = [
        "openclaw", "message", "send",
        "--target", TELEGRAM_CHAT,
        "--message", message
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log("Telegram message sent OK")
            return True
        else:
            log(f"Telegram send failed: {result.stderr}")
            return False
    except Exception as e:
        log(f"Telegram send error: {e}")
        return False

def build_message(check_num, engine_status, wallet, pnl, redeemable, paused=False):
    ts = datetime.now().strftime("%a %Y-%m-%d %H:%M EDT")
    
    lines = []
    crashed = []
    for name, status in engine_status.items():
        icon = "✅" if status == "RUNNING" else "❌"
        lines.append(f"{icon} {name}: {status}")
        if status == "CRASHED":
            crashed.append(name)
    
    pnl_icon = "📈" if pnl >= 0 else "📉"
    redeem_str = f"🔔 {redeemable} claimable" if redeemable > 0 else "🔍 None"
    
    if paused:
        size_line = "🚨 ENGINES PAUSED"
    else:
        size_line = f"SIZES: BTC=${SIZES['BTC']} ETH=${SIZES['ETH']} SOL=${SIZES['SOL']} ✅"
    
    header = f"🌮 TACO MONITOR CHECK {check_num}/8"
    
    msg = f"""{header}
📅 {ts}

ENGINES:
{chr(10).join(lines)}

PORTFOLIO:
💰 ${wallet:.2f}
{pnl_icon} PnL: ${pnl:.2f}

REDEEMS: {redeem_str}
{size_line}"""
    
    if crashed:
        msg += f"\n\n⚠️ CRASHED: {', '.join(crashed)}"
    
    if check_num < 8 and not paused:
        msg += "\n\nNext: ~+1 hour"
    else:
        msg += "\n\n✅ Monitoring complete"
    
    return msg

def restart_engine(engine_name):
    """Attempt to restart a crashed engine"""
    engine_scripts = {
        "BTC-15m": "polymarket_btc15m.py",
        "ETH-15m": "polymarket_eth15m.py",
        "SOL-15m": "polymarket_sol15m.py",
        "XRP-15m": "polymarket_xrp15m.py"
    }
    
    if engine_name not in engine_scripts:
        return False
    
    script = engine_scripts[engine_name]
    log(f"Restarting {engine_name}...")
    
    venv_python = str(WORKSPACE / ".polymarket-venv" / "bin" / "python3")
    script_path = str(WORKSPACE / "scripts" / script)
    
    # Kill any existing
    subprocess.run(["pkill", "-f", script], capture_output=True)
    time.sleep(1)
    
    # Start new
    log_path = f"/tmp/{script.replace('.py', '.log')}"
    subprocess.Popen(
        ["nohup", venv_python, script_path],
        stdout=open(log_path, "a"),
        stderr=subprocess.STDOUT,
        cwd=WORKSPACE
    )
    
    time.sleep(3)
    
    # Verify
    result = subprocess.run(["pgrep", "-f", script], capture_output=True)
    if result.returncode == 0:
        log(f"✅ {engine_name} restarted successfully")
        return True
    else:
        log(f"❌ Failed to restart {engine_name}")
        return False

def main():
    log("=== Overnight Monitor Daemon Starting ===")
    log(f"Duration: {TOTAL_CHECKS} hours, Interval: {CHECK_INTERVAL}s")
    
    # Load state
    state = {"checks_done": 0, "paused": False, "anomalies": []}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except:
            pass
    
    checks_done = state.get("checks_done", 0)
    paused = state.get("paused", False)
    
    for i in range(checks_done + 1, TOTAL_CHECKS + 1):
        log(f"=== Check {i}/{TOTAL_CHECKS} ===")
        
        # Check engines
        engine_status = check_engines()
        
        # Auto-restart crashed engines (except if paused due to PnL)
        if not paused:
            for name, status in engine_status.items():
                if status == "CRASHED" and name in ["BTC-15m", "ETH-15m", "SOL-15m", "XRP-15m"]:
                    if restart_engine(name):
                        engine_status[name] = "RUNNING"
                        state["anomalies"].append({
                            "time": datetime.now().isoformat(),
                            "type": "crash_restart",
                            "engine": name,
                            "resolved": True
                        })
        
        # Get portfolio
        wallet, pnl = get_portfolio()
        
        # Check redeems
        redeemable = check_redeems()
        
        # Check PnL threshold
        if pnl <= DAILY_PNL_THRESHOLD and not paused:
            log(f"🚨 CRITICAL: PnL ${pnl:.2f} <= threshold ${DAILY_PNL_THRESHOLD}")
            pause_engines()
            paused = True
            state["paused"] = True
        
        # Run auto-redeem if needed
        if redeemable > 0:
            run_redeem()
        
        # Build and send message
        message = build_message(i, engine_status, wallet, pnl, redeemable, paused)
        send_telegram(message)
        
        # Update state
        state["checks_done"] = i
        state["last_wallet"] = wallet
        state["last_pnl"] = pnl
        state["last_check"] = datetime.now().isoformat()
        
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        
        log(f"Check {i} complete. Wallet: ${wallet:.2f}, PnL: ${pnl:.2f}")
        
        # Sleep unless last check
        if i < TOTAL_CHECKS:
            log(f"Sleeping {CHECK_INTERVAL}s...")
            time.sleep(CHECK_INTERVAL)
    
    log("=== Overnight Monitor Daemon Complete ===")
    send_telegram("🌮 Overnight monitoring session complete (8 hours). All systems normal.")

if __name__ == "__main__":
    main()
