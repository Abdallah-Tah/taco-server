#!/usr/bin/env python3
"""Focused tests for edge report helper classification."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from edge_report import (  # noqa: E402
    disagreement_bucket,
    infer_shadow_decision,
    is_live_place,
    is_live_skip,
    net_edge_bucket,
    shadow_is_trade,
)


def test_live_decision_classification():
    assert is_live_place("place_yes")
    assert is_live_place("place_no")
    assert not is_live_place("skip_data")
    assert is_live_skip("skip_data")
    assert is_live_skip("skip_spread")
    assert not is_live_skip("place_yes")


def test_infer_shadow_decision_from_regime_and_edge():
    decision, reason = infer_shadow_decision(
        {"regime_ok": 0, "spread": 0.25, "net_edge": 0.1},
        has_shadow_decision=False,
    )
    assert decision == "skip_spread"
    assert reason == "spread_too_wide"

    decision, reason = infer_shadow_decision(
        {"regime_ok": 1, "net_edge": 0.03},
        has_shadow_decision=False,
    )
    assert decision == "place_yes"
    assert reason is None

    decision, reason = infer_shadow_decision(
        {"regime_ok": 1, "net_edge": 0.005},
        has_shadow_decision=False,
    )
    assert decision == "skip_no_edge"
    assert reason == "net_edge_below_floor"

    decision, reason = infer_shadow_decision(
        {"regime_ok": 1, "net_edge": -0.02},
        has_shadow_decision=False,
    )
    assert decision == "skip_no_edge"
    assert reason == "net_edge_non_positive"


def test_disagreement_bucket_and_net_edge_bucket():
    assert disagreement_bucket("place_yes", "skip_no_edge") == "live_place_shadow_skip"
    assert disagreement_bucket("skip_data", "place_yes") == "live_skip_shadow_trade"
    assert disagreement_bucket("place_no", "place_yes") == "both_place"
    assert disagreement_bucket("skip_spread", "skip_regime") == "both_skip"
    assert disagreement_bucket("unknown", None) == "unknown"

    assert net_edge_bucket(-0.01) == "<0"
    assert net_edge_bucket(0.0) == "0_to_0.01"
    assert net_edge_bucket(0.015) == "0.01_to_0.02"
    assert net_edge_bucket(0.03) == "0.02_to_0.05"
    assert net_edge_bucket(0.06) == ">0.05"
    assert net_edge_bucket(None) == "null"


def test_shadow_trade_classification_matches_semantics():
    assert shadow_is_trade("place_yes")
    assert not shadow_is_trade("place_no")
    assert not shadow_is_trade("skip_no_edge")


if __name__ == "__main__":
    tests = [
        test_live_decision_classification,
        test_infer_shadow_decision_from_regime_and_edge,
        test_disagreement_bucket_and_net_edge_bucket,
        test_shadow_trade_classification_matches_semantics,
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
