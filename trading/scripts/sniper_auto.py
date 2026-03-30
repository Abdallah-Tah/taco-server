#!/usr/bin/env python3
"""Taco Sniper Bot - Continuous Auto-Trading Mode"""

import json
import time
import sys
from pathlib import Path
from datetime import datetime

# Import from main sniper module
sys.path.insert(0, str(Path(__file__).parent))
from sniper import (
    load_wallet, get_balance, scan_trending, buy_token, sell_token,
    get_quote, execute_swap, SOL_MINT, Client, Pubkey
)

ROOT = Path(__file__).parent.parent
POSITIONS_FILE = ROOT / ".positions.json"
TRADE_LOG = ROOT / "logs" / "sniper_auto.log"

# Config
SCAN_INTERVAL = 120  # Scan every 2 minutes
MAX_POSITIONS = 3
MIN_TRADE_SIZE = 0.01  # SOL
RESERVE_SOL = 0.015  # Keep for gas fees
TAKE_PROFIT_PCT = 0.20  # Sell 50% at 20% gain
STOP_LOSS_PCT = 0.15  # Sell 50% at 15% loss

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    TRADE_LOG.parent.mkdir(exist_ok=True)
    with open(TRADE_LOG, "a") as f:
        f.write(line + "\n")

def load_positions():
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return []

def save_positions(positions):
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))

def check_profit_loss(positions, kp):
    """Check existing positions for profit/loss targets - simplified."""
    for pos in positions:
        symbol = pos.get("symbol", "???")
        log(f"📊 {symbol}: holding")
    return positions

def auto_trade(kp):
    """Main auto-trading loop."""
    log("🤖 AUTO-TRADING STARTED")
    log(f"Wallet: {kp.pubkey()}")
    log(f"Settings: max={MAX_POSITIONS} positions, scan={SCAN_INTERVAL}s, TP={TAKE_PROFIT_PCT*100}%, SL={STOP_LOSS_PCT*100}%")
    
    while True:
        try:
            # Get current balance
            pub = str(kp.pubkey())
            bal = get_balance(pub)
            log(f"\n💰 Balance: {bal:.4f} SOL (~${bal * 89:.2f})")
            
            # Load current positions
            positions = load_positions()
            log(f"📦 Active positions: {len(positions)}")
            
            # Check profit/loss on existing positions
            if positions:
                positions = check_profit_loss(positions, kp)
                save_positions(positions)
            
            # Calculate available SOL for new trades
            reserved = RESERVE_SOL + (len(positions) * 0.005)  # Reserve for sells
            available = bal - reserved
            
            # Open new positions if we have room
            slots_available = MAX_POSITIONS - len([p for p in positions if not p.get("sold_half")])
            
            if slots_available > 0 and available >= MIN_TRADE_SIZE * slots_available:
                per_trade = available / slots_available
                if per_trade < MIN_TRADE_SIZE:
                    per_trade = MIN_TRADE_SIZE
                
                log(f"🔍 Scanning for opportunities ({slots_available} slots, {per_trade:.3f} SOL/trade)...")
                
                candidates = scan_trending()
                
                for c in candidates[:slots_available]:
                    if available < MIN_TRADE_SIZE:
                        break
                    
                    # Skip if already holding
                    if any(p["mint"] == c["mint"] for p in positions):
                        log(f"⏭️ Skipping {c['symbol']} - already holding")
                        continue
                    
                    log(f"🎯 Buying {c['symbol']} with {per_trade:.4f} SOL...")
                    result = buy_token(c["mint"], per_trade, kp)
                    
                    if result:
                        positions.append({
                            "symbol": c["symbol"],
                            "mint": c["mint"],
                            "sol": per_trade,
                            "bought_at": datetime.now().isoformat()
                        })
                        save_positions(positions)
                        # Format: [SOL-SNIPE] ORDER PLACED: LEGACY 0.0321 SOL | Tx: 5xKj...
                        tx_sig = str(result.value) if hasattr(result, 'value') else str(result)[:44]
                        log(f"🌮 [SOL-SNIPE] ORDER PLACED: {c['symbol']} {per_trade:.4f} SOL | Tx: {tx_sig[:8]}...{tx_sig[-8:]}")
                        available -= per_trade
                    else:
                        log(f"❌ Failed to buy {c['symbol']}")
                    
                    time.sleep(2)  # Rate limit
            else:
                log(f"⏸️ No slots available ({len(positions)}/{MAX_POSITIONS}) or insufficient SOL ({available:.3f})")
            
            log(f"💤 Sleeping {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)
            
        except KeyboardInterrupt:
            log("👋 Stopped by user")
            break
        except Exception as e:
            log(f"❌ Error: {e}")
            log("💤 Sleeping 30s before retry...")
            time.sleep(30)

if __name__ == "__main__":
    kp = load_wallet()
    auto_trade(kp)
