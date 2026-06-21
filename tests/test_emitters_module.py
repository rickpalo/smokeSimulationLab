"""TODO-58 module #2 regression tests: emitter discovery/sync lives in emitters.py.

The second extraction moved the fluid-emitter discovery + sync cluster into
``BatchSimLab.emitters`` and re-imported every name back into the package
``__init__``.  Like ``jobgen``, the module is duck-typed on its arguments and
stays ``bpy``-free, and it depends only on the leaf ``jobgen`` module for the
shared velocity-text helpers.

These tests lock in that contract: the names live in ``emitters``, remain
reachable from the package root as the *same* object, and the module imports
without a running Blender (and without a circular import back through
``__init__``).
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

_EMITTER_NAMES = [
    "_blend_domain_resolution",
    "_is_flow_object",
    "find_fluid_emitters",
    "_world_aabb",
    "_aabb_overlap",
    "emitters_inside_domain",
    "find_emitters",
    "_emitter_sync_plan",
    "_EMITTER_FLOW_IMPORT_MAP",
    "_flow_settings_of",
    "_seed_emitter_from_flow",
    "_populate_emitters",
]


@pytest.fixture(scope="module")
def pkg():
    return importlib.import_module("BatchSimLab")


@pytest.fixture(scope="module")
def emitters():
    return importlib.import_module("BatchSimLab.emitters")


def test_emitters_is_a_submodule(emitters):
    assert emitters.__name__ == "BatchSimLab.emitters"


def test_emitters_is_bpy_free(emitters):
    """Discovery is duck-typed on its args, so the module must not import bpy."""
    src = open(emitters.__file__, encoding="utf-8").read()
    assert "import bpy" not in src, "emitters.py must stay bpy-free (pure, unit-testable)"


def test_emitters_depends_on_jobgen_leaf(emitters):
    """The only intra-package dependency is the leaf jobgen module — re-using its
    velocity-text helpers — so there is no import cycle."""
    assert emitters._VELOCITY_DEFAULT is importlib.import_module(
        "BatchSimLab.jobgen")._VELOCITY_DEFAULT


@pytest.mark.parametrize("name", _EMITTER_NAMES)
def test_name_defined_in_emitters(emitters, name):
    assert hasattr(emitters, name), f"{name} must be defined in BatchSimLab.emitters"


@pytest.mark.parametrize("name", _EMITTER_NAMES)
def test_name_reexported_from_package(pkg, name):
    assert hasattr(pkg, name), (
        f"{name} must remain importable from the BatchSimLab package "
        f"(re-export from emitters in __init__)"
    )


@pytest.mark.parametrize("name", _EMITTER_NAMES)
def test_reexport_is_same_object(pkg, emitters, name):
    assert getattr(pkg, name) is getattr(emitters, name), (
        f"BatchSimLab.{name} and BatchSimLab.emitters.{name} diverged — a "
        f"duplicate definition likely survived the extraction"
    )


def test_discovery_works_with_stub_scene(emitters):
    """End-to-end sanity: find_emitters on a stub scene with one in-bounds FLOW
    object returns it — proves the moved cluster is wired together correctly."""
    class _Mod:
        def __init__(self, ftype):
            self.type = 'FLUID'
            self.fluid_type = ftype

    unit_box = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]

    class _Obj:
        def __init__(self, name, ftype):
            self.name = name
            self.modifiers = [_Mod(ftype)]
            self.bound_box = unit_box
            self.matrix_world = identity

    flow = _Obj("Emitter", 'FLOW')
    domain = _Obj("Domain", 'DOMAIN')
    scene = type("Scene", (), {"objects": [flow, domain]})()

    found = emitters.find_emitters(scene, domain)
    assert [o.name for o in found] == ["Emitter"]
