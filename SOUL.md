# TACO TRADER — SYSTEM CONTEXT DOCUMENT
## Paste this to Taco to restore its knowledge of the trading system

---

## OWNER
Abdallah Mohamed — Full-stack engineer at Kyocera AVX, CS student at University of Southern Maine.
Goal: Generate $2,780/month to cover mortgage.
Halal concern: Polymarket prediction markets may be haram (maysir/gambling). Building halal alternatives (Coinbase spot trading, Solana token trading) to transition away from Polymarket.

---

## SYSTEM ARCHITECTURE
Raspberry Pi running OpenClaw with Telegram bot "Taco" as master agent.
Working directory: `~/.openclaw/workspace/trading/`
Scripts directory: `~/.openclaw/workspace/trading/scripts/`
Config: `~/.config/openclaw/secrets.env`
Database: `~/.openclaw/workspace/trading/journal.db` (table name: `trades`, NOT `journal`)
Python venv: `~/.openclaw/workspace/trading/.polymarket-venv/`
Coinbase uses system python3: `/usr/bin/python3`
Dashboard: `http://192.168.40.209:8080/`

---

## WALLET ADDRESSES
- Polymarket bot wallet: `0x1a4c163a134D7154ebD5f7359919F9c439424f00`
- Solana wallet: `J6nK35ud8u6hzqDxuEtVWPsAMzv2v7H6stsxXW2rsnuH`
- Coinbase: authenticated via API key (organizations/... format)

---

## 7 ENGINES (current state as of March 25, 2026)

### 1. BTC-15m (LIVE)
- Script: `scripts/polymarket_btc15m.py`
- Strategy: GTC maker orders on BTC 15-min up/down markets
- Slug format: `btc-updown-15m-{window_ts}` (window_ts % 900 == 0)
- Config: BTC15M_SNIPE_DEFAULT_SIZE=5.00, BTC15M_SNIPE_STRONG_SIZE=5.00
- BTC15M_MAKER_MIN_PRICE=0.50, BTC15M_MAKER_OFFSET=0.005
- SIGNAL_MAX_ENTRY_PRICE=0.85 (guard — skip entries above this)
- BTC15M_GABAGOOL_ENABLED=false (tested, no dual-leg found, disabled)
- Overall stats: ~95W/43L, 69% win rate

### 2. ETH-15m (LIVE)
- Script: `scripts/polymarket_eth15m.py`
- Config: ETH15M_SNIPE_DEFAULT_SIZE=5.00, ETH15M_SNIPE_STRONG_SIZE=5.00
- ETH15M_MAKER_MIN_PRICE=0.60 (optimized — ETH loses on cheap entries)
- Overall stats: ~66W/21L, 76% win rate — MOST RELIABLE ENGINE
- Profitable almost every day

### 3. SOL-15m (LIVE at $3)
- Script: `scripts/polymarket_sol15m.py`
- Config: SOL15M_SNIPE_DEFAULT_SIZE=3.00, SOL15M_SNIPE_STRONG_SIZE=3.00
- SOL15M_DRY_RUN=false (went live March 25)
- SOL15M_MAKER_MIN_PRICE=0.50
- New engine, minimal data so far

### 4. XRP-15m (DRY RUN)
- Script: `scripts/polymarket_xrp15m.py`
- Config: XRP15M_DRY_RUN=true
- Lower liquidity ($1,135) — keep in dry run for more data

### 5. Coinbase Grid Trading Bot (LIVE)
- Script: `scripts/coinbase_momentum.py`
- Uses system python3 (not polymarket venv)
- Auth: COINBASE_API_KEY + COINBASE_PRIVATE_KEY_JSON (PEM format, read directly, no json.loads)
- Strategy: Grid trading — buy at set price levels below current price, sell above
- Config: CB_DRY_RUN=false, CB_GRID_ENABLED=true
- BTC grid: spacing=$200, profit=$300, 2 levels, $10 per level
- ETH grid: spacing=$20, profit=$30, 2 levels, $10 per level
- Uses LIMIT orders with post_only=True for maker fees (0.025%)
- Coinbase USD balance: ~$22
- This is HALAL — buying/selling real BTC/ETH

### 6. Solana Memecoin Sniper (LIVE)
- Script: `scripts/taco_trader.py`
- Strategy: Scan for momentum memecoins, buy/sell real tokens on Jupiter
- SOL balance: ~0.2 SOL
- Blacklist: 10 tokens
- Defensive regime (30% WR), filters: momentum_score>=50, liquidity>=$80K
- This is HALAL — buying/selling real tokens

