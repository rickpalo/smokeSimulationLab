"""v0.7.3 — output-path default (no hard-coded machine paths) + TODO-51 step 1
(perf record carries the fields needed to fit samples/noise time estimates).
"""
import re
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab"))

import BatchSimLab as ssl


def _worker_src():
    p = os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab", "smoke_worker.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class TestOutputPathDefault:
    def _default(self):
        return ssl.SmokeSettings.__annotations__["output_path"][1].get("default")

    def test_default_is_empty_not_hardcoded(self):
        # Empty until a .blend loads; _reset_on_load then fills the blend folder.
        # NOT the literal "//" — a Python StringProperty rejects that token on
        # Blender 5.x ("does not support blend relative // prefix").
        assert self._default() == ""

    def test_no_hardcoded_drive_letter(self):
        # Guard against a machine-specific path (e.g. C:/tmp, D:\...) sneaking
        # back into the default.
        assert not re.match(r"^[A-Za-z]:[\\/]", self._default() or "")

    def test_reset_on_load_uses_resolved_blend_folder(self):
        # _reset_on_load must drop the hard-coded path AND avoid the literal
        # "//" token; it stores the resolved absolute path via the helper.
        import inspect
        src = inspect.getsource(ssl._reset_on_load)
        assert "s.output_path = _default_output_path()" in src
        assert "C:/tmp" not in src
        assert 's.output_path = "//"' not in src

    def test_default_output_path_resolves_not_token(self):
        # The helper must never return the literal "//" token.
        import inspect
        src = inspect.getsource(ssl._default_output_path)
        assert 'bpy.path.abspath("//")' in src   # resolves, not stores, the token
        # When unsaved (no filepath) it returns "" — exercised via the stub,
        # whose bpy.data.filepath is empty.
        assert ssl._default_output_path() == ""


class TestWorkerVersionBump:
    def test_worker_version_at_least_071(self):
        m = re.search(r'^WORKER_VERSION = "(\d+)\.(\d+)\.(\d+)"', _worker_src(), re.MULTILINE)
        assert m, "WORKER_VERSION constant missing"
        major, minor, patch = (int(g) for g in m.groups())
        assert (major, minor, patch) >= (0, 7, 1)

    def test_addon_expects_matching_worker(self):
        # The version gate compares the exported worker against this constant.
        # 0.9.1 = BUG-016 (per-job render_samples via taa_render_samples);
        # 0.9.2 = generic "Emitter"/"Emitter[i]" overlay labels stacked in the
        # Dissolve text (item-4 overlay change);
        # 0.9.3 = TODO-66 follow-up (negative Frame Start: cache-filename
        # regexes tolerate a sign, frame-count math no longer assumes
        # frame_start == 1).
        assert ssl._EXPECTED_WORKER_VERSION == "0.9.3"


class TestPerfRecordEstimationFields:
    """TODO-51: the perf record must carry samples + noise so analyze_estim can
    fit the new render/bake terms; without them the data can't be collected."""

    def test_perf_includes_render_samples(self):
        assert '"render_samples": render_samples,' in _worker_src()

    def test_perf_includes_noise_fields(self):
        src = _worker_src()
        assert '"use_noise":      bool(p["use_noise"]),' in src
        assert '"noise_upres":    int(p["noise_upres"]) if p["use_noise"] else None,' in src
