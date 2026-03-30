#!/usr/bin/env python3
"""Taco Trading Bot - Full Report Generator"""

import sqlite3
import json
import subprocess
import requests
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "journal.db"
POSITIONS_FILE = ROOT / ".positions.json"
WALLET_FILE = ROOT / ".trading_wallet.json"

def get_engine_status(name, patterns):
    """Check if engine is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-fa"] + patterns,
            capture_output=True, text=True, timeout=5
        )
        # Filter out bash -c processes
        lines = [l for l in result.stdout.strip().split('\n') if l and 'bash -c' not in l]
        return "LIVE" if lines else "OFFLINE"
    except:
        return "OFFLINE"

def get_engine_stats(asset_keyword, conn):
    """Get stats for an engine from journal.db - matches asset name."""
    cur = conn.cursor()
    
    # Match by asset name (Bitcoin, Ethereum, Solana, etc.)
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN exit_type = 'filled' THEN 1 ELSE 0 END) as filled,
            SUM(CASE WHEN exit_type IN ('redeemed', 'filled') THEN 1 ELSE 0 END) as resolved,
            SUM(CASE WHEN exit_type IN ('redeemed', 'filled') AND pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN exit_type IN ('redeemed', 'filled') AND pnl_absolute <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(CASE WHEN exit_type IN ('redeemed', 'filled') THEN pnl_absolute ELSE 0 END), 0) as realized_pnl
        FROM trades 
        WHERE asset LIKE ?
    """, (f"%{asset_keyword}%",))
    
    row = cur.fetchone()
    total = row[0] or 0
    filled = row[1] or 0
    resolved = row[2] or 0
    wins = row[3] or 0
    losses = row[4] or 0
    realized = row[5] or 0
    
    # Get posted (all orders placed)
    cur.execute("""
        SELECT COUNT(*) FROM trades WHERE engine LIKE ?
    """, (f"%{asset_keyword}%",))
    posted = cur.fetchone()[0] or 0
    
    fill_rate = (filled / posted * 100) if posted > 0 else 0
    
    # Today's stats
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_absolute <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(CASE WHEN exit_type IN ('redeemed', 'filled') THEN pnl_absolute ELSE 0 END), 0) as pnl
        FROM trades 
        WHERE asset LIKE ? AND date(timestamp_open) = date(?)
    """, (f"%{asset_keyword}%", today))
    
    today_row = cur.fetchone()
    today_total = today_row[0] or 0
    today_wins = today_row[1] or 0
    today_losses = today_row[2] or 0
    today_pnl = today_row[3] or 0
    
    return {
        "posted": posted,
        "filled": filled,
        "fill_rate": fill_rate,
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "realized_pnl": realized,
        "today_total": today_total,
        "today_wins": today_wins,
        "today_losses": today_losses,
        "today_pnl": today_pnl
    }

def get_open_positions(conn):
    """Get count and value of open positions - uses live API via get_capital."""
    # This is now handled by get_capital() which fetches from API
    # Return placeholder - actual values come from capital dict
    return 0, 0.0, 0.0

def get_sniper_positions():
    """Get Solana sniper positions."""
    if not POSITIONS_FILE.exists():
        return 0, 0.0
    
    positions = json.loads(POSITIONS_FILE.read_text())
    return len(positions), sum(p.get("sol", 0) for p in positions)

def get_redeemable(conn):
    """Get total redeemable amount from Polymarket API (not journal)."""
    import requests
    user = "0x1a4c163a134D7154ebD5f7359919F9c439424f00"
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={user}", timeout=20)
        positions = r.json() if r.status_code == 200 else []
        redeemable = sum(p.get('pnl', 0) or 0 for p in positions if p.get('redeemable') == True)
        return redeemable
    except:
        return 0.0

def get_7day_stats(conn):
    """Get 7-day win rate and streak."""
    cur = conn.cursor()
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    cur.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins
        FROM trades 
        WHERE exit_type IN ('redeemed', 'filled') AND date(timestamp_open) >= date(?)
    """, (seven_days_ago,))
    
    row = cur.fetchone()
    total = row[0] or 0
    wins = row[1] or 0
    win_rate = (wins / total * 100) if total > 0 else 0
    
    # Calculate streak (simplified - just check last 5 resolved)
    cur.execute("""
        SELECT pnl_absolute FROM trades 
        WHERE exit_type IN ('redeemed', 'filled')
        ORDER BY timestamp_open DESC LIMIT 5
    """)
    recent = cur.fetchall()
    streak = 0
    streak_type = None
    for r in recent:
        pnl = r[0] or 0
        if pnl > 0:
            if streak_type == "W" or streak_type is None:
                streak += 1
                streak_type = "W"
            else:
                break
        elif pnl <= 0:
            if streak_type == "L" or streak_type is None:
                streak += 1
                streak_type = "L"
            else:
                break
    
    # Determine regime
    if win_rate >= 60:
        regime = "aggressive"
    elif win_rate >= 50:
        regime = "balanced"
    else:
        regime = "defensive"
    
    return win_rate, streak, streak_type, regime

