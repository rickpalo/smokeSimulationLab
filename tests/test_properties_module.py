"""TODO-58 module #5 regression tests: the bpy.props PropertyGroups live in properties.py.

The fifth extraction moved the five PropertyGroups (ValueItem, VelocityItem,
EmitterSettings, SmokeJobItem, SmokeSettings) and the class-body callback
factories/helpers they wire up (make_toggle_range/list, _sync_frame_defaults,
_import_domain_params + its _DOMAIN_IMPORT_MAP, _on_render_sim_result_update) into
``BatchSimLab.properties`` and re-imported every name back into the package
``__init__`` ABOVE the registration code.

The two UIList classes physically interleaved with the PropertyGroups
(SMOKE_UL_value_list, SMOKE_UL_job_log) are UI and deliberately STAYED in
``__init__`` (-> future ui.py).  This test pins the boundary: the names are
reachable from the package root as the *same* object, the UILists did NOT migrate,
and the moved callbacks still behave.
"""
import importlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

_PROPERTIES_NAMES = [
    "make_toggle_range",
    "make_toggle_list",
    "_sync_frame_defaults",
    "_DOMAIN_IMPORT_MAP",
    "_import_domain_params",
    "_on_render_sim_result_update",
    "ValueItem",
    "VelocityItem",
    "EmitterSettings",
    "SmokeJobItem",
    "SmokeSettings",
]


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("BatchSimLab")


@pytest.fixture(scope="module")
def properties():
    return importlib.import_module("BatchSimLab.properties")


def test_properties_is_a_submodule(properties):
    assert properties.__name__ == "BatchSimLab.properties"


def test_uilists_stayed_in_init(properties):
    """The two UIList classes interleaved with the PropertyGroups are UI and stay
    in __init__ (future ui.py); guard against them accidentally migrating here."""
    for ui_only in ("SMOKE_UL_value_list", "SMOKE_UL_job_log",
                    "SMOKE_UL_velocity_list", "SMOKE_PT_panel"):
        assert not hasattr(properties, ui_only), (
            f"{ui_only} is UI and must stay out of properties.py"
        )


@pytest.mark.parametrize("name", _PROPERTIES_NAMES)
def test_name_defined_in_properties(properties, name):
    assert hasattr(properties, name), f"{name} must be defined in BatchSimLab.properties"


@pytest.mark.parametrize("name", _PROPERTIES_NAMES)
def test_name_reexported_from_package(pkg, name):
    assert hasattr(pkg, name), (
        f"{name} must remain importable from the BatchSimLab package "
        f"(re-export from properties in __init__)"
    )


@pytest.mark.parametrize("name", _PROPERTIES_NAMES)
def test_reexport_is_same_object(pkg, properties, name):
    assert getattr(pkg, name) is getattr(properties, name), (
        f"BatchSimLab.{name} and BatchSimLab.properties.{name} diverged — a "
        f"duplicate definition likely survived the extraction"
    )


def test_property_groups_subclass_propertygroup(properties):
    """Under the bpy stub PropertyGroup is `object`, but the classes must still be
    real classes carrying the property annotations."""
    import bpy
    for cls_name in ("ValueItem", "VelocityItem", "EmitterSettings",
                     "SmokeJobItem", "SmokeSettings"):
        cls = getattr(properties, cls_name)
        assert isinstance(cls, type)
        assert issubclass(cls, bpy.types.PropertyGroup)


def test_smokesettings_has_sweep_sextet(properties):
    """SmokeSettings still carries the full Range/List sextet for a sweep param
    (the bulk of what moved) plus the wired callbacks."""
    ann = properties.SmokeSettings.__annotations__
    for suffix in ("_begin", "_end", "_step", "_use_range", "_use_list",
                   "_list", "_index"):
        assert "resolution" + suffix in ann, f"resolution{suffix} missing"
    # domain_obj uses the moved import callback; settings enum uses settings_io.
    assert "domain_obj" in ann
    assert "settings_file_enum" in ann


def test_make_toggle_callbacks_are_mutually_exclusive(properties):
    """make_toggle_range disables the matching use_list (and vice-versa)."""
    upd_range = properties.make_toggle_range("resolution")
    s = types.SimpleNamespace(resolution_use_range=True, resolution_use_list=True)
    upd_range(s, None)
    assert s.resolution_use_list is False

    upd_list = properties.make_toggle_list("resolution")
    s = types.SimpleNamespace(resolution_use_range=True, resolution_use_list=True)
    upd_list(s, None)
    assert s.resolution_use_range is False


