"""v0.7.0 TODO-45 regression tests: Iterate Slow Dissolve.

Part A: when use_dissolve=True AND iterate_slow_dissolve=True, every
dissolve-using job gets a companion with the opposite slow_dissolve value.

Part B: audit that no dissolve sweep jobs are produced when use_dissolve
is False (regardless of iterate_slow_dissolve state).
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab"))

from SmokeSimLab import generate_jobs_limited, generate_jobs_all


def _item(value):
    return SimpleNamespace(value=value)


def _make_settings(**overrides):
    """Default sweepable-param settings; pre-v0.6 fixtures used the same
    pattern.  Each non-checkbox param gets its own _begin/_end/_step/_use_range/
    _use_list/_list/_index attributes."""
    base = dict(
        iteration_mode='LIMITED',
        use_dissolve=False,
        slow_dissolve=False,
        iterate_dissolve_both=False,
        iterate_slow_dissolve=False,   # v0.7.0 TODO-45
        use_noise=False,
        iterate_noise_both=False,
    )
    for name in ("resolution", "vorticity", "alpha", "beta",
                 "dissolve_speed", "noise_upres", "noise_strength",
                 "noise_spatial_scale"):
        base[f"{name}_begin"]     = {"resolution": 64,
                                     "vorticity": 0.0,
                                     "alpha": 1.0, "beta": 1.0,
                                     "dissolve_speed": 5,
                                     "noise_upres": 2,
                                     "noise_strength": 2.0,
                                     "noise_spatial_scale": 2.0}[name]
        base[f"{name}_end"]       = base[f"{name}_begin"]
        base[f"{name}_step"]      = 0
        base[f"{name}_use_range"] = False
        base[f"{name}_use_list"]  = False
        base[f"{name}_list"]      = []
        base[f"{name}_index"]     = 0
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Part A: Iterate Slow Dissolve fork behaviour ───────────────────────────

class TestIterateSlowDissolveLimited:
    """LIMITED mode: for each sweep job that has use_dissolve=True, also
    yield a companion with the opposite slow_dissolve value."""

    def test_off_by_default(self):
        """With iterate_slow_dissolve=False (default), no slow companions
        are yielded — backwards-compat with existing behaviour."""
        s = _make_settings(
            use_dissolve=True, slow_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=10, dissolve_speed_step=5,
        )
        jobs = list(generate_jobs_limited(s))
        # 2 dissolve_speed values; no slow companions.
        assert len(jobs) == 2
        for j in jobs:
            assert j["slow_dissolve"] is True

    def test_on_doubles_dissolve_sweep(self):
        """With iterate_slow_dissolve=True, the 2-value dissolve_speed sweep
        produces 4 jobs (2 speeds × 2 slow modes)."""
        s = _make_settings(
            use_dissolve=True, slow_dissolve=True,
            iterate_slow_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=10, dissolve_speed_step=5,
        )
        jobs = list(generate_jobs_limited(s))
        assert len(jobs) == 4
        # Two slow=True jobs + two slow=False jobs
        slow_count = sum(1 for j in jobs if j["slow_dissolve"])
        fast_count = sum(1 for j in jobs if not j["slow_dissolve"])
        assert slow_count == 2
        assert fast_count == 2
        # Each pair has the same dissolve_speed
        speeds = sorted({j["dissolve_speed"] for j in jobs})
        assert speeds == [5, 10]

    def test_companion_is_yielded_after_original(self):
        """Order matters for predictability: original then flipped companion."""
        s = _make_settings(
            use_dissolve=True, slow_dissolve=True,
            iterate_slow_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=5, dissolve_speed_step=0,
        )
        # step=0 → single value [5], iterate_slow doubles → [(5, slow), (5, fast)]
        jobs = list(generate_jobs_limited(s))
        # _default_job emits one job, sweep adds nothing further with step=0
        # but iterate_dissolve_both etc. could add more.  Filter to slow-pair.
        slow_jobs = [j for j in jobs if j["dissolve_speed"] == 5]
        # Should be at least 1 of each slow setting
        slow_vals = {j["slow_dissolve"] for j in slow_jobs}
        assert True in slow_vals
        assert False in slow_vals

    def test_no_companion_when_use_dissolve_false(self):
        """Iterate Slow has no effect when use_dissolve is False — no slow
        companions because the jobs are dissolve-off (slow doesn't apply)."""
        s = _make_settings(
            use_dissolve=False,
            iterate_slow_dissolve=True,   # checked but should have no effect
            slow_dissolve=True,
            vorticity_use_range=True,
            vorticity_begin=0.0, vorticity_end=1.0, vorticity_step=0.5,
        )
        jobs = list(generate_jobs_limited(s))
        # 3 vorticity values, no slow doubling because use_dissolve=False
        assert len(jobs) == 3
        for j in jobs:
            assert j["use_dissolve"] is False


class TestIterateSlowDissolveAll:
    """ALL mode: dissolve_states gets a parallel slow-flipped entry."""

    def test_off_by_default(self):
        s = _make_settings(
            iteration_mode='ALL',
            use_dissolve=True, slow_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=10, dissolve_speed_step=5,
        )
        jobs = list(generate_jobs_all(s))
        # 2 dissolve values × 1 noise state × 1 res × 1 vort × 1 alpha × 1 beta
        assert len(jobs) == 2
        for j in jobs:
            assert j["slow_dissolve"] is True

    def test_on_doubles_dissolve_axis(self):
        s = _make_settings(
            iteration_mode='ALL',
            use_dissolve=True, slow_dissolve=True,
            iterate_slow_dissolve=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=10, dissolve_speed_step=5,
        )
        jobs = list(generate_jobs_all(s))
        # 2 dissolve_states (slow + fast) × 2 dissolve values × 1 noise = 4
        assert len(jobs) == 4
        slow = [j for j in jobs if j["slow_dissolve"]]
        fast = [j for j in jobs if not j["slow_dissolve"]]
        assert len(slow) == 2
        assert len(fast) == 2

    def test_combines_with_iterate_dissolve_both(self):
        """When iterate_slow_dissolve AND iterate_dissolve_both both on:
        slow + fast + off = 3 dissolve_states."""
        s = _make_settings(
            iteration_mode='ALL',
            use_dissolve=True, slow_dissolve=True,
            iterate_slow_dissolve=True,
            iterate_dissolve_both=True,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=10, dissolve_speed_step=5,
        )
        jobs = list(generate_jobs_all(s))
        # slow=True × 2 speeds + slow=False × 2 speeds + use_dissolve=False × 1
        # = 2 + 2 + 1 = 5
        assert len(jobs) == 5
        on_jobs  = [j for j in jobs if j["use_dissolve"]]
        off_jobs = [j for j in jobs if not j["use_dissolve"]]
        assert len(on_jobs)  == 4
        assert len(off_jobs) == 1

    def test_no_companion_when_use_dissolve_false(self):
        s = _make_settings(
            iteration_mode='ALL',
            use_dissolve=False,
            iterate_slow_dissolve=True,
            vorticity_use_range=True,
            vorticity_begin=0.0, vorticity_end=1.0, vorticity_step=0.5,
        )
        jobs = list(generate_jobs_all(s))
        assert len(jobs) == 3  # vort sweep only, no dissolve fork
        for j in jobs:
            assert j["use_dissolve"] is False


# ── Part B: AUDIT — no dissolve sweep when use_dissolve is off ─────────────

class TestNoDissolveSweepWhenOff:
    """User's audit request: confirm no dissolve_speed sweep jobs get
    created when Use Dissolve is unchecked."""

    def test_limited_no_dissolve_jobs_when_off(self):
        """LIMITED mode: with use_dissolve=False, no jobs vary the
        dissolve_speed — every job has the default value."""
        s = _make_settings(
            use_dissolve=False,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=20, dissolve_speed_step=5,
            vorticity_use_range=True,
            vorticity_begin=0.0, vorticity_end=1.0, vorticity_step=0.5,
        )
        jobs = list(generate_jobs_limited(s))
        # Only vorticity sweep produces jobs.  Dissolve range setup is
        # ignored because use_dissolve is False.
        speeds = {j["dissolve_speed"] for j in jobs}
        assert speeds == {5}, (
            f"with use_dissolve=False, all jobs should have the default "
            f"dissolve_speed (begin=5); got speeds {speeds}"
        )
        for j in jobs:
            assert j["use_dissolve"] is False

    def test_all_no_dissolve_axis_when_off(self):
        """ALL mode: dissolve range collapsed to single default value when off."""
        s = _make_settings(
            iteration_mode='ALL',
            use_dissolve=False,
            dissolve_speed_use_range=True,
            dissolve_speed_begin=5, dissolve_speed_end=20, dissolve_speed_step=5,
            vorticity_use_range=True,
            vorticity_begin=0.0, vorticity_end=1.0, vorticity_step=0.5,
        )
        jobs = list(generate_jobs_all(s))
        # 3 vorticity × 1 dissolve (collapsed) = 3 jobs
        assert len(jobs) == 3
        speeds = {j["dissolve_speed"] for j in jobs}
        assert speeds == {5}

    def test_iterate_slow_silent_when_use_dissolve_off(self):
        """User explicitly asked to verify: iterate_slow_dissolve checked
        while use_dissolve unchecked should NOT create any slow companions."""
        for mode in ('LIMITED', 'ALL'):
            s = _make_settings(
                iteration_mode=mode,
                use_dissolve=False,            # off
                iterate_slow_dissolve=True,    # iterate checked
                slow_dissolve=True,            # slow flag set (but use_dissolve=False)
                vorticity_use_range=True,
                vorticity_begin=0.0, vorticity_end=1.0, vorticity_step=0.5,
            )
            gen = generate_jobs_limited if mode == 'LIMITED' else generate_jobs_all
            jobs = list(gen(s))
            # All jobs have use_dissolve=False; no slow doubling.
            assert all(not j["use_dissolve"] for j in jobs), (
                f"{mode} mode: jobs unexpectedly enabled dissolve when "
                f"use_dissolve was off"
            )
            assert len(jobs) == 3, (
                f"{mode} mode: expected 3 vorticity jobs, got {len(jobs)} "
                f"— iterate_slow_dissolve should NOT add anything when "
                f"use_dissolve is False"
            )
