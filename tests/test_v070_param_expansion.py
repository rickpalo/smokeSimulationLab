"""v0.7.0 regression tests: parameter expansion + auto-import from domain.

TODO-41: Time Scale + Adaptive Time Step + CFL + Timesteps Max/Min sweep params
TODO-42: Fire Parameters section (Reaction Speed, Flames Smoke, Vorticity,
         Temp Max, Ignition Temp)
TODO-40: Auto-import of domain settings on PointerProperty selection
"""
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab"))

import BatchSimLab as ssl
from BatchSimLab import (
    ITERABLE_PARAMS, _SWEEP_PARAMS, _PARAM_BOUNDS,
    _default_job, generate_jobs_limited, generate_jobs_all,
    _import_domain_params,
)


from addon_src import read_addon_source


def _addon_src():
    # TODO-58: the SmokeSettings props these tests assert on now live in
    # properties.py — read the whole addon package.
    return read_addon_source()


def _worker_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab",
                     "smoke_worker.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _make_settings(**overrides):
    """Fully-populated SmokeSettings fixture including v0.7.0 params."""
    base = dict(
        iteration_mode='LIMITED',
        use_dissolve=False, slow_dissolve=False,
        iterate_dissolve_both=False, iterate_slow_dissolve=False,
        use_noise=False, iterate_noise_both=False,
        # v0.7.0 TODO-41/42 master toggles
        use_adaptive_timesteps=True,
        use_fire=False,
    )
    # Standard sweep params (each gets full _begin/_end/_step/_use_range/
    # _use_list/_list/_index machinery).
    defaults = {
        "resolution": 64, "vorticity": 0.0, "alpha": 1.0, "beta": 1.0,
        "dissolve_speed": 5, "noise_upres": 2,
        "noise_strength": 2.0, "noise_spatial_scale": 2.0,
        "time_scale": 1.0, "cfl_number": 4.0,
        "timesteps_max": 4, "timesteps_min": 1,
        "burning_rate": 0.75, "flame_smoke": 1.0, "flame_vorticity": 0.5,
        "flame_max_temp": 1.7, "flame_ignition": 1.5,
    }
    for name, val in defaults.items():
        base[f"{name}_begin"]     = val
        base[f"{name}_end"]       = val
        base[f"{name}_step"]      = 0
        base[f"{name}_use_range"] = False
        base[f"{name}_use_list"]  = False
        base[f"{name}_list"]      = []
        base[f"{name}_index"]     = 0
    base.update(overrides)
    return SimpleNamespace(**base)


# ── TODO-41 + TODO-42: new params are first-class members of the schemas ──

class TestNewParamsRegistered:
    def test_iterable_params_contains_new_v070_params(self):
        for name in ("time_scale", "cfl_number", "timesteps_max",
                     "timesteps_min", "burning_rate", "flame_smoke",
                     "flame_vorticity", "flame_max_temp", "flame_ignition"):
            assert name in ITERABLE_PARAMS, (
                f"v0.7.0 param {name!r} missing from ITERABLE_PARAMS"
            )

    def test_sweep_params_contains_new_v070_params(self):
        for name in ("time_scale", "cfl_number", "timesteps_max",
                     "timesteps_min", "burning_rate", "flame_smoke",
                     "flame_vorticity", "flame_max_temp", "flame_ignition"):
            assert name in _SWEEP_PARAMS

    def test_param_bounds_contains_new_v070_params(self):
        for name in ("time_scale", "cfl_number", "timesteps_max",
                     "timesteps_min", "burning_rate", "flame_smoke",
                     "flame_vorticity", "flame_max_temp", "flame_ignition"):
            assert name in _PARAM_BOUNDS, (
                f"v0.7.0 param {name!r} missing from _PARAM_BOUNDS"
            )

    def test_master_toggle_properties_registered(self):
        src = _addon_src()
        assert "use_adaptive_timesteps: bpy.props.BoolProperty(" in src
        assert "use_fire: bpy.props.BoolProperty(" in src

    def test_show_section_properties_registered(self):
        src = _addon_src()
        assert "show_time: bpy.props.BoolProperty(" in src
        assert "show_fire: bpy.props.BoolProperty(" in src


# ── _default_job carries the new params ────────────────────────────────────

