"""Tests for expand_param, generate_jobs_limited, generate_jobs_all, and make_name."""
import sys, os
import types
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import (
    expand_param,
    generate_jobs_limited,
    generate_jobs_all,
    make_name,
    _dedupe_jobs,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PARAM_NAMES = [
    "resolution", "vorticity", "alpha", "beta",
    "dissolve_speed", "noise_upres", "noise_strength", "noise_spatial_scale",
]

_BASE_VALUES = {
    "resolution": 64,
    "vorticity": 0.0,
    "alpha": 1.0,
    "beta": 1.0,
    "dissolve_speed": 5,
    "noise_upres": 2,
    "noise_strength": 2.0,
    "noise_spatial_scale": 2.0,
}


def _item(value):
    """Minimal stand-in for a ValueItem CollectionProperty entry."""
    return types.SimpleNamespace(value=float(value))


def _make_settings(**overrides):
    """
    Return a SimpleNamespace with all SmokeSettings attributes at defaults.
    Pass overrides as keyword arguments to change specific values.
    """
    d = dict(_BASE_VALUES)
    d.update({
        "use_dissolve":          False,
        "slow_dissolve":         False,
        "iterate_dissolve_both": False,
        "use_noise":             False,
        "iterate_noise_both":    False,
        "iteration_mode":        "LIMITED",
    })
    for param in _PARAM_NAMES:
        d[f"{param}_use_list"]  = False
        d[f"{param}_use_range"] = False
        d[f"{param}_list"]      = []
        d[f"{param}_begin"]     = d[param]
        d[f"{param}_end"]       = d[param]
        d[f"{param}_step"]      = 0
    d.update(overrides)
    return types.SimpleNamespace(**d)


# ---------------------------------------------------------------------------
# expand_param
# ---------------------------------------------------------------------------

class TestExpandParam:
    def test_single_value_mode_returns_begin(self):
        assert expand_param(_make_settings(), "resolution") == [64]

    def test_list_mode_returns_values(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(128), _item(256)],
        )
        assert expand_param(s, "resolution") == [128.0, 256.0]

    def test_empty_list_falls_back_to_begin(self):
        s = _make_settings(resolution_use_list=True, resolution_list=[])
        assert expand_param(s, "resolution") == [64]

    def test_range_zero_step_returns_begin(self):
        s = _make_settings(
            resolution_use_range=True,
            resolution_begin=128, resolution_end=512, resolution_step=0,
        )
        assert expand_param(s, "resolution") == [128]

    def test_integer_range(self):
        s = _make_settings(
            resolution_use_range=True,
            resolution_begin=64, resolution_end=256, resolution_step=64,
        )
        assert expand_param(s, "resolution") == [64, 128, 192, 256]

    def test_float_range(self):
        s = _make_settings(
            vorticity_use_range=True,
            vorticity_begin=0.5, vorticity_end=1.5, vorticity_step=0.5,
        )
        result = expand_param(s, "vorticity")
        assert len(result) == 3
        assert result[0] == pytest.approx(0.5)
        assert result[2] == pytest.approx(1.5)

    def test_list_takes_priority_over_range(self):
        # Both enabled — list wins because expand_param checks list first.
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(99)],
            resolution_use_range=True,
            resolution_begin=64, resolution_end=256, resolution_step=64,
        )
        assert expand_param(s, "resolution") == [99.0]


# ---------------------------------------------------------------------------
# generate_jobs_limited
# ---------------------------------------------------------------------------

