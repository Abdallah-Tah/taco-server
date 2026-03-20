#!/usr/bin/env python3
"""Tests for milestones.py"""
import os
import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

# Redirect state files to temp dir for testing
_tmp_dir = tempfile.mkdtemp()
import milestones as _m
_m.MILESTONES_FILE = Path(_tmp_dir) / ".milestones.json"
_m.TRADE_LOG_FILE = Path(_tmp_dir) / ".trade_log.json"
_m.POLY_TRADE_LOG_FILE = Path(_tmp_dir) / ".poly_trade_log.json"


def _reset():
    if _m.MILESTONES_FILE.exists():
        _m.MILESTONES_FILE.unlink()


def test_no_milestone_below_threshold():
    _reset()
    newly = _m.check_milestones(100.0)
    assert newly == [], f"Expected no milestones at $100, got {newly}"


def test_first_milestone_reached():
    _reset()
    newly = _m.check_milestones(155.0)
    assert 150 in newly, f"Expected $150 milestone, got {newly}"


def test_milestone_not_double_counted():
    _reset()
    newly1 = _m.check_milestones(155.0)
    assert 150 in newly1
    # Second call — should not re-report
    newly2 = _m.check_milestones(155.0)
    assert 150 not in newly2, "Milestone should not be reported twice"


def test_multiple_milestones_at_once():
    _reset()
    newly = _m.check_milestones(300.0)
    assert 150 in newly, f"150 not in {newly}"
    assert 250 in newly, f"250 not in {newly}"
    assert 500 not in newly, f"500 should not be reached at $300"


def test_get_progress():
    _reset()
    _m.check_milestones(160.0)
    progress = _m.get_progress()
    assert 150 in progress["reached"]
    assert progress["next"] == 250
    assert progress["goal"] == _m.GOAL_USD


def test_all_milestones():
    _reset()
    newly = _m.check_milestones(9999.0)
    assert len(newly) == len(_m.MILESTONES), f"Expected all milestones, got {newly}"


if __name__ == "__main__":
    tests = [
        test_no_milestone_below_threshold,
        test_first_milestone_reached,
        test_milestone_not_double_counted,
        test_multiple_milestones_at_once,
        test_get_progress,
        test_all_milestones,
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
            print(f"  💥 {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{passed+failed} passed")
    sys.exit(0 if failed == 0 else 1)
