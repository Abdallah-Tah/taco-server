# Taco's Trading Knowledge Base

## Core Lesson: Simplicity Wins

After building and backtesting 5 strategies against 6 months of data (-48% bear market):

| # | Strategy | Approach | Result |
|---|----------|----------|--------|
| 1 | TacoGridStrategy | Mean reversion (RSI+BB) | -46% ❌ |
| 2 | TacoTrendStrategy | Trend following (EMA+MACD+ADX) | -22% ❌ |
| 3 | TacoDCAStrategy | **Ultra-selective dip buying** | **+0.7%** ✅ |
| 4 | TacoProStrategy | Advanced multi-signal | -38% ❌ |
| 5 | TacoDCAv2Strategy | DCA + more signals | -46% ❌ |
| 6 | TacoDCAv3Strategy | DCA + custom stoploss | -11% ❌ |

**The ONLY profitable strategy used the simplest, most selective entry criteria.**

## Why TacoDCAStrategy Wins

### Entry Criteria (ALL must be true):
1. Price dropped 3%+ in last 6 hours (`pct_change_6 < -3`)
2. RSI below 30 (extremely oversold)
3. Price at/below lower Bollinger Band (2.5 std dev)
4. Volume 1.5x above 20-period average (panic selling = opportunity)
5. RSI(7) above 15 (not in total freefall — some buyers stepping in)

### Exit Criteria:
- RSI > 72 AND price above middle Bollinger Band
- OR minimal_roi takes profit at tiered levels (8% → 5% → 3% → 2% → 1%)
- OR stoploss at -10%

### Why This Works:
- **Extreme selectivity**: Only 22 trades in 6 months. Quality over quantity.
- **Buys panic**: Volume spike + RSI oversold + price crash = capitulation. Smart money buys here.
- **Patient exits**: Doesn't sell too early. Lets winners run to 8%.
- **Simple stoploss**: -10% flat. No complex trailing that cuts winners.
- **No overcomplication**: No MACD, no trend filters, no custom callbacks. Just RSI + BB + Volume + Price drop.

## Key Trading Principles Learned

1. **Fewer trades = better** in bear markets
2. **Don't add signals** that loosen entry criteria — they let in bad trades
3. **Simple stoploss > custom stoploss** — adaptive stops cut winners too early in volatile crypto
4. **Patience pays** — 81.8% win rate comes from waiting for the best setups
5. **Volume confirms** — panic selling (high volume + low RSI) precedes bounces
6. **Bollinger Bands work** — price tends to revert to the mean, especially after extreme deviations
7. **The market is -48% and this strategy is +0.7%** — that's alpha generation

## Exchange Selection (US-based)

| Exchange | Status | Notes |
|----------|--------|-------|
| Binance | ❌ Blocked (US 451) | Not available |
| Bybit | ❌ Blocked (CloudFront 403) | Not available |
| Gate.io | ✅ Works | Data downloads OK, backtesting works |
| Kraken | ⚠️ Needs `--dl-trades` | Slow data download |

**Gate.io is our working exchange for both backtesting and live trading.**

## Freqtrade Installation

- **Location**: `/home/abdaltm86/.openclaw/workspace/trading/freqtrade`
- **Venv**: `.venv/bin/freqtrade`
- **Version**: 2026.3-dev
- **Data**: `user_data/data/gateio/` (BTC/USDT, ETH/USDT, SOL/USDT — 1h + 1d)
- **Strategies**: `user_data/strategies/`
- **Config**: `user_data/config.json`

## Realistic Income Expectations

With $1,000 starting capital and TacoDCAStrategy:
- Backtested return: +0.72% over 6 months (in -48% market)
- That's ~$7.20 profit on $1,000
- **This is NOT mortgage money.** This is capital preservation + slight growth in a crash.

To generate meaningful income:
- Need $10,000+ capital minimum
- Need bull/sideways market (not -48% crash)
- Need to optimize strategy with hyperopt
- Need to diversify across more pairs
- **Most realistic path**: Use trading as supplementary income, not primary

## Next Steps
1. **Paper trade with dry-run mode** — validate strategy in real-time
2. **Run hyperopt** to optimize parameters (needs more CPU than Pi)
3. **Connect Telegram** for trade notifications
4. **Open Gate.io account** with API keys
5. **Start small** — $100-500 real money after 2+ weeks paper trading
6. **Add more pairs** — expand beyond BTC/ETH/SOL
7. **Consider VPS** — for 24/7 uptime (Pi can work but less reliable)

## Halal Trading Guidelines
- ✅ Spot trading only
- ✅ No leverage/margin (riba)
- ✅ No short selling
- ✅ DCA/dip-buying is permissible (commodity trading)
- ❌ No futures contracts
- ❌ No interest-bearing positions
- ❌ No gambling/speculation without analysis
