"""Version stamping in logs/data files (for later cross-version comparison)."""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab"))

import SmokeSimLab as ssl


def _worker_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab", "smoke_worker.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _launcher_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "SmokeSimLab", "smoke_launcher.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class TestAddonVersionConstant:
    def test_matches_bl_info(self):
        expected = ".".join(str(v) for v in ssl.bl_info["version"])
        assert ssl.ADDON_VERSION == expected


class TestEstimLogStamp:
    def test_estim_log_adds_addon_version(self, tmp_path):
        ssl._estim["output_path"] = str(tmp_path)
        try:
            ssl._estim_log({"event": "unit-test"})
        finally:
            ssl._estim["output_path"] = ""
        line = (tmp_path / "estim_log.jsonl").read_text(encoding="utf-8").strip()
        rec = json.loads(line)
        assert rec["addon_version"] == ssl.ADDON_VERSION

    def test_estim_log_respects_explicit_version(self, tmp_path):
        # setdefault must not clobber a caller-supplied value
        ssl._estim["output_path"] = str(tmp_path)
        try:
            ssl._estim_log({"event": "x", "addon_version": "9.9.9"})
        finally:
            ssl._estim["output_path"] = ""
        rec = json.loads((tmp_path / "estim_log.jsonl").read_text(encoding="utf-8").strip())
        assert rec["addon_version"] == "9.9.9"


class TestExportStampsJobJson:
    def test_export_includes_addon_version(self):
        # export_batch needs a full bpy context; assert the JSON field is wired.
        import inspect
        src = inspect.getsource(ssl.export_batch)
        assert '"addon_version":  ADDON_VERSION' in src or '"addon_version": ADDON_VERSION' in src


class TestWorkerStamps:
    def test_reads_addon_version_from_cfg(self):
        assert 'addon_version      = cfg.get("addon_version"' in _worker_src()

    def test_perf_record_has_versions(self):
        src = _worker_src()
        assert '"addon_version": addon_version' in src
        assert '"worker_version": WORKER_VERSION' in src

    def test_results_csv_has_version_column(self):
        src = _worker_src()
        # header entry + row value (addon_version appended last)
        assert '"version",' in src
        assert "int(bake_seconds),\n        addon_version," in src


class TestLauncherCrashStamp:
    def test_crash_header_records_addon_version(self):
        src = _launcher_src()
        assert "_job_addon_version" in src
        # v0.6.3: brand renamed SmokeSimLab → BatchSimLab in user-visible
        # log strings (crash header is user-visible in crash_log.txt).
        assert "BatchSimLab addon {_job_addon_version}" in src
