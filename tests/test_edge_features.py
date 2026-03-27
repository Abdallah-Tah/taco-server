#!/usr/bin/env python3
"""Focused tests for edge feature helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from edge_features import build_feature_snapshot, midprice, safe_spread  # noqa: E402


def test_safe_spread_and_midprice():
    assert abs(safe_spread(0.49, 0.51) - 0.02) < 1e-12
    assert safe_spread(None, 0.51) is None
    assert midprice(0.49, 0.51) == 0.5
    assert midprice(None, None, fallback_price=0.44) == 0.44


def test_build_feature_snapshot_with_history_and_depth():
    points = [
        (100, 100.0),
        (110, 101.0),
        (120, 102.0),
        (129, 103.0),
    ]
    snapshot = build_feature_snapshot(
        price_points=points,
        now_ts=130,
        best_bid=0.49,
        best_ask=0.51,
        bid_size=120,
        ask_size=80,
        depth_bids=[(0.49, 120), (0.48, 90), (0.47, 70)],
        depth_asks=[(0.51, 80), (0.52, 100), (0.53, 110)],
        seconds_remaining=20,
    )
    assert snapshot["price_now"] == 103.0
    assert snapshot["price_10s_ago"] == 102.0
    assert abs(snapshot["ret_10s"] - ((103.0 - 102.0) / 102.0)) < 1e-12
    assert abs(snapshot["spread"] - 0.02) < 1e-12
    assert snapshot["midprice"] == 0.5
    assert snapshot["microprice"] is not None
    assert snapshot["vol_30s"] is not None
    assert snapshot["imbalance_1"] == (120 - 80) / (120 + 80)
    assert snapshot["imbalance_3"] is not None


def test_build_feature_snapshot_handles_null_inputs():
    snapshot = build_feature_snapshot(
        price_points=[],
        now_ts=500,
        best_bid=None,
        best_ask=None,
        depth_bids=None,
        depth_asks=None,
    )
    assert snapshot["price_now"] is None
    assert snapshot["ret_1s"] is None
    assert snapshot["vol_10s"] is None
    assert snapshot["microprice"] is None
    assert snapshot["imbalance_1"] is None
    assert snapshot["imbalance_3"] is None


if __name__ == "__main__":
    tests = [
        test_safe_spread_and_midprice,
        test_build_feature_snapshot_with_history_and_depth,
        test_build_feature_snapshot_handles_null_inputs,
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
