#!/usr/bin/env python3
"""
monitor_maker_zone.py - Monitor maker zone entry and order behavior.

Outputs real-time monitoring of maker zone entry and order behavior for BTC/ETH engines.
"""
import json
import time
import subprocess
import sys
from datetime import datetime

BTC_LOG = "/home/abdaltm86/.openclaw/workspace/trading/.poly_btc15m.log"
ETH_LOG = "/home/abdaltm86/.openclaw/workspace/trading/.poly_eth15m.log"

BTC_STATE = "/home/abdaltm86/.openclaw/workspace/trading/.poly_btc15m_state.json"
ETH_STATE = "/home/abdaltm86/.openclaw/workspace/trading/.poly_eth15m_state.json"

MAKER_START_SEC = 300
MAKER_CANCEL_SEC = 60
SCAN_INTERVAL = 5  # seconds between checks


def load_state(engine):
    """Load state from JSON file."""
    try:
        with open(f"/home/abdaltm86/.openclaw/workspace/trading/.poly_{engine}15m_state.json") as f:
            return json.load(f)
    except Exception as e:
        return {}


def get_maker_config(engine):
    """Get maker config from environment or defaults."""
    import os
    prefix = f"{engine.upper()}15M_MAKER_"
    start = int(os.environ.get(f"{prefix}START_SEC", "300"))
    cancel = int(os.environ.get(f"{prefix}CANCEL_SEC", "60"))
    return start, cancel


def watch_maker_zone(engine):
    """Watch for maker zone entry and order behavior."""
    config = get_maker_config(engine)
    start_sec, cancel_sec = config

    print(f"\n=== {engine.upper()}15m Maker Zone Monitor ===")
    print(f"Config: start=T-{start_sec}s, cancel=T-{cancel_sec}s")
    print(f"Waiting for maker zone entry...")
    print()

    last_order_id = None
    order_placed_time = None

    try:
        while True:
            state = load_state(engine)
            window_ts = state.get('window_ts', 0)
            now = int(time.time())
            sec_rem = window_ts + 900 - now

            # Check if we're in the maker zone
            if sec_rem <= start_sec and sec_rem > cancel_sec:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] === MAKER ZONE ENTERED ===")
                print(f"  Seconds remaining: {sec_rem}")

                order_id = state.get('maker_order_id', '')

                if order_id and order_id != last_order_id:
                    order_placed_time = time.time()
                    print(f"  ORDER PLACED: {order_id[:24]}...")
                    print(f"  Side: {state.get('maker_side', 'UNKNOWN')}")
                    print(f"  Price: {state.get('maker_price', 0)}")
                    print(f"  Shares: {state.get('maker_shares', 0)}")
                    last_order_id = order_id
                elif order_id:
                    # Check status
                    cmd = [
                        "/home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3",
                        "/home/abdaltm86/.openclaw/workspace/trading/scripts/polymarket_executor.py",
                        "order_status", order_id
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                    status = 'unknown'
                    for line in result.stdout.splitlines():
                        if '__RESULT__' in line:
                            status_data = json.loads(line.split('__RESULT__')[1])
                            status = status_data.get('status', 'unknown')

                    print(f"[{datetime.now().strftime('%H:%M:%S')}] Order status: {status}")

                    if status == 'filled':
                        print(f"  >>> FILL CONFIRMED! Order held for {time.time() - order_placed_time:.0f}s")
                        return  # Exit on fill
                    elif status == 'not_found':
                        print(f"  >>> Order not found - may have filled or been cancelled")
                        return
                    elif status == 'cancelled':
                        print(f"  >>> Order cancelled by maker")
                        return

                if sec_rem <= cancel_sec:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] approaching cancel threshold ({cancel_sec}s)")

            elif sec_rem <= cancel_sec:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] At cancel threshold ({sec_rem}s) - no active order")
            else:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Window ({sec_rem}s left) - not in maker zone yet")

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        print("\nMonitor stopped by user")


def main():
    if len(sys.argv) < 2:
        print("Usage: python monitor_maker_zone.py <btc|eth>")
        sys.exit(1)

    engine = sys.argv[1].lower()
    if engine not in ('btc', 'eth'):
        print("Invalid engine. Use 'btc' or 'eth'")
        sys.exit(1)

    watch_maker_zone(engine)


if __name__ == "__main__":
    main()
