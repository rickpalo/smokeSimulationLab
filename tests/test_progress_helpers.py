"""Tests for progress-bar helper functions: _format_eta, _count_png_frames, _find_running_log."""
import sys
import os
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import _count_png_frames, _find_running_log, _format_eta

# Regression: _format_eta must never produce negative strings.


# ---------------------------------------------------------------------------
# _format_eta
# ---------------------------------------------------------------------------

class TestFormatEta:
    def test_zero_seconds(self):
        s = _format_eta(0)
        assert "0s" in s and "remaining" in s

    def test_under_one_minute(self):
        s = _format_eta(45)
        assert "45s" in s and "remaining" in s

    def test_exactly_one_minute(self):
        s = _format_eta(60)
        assert "1 min" in s and "remaining" in s

    def test_minutes(self):
        s = _format_eta(150)
        assert "2 min" in s and "remaining" in s

    def test_one_hour(self):
        s = _format_eta(3600)
        assert "1h" in s

    def test_hours_and_minutes(self):
        s = _format_eta(3660)
        assert "1h" in s and "1min" in s

    def test_under_sixty_is_seconds_not_minutes(self):
        assert "min" not in _format_eta(59)

    # Regression: Bar 2 showed ~-60000s when job_remaining went negative.
    # _format_eta must clamp negatives to zero rather than propagating them.
    def test_negative_clamped_to_zero(self):
        s = _format_eta(-60000)
        assert "-" not in s
        assert "0s" in s

    def test_negative_one_clamped(self):
        s = _format_eta(-1)
        assert "-" not in s


# ---------------------------------------------------------------------------
# _count_png_frames
# ---------------------------------------------------------------------------

