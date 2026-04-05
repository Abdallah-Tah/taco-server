# Trading Scripts Notes

## Live Shadow Strategy

Current live directional-entry gates promoted from shadow classification:

- `BTC`
  - Shadow keep band: `0.45 <= entry_price <= 0.62`
  - Shadow hard block above: `0.70`
  - Live effect: directional `maker` and `snipe` entries are allowed only when shadow classifies the setup as `kept`
  - No sizing, timing, reconcile, or execution-flow changes were bundled into this promotion

- `ETH`
  - Shadow keep band: `0.55 <= entry_price <= 0.70`
  - Shadow hard block above: `0.80`
  - Live effect: directional `maker` and `snipe` entries are allowed only when shadow classifies the setup as `kept`
  - No sizing or timing changes were bundled into this promotion

## ETH Maker Execution

Current ETH maker execution guardrails:

- Maker pricing is book-aware
- FOK fallback is book-aware
- `ETH15M_MAKER_MAX_RETRIES=1` by default to reduce same-window churn
- Existing price caps still apply

## Current Config Values

These are the current non-secret effective values for BTC and ETH 15m engines. Values come from `secrets.env` where present, otherwise from the code defaults. Secrets are intentionally excluded.

### BTC Effective Values

- Runtime
  - `BTC15M_DRY_RUN=false`
  - `BTC15M_WINDOW_SEC=900`
  - `BTC15M_SCAN_INTERVAL=10`
  - `BTC15M_PRICE_POLL_SEC=5`
- Journal
  - `BTC15M_SHARED_JOURNAL=false`
  - `BTC15M_JOURNAL_DB=/home/abdaltm86/.openclaw/workspace/trading/btc15m_journal.db`
- Arb
  - `BTC15M_ARB_THRESHOLD=0.98`
  - `BTC15M_ARB_SIZE=10.00`
- Direction control
  - `BTC15M_DOWN_ENABLED=false`
- Signal confirmation
  - `BTC15M_SIGNAL_CONFIRM_COUNT=2`
  - `BTC15M_SIGNAL_CONFIRM_SEC=15`
- Entry pricing
  - `BTC15M_SIGNAL_MIN_ENTRY_PRICE=0.38`
  - `BTC15M_SIGNAL_MAX_ENTRY_PRICE=0.65`
  - `BTC15M_SNIPE_MAX_PRICE=0.90`
- Snipe
  - `BTC15M_SNIPE_WINDOW_SEC=30`
  - `BTC15M_SNIPE_DEFAULT_SIZE=5.00`
  - `BTC15M_SNIPE_STRONG_SIZE=5.00`
  - `BTC15M_SNIPE_DELTA_MIN=0.025`
  - `BTC15M_SNIPE_DELTA_MIN_DOWN=0.10`
  - `BTC15M_SNIPE_STRONG_DELTA=0.10`
- Maker
  - `BTC15M_MAKER_ENABLED=true`
  - `BTC15M_MAKER_DRY_RUN=false`
  - `BTC15M_MAKER_START_SEC=540`
  - `BTC15M_MAKER_CANCEL_SEC=10`
  - `BTC15M_MAKER_OFFSET=0.001`
  - `BTC15M_MAKER_POLL_SEC=10`
  - `BTC15M_MAKER_MIN_PRICE=0.01`
  - `BTC15M_MAKER_FOK_FALLBACK_SEC=30`
  - `BTC15M_MAKER_RETRY_MIN_SEC=45`
- Risk
  - `BTC15M_MAX_DAILY_LOSS=15.00`
  - `BTC15M_ROLLING_RISK_ENABLED=true`
  - `BTC15M_ROLLING_RISK_LOOKBACK=20`
  - `BTC15M_ROLLING_RISK_MIN_SAMPLE=10`
  - `BTC15M_ROLLING_RISK_MULTIPLIER=0.60`
  - `BTC15M_ROLLING_RISK_REFRESH_SEC=120`
