#!/usr/bin/env python3
"""Counterfactual analysis: BTC-15m with filtering."""
import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path

DB_PATH = Path.home() / ".openclaw" / "workspace" / "trading" / "journal.db"
ENGINE_NAME = "btc15m"

def get_all_trades():
    """Get ALL completed btc15m trades."""
    with sqlite3.connect(DB_PATH) as con:
        query = f"""
        SELECT 
            timestamp_open,
            asset as market_slug,
            entry_price,
            exit_price,
            pnl_absolute as realized_pnl,
            regime
        FROM trades 
        WHERE engine = '{ENGINE_NAME}' 
        AND pnl_absolute IS NOT NULL
        ORDER BY timestamp_open ASC
        """
        df = pd.read_sql_query(query, con)
    
    df['timestamp_open'] = pd.to_datetime(df['timestamp_open'], utc=True, errors='coerce')
    df = df.dropna(subset=['timestamp_open'])
    df['is_win'] = df['realized_pnl'] > 0
    
    # Derive sec_remaining
    import re
    df['window_start'] = df['market_slug'].apply(lambda x: int(re.search(r'btc-updown-15m-(\d{10})', x).group(1)) if re.search(r'btc-updown-15m-(\d{10})', x) else None)
    df['window_end'] = df['window_start'] + 900
    df['entry_ts'] = df['timestamp_open'].apply(lambda x: int(x.timestamp()))
    df['sec_remaining'] = df['window_end'] - df['entry_ts']
    
    return df

def analyze_subset(df, subset_name):
    """Analyze a subset of trades."""
    if len(df) == 0:
        return {
            'subset': subset_name,
            'trades': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'expectancy': 0,
            'total_pnl': 0,
            'profit_factor': 0,
            'ci_lower': 0,
            'ci_upper': 0
        }
    
    wins = df[df['is_win'] == True]
    losses = df[df['is_win'] == False]
    
    n_wins = len(wins)
    n_losses = len(losses)
    total = len(df)
    
    win_rate = n_wins / total if total > 0 else 0
    avg_win = wins['realized_pnl'].mean() if n_wins > 0 else 0
    avg_loss = losses['realized_pnl'].mean() if n_losses > 0 else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss) if total > 0 else 0
    total_pnl = df['realized_pnl'].sum()
    
    # Profit factor
    wins_sum = wins['realized_pnl'].sum() if n_wins > 0 else 0
    losses_sum = abs(losses['realized_pnl'].sum()) if n_losses > 0 else 0
    profit_factor = wins_sum / losses_sum if losses_sum > 0 else float('inf')
    
    # 95% CI
    mean_pnl = df['realized_pnl'].mean()
    std_pnl = df['realized_pnl'].std()
    sem = std_pnl / np.sqrt(total) if total > 0 else 0
    ci_lower = mean_pnl - 1.96 * sem
    ci_upper = mean_pnl + 1.96 * sem
    
    return {
        'subset': subset_name,
        'trades': total,
        'win_rate': win_rate * 100,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'expectancy': expectancy,
        'total_pnl': total_pnl,
        'profit_factor': profit_factor if profit_factor != float('inf') else 999,
        'ci_lower': ci_lower,
        'ci_upper': ci_upper
    }

