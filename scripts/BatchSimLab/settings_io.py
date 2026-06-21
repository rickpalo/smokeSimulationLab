"""
settings_io.py — .smokesettings preset save/load for BatchSimLab (TODO-58 module #3).

Extracted from ``__init__.py`` as the third module split.  This is the
settings-snapshot layer: serialise the Simulation Parameter sweep settings to a
JSON-serialisable dict, apply such a dict back onto a ``SmokeSettings`` group,
read/write ``.smokesettings`` preset files on disk, and back the dynamic
"settings file" EnumProperty in the panel.

Unlike the first two modules this one is **not** fully ``bpy``-free: the two
EnumProperty callbacks (``_settings_files_enum_items`` / ``_on_settings_enum_update``)
call ``bpy.path.abspath`` to resolve the output path.  The pytest conftest stubs
``bpy.path.abspath`` so the suite still runs without Blender; the real-Blender
REGISTER smoke-test covers the live path.  It has no dependency on ``jobgen`` or
``emitters`` (near-leaf).

⚠ Registration-order note: ``_settings_files_enum_items`` and
``_on_settings_enum_update`` are referenced at CLASS-BODY level in the
``SmokeSettings`` PropertyGroup (``items=…``/``update=…``), so the package
``__init__`` must re-import them ABOVE the class definitions.

Contents:
  * ``_SWEEP_PARAMS`` — the parameter names that participate in settings I/O.
  * Snapshot: ``_settings_dict`` / ``_apply_settings_dict`` / ``_is_settings_dirty``.
  * Disk: ``_load_settings_from_path``.
  * Preset dropdown: ``_SETTINGS_ENUM_SENTINEL`` / ``_settings_items_cache`` /
    ``_settings_files_enum_items`` / ``_on_settings_enum_update``.

``__init__.py`` re-imports every public name from here so existing call sites and
the TODO settings tests keep resolving against the package namespace.
"""
import bpy
import json
import os


_SWEEP_PARAMS = [
    "resolution", "vorticity", "alpha", "beta",
    "dissolve_speed", "noise_upres", "noise_strength", "noise_spatial_scale",
    # v0.7.0 TODO-41 + TODO-42 — gas timing + fire params
    "time_scale", "cfl_number", "timesteps_max", "timesteps_min",
    "burning_rate", "flame_smoke", "flame_vorticity", "flame_max_temp",
    "flame_ignition",
]


def _settings_dict(s):
    """Return a JSON-serialisable snapshot of all Simulation Parameter settings."""
    d = {
        "smokesettings_version": 2,
        "iteration_mode":        s.iteration_mode,
        "use_dissolve":          s.use_dissolve,
        "slow_dissolve":         s.slow_dissolve,
        "iterate_dissolve_both": getattr(s, "iterate_dissolve_both", False),
        "iterate_slow_dissolve": getattr(s, "iterate_slow_dissolve", False),
        "use_noise":             s.use_noise,
        "iterate_noise_both":    getattr(s, "iterate_noise_both", False),
        # v0.7.0 TODO-41 / TODO-42 master toggles
        "use_adaptive_timesteps": getattr(s, "use_adaptive_timesteps", True),
        "use_fire":               getattr(s, "use_fire", False),
        "params": {},
    }
    for name in _SWEEP_PARAMS:
        d["params"][name] = {
            "use_range": getattr(s, name + "_use_range"),
            "use_list":  getattr(s, name + "_use_list"),
            "begin":     getattr(s, name + "_begin"),
            "end":       getattr(s, name + "_end"),
            "step":      getattr(s, name + "_step"),
            "list":      [item.value for item in getattr(s, name + "_list")],
        }
    return d