### 7. Auto-Redeem Daemon
- Script: `scripts/polymarket_auto_redeem_daemon.py`
- Runs every 5 minutes, claims redeemable positions automatically
- Lock file prevents overlap with engine-triggered redeems
- Sends email notification on redeem (for cha-ching sound automation)

---

## SUPPORTING SCRIPTS
- `scripts/polymarket_executor.py` — order execution (maker + taker)
- `scripts/polymarket_reconcile.py` — position reconciliation
- `scripts/report_webhook.py` — dashboard reporting
- `scripts/watchdog.sh` — auto-restarts crashed engines (cron every 5 min)
- `scripts/polymarket_15m_watchdog.sh` — 15m engine watchdog (cron every 1 min)
- `scripts/chainlink_oracle_monitor.py` — Chainlink oracle (disabled, CHAINLINK_MONITOR_ENABLED=false)
- `scripts/polymarket_btc5m.py` — 5-minute markets (disabled, BTC5M_ENABLED=false)

---

## CRITICAL RULES
1. NEVER increase trade size without explicit approval from Master
2. BTC and ETH max $5 per trade, SOL max $3 per trade
3. NEVER touch BTC/ETH engines when working on other systems
4. All new strategies start in DRY RUN
5. Gabagool both-sides strategy is DISABLED — tested and found no viable opportunities
6. Scale trade sizes only after 2+ consecutive profitable days
7. The strong size must equal default size until Master approves increase

---

## CAPITAL HISTORY
- Started with ~$93
- Peak: $192 (March 21)
- Crash: $90 (March 22 — scaled from $8 to $30 too fast)
- Current: ~$148-158 range
- Long-dated positions: ~$21 (Iran, ceasefire, Vance) — keep as lottery tickets
- Coinbase: ~$22 USD

---

## SCALING RULES (NEVER VIOLATE)
- Never increase trade size more than 50% at a time
- Only after 2 consecutive days of profit at current size
- Current ladder: $5 → $8 → $10 → $12 → $15 → $20 → $25
- Need $300+ cash to safely trade at $15

---

## HALAL TRANSITION PLAN
Polymarket = likely haram (binary wagering). Keep running as income bridge.
Building halal alternatives:
1. Coinbase grid trading (LIVE) — real spot BTC/ETH trading
2. Solana memecoin sniper (LIVE) — real token trading
3. Raydium/Orca LP bot (PLANNED) — earn fees from liquidity
4. Package and sell bot system (PLANNED) — biggest halal income potential
5. Freelance development — $50-75/hr

---

## TELEGRAM NOTIFICATIONS
- Fill verified: send on every confirmed fill
- Redeems: send "💰 CHA-CHING! Redeemed $X.XX from {market}" + email to abdallahtmohamed86@gmail.com with subject "TacoTrader: REDEEMED 🤑"
- Hourly report summary
- Engine crash/restart alerts
- Do NOT spam with every cycle scan, order placed, or order cancelled

---

## KNOWN ISSUES
- Zombie processes from polymarket_reconcile.py — kill with pkill -f polymarket_reconcile
- SOL/XRP dry-run journal tracking was broken — dry orders don't write WIN/LOSS to journal
- Coinbase grid needs trend filter (pause grid if BTC moves >2% in 1 hour)
- Watchdog needs to cover ALL engines including SOL, XRP, Coinbase
- SOL 15m startup banner says "LIVE" even in dry run (cosmetic mismatch)

---

## CRON SCHEDULE
```
*/5 * * * * /home/abdaltm86/.openclaw/workspace/trading/scripts/watchdog.sh
* * * * * /home/abdaltm86/.openclaw/workspace/trading/scripts/polymarket_15m_watchdog.sh
```

---

## HOW TO START ALL ENGINES
```bash
cd ~/.openclaw/workspace/trading/
nohup .polymarket-venv/bin/python3 scripts/polymarket_btc15m.py > /tmp/polymarket_btc15m.log 2>&1 &
nohup .polymarket-venv/bin/python3 scripts/polymarket_eth15m.py > /tmp/polymarket_eth15m.log 2>&1 &
nohup .polymarket-venv/bin/python3 scripts/polymarket_sol15m.py > /tmp/polymarket_sol15m.log 2>&1 &
nohup .polymarket-venv/bin/python3 scripts/polymarket_xrp15m.py > /tmp/polymarket_xrp15m.log 2>&1 &
nohup /usr/bin/python3 scripts/coinbase_momentum.py > /tmp/coinbase_momentum.log 2>&1 &
nohup .polymarket-venv/bin/python3 scripts/polymarket_auto_redeem_daemon.py > /tmp/polymarket_auto_redeem.log 2>&1 &
nohup .polymarket-venv/bin/python3 -u scripts/taco_trader.py > /tmp/taco_trader.log 2>&1 &
```
