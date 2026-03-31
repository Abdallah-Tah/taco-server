# MEMORY.md

## Identity & human
- Assistant name: Taco.
- Human/owner: Abdallah Mohamed; Taco should call him "Master" when appropriate.
- Environment: Raspberry Pi running OpenClaw with Telegram bot workflow.

## Trading system core rules
- 7 engines total: BTC 15m, ETH 15m, SOL 15m, XRP 15m, Coinbase grid, Solana sniper, auto-redeem.
- Never increase trade size without explicit approval from Master.
- Never touch BTC/ETH when working on other engines unless explicitly instructed.
- BTC/ETH trade size cap: $5 per trade.
- SOL trade size cap: $3 per trade.
- XRP remains dry run unless explicitly changed.
- Gabagool is disabled.
- Coinbase grid target config: live, 2 levels per asset, $10 per level.

## Known recent context (2026-03-25)
- Prior Telegram conversation established that watchdog coverage was intended for all engines, but current local file state should always be verified before trusting chat claims.
- Coinbase grid was intentionally switched from dry run to live in conversation history, though local code/config can drift and must be checked directly.
- SOL sniper has placed real orders before; local logs showed it running but with a non-fatal `journal_old_package` import warning during portfolio checks.
- ETH and SOL had a missing SOCKS dependency issue; fixed locally by installing `httpx[socks]`/`socksio` and restarting only ETH and SOL.

## Operational reminders
- Use local files/logs/process state as source of truth when chat history and machine state disagree.
- Persist important decisions to memory files so context survives resets.

## /report command format (permanent rule)
When Abdallah asks for /report, always use live data and this exact template:

```
📊 TRADING REPORT
Generated: {datetime UTC}

💰 Portfolio: ${total} | Free: ${usdc} | Positions: ${open_val}

BTC-15m:
  Resolved: {total} | W:{wins} L:{losses} | PnL: ${pnl}

ETH-15m:
  Resolved: {total} | W:{wins} L:{losses} | PnL: ${pnl}

Today:
  BTC: {n} trades | W:{w} | ${pnl}
  ETH: {n} trades | W:{w} | ${pnl}

Net today: ${net} | 🟢/🟡/🔴 status
```

Data sources:
- Wallet balance: `scripts/polymarket_executor.py balance` (divide raw by 1e6)
- Open positions: `https://data-api.polymarket.com/positions?user=0x1a4c163a134D7154ebD5f7359919F9c439424f00`
- Trade stats: `journal.db`

Rules:
- Default = current day stats
- If user says "from X to Y" = use that date range for Today section
- Never use cached/stale numbers — always pull live
