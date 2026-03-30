#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from edge_model import (
    SHADOW_CONFIDENCE_FLOOR,
    SHADOW_EDGE_FLOOR,
    decide_shadow_outcome,
)
from edge_report import build_report, infer_shadow_decision


class ShadowDecisionTests(unittest.TestCase):
    def test_negative_net_edge_is_skip(self):
        decision, reason = decide_shadow_outcome(net_edge=-0.02, confidence=0.5, regime_ok=1)
        self.assertEqual(decision, "skip_no_edge")
        self.assertEqual(reason, "net_edge_non_positive")

    def test_net_edge_below_floor_is_skip(self):
        decision, reason = decide_shadow_outcome(net_edge=SHADOW_EDGE_FLOOR / 2, confidence=0.5, regime_ok=1)
        self.assertEqual(decision, "skip_no_edge")
        self.assertEqual(reason, "net_edge_below_floor")

    def test_net_edge_above_floor_but_low_confidence_is_skip(self):
        decision, reason = decide_shadow_outcome(
            net_edge=SHADOW_EDGE_FLOOR + 0.01,
            confidence=SHADOW_CONFIDENCE_FLOOR - 0.01,
            regime_ok=1,
        )
        self.assertEqual(decision, "skip_no_edge")
        self.assertEqual(reason, "confidence_below_floor")

    def test_net_edge_above_floor_with_confidence_can_trade(self):
        decision, reason = decide_shadow_outcome(
            net_edge=SHADOW_EDGE_FLOOR + 0.01,
            confidence=SHADOW_CONFIDENCE_FLOOR,
            regime_ok=1,
        )
        self.assertEqual(decision, "place_yes")
        self.assertIsNone(reason)

    def test_report_classification_matches_stored_shadow_trade(self):
        decision, reason = infer_shadow_decision(
            {
                "shadow_decision": "shadow_trade",
                "shadow_skip_reason": None,
                "net_edge": 0.03,
                "confidence": 0.5,
                "regime_ok": 1,
                "spread": 0.01,
            },
            has_shadow_decision=True,
        )
        self.assertEqual(decision, "place_yes")
        self.assertIsNone(reason)

    def test_report_negative_edge_counts_do_not_mark_trade(self):
        rows = [
            {
                "asset": "BTC",
                "timestamp_et": "2026-03-27T10:00:00",
                "decision": "skip_example",
                "skip_reason": "example",
                "spread": 0.01,
                "net_edge": -0.03,
                "confidence": 0.4,
                "model_p_yes": 0.45,
                "model_p_no": 0.55,
                "regime_ok": 1,
            }
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            build_report(rows, has_shadow_decision=False)
        output = buf.getvalue()
        self.assertIn("negative-edge events: 1", output)
        self.assertIn("negative-edge shadow trades: 0", output)
        self.assertIn("negative-edge shadow skips: 1", output)


if __name__ == "__main__":
    unittest.main()
