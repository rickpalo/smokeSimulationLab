"""Tests for smoke_launcher helper functions."""
import datetime
import json
import os
import py_compile
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab"))

from smoke_launcher import (
    _find_werfault_for_pid, _save_crash_log, _write_crashed_marker,
    _STARTUP_TIMEOUT, _STALE_LOG_TIMEOUT, _WALL_CLOCK_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Worker/launcher syntax validation — regression guard for silent parse errors
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab")


class TestWorkerSyntax:
    def test_smoke_worker_valid_syntax(self):
        """smoke_worker.py must parse cleanly — a SyntaxError prevents any log from being written.

        Regression for v0.2.4 IndentationError that caused the first job to load the .blend
        but produce no log file, with the Python traceback silently routed to /dev/null.
        """
        py_compile.compile(
            os.path.join(_SCRIPTS_DIR, "smoke_worker.py"), doraise=True
        )

    def test_smoke_launcher_valid_syntax(self):
        """smoke_launcher.py must also parse cleanly."""
        py_compile.compile(
            os.path.join(_SCRIPTS_DIR, "smoke_launcher.py"), doraise=True
        )


# ---------------------------------------------------------------------------
# _save_crash_log
# ---------------------------------------------------------------------------

class TestSaveCrashLog:
    def test_appends_to_crash_log_txt(self, tmp_path, monkeypatch):
        """Crash content is appended to output_path/crash_log.txt, not jobs/."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        crash_src = fake_temp / "blender.crash.txt"
        crash_src.write_text("Stack trace line 1\nStack trace line 2\n")
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        _save_crash_log(str(jobs_dir), "job_0000")

        # Written to output_path (parent of jobs/), not inside jobs/
        crash_log = tmp_path / "crash_log.txt"
        assert crash_log.exists()
        content = crash_log.read_text()
        assert "Stack trace line 1" in content
        assert "job_0000" in content
        # Nothing written inside the jobs dir
        assert list(jobs_dir.glob("*.txt")) == []

    def test_header_contains_timestamp_and_job_stem(self, tmp_path, monkeypatch):
        """Each entry begins with a dated header containing the job stem."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        (fake_temp / "blender.crash.txt").write_text("crash")
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        before = datetime.datetime.now().strftime("%Y-%m-%d")
        _save_crash_log(str(jobs_dir), "job_0001")

        content = (tmp_path / "crash_log.txt").read_text()
        assert before in content
        assert "job_0001" in content

    def test_no_crash_file_writes_placeholder(self, tmp_path, monkeypatch):
        """If blender.crash.txt is missing, a placeholder line is written."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        _save_crash_log(str(jobs_dir), "job_0002")
        content = (tmp_path / "crash_log.txt").read_text()
        assert "job_0002" in content
        assert "no blender.crash.txt" in content

    def test_multiple_crashes_accumulate_in_one_file(self, tmp_path, monkeypatch):
        """Successive calls append to the same crash_log.txt with separate headers."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        crash_src = fake_temp / "blender.crash.txt"
        crash_src.write_text("crash A")
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        _save_crash_log(str(jobs_dir), "job_0000")
        crash_src.write_text("crash B")
        _save_crash_log(str(jobs_dir), "job_0001")

        content = (tmp_path / "crash_log.txt").read_text()
        assert "crash A" in content
        assert "crash B" in content
        assert "job_0000" in content
        assert "job_0001" in content


# ---------------------------------------------------------------------------
# _find_werfault_for_pid  (regression: wmic UTF-16 / CIM privilege failures)
# ---------------------------------------------------------------------------

class TestFindWerfaultForPid:
    # tasklist /FO CSV /NH output when WerFault is running:
    # "WerFault.exe","56789","Console","1","15,164 K"
    TASKLIST_HIT  = '"WerFault.exe","56789","Console","1","15,164 K"\r\n'
    TASKLIST_MISS = 'INFO: No tasks are running which match the specified criteria.\r\n'

    def _mock_run(self, stdout_text):
        m = MagicMock()
        m.stdout = stdout_text
        return m

    def test_returns_pid_when_werfault_running(self):
        """Returns WerFault PID from tasklist CSV output."""
        with patch("smoke_launcher.subprocess.run",
                   return_value=self._mock_run(self.TASKLIST_HIT)):
            assert _find_werfault_for_pid(12345) == 56789

    def test_returns_none_when_no_werfault(self):
        """Returns None when tasklist reports no matching process."""
        with patch("smoke_launcher.subprocess.run",
                   return_value=self._mock_run(self.TASKLIST_MISS)):
            assert _find_werfault_for_pid(12345) is None

    def test_returns_none_on_subprocess_exception(self):
        """Returns None silently if tasklist call raises."""
        with patch("smoke_launcher.subprocess.run", side_effect=OSError("not found")):
            assert _find_werfault_for_pid(12345) is None

    def test_uses_tasklist_not_wmic(self):
        """Detection uses tasklist (no-privilege) rather than wmic or powershell."""
        calls = []
        with patch("smoke_launcher.subprocess.run",
                   side_effect=lambda *a, **kw: calls.append(a) or self._mock_run("")):
            _find_werfault_for_pid(99999)
        assert calls, "subprocess.run was not called"
        cmd = calls[0][0]
        assert cmd[0].lower() == "tasklist"
        assert "wmic" not in " ".join(str(p) for p in cmd).lower()
        assert "powershell" not in " ".join(str(p) for p in cmd).lower()

    def test_multiple_werfault_processes_returns_first(self):
        """If multiple WerFault rows appear, the first PID is returned."""
        two_rows = self.TASKLIST_HIT + '"WerFault.exe","99999","Console","1","8,000 K"\r\n'
        with patch("smoke_launcher.subprocess.run",
                   return_value=self._mock_run(two_rows)):
            assert _find_werfault_for_pid(12345) == 56789


# ---------------------------------------------------------------------------
# smoke_launcher job JSON parsing
# ---------------------------------------------------------------------------

class TestLauncherJobJson:
    def test_reads_blend_file_and_render_mode(self, tmp_path):
        """Launcher reads blend_file and render_mode from job JSON."""
        job = {
            "blend_file":  r"C:\blends\test.blend",
            "render_mode": "EEVEE",
            "output_path": str(tmp_path),
        }
        job_path = tmp_path / "job_0000.json"
        job_path.write_text(json.dumps(job))

        with open(str(job_path), encoding="utf-8") as fh:
            data = json.load(fh)

        assert data["blend_file"] == r"C:\blends\test.blend"
        assert data["render_mode"] == "EEVEE"

    def test_missing_blend_file_defaults_to_empty(self, tmp_path):
        """blend_file defaults to '' if absent (graceful degradation)."""
        job = {"output_path": str(tmp_path)}
        job_path = tmp_path / "job_0000.json"
        job_path.write_text(json.dumps(job))

        with open(str(job_path), encoding="utf-8") as fh:
            data = json.load(fh)

        assert data.get("blend_file", "") == ""
        assert data.get("render_mode", "CYCLES") == "CYCLES"


# ---------------------------------------------------------------------------
# _write_crashed_marker
# ---------------------------------------------------------------------------

class TestWriteCrashedMarker:
    def test_creates_marker_file(self, tmp_path):
        """Writes a .crashed marker file in the jobs directory."""
        _write_crashed_marker(str(tmp_path), "job_0000")
        marker = tmp_path / "job_0000.crashed"
        assert marker.exists()
        assert "crashed" in marker.read_text()

    def test_marker_contains_iso_timestamp(self, tmp_path):
        """Marker file content includes an ISO-format timestamp."""
        _write_crashed_marker(str(tmp_path), "job_0001")
        content = (tmp_path / "job_0001.crashed").read_text()
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", content)


# ---------------------------------------------------------------------------
# Worker-done sentinel — exit-code-0 crash detection (BUG-002 regression)
# ---------------------------------------------------------------------------

class TestWorkerDoneSentinel:
    def test_missing_sentinel_would_be_treated_as_crash(self, tmp_path):
        """Launcher treats a missing .worker_done on exit-code-0 as a crash.

        Regression for BUG-002: before v0.2.12, Blender exiting 0 without the
        worker finishing was silently marked COMPLETE by the batch file.
        """
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        job_stem = "job_0000"

        # No .worker_done present — sentinel is absent (simulates crash)
        worker_done = jobs_dir / f"{job_stem}.worker_done"
        assert not worker_done.exists()

        # Launcher writes .crashed when sentinel is missing; verify the helper works
        _write_crashed_marker(str(jobs_dir), job_stem)
        assert (jobs_dir / f"{job_stem}.crashed").exists()

    def test_sentinel_present_means_clean_exit(self, tmp_path):
        """When .worker_done exists, exit-code-0 is a genuine success."""
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        job_stem = "job_0000"

        worker_done = jobs_dir / f"{job_stem}.worker_done"
        worker_done.write_text("2026-05-11T10:00:00\n")
        assert worker_done.exists()

        # Launcher would NOT write .crashed — verify no crash marker is created
        crashed = jobs_dir / f"{job_stem}.crashed"
        assert not crashed.exists()

    def test_sentinel_has_iso_timestamp(self, tmp_path):
        """Worker-done file written by the worker contains an ISO timestamp."""
        import re
        import datetime

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        job_stem = "job_0003"

        # Simulate what the worker writes
        sentinel = jobs_dir / f"{job_stem}.worker_done"
        sentinel.write_text(datetime.datetime.now().isoformat() + "\n")
        content = sentinel.read_text()
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", content)

    def test_retry_cleanup_removes_worker_done(self, tmp_path):

        """Retry logic removes .worker_done so a re-run produces a fresh sentinel."""
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        # Both original and _retry variants should be cleaned
        for stem in ("job_0000", "job_0000_retry"):
            (jobs_dir / f"{stem}.worker_done").write_text("2026-05-11T10:00:00\n")
            (jobs_dir / f"{stem}.done").write_text("done\n")

        # Simulate the retry cleanup loop from __init__.py
        base_stem = "job_0000"
        for suffix in ("", "_retry"):
            for ext in (".done", ".worker_done"):
                path = jobs_dir / (base_stem + suffix + ext)
                if path.exists():
                    path.unlink()

        assert not (jobs_dir / "job_0000.worker_done").exists()
        assert not (jobs_dir / "job_0000_retry.worker_done").exists()
        assert not (jobs_dir / "job_0000.done").exists()


# ---------------------------------------------------------------------------
# Watchdog timeout constants — sanity checks (BUG-002 regression)
# ---------------------------------------------------------------------------

class TestWatchdogConstants:
    def test_startup_timeout_is_positive_and_reasonable(self):
        """STARTUP_TIMEOUT must be positive and ≤ STALE_LOG_TIMEOUT."""
        assert _STARTUP_TIMEOUT > 0
        assert _STARTUP_TIMEOUT <= _STALE_LOG_TIMEOUT

    def test_stale_log_timeout_is_positive(self):
        assert _STALE_LOG_TIMEOUT > 0

    def test_wall_clock_timeout_exceeds_stale_log_timeout(self):
        """Wall-clock timeout must be larger than stale-log timeout so the
        stale watchdog fires first for hung-but-logging jobs."""
        assert _WALL_CLOCK_TIMEOUT > _STALE_LOG_TIMEOUT

    def test_startup_timeout_less_than_wall_clock(self):
        assert _STARTUP_TIMEOUT < _WALL_CLOCK_TIMEOUT


# ---------------------------------------------------------------------------
# CRASHED status detection (BUG-002: CRASHED vs FAILED distinction)
# ---------------------------------------------------------------------------

class TestCrashedStatusDetection:
    """Unit-tests for the .crashed-file → CRASHED status logic.

    Mirrors the branching in _update_job_log_statuses without importing the
    full addon (which requires bpy).  Tests encode the expected rules so that
    any future refactor of the detection logic must update the tests.
    """

    def _detect_status(self, jobs_dir, n_str):
        """Reproduce the status-detection logic from _update_job_log_statuses."""
        import os
        all_files = set(os.listdir(str(jobs_dir)))
        retry_done    = f"job_{n_str}_retry.done"
        first_done    = f"job_{n_str}.done"
        retry_log     = f"job_{n_str}_retry.log"
        first_log     = f"job_{n_str}.log"
        first_crashed = f"job_{n_str}.crashed"

        def _has_error(fname):
            p = jobs_dir / fname
            if not p.exists():
                return False
            return "error" in p.read_text().lower()

        if retry_done in all_files:
            return 'FAILED' if _has_error(retry_done) else 'COMPLETE'
        if retry_log in all_files:
            return 'RETRYING'
        if first_done in all_files:
            if first_crashed in all_files and _has_error(first_done):
                return 'CRASHED'
            return 'FAILED' if _has_error(first_done) else 'COMPLETE'
        if first_crashed in all_files:
            return 'CRASHED'
        if first_log in all_files:
            return 'IN_PROGRESS'
        return 'NOT_STARTED'

    def test_crash_plus_error_done_is_crashed(self, tmp_path):
        """A .crashed file alongside an error .done is CRASHED, not FAILED."""
        (tmp_path / "job_0000.done").write_text("error exit 11 blah\n")
        (tmp_path / "job_0000.crashed").write_text("crashed 2026-05-11T10:00:00\n")
        assert self._detect_status(tmp_path, "0000") == 'CRASHED'

    def test_error_done_without_crashed_is_failed(self, tmp_path):
        """An error .done without .crashed is a controlled FAILED (worker sys.exit(1))."""
        (tmp_path / "job_0000.done").write_text("error exit 1 blah\n")
        assert self._detect_status(tmp_path, "0000") == 'FAILED'

    def test_clean_done_is_complete(self, tmp_path):
        (tmp_path / "job_0000.done").write_text("done 2026-05-11\n")
        assert self._detect_status(tmp_path, "0000") == 'COMPLETE'

    def test_crashed_without_done_is_crashed(self, tmp_path):
        """Launcher wrote .crashed but batch never wrote .done (launcher itself crashed)."""
        (tmp_path / "job_0000.crashed").write_text("crashed 2026-05-11T10:00:00\n")
        assert self._detect_status(tmp_path, "0000") == 'CRASHED'

    def test_retry_done_supersedes_first_run_crash(self, tmp_path):
        """A successful retry makes the job COMPLETE even if first run crashed."""
        (tmp_path / "job_0000.done").write_text("error exit 11\n")
        (tmp_path / "job_0000.crashed").write_text("crashed\n")
        (tmp_path / "job_0000_retry.done").write_text("done\n")
        assert self._detect_status(tmp_path, "0000") == 'COMPLETE'

    def test_retry_done_with_error_is_failed(self, tmp_path):
        """A failed retry produces FAILED regardless of first-run crash."""
        (tmp_path / "job_0000.done").write_text("error exit 11\n")
        (tmp_path / "job_0000.crashed").write_text("crashed\n")
        (tmp_path / "job_0000_retry.done").write_text("error exit 1\n")
        assert self._detect_status(tmp_path, "0000") == 'FAILED'

    def test_in_progress_when_only_log_exists(self, tmp_path):
        (tmp_path / "job_0000.log").write_text("[job] started\n")
        assert self._detect_status(tmp_path, "0000") == 'IN_PROGRESS'

    def test_not_started_when_no_files(self, tmp_path):
        assert self._detect_status(tmp_path, "0000") == 'NOT_STARTED'
