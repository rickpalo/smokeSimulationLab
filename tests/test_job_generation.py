"""Tests for expand_param, generate_jobs_limited, and generate_jobs_all."""
import sys, os
import types
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import expand_param, generate_jobs_limited, generate_jobs_all


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_PARAM_NAMES = [
    "resolution", "vorticity", "alpha", "beta",
    "dissolve_speed", "noise_upres", "noise_strength", "noise_spatial_scale",
]

_BASE_VALUES = {
    "resolution": 64,
    "vorticity": 1.0,
    "alpha": 1.0,
    "beta": 1.0,
    "dissolve_speed": 50,
    "noise_upres": 2,
    "noise_strength": 1.0,
    "noise_spatial_scale": 1.0,
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
        "use_dissolve": False,
        "slow_dissolve": False,
        "use_noise": False,
        "iteration_mode": "LIMITED",
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
    def test_default_mode_returns_base(self):
        assert expand_param(_make_settings(), "resolution") == [64]

    def test_list_mode_returns_values(self):
        s = _make_settings(
            resolution_use_list=True,
            resolution_list=[_item(128), _item(256)],
        )
        assert expand_param(s, "resolution") == [128.0, 256.0]

    def test_empty_list_falls_back_to_base(self):
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
    def test_no_sweeps_yields_no_jobs(self):
        assert list(generate_jobs_limited(_make_settings())) == []

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

    def test_zero_step_range_yields_no_jobs(self):
        # Range enabled but step=0 produces no variation — not swept.
        s = _make_settings(
            vorticity_use_range=True,
            vorticity_begin=0.5, vorticity_end=2.0, vorticity_step=0,
        )
        assert list(generate_jobs_limited(s)) == []

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
            assert job["vorticity"] == pytest.approx(1.0)

    def test_dissolve_ignored_when_disabled(self):
        s = _make_settings(
            use_dissolve=False,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=10, dissolve_speed_end=50, dissolve_speed_step=10,
        )
        assert list(generate_jobs_limited(s)) == []

    def test_dissolve_swept_when_enabled(self):
        s = _make_settings(
            use_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=10, dissolve_speed_end=30, dissolve_speed_step=10,
        )
        assert len(list(generate_jobs_limited(s))) == 3

    def test_noise_ignored_when_disabled(self):
        s = _make_settings(
            use_noise=False,
            noise_strength_use_range=True,
            noise_strength_begin=0.5, noise_strength_end=1.5, noise_strength_step=0.5,
        )
        assert list(generate_jobs_limited(s)) == []

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