def get_polymarket_balance():
    """Fetch real Polymarket wallet balance from API + on-chain USDC.e query."""
    import requests
    import json
    
    user = "0x1a4c163a134D7154ebD5f7359919F9c439424f00"
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    
    # Get positions from Polymarket API
    try:
        r = requests.get(f"https://data-api.polymarket.com/positions?user={user}", timeout=20)
        positions = r.json() if r.status_code == 200 else []
        open_positions = [p for p in positions if (p.get('currentValue', 0) or 0) > 0 or (p.get('size', 0) or 0) > 0]
        position_value = sum(p.get('currentValue', 0) or 0 for p in positions)
        unrealized_pnl = sum(p.get('cashPnl', 0) or 0 for p in positions)
    except:
        position_value = 0.0
        open_positions = []
        unrealized_pnl = 0.0
    
    # Get USDC.e balance on-chain via multiple RPC endpoints (fallback chain)
    rpc_endpoints = [
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon.drpc.org",
        "https://rpc.ankr.com/polygon",
        "https://137.rpc.thirdweb.com",
    ]
    
    usdc_balance = 0.0
    for rpc_url in rpc_endpoints:
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{
                    "to": USDC_E,
                    "data": "0x70a08231" + user[2:].zfill(64)
                }, "latest"],
                "id": 1
            }
            r = requests.post(rpc_url, json=payload, timeout=10)
            if r.status_code == 200:
                result = r.json()
                balance_hex = result.get("result", "0x0")
                if balance_hex and balance_hex != "0x":
                    usdc_balance = int(balance_hex, 16) / 1_000_000
                    break
        except:
            continue
    
    # Total = USDC cash + position values
    total = usdc_balance + position_value
    
    return total, open_positions, unrealized_pnl, usdc_balance

def get_solana_balance():
    """Fetch real Solana wallet balance from RPC."""
    import requests
    import json
    
    wallet = "WALLET_NOT_CONFIGURED"
    
    try:
        # Get SOL balance
        resp = requests.post(
            "https://api.mainnet-beta.solana.com",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [wallet]
            },
            timeout=20
        )
        data = resp.json()
        sol_lamports = data.get("result", {}).get("value", 0)
        sol_balance = sol_lamports / 1_000_000_000
        
        # Get token positions
        positions_file = ROOT / ".positions.json"
        if positions_file.exists():
            positions = json.loads(positions_file.read_text())
            token_count = len(positions)
        else:
            token_count = 0
        
        return sol_balance, token_count
    except Exception as e:
        print(f"Error fetching Solana balance: {e}")
        return 0.0, 0

