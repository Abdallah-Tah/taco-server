#!/bin/bash
set -u

# ── DISABLED ENGINES (Phase 1 cleanup, 2026-04-30) ──
# These engines must NOT be auto-restarted by this watchdog.
# To re-enable an engine:
#   1. Remove it from DISABLED_ENGINES below
#   2. Get Master approval
DISABLED_ENGINES="SOL15M XRP15M DOGE15M COINBASE_GRID COINBASE_EMA SOLANA_SNIPER"
# ─────────────────────────────────────────────────────────────

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
SOL15M_PAUSED="false"
XRP15M_PAUSED="false"
DOGE15M_PAUSED="false"
if [ -f "$SECRETS" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      TELEGRAM_TOKEN) TELEGRAM_TOKEN=$(printf '%s' "$v" | sed "s/^['\"]//;s/['\"]$//") ;;
      CHAT_ID) CHAT_ID=$(printf '%s' "$v" | sed "s/^['\"]//;s/['\"]$//") ;;
      BTC15M_PAUSED) BTC15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
      ETH15M_PAUSED) ETH15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
      SOL15M_PAUSED) SOL15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
      XRP15M_PAUSED) XRP15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
      DOGE15M_PAUSED) DOGE15M_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
    esac
  done < "$SECRETS"
fi

send_tg() {
  local msg="$1"
  [ -n "$TELEGRAM_TOKEN" ] || return 0
  [ -n "$CHAT_ID" ] || return 0
  curl -sS -m 10 -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "{\"chat_id\":\"-1003948211258\",\"message_thread_id\":3,\"text\":$(python3 - <<'PY' "$msg"
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
    nohup env -u BTC15M_SIGNAL_MAX_ENTRY_PRICE -u ETH15M_SIGNAL_MAX_ENTRY_PRICE -u BTC15M_DOWN_ENABLED -u BTC15M_UP_ENABLED -u ETH15M_DOWN_ENABLED -u ETH15M_UP_ENABLED -u SOL15M_DOWN_ENABLED -u SOL15M_UP_ENABLED -u DOGE15M_DOWN_ENABLED -u DOGE15M_UP_ENABLED -u POLYMARKET_CLOB_FUNDER -u POLYMARKET_CLOB_SIGNATURE_TYPE -u POLYMARKET_DEPOSIT_WALLET -u WEATHER_BOUNDARY_MARGIN_SIMPLE -u WEATHER_BOUNDARY_MARGIN_RANGE "$TRADING/scripts/$script" >> "$logfile" 2>&1 &
  else
    nohup env -u BTC15M_SIGNAL_MAX_ENTRY_PRICE -u ETH15M_SIGNAL_MAX_ENTRY_PRICE -u BTC15M_DOWN_ENABLED -u BTC15M_UP_ENABLED -u ETH15M_DOWN_ENABLED -u ETH15M_UP_ENABLED -u SOL15M_DOWN_ENABLED -u SOL15M_UP_ENABLED -u DOGE15M_DOWN_ENABLED -u DOGE15M_UP_ENABLED -u POLYMARKET_CLOB_FUNDER -u POLYMARKET_CLOB_SIGNATURE_TYPE -u POLYMARKET_DEPOSIT_WALLET -u WEATHER_BOUNDARY_MARGIN_SIMPLE -u WEATHER_BOUNDARY_MARGIN_RANGE "$VENV" "$TRADING/scripts/$script" >> "$logfile" 2>&1 &
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

# ═══════════════════════════════════════════════════════════════
#  APPROVED ENGINES — auto-restart enabled
# ═══════════════════════════════════════════════════════════════

# ── BTC-15m (LIVE) ──
if [ "$BTC15M_PAUSED" != "true" ]; then
  check_engine "BTC-15m" "polymarket_btc15m.py" "/tmp/polymarket_btc15m.pid" "/tmp/polymarket_btc15m.log"
else
  log "BTC-15m paused via BTC15M_PAUSED=true"
fi

# ── ETH-15m (LIVE) ──
if [ "$ETH15M_PAUSED" != "true" ]; then
  check_engine "ETH-15m" "polymarket_eth15m.py" "/tmp/polymarket_eth15m.pid" "/tmp/polymarket_eth15m.log"
else
  log "ETH-15m paused via ETH15M_PAUSED=true"
fi

# ── AUTO-REDEEM ──
check_engine "AUTO-REDEEM" "polymarket_auto_redeem_daemon.py" "/tmp/polymarket_auto_redeem.pid" "/tmp/polymarket_auto_redeem.log"

# ── Weather v2.1 ──
WEATHER_PAUSED="false"
if [ -f "$SECRETS" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      WEATHER_PAUSED) WEATHER_PAUSED=$(printf '%s' "$v" | tr '[:upper:]' '[:lower:]' | sed "s/^['\"]//;s/['\"]$//") ;;
    esac
  done < "$SECRETS"
fi
if [ "$WEATHER_PAUSED" != "true" ]; then
  export WEATHER_DRY_RUN=false
  export WEATHER_LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY
  check_engine "WEATHER" "polymarket_weather.py" "/tmp/polymarket_weather.pid" "/tmp/polymarket_weather.log"
  unset WEATHER_DRY_RUN
  unset WEATHER_LIVE_CONFIRM
else
  log "WEATHER paused via WEATHER_PAUSED=true"
fi

# ═══════════════════════════════════════════════════════════════
#  DISABLED ENGINES — auto-restart blocked
#  To re-enable: remove from DISABLED_ENGINES at top of file
# ═══════════════════════════════════════════════════════════════

# ── SOL-15m (DISABLED per Phase 1) ──
if [[ " $DISABLED_ENGINES " =~ " SOL15M " ]]; then
  log "SOL-15m disabled (Phase 1 cleanup)"
else
  log "SOL-15m would restart but DISABLED_ENGINES gate removed — WARNING"
fi

# ── XRP-15m (DISABLED per Phase 1) ──
if [[ " $DISABLED_ENGINES " =~ " XRP15M " ]]; then
  log "XRP-15m disabled (Phase 1 cleanup)"
else
  log "XRP-15m would restart but DISABLED_ENGINES gate removed — WARNING"
fi

# ── DOGE-15m (DISABLED per Phase 1) ──
if [[ " $DISABLED_ENGINES " =~ " DOGE15M " ]]; then
  log "DOGE-15m disabled (Phase 1 cleanup)"
else
  log "DOGE-15m would restart but DISABLED_ENGINES gate removed — WARNING"
fi

# ── AERO Grid (DISABLED per Phase 1) ──
if [[ " $DISABLED_ENGINES " =~ " AERO_GRID " ]]; then
  log "AERO-GRID disabled (Phase 1 cleanup)"
else
  log "AERO-GRID would restart but DISABLED_ENGINES gate removed — WARNING"
fi

# ── Coinbase Grid (DISABLED per Phase 1) ──
if [[ " $DISABLED_ENGINES " =~ " COINBASE_GRID " ]]; then
  log "CB-GRID disabled (Phase 1 cleanup)"
else
  log "CB-GRID would restart but DISABLED_ENGINES gate removed — WARNING"
fi