class TestDefaultJobIncludesNewParams:
    def test_includes_gas_timing_params(self):
        s = _make_settings()
        job = _default_job(s)
        for k in ("time_scale", "use_adaptive_timesteps", "cfl_number",
                  "timesteps_max", "timesteps_min"):
            assert k in job, f"_default_job missing {k!r}"

    def test_includes_fire_params(self):
        s = _make_settings()
        job = _default_job(s)
        for k in ("use_fire", "burning_rate", "flame_smoke",
                  "flame_vorticity", "flame_max_temp", "flame_ignition"):
            assert k in job

    def test_blender_default_values_match(self):
        """When the user hasn't changed anything, _default_job should
        produce values matching Blender's documented defaults so a
        baseline bake matches an un-instrumented Blender run."""
        s = _make_settings()
        job = _default_job(s)
        assert job["time_scale"]    == pytest.approx(1.0)
        assert job["cfl_number"]    == pytest.approx(4.0)
        assert job["timesteps_max"] == 4
        assert job["timesteps_min"] == 1
        assert job["burning_rate"]    == pytest.approx(0.75)
        assert job["flame_smoke"]     == pytest.approx(1.0)
        assert job["flame_vorticity"] == pytest.approx(0.5)
        assert job["flame_max_temp"]  == pytest.approx(1.7)
        assert job["flame_ignition"]  == pytest.approx(1.5)


# ── generate_jobs_limited sweeps the new params when enabled ───────────────

class TestLimitedSweepsNewParams:
    def test_time_scale_always_sweepable(self):
        """time_scale has no master toggle — always available."""
        s = _make_settings(
            time_scale_use_range=True,
            time_scale_begin=1.0, time_scale_end=2.0, time_scale_step=0.5,
        )
        jobs = list(generate_jobs_limited(s))
        ts_vals = sorted({j["time_scale"] for j in jobs})
        assert ts_vals == [1.0, 1.5, 2.0]

    def test_cfl_swept_only_when_adaptive_on(self):
        """cfl_number is in sweepable only when use_adaptive_timesteps."""
        # Adaptive on, CFL range → swept
        s_on = _make_settings(
            use_adaptive_timesteps=True,
            cfl_number_use_range=True,
            cfl_number_begin=2.0, cfl_number_end=4.0, cfl_number_step=1.0,
        )
        jobs_on = list(generate_jobs_limited(s_on))
        cfl_vals_on = sorted({j["cfl_number"] for j in jobs_on})
        assert cfl_vals_on == [2.0, 3.0, 4.0]

        # Adaptive off → CFL not swept regardless of range
        s_off = _make_settings(
            use_adaptive_timesteps=False,
            cfl_number_use_range=True,
            cfl_number_begin=2.0, cfl_number_end=4.0, cfl_number_step=1.0,
        )
        jobs_off = list(generate_jobs_limited(s_off))
        cfl_vals_off = sorted({j["cfl_number"] for j in jobs_off})
        assert cfl_vals_off == [2.0]  # single default, not swept

    def test_fire_params_swept_only_when_use_fire_on(self):
        # use_fire off → burning_rate not swept
        s_off = _make_settings(
            use_fire=False,
            burning_rate_use_range=True,
            burning_rate_begin=0.5, burning_rate_end=1.0, burning_rate_step=0.25,
        )
        jobs_off = list(generate_jobs_limited(s_off))
        br_vals = sorted({j["burning_rate"] for j in jobs_off})
        assert br_vals == [0.5]  # not swept

        # use_fire on → swept
        s_on = _make_settings(
            use_fire=True,
            burning_rate_use_range=True,
            burning_rate_begin=0.5, burning_rate_end=1.0, burning_rate_step=0.25,
        )
        jobs_on = list(generate_jobs_limited(s_on))
        br_vals_on = sorted({j["burning_rate"] for j in jobs_on})
        assert br_vals_on == [0.5, 0.75, 1.0]


# ── generate_jobs_all extends Cartesian product ─────────────────────────────

class TestAllModeIncludesNewAxes:
    def test_time_scale_multiplies_cartesian(self):
        s = _make_settings(
            iteration_mode='ALL',
            time_scale_use_range=True,
            time_scale_begin=1.0, time_scale_end=2.0, time_scale_step=1.0,
        )
        jobs = list(generate_jobs_all(s))
        assert len(jobs) == 2
        assert sorted({j["time_scale"] for j in jobs}) == [1.0, 2.0]

    def test_fire_off_collapses_fire_axes(self):
        """With use_fire=False, fire ranges collapse to begin-only so
        the Cartesian product doesn't explode."""
        s = _make_settings(
            iteration_mode='ALL',
            use_fire=False,
            burning_rate_use_range=True,
            burning_rate_begin=0.5, burning_rate_end=1.0, burning_rate_step=0.25,
            flame_smoke_use_range=True,
            flame_smoke_begin=1.0, flame_smoke_end=2.0, flame_smoke_step=0.5,
        )
        jobs = list(generate_jobs_all(s))
        # Both ranges collapsed → still 1 combination from fire axes.
        assert len(jobs) == 1


