#!/bin/bash
set -u

WORK="/home/abdaltm86/.openclaw/workspace/trading"
PYTHON_BIN="$(command -v python3)"
SCRIPT="$WORK/scripts/coinbase_momentum.py"
PIDFILE="/tmp/coinbase_momentum.pid"
LOGFILE="/tmp/coinbase_momentum.log"

log() {
  echo "[$(date '+%F %T')] $*" >> /tmp/coinbase_watchdog.log
}

running=0
pid=""
if [ -f "$PIDFILE" ]; then
  pid=$(cat "$PIDFILE" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    running=1
  fi
fi

if [ "$running" -eq 0 ] && pgrep -f "$SCRIPT" >/dev/null 2>&1; then
  pid=$(pgrep -f "$SCRIPT" | head -n1)
  echo "$pid" > "$PIDFILE"
  running=1
fi

if [ "$running" -eq 1 ]; then
  log "coinbase_momentum ok pid=$pid"
  exit 0
fi

log "coinbase_momentum down — restarting"
nohup env CB_DRY_RUN=${CB_DRY_RUN:-true} "$PYTHON_BIN" "$SCRIPT" >> "$LOGFILE" 2>&1 &
newpid=$!
echo "$newpid" > "$PIDFILE"
sleep 3
if kill -0 "$newpid" 2>/dev/null; then
  log "coinbase_momentum restarted pid=$newpid"
  exit 0
else
  log "coinbase_momentum restart FAILED"
  exit 1
fi