class TestGenerateJobsLimited:
    def test_no_sweeps_yields_one_baseline_job(self):
        # When no axis is swept and no iterate-both is configured, the
        # generator falls back to a single baseline job rather than yielding
        # nothing. Testing one specific param combination is a valid use case
        # and should not require enabling All Combinations mode.
        jobs = list(generate_jobs_limited(_make_settings()))
        assert len(jobs) == 1
        # The single job uses every axis's default value.
        for param, expected in _BASE_VALUES.items():
            assert jobs[0][param] == expected, f"{param}: expected {expected}, got {jobs[0][param]}"

    def test_single_item_list_yields_one_job(self):
        # Regression: previously a 1-item list + 4 zero-step gas params produced 4 jobs.
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(128)],
        )
        jobs = list(generate_jobs_limited(s))
        assert len(jobs) == 1
        assert jobs[0]["resolution"] == 128.0

    def test_three_item_list_yields_three_jobs(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128), _item(256)],
        )
        assert len(list(generate_jobs_limited(s))) == 3

    def test_zero_step_range_yields_one_baseline_job(self):
        # Range enabled but step=0 produces no variation — not a real sweep,
        # so the fallback emits one baseline job (matching no-sweeps behavior).
        s = _make_settings(
            vorticity_use_range=True,
            vorticity_begin=0.5, vorticity_end=2.0, vorticity_step=0,
        )
        jobs = list(generate_jobs_limited(s))
        assert len(jobs) == 1

    def test_no_sweeps_baseline_not_emitted_when_iterate_both_active(self):
        # iterate_both already adds an explicit comparison job, so the
        # fallback baseline must NOT also fire — otherwise we'd get an
        # unintended extra job.
        s = _make_settings(use_dissolve=True, iterate_dissolve_both=True)
        jobs = list(generate_jobs_limited(s))
        # Exactly one off-pass job, no extra baseline.
        assert len(jobs) == 1
        assert jobs[0]["use_dissolve"] is False

    def test_nonzero_range_yields_correct_count(self):
        s = _make_settings(
            vorticity_use_range=True,
            vorticity_begin=0.5, vorticity_end=1.5, vorticity_step=0.5,
        )
        jobs = list(generate_jobs_limited(s))
        assert len(jobs) == 3
        assert jobs[0]["vorticity"] == pytest.approx(0.5)
        assert jobs[2]["vorticity"] == pytest.approx(1.5)

    def test_two_params_job_counts_sum(self):
        # Limited mode: 2 res + 3 vorticity = 5 total (not 6 Cartesian).
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
            vorticity_use_list=True,
            vorticity_list=[_item(0.5), _item(1.0), _item(1.5)],
        )
        assert len(list(generate_jobs_limited(s))) == 5

    def test_sweeping_resolution_leaves_vorticity_at_default(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
        )
        for job in generate_jobs_limited(s):
            assert job["vorticity"] == pytest.approx(0.0)

    def test_dissolve_ignored_when_disabled(self):
        # use_dissolve=False means the range is ignored; with no other sweeps
        # the fallback emits one baseline job with dissolve disabled.
        s = _make_settings(
            use_dissolve=False,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=10, dissolve_speed_end=50, dissolve_speed_step=10,
        )
        jobs = list(generate_jobs_limited(s))
        assert len(jobs) == 1
        assert jobs[0]["use_dissolve"] is False

    def test_dissolve_swept_when_enabled(self):
        s = _make_settings(
            use_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=10, dissolve_speed_end=30, dissolve_speed_step=10,
        )
        assert len(list(generate_jobs_limited(s))) == 3

    def test_noise_ignored_when_disabled(self):
        # use_noise=False means the range is ignored; with no other sweeps
        # the fallback emits one baseline job with noise disabled.
        s = _make_settings(
            use_noise=False,
            noise_strength_use_range=True,
            noise_strength_begin=0.5, noise_strength_end=1.5, noise_strength_step=0.5,
        )
        jobs = list(generate_jobs_limited(s))
        assert len(jobs) == 1
        assert jobs[0]["use_noise"] is False

    def test_noise_swept_when_enabled(self):
        s = _make_settings(
            use_noise=True,
            noise_strength_use_range=True,
            noise_strength_begin=0.5, noise_strength_end=1.5, noise_strength_step=0.5,
        )
        assert len(list(generate_jobs_limited(s))) == 3

    def test_job_contains_required_keys(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(128)],
        )
        job = list(generate_jobs_limited(s))[0]
        for key in ("resolution", "vorticity", "alpha", "beta",
                    "dissolve_speed", "noise_upres", "noise_strength",
                    "noise_spatial_scale", "use_dissolve", "use_noise"):
            assert key in job, f"missing key: {key}"


