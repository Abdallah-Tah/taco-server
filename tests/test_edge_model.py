#!/usr/bin/env python3
"""Focused tests for deterministic shadow edge model."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from edge_model import score_edge  # noqa: E402


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
    assert out["net_edge"] > 0
    assert out["regime_ok"] == 1


def test_score_edge_skip_spread_when_too_wide():
    snapshot = {
        "ret_3s": 0.002,
        "spread": 0.25,
        "midprice": 0.5,
        "seconds_remaining": 120,
    }
    out = score_edge(snapshot)
    assert out["shadow_decision"] == "skip_spread"
    assert out["shadow_skip_reason"] == "spread_too_wide"
    assert out["regime_ok"] == 0


if __name__ == "__main__":
    tests = [
        test_score_edge_skip_when_insufficient,
        test_score_edge_place_yes_with_positive_momentum,
        test_score_edge_skip_spread_when_too_wide,
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