# ── Worker applies the new params ──────────────────────────────────────────

class TestWorkerAppliesNewParams:
    def test_time_scale_applied(self):
        src = _worker_src()
        assert "d.time_scale = float(p.get(\"time_scale\", 1.0))" in src

    def test_adaptive_timesteps_applied(self):
        src = _worker_src()
        assert "d.use_adaptive_timesteps = _use_adaptive" in src

    def test_cfl_timesteps_applied_only_when_adaptive(self):
        src = _worker_src()
        # Find the if block that gates CFL/timesteps assignment on _use_adaptive.
        m = re.search(
            r"if _use_adaptive:[\s\S]+?d\.cfl_condition[\s\S]+?d\.timesteps_max[\s\S]+?d\.timesteps_min",
            src,
        )
        assert m, "worker must guard CFL/timesteps on _use_adaptive"

    def test_fire_params_applied_only_when_use_fire(self):
        src = _worker_src()
        # Locate the fire-guard if-block and assert it sets all 5 fire props.
        m = re.search(
            r'if p\.get\(["\']use_fire["\'], False\):([\s\S]+?)(?=\n# |\n\n# )',
            src,
        )
        assert m, "fire-param if-block (gated on p['use_fire']) not found"
        body = m.group(1)
        for attr in ("d.burning_rate", "d.flame_smoke", "d.flame_vorticity",
                     "d.flame_max_temp", "d.flame_ignition"):
            assert attr in body, (
                f"worker fire-guard block must set {attr}; "
                f"missing from block body"
            )


# ── CSV columns ──────────────────────────────────────────────────────────────

class TestCsvHeaderHasNewColumns:
    def test_all_v070_columns_in_header(self):
        src = _worker_src()
        for col in ("time_scale", "use_adaptive_timesteps", "cfl_number",
                    "timesteps_max", "timesteps_min", "use_fire",
                    "burning_rate", "flame_smoke", "flame_vorticity",
                    "flame_max_temp", "flame_ignition"):
            assert f'"{col}",' in src, (
                f"v0.7.0 column {col!r} missing from CSV header"
            )

    def test_version_column_stays_last(self):
        """Legacy CSV readers that index from the end of the row depend on
        `version` being the last column — new v0.7.0 columns must be
        inserted BEFORE version, not after."""
        src = _worker_src()
        # Find the header list and assert "version" is the last name in it.
        m = re.search(r"header\s*=\s*\[([\s\S]+?)\]", src)
        assert m, "header list not found"
        body = m.group(1)
        names = re.findall(r'"([a-z_]+)"', body)
        assert names[-1] == "version", (
            f"version column must be last; got order ending with {names[-3:]!r}"
        )


# ── TODO-40: auto-import callback ───────────────────────────────────────────