# ---------------------------------------------------------------------------
# generate_jobs_all
# ---------------------------------------------------------------------------

class TestGenerateJobsAll:
    def test_all_defaults_yields_one_job(self):
        assert len(list(generate_jobs_all(_make_settings()))) == 1

    def test_two_resolution_values(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
        )
        assert len(list(generate_jobs_all(s))) == 2

    def test_cartesian_product(self):
        # 2 resolutions × 3 vorticities = 6 jobs.
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
            vorticity_use_range=True,
            vorticity_begin=0.5, vorticity_end=1.5, vorticity_step=0.5,
        )
        jobs = list(generate_jobs_all(s))
        assert len(jobs) == 6
        combos = {(j["resolution"], round(j["vorticity"], 1)) for j in jobs}
        assert combos == {
            (64, 0.5), (64, 1.0), (64, 1.5),
            (128, 0.5), (128, 1.0), (128, 1.5),
        }

    def test_disabled_dissolve_does_not_multiply(self):
        # dissolve range should be ignored when use_dissolve=False.
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
            use_dissolve=False,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=10, dissolve_speed_end=30, dissolve_speed_step=10,
        )
        assert len(list(generate_jobs_all(s))) == 2

    def test_enabled_dissolve_multiplies(self):
        # 2 res × 3 dissolve = 6 jobs when use_dissolve=True.
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
            use_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=10, dissolve_speed_end=30, dissolve_speed_step=10,
        )
        assert len(list(generate_jobs_all(s))) == 6

    def test_disabled_noise_does_not_multiply(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128)],
            use_noise=False,
            noise_strength_use_range=True,
            noise_strength_begin=0.5, noise_strength_end=1.5, noise_strength_step=0.5,
        )
        assert len(list(generate_jobs_all(s))) == 2

    def test_all_combinations_larger_product(self):
        # 3 res × 2 vorticity × 2 alpha = 12 jobs.
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(64), _item(128), _item(256)],
            vorticity_use_list=True,
            vorticity_list=[_item(0.5), _item(1.5)],
            alpha_use_list=True,
            alpha_list=[_item(0.5), _item(1.5)],
        )
        assert len(list(generate_jobs_all(s))) == 12


# ---------------------------------------------------------------------------
# Iterate-both: dissolve on/off and noise on/off
# ---------------------------------------------------------------------------

class TestIterateDissolveBoth:
    def test_limited_adds_off_job_when_dissolve_on(self):
        # use_dissolve=True + iterate_dissolve_both=True → one normal job + one off job
        s = _make_settings(
            use_dissolve=True,
            iterate_dissolve_both=True,
            dissolve_speed_use_list=True,
            dissolve_speed_list=[_item(30)],
        )
        jobs = list(generate_jobs_limited(s))
        # 1 dissolve sweep job + 1 off job
        assert len(jobs) == 2
        on_jobs  = [j for j in jobs if j["use_dissolve"] is True]
        off_jobs = [j for j in jobs if j["use_dissolve"] is False]
        assert len(on_jobs)  == 1
        assert len(off_jobs) == 1

    def test_limited_no_sweep_still_adds_off_job(self):
        # No dissolve_speed sweep configured — iterate_both still adds one off job.
        s = _make_settings(use_dissolve=True, iterate_dissolve_both=True)
        jobs = list(generate_jobs_limited(s))
        off_jobs = [j for j in jobs if j["use_dissolve"] is False]
        assert len(off_jobs) == 1

    def test_limited_iterate_off_by_default(self):
        s = _make_settings(
            use_dissolve=True,
            dissolve_speed_use_list=True,
            dissolve_speed_list=[_item(30), _item(50)],
        )
        jobs = list(generate_jobs_limited(s))
        # Only the 2 dissolve-on sweep jobs, no extra off job
        assert all(j["use_dissolve"] is True for j in jobs)
        assert len(jobs) == 2

    def test_all_adds_off_pass_when_dissolve_on(self):
        # iterate_dissolve_both doubles the Cartesian product with dissolve off.
        s = _make_settings(
            use_dissolve=True,
            iterate_dissolve_both=True,
            dissolve_speed_use_list=True,
            dissolve_speed_list=[_item(30), _item(50)],
        )
        jobs = list(generate_jobs_all(s))
        # 2 dissolve-on jobs + 1 dissolve-off job (single dissolve placeholder)
        on_jobs  = [j for j in jobs if j["use_dissolve"] is True]
        off_jobs = [j for j in jobs if j["use_dissolve"] is False]
        assert len(on_jobs)  == 2
        assert len(off_jobs) == 1

    def test_all_no_extra_pass_when_flag_off(self):
        s = _make_settings(
            use_dissolve=True,
            iterate_dissolve_both=False,
            dissolve_speed_use_list=True,
            dissolve_speed_list=[_item(30), _item(50)],
        )
        jobs = list(generate_jobs_all(s))
        assert all(j["use_dissolve"] is True for j in jobs)
        assert len(jobs) == 2

    def test_all_backward_compat_dissolve_off_unchanged(self):
        # use_dissolve=False without iterate flag → existing behavior unchanged.
        s = _make_settings(use_dissolve=False, iterate_dissolve_both=False)
        jobs = list(generate_jobs_all(s))
        assert len(jobs) == 1
        assert jobs[0]["use_dissolve"] is False


