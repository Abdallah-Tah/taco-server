#!/usr/bin/env python3
import json
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

DB = Path('/home/abdaltm86/.openclaw/workspace/trading/journal.db')
OUT_DECISIONS = Path('/home/abdaltm86/.openclaw/workspace/trading/reports/btc_live_gating_decisions.jsonl')
OUT_SUMMARY = Path('/home/abdaltm86/.openclaw/workspace/trading/reports/btc_live_gating_summary_30m.jsonl')
STATE = Path('/tmp/btc_live_gating_monitor.state.json')
SECRETS = Path('/home/abdaltm86/.config/openclaw/secrets.env')

OUT_DECISIONS.parent.mkdir(parents=True, exist_ok=True)


def load_env(path: Path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"last_rowid": 0, "window_start": time.time(), "window_rows": []}


def save_state(state):
    STATE.write_text(json.dumps(state))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def send_telegram(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
    except Exception:
        pass


env = load_env(SECRETS)
TELEGRAM_TOKEN = env.get('TELEGRAM_TOKEN', '8457917317:AAGtnuRix7Ei-rslwVAbfFIFJSK0UIwi0d4')
CHAT_ID = env.get('CHAT_ID', '7520899464')

state = load_state()

while True:
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT rowid, timestamp_et, net_edge, confidence, shadow_decision, execution_status, decision, skip_reason
        FROM edge_events
        WHERE asset='BTC' AND rowid > ?
        ORDER BY rowid ASC
        """,
        (state['last_rowid'],),
    ).fetchall()
    conn.close()

    for row in rows:
        rec = {
            'timestamp': row['timestamp_et'] or now_iso(),
            'net_edge': row['net_edge'],
            'confidence': row['confidence'],
            'shadow_decision': row['shadow_decision'],
            'execution_status': row['execution_status'] or 'unknown',
            'skip_reason': row['skip_reason'],
            'live_decision': row['decision'],
            'rowid': row['rowid'],
        }
        with OUT_DECISIONS.open('a') as f:
            f.write(json.dumps(rec) + '\n')
        state['last_rowid'] = row['rowid']
        state['window_rows'].append(rec)

    if time.time() - state['window_start'] >= 1800:
        window_rows = state['window_rows']
        executed = [x for x in window_rows if x['execution_status'] == 'executed']
        skipped = [x for x in window_rows if x['execution_status'] != 'executed']
        avg_net = (sum((x['net_edge'] or 0.0) for x in executed) / len(executed)) if executed else None
        avg_conf = (sum((x['confidence'] or 0.0) for x in executed) / len(executed)) if executed else None

        summary = {
            'window_end_timestamp': now_iso(),
            'window_seconds': 1800,
            'total_events': len(window_rows),
            'executed_trades': len(executed),
            'skipped_trades': len(skipped),
            'avg_net_edge_executed': avg_net,
            'avg_confidence_executed': avg_conf,
            'summary_file_path': str(OUT_SUMMARY),
        }

        with OUT_SUMMARY.open('a') as f:
            f.write(json.dumps(summary) + '\n')

        msg = (
            "[BTC-GATE 30m]\n"
            f"window end: {summary['window_end_timestamp']}\n"
            f"total events: {summary['total_events']}\n"
            f"executed trades: {summary['executed_trades']}\n"
            f"skipped trades: {summary['skipped_trades']}\n"
            f"avg net_edge executed: {summary['avg_net_edge_executed']}\n"
            f"avg confidence executed: {summary['avg_confidence_executed']}\n"
            f"summary file: {summary['summary_file_path']}"
        )
        send_telegram(TELEGRAM_TOKEN, CHAT_ID, msg)

        state['window_start'] = time.time()
        state['window_rows'] = []

    save_state(state)
    time.sleep(10)
