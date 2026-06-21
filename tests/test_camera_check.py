"""TODO-29: no-camera warning at Export Batch + Run Batch."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import BatchSimLab as ssl

from addon_src import read_addon_source


class _StubObj:
    def __init__(self, obj_type):
        self.type = obj_type


def _make_scene(types_):
    return types.SimpleNamespace(objects=[_StubObj(t) for t in types_])


class TestSceneHasCamera:
    def test_camera_present(self):
        scene = _make_scene(['CAMERA', 'MESH', 'LIGHT'])
        assert ssl._scene_has_camera(scene) is True

    def test_camera_absent(self):
        scene = _make_scene(['MESH', 'LIGHT', 'EMPTY'])
        assert ssl._scene_has_camera(scene) is False

    def test_empty_scene(self):
        scene = _make_scene([])
        assert ssl._scene_has_camera(scene) is False

    def test_multiple_cameras(self):
        scene = _make_scene(['CAMERA', 'CAMERA', 'MESH'])
        assert ssl._scene_has_camera(scene) is True

    def test_none_scene_falls_back_to_false(self):
        # Defensive: if .objects is missing entirely (None or bad type),
        # don't raise — just return False so the dialog won't fire.
        assert ssl._scene_has_camera(types.SimpleNamespace()) is False


class TestExportBatchWiring:
    """Source-level check: Export Batch's invoke caches both warning flags so
    draw() can compose a single dialog (resolution + camera) or fire either
    independently."""
    def _src(self):
        # TODO-58: SMOKE_OT_export_batch moved to operators.py — read the whole
        # addon package so the wiring assertions span the split modules.
        return read_addon_source()

    def test_invoke_sets_both_warning_flags(self):
        src = self._src()
        assert "self._warn_res" in src
        assert "self._warn_cam" in src
        assert "not _scene_has_camera(context.scene)" in src

    def test_dialog_fires_on_either_warning(self):
        assert "if self._warn_res or self._warn_cam:" in self._src()

    def test_draw_shows_camera_section_when_warned(self):
        src = self._src()
        assert 'getattr(self, "_warn_cam", False)' in src
        assert "No camera in scene — renders will be black/fail." in src


class TestRunBatchWiring:
    """Source-level check: Run Batch's invoke fires ONLY on the no-camera +
    render-on case (the save-before dialog removed in v0.4.1 stays gone)."""
    def _src(self):
        # TODO-58: run_batch stays in __init__ for now, but read the whole addon
        # package so this keeps working once it moves to operators.py too.
        return read_addon_source()

    def test_run_batch_invoke_checks_camera(self):
        src = self._src()
        assert "s.render_simulation_result and not _scene_has_camera(context.scene)" in src
