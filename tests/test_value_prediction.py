"""Tests for _next_list_value — pattern detection and bounds clamping."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import _next_list_value


class TestArithmetic:
    def test_basic_sequence(self):
        assert _next_list_value([64, 128], 64, 8.0, None) == 192

    def test_descending_sequence(self):
        # 256 → 128 → 64 — next should be 32 (valid, >= 8)
        assert _next_list_value([256, 128, 64], 64, 8.0, None) == 32

    def test_descending_would_go_below_min(self):
        # 64 → 0: step = -64, next would be -64 which is < min=8
        # must fall back to default (64), not return -64
        result = _next_list_value([64, 0], 64, 8.0, None)
        assert result >= 8, f"got {result}, expected >= 8"

    def test_single_item_returns_default(self):
        assert _next_list_value([64], 64, 8.0, None) == 64

    def test_empty_returns_default(self):
        assert _next_list_value([], 64, 8.0, None) == 64

    def test_negative_step_clamped_to_fallback(self):
        # 32 → 8: step = -24, next = -16 which is < 8 → fallback = max(64, 8) = 64
        result = _next_list_value([32, 8], 64, 8.0, None)
        assert result >= 8, f"got {result}, expected >= 8"

    def test_bounds_none_still_works(self):
        # No bounds — arithmetic pattern should work freely
        assert _next_list_value([1.0, 2.0, 3.0], 1.0, None, None) == 4.0

    def test_max_bound_enforced(self):
        result = _next_list_value([500, 1000], 64, 8.0, 1024.0)
        assert result <= 1024.0, f"got {result}, expected <= 1024"


class TestGeometric:
    def test_doubling_sequence(self):
        assert _next_list_value([1, 2, 4], 1, 0.0, None) == 8

    def test_geometric_clamped_at_max(self):
        result = _next_list_value([256, 512], 64, 8.0, 1024.0)
        assert result <= 1024.0


class TestFallback:
    def test_mixed_pattern_repeats_default(self):
        # No clear arithmetic or geometric pattern → fallback = clamped default
        result = _next_list_value([1, 3, 10], 64, 8.0, None)
        assert result >= 8

    def test_fallback_respects_min(self):
        # Default is below min — fallback should be clamped up
        result = _next_list_value([1, 3, 10], 4, 8.0, None)
        assert result >= 8
