#!/usr/bin/env python3
"""
Taco Shitcoin Sniper — Buys trending pump.fun tokens on Solana.
Uses Jupiter aggregator for best swap rates.
"""
import json
import time
import requests
from pathlib import Path
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client

WALLET_FILE = Path(__file__).parent.parent / ".trading_wallet.json"
RPC = "https://api.mainnet-beta.solana.com"
JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
SOL_MINT = "So11111111111111111111111111111111111111112"

def load_wallet():
    data = json.loads(WALLET_FILE.read_text())
    secret = bytes(data["secret_key"])
    kp = Keypair.from_bytes(secret)
    return kp

def get_balance(pubkey_str):
    client = Client(RPC)
    bal = client.get_balance(Pubkey.from_string(pubkey_str))
    return bal.value / 1_000_000_000

def get_quote(input_mint, output_mint, amount_lamports):
    """Get Jupiter swap quote."""
    r = requests.get(JUPITER_QUOTE, params={
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": "500",  # 5% slippage for shitcoins
    })
    if r.status_code != 200:
        print(f"Quote error: {r.status_code} {r.text[:200]}")
        return None
    return r.json()

def execute_swap(quote, keypair):
    """Execute Jupiter swap."""
    r = requests.post(JUPITER_SWAP, json={
        "quoteResponse": quote,
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True,
    })
    if r.status_code != 200:
        print(f"Swap error: {r.status_code} {r.text[:200]}")
        return None
    
    swap_data = r.json()
    swap_tx = swap_data.get("swapTransaction")
    if not swap_tx:
        print(f"No swap transaction returned: {swap_data}")
        return None
    
    # Deserialize, sign, and send
    import base64
    from solders.transaction import VersionedTransaction
    from solana.rpc.api import Client
    
    tx_bytes = base64.b64decode(swap_tx)
    tx = VersionedTransaction.from_bytes(tx_bytes)
    
    # Sign the transaction
    signed_tx = VersionedTransaction(tx.message, [keypair])
    
    client = Client(RPC)
    result = client.send_transaction(signed_tx)
    return result

def scan_trending():
    """Find best token to buy right now."""
    r = requests.get("https://api.dexscreener.com/token-boosts/top/v1")
    boosts = r.json()
    
    candidates = []
    for t in boosts:
        if t.get("chainId") != "solana":
            continue
        addr = t["tokenAddress"]
        
        r2 = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}")
        pairs = r2.json().get("pairs", [])
        if not pairs:
            continue
        
        p = pairs[0]
        liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
        vol24 = float(p.get("volume", {}).get("h24", 0) or 0)
        chg1h = float(p.get("priceChange", {}).get("h1", 0) or 0)
        chg24 = float(p.get("priceChange", {}).get("h24", 0) or 0)
        mcap = float(p.get("marketCap", 0) or p.get("fdv", 0) or 0)
        txns = p.get("txns", {}).get("h24", {})
        buys = int(txns.get("buys", 0))
        sells = int(txns.get("sells", 0))
        symbol = p.get("baseToken", {}).get("symbol", "?")
        
        # Scoring
        score = 0
        
        # Liquidity (need enough to exit)
        if liq < 10000: continue  # skip illiquid
        if liq > 50000: score += 20
        elif liq > 20000: score += 10
        
        # Volume (active trading)
        if vol24 > 500000: score += 20
        elif vol24 > 100000: score += 10
        
        # Momentum (rising, not dumping)
        if 5 < chg1h < 100: score += 15  # positive but not parabolic
        if chg1h < -20: continue  # dumping, skip
        
        # Buy pressure
        if buys > sells * 1.2: score += 10
        
        # Not a rug (mcap not too tiny)
        if mcap > 100000: score += 5
        
        # Not already mooned too hard
        if chg24 > 2000: continue  # already 20x, late entry
        
        candidates.append({
            "symbol": symbol,
            "mint": addr,
            "score": score,
            "liq": liq,
            "vol24": vol24,
            "chg1h": chg1h,
            "chg24": chg24,
            "mcap": mcap,
            "buys": buys,
            "sells": sells,
        })
    
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates

