#!/usr/bin/env python3
"""
Trading dashboard API and report webhook.

This process now serves both Telegram report endpoints and the live dashboard API:
- `/api/events/stream` streams real engine events over SSE
- `/api/report`, `/api/stats/extended`, `/api/dashboard/summary` serve live data
- `/api/latest-sale`, `/api/redeemed`, `/api/system` remain for compatibility
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path.home() / ".openclaw" / "workspace" / "trading"
VENV_PY = ROOT / ".polymarket-venv" / "bin" / "python3"
REPORT_SCRIPT = ROOT / "scripts" / "trading_reports.py"
FULL_REPORT_SCRIPT = ROOT / "scripts" / "trading_report.py"
JOURNAL_DB = ROOT / "journal.db"
POLY_POSITIONS = ROOT / ".poly_positions.json"
PORTFOLIO_FILE = ROOT / ".portfolio.json"
BTC_LOG = ROOT / ".poly_btc15m.log"
ETH_LOG = ROOT / ".poly_eth15m.log"
SOL_LOG = ROOT / ".poly_sol15m.log"
XRP_LOG = ROOT / ".poly_xrp15m.log"

LOG_SOURCES = {
    "btc15m": BTC_LOG,
    "eth15m": ETH_LOG,
    "sol15m": SOL_LOG,
    "xrp15m": XRP_LOG,
}

SKIP_REASON_MAP = {
    "high price": "high_price",
    "low price": "low_price",
    "ultra-late entry": "late_entry",
    "outside_maker_window": "late_entry",
    "cooldown": "cooldown",
    "weak": "delta_too_small",
    "decelerating": "delta_too_small",
    "confirm: 1/2": "delta_too_small",
    "conflicts with": "trend_conflict",
}

COMPAT_HISTORY_LIMIT = 40
EVENT_BACKLOG_LIMIT = 60
EVENT_POLL_SEC = 0.5


COMMAND_MAP = {
    "/short-report": ("short", "Short trading report"),
    "/long-report": ("long", "Detailed trading report"),
    "/report": ("full", "Full trading report"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw: str | None) -> datetime:
    if not raw:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return datetime.fromtimestamp(0, tz=timezone.utc)


def _fmt_money(value: Any) -> str:
    try:
        num = float(value or 0)
    except Exception:
        num = 0.0
    return f"${num:,.2f}"


def _clean_symbol(engine: str | None, asset: str | None, market_slug: str | None = None) -> str:
    for candidate in (asset, market_slug, engine):
        text = str(candidate or "").upper()
        if "BTC" in text:
            return "BTC-USD"
        if "ETH" in text:
            return "ETH-USD"
        if "SOL" in text:
            return "SOL-USD"
        if "XRP" in text:
            return "XRP-USD"
    return str(asset or engine or "UNKNOWN").upper()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{JOURNAL_DB}?mode=ro&immutable=1",
        uri=True,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text())
    except Exception:
        return fallback


def send_to_telegram(message: str, chat_id: str) -> bool:
    try:
        chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)] or [""]
        for chunk in chunks:
            result = subprocess.run(
                [
                    "openclaw",
                    "message",
                    "send",
                    "--channel",
                    "telegram",
                    "--target",
                    str(chat_id),
                    "--message",
                    chunk,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                print(f"Error sending chunk: {result.stderr}")
                return False
        return True
    except Exception as exc:
        print(f"Failed to send message: {exc}")
        return False


def generate_report(command: str) -> str:
    if command == "full":
        result = subprocess.run([str(VENV_PY), str(FULL_REPORT_SCRIPT)], capture_output=True, text=True, timeout=60)
        return result.stdout.strip()

    cmd = [str(VENV_PY), str(REPORT_SCRIPT), f"--{command}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    if command == "long":
        import glob

        files = sorted(glob.glob(str(ROOT / "reports" / "*-trading-report.md")), reverse=True)
        return Path(files[0]).read_text() if files else "No report file found."

    lines = []
    for line in result.stdout.strip().splitlines():
        if line.startswith("=== SHORT REPORT") or line.startswith("=== LONG REPORT"):
            continue
        if line.startswith("[Length:") or line.startswith("[WARNING:"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def handle_command(command_name: str, chat_id: str, dry_run: bool = False) -> dict[str, Any]:
    if command_name in COMMAND_MAP:
        report_type, description = COMMAND_MAP[command_name]
    elif command_name in ("short", "long", "full"):
        report_type = command_name
        if command_name == "full":
            description = "Full trading report"
            command_name = "/report"
        else:
            description = COMMAND_MAP.get(f"/{command_name}-report", (None, "Report"))[1]
            command_name = f"/{command_name}-report"
    else:
        return {"error": f"Unknown command: {command_name}"}

    report = generate_report(report_type)
    if dry_run:
        return {"status": "dry-run", "report": report, "description": description}

    success = send_to_telegram(report, chat_id)
    return {
        "status": "sent" if success else "failed",
        "command": command_name,
        "chat_id": chat_id,
        "description": description,
    }


def handle_command_async(command_name: str, chat_id: str) -> None:
    try:
        handle_command(command_name, chat_id)
    except Exception as exc:
        print(f"Async command handling failed: {exc}", file=sys.stderr, flush=True)


def _load_secrets() -> dict[str, str]:
    secrets = {}
    secrets_file = Path.home() / ".config" / "openclaw" / "secrets.env"
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                secrets[key.strip()] = value.strip().strip('"').strip("'")
    return secrets


def get_system() -> dict[str, Any]:
    try:
        import shutil
        import socket

        uptime_sec = float(Path("/proc/uptime").read_text().split()[0])
        uptime_h = int(uptime_sec // 3600)
        uptime_m = int((uptime_sec % 3600) // 60)
        disk = shutil.disk_usage("/")
        mem_lines = Path("/proc/meminfo").read_text().splitlines()
        mem = {}
        for line in mem_lines:
            name, value = line.split(":", 1)
            if name in ("MemTotal", "MemAvailable"):
                mem[name] = int(value.split()[0])
        cpu_temp_c = None
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        if thermal.exists():
            cpu_temp_c = int(thermal.read_text().strip()) / 1000.0
        return {
            "host": socket.gethostname(),
            "uptime": f"{uptime_h}h {uptime_m}m",
            "disk_used_gb": round(disk.used / 1e9, 2),
            "disk_total_gb": round(disk.total / 1e9, 2),
            "disk_percent": round(disk.used / disk.total * 100, 1),
            "mem_total_mb": round(mem.get("MemTotal", 0) / 1024, 1),
            "mem_available_mb": round(mem.get("MemAvailable", 0) / 1024, 1),
            "cpu_temp_c": cpu_temp_c,
        }
    except Exception as exc:
        return {"error": str(exc)}


def get_live_wallet_state() -> dict[str, Any]:
    positions = get_poly_positions()
    open_positions_value = round(sum(float(p.get("current_value") or 0) for p in positions), 2)
    wallet = {
        "free_balance": None,
        "open_positions_value": open_positions_value,
        "portfolio_value": None,
        "source": "fallback",
        "positions": positions,
    }
    try:
        from py_clob_client import constants
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams

        secrets = _load_secrets()
        creds = ApiCreds(
            api_key=secrets.get("POLYMARKET_API_KEY"),
            api_secret=secrets.get("POLYMARKET_API_SECRET"),
            api_passphrase=secrets.get("POLYMARKET_PASSPHRASE"),
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=constants.POLYGON,
            key=secrets.get("POLYMARKET_PRIVATE_KEY"),
            signature_type=constants.L2,
            funder=secrets.get("POLYMARKET_FUNDER"),
            creds=creds,
        )
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        free_balance = float(bal.get("balance") or 0)
        wallet.update(
            {
                "free_balance": round(free_balance, 2),
                "portfolio_value": round(free_balance + open_positions_value, 2),
                "source": "live_api",
            }
        )
        return wallet
    except Exception:
        portfolio = _load_json(PORTFOLIO_FILE, {})
        free_balance = float(portfolio.get("wallet_balance_usd") or portfolio.get("current_capital_usd") or 0)
        wallet.update(
            {
                "free_balance": round(free_balance, 2),
                "portfolio_value": round(free_balance + open_positions_value, 2),
            }
        )
        return wallet


def get_poly_positions() -> list[dict[str, Any]]:
    raw = _load_json(POLY_POSITIONS, [])
    if isinstance(raw, dict):
        items = raw.values()
    elif isinstance(raw, list):
        items = raw
    else:
        items = []

    positions = []
    for pos in items:
        if not isinstance(pos, dict):
            continue
        amount = float(pos.get("amount") or 0)
        if amount <= 0:
            continue
        market = str(pos.get("market") or pos.get("title") or pos.get("asset") or pos.get("slug") or "Unknown Market")
        symbol = _clean_symbol(str(pos.get("engine") or ""), market, str(pos.get("slug") or ""))
        avg_price = float(pos.get("avg_price") or pos.get("entry_price") or 0)
        current_value = float(pos.get("current_value") or (avg_price * amount))
        cost_basis = float(pos.get("cost_basis") or pos.get("position_size_usd") or (avg_price * amount))
        cash_pnl = float(pos.get("cash_pnl") or pos.get("unrealized_pnl") or (current_value - cost_basis))
        positions.append(
            {
                "symbol": symbol,
                "market": market,
                "slug": pos.get("slug") or pos.get("market_slug") or "",
                "entry_price": avg_price,
                "avg_price": avg_price,
                "amount": amount,
                "current_value": round(current_value, 2),
                "cost_basis": round(cost_basis, 2),
                "unrealized_pnl": round(cash_pnl, 2),
                "cash_pnl": round(cash_pnl, 2),
                "outcome": pos.get("outcome") or pos.get("side") or "",
                "redeemable": bool(pos.get("redeemable")),
            }
        )
    return positions


def get_recent_trades(limit: int = 40) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT rowid, engine, asset, direction, entry_price, exit_price, position_size_usd,
                   pnl_absolute, pnl_percent, exit_type, timestamp_open, timestamp_close
            FROM trades
            ORDER BY COALESCE(timestamp_close, timestamp_open) DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        trades = []
        for row in rows:
            item = dict(row)
            item["symbol"] = _clean_symbol(item.get("engine"), item.get("asset"))
            item["timestamp"] = item.get("timestamp_close") or item.get("timestamp_open")
            item["status"] = item.get("exit_type") or ("open" if not item.get("timestamp_close") else "closed")
            trades.append(item)
        return trades
    finally:
        conn.close()


def get_stats_extended() -> dict[str, Any]:
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT pnl_absolute
            FROM trades
            WHERE timestamp_close IS NOT NULL
            ORDER BY timestamp_close DESC, rowid DESC
            LIMIT 500
            """
        ).fetchall()
    finally:
        conn.close()

    pnls = [float(row["pnl_absolute"] or 0) for row in rows]
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = sum(1 for pnl in pnls if pnl < 0)
    total = wins + losses
    streak_side = None
    streak_count = 0
    for pnl in pnls:
        side = "W" if pnl > 0 else "L" if pnl < 0 else None
        if side is None:
            continue
        if streak_side == side:
            streak_count += 1
        else:
            streak_side = side
            streak_count = 1
        if side != ("W" if pnls[0] > 0 else "L" if pnls[0] < 0 else None):
            break
    return {
        "win_rate": round((wins / total) * 100, 1) if total else 0.0,
        "wins": wins,
        "losses": losses,
        "total": total,
        "streak": f"{streak_count}{streak_side}" if streak_side else "N/A",
    }


