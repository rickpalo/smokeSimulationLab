"""Static guard against the TODO-58 package-split's worst failure mode: a name
USED in a module but IMPORTED nowhere in it (a missing cross-module import or a
typo).

The pytest suite stubs ``bpy`` and the real-Blender REGISTER smoke-test only
imports the package — NEITHER actually calls the operator ``execute`` / panel
``draw`` bodies, so a missing import inside one of those functions raises only at
runtime, in production.  That is exactly how ``export_batch`` shipped in 0.9.5
referencing ``_dedupe_jobs`` / ``_blend_domain_resolution`` without importing them
(BUG-019), and how engine.py initially missed ``math`` + the rate constants.

This test parses every addon-package module and flags any ``Name`` loaded but
bound in NO scope (module-level def/class/import/assignment, any function's
params/locals, ``global``/``nonlocal``, comprehension/for/with/except targets) and
not a builtin.  Union-of-all-bindings is deliberate: a genuine missing import is
unbound *everywhere*, so we don't need precise per-scope resolution and we avoid
false positives from names that are legitimately bound in some other scope.

Deferred (function-local) ``from . import X`` counts as a binding, so the package's
intentional cycle-breaking imports pass.
"""
import ast
import builtins
import glob
import os

import pytest

_PKG_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab")

# Worker/launcher are standalone deployables (run by Blender with their own
# runtime globals), not part of the importable addon package — out of scope here.
_MODULES = [
    p for p in sorted(glob.glob(os.path.join(_PKG_DIR, "*.py")))
    if not os.path.basename(p).startswith("smoke_")
]

_ALWAYS_BOUND = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__builtins__",
    "__spec__", "__loader__", "__annotations__",
}


def _unbound_names(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    bound = set(_ALWAYS_BOUND)
    loaded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                bound.add(arg.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Lambda):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                bound.add(arg.arg)
            if args.vararg:
                bound.add(args.vararg.arg)
            if args.kwarg:
                bound.add(args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                loaded.add(node.id)
            else:  # Store / Del
                bound.add(node.id)
    return sorted(loaded - bound)


def test_modules_discovered():
    names = {os.path.basename(p) for p in _MODULES}
    # the split modules must be present (guards a bad glob / moved dir)
    assert {"__init__.py", "jobgen.py", "operators.py", "engine.py"} <= names


@pytest.mark.parametrize("path", _MODULES, ids=[os.path.basename(p) for p in _MODULES])
def test_no_unbound_names(path):
    undef = _unbound_names(path)
    assert not undef, (
        f"{os.path.basename(path)} uses name(s) bound nowhere in the module — a "
        f"missing cross-module import or typo (would NameError at runtime, not "
        f"caught by the bpy-stubbed suite or the REGISTER smoke-test): {undef}"
    )
