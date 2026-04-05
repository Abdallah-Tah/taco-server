#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

DB_PATH = "/home/abdaltm86/.openclaw/workspace/trading/journal.db"
STATUS_PATH = "/tmp/btc_eth_canary_status.json"
LOG_PATH = "/tmp/btc_eth_canary_guard.log"

DURATION_SEC = int(os.environ.get("CANARY_DURATION_SEC", "7200"))
POLL_SEC = int(os.environ.get("CANARY_POLL_SEC", "30"))
STOP_PNL = float(os.environ.get("CANARY_STOP_PNL", "-20"))
STOP_STREAK = int(os.environ.get("CANARY_STOP_STREAK", "3"))

START_ISO = os.environ.get("CANARY_START_ISO", datetime.now(timezone.utc).isoformat())

DEDUP_CTE = """
WITH ranked AS (
  SELECT
    rowid,
    *,
    ROW_NUMBER() OVER (
      PARTITION BY engine, asset, timestamp_open, direction
      ORDER BY COALESCE(timestamp_close, '') DESC, rowid DESC
    ) AS rn
  FROM trades
  WHERE engine IN ('btc15m', 'eth15m')
    AND COALESCE(exit_type, '') = 'resolved'
    AND pnl_absolute IS NOT NULL
    AND COALESCE(timestamp_close, timestamp_open) >= ?
),
dedup AS (
  SELECT * FROM ranked WHERE rn = 1
)
"""


def log(msg: str) -> None:
  line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
  print(line, flush=True)
  with open(LOG_PATH, "a", encoding="utf-8") as f:
    f.write(line + "\n")


def get_metrics():
  conn = sqlite3.connect(DB_PATH)
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()
  cur.execute(
    DEDUP_CTE
    + """
    SELECT
      COALESCE(SUM(pnl_absolute), 0.0) AS total_pnl,
      COALESCE(SUM(CASE WHEN engine='btc15m' THEN pnl_absolute ELSE 0 END), 0.0) AS btc_pnl,
      COALESCE(SUM(CASE WHEN engine='eth15m' THEN pnl_absolute ELSE 0 END), 0.0) AS eth_pnl,
      COUNT(*) AS resolved_count
    FROM dedup
    """,
    (START_ISO,),
  )
  row = cur.fetchone()
  total = float(row["total_pnl"] or 0.0)
  btc = float(row["btc_pnl"] or 0.0)
  eth = float(row["eth_pnl"] or 0.0)
  count = int(row["resolved_count"] or 0)

  streaks = {}
  for engine in ("btc15m", "eth15m"):
    cur.execute(
      DEDUP_CTE
      + """
      SELECT pnl_absolute
      FROM dedup
      WHERE engine = ?
      ORDER BY COALESCE(timestamp_close, timestamp_open) DESC
      LIMIT 12
      """,
      (START_ISO, engine),
    )
    vals = [float(r["pnl_absolute"] or 0.0) for r in cur.fetchall()]
    streak = 0
    for v in vals:
      if v < 0:
        streak += 1
      else:
        break
    streaks[engine] = streak

  conn.close()
  return total, btc, eth, count, streaks


def stop_engines(reason: str):
  log(f"STOP condition hit: {reason}. Stopping BTC/ETH engines.")
  subprocess.run(["pkill", "-f", "polymarket_btc15m.py"], check=False)
  subprocess.run(["pkill", "-f", "polymarket_eth15m.py"], check=False)


def write_status(payload):
  with open(STATUS_PATH, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)


def main():
  t0 = time.time()
  deadline = t0 + DURATION_SEC
  log(
    f"Canary guard started: start_iso={START_ISO} duration_sec={DURATION_SEC} "
    f"stop_pnl={STOP_PNL} stop_streak={STOP_STREAK}"
  )

  while time.time() < deadline:
    total, btc, eth, count, streaks = get_metrics()
    status = {
      "start_iso": START_ISO,
      "timestamp": datetime.now(timezone.utc).isoformat(),
      "resolved_count": count,
      "total_pnl": round(total, 4),
      "btc_pnl": round(btc, 4),
      "eth_pnl": round(eth, 4),
      "btc_loss_streak": streaks["btc15m"],
      "eth_loss_streak": streaks["eth15m"],
      "stop_pnl": STOP_PNL,
      "stop_streak": STOP_STREAK,
    }
    write_status(status)
    log(
      f"status resolved={count} pnl_total={total:+.2f} btc={btc:+.2f} eth={eth:+.2f} "
      f"streak_btc={streaks['btc15m']} streak_eth={streaks['eth15m']}"
    )

    if total <= STOP_PNL:
      stop_engines(f"combined pnl {total:+.2f} <= {STOP_PNL:+.2f}")
      return
    if streaks["btc15m"] >= STOP_STREAK:
      stop_engines(f"btc streak {streaks['btc15m']} >= {STOP_STREAK}")
      return
    if streaks["eth15m"] >= STOP_STREAK:
      stop_engines(f"eth streak {streaks['eth15m']} >= {STOP_STREAK}")
      return

    time.sleep(POLL_SEC)

  log("Canary window finished without stop trigger.")


if __name__ == "__main__":
  main()
