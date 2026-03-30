#!/usr/bin/env python3
"""Focused tests for deterministic shadow edge model."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from edge_model import (  # noqa: E402
    REGIME_THRESHOLDS,
    SHADOW_CONFIDENCE_FLOOR,
    SHADOW_EDGE_FLOOR,
    resolve_thresholds,
    score_edge,
)


def test_score_edge_skip_when_insufficient():
    out = score_edge({"seconds_remaining": None})
    assert out["shadow_decision"] == "skip_data"
    assert out["shadow_skip_reason"] == "insufficient_features"
    assert out["model_p_yes"] is None
    assert out["regime_ok"] is None


def test_score_edge_place_yes_with_positive_momentum():
    snapshot = {
        "ret_1s": 0.010,
        "ret_3s": 0.009,
        "ret_5s": 0.008,
        "ret_10s": 0.007,
        "ret_30s": 0.005,
        "spread": 0.01,
        "midprice": 0.50,
        "seconds_remaining": 20,
        "vol_10s": 0.003,
        "vol_30s": 0.004,
        "vol_60s": 0.005,
    }
    out = score_edge(snapshot, asset="BTC", engine="btc15m")
    assert out["shadow_decision"] == "place_yes"
    assert out["shadow_skip_reason"] is None
    assert 0.01 <= out["model_p_yes"] <= 0.99
    assert abs((out["model_p_yes"] + out["model_p_no"]) - 1.0) < 1e-9
    assert out["net_edge"] > REGIME_THRESHOLDS["trend"]["edge_floor"]
    assert out["confidence"] >= REGIME_THRESHOLDS["trend"]["confidence_floor"]
    assert out["regime_ok"] == 1
    assert out["regime"] == "trend"


def test_score_edge_skip_spread_when_too_wide():
    snapshot = {
        "ret_3s": 0.002,
        "spread": 0.25,
        "midprice": 0.5,
        "seconds_remaining": 120,
    }
    out = score_edge(snapshot, asset="BTC", engine="btc15m")
    assert out["shadow_decision"] == "skip_spread"
    assert out["shadow_skip_reason"] == "spread_too_wide"
    assert out["regime_ok"] == 0
    assert out["regime"] == "unstable"


def test_negative_net_edge_must_skip():
    snapshot = {
        "ret_1s": -0.010,
        "ret_3s": -0.009,
        "ret_5s": -0.008,
        "ret_10s": -0.007,
        "ret_30s": -0.005,
        "spread": 0.01,
        "midprice": 0.50,
        "seconds_remaining": 60,
        "vol_10s": 0.002,
        "vol_30s": 0.003,
        "vol_60s": 0.004,
    }
    out = score_edge(snapshot, asset="BTC", engine="btc15m")
    assert out["net_edge"] is not None
    assert out["net_edge"] <= 0
    assert out["shadow_decision"] == "skip_no_edge"
    assert out["shadow_skip_reason"] == "net_edge_non_positive"


def test_positive_below_floor_must_skip():
    snapshot = {
        "ret_1s": 0.0004,
        "ret_3s": 0.0003,
        "ret_5s": 0.0002,
        "ret_10s": 0.0002,
        "ret_30s": 0.0001,
        "spread": 0.01,
        "midprice": 0.50,
        "seconds_remaining": 60,
        "vol_10s": 0.001,
        "vol_30s": 0.001,
        "vol_60s": 0.001,
    }
    out = score_edge(snapshot)
    assert out["net_edge"] is not None
    assert out["confidence"] < SHADOW_CONFIDENCE_FLOOR
    assert out["shadow_decision"] == "skip_no_edge"
    assert out["shadow_skip_reason"] == "confidence_below_floor"


def test_positive_above_floor_and_fixed_thresholds_trade():
    snapshot = {
        "ret_1s": 0.0019,
        "ret_3s": 0.0,
        "ret_5s": 0.0,
        "ret_10s": 0.0,
        "ret_30s": 0.0,
        "spread": 0.01,
        "midprice": 0.50,
        "seconds_remaining": 60,
        "vol_10s": 0.001,
        "vol_30s": 0.001,
        "vol_60s": 0.001,
    }
    out = score_edge(snapshot)
    assert out["net_edge"] > SHADOW_EDGE_FLOOR
    assert out["confidence"] >= SHADOW_CONFIDENCE_FLOOR
    assert out["shadow_decision"] == "place_yes"
    assert out["shadow_skip_reason"] is None


def test_positive_above_floor_and_adequate_confidence_may_trade():
    snapshot = {
        "ret_1s": 0.0030,
        "ret_3s": 0.0028,
        "ret_5s": 0.0026,
        "ret_10s": 0.0024,
        "ret_30s": 0.0022,
        "spread": 0.01,
        "midprice": 0.50,
        "seconds_remaining": 60,
        "vol_10s": 0.001,
        "vol_30s": 0.001,
        "vol_60s": 0.001,
    }
    out = score_edge(snapshot)
    assert out["net_edge"] > SHADOW_EDGE_FLOOR
    assert out["confidence"] >= SHADOW_CONFIDENCE_FLOOR
    assert out["shadow_decision"] == "place_yes"
    assert out["shadow_skip_reason"] is None


def test_threshold_selection_by_regime():
    trend_snapshot = {
        "ret_1s": 0.010,
        "ret_3s": 0.009,
        "ret_5s": 0.008,
        "ret_10s": 0.007,
        "ret_30s": 0.005,
        "spread": 0.01,
        "vol_10s": 0.003,
        "vol_30s": 0.004,
        "vol_60s": 0.005,
    }
    chop_snapshot = {
        "ret_1s": 0.0020,
        "ret_3s": -0.0018,
        "ret_5s": 0.0019,
        "ret_10s": -0.0015,
        "ret_30s": 0.0013,
        "spread": 0.03,
        "vol_10s": 0.006,
        "vol_30s": 0.007,
        "vol_60s": 0.008,
    }
    unstable_snapshot = {
        "ret_1s": 0.006,
        "ret_3s": -0.007,
        "ret_5s": 0.008,
        "ret_10s": -0.009,
        "ret_30s": 0.007,
        "spread": 0.22,
        "vol_10s": 0.021,
        "vol_30s": 0.023,
        "vol_60s": 0.024,
    }
    trend = resolve_thresholds(trend_snapshot, asset="BTC", engine="btc15m")
    chop = resolve_thresholds(chop_snapshot, asset="BTC", engine="btc15m")
    unstable = resolve_thresholds(unstable_snapshot, asset="BTC", engine="btc15m")
    assert trend["regime"] == "trend"
    assert (trend["edge_floor"], trend["confidence_floor"]) == (0.02, 0.20)
    assert chop["regime"] == "chop"
    assert (chop["edge_floor"], chop["confidence_floor"]) == (0.015, 0.18)
    assert unstable["regime"] == "unstable"
    assert (unstable["edge_floor"], unstable["confidence_floor"]) == (0.025, 0.30)


def test_adaptive_thresholds_change_btc_only_decisions():
    snapshot = {
        "ret_1s": 0.0025,
        "ret_3s": -0.00225,
        "ret_5s": 0.002125,
        "ret_10s": -0.0020,
        "ret_30s": 0.001875,
        "spread": 0.02,
        "midprice": 0.50,
        "seconds_remaining": 120,
        "vol_10s": 0.004,
        "vol_30s": 0.005,
        "vol_60s": 0.006,
    }
    btc = score_edge(snapshot, asset="BTC", engine="btc15m")
    eth = score_edge(snapshot, asset="ETH", engine="eth15m")
    assert btc["regime"] == "chop"
    assert btc["adaptive_net_edge_floor"] == 0.015
    assert btc["adaptive_confidence_floor"] == 0.18
    assert btc["net_edge"] > btc["adaptive_net_edge_floor"]
    assert btc["confidence"] >= btc["adaptive_confidence_floor"]
    assert btc["shadow_decision"] == "place_yes"
    assert eth["regime"] == "fixed"
    assert eth["adaptive_net_edge_floor"] == SHADOW_EDGE_FLOOR
    assert eth["adaptive_confidence_floor"] == SHADOW_CONFIDENCE_FLOOR
    assert eth["shadow_decision"] != "place_yes"


if __name__ == "__main__":
    tests = [
        test_score_edge_skip_when_insufficient,
        test_score_edge_place_yes_with_positive_momentum,
        test_score_edge_skip_spread_when_too_wide,
        test_negative_net_edge_must_skip,
        test_positive_below_floor_must_skip,
        test_positive_above_floor_and_fixed_thresholds_trade,
        test_positive_above_floor_and_adequate_confidence_may_trade,
        test_threshold_selection_by_regime,
        test_adaptive_thresholds_change_btc_only_decisions,
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