class TestIterateNoiseBoth:
    def test_limited_adds_off_job_when_noise_on(self):
        s = _make_settings(
            use_noise=True,
            iterate_noise_both=True,
            noise_strength_use_list=True,
            noise_strength_list=[_item(0.5), _item(1.0)],
        )
        jobs = list(generate_jobs_limited(s))
        # 2 noise-on + 1 noise-off
        on_jobs  = [j for j in jobs if j["use_noise"] is True]
        off_jobs = [j for j in jobs if j["use_noise"] is False]
        assert len(on_jobs)  == 2
        assert len(off_jobs) == 1

    def test_all_adds_off_pass_when_noise_on(self):
        s = _make_settings(
            use_noise=True,
            iterate_noise_both=True,
            noise_upres_use_list=True,
            noise_upres_list=[_item(2), _item(4)],
        )
        jobs = list(generate_jobs_all(s))
        on_jobs  = [j for j in jobs if j["use_noise"] is True]
        off_jobs = [j for j in jobs if j["use_noise"] is False]
        assert len(on_jobs)  == 2
        assert len(off_jobs) == 1

    def test_both_flags_combine(self):
        # iterate_dissolve_both + iterate_noise_both → 4 state combinations.
        s = _make_settings(
            use_dissolve=True,
            iterate_dissolve_both=True,
            use_noise=True,
            iterate_noise_both=True,
        )
        jobs = list(generate_jobs_all(s))
        state_pairs = {(j["use_dissolve"], j["use_noise"]) for j in jobs}
        assert (True,  True)  in state_pairs
        assert (True,  False) in state_pairs
        assert (False, True)  in state_pairs
        assert (False, False) in state_pairs

    # ── Regression: iterate_both off-pass used undefined properties ──────────
    # Before the fix, the "off" pass in generate_jobs_all referenced s.dissolve_speed,
    # s.noise_upres, etc. — which don't exist as Blender RNA properties (only the
    # _begin variants are registered).  The fix uses expand_param(s, name)[0].
    # These tests verify that:
    #  (a) the off-pass job carries the correct _begin value
    #  (b) generate_jobs_all does not raise AttributeError when the shorthand
    #      attribute is absent (simulating the Blender RNA environment)

    def test_all_dissolve_off_job_uses_begin_value(self):
        # dissolve_speed_begin=99 (non-default); the off-pass job must use 99,
        # not the base default of 5.
        s = _make_settings(
            use_dissolve=True,
            iterate_dissolve_both=True,
            dissolve_speed_begin=99,
            dissolve_speed_end=99,
        )
        jobs = list(generate_jobs_all(s))
        off_jobs = [j for j in jobs if not j["use_dissolve"]]
        assert len(off_jobs) == 1
        assert off_jobs[0]["dissolve_speed"] == 99

    def test_all_noise_off_job_uses_begin_values(self):
        # Verify all three noise params in the off-pass come from _begin.
        s = _make_settings(
            use_noise=True,
            iterate_noise_both=True,
            noise_upres_begin=8,      noise_upres_end=8,
            noise_strength_begin=3.5, noise_strength_end=3.5,
            noise_spatial_scale_begin=1.5, noise_spatial_scale_end=1.5,
        )
        jobs = list(generate_jobs_all(s))
        off_jobs = [j for j in jobs if not j["use_noise"]]
        assert len(off_jobs) == 1
        assert off_jobs[0]["noise_upres"]         == 8
        assert off_jobs[0]["noise_strength"]       == pytest.approx(3.5)
        assert off_jobs[0]["noise_spatial_scale"]  == pytest.approx(1.5)

    def test_all_no_shorthand_attr_does_not_raise(self):
        # Simulate Blender: remove the base shorthand attributes so that only
        # _begin variants exist.  This would have caused AttributeError before the fix.
        s = _make_settings(
            use_dissolve=True,
            iterate_dissolve_both=True,
            use_noise=True,
            iterate_noise_both=True,
        )
        del s.dissolve_speed
        del s.noise_upres
        del s.noise_strength
        del s.noise_spatial_scale
        # Must not raise AttributeError.
        jobs = list(generate_jobs_all(s))
        assert len(jobs) == 4  # (dissolve on/off) × (noise on/off)


