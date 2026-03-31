#!/bin/bash
set -u

WORK="/home/abdaltm86/.openclaw/workspace"
TRADING="$WORK/trading"
VENV="$TRADING/.polymarket-venv/bin/python3"
[ -x "$VENV" ] || VENV="/usr/bin/python3"
SECRETS="$HOME/.config/openclaw/secrets.env"
STATE_DIR="/tmp/polymarket_15m_watchdog"
LOG_FILE="/tmp/polymarket_15m_watchdog.log"
mkdir -p "$STATE_DIR"

TELEGRAM_TOKEN=""
CHAT_ID=""
BTC15M_PAUSED="false"
ETH15M_PAUSED="false"
if [ -f "$SECRETS" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      TELEGRAM_TOKEN) TELEGRAM_TOKEN=$(printf '%s' "$v" | sed "s/^['\"]//;s/['\"]$//") ;;
      CHAT_ID) CHAT_ID=$(printf '%s' "$v" | sed "s/^['\"]//;s/['\"]$//") ;;
      BTC15M_PAUSED) BTC15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
      ETH15M_PAUSED) ETH15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
    esac
  done < "$SECRETS"
fi

send_tg() {
  local msg="$1"
  [ -n "$TELEGRAM_TOKEN" ] || return 0
  [ -n "$CHAT_ID" ] || return 0
  curl -sS -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "{\"chat_id\":\"${CHAT_ID}\",\"text\":$(python3 - <<'PY' "$msg"
import json,sys
print(json.dumps(sys.argv[1]))
PY
)}" >/dev/null 2>&1 || true
}

log() {
  echo "[$(date '+%F %T')] $*" >> "$LOG_FILE"
}

check_engine() {
  local name="$1"
  local script="$2"
  local pidfile="$3"
  local logfile="$4"
  local mode="${5:-python}"
  local statefile="$STATE_DIR/${name}.state"
  local was_down=0
  [ -f "$statefile" ] && grep -q '^down$' "$statefile" && was_down=1

  local running=0
  local pid=""
  if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      running=1
    else
      rm -f "$pidfile"
    fi
  fi
  if [ "$running" -eq 0 ] && pgrep -f "$script" >/dev/null 2>&1; then
    pid=$(pgrep -f "$script" | head -n1)
    echo "$pid" > "$pidfile"
    running=1
  fi

  if [ "$running" -eq 1 ]; then
    echo up > "$statefile"
    if [ "$was_down" -eq 1 ]; then
      log "$name recovered (pid $pid)"
      send_tg "✅ ${name} recovered — running again (PID ${pid})"
    fi
    return 0
  fi

  log "$name DOWN — restarting"
  if [ "$mode" = "shell" ]; then
    nohup "$TRADING/scripts/$script" >> "$logfile" 2>&1 &
  else
    nohup "$VENV" "$TRADING/scripts/$script" >> "$logfile" 2>&1 &
  fi
  local newpid=$!
  echo "$newpid" > "$pidfile"
  sleep 4
  if kill -0 "$newpid" 2>/dev/null; then
    echo down > "$statefile"
    log "$name restarted successfully (pid $newpid)"
    send_tg "🚨 ${name} engine DOWN — restarted automatically (PID ${newpid})"
  else
    echo down > "$statefile"
    log "$name restart FAILED"
    tail -20 "$logfile" >> "$LOG_FILE" 2>/dev/null || true
    send_tg "🚨 ${name} engine DOWN — automatic restart FAILED. Check logs now."
  fi
}

if [ "$BTC15M_PAUSED" != "true" ]; then
  check_engine "BTC-15m" "polymarket_btc15m.py" "/tmp/polymarket_btc15m.pid" "/tmp/polymarket_btc15m.log"
else
  log "BTC-15m paused via BTC15M_PAUSED=true"
fi

if [ "$ETH15M_PAUSED" != "true" ]; then
  check_engine "ETH-15m" "polymarket_eth15m.py" "/tmp/polymarket_eth15m.pid" "/tmp/polymarket_eth15m.log"
else
  log "ETH-15m paused via ETH15M_PAUSED=true"
fi

check_engine "AUTO-REDEEM" "polymarket_auto_redeem_daemon.py" "/tmp/polymarket_auto_redeem.pid" "/tmp/polymarket_auto_redeem.log"
