# Trading Research — Taco's Overnight Research (2026-03-13)

## Goal
Help Master generate consistent income to pay mortgage through crypto trading automation.

## ⚠️ Important Disclaimer
- Crypto trading is HIGH RISK. No strategy guarantees profits.
- Never risk money you can't afford to lose.
- Start small, paper trade first, scale only after proven results.
- Master is Muslim — no gambling/speculation. Focus on systematic, data-driven approaches.

## Recommended Approach: Freqtrade (Open Source Crypto Bot)

### Why Freqtrade?
- **Free & open source** — no subscription fees
- **Python-based** — can run on Pi (lightweight strategies) or VPS
- **Dry-run mode** — paper trade first with real market data
- **Backtesting** — test strategies on historical data before risking money
- **FreqAI** — built-in machine learning for adaptive strategies
- **Telegram integration** — manage bot from Telegram (we already have this!)
- **Supports major exchanges**: Binance, Bybit, Kraken, OKX, Gate.io, KuCoin

### Strategies to Explore

#### 1. Grid Trading (Low Risk, Steady Returns)
- Places buy/sell orders at fixed intervals around a price
- Profits from natural price oscillation (sideways markets)
- Works best for BTC/USDT, ETH/USDT in ranging markets
- Expected: 0.5-2% monthly in sideways markets
- Risk: Losses in strong trends (need stop-loss)

#### 2. DCA (Dollar Cost Averaging) Bot
- Buy fixed amounts at regular intervals
- Reduces impact of volatility
- Long-term accumulation strategy
- Not active trading, but reduces risk

#### 3. Mean Reversion
- Buy when price drops below average, sell when above
- Uses RSI, Bollinger Bands, moving averages
- Good for crypto (tends to mean-revert short-term)
- Expected: 1-5% monthly (varies wildly)

#### 4. Trend Following (Moving Average Crossover)
- Buy when 50-day MA crosses above 200-day MA
- Sell when it crosses below
- Simple, proven, but slow signals
- Better for longer timeframes

#### 5. Arbitrage
- Buy on one exchange, sell on another (price differences)
- Very low risk but requires capital on multiple exchanges
- Margins thin (0.1-0.5% per trade)
- Need fast execution

### Realistic Expectations
| Strategy | Monthly Return | Risk Level | Capital Needed |
|----------|---------------|------------|----------------|
| Grid Trading | 0.5-2% | Medium | $1,000+ |
| DCA | Market dependent | Low | Any amount |
| Mean Reversion | 1-5% | Medium-High | $2,000+ |
| Trend Following | 2-10% (but irregular) | Medium | $1,000+ |
| Arbitrage | 0.5-2% | Low | $5,000+ |

### For Mortgage ($1,500-2,500/month typical)
To generate ~$2,000/month:
- Grid @ 1.5%/mo → need ~$133K capital
- Mean reversion @ 3%/mo → need ~$67K capital
- Aggressive strategy @ 5%/mo → need ~$40K capital (HIGH RISK)

**Reality check:** Generating mortgage-level income from trading requires either:
1. Significant capital ($50K+)
2. Higher risk tolerance (leverage, futures)
3. Multiple income streams (trading + other)

## Recommended Plan

### Phase 1: Learn & Paper Trade (Week 1-2)
1. Install Freqtrade on Pi
2. Connect to Binance/Bybit in dry-run mode
3. Run sample strategies on BTC/ETH/SOL
4. Backtest on 6 months historical data
5. Track results daily

### Phase 2: Small Live Trading (Week 3-4)
1. Start with $100-500 real money
2. Grid trading on BTC/USDT (safest)
3. Strict stop-loss (5% max per trade)
4. Monitor daily, adjust weekly

### Phase 3: Scale Up (Month 2+)
1. If profitable, increase capital gradually
2. Add more pairs (ETH, SOL)
3. Test mean reversion strategy
4. Consider VPS for 24/7 uptime

### Phase 4: Optimize (Month 3+)
1. Use FreqAI (machine learning)
2. Custom indicators
3. Multi-strategy portfolio
4. Diversify across exchanges

## Alternative: Hummingbot (Market Making)
- Open-source, Apache 2.0 license
- Focused on market making and arbitrage
- $34B+ volume from users
- More complex but potentially more profitable
- Requires more capital for market making

## Tools Needed
- **Exchange account**: Binance, Bybit, or Kraken (with API keys)
- **Freqtrade**: Free, install via Docker or pip
- **Capital**: Start with what you can afford to lose
- **VPS (optional)**: $5-10/month for 24/7 trading (or use Pi)

## Halal Considerations
- Spot trading only (no futures/margin initially — interest/riba concerns)
- No short selling (selling what you don't own)
- Grid trading on spot is generally considered permissible
- DCA is permissible (just buying crypto)
- Avoid leverage/margin (involves interest)
- Some scholars permit crypto trading as a commodity

## Backtest Results (Sep 2025 - Mar 2026, market -48%)

| Strategy | Trades | Win% | Profit | Max Drawdown | Verdict |
|----------|--------|------|--------|--------------|---------|
| TacoGridStrategy (mean reversion) | 228 | 71.1% | -$460 (-46%) | 48% | ❌ Bad — losses too big |
| TacoTrendStrategy (trend follow) | 138 | 44.9% | -$224 (-22%) | 28% | ⚠️ Better but still loses |
| **TacoDCAStrategy (buy dips)** | **22** | **81.8%** | **+$7 (+0.7%)** | **10%** | **✅ PROFITABLE in bear market!** |

**Key insight:** In a 48% crash, the conservative dip-buying strategy was the ONLY one that made money. Trend following and grid trading both lost. This aligns with the "buy fear, sell greed" principle.

**The DCA strategy needs optimization:**
- More frequent entries (loosen RSI threshold)
- Multi-pair diversification
- Position sizing optimization
- FreqAI machine learning layer

## Next Steps for Master
1. Which exchange do you have an account on? (Binance, Bybit, Kraken?)
2. How much capital can you start with? (be honest, only risk what you can lose)
3. Install Freqtrade and start paper trading
4. I'll build custom strategies and backtest them for you
