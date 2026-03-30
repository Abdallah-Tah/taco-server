#!/usr/bin/env python3
"""
trading_reports.py - Short and Long report commands for OpenClaw.

Usage:
  # Short report (for Telegram - fits in 4096 chars)
  python scripts/trading_reports.py --short

  # Long report (detailed, saved to reports/)
  python scripts/trading_reports.py --long

  # Both reports
  python scripts/trading_reports.py --both
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
DB_PATH = ROOT / "journal.db"
REPORTS_DIR = ROOT / "reports"


def fmt_money(x):
    return f"${x:.2f}"


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_engine_stats(engine):
    """Get engine stats from journal."""
    conn = get_db()
    c = conn.cursor()

    # Total stats
    c.execute("""
        SELECT
            COUNT(*) as total_orders,
            SUM(CASE WHEN exit_price > 0 THEN 1 ELSE 0 END) as resolved,
            SUM(CASE WHEN exit_price > 0 AND pnl_percent > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN exit_price > 0 AND pnl_percent <= 0 THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(pnl_absolute), 0) as total_pnl
        FROM trades WHERE engine = ? AND timestamp_open IS NOT NULL
    """, (engine,))

    row = c.fetchone()
    total_orders = row[0] or 0
    resolved = row[1] or 0
    wins = row[2] or 0
    losses = row[3] or 0

    # Daily stats
    c.execute("""
        SELECT DATE(timestamp_open) as date,
               COUNT(*) as trades,
               SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_absolute < 0 THEN 1 ELSE 0 END) as losses,
               SUM(pnl_absolute) as pnl
        FROM trades WHERE engine = ? AND timestamp_close IS NOT NULL
        GROUP BY DATE(timestamp_open)
        ORDER BY date DESC LIMIT 3
    """, (engine,))
    daily = c.fetchall()

    conn.close()
    return {
        'total_orders': total_orders,
        'resolved': resolved,
        'wins': wins,
        'losses': losses,
        'total_pnl': row[4] or 0,
        'daily': daily
    }


def get_recent_trades(engine, limit=20):
    """Get recent trades for an engine."""
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp_open, direction, pnl_absolute, pnl_percent, exit_type
        FROM trades
        WHERE engine = ? AND timestamp_close IS NOT NULL
        ORDER BY timestamp_open DESC LIMIT ?
    """, (engine, limit))
    trades = c.fetchall()
    conn.close()
    return trades


def get_portfolio():
    """Get portfolio status from current positions."""
    try:
        pos_file = ROOT / ".poly_positions.json"
        if pos_file.exists():
            positions = json.loads(pos_file.read_text())
            total_value = sum(float(p.get('current_value', 0)) for p in positions.values())
            return len(positions), total_value
    except Exception:
        pass
    return 0, 0.0


