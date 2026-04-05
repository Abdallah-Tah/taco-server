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

## Scope

These notes describe the current BTC/ETH 15m live behavior in this folder only.
