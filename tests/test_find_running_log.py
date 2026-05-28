"""Regression tests for _find_running_log mtime-based ordering.

The previous implementation used `reversed(sorted(first_logs))` — alphabetical-
highest-first.  That worked for single-pass batches (later jobs both have
higher numbers AND later mtimes), but broke for the two-pass pipeline: after
the bake pass every job's <stem>.log existed, so reversed-alphabetical wrongly
returned the highest-numbered log even while the render pass was working on an
earlier one (v0.4.2 EEVEE test, 2026-05-28: Job 1 rendering frame 6, the poll
reported Job 2 as active and parsed Job 2's stale 'Verifying cache' line into
the subtask text).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import _find_running_log


def _make_log(path, content, mtime):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.utime(path, (mtime, mtime))


class TestFindRunningLogMtimeSort:
    def test_most_recently_touched_wins(self, tmp_path):
        # Two-pass scenario: both bake.done sentinels present (job done baking),
        # neither unphased .done yet, but the render phase is actively writing
        # job_0000.log right now (newer mtime than job_0001.log).
        now = time.time()
        _make_log(tmp_path / "job_0000.log",
                  "Rendering animation (20 frame(s))\n", mtime=now - 1)
        _make_log(tmp_path / "job_0001.log",
                  "phase=bake complete — skipping render and CSV.\n",
                  mtime=now - 60)
        # bake.done present for both; no .done aliases yet.
        (tmp_path / "job_0000.bake.done").write_text("done")
        (tmp_path / "job_0001.bake.done").write_text("done")

        result = _find_running_log(str(tmp_path))
        assert result is not None, "should find an active log"
        log_file = result[0]
        assert log_file == "job_0000.log", (
            f"expected job_0000.log (more recent mtime) but got {log_file}"
        )

    def test_single_pass_still_picks_latest(self, tmp_path):
        # Single-pass: later jobs both have higher numbers AND later mtimes —
        # mtime sort and reversed-alphabetical agree.
        now = time.time()
        _make_log(tmp_path / "job_0000.log", "Done. Results -> ...\n",
                  mtime=now - 120)
        _make_log(tmp_path / "job_0001.log", "Baking...\n", mtime=now - 1)
        (tmp_path / "job_0000.done").write_text("done")

        result = _find_running_log(str(tmp_path))
        assert result is not None
        assert result[0] == "job_0001.log"

    def test_log_with_done_marker_skipped(self, tmp_path):
        # Realistic mid-render-pass: job_0000 finished (its .done alias
        # written), job_0001 is now actively rendering.  The .done filter must
        # exclude job_0000.log even though job_0001.log has the newer mtime
        # (which it does in two-pass mode after the render pass moves on).
        now = time.time()
        _make_log(tmp_path / "job_0000.log",
                  "Done. Results -> ...\n", mtime=now - 10)
        _make_log(tmp_path / "job_0001.log",
                  "Rendering animation (20 frame(s))\n", mtime=now - 1)
        (tmp_path / "job_0000.done").write_text("done")

        result = _find_running_log(str(tmp_path))
        assert result is not None
        assert result[0] == "job_0001.log"

    def test_tied_mtime_breaks_to_higher_filename(self, tmp_path):
        same_mtime = time.time() - 5
        _make_log(tmp_path / "job_0000.log", "x\n", mtime=same_mtime)
        _make_log(tmp_path / "job_0001.log", "y\n", mtime=same_mtime)
        result = _find_running_log(str(tmp_path))
        assert result is not None
        # Deterministic: higher filename wins on mtime tie.
        assert result[0] == "job_0001.log"
