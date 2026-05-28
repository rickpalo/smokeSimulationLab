"""Tests for append/replace export mode helpers."""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import (
    _find_next_job_index,
    _existing_jobs_for_bat,
    _job_run_cmd,
    _job_bat_block,
)


class TestFindNextJobIndex:
    def test_missing_dir_returns_zero(self, tmp_path):
        assert _find_next_job_index(str(tmp_path / "nonexistent")) == 0

    def test_empty_dir_returns_zero(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        assert _find_next_job_index(str(d)) == 0

    def test_single_job_returns_one(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        assert _find_next_job_index(str(d)) == 1

    def test_sequential_jobs(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        for i in range(5):
            (d / f"job_{i:04d}.json").write_text("{}")
        # 5 jobs (0-4) → next is 5
        assert _find_next_job_index(str(d)) == 5

    def test_non_sequential_uses_max(self, tmp_path):
        # If jobs 0, 2, 7 exist (gap), next is 8.
        d = tmp_path / "jobs"
        d.mkdir()
        for i in (0, 2, 7):
            (d / f"job_{i:04d}.json").write_text("{}")
        assert _find_next_job_index(str(d)) == 8

    def test_ignores_non_json_files(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        (d / "job_0001.log").write_text("log")
        (d / "job_0002.done").write_text("done")
        # Only .json files count
        assert _find_next_job_index(str(d)) == 1

    def test_ignores_non_job_json_files(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        (d / "config.json").write_text("{}")      # no "job_NNNN" pattern
        (d / "job_abc.json").write_text("{}")     # non-numeric suffix
        assert _find_next_job_index(str(d)) == 1

    def test_retry_log_ignored(self, tmp_path):
        # job_0000_retry.log must not be mistaken for a job JSON.
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        (d / "job_0000_retry.log").write_text("log")
        assert _find_next_job_index(str(d)) == 1

    def test_large_index(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_9999.json").write_text("{}")
        assert _find_next_job_index(str(d)) == 10000

    # ── Semantic tests: append numbering starts correctly ────────────────────

    def test_append_after_five_jobs_starts_at_five(self, tmp_path):
        """Simulates: first batch had 5 jobs; append should start at index 5."""
        d = tmp_path / "jobs"
        d.mkdir()
        for i in range(5):
            (d / f"job_{i:04d}.json").write_text(json.dumps({"name": f"job_{i}"}))
        next_idx = _find_next_job_index(str(d))
        assert next_idx == 5
        # New jobs should be job_0005.json, job_0006.json, ...
        first_new = f"job_{next_idx:04d}.json"
        assert first_new == "job_0005.json"


# ── TODO-28: append must re-list previously exported jobs in the .bat ─────────

class TestExistingJobsForBat:
    """_existing_jobs_for_bat reads prior jobs so APPEND can re-list them.

    Regression for TODO-28: APPEND rewrote run_smoke_batch.bat in "w" mode with
    only the new jobs, silently dropping every earlier job from the launcher.
    """
    def _write_job(self, d, idx, name, render_mode="CYCLES"):
        (d / f"job_{idx:04d}.json").write_text(
            json.dumps({"name": name, "render_mode": render_mode})
        )

    def test_missing_dir_returns_empty(self, tmp_path):
        assert _existing_jobs_for_bat(str(tmp_path / "none"), 5) == []

    def test_returns_jobs_below_start_index(self, tmp_path):
        d = tmp_path / "jobs"; d.mkdir()
        self._write_job(d, 0, "R64", "CYCLES")
        self._write_job(d, 1, "R128", "EEVEE")
        result = _existing_jobs_for_bat(str(d), 2)
        assert result == [(0, "R64", "CYCLES"), (1, "R128", "EEVEE")]

    def test_excludes_jobs_at_or_above_start_index(self, tmp_path):
        # Newly written jobs (idx >= start) must not be double-listed.
        d = tmp_path / "jobs"; d.mkdir()
        self._write_job(d, 0, "R64")
        self._write_job(d, 1, "R128")   # this is the first NEW job
        self._write_job(d, 2, "R256")
        result = _existing_jobs_for_bat(str(d), 1)
        assert [idx for idx, _, _ in result] == [0]

    def test_sorted_by_index(self, tmp_path):
        d = tmp_path / "jobs"; d.mkdir()
        for idx in (3, 0, 2, 1):
            self._write_job(d, idx, f"job{idx}")
        result = _existing_jobs_for_bat(str(d), 10)
        assert [idx for idx, _, _ in result] == [0, 1, 2, 3]

    def test_missing_fields_use_defaults(self, tmp_path):
        d = tmp_path / "jobs"; d.mkdir()
        (d / "job_0000.json").write_text("{}")           # no name / render_mode
        result = _existing_jobs_for_bat(str(d), 1)
        assert result == [(0, "job_0000", "CYCLES")]

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        d = tmp_path / "jobs"; d.mkdir()
        (d / "job_0000.json").write_text("{not valid json")
        result = _existing_jobs_for_bat(str(d), 1)
        assert result == [(0, "job_0000", "CYCLES")]


class TestJobRunCmd:
    def test_uses_launcher_when_present(self):
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "CYCLES", launcher_exists=True)
        assert cmd == '"py.exe" "L.py" "b.exe" "j.json"'

    def test_eevee_fallback_windowed(self):
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "EEVEE", launcher_exists=False)
        assert "--window-geometry" in cmd
        assert "--background" not in cmd
        assert '"W.py"' in cmd

    def test_cycles_fallback_background(self):
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "CYCLES", launcher_exists=False)
        assert "--background" in cmd
        assert "2>nul" in cmd


class TestJobBatBlock:
    def test_block_structure(self):
        block = _job_bat_block(3, 10, "R128", '"run" "cmd"',
                               r"C:\out\jobs\job_0002.done")
        assert block[0] == "echo === Job 3/10: R128 ==="
        assert block[1] == '"run" "cmd"'
        # error branch increments ERRORS and writes an error .done sentinel
        assert any("set /a ERRORS+=1" in ln for ln in block)
        assert any('echo error exit' in ln and "job_0002.done" in ln for ln in block)
        # success branch writes a plain done sentinel
        assert any(ln.strip().startswith("echo done R128") for ln in block)

    def test_label_drives_header_and_counter(self):
        # Bake-phase blocks: header "Bake i/N", counter BAKE_ERRORS.
        bk = _job_bat_block(1, 3, "R64", '"x"',
                            r"C:\out\jobs\job_0000.bake.done", label="Bake")
        assert bk[0] == "echo === Bake 1/3: R64 ==="
        assert any("set /a BAKE_ERRORS+=1" in ln for ln in bk)
        assert any("job_0000.bake.done" in ln for ln in bk)
        # Render-phase blocks: counter RENDER_ERRORS.
        rd = _job_bat_block(2, 3, "R64", '"x"',
                            r"C:\out\jobs\job_0001.render.done", label="Render")
        assert any("set /a RENDER_ERRORS+=1" in ln for ln in rd)


class TestJobRunCmdPhase:
    """Phase param drives the launcher --phase arg + EEVEE windowed decision."""
    def test_both_phase_omits_argument(self):
        # Default phase preserves the original single-pass invocation.
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "EEVEE", launcher_exists=True)
        assert "--phase" not in cmd

    def test_bake_phase_appends_arg_via_launcher(self):
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "EEVEE", launcher_exists=True, phase="bake")
        assert cmd.endswith(" --phase bake")

    def test_bake_phase_forces_background_in_fallback(self):
        # No launcher + EEVEE + bake phase: must NOT use --window-geometry.
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "EEVEE", launcher_exists=False, phase="bake")
        assert "--window-geometry" not in cmd
        assert "--background" in cmd
        assert " --phase bake" in cmd

    def test_render_phase_keeps_window_for_eevee_fallback(self):
        cmd = _job_run_cmd("py.exe", "L.py", "W.py", "b.exe", "f.blend",
                           "j.json", "EEVEE", launcher_exists=False, phase="render")
        assert "--window-geometry" in cmd
        assert " --phase render" in cmd