def main():
    df = get_all_trades()
    
    print("=" * 80)
    print("BTC-15M COUNTERFACTUAL ANALYSIS - FILTERED PERFORMANCE")
    print("=" * 80)
    print()
    
    # Baseline (all trades)
    baseline = analyze_subset(df, "A. BASELINE (all trades)")
    
    # Filter 1: Exclude sec_remaining < 60
    filter1_df = df[df['sec_remaining'] >= 60]
    filter1 = analyze_subset(filter1_df, "B. EXCLUDE late entries (sec_remaining >= 60)")
    
    # Filter 2: Exclude entry_price >= 0.80
    filter2_df = df[df['entry_price'] < 0.80]
    filter2 = analyze_subset(filter2_df, "C. EXCLUDE high prices (entry_price < 0.80)")
    
    # Filter 3: Both filters - sec_remaining >= 60 AND 0.40 <= entry_price < 0.80
    filter3_df = df[(df['sec_remaining'] >= 60) & (df['entry_price'] >= 0.40) & (df['entry_price'] < 0.80)]
    filter3 = analyze_subset(filter3_df, "D. BOTH filters (sec >=60, price 0.40-0.80)")
    
    # Filter 4: Both filters - sec_remaining >= 60 AND 0.40 <= entry_price < 0.60
    filter4_df = df[(df['sec_remaining'] >= 60) & (df['entry_price'] >= 0.40) & (df['entry_price'] < 0.60)]
    filter4 = analyze_subset(filter4_df, "E. BOTH filters (sec >=60, price 0.40-0.60)")
    
    # Combine results
    results = [baseline, filter1, filter2, filter3, filter4]
    
    # Print table
    print(f"{'Subset':<50} {'Trades':<8} {'Win%':<8} {'AvgWin':<8} {'AvgLoss':<9} {'Exp':<8} {'TotalPnL':<10} {'PF':<6} {'95% CI':<20}")
    print("-" * 140)
    
    for r in results:
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != 999 else "∞"
        ci_str = f"[${r['ci_lower']:.2f}, ${r['ci_upper']:.2f}]"
        print(f"{r['subset']:<50} {r['trades']:<8} {r['win_rate']:<8.1f} ${r['avg_win']:<7.2f} ${r['avg_loss']:<8.2f} ${r['expectancy']:<7.2f} ${r['total_pnl']:<9.2f} {pf_str:<6} {ci_str:<20}")
    
    print()
    print("=" * 80)
    print("DETAILED COMPARISON")
    print("=" * 80)
    
    for r in results:
        print()
        print(f"{r['subset']}")
        print(f"  Trades: {r['trades']}")
        print(f"  Win rate: {r['win_rate']:.1f}%")
        print(f"  Avg win: ${r['avg_win']:.2f}")
        print(f"  Avg loss: ${r['avg_loss']:.2f}")
        print(f"  Expectancy: ${r['expectancy']:.2f}")
        print(f"  Total P&L: ${r['total_pnl']:.2f}")
        print(f"  Profit factor: {r['profit_factor']:.2f}" if r['profit_factor'] != 999 else "  Profit factor: ∞")
        print(f"  95% CI: [${r['ci_lower']:.2f}, ${r['ci_upper']:.2f}]")
    
    print()
    print("=" * 80)
    print("KEY FINDINGS")
    print("=" * 80)
    
    # Compare baseline vs best filter
    print()
    print(f"Baseline: PF={baseline['profit_factor']:.2f}, Exp=${baseline['expectancy']:.2f}")
    print(f"Filter D (sec>=60, price 0.40-0.80): PF={filter3['profit_factor']:.2f}, Exp=${filter3['expectancy']:.2f}")
    print(f"Filter E (sec>=60, price 0.40-0.60): PF={filter4['profit_factor']:.2f}, Exp=${filter4['expectancy']:.2f}")
    
    if filter3['profit_factor'] > baseline['profit_factor'] * 1.2:
        print("✓ Filter D shows meaningful improvement in profit factor")
    if filter4['profit_factor'] > baseline['profit_factor'] * 1.2:
        print("✓ Filter E shows meaningful improvement in profit factor")
    if filter3['ci_lower'] > 0:
        print("✓ Filter D has statistically positive mean P&L (CI above zero)")
    if filter4['ci_lower'] > 0:
        print("✓ Filter E has statistically positive mean P&L (CI above zero)")
    
    print()
    print("DECISION TARGET:")
    print("Do these filters turn profit factor from 1.10 into something materially stronger?")
    print(f"Baseline PF: {baseline['profit_factor']:.2f}")
    print(f"Best filtered PF: {max(filter3['profit_factor'], filter4['profit_factor']):.2f}")

if __name__ == "__main__":
    main()
