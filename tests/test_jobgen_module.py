"""TODO-58 module #1 regression tests: the pure job-gen core lives in jobgen.py.

The 6.1k-line ``__init__.py`` is being split into a package one module at a time.
The first extraction moved the ``bpy``-free job-generation cluster into
``BatchSimLab.jobgen`` and re-imported every public name back into the package
``__init__`` so existing ``from BatchSimLab import …`` entry points (and the ~209
job-gen tests) keep resolving unchanged.

These tests lock in that contract: the names must live in ``jobgen`` AND remain
reachable from the package, as the *same* object (a future move that forgets the
re-export, or that leaves a stale duplicate behind in ``__init__``, fails CI
rather than silently breaking callers).
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# The job-gen surface that callers/tests rely on being importable from the
# package root.  Underscore-prefixed names are included deliberately — the test
# corpus and internal call sites reach for them via the package namespace.
_JOBGEN_NAMES = [
    "ITERABLE_PARAMS",
    "_VELOCITY_DEFAULT",
    "_parse_velocity_vector",
    "_format_velocity_vector",
    "expand_param",
    "_first_value",
    "_default_job",
    "generate_jobs_limited",
    "generate_jobs_all",
    "_EMITTER_SCALARS",
    "_EMITTER_VELOCITY_SCALARS",
    "_emitter_velocity_vectors",
    "_emitter_baseline",
    "_default_emitters",
    "_emitter_sweep_axes",
    "_emitter_combinations",
    "generate_jobs",
    "_dedupe_jobs",
    "_fmt_num",
    "_OFF_SUFFIX",
    "_EMITTER_NAME_DEFAULTS",
    "_EMITTER_NAME_ABBR",
    "_emitter_name_tokens",
    "make_name",
]


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("BatchSimLab")


@pytest.fixture(scope="module")
def jobgen():
    return importlib.import_module("BatchSimLab.jobgen")


def test_jobgen_is_a_submodule(jobgen):
    assert jobgen.__name__ == "BatchSimLab.jobgen"


def test_jobgen_is_bpy_free(jobgen):
    """The whole point of this module is to be importable without Blender."""
    import sys
    src_imports_bpy = "import bpy" in open(jobgen.__file__, encoding="utf-8").read()
    assert not src_imports_bpy, "jobgen.py must stay bpy-free (pure, unit-testable)"


@pytest.mark.parametrize("name", _JOBGEN_NAMES)
def test_name_defined_in_jobgen(jobgen, name):
    assert hasattr(jobgen, name), f"{name} must be defined in BatchSimLab.jobgen"


@pytest.mark.parametrize("name", _JOBGEN_NAMES)
def test_name_reexported_from_package(pkg, name):
    assert hasattr(pkg, name), (
        f"{name} must remain importable from the BatchSimLab package "
        f"(re-export from jobgen in __init__)"
    )


@pytest.mark.parametrize("name", _JOBGEN_NAMES)
def test_reexport_is_same_object(pkg, jobgen, name):
    """Package attribute must be the identical object from jobgen — not a stale
    duplicate left behind in __init__ after the move."""
    assert getattr(pkg, name) is getattr(jobgen, name), (
        f"BatchSimLab.{name} and BatchSimLab.jobgen.{name} diverged — a duplicate "
        f"definition likely survived the extraction"
    )