def get_report_summary() -> dict[str, Any]:
    wallet = get_live_wallet_state()
    stats = get_stats_extended()
    recent = get_recent_trades(20)
    return {
        "current_capital": wallet.get("portfolio_value") or 0.0,
        "free_balance": wallet.get("free_balance") or 0.0,
        "open_positions_value": wallet.get("open_positions_value") or 0.0,
        "wallet_source": wallet.get("source"),
        "streak": stats.get("streak"),
        "recent_trades_count": len(recent),
        "timestamp": _now_iso(),
    }


def get_dashboard_summary() -> dict[str, Any]:
    wallet = get_live_wallet_state()
    positions = wallet.get("positions") or []
    recent_trades = get_recent_trades(20)
    return {
        "wallet": wallet,
        "positions": positions,
        "recent_trades": recent_trades,
        "event_history": broker.snapshot(EVENT_BACKLOG_LIMIT),
        "timestamp": _now_iso(),
    }


def get_latest_sale() -> dict[str, Any]:
    conn = _db()
    try:
        row = conn.execute(
            """
            SELECT engine, asset, direction, entry_price, exit_price,
                   position_size_usd, pnl_absolute, exit_type, timestamp_close
            FROM trades
            WHERE timestamp_close IS NOT NULL
            ORDER BY timestamp_close DESC, rowid DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row else {"message": "No resolved trades found"}
    finally:
        conn.close()


def get_redeemed() -> list[dict[str, Any]] | dict[str, str]:
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT engine, asset, direction, entry_price, exit_price,
                   position_size_usd, pnl_absolute, exit_type, timestamp_close
            FROM trades
            WHERE timestamp_close IS NOT NULL
            ORDER BY timestamp_close DESC, rowid DESC
            LIMIT 10
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


class EventBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=400)
        self._seq = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._seen_keys: deque[str] = deque(maxlen=1000)
        self._seen_lookup: set[str] = set()
        self._last_edge_rowid = 0
        self._last_trade_rowid = 0
        self._log_offsets = {engine: 0 for engine in LOG_SOURCES}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._prime_state()
        self._thread = threading.Thread(target=self._run, name="dashboard-event-broker", daemon=True)
        self._thread.start()

    def _prime_state(self) -> None:
        for engine, path in LOG_SOURCES.items():
            try:
                self._log_offsets[engine] = path.stat().st_size if path.exists() else 0
            except Exception:
                self._log_offsets[engine] = 0
        conn = _db()
        try:
            row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS max_rowid FROM edge_events").fetchone()
            self._last_edge_rowid = int(row["max_rowid"] or 0)
            row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS max_rowid FROM trades").fetchone()
            self._last_trade_rowid = int(row["max_rowid"] or 0)
            edge_rows = conn.execute(
                """
                SELECT rowid AS _rowid, *
                FROM edge_events
                ORDER BY _rowid DESC
                LIMIT 20
                """
            ).fetchall()
            for row in reversed(edge_rows):
                event = self._edge_row_to_event(dict(row))
                if event:
                    self.emit(event)

            trade_rows = conn.execute(
                """
                SELECT rowid AS _rowid, engine, asset, direction, entry_price, exit_price, position_size_usd,
                       pnl_absolute, pnl_percent, exit_type, timestamp_open, timestamp_close
                FROM trades
                WHERE timestamp_close IS NOT NULL
                ORDER BY _rowid DESC
                LIMIT 10
                """
            ).fetchall()
            for row in reversed(trade_rows):
                trade = dict(row)
                pnl = float(trade.get("pnl_absolute") or 0)
                event_type = "win" if pnl > 0 else "loss" if pnl < 0 else "redeem"
                self.emit(
                    {
                        "id": f"trade-close:{trade['_rowid']}",
                        "dedupe_key": f"trade-close:{trade['_rowid']}",
                        "event_type": event_type,
                        "engine": trade.get("engine"),
                        "asset": trade.get("asset"),
                        "symbol": _clean_symbol(trade.get("engine"), trade.get("asset")),
                        "timestamp": trade.get("timestamp_close") or trade.get("timestamp_open"),
                        "entry_price": trade.get("entry_price"),
                        "price": trade.get("exit_price"),
                        "amount": trade.get("position_size_usd"),
                        "pnl_absolute": pnl,
                        "pnl_percent": trade.get("pnl_percent"),
                        "exit_type": trade.get("exit_type"),
                        "message": (
                            f"{event_type.upper()} {trade.get('asset') or ''} "
                            f"{_fmt_money(pnl)} exit={trade.get('exit_price')}"
                        ).strip(),
                    }
                )
        finally:
            conn.close()

        for engine, path in LOG_SOURCES.items():
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
                for line in lines:
                    event = self._parse_log_line(engine, line)
                    if event:
                        self.emit(event)
            except Exception:
                continue

    def snapshot(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._events)[-limit:]

    def get_since(self, seq: int) -> tuple[int, list[dict[str, Any]]]:
        with self._lock:
            events = [event for event in self._events if event["seq"] > seq]
            return self._seq, events

    def emit(self, event: dict[str, Any]) -> None:
        key = str(event.get("dedupe_key") or event.get("id") or json.dumps(event, sort_keys=True))
        with self._lock:
            if key in self._seen_lookup:
                return
            self._seq += 1
            payload = dict(event)
            payload["seq"] = self._seq
            payload.setdefault("timestamp", _now_iso())
            payload.setdefault("live_timestamp", payload["timestamp"])
            payload.setdefault("engine", "")
            payload.setdefault("symbol", _clean_symbol(payload.get("engine"), payload.get("asset"), payload.get("market_slug")))
            payload.setdefault("event_type", "info")
            payload.setdefault("type", payload["event_type"])
            payload.setdefault("reason", payload.get("skip_reason"))
            self._events.append(payload)
            self._seen_keys.append(key)
            self._seen_lookup.add(key)
            while len(self._seen_lookup) > 900 and self._seen_keys:
                old = self._seen_keys.popleft()
                self._seen_lookup.discard(old)

    def _run(self) -> None:
        while self._running:
            try:
                self._poll_edge_events()
                self._poll_trades()
                self._poll_logs()
            except Exception as exc:
                print(f"[dashboard-events] poll error: {exc}", file=sys.stderr, flush=True)
            time.sleep(EVENT_POLL_SEC)

    def _poll_edge_events(self) -> None:
        conn = _db()
        try:
            rows = conn.execute(
                """
                SELECT rowid AS _rowid, *
                FROM edge_events
                WHERE rowid > ?
                ORDER BY _rowid ASC
                """,
                (self._last_edge_rowid,),
            ).fetchall()
            for row in rows:
                self._last_edge_rowid = int(row["_rowid"])
                event = self._edge_row_to_event(dict(row))
                if event:
                    self.emit(event)
        finally:
            conn.close()

    def _poll_trades(self) -> None:
        conn = _db()
        try:
            rows = conn.execute(
                """
                SELECT rowid AS _rowid, engine, asset, direction, entry_price, exit_price, position_size_usd,
                       pnl_absolute, pnl_percent, exit_type, timestamp_open, timestamp_close
                FROM trades
                WHERE rowid > ?
                ORDER BY _rowid ASC
                """,
                (self._last_trade_rowid,),
            ).fetchall()
            for row in rows:
                self._last_trade_rowid = int(row["_rowid"])
                trade = dict(row)
                if trade.get("timestamp_close"):
                    pnl = float(trade.get("pnl_absolute") or 0)
                    event_type = "win" if pnl > 0 else "loss" if pnl < 0 else "redeem"
                    self.emit(
                        {
                            "id": f"trade-close:{trade['_rowid']}",
                            "dedupe_key": f"trade-close:{trade['_rowid']}",
                            "event_type": event_type,
                            "engine": trade.get("engine"),
                            "asset": trade.get("asset"),
                            "symbol": _clean_symbol(trade.get("engine"), trade.get("asset")),
                            "timestamp": trade.get("timestamp_close") or trade.get("timestamp_open"),
                            "entry_price": trade.get("entry_price"),
                            "price": trade.get("exit_price"),
                            "amount": trade.get("position_size_usd"),
                            "pnl_absolute": pnl,
                            "pnl_percent": trade.get("pnl_percent"),
                            "exit_type": trade.get("exit_type"),
                            "message": (
                                f"{event_type.upper()} {trade.get('asset') or ''} "
                                f"{_fmt_money(pnl)} exit={trade.get('exit_price')}"
                            ).strip(),
                        }
                    )
        finally:
            conn.close()

    def _poll_logs(self) -> None:
        for engine, path in LOG_SOURCES.items():
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    handle.seek(self._log_offsets.get(engine, 0))
                    for line in handle:
                        event = self._parse_log_line(engine, line.rstrip())
                        if event:
                            self.emit(event)
                    self._log_offsets[engine] = handle.tell()
            except Exception:
                continue

    def _edge_row_to_event(self, row: dict[str, Any]) -> dict[str, Any] | None:
        symbol = _clean_symbol(row.get("engine"), row.get("asset"), row.get("market_slug"))
        execution_status = str(row.get("execution_status") or "")
        decision = str(row.get("decision") or "")
        skip_reason = row.get("skip_reason")
        timestamp = row.get("timestamp_et") or _now_iso()
        base = {
            "id": row.get("id"),
            "dedupe_key": f"edge:{row.get('id')}",
            "engine": row.get("engine"),
            "asset": row.get("asset"),
            "symbol": symbol,
            "timestamp": timestamp,
            "market_slug": row.get("market_slug"),
            "signal_type": row.get("signal_type"),
            "sec_remaining": row.get("seconds_remaining"),
            "entry_price": row.get("intended_entry_price"),
            "price": row.get("actual_fill_price") or row.get("intended_entry_price"),
            "amount": None,
            "side": row.get("side"),
            "reason": skip_reason,
            "skip_reason": skip_reason,
        }
        if execution_status in ("skipped", "blocked") or decision.startswith("skip") or skip_reason:
            reason = self._normalize_skip_reason(skip_reason or decision or execution_status)
            base.update(
                {
                    "event_type": "skip",
                    "reason": reason,
                    "skip_reason": reason,
                    "message": (
                        f"{row.get('engine')} {symbol} skipped: {reason}"
                        f" price={row.get('intended_entry_price')} sec={row.get('seconds_remaining')}"
                    ),
                }
            )
            return base
        if execution_status in ("posted", "pending"):
            base.update(
                {
                    "event_type": "order_placed",
                    "message": f"{row.get('engine')} {symbol} order placed @ {row.get('intended_entry_price')}",
                }
            )
            return base
        if execution_status in ("cancelled",):
            base.update(
                {
                    "event_type": "order_cancelled_fok",
                    "reason": "fok_fallback",
                    "message": f"{row.get('engine')} {symbol} order cancelled / FOK fallback",
                }
            )
            return base
        if execution_status in ("filled", "executed"):
            base.update(
                {
                    "event_type": "fill_verified",
                    "message": f"{row.get('engine')} {symbol} fill verified @ {row.get('actual_fill_price') or row.get('intended_entry_price')}",
                }
            )
            return base
        return None

    def _parse_log_line(self, engine: str, line: str) -> dict[str, Any] | None:
        ts_match = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        timestamp = ts_match.group(1) if ts_match else _now_iso()
        symbol = _clean_symbol(engine, engine)

        if "fill verified" in line.lower():
            size_match = re.search(r"size=([0-9.]+)", line)
            price_match = re.search(r"avg_fill_price=([0-9.]+)", line)
            return {
                "id": f"log-fill:{engine}:{timestamp}:{hash(line)}",
                "dedupe_key": f"log-fill:{engine}:{timestamp}:{hash(line)}",
                "event_type": "fill_verified",
                "engine": engine,
                "symbol": symbol,
                "timestamp": timestamp,
                "amount": float(size_match.group(1)) if size_match else None,
                "price": float(price_match.group(1)) if price_match else None,
                "message": line.split("] ", 1)[-1],
            }

        if "cancel by deadline" in line.lower() or "order cancelled (fok fallback)" in line.lower():
            order_match = re.search(r"order_id=([^\s]+)", line)
            sec_match = re.search(r"sec_rem=(\d+)", line)
            return {
                "id": f"log-cancel:{engine}:{timestamp}:{hash(line)}",
                "dedupe_key": f"log-cancel:{engine}:{timestamp}:{hash(line)}",
                "event_type": "order_cancelled_fok",
                "engine": engine,
                "symbol": symbol,
                "timestamp": timestamp,
                "order_id": order_match.group(1) if order_match else None,
                "sec_remaining": int(sec_match.group(1)) if sec_match else None,
                "reason": "fok_fallback",
                "message": line.split("] ", 1)[-1],
            }

        if "order placed" in line.lower():
            order_match = re.search(r"order_id=([^\s]+)", line)
            price_match = re.search(r"price=([0-9.]+)", line)
            return {
                "id": f"log-order:{engine}:{timestamp}:{hash(line)}",
                "dedupe_key": f"log-order:{engine}:{timestamp}:{hash(line)}",
                "event_type": "order_placed",
                "engine": engine,
                "symbol": symbol,
                "timestamp": timestamp,
                "order_id": order_match.group(1) if order_match else None,
                "price": float(price_match.group(1)) if price_match else None,
                "message": line.split("] ", 1)[-1],
            }

        if "redeem" in line.lower():
            return {
                "id": f"log-redeem:{engine}:{timestamp}:{hash(line)}",
                "dedupe_key": f"log-redeem:{engine}:{timestamp}:{hash(line)}",
                "event_type": "redeem",
                "engine": engine,
                "symbol": symbol,
                "timestamp": timestamp,
                "message": line.split("] ", 1)[-1],
            }

        if "skipping" in line.lower() or "skip " in line.lower():
            reason = self._reason_from_log_line(line)
            entry_match = re.search(r"entry_price=([0-9.]+)", line) or re.search(r"entry=([0-9.]+)", line)
            min_match = re.search(r"min ([0-9.]+)", line)
            max_match = re.search(r"max ([0-9.]+)", line) or re.search(r">= ([0-9.]+)", line)
            sec_match = re.search(r"sec_remaining=(\d+)s", line) or re.search(r"sec_rem=(\d+)", line)
            return {
                "id": f"log-skip:{engine}:{timestamp}:{hash(line)}",
                "dedupe_key": f"log-skip:{engine}:{timestamp}:{hash(line)}",
                "event_type": "skip",
                "engine": engine,
                "symbol": symbol,
                "timestamp": timestamp,
                "reason": reason,
                "skip_reason": reason,
                "entry_price": float(entry_match.group(1)) if entry_match else None,
                "min_price": float(min_match.group(1)) if min_match else None,
                "max_price": float(max_match.group(1)) if max_match else None,
                "sec_remaining": int(sec_match.group(1)) if sec_match else None,
                "message": line.split("] ", 1)[-1],
            }

        return None

    def _normalize_skip_reason(self, raw: str) -> str:
        text = str(raw or "").strip().lower()
        if not text:
            return "unknown"
        return re.sub(r"[^a-z0-9]+", "_", text).strip("_")

    def _reason_from_log_line(self, line: str) -> str:
        lower = line.lower()
        for marker, reason in SKIP_REASON_MAP.items():
            if marker in lower:
                return reason
        if "entry_price" in lower and "high" in lower:
            return "high_price"
        if "entry=" in lower and "min" in lower:
            return "low_price"
        return "skip"


broker = EventBroker()
broker.start()


class ReportWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/webhook":
            self.send_error(404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            update = json.loads(body)

            chat_id = None
            command = None
            if "message" in update and "text" in update["message"]:
                text = update["message"]["text"]
                chat_id = update["message"]["chat"]["id"]
                for cmd_name in COMMAND_MAP:
                    if text.startswith(cmd_name):
                        command = cmd_name
                        break

            if chat_id and command:
                self._send_json({"status": "accepted"})
                threading.Thread(target=handle_command_async, args=(command, chat_id), daemon=True).start()
            else:
                self._send_json({"status": "ignored"})
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json({"status": "ok"})
        elif path == "/api/short-report":
            self._send_text(generate_report("short"))
        elif path == "/api/long-report":
            self._send_text(generate_report("long"))
        elif path == "/api/full-report":
            self._send_text(generate_report("full"))
        elif path in ("/api/latest-sale", "/latest-sale"):
            self._send_json(get_latest_sale())
        elif path in ("/api/redeemed", "/redeemed"):
            self._send_json(get_redeemed())
        elif path in ("/api/system", "/system"):
            self._send_json(get_system())
        elif path in ("/api/report",):
            self._send_json(get_report_summary())
        elif path in ("/api/stats/extended",):
            self._send_json(get_stats_extended())
        elif path in ("/api/dashboard/summary",):
            self._send_json(get_dashboard_summary())
        elif path in ("/api/transactions",):
            self._send_json(broker.snapshot(COMPAT_HISTORY_LIMIT))
        elif path == "/api/events/stream":
            self._stream_events()
        else:
            self.send_error(404)

    def _stream_events(self) -> None:
        self._send_sse_headers()
        last_seq = 0
        try:
            backlog = broker.snapshot(EVENT_BACKLOG_LIMIT)
            for event in backlog:
                last_seq = max(last_seq, int(event.get("seq") or 0))
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
            self.wfile.flush()

            while True:
                seq, events = broker.get_since(last_seq)
                if events:
                    for event in events:
                        last_seq = max(last_seq, int(event.get("seq") or 0))
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
                last_seq = max(last_seq, seq)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                self.wfile.write(f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                return


def run_server(port: int = 18791) -> None:
    server_address = ("", port)
    httpd = ThreadingHTTPServer(server_address, ReportWebhookHandler)
    print(f"Report/dashboard server running on port {port}")
    print(f"Commands: {list(COMMAND_MAP.keys())}")
    httpd.serve_forever()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Trading dashboard and report webhook")
    parser.add_argument("--command", choices=["short", "long", "full"], help="Report type to generate")
    parser.add_argument("--target", help="Telegram chat ID")
    parser.add_argument("--server", action="store_true", help="Run as webhook/API server")
    parser.add_argument("--port", type=int, default=18791, help="Port for webhook server")
    parser.add_argument("--dry-run", action="store_true", help="Print to stdout instead of sending")
    args = parser.parse_args()

    if args.server:
        run_server(args.port)
    elif args.command and args.target:
        result = handle_command(args.command, args.target, args.dry_run)
        if args.dry_run:
            print("=== DRY RUN ===")
            print(result.get("report", ""))
        else:
            print(f"Command: {result.get('command', args.command)}")
            print(f"Status: {result.get('status')}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