def _apply_settings_dict(s, data):
    """Apply a settings snapshot dict to SmokeSettings *s*."""
    s.iteration_mode = data.get("iteration_mode", "LIMITED")
    s.use_dissolve   = data.get("use_dissolve",   False)
    s.slow_dissolve  = data.get("slow_dissolve",  False)
    if hasattr(s, "iterate_dissolve_both"):
        s.iterate_dissolve_both = data.get("iterate_dissolve_both", False)
    if hasattr(s, "iterate_slow_dissolve"):
        s.iterate_slow_dissolve = data.get("iterate_slow_dissolve", False)
    s.use_noise      = data.get("use_noise",       False)
    if hasattr(s, "iterate_noise_both"):
        s.iterate_noise_both = data.get("iterate_noise_both", False)
    # v0.7.0 TODO-41 / TODO-42 master toggles
    if hasattr(s, "use_adaptive_timesteps"):
        s.use_adaptive_timesteps = data.get("use_adaptive_timesteps", True)
    if hasattr(s, "use_fire"):
        s.use_fire = data.get("use_fire", False)
    params = data.get("params", {})
    for name in _SWEEP_PARAMS:
        if name not in params:
            continue
        p = params[name]
        # v1 presets stored a "value" key (the old base/default property).
        # Use it as a fallback for "begin"/"end" so old presets load correctly.
        v1_value = p.get("value")
        cur_begin = getattr(s, name + "_begin", 0)
        setattr(s, name + "_use_range",  p.get("use_range", False))
        setattr(s, name + "_use_list",   p.get("use_list",  False))
        setattr(s, name + "_begin",      p.get("begin", v1_value if v1_value is not None else cur_begin))
        setattr(s, name + "_end",        p.get("end",   v1_value if v1_value is not None else cur_begin))
        setattr(s, name + "_step",       p.get("step",  0))
        lst = getattr(s, name + "_list")
        lst.clear()
        for val in p.get("list", []):
            item = lst.add()
            item.value = val
    s.settings_snapshot = json.dumps(_settings_dict(s), sort_keys=True)


def _load_settings_from_path(s, path):
    """Load and apply a .smokesettings file; update tracking properties."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _apply_settings_dict(s, data)
        s.settings_file_path   = os.path.normpath(path)
        s.settings_search_path = os.path.dirname(os.path.normpath(path))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"[BatchSimLab] Failed to load settings from {path!r}: {exc}")


def _is_settings_dirty(s):
    """Return True if current settings differ from the last saved/loaded snapshot."""
    if not s.settings_file_path:
        return False
    snap = s.settings_snapshot
    if not snap:
        return True
    return json.dumps(_settings_dict(s), sort_keys=True) != snap


# Sentinel for "no preset selected". A non-empty identifier sidesteps a
# Blender quirk where assigning "" to a dynamic EnumProperty can hit the
# "enum \"\" not found in ()" TypeError even when the items list nominally
# contains a blank-id entry — the special handling of empty identifiers is
# inconsistent across operations. The display name stays blank so the UI
# still shows nothing in the dropdown when no preset is active.
_SETTINGS_ENUM_SENTINEL = "__none__"

# Module-level reference to the items list. Required: Blender's dynamic
# EnumProperty docs explicitly warn that Python must keep a reference to
# strings returned from the callback or Blender will crash / see ()
# instead of the actual items.
_settings_items_cache: list = [(_SETTINGS_ENUM_SENTINEL, "", "")]


def _settings_files_enum_items(self, _context):
    """EnumProperty items — list .smokesettings files in the preset search path.

    The identifier for each item is the filename stem (no extension, no path).
    This avoids issues with spaces, backslashes, or long Windows paths being
    used as Blender EnumProperty identifiers.
    """
    global _settings_items_cache
    folder = self.settings_search_path
    if not folder and self.output_path:
        folder = bpy.path.abspath(self.output_path)
    # First item: blank-display sentinel so the dropdown reads as empty
    # whenever no preset has been explicitly loaded/saved/selected.
    items = [(_SETTINGS_ENUM_SENTINEL, "", "")]
    if folder and os.path.isdir(folder):
        try:
            for fname in sorted(os.listdir(folder)):
                if fname.endswith(".smokesettings"):
                    stem = fname[: -len(".smokesettings")]
                    items.append((stem, stem, fname))
        except OSError:
            pass
    _settings_items_cache = items   # keep strings alive across the callback boundary
    return items


def _on_settings_enum_update(self, _context):
    """Update callback for settings_file_enum — auto-load when selection changes."""
    stem = self.settings_file_enum
    if not stem or stem == _SETTINGS_ENUM_SENTINEL:
        return
    folder = self.settings_search_path
    if not folder and self.output_path:
        folder = bpy.path.abspath(self.output_path)
    if not folder:
        return
    path = os.path.normpath(os.path.join(folder, stem + ".smokesettings"))
    # Guard: don't reload if this is already the active file (avoids a
    # redundant re-load when save/load operators set settings_file_enum).
    if path == self.settings_file_path:
        return
    _load_settings_from_path(self, path)
