#!/usr/bin/env python3
import sqlite3
import pandas as pd
import numpy as np
import re
import ast
import os
from pathlib import Path

# --- CONFIGURATION ---
DB_PATH = Path.home() / ".openclaw" / "workspace" / "trading" / "journal.db"
BTC_SCRIPT_PATH = Path.home() / ".openclaw" / "workspace" / "trading" / "scripts" / "polymarket_btc15m.py"
ENGINE_NAME = "btc15m"

# --- HELPER FUNCTIONS ---

def get_script_content(path):
    """Reads the content of the btc15m script."""
    try:
        return path.read_text()
    except FileNotFoundError:
        print(f"ERROR: Script not found at {path}")
        return None

def parse_notes(notes):
    """Parses the 'notes' column from the journal to extract key metrics."""
    if not isinstance(notes, str):
        return {}
    
    patterns = {
        'confidence': r"confidence=([0-9.]+)",
        'net_edge': r"net_edge=([0-9.]+)",
        'sec_remaining': r"sec_rem=(\d+)",
        'regime': r"regime=(\w+)",
        'model_prob': r"model_prob=([0-9.]+)"
    }
    
    data = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, notes)
        if match:
            try:
                data[key] = float(match.group(1))
            except ValueError:
                data[key] = match.group(1) # For regime string
    return data

def get_trades_from_db(db_path, engine_name):
    """Fetches and processes trades from the SQLite database."""
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        return None

    with sqlite3.connect(db_path) as con:
        # The table is named 'trades', and columns have specific names.
        # 'outcome' is not a column, it is derived from pnl_absolute.
        query = f"SELECT timestamp_open, asset as market_slug, entry_price, pnl_absolute as realized_pnl, notes FROM trades WHERE engine = '{engine_name}' AND pnl_absolute IS NOT NULL"
        df = pd.read_sql_query(query, con)

    if df.empty:
        print(f"No completed trades found for engine '{engine_name}'.")
        return None

    # Rename columns and parse notes
    df.rename(columns={
        'price': 'entry_price',
        'pnl': 'realized_pnl'
    }, inplace=True)

    notes_data = df['notes'].apply(parse_notes).apply(pd.Series)
    df = pd.concat([df.drop('notes', axis=1), notes_data], axis=1)

    # Convert types
    df['timestamp_open'] = pd.to_datetime(df['timestamp_open'], utc=True, errors='coerce')
    df.dropna(subset=['timestamp_open'], inplace=True)
    df['outcome'] = df['realized_pnl'].apply(lambda x: 1 if x > 0 else 0)
    df['realized_pnl'] = pd.to_numeric(df['realized_pnl'], errors='coerce')
    df['entry_price'] = pd.to_numeric(df['entry_price'], errors='coerce')
    df['confidence'] = pd.to_numeric(df['confidence'], errors='coerce')
    df['net_edge'] = pd.to_numeric(df['net_edge'], errors='coerce')
    df['sec_remaining'] = pd.to_numeric(df['sec_remaining'], errors='coerce')
    df['model_prob'] = pd.to_numeric(df['model_prob'], errors='coerce')

    # Add required columns
    df['market_implied_prob'] = df['entry_price']
    df['payout'] = df.apply(lambda row: (1 - row['entry_price']) if row['outcome'] == 1 else -row['entry_price'], axis=1)
    
    # Fill missing regimes
    df['regime'].fillna('unknown', inplace=True)
    
    return df