class TestTodo40AutoImport:
    """The PointerProperty update callback _import_domain_params copies
    a freshly-selected domain's settings into the addon's _begin values
    + master toggles, without touching sweep config."""

    def _make_fake_domain(self, **kwargs):
        """SimpleNamespace mimicking a fluid DOMAIN object's modifier."""
        d_attrs = dict(
            resolution_max=128,
            vorticity=0.5, alpha=1.5, beta=0.5,
            use_dissolve_smoke=True,
            dissolve_speed=20,
            use_dissolve_smoke_log=True,
            use_noise=True,
            noise_scale=3, noise_strength=3.0, noise_pos_scale=1.5,
            time_scale=2.0, use_adaptive_timesteps=False,
            cfl_condition=2.0, timesteps_max=8, timesteps_min=2,
            burning_rate=1.0, flame_smoke=1.5, flame_vorticity=1.0,
            flame_max_temp=2.5, flame_ignition=2.0,
        )
        d_attrs.update(kwargs)
        d = SimpleNamespace(**d_attrs)
        modifier = SimpleNamespace(type='FLUID', fluid_type='DOMAIN',
                                   domain_settings=d)
        obj = SimpleNamespace(modifiers=[modifier])
        return obj

    def test_noop_when_domain_obj_is_none(self):
        """No selection → no-op (no AttributeError)."""
        s = _make_settings()
        s.domain_obj = None
        # Should not raise.
        _import_domain_params(s, context=None)
        # Begin values unchanged.
        assert s.resolution_begin == 64

    def test_noop_when_no_fluid_modifier(self):
        """Object without a Fluid modifier → no-op."""
        s = _make_settings()
        s.domain_obj = SimpleNamespace(modifiers=[])
        _import_domain_params(s, context=None)
        assert s.resolution_begin == 64

    def test_noop_when_modifier_is_not_domain(self):
        """Fluid modifier with fluid_type=FLOW (emitter) → no-op."""
        s = _make_settings()
        modifier = SimpleNamespace(type='FLUID', fluid_type='FLOW',
                                   domain_settings=None)
        s.domain_obj = SimpleNamespace(modifiers=[modifier])
        _import_domain_params(s, context=None)
        assert s.resolution_begin == 64

    def test_imports_basic_params_into_begin(self):
        s = _make_settings()
        s.domain_obj = self._make_fake_domain()
        _import_domain_params(s, context=None)
        assert s.resolution_begin     == 128
        assert s.vorticity_begin      == pytest.approx(0.5)
        assert s.alpha_begin          == pytest.approx(1.5)
        assert s.beta_begin           == pytest.approx(0.5)
        assert s.dissolve_speed_begin == 20

    def test_imports_master_toggles(self):
        s = _make_settings()
        s.domain_obj = self._make_fake_domain()
        _import_domain_params(s, context=None)
        assert s.use_dissolve  is True
        assert s.slow_dissolve is True
        assert s.use_noise     is True
        assert s.use_adaptive_timesteps is False

    def test_imports_new_v070_params(self):
        s = _make_settings()
        s.domain_obj = self._make_fake_domain()
        _import_domain_params(s, context=None)
        # Gas timing
        assert s.time_scale_begin    == pytest.approx(2.0)
        assert s.cfl_number_begin    == pytest.approx(2.0)
        assert s.timesteps_max_begin == 8
        assert s.timesteps_min_begin == 2
        # Fire
        assert s.burning_rate_begin    == pytest.approx(1.0)
        assert s.flame_smoke_begin     == pytest.approx(1.5)
        assert s.flame_vorticity_begin == pytest.approx(1.0)
        assert s.flame_max_temp_begin  == pytest.approx(2.5)
        assert s.flame_ignition_begin  == pytest.approx(2.0)

    def test_use_fire_heuristic_flips_on_for_nondefault_values(self):
        """Domain with non-default burning_rate or flame_ignition triggers
        use_fire=True so the imported fire values get applied at bake."""
        s = _make_settings()
        s.use_fire = False
        s.domain_obj = self._make_fake_domain(burning_rate=1.5)  # non-default
        _import_domain_params(s, context=None)
        assert s.use_fire is True

    def test_sweep_config_preserved(self):
        """Pre-existing sweep config (range / list / step) must NOT be
        overwritten by auto-import — only _begin values change."""
        s = _make_settings(
            resolution_use_range=True,
            resolution_begin=64, resolution_end=512, resolution_step=64,
        )
        s.domain_obj = self._make_fake_domain()
        _import_domain_params(s, context=None)
        # Begin updated to 128, BUT _end / _step / _use_range untouched.
        assert s.resolution_begin == 128
        assert s.resolution_end   == 512
        assert s.resolution_step  == 64
        assert s.resolution_use_range is True

    def test_missing_blender_attr_is_silently_skipped(self):
        """Older Blender builds may lack one of the v0.7.0 props (e.g.
        cfl_condition).  Callback must not raise."""
        s = _make_settings()
        # Build a domain that's missing cfl_condition + fire props.
        d = SimpleNamespace(
            resolution_max=128, vorticity=0.5, alpha=1.0, beta=1.0,
            use_dissolve_smoke=False, dissolve_speed=5,
            use_dissolve_smoke_log=False,
            use_noise=False, noise_scale=2, noise_strength=2.0,
            noise_pos_scale=2.0,
            time_scale=1.5, use_adaptive_timesteps=True,
            # No cfl_condition, timesteps_*, burning_rate, flame_* attrs.
        )
        modifier = SimpleNamespace(type='FLUID', fluid_type='DOMAIN',
                                   domain_settings=d)
        s.domain_obj = SimpleNamespace(modifiers=[modifier])
        # Must not raise.
        _import_domain_params(s, context=None)
        # The present attrs still imported.
        assert s.time_scale_begin == pytest.approx(1.5)
        # Missing attrs left at fixture defaults.
        assert s.cfl_number_begin == pytest.approx(4.0)