def _make_job(tmp_path, frame_end=5, name="MyJob"):
    """Write a minimal job JSON and return (jobs_dir, frames_dir)."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(exist_ok=True)
    frames_dir = tmp_path / "Renders" / f"{name}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    job_data = {"frame_end": frame_end, "output_path": str(tmp_path), "name": name}
    (jobs_dir / "job_0000.json").write_text(json.dumps(job_data))
    return jobs_dir, frames_dir


class TestCountPngFrames:
    def test_returns_none_on_missing_json(self, tmp_path):
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        assert _count_png_frames(str(jobs_dir), "job_0000") is None

    def test_empty_frames_dir_returns_zero(self, tmp_path):
        jobs_dir, _ = _make_job(tmp_path)
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 0
        assert total == 5

    def test_counts_all_frames_without_since(self, tmp_path):
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        for i in range(1, 6):
            (frames_dir / f"frame_{i:04d}.png").write_bytes(b"")
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 5
        assert total == 5

    def test_counts_all_frames_when_no_start_time(self, tmp_path):
        # Without start_time, all PNGs are counted regardless of mtime.
        # Used for the baseline-setting call at render-stage entry.
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        t_old = time.time() - 200
        for i in range(1, 6):
            f = frames_dir / f"frame_{i:04d}.png"
            f.write_bytes(b"")
            os.utime(str(f), (t_old, t_old))
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 5
        assert total == 5

    # ── Regression: BUG-003 "0 of N" render progress (v0.2.19) ──────────────
    # When PNGs are re-rendered (overwritten, not added), the total file count
    # stays constant so baseline subtraction always yields 0.  The mtime-based
    # start_time parameter fixes this.

    def test_start_time_filters_old_files(self, tmp_path):
        # Files with mtime < (start_time - 10) must NOT be counted.
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        t_old   = time.time() - 200
        t_start = time.time() - 5   # "job started 5 seconds ago"
        for i in range(1, 6):
            f = frames_dir / f"frame_{i:04d}.png"
            f.write_bytes(b"")
            os.utime(str(f), (t_old, t_old))   # all files are "old"
        count, total = _count_png_frames(str(jobs_dir), "job_0000", start_time=t_start)
        assert count == 0   # all filtered out
        assert total == 5

    def test_start_time_counts_new_files_only(self, tmp_path):
        # Only files with mtime >= (start_time - 10) are counted.
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        t_old   = time.time() - 200
        t_start = time.time() - 5
        # Frames 1-3 are old; frames 4-5 are recent (simulating re-render progress).
        for i in range(1, 4):
            f = frames_dir / f"frame_{i:04d}.png"
            f.write_bytes(b"")
            os.utime(str(f), (t_old, t_old))
        for i in range(4, 6):
            (frames_dir / f"frame_{i:04d}.png").write_bytes(b"")  # mtime = now
        count, total = _count_png_frames(str(jobs_dir), "job_0000", start_time=t_start)
        assert count == 2   # only frames 4 and 5
        assert total == 5

    def test_start_time_overwrite_scenario(self, tmp_path):
        # Regression for "Rendering (0 of 500)": 500 PNGs exist from a previous run,
        # now being overwritten one by one.  Without start_time the count stays at 500
        # forever.  With start_time, each overwritten file's mtime updates and is counted.
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        t_old   = time.time() - 200
        t_start = time.time() - 5
        # Create 5 "old" PNGs (previous run).
        for i in range(1, 6):
            f = frames_dir / f"frame_{i:04d}.png"
            f.write_bytes(b"")
            os.utime(str(f), (t_old, t_old))
        # Simulate 2 frames re-rendered (mtime updated to now).
        for i in range(1, 3):
            (frames_dir / f"frame_{i:04d}.png").write_bytes(b"x")
        # Without start_time: sees all 5 → baseline would give 0 progress.
        raw, _ = _count_png_frames(str(jobs_dir), "job_0000")
        assert raw == 5
        # With start_time: sees only the 2 re-rendered frames → correct progress.
        new_count, _ = _count_png_frames(str(jobs_dir), "job_0000", start_time=t_start)
        assert new_count == 2

    def test_baseline_subtraction_gives_new_frames(self, tmp_path):
        # Caller subtracts baseline to get only frames rendered in the current run.
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        for i in range(1, 6):
            (frames_dir / f"frame_{i:04d}.png").write_bytes(b"")
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        baseline = 3
        new_frames = max(count - baseline, 0)
        assert new_frames == 2   # frames 4 and 5 are "new" this run
        assert total == 5

    def test_zero_baseline_means_all_frames_new(self, tmp_path):
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=3)
        for i in range(1, 4):
            (frames_dir / f"frame_{i:04d}.png").write_bytes(b"")
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert max(count - 0, 0) == 3
        assert total == 3

    def test_ignores_non_png_files(self, tmp_path):
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=3)
        (frames_dir / "frame_0001.png").write_bytes(b"")
        (frames_dir / "frame_0002.jpg").write_bytes(b"")   # wrong extension
        (frames_dir / "result.png").write_bytes(b"")       # wrong name pattern
        count, _ = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 1

    def test_frame_end_comes_from_json(self, tmp_path):
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=250)
        (frames_dir / "frame_0001.png").write_bytes(b"")
        _, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert total == 250

    def test_missing_frames_dir_returns_zero_count(self, tmp_path):
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=5)
        # Don't create the frames directory
        frames_dir.rmdir()
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 0
        assert total == 5


# ---------------------------------------------------------------------------
# _find_running_log
# ---------------------------------------------------------------------------

def _make_jobs_dir(tmp_path):
    d = tmp_path / "jobs"
    d.mkdir()
    return d


def _write(jobs_dir, name, content=""):
    (jobs_dir / name).write_text(content)


class TestFindRunningLog:
    def test_returns_none_when_no_logs(self, tmp_path):
        jobs_dir = _make_jobs_dir(tmp_path)
        assert _find_running_log(str(jobs_dir)) is None

    def test_returns_none_on_missing_dir(self, tmp_path):
        assert _find_running_log(str(tmp_path / "missing")) is None

    def test_finds_active_log(self, tmp_path):
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Baking simulation")
        result = _find_running_log(str(jobs_dir))
        assert result is not None
        log_file, log_stem, tail = result
        assert log_file == "job_0000.log"
        assert log_stem == "job_0000"

    def test_skips_log_with_done_file(self, tmp_path):
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Baking simulation")
        _write(jobs_dir, "job_0000.done", "")
        assert _find_running_log(str(jobs_dir)) is None

    def test_skips_log_with_done_marker_in_tail(self, tmp_path):
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Done. Results logged")
        assert _find_running_log(str(jobs_dir)) is None

    def test_returns_highest_active_log(self, tmp_path):
        # job_0000 is done; job_0001 is still running.
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Baking simulation")
        _write(jobs_dir, "job_0000.done", "")
        _write(jobs_dir, "job_0001.log", "Rendering animation")
        result = _find_running_log(str(jobs_dir))
        assert result is not None
        assert result[0] == "job_0001.log"

    # ── Regression: BUG — stale log shadows later completed jobs (v0.2.19) ──
    # A job that crashes without writing a .done file stays the "active" log
    # forever.  Fix: skip any log where a higher-numbered .done stem exists.

    def test_stale_log_skipped_when_higher_done_exists(self, tmp_path):
        # job_0000 crashed — no .done, no done marker in log.
        # job_0001 completed normally.
        # _find_running_log must return None (no active job), not job_0000.
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Baking simulation")   # stale, no .done
        _write(jobs_dir, "job_0001.log", "Done. Results logged")
        _write(jobs_dir, "job_0001.done", "")
        assert _find_running_log(str(jobs_dir)) is None

    def test_stale_log_skipped_with_multiple_later_jobs(self, tmp_path):
        # job_0000 stale; jobs 1 and 2 both completed.
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Some stage")
        _write(jobs_dir, "job_0001.log", "Done. Results logged")
        _write(jobs_dir, "job_0001.done", "")
        _write(jobs_dir, "job_0002.log", "Done. Results logged")
        _write(jobs_dir, "job_0002.done", "")
        assert _find_running_log(str(jobs_dir)) is None

    def test_stale_log_skipped_later_job_still_running(self, tmp_path):
        # job_0000 stale; job_0001 completed; job_0002 is active.
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0000.log", "Baking simulation")   # stale
        _write(jobs_dir, "job_0001.log", "Done. Results logged")
        _write(jobs_dir, "job_0001.done", "")
        _write(jobs_dir, "job_0002.log", "Rendering animation")  # active
        result = _find_running_log(str(jobs_dir))
        assert result is not None
        assert result[0] == "job_0002.log"

    def test_retry_log_not_skipped_by_base_done(self, tmp_path):
        # First run of job_0001 failed (job_0001.done exists with error content).
        # Retry (job_0001_retry.log) is the active log — must NOT be skipped.
        jobs_dir = _make_jobs_dir(tmp_path)
        _write(jobs_dir, "job_0001.log", "Done. Results logged")
        _write(jobs_dir, "job_0001.done", "error: worker exit 1")
        _write(jobs_dir, "job_0001_retry.log", "Baking simulation")
        result = _find_running_log(str(jobs_dir))
        assert result is not None
        assert result[0] == "job_0001_retry.log"