class TestJobBatBlockAlias:
    """alias_done_path is the two-pass pipeline's mechanism for letting the
    legacy `<stem>.done` poll/summary code see a job as complete after the
    FINAL pass (render, or bake in bake-only mode)."""
    def test_alias_omitted_by_default(self):
        block = _job_bat_block(1, 1, "n", '"c"', r"a.bake.done", label="Bake")
        assert not any("a.bake.done" not in ln and ".done" in ln for ln in block
                       if ln.strip().startswith(("echo error", "echo done")))

    def test_alias_writes_both_paths_in_both_branches(self):
        block = _job_bat_block(1, 1, "R64", '"c"', r"jobs/job_0000.render.done",
                               label="Render", alias_done_path=r"jobs/job_0000.done")
        err_lines = [ln for ln in block if "echo error exit" in ln]
        ok_lines  = [ln for ln in block if "echo done" in ln and "WARNING" not in ln]
        assert len(err_lines) == 2  # one phased, one alias
        assert len(ok_lines)  == 2
        assert any("job_0000.render.done" in ln for ln in err_lines)
        assert any("job_0000.done" in ln and ".render.done" not in ln for ln in err_lines)
        assert any("job_0000.render.done" in ln for ln in ok_lines)
        assert any("job_0000.done" in ln and ".render.done" not in ln for ln in ok_lines)


class TestPhasedSentinelRegexes:
    """The unphased sentinel matchers must EXCLUDE the per-phase variants so the
    poll/summary don't over-count completed jobs in the two-pass pipeline."""
    def test_unphased_done_matches(self):
        from SmokeSimLab import _DONE_RE, _RETRY_DONE_RE, _CRASHED_RE
        assert _DONE_RE.match("job_0000.done")
        assert _RETRY_DONE_RE.match("job_0007_retry.done")
        assert _CRASHED_RE.match("job_0003.crashed")

    def test_phased_done_does_NOT_match_unphased(self):
        from SmokeSimLab import _DONE_RE, _RETRY_DONE_RE, _CRASHED_RE
        for f in ("job_0000.bake.done", "job_0000.render.done"):
            assert not _DONE_RE.match(f), f"{f} must not match _DONE_RE"
            assert not _RETRY_DONE_RE.match(f), f"{f} must not match _RETRY_DONE_RE"
        for f in ("job_0000.bake.crashed", "job_0000.render.crashed"):
            assert not _CRASHED_RE.match(f), f"{f} must not match _CRASHED_RE"

    def test_phased_done_matchers(self):
        from SmokeSimLab import _BAKE_DONE_RE, _RENDER_DONE_RE
        assert _BAKE_DONE_RE.match("job_0000.bake.done")
        assert _RENDER_DONE_RE.match("job_0042.render.done")
        # And don't cross-match
        assert not _BAKE_DONE_RE.match("job_0000.render.done")
        assert not _RENDER_DONE_RE.match("job_0000.bake.done")
        assert not _BAKE_DONE_RE.match("job_0000.done")
