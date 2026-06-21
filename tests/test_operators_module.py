"""TODO-58 module #6 (Tiers 1+2) regression tests: the CRUD/settings/emitter +
export operators live in operators.py.

The sixth extraction moved the operators that do NOT touch the stateful batch-run /
poll engine — export, save/load preset, the value + emitter list editing ops, the
emitter refresh, and open-docs — plus their private helpers and ``_PARAM_BOUNDS``
into ``BatchSimLab.operators``, re-imported back into the package ``__init__`` so
the ``classes = [...]`` registration list and ``from BatchSimLab import …`` resolve
unchanged.

DELIBERATELY LEFT in ``__init__`` (the run/poll engine + the UI it shares): the
SMOKE_OT_run_batch / retry_failed / monitor_existing_jobs / remove_all_jobs /
setup_results / reset_to_defaults operators, the poll loop + its rebindable
globals, the three UILists, and SmokeSimLabPreferences (whose ``bl_idname =
__name__`` must resolve to the package name).  This test pins that boundary and
verifies the two function-local deferred imports' targets are reachable.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

_OPERATOR_NAMES = [
    "_PARAM_BOUNDS",
    "_scene_has_camera",
    "_find_next_job_index",
    "_existing_jobs_for_bat",
    "_job_run_cmd",
    "_job_bat_block",
    "_batch_ready",
    "export_batch",
    "_next_list_value",
    "_emitter_of",
    "SMOKE_OT_export_batch",
    "SMOKE_OT_save_settings",
    "SMOKE_OT_load_settings",
    "SMOKE_OT_add_value",
    "SMOKE_OT_remove_value",
    "SMOKE_OT_refresh_emitters",
    "SMOKE_OT_add_emitter_value",
    "SMOKE_OT_remove_emitter_value",
    "SMOKE_OT_add_emitter_velocity",
    "SMOKE_OT_remove_emitter_velocity",
    "SMOKE_OT_open_docs",
]

# Names export_batch / open_docs reach via a function-local deferred import
# (they live with the run engine / addon metadata in __init__).
_DEFERRED_TARGETS = [
    "_job_log_rows", "_job_statuses", "_debug_log", "ADDON_VERSION", "DOCS_URL",
]


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("BatchSimLab")


@pytest.fixture(scope="module")
def operators():
    return importlib.import_module("BatchSimLab.operators")


def test_operators_is_a_submodule(operators):
    assert operators.__name__ == "BatchSimLab.operators"


def test_run_engine_stayed_in_init(operators):
    """The stateful batch-run / poll engine + the shared UI deliberately stay in
    __init__; guard against any of it migrating into operators.py."""
    for stayed in (
        "SMOKE_OT_run_batch", "SMOKE_OT_retry_failed", "SMOKE_OT_monitor_existing_jobs",
        "SMOKE_OT_remove_all_jobs", "SMOKE_OT_setup_results", "SMOKE_OT_reset_to_defaults",
        "_poll_batch_progress", "_poll_batch_progress_impl", "_bt_set", "_redraw_panels",
        "_batch_is_running",
        "SMOKE_UL_value_list", "SMOKE_UL_job_log", "SMOKE_UL_velocity_list",
        "SmokeSimLabPreferences",
    ):
        assert not hasattr(operators, stayed), (
            f"{stayed} must stay in __init__ (run/poll engine or shared UI)"
        )


def test_rebindable_state_not_a_module_global_here(operators):
    """The job-log lists are reached by export_batch via a function-local deferred
    import — they must NOT exist as operators.py module globals (that would create
    a second, diverging binding)."""
    for state in ("_job_log_rows", "_job_statuses", "_last_auto_index",
                  "_auto_retry_count", "_batch_times", "_estim"):
        assert not hasattr(operators, state), (
            f"{state} leaked into operators.py as a module global"
        )


@pytest.mark.parametrize("name", _OPERATOR_NAMES)
def test_name_defined_in_operators(operators, name):
    assert hasattr(operators, name), f"{name} must be defined in BatchSimLab.operators"


@pytest.mark.parametrize("name", _OPERATOR_NAMES)
def test_name_reexported_from_package(pkg, name):
    assert hasattr(pkg, name), (
        f"{name} must remain importable from the BatchSimLab package "
        f"(re-export from operators in __init__)"
    )


@pytest.mark.parametrize("name", _OPERATOR_NAMES)
def test_reexport_is_same_object(pkg, operators, name):
    assert getattr(pkg, name) is getattr(operators, name), (
        f"BatchSimLab.{name} and BatchSimLab.operators.{name} diverged — a "
        f"duplicate definition likely survived the extraction"
    )


@pytest.mark.parametrize("name", _DEFERRED_TARGETS)
def test_deferred_import_targets_reachable(pkg, name):
    """export_batch / open_docs do `from . import <name>` at call time; if any of
    these stopped being reachable from the package the operator would raise at
    runtime (not caught by import-time tests)."""
    assert hasattr(pkg, name), (
        f"{name} must stay reachable from the BatchSimLab package for the "
        f"deferred imports in export_batch / open_docs"
    )


def test_scene_has_camera_behaves(operators):
    """Sanity on a pure moved helper."""
    import types
    scene = types.SimpleNamespace(objects=[types.SimpleNamespace(type='CAMERA'),
                                           types.SimpleNamespace(type='MESH')])
    assert operators._scene_has_camera(scene) is True
    empty = types.SimpleNamespace(objects=[])
    assert operators._scene_has_camera(empty) is False


def test_param_bounds_is_the_moved_table(operators):
    assert operators._PARAM_BOUNDS["resolution"] == (8.0, None)
    assert operators._PARAM_BOUNDS["alpha"] == (-5.0, 5.0)