def analyze_and_print(df):
    """Performs and prints all analyses based on the dataframe."""

    # --- Validation Block ---
    print("--- DATASET VALIDATION ---")
    print(f"Total rows for 'btc15m' with completed PNL: {len(df)}")
    print(f"Min trade timestamp: {df['timestamp_open'].min()}")
    print(f"Max trade timestamp: {df['timestamp_open'].max()}")
    print("Sample 5 rows for audit:")
    print(df.head().to_string())
    print("\\n---\\n")

    # --- 1. Define the Model Precisely ---
    print("--- 1. Model Definition ---")
    print("Based on manual inspection of polymarket_btc15m.py:")
    print(" • confidence: Not explicitly defined by a simple formula. Appears to be a score derived from momentum, volatility, and time decay factors. It is NOT a probability.")
    print(" • net_edge: `(confidence - entry_price)`. This is a heuristic score, NOT a true Expected Value calculation, as 'confidence' is not a probability.")
    print(" • model_predicted_prob: The script was recently updated to calculate and log `model_prob`. This will be used for the audit.")
    print("\n")

    # --- 3. Compute True Expected Value ---
    print("--- 3. True Expected Value (EV) Analysis ---")
    df.dropna(subset=['model_prob', 'entry_price'], inplace=True)
    df['ev_at_entry'] = (df['model_prob'] * (1 - df['entry_price'])) - ((1 - df['model_prob']) * df['entry_price'])
    
    avg_ev = df['ev_at_entry'].mean()
    avg_pnl = df['realized_pnl'].mean()
    
    print(f"Total Trades Analyzed: {len(df)}")
    print(f"Average EV at Entry: {avg_ev:.4f}")
    print(f"Average Realized P&L: {avg_pnl:.4f}")
    print("\n")

    # --- 4. Calibration Test ---
    print("--- 4. Model Calibration Test ---")
    bins = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0]
    df['prob_bucket'] = pd.cut(df['model_prob'], bins, right=False)
    
    cal_table = df.groupby('prob_bucket').agg(
        trades=('outcome', 'count'),
        predicted_prob_avg=('model_prob', 'mean'),
        actual_win_rate=('outcome', 'mean')
    ).reset_index()
    
    print(cal_table[cal_table['trades'] > 0].to_string(index=False))
    print("\n")
    
    # --- 5. Edge Validation ---
    print("--- 5. Edge Validation ---")
    df['edge'] = df['model_prob'] - df['market_implied_prob']
    
    edge_analysis = {
        'Average Edge': df['edge'].mean(),
        'Win Rate (Edge > 0)': df[df['edge'] > 0]['outcome'].mean(),
        'Win Rate (Edge <= 0)': df[df['edge'] <= 0]['outcome'].mean(),
        'Count (Edge > 0)': len(df[df['edge'] > 0]),
        'Count (Edge <= 0)': len(df[df['edge'] <= 0]),
    }
    for k, v in edge_analysis.items():
        print(f" • {k}: {v:.4f}")
    print("\n")
    
    # --- 6. Timing Analysis ---
    print("--- 6. Timing Analysis (sec_remaining) ---")
    timing_bins = [-1, 60, 180, 300, float('inf')]
    timing_labels = ['0-60', '61-180', '181-300', '>300']
    df['time_bucket'] = pd.cut(df['sec_remaining'], bins=timing_bins, labels=timing_labels)
    
    timing_table = df.groupby('time_bucket').agg(
        trades=('outcome', 'count'),
        win_rate=('outcome', 'mean'),
        avg_ev=('ev_at_entry', 'mean'),
        avg_pnl=('realized_pnl', 'mean')
    ).reset_index()
    
    print(timing_table[timing_table['trades'] > 0].to_string(index=False))
    print("\n")

    # --- 7. Regime Validation ---
    print("--- 7. Regime Validation ---")
    regime_table = df.groupby('regime').agg(
        trades=('outcome', 'count'),
        win_rate=('outcome', 'mean'),
        avg_ev=('ev_at_entry', 'mean'),
        avg_pnl=('realized_pnl', 'mean')
    ).reset_index()
    print(regime_table[regime_table['trades'] > 0].to_string(index=False))
    print("\n")

    # --- 9. Final Verdict ---
    print("--- 9. Final Verdict ---")
    is_positive_ev = avg_pnl > 0
    is_calibrated = cal_table[cal_table['trades'] > 0].apply(lambda row: abs(row['predicted_prob_avg'] - row['actual_win_rate']) < 0.1, axis=1).all() # Simple check: is error < 10%
    is_edge_real = edge_analysis['Win Rate (Edge > 0)'] > edge_analysis['Win Rate (Edge <= 0)']

    print(f" • Does the model have positive expected value? {'YES' if is_positive_ev else 'NO'} (Based on realized P&L)")
    print(f" • Is the model calibrated? {'YES' if is_calibrated else 'NO'} (Approximate check)")
    print(f" • Is edge real or illusory? {'REAL' if is_edge_real else 'NOT REAL'}")


def main():
    """Main execution function."""
    df = get_trades_from_db(DB_PATH, ENGINE_NAME)
    
    if df is not None:
        analyze_and_print(df)

if __name__ == "__main__":
    main()