# ---------------------------------------------------------------------------
# make_name
# ---------------------------------------------------------------------------

class TestMakeName:
    def _base_params(self, **overrides):
        p = {
            "resolution":          64,
            "vorticity":           0.0,
            "alpha":               1.0,
            "beta":                1.0,
            "dissolve_speed":      5,
            "noise_upres":         2,
            "noise_strength":      2.0,
            "noise_spatial_scale": 2.0,
            "use_dissolve":        True,
            "slow_dissolve":       False,
            "use_noise":           True,
        }
        p.update(overrides)
        return p

    def test_basic_format(self):
        p = self._base_params()
        name = make_name(p)
        assert name == "R64_V0.0_A1.0_B1.0_D5_N2_NS2.0_SC2.0"

    def test_dissolve_off(self):
        p = self._base_params(use_dissolve=False)
        name = make_name(p)
        assert "D-OFF" in name
        assert "D5" not in name

    def test_noise_off(self):
        p = self._base_params(use_noise=False)
        name = make_name(p)
        assert "N-OFF" in name
        assert "N2" not in name

    def test_both_off(self):
        p = self._base_params(use_dissolve=False, use_noise=False)
        name = make_name(p)
        assert "D-OFF" in name
        assert "N-OFF" in name

    def test_float_rounding(self):
        # Floats are rounded to 2 decimal places in the name.
        p = self._base_params(vorticity=0.333333, alpha=-0.666667)
        name = make_name(p)
        assert "V0.33" in name
        assert "A-0.67" in name

    def test_resolution_is_integer(self):
        p = self._base_params(resolution=128.0)
        name = make_name(p)
        assert name.startswith("R128_")

    def test_same_params_same_name(self):
        p1 = self._base_params(resolution=256, vorticity=1.5)
        p2 = self._base_params(resolution=256, vorticity=1.5)
        assert make_name(p1) == make_name(p2)

    def test_different_params_different_names(self):
        p1 = self._base_params(resolution=64)
        p2 = self._base_params(resolution=128)
        assert make_name(p1) != make_name(p2)


# ---------------------------------------------------------------------------
# _dedupe_jobs — collapses identical jobs produced by overlapping sweeps
# ---------------------------------------------------------------------------

