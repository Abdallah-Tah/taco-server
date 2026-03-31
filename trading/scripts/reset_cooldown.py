#!/usr/bin/env python3
import os
import json
import time
from pathlib import Path

STATE_FILE = Path.home() / ".openclaw" / "workspace" / "trading" / ".poly_btc15m.json"

def main():
    if not STATE_FILE.exists():
        print("State file not found. Creating a default one.")
        state = {
          "window_ts": 0,
          "window_open_btc": 0,
          "cooldown_until": 0,
          "consecutive_losses": 0
        }
    else:
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
        except json.JSONDecodeError:
            print("State file is corrupted. Creating a default one.")
            state = {
              "window_ts": 0,
              "window_open_btc": 0,
              "cooldown_until": 0,
              "consecutive_losses": 0
            }

    # Set cooldown_until to a negative timestamp to signal a manual reset
    # The main script will ignore this for one cycle then reset it
    state['cooldown_until'] = -(time.time() + 900) # Lockout new auto-cooldowns for 15m
    state['consecutive_losses'] = 0

    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

    print("Cooldown reset. Will take effect on next cycle.")
    print(f"Auto-cooldowns disabled for ~15 minutes.")

if __name__ == "__main__":
    main()
