"""v0.6.0 regression tests:

- BUG-012: done-count excludes failed jobs in batch_progress display
- TODO-37: Gas Parameters UI order is Density → Heat → Vorticity
- TODO-38: text object precision preserves multi-decimal values
- TODO-39: make_name() adds -Slow suffix when slow_dissolve=True
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab"))

from SmokeSimLab import make_name


def _addon_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab",
                     "__init__.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _worker_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab",
                     "smoke_worker.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


# ── BUG-012: done-count excludes failed ────────────────────────────────────

class TestBug012DoneCountExcludesFailed:
    """The (N/M done) display in batch_progress must show successful jobs
    only.  Failed jobs (whose .done file content contains "error" — the
    .bat's record of a nonzero launcher exit) get a separate ", F failed"
    suffix when F > 0."""

    def test_done_files_split_into_success_and_failed_buckets(self):
        src = _addon_src()
        # The poll body must compute both done_success and done_failed by
        # reading each .done file's content for "error".
        assert "done_success" in src, (
            "BUG-012 fix requires a done_success counter"
        )
        assert "done_failed" in src, (
            "BUG-012 fix requires a done_failed counter"
        )

    def test_done_split_inspects_file_content(self):
        """The split must read each .done file and inspect content for
        "error" (matches the .bat's `error exit N` format on launcher
        nonzero exit)."""
        src = _addon_src()
        # Find the BUG-012 split block.
        idx = src.find("done_success = 0")
        assert idx > 0, "BUG-012 split block not found"
        block = src[idx:idx + 800]
        assert "open(" in block, "must open each .done file"
        assert '"error"' in block, "must check for 'error' in content"
        assert "done_failed += 1" in block
        assert "done_success += 1" in block

    def test_progress_display_uses_done_success_not_total_done(self):
        """The user-visible (N/M done) string must use done_success via the
        _done_str helper — otherwise the bug isn't fixed."""
        src = _addon_src()
        # The bake-only branch and the two-pass branch must both assign
        # batch_progress using the _done_str helper.  Count occurrences.
        assert src.count("{_done_str}") >= 2, (
            f"expected 2+ batch_progress assignments referencing {{_done_str}}; "
            f"found {src.count('{_done_str}')}"
        )
        # And _done_str must be derived from done_success.
        assert "done_success" in src
        # The raw len(done_files) approach must no longer be in the display
        # (it's fine for the batch-completion trigger, just not the (N done) text).
        assert "{done}/{total} done" not in src, (
            "old '{done}/{total} done' display string must be removed; "
            "use done_success-based _done_str instead"
        )

    def test_failed_count_only_shown_when_nonzero(self):
        """Clean batches (no failures) should still display "(N done)" —
        not "(N done, 0 failed)" which adds noise."""
        src = _addon_src()
        # The conditional builds two different format strings.
        assert "if done_failed > 0:" in src
        # The non-failed branch should NOT include "0 failed" literally.
        assert ", 0 failed" not in src


# ── TODO-37: Gas Parameters order matches Blender ──────────────────────────

class TestTodo37GasParamsOrder:
    """Gas Parameters UI order must be Density → Heat → Vorticity to match
    Blender's native Fluid Domain panel."""

    def test_density_drawn_before_heat_before_vorticity(self):
        src = _addon_src()
        idx_alpha     = src.find('_sub_param_ui(box, s, "alpha",')
        idx_beta      = src.find('_sub_param_ui(box, s, "beta",')
        idx_vorticity = src.find('_sub_param_ui(box, s, "vorticity",')
        assert idx_alpha     > 0
        assert idx_beta      > 0
        assert idx_vorticity > 0
        assert idx_alpha < idx_beta < idx_vorticity, (
            f"v0.6.0 TODO-37: expected Density(alpha) → Heat(beta) → Vorticity "
            f"draw order, got positions {idx_alpha}/{idx_beta}/{idx_vorticity}"
        )


# ── TODO-38: text object precision preserves values ────────────────────────

class TestTodo38TextPrecision:
    """Vort/Dens/Heat text objects must not be truncated to 1 decimal —
    use 3-decimal rounding with :g formatting to preserve precision while
    trimming trailing zeros."""

    def test_worker_uses_precision_3_or_better(self):
        src = _worker_src()
        # The old form was round(x, 1).  The new form is round(x, 3):g.
        # Assert the old single-decimal form is gone from the text-objects
        # block, and the new :g format is present.
        m = re.search(r"def update_text_objects[\s\S]+?\n\n\n", src)
        assert m, "update_text_objects function not found"
        body = m.group(0)
        # New form present
        assert ":g}" in body or '"%g"' in body or "format(" in body, (
            "expected `:g` (or equivalent) formatting in text-object output "
            "to preserve precision while trimming trailing zeros"
        )
        # Old `round(x, 1)` form on vort/dens/heat must be gone.
        assert "round(float(params['vorticity']), 1)" not in body
        assert "round(float(params['alpha']), 1)" not in body
        assert "round(float(params['beta']), 1)" not in body


# ── TODO-39: Slow indicator in filename ────────────────────────────────────

class TestTodo39SlowFilenameIndicator:
    """make_name() must append "-Slow" to the dissolve part when
    slow_dissolve=True so users can distinguish slow from fast dissolve
    jobs by filename alone.  Backwards-compat: slow=False keeps the
    original "D5" form (matches existing on-disk caches)."""

    def _base_params(self):
        return dict(
            resolution=128, vorticity=0.0, alpha=1.0, beta=1.0,
            dissolve_speed=5, slow_dissolve=False,
            noise_upres=2, noise_strength=2.0, noise_spatial_scale=2.0,
            use_dissolve=False, use_noise=False,
        )

    def test_dissolve_off_unaffected(self):
        p = self._base_params()
        p["use_dissolve"] = False
        assert "D-OFF" in make_name(p)
        assert "Slow" not in make_name(p)

    def test_slow_off_uses_fast_suffix(self):
        """v0.7.0 BUG-013: slow=False jobs now produce explicit '-Fast'
        suffix.  Previously (v0.6.0) they kept the original 'D5' form for
        backwards-compat — but that caused pre-v0.6.0 slow=True caches
        (also named 'D5') to be silently reused by v0.6.0 slow=False
        jobs.  Explicit '-Fast' suffix eliminates the collision; pre-v0.6.0
        caches become orphaned and must be re-baked (acceptable for
        correctness)."""
        p = self._base_params()
        p["use_dissolve"]  = True
        p["dissolve_speed"] = 5
        p["slow_dissolve"]  = False
        name = make_name(p)
        assert "_D5-Fast_" in name, (
            f"slow=False must produce 'D5-Fast' (v0.7.0 BUG-013 fix); "
            f"got: {name}"
        )
        assert "-Slow" not in name

    def test_slow_on_appends_slow_suffix(self):
        p = self._base_params()
        p["use_dissolve"]  = True
        p["dissolve_speed"] = 5
        p["slow_dissolve"]  = True
        name = make_name(p)
        assert "_D5-Slow_" in name, (
            f"slow=True must produce 'D5-Slow' so it distinguishes from "
            f"a slow=False job at the same speed; got: {name}"
        )

    def test_two_jobs_differing_only_in_slow_get_distinct_names(self):
        """The reason the bug was filed: at the same dissolve_speed, slow=on
        and slow=off jobs previously had the same filename, making them
        indistinguishable on disk."""
        p_fast = self._base_params()
        p_fast["use_dissolve"] = True
        p_fast["slow_dissolve"] = False
        p_fast["dissolve_speed"] = 5
        p_slow = dict(p_fast)
        p_slow["slow_dissolve"] = True
        assert make_name(p_fast) != make_name(p_slow), (
            "slow=on and slow=off jobs with otherwise-identical params "
            "must produce distinct filenames"
        )


class TestV060VersionBumps:
    """Version floor regression guards.  Updated in v0.6.1: assert
    minimum version (>= 0.6.0) rather than exact pin, so future patch
    bumps don't keep breaking these tests."""

    def test_addon_at_least_0_6_0(self):
        src = _addon_src()
        m = re.search(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)', src)
        assert m, "addon bl_info version line not found"
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        assert (major, minor, patch) >= (0, 6, 0), (
            f"addon version {major}.{minor}.{patch} < 0.6.0 — "
            f"v0.6.0 introduced the TODO-39 / BUG-012 fixes"
        )

    def test_worker_at_least_0_6_0(self):
        src = _worker_src()
        m = re.search(r'^WORKER_VERSION = "(\d+)\.(\d+)\.(\d+)"', src, re.MULTILINE)
        assert m, "WORKER_VERSION constant not found"
        major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        assert (major, minor, patch) >= (0, 6, 0), (
            f"WORKER_VERSION {major}.{minor}.{patch} < 0.6.0 — "
            f"v0.6.0 introduced the text-precision fix"
        )

    def test_addon_expects_matching_worker(self):
        """The addon's _EXPECTED_WORKER_VERSION should match what's
        currently in smoke_worker.py — otherwise the addon would warn
        users about a "wrong" worker version on every Run Batch."""
        src_addon  = _addon_src()
        src_worker = _worker_src()
        m_expected = re.search(
            r'_EXPECTED_WORKER_VERSION\s+= "(\d+\.\d+\.\d+)"',
            src_addon,
        )
        m_actual   = re.search(
            r'^WORKER_VERSION = "(\d+\.\d+\.\d+)"',
            src_worker,
            re.MULTILINE,
        )
        assert m_expected and m_actual
        assert m_expected.group(1) == m_actual.group(1), (
            f"addon expects worker {m_expected.group(1)!r} but worker "
            f"reports {m_actual.group(1)!r} — bump one to match the other"
        )
