#!/bin/bash
BTC_LOG=/tmp/polymarket_btc15m.log
ETH_LOG=/tmp/polymarket_eth15m.log
VENV=/home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3
EXEC=/home/abdaltm86/.openclaw/workspace/trading/scripts/polymarket_executor.py

ba=$(grep -c 'signal!' "$BTC_LOG" 2>/dev/null); ba=${ba:-0}
bf=$(grep -c 'filled=True' "$BTC_LOG" 2>/dev/null); bf=${bf:-0}
ea=$(grep -c 'signal!' "$ETH_LOG" 2>/dev/null); ea=${ea:-0}
ef=$(grep -c 'filled=True' "$ETH_LOG" 2>/dev/null); ef=${ef:-0}

bal=$($VENV "$EXEC" balance 2>/dev/null | python3 -c "
import sys,re
m=re.search(r'\"balance\":\s*\"(\d+)\"',sys.stdin.read())
print(f'\${int(m.group(1))/1e6:.2f}' if m else 'error')
")

printf '[HOURLY] BTC: %s attempts, %s fills | ETH: %s attempts, %s fills | wallet: %s\n' "$ba" "$bf" "$ea" "$ef" "$bal"
