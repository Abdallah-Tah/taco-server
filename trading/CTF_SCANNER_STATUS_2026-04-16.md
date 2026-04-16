# CTF Scanner Status — 2026-04-16

## Current state
- Live CTF trading: **STOPPED**
- Current mode: **scanner-first / dry-run / no-loss path**
- Scanner purpose: evaluate BTC 15m windows without splitting, posting orders, or burning gas

## What was fixed
### 1. Executor size ceiling
- Added a CTF-only max-trade-size override for sell commands
- Normal engine caps remain unchanged

### 2. Redeem bug
- `polymarket_redeem.py` previously grouped redeemables by `conditionId` and overwrote one side with the other
- This could redeem only one side of a failed-merge CTF window
- Fixed so redeem now groups **all positions per condition** and redeems **all held outcome sides** together

### 3. Sane profile / scanner-first logic
- CTF script now defaults to scanner-first evaluation
- No split / no orders / no gas in scanner mode
- Reports only:
  - rewards min size
  - spread quality
  - bid quality
  - pair balance
  - safe post-only quoteability
  - combined YES+NO threshold
  - verdict: `TRADEABLE` or `SKIP`

## Important lessons from live tests
- The 8:00 window returned principal cleanly
- The 8:30 window exposed a real failure mode after non-scoring abort + failed merge
- Because of that, live CTF should remain off until scanner data proves repeatable edge

## Current recommended operating mode
- `CTF_REBATE_SCAN_ONLY=true`
- `CTF_REBATE_DRY_RUN=true`
- Asset focus: `btc`
- Evaluation size: `$50`

## Goal
Find only windows where a future live version could plausibly achieve:
- principal back
- small gain
- low drama

## Files changed
- `trading/scripts/polymarket_ctf_rebate.py`
- `trading/scripts/polymarket_executor.py`
- `trading/scripts/polymarket_redeem.py`

## Next recommended step
Collect scanner evidence across multiple BTC windows first.
Only restart live CTF after the scanner shows consistently tradeable windows.
