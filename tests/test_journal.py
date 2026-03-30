#!/usr/bin/env python3
"""Tests for journal.py"""
import os
import sys
import tempfile
import json
from pathlib import Path

# Use a temp DB for testing
_tmp_dir = tempfile.mkdtemp()
os.environ["_JOURNAL_TEST_DB"] = os.path.join(_tmp_dir, "test_journal.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

# Monkey-patch DB_PATH before importing journal
import journal as _j
_j.DB_PATH = Path(_tmp_dir) / "test_journal.db"
_j.TRADE_LOG_JSON = Path(_tmp_dir) / ".trade_log.json"
_j.POLY_TRADE_LOG_JSON = Path(_tmp_dir) / ".poly_trade_log.json"


def test_log_trade_open():
    from journal import log_trade_open, get_trades, get_db
    tid = log_trade_open(
        engine="solana",
        asset="BONK",
        category="meme",
        direction="BUY",
        entry_price=0.000012,
        position_size=100000,
        position_size_usd=1.2,
        momentum_score=75.0,
        regime="normal",
    )
    assert tid is not None
    trades = get_trades()
    assert any(t["id"] == tid for t in trades), "Trade not found after log_trade_open"
    t = next(t for t in trades if t["id"] == tid)
    assert t["asset"] == "BONK"
    assert t["engine"] == "solana"


def test_log_trade_close():
    from journal import log_trade_open, log_trade_close, get_trades
    tid = log_trade_open(
        engine="polymarket",
        asset="Will BTC hit 100k?",
        direction="BUY",
        entry_price=0.45,
        position_size=10.0,
        position_size_usd=4.5,
    )
    ok = log_trade_close(
        trade_id=tid,
        exit_price=0.85,
        pnl_absolute=4.0,
        pnl_percent=88.9,
        exit_type="TAKE_PROFIT",
        hold_duration_seconds=3600,
    )
    assert ok
    trades = get_trades()
    t = next((t for t in trades if t["id"] == tid), None)
    assert t is not None
    assert t["exit_price"] == 0.85
    assert t["exit_type"] == "TAKE_PROFIT"
    assert t["pnl_absolute"] == 4.0


def test_get_trades_filter():
    from journal import log_trade_open, get_trades
    tid = log_trade_open(engine="solana", asset="WAR", direction="BUY", entry_price=0.001)
    solana_trades = get_trades(engine="solana")
    poly_trades = get_trades(engine="polymarket")
    assert any(t["id"] == tid for t in solana_trades)
    assert not any(t["id"] == tid for t in poly_trades)


def test_migrate_json_logs():
    from journal import migrate_json_logs

    # Write fake JSON logs
    trade_log = [
        {
            "id": "sol-001",
            "timestamp": "2025-01-01T10:00:00+00:00",
            "action": "BUY",
            "token_address": "So111...",
            "sym": "PEPE",
            "entry_price": 0.001,
            "amount": 1000,
            "pnl_absolute": 0.5,
            "regime": "normal",
        }
    ]
    poly_log = [
        {
            "id": "poly-001",
            "timestamp": "2025-01-02T10:00:00+00:00",
            "action": "BUY",
            "market": "Will ETH flip BTC?",
            "price": 0.3,
            "amount": 16.6,
        }
    ]

    _j.TRADE_LOG_JSON.write_text(json.dumps(trade_log))
    _j.POLY_TRADE_LOG_JSON.write_text(json.dumps(poly_log))

    n = migrate_json_logs()
    assert n >= 2, f"Expected at least 2 migrated, got {n}"

    trades = _j.get_trades()
    ids = [t["id"] for t in trades]
    assert "poly-001" in ids
    assert any(t["engine"] == "solana" for t in trades)


def test_db_init_creates_edge_events():
    from journal import get_db
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='edge_events'"
    ).fetchone()
    conn.close()
    assert row is not None


def test_log_edge_event_insert():
    from journal import get_db, log_edge_event
    event_id = log_edge_event(
        engine="polymarket",
        asset="BTC",
        timestamp_et="2026-03-27T11:30:00-04:00",
        market_slug="btc-above-100k",
        market_id="mkt-123",
        side="YES",
        signal_type="microprice_reversion",
        seconds_remaining=120,
        best_bid=0.49,
        best_ask=0.51,
        spread=0.02,
        midprice=0.50,
        microprice=0.505,
        price_now=0.505,
        model_p_yes=0.54,
        model_p_no=0.46,
        edge_yes=0.035,
        edge_no=-0.025,
        net_edge=0.01,
        confidence=0.62,
        regime_ok=True,
        decision="ENTER",
    )
    conn = get_db()
    row = conn.execute(
        "SELECT id, regime_ok, decision FROM edge_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["id"] == event_id
    assert row["regime_ok"] == 1
    assert row["decision"] == "ENTER"


def test_log_edge_event_nullable_fields():
    from journal import get_db, log_edge_event
    event_id = log_edge_event(
        engine="polymarket",
        asset="ETH",
        side="NO",
        regime_ok=None,
        skip_reason=None,
        intended_entry_price=None,
        actual_fill_price=None,
        slippage=None,
        decision=None,
    )
    conn = get_db()
    row = conn.execute(
        """
        SELECT regime_ok, skip_reason, intended_entry_price, actual_fill_price, slippage, decision
        FROM edge_events
        WHERE id = ?
        """,
        (event_id,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["regime_ok"] is None
    assert row["skip_reason"] is None
    assert row["intended_entry_price"] is None
    assert row["actual_fill_price"] is None
    assert row["slippage"] is None
    assert row["decision"] is None


def test_log_edge_event_ignores_extra_fields():
    from journal import get_db, log_edge_event
    conn = get_db()
    before = conn.execute("SELECT COUNT(*) AS c FROM edge_events").fetchone()["c"]
    conn.close()

    event_id = log_edge_event(
        engine="polymarket",
        asset="BTC",
        timestamp_et="2026-03-27T12:00:00-04:00",
        side="YES",
        decision="TEST",
        net_edge=0.02,
        confidence=0.7,
        extra_debug="ignored",
        bogus_1=123,
        bogus_2={"a": 1},
    )

    conn = get_db()
    after = conn.execute("SELECT COUNT(*) AS c FROM edge_events").fetchone()["c"]
    row = conn.execute(
        "SELECT id, asset, decision, net_edge, confidence FROM edge_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    conn.close()

    assert after == before + 1
    assert row is not None
    assert row["id"] == event_id
    assert row["asset"] == "BTC"
    assert row["decision"] == "TEST"
    assert row["net_edge"] == 0.02
    assert row["confidence"] == 0.7


if __name__ == "__main__":
    tests = [
        test_log_trade_open,
        test_log_trade_close,
        test_get_trades_filter,
        test_migrate_json_logs,
        test_db_init_creates_edge_events,
        test_log_edge_event_insert,
        test_log_edge_event_nullable_fields,
        test_log_edge_event_ignores_extra_fields,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  💥 {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(0 if failed == 0 else 1)