- Momentum/trend
  - `BTC15M_MOMENTUM_DECEL_THRESHOLD=0.30`
  - `BTC15M_MOMENTUM_MIN_MULTIPLIER=1.0`
- Optional/testing
  - `BTC15M_GABAGOOL_ENABLED=false`
  - `BTC15M_GABAGOOL_DRY_RUN=true`
  - `BTC15M_ONE_SHOT_TEST` unset

### ETH Effective Values

- Runtime
  - `ETH15M_DRY_RUN=false`
  - `ETH15M_WINDOW_SEC=900`
  - `ETH15M_SCAN_INTERVAL=10`
  - `ETH15M_PRICE_POLL_SEC=5`
- Journal
  - `ETH15M_SHARED_JOURNAL=false`
  - `ETH15M_JOURNAL_DB=/home/abdaltm86/.openclaw/workspace/trading/eth15m_journal.db`
- Arb
  - `ETH15M_ARB_THRESHOLD=0.98`
  - `ETH15M_ARB_SIZE=10.00`
- Direction control
  - `ETH15M_DOWN_ENABLED=false`
- Signal confirmation
  - `ETH15M_SIGNAL_CONFIRM_COUNT=2`
  - `ETH15M_SIGNAL_CONFIRM_SEC=15`
- Entry pricing
  - `ETH15M_SIGNAL_MIN_ENTRY_PRICE=0.38`
  - `ETH15M_SIGNAL_MAX_ENTRY_PRICE=0.65`
  - `ETH15M_SNIPE_MIN_PRICE=0.38`
  - `ETH15M_SNIPE_MAX_PRICE=0.65`
  - `ETH15M_MIN_ENTRY_SEC=8`
- Snipe
  - `ETH15M_SNIPE_WINDOW_SEC=45`
  - `ETH15M_SNIPE_DEFAULT_SIZE=5.00`
  - `ETH15M_SNIPE_STRONG_SIZE=5.00`
  - `ETH15M_SNIPE_DELTA_MIN=0.05`
  - `ETH15M_SNIPE_DELTA_MIN_DOWN=0.12`
  - `ETH15M_SNIPE_STRONG_DELTA=0.10`
- Maker
  - `ETH15M_MAKER_ENABLED=true`
  - `ETH15M_MAKER_DRY_RUN=false`
  - `ETH15M_MAKER_START_SEC=540`
  - `ETH15M_MAKER_CANCEL_SEC=10`
  - `ETH15M_MAKER_OFFSET=0.001`
  - `ETH15M_MAKER_POLL_SEC=10`
  - `ETH15M_MAKER_MIN_PRICE=0.45`
  - `ETH15M_MAKER_FOK_FALLBACK_SEC=30`
  - `ETH15M_MAKER_RETRY_MIN_SEC=45`
  - `ETH15M_MAKER_MAX_RETRIES=1`
- Risk
  - `ETH15M_MAX_DAILY_LOSS=15.00`
  - `ETH15M_ROLLING_RISK_ENABLED=true`
  - `ETH15M_ROLLING_RISK_LOOKBACK=20`
  - `ETH15M_ROLLING_RISK_MIN_SAMPLE=10`
  - `ETH15M_ROLLING_RISK_MULTIPLIER=0.60`
  - `ETH15M_ROLLING_RISK_REFRESH_SEC=120`
- Momentum/trend
  - `ETH15M_MOMENTUM_DECEL_THRESHOLD=0.30`
  - `ETH15M_MOMENTUM_MIN_MULTIPLIER=1.0`
  - `ETH15M_HOUR_TREND_THRESHOLD=0.50`
- Late reversal
  - `ETH15M_LATE_REVERSAL_SEC=45`
  - `ETH15M_LATE_REVERSAL_CONFIRM_COUNT=1`
  - `ETH15M_LATE_REVERSAL_MAX_PRICE=0.35`

## Scope

These notes describe the current BTC/ETH 15m live behavior in this folder only.
