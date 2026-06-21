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


class TestPanelDrawUsesAddonVersion:
    """Regression: when installed as an *extension* (Blender 4.2+), Blender
    deletes `bl_info` from the module namespace after import.  Any runtime
    (draw-time) reference to `bl_info` then raises NameError, blanking the
    N-panel body.  draw() must use the import-time ADDON_VERSION constant."""

    def test_panel_draw_does_not_reference_bl_info(self):
        import inspect
        # Strip comment lines so the explanatory comment (which names bl_info)
        # doesn't trip the check — we only care about executable references.
        code = "\n".join(
            ln for ln in inspect.getsource(ssl.SMOKE_PT_panel.draw).splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "bl_info" not in code, (
            "SMOKE_PT_panel.draw references bl_info, which is removed by "
            "Blender at runtime for extensions; use ADDON_VERSION instead."
        )

    def test_panel_draw_uses_addon_version(self):
        import inspect
        src = inspect.getsource(ssl.SMOKE_PT_panel.draw)
        assert "ADDON_VERSION" in src

    def test_no_runtime_bl_info_reference_anywhere(self):
        """Class-wide guard: `bl_info` is removed from an extension module after
        import, so it may ONLY be referenced at module top level (its definition
        and the import-time ADDON_VERSION).  Any `bl_info` inside a function or
        method body is a latent NameError under extension installs.  Parse the
        AST so this catches a reintroduction in *any* operator/panel/handler,
        not just SMOKE_PT_panel.draw."""
        import ast
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "__init__.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id == "bl_info":
                    offenders.append(f"{fn.name}() line {node.lineno}")
        assert not offenders, (
            "bl_info referenced inside function/method bodies (removed at "
            "runtime for extensions — use ADDON_VERSION): " + ", ".join(offenders)
        )


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
