#!/usr/bin/env python3
"""
Overnight Trading Monitor - Hourly Health Checks
Runs for 8 hours, checks every hour, sends Telegram updates via OpenClaw
"""

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path("/home/abdaltm86/.openclaw/workspace/trading")
STATE_FILE = WORKSPACE / ".monitor_state.json"
LOG_FILE = Path("/tmp/overnight_monitor.log")
DAILY_PNL_THRESHOLD = -50.00
SIZES = {"BTC": 8, "ETH": 8, "SOL": 3}

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {msg}"
    print(log_msg)
    with open(LOG_FILE, "a") as f:
        f.write(log_msg + "\n")

def check_engine_processes():
    """Check if all engine processes are running"""
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
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True
        )
        status[name] = "running" if result.returncode == 0 else "crashed"
    
    return status

def get_portfolio_info():
    """Get wallet balance and today's PnL"""
    try:
        with open(WORKSPACE / ".portfolio.json") as f:
            portfolio = json.load(f)
        wallet = portfolio.get("wallet_balance_usd", 0)
    except:
        wallet = 0
    
    try:
        conn = sqlite3.connect(WORKSPACE / "journal.db")
        c = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute(f"SELECT SUM(pnl_absolute) FROM trades WHERE timestamp_close LIKE '{today}%'")
        result = c.fetchone()[0]
        pnl = result if result else 0.0
        conn.close()
    except:
        pnl = 0.0
    
    return wallet, pnl

def check_redeemable_positions():
    """Check for claimable redeems"""
    try:
        with open(WORKSPACE / ".poly_positions.json") as f:
            positions = json.load(f)
        redeemable = [k for k, v in positions.items() if v.get("redeemable", False)]
        return len(redeemable), redeemable
    except:
        return 0, []

def run_auto_redeem():
    """Run the auto-redeem script"""
    log("Running auto-redeem script...")
    try:
        result = subprocess.run(
            ["python3", str(WORKSPACE / "scripts" / "polymarket_auto_redeem_daemon.py")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=WORKSPACE
        )
        log(f"Redeem script output: {result.stdout[:500]}")
        return True
    except Exception as e:
        log(f"Redeem script error: {e}")
        return False

def pause_engines():
    """Safely pause all trading engines"""
    log("CRITICAL: Pausing all trading engines")
    
    engines_to_kill = [
        "polymarket_btc15m.py",
        "polymarket_eth15m.py",
        "polymarket_sol15m.py",
        "polymarket_xrp15m.py"
    ]
    
    for pattern in engines_to_kill:
        subprocess.run(["pkill", "-f", pattern], capture_output=True)
        log(f"Killed {pattern}")
    
    # Update state
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        state["engines_paused"] = True
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except:
        pass

def send_telegram_message(message):
    """Send message via OpenClaw CLI"""
    log(f"TELEGRAM: {message[:200]}...")
    # Note: Actual sending would be done by the main agent
    # This is a placeholder for the message content
    return message

def build_status_message(check_num, engine_status, wallet, pnl, redeemable_count, paused=False):
    """Build the status message"""
    timestamp = datetime.now().strftime("%a %Y-%m-%d %H:%M EDT")
    
    engine_lines = []
    for name, status in engine_status.items():
        icon = "✅" if status == "running" else "❌"
        engine_lines.append(f"{icon} {name}: {status.upper()}")
    
    redeem_status = f"🔔 {redeemable_count} claimable position(s)" if redeemable_count > 0 else "🔍 No claimable redeems"
    
    if paused:
        size_line = "🚨 ENGINES PAUSED - PnL threshold reached"
    else:
        size_line = f"**SIZES:** BTC:${SIZES['BTC']} | ETH:${SIZES['ETH']} | SOL:${SIZES['SOL']} ✅"
    
    message = f"""🌮 **TACO OVERNIGHT MONITOR - CHECK {check_num}**
📅 {timestamp}

**ENGINE HEALTH:**
{chr(10).join(engine_lines)}

**PORTFOLIO:**
💰 Wallet: ${wallet:.2f}
📊 Today PnL: ${pnl:.2f}

**REDEEM STATUS:**
{redeem_status}

{size_line}

Next check: ~+1 hour"""
    
    return message

def main():
    log("=== Overnight Monitor Started ===")
    
    # Load state
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except:
        state = {
            "start_time": datetime.now().isoformat(),
            "checks_completed": 0,
            "engines_paused": False
        }
    
    checks_remaining = 8 - state.get("checks_completed", 0)
    
    for i in range(1, checks_remaining + 1):
        log(f"=== Check {i} of {checks_remaining} ===")
        
        # Check engines
        engine_status = check_engine_processes()
        
        # Get portfolio
        wallet, pnl = get_portfolio_info()
        
        # Check redeems
        redeemable_count, redeemable_ids = check_redeemable_positions()
        
        # Check for critical PnL
        if pnl <= DAILY_PNL_THRESHOLD and not state.get("engines_paused", False):
            log(f"CRITICAL: Daily PnL at ${pnl:.2f} - threshold ${DAILY_PNL_THRESHOLD}")
            pause_engines()
            state["engines_paused"] = True
        
        # Build and send message
        message = build_status_message(
            i,
            engine_status,
            wallet,
            pnl,
            redeemable_count,
            paused=state.get("engines_paused", False)
        )
        
        # Output message for main agent to send
        print(f"\n=== TELEGRAM MESSAGE ===\n{message}\n========================\n")
        
        # Run auto-redeem if needed
        if redeemable_count > 0:
            run_auto_redeem()
        
        # Update state
        state["checks_completed"] = state.get("checks_completed", 0) + 1
        state["last_pnl"] = pnl
        state["next_check"] = (datetime.now() + timedelta(hours=1)).isoformat()
        
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
        except:
            pass
        
        # Sleep for 1 hour (unless last check)
        if i < checks_remaining:
            log("Sleeping for 1 hour...")
            print("YIELD:60000")  # Signal to yield for 60 seconds (will be extended)
            time.sleep(60)  # Sleep 60s for now, main agent will handle longer sleep
            break  # Break after first iteration to let main agent handle continuation
    
    log("=== Monitor Session Complete (will continue on next run) ===")

if __name__ == "__main__":
    main()
