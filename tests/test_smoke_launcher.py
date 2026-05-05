"""Tests for smoke_launcher helper functions."""
import datetime
import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab"))

from smoke_launcher import _find_werfault_for_pid, _save_crash_log, _write_crashed_marker


# ---------------------------------------------------------------------------
# _save_crash_log
# ---------------------------------------------------------------------------

class TestSaveCrashLog:
    def test_copies_crash_file(self, tmp_path, monkeypatch):
        """Crash log is copied with a timestamped name."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        crash_src = fake_temp / "blender.crash.txt"
        crash_src.write_text("Stack trace line 1\nStack trace line 2\n")

        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        _save_crash_log(str(jobs_dir), "job_0000")

        crash_files = list(jobs_dir.glob("job_0000_crash_*.txt"))
        assert len(crash_files) == 1
        assert crash_files[0].read_text() == "Stack trace line 1\nStack trace line 2\n"

    def test_filename_contains_timestamp(self, tmp_path, monkeypatch):
        """Saved filename contains a YYYYMMDD_HHMMSS timestamp."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        (fake_temp / "blender.crash.txt").write_text("crash")
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        before = datetime.datetime.now().strftime("%Y%m%d")
        _save_crash_log(str(jobs_dir), "job_0001")

        crash_files = list(jobs_dir.glob("job_0001_crash_*.txt"))
        assert len(crash_files) == 1
        assert before in crash_files[0].name

    def test_no_crash_file_does_not_raise(self, tmp_path, monkeypatch):
        """If blender.crash.txt does not exist the function returns silently."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        _save_crash_log(str(jobs_dir), "job_0002")  # must not raise
        assert list(jobs_dir.glob("*.txt")) == []

    def test_multiple_crashes_produce_separate_files(self, tmp_path, monkeypatch):
        """Each call produces a uniquely-named file (different timestamps)."""
        fake_temp = tmp_path / "TEMP"
        fake_temp.mkdir()
        crash_src = fake_temp / "blender.crash.txt"
        crash_src.write_text("crash A")
        monkeypatch.setenv("TEMP", str(fake_temp))

        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()

        import time as _time
        _save_crash_log(str(jobs_dir), "job_0000")
        _time.sleep(1.1)  # ensure different second → different filename
        crash_src.write_text("crash B")
        _save_crash_log(str(jobs_dir), "job_0000")

        crash_files = sorted(jobs_dir.glob("job_0000_crash_*.txt"))
        assert len(crash_files) == 2
        assert crash_files[0].read_text() == "crash A"
        assert crash_files[1].read_text() == "crash B"


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
