#!/bin/bash
# Restart BTC and ETH 15m engines with new fixes

cd ~/.openclaw/workspace/trading

echo "Killing old engines..."
pkill -9 -f polymarket_btc15m.py
pkill -9 -f polymarket_eth15m.py
sleep 3

echo "Checking all processes killed..."
ps aux | grep -E "polymarket_btc15m|polymarket_eth15m" | grep -v grep || echo "All engines stopped"

echo "Starting new engines with Fix 1-8..."
nohup .polymarket-venv/bin/python3 scripts/polymarket_btc15m.py > /tmp/polymarket_btc15m.log 2>&1 &
nohup .polymarket-venv/bin/python3 scripts/polymarket_eth15m.py > /tmp/polymarket_eth15m.log 2>&1 &

sleep 3
echo "Verifying engines are running..."
ps aux | grep -E "polymarket_btc15m|polymarket_eth15m" | grep -v grep

echo "Done! Check logs in /tmp/polymarket_btc15m.log and /tmp/polymarket_eth15m.log"