class TestDedupeJobs:
    def _job(self, **overrides):
        p = {
            "resolution":          128,
            "vorticity":           0.0,
            "alpha":               1.0,
            "beta":                1.0,
            "dissolve_speed":      300,
            "noise_upres":         1,
            "noise_strength":      0.25,
            "noise_spatial_scale": 0.5,
            "use_dissolve":        True,
            "slow_dissolve":       False,
            "use_noise":           True,
        }
        p.update(overrides)
        return p

    def test_empty_list_returns_empty(self):
        assert _dedupe_jobs([]) == []

    def test_all_unique_unchanged(self):
        jobs = [self._job(vorticity=0.0),
                self._job(vorticity=0.1),
                self._job(vorticity=0.2)]
        assert _dedupe_jobs(jobs) == jobs

    def test_all_identical_collapsed_to_one(self):
        jobs = [self._job(), self._job(), self._job()]
        result = _dedupe_jobs(jobs)
        assert len(result) == 1
        assert result[0] == self._job()

    def test_preserves_first_seen_order(self):
        a = self._job(vorticity=0.1)
        b = self._job(vorticity=0.2)
        c = self._job(vorticity=0.3)
        jobs = [a, b, a, c, b]  # duplicates after first occurrence
        assert _dedupe_jobs(jobs) == [a, b, c]

    def test_differs_only_in_slow_dissolve_kept_separate(self):
        # slow_dissolve doesn't appear in make_name but does affect the bake,
        # so two jobs differing only in slow_dissolve are NOT duplicates.
        a = self._job(slow_dissolve=False)
        b = self._job(slow_dissolve=True)
        assert _dedupe_jobs([a, b]) == [a, b]

    def test_user_batch_regression(self):
        # Reproduces the May-18 production batch: 38 jobs from 8 axis sweeps
        # where each sweep starts at the axis's default value, producing 8
        # baseline duplicates targeting the same cache directory.
        s = _make_settings(
            iteration_mode="LIMITED",
            use_dissolve=True,
            dissolve_speed_begin=300,
            dissolve_speed_end=300,
            use_noise=True,
            iterate_dissolve_both=True,
            iterate_noise_both=True,
            # Resolution: 1-item list including baseline
            resolution_use_list=True,
            resolution_list=[_item(128)],
            # Vorticity sweep starting at default (0.0)
            vorticity_use_range=True,
            vorticity_begin=0.0, vorticity_end=0.3, vorticity_step=0.1,
            # Alpha sweep starting at default (1.0)
            alpha_use_range=True,
            alpha_begin=1.0, alpha_end=3.0, alpha_step=0.5,
            # Beta sweep starting at default (1.0)
            beta_use_range=True,
            beta_begin=1.0, beta_end=3.0, beta_step=0.5,
            # Dissolve list including baseline (300)
            dissolve_speed_use_list=True,
            dissolve_speed_list=[_item(300), _item(400)],
            # Noise upres sweep starting at default (1)
            noise_upres_begin=1, noise_upres_end=4,
            noise_upres_use_range=True, noise_upres_step=1,
            # Noise strength sweep starting at default (0.25)
            noise_strength_use_range=True,
            noise_strength_begin=0.25, noise_strength_end=2.0,
            noise_strength_step=0.25,
            # Noise spatial scale sweep starting at default (0.5)
            noise_spatial_scale_use_range=True,
            noise_spatial_scale_begin=0.5, noise_spatial_scale_end=2.0,
            noise_spatial_scale_step=0.25,
        )
        raw    = list(generate_jobs_limited(s))
        unique = _dedupe_jobs(raw)
        # Before fix the production batch had 38 jobs with 7 hidden duplicates
        # of the baseline (8 total occurrences of the baseline combo).
        assert len(raw) > len(unique), "expected raw run to contain duplicates"
        # All eight sweep-axes that include the baseline value collapse to
        # exactly one baseline job in the deduped set.
        baseline_key = (
            128,                # resolution (note: _item(128) makes it float)
            0.0,                # vorticity
            1.0,                # alpha
            1.0,                # beta
            300,                # dissolve_speed
            1,                  # noise_upres
            0.25,               # noise_strength
            0.5,                # noise_spatial_scale
        )
        baselines = [
            j for j in unique
            if (int(j["resolution"]), round(j["vorticity"], 2),
                round(j["alpha"], 2), round(j["beta"], 2),
                int(j["dissolve_speed"]), int(j["noise_upres"]),
                round(j["noise_strength"], 2),
                round(j["noise_spatial_scale"], 2)) == baseline_key
            and j["use_dissolve"] and j["use_noise"]
        ]
        assert len(baselines) == 1, (
            f"expected exactly 1 baseline after dedup, got {len(baselines)}"
        )
