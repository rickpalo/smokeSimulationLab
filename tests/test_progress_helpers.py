"""Tests for progress-bar helper functions: _format_eta, _count_png_frames, _find_running_log."""
import sys
import os
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from BatchSimLab import _count_png_frames, _find_running_log, _format_eta, _STAGES

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

def _make_job(tmp_path, frame_end=5, name="MyJob", frame_start=None):
    """Write a minimal job JSON and return (jobs_dir, frames_dir)."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(exist_ok=True)
    frames_dir = tmp_path / "Renders" / f"{name}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    job_data = {"frame_end": frame_end, "output_path": str(tmp_path), "name": name}
    if frame_start is not None:
        job_data["frame_start"] = frame_start
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

    # ── TODO-66 follow-up: negative Frame Start ─────────────────────────────
    # frame_end alone is the absolute end-frame number, not the frame count,
    # once frame_start can be negative — total must be (end - start + 1).

    def test_total_uses_frame_start_when_present(self, tmp_path):
        jobs_dir, _ = _make_job(tmp_path, frame_end=200, frame_start=-50)
        _, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert total == 251   # -50..200 inclusive

    def test_total_defaults_frame_start_to_one(self, tmp_path):
        # No frame_start key (older job JSON) — must match the pre-TODO-66
        # behavior of total == frame_end.
        jobs_dir, _ = _make_job(tmp_path, frame_end=250)
        _, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert total == 250

    def test_counts_png_named_with_negative_frame_number(self, tmp_path):
        # The worker names PNGs with f"frame_{frame_num:04d}.png", which pads
        # the sign into the width for negative frame numbers (e.g. -49 -> "-049").
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=-40, frame_start=-49)
        for n in ("-049", "-048", "-047"):
            (frames_dir / f"frame_{n}.png").write_bytes(b"")
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 3
        assert total == 10   # -49..-40 inclusive

    def test_frame_end_zero_is_not_treated_as_missing(self, tmp_path):
        # A range like -10..0 is legitimate under TODO-66 (pre-roll ending at
        # frame 0) — frame_end == 0 must not be confused with "key absent".
        jobs_dir, frames_dir = _make_job(tmp_path, frame_end=0, frame_start=-10)
        (frames_dir / "frame_-010.png").write_bytes(b"")
        count, total = _count_png_frames(str(jobs_dir), "job_0000")
        assert count == 1
        assert total == 11   # -10..0 inclusive


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


# ---------------------------------------------------------------------------
# _STAGES vs worker log strings  (v0.5.5 regression guard)
# ---------------------------------------------------------------------------

_WORKER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "BatchSimLab", "smoke_worker.py"
)


def _worker_src():
    with open(_WORKER_PATH, encoding="utf-8") as fh:
        return fh.read()


class TestStagesMatchWorkerLog:
    """v0.5.5: every _STAGES keyword must actually appear in the worker's log
    output, otherwise the progress bar's text gets stuck on whatever stage
    last matched.

    This was a real bug: v0.5.0 changed the bake-start log line from
    "Baking..." to "Baking (MODULAR resume — bake_data)..." (and the FULL
    variant).  Neither contains the substring "Baking..." — so the stage
    never advanced.  FULL bakes were stuck on "Clearing cache" for the
    entire bake (the previous match), and SKIP bakes never moved past
    "Starting".

    These tests assert each _STAGES keyword is present somewhere in the
    worker source so a future log-message rewrite can't silently break
    progress tracking again.
    """

    def test_every_stage_keyword_appears_in_worker(self):
        """Each (keyword, label, completed) entry must match SOMETHING the
        worker actually logs.  Otherwise the stage is dead."""
        src = _worker_src()
        # Exceptions: these keywords are aspirational placeholders the
        # worker doesn't (currently) log directly.  Listed so the test
        # doesn't fail on them, but with a reason that documents why.
        # If you want one of these to work, either add the matching log
        # line to smoke_worker.py or drop the entry from _STAGES.
        _ALLOWED_MISSING = {
            "Setting up cache",   # purely aspirational; bake doesn't log this
                                  # (no separate "setup" phase log line).
        }
        missing = []
        for keyword, _label, _completed in _STAGES:
            if keyword in _ALLOWED_MISSING:
                continue
            if keyword not in src:
                missing.append(keyword)
        assert not missing, (
            f"_STAGES keywords not found in smoke_worker.py: {missing}.\n"
            f"This means the progress bar will get stuck on the previous "
            f"stage's label when the worker reaches that phase. Either add "
            f"a matching _log() call to smoke_worker.py or update _STAGES "
            f"to a keyword the worker actually emits."
        )

    def test_baking_keyword_uses_paren_form(self):
        """v0.5.0 regression guard: the Baking stage's keyword must be the
        paren-prefix "Baking (" form, not the obsolete "Baking..." string."""
        bake_stages = [(kw, lbl) for kw, lbl, _c in _STAGES
                       if lbl == "Baking simulation"]
        assert len(bake_stages) == 1, (
            f"expected exactly one 'Baking simulation' _STAGES entry, "
            f"got {bake_stages}"
        )
        kw, _ = bake_stages[0]
        assert kw == "Baking (", (
            f"v0.5.5: 'Baking simulation' keyword must be 'Baking (' to "
            f"match v0.5.0's 'Baking (MODULAR resume — bake_data)...' / "
            f"'Baking (MODULAR full — bake_data)...' log lines.  Got: {kw!r}"
        )

    def test_skip_bake_keyword_uses_decision_form(self):
        """v0.5.5: the 'Using existing cache' stage's keyword must match
        the worker's actual SKIP BAKE decision log line."""
        skip_stages = [(kw, lbl) for kw, lbl, _c in _STAGES
                       if lbl == "Using existing cache"]
        assert len(skip_stages) == 1
        kw, _ = skip_stages[0]
        assert kw == "Decision : SKIP BAKE", (
            f"v0.5.5: 'Using existing cache' keyword must be "
            f"'Decision : SKIP BAKE' to match the worker's SKIP BAKE "
            f"decision log line.  Got: {kw!r}"
        )

    def test_stage_label_advancement_with_sample_logs(self):
        """End-to-end: feed sample log tails through the same find() loop
        the poller uses, assert the label advances as expected."""
        # Sample log fragments (truncated tails from real runs).
        FULL_BAKE_TAIL = (
            "[J] Job started.\n"
            "[J] Freeing previous cache and baking from scratch...\n"
            "[J] Baking (MODULAR full — bake_data)...\n"
        )
        SKIP_BAKE_TAIL = (
            "[J] Job started.\n"
            "[J] --- Bake decision ---\n"
            "[J]   Decision : SKIP BAKE — all 500 frames confirmed\n"
        )
        RESUME_BAKE_TAIL = (
            "[J] Job started.\n"
            "[J] Baking (MODULAR resume — bake_data)...\n"
        )

        def _stage_for(tail):
            label = "Starting"
            best = -1
            for kw, lbl, _c in _STAGES:
                pos = tail.find(kw)
                if pos > best:
                    best, label = pos, lbl
            return label

        assert _stage_for(FULL_BAKE_TAIL)   == "Baking simulation"
        assert _stage_for(SKIP_BAKE_TAIL)   == "Using existing cache"
        assert _stage_for(RESUME_BAKE_TAIL) == "Baking simulation"
