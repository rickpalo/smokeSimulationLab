"""v0.5.0 BUG-010 regression tests: MODULAR cache_type + bake_data/bake_noise.

The worker switched from `bpy.ops.fluid.bake_all()` under cache_type='ALL' to
`bpy.ops.fluid.bake_data()` (+ `bake_noise()` if use_noise) under cache_type=
'MODULAR'.  Probes v6/v7 in scripts/experiments/ proved this gives true
in-process partial resume (frames 1..N-1 preserved, frame N rewritten as
boundary, no save/reload needed).

These tests assert the source contains the expected pattern so a future
regression — e.g. someone reverting to bake_all() — fails CI rather than
breaking RESUME silently in production.
"""
import os
import re

import pytest


def _worker_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab",
                     "smoke_worker.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class TestCacheTypeModular:
    """Worker must force d.cache_type = 'MODULAR' before bake."""

    def test_assigns_modular_cache_type(self):
        src = _worker_src()
        assert "d.cache_type = 'MODULAR'" in src, (
            "worker must assign d.cache_type = 'MODULAR' for resume to work"
        )

    def test_assignment_in_try_block(self):
        """The assignment is wrapped in try/except so older Blender versions
        without the MODULAR enum don't crash the worker outright."""
        src = _worker_src()
        # Find the line and check the surrounding context.
        idx = src.find("d.cache_type = 'MODULAR'")
        assert idx > 0
        # Within ~50 chars before, we should see 'try:'.
        assert "try:" in src[max(0, idx - 80):idx], (
            "d.cache_type = 'MODULAR' should be in a try block"
        )
        # Within ~250 chars after, we should see 'except'.
        assert "except" in src[idx:idx + 250], (
            "d.cache_type assignment needs an except handler"
        )


class TestBakeOperators:
    """Worker must call bake_data() (and bake_noise() if use_noise), never bake_all()."""

    def test_no_bake_all_calls(self):
        """bake_all() does NOT honor MODULAR's resume semantics — must be gone."""
        src = _worker_src()
        # Match `bpy.ops.fluid.bake_all(` with optional whitespace.
        # Comments / docstrings referencing the old name are fine; actual call is not.
        for m in re.finditer(r"bpy\.ops\.fluid\.bake_all\s*\(", src):
            line_start = src.rfind("\n", 0, m.start()) + 1
            line_end   = src.find("\n", m.start())
            line       = src[line_start:line_end].lstrip()
            # Skip comment lines.
            if line.startswith("#"):
                continue
            pytest.fail(f"unexpected bake_all() call: {line!r}")

    def test_bake_data_called(self):
        src = _worker_src()
        assert "bpy.ops.fluid.bake_data()" in src, (
            "worker must call bpy.ops.fluid.bake_data() in place of bake_all()"
        )
        # Should appear at least twice — once in RESUME branch, once in FULL branch.
        assert src.count("bpy.ops.fluid.bake_data()") >= 2, (
            f"expected bake_data() in both RESUME and FULL branches, "
            f"found {src.count('bpy.ops.fluid.bake_data()')}"
        )

    def test_bake_noise_called_conditionally(self):
        """bake_noise() must be guarded by `if p["use_noise"]:` so jobs with
        noise off don't crash on a missing noise layer."""
        src = _worker_src()
        assert "bpy.ops.fluid.bake_noise()" in src, (
            "worker must call bpy.ops.fluid.bake_noise() under MODULAR"
        )
        # Both branches need the conditional guard.
        assert src.count("bpy.ops.fluid.bake_noise()") >= 2, (
            f"expected bake_noise() in both RESUME and FULL branches, "
            f"found {src.count('bpy.ops.fluid.bake_noise()')}"
        )
        # Each bake_noise() call should be preceded (within ~150 chars) by
        # the use_noise guard.
        for m in re.finditer(r"bpy\.ops\.fluid\.bake_noise\(\)", src):
            ctx = src[max(0, m.start() - 200):m.start()]
            assert 'p["use_noise"]' in ctx or "p['use_noise']" in ctx, (
                f"bake_noise() at char {m.start()} not guarded by use_noise check; "
                f"context: {ctx[-200:]!r}"
            )


class TestErrorHandling:
    """Each modular bake operator must check 'FINISHED' and sys.exit(1) on failure."""

    def test_bake_data_finished_check(self):
        src = _worker_src()
        # After each bake_data() call there should be a FINISHED guard within ~250 chars.
        for m in re.finditer(r"bpy\.ops\.fluid\.bake_data\(\)", src):
            window = src[m.end():m.end() + 400]
            assert "'FINISHED'" in window, (
                f"bake_data() at char {m.start()} missing 'FINISHED' check"
            )
            assert "sys.exit(1)" in window, (
                f"bake_data() at char {m.start()} missing sys.exit(1) on failure"
            )

    def test_bake_noise_finished_check(self):
        src = _worker_src()
        for m in re.finditer(r"bpy\.ops\.fluid\.bake_noise\(\)", src):
            window = src[m.end():m.end() + 400]
            assert "'FINISHED'" in window, (
                f"bake_noise() at char {m.start()} missing 'FINISHED' check"
            )
            assert "sys.exit(1)" in window, (
                f"bake_noise() at char {m.start()} missing sys.exit(1) on failure"
            )


class TestWorkerVersion:
    def test_worker_version_bumped(self):
        src = _worker_src()
        # 0.5.0 or later (regex matches 0.5.0, 0.5.1, 0.6.0, ...).  Once we
        # leave 0.x this test will need updating.
        m = re.search(r'^WORKER_VERSION = "(\d+)\.(\d+)\.(\d+)"', src, re.MULTILINE)
        assert m, "WORKER_VERSION constant missing"
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        assert (major, minor, patch) >= (0, 5, 0), (
            f"WORKER_VERSION {major}.{minor}.{patch} < 0.5.0 — "
            f"MODULAR fix should bump to 0.5.0"
        )
