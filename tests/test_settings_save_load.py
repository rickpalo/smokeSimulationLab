"""Tests for _settings_dict, _apply_settings_dict, _is_settings_dirty, _load_settings_from_path."""
import sys, os, json, types
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import (
    _settings_dict, _apply_settings_dict, _is_settings_dirty,
    _load_settings_from_path, _SWEEP_PARAMS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(value):
    return types.SimpleNamespace(value=float(value))


def _make_s(**overrides):
    """Return a minimal SmokeSettings stand-in with all required attributes."""
    d = {
        "iteration_mode":        "LIMITED",
        "use_dissolve":          False,
        "slow_dissolve":         False,
        "iterate_dissolve_both": False,
        "use_noise":             False,
        "iterate_noise_both":    False,
        "settings_file_path":   "",
        "settings_search_path": "",
        "settings_snapshot":    "",
    }
    defaults = {
        "resolution": 64, "vorticity": 0.0, "alpha": 1.0, "beta": 1.0,
        "dissolve_speed": 5, "noise_upres": 2, "noise_strength": 2.0,
        "noise_spatial_scale": 2.0,
        # v0.7.0 TODO-41: gas timing params
        "time_scale": 1.0, "cfl_number": 4.0,
        "timesteps_max": 4, "timesteps_min": 1,
        # v0.7.0 TODO-42: fire params (Blender defaults)
        "burning_rate": 0.75, "flame_smoke": 1.0, "flame_vorticity": 0.5,
        "flame_max_temp": 1.7, "flame_ignition": 1.5,
    }
    for name in _SWEEP_PARAMS:
        base = defaults[name]
        d[name + "_use_range"]     = False
        d[name + "_use_list"]      = False
        d[name + "_begin"]         = base
        d[name + "_end"]           = base
        d[name + "_step"]          = 0
        d[name + "_list"]          = []
    d.update(overrides)
    return types.SimpleNamespace(**d)


# ---------------------------------------------------------------------------
# _settings_dict
# ---------------------------------------------------------------------------

class TestSettingsDict:
    def test_returns_version(self):
        d = _settings_dict(_make_s())
        assert d["smokesettings_version"] == 2

    def test_top_level_flags(self):
        s = _make_s(use_dissolve=True, slow_dissolve=True, use_noise=True,
                    iteration_mode="ALL")
        d = _settings_dict(s)
        assert d["use_dissolve"]  is True
        assert d["slow_dissolve"] is True
        assert d["use_noise"]     is True
        assert d["iteration_mode"] == "ALL"

    def test_all_sweep_params_present(self):
        d = _settings_dict(_make_s())
        for name in _SWEEP_PARAMS:
            assert name in d["params"], f"missing param: {name}"

    def test_param_fields(self):
        d = _settings_dict(_make_s(resolution_begin=128))
        p = d["params"]["resolution"]
        assert "value" not in p          # removed in v2
        assert p["begin"]     == 128
        assert p["use_range"] is False
        assert p["use_list"]  is False
        assert p["list"]      == []

    def test_list_items_serialised(self):
        s = _make_s(resolution_use_list=True,
                    resolution_list=[_item(64), _item(128)])
        d = _settings_dict(s)
        assert d["params"]["resolution"]["list"] == [64.0, 128.0]

    def test_is_json_serialisable(self):
        d = _settings_dict(_make_s())
        json.dumps(d)  # must not raise


# ---------------------------------------------------------------------------
# _apply_settings_dict
# ---------------------------------------------------------------------------

class TestApplySettingsDict:
    def test_applies_top_level_flags(self):
        s = _make_s()
        data = {
            "iteration_mode": "ALL",
            "use_dissolve": True,
            "slow_dissolve": True,
            "use_noise": True,
            "params": {},
        }
        _apply_settings_dict(s, data)
        assert s.iteration_mode == "ALL"
        assert s.use_dissolve   is True
        assert s.slow_dissolve  is True
        assert s.use_noise      is True

    def test_applies_param_values(self):
        s = _make_s()

        class _Collection(list):
            def clear(self):
                self[:] = []
            def add(self):
                item = types.SimpleNamespace(value=0.0)
                self.append(item)
                return item

        s.resolution_list = _Collection()
        data = _settings_dict(_make_s(resolution_use_range=True,
                                      resolution_begin=64,
                                      resolution_end=256,
                                      resolution_step=64))
        data["params"]["resolution"]["list"] = [64.0, 128.0]
        data["params"]["resolution"]["use_list"] = True
        _apply_settings_dict(s, data)
        assert s.resolution_begin    == 64
        assert s.resolution_end      == 256
        assert s.resolution_step     == 64
        assert s.resolution_use_list is True
        assert [item.value for item in s.resolution_list] == [64.0, 128.0]

    def test_updates_snapshot(self):
        s = _make_s()
        data = _settings_dict(_make_s(resolution=256))
        _apply_settings_dict(s, data)
        assert s.settings_snapshot != ""
        # snapshot matches re-computed dict
        assert s.settings_snapshot == json.dumps(_settings_dict(s), sort_keys=True)

    def test_missing_param_leaves_begin_unchanged(self):
        s = _make_s(resolution_begin=128)
        data = {"params": {}, "iteration_mode": "LIMITED",
                "use_dissolve": False, "slow_dissolve": False, "use_noise": False}
        _apply_settings_dict(s, data)
        assert s.resolution_begin == 128  # unchanged


# ---------------------------------------------------------------------------
# _is_settings_dirty
# ---------------------------------------------------------------------------

class TestIsSettingsDirty:
    def test_no_file_path_never_dirty(self):
        s = _make_s(settings_file_path="")
        assert _is_settings_dirty(s) is False

    def test_file_path_but_empty_snapshot_is_dirty(self):
        s = _make_s(settings_file_path="/some/file.smokesettings",
                    settings_snapshot="")
        assert _is_settings_dirty(s) is True

    def test_matching_snapshot_not_dirty(self):
        s = _make_s(settings_file_path="/some/file.smokesettings")
        s.settings_snapshot = json.dumps(_settings_dict(s), sort_keys=True)
        assert _is_settings_dirty(s) is False

    def test_changed_begin_is_dirty(self):
        s = _make_s(settings_file_path="/some/file.smokesettings")
        s.settings_snapshot = json.dumps(_settings_dict(s), sort_keys=True)
        s.resolution_begin = 256   # change after snapshot
        assert _is_settings_dirty(s) is True


# ---------------------------------------------------------------------------
# _load_settings_from_path
# ---------------------------------------------------------------------------

class TestLoadSettingsFromPath:
    def test_load_writes_file_path(self, tmp_path):
        s = _make_s()
        data = _settings_dict(_make_s(resolution=128))
        path = tmp_path / "test.smokesettings"
        path.write_text(json.dumps(data), encoding="utf-8")
        _load_settings_from_path(s, str(path))
        assert s.settings_file_path == str(path)

    def test_load_writes_search_path(self, tmp_path):
        s = _make_s()
        data = _settings_dict(_make_s())
        path = tmp_path / "test.smokesettings"
        path.write_text(json.dumps(data), encoding="utf-8")
        _load_settings_from_path(s, str(path))
        assert s.settings_search_path == str(tmp_path)

    def test_load_applies_values(self, tmp_path):
        s = _make_s()
        data = _settings_dict(_make_s(resolution_begin=512, use_dissolve=True))
        path = tmp_path / "test.smokesettings"
        path.write_text(json.dumps(data), encoding="utf-8")
        _load_settings_from_path(s, str(path))
        assert s.resolution_begin == 512
        assert s.use_dissolve     is True

    def test_missing_file_does_not_raise(self):
        s = _make_s()
        _load_settings_from_path(s, "/nonexistent/path.smokesettings")
        assert s.settings_file_path == ""  # unchanged

    def test_invalid_json_does_not_raise(self, tmp_path):
        s = _make_s()
        path = tmp_path / "bad.smokesettings"
        path.write_text("not json", encoding="utf-8")
        _load_settings_from_path(s, str(path))
        assert s.settings_file_path == ""  # unchanged


# ---------------------------------------------------------------------------
# iterate_dissolve_both / iterate_noise_both round-trip
# ---------------------------------------------------------------------------

class TestIterateBothRoundtrip:
    def test_iterate_dissolve_both_serialised(self):
        d = _settings_dict(_make_s(iterate_dissolve_both=True))
        assert d["iterate_dissolve_both"] is True

    def test_iterate_noise_both_serialised(self):
        d = _settings_dict(_make_s(iterate_noise_both=True))
        assert d["iterate_noise_both"] is True

    def test_defaults_are_false(self):
        d = _settings_dict(_make_s())
        assert d["iterate_dissolve_both"] is False
        assert d["iterate_noise_both"]    is False

    def test_apply_restores_iterate_flags(self):
        s    = _make_s()
        data = _settings_dict(_make_s(iterate_dissolve_both=True, iterate_noise_both=True))
        _apply_settings_dict(s, data)
        assert s.iterate_dissolve_both is True
        assert s.iterate_noise_both    is True

    def test_apply_missing_flags_defaults_false(self):
        # Older preset files without these keys should apply safely.
        s    = _make_s(iterate_dissolve_both=True)
        data = {"iteration_mode": "LIMITED", "use_dissolve": False,
                "slow_dissolve": False, "use_noise": False, "params": {}}
        _apply_settings_dict(s, data)
        assert s.iterate_dissolve_both is False
