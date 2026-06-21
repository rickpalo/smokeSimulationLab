"""TODO-58 module #4 regression tests: pure progress/ETA helpers live in progress.py.

The fourth extraction moved the *pure* half of the batch-progress machinery —
the jobs-dir scanners, the phase-aware ETA estimator, and the formatters (plus
their constants) — into ``BatchSimLab.progress`` and re-imported every name back
into the package ``__init__``.

The *stateful* half (the live poll engine + the rebindable ``_bt``/``_estim``/
``_job_*``/``_last_auto_index``/``_auto_retry_count`` globals + their mutators)
deliberately STAYED in ``__init__``: those globals are rebound from operators and
load handlers that also stay in ``__init__``, so splitting the variable across two
modules would diverge the binding.  This test pins that boundary: ``progress`` is
bpy-free and holds no addon mutable state, the names are reachable from the
package root as the *same* object, and the moved helpers still behave.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

_PROGRESS_NAMES = [
    "_SETUP_SECS_DEFAULT",
    "_STILL_SECS_DEFAULT",
    "_DONE_RE",
    "_RETRY_DONE_RE",
    "_CRASHED_RE",
    "_LOG_DONE_MARKERS",
    "_find_running_log",
    "_count_vdb_frames",
    "_count_png_frames",
    "_format_eta",
    "_estimate_batch_remaining",
    "_format_elapsed",
    "_has_error",
    "_compute_batch_summary",
]


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("BatchSimLab")


@pytest.fixture(scope="module")
def progress():
    return importlib.import_module("BatchSimLab.progress")


def test_progress_is_a_submodule(progress):
    assert progress.__name__ == "BatchSimLab.progress"


def test_progress_is_bpy_free(progress):
    """The pure half must not import bpy — it is the unit-testable layer."""
    src = open(progress.__file__, encoding="utf-8").read()
    assert "import bpy" not in src, "progress.py must stay bpy-free (pure scanners/estimator)"


def test_progress_holds_no_stateful_engine(progress):
    """The rebindable batch-progress globals + the poll engine deliberately stay
    in __init__; guard against them accidentally migrating here (which would split
    a rebound global across two modules)."""
    for leaked in ("_poll_batch_progress_impl", "_batch_times", "_job_statuses",
                   "_job_log_rows", "_last_auto_index", "_update_job_log_statuses"):
        assert not hasattr(progress, leaked), (
            f"{leaked} must stay in __init__ with the mutable state it rebinds"
        )


@pytest.mark.parametrize("name", _PROGRESS_NAMES)
def test_name_defined_in_progress(progress, name):
    assert hasattr(progress, name), f"{name} must be defined in BatchSimLab.progress"


@pytest.mark.parametrize("name", _PROGRESS_NAMES)
def test_name_reexported_from_package(pkg, name):
    assert hasattr(pkg, name), (
        f"{name} must remain importable from the BatchSimLab package "
        f"(re-export from progress in __init__)"
    )


@pytest.mark.parametrize("name", _PROGRESS_NAMES)
def test_reexport_is_same_object(pkg, progress, name):
    assert getattr(pkg, name) is getattr(progress, name), (
        f"BatchSimLab.{name} and BatchSimLab.progress.{name} diverged — a "
        f"duplicate definition likely survived the extraction"
    )


def test_format_helpers_behave(progress):
    """Sanity on the pure formatters."""
    assert progress._format_eta(30) == "~30s remaining"
    assert progress._format_eta(90) == "~1 min remaining"
    assert progress._format_elapsed(45) == "45 sec"
    assert progress._format_elapsed(27 * 60) == "27 min"


def test_estimate_batch_remaining_counts_down_through_phases(progress):
    """Bake phase (current job not yet baked) charges every pending render its full
    cost; once we cross into the render phase the estimate is strictly smaller —
    the regression TODO-46 fixed."""
    common = dict(
        total=4, bake_done_n=0, render_done_n=0, bake_only=False,
        setup_remaining=0.0, bake_remaining=10.0, render_remaining=10.0,
        still_remaining=0.0, default_bake_secs=20.0, default_render_secs=30.0,
    )
    bake_phase = progress._estimate_batch_remaining(current_job_baked=False, **common)
    render_phase = progress._estimate_batch_remaining(current_job_baked=True, **common)
    assert bake_phase > render_phase > 0.0


def test_find_running_log_on_tmpdir(progress, tmp_path):
    """End-to-end on a temp jobs dir: an active log with no .done marker is found;
    once its .done lands it is no longer the running job."""
    (tmp_path / "job_0000.log").write_text("Baking frame 3\n", encoding="utf-8")
    result = progress._find_running_log(str(tmp_path))
    assert result is not None and result[1] == "job_0000"

    (tmp_path / "job_0000.done").write_text("ok", encoding="utf-8")
    assert progress._find_running_log(str(tmp_path)) is None
