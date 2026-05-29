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


class TestPresaveRenameRetry:
    """v0.5.1: presave rename must retry to survive Synology Drive / killed-
    process file-handle locks.  v0.5.0Test (2026-05-28) failed because the
    single rename hit Access Denied, _presave_active stayed False, the next
    cache_directory assignment triggered BUG-004 wipe, and 34 baked frames
    were destroyed."""

    def test_rename_in_retry_loop(self):
        src = _worker_src()
        # Find the presave rename call and check the surrounding context for
        # a retry loop (for-loop with range(N) where N >= 3).
        idx = src.find("os.rename(effective_cache_dir, _presave_dir)")
        assert idx > 0, "presave os.rename call missing"
        # Within ~500 chars before, expect a `for _attempt in range(...)` loop.
        ctx = src[max(0, idx - 500):idx]
        assert re.search(r"for\s+\w+\s+in\s+range\((\d+)\)", ctx), (
            f"presave rename should be inside a for-loop for retry; "
            f"context: {ctx[-300:]!r}"
        )
        m = re.search(r"for\s+\w+\s+in\s+range\((\d+)\)", ctx)
        attempts = int(m.group(1))
        assert attempts >= 3, (
            f"presave retry loop has only {attempts} attempts; "
            f"need at least 3 to survive Windows file-lock transients"
        )

    def test_retry_sleeps_between_attempts(self):
        """The retry loop must include a sleep — otherwise it spins faster than
        the kernel releases handles and all attempts fail in milliseconds."""
        src = _worker_src()
        idx = src.find("os.rename(effective_cache_dir, _presave_dir)")
        assert idx > 0
        # Within ~700 chars after the rename call (covering the except + retry),
        # we should see a _time.sleep call.
        window = src[idx:idx + 1200]
        assert re.search(r"_time\.sleep\(", window), (
            f"presave retry loop missing sleep between attempts; "
            f"context: {window!r}"
        )


class TestCountDataFilesFastScan:
    """v0.5.3: _count_data_files must use os.scandir on data/+noise/ only,
    NOT os.walk on the full tree.  v0.5.2Test render-phase hung indefinitely
    on os.walk because Windows file-system filter chain (Norton + Synology
    mount + Windows Search) serialised kernel calls while the cache dir's
    catalog was updating after the bake-phase rename → restore."""

    def test_uses_scandir_not_walk(self):
        src = _worker_src()
        # Find the function body and check it uses os.scandir, not os.walk.
        m = re.search(
            r"def _count_data_files\(directory\):[\s\S]+?return count",
            src,
        )
        assert m, "_count_data_files function not found"
        body = m.group(0)
        # Strip the docstring so historical mentions of os.walk in the
        # explanation don't trip the assertion.
        code = re.sub(r'"""[\s\S]*?"""', "", body)
        assert "os.scandir(" in code, (
            "v0.5.3 fix requires os.scandir (faster on Windows + fewer kernel "
            "calls through the file-system filter chain)"
        )
        assert "os.walk(" not in code, (
            "os.walk was the v0.5.2 hang cause — must not appear as a call"
        )

    def test_only_scans_data_and_noise_subdirs(self):
        """The function must explicitly target data/ and noise/, skipping
        the liquid-only mesh/, particles/, guiding/ and the already-excluded
        config/ — that's how we avoid the slow paths."""
        src = _worker_src()
        m = re.search(
            r"def _count_data_files\(directory\):[\s\S]+?return count",
            src,
        )
        assert m
        body = m.group(0)
        # The subdir tuple should mention both data and noise
        assert re.search(r'"data"\s*,\s*"noise"|"noise"\s*,\s*"data"', body) or \
               ("'data'" in body and "'noise'" in body), (
            "function should iterate exactly the (data, noise) subdirs — see "
            "v0.5.3 docstring for the rationale"
        )
        # And NOT mention the slow liquid-only ones
        for slow_subdir in ("mesh", "particles", "guiding"):
            # quoted as literal subdir name
            for quote in ('"', "'"):
                assert f"{quote}{slow_subdir}{quote}" not in body, (
                    f"function should not scan {slow_subdir}/ — slow and "
                    f"smoke-only addon doesn't write there"
                )

    def test_still_uses_vdb_uni_regex(self):
        """Preserves prior count semantics — only frame-numbered files count."""
        src = _worker_src()
        m = re.search(
            r"def _count_data_files\(directory\):[\s\S]+?return count",
            src,
        )
        assert m
        body = m.group(0)
        assert r"_\d+\.(vdb|uni)" in body or r'_\\d+\\.(vdb|uni)' in body, (
            "must still filter by the _NNNN.vdb / _NNNN.uni pattern"
        )

    def test_handles_missing_subdirs_gracefully(self):
        """Empty cache dir (e.g. fresh after wipe) must not raise — the
        function returns 0 if data/ and noise/ don't exist."""
        src = _worker_src()
        m = re.search(
            r"def _count_data_files\(directory\):[\s\S]+?return count",
            src,
        )
        assert m
        body = m.group(0)
        assert "os.path.isdir" in body, (
            "must guard each subdir with os.path.isdir to avoid raising "
            "when the cache dir is fresh/empty"
        )
        assert "except OSError" in body or "try:" in body, (
            "should catch OSError around scandir for transient I/O errors"
        )


class TestPostAssignmentRewalk:
    """v0.5.1: when presave didn't happen and we counted existing frames,
    re-walk the cache after Mantaflow's cache_directory assignment.  If the
    count dropped (BUG-004 wipe), downgrade baked_frames so the RESUME
    decision doesn't claim to preserve frames that no longer exist."""

    def test_rewalk_after_assignment_when_no_presave(self):
        src = _worker_src()
        # Find the "No presave active" log line and verify there's a re-walk
        # after it (using os.walk on effective_cache_dir).
        idx = src.find("No presave active — Mantaflow reinitialised the domain")
        assert idx > 0, "expected log line missing"
        # Within ~1500 chars after, expect a re-walk and a baked_frames update.
        window = src[idx:idx + 1500]
        assert "os.walk(effective_cache_dir)" in window, (
            "no re-walk after cache_directory assignment when presave inactive"
        )
        assert "baked_frames = " in window, (
            "re-walk should reassign baked_frames so the bake decision is honest"
        )

    def test_rewalk_logs_downgrade(self):
        """The re-walk must log when it detects a wipe so the user can correlate
        with the 'rename failed' warning above it in the log."""
        src = _worker_src()
        # A wipe log should mention BUG-004 or "wipe" and the frame count diff.
        assert re.search(r"BUG-004|cache_directory assignment dropped|wipe", src,
                          re.IGNORECASE), (
            "re-walk should log clearly when it detects a wipe"
        )
