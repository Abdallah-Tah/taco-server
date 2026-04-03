#!/usr/bin/env python3
"""
Taco Trader iOS API Server
Serves REST API endpoints for the iOS TacoTrader app
"""
import os
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request
from functools import wraps
import uuid

app = Flask(__name__)

# Configuration
ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
PUBLIC_URL = "https://degrees-aluminum-equilibrium-rarely.trycloudflare.com"
DB_PATH = ROOT / "journal.db"
AUTH_TOKEN = os.environ.get("TACO_API_TOKEN", "your-secret-token-here")

# Auth decorator
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "Missing authorization"}), 401
        token = auth_header.replace('Bearer ', '')
        if token != AUTH_TOKEN:
            return jsonify({"error": "Invalid token"}), 401
        return f(*args, **kwargs)
    return decorated

# Helper functions
def get_db():
    """Get database connection"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def get_engine_status(patterns):
    """Check if an engine is running by process pattern"""
    try:
        result = subprocess.run(
            ["pgrep", "-fa"] + patterns,
            capture_output=True, text=True, timeout=5
        )
        lines = [l for l in result.stdout.strip().split('\n') 
                if l and 'bash -c' not in l and patterns[0] in l]
        
        if lines:
            pid = int(lines[0].split()[0])
            mode = "live"
            for line in lines:
                if "--dry-run" in line or "dry" in line.lower():
                    mode = "dry"
                    break
            return {"running": True, "mode": mode, "pid": pid}
        return {"running": False, "mode": "dry", "pid": None}
    except Exception as e:
        print(f"Error checking engine status: {e}")
        return {"running": False, "mode": "dry", "pid": None}

def read_json_file(filename):
    """Read JSON file safely"""
    filepath = ROOT / filename
    try:
        if filepath.exists():
            with open(filepath) as f:
                return json.load(f)
    except:
        pass
    return None

def get_system_stats():
    """Get system health stats"""
    try:
        temp = subprocess.run(
            ["vcgencmd", "measure_temp"],
            capture_output=True, text=True
        ).stdout.strip()
        cpu_temp = float(temp.split('=')[1].split("'")[0]) if temp else 0.0
        
        mem = subprocess.run(
            ["free", "-m"],
            capture_output=True, text=True
        ).stdout.split('\n')[1].split()
        memory_used = float(mem[2])
        
        disk = subprocess.run(
            ["df", "-h", "/"],
            capture_output=True, text=True
        ).stdout.split('\n')[1].split()
        disk_used = int(disk[4].replace('%', ''))
        
        uptime = subprocess.run(
            ["uptime", "-p"],
            capture_output=True, text=True
        ).stdout.strip()
        
        return {
            "host": "raspberrypi",
            "uptime": uptime.replace("up ", ""),
            "cpu_temp_c": cpu_temp,
            "memory_used_mb": memory_used,
            "disk_used_percent": disk_used,
            "openclaw_running": True,
            "last_updated": datetime.utcnow().isoformat() + "Z"
        }
    except Exception as e:
        print(f"Error getting system stats: {e}")
        return {
            "host": "raspberrypi",
            "uptime": "unknown",
            "cpu_temp_c": 0.0,
            "memory_used_mb": 0.0,
            "disk_used_percent": 0,
            "openclaw_running": False,
            "last_updated": datetime.utcnow().isoformat() + "Z"
        }

# API Endpoints

@app.route('/system', methods=['GET'])
@app.route('/api/system', methods=['GET'])
def api_system():
    """System health endpoint"""
    return jsonify(get_system_stats())

@app.route('/api/report', methods=['GET'])
def api_report():
    """Trading report endpoint"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        positions = read_json_file(".poly_positions.json") or {}
        wallet = read_json_file(".trading_wallet.json") or {}
        
        # Polymarket free cash
        poly_free_cash = wallet.get("usdc_balance", 0.0)
        
        # Calculate open value from Polymarket positions
        poly_open_value = 0.0
        if isinstance(positions, dict):
            for pos_data in positions.values():
                if isinstance(pos_data, dict):
                    poly_open_value += pos_data.get("current_value", 0.0)
        
        # Coinbase balance (from coinbase_journal.db or config)
        cb_free_cash = 0.0
        try:
            cb_conn = sqlite3.connect(str(ROOT / "coinbase_journal.db"))
            cb_cur = cb_conn.cursor()
            cb_cur.execute("SELECT balance_usd FROM balance_snapshots ORDER BY timestamp DESC LIMIT 1")
            cb_row = cb_cur.fetchone()
            cb_free_cash = cb_row[0] if cb_row else 0.0
            cb_conn.close()
        except:
            cb_free_cash = 0.0
        
        # Solana balance (from .trading_wallet_sol.json or config)
        sol_wallet = read_json_file(".trading_wallet_sol.json") or {}
        sol_balance = sol_wallet.get("sol_balance", 0.0)
        sol_usd_value = sol_balance * 140.0  # Approx SOL price
        
        # Total capital across all systems
        total_free_cash = poly_free_cash + cb_free_cash
        total_open_value = poly_open_value
        current_capital = total_free_cash + total_open_value + sol_usd_value
        free_cash = total_free_cash
        open_value = total_open_value
        
        cur.execute("""
            SELECT 
                SUM(CASE WHEN asset LIKE '%Bitcoin%' AND exit_type IN ('redeemed', 'filled') 
                    THEN pnl_absolute ELSE 0 END) as btc_pnl,
                SUM(CASE WHEN asset LIKE '%Ethereum%' AND exit_type IN ('redeemed', 'filled') 
                    THEN pnl_absolute ELSE 0 END) as eth_pnl,
                SUM(CASE WHEN exit_type IN ('redeemed', 'filled') 
                    THEN pnl_absolute ELSE 0 END) as combined_pnl
            FROM trades
            WHERE timestamp_open > datetime('now', '-1 day')
        """)
        row = cur.fetchone()
        btc_pnl = row[0] or 0.0
        eth_pnl = row[1] or 0.0
        combined_pnl = row[2] or 0.0
        
        cur.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN pnl_absolute > 0 THEN 1 ELSE 0 END) as wins
            FROM trades
            WHERE exit_type IN ('redeemed', 'filled', 'resolved')
            AND timestamp_open > datetime('now', '-7 days')
            AND engine IN ('btc15m', 'eth15m', 'sol15m', 'xrp15m')
        """)
        row = cur.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        win_rate = round((wins / total), 4) if total > 0 else 0.5
        
        cur.execute("""
            SELECT pnl_absolute 
            FROM trades 
            WHERE exit_type IN ('redeemed', 'filled')
            ORDER BY timestamp_close DESC 
            LIMIT 20
        """)
        recent = [r[0] for r in cur.fetchall()]
        streak = 0
        for pnl in recent:
            if pnl and pnl > 0:
                streak += 1
            else:
                break
        
        conn.close()
        
        return jsonify({
            "current_capital": current_capital,
            "free_cash": free_cash,
            "open_value": open_value,
            "goal_progress": (current_capital / 10000.0) if current_capital > 0 else 0.0,
            "btc_realized_pnl": btc_pnl,
            "eth_realized_pnl": eth_pnl,
            "combined_15m_pnl": combined_pnl,
            "seven_day_win_rate": win_rate,
            "streak": streak,
            "regime": "normal"
        })
    except Exception as e:
        print(f"Error in /api/report: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/engines/status', methods=['GET'])
def api_engines_status():
    """Engine status endpoint"""
    return jsonify({
        "btc_15m": get_engine_status(["polymarket_btc15m.py"]),
        "eth_15m": get_engine_status(["polymarket_eth15m.py"]),
        "sol_15m": get_engine_status(["polymarket_sol15m.py"]),
        "xrp_15m": get_engine_status(["polymarket_xrp15m.py"]),
        "coinbase": get_engine_status(["coinbase_momentum.py"])
    })

@app.route('/api/dashboard/summary', methods=['GET'])
def api_dashboard_summary():
    """Dashboard summary endpoint"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        # Read positions as dict and convert to list
        positions_dict = read_json_file(".poly_positions.json") or {}
        positions = []
        
        if isinstance(positions_dict, dict):
            for position_id, pos_data in list(positions_dict.items())[:10]:
                if isinstance(pos_data, dict):
                    positions.append({
                        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, position_id)),
                        "symbol": pos_data.get("market", "UNKNOWN")[:20],
                        "size": pos_data.get("amount", 0.0),
                        "entry_price": pos_data.get("avg_price", 0.0),
                        "current_price": pos_data.get("current_value", 0.0) / max(pos_data.get("amount", 1), 1),
                        "unrealized_pnl": pos_data.get("cash_pnl", 0.0)
                    })
        
        # Get recent trades
        cur.execute("""
            SELECT 
                id, timestamp_open, asset, direction, position_size, pnl_absolute, exit_type
            FROM trades
            ORDER BY timestamp_open DESC
            LIMIT 20
        """)
        trades = []
        for row in cur.fetchall():
            trades.append({
                "id": str(uuid.uuid4()),
                "timestamp": row[1],
                "symbol": (row[2] or "UNKNOWN").split()[0],
                "side": row[3] or "buy",
                "amount": row[4] or 0.0,
                "pnl": row[5],
                "status": row[6] or "filled"
            })
        
        # Calculate PNL stats
        cur.execute("""
            SELECT 
                SUM(CASE WHEN exit_type IN ('redeemed', 'filled') 
                    THEN pnl_absolute ELSE 0 END) as all_time_pnl,
                SUM(CASE WHEN exit_type IN ('redeemed', 'filled') 
                    AND timestamp_open > datetime('now', '-1 day')
                    THEN pnl_absolute ELSE 0 END) as today_pnl
            FROM trades
        """)
        row = cur.fetchone()
        all_time_pnl = row[0] or 0.0
        today_pnl = row[1] or 0.0
        
        # Calculate open PnL from positions
        open_pnl = 0.0
        if isinstance(positions_dict, dict):
            for pos_data in positions_dict.values():
                if isinstance(pos_data, dict):
                    open_pnl += pos_data.get("cash_pnl", 0.0)
        
        conn.close()
        
        return jsonify({
            "open_pnl": open_pnl,
            "today_pnl": today_pnl,
            "all_time_pnl": all_time_pnl,
            "positions": positions,
            "recent_trades": trades
        })
    except Exception as e:
        print(f"Error in /api/dashboard/summary: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/transactions', methods=['GET'])
@require_auth
def api_transactions():
    """Transaction feed endpoint"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, timestamp_open, asset, exit_type, position_size, notes
            FROM trades
            ORDER BY timestamp_open DESC
            LIMIT 100
        """)
        
        transactions = []
        for row in cur.fetchall():
            transactions.append({
                "id": str(uuid.uuid4()),
                "timestamp": row[1],
                "type": row[3] or "fill",
                "symbol": row[2],
                "amount": row[4],
                "note": row[5]
            })
        
        conn.close()
        return jsonify(transactions)
    except Exception as e:
        print(f"Error in /api/transactions: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/api/redeems', methods=['GET'])
@require_auth
def api_redeems():
    """Redeem history endpoint"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, timestamp_close, asset, pnl_absolute, exit_type
            FROM trades
            WHERE exit_type = 'redeemed'
            ORDER BY timestamp_close DESC
            LIMIT 50
        """)
        
        redeems = []
        for row in cur.fetchall():
            redeems.append({
                "id": str(uuid.uuid4()),
                "timestamp": row[1] or datetime.utcnow().isoformat() + "Z",
                "title": (row[2] or "Redeem").split()[0],
                "value": row[3] or 0.0,
                "status": "success" if row[4] == "redeemed" else "pending"
            })
        
        conn.close()
        return jsonify(redeems)
    except Exception as e:
        print(f"Error in /api/redeems: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/latest-sale', methods=['GET'])
def api_latest_sale():
    """Latest sale/redeem endpoint for iOS app (no auth required)"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, timestamp_close, asset, direction, position_size_usd, pnl_absolute, exit_type
            FROM trades
            WHERE exit_type IN ('redeemed', 'filled')
            ORDER BY timestamp_close DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        
        if not row:
            return jsonify({
                "status": "no_sale",
                "message": "No sale received yet."
            })
        
        return jsonify({
            "status": "success",
            "sale_id": row[0],
            "timestamp": row[1] or datetime.utcnow().isoformat() + "Z",
            "event": (row[2] or "Unknown").split(" - ")[0] if " - " in (row[2] or "") else (row[2] or "Unknown"),
            "direction": row[3] or "UP",
            "size_usd": row[4] or 0.0,
            "pnl": row[5] or 0.0,
            "exit_type": row[6] or "filled",
            "title": "Redeemed back to USDC.e" if row[6] == "redeemed" else "Trade completed",
            "notification_body": f"${row[5]:.2f} {'profit' if (row[5] or 0) > 0 else 'loss'}"
        })
    except Exception as e:
        print(f"Error in /latest-sale: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/redeemed', methods=['GET'])
def api_redeemed():
    """Recent redeems endpoint for iOS app (no auth required)"""
    try:
        conn = get_db()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, timestamp_close, asset, pnl_absolute, exit_type
            FROM trades
            WHERE exit_type = 'redeemed'
            ORDER BY timestamp_close DESC
            LIMIT 20
        """)
        
        redeems = []
        for row in cur.fetchall():
            event_name = (row[2] or "Unknown").split(" - ")[0] if " - " in (row[2] or "") else (row[2] or "Unknown")
            redeems.append({
                "sale_id": row[0],
                "timestamp": row[1] or datetime.utcnow().isoformat() + "Z",
                "event": event_name,
                "amount": row[3] or 0.0,
                "status": "success" if (row[3] or 0) > 0 else "loss",
                "title": "Redeemed back to USDC.e",
                "body": f"${abs(row[3] or 0):.2f} {'profit' if (row[3] or 0) > 0 else 'loss'}"
            })
        
        conn.close()
        return jsonify(redeems)
    except Exception as e:
        print(f"Error in /redeemed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/positions/live', methods=['GET'])
def api_positions_live():
    """Live open positions endpoint (no auth required)"""
    try:
        positions_dict = read_json_file(".poly_positions.json") or {}
        positions = []
        
        if isinstance(positions_dict, dict):
            for position_id, pos_data in positions_dict.items():
                if isinstance(pos_data, dict):
                    positions.append({
                        "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, position_id)),
                        "market": pos_data.get("market", "Unknown"),
                        "slug": pos_data.get("slug", ""),
                        "outcome": pos_data.get("outcome", ""),
                        "shares": pos_data.get("amount", 0.0),
                        "avg_price": pos_data.get("avg_price", 0.0),
                        "current_value": pos_data.get("current_value", 0.0),
                        "pnl": pos_data.get("cash_pnl", 0.0),
                        "redeemable": pos_data.get("redeemable", False),
                        "opened": pos_data.get("opened", "")
                    })
        
        # Sort by PnL (worst first)
        positions.sort(key=lambda x: x["pnl"])
        
        return jsonify({
            "count": len(positions),
            "total_value": sum(p["current_value"] for p in positions),
            "total_pnl": sum(p["pnl"] for p in positions),
            "positions": positions
        })
    except Exception as e:
        print(f"Error in /api/positions/live: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({"status": "ok", "service": "taco-trader-api"})

if __name__ == '__main__':
    print(f"Starting Taco Trader API Server on port 5000")
    print(f"Database: {DB_PATH}")
    print(f"Auth token: {AUTH_TOKEN[:10]}...")
    app.run(host='0.0.0.0', port=5000, debug=False)
