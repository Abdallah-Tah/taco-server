# Taco Trading Bot

Autonomous dual-engine trading system for Polymarket prediction markets.

## Engines

| Engine | What | Script |
|--------|------|--------|
| BTC-15m | Bitcoin 15-min snipes | `scripts/polymarket_btc15m.py` |
| ETH-15m | Ethereum 15-min snipes | `scripts/polymarket_eth15m.py` |
| Solana Sniper | Memecoin momentum | `scripts/taco_trader.py` |
| Sentinel | Auto-restart dead processes | `scripts/sentinel.py` |

## Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Configure secrets in ~/.config/openclaw/secrets.env:
# POLYMARKET_WALLET=0x...
# GITHUB_TOKEN=ghp_...
```

## Usage

```bash
# Run a specific engine
python scripts/polymarket_btc15m.py
python scripts/polymarket_eth15m.py

# Daily report
python scripts/analytics.py
```

## Architecture

- `journal.db` — SQLite trade journal (single source of truth)
- `scripts/config.py` — All trading parameters
- `scripts/journal.py` — Trade logging and migration
- `scripts/milestones.py` — Portfolio milestone tracking