def buy_token(mint, sol_amount, keypair):
    """Buy a token with SOL via Jupiter."""
    lamports = int(sol_amount * 1_000_000_000)
    
    print(f"Getting quote: {sol_amount} SOL → {mint[:20]}...")
    quote = get_quote(SOL_MINT, mint, lamports)
    if not quote:
        return None
    
    out_amount = quote.get("outAmount", "?")
    print(f"Quote: {out_amount} tokens for {sol_amount} SOL")
    
    print("Executing swap...")
    result = execute_swap(quote, keypair)
    return result

def sell_token(mint, keypair, fraction=1.0):
    """Sell some or all of a token back to SOL via Jupiter."""
    fraction = max(0.0, min(1.0, float(fraction)))
    # Get token balance
    from solana.rpc.api import Client
    client = Client(RPC)
    
    from solana.rpc.types import TokenAccountOpts
    r = client.get_token_accounts_by_owner_json_parsed(
        keypair.pubkey(),
        TokenAccountOpts(mint=Pubkey.from_string(mint))
    )
    accounts = r.value
    if not accounts:
        print("No tokens to sell")
        return None
    
    balance_raw = int(accounts[0].account.data.parsed["info"]["tokenAmount"]["amount"])
    if balance_raw == 0:
        print("Zero balance")
        return None

    amount_raw = int(balance_raw * fraction)
    if amount_raw <= 0:
        print(f"Sell amount too small for fraction={fraction}")
        return None
    
    print(f"Selling {amount_raw} / {balance_raw} tokens ({fraction:.2%}) of {mint[:20]}...")
    quote = get_quote(mint, SOL_MINT, amount_raw)
    if not quote:
        return None
    
    result = execute_swap(quote, keypair)
    return result

if __name__ == "__main__":
    import sys
    
    kp = load_wallet()
    pub = str(kp.pubkey())
    bal = get_balance(pub)
    print(f"🌮 Taco Sniper Bot")
    print(f"Wallet: {pub}")
    print(f"Balance: {bal:.6f} SOL (~${bal * 89:.2f})")
    print()
    
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        print("🔍 Scanning trending tokens...\n")
        candidates = scan_trending()
        for c in candidates[:10]:
            print(f"Score: {c['score']:3d} | {c['symbol']:12s} | 1h: {c['chg1h']:>7}% | 24h: {c['chg24']:>7}%")
            print(f"  Liq: ${c['liq']:>10,.0f} | Vol: ${c['vol24']:>10,.0f} | B/S: {c['buys']}/{c['sells']}")
            print(f"  Mint: {c['mint']}")
            print()
    
    elif len(sys.argv) > 1 and sys.argv[1] == "buy":
        if len(sys.argv) < 4:
            print("Usage: sniper.py buy <mint> <sol_amount>")
            sys.exit(1)
        mint = sys.argv[2]
        amount = float(sys.argv[3])
        result = buy_token(mint, amount, kp)
        print(f"Result: {result}")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "sell":
        if len(sys.argv) < 3:
            print("Usage: sniper.py sell <mint> [fraction]")
            sys.exit(1)
        mint = sys.argv[2]
        fraction = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        result = sell_token(mint, kp, fraction=fraction)
        print(f"Result: {result}")
    
    elif len(sys.argv) > 1 and sys.argv[1] == "auto":
        print("🤖 AUTO MODE — scanning and trading...\n")
        # Reserve 0.01 SOL for fees
        tradeable = bal - 0.01
        if tradeable < 0.01:
            print("Not enough SOL to trade")
            sys.exit(1)
        
        # Split into 3 positions max
        per_trade = tradeable / 3
        
        candidates = scan_trending()
        bought = []
        
        for c in candidates[:3]:
            if per_trade < 0.01:
                break
            print(f"🎯 Buying {c['symbol']} with {per_trade:.4f} SOL...")
            result = buy_token(c["mint"], per_trade, kp)
            if result:
                bought.append({"symbol": c["symbol"], "mint": c["mint"], "sol": per_trade})
                print(f"✅ Bought {c['symbol']}")
            else:
                print(f"❌ Failed to buy {c['symbol']}")
            time.sleep(2)
        
        if bought:
            print(f"\n📊 Positions: {json.dumps(bought, indent=2)}")
            Path(__file__).parent.parent.joinpath(".positions.json").write_text(json.dumps(bought, indent=2))
    
    else:
        print("Commands: scan | buy <mint> <sol> | sell <mint> [fraction] | auto")