def get_capital(conn):
    """Calculate capital breakdown from live wallet APIs."""
    # Get real Polymarket balance from API + on-chain
    poly_total, open_positions, unrealized_pnl, usdc_balance = get_polymarket_balance()
    open_value = sum(p.get('currentValue', 0) or 0 for p in open_positions)
    open_count = len(open_positions)
    
    # Get realized PnL from journal
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(pnl_absolute), 0) FROM trades 
        WHERE exit_type IN ('redeemed', 'filled')
    """)
    realized = cur.fetchone()[0] or 0
    
    # Current = Polymarket total (USDC cash + positions)
    current = poly_total
    free_cash = usdc_balance  # Cash is the USDC balance
    goal = 2780
    progress = (current / goal * 100) if current > 0 else 0
    
    return {
        "current": current,
        "free_cash": free_cash,
        "open_value": open_value,
        "open_count": open_count,
        "goal": goal,
        "progress": progress,
        "realized": realized,
        "unrealized": unrealized_pnl,
        "usdc_balance": usdc_balance
    }

def generate_report():
    """Generate full trading report."""
    conn = sqlite3.connect(DB_PATH)
    
    # Get all stats - match by asset name
    btc_stats = get_engine_stats("Bitcoin", conn)
    eth_stats = get_engine_stats("Ethereum", conn)
    
    btc_status = get_engine_status("BTC", ["polymarket_btc15m.py"])
    eth_status = get_engine_status("ETH", ["polymarket_eth15m.py"])
    sol_status = get_engine_status("SOL", ["polymarket_sol15m.py"])
    
    sniper_count, sniper_sol = get_sniper_positions()
    redeemable = get_redeemable(conn)
    win_rate, streak, streak_type, regime = get_7day_stats(conn)
    capital = get_capital(conn)
    
    # Use capital values directly (from live API)
    open_count = capital.get("open_count", 0)
    
    # Format report
    report = f"""📊 Report, Master

Capital

• Current: ${capital['current']:.2f}
• Free cash: ${capital['free_cash']:.2f}
• Open value: ${capital['open_value']:.2f}
• Goal progress: {capital['progress']:.1f}% (${capital['current']:.2f} / ${capital['goal']})

Results

• BTC realized: +${btc_stats['realized_pnl']:.2f}
• ETH realized: +${eth_stats['realized_pnl']:.2f}
• Combined 15m: +${btc_stats['realized_pnl'] + eth_stats['realized_pnl']:.2f}
• Open 15m positions: {open_count}
• Portfolio unrealized/open PnL: ${capital['unrealized']:.2f} ({capital['unrealized']/capital['current']*100 if capital['current'] > 0 else 0:.1f}%)

7-day

• Win rate: {win_rate:.1f}%
• Streak: {streak}{streak_type or ''}
• Regime: {regime}

Engines

BTC-15m

• {btc_status}
• Posted: {btc_stats['posted']}
• Filled: {btc_stats['filled']}
• Fill rate: {btc_stats['fill_rate']:.1f}%
• Resolved: {btc_stats['resolved']}
• W/L: {btc_stats['wins']} / {btc_stats['losses']}
• Realized PnL: +${btc_stats['realized_pnl']:.2f}

ETH-15m

• {eth_status}
• Posted: {eth_stats['posted']}
• Filled: {eth_stats['filled']}
• Fill rate: {eth_stats['fill_rate']:.1f}%
• Resolved: {eth_stats['resolved']}
• W/L: {eth_stats['wins']} / {eth_stats['losses']}
• Realized PnL: +${eth_stats['realized_pnl']:.2f}

Solana

• {sol_status}
• {sniper_count} positions
• {sniper_sol:.4f} SOL

Redeem

• Redeemable: ${redeemable:.2f}
• {'Nothing to redeem' if redeemable <= 0 else f'{redeemable:.2f} available'}

Daily summary

BTC today

• {btc_stats['today_total']} trades
• {btc_stats['today_wins']}W / {btc_stats['today_losses']}L
• ${btc_stats['today_pnl']:.2f}

ETH today

• {eth_stats['today_total']} trades
• {eth_stats['today_wins']}W / {eth_stats['today_losses']}L
• ${eth_stats['today_pnl']:.2f}

{generate_closing_message(btc_stats, eth_stats)}"""
    
    conn.close()
    return report

def generate_closing_message(btc, eth):
    """Generate closing summary message."""
    total_pnl = btc['today_pnl'] + eth['today_pnl']
    
    if total_pnl > 10:
        return f"Strong day overall, Master — BTC is carrying nicely and combined 15m is now solidly green."
    elif total_pnl > 0:
        return f"Decent day, Master — both engines contributing positively."
    elif total_pnl > -10:
        return f"Flat day, Master — waiting for better setups."
    else:
        return f"Rough day, Master — but we stay disciplined and wait for recovery."

if __name__ == "__main__":
    print(generate_report())
