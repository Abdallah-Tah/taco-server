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

## Config Surface

The lists below include only non-secret runtime/config keys. They intentionally exclude tokens, chat IDs, wallets, passwords, and other credentials.

### BTC Config

- Runtime: `BTC15M_DRY_RUN`, `BTC15M_WINDOW_SEC`, `BTC15M_SCAN_INTERVAL`, `BTC15M_PRICE_POLL_SEC`
- Journal: `BTC15M_SHARED_JOURNAL`, `BTC15M_JOURNAL_DB`
- Arb: `BTC15M_ARB_THRESHOLD`, `BTC15M_ARB_SIZE`
- Direction control: `BTC15M_DOWN_ENABLED`
- Signal confirmation: `BTC15M_SIGNAL_CONFIRM_COUNT`, `BTC15M_SIGNAL_CONFIRM_SEC`
- Entry pricing: `BTC15M_SIGNAL_MIN_ENTRY_PRICE`, `BTC15M_SIGNAL_MAX_ENTRY_PRICE`, `BTC15M_SNIPE_MAX_PRICE`
- Snipe: `BTC15M_SNIPE_WINDOW_SEC`, `BTC15M_SNIPE_DEFAULT_SIZE`, `BTC15M_SNIPE_STRONG_SIZE`, `BTC15M_SNIPE_DELTA_MIN`, `BTC15M_SNIPE_DELTA_MIN_DOWN`, `BTC15M_SNIPE_STRONG_DELTA`
- Maker: `BTC15M_MAKER_ENABLED`, `BTC15M_MAKER_DRY_RUN`, `BTC15M_MAKER_START_SEC`, `BTC15M_MAKER_CANCEL_SEC`, `BTC15M_MAKER_OFFSET`, `BTC15M_MAKER_POLL_SEC`, `BTC15M_MAKER_MIN_PRICE`, `BTC15M_MAKER_FOK_FALLBACK_SEC`, `BTC15M_MAKER_RETRY_MIN_SEC`
- Risk: `BTC15M_MAX_DAILY_LOSS`, `BTC15M_ROLLING_RISK_ENABLED`, `BTC15M_ROLLING_RISK_LOOKBACK`, `BTC15M_ROLLING_RISK_MIN_SAMPLE`, `BTC15M_ROLLING_RISK_MULTIPLIER`, `BTC15M_ROLLING_RISK_REFRESH_SEC`
- Momentum/trend: `BTC15M_MOMENTUM_DECEL_THRESHOLD`, `BTC15M_MOMENTUM_MIN_MULTIPLIER`
- Optional/testing: `BTC15M_GABAGOOL_ENABLED`, `BTC15M_GABAGOOL_DRY_RUN`, `BTC15M_ONE_SHOT_TEST`

### ETH Config

- Runtime: `ETH15M_DRY_RUN`, `ETH15M_WINDOW_SEC`, `ETH15M_SCAN_INTERVAL`, `ETH15M_PRICE_POLL_SEC`
- Journal: `ETH15M_SHARED_JOURNAL`, `ETH15M_JOURNAL_DB`
- Arb: `ETH15M_ARB_THRESHOLD`, `ETH15M_ARB_SIZE`
- Direction control: `ETH15M_DOWN_ENABLED`
- Signal confirmation: `ETH15M_SIGNAL_CONFIRM_COUNT`, `ETH15M_SIGNAL_CONFIRM_SEC`
- Entry pricing: `ETH15M_SIGNAL_MIN_ENTRY_PRICE`, `ETH15M_SIGNAL_MAX_ENTRY_PRICE`, `ETH15M_SNIPE_MIN_PRICE`, `ETH15M_SNIPE_MAX_PRICE`, `ETH15M_MIN_ENTRY_SEC`
- Snipe: `ETH15M_SNIPE_WINDOW_SEC`, `ETH15M_SNIPE_DEFAULT_SIZE`, `ETH15M_SNIPE_STRONG_SIZE`, `ETH15M_SNIPE_DELTA_MIN`, `ETH15M_SNIPE_DELTA_MIN_DOWN`, `ETH15M_SNIPE_STRONG_DELTA`
- Maker: `ETH15M_MAKER_ENABLED`, `ETH15M_MAKER_DRY_RUN`, `ETH15M_MAKER_START_SEC`, `ETH15M_MAKER_CANCEL_SEC`, `ETH15M_MAKER_OFFSET`, `ETH15M_MAKER_POLL_SEC`, `ETH15M_MAKER_MIN_PRICE`, `ETH15M_MAKER_FOK_FALLBACK_SEC`, `ETH15M_MAKER_RETRY_MIN_SEC`, `ETH15M_MAKER_MAX_RETRIES`
- Risk: `ETH15M_MAX_DAILY_LOSS`, `ETH15M_ROLLING_RISK_ENABLED`, `ETH15M_ROLLING_RISK_LOOKBACK`, `ETH15M_ROLLING_RISK_MIN_SAMPLE`, `ETH15M_ROLLING_RISK_MULTIPLIER`, `ETH15M_ROLLING_RISK_REFRESH_SEC`
- Momentum/trend: `ETH15M_MOMENTUM_DECEL_THRESHOLD`, `ETH15M_MOMENTUM_MIN_MULTIPLIER`, `ETH15M_HOUR_TREND_THRESHOLD`
- Late reversal: `ETH15M_LATE_REVERSAL_SEC`, `ETH15M_LATE_REVERSAL_CONFIRM_COUNT`, `ETH15M_LATE_REVERSAL_MAX_PRICE`

## Scope

These notes describe the current BTC/ETH 15m live behavior in this folder only.
