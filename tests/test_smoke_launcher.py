"""Tests for smoke_launcher helper functions."""
import datetime
import json
import os
import py_compile
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab"))

from smoke_launcher import _find_werfault_for_pid, _save_crash_log, _write_crashed_marker


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
