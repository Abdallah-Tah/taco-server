#!/usr/bin/env python3
"""Focused tests for edge telemetry helper payload builders."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
if "requests" not in sys.modules:
    sys.modules["requests"] = types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None)

import polymarket_btc15m as btc15m  # noqa: E402
import polymarket_eth15m as eth15m  # noqa: E402


def test_btc_build_edge_event_payload_with_price_history():
    now_ts = 200
    btc15m._state["btc_prices"] = [
        (160, 100.0),
        (170, 102.0),
        (190, 104.0),
        (199, 105.0),
    ]
    market = {"id": "btc-market-1", "yes_price": 0.53, "no_price": 0.47}
    payload = btc15m.build_edge_event_payload(
        market=market,
        signal_type="snipe",
        decision="place_yes",
        side="UP",
        seconds_remaining=12,
        intended_entry_price=0.53,
        market_slug="btc-updown-15m-123",
        now_ts=now_ts,
    )
    assert payload["engine"] == "btc15m"
    assert payload["asset"] == "BTC"
    assert payload["market_id"] == "btc-market-1"
    assert payload["market_slug"] == "btc-updown-15m-123"
    assert payload["side"] == "YES"
    assert payload["decision"] == "place_yes"
    assert payload["price_now"] == 105.0
    assert payload["price_10s_ago"] == 104.0
    assert payload["ret_10s"] == (105.0 - 104.0) / 104.0
    assert payload["vol_10s"] is None
    assert payload["vol_30s"] is not None
    assert payload["best_bid"] is None
    assert payload["model_p_yes"] is None


def test_eth_build_edge_event_payload_skip_with_nulls():
    now_ts = 300
    eth15m._state["eth_prices"] = []
    market = {"id": "eth-market-1"}
    payload = eth15m.build_edge_event_payload(
        market=market,
        signal_type="maker_snipe",
        decision="skip_data",
        skip_reason="missing_eth_price",
        seconds_remaining=45,
        market_slug="eth-updown-15m-123",
        now_ts=now_ts,
    )
    assert payload["engine"] == "eth15m"
    assert payload["asset"] == "ETH"
    assert payload["decision"] == "skip_data"
    assert payload["skip_reason"] == "missing_eth_price"
    assert payload["price_now"] is None
    assert payload["ret_1s"] is None
    assert payload["vol_60s"] is None
    assert payload["side"] is None


if __name__ == "__main__":
    tests = [
        test_btc_build_edge_event_payload_with_price_history,
        test_eth_build_edge_event_payload_skip_with_nulls,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            import traceback
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(0 if failed == 0 else 1)
