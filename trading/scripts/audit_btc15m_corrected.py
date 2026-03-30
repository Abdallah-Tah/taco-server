#!/usr/bin/env python3
"""Corrected BTC-15m audit using FULL trade distribution (wins + losses)."""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import json

DB_PATH = Path.home() / ".openclaw" / "workspace" / "trading" / "journal.db"
ENGINE_NAME = "btc15m"

def get_all_trades():
    """Get ALL completed btc15m trades including wins AND losses."""
    with sqlite3.connect(DB_PATH) as con:
        # Get all btc15m trades with non-null pnl_absolute (completed trades)
        query = f"""
        SELECT 
            timestamp_open,
            asset as market_slug,
            entry_price,
            exit_price,
            pnl_absolute as realized_pnl,
            pnl_percent,
            hold_duration_seconds,
            regime,
            notes
        FROM trades 
        WHERE engine = '{ENGINE_NAME}' 
        AND pnl_absolute IS NOT NULL
        ORDER BY timestamp_open ASC
        """
        df = pd.read_sql_query(query, con)
    
    # Parse timestamps
    df['timestamp_open'] = pd.to_datetime(df['timestamp_open'], utc=True, errors='coerce')
    df = df.dropna(subset=['timestamp_open'])
    df['timestamp_open'] = pd.to_datetime(df['timestamp_open'], utc=True)
    
    # Determine outcome: win if pnl > 0, loss if pnl <= 0
    df['is_win'] = df['realized_pnl'] > 0
    df['date'] = df['timestamp_open'].dt.date
    
    return df

def compute_drawdown(pnl_series):
    """Compute max drawdown from P&L series."""
    cumulative = pnl_series.cumsum()
    running_max = cumulative.expanding().max()
    drawdown = cumulative - running_max
    return drawdown.min()

def compute_longest_losing_streak(is_win_series):
    """Compute longest consecutive losing streak."""
    streaks = []
    current_streak = 0
    for is_win in is_win_series:
        if not is_win:
            current_streak += 1
        else:
            if current_streak > 0:
                streaks.append(current_streak)
            current_streak = 0
    if current_streak > 0:
        streaks.append(current_streak)
    return max(streaks) if streaks else 0