def test_sync_frame_defaults_copies_scene_range(properties):
    """On uncheck, _sync_frame_defaults copies the scene's frame range."""
    scene = types.SimpleNamespace(frame_start=7, frame_end=99)
    ctx = types.SimpleNamespace(scene=scene)
    s = types.SimpleNamespace(use_default_frames=False, sim_frame_start=0,
                              sim_frame_end=0)
    properties._sync_frame_defaults(s, ctx)
    assert (s.sim_frame_start, s.sim_frame_end) == (7, 99)

    # When default frames are in use the callback is a no-op.
    s2 = types.SimpleNamespace(use_default_frames=True, sim_frame_start=0,
                               sim_frame_end=0)
    properties._sync_frame_defaults(s2, ctx)
    assert (s2.sim_frame_start, s2.sim_frame_end) == (0, 0)


def test_on_render_sim_result_update_clears_show_results(properties):
    """Turning rendering off clears the now-meaningless display-results flag."""
    s = types.SimpleNamespace(render_simulation_result=False, show_results=True)
    properties._on_render_sim_result_update(s, None)
    assert s.show_results is False

    # Leaves it alone while rendering stays on.
    s2 = types.SimpleNamespace(render_simulation_result=True, show_results=True)
    properties._on_render_sim_result_update(s2, None)
    assert s2.show_results is True


def test_domain_import_map_shape(properties):
    """_DOMAIN_IMPORT_MAP is the (blender_attr, addon_param) table the domain-select
    callback iterates; spot-check a couple of the documented mappings."""
    m = dict(properties._DOMAIN_IMPORT_MAP)
    assert m["resolution_max"] == "resolution"
    assert m["noise_scale"] == "noise_upres"
    assert m["cfl_condition"] == "cfl_number"


class _ClampTracker:
    """Stub ValueItem that counts writes to `value`.  `_clamp_value` is the update
    callback for `value`, so in real Blender each write re-fires it — a write to an
    already-in-range value recurses to a C stack overflow (BUG-018).  Counting
    writes lets us assert the fix without a live RNA loop: an in-range value must
    produce ZERO writes (no re-fire), an out-of-range value exactly ONE."""
    def __init__(self, value, lo, hi):
        self._v = value
        self.min_bound = lo
        self.max_bound = hi
        self.writes = 0

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, x):
        self.writes += 1
        self._v = x


def test_clamp_in_range_does_not_rewrite(properties):
    """BUG-018 regression: editing a list value that is already within bounds must
    NOT reassign `value` (that re-fires the update callback → infinite recursion →
    Blender stack-overflow crash with no log)."""
    t = _ClampTracker(0.1, -5.0, 5.0)   # Buoyancy Heat (beta) bounds; 0.1 in range
    properties.ValueItem._clamp_value(t, None)
    assert t.writes == 0, "in-range edit must not rewrite value (recursion guard)"
    assert t.value == 0.1


def test_clamp_out_of_range_writes_once(properties):
    """An out-of-range value clamps with exactly one write (the clamped value is in
    range, so the single re-fire it causes is itself a no-op)."""
    hi = _ClampTracker(9.0, -5.0, 5.0)
    properties.ValueItem._clamp_value(hi, None)
    assert hi.writes == 1 and hi.value == 5.0

    lo = _ClampTracker(-9.0, -5.0, 5.0)
    properties.ValueItem._clamp_value(lo, None)
    assert lo.writes == 1 and lo.value == -5.0


def test_clamp_no_bounds_is_noop(properties):
    """0/0 bounds (no limit active) must never write — vorticity/dissolve_speed
    style params that never enter the clamp branch."""
    t = _ClampTracker(123.0, 0.0, 0.0)
    properties.ValueItem._clamp_value(t, None)
    assert t.writes == 0 and t.value == 123.0


def test_clamp_min_only_clamps_below(properties):
    """min-only bound (e.g. resolution lo=8, hi=0): below-min clamps up once; at/above
    is a no-op."""
    below = _ClampTracker(3.0, 8.0, 0.0)
    properties.ValueItem._clamp_value(below, None)
    assert below.writes == 1 and below.value == 8.0

    ok = _ClampTracker(64.0, 8.0, 0.0)
    properties.ValueItem._clamp_value(ok, None)
    assert ok.writes == 0 and ok.value == 64.0
