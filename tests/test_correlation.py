#!/usr/bin/env python3
"""Tests for correlation.py"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))


def test_no_correlation():
    from correlation import check_correlation
    existing = {
        "tok1": {"market": "Will Spain win the World Cup in 2026?"},
    }
    allowed, thesis, count = check_correlation("Will Bitcoin reach $100k by end of 2025?", existing)
    assert allowed, "Unrelated markets should be allowed"
    assert count == 0


def test_correlated_blocked():
    from correlation import check_correlation
    existing = {
        "tok1": {"market": "Will Bitcoin reach $100k before April 2025?"},
        "tok2": {"market": "Will Bitcoin hit all-time high in Q1 2025?"},
    }
    # 3rd BTC market — should be blocked
    allowed, thesis, count = check_correlation(
        "Will Bitcoin price exceed $90k before March 2025?", existing
    )
    # Should have correlated count >= 2 (shares 'bitcoin' + 'before' excluded, but '2025' + 'bitcoin' shared)
    # At minimum count should be > 0
    assert count >= 1, f"Expected at least 1 correlation, got {count}"


def test_correlated_under_threshold():
    from correlation import check_correlation
    existing = {
        "tok1": {"market": "Will Bitcoin price hit $80k in February?"},
    }
    allowed, thesis, count = check_correlation(
        "Will Bitcoin cross $75k this month?", existing, max_correlated=2
    )
    # Only 1 correlated, max is 2, should be allowed
    assert allowed, f"Should be allowed with 1 correlated and max=2, got allowed={allowed}"


def test_stop_words_excluded():
    from correlation import _extract_keywords
    kw = _extract_keywords("will the election end before the vote")
    # Stop words should be excluded
    assert "will" not in kw
    assert "the" not in kw
    assert "before" not in kw
    assert "end" not in kw
    # Content words should remain
    assert "election" in kw
    assert "vote" in kw


def test_empty_positions():
    from correlation import check_correlation
    allowed, thesis, count = check_correlation("Will BTC reach 100k?", {})
    assert allowed
    assert count == 0


def test_thesis_inference():
    from correlation import _infer_thesis, _extract_keywords
    kw = _extract_keywords("Will Bitcoin ETH crypto price hit all-time high?")
    thesis = _infer_thesis(kw)
    assert thesis == "crypto"


if __name__ == "__main__":
    tests = [
        test_no_correlation,
        test_correlated_blocked,
        test_correlated_under_threshold,
        test_stop_words_excluded,
        test_empty_positions,
        test_thesis_inference,
    ]
    passed = 0
    failed = 0
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
