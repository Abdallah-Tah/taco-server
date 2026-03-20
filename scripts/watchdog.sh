#!/bin/bash
# Taco Trader Watchdog — restarts trader if it dies
LOGFILE="/tmp/taco_trader.log"
PIDFILE="/tmp/taco_trader.pid"

if ! pgrep -f "taco_trader.py" > /dev/null 2>&1; then
    echo "[$(date)] Trader not running — restarting..." >> /tmp/taco_watchdog.log
    cd /home/abdaltm86/.openclaw/workspace/trading
    setsid /home/abdaltm86/.openclaw/workspace/trading/.polymarket-venv/bin/python3 -u scripts/taco_trader.py >> "$LOGFILE" 2>&1 &
    disown
    echo "[$(date)] Restarted PID: $!" >> /tmp/taco_watchdog.log
else
    echo "[$(date)] Trader OK (PID: $(pgrep -f taco_trader.py))" >> /tmp/taco_watchdog.log
fi
