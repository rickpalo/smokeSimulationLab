"""TODO-58 module #3 regression tests: settings save/load lives in settings_io.py.

The third extraction moved the ``.smokesettings`` preset save/load cluster — the
snapshot dict, apply, dirty-check, disk load, and the dynamic preset-file
EnumProperty callbacks — into ``BatchSimLab.settings_io`` and re-imported every
name back into the package ``__init__``.

Unlike ``jobgen``/``emitters`` this module is *not* bpy-free: the two enum
callbacks call ``bpy.path.abspath`` (stubbed by conftest for pytest; the
real-Blender REGISTER smoke-test covers the live path).  It has no dependency on
``jobgen`` or ``emitters`` (near-leaf).

These tests lock in that contract: the names live in ``settings_io``, remain
reachable from the package root as the *same* object, and the moved cluster is
wired together correctly (a settings round-trip through the package namespace).
"""
import importlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

_SETTINGS_NAMES = [
    "_SWEEP_PARAMS",
    "_settings_dict",
    "_apply_settings_dict",
    "_load_settings_from_path",
    "_is_settings_dirty",
    "_SETTINGS_ENUM_SENTINEL",
    "_settings_items_cache",
    "_settings_files_enum_items",
    "_on_settings_enum_update",
]


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("BatchSimLab")


@pytest.fixture(scope="module")
def settings_io():
    return importlib.import_module("BatchSimLab.settings_io")


def test_settings_io_is_a_submodule(settings_io):
    assert settings_io.__name__ == "BatchSimLab.settings_io"


def test_settings_io_is_near_leaf(settings_io):
    """The cluster has no intra-package dependency on jobgen/emitters, so its
    source must not import either (only bpy/json/os)."""
    src = open(settings_io.__file__, encoding="utf-8").read()
    assert "from .jobgen" not in src, "settings_io must not depend on jobgen"
    assert "from .emitters" not in src, "settings_io must not depend on emitters"


@pytest.mark.parametrize("name", _SETTINGS_NAMES)
def test_name_defined_in_settings_io(settings_io, name):
    assert hasattr(settings_io, name), f"{name} must be defined in BatchSimLab.settings_io"


@pytest.mark.parametrize("name", _SETTINGS_NAMES)
def test_name_reexported_from_package(pkg, name):
    assert hasattr(pkg, name), (
        f"{name} must remain importable from the BatchSimLab package "
        f"(re-export from settings_io in __init__)"
    )


@pytest.mark.parametrize("name", _SETTINGS_NAMES)
def test_reexport_is_same_object(pkg, settings_io, name):
    # The mutable items-cache list is rebound on each enum callback, so compare
    # by identity only for the immutable/function names; the cache just needs to
    # be the same list object at import time.
    assert getattr(pkg, name) is getattr(settings_io, name), (
        f"BatchSimLab.{name} and BatchSimLab.settings_io.{name} diverged — a "
        f"duplicate definition likely survived the extraction"
    )


def _make_s(settings_io, **overrides):
    """Minimal SmokeSettings stand-in (mirrors test_settings_save_load._make_s)."""
    d = {
        "iteration_mode":        "LIMITED",
        "use_dissolve":          False,
        "slow_dissolve":         False,
        "iterate_dissolve_both": False,
        "use_noise":             False,
        "iterate_noise_both":    False,
        "settings_file_path":   "",
        "settings_search_path": "",
        "settings_snapshot":    "",
    }
    defaults = {
        "resolution": 64, "vorticity": 0.0, "alpha": 1.0, "beta": 1.0,
        "dissolve_speed": 5, "noise_upres": 2, "noise_strength": 2.0,
        "noise_spatial_scale": 2.0,
        "time_scale": 1.0, "cfl_number": 4.0,
        "timesteps_max": 4, "timesteps_min": 1,
        "burning_rate": 0.75, "flame_smoke": 1.0, "flame_vorticity": 0.5,
        "flame_max_temp": 1.7, "flame_ignition": 1.5,
    }
    for name in settings_io._SWEEP_PARAMS:
        base = defaults[name]
        d[name + "_use_range"] = False
        d[name + "_use_list"]  = False
        d[name + "_begin"]     = base
        d[name + "_end"]       = base
        d[name + "_step"]      = 0
        d[name + "_list"]      = []
    d.update(overrides)
    return types.SimpleNamespace(**d)


def test_settings_roundtrip_through_package(pkg, settings_io):
    """Behavioural sanity: snapshot → apply onto a fresh stub reproduces the
    snapshot, proving the moved cluster is internally consistent and reachable
    from the package root."""
    src = _make_s(settings_io, resolution_begin=256, use_dissolve=True)
    data = pkg._settings_dict(src)

    dst = _make_s(settings_io)
    pkg._apply_settings_dict(dst, data)
    assert pkg._settings_dict(dst) == data
