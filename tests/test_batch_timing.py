"""
Tests for the _bt / _bt_set / _bt_reset_all batch-timing helpers.

Regression context: prior versions stored absolute Unix timestamps in
bpy.props.FloatProperty (single-precision). A 10-digit Unix epoch rounds
to the nearest ~128 sec grid point in float32, so deltas of the form
(time.time() - stored) regularly came out negative — visible in
estim_log.jsonl as setup_actual_secs of -8 to -53 sec and bake actuals
of -13 to -18 sec.  These tests verify that the dict-based storage
preserves full double-precision and that deltas are exact.
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import _bt, _bt_set, _bt_reset_all, _batch_times


def _f32(x):
    """Round x to the nearest single-precision float (the bug's mechanism)."""
    return struct.unpack("f", struct.pack("f", x))[0]


class TestBatchTimingHelpers:
    def setup_method(self):
        _bt_reset_all()

    def test_default_is_zero(self):
        assert _bt("start_time") == 0.0
        assert _bt("job_start_time") == 0.0
        assert _bt("bake_start_time") == 0.0
        assert _bt("render_start_time") == 0.0
        assert _bt("still_start_time") == 0.0

    def test_unknown_key_returns_zero(self):
        # _bt uses dict.get(key, 0.0) — safe for misspellings.
        assert _bt("nonexistent") == 0.0

    def test_round_trip_preserves_full_precision(self):
        # The bug: float32 storage loses ~64 sec on a 1.78e9 timestamp.
        # The fix: store as Python float (double precision) so the exact
        # value round-trips bit-for-bit.
        ts = 1778125671.52
        _bt_set("job_start_time", ts)
        assert _bt("job_start_time") == ts

    def test_delta_against_current_time_is_nonnegative(self):
        # The headline regression: storing now and reading it back must
        # produce a delta >= 0, not the -8 to -53 sec we saw with float32.
        for key in _batch_times:
            now = time.time()
            _bt_set(key, now)
            delta = time.time() - _bt(key)
            assert delta >= 0, (
                f"{key}: delta {delta} should be non-negative "
                f"(stored {_bt(key)}, would-be float32 {_f32(now)})"
            )

    def test_float32_would_have_failed_at_modern_epoch(self):
        # Sanity check that the bug *would* still trigger if FloatProperty
        # were used: float32 of a 2026-era timestamp differs from the input
        # by enough to make the (now - stored) delta negative.
        ts = 1778125671.52
        f32_ts = _f32(ts)
        # The float32 rounding error must be larger than 1 sec (in fact ~24 sec)
        # to cause a problem; verify the test stays valid as time progresses.
        assert abs(f32_ts - ts) > 1.0, (
            "float32 precision is now adequate for this epoch — "
            "regression test may need to use a later timestamp"
        )

    def test_set_and_get_isolated_per_key(self):
        _bt_set("start_time", 100.0)
        _bt_set("job_start_time", 200.0)
        assert _bt("start_time") == 100.0
        assert _bt("job_start_time") == 200.0
        # Other keys remain at 0.0
        assert _bt("bake_start_time") == 0.0

    def test_reset_all_clears_every_key(self):
        for k in _batch_times:
            _bt_set(k, 12345.6)
        _bt_reset_all()
        for k in _batch_times:
            assert _bt(k) == 0.0