def main():
    # Load data
    df = get_all_trades()
    
    # ========== DATASET VALIDATION ==========
    print("=" * 60)
    print("BTC-15M CORRECTED AUDIT - FULL DISTRIBUTION")
    print("=" * 60)
    print()
    
    # Raw validation
    total_raw = len(df)
    wins = df[df['is_win'] == True]
    losses = df[df['is_win'] == False]
    n_wins = len(wins)
    n_losses = len(losses)
    
    print("DATASET VALIDATION")
    print("-" * 60)
    print(f"1. Total raw btc15m rows: {total_raw}")
    print(f"2. Total completed trades: {total_raw}")
    print(f"3. Total wins: {n_wins}")
    print(f"4. Total losses: {n_losses}")
    print(f"5. Min timestamp: {df['timestamp_open'].min()}")
    print(f"   Max timestamp: {df['timestamp_open'].max()}")
    print()
    
    print("6. Sample 5 rows from final dataset:")
    print(df[['timestamp_open', 'market_slug', 'entry_price', 'realized_pnl', 'is_win', 'regime']].head().to_string())
    print()
    
    if total_raw == 0:
        print("ERROR: No trades found!")
        return
    
    # ========== FULL DISTRIBUTION METRICS ==========
    print()
    print("FULL DISTRIBUTION METRICS")
    print("-" * 60)
    
    # Basic counts
    win_rate = n_wins / total_raw
    print(f"Total trades: {total_raw}")
    print(f"Win rate: {win_rate:.4f} ({win_rate*100:.2f}%)")
    print()
    
    # P&L breakdown
    wins_pnl = wins['realized_pnl'].sum() if n_wins > 0 else 0
    losses_pnl = losses['realized_pnl'].sum() if n_losses > 0 else 0
    
    avg_win = wins['realized_pnl'].mean() if n_wins > 0 else 0
    avg_loss = losses['realized_pnl'].mean() if n_losses > 0 else 0
    
    print(f"Total wins: {n_wins}")
    print(f"  Sum of wins: ${wins_pnl:.2f}")
    print(f"  Average win: ${avg_win:.2f}")
    print()
    print(f"Total losses: {n_losses}")
    print(f"  Sum of losses: ${losses_pnl:.2f}")
    print(f"  Average loss: ${avg_loss:.2f}")
    print()
    
    # Total P&L
    total_pnl = df['realized_pnl'].sum()
    print(f"Total P&L: ${total_pnl:.2f}")
    print()
    
    # Expectancy
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    print(f"Expectancy per trade: ${expectancy:.2f}")
    print()
    
    # Statistical metrics on FULL distribution
    mean_pnl = df['realized_pnl'].mean()
    std_pnl = df['realized_pnl'].std()
    sem_pnl = std_pnl / np.sqrt(total_raw)
    ci_95_lower = mean_pnl - 1.96 * sem_pnl
    ci_95_upper = mean_pnl + 1.96 * sem_pnl
    
    print(f"Mean P&L per trade: ${mean_pnl:.2f}")
    print(f"Standard deviation: ${std_pnl:.2f}")
    print(f"Standard error: ${sem_pnl:.2f}")
    print(f"95% CI for mean P&L: [${ci_95_lower:.2f}, ${ci_95_upper:.2f}]")
    print()
    
    # Profit factor
    if losses_pnl != 0:
        profit_factor = abs(wins_pnl / losses_pnl) if losses_pnl != 0 else float('inf')
    else:
        profit_factor = float('inf') if wins_pnl > 0 else 0
    print(f"Profit factor: {profit_factor:.2f}" if profit_factor != float('inf') else "Profit factor: ∞ (no losses)")
    print()
    
    # Drawdown and streaks
    max_dd = compute_drawdown(df['realized_pnl'])
    longest_streak = compute_longest_losing_streak(df['is_win'])
    print(f"Max drawdown: ${max_dd:.2f}")
    print(f"Longest losing streak: {longest_streak} trades")
    print()
    
    # ========== DAILY BREAKDOWN ==========
    print()
    print("DAILY BREAKDOWN")
    print("-" * 60)
    daily = df.groupby('date').agg({
        'realized_pnl': ['count', 'sum', 'mean'],
        'is_win': 'mean'
    }).reset_index()
    daily.columns = ['date', 'trades', 'total_pnl', 'avg_pnl', 'win_rate']
    daily['win_rate'] = daily['win_rate'] * 100  # Convert to percentage
    print(daily.to_string(index=False))
    print()
    
    # ========== RECENT DRIFT ANALYSIS ==========
    print()
    print("RECENT DRIFT ANALYSIS")
    print("-" * 60)
    
    # Split into prior and recent
    n_recent = min(20, total_raw // 2)
    n_prior = total_raw - n_recent
    
    if n_recent > 0:
        prior_trades = df.iloc[:n_prior]
        recent_trades = df.iloc[-n_recent:]
        
        print(f"Prior {n_prior} trades:")
        print(f"  Mean P&L: ${prior_trades['realized_pnl'].mean():.2f}")
        print(f"  Win rate: {prior_trades['is_win'].mean()*100:.1f}%")
        print(f"  Total P&L: ${prior_trades['realized_pnl'].sum():.2f}")
        print()
        
        print(f"Recent {n_recent} trades:")
        print(f"  Mean P&L: ${recent_trades['realized_pnl'].mean():.2f}")
        print(f"  Win rate: {recent_trades['is_win'].mean()*100:.1f}%")
        print(f"  Total P&L: ${recent_trades['realized_pnl'].sum():.2f}")
        print()
        
        # Drift detection
        recent_degradation = (
            recent_trades['realized_pnl'].mean() < prior_trades['realized_pnl'].mean() and
            recent_trades['is_win'].mean() < prior_trades['is_win'].mean()
        )
    else:
        recent_degradation = False
        print("Not enough data for drift analysis")
    
    # ========== BINARY CONCLUSIONS ==========
    print()
    print("=" * 60)
    print("BINARY CONCLUSIONS")
    print("=" * 60)
    
    # Profitable on full sample?
    profitable = total_pnl > 0
    print(f"• Profitable on full sample: {'YES' if profitable else 'NO'} (Total P&L: ${total_pnl:.2f})")
    
    # Statistically positive mean P&L?
    positive_mean = mean_pnl > 0 and ci_95_lower > 0
    print(f"• Statistically positive mean P&L: {'YES' if positive_mean else 'NO'} (95% CI: [${ci_95_lower:.2f}, ${ci_95_upper:.2f}])")
    
    # Recent degradation?
    print(f"• Recent degradation present: {'YES' if recent_degradation else 'NO'}")
    print()
    
    # Summary
    print("SUMMARY")
    print("-" * 60)
    print(f"Based on {total_raw} completed trades:")
    print(f"  • Win rate: {win_rate*100:.1f}% ({n_wins}W / {n_losses}L)")
    print(f"  • Total P&L: ${total_pnl:.2f}")
    print(f"  • Expectancy: ${expectancy:.2f} per trade")
    print(f"  • Avg P&L per trade: ${mean_pnl:.2f}")
    print(f"  • Profit factor: {profit_factor:.2f}" if profit_factor != float('inf') else "  • Profit factor: ∞")

if __name__ == "__main__":
    main()