def generate_short_report():
    """Generate short report (under 4096 chars for Telegram)."""
    lines = []

    # Header
    lines.append("📊 TRADING REPORT - SHORT")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    # Portfolio
    pos_count, pos_value = get_portfolio()
    lines.append(f"💰 Portfolio: {fmt_money(pos_value + 151.88)} | Free: {fmt_money(151.88)}")

    # BTC stats
    btc = get_engine_stats('btc15m')
    lines.append("")
    lines.append("BTC-15m:")
    lines.append(f"  Resolved: {btc['resolved']} | W:{btc['wins']} L:{btc['losses']} | PnL: {fmt_money(btc['total_pnl'])}")

    # ETH stats
    eth = get_engine_stats('eth15m')
    lines.append("")
    lines.append("ETH-15m:")
    lines.append(f"  Resolved: {eth['resolved']} | W:{eth['wins']} L:{eth['losses']} | PnL: {fmt_money(eth['total_pnl'])}")

    # Today's performance
    lines.append("")
    lines.append("Today (Mar 22):")

    # Get today's stats
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT engine, COUNT(*) as trades,
               SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
               SUM(pnl_absolute) as pnl
        FROM trades WHERE DATE(timestamp_open) = '2026-03-22' AND timestamp_close IS NOT NULL
        GROUP BY engine
    """)
    today = {row[0]: (row[1], row[2], row[3]) for row in c.fetchall()}
    conn.close()

    if 'btc15m' in today:
        t = today['btc15m']
        lines.append(f"  BTC: {t[0]} trades | W:{t[1]} | {fmt_money(t[2])}")

    if 'eth15m' in today:
        t = today['eth15m']
        lines.append(f"  ETH: {t[0]} trades | W:{t[1]} | {fmt_money(t[2])}")

    # Key insight
    lines.append("")
    if btc['total_pnl'] > 0 and eth['total_pnl'] > 0:
        lines.append("Both engines profitable! 🟢")
    elif btc['total_pnl'] < 0 and eth['total_pnl'] > 0:
        lines.append("BTC dragging, ETH profitable 🟡")
    elif btc['total_pnl'] > 0 and eth['total_pnl'] < 0:
        lines.append("ETH dragging, BTC profitable 🟡")
    else:
        lines.append("Both engines negative 🛑")

    return "\n".join(lines)


def generate_long_report():
    """Generate detailed long report (saved to file)."""
    lines = []

    lines.append("# 📊 Trading Report")
    lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    # Portfolio
    pos_count, pos_value = get_portfolio()
    total_capital = pos_value + 151.88  # approximate free cash
    lines.append("## 💰 Capital")
    lines.append(f"- Current: {fmt_money(total_capital)}")
    lines.append(f"- Positions: {pos_count} | Value: {fmt_money(pos_value)}")
    lines.append(f"- Free cash: {fmt_money(151.88)}")
    lines.append("")

    # Engine Stats
    btc = get_engine_stats('btc15m')
    eth = get_engine_stats('eth15m')

    lines.append("## 📈 Engine Stats")
    lines.append("")
    lines.append("### BTC-15m")
    lines.append(f"- Resolved markets: {btc['resolved']}")
    lines.append(f"- Wins: {btc['wins']} | Losses: {btc['losses']}")
    lines.append(f"- Realized PnL: {fmt_money(btc['total_pnl'])}")
    lines.append("")
    lines.append("### ETH-15m")
    lines.append(f"- Resolved markets: {eth['resolved']}")
    lines.append(f"- Wins: {eth['wins']} | Losses: {eth['losses']}")
    lines.append(f"- Realized PnL: {fmt_money(eth['total_pnl'])}")
    lines.append("")

    # Today's details
    conn = get_db()
    c = conn.cursor()

    # BTC recent trades
    lines.append("## 🎯 BTC - Recent Trades (Last 10)")
    c.execute("""
        SELECT timestamp_open, direction, position_size_usd, pnl_absolute, pnl_percent, exit_type
        FROM trades WHERE engine = 'btc15m' AND timestamp_close IS NOT NULL
        ORDER BY timestamp_open DESC LIMIT 10
    """)
    for t in c.fetchall():
        ts = t[0][:16].replace('T', ' ')
        pnl_sign = "+" if t[3] >= 0 else ""
        pnl_emoji = "✅" if t[3] > 0 else "❌"
        lines.append(f"- {ts} {t[1]} {pnl_emoji} {fmt_money(t[3])} ({pnl_sign}{t[4]:.1f}%)")

    lines.append("")

    # ETH recent trades
    lines.append("## 🎯 ETH - Recent Trades (Last 10)")
    c.execute("""
        SELECT timestamp_open, direction, position_size_usd, pnl_absolute, pnl_percent, exit_type
        FROM trades WHERE engine = 'eth15m' AND timestamp_close IS NOT NULL
        ORDER BY timestamp_open DESC LIMIT 10
    """)
    for t in c.fetchall():
        ts = t[0][:16].replace('T', ' ')
        pnl_sign = "+" if t[3] >= 0 else ""
        pnl_emoji = "✅" if t[3] > 0 else "❌"
        lines.append(f"- {ts} {t[1]} {pnl_emoji} {fmt_money(t[3])} ({pnl_sign}{t[4]:.1f}%)")

    lines.append("")

    # Daily summary
    lines.append("## 📅 Daily Summary")

    c.execute("""
        SELECT DATE(timestamp_open) as date, engine,
               COUNT(*) as trades,
               SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins,
               SUM(pnl_absolute) as pnl
        FROM trades WHERE timestamp_close IS NOT NULL
        GROUP BY DATE(timestamp_open), engine
        ORDER BY date DESC, engine LIMIT 10
    """)
    daily = c.fetchall()
    conn.close()

    for row in daily:
        date, engine, trades, wins, pnl = row
        eng_short = "BTC" if engine == "btc15m" else "ETH"
        lines.append(f"- {date} {eng_short}: {trades} trades | W:{wins} | {fmt_money(pnl)}")

    lines.append("")
    lines.append("---")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Trading report generator")
    parser.add_argument('--short', action='store_true', help="Short report for Telegram")
    parser.add_argument('--long', action='store_true', help="Long detailed report")
    parser.add_argument('--both', action='store_true', help="Generate both reports")

    args = parser.parse_args()

    if args.short or args.both:
        report = generate_short_report()
        print("=== SHORT REPORT (for Telegram) ===")
        print(report)
        print(f"\n[Length: {len(report)} chars]")
        if len(report) > 4096:
            print("[WARNING: Report too long for Telegram!]")

    if args.long or args.both:
        report = generate_long_report()
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M')
        filename = f"reports/{timestamp}-trading-report.md"
        filepath = ROOT / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(report)
        print(f"=== LONG REPORT SAVED TO: {filepath} ===")


if __name__ == "__main__":
    main()
