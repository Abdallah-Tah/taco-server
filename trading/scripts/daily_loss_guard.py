#!/usr/bin/env python3
"""
daily_loss_guard.py — Safety module for 15m engines

Monitors daily PnL across all 15m engines. If combined daily loss exceeds
DAILY_LOSS_LIMIT ($50), automatically reduces position sizes to FALLBACK_SIZE ($5)
and sends Telegram alert.

State file: ~/.openclaw/workspace/trading/.daily_loss_state.json
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

WORK_DIR = Path("/home/abdaltm86/.openclaw/workspace/trading")
STATE_FILE = WORK_DIR / ".daily_loss_state.json"
DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT", "50.00"))
FALLBACK_SIZE = float(os.environ.get("FALLBACK_SIZE", "5.00"))
NORMAL_SIZE = float(os.environ.get("POLY_DEFAULT_SIZE", "8.00"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4")
CHAT_ID = os.environ.get("CHAT_ID", "7520899464")

def send_telegram(message):
    """Send message to Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def load_state():
    """Load daily loss state."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "date": today,
        "daily_pnl": 0.0,
        "size_reduced": False,
        "original_size": NORMAL_SIZE,
        "current_size": NORMAL_SIZE,
        "loss_trigger_count": 0
    }

def save_state(state):
    """Save daily loss state."""
    STATE_FILE.write_text(json.dumps(state, indent=2))

def check_daily_loss():
    """Check if daily loss limit exceeded."""
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Reset if new day
    if state["date"] != today:
        state = {
            "date": today,
            "daily_pnl": 0.0,
            "size_reduced": False,
            "original_size": NORMAL_SIZE,
            "current_size": NORMAL_SIZE,
            "loss_trigger_count": 0
        }
        save_state(state)
        return state
    
    # Check if loss limit exceeded
    if state["daily_pnl"] <= -DAILY_LOSS_LIMIT and not state["size_reduced"]:
        # Trigger size reduction
        state["size_reduced"] = True
        state["current_size"] = FALLBACK_SIZE
        state["loss_trigger_count"] += 1
        save_state(state)
        
        # Send alert
        msg = f"""🚨 **DAILY LOSS LIMIT REACHED**

Daily PnL: ${state["daily_pnl"]:.2f}
Limit: -${DAILY_LOSS_LIMIT:.2f}

**Action Taken:**
• Position sizes reduced: ${NORMAL_SIZE} → ${FALLBACK_SIZE}
• All 15m engines will trade smaller

This protects capital during rough patches. Sizes reset tomorrow at midnight UTC.

Stay safe, Master. 🎯"""
        send_telegram(msg)
        print(f"LOSS LIMIT HIT: Sizes reduced to ${FALLBACK_SIZE}")
    
    return state

def update_pnl(pnl_change):
    """Update daily PnL with new trade result."""
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    if state["date"] != today:
        state = {
            "date": today,
            "daily_pnl": pnl_change,
            "size_reduced": False,
            "original_size": NORMAL_SIZE,
            "current_size": NORMAL_SIZE,
            "loss_trigger_count": 0
        }
    else:
        state["daily_pnl"] += pnl_change
    
    save_state(state)
    check_daily_loss()
    return state

def get_current_size():
    """Get current allowed position size."""
    state = load_state()
    return state["current_size"]

def reset_sizes():
    """Manually reset sizes to normal (use with caution)."""
    state = load_state()
    state["size_reduced"] = False
    state["current_size"] = NORMAL_SIZE
    save_state(state)
    print(f"Sizes reset to ${NORMAL_SIZE}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "check":
            state = check_daily_loss()
            print(f"Daily PnL: ${state['daily_pnl']:.2f}")
            print(f"Current size: ${state['current_size']}")
            print(f"Size reduced: {state['size_reduced']}")
        elif cmd == "reset":
            reset_sizes()
        elif cmd == "status":
            state = load_state()
            print(json.dumps(state, indent=2))
    else:
        state = check_daily_loss()
        print(f"Daily PnL: ${state['daily_pnl']:.2f} / -${DAILY_LOSS_LIMIT:.2f} limit")
        print(f"Current size: ${state['current_size']}")
