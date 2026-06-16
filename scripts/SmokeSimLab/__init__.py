"""
BatchSimLab/__init__.py
=======================
Blender 4.x / 5.x addon — Batch Sim Lab tab in the 3D Viewport N-panel.

BatchSimLab automates batch smoke simulation parameter sweeps (with fire +
fluid simulation planned for v2.0.0 / v3.0.0).  For each parameter
combination it bakes the Mantaflow fluid simulation, renders an EEVEE / Cycles
playblast animation (MP4), renders a final quality still frame (PNG), and
logs results to a CSV file for later comparison.

History note: this addon was originally branded "SmokeSimLab"; v0.6.3
rebranded it to "BatchSimLab" in preparation for the v2.0.0 (fire) and
v3.0.0 (fluid) roadmap.  Internal Python identifiers, folder names, and
operator IDs retain the "smoke*" prefix for backwards compatibility with
existing .blend saves and keymaps; the rename is surface-only.

Installation
------------
1. Zip the SmokeSimLab folder (containing __init__.py + smoke_worker.py +
   smoke_launcher.py).
2. In Blender: Edit → Preferences → Add-ons → Install → select the zip.
3. Enable "BatchSimLab" in the add-on list.

Workflow
--------
1. Set your fluid domain object and output directory in the Batch Sim Lab panel.
2. Configure parameter defaults and optional ranges/lists.
3. Choose iteration mode:
     • Limited Combinations — vary one parameter at a time, all others at default.
     • All Combinations    — full Cartesian product of all ranges (can be large).
4. Save your .blend file.
5. Click "Export Batch" — writes to <output_path>:
       run_smoke_batch.bat     Windows batch launcher
       smoke_worker.py         copy of the per-job worker script
       jobs/job_NNNN.json      one JSON config per parameter combination
6. Double-click run_smoke_batch.bat in Windows Explorer.
   Each job opens a fresh Blender instance, bakes, renders, writes CSV, exits.

Documentation
-------------
Full documentation: https://github.com/rickpalo/SmokeSimLab

Requires Blender 4.x (tested on 4.5.5 and 5.1.1) on Windows 10/11.  May work on other OSes but the batch export is
"""

# ---------------------------------------------------------------------------
# Blender addon metadata — required for proper addon registration.
# Blender reads bl_info to display the addon in Preferences → Add-ons.
# ---------------------------------------------------------------------------
bl_info = {
    "name":        "BatchSimLab",
    "author":      "Rick Palo",
    "version":     (0, 9, 0),
    "blender":     (4, 0, 0),
    "location":    "View3D > Sidebar > BatchLab",
    "description": "Batch smoke simulation parameter sweeper with CSV logging "
                   "(roadmap: fire @ v2.0.0, fluid @ v3.0.0)",
    "doc_url":     "https://github.com/rickpalo/SmokeSimLab",
    "tracker_url": "https://github.com/rickpalo/SmokeSimLab/issues",
    "category":    "Fluid Simulation",
}

import bpy
import copy
import math
import os
import re
import shutil
import itertools
import json
import subprocess
import sys
import time

ADDON_VERSION = ".".join(str(v) for v in bl_info["version"])
print(f"BatchSimLab {ADDON_VERSION} loaded")


# GitHub repo URL — repository slug stays "SmokeSimLab" for v0.6.3 surface
# rebrand (no folder/repo rename in this version).  Future repository
# migration could happen at v1.0.0 alongside the deeper internal rename.
DOCS_URL = "https://github.com/rickpalo/SmokeSimLab"

# Expected version strings in the helper files exported to the output folder.
# When Run Batch detects a mismatch it warns the user to re-run Export Batch.
# Keep these in sync with WORKER_VERSION / LAUNCHER_VERSION in those files.
_EXPECTED_WORKER_VERSION   = "0.9.0"
_EXPECTED_LAUNCHER_VERSION = "0.6.4"


def _read_helper_version(path: str, var_name: str) -> str:
    """Return the version string for var_name from the first 30 lines of path.

    Returns "" if the file is missing or the variable is not found.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 30:
                    break
                line = line.strip()
                if line.startswith(var_name + " ="):
                    m = re.search(r'["\']([^"\']+)["\']', line)
                    if m:
                        return m.group(1)
    except OSError:
        pass
    return ""

# All iterable parameter base names — used by _clear_lists and generate_jobs.
# Any parameter added here must also have corresponding properties in
# SmokeSettings (name, name_begin, name_end, name_step, name_use_range,
# name_use_list, name_list, name_index).
ITERABLE_PARAMS = [
    "resolution",
    "vorticity",
    "alpha",
    "beta",
    "dissolve_speed",
    "noise_upres",
    "noise_strength",
    "noise_spatial_scale",
    # v0.7.0 TODO-41: gas-side simulation timing parameters
    "time_scale",
    "cfl_number",
    "timesteps_max",
    "timesteps_min",
    # v0.7.0 TODO-42: Fire Parameters (only applied when use_fire is True)
    "burning_rate",
    "flame_smoke",
    "flame_vorticity",
    "flame_max_temp",
    "flame_ignition",
]

# Hard bounds for each iterable parameter.  Used as a reliable fallback when
# RNA hard_min/hard_max lookup fails.  None means no limit in that direction.
_PARAM_BOUNDS = {
    "resolution":          (8.0,  None),
    "vorticity":           (0.0,  None),
    "alpha":               (-5.0, 5.0),
    "beta":                (-5.0, 5.0),
    "dissolve_speed":      (0.0,  None),
    "noise_upres":         (1.0,  None),
    "noise_strength":      (0.0,  None),
    "noise_spatial_scale": (0.0,  None),
    # v0.7.0 TODO-41
    "time_scale":          (0.001, None),  # must be > 0 to avoid divide-by-zero
    "cfl_number":          (0.5,  10.0),
    "timesteps_max":       (1.0,  100.0),
    "timesteps_min":       (1.0,  100.0),
    # v0.7.0 TODO-42 (Blender's Fire settings — all non-negative floats)
    "burning_rate":        (0.01, 4.0),
    "flame_smoke":         (0.0,  8.0),
    "flame_vorticity":     (0.0,  2.0),
    "flame_max_temp":      (1.0,  10.0),
    "flame_ignition":      (0.5,  10.0),
}

# Default per-frame / per-stage timing estimates used before real data is available.
_SETUP_SECS_DEFAULT  =  10.0   # seconds for setup / cache phase
_STILL_SECS_DEFAULT  =  30.0   # seconds for final still frame

# Bake estimate: bake_secs ≈ _BAKE_RATE_PER_RES3_FRAME × resolution³ × frames
# Calibrated from perf_log.json (May-2026, 164 samples, res 32–512).  The
# implied rate is not constant across resolutions (cv=75%, see analyze_estim):
# res=128 baseline media is the dominant workload, so we use its median.  At
# very low resolutions (32) the per-cell rate is ~5× higher because per-job
# overhead dominates — known model limitation.
_BAKE_RATE_PER_RES3_FRAME = 1.8261e-07  # s / (res^3 * frame); median of 119 res=128 samples

# Render estimate: render_secs ≈ rate × width × height × frames
# CYCLES: no real data yet — kept as placeholder derived from 15 s/frame at 1920×1080.
# EEVEE:  calibrated from 167 samples (cv=27%, median 7.92e-07).
_RENDER_RATE_CYCLES_PER_PIXEL_FRAME = 7.23e-9    # s / (pixel * frame) — placeholder
_RENDER_RATE_EEVEE_PER_PIXEL_FRAME  = 7.9239e-07 # s / (pixel * frame); median of 167 EEVEE samples

# Legacy flat rates kept as fallback when resolution/dimensions are unknown.
_BAKE_RATE_DEFAULT   =   1.0  # s/frame at unspecified resolution
_RENDER_RATE_DEFAULT =  45.0   # s/frame at unspecified resolution


# ---------------------------------------------------------------------------
# Noise (up-res) bake ceiling
# ---------------------------------------------------------------------------
# The noise pass bakes a separate up-resolution grid whose edge length is
# (domain resolution × noise up-res factor).  At high effective resolutions
# Mantaflow's noise bake has been observed to either crash with an
# EXCEPTION_ACCESS_VIOLATION in tbbmalloc.dll or hang at "Baking 500 of 500"
# (the data pass finishes, the noise pass never returns).  Observed on a
# 128 GB / i9-13900 machine, Blender 5.1.1:
#   • 128×3 = 384³  — fine (many jobs completed)
#   • 256×2 = 512³  — fine
#   • 256×3 = 768³  — crashed (tbbmalloc) until re-exported + retried
#   • 256×4 = 1024³ — hung, killed by the launcher's stale-log watchdog
# It is NOT a hard limit: every config eventually completed after re-export and
# restart, so callers warn the user rather than block.  The edge threshold below
# sits just above the known-good 512³ case so it flags only the flaky zone.
_NOISE_UPRES_EDGE_WARN = 512   # warn when (resolution × noise_upres) exceeds this


def noise_grid_edge(resolution, use_noise, noise_upres):
    """Effective edge length of the noise up-res grid = resolution × up-res factor.

    Returns 0 when noise is disabled, since no separate noise grid is baked then.
    """
    if not use_noise:
        return 0
    return int(resolution) * int(noise_upres)


def noise_grid_exceeds_ceiling(resolution, use_noise, noise_upres):
    """True when the noise up-res grid is large enough to risk a crash or hang.

    See _NOISE_UPRES_EDGE_WARN for the empirical basis.  This is advisory only —
    the bake may still succeed — so the UI warns and lets the user continue.
    """
    return noise_grid_edge(resolution, use_noise, noise_upres) > _NOISE_UPRES_EDGE_WARN


# ---------------------------------------------------------------------------
# Toggle helpers
# ---------------------------------------------------------------------------

def make_toggle_range(name):
    """
    Return an update callback for <name>_use_range BoolProperty.
    When 'use_range' is enabled it automatically disables 'use_list' so
    only one input mode is active at a time.
    """
    def update(self, context):
        if getattr(self, name + "_use_range"):
            setattr(self, name + "_use_list", False)
    return update


def make_toggle_list(name):
    """
    Return an update callback for <name>_use_list BoolProperty.
    When 'use_list' is enabled it automatically disables 'use_range' so
    only one input mode is active at a time.
    """
    def update(self, context):
        if getattr(self, name + "_use_list"):
            setattr(self, name + "_use_range", False)
    return update


def _sync_frame_defaults(self, context):
    """Update callback for use_default_frames — copies scene range on uncheck."""
    if not self.use_default_frames and context and context.scene:
        self.sim_frame_start = context.scene.frame_start
        self.sim_frame_end   = context.scene.frame_end


# v0.7.0 TODO-40: when the user picks a fluid domain object in the addon
# panel, copy the domain's CURRENT settings into the addon's `_begin`
# (baseline) values + master toggles.  Only baseline values are touched —
# any `_end` / `_step` / `_use_range` / `_use_list` sweep configuration the
# user has already set up is preserved, so re-selecting a domain doesn't
# wipe an in-progress sweep design.
#
# Each property read is wrapped in try/except so older Blender builds or
# liquid-domain objects (missing fire/noise/dissolve attrs) don't crash
# the callback — the missing properties are just silently skipped.
#
# The Blender attribute names don't always match our addon naming:
#   d.cfl_condition           → s.cfl_number_begin
#   d.use_dissolve_smoke      → s.use_dissolve  (master toggle)
#   d.use_dissolve_smoke_log  → s.slow_dissolve (master toggle)
#   d.noise_scale             → s.noise_upres_begin
#   d.noise_pos_scale         → s.noise_spatial_scale_begin
# All others map by direct name with `_begin` suffix.
_DOMAIN_IMPORT_MAP = (
    # (Blender domain attr, addon param name).  None for the addon name
    # means the attr maps to a master toggle (handled separately).
    ("resolution_max",        "resolution"),
    ("vorticity",             "vorticity"),
    ("alpha",                 "alpha"),
    ("beta",                  "beta"),
    ("dissolve_speed",        "dissolve_speed"),
    ("noise_scale",           "noise_upres"),
    ("noise_strength",        "noise_strength"),
    ("noise_pos_scale",       "noise_spatial_scale"),
    # v0.7.0 TODO-41 gas timing
    ("time_scale",            "time_scale"),
    ("cfl_condition",         "cfl_number"),
    ("timesteps_max",         "timesteps_max"),
    ("timesteps_min",         "timesteps_min"),
    # v0.7.0 TODO-42 fire
    ("burning_rate",          "burning_rate"),
    ("flame_smoke",           "flame_smoke"),
    ("flame_vorticity",       "flame_vorticity"),
    ("flame_max_temp",        "flame_max_temp"),
    ("flame_ignition",        "flame_ignition"),
)


def _import_domain_params(self, context):
    """PointerProperty update callback for `domain_obj`.

    When the user picks a new fluid domain object, copy its current
    FluidDomainSettings into the addon's `_begin` (baseline) values and
    master toggles.  Sweep config (`_end` / `_step` / `_use_range` /
    `_use_list` / `_list`) is left untouched so an in-progress sweep
    design isn't blown away by re-selecting the same domain.

    No-op when the new selection is None, has no Fluid modifier, or has
    a non-DOMAIN fluid_type (e.g. an emitter/flow object).
    """
    obj = self.domain_obj
    if obj is None:
        return
    mod = next((m for m in obj.modifiers if m.type == 'FLUID'), None)
    if mod is None or mod.fluid_type != 'DOMAIN':
        return
    d = mod.domain_settings
    if d is None:
        return

    # Direct mapping: copy each domain attr to s.<addon_name>_begin.
    for _battr, _paddon in _DOMAIN_IMPORT_MAP:
        try:
            _val = getattr(d, _battr)
        except AttributeError:
            continue  # property absent in this Blender version
        # The addon's _begin properties are Int / Float depending on the
        # param; pass-through works because RNA does the coercion.
        try:
            setattr(self, _paddon + "_begin", _val)
        except (AttributeError, TypeError):
            continue

    # Master toggles (separately because addon attr names differ from
    # the Blender attr names).
    for _battr, _paddon in (
        ("use_dissolve_smoke",     "use_dissolve"),
        ("use_dissolve_smoke_log", "slow_dissolve"),
        ("use_noise",              "use_noise"),
        ("use_adaptive_timesteps", "use_adaptive_timesteps"),
    ):
        try:
            setattr(self, _paddon, bool(getattr(d, _battr)))
        except (AttributeError, TypeError):
            continue

    # use_fire is an addon-side override flag, not a Blender domain
    # attribute (fire is driven by flow_type on flow objects).  Probe
    # whether the domain has any fire characteristics: a non-default
    # burning_rate or flame_ignition value suggests the user intends to
    # use fire — flip use_fire on so the imported fire values get applied
    # at bake time.  False positives here are harmless (user can uncheck).
    try:
        self.use_fire = (
            float(getattr(d, "burning_rate", 0.75)) != 0.75
            or float(getattr(d, "flame_ignition", 1.5)) != 1.5
        )
    except (AttributeError, TypeError):
        pass

    # v0.9.0 TODO-55: refresh the per-emitter sections for the new domain.
    # Update callbacks must never raise, so guard broadly.
    try:
        scene = getattr(context, "scene", None) if context else None
        if scene is not None:
            _populate_emitters(self, scene)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Settings save/load — helper functions
# ---------------------------------------------------------------------------

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


def _on_render_sim_result_update(self, _context):
    """Update callback for render_simulation_result (TODO-26).

    A bake-only run produces no renders, so "Display Results When Finished" has
    nothing to display — clear it when rendering is turned off.  Writing the
    property here (rather than in draw()) keeps the value mutation out of the
    draw pass, where RNA writes are unsafe.
    """
    if not self.render_simulation_result:
        self.show_results = False


# ---------------------------------------------------------------------------
# Parameter expansion
# ---------------------------------------------------------------------------

def expand_param(s, name):
    """
    Return a list of values for iterable parameter *name* from SmokeSettings *s*.

    In all three modes the Begin field is the authoritative single value:

      1. Explicit list  — user-entered values in the UIList; falls back to
                          [begin] when the list is empty.
      2. Range          — begin/end/step sweep; step=0 returns [begin].
      3. Single value   — [begin] (Begin field shown as "Value" in the UI).

    Float ranges use a small epsilon tolerance on the end boundary to
    avoid off-by-one errors caused by IEEE 754 floating point accumulation
    (e.g. 0.2 * 5 = 1.0000000000000002 in Python, which would incorrectly
    fail the <= 1.0 check without the epsilon).

    Parameters
    ----------
    s    : SmokeSettings — the scene's smoke_settings property group
    name : str           — base parameter name, e.g. "vorticity"

    Returns
    -------
    list of float/int values to iterate over
    """
    # v0.7.0 defensive: real SmokeSettings always has every sweep param's
    # _begin attribute, but test SimpleNamespace fixtures may not enumerate
    # every property (especially after adding TODO-41/42 params).  Return a
    # single-element sentinel so callers' `[0]` index never crashes and so
    # _default_job stays robust when called from older fixtures.
    if not hasattr(s, name + "_begin"):
        return [0]
    begin = getattr(s, name + "_begin")

    # Mode 1: explicit list
    if getattr(s, name + "_use_list"):
        lst  = getattr(s, name + "_list")
        vals = [i.value for i in lst]
        return vals if vals else [begin]

    # Mode 2: range sweep
    if getattr(s, name + "_use_range"):
        end   = getattr(s, name + "_end")
        step  = getattr(s, name + "_step")
        if step == 0 or end == begin:
            return [begin]
        # v0.5.2: derive step sign from begin/end so a descending sweep
        # (begin > end) works without requiring the user to type a negative
        # step.  Previously the while-loop's `v <= end + epsilon` condition
        # was immediately false for descending ranges, returning [] and
        # crashing _default_job's `[0]` index — which silently aborted the
        # rest of the panel's draw callback.
        step_abs = abs(step)
        if end >= begin:
            stepped, cmp = step_abs, lambda v: v <= end + step_abs * 1e-6
        else:
            stepped, cmp = -step_abs, lambda v: v >= end - step_abs * 1e-6
        vals, v = [], begin
        while cmp(v):
            vals.append(round(v, 6))  # round to avoid 0.200000000001 in names
            v += stepped
        # Defensive: even after the direction fix, an exotic input (e.g.
        # NaN end) could yield an empty list.  Always return at least
        # [begin] so callers' `[0]` indexing never crashes the UI.
        return vals if vals else [begin]

    # Mode 3: single value — Begin field doubles as the fixed value
    return [begin]


def _default_job(s):
    """
    Return a job-parameter dict using the effective default value for every
    parameter.  Used as the baseline in Limited Combinations mode.

    Uses expand_param()[0] rather than the raw base property so that a
    single-point range (begin=128, step=0) or a single-item list is
    honoured — the user's chosen value becomes the baseline for all other
    parameter sweeps rather than falling back to the raw default.
    """
    return {
        "resolution":          expand_param(s, "resolution")[0],
        "vorticity":           expand_param(s, "vorticity")[0],
        "alpha":               expand_param(s, "alpha")[0],
        "beta":                expand_param(s, "beta")[0],
        "dissolve_speed":      expand_param(s, "dissolve_speed")[0],
        "noise_upres":         expand_param(s, "noise_upres")[0],
        "noise_strength":      expand_param(s, "noise_strength")[0],
        "noise_spatial_scale": expand_param(s, "noise_spatial_scale")[0],
        "use_dissolve":        s.use_dissolve,
        "slow_dissolve":       s.slow_dissolve,
        "use_noise":           s.use_noise,
        # v0.7.0 TODO-41: gas timing parameters
        "time_scale":          expand_param(s, "time_scale")[0],
        "use_adaptive_timesteps": getattr(s, "use_adaptive_timesteps", True),
        "cfl_number":          expand_param(s, "cfl_number")[0],
        "timesteps_max":       expand_param(s, "timesteps_max")[0],
        "timesteps_min":       expand_param(s, "timesteps_min")[0],
        # v0.7.0 TODO-42: fire parameters
        "use_fire":            getattr(s, "use_fire", False),
        "burning_rate":        expand_param(s, "burning_rate")[0],
        "flame_smoke":         expand_param(s, "flame_smoke")[0],
        "flame_vorticity":     expand_param(s, "flame_vorticity")[0],
        "flame_max_temp":      expand_param(s, "flame_max_temp")[0],
        "flame_ignition":      expand_param(s, "flame_ignition")[0],
    }


# ---------------------------------------------------------------------------
# Job generation — two modes
# ---------------------------------------------------------------------------

def generate_jobs_limited(s):
    """
    Limited Combinations mode.

    Yields one group of jobs per parameter that has a range or list defined.
    Within each group every other parameter is held at its default value.

    Example with vorticity range [0.5, 1.0, 1.5] and noise_strength range
    [0.5, 1.0]:
        Job 0: vorticity=0.5,  noise_strength=default  (vorticity sweep)
        Job 1: vorticity=1.0,  noise_strength=default
        Job 2: vorticity=1.5,  noise_strength=default
        Job 3: vorticity=default, noise_strength=0.5   (noise_strength sweep)
        Job 4: vorticity=default, noise_strength=1.0

    Parameters that are disabled (use_dissolve=False, use_noise=False) are
    included in the default job dict but their ranges are never swept.

    Parameters
    ----------
    s : SmokeSettings

    Yields
    ------
    dict — job parameter dict suitable for JSON serialisation
    """
    # Determine which parameters are enabled for sweeping.
    # Gas params, resolution, time_scale are always available.
    # Dissolve / noise / adaptive-timesteps / fire params only when their
    # section is enabled.
    sweepable = ["resolution", "vorticity", "alpha", "beta"]
    # v0.7.0 TODO-41: time_scale is always-on (no master enable).
    sweepable.append("time_scale")
    if s.use_dissolve:
        sweepable.append("dissolve_speed")
    if s.use_noise:
        sweepable += ["noise_upres", "noise_strength", "noise_spatial_scale"]
    # v0.7.0 TODO-41: CFL / timesteps only when adaptive timesteps are on.
    if getattr(s, "use_adaptive_timesteps", True):
        sweepable += ["cfl_number", "timesteps_max", "timesteps_min"]
    # v0.7.0 TODO-42: fire sub-params only when use_fire is on.
    if getattr(s, "use_fire", False):
        sweepable += ["burning_rate", "flame_smoke", "flame_vorticity",
                      "flame_max_temp", "flame_ignition"]

    yielded = False
    for param_name in sweepable:
        use_list  = getattr(s, param_name + "_use_list",  False)
        use_range = getattr(s, param_name + "_use_range", False)
        vals      = expand_param(s, param_name)

        # A list is always intentional even if it has only 1 item — the user
        # explicitly chose that value.  A range with step=0 collapses to a
        # single point and adds no variation; treat it as "no sweep".
        if use_list:
            is_explicit = True
        elif use_range:
            is_explicit = len(vals) > 1
        else:
            is_explicit = False

        if not is_explicit:
            continue

        # Sweep this parameter while all others stay at default
        base = _default_job(s)
        for v in vals:
            job = dict(base)
            job[param_name] = v
            yielded = True
            yield job
            # v0.7.0 TODO-45: Iterate Slow Dissolve — for each sweep job
            # that has use_dissolve on, also yield a companion with the
            # opposite slow_dissolve.  Skip when use_dissolve is False on
            # the job itself (e.g. an iterate_dissolve_both off-pass)
            # because slow doesn't apply there.
            if (s.use_dissolve and getattr(s, "iterate_slow_dissolve", False)
                    and job.get("use_dissolve")):
                flipped = dict(job)
                flipped["slow_dissolve"] = not job["slow_dissolve"]
                yield flipped

    # Iterate-both: append one comparison job with the feature toggled off.
    # Only fires when the feature is currently enabled (the checkbox is hidden
    # when the feature is off, so this path is only reached intentionally).
    if s.use_dissolve and s.iterate_dissolve_both:
        base = _default_job(s)
        job  = dict(base)
        job["use_dissolve"] = False
        yielded = True
        yield job
        # NOTE: no slow-flip companion here — this job has
        # use_dissolve=False so slow_dissolve doesn't apply.

    if s.use_noise and s.iterate_noise_both:
        base = _default_job(s)
        job  = dict(base)
        job["use_noise"] = False
        yielded = True
        yield job
        # v0.7.0 TODO-45: this noise-off job retains current use_dissolve
        # — if dissolve is on, also yield its slow-flipped companion.
        if (s.use_dissolve and getattr(s, "iterate_slow_dissolve", False)
                and job.get("use_dissolve")):
            flipped = dict(job)
            flipped["slow_dissolve"] = not job["slow_dissolve"]
            yield flipped

    # Fallback: if no axis sweep produced jobs and no iterate-both pass was
    # configured, emit a single baseline job. Otherwise a user with only
    # single-value parameters would see "0 jobs" and a disabled Export button,
    # which is surprising — testing one specific param combination is a valid
    # use case and should not require enabling All Combinations mode.
    if not yielded:
        base = _default_job(s)
        yield base
        # v0.7.0 TODO-45: fallback baseline also gets a slow companion
        # when iterate_slow_dissolve is on and use_dissolve is True.
        if (s.use_dissolve and getattr(s, "iterate_slow_dissolve", False)
                and base.get("use_dissolve")):
            flipped = dict(base)
            flipped["slow_dissolve"] = not base["slow_dissolve"]
            yield flipped


def generate_jobs_all(s):
    """
    All Combinations mode (original behaviour).

    Yields one job per element of the Cartesian product of all parameter
    ranges.  The total job count is the product of all range lengths, which
    can grow very large when multiple parameters have wide ranges.

    When iterate_dissolve_both / iterate_noise_both are enabled the product
    is extended to include jobs with that feature disabled, giving a direct
    on-vs-off comparison within a single batch.

    Parameters
    ----------
    s : SmokeSettings

    Yields
    ------
    dict — job parameter dict suitable for JSON serialisation
    """
    def param(name):
        return expand_param(s, name)

    res   = param("resolution")
    vort  = param("vorticity")
    alpha = param("alpha")
    beta  = param("beta")

    # Build the set of (use_dissolve, slow_dissolve, dissolve_vals) states.
    # When iterate_dissolve_both is on, generate two passes: feature on then off.
    # v0.7.0 TODO-45: when iterate_slow_dissolve is on AND use_dissolve is on,
    # also generate a parallel state with the slow_dissolve flag flipped, so
    # every dissolve-enabled combo gets baked both slow and fast.
    if s.use_dissolve:
        dissolve_states = [(True, s.slow_dissolve, param("dissolve_speed"))]
        if getattr(s, "iterate_slow_dissolve", False):
            dissolve_states.append((True, not s.slow_dissolve, param("dissolve_speed")))
        if s.iterate_dissolve_both:
            # use_dissolve=False — slow doesn't apply, no slow-flip companion.
            dissolve_states.append((False, s.slow_dissolve, [expand_param(s, "dissolve_speed")[0]]))
    else:
        dissolve_states = [(False, s.slow_dissolve, [expand_param(s, "dissolve_speed")[0]])]

    # Build the set of (use_noise, nu, ns, nss) states.
    if s.use_noise:
        noise_states = [(True,
                         param("noise_upres"),
                         param("noise_strength"),
                         param("noise_spatial_scale"))]
        if s.iterate_noise_both:
            noise_states.append((False,
                                  [expand_param(s, "noise_upres")[0]],
                                  [expand_param(s, "noise_strength")[0]],
                                  [expand_param(s, "noise_spatial_scale")[0]]))
    else:
        noise_states = [(False,
                         [expand_param(s, "noise_upres")[0]],
                         [expand_param(s, "noise_strength")[0]],
                         [expand_param(s, "noise_spatial_scale")[0]])]

    # v0.7.0 TODO-41: gas-timing axes always present in the product.
    # Adaptive-only sub-params (cfl_number, timesteps_*) collapse to their
    # begin value when adaptive is off, since they have no effect then.
    use_adapt = getattr(s, "use_adaptive_timesteps", True)
    time_scale_vals    = param("time_scale")
    if use_adapt:
        cfl_vals           = param("cfl_number")
        timesteps_max_vals = param("timesteps_max")
        timesteps_min_vals = param("timesteps_min")
    else:
        cfl_vals           = [expand_param(s, "cfl_number")[0]]
        timesteps_max_vals = [expand_param(s, "timesteps_max")[0]]
        timesteps_min_vals = [expand_param(s, "timesteps_min")[0]]

    # v0.7.0 TODO-42: fire sub-params collapse to single values when fire is off.
    use_fire = getattr(s, "use_fire", False)
    if use_fire:
        burning_rate_vals    = param("burning_rate")
        flame_smoke_vals     = param("flame_smoke")
        flame_vorticity_vals = param("flame_vorticity")
        flame_max_temp_vals  = param("flame_max_temp")
        flame_ignition_vals  = param("flame_ignition")
    else:
        burning_rate_vals    = [expand_param(s, "burning_rate")[0]]
        flame_smoke_vals     = [expand_param(s, "flame_smoke")[0]]
        flame_vorticity_vals = [expand_param(s, "flame_vorticity")[0]]
        flame_max_temp_vals  = [expand_param(s, "flame_max_temp")[0]]
        flame_ignition_vals  = [expand_param(s, "flame_ignition")[0]]

    for (use_d, slow_d, dissolve) in dissolve_states:
        for (use_n, nu, ns, nss) in noise_states:
            for combo in itertools.product(
                    res, vort, alpha, beta,
                    dissolve, nu, ns, nss,
                    time_scale_vals, cfl_vals, timesteps_max_vals, timesteps_min_vals,
                    burning_rate_vals, flame_smoke_vals, flame_vorticity_vals,
                    flame_max_temp_vals, flame_ignition_vals):
                yield {
                    "resolution":          combo[0],
                    "vorticity":           combo[1],
                    "alpha":               combo[2],
                    "beta":                combo[3],
                    "dissolve_speed":      combo[4],
                    "noise_upres":         combo[5],
                    "noise_strength":      combo[6],
                    "noise_spatial_scale": combo[7],
                    "use_dissolve":        use_d,
                    "slow_dissolve":       slow_d,
                    "use_noise":           use_n,
                    # v0.7.0 TODO-41: gas timing params
                    "time_scale":          combo[8],
                    "use_adaptive_timesteps": use_adapt,
                    "cfl_number":          combo[9],
                    "timesteps_max":       combo[10],
                    "timesteps_min":       combo[11],
                    # v0.7.0 TODO-42: fire params
                    "use_fire":            use_fire,
                    "burning_rate":        combo[12],
                    "flame_smoke":         combo[13],
                    "flame_vorticity":     combo[14],
                    "flame_max_temp":      combo[15],
                    "flame_ignition":      combo[16],
                }


# ---------------------------------------------------------------------------
# v0.9.0 TODO-55: emitter sweep layering (increment 3)
#
# The domain generators above stay emitter-agnostic.  generate_jobs() layers
# per-emitter values onto each domain job: in LIMITED mode every domain job gets
# the baseline emitters and each explicitly-swept emitter axis adds a row (one
# axis varied, everything else baseline); in ALL mode each domain job is crossed
# with the full emitter cartesian product.  The job dict gains an "emitters" key
# {name: {temperature, density, surface_distance, volume_density,
# use_initial_velocity, velocity_factor, velocity_normal, velocity_coord}} which
# rides into job JSON via job_data["params"] and into make_name().
# ---------------------------------------------------------------------------

# Emitter scalar params always sweepable; the velocity scalars only when
# use_initial_velocity is on (mirrors the use_dissolve / use_noise gating).
_EMITTER_SCALARS = ("temperature", "density", "surface_distance", "volume_density")
_EMITTER_VELOCITY_SCALARS = ("velocity_factor", "velocity_normal")


def _emitter_velocity_vectors(em):
    """Parsed (x, y, z) tuples from an emitter's velocity_list.

    Skips malformed entries (the UI red-tints them; export validation reports
    them).  Falls back to [_VELOCITY_DEFAULT] when empty / all-invalid so the
    baseline always has one vector.
    """
    vecs = []
    for item in getattr(em, "velocity_list", []):
        v = _parse_velocity_vector(getattr(item, "text", ""))
        if v is not None:
            vecs.append(v)
    return vecs if vecs else [_VELOCITY_DEFAULT]


def _emitter_baseline(em):
    """One emitter's baseline param dict — every axis at its first value."""
    d = {p: expand_param(em, p)[0] for p in _EMITTER_SCALARS}
    d["use_initial_velocity"] = bool(getattr(em, "use_initial_velocity", False))
    d["velocity_factor"] = expand_param(em, "velocity_factor")[0]
    d["velocity_normal"] = expand_param(em, "velocity_normal")[0]
    d["velocity_coord"]  = list(_emitter_velocity_vectors(em)[0])
    return d


def _default_emitters(s):
    """Baseline {emitter_name: param_dict} for all of s.emitters."""
    return {em.name: _emitter_baseline(em) for em in getattr(s, "emitters", [])}


def _emitter_sweep_axes(s):
    """Return [(emitter_name, param_key, values), ...] for every emitter param
    the user explicitly swept (list, or range with >1 value; velocity vector
    list with >1 vector).  Mirrors the is_explicit rule used for domain axes."""
    axes = []
    for em in getattr(s, "emitters", []):
        scalars = list(_EMITTER_SCALARS)
        if getattr(em, "use_initial_velocity", False):
            scalars += list(_EMITTER_VELOCITY_SCALARS)
        for p in scalars:
            use_list  = getattr(em, p + "_use_list",  False)
            use_range = getattr(em, p + "_use_range", False)
            vals = expand_param(em, p)
            if use_list:
                explicit = True
            elif use_range:
                explicit = len(vals) > 1
            else:
                explicit = False
            if explicit:
                axes.append((em.name, p, vals))
        # Initial X/Y/Z vector list — a sweep when >1 distinct vector entered.
        if getattr(em, "use_initial_velocity", False):
            vecs = _emitter_velocity_vectors(em)
            if len(vecs) > 1:
                axes.append((em.name, "velocity_coord", [list(v) for v in vecs]))
    return axes


def _emitter_combinations(s):
    """ALL-combinations: cartesian product over every swept emitter axis.

    Returns a list of complete emitters dicts.  When no emitter axis is swept,
    returns a single baseline combo so the domain product is unchanged.
    """
    default = _default_emitters(s)
    axes = _emitter_sweep_axes(s)
    if not axes:
        return [copy.deepcopy(default)]
    combos = []
    for combo in itertools.product(*[vals for (_, _, vals) in axes]):
        em = copy.deepcopy(default)
        for (ename, pkey, _), v in zip(axes, combo):
            em[ename][pkey] = v
        combos.append(em)
    return combos


def generate_jobs(s):
    """
    Dispatch to the appropriate domain generator based on s.iteration_mode,
    then layer per-emitter values (TODO-55) onto every job.

    Returns a generator of job dicts (each with an "emitters" block).
    """
    default_emitters = _default_emitters(s)

    if s.iteration_mode == 'LIMITED':
        # Domain sweeps: each domain job keeps emitters at baseline.
        for job in generate_jobs_limited(s):
            job["emitters"] = copy.deepcopy(default_emitters)
            yield job
        # Emitter sweeps: one emitter axis varied at a time, domain + other
        # emitters held at baseline (mirrors the domain Limited pattern).
        if default_emitters:
            domain_base = _default_job(s)
            for (ename, pkey, vals) in _emitter_sweep_axes(s):
                for v in vals:
                    job = dict(domain_base)
                    job["emitters"] = copy.deepcopy(default_emitters)
                    job["emitters"][ename][pkey] = v
                    yield job
    else:  # ALL — cross every domain job with every emitter combination.
        emitter_combos = _emitter_combinations(s)
        for job in generate_jobs_all(s):
            for em in emitter_combos:
                j = dict(job)
                j["emitters"] = copy.deepcopy(em)
                yield j


def _dedupe_jobs(jobs):
    """
    Return a new list with duplicate jobs removed, preserving first-seen order.

    Every axis sweep in generate_jobs_limited starts from the baseline (all
    other params at default), so when a sweep value equals that axis's default
    the baseline combination is emitted again.  An 8-axis sweep produces up to
    8 baseline duplicates per batch — each one targets the same cache directory
    (make_name is param-only) and would re-bake redundantly, with each FULL
    BAKE branch calling bpy.ops.fluid.free_all() to wipe the previous bake.

    Two jobs are duplicates iff every param value is equal.
    """
    seen   = set()
    unique = []
    for j in jobs:
        # v0.9.0 TODO-55: jobs now carry a nested "emitters" dict, so the old
        # tuple(sorted(items())) key is unhashable.  A sort_keys JSON dump is a
        # stable, hashable signature that handles nested dicts/lists too.
        key = json.dumps(j, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    return unique


def _fmt_num(x):
    """Compact float formatting for filename use (v0.7.1 TODO-48 A).

    Trims trailing zeros and unnecessary decimal points so:
        0.0   → "0"
        1.0   → "1"
        0.50  → "0.5"
        2.25  → "2.25"
        0.123456789 → "0.123" (rounded to 3 decimals)
    """
    return f"{round(float(x), 3):g}"


# v0.7.1 TODO-48 B: Single-character "OFF" indicator used by make_name().
# Lowercase 'x' chosen as the most-compact distinct marker — no value
# letter (D / N / F / etc.) could legitimately be followed by an 'x', so
# 'Dx' / 'Nx' / 'Fx' read unambiguously as "feature off" without needing
# the verbose "-OFF" suffix.
_OFF_SUFFIX = "x"


# v0.9.0 TODO-55: emitter param encoding for make_name().  Suffixes are
# default-suppressed against the documented FluidFlowSettings defaults — a value
# at its default contributes no token, so cache names stay short.  This is
# collision-SAFE because only the exact-default value is dropped: any differing
# value is encoded, so two jobs that differ in an emitter param always get
# distinct names (BUG-013 family).  Each token is namespaced by the emitter's
# sorted-order index (E0, E1, ...) so multiple emitters never clash.
_EMITTER_NAME_DEFAULTS = {
    "temperature": 1.0, "density": 1.0,
    "surface_distance": 1.5, "volume_density": 0.0,
    "velocity_factor": 0.0, "velocity_normal": 0.0,
}
_EMITTER_NAME_ABBR = {
    "temperature": "T", "density": "D",
    "surface_distance": "SE", "volume_density": "VE",
    "velocity_factor": "VS", "velocity_normal": "VN",
}


def _emitter_name_tokens(emitters):
    """Return make_name() tokens for the per-emitter params (default-suppressed,
    namespaced by sorted-order emitter index).  Pure / testable."""
    tokens = []
    for i, ename in enumerate(sorted(emitters or {})):
        em = emitters[ename]
        for key in _EMITTER_SCALARS:
            val = float(em.get(key, _EMITTER_NAME_DEFAULTS[key]))
            if val != _EMITTER_NAME_DEFAULTS[key]:
                tokens.append(f"E{i}{_EMITTER_NAME_ABBR[key]}{_fmt_num(val)}")
        # Velocity block — only when initial velocity is on.  The "Vy" marker
        # guarantees on/off differ even when every velocity value is at default.
        if em.get("use_initial_velocity"):
            tokens.append(f"E{i}Vy")
            for key in _EMITTER_VELOCITY_SCALARS:
                val = float(em.get(key, 0.0))
                if val != 0.0:
                    tokens.append(f"E{i}{_EMITTER_NAME_ABBR[key]}{_fmt_num(val)}")
            vc = list(em.get("velocity_coord") or [0.0, 0.0, 0.0])
            if [float(c) for c in vc] != [0.0, 0.0, 0.0]:
                comps = "c".join(_fmt_num(c) for c in vc)
                tokens.append(f"E{i}VC{comps}")
    return tokens


def make_name(p):
    """
    Build a human-readable filename stem from a job-parameter dict.

    Format (v0.7.1 — compact + default-suppressed):
        R<res>_V<vort>_A<alpha>_B<beta>_<dissolve>_<noise>[_<extras>]

    Where:
        <dissolve> = D<speed>-Slow|D<speed>-Fast  when use_dissolve else 'Dx'
        <noise>    = N<upres>_NS<str>_SC<scale>   when use_noise    else 'Nx'

    Optional extras (appended ONLY when the value differs from Blender's
    default — keeps v0.6.x cache names unchanged when nothing v0.7.x
    related has been touched):
        TS<n>         time_scale != 1.0
        ATx           use_adaptive_timesteps False (skip when True == default)
        CFL<n>        cfl_number != 4.0 AND adaptive on
        TMx<n>        timesteps_max != 4 AND adaptive on
        TMn<n>        timesteps_min != 1 AND adaptive on
        F-Y_BR<n>_FS<n>_FV<n>_TMax<n>_TIgn<n>   when use_fire (Fx omitted)

    Numbers are formatted with :g via _fmt_num() — trailing zeros stripped.

    The name is derived purely from simulation parameters so that identical
    parameter combinations always map to the same cache directory, render
    directory, and output files — regardless of job order or batch index.

    Parameters
    ----------
    p : dict — job parameter dict from generate_jobs()

    Returns
    -------
    str — filename stem without extension
    """
    # ── Existing core params (always present) ────────────────────────────
    # v0.7.0 BUG-013: explicit slow/fast indicator when use_dissolve.
    # v0.7.1 TODO-48 B: 'Dx' / 'Nx' replace '-OFF' suffixes.
    dissolve_part = (
        (f"D{int(p['dissolve_speed'])}-Slow" if p.get('slow_dissolve')
         else f"D{int(p['dissolve_speed'])}-Fast")
        if p['use_dissolve'] else f"D{_OFF_SUFFIX}"
    )
    noise_part = (
        f"N{int(p['noise_upres'])}_"
        f"NS{_fmt_num(p['noise_strength'])}_"
        f"SC{_fmt_num(p['noise_spatial_scale'])}"
        if p['use_noise'] else f"N{_OFF_SUFFIX}"
    )

    # ── v0.7.0 TODO-47: extras for new params (default-suppressed) ───────
    # Each suffix is appended ONLY when the value differs from Blender's
    # documented default, so jobs with nothing v0.7.x touched keep their
    # v0.6.x cache names unchanged.  Defaults: time_scale=1.0,
    # use_adaptive_timesteps=True, cfl=4.0, timesteps_max=4,
    # timesteps_min=1, use_fire=False (all 5 fire sub-params irrelevant
    # when off).
    extras = []

    # Time scale — suffix only when != 1.0
    _ts = float(p.get("time_scale", 1.0))
    if _ts != 1.0:
        extras.append(f"TS{_fmt_num(_ts)}")

    # Adaptive timesteps — when OFF, append a marker (default is ON).
    # When ON, CFL/timesteps sub-params each get their own suffix if
    # non-default.
    _adapt = bool(p.get("use_adaptive_timesteps", True))
    if not _adapt:
        extras.append(f"AT{_OFF_SUFFIX}")   # ATx
    else:
        _cfl = float(p.get("cfl_number", 4.0))
        if _cfl != 4.0:
            extras.append(f"CFL{_fmt_num(_cfl)}")
        _tmax = int(p.get("timesteps_max", 4))
        if _tmax != 4:
            extras.append(f"TMx{_tmax}")
        _tmin = int(p.get("timesteps_min", 1))
        if _tmin != 1:
            extras.append(f"TMn{_tmin}")

    # Fire — when ON, append F-Y plus the 5 sub-params.  When OFF
    # (default), suppress the suffix entirely so v0.6.x cache names
    # match exactly for any job that never touched fire.
    if p.get("use_fire", False):
        extras.append("F-Y")
        extras.append(f"BR{_fmt_num(p['burning_rate'])}")
        extras.append(f"FS{_fmt_num(p['flame_smoke'])}")
        extras.append(f"FV{_fmt_num(p['flame_vorticity'])}")
        extras.append(f"TMax{_fmt_num(p['flame_max_temp'])}")
        extras.append(f"TIgn{_fmt_num(p['flame_ignition'])}")

    # v0.9.0 TODO-55: per-emitter param tokens (default-suppressed, namespaced).
    extras.extend(_emitter_name_tokens(p.get("emitters")))

    extras_suffix = ("_" + "_".join(extras)) if extras else ""

    return (
        f"R{int(p['resolution'])}_"
        f"V{_fmt_num(p['vorticity'])}_"
        f"A{_fmt_num(p['alpha'])}_"
        f"B{_fmt_num(p['beta'])}_"
        f"{dissolve_part}_"
        f"{noise_part}"
        f"{extras_suffix}"
    )


# ---------------------------------------------------------------------------
# Batch export
# ---------------------------------------------------------------------------

def _blend_domain_resolution(domain_obj):
    """Return the domain's current resolution_max as stored in the .blend file.

    This is the resolution the scene was saved at — used as the denominator
    when scaling emitter density for different-resolution jobs.  Returns 0 if
    the object has no FLUID DOMAIN modifier (caller should treat 0 as 'unknown').
    """
    if domain_obj:
        for mod in domain_obj.modifiers:
            if mod.type == 'FLUID' and mod.fluid_type == 'DOMAIN':
                return mod.domain_settings.resolution_max
    return 0


# ---------------------------------------------------------------------------
# v0.9.0 TODO-55: emitter (flow object) discovery
#
# A fluid DOMAIN keeps NO backlink to its flow objects — Mantaflow simply
# includes every scene object that has a FLUID modifier of fluid_type 'FLOW'
# and whose geometry overlaps the domain.  So we discover emitters by scanning
# the scene (find_fluid_emitters), then filter to those whose world-space
# bounding box overlaps the domain's (emitters_inside_domain).  BatchSimLab is
# SINGLE-DOMAIN only — one domain per scene; a second domain would need
# per-domain emitter attribution and is intentionally out of scope.
#
# These helpers are deliberately pure (no bpy / mathutils import) so they are
# unit-testable with stub objects:
#   obj.modifiers     — iterable of objects with .type / .fluid_type
#   obj.bound_box     — 8 (x, y, z) corners in local space
#   obj.matrix_world  — any 4x4 row-indexable matrix (mathutils.Matrix at
#                       runtime; a nested list in tests)
# ---------------------------------------------------------------------------

def _is_flow_object(obj):
    """True if *obj* has a FLUID modifier configured as a flow/emitter."""
    try:
        return any(m.type == 'FLUID' and m.fluid_type == 'FLOW'
                   for m in obj.modifiers)
    except AttributeError:
        return False


def find_fluid_emitters(scene):
    """Return all fluid-emitter (FLOW) objects in *scene*, sorted by name.

    Step 1 of TODO-55 discovery: a pure modifier scan, NOT yet filtered to a
    domain.  Deterministic name order keeps the per-emitter UI sections and
    job-dict keys stable across re-exports.
    """
    try:
        objs = list(scene.objects)
    except AttributeError:
        return []
    emitters = [o for o in objs if _is_flow_object(o)]
    emitters.sort(key=lambda o: getattr(o, "name", ""))
    return emitters


def _world_aabb(obj):
    """Return (min_xyz, max_xyz) world-space axis-aligned bounds for *obj*.

    Transforms the 8 local-space `bound_box` corners by `matrix_world` (a plain
    affine multiply, so any 4x4 row-indexable matrix works) and takes the
    component-wise min/max.  Returns None when bounds can't be computed.
    """
    try:
        corners = list(obj.bound_box)
        m = obj.matrix_world
    except AttributeError:
        return None
    if not corners or m is None:
        return None
    xs, ys, zs = [], [], []
    for c in corners:
        x, y, z = c[0], c[1], c[2]
        xs.append(m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3])
        ys.append(m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3])
        zs.append(m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3])
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def _aabb_overlap(a, b):
    """True if two world-space AABBs intersect on all three axes.

    Each AABB is ((minx, miny, minz), (maxx, maxy, maxz)).  Touching faces
    count as overlapping.  None for either box → False.
    """
    if a is None or b is None:
        return False
    (amin, amax) = a
    (bmin, bmax) = b
    return all(amin[i] <= bmax[i] and bmin[i] <= amax[i] for i in range(3))


def emitters_inside_domain(emitters, domain_obj):
    """Step 2 of TODO-55 discovery: keep only emitters whose world bounds
    overlap the domain's world bounds.

    If the domain's bounds can't be computed (no bound_box / matrix_world),
    return *emitters* unchanged — better to over-include than to silently drop
    every emitter when containment can't be measured.
    """
    dom = _world_aabb(domain_obj) if domain_obj is not None else None
    if dom is None:
        return list(emitters)
    return [e for e in emitters if _aabb_overlap(_world_aabb(e), dom)]


def find_emitters(scene, domain_obj):
    """TODO-55 discovery composed: all scene FLOW objects, filtered to those
    inside *domain_obj*'s bounds, sorted by name (single-domain addon)."""
    return emitters_inside_domain(find_fluid_emitters(scene), domain_obj)


def _emitter_sync_plan(existing_names, desired_names):
    """Reconcile the `emitters` collection with the currently-discovered set.

    Returns (to_add, to_remove): emitter names to append (newly discovered) and
    names to drop (object gone / no longer inside the domain).  EXISTING
    elements are preserved so a user's in-progress sweep config survives a
    Refresh — only genuinely new/stale emitters are touched.  `to_add` follows
    `desired_names` order; `to_remove` follows `existing_names` order.  Pure /
    unit-testable; the bpy collection mutation lives in `_populate_emitters`.
    """
    existing = list(existing_names)
    desired  = list(desired_names)
    to_add    = [n for n in desired  if n not in existing]
    to_remove = [n for n in existing if n not in desired]
    return to_add, to_remove


# Map: addon EmitterSettings scalar name → bpy.types.FluidFlowSettings attr.
# (Names happen to match 1:1, but keep the map explicit so a future rename or a
# differently-named Blender attr is a one-line change.)
_EMITTER_FLOW_IMPORT_MAP = (
    ("temperature",      "temperature"),       # Initial Temperature
    ("density",          "density"),           # Density
    ("surface_distance", "surface_distance"),  # Surface Emission
    ("volume_density",   "volume_density"),    # Volume Emission
    ("velocity_factor",  "velocity_factor"),   # Source (velocity)
    ("velocity_normal",  "velocity_normal"),   # Normal (velocity)
)


def _flow_settings_of(obj):
    """Return the FluidFlowSettings of *obj*'s FLOW modifier, or None."""
    try:
        for m in obj.modifiers:
            if m.type == 'FLUID' and m.fluid_type == 'FLOW':
                return m.flow_settings
    except AttributeError:
        pass
    return None


def _seed_emitter_from_flow(em, flow):
    """Seed an EmitterSettings element's baseline (_begin) values from the
    emitter's CURRENT flow settings (TODO-55 section B auto-populate).

    Sweep config (_end/_step/_use_range/_use_list/_list) is left at defaults —
    only baselines + the velocity seed are written.  Each read is guarded so a
    liquid flow object or an older Blender build missing an attr is skipped
    rather than crashing.  No-op when *flow* is None.
    """
    if flow is None:
        return
    for addon_name, flow_attr in _EMITTER_FLOW_IMPORT_MAP:
        try:
            setattr(em, addon_name + "_begin", float(getattr(flow, flow_attr)))
        except (AttributeError, TypeError, ValueError):
            continue
    try:
        em.use_initial_velocity = bool(getattr(flow, "use_initial_velocity"))
    except (AttributeError, TypeError):
        pass
    # Seed the velocity vector list with the emitter's current Initial X/Y/Z.
    try:
        coord = getattr(flow, "velocity_coord")
        vec = (float(coord[0]), float(coord[1]), float(coord[2]))
    except (AttributeError, TypeError, IndexError, ValueError):
        vec = _VELOCITY_DEFAULT
    em.velocity_list.clear()
    item = em.velocity_list.add()
    item.text = _format_velocity_vector(vec)


def _populate_emitters(s, scene):
    """Sync `s.emitters` with the flow objects discovered inside the domain.

    Adds an element per newly-discovered emitter (seeded from its live flow
    settings), removes elements whose object is gone / no longer inside the
    domain, and leaves existing elements — and their in-progress sweep config —
    untouched.  Safe to call repeatedly (Refresh Emitters button + domain
    select).  No-op-ish when there's no scene.
    """
    domain = getattr(s, "domain_obj", None)
    objs = find_emitters(scene, domain) if scene is not None else []
    by_name = {o.name: o for o in objs}
    to_add, to_remove = _emitter_sync_plan(
        [em.name for em in s.emitters], list(by_name.keys()))

    if to_remove:
        remove_set = set(to_remove)
        for i in range(len(s.emitters) - 1, -1, -1):
            if s.emitters[i].name in remove_set:
                s.emitters.remove(i)

    for name in to_add:
        em = s.emitters.add()
        em.name = name
        _seed_emitter_from_flow(em, _flow_settings_of(by_name[name]))


# ---------------------------------------------------------------------------
# v0.9.0 TODO-55: emitter Initial Velocity is swept as a LIST OF XYZ VECTORS.
# The user adds as many "x, y, z" vectors as they want to compare (default
# "0, 0, 0"); each vector is one swept value.  The entry widget shows the
# expected format.  These pure helpers parse/format the string form so the UI
# and job generation share one definition (and so parsing is unit-testable
# without a running Blender).
# ---------------------------------------------------------------------------

_VELOCITY_DEFAULT = (0.0, 0.0, 0.0)
_VELOCITY_FORMAT_HINT = "x, y, z  (e.g. 0, 0, 1)"


def _parse_velocity_vector(text):
    """Parse a user-entered velocity string into an (x, y, z) float tuple.

    Accepts comma- or whitespace-separated values with optional surrounding
    spaces or brackets, e.g. "0,0,1", " 1, 0, -2 ", "[0, 0, 5]".  Returns None
    when the text does not contain exactly three numbers — callers treat None
    as 'invalid: show the format hint / skip this entry'.
    """
    if text is None:
        return None
    cleaned = text.strip().strip("[](){}").strip()
    if not cleaned:
        return None
    parts = cleaned.split(",") if "," in cleaned else cleaned.split()
    if len(parts) != 3:
        return None
    try:
        return tuple(float(p) for p in parts)
    except (ValueError, TypeError):
        return None


def _format_velocity_vector(vec):
    """Format an (x, y, z) iterable back to a compact "x, y, z" string.

    Uses the same trailing-zero-trimming `_fmt_num` as filename/value display
    so "0, 0, 1" round-trips cleanly.
    """
    return ", ".join(_fmt_num(c) for c in vec)


# Sentinel filename matchers used by the poll + summary code.  Defined as exact
# regexes (NOT endswith) so the two-pass pipeline's per-phase sentinels —
# job_NNNN.bake.done / .render.done / .bake.crashed / .render.crashed — are NOT
# counted as job completions or crashes (they're diagnostic-only; the .bat /
# launcher also write the unphased aliases below so the existing poll/summary
# logic keeps working unchanged).
_DONE_RE         = re.compile(r"^job_\d{4}\.done$")
_RETRY_DONE_RE   = re.compile(r"^job_\d{4}_retry\.done$")
_CRASHED_RE      = re.compile(r"^job_\d{4}\.crashed$")
# Phased completion markers — counted only by the overall-progress display so
# the bar advances during the bake pass (before any unphased <stem>.done is
# written).  Final job-complete trigger still uses _DONE_RE / _RETRY_DONE_RE.
_BAKE_DONE_RE    = re.compile(r"^job_\d{4}\.bake\.done$")
_RENDER_DONE_RE  = re.compile(r"^job_\d{4}\.render\.done$")


def _scene_has_camera(scene):
    """Return True if `scene` has at least one CAMERA object.

    Used by Export Batch and Run Batch to warn when rendering is on but no
    camera exists in the scene — the render passes will produce black/empty
    images.  Pure helper (testable with any object-list-bearing stub).
    """
    try:
        return any(obj.type == 'CAMERA' for obj in scene.objects)
    except AttributeError:
        return False


def _find_next_job_index(jobs_dir):
    """Return the next available job index given a jobs directory.

    Scans for job_NNNN.json files and returns max(existing_index) + 1,
    or 0 if no job files exist yet.
    """
    if not os.path.isdir(jobs_dir):
        return 0
    indices = [
        int(m.group(1))
        for f in os.listdir(jobs_dir)
        if (m := re.match(r'^job_(\d{4})\.json$', f))
    ]
    return max(indices) + 1 if indices else 0


def _existing_jobs_for_bat(jobs_dir, job_start_index):
    """Return [(index, name, render_mode), ...] for already-exported jobs.

    Used in APPEND mode (TODO-28): the regenerated run_smoke_batch.bat must
    re-list every previously-exported job (indices < job_start_index) ahead of
    the newly appended ones, otherwise re-writing the .bat in "w" mode drops
    them and Run Batch silently skips all earlier jobs.  Re-listed jobs are
    cheap on a second run — they SKIP BAKE / reuse placeholders.

    Reads name and render_mode from each job_NNNN.json; falls back to sane
    defaults if a file is missing fields or cannot be parsed.  Sorted by index.
    """
    result = []
    if not os.path.isdir(jobs_dir):
        return result
    for f in os.listdir(jobs_dir):
        m = re.match(r'^job_(\d{4})\.json$', f)
        if not m:
            continue
        idx = int(m.group(1))
        if idx >= job_start_index:
            continue
        try:
            with open(os.path.join(jobs_dir, f), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        result.append((idx,
                       data.get("name", f"job_{idx:04d}"),
                       data.get("render_mode", "CYCLES")))
    result.sort(key=lambda t: t[0])
    return result


def _job_run_cmd(python_exe, dest_launcher, dest_worker, blender_exe,
                 blend_file, job_path, render_mode, launcher_exists,
                 phase="both"):
    """Return the command line that runs a single job inside run_smoke_batch.bat.

    Prefers smoke_launcher.py (crash detection + logging); falls back to calling
    Blender directly when the launcher was not exported.  The launcher reads all
    job details from job_path, so the same command form works for both newly
    exported and previously exported (re-listed) jobs.

    The `phase` arg drives the two-phase pipeline: ``"bake"`` forces background
    even for EEVEE jobs (baking is engine-independent), ``"render"`` keeps the
    visible window for EEVEE, and ``"both"`` (the default) preserves the
    original single-pass invocation with no ``--phase`` argument.
    """
    _phase_suffix = "" if phase == "both" else f' --phase {phase}'
    if launcher_exists:
        return f'"{python_exe}" "{dest_launcher}" "{blender_exe}" "{job_path}"{_phase_suffix}'
    # Fallback (no launcher) — mirror the launcher's mode decision.
    _windowed = (render_mode == "EEVEE" and phase != "bake")
    if _windowed:
        return (
            f'"{blender_exe}" "{blend_file}" '
            f'--window-geometry 0 0 100 100 --factory-startup '
            f'--python "{dest_worker}" -- "{job_path}"{_phase_suffix}'
        )
    return (
        f'"{blender_exe}" "{blend_file}" '
        f'--background --factory-startup '
        f'--python "{dest_worker}" -- "{job_path}"{_phase_suffix} 2>nul'
    )


def _job_bat_block(job_num, total_jobs, name, run_cmd, done_path,
                   label="Job", alias_done_path=None):
    """Return the run_smoke_batch.bat lines for a single job.

    job_num is 1-based (what the echo shows the user).  The block runs the job,
    then writes a .done sentinel (`done_path`) recording success or the error
    exit code so the addon's poll timer can mark the row COMPLETE / FAILED.

    `label` is the echo header word (e.g. "Job", "Bake", "Render") — defaults to
    "Job" so single-pass callers keep their existing output.  ERRORS counter
    name is derived from the label so bake-pass and render-pass failures are
    reported separately in the batch console summary.

    `alias_done_path`, when given, receives a duplicate of the same sentinel
    line.  Used by the two-pass pipeline to also write the legacy unphased
    `<stem>.done` after the FINAL pass (render — or bake in bake-only mode), so
    existing addon code that scans for `*.done` keeps working unchanged.
    """
    _counter = "ERRORS" if label == "Job" else f"{label.upper()}_ERRORS"
    # Compute the sentinel line INSIDE the if/else, then write the file(s)
    # OUTSIDE the block with a single redirect each.  Multiple "echo … > path"
    # redirects nested in a parenthesised `if/else` block have produced
    # silently-missing files on at least one Windows configuration (v0.4.0Test
    # 2026-05-28: the `.render.done` was written but the unphased alias `.done`
    # was not).  This pattern uses one redirect per line at the top scope and
    # has no double-redirect-in-block risk.
    block = [
        f"echo === {label} {job_num}/{total_jobs}: {name} ===",
        run_cmd,
        "if errorlevel 1 (",
        f"    echo   WARNING: {label.lower()} exited with error",
        f"    set /a {_counter}+=1",
        f'    set "_SSL_DONE_LINE=error exit !ERRORLEVEL! {name} %DATE% %TIME%"',
        ") else (",
        f'    set "_SSL_DONE_LINE=done {name} %DATE% %TIME%"',
        ")",
        f'echo !_SSL_DONE_LINE!>"{done_path}"',
    ]
    if alias_done_path:
        block.append(f'echo !_SSL_DONE_LINE!>"{alias_done_path}"')
    block.append("echo.")
    return block


def _batch_ready(output_path):
    """True if a runnable batch exists on disk (TODO-25).

    Requires both run_smoke_batch.bat and at least one job_NNNN.json so the Run
    Batch button is only enabled when there is actually something to run —
    whether jobs were just exported or left over from a previous session.
    """
    bat = os.path.join(output_path, "run_smoke_batch.bat")
    jobs_dir = os.path.join(output_path, "jobs")
    if not os.path.isfile(bat) or not os.path.isdir(jobs_dir):
        return False
    return any(re.match(r'^job_\d{4}\.json$', f) for f in os.listdir(jobs_dir))


def _batch_is_running():
    """True while a batch poll timer is active (a Run Batch is in progress).

    Used to disable Export/Append and Run Batch mid-run: the running cmd.exe has
    already parsed run_smoke_batch.bat into memory, so editing it would not
    affect the active batch and only invites the TODO-28 confusion.
    """
    try:
        return bpy.app.timers.is_registered(_poll_batch_progress)
    except Exception:
        return False


def export_batch(context):
    """
    Prepare all batch job files and write the Windows .bat launcher.

    Steps:
      1. Copy smoke_worker.py from the addon folder to <output_path>.
      2. For each job, write a JSON config file to <output_path>/jobs/.
      3. Write run_smoke_batch.bat which launches one Blender process per job.

    The .bat file redirects each job's stdout+stderr to a per-job .log file
    so output is preserved even when the command window closes.

    Parameters
    ----------
    context : bpy.types.Context

    Returns
    -------
    (job_count, bat_path, dup_count) : (int, str, int)
        dup_count is the number of identical job dicts that were collapsed
        before writing (see _dedupe_jobs).

    Raises
    ------
    FileNotFoundError — if smoke_worker.py is missing from the addon folder
    """
    s = context.scene.smoke_settings

    output_path = bpy.path.abspath(s.output_path)
    jobs_dir    = os.path.join(output_path, "jobs")

    is_append = (s.export_mode == 'APPEND')

    # In append mode find the highest existing job index so new jobs continue
    # the numbering sequence without overwriting any previous results.
    job_start_index = _find_next_job_index(jobs_dir) if is_append else 0

    # Clear the jobs folder in replace mode so stale jobs from a previous
    # export don't linger.  Fall back to per-file deletion if rmtree hits a
    # PermissionError (e.g. a _retry.log held open by a running Blender).
    if not is_append:
        if os.path.isdir(jobs_dir):
            try:
                shutil.rmtree(jobs_dir)
            except PermissionError:
                for _fn in os.listdir(jobs_dir):
                    try:
                        os.unlink(os.path.join(jobs_dir, _fn))
                    except OSError:
                        pass
    os.makedirs(jobs_dir, exist_ok=True)

    # Use the currently running Blender instance
    blender_exe = bpy.app.binary_path
    blend_file  = bpy.data.filepath
    if s.use_default_frames:
        frame_start = context.scene.frame_start
        frame_end   = context.scene.frame_end
    else:
        frame_start = s.sim_frame_start
        frame_end   = s.sim_frame_end
    python_exe  = sys.executable   # Blender's bundled Python — always on disk, no PATH needed
    jobs        = list(generate_jobs(s))
    _raw_count  = len(jobs)
    jobs        = _dedupe_jobs(jobs)
    _dup_count  = _raw_count - len(jobs)
    jobs.sort(key=lambda p: p.get("resolution", 0))

    # ── Pre-compute per-job emitter densities (done once, not in the worker) ──
    # Read every FLOW emitter's current density from the scene, then for each
    # job compute the scaled density = base * (job_res / blend_res).  The
    # worker just applies the pre-computed value — no math, no domain lookup.
    _blend_res   = _blend_domain_resolution(s.domain_obj)
    _base_densities = {}  # {obj_name: base_density}
    if s.maintain_density and _blend_res > 0:
        for _obj in context.scene.objects:
            for _mod in _obj.modifiers:
                if _mod.type == 'FLUID' and _mod.fluid_type == 'FLOW':
                    _base_densities[_obj.name] = _mod.flow_settings.density

    # ── Locate and copy worker script ────────────────────────────────────────
    # __file__ is reliable here because we are installed as a proper addon,
    # so it points to the addon's folder (still `SmokeSimLab/` on disk per
    # v0.6.3 surface rename — see module docstring), not the .blend file.
    addon_dir      = os.path.dirname(os.path.abspath(__file__))
    src_worker     = os.path.join(addon_dir, "smoke_worker.py")
    dest_worker    = os.path.join(output_path, "smoke_worker.py")
    src_launcher   = os.path.join(addon_dir, "smoke_launcher.py")
    dest_launcher  = os.path.join(output_path, "smoke_launcher.py")

    if not os.path.exists(src_worker):
        raise FileNotFoundError(
            f"smoke_worker.py not found in addon folder.\n"
            f"Expected: {src_worker}\n"
            f"Re-install the BatchSimLab addon."
        )
    shutil.copy2(src_worker, dest_worker)
    if os.path.exists(src_launcher):
        shutil.copy2(src_launcher, dest_launcher)
    _launcher_exists = os.path.exists(dest_launcher)

    # ── Write .bat header ────────────────────────────────────────────────────
    total_jobs = job_start_index + len(jobs)
    _bat_header = (
        f"BatchSimLab batch - {len(jobs)} new job(s) (total {total_jobs})"
        if is_append else
        f"BatchSimLab batch - {len(jobs)} job(s)"
    )
    # Two-phase pipeline: bake all jobs (headless), then render (per-engine mode).
    # In bake-only mode (render_simulation_result=False) the render pass is omitted.
    _bake_only = not s.render_simulation_result
    bat_lines = [
        "@echo off",
        # Switch to the bat file's own directory so cmd always has a valid cwd,
        # regardless of what working directory Blender or the shell inherited.
        'cd /d "%~dp0"',
        "setlocal enabledelayedexpansion",
        f"echo {_bat_header}",
        "echo.",
        "set BAKE_ERRORS=0",
    ]
    if not _bake_only:
        bat_lines.append("set RENDER_ERRORS=0")
    bat_lines.append("")

    _dbg = s.collect_debug_log
    _dedup_note = f"  (deduplicated {_dup_count} identical job(s))" if _dup_count else ""
    _debug_log(_dbg, output_path, "addon",
               f"export_batch: {'append' if is_append else 'replace'}  "
               f"{len(jobs)} new job(s) starting at {job_start_index}{_dedup_note}  "
               f"blend={bpy.data.filepath!r}  out={output_path!r}  "
               f"bpy={bpy.app.version_string}")

    # ── Seed the Job Log list (one row per job, status=NOT_STARTED) ─────────
    # In replace mode clear all existing log state first.
    if not is_append:
        _job_statuses.clear()
        _job_log_rows.clear()
        s.job_log_items.clear()
    for i_offset, p in enumerate(jobs):
        i = job_start_index + i_offset
        _log_row = s.job_log_items.add()
        _log_row.job_number = i + 1
        _log_row.job_name   = make_name(p)
        _log_row.status     = 'NOT_STARTED'
        _job_log_rows.append((i + 1, make_name(p)))

    # ── Write one JSON per new job (the two-pass .bat is emitted after the
    #    full job set is known so existing append-mode jobs can be folded in) ──
    for i_offset, p in enumerate(jobs):
        i         = job_start_index + i_offset
        name      = make_name(p)
        job_path  = os.path.join(jobs_dir, f"job_{i:04d}.json")
        log_path  = os.path.join(jobs_dir, f"job_{i:04d}.log")  # unified across phases

        job_data = {
            "params":         p,
            "name":           name,
            "blend_file":     blend_file,
            "output_path":    output_path,
            "domain_name":    s.domain_obj.name,
            "frame_start":    frame_start,
            "frame_end":      frame_end,
            "addon_version":  ADDON_VERSION,
            "render_mode":    s.render_mode,
            "render_samples": s.render_samples,
            "render_simulation_result": s.render_simulation_result,
            "render_animation":         s.render_animation,
            "render_resolution_x": context.scene.render.resolution_x,
            "render_resolution_y": context.scene.render.resolution_y,
            "use_placeholders": s.use_placeholders,
            "use_existing_cache": s.use_existing_cache or s.use_placeholders,
            "maintain_density": s.maintain_density,
            "emitter_densities": {
                name: base * (float(p.get("resolution", _blend_res)) / _blend_res)
                for name, base in _base_densities.items()
            } if _base_densities else {},
            "collect_crash_logs":      s.collect_crash_logs,
            "collect_estimation_data": s.collect_estimation_data,
            "collect_debug_log":       s.collect_debug_log,
            "log_path":       log_path,
            "text_objects": {
                "resolution": s.text_resolution,
                "noise":      s.text_noise,
                "dissolve":   s.text_dissolve,
                "time":       s.text_time,
            },
        }

        with open(job_path, "w") as fh:
            json.dump(job_data, fh, indent=2)
        _debug_log(_dbg, output_path, "addon", f"job {i} ({i_offset+1}/{len(jobs)}): {name}  params={p}")

    # ── Two-pass .bat: BAKE pass then RENDER pass ────────────────────────────
    # Existing append-mode jobs are folded in first so a re-run sees the full
    # batch in order.  Each pass uses phased sentinels (<stem>.bake.done /
    # <stem>.render.done) so the two launcher calls per job don't clobber
    # each other.  In bake-only mode the render pass is omitted entirely.
    _all_jobs = []   # (idx, name, render_mode) for every job in this batch
    if is_append:
        _all_jobs.extend(_existing_jobs_for_bat(jobs_dir, job_start_index))
    for i_offset, p in enumerate(jobs):
        _all_jobs.append((job_start_index + i_offset, make_name(p), s.render_mode))

    def _emit_pass(label, phase, suffix, is_final):
        lines = [
            "echo ================================",
            f"echo {label.upper()} PASS ({total_jobs} job(s))",
            "echo ================================",
            "echo.",
        ]
        for _i, _n, _rm in _all_jobs:
            _jp = os.path.join(jobs_dir, f"job_{_i:04d}.json")
            _dp = os.path.join(jobs_dir, f"job_{_i:04d}.{suffix}.done")
            # Final pass also writes the legacy unphased <stem>.done alias so
            # the addon's existing poll/summary (which scans for *.done) marks
            # the job complete without phase-aware changes.
            _alias = os.path.join(jobs_dir, f"job_{_i:04d}.done") if is_final else None
            _cmd = _job_run_cmd(python_exe, dest_launcher, dest_worker,
                                blender_exe, blend_file, _jp, _rm,
                                _launcher_exists, phase=phase)
            lines += _job_bat_block(_i + 1, total_jobs, _n, _cmd, _dp,
                                    label=label, alias_done_path=_alias)
        return lines

    # In bake-only mode the bake pass IS the final pass.
    bat_lines += _emit_pass("Bake", "bake", "bake", is_final=_bake_only)
    if not _bake_only:
        bat_lines += _emit_pass("Render", "render", "render", is_final=True)

    # ── Write .bat footer ────────────────────────────────────────────────────
    _summary = "Bake errors: %BAKE_ERRORS%"
    if not _bake_only:
        _summary += "  Render errors: %RENDER_ERRORS%"
    bat_lines += [
        "echo ================================",
        f"echo Batch complete.  {_summary}",
        f'echo Results: {os.path.join(output_path, "Renders", "results.csv")}',
        "echo ================================",
    ]
    if s.collect_debug_log:
        bat_lines.append("pause")

    bat_path = os.path.join(output_path, "run_smoke_batch.bat")
    with open(bat_path, "w") as fh:
        fh.write("\n".join(bat_lines))

    return len(jobs), bat_path, _dup_count


# ---------------------------------------------------------------------------
# Property groups
# ---------------------------------------------------------------------------

class ValueItem(bpy.types.PropertyGroup):
    """
    Single float entry in a parameter explicit-value list.

    min_bound / max_bound are set by SMOKE_OT_add_value from the RNA hard
    limits so manually-typed values outside the parameter's allowed range
    are clamped automatically on edit.  0/0 means no limit active.
    """
    def _clamp_value(self, context):
        lo, hi = self.min_bound, self.max_bound
        if lo < hi:
            self.value = max(lo, min(hi, self.value))
        elif lo > 0 and self.value < lo:
            self.value = lo

    value:     bpy.props.FloatProperty(update=_clamp_value)
    int_value: bpy.props.IntProperty()
    marked:    bpy.props.BoolProperty(default=False)
    min_bound: bpy.props.FloatProperty(default=0.0)
    max_bound: bpy.props.FloatProperty(default=0.0)


class SMOKE_UL_value_list(bpy.types.UIList):
    """
    Custom UIList: checkbox on the left marks items for deletion; the float
    field on the right is the editable value.  Press the - button to remove
    all checked items (or the highlighted item if none are checked).
    """
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "marked", text="")
        row.prop(item, "value", text="", emboss=True)


class VelocityItem(bpy.types.PropertyGroup):
    """One Initial-Velocity vector entry, stored as an "x, y, z" string.

    v0.9.0 TODO-55: an emitter's Initial Velocity is swept as a LIST of explicit
    XYZ vectors.  Each entry holds the raw user text (validated against
    `_parse_velocity_vector`); `marked` flags the row for deletion in the UIList,
    mirroring ValueItem.
    """
    text:   bpy.props.StringProperty(
        name="Velocity",
        description='Initial velocity vector, format "x, y, z" (e.g. 0, 0, 1)',
        default="0, 0, 0",
    )
    marked: bpy.props.BoolProperty(default=False)


class EmitterSettings(bpy.types.PropertyGroup):
    """Per-emitter sweep settings — one element per discovered flow object.

    v0.9.0 TODO-55.  Held in `SmokeSettings.emitters` (a CollectionProperty),
    keyed by the flow object's `name`.  Each SCALAR emitter property reuses the
    exact same Range/List sextet as the domain params, so `expand_param()` works
    on an EmitterSettings instance with no changes:

        <p>_begin / _end / _step / _use_range / _use_list / _list / _index

    Scalars always available (map to bpy.types.FluidFlowSettings):
        temperature       → temperature        (Initial Temperature)
        density           → density            (Density)
        surface_distance  → surface_distance   (Surface Emission)
        volume_density    → volume_density     (Volume Emission)

    Gated by `use_initial_velocity` (mirrors the use_dissolve / use_noise
    gating in generate_jobs_*):
        velocity_factor   → velocity_factor    (Source — scalar)
        velocity_normal   → velocity_normal    (Normal — scalar)
        velocity_list     → velocity_coord     (Initial X/Y/Z — list of vectors)
    """
    name: bpy.props.StringProperty(default="")
    show: bpy.props.BoolProperty(
        default=False,
        description="Expand or collapse this emitter's parameters",
    )

    # Initial Temperature — flow_settings.temperature
    temperature_begin:     bpy.props.FloatProperty(default=1.0)
    temperature_end:       bpy.props.FloatProperty(default=1.0)
    temperature_step:      bpy.props.FloatProperty(default=0)
    temperature_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("temperature"))
    temperature_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("temperature"))
    temperature_list:      bpy.props.CollectionProperty(type=ValueItem)
    temperature_index:     bpy.props.IntProperty()

    # Density — flow_settings.density
    density_begin:     bpy.props.FloatProperty(default=1.0)
    density_end:       bpy.props.FloatProperty(default=1.0)
    density_step:      bpy.props.FloatProperty(default=0)
    density_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("density"))
    density_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("density"))
    density_list:      bpy.props.CollectionProperty(type=ValueItem)
    density_index:     bpy.props.IntProperty()

    # Surface Emission — flow_settings.surface_distance
    surface_distance_begin:     bpy.props.FloatProperty(default=1.5)
    surface_distance_end:       bpy.props.FloatProperty(default=1.5)
    surface_distance_step:      bpy.props.FloatProperty(default=0)
    surface_distance_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("surface_distance"))
    surface_distance_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("surface_distance"))
    surface_distance_list:      bpy.props.CollectionProperty(type=ValueItem)
    surface_distance_index:     bpy.props.IntProperty()

    # Volume Emission — flow_settings.volume_density
    volume_density_begin:     bpy.props.FloatProperty(default=0.0)
    volume_density_end:       bpy.props.FloatProperty(default=0.0)
    volume_density_step:      bpy.props.FloatProperty(default=0)
    volume_density_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("volume_density"))
    volume_density_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("volume_density"))
    volume_density_list:      bpy.props.CollectionProperty(type=ValueItem)
    volume_density_index:     bpy.props.IntProperty()

    # ── Initial Velocity (master toggle gates the three below) ───────────────
    use_initial_velocity: bpy.props.BoolProperty(
        default=False,
        description=(
            "Sweep this emitter's initial velocity — Source (factor), Normal, "
            "and the Initial X/Y/Z vector list"
        ),
    )

    # Source — flow_settings.velocity_factor (scalar)
    velocity_factor_begin:     bpy.props.FloatProperty(default=0.0)
    velocity_factor_end:       bpy.props.FloatProperty(default=0.0)
    velocity_factor_step:      bpy.props.FloatProperty(default=0)
    velocity_factor_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("velocity_factor"))
    velocity_factor_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("velocity_factor"))
    velocity_factor_list:      bpy.props.CollectionProperty(type=ValueItem)
    velocity_factor_index:     bpy.props.IntProperty()

    # Normal — flow_settings.velocity_normal (scalar)
    velocity_normal_begin:     bpy.props.FloatProperty(default=0.0)
    velocity_normal_end:       bpy.props.FloatProperty(default=0.0)
    velocity_normal_step:      bpy.props.FloatProperty(default=0)
    velocity_normal_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("velocity_normal"))
    velocity_normal_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("velocity_normal"))
    velocity_normal_list:      bpy.props.CollectionProperty(type=ValueItem)
    velocity_normal_index:     bpy.props.IntProperty()

    # Initial X/Y/Z — flow_settings.velocity_coord (list of XYZ vectors)
    velocity_list:  bpy.props.CollectionProperty(type=VelocityItem)
    velocity_index: bpy.props.IntProperty()


class SmokeJobItem(bpy.types.PropertyGroup):
    """One row in the Job Log panel section."""
    job_number: bpy.props.IntProperty(name="Job #",  default=0)
    job_name:   bpy.props.StringProperty(name="Name", default="")
    status:     bpy.props.EnumProperty(
        name="Status",
        items=[
            ('NOT_STARTED', "Not Started", ""),
            ('IN_PROGRESS', "Baking",      ""),  # active during the bake phase
            ('BAKED',       "Baked (awaiting render)", ""),
            ('RENDERING',   "Rendering",   ""),  # active during the render phase
            ('RETRYING',    "Retrying",     ""),
            ('COMPLETE',    "Complete",     ""),
            ('FAILED',      "Failed",       ""),
            ('CRASHED',     "Crashed",      ""),
        ],
        default='NOT_STARTED',
    )


class SMOKE_UL_job_log(bpy.types.UIList):
    """Job Log list — one row per exported job, colour-coded by status."""

    # Icons that work reliably across Blender 4.x and 5.x.
    # SEQUENCE_COLOR_XX icons are unavailable in Blender 5.1.1 and cause
    # silent row blanking, so they have been replaced with stable alternatives.
    _STATUS_ICONS = {
        'NOT_STARTED': 'RADIOBUT_OFF',
        'IN_PROGRESS': 'PLAY',           # active during bake phase
        'BAKED':       'CHECKBOX_HLT',   # bake done, render pending (two-phase)
        'RENDERING':   'RENDER_ANIMATION',  # active during render phase
        'RETRYING':    'FILE_REFRESH',
        'COMPLETE':    'CHECKMARK',
        'FAILED':      'CANCEL',
        'CRASHED':     'ERROR',
    }

    # Unicode prefix prepended to the job name for a second colour-free status
    # indicator that is visible even when icons fail to render.
    _STATUS_PREFIX = {
        'NOT_STARTED': '',
        'IN_PROGRESS': '▶ ',   # ▶  active in bake phase
        'BAKED':       '◐ ',   # ◐  bake done, awaiting render pass
        'RENDERING':   '◉ ',   # ◉  active in render phase
        'RETRYING':    '↻ ',   # ↻
        'COMPLETE':    '✓ ',   # ✓
        'FAILED':      '✗ ',   # ✗
        'CRASHED':     '⚠ ',   # ⚠
    }

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index=0, _flt_flag=0):
        if self.layout_type not in {'DEFAULT', 'COMPACT'}:
            return
        # Blender passes `index` — the item's position in the displayed list.
        # Since we apply no filtering, this equals the collection index and maps
        # directly to _job_log_rows.  No RNA property reads needed at all.
        # _flt_flag=0 accepts Blender 5.x passing it as a positional argument.
        if index >= len(_job_log_rows):
            return
        job_number, job_name = _job_log_rows[index]
        status = _job_statuses.get(job_number, 'NOT_STARTED')
        status_icon   = self._STATUS_ICONS.get(status, 'NONE')
        status_prefix = self._STATUS_PREFIX.get(status, '')
        # Alert tint (red background) for terminal error states.
        if status in ('FAILED', 'CRASHED'):
            layout.alert = True
        split = layout.split(factor=0.10, align=True)
        try:
            split.label(icon=status_icon, text="")
        except Exception:
            split.label(icon='NONE', text="")
        inner = split.split(factor=0.22, align=True)
        inner.label(text=str(job_number))
        inner.label(text=status_prefix + job_name)

    def draw_filter(self, context, layout):
        pass  # suppress the filter / sort bar


class SmokeSettings(bpy.types.PropertyGroup):
    """
    All user-facing settings for BatchSimLab, stored on bpy.types.Scene.

    Storing settings on the Scene means they are saved with the .blend file
    and persist across Blender sessions.  Each iterable parameter follows
    the same naming pattern:

        <name>           — default/base value
        <name>_begin     — range start
        <name>_end       — range end
        <name>_step      — range step
        <name>_use_range — enable range mode (mutually exclusive with list)
        <name>_use_list  — enable list mode  (mutually exclusive with range)
        <name>_list      — CollectionProperty of ValueItem
        <name>_index     — active item index in the UIList
    """

    # ── Scene setup ──────────────────────────────────────────────────────────

    domain_obj: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Domain Object",
        description=(
            "The Mantaflow fluid domain object to bake.  "
            "v0.7.0 TODO-40: selecting a domain auto-imports its current "
            "settings into the addon's baseline (_begin) values — sweep "
            "config (range/list/step) is preserved"
        ),
        update=_import_domain_params,
    )

    output_path: bpy.props.StringProperty(
        name="Output",
        description="Root output folder for cache, renders, and CSV.  Defaults "
                    "to the current .blend file's folder on load; change it to "
                    "any folder (e.g. a fast local scratch disk)",
        subtype='DIR_PATH',
        # Empty until a .blend loads; _reset_on_load fills it with the blend's
        # own folder (resolved absolute — see _default_output_path).  Replaces the
        # old hard-coded "C:/tmp"; a Python StringProperty can't store the literal
        # "//" token (Blender 5.x warns "does not support blend relative // prefix"),
        # so we store the resolved path instead.
        default="",
    )

    # ── Iteration mode ───────────────────────────────────────────────────────

    iteration_mode: bpy.props.EnumProperty(
        name="Iteration Mode",
        description=(
            "Limited Combinations: vary one parameter at a time while all "
            "others stay at their default value.  Produces far fewer jobs "
            "than All Combinations.\n\n"
            "All Combinations: full Cartesian product of all ranges.  Job "
            "count = product of all range lengths."
        ),
        items=[
            ('LIMITED', "Limited Combinations",
             "Vary one parameter at a time; all others at default"),
            ('ALL',     "All Combinations",
             "Full Cartesian product — can produce very many jobs"),
        ],
        default='LIMITED',
    )

    # ── Simulation Parameters outer collapse ─────────────────────────────────

    show_sim_params: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the entire Simulation Parameters section",
    )

    # ── v0.9.0 TODO-55: per-emitter sweep settings ───────────────────────────
    # One EmitterSettings element per flow object discovered inside the domain
    # (see find_emitters / _populate_emitters).  Populated on domain-select and
    # via the Refresh Emitters button; each gets its own collapsible UI section.
    emitters:        bpy.props.CollectionProperty(type=EmitterSettings)
    emitters_index:  bpy.props.IntProperty()
    show_emitters:   bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Emitters section",
    )

    # ── Frame range ───────────────────────────────────────────────────────────

    use_default_frames: bpy.props.BoolProperty(
        name="Use Default Frames",
        default=True,
        description="Use the .blend scene frame range; uncheck to override",
        update=_sync_frame_defaults,
    )
    sim_frame_start: bpy.props.IntProperty(
        name="Frame Start",
        default=1, min=1,
        description="First frame to bake and render",
    )
    sim_frame_end: bpy.props.IntProperty(
        name="Frame End",
        default=250, min=1,
        description="Last frame to bake and render",
    )

    # ── Settings save/load ────────────────────────────────────────────────────

    settings_file_path:   bpy.props.StringProperty(default="")
    settings_search_path: bpy.props.StringProperty(default="")
    settings_snapshot:    bpy.props.StringProperty(default="")
    settings_file_enum:   bpy.props.EnumProperty(
        name="Preset",
        items=_settings_files_enum_items,
        update=_on_settings_enum_update,
    )

    # ── Resolution ───────────────────────────────────────────────────────────

    show_resolution: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Resolution section",
    )
    resolution_begin:     bpy.props.IntProperty(default=64, min=8)
    resolution_end:       bpy.props.IntProperty(default=64, min=8)
    resolution_step:      bpy.props.IntProperty(default=0)
    resolution_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("resolution"))
    resolution_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("resolution"))
    resolution_list:      bpy.props.CollectionProperty(type=ValueItem)
    resolution_index:     bpy.props.IntProperty()

    # ── Gas Parameters ───────────────────────────────────────────────────────

    show_gas: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Gas Parameters section",
    )



    # Alpha — d.alpha — buoyancy based on smoke density
    alpha_begin:     bpy.props.FloatProperty(default=1.0)
    alpha_end:       bpy.props.FloatProperty(default=1.0)
    alpha_step:      bpy.props.FloatProperty(default=0)
    alpha_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("alpha"))
    alpha_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("alpha"))
    alpha_list:      bpy.props.CollectionProperty(type=ValueItem)
    alpha_index:     bpy.props.IntProperty()

    # Beta — d.beta — buoyancy based on smoke heat/temperature
    beta_begin:     bpy.props.FloatProperty(default=1.0)
    beta_end:       bpy.props.FloatProperty(default=1.0)
    beta_step:      bpy.props.FloatProperty(default=0)
    beta_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("beta"))
    beta_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("beta"))
    beta_list:      bpy.props.CollectionProperty(type=ValueItem)
    beta_index:     bpy.props.IntProperty()

    # Vorticity — d.vorticity — adds turbulent detail to smoke
    vorticity_begin:     bpy.props.FloatProperty(default=0.0)
    vorticity_end:       bpy.props.FloatProperty(default=0.0)
    vorticity_step:      bpy.props.FloatProperty(default=0)
    vorticity_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("vorticity"))
    vorticity_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("vorticity"))
    vorticity_list:      bpy.props.CollectionProperty(type=ValueItem)
    vorticity_index:     bpy.props.IntProperty()

    # ── Dissolve ─────────────────────────────────────────────────────────────

    show_dissolve: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Dissolve section",
    )
    use_dissolve: bpy.props.BoolProperty(
        default=False,
        description="Enable smoke dissolve (smoke fades out over time)",
    )
    iterate_dissolve_both: bpy.props.BoolProperty(
        name="Iterate Both On and Off",
        description=(
            "In addition to the Dissolve-enabled jobs, also generate one job "
            "with Dissolve disabled so you can compare with and without dissolve "
            "in the same batch"
        ),
        default=False,
    )
    slow_dissolve: bpy.props.BoolProperty(
        default=False,
        description="Use logarithmic (slow) dissolve instead of linear",
    )
    # v0.7.0 TODO-45: Iterate Slow Dissolve.  When checked, every job
    # produced by the dissolve sweep also gets a companion job with the
    # opposite slow_dissolve value (slow ↔ fast).  Mirrors the existing
    # iterate_dissolve_both pattern but at one more level of nesting
    # (the slow/fast axis within use_dissolve=True jobs).  Only
    # meaningful when use_dissolve is True (greyed out otherwise).
    iterate_slow_dissolve: bpy.props.BoolProperty(
        name="Iterate Slow Dissolve",
        description=(
            "For each dissolve job, also generate a companion job with "
            "the opposite Slow Dissolve setting (slow ↔ fast), so you can "
            "compare both dissolve modes in the same batch.  Only "
            "applies when Use Dissolve is enabled."
        ),
        default=False,
    )
    dissolve_speed_begin:     bpy.props.IntProperty(default=5)
    dissolve_speed_end:       bpy.props.IntProperty(default=5)
    dissolve_speed_step:      bpy.props.IntProperty(default=0)
    dissolve_speed_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("dissolve_speed"))
    dissolve_speed_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("dissolve_speed"))
    dissolve_speed_list:      bpy.props.CollectionProperty(type=ValueItem)
    dissolve_speed_index:     bpy.props.IntProperty()

    # ── Noise ────────────────────────────────────────────────────────────────

    show_noise: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Noise section",
    )
    use_noise: bpy.props.BoolProperty(
        default=False,
        description="Enable high-resolution noise for added smoke detail",
    )
    iterate_noise_both: bpy.props.BoolProperty(
        name="Iterate Both On and Off",
        description=(
            "In addition to the Noise-enabled jobs, also generate one job "
            "with Noise disabled so you can compare with and without noise "
            "in the same batch"
        ),
        default=False,
    )

    # Noise scale — d.noise_scale — upres factor
    noise_upres_begin:     bpy.props.IntProperty(default=2)
    noise_upres_end:       bpy.props.IntProperty(default=2)
    noise_upres_step:      bpy.props.IntProperty(default=0)
    noise_upres_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_upres"))
    noise_upres_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_upres"))
    noise_upres_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_upres_index:     bpy.props.IntProperty()

    # Noise strength — d.noise_strength
    noise_strength_begin:     bpy.props.FloatProperty(default=2.0)
    noise_strength_end:       bpy.props.FloatProperty(default=2.0)
    noise_strength_step:      bpy.props.FloatProperty(default=0)
    noise_strength_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_strength"))
    noise_strength_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_strength"))
    noise_strength_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_strength_index:     bpy.props.IntProperty()

    # Noise position scale — d.noise_pos_scale
    noise_spatial_scale_begin:     bpy.props.FloatProperty(default=2.0)
    noise_spatial_scale_end:       bpy.props.FloatProperty(default=2.0)
    noise_spatial_scale_step:      bpy.props.FloatProperty(default=0)
    noise_spatial_scale_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_spatial_scale"))
    noise_spatial_scale_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_spatial_scale"))
    noise_spatial_scale_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_spatial_scale_index:     bpy.props.IntProperty()

    # ── Time + Adaptive Timesteps  (v0.7.0 TODO-41) ──────────────────────────
    # Time Scale — d.time_scale (always-on global sim speed multiplier).
    # Adaptive Timesteps + CFL + Timesteps Max/Min — Blender's adaptive
    # timestep system; CFL/max/min only matter when adaptive is on.
    show_time: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Time / Adaptive Timesteps section",
    )
    use_adaptive_timesteps: bpy.props.BoolProperty(
        name="Adaptive Time Step",
        description=(
            "Enable Blender's adaptive timestepping (uses CFL Number, "
            "Timesteps Max, Timesteps Min).  When off, simulation runs "
            "at a fixed substep count"
        ),
        default=True,
    )

    # time_scale — d.time_scale
    time_scale_begin:     bpy.props.FloatProperty(default=1.0)
    time_scale_end:       bpy.props.FloatProperty(default=1.0)
    time_scale_step:      bpy.props.FloatProperty(default=0)
    time_scale_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("time_scale"))
    time_scale_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("time_scale"))
    time_scale_list:      bpy.props.CollectionProperty(type=ValueItem)
    time_scale_index:     bpy.props.IntProperty()

    # cfl_number — d.cfl_condition
    cfl_number_begin:     bpy.props.FloatProperty(default=4.0)
    cfl_number_end:       bpy.props.FloatProperty(default=4.0)
    cfl_number_step:      bpy.props.FloatProperty(default=0)
    cfl_number_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("cfl_number"))
    cfl_number_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("cfl_number"))
    cfl_number_list:      bpy.props.CollectionProperty(type=ValueItem)
    cfl_number_index:     bpy.props.IntProperty()

    # timesteps_max — d.timesteps_max
    timesteps_max_begin:     bpy.props.IntProperty(default=4)
    timesteps_max_end:       bpy.props.IntProperty(default=4)
    timesteps_max_step:      bpy.props.IntProperty(default=0)
    timesteps_max_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("timesteps_max"))
    timesteps_max_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("timesteps_max"))
    timesteps_max_list:      bpy.props.CollectionProperty(type=ValueItem)
    timesteps_max_index:     bpy.props.IntProperty()

    # timesteps_min — d.timesteps_min
    timesteps_min_begin:     bpy.props.IntProperty(default=1)
    timesteps_min_end:       bpy.props.IntProperty(default=1)
    timesteps_min_step:      bpy.props.IntProperty(default=0)
    timesteps_min_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("timesteps_min"))
    timesteps_min_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("timesteps_min"))
    timesteps_min_list:      bpy.props.CollectionProperty(type=ValueItem)
    timesteps_min_index:     bpy.props.IntProperty()

    # ── Fire Parameters  (v0.7.0 TODO-42) ────────────────────────────────────
    # Fire is enabled per-flow-object in the .blend; the addon's use_fire
    # checkbox controls whether the worker APPLIES the addon's fire-tuning
    # values to the domain (when off, the .blend's existing fire settings
    # are left untouched — same model as use_noise).
    show_fire: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Fire Parameters section",
    )
    use_fire: bpy.props.BoolProperty(
        name="Use Fire",
        description=(
            "When enabled, the addon writes its Fire Parameters into the "
            "domain.  When disabled, the .blend's existing fire settings "
            "are left as-is"
        ),
        default=False,
    )

    # burning_rate — d.burning_rate (UI label "Reaction Speed")
    burning_rate_begin:     bpy.props.FloatProperty(default=0.75)
    burning_rate_end:       bpy.props.FloatProperty(default=0.75)
    burning_rate_step:      bpy.props.FloatProperty(default=0)
    burning_rate_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("burning_rate"))
    burning_rate_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("burning_rate"))
    burning_rate_list:      bpy.props.CollectionProperty(type=ValueItem)
    burning_rate_index:     bpy.props.IntProperty()

    # flame_smoke — d.flame_smoke (UI label "Flames Smoke")
    flame_smoke_begin:     bpy.props.FloatProperty(default=1.0)
    flame_smoke_end:       bpy.props.FloatProperty(default=1.0)
    flame_smoke_step:      bpy.props.FloatProperty(default=0)
    flame_smoke_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_smoke"))
    flame_smoke_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_smoke"))
    flame_smoke_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_smoke_index:     bpy.props.IntProperty()

    # flame_vorticity — d.flame_vorticity (separate from gas vorticity!)
    flame_vorticity_begin:     bpy.props.FloatProperty(default=0.5)
    flame_vorticity_end:       bpy.props.FloatProperty(default=0.5)
    flame_vorticity_step:      bpy.props.FloatProperty(default=0)
    flame_vorticity_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_vorticity"))
    flame_vorticity_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_vorticity"))
    flame_vorticity_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_vorticity_index:     bpy.props.IntProperty()

    # flame_max_temp — d.flame_max_temp (UI label "Temp Max")
    flame_max_temp_begin:     bpy.props.FloatProperty(default=1.7)
    flame_max_temp_end:       bpy.props.FloatProperty(default=1.7)
    flame_max_temp_step:      bpy.props.FloatProperty(default=0)
    flame_max_temp_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_max_temp"))
    flame_max_temp_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_max_temp"))
    flame_max_temp_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_max_temp_index:     bpy.props.IntProperty()

    # flame_ignition — d.flame_ignition (UI label "Temp Min" / ignition temp)
    flame_ignition_begin:     bpy.props.FloatProperty(default=1.5)
    flame_ignition_end:       bpy.props.FloatProperty(default=1.5)
    flame_ignition_step:      bpy.props.FloatProperty(default=0)
    flame_ignition_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_ignition"))
    flame_ignition_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_ignition"))
    flame_ignition_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_ignition_index:     bpy.props.IntProperty()

    # ── Text object names for in-render parameter labels ─────────────────────

    show_text_objects: bpy.props.BoolProperty(
        default=False,
        description="Expand or collapse the Text Objects section",
    )
    text_resolution: bpy.props.StringProperty(
        default="Resolution_Text",
        description="Name of the FONT object in the scene that displays resolution",
    )
    text_noise: bpy.props.StringProperty(
        default="Noise_Text",
        description="Name of the FONT object that displays noise parameters",
    )
    text_dissolve: bpy.props.StringProperty(
        default="Dissolve_Text",
        description="Name of the FONT object that displays dissolve parameters",
    )
    text_time: bpy.props.StringProperty(
        default="Time_Text",
        description="Name of the FONT object that displays bake time",
    )

    # ── Render settings ──────────────────────────────────────────────────────

    render_mode: bpy.props.EnumProperty(
        name="Render Mode",
        description=(
            "Cycles GPU: reliable in --background mode, works without a display.\n"
            "EEVEE: faster but requires a visible Blender window (windowed mode)."
        ),
        items=[
            ('CYCLES', "Cycles GPU",
             "Reliable in background mode; uses OptiX/CUDA if available"),
            ('EEVEE',  "EEVEE",
             "Faster renders but requires windowed mode (no --background)"),
        ],
        default='CYCLES',
    )

    render_samples: bpy.props.IntProperty(
        name="Render Samples",
        description=(
            "Number of render samples for both the animation frame sequence "
            "and the final still frame. Cycles only — EEVEE ignores this."
        ),
        default=16,
        min=1,
        max=4096,
    )

    show_setup: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Setup section",
    )

    # ── Density ───────────────────────────────────────────────────────────────

    maintain_density: bpy.props.BoolProperty(
        name="Maintain Consistent Density",
        description=(
            "Scale emitter fluid density proportionally to keep visual density "
            "consistent as resolution changes. "
            "Formula: density = base_density × (job_resolution / default_resolution)"
        ),
        default=True,
    )

    use_placeholders: bpy.props.BoolProperty(
        name="Use Placeholders",
        description=(
            "If a rendered frame PNG already exists it will not be re-rendered, "
            "saving time when resuming an interrupted batch. "
            "Enabling this also forces Use Existing Cache on"
        ),
        default=False,
        update=lambda self, _ctx: setattr(self, "use_existing_cache", True)
                                  if self.use_placeholders else None,
    )

    use_existing_cache: bpy.props.BoolProperty(
        name="Use Existing Cache",
        description=(
            "Skip baking frames whose cache files are already present on disk. "
            "Automatically enabled when Use Placeholders is on. "
            "Auto-retry always uses existing cache"
        ),
        default=False,
    )

    auto_retry_failed: bpy.props.BoolProperty(
        name="Automatically Retry Failed Jobs",
        description=(
            "After all jobs finish, automatically re-run any that reported errors, "
            "with Use Existing Cache and Use Placeholders both forced on. "
            "Repeats up to 3 times per batch, re-running only the still-failing jobs"
        ),
        default=False,
    )
    render_simulation_result: bpy.props.BoolProperty(
        name="Render Simulation Result",
        description=(
            "When enabled, each job renders an MP4 animation and a final still "
            "PNG after baking. Disable for a bake-only batch — validate the "
            "simulation cache first, or render later by hand with other settings"
        ),
        default=True,
        update=_on_render_sim_result_update,
    )
    render_animation: bpy.props.BoolProperty(
        name="Render Animation",
        description=(
            "When enabled, render the full PNG sequence and mux it to MP4. "
            "Disable to render only the final still PNG (skips the per-frame "
            "render pass — useful when you only need the result image). "
            "Has no effect when Render Simulation Result is off"
        ),
        default=True,
    )

    # ── Output (collapsible: Iteration Mode + render settings + Run Batch) ────
    # v0.7.0 TODO-44: groups Iteration Mode through Run Batch into a single
    # collapsible section so the panel doesn't sprawl as v0.7.0 / v0.8.0
    # add more rows.
    show_output: bpy.props.BoolProperty(
        name="Output",
        default=True,
        description="Expand or collapse the Output section "
                    "(Iteration Mode, render settings, Export/Run Batch)",
    )

    # ── Progress (collapsible: progress bars + Job Log + summary) ─────────────
    # v0.7.0 TODO-44: groups all in-flight / post-batch display into one
    # collapsible.  Force-opens whenever a batch is running or a post-batch
    # summary is visible — the draw code overrides the user's collapse state
    # in those cases so they can't accidentally hide active progress.
    show_progress: bpy.props.BoolProperty(
        name="Progress",
        default=True,
        description="Expand or collapse the Progress section "
                    "(force-opened while a batch is running)",
    )

    # ── Job Log ───────────────────────────────────────────────────────────────

    show_job_log: bpy.props.BoolProperty(
        name="Job Log",
        default=False,
        description="Expand or collapse the Job Log section",
    )
    job_log_items:       bpy.props.CollectionProperty(type=SmokeJobItem)
    job_log_index:       bpy.props.IntProperty(default=0)
    job_log_auto_scroll: bpy.props.BoolProperty(default=True)

    # ── Utilities ─────────────────────────────────────────────────────────────

    show_utilities: bpy.props.BoolProperty(
        default=False,
        description="Expand or collapse the Utilities section",
    )
    collect_crash_logs: bpy.props.BoolProperty(
        name="Collect Crash Logs",
        description=(
            "Append each Blender crash log to crash_log.txt in the output folder. "
            "When unchecked, crash detection still stops the job but no log is written"
        ),
        default=False,
    )
    collect_estimation_data: bpy.props.BoolProperty(
        name="Collect Estimation Data",
        description=(
            "Write estim_log.jsonl (timing estimates vs actuals) and perf_log.json "
            "(per-job bake/render rates). Disable when not actively calibrating estimates"
        ),
        default=False,
    )
    collect_debug_log: bpy.props.BoolProperty(
        name="Collect Debug Log",
        description=(
            "Write verbose diagnostic info to debug_log.txt in the output folder. "
            "Enable when investigating problems on a new machine. "
            "Nothing is written unless this checkbox is checked"
        ),
        default=False,
    )

    # ── Batch run status ─────────────────────────────────────────────────────

    batch_progress:       bpy.props.StringProperty(default="")
    batch_total:          bpy.props.IntProperty(default=0)
    batch_jobs_dir:       bpy.props.StringProperty(default="")
    batch_overall_factor: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    batch_subtask_text:   bpy.props.StringProperty(default="")
    batch_subtask_factor: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    batch_job_text:       bpy.props.StringProperty(default="")
    batch_job_factor:     bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    batch_summary_line1:  bpy.props.StringProperty(default="")
    batch_summary_line2:  bpy.props.StringProperty(default="")
    batch_summary_line3:  bpy.props.StringProperty(default="")
    batch_summary_line4:  bpy.props.StringProperty(default="")
    # Per-stage absolute Unix timestamps live in _batch_times (module-level
    # dict), not RNA — bpy.props.FloatProperty is single-precision and loses
    # ~64 sec of precision on a 10-digit Unix epoch, producing negative deltas
    # in estim_log.  See _bt / _bt_set helpers.
    batch_time_remaining: bpy.props.StringProperty(default="")
    batch_job_log_key:    bpy.props.StringProperty(default="")
    batch_frame_end:      bpy.props.IntProperty(default=0)
    batch_jobs_elapsed:      bpy.props.FloatProperty(default=0.0)
    batch_resolution:        bpy.props.IntProperty(default=0)
    batch_render_width:      bpy.props.IntProperty(default=0)
    batch_render_height:     bpy.props.IntProperty(default=0)
    batch_render_mode:       bpy.props.StringProperty(default="CYCLES")
    batch_bake_secs_actual:  bpy.props.FloatProperty(default=-1.0)
    batch_bake_secs_actual:  bpy.props.FloatProperty(default=-1.0)
    batch_render_secs_actual: bpy.props.FloatProperty(default=-1.0)
    batch_bake_frame_baseline:   bpy.props.IntProperty(default=-1)
    batch_render_frame_baseline: bpy.props.IntProperty(default=-1)
    show_results:         bpy.props.BoolProperty(
        name="Display Results When Finished",
        description="After all jobs complete, create a grid of result planes in a SmokeOutput collection",
        default=False,
    )

    # ── Status / UI state ────────────────────────────────────────────────────

    export_mode: bpy.props.EnumProperty(
        name="Export Mode",
        description="Whether to replace all existing jobs or add new ones after them",
        items=[
            ('REPLACE', "Replace", "Clear all existing jobs and start fresh"),
            ('APPEND',  "Append",  "Add new jobs after the existing ones, keeping previous results"),
        ],
        default='REPLACE',
    )

    last_export_info: bpy.props.StringProperty(
        default="",
        description="Status message shown after the last Export Batch operation",
    )


# ---------------------------------------------------------------------------
# Addon preferences
# ---------------------------------------------------------------------------

class SmokeSimLabPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    resolution_caution: bpy.props.IntProperty(
        name="High-Resolution Caution Threshold",
        description="Show a caution popup when any resolution value exceeds this number",
        default=1024,
        min=8,
    )

    def draw(self, context):
        self.layout.prop(self, "resolution_caution")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class SMOKE_OT_export_batch(bpy.types.Operator):
    """Export .bat launcher, worker script copy, and per-job JSON files."""

    bl_idname = "smoke.export_batch"
    bl_label  = "Export Batch"
    bl_description = (
        "Write run_smoke_batch.bat, smoke_worker.py, and one JSON file per "
        "job to the output directory.  Double-click the .bat to run all jobs."
    )

    def invoke(self, context, event):
        s      = context.scene.smoke_settings
        prefs  = context.preferences.addons.get(__name__)
        limit  = prefs.preferences.resolution_caution if prefs else 1024
        # Cache both warning flags on the operator instance so draw() can pick
        # which sections to show (operator instance survives invoke→draw→execute
        # within a single call).
        self._warn_res = any(v > limit for v in expand_param(s, "resolution"))
        self._warn_cam = (s.render_simulation_result
                          and not _scene_has_camera(context.scene))
        if self._warn_res or self._warn_cam:
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        col = self.layout.column(align=True)
        # TODO-29: no-camera warning — surfaces here so the user can cancel
        # before exporting JSONs with render_simulation_result=True that would
        # produce black/empty renders.
        if getattr(self, "_warn_cam", False):
            col.label(text="No camera in scene — renders will be black/fail.",
                      icon='ERROR')
            col.label(text="(Add a camera, or uncheck 'Render Simulation Result')")
            col.separator()
        if getattr(self, "_warn_res", False):
            prefs = context.preferences.addons.get(__name__)
            limit = prefs.preferences.resolution_caution if prefs else 1024
            col.label(text=f"Caution: Resolution exceeds {limit}.", icon='ERROR')
            col.label(text="High resolution values may lead to")
            col.label(text="excessively long bake times.")
            col.separator()
        col.label(text="Click OK to export anyway, or Cancel to go back.")

    def execute(self, context):
        s = context.scene.smoke_settings

        if not s.domain_obj:
            self.report({'ERROR'}, "No domain object selected")
            return {'CANCELLED'}

        if not bpy.data.filepath:
            self.report({'ERROR'}, "Please save the .blend file first")
            return {'CANCELLED'}

        # Validate all list values against known parameter bounds before export.
        violations = []
        for param in ITERABLE_PARAMS:
            if not getattr(s, param + "_use_list", False):
                continue
            lo, hi = _PARAM_BOUNDS.get(param, (None, None))
            for item in getattr(s, param + "_list"):
                v = item.value
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    bound_str = (
                        f"[{lo if lo is not None else '-'}, "
                        f"{hi if hi is not None else '-'}]"
                    )
                    violations.append(f"{param}={v:.4g} (valid range {bound_str})")
        if violations:
            self.report({'ERROR'},
                        "List values out of bounds — fix before exporting: "
                        + "; ".join(violations))
            return {'CANCELLED'}

        # Defensive: refuse to export 0 jobs even when called from script.
        # The UI button is disabled when count == 0, but the operator can
        # still be invoked via bpy.ops, and we shouldn't report success when
        # nothing was written. generate_jobs is cheap (pure Python iteration).
        if not any(True for _ in generate_jobs(s)):
            self.report({'ERROR'},
                        "Nothing to export — configure at least one parameter "
                        "or enable iterate-both.")
            return {'CANCELLED'}

        try:
            count, bat_path, dup_count = export_batch(context)
        except FileNotFoundError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        total = len(s.job_log_items)
        dup_note = f"  (removed {dup_count} duplicate(s))" if dup_count else ""
        if s.export_mode == 'APPEND':
            msg = f"Appended {count} job(s) — {total} total{dup_note}  [{bat_path}]"
        else:
            msg = f"Exported {count} job(s){dup_note} to {bat_path}"
        s.last_export_info = msg
        self.report({'INFO'}, msg)
        return {'FINISHED'}


def _next_list_value(vals, default, min_val=None, max_val=None):
    """Predict the next value to pre-fill when adding a list item.

    - 0 or 1 existing items  → return the parameter default.
    - 2+ existing items      → detect arithmetic or geometric progression;
                               fall back to the last value.
    Candidate values are clamped to [min_val, max_val] if provided; if the
    projected next value would exceed the bounds, the last value is repeated.
    """
    n = len(vals)
    if n <= 1:
        return float(default)

    def _clamp(v):
        if min_val is not None and v < min_val:
            return None   # out of bounds — signal caller to fall back
        if max_val is not None and v > max_val:
            return None
        return v

    # Arithmetic: all consecutive differences equal
    diffs = [vals[i + 1] - vals[i] for i in range(n - 1)]
    tol   = max(abs(diffs[0]) * 1e-4, 1e-9)
    if all(abs(d - diffs[0]) <= tol for d in diffs):
        candidate = _clamp(vals[-1] + diffs[0])
        if candidate is not None:
            return candidate

    # Geometric: all consecutive ratios equal (all same sign, non-zero)
    if all(v > 0 for v in vals) or all(v < 0 for v in vals):
        ratios = [vals[i + 1] / vals[i] for i in range(n - 1)]
        tol    = max(abs(ratios[0]) * 1e-4, 1e-9)
        if all(abs(r - ratios[0]) <= tol for r in ratios):
            candidate = _clamp(vals[-1] * ratios[0])
            if candidate is not None:
                return candidate

    # Fallback: clamped default is safer than repeating vals[-1] (which may
    # itself be out of bounds, e.g. a manually-entered 0 for resolution).
    fallback = float(default)
    if min_val is not None:
        fallback = max(fallback, min_val)
    if max_val is not None:
        fallback = min(fallback, max_val)
    return fallback


class SMOKE_OT_save_settings(bpy.types.Operator):
    """Save current Simulation Parameter settings to a .smokesettings file."""

    bl_idname    = "smoke.save_settings"
    bl_label     = "Save Preset"
    bl_options   = {'REGISTER'}

    filepath:    bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.smokesettings", options={'HIDDEN'})

    def invoke(self, context, _event):
        s = context.scene.smoke_settings
        if s.settings_file_path:
            self.filepath = bpy.path.abspath(s.settings_file_path)
        else:
            folder = bpy.path.abspath(s.output_path) if s.output_path else (s.settings_search_path or "")
            self.filepath = (folder.rstrip("/\\") + "/") if folder else ""
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        s    = context.scene.smoke_settings
        path = os.path.normpath(self.filepath)
        if not path.endswith(".smokesettings"):
            path += ".smokesettings"
        data = _settings_dict(s)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError as exc:
            self.report({'ERROR'}, f"Could not save: {exc}")
            return {'CANCELLED'}
        s.settings_file_path   = os.path.normpath(path)
        s.settings_search_path = os.path.dirname(os.path.normpath(path))
        s.settings_snapshot    = json.dumps(data, sort_keys=True)
        # Select the saved preset in the dropdown using the stem as identifier.
        stem = os.path.splitext(os.path.basename(path))[0]
        s.settings_file_enum   = stem
        self.report({'INFO'}, f"Saved preset to {os.path.basename(path)}")
        return {'FINISHED'}


class SMOKE_OT_load_settings(bpy.types.Operator):
    """Load Simulation Parameter settings from a .smokesettings file."""

    bl_idname    = "smoke.load_settings"
    bl_label     = "Load Preset"
    bl_options   = {'REGISTER'}

    filepath:    bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.smokesettings", options={'HIDDEN'})

    def invoke(self, context, _event):
        s = context.scene.smoke_settings
        folder = bpy.path.abspath(s.output_path) if s.output_path else (s.settings_search_path or "")
        self.filepath = folder
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        s = context.scene.smoke_settings
        _load_settings_from_path(s, self.filepath)
        if s.settings_file_path:
            stem = os.path.splitext(os.path.basename(s.settings_file_path))[0]
            s.settings_file_enum = stem
        return {'FINISHED'}


class SMOKE_OT_add_value(bpy.types.Operator):
    """Add a new entry to a parameter explicit-value list."""

    bl_idname  = "smoke.add_value"
    bl_label   = "Add Value"
    bl_options = {'REGISTER', 'UNDO'}

    param: bpy.props.StringProperty()

    def execute(self, context):
        s       = context.scene.smoke_settings
        lst     = getattr(s, self.param + "_list")
        default = float(getattr(s, self.param + "_begin"))

        # Read hard bounds — RNA first, hardcoded _PARAM_BOUNDS as fallback.
        # The fallback matters: if RNA returns None the pattern estimator can
        # suggest a value like -64 for resolution (min=8) without clamping.
        fb_min, fb_max = _PARAM_BOUNDS.get(self.param, (None, None))
        min_val, max_val = fb_min, fb_max
        try:
            rna_prop = bpy.types.SmokeSettings.bl_rna.properties[self.param]
            hmin, hmax = rna_prop.hard_min, rna_prop.hard_max
            if abs(hmin) < 1e30:
                min_val = float(hmin)
            if abs(hmax) < 1e30:
                max_val = float(hmax)
        except (KeyError, AttributeError):
            pass

        current           = [item.value for item in lst]
        new_item          = lst.add()
        new_item.value    = _next_list_value(current, default, min_val, max_val)
        new_item.min_bound = min_val if min_val is not None else 0.0
        new_item.max_bound = max_val if max_val is not None else 0.0
        setattr(s, self.param + "_index", len(lst) - 1)
        return {'FINISHED'}


class SMOKE_OT_remove_value(bpy.types.Operator):
    """Remove checked items from a parameter list, or the highlighted item if none are checked."""

    bl_idname  = "smoke.remove_value"
    bl_label   = "Remove Value"
    bl_options = {'REGISTER', 'UNDO'}

    param: bpy.props.StringProperty()

    def execute(self, context):
        s   = context.scene.smoke_settings
        lst = getattr(s, self.param + "_list")
        idx = getattr(s, self.param + "_index")

        marked = [i for i, item in enumerate(lst) if item.marked]
        if marked:
            for i in sorted(marked, reverse=True):
                lst.remove(i)
        elif len(lst) > 0:
            lst.remove(idx)

        setattr(s, self.param + "_index", max(min(idx, len(lst) - 1), 0))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# v0.9.0 TODO-55: per-emitter operators + the velocity-vector UIList.
#
# The domain-param add/remove ops above target s.<param>_list directly; emitter
# lists live on a collection element (s.emitters[i].<param>_list), so these
# variants carry an emitter_index alongside the param name.
# ---------------------------------------------------------------------------

class SMOKE_UL_velocity_list(bpy.types.UIList):
    """Initial-Velocity vectors: checkbox marks a row for deletion; the text
    field is the editable "x, y, z" value (tinted red when it can't be parsed)."""
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        row = layout.row(align=True)
        row.prop(item, "marked", text="")
        if _parse_velocity_vector(item.text) is None:
            row.alert = True   # malformed vector — visible cue to fix it
        row.prop(item, "text", text="", emboss=True)


class SMOKE_OT_refresh_emitters(bpy.types.Operator):
    """Re-scan the scene for flow objects inside the domain and update the
    per-emitter sections (keeps any in-progress sweep config)."""

    bl_idname  = "smoke.refresh_emitters"
    bl_label   = "Refresh Emitters"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.smoke_settings
        _populate_emitters(s, context.scene)
        self.report({'INFO'}, f"{len(s.emitters)} emitter(s) inside the domain")
        return {'FINISHED'}


def _emitter_of(context, emitter_index):
    """Return s.emitters[emitter_index] or None if out of range."""
    s = context.scene.smoke_settings
    if 0 <= emitter_index < len(s.emitters):
        return s.emitters[emitter_index]
    return None


class SMOKE_OT_add_emitter_value(bpy.types.Operator):
    """Add a value to a per-emitter scalar parameter's explicit-value list."""

    bl_idname  = "smoke.add_emitter_value"
    bl_label   = "Add Value"
    bl_options = {'REGISTER', 'UNDO'}

    emitter_index: bpy.props.IntProperty()
    param:         bpy.props.StringProperty()

    def execute(self, context):
        em = _emitter_of(context, self.emitter_index)
        if em is None:
            return {'CANCELLED'}
        lst      = getattr(em, self.param + "_list")
        default  = float(getattr(em, self.param + "_begin"))
        current  = [item.value for item in lst]
        new_item = lst.add()
        new_item.value = _next_list_value(current, default)
        setattr(em, self.param + "_index", len(lst) - 1)
        return {'FINISHED'}


class SMOKE_OT_remove_emitter_value(bpy.types.Operator):
    """Remove checked items (or the highlighted item) from a per-emitter list."""

    bl_idname  = "smoke.remove_emitter_value"
    bl_label   = "Remove Value"
    bl_options = {'REGISTER', 'UNDO'}

    emitter_index: bpy.props.IntProperty()
    param:         bpy.props.StringProperty()

    def execute(self, context):
        em = _emitter_of(context, self.emitter_index)
        if em is None:
            return {'CANCELLED'}
        lst = getattr(em, self.param + "_list")
        idx = getattr(em, self.param + "_index")
        marked = [i for i, item in enumerate(lst) if item.marked]
        if marked:
            for i in sorted(marked, reverse=True):
                lst.remove(i)
        elif len(lst) > 0:
            lst.remove(idx)
        setattr(em, self.param + "_index", max(min(idx, len(lst) - 1), 0))
        return {'FINISHED'}


class SMOKE_OT_add_emitter_velocity(bpy.types.Operator):
    """Add an Initial-Velocity vector entry (defaults to "0, 0, 0")."""

    bl_idname  = "smoke.add_emitter_velocity"
    bl_label   = "Add Velocity Vector"
    bl_options = {'REGISTER', 'UNDO'}

    emitter_index: bpy.props.IntProperty()

    def execute(self, context):
        em = _emitter_of(context, self.emitter_index)
        if em is None:
            return {'CANCELLED'}
        item = em.velocity_list.add()
        item.text = _format_velocity_vector(_VELOCITY_DEFAULT)
        em.velocity_index = len(em.velocity_list) - 1
        return {'FINISHED'}


class SMOKE_OT_remove_emitter_velocity(bpy.types.Operator):
    """Remove checked Initial-Velocity vectors (or the highlighted one)."""

    bl_idname  = "smoke.remove_emitter_velocity"
    bl_label   = "Remove Velocity Vector"
    bl_options = {'REGISTER', 'UNDO'}

    emitter_index: bpy.props.IntProperty()

    def execute(self, context):
        em = _emitter_of(context, self.emitter_index)
        if em is None:
            return {'CANCELLED'}
        lst = em.velocity_list
        idx = em.velocity_index
        marked = [i for i, item in enumerate(lst) if item.marked]
        if marked:
            for i in sorted(marked, reverse=True):
                lst.remove(i)
        elif len(lst) > 0:
            lst.remove(idx)
        em.velocity_index = max(min(idx, len(lst) - 1), 0)
        return {'FINISHED'}


# Four major sub-tasks: Setup, Baking, Animation, Still
# Each row: (log_keyword, bar-3a label, completed_subtasks_when_detected)
# completed_subtasks = number of major sub-tasks DONE when this keyword first appears.
#
# v0.5.5: two keywords updated to match the actual worker log output —
#   * "Baking..." → "Baking (" — v0.5.0 changed the worker's bake-start log
#     line to "Baking (MODULAR resume — bake_data)..." or
#     "Baking (MODULAR full — bake_data)..."; neither contains the literal
#     substring "Baking..." so the stage never advanced.  Result: FULL bakes
#     were stuck on "Clearing cache" (the previous match) for the whole bake.
#   * "Use Existing Cache enabled" → "Decision : SKIP BAKE" — the worker has
#     never logged the former (looks like an artifact from an older addon
#     version); SKIP BAKE jobs were stuck on "Starting" because no later
#     keyword matched.  The Decision line is reliably logged at the moment
#     the worker picks SKIP BAKE.
_STAGES = (
    ("Job started",                "Starting",              0),
    ("Setting up cache",           "Setting up cache",      0),
    ("Freeing previous cache",     "Clearing cache",        0),
    ("Decision : SKIP BAKE",       "Using existing cache",  2),  # setup + bake both done
    ("Baking (",                   "Baking simulation",     1),  # setup done
    # TODO-52: the worker logs "Baking noise (bake_noise)..." between bake_data()
    # and bake_noise().  Same completed-rank (1) as the data bake, so this is a
    # *label/sub-bar* refinement inside the single Baking macro-stage — Bar-3 band
    # math and _TOTAL_SUBTASKS are deliberately untouched (estimates aren't vital;
    # a full separate band is deferred — see TODO-52).  Detected by rightmost
    # position, so the label flips to "Baking noise" once that line appears.
    # "Baking noise (" does NOT contain the substring "Baking (", so the data
    # keyword never false-matches the noise line.
    ("Baking noise (",             "Baking noise",          1),  # data bake done, noise running
    ("Bake complete",              "Verifying cache",       2),  # baking done
    ("Rendering animation",        "Rendering animation",   2),
    ("frame sequence complete",    "Animation complete",    2),
    ("MP4 conversion complete",    "Encoding MP4",          2),
    ("Rendering final frame",      "Rendering still",       3),  # animation done
    ("PNG render complete",        "Still complete",        4),  # still done
    ("Done. Results",              "Writing results",       4),
)
_TOTAL_SUBTASKS = 4


_LOG_DONE_MARKERS = ("Done. Results", "Performance record written to perf_log")

def _find_running_log(jobs_dir):
    """Return (log_file, log_stem, tail) for the current active job, or None.

    A job is considered finished if its log tail contains a done marker, a
    .done file exists, OR a higher-numbered .done stem exists (sequential batch
    guarantee: if job_0005.done is present, job_0003 must already be complete).

    Two-pass search: retry logs are checked first using only retry done-stems.
    Mixing first-run and retry stems in one sorted pass causes false skips because
    _retry stems sort before later first-run stems alphabetically — for example,
    "job_0003_retry" < "job_0010", so a completed job_0010.done would incorrectly
    cause job_0003_retry.log to be skipped, and the poller would fall through to the
    stale first-run log instead, displaying frame counts from the wrong job.

    First-run pass also treats .crashed files as a "done" signal so that a crashed
    job whose launcher never wrote .done doesn't shadow all subsequent logs.
    """
    try:
        all_files = set(os.listdir(jobs_dir))
    except OSError:
        return None

    # _DONE_RE / _RETRY_DONE_RE match only the unphased completion sentinels
    # (the per-phase .bake.done / .render.done would over-fill these sets and
    # break the alphabetical sequential-skip check).
    done_stems       = {f[:-5]  for f in all_files if _DONE_RE.match(f)}
    retry_done_stems = {f[:-11] for f in all_files if _RETRY_DONE_RE.match(f)}

    def _read_candidate(log_file, seq_stems, *, crashed_done=False):
        log_stem = log_file[:-4]
        if log_stem + ".done" in all_files:
            return None
        if crashed_done and log_stem + ".crashed" in all_files:
            return None
        if any(s > log_stem for s in seq_stems):
            return None
        try:
            with open(os.path.join(jobs_dir, log_file), "r", errors="replace") as fh:
                tail = fh.read()[-4096:]
        except OSError:
            return None
        if any(marker in tail for marker in _LOG_DONE_MARKERS):
            return None
        return log_file, log_stem, tail

    # Sort candidate logs by MOST RECENT mtime first so the actively-written
    # log wins.  Single-pass: same result as reversed-alphabetical (later jobs
    # have higher numbers AND later mtimes).  Two-pass: critical — after the
    # bake pass touches every job's <stem>.log, reversed-alphabetical wrongly
    # picked the highest-numbered log even while the render pass was working
    # on an earlier one (observed v0.4.2 EEVEE test: Job 1 rendering frame 6,
    # poll reported Job 2 as active and parsed Job 2's stale "Verifying cache"
    # line into the subtask text).
    def _log_sort_key(f):
        # Most recent mtime first; ties broken by higher filename (matches the
        # old reversed-alphabetical fallback in the rare tied-mtime case).
        try:
            mt = os.path.getmtime(os.path.join(jobs_dir, f))
        except OSError:
            mt = 0.0
        return (mt, f)

    # Pass 1: retry logs — sequential check uses only retry done-stems so a
    # completed job_NNNN.done doesn't skip an active job_MMMM_retry.log.
    retry_logs = [f for f in all_files if f.endswith("_retry.log")]
    retry_logs.sort(key=_log_sort_key, reverse=True)
    for log_file in retry_logs:
        result = _read_candidate(log_file, retry_done_stems)
        if result:
            return result

    # Pass 2: first-run logs — skip any log whose stem has a .crashed marker.
    first_logs = [f for f in all_files if f.endswith(".log") and "_retry" not in f]
    first_logs.sort(key=_log_sort_key, reverse=True)
    for log_file in first_logs:
        result = _read_candidate(log_file, done_stems, crashed_done=True)
        if result:
            return result

    return None


def _count_vdb_frames(jobs_dir, log_stem, tail=None, start_time=None, subdir="data"):
    """Return (frames_counted, frame_end) by counting VDB files for log_stem, or None.

    When tail is supplied the function tries to extract the effective cache dir
    from the v0.2.7+ "Effective cache dir" log line so it counts files in the
    directory the worker is actually baking into (which may differ from the
    current job's own cache dir when use_existing_cache resumes from another run).
    Falls back to Cache/<name>/<subdir>/ when the log line is absent.

    subdir selects the cache sub-directory to count: "data" (default) for the
    data bake, "noise" for the TODO-52 noise bake stage.  The frame-number regex
    is the same for both (Mantaflow writes fluid_data_NNNN.vdb / fluid_noise_NNNN.vdb).

    When start_time is provided (a time.time() value), only VDB files with an
    mtime >= start_time - 10.0 are counted.  This handles two failure modes:
    (1) Mantaflow re-bakes from frame 1 instead of resuming (overwriting the
        moved presave files — count stays constant, baseline subtraction produces 0)
    (2) Monitor Existing Jobs reconnects mid-bake (pre-existing files would inflate
        the baseline).
    Files moved from the presave directory keep their original mtime, so they are
    naturally excluded when start_time filtering is active.
    """
    json_path = os.path.join(jobs_dir, log_stem + ".json")
    try:
        with open(json_path) as fh:
            job_data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    frame_end   = job_data.get("frame_end", 0)
    output_path = job_data.get("output_path", "")
    name        = job_data.get("name", "")
    if not (output_path and name and frame_end):
        return None

    mtime_cutoff = (start_time - 10.0) if start_time else None

    def _vdb_count_from_dir(data_dir):
        counted = set()
        if not os.path.isdir(data_dir):
            return counted
        for f in os.listdir(data_dir):
            m = re.search(r'_(\d{4})\.vdb$', f)
            if not m:
                continue
            if mtime_cutoff is not None:
                try:
                    if os.path.getmtime(os.path.join(data_dir, f)) < mtime_cutoff:
                        continue
                except OSError:
                    pass
            counted.add(int(m.group(1)))
        return counted

    frames_baked = set()

    # Try to use the effective cache dir recorded in the log (v0.2.7+).
    if tail:
        _m = re.search(r'Effective cache dir\s+:\s+(.+)', tail)
        if _m:
            _eff = _m.group(1).strip()
            _data = os.path.join(_eff, subdir)
            if os.path.isdir(_data):
                frames_baked = _vdb_count_from_dir(_data)
                return len(frames_baked), frame_end

    # Fall back: look in this job's own cache dir.
    frames_baked = _vdb_count_from_dir(os.path.join(output_path, "Cache", name, subdir))
    return len(frames_baked), frame_end


def _count_png_frames(jobs_dir, log_stem, start_time=None):
    """Return (frames_rendered, frame_end) for log_stem, or None.

    When start_time is provided (a time.time() value), only PNGs with an mtime
    >= start_time - 10.0 are counted.  This correctly handles re-render runs
    where PNGs are overwritten rather than added — the total file count stays
    constant so the baseline approach shows "0 of N" forever.  Pass
    start_time=_bt("job_start_time") for the per-poll progress call; omit it
    (or pass None) for the baseline-setting call at render-stage entry.
    """
    json_path = os.path.join(jobs_dir, log_stem + ".json")
    try:
        with open(json_path) as fh:
            job_data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    frame_end   = job_data.get("frame_end", 0)
    output_path = job_data.get("output_path", "")
    name        = job_data.get("name", "")
    if not (output_path and name and frame_end):
        return None
    frames_dir = os.path.join(output_path, "Renders", f"{name}_frames")
    count = 0
    if os.path.isdir(frames_dir):
        mtime_cutoff = (start_time - 10.0) if start_time else None
        for f in os.listdir(frames_dir):
            if re.match(r'frame_\d{4}\.png$', f):
                if mtime_cutoff is not None:
                    try:
                        if os.path.getmtime(os.path.join(frames_dir, f)) < mtime_cutoff:
                            continue
                    except OSError:
                        continue
                count += 1
    return count, frame_end


def _format_eta(seconds):
    """Return a human-readable time-remaining string."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"~{int(seconds)}s remaining"
    if seconds < 3600:
        return f"~{int(seconds / 60)} min remaining"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"~{h}h {m}min remaining"


def _format_elapsed(secs):
    """Return elapsed wall-clock time as '1 h, 7 min', '27 min', or '45 sec'."""
    secs = int(max(0.0, secs))
    h    = secs // 3600
    m    = (secs % 3600) // 60
    s    = secs % 60
    if h > 0:
        return f"{h} h, {m} min"
    if m > 0:
        return f"{m} min"
    return f"{s} sec"


def _has_error(jobs_dir, fname):
    """Return True if fname in jobs_dir contains the word 'error'."""
    try:
        with open(os.path.join(jobs_dir, fname)) as fh:
            return "error" in fh.read().lower()
    except OSError:
        return False


def _update_job_log_statuses(s, jobs_dir):
    """Refresh each SmokeJobItem status and drive auto-scroll."""
    global _last_auto_index, _job_statuses

    # Detect manual scroll: if job_log_index moved since we last wrote it,
    # the user dragged the list — disable auto-scroll for this run.
    if s.job_log_auto_scroll and s.job_log_index != _last_auto_index:
        s.job_log_auto_scroll = False

    try:
        all_files = set(os.listdir(jobs_dir))
    except OSError:
        return

    # Two-pass pipeline: only ONE job's log is being written at any moment (the
    # .bat runs jobs sequentially within each pass).  Use _find_running_log to
    # pinpoint THAT job so all other in-flight jobs can be marked BAKED rather
    # than IN_PROGRESS (the v0.4.0Test bug was every job's <stem>.log existing
    # during the bake pass, so all jobs read as IN_PROGRESS at once).
    _running = _find_running_log(jobs_dir)
    _active_n = None
    if _running:
        _m = re.match(r"^job_(\d{4})(?:_retry)?\.log$", _running[0])
        if _m:
            _active_n = _m.group(1)

    active_index = -1
    for idx in range(len(s.job_log_items)):
        if idx >= len(_job_log_rows):
            break
        # Read job_number from module-level state, not from RNA (item.job_number
        # can return 0 when Blender re-evaluates SmokeSettings mid-timer-write).
        job_number       = _job_log_rows[idx][0]
        n                = f"{job_number - 1:04d}"   # job_number is 1-based; filenames are 0-based
        retry_done       = f"job_{n}_retry.done"
        first_done       = f"job_{n}.done"
        retry_log        = f"job_{n}_retry.log"
        first_log        = f"job_{n}.log"
        first_crashed    = f"job_{n}.crashed"
        bake_done_f      = f"job_{n}.bake.done"
        render_done_f    = f"job_{n}.render.done"
        bake_crashed_f   = f"job_{n}.bake.crashed"
        render_crashed_f = f"job_{n}.render.crashed"

        if retry_done in all_files:
            # Retry completed — it supersedes any first-run crash.
            _job_statuses[job_number] = 'FAILED' if _has_error(jobs_dir, retry_done) else 'COMPLETE'
        elif retry_log in all_files:
            _job_statuses[job_number] = 'RETRYING'
            if active_index < 0:
                active_index = idx
        elif first_done in all_files:
            # Final pass (render — or bake in bake-only mode) completed and wrote
            # the unphased <stem>.done alias.
            if first_crashed in all_files and _has_error(jobs_dir, first_done):
                _job_statuses[job_number] = 'CRASHED'
            else:
                _job_statuses[job_number] = 'FAILED' if _has_error(jobs_dir, first_done) else 'COMPLETE'
        elif render_done_f in all_files:
            # Render-phase sentinel present but the unphased alias is missing
            # (defensive — should be rare with the v0.4.1+ bat-block fix).
            if render_crashed_f in all_files and _has_error(jobs_dir, render_done_f):
                _job_statuses[job_number] = 'CRASHED'
            else:
                _job_statuses[job_number] = 'FAILED' if _has_error(jobs_dir, render_done_f) else 'COMPLETE'
        elif first_crashed in all_files or render_crashed_f in all_files or bake_crashed_f in all_files:
            # Any phase crashed (launcher wrote the .crashed marker).
            _job_statuses[job_number] = 'CRASHED'
        elif n == _active_n:
            # This is the one job whose log is being actively touched right now.
            # Distinguish bake-phase from render-phase activity by .bake.done:
            # if it exists, the bake completed and this job's render is running.
            if bake_done_f in all_files:
                _job_statuses[job_number] = 'RENDERING'
            else:
                _job_statuses[job_number] = 'IN_PROGRESS'
            if active_index < 0:
                active_index = idx
        elif bake_done_f in all_files:
            # Bake done, render phase hasn't started for this job yet → waiting.
            _job_statuses[job_number] = 'BAKED'
        elif first_log in all_files:
            # Log file exists but this job isn't the running one and has no
            # bake.done yet — fall back to IN_PROGRESS (queued / stale state).
            _job_statuses[job_number] = 'IN_PROGRESS'
            if active_index < 0:
                active_index = idx
        # else: no entry → draw_item defaults to NOT_STARTED

    if s.job_log_auto_scroll and active_index >= 0:
        s.job_log_index  = active_index
        _last_auto_index = active_index


def _compute_batch_summary(jobs_dir, elapsed_secs):
    """Scan done/crashed files and return (line1, line2, line3, line4) summary strings.

    line3 and line4 are empty strings when the respective counts are zero.
    Crashed (unexpected process crash) is reported separately from Failed
    (worker-controlled error exit).
    """
    try:
        all_files = os.listdir(jobs_dir)
    except OSError:
        all_files = []
    # Use exact regex matchers so the per-phase .bake.done / .render.done /
    # .bake.crashed / .render.crashed diagnostic files (also present in two-pass
    # mode) are NOT counted — only the unphased aliases that the .bat / launcher
    # write are treated as authoritative completion / crash markers.
    first_dones   = [f for f in all_files if _DONE_RE.match(f)]
    retry_dones   = [f for f in all_files if _RETRY_DONE_RE.match(f)]
    crashed_bases = {f[:-8] for f in all_files if _CRASHED_RE.match(f)}

    first_failed_stems = {f[:-5] for f in first_dones if _has_error(jobs_dir, f)}
    # Stems that eventually succeeded via retry
    retry_ok_bases   = {f[:-11] for f in retry_dones if not _has_error(jobs_dir, f)}
    retry_fail_bases = {f[:-11] for f in retry_dones if     _has_error(jobs_dir, f)}
    retry_ok   = len(retry_ok_bases)
    retry_fail = len(retry_fail_bases)

    # First-run failures not resolved by a successful retry
    unresolved = first_failed_stems - retry_ok_bases
    total_crashed = len(unresolved & crashed_bases)
    total_failed  = len(unresolved - crashed_bases) + retry_fail

    clean_complete = len(first_dones) - len(first_failed_stems)
    total_complete = clean_complete + retry_ok

    def _n(count, noun):
        return f"{count} {noun}{'s' if count != 1 else ''}"

    line1 = f"All Jobs Finished — {_format_elapsed(elapsed_secs)}"
    line2 = f"{_n(total_complete, 'Job')} Complete"
    # line3: crashed takes priority over "retried successfully"; both can't fit in 4 lines
    if total_crashed > 0:
        line3 = f"{_n(total_crashed, 'Job')} Crashed"
    elif retry_ok > 0:
        line3 = f"{_n(retry_ok, 'Job')} Error, but Retried Successfully"
    else:
        line3 = ""
    line4 = f"{_n(total_failed, 'Job')} Failed" if total_failed > 0 else ""
    return line1, line2, line3, line4


# ---------------------------------------------------------------------------
# Batch-stage Unix timestamps.  Module-level (Python float64) instead of
# bpy.props.FloatProperty: Blender FloatProperty is single-precision and a
# 10-digit Unix epoch rounds to the nearest ~128 sec grid point, so
# (now - stored) yields up to ±64 sec of noise — sometimes negative.  These
# values are transient (cleared by Run Batch and on file load) and never
# need to persist, so they live here in module state.
_batch_times: dict = {
    "start_time":        0.0,
    "job_start_time":    0.0,
    "bake_start_time":   0.0,
    "render_start_time": 0.0,
    "still_start_time":  0.0,
}

def _bt(key):
    """Read a batch timing value (full-precision Unix timestamp, 0.0 if unset)."""
    return _batch_times.get(key, 0.0)

def _bt_set(key, value):
    """Write a batch timing value (use time.time() values directly)."""
    _batch_times[key] = value

def _bt_reset_all():
    """Reset every batch timing value to 0.0 (called on file load / Run Batch)."""
    for k in _batch_times:
        _batch_times[k] = 0.0


# ---------------------------------------------------------------------------
# Job log display data — populated at export, never touched by the timer.
# draw_item reads from these dicts/lists instead of from CollectionProperty
# item fields, so no RNA read of item data happens during a draw pass that
# could return default values due to timer-triggered PropertyGroup re-eval.
_job_statuses:  dict = {}   # {job_number: status_string}  — written by timer
_job_log_rows:  list = []   # [(job_number, job_name), ...]  — written at export only

# ---------------------------------------------------------------------------
# Job log auto-scroll: last index the timer wrote, so we can detect manual scrolls.
_last_auto_index: int = 0

# Auto-retry: re-run failed jobs up to _MAX_AUTO_RETRIES times per batch.
# _auto_retry_count is the number of automatic retry rounds already launched for
# the current batch; reset to 0 on each fresh Run Batch.
_MAX_AUTO_RETRIES: int = 3
_auto_retry_count: int = 0


def _should_auto_retry(errors, enabled, retry_count, max_retries=_MAX_AUTO_RETRIES):
    """Return True if the completed run should trigger another auto-retry round.

    Fires while there are failures, the option is on, and we haven't yet used the
    per-batch retry budget.  (Replaces the old single-round `not is_retry_run`
    guard so failures get up to `max_retries` automatic re-runs.)
    """
    return bool(errors > 0 and enabled and retry_count < max_retries)

# Estimation log — append-only JSONL diagnostic file
# ---------------------------------------------------------------------------

_estim: dict = {
    "output_path":         "",
    "batch_logged":        False,
    "job_key":             "",
    "job_name":            "",
    "job_start_logged":    False,
    "est_bake_0":          0.0,    # initial model estimate saved at job_start
    "est_render_0":        0.0,
    "est_total_0":         0.0,
    "bake_start_logged":   False,
    "bake_rt_logged":      False,
    "bake_done_logged":    False,
    "render_start_logged": False,
    "render_rt_logged":    False,
    "render_done_logged":  False,
    "still_start_logged":  False,
    "still_done_logged":   False,
    "job_done_logged":     False,
}


def _debug_log(enabled: bool, output_path: str, component: str, msg: str) -> None:
    """Append one timestamped line to <output_path>/debug_log.txt.

    The gate is inside this function: nothing is written — and the file is
    never created — unless enabled is True.
    """
    if not enabled or not output_path:
        return
    ts   = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  [{component}]  {msg}"
    try:
        with open(os.path.join(output_path, "debug_log.txt"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _estim_log(record: dict) -> None:
    """Append one JSON record to <output_path>/estim_log.jsonl."""
    op = _estim["output_path"]
    if not op:
        return
    record.setdefault("ts", round(time.time(), 2))
    record.setdefault("addon_version", ADDON_VERSION)
    try:
        with open(os.path.join(op, "estim_log.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _estim_reset_job(log_key: str) -> None:
    """Clear per-job estimation state when a new job is detected."""
    _estim.update({
        "job_key":             log_key,
        "job_name":            "",
        "job_start_logged":    False,
        "est_bake_0":          0.0,
        "est_render_0":        0.0,
        "est_total_0":         0.0,
        "bake_start_logged":   False,
        "bake_rt_logged":      False,
        "bake_done_logged":    False,
        "render_start_logged": False,
        "render_rt_logged":    False,
        "render_done_logged":  False,
        "still_start_logged":  False,
        "still_done_logged":   False,
        "job_done_logged":     False,
    })


# Stale-log detection state: detect when the active job log stops updating.
# The launcher kills the process after 30 min of log silence; the poller warns
# at 35 min so the user sees a UI indicator even if the launcher also died.
_POLLER_STALE_SECS: float = 35 * 60
_poll_state: dict = {"log_key": "", "log_mtime": 0.0, "stale_since": 0.0}


def _poll_batch_progress():
    """
    Timer callback — updates overall and sub-task progress bars.
    Registered when Run Batch is clicked; unregisters itself when all jobs are done.
    Returns the next interval (seconds) or None to stop.
    Wrapped in try/except: an unhandled exception prints a warning and keeps
    the timer alive rather than silently killing it mid-batch.
    """
    try:
        return _poll_batch_progress_impl()
    except Exception as _exc:
        print(f"[BatchSimLab] poll timer error — {_exc}")
        return 5.0


def _poll_batch_progress_impl():
    for scene in bpy.data.scenes:
        s = getattr(scene, "smoke_settings", None)
        if s is None or not s.batch_jobs_dir or s.batch_total == 0:
            continue

        jobs_dir = s.batch_jobs_dir
        if not os.path.isdir(jobs_dir):
            s.batch_progress = ""
            return None

        # Count only the unphased completion markers (the .bat writes
        # <stem>.done after the FINAL pass — render, or bake in bake-only mode).
        # Phased .bake.done / .render.done are diagnostic only and must NOT be
        # counted here or the poll would over-report completion.
        done_files = [f for f in os.listdir(jobs_dir)
                      if _DONE_RE.match(f) or _RETRY_DONE_RE.match(f)]
        done  = len(done_files)
        total = s.batch_total

        # v0.6.0 BUG-012: split done_files into successful vs failed so the
        # "(N done)" display in batch_progress reflects only successful jobs.
        # A .done file with "error" in its content is the .bat's record of a
        # nonzero exit code from the launcher (worker crashed / failed setup
        # / etc.).  These were previously lumped into the "done" count, so a
        # 10/11 batch with 1 failure displayed "10/11 done" — misleading.
        # Reading 10-30 small .done files on every 5s poll is negligible
        # (~10 ms total).
        done_success = 0
        done_failed  = 0
        for _df in done_files:
            try:
                with open(os.path.join(jobs_dir, _df), "r") as _fh:
                    if "error" in _fh.read().lower():
                        done_failed += 1
                    else:
                        done_success += 1
            except OSError:
                # File present but unreadable — count as done (avoid stalling
                # the batch-complete trigger) but not as success or failure.
                done_success += 0  # explicit no-op; bucket TBD if encountered

        _update_job_log_statuses(s, jobs_dir)

        # Estimation log: record batch start once per run.
        # Output path is only set when collect_estimation_data is on; _estim_log
        # is a no-op when output_path is empty, so all logging is suppressed.
        _op = os.path.dirname(jobs_dir)
        if not _estim["batch_logged"]:
            if s.collect_estimation_data:
                _estim["output_path"] = _op
            _estim["batch_logged"] = True
            _estim_log({
                "event": "batch_start", "jobs": total,
                "constants": {
                    "bake_rate":      _BAKE_RATE_PER_RES3_FRAME,
                    "render_cycles":  _RENDER_RATE_CYCLES_PER_PIXEL_FRAME,
                    "render_eevee":   _RENDER_RATE_EEVEE_PER_PIXEL_FRAME,
                    "setup_secs":     _SETUP_SECS_DEFAULT,
                    "still_secs":     _STILL_SECS_DEFAULT,
                },
            })

        if done >= total:
            errors = 0
            for df in done_files:
                try:
                    with open(os.path.join(jobs_dir, df), "r") as fh:
                        if "error" in fh.read().lower():
                            errors += 1
                except OSError:
                    pass

            # Auto-retry up to _MAX_AUTO_RETRIES rounds per batch (_auto_retry_count
            # tracks rounds already launched; reset on a fresh Run Batch).  Each
            # round re-runs only the jobs still reporting errors.
            will_auto_retry = _should_auto_retry(errors, s.auto_retry_failed, _auto_retry_count)

            # Estimation log: batch complete.
            _estim_log({
                "event":        "batch_complete",
                "jobs":         total,
                "errors":       errors,
                "elapsed_secs": round(time.time() - _bt("start_time"), 1),
            })
            _estim["batch_logged"] = False   # allow logging for any future run

            if will_auto_retry:
                s.batch_summary_line1 = s.batch_summary_line2 = ""
                s.batch_summary_line3 = s.batch_summary_line4 = ""
            else:
                elapsed = time.time() - _bt("start_time")
                l1, l2, l3, l4 = _compute_batch_summary(jobs_dir, elapsed)
                s.batch_summary_line1 = l1
                s.batch_summary_line2 = l2
                s.batch_summary_line3 = l3
                s.batch_summary_line4 = l4
            s.batch_progress       = ""
            s.batch_overall_factor = 0.0
            s.batch_subtask_text   = ""
            s.batch_subtask_factor = 0.0
            s.batch_job_text       = ""
            s.batch_job_factor     = 0.0
            s.batch_jobs_dir       = ""
            s.batch_time_remaining = ""
            s.batch_job_log_key       = ""
            _bt_set("job_start_time", 0.0)
            s.batch_frame_end         = 0
            s.batch_jobs_elapsed      = 0.0
            s.batch_resolution        = 0
            s.batch_render_width      = 0
            s.batch_render_height     = 0
            s.batch_render_mode       = "CYCLES"
            _bt_set("bake_start_time", 0.0)
            _bt_set("render_start_time", 0.0)
            _bt_set("still_start_time", 0.0)
            s.batch_bake_secs_actual  = -1.0
            s.batch_render_secs_actual = -1.0
            s.batch_bake_frame_baseline   = -1
            s.batch_render_frame_baseline = -1
            _redraw_panels()
            if will_auto_retry:
                bpy.app.timers.register(_auto_retry_deferred, first_interval=2.0)
            elif s.show_results:
                bpy.app.timers.register(_setup_results_deferred, first_interval=0.5)
            return None

        # Two-phase aware overall progress: count per-phase .bake.done /
        # .render.done so the bar advances during the bake pass (before any
        # unphased <stem>.done — written only after the FINAL pass — appears).
        # Bake-only mode (no render pass in this batch) collapses to N phases.
        #
        # v0.7.0 BUG-014: like the unphased done count (BUG-012, v0.6.0), the
        # phased counts must EXCLUDE failed jobs.  The .bat writes
        # `.bake.done` for both success (`done <stem>`) and failure
        # (`error exit N <stem>`); naive counting puts crashed bakes into
        # the "13/13 baked" total — user reported 2026-06-01 with a
        # 13-job batch where 1 bake crashed but Bake count still said 13/13.
        # Same content-read pattern as the BUG-012 fix below.
        _all_files = os.listdir(jobs_dir)
        def _count_phase_success(_re):
            n = 0
            for _f in _all_files:
                if not _re.match(_f):
                    continue
                try:
                    with open(os.path.join(jobs_dir, _f), "r") as _fh:
                        if "error" not in _fh.read().lower():
                            n += 1
                except OSError:
                    pass
            return n
        _bake_done_n   = _count_phase_success(_BAKE_DONE_RE)
        _render_done_n = _count_phase_success(_RENDER_DONE_RE)
        _bake_only     = not s.render_simulation_result
        # v0.6.0 BUG-012: show "(N done)" with successful count, and append
        # ", F failed" only when failed > 0 (avoids visual noise on clean runs).
        if done_failed > 0:
            _done_str = f"({done_success}/{total} done, {done_failed} failed)"
        else:
            _done_str = f"({done_success}/{total} done)"
        if _bake_only:
            s.batch_overall_factor = _bake_done_n / total if total else 0.0
            s.batch_progress       = f"Bake {_bake_done_n}/{total}  {_done_str}"
        else:
            s.batch_overall_factor = (_bake_done_n + _render_done_n) / (2 * total) if total else 0.0
            s.batch_progress       = (
                f"Bake {_bake_done_n}/{total}  Render {_render_done_n}/{total}  "
                f"{_done_str}"
            )

        running = _find_running_log(jobs_dir)
        if running:
            log_file, log_stem, tail = running
            now = time.time()

            # Stale-log detection: warn in UI if the active log hasn't been
            # written to for _POLLER_STALE_SECS.  The launcher's watchdog kills
            # the process at 30 min; 35 min here catches cases where the
            # launcher itself crashes without writing a .done/.crashed marker.
            _log_path = os.path.join(jobs_dir, log_file)
            try:
                _cur_mtime = os.path.getmtime(_log_path)
            except OSError:
                _cur_mtime = None
            if _cur_mtime is not None:
                if log_file != _poll_state["log_key"] or _cur_mtime != _poll_state["log_mtime"]:
                    _poll_state["log_key"]    = log_file
                    _poll_state["log_mtime"]  = _cur_mtime
                    _poll_state["stale_since"] = 0.0
                elif _poll_state["stale_since"] == 0.0:
                    _poll_state["stale_since"] = time.time()
                elif time.time() - _poll_state["stale_since"] >= _POLLER_STALE_SECS:
                    _idle_min = int((time.time() - _poll_state["stale_since"]) / 60)
                    s.batch_subtask_text = f"No log activity for {_idle_min} min — job may be frozen"
            else:
                _poll_state["log_key"]    = log_file
                _poll_state["stale_since"] = 0.0

            # Detect job transition — accumulate elapsed time for completed job,
            # then reset per-job timer and load frame count for the new job.
            if log_file != s.batch_job_log_key:
                # Estimation log: previous job complete (if there was one).
                if _estim["job_key"] and not _estim["job_done_logged"] and _bt("job_start_time") > 0:
                    _prev_elapsed = round(now - _bt("job_start_time"), 1)
                    _estim_log({
                        "event":        "job_complete",
                        "job":          _estim["job_name"],
                        "elapsed_secs": _prev_elapsed,
                        "est_total_0":  _estim["est_total_0"],
                        "ratio":        round(_prev_elapsed / _estim["est_total_0"], 3)
                                        if _estim["est_total_0"] > 0 else None,
                        "bake_actual":   round(s.batch_bake_secs_actual, 1)
                                         if s.batch_bake_secs_actual >= 0 else None,
                        "render_actual": round(s.batch_render_secs_actual, 1)
                                         if s.batch_render_secs_actual >= 0 else None,
                    })
                _estim_reset_job(log_file)

                if s.batch_job_log_key and _bt("job_start_time") > 0:
                    s.batch_jobs_elapsed += max(now - _bt("job_start_time"), 0.0)
                s.batch_job_log_key    = log_file
                _bt_set("job_start_time", now)
                json_path = os.path.join(jobs_dir, log_stem + ".json")
                try:
                    with open(json_path) as fh:
                        jd = json.load(fh)
                    s.batch_frame_end     = jd.get("frame_end", 0)
                    s.batch_resolution    = int(jd.get("params", {}).get("resolution", 0))
                    s.batch_render_width  = jd.get("render_resolution_x", 0)
                    s.batch_render_height = jd.get("render_resolution_y", 0)
                    s.batch_render_mode   = jd.get("render_mode", "CYCLES")
                    _estim["job_name"]    = jd.get("name", log_stem)
                except (OSError, json.JSONDecodeError):
                    s.batch_frame_end     = 0
                    s.batch_resolution    = 0
                    s.batch_render_width  = 0
                    s.batch_render_height = 0
                    s.batch_render_mode   = "CYCLES"
                _bt_set("bake_start_time", 0.0)
                _bt_set("render_start_time", 0.0)
                _bt_set("still_start_time", 0.0)
                s.batch_bake_secs_actual  = -1.0
                s.batch_render_secs_actual = -1.0
                s.batch_bake_frame_baseline   = -1
                s.batch_render_frame_baseline = -1
                s.batch_subtask_text      = ""
                s.batch_subtask_factor    = 0.0
                s.batch_job_text          = ""
                s.batch_job_factor        = 0.0

            frame_end      = max(s.batch_frame_end, 1)
            elapsed_in_job = max(now - _bt("job_start_time"), 0.0) if _bt("job_start_time") > 0 else 0.0

            # Determine current stage from log tail.
            # Use rightmost (most recently written) keyword match, not the
            # highest-completion-rank match.  reversed(_STAGES) would pick the
            # most advanced stage found anywhere in the tail, which is wrong
            # when an old run's later-stage lines still appear in the buffer.
            stage_label     = "Starting"
            stage_completed = 0
            best_pos        = -1
            for keyword, label, completed in _STAGES:
                pos = tail.rfind(keyword)
                if pos > best_pos:
                    best_pos        = pos
                    stage_label     = label
                    stage_completed = completed

            # Stage start time tracking (set once per job)
            if stage_label == "Baking simulation" and _bt("bake_start_time") == 0.0:
                _bt_set("bake_start_time", now)
                if s.batch_bake_frame_baseline < 0:
                    _bi = _count_vdb_frames(jobs_dir, log_stem, tail)
                    s.batch_bake_frame_baseline = _bi[0] if _bi else 0
                if not _estim["bake_start_logged"]:
                    _estim["bake_start_logged"] = True
                    _setup_actual = round(now - _bt("job_start_time"), 1) if _bt("job_start_time") > 0 else None
                    _estim_log({
                        "event":            "bake_start",
                        "job":              _estim["job_name"],
                        "est_bake_secs":    _estim["est_bake_0"],
                        "setup_actual_secs": _setup_actual,
                        "setup_est_secs":   _SETUP_SECS_DEFAULT,
                    })
            if stage_label == "Rendering animation" and _bt("render_start_time") == 0.0:
                _bt_set("render_start_time", now)
                if s.batch_render_frame_baseline < 0:
                    _ri = _count_png_frames(jobs_dir, log_stem)
                    s.batch_render_frame_baseline = _ri[0] if _ri else 0
                    _output_path = os.path.dirname(jobs_dir)
                    _debug_log(s.collect_debug_log, _output_path, "poller",
                               f"render baseline set: {s.batch_render_frame_baseline} "
                               f"existing PNGs; log_stem={log_stem!r} "
                               f"target={_ri[1] if _ri else '?'}")
                if not _estim["render_start_logged"]:
                    _estim["render_start_logged"] = True
                    _estim_log({
                        "event":           "render_start",
                        "job":             _estim["job_name"],
                        "est_render_secs": _estim["est_render_0"],
                        "bake_actual_secs": round(s.batch_bake_secs_actual, 1)
                                           if s.batch_bake_secs_actual >= 0 else None,
                    })
            if stage_label == "Rendering still" and _bt("still_start_time") == 0.0:
                _bt_set("still_start_time", now)
                if not _estim["still_start_logged"]:
                    _estim["still_start_logged"] = True
                    _estim_log({
                        "event":          "still_start",
                        "job":            _estim["job_name"],
                        "est_still_secs": _STILL_SECS_DEFAULT,
                    })

            # Stage completion: record actual duration once (guard with < 0)
            if s.batch_bake_secs_actual < 0:
                if stage_label == "Using existing cache":
                    s.batch_bake_secs_actual = 0.0
                    if not _estim["bake_done_logged"]:
                        _estim["bake_done_logged"] = True
                        _estim_log({
                            "event":       "bake_actual",
                            "job":         _estim["job_name"],
                            "actual_secs": 0.0,
                            "source":      "cache_skip",
                            "default_est": _estim["est_bake_0"],
                        })
                elif stage_completed >= 2 and _bt("bake_start_time") > 0:
                    s.batch_bake_secs_actual = now - _bt("bake_start_time")
                    if not _estim["bake_done_logged"]:
                        _estim["bake_done_logged"] = True
                        _res3 = s.batch_resolution ** 3 if s.batch_resolution > 0 else 0
                        _implied = (s.batch_bake_secs_actual / (_res3 * frame_end)
                                    if _res3 > 0 and frame_end > 0 else None)
                        _estim_log({
                            "event":         "bake_actual",
                            "job":           _estim["job_name"],
                            "actual_secs":   round(s.batch_bake_secs_actual, 1),
                            "default_est":   _estim["est_bake_0"],
                            "ratio":         round(s.batch_bake_secs_actual / _estim["est_bake_0"], 3)
                                             if _estim["est_bake_0"] > 0 else None,
                            "resolution":    s.batch_resolution,
                            "frames":        frame_end,
                            "implied_rate":  _implied,
                            "model_rate":    _BAKE_RATE_PER_RES3_FRAME,
                        })
            if (s.batch_render_secs_actual < 0
                    and stage_completed >= 3
                    and _bt("render_start_time") > 0):
                s.batch_render_secs_actual = now - _bt("render_start_time")
                if not _estim["render_done_logged"]:
                    _estim["render_done_logged"] = True
                    _render_px = s.batch_render_width * s.batch_render_height
                    _implied_r = (s.batch_render_secs_actual / (_render_px * frame_end)
                                  if _render_px > 0 and frame_end > 0 else None)
                    _estim_log({
                        "event":        "render_actual",
                        "job":          _estim["job_name"],
                        "actual_secs":  round(s.batch_render_secs_actual, 1),
                        "default_est":  _estim["est_render_0"],
                        "ratio":        round(s.batch_render_secs_actual / _estim["est_render_0"], 3)
                                        if _estim["est_render_0"] > 0 else None,
                        "render_px":    _render_px,
                        "frames":       frame_end,
                        "render_mode":  s.batch_render_mode,
                        "implied_rate": _implied_r,
                        "model_rate":   (_RENDER_RATE_EEVEE_PER_PIXEL_FRAME
                                         if s.batch_render_mode == "EEVEE"
                                         else _RENDER_RATE_CYCLES_PER_PIXEL_FRAME),
                    })

            # How many frames does THIS run need to render?
            # Parse "Rendering animation (N frame(s))" from the log so re-render
            # runs (where existing PNGs inflate the directory count) use the
            # correct denominator.  Falls back to frame_end if not yet logged.
            _rm = re.search(r'Rendering animation \((\d+) frame', tail)
            render_target = int(_rm.group(1)) if _rm else frame_end

            # --- Bar 1: sub-task with real frame-level progress ---
            frames_baked    = 0
            frames_rendered = 0
            subtask_text   = stage_label
            subtask_factor = min((stage_completed + 0.5) / _TOTAL_SUBTASKS, 1.0)

            if stage_label == "Baking simulation":
                # Pass bake_start_time so only VDB files written this session are
                # counted.  This handles Mantaflow re-baking from frame 1 (it
                # overwrites the moved presave files rather than resuming): since
                # moved files keep their original mtime, mtime filtering naturally
                # excludes them and counts only freshly written frames.
                _bake_st  = _bt("bake_start_time") if _bt("bake_start_time") > 0 else None
                bake_info = _count_vdb_frames(jobs_dir, log_stem, tail,
                                              start_time=_bake_st)
                if bake_info:
                    baked_new, total_frames = bake_info   # baked_new = mtime-filtered
                    bake_baseline = max(s.batch_bake_frame_baseline, 0)
                    to_bake       = max(total_frames - bake_baseline, 1)
                    # If Mantaflow is doing a full rebake, mtime count will exceed
                    # the expected remaining frames — expand denominator to total.
                    if baked_new > to_bake:
                        to_bake = total_frames
                    frames_baked  = baked_new
                    if to_bake > 0:
                        subtask_text   = f"Baking ({baked_new} of {to_bake})"
                        subtask_factor = min(baked_new / to_bake, 1.0)

            elif stage_label == "Baking noise":
                # TODO-52: data bake is done; count the noise/ subdir so the bar
                # keeps moving (and a hung noise bake shows "Baking noise (0 of N)"
                # instead of a frozen data bar at full).  No mtime filtering: the
                # noise/ dir is freshly written this session (data resume reuses
                # data/, never noise/), so a plain count is the live progress.
                noise_info = _count_vdb_frames(jobs_dir, log_stem, tail,
                                               subdir="noise")
                if noise_info:
                    noise_baked, total_frames = noise_info
                    if total_frames > 0:
                        frames_baked   = noise_baked
                        subtask_text   = f"Baking noise ({noise_baked} of {total_frames})"
                        subtask_factor = min(noise_baked / total_frames, 1.0)

            elif stage_label == "Rendering animation":
                _start_t    = _bt("job_start_time") if _bt("job_start_time") > 0 else None
                render_info = _count_png_frames(jobs_dir, log_stem, start_time=_start_t)
                if render_info:
                    raw_rendered, _ = render_info
                    if _start_t is not None:
                        # mtime-filtered: count is "new only" — baseline subtraction not needed
                        rendered_new = raw_rendered
                        _debug_log(s.collect_debug_log, os.path.dirname(jobs_dir), "poller",
                                   f"render poll (mtime): raw={raw_rendered} "
                                   f"new={rendered_new} target={render_target}")
                    else:
                        render_baseline = max(s.batch_render_frame_baseline, 0)
                        rendered_new    = max(raw_rendered - render_baseline, 0)
                        _debug_log(s.collect_debug_log, os.path.dirname(jobs_dir), "poller",
                                   f"render poll: raw={raw_rendered} baseline={render_baseline} "
                                   f"new={rendered_new} target={render_target}")
                    if render_target > 0:
                        frames_rendered = min(rendered_new, render_target)
                        subtask_text   = f"Rendering ({frames_rendered} of {render_target})"
                        subtask_factor = frames_rendered / render_target

            s.batch_subtask_text   = subtask_text
            s.batch_subtask_factor = subtask_factor

            # --- Bar 2: per-stage time estimate (actual → real-time rate → default) ---

            # Derive default bake/render seconds from resolution and render dims.
            batch_res    = max(s.batch_resolution, 64)
            render_px    = s.batch_render_width * s.batch_render_height
            render_rate  = (_RENDER_RATE_EEVEE_PER_PIXEL_FRAME
                            if s.batch_render_mode == "EEVEE"
                            else _RENDER_RATE_CYCLES_PER_PIXEL_FRAME)
            default_bake_secs   = (_BAKE_RATE_PER_RES3_FRAME * (batch_res ** 3) * frame_end
                                   if s.batch_resolution > 0
                                   else _BAKE_RATE_DEFAULT * frame_end)
            default_render_secs = (render_rate * render_px * frame_end
                                   if render_px > 0
                                   else _RENDER_RATE_DEFAULT * frame_end)

            # Estimation log: job_start (once — first poll where estimates are ready)
            if not _estim["job_start_logged"] and _estim["job_key"] == log_file:
                _estim["job_start_logged"] = True
                _djs = _SETUP_SECS_DEFAULT + default_bake_secs + default_render_secs + _STILL_SECS_DEFAULT
                _estim["est_bake_0"]   = round(default_bake_secs,   1)
                _estim["est_render_0"] = round(default_render_secs, 1)
                _estim["est_total_0"]  = round(_djs, 1)
                _estim_log({
                    "event":       "job_start",
                    "job":         _estim["job_name"],
                    "resolution":  s.batch_resolution,
                    "frames":      frame_end,
                    "render_px":   s.batch_render_width * s.batch_render_height,
                    "render_mode": s.batch_render_mode,
                    "est_setup":   _SETUP_SECS_DEFAULT,
                    "est_bake":    round(default_bake_secs,   1),
                    "est_render":  round(default_render_secs, 1),
                    "est_still":   _STILL_SECS_DEFAULT,
                    "est_total":   round(_djs, 1),
                })

            # Setup: done once any timed stage has started or bake was skipped
            if (_bt("bake_start_time") > 0 or s.batch_bake_secs_actual >= 0
                    or _bt("render_start_time") > 0 or _bt("still_start_time") > 0):
                setup_remaining = 0.0
            else:
                setup_remaining = max(_SETUP_SECS_DEFAULT - elapsed_in_job, 0.0)

            # Bake: actual → real-time rate estimate → default
            if s.batch_bake_secs_actual >= 0:
                bake_remaining = 0.0
            elif _bt("bake_start_time") > 0 and frames_baked > 0:
                elapsed_bake = max(now - _bt("bake_start_time"), 0.0)
                if elapsed_bake > 0:
                    _bake_baseline = max(s.batch_bake_frame_baseline, 0)
                    _to_bake_total = max(frame_end - _bake_baseline, 1)
                    # Full-rebake detected: mtime count exceeded expected remaining.
                    if frames_baked > _to_bake_total:
                        _to_bake_total = frame_end
                    bake_to_go     = max(_to_bake_total - frames_baked, 0)
                    rate           = elapsed_bake / frames_baked
                    bake_remaining = rate * bake_to_go
                    if not _estim["bake_rt_logged"]:
                        _estim["bake_rt_logged"] = True
                        _estim_log({
                            "event":              "bake_rt",
                            "job":                _estim["job_name"],
                            "frames_baked":       frames_baked,
                            "total_frames":       frame_end,
                            "elapsed_bake_secs":  round(elapsed_bake, 1),
                            "rate_secs_per_frame": round(rate, 4),
                            "est_remaining_secs": round(bake_remaining, 1),
                            "est_total_rt_secs":  round(elapsed_bake + bake_remaining, 1),
                            "default_est_secs":   _estim["est_bake_0"],
                        })
                else:
                    bake_remaining = default_bake_secs
            else:
                # Scale the default estimate by the fraction of frames still to bake.
                # When resuming a partial bake (e.g. after Monitor or a crash+retry),
                # bake_baseline > 0 so only a fraction of the full default is needed.
                _bbl      = max(s.batch_bake_frame_baseline, 0)
                _to_go    = max(frame_end - _bbl, 1)
                bake_remaining = default_bake_secs * _to_go / max(frame_end, 1)

            # Render: actual → real-time rate → default.
            # Guard against frames_rendered == render_target (directory fully
            # pre-populated) while the stage is still active — use elapsed
            # time against the default estimate to avoid dropping to 0.
            if s.batch_render_secs_actual >= 0:
                render_remaining = 0.0
            elif _bt("render_start_time") > 0 and 0 < frames_rendered < render_target:
                elapsed_render = max(now - _bt("render_start_time"), 0.0)
                if elapsed_render > 0:
                    rate             = elapsed_render / frames_rendered
                    render_remaining = rate * max(render_target - frames_rendered, 0)
                    if not _estim["render_rt_logged"]:
                        _estim["render_rt_logged"] = True
                        _estim_log({
                            "event":                "render_rt",
                            "job":                  _estim["job_name"],
                            "frames_rendered":      frames_rendered,
                            "total_frames":         render_target,
                            "elapsed_render_secs":  round(elapsed_render, 1),
                            "rate_secs_per_frame":  round(rate, 4),
                            "est_remaining_secs":   round(render_remaining, 1),
                            "est_total_rt_secs":    round(elapsed_render + render_remaining, 1),
                            "default_est_secs":     _estim["est_render_0"],
                        })
                else:
                    render_remaining = max(default_render_secs - (
                        now - _bt("render_start_time")
                        if _bt("render_start_time") > 0 else 0.0), 0.0)
            else:
                render_remaining = max(default_render_secs - (
                    now - _bt("render_start_time")
                    if _bt("render_start_time") > 0 else 0.0), 0.0)

            # Still: done once stage_completed >= 4; countdown if started; else default
            if stage_completed >= 4:
                still_remaining = 0.0
                # Estimation log: still and job complete (once each).
                if not _estim["still_done_logged"] and _bt("still_start_time") > 0:
                    _estim["still_done_logged"] = True
                    _still_actual = round(now - _bt("still_start_time"), 1)
                    _estim_log({
                        "event":       "still_actual",
                        "job":         _estim["job_name"],
                        "actual_secs": _still_actual,
                        "est_secs":    _STILL_SECS_DEFAULT,
                        "ratio":       round(_still_actual / _STILL_SECS_DEFAULT, 3),
                    })
                if not _estim["job_done_logged"]:
                    _estim["job_done_logged"] = True
                    _job_elapsed = round(now - _bt("job_start_time"), 1) if _bt("job_start_time") > 0 else 0.0
                    _estim_log({
                        "event":         "job_complete",
                        "job":           _estim["job_name"],
                        "elapsed_secs":  _job_elapsed,
                        "est_total_0":   _estim["est_total_0"],
                        "ratio":         round(_job_elapsed / _estim["est_total_0"], 3)
                                         if _estim["est_total_0"] > 0 else None,
                        "bake_actual":   round(s.batch_bake_secs_actual, 1)
                                         if s.batch_bake_secs_actual >= 0 else None,
                        "render_actual": round(s.batch_render_secs_actual, 1)
                                         if s.batch_render_secs_actual >= 0 else None,
                    })
            elif _bt("still_start_time") > 0:
                still_remaining = max(
                    _STILL_SECS_DEFAULT - (now - _bt("still_start_time")), 0.0)
            else:
                still_remaining = _STILL_SECS_DEFAULT

            job_remaining = setup_remaining + bake_remaining + render_remaining + still_remaining
            if job_remaining < 0:
                import sys
                print(
                    f"[BatchSimLab] WARNING: negative job_remaining={job_remaining:.1f}  "
                    f"setup={setup_remaining:.1f}  bake={bake_remaining:.1f}  "
                    f"render={render_remaining:.1f}  still={still_remaining:.1f}  "
                    f"bake_start={_bt("bake_start_time"):.0f}  frames_baked={frames_baked}  "
                    f"frame_end={frame_end}  resolution={s.batch_resolution}",
                    file=sys.stderr,
                )
                job_remaining = 0.0
            # --- Bar 2: cumulative job-level progress (band-based, never backwards) ---
            # Each stage is allocated a band proportional to its time estimate.
            # Within a band, progress = (estimate - remaining) / estimate, clamped
            # to [0, estimate] so slow stages stay within their band.
            default_job_secs = (
                _SETUP_SECS_DEFAULT
                + default_bake_secs
                + default_render_secs
                + _STILL_SECS_DEFAULT
            )
            stage_secs      = [_SETUP_SECS_DEFAULT, default_bake_secs,
                                default_render_secs, _STILL_SECS_DEFAULT]
            stage_remaining = [setup_remaining, bake_remaining,
                               render_remaining, still_remaining]
            total_est  = max(sum(stage_secs), 1.0)
            job_factor = sum(
                min(max(s - r, 0.0), s) / total_est
                for s, r in zip(stage_secs, stage_remaining)
            )
            job_factor = min(max(job_factor, 0.0), 0.99)
            current_stage = min(stage_completed + 1, _TOTAL_SUBTASKS)
            s.batch_job_factor = job_factor
            s.batch_job_text   = f"Job stage {current_stage} of {_TOTAL_SUBTASKS} ({_format_eta(job_remaining)} this job)"

            # --- ETA: current_job_remaining + not-started jobs × model estimate ---
            jobs_not_started = max(total - done - 1, 0)
            remaining        = job_remaining + jobs_not_started * default_job_secs
            s.batch_time_remaining = f"All jobs: {_format_eta(remaining)}"

        else:
            s.batch_subtask_text   = ""
            s.batch_subtask_factor = 0.0
            s.batch_job_text       = ""
            s.batch_job_factor     = 0.0

        _redraw_panels()
        return 5.0

    return None


def _redraw_panels():
    """Force all 3D View areas to redraw so the progress label updates."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class SMOKE_OT_run_batch(bpy.types.Operator):
    """Launch run_smoke_batch.bat in a new console window and track progress."""

    bl_idname     = "smoke.run_batch"
    bl_label      = "Run Batch"
    bl_description = (
        "Open run_smoke_batch.bat in a new console window. "
        "Progress is tracked in the panel below."
    )

    # The save-before-batch dialog was removed in v0.4.1 (Export Batch flips
    # is_dirty, so it fired on almost every Run — pure noise).  TODO-29 added
    # a NARROWER invoke/draw that only triggers on the genuine-failure case:
    # rendering is on but the scene has no camera (renders will be black).

    def invoke(self, context, event):
        s = context.scene.smoke_settings
        if s.render_simulation_result and not _scene_has_camera(context.scene):
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="No camera in scene — renders will be black/fail.",
                  icon='ERROR')
        col.label(text="(Add a camera, or uncheck 'Render Simulation Result'")
        col.label(text=" and re-export as a bake-only batch)")
        col.separator()
        col.label(text="Click OK to run anyway, or Cancel to abort.")

    def execute(self, context):
        s           = context.scene.smoke_settings
        output_path = bpy.path.abspath(s.output_path)
        bat_path    = os.path.join(output_path, "run_smoke_batch.bat")

        if not os.path.exists(bat_path):
            self.report({'ERROR'}, "No batch file found — run Export Batch first")
            return {'CANCELLED'}

        jobs_dir   = os.path.join(output_path, "jobs")
        job_files  = [f for f in os.listdir(jobs_dir) if f.endswith(".json")] \
                     if os.path.isdir(jobs_dir) else []

        if not job_files:
            self.report({'ERROR'}, "No job files found — run Export Batch first")
            return {'CANCELLED'}

        # Verify exported helper files match the addon version.  A mismatch means
        # Export Batch was run with an older addon and the worker/launcher may lack
        # bug fixes or features present in the current installation.
        _worker_path   = os.path.join(output_path, "smoke_worker.py")
        _launcher_path = os.path.join(output_path, "smoke_launcher.py")
        _wv  = _read_helper_version(_worker_path,   "WORKER_VERSION")
        _lv  = _read_helper_version(_launcher_path, "LAUNCHER_VERSION")
        _bad = []
        if _wv != _EXPECTED_WORKER_VERSION:
            _bad.append(
                f"smoke_worker.py (found {_wv!r}, expected {_EXPECTED_WORKER_VERSION!r})"
            )
        if _lv != _EXPECTED_LAUNCHER_VERSION:
            _bad.append(
                f"smoke_launcher.py (found {_lv!r}, expected {_EXPECTED_LAUNCHER_VERSION!r})"
            )
        if _bad:
            self.report(
                {'WARNING'},
                "Helper file version mismatch — re-run Export Batch to update: "
                + ", ".join(_bad),
            )

        # Remove old log/done/sentinel files so the counter starts from zero.
        if os.path.isdir(jobs_dir):
            for f in os.listdir(jobs_dir):
                # Clear logs and ALL sentinel variants (legacy unphased + the
                # two-pass phased forms: .bake.done / .render.done /
                # .bake.worker_done / .render.worker_done / .bake.crashed /
                # .render.crashed). The phased names all end with the same
                # ".done"/".worker_done"/".crashed" suffixes, so the existing
                # endswith() checks cover them — we just add .crashed.
                if (f.endswith(".log") or f.endswith(".done")
                        or f.endswith(".worker_done") or f.endswith(".crashed")):
                    try:
                        os.remove(os.path.join(jobs_dir, f))
                    except OSError:
                        pass

        global _last_auto_index, _auto_retry_count
        s.batch_summary_line1 = s.batch_summary_line2 = ""
        s.batch_summary_line3 = s.batch_summary_line4 = ""
        s.show_job_log           = True
        s.job_log_auto_scroll    = True
        _last_auto_index         = 0
        _auto_retry_count        = 0   # fresh per-batch auto-retry budget
        _job_statuses.clear()
        # Repopulate _job_log_rows if it was lost (e.g. addon reload between
        # Export and Run).  job_log_items is the persistent source of truth.
        if not _job_log_rows and s.job_log_items:
            for _it in s.job_log_items:
                _job_log_rows.append((_it.job_number, _it.job_name))
        s.batch_total          = len(job_files)
        s.batch_jobs_dir       = jobs_dir
        s.batch_progress       = f"0 of {len(job_files)} job(s) complete"
        s.batch_overall_factor = 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0
        _bt_set("start_time", time.time())
        s.batch_time_remaining = "Estimating..."
        s.batch_job_log_key       = ""
        _bt_set("job_start_time", 0.0)
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        _bt_set("bake_start_time", 0.0)
        _bt_set("render_start_time", 0.0)
        _bt_set("still_start_time", 0.0)
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0
        s.batch_bake_frame_baseline   = -1
        s.batch_render_frame_baseline = -1

        # Launch the bat in a new console window; returns immediately.
        # cwd is set to output_path so the new cmd starts with a valid directory.
        subprocess.Popen(
            ["cmd", "/c", "start", "BatchSimLab Batch", bat_path],
            shell=False,
            cwd=output_path,
        )

        if not bpy.app.timers.is_registered(_poll_batch_progress):
            bpy.app.timers.register(_poll_batch_progress, first_interval=5.0)

        self.report({'INFO'}, f"Batch started — {len(job_files)} job(s) queued")
        return {'FINISHED'}


class SMOKE_OT_open_docs(bpy.types.Operator):
    """Open the BatchSimLab documentation in a web browser."""

    bl_idname  = "smoke.open_docs"
    bl_label   = "Documentation"
    bl_description = "Open the BatchSimLab documentation on GitHub"

    def execute(self, context):
        bpy.ops.wm.url_open(url=DOCS_URL)
        return {'FINISHED'}


class SMOKE_OT_retry_failed(bpy.types.Operator):
    """Re-run failed jobs with Use Placeholders and Use Existing Cache forced on."""

    bl_idname     = "smoke.retry_failed"
    bl_label      = "Retry Failed Jobs"
    bl_description = (
        "Write and launch run_retry_failed.bat containing only the failed jobs. "
        "Use Placeholders and Use Existing Cache are always forced on so each "
        "retry resumes from where the job last succeeded."
    )

    def execute(self, context):
        s           = context.scene.smoke_settings
        output_path = bpy.path.abspath(s.output_path)
        jobs_dir    = os.path.join(output_path, "jobs")

        if not os.path.isdir(jobs_dir):
            self.report({'ERROR'}, "Jobs folder not found — run Export Batch first")
            return {'CANCELLED'}

        # Collect failed jobs — check ALL .done files (including _retry.done) for
        # "error"; strip _retry suffix to get the base job stem so re-retrying works.
        failed = []
        seen_base_stems = set()
        for f in sorted(os.listdir(jobs_dir)):
            # Only the unphased completion markers count as job results — the
            # phased .bake.done / .render.done are diagnostic.
            if not (_DONE_RE.match(f) or _RETRY_DONE_RE.match(f)):
                continue
            try:
                with open(os.path.join(jobs_dir, f)) as fh:
                    if "error" not in fh.read().lower():
                        continue
                stem      = f[:-5]
                base_stem = stem[:-6] if stem.endswith("_retry") else stem
                if base_stem in seen_base_stems:
                    continue
                job_json = os.path.join(jobs_dir, base_stem + ".json")
                if os.path.exists(job_json):
                    failed.append((base_stem, job_json))
                    seen_base_stems.add(base_stem)
            except OSError:
                pass

        if not failed:
            self.report({'INFO'}, "No failed jobs found")
            return {'CANCELLED'}

        blender_exe = bpy.app.binary_path
        blend_file  = bpy.data.filepath
        dest_worker = os.path.join(output_path, "smoke_worker.py")

        bat_lines = [
            "@echo off",
            'cd /d "%~dp0"',
            "setlocal enabledelayedexpansion",
            f"echo BatchSimLab retry — {len(failed)} failed job(s)",
            "echo.",
            "set ERRORS=0",
            "",
        ]

        for base_stem, job_json in failed:
            with open(job_json) as fh:
                job_data = json.load(fh)

            job_data["use_placeholders"]   = True
            job_data["use_existing_cache"] = True

            log_path  = os.path.join(jobs_dir, base_stem + "_retry.log")
            done_path = os.path.join(jobs_dir, base_stem + "_retry.done")
            job_data["log_path"] = log_path

            retry_json = os.path.join(jobs_dir, base_stem + "_retry.json")
            with open(retry_json, "w") as fh:
                json.dump(job_data, fh, indent=2)

            name        = job_data.get("name", base_stem)
            render_mode = job_data.get("render_mode", "CYCLES")
            if render_mode == "EEVEE":
                blender_cmd = (
                    f'"{blender_exe}" "{blend_file}" '
                    f'--window-geometry 0 0 100 100 --factory-startup '
                    f'--python "{dest_worker}" -- "{retry_json}"'
                )
            else:
                blender_cmd = (
                    f'"{blender_exe}" "{blend_file}" '
                    f'--background --factory-startup '
                    f'--python "{dest_worker}" -- "{retry_json}"'
                )

            bat_lines += [
                f"echo === Retrying: {name} ===",
                f'{blender_cmd} 2>nul',
                "if errorlevel 1 (",
                "    echo   WARNING: retry exited with error",
                "    set /a ERRORS+=1",
                f'    echo error>"{done_path}"',
                ") else (",
                f'    echo done>"{done_path}"',
                ")",
                "echo.",
            ]

        bat_lines += [
            "echo ================================",
            "echo Retry complete.  Errors: %ERRORS%",
            "echo ================================",
        ]
        if s.collect_debug_log:
            bat_lines.append("pause")

        bat_path = os.path.join(output_path, "run_retry_failed.bat")
        with open(bat_path, "w") as fh:
            fh.write("\n".join(bat_lines))

        # Remove all .done and .worker_done markers for the jobs being retried
        # so they are counted as "in progress" by the poll timer.
        for base_stem, _ in failed:
            for suffix in ("", "_retry"):
                for ext in (".done", ".worker_done"):
                    try:
                        os.remove(os.path.join(jobs_dir, base_stem + suffix + ext))
                    except OSError:
                        pass

        # Reset progress tracking so the panel shows bars instead of the
        # "All N complete" message while the retry jobs are running.
        total_jobs = len([f for f in os.listdir(jobs_dir)
                          if f.endswith(".json") and "_retry" not in f])
        done_now   = len([f for f in os.listdir(jobs_dir)
                          if _DONE_RE.match(f) or _RETRY_DONE_RE.match(f)])

        global _last_auto_index
        s.batch_summary_line1 = s.batch_summary_line2 = ""
        s.batch_summary_line3 = s.batch_summary_line4 = ""
        s.job_log_auto_scroll  = True
        _last_auto_index       = 0
        s.batch_total          = total_jobs
        s.batch_jobs_dir       = jobs_dir
        s.batch_progress       = f"{done_now} of {total_jobs} job(s) complete"
        s.batch_overall_factor = done_now / total_jobs if total_jobs > 0 else 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0
        _bt_set("start_time", time.time())
        s.batch_time_remaining = "Estimating..."
        s.batch_job_log_key       = ""
        _bt_set("job_start_time", 0.0)
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        _bt_set("bake_start_time", 0.0)
        _bt_set("render_start_time", 0.0)
        _bt_set("still_start_time", 0.0)
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0
        s.batch_bake_frame_baseline   = -1
        s.batch_render_frame_baseline = -1

        if not bpy.app.timers.is_registered(_poll_batch_progress):
            bpy.app.timers.register(_poll_batch_progress, first_interval=5.0)

        _redraw_panels()

        subprocess.Popen(
            ["cmd", "/c", "start", "BatchSimLab Retry", bat_path],
            shell=False,
            cwd=output_path,
        )
        self.report({'INFO'}, f"Retry started — {len(failed)} job(s) queued")
        return {'FINISHED'}


def _auto_retry_deferred():
    """Called from a timer; runs Retry Failed Jobs automatically."""
    global _auto_retry_count
    _auto_retry_count += 1   # count this round against the per-batch retry budget
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            with bpy.context.temp_override(window=window, area=area):
                bpy.ops.smoke.retry_failed()
            return None
    return None


def _setup_results_deferred():
    """Called from a timer; finds a 3D view and runs the results operator."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                with bpy.context.temp_override(window=window, area=area):
                    bpy.ops.smoke.setup_results()
                return None
    return None


class SMOKE_OT_setup_results(bpy.types.Operator):
    """Create a SmokeOutput grid of planes showing each job's final render."""

    bl_idname     = "smoke.setup_results"
    bl_label      = "Setup Results Viewer"
    bl_description = (
        "Create a SmokeOutput collection containing a grid of mesh planes, "
        "each displaying the final PNG render from one job."
    )

    def execute(self, context):
        s           = context.scene.smoke_settings
        output_path = bpy.path.abspath(s.output_path)
        jobs_dir    = os.path.join(output_path, "jobs")
        render_dir  = os.path.join(output_path, "Renders")

        if not os.path.isdir(render_dir):
            self.report({'ERROR'}, "Renders folder not found")
            return {'CANCELLED'}

        # Collect job names in original order from the JSON files
        job_names = []
        if os.path.isdir(jobs_dir):
            for f in sorted(os.listdir(jobs_dir)):
                if f.endswith(".json") and not f.endswith("_retry.json"):
                    try:
                        with open(os.path.join(jobs_dir, f)) as fh:
                            job_names.append(json.load(fh)["name"])
                    except (OSError, KeyError):
                        pass

        # Pair each job name with its PNG
        png_files = [
            os.path.join(render_dir, n + ".png")
            for n in job_names
            if os.path.exists(os.path.join(render_dir, n + ".png"))
        ]

        if not png_files:
            self.report({'ERROR'}, "No PNG renders found in Renders folder")
            return {'CANCELLED'}

        # Measure aspect ratio from the last image
        probe = bpy.data.images.load(png_files[-1], check_existing=True)
        iw, ih = probe.size
        aspect = ih / iw if iw > 0 else 1.0

        # Remove any existing SmokeOutput collection
        old = bpy.data.collections.get("SmokeOutput")
        if old:
            for ob in list(old.objects):
                bpy.data.objects.remove(ob, do_unlink=True)
            bpy.data.collections.remove(old)

        coll = bpy.data.collections.new("SmokeOutput")
        context.scene.collection.children.link(coll)

        n    = len(png_files)
        cols = math.ceil(math.sqrt(n))
        pw   = 1.0           # plane width in Blender units
        ph   = aspect        # plane height (maintains image aspect ratio)
        gap  = 0.05 * pw     # 5% of plane width between planes

        planes = []
        for idx, png_path in enumerate(png_files):
            row = idx // cols
            col = idx % cols
            x   = col * (pw + gap)
            y   = -row * (ph + gap)

            bpy.ops.mesh.primitive_plane_add(size=1.0, location=(x, y, 0.0))
            obj = context.active_object
            obj.scale = (pw, ph, 1.0)
            bpy.ops.object.transform_apply(scale=True)
            obj.name = f"SmokeResult_{idx:04d}"

            for c in list(obj.users_collection):
                c.objects.unlink(obj)
            coll.objects.link(obj)

            mat   = bpy.data.materials.new(name=f"SmokeResult_{idx:04d}")
            mat.use_nodes = True
            ntree = mat.node_tree
            ntree.nodes.clear()

            nd_out  = ntree.nodes.new('ShaderNodeOutputMaterial')
            nd_emit = ntree.nodes.new('ShaderNodeEmission')
            nd_tex  = ntree.nodes.new('ShaderNodeTexImage')
            nd_uv   = ntree.nodes.new('ShaderNodeTexCoord')

            nd_tex.image = bpy.data.images.load(png_path, check_existing=True)

            ntree.links.new(nd_uv.outputs['UV'],         nd_tex.inputs['Vector'])
            ntree.links.new(nd_tex.outputs['Color'],     nd_emit.inputs['Color'])
            ntree.links.new(nd_emit.outputs['Emission'], nd_out.inputs['Surface'])

            nd_out.location  = (400,  0)
            nd_emit.location = (200,  0)
            nd_tex.location  = (  0,  0)
            nd_uv.location   = (-200, 0)

            obj.data.materials.append(mat)
            planes.append(obj)

        # Switch to top view, frame all result planes, Material Preview shading.
        # context.area is already a 3D viewport — set by _setup_results_deferred.
        # view_axis and view_selected both require a WINDOW region in the override.
        area = context.area
        if area and area.type == 'VIEW_3D' and planes:
            region = next((r for r in area.regions if r.type == 'WINDOW'), None)
            if region:
                bpy.ops.object.select_all(action='DESELECT')
                for ob in planes:
                    ob.select_set(True)
                context.view_layer.objects.active = planes[0]
                with context.temp_override(area=area, region=region):
                    bpy.ops.view3d.view_axis(type='TOP')
                    bpy.ops.view3d.view_selected()
                area.spaces.active.shading.type = 'MATERIAL'

        self.report({'INFO'}, f"SmokeOutput: {len(planes)} result plane(s) created")
        return {'FINISHED'}


class SMOKE_OT_remove_all_jobs(bpy.types.Operator):
    """Delete all exported jobs and reset batch state (simulation params untouched)."""

    bl_idname      = "smoke.remove_all_jobs"
    bl_label       = "Remove All Jobs"
    bl_description = (
        "Delete the exported jobs/ folder, run_smoke_batch.bat, smoke_worker.py, "
        "and smoke_launcher.py from the output path, then clear the job log and "
        "reset all batch progress state.  Simulation parameters are not changed."
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        s           = context.scene.smoke_settings
        output_path = s.output_path

        # Stop the poll timer before touching any state.
        if bpy.app.timers.is_registered(_poll_batch_progress):
            bpy.app.timers.unregister(_poll_batch_progress)

        deleted   = []
        skipped   = []

        # Delete the jobs/ folder.
        jobs_dir = os.path.join(output_path, "jobs")
        if os.path.isdir(jobs_dir):
            try:
                shutil.rmtree(jobs_dir)
                deleted.append("jobs/")
            except PermissionError:
                # Fallback: delete files individually.
                for fname in os.listdir(jobs_dir):
                    fpath = os.path.join(jobs_dir, fname)
                    try:
                        os.remove(fpath)
                        deleted.append(f"jobs/{fname}")
                    except OSError as exc:
                        skipped.append(f"jobs/{fname} ({exc})")
                try:
                    os.rmdir(jobs_dir)
                except OSError:
                    pass

        # Delete exported helper files and the batch launcher.
        for fname in ("run_smoke_batch.bat", "smoke_worker.py", "smoke_launcher.py"):
            fpath = os.path.join(output_path, fname)
            if os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                    deleted.append(fname)
                except OSError as exc:
                    skipped.append(f"{fname} ({exc})")

        # Reset all batch / job-log state (mirrors the relevant part of _reset_on_load).
        s.last_export_info     = ""
        s.batch_summary_line1 = s.batch_summary_line2 = ""
        s.batch_summary_line3 = s.batch_summary_line4 = ""
        s.batch_progress         = ""
        s.show_job_log           = False
        s.job_log_auto_scroll    = True
        _job_statuses.clear()
        _job_log_rows.clear()
        s.job_log_items.clear()
        s.batch_total            = 0
        s.batch_jobs_dir         = ""
        s.batch_overall_factor   = 0.0
        s.batch_subtask_text     = ""
        s.batch_subtask_factor   = 0.0
        s.batch_job_text         = ""
        s.batch_job_factor       = 0.0
        _bt_set("start_time", 0.0)
        s.batch_time_remaining   = ""
        s.batch_job_log_key      = ""
        _bt_set("job_start_time", 0.0)
        s.batch_frame_end        = 0
        s.batch_jobs_elapsed     = 0.0
        s.batch_resolution       = 0
        s.batch_render_width     = 0
        s.batch_render_height    = 0
        s.batch_render_mode      = "CYCLES"
        _bt_set("bake_start_time", 0.0)
        _bt_set("render_start_time", 0.0)
        _bt_set("still_start_time", 0.0)
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0
        s.batch_bake_frame_baseline   = -1
        s.batch_render_frame_baseline = -1

        _redraw_panels()

        if skipped:
            self.report({'WARNING'}, f"Removed {len(deleted)} item(s); could not remove: {', '.join(skipped)}")
        else:
            self.report({'INFO'}, f"Removed {len(deleted)} exported item(s)")
        return {'FINISHED'}


class SMOKE_OT_monitor_existing_jobs(bpy.types.Operator):
    """Reconnect to a batch that is already running (or recently finished)."""

    bl_idname = "smoke.monitor_existing_jobs"
    bl_label  = "Monitor Existing Jobs"
    bl_description = (
        "Scan the jobs folder and resume monitoring an in-progress or completed batch. "
        "Use this if Blender was reopened while a batch was running — the job log "
        "and progress bars will pick up as if the addon had been watching all along."
    )

    def execute(self, context):
        s           = context.scene.smoke_settings
        output_path = bpy.path.abspath(s.output_path)
        jobs_dir    = os.path.join(output_path, "jobs")

        if not os.path.isdir(jobs_dir):
            self.report({'ERROR'}, "Jobs folder not found — run Export Batch first")
            return {'CANCELLED'}

        # Read all first-run JSON files sorted by job index.
        job_entries = []
        for f in sorted(os.listdir(jobs_dir)):
            if not (f.endswith(".json") and "_retry" not in f):
                continue
            try:
                with open(os.path.join(jobs_dir, f)) as fh:
                    jd = json.load(fh)
                stem    = f[:-5]                   # "job_NNNN"
                idx     = int(stem.split("_", 1)[1])  # 0-based
                job_entries.append((idx + 1, jd.get("name", stem)))
            except (OSError, json.JSONDecodeError, ValueError):
                pass

        if not job_entries:
            self.report({'ERROR'}, "No job files found in jobs folder")
            return {'CANCELLED'}

        # Stop any already-running poll timer to avoid double-registration.
        if bpy.app.timers.is_registered(_poll_batch_progress):
            bpy.app.timers.unregister(_poll_batch_progress)

        global _last_auto_index
        _job_statuses.clear()
        _job_log_rows.clear()
        s.job_log_items.clear()

        for job_num, job_name in job_entries:
            item            = s.job_log_items.add()
            item.job_number = job_num
            item.job_name   = job_name
            item.status     = 'NOT_STARTED'
            _job_log_rows.append((job_num, job_name))

        s.show_job_log        = True
        s.job_log_auto_scroll = True
        _last_auto_index      = 0

        all_files = set(os.listdir(jobs_dir))
        done_now  = len([f for f in all_files
                         if _DONE_RE.match(f) or _RETRY_DONE_RE.match(f)])
        total     = len(job_entries)

        s.batch_summary_line1 = s.batch_summary_line2 = ""
        s.batch_summary_line3 = s.batch_summary_line4 = ""
        s.batch_total          = total
        s.batch_jobs_dir       = jobs_dir
        s.batch_progress       = f"{done_now} of {total} job(s) complete"
        s.batch_overall_factor = done_now / total if total > 0 else 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0
        _bt_set("start_time", time.time())
        s.batch_time_remaining = "Estimating..."
        s.batch_job_log_key       = ""
        _bt_set("job_start_time", 0.0)
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        _bt_set("bake_start_time", 0.0)
        _bt_set("render_start_time", 0.0)
        _bt_set("still_start_time", 0.0)
        s.batch_bake_secs_actual      = -1.0
        s.batch_render_secs_actual    = -1.0
        s.batch_bake_frame_baseline   = -1
        s.batch_render_frame_baseline = -1

        bpy.app.timers.register(_poll_batch_progress, first_interval=2.0)
        _redraw_panels()
        self.report({'INFO'}, f"Monitoring {total} job(s) — {done_now} already complete")
        return {'FINISHED'}


class SMOKE_OT_reset_to_defaults(bpy.types.Operator):
    """Reset ALL BatchSimLab settings to factory defaults."""

    bl_idname      = "smoke.reset_to_defaults"
    bl_label       = "Reset To Defaults"
    bl_description = (
        "Reset ALL BatchSimLab settings to factory defaults: simulation parameters, "
        "render settings, output path, domain object, utilities toggles, and job log "
        "are all cleared.  This cannot be undone."
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        _reset_on_load()
        _redraw_panels()
        self.report({'INFO'}, "BatchSimLab: all settings reset to defaults")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------

def _sub_param_ui(box, s, name, label):
    """
    Draw range/list controls for a sub-parameter inside an existing box.

    Used for Gas sub-params (vorticity, alpha, beta) and Noise sub-params
    where the outer collapsible box already exists and we only need to draw
    the Value/Range/List controls.

    Parameters
    ----------
    box   : bpy UILayout — the enclosing box to draw into
    s     : SmokeSettings
    name  : str — base parameter name, e.g. "vorticity"
    label : str — human-readable label shown above the controls
    """
    box.separator()
    box.label(text=f"{label}:")

    row = box.row()
    row.prop(s, f"{name}_use_range", text="Range", toggle=True)
    row.prop(s, f"{name}_use_list",  text="List",  toggle=True)

    if getattr(s, f"{name}_use_range"):
        box.prop(s, f"{name}_begin", text="Begin")
        box.prop(s, f"{name}_end",   text="End")
        box.prop(s, f"{name}_step",  text="Step")
    elif getattr(s, f"{name}_use_list"):
        row = box.row()
        row.template_list("SMOKE_UL_value_list", f"{name}_list",
                          s, f"{name}_list", s, f"{name}_index")
        col = row.column(align=True)
        col.operator("smoke.add_value",    text="", icon='ADD').param    = name
        col.operator("smoke.remove_value", text="", icon='REMOVE').param = name
    else:
        box.prop(s, f"{name}_begin", text="Value")


def _settings_ui(layout, s):
    """Draw the preset save/load row at the top of Simulation Parameters."""
    row = layout.row(align=True)
    row.prop(s, "settings_file_enum", text="")
    if _is_settings_dirty(s):
        row.label(text="*")
    row.operator("smoke.save_settings", text="", icon='FILE_TICK')
    row.operator("smoke.load_settings", text="", icon='FILE_FOLDER')
    if s.settings_file_path:
        stem = os.path.splitext(os.path.basename(s.settings_file_path))[0]
        layout.label(text=f"Loaded: {stem}", icon='CHECKMARK')


def _standalone_param_ui(layout, s, name, label,
                         show_prop, enable_prop=None, extra_props=None):
    """
    Draw a standalone collapsible parameter section with its own box.

    Used for Resolution and Dissolve which are top-level sections rather
    than sub-params inside a group box.

    Parameters
    ----------
    layout      : bpy UILayout
    s           : SmokeSettings
    name        : str — base parameter name, e.g. "resolution"
    label       : str — section header label
    show_prop   : str — name of the BoolProperty controlling collapse
    enable_prop : str or None — if set, draws an enable checkbox in the header
    extra_props : list of items drawn before the value controls.  Each item
                  may be either:
                    • a (prop_name, label) tuple → drawn on its own row, OR
                    • a list of such tuples → drawn together on ONE row
                                              (v0.7.0 TODO-45 pairing).
    """
    box = layout.box()
    row = box.row()
    row.prop(s, show_prop,
             icon='TRIA_DOWN' if getattr(s, show_prop) else 'TRIA_RIGHT',
             emboss=False, text="")
    if enable_prop:
        row.prop(s, enable_prop, text="")
    row.label(text=label)

    if not getattr(s, show_prop):
        return box
    if enable_prop and not getattr(s, enable_prop):
        return box

    if extra_props:
        for item in extra_props:
            if isinstance(item, list):
                # Same-row pairing: draw all tuples in one row, equal-split.
                shared = box.row(align=True)
                for prop_name, prop_label in item:
                    shared.prop(s, prop_name, text=prop_label)
            else:
                prop_name, prop_label = item
                box.prop(s, prop_name, text=prop_label)

    row = box.row()
    row.prop(s, f"{name}_use_range", text="Range", toggle=True)
    row.prop(s, f"{name}_use_list",  text="List",  toggle=True)

    if getattr(s, f"{name}_use_range"):
        box.prop(s, f"{name}_begin", text="Begin")
        box.prop(s, f"{name}_end",   text="End")
        box.prop(s, f"{name}_step",  text="Step")
    elif getattr(s, f"{name}_use_list"):
        row = box.row()
        row.template_list("SMOKE_UL_value_list", f"{name}_list",
                          s, f"{name}_list", s, f"{name}_index")
        col = row.column(align=True)
        col.operator("smoke.add_value",    text="", icon='ADD').param    = name
        col.operator("smoke.remove_value", text="", icon='REMOVE').param = name
    else:
        box.prop(s, f"{name}_begin", text="Value")
    return box


def _gas_ui(layout, s):
    """
    Draw the Gas Parameters collapsible section.

    Contains three sub-parameters: Vorticity, Buoyancy Density (alpha),
    Buoyancy Heat (beta).  All share the show_gas collapse toggle.
    """
    box = layout.box()
    row = box.row()
    row.prop(s, "show_gas",
             icon='TRIA_DOWN' if s.show_gas else 'TRIA_RIGHT',
             emboss=False, text="")
    row.label(text="Gas Parameters")

    if not s.show_gas:
        return

    # v0.6.0 TODO-37: order matches Blender's native Fluid Domain panel
    # (Buoyancy Density → Heat → Vorticity).  Underlying property names
    # (vorticity / alpha / beta), job-dict serialisation order, CSV column
    # order, and make_name() output are all unaffected — purely visual.
    _sub_param_ui(box, s, "alpha",     "Buoyancy Density")
    _sub_param_ui(box, s, "beta",      "Buoyancy Heat")
    _sub_param_ui(box, s, "vorticity", "Vorticity")


def _noise_ui(layout, s):
    """
    Draw the Noise collapsible section with enable checkbox in the header.

    Contains three sub-parameters: Scale (noise_upres), Strength, Position
    Scale.  The entire section is gated on use_noise.
    """
    box = layout.box()
    row = box.row()
    row.prop(s, "show_noise",
             icon='TRIA_DOWN' if s.show_noise else 'TRIA_RIGHT',
             emboss=False, text="")
    row.prop(s, "use_noise", text="")   # enable checkbox
    row.label(text="Noise")

    if not s.show_noise or not s.use_noise:
        return

    box.prop(s, "iterate_noise_both", text="Iterate Both On and Off")
    _sub_param_ui(box, s, "noise_upres",         "Scale")
    _sub_param_ui(box, s, "noise_strength",      "Strength")
    _sub_param_ui(box, s, "noise_spatial_scale", "Position Scale")


def _fire_ui(layout, s):
    """
    Draw the Fire Parameters collapsible section with enable checkbox in
    the header.  Parallel to _noise_ui.

    v0.7.0 TODO-42.  Contains five sub-parameters: Reaction Speed
    (burning_rate), Flames Smoke, Vorticity (separate from gas vorticity!),
    Temp Max, Ignition Temp.  When use_fire is unchecked the addon leaves
    the .blend's existing fire settings alone (same model as use_noise).
    """
    box = layout.box()
    row = box.row()
    row.prop(s, "show_fire",
             icon='TRIA_DOWN' if s.show_fire else 'TRIA_RIGHT',
             emboss=False, text="")
    row.prop(s, "use_fire", text="")   # enable checkbox
    row.label(text="Fire Parameters")

    if not s.show_fire or not s.use_fire:
        return

    _sub_param_ui(box, s, "burning_rate",    "Reaction Speed")
    _sub_param_ui(box, s, "flame_smoke",     "Flames Smoke")
    _sub_param_ui(box, s, "flame_vorticity", "Vorticity")
    _sub_param_ui(box, s, "flame_max_temp",  "Temp Max")
    _sub_param_ui(box, s, "flame_ignition",  "Ignition Temp")


def _emitter_sub_param_ui(box, em, ei, name, label):
    """Per-emitter analogue of _sub_param_ui — data block is the EmitterSettings
    element `em`; add/remove ops carry the emitter index `ei` and `param`."""
    box.separator()
    box.label(text=f"{label}:")

    row = box.row()
    row.prop(em, f"{name}_use_range", text="Range", toggle=True)
    row.prop(em, f"{name}_use_list",  text="List",  toggle=True)

    if getattr(em, f"{name}_use_range"):
        box.prop(em, f"{name}_begin", text="Begin")
        box.prop(em, f"{name}_end",   text="End")
        box.prop(em, f"{name}_step",  text="Step")
    elif getattr(em, f"{name}_use_list"):
        row = box.row()
        # Unique list-id per emitter+param so Blender doesn't share UI state.
        row.template_list("SMOKE_UL_value_list", f"em{ei}_{name}",
                          em, f"{name}_list", em, f"{name}_index")
        col = row.column(align=True)
        op = col.operator("smoke.add_emitter_value", text="", icon='ADD')
        op.emitter_index, op.param = ei, name
        op = col.operator("smoke.remove_emitter_value", text="", icon='REMOVE')
        op.emitter_index, op.param = ei, name
    else:
        box.prop(em, f"{name}_begin", text="Value")


def _emitter_velocity_ui(box, em, ei):
    """Draw the Initial Velocity block: the master toggle, then (when on) the
    Source / Normal scalars and the list of Initial X/Y/Z vectors."""
    box.separator()
    box.prop(em, "use_initial_velocity", text="Initial Velocity")
    if not em.use_initial_velocity:
        return
    _emitter_sub_param_ui(box, em, ei, "velocity_factor", "Source")
    _emitter_sub_param_ui(box, em, ei, "velocity_normal", "Normal")
    box.separator()
    box.label(text="Initial X/Y/Z vectors:")
    box.label(text=_VELOCITY_FORMAT_HINT, icon='INFO')
    row = box.row()
    row.template_list("SMOKE_UL_velocity_list", f"em{ei}_velocity",
                      em, "velocity_list", em, "velocity_index")
    col = row.column(align=True)
    col.operator("smoke.add_emitter_velocity", text="", icon='ADD').emitter_index = ei
    col.operator("smoke.remove_emitter_velocity", text="", icon='REMOVE').emitter_index = ei


def _emitters_ui(layout, s):
    """Draw the Emitters collapsible section — one sub-box per discovered
    emitter (default collapsed), each exposing its iterable flow params.

    v0.9.0 TODO-55.  Single-domain addon: emitters are the FLOW objects found
    inside the selected domain (Refresh icon re-scans)."""
    box = layout.box()
    row = box.row()
    row.prop(s, "show_emitters",
             icon='TRIA_DOWN' if s.show_emitters else 'TRIA_RIGHT',
             emboss=False, text="")
    row.label(text="Emitters")
    row.operator("smoke.refresh_emitters", text="", icon='FILE_REFRESH')

    if not s.show_emitters:
        return
    if not s.domain_obj:
        box.label(text="Select a domain to list its emitters.", icon='INFO')
        return
    if len(s.emitters) == 0:
        box.label(text="No emitters found inside the domain.", icon='INFO')
        box.label(text="Add flow objects, then click the refresh icon.")
        return

    for ei, em in enumerate(s.emitters):
        ebox = box.box()
        hrow = ebox.row()
        hrow.prop(em, "show",
                  icon='TRIA_DOWN' if em.show else 'TRIA_RIGHT',
                  emboss=False, text="")
        hrow.label(text=em.name, icon='OBJECT_DATA')
        if not em.show:
            continue
        _emitter_sub_param_ui(ebox, em, ei, "temperature",      "Initial Temperature")
        _emitter_sub_param_ui(ebox, em, ei, "density",          "Density")
        _emitter_sub_param_ui(ebox, em, ei, "surface_distance", "Surface Emission")
        _emitter_sub_param_ui(ebox, em, ei, "volume_density",   "Volume Emission")
        _emitter_velocity_ui(ebox, em, ei)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class SMOKE_PT_panel(bpy.types.Panel):
    """
    Main BatchSimLab panel in the 3D Viewport N-panel (Sidebar → Batch Sim Lab tab).

    Layout order:
      • Header row with title and documentation link
      • Domain object + output path
      • Resolution section
      • Gas Parameters section (Vorticity, Buoyancy Density, Heat)
      • Dissolve section
      • Noise section
      • Text Objects section
      • Iteration mode selector
      • Render engine selector
      • Export Batch button + status
    """

    bl_label       = "Batch Sim Lab"
    bl_idname      = "SMOKE_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'BatchLab'

    def draw_header(self, context):
        """
        Draw the panel title bar.  Adds a documentation link icon to the
        right of the standard panel title so users can quickly open the docs.
        """
        self.layout.operator(
            "smoke.open_docs",
            text="", icon='HELP', emboss=False,
        )

    def draw(self, context):
        s      = context.scene.smoke_settings
        layout = self.layout

        version = ".".join(str(v) for v in bl_info["version"])
        layout.label(text=f"BatchSimLab v{version}", icon='TOOL_SETTINGS')

        # ── Setup (collapsible) ───────────────────────────────────────────
        box_setup = layout.box()
        row = box_setup.row()
        row.prop(s, "show_setup",
                 icon='TRIA_DOWN' if s.show_setup else 'TRIA_RIGHT',
                 emboss=False, text="")
        row.label(text="Setup")
        if s.show_setup:
            box_setup.prop(s, "domain_obj", text="Domain Object")
            # Text Objects (moved inside Setup)
            box_to = box_setup.box()
            row_to = box_to.row()
            row_to.prop(s, "show_text_objects",
                        icon='TRIA_DOWN' if s.show_text_objects else 'TRIA_RIGHT',
                        emboss=False, text="")
            row_to.label(text="Text Objects")
            if s.show_text_objects:
                box_to.prop(s, "text_resolution", text="Resolution")
                box_to.prop(s, "text_noise",      text="Noise")
                box_to.prop(s, "text_dissolve",   text="Dissolve")
                box_to.prop(s, "text_time",       text="Bake Time")
            box_setup.prop(s, "output_path")

        layout.separator()

        # ── Simulation Parameters (outer collapsible) ─────────────────────
        box_sim = layout.box()
        row = box_sim.row()
        row.prop(s, "show_sim_params",
                 icon='TRIA_DOWN' if s.show_sim_params else 'TRIA_RIGHT',
                 emboss=False, text="")
        row.label(text="Simulation Parameters")

        if s.show_sim_params:
            # ── Settings save/load ────────────────────────────────────────
            _settings_ui(box_sim, s)
            box_sim.separator()

            # ── Frame range ───────────────────────────────────────────────
            fr_box = box_sim.box()
            fr_row = fr_box.row()
            fr_row.prop(s, "use_default_frames", text="Use Default Frames")
            sub = fr_box.column()
            sub.enabled = not s.use_default_frames
            sub.prop(s, "sim_frame_start", text="Frame Start")
            sub.prop(s, "sim_frame_end",   text="Frame End")
            box_sim.separator()

            res_box = _standalone_param_ui(box_sim, s, "resolution", "Resolution",
                                          show_prop="show_resolution")
            if s.show_resolution:
                res_box.prop(s, "maintain_density")
            box_sim.separator()

            _gas_ui(box_sim, s)
            box_sim.separator()

            # v0.7.0 TODO-45: Iterate Slow Dissolve checkbox paired on the
            # same row as the Slow Dissolve checkbox (nested list = one row).
            _standalone_param_ui(box_sim, s, "dissolve_speed", "Dissolve",
                                 show_prop="show_dissolve",
                                 enable_prop="use_dissolve",
                                 extra_props=[
                                     ("iterate_dissolve_both", "Iterate Both On and Off"),
                                     [
                                         ("slow_dissolve",         "Slow Dissolve"),
                                         ("iterate_slow_dissolve", "Iterate Slow"),
                                     ],
                                 ])
            box_sim.separator()

            _noise_ui(box_sim, s)
            box_sim.separator()

            # v0.7.0 TODO-41: Time / Adaptive Timesteps section.
            # Time Scale is a standalone always-on sweepable param; the
            # adaptive sub-block (CFL, Timesteps Max/Min) appears only
            # when use_adaptive_timesteps is checked.
            _standalone_param_ui(box_sim, s, "time_scale", "Time Scale",
                                 show_prop="show_time")
            if s.show_time:
                # Indent the Adaptive sub-block under the Time Scale box.
                box_adapt = box_sim.box()
                row_adapt = box_adapt.row()
                row_adapt.prop(s, "use_adaptive_timesteps",
                               text="Adaptive Time Step")
                if s.use_adaptive_timesteps:
                    _sub_param_ui(box_adapt, s, "cfl_number",     "CFL Number")
                    _sub_param_ui(box_adapt, s, "timesteps_max",  "Timesteps Max")
                    _sub_param_ui(box_adapt, s, "timesteps_min",  "Timesteps Min")
            box_sim.separator()

            # v0.7.0 TODO-42: Fire Parameters section.  Parallel to
            # Dissolve / Noise — enable checkbox gates the sub-params.
            _fire_ui(box_sim, s)
            box_sim.separator()

            # v0.9.0 TODO-55: per-emitter sweep sections (one per flow object
            # inside the domain; Refresh re-scans).
            _emitters_ui(box_sim, s)

        layout.separator()

        # v0.7.0 TODO-44: pre-compute _running here so both the Output and
        # Progress sections can reference it (and the Progress auto-expand
        # logic below).
        _running = _batch_is_running()

        # ── Output (collapsible: Iteration Mode + render settings + Run Batch) ──
        box_out = layout.box()
        row_out = box_out.row()
        row_out.prop(s, "show_output",
                     icon='TRIA_DOWN' if s.show_output else 'TRIA_RIGHT',
                     emboss=False, text="")
        row_out.label(text="Output")
        # job_count needed for both the in-section label and the Export Batch
        # button; compute outside the if so the button-enable logic below
        # has access regardless of collapse state (though the button only
        # draws inside the if).
        # Materialise the job list once: we need both the count and a scan for
        # oversized noise grids (the per-job dict carries resolution/up-res).
        _jobs_preview = list(generate_jobs(s))
        job_count = len(_jobs_preview)
        _noise_ceiling_jobs = sum(
            1 for p in _jobs_preview
            if noise_grid_exceeds_ceiling(
                p["resolution"], p["use_noise"], p["noise_upres"])
        )
        if s.show_output:
            # ── Iteration mode + job count ────────────────────────────────
            box_iter = box_out.box()
            box_iter.label(text="Iteration Mode:")
            box_iter.prop(s, "iteration_mode", expand=True)
            box_iter.label(text=f"{job_count} job(s) will be created")

            # Warn (don't block) when any job's noise up-res grid is in the zone
            # where Mantaflow's noise bake has crashed or hung.  edge =
            # resolution × noise_upres; see _NOISE_UPRES_EDGE_WARN.
            if _noise_ceiling_jobs:
                warn = box_iter.box()
                warn.alert = True
                warn.label(
                    text=f"{_noise_ceiling_jobs} job(s) exceed the noise up-res "
                         f"ceiling ({_NOISE_UPRES_EDGE_WARN}³)",
                    icon='ERROR')
                col_w = warn.column(align=True)
                col_w.scale_y = 0.75
                col_w.label(text="Noise bake may crash or hang at this size.")
                col_w.label(text="Baking can still succeed — retry if it stalls.")

            box_out.separator()

            # ── Render settings ──────────────────────────────────────────
            box_out.prop(s, "use_placeholders",   text="Use Placeholders")
            row_cache = box_out.row()
            row_cache.separator(factor=2.0)
            sub_cache = row_cache.column()
            sub_cache.enabled = not s.use_placeholders
            sub_cache.prop(s, "use_existing_cache", text="Use Existing Cache")
            box_out.prop(s, "auto_retry_failed",  text="Automatically Retry Failed Jobs")

            # Render Simulation Result (TODO-26): when off, run a bake-only batch and
            # grey out everything that only matters when rendering.
            box_out.prop(s, "render_simulation_result", text="Render Simulation Result")
            _render_on = s.render_simulation_result

            # Render Animation (TODO-33): still-only mode skips the PNG sequence + MP4.
            # Only meaningful when rendering is on at all.
            row_anim = box_out.row()
            row_anim.enabled = _render_on
            row_anim.prop(s, "render_animation", text="Render Animation")

            row = box_out.row()
            row.enabled = _render_on
            row.prop(s, "render_mode",    text="Render Engine")
            row.prop(s, "render_samples", text="Samples")

            # Disable Export/Append while a batch is running (TODO-28 safeguard): the
            # running cmd.exe already parsed the .bat, so editing it now can't help.
            row_mode = box_out.row(align=True)
            row_mode.enabled = not _running
            row_mode.prop(s, "export_mode", expand=True)
            export_row = box_out.row()
            # Grey out the button when no jobs would be created so the user can't
            # click it and get a misleading "Exported 0 job(s)" success message.
            # In LIMITED mode the fallback baseline ensures count >= 1, so this
            # only fires in pathological cases (e.g. all-empty lists in ALL mode).
            export_row.enabled = job_count > 0 and not _running
            export_row.operator(
                "smoke.export_batch",
                text=f"Export Batch  ({job_count} jobs)",
                icon='EXPORT',
            )

            # Status line from last export (word-wrapped at 60 chars)
            if s.last_export_info:
                col = box_out.column(align=True)
                col.scale_y = 0.75
                info = s.last_export_info
                col.label(text=info[:60])
                if len(info) > 60:
                    col.label(text=info[60:])

            box_out.separator()
            # "Display Results When Finished" is meaningless in bake-only mode; grey
            # it out there (the property is also force-cleared by its update callback).
            row_show = box_out.row()
            row_show.enabled = _render_on
            row_show.prop(s, "show_results")
            # Run Batch is enabled only when a runnable batch exists on disk (TODO-25)
            # and no batch is already running (TODO-28 safeguard).
            run_row = box_out.row()
            run_row.enabled = _batch_ready(bpy.path.abspath(s.output_path)) and not _running
            run_row.operator("smoke.run_batch", text="Run Batch", icon='PLAY')

        layout.separator()

        # ── Progress (collapsible: bars + summary + Job Log) ──────────────
        # v0.7.0 TODO-44: progress display lives in its own collapsible.
        # Auto-expand whenever a batch is running OR a post-batch summary
        # is visible — overrides the user's manual collapse so they can't
        # accidentally hide active progress.  The toggle still binds to
        # show_progress so the manual choice persists for the next batch.
        _progress_active = (
            _running
            or bool(s.batch_summary_line1)
            or bool(s.batch_progress)
            or bool(s.job_log_items)
        )
        _effective_show_progress = s.show_progress or _progress_active

        box_prog = layout.box()
        row_prog = box_prog.row()
        row_prog.prop(s, "show_progress",
                      icon='TRIA_DOWN' if _effective_show_progress else 'TRIA_RIGHT',
                      emboss=False, text="")
        row_prog.label(text="Progress")
        if _progress_active and not s.show_progress:
            # Visual hint that the section is force-opened (subtle — the
            # arrow icon already shows DOWN).  Skip an extra label to keep
            # the header tight.
            pass

        if _effective_show_progress:
            if s.batch_summary_line1:
                box_prog.label(text=s.batch_summary_line1, icon='CHECKMARK')
                box_prog.label(text=s.batch_summary_line2)
                if s.batch_summary_line3:
                    box_prog.label(text=s.batch_summary_line3)
                if s.batch_summary_line4:
                    box_prog.label(text=s.batch_summary_line4)
                    box_prog.operator("smoke.retry_failed", icon='FILE_REFRESH')
                box_prog.operator("smoke.setup_results", icon='IMAGE_DATA')
            elif s.batch_progress:
                # Bar 3a — current sub-task (what is happening right now)
                if s.batch_subtask_text:
                    try:
                        box_prog.progress(factor=s.batch_subtask_factor, type='BAR',
                                          text=s.batch_subtask_text)
                    except AttributeError:
                        box_prog.label(text=s.batch_subtask_text)

                # Bar 3b — job stage progress (how many sub-tasks are complete)
                if s.batch_job_text:
                    try:
                        box_prog.progress(factor=s.batch_job_factor, type='BAR',
                                          text=s.batch_job_text)
                    except AttributeError:
                        box_prog.label(text=s.batch_job_text)

                # Bar 3c — overall job count (X of Y jobs complete)
                try:
                    box_prog.progress(factor=s.batch_overall_factor, type='BAR',
                                      text=s.batch_progress)
                except AttributeError:
                    box_prog.label(text=s.batch_progress, icon='TIME')

                if s.batch_time_remaining:
                    box_prog.label(text=s.batch_time_remaining, icon='TIME')

            # ── Job Log (nested inside Progress; only shown once populated) ──
            if s.job_log_items:
                box_log = box_prog.box()
                row_log = box_log.row()
                row_log.prop(s, "show_job_log",
                             icon='TRIA_DOWN' if s.show_job_log else 'TRIA_RIGHT',
                             emboss=False, text="")
                row_log.label(text="Job Log")
                if s.show_job_log:
                    hdr = box_log.row()
                    hdr.label(text="", icon='BLANK1')
                    hdr.label(text="#")
                    hdr.label(text="Job Name")
                    box_log.template_list(
                        "SMOKE_UL_job_log", "",
                        s, "job_log_items",
                        s, "job_log_index",
                        rows=min(len(s.job_log_items), 8),
                    )

        layout.separator()

        # ── Utilities (collapsible, default collapsed) ────────────────────
        box_util = layout.box()
        row = box_util.row()
        row.prop(s, "show_utilities",
                 icon='TRIA_DOWN' if s.show_utilities else 'TRIA_RIGHT',
                 emboss=False, text="")
        row.label(text="Utilities")
        if s.show_utilities:
            box_util.prop(s, "collect_crash_logs")
            box_util.prop(s, "collect_estimation_data")
            box_util.prop(s, "collect_debug_log")
            box_util.separator()
            # Only useful when an exported jobs folder is present to monitor.
            _jobs_dir = os.path.join(bpy.path.abspath(s.output_path), "jobs")
            row_mon = box_util.row()
            row_mon.enabled = os.path.isdir(_jobs_dir) and any(
                re.match(r'^job_\d{4}\.json$', f) for f in os.listdir(_jobs_dir)
            )
            row_mon.operator("smoke.monitor_existing_jobs", text="Monitor Existing Jobs", icon='RECOVER_LAST')
            box_util.separator()
            box_util.operator("smoke.remove_all_jobs", text="Remove All Jobs", icon='TRASH')
            box_util.separator()
            row_reset = box_util.row()
            row_reset.alert = True
            row_reset.operator("smoke.reset_to_defaults", text="Reset To Defaults", icon='LOOP_BACK')


# ---------------------------------------------------------------------------
# File-load reset
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _default_output_path():
    """Absolute folder of the current .blend, or '' if the file is unsaved.

    Used instead of storing the literal '//' token: a Python-defined
    StringProperty rejects '//' with a "does not support blend relative //
    prefix" RuntimeWarning on Blender 5.x, so we resolve it to an absolute path
    here (bpy.path.abspath is a pure path utility — no property assignment, no
    warning).  Every consumer still wraps the value in bpy.path.abspath, so an
    absolute path passes through unchanged.
    """
    try:
        if bpy.data.filepath:
            return bpy.path.abspath("//")
    except (AttributeError, RuntimeError):
        pass
    return ""


def _reset_on_load(dummy=None):
    """
    Reset ALL addon properties to their defaults whenever a .blend file is opened.

    Every property is reset so the addon always starts from a known clean state.
    The user must re-select the domain object and output path after load; this
    prevents stale batch state, job log rows, and export artefacts from persisting
    across sessions.
    """
    # Stop the poll timer immediately so it cannot fire between property resets.
    if bpy.app.timers.is_registered(_poll_batch_progress):
        bpy.app.timers.unregister(_poll_batch_progress)

    # bpy.data is a _RestrictData object during addon install — bail out early.
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        return

    for scene in scenes:
        s = getattr(scene, "smoke_settings", None)
        if s is None:
            continue

        # ── Setup ────────────────────────────────────────────────────────────
        s.domain_obj  = None
        # Default to the loaded .blend's own folder (resolved absolute) rather
        # than a hard-coded machine path; '' when the file is still unsaved.
        s.output_path = _default_output_path()

        # ── Simulation parameters ─────────────────────────────────────────────
        for name in ITERABLE_PARAMS:
            lst = getattr(s, name + "_list", None)
            if lst is not None:
                lst.clear()
            setattr(s, name + "_use_range", False)
            setattr(s, name + "_use_list",  False)

        s.resolution_step          = 0
        s.resolution_begin         = 64
        s.resolution_end           = 64
        s.vorticity_step           = 0.0
        s.vorticity_begin          = 0.0
        s.vorticity_end            = 0.0
        s.alpha_step               = 0.0
        s.alpha_begin              = 1.0
        s.alpha_end                = 1.0
        s.beta_step                = 0.0
        s.beta_begin               = 1.0
        s.beta_end                 = 1.0
        s.dissolve_speed_step      = 0
        s.dissolve_speed_begin     = 5
        s.dissolve_speed_end       = 5
        s.noise_upres_step         = 0
        s.noise_upres_begin        = 2
        s.noise_upres_end          = 2
        s.noise_strength_step      = 0.0
        s.noise_strength_begin     = 2.0
        s.noise_strength_end       = 2.0
        s.noise_spatial_scale_step  = 0.0
        s.noise_spatial_scale_begin = 2.0
        s.noise_spatial_scale_end   = 2.0

        s.use_dissolve          = False
        s.slow_dissolve         = False
        s.iterate_dissolve_both = False
        s.iterate_slow_dissolve = False
        s.use_noise             = False
        s.iterate_noise_both    = False

        # v0.7.0 TODO-41: gas timing master toggle + sweep values reset to
        # Blender's domain defaults (time_scale=1.0, cfl=4.0, max=4, min=1).
        s.use_adaptive_timesteps = True
        s.show_time              = True
        for _attr, _val in (
            ("time_scale", 1.0), ("cfl_number", 4.0),
            ("timesteps_max", 4), ("timesteps_min", 1),
        ):
            setattr(s, _attr + "_step",  0)
            setattr(s, _attr + "_begin", _val)
            setattr(s, _attr + "_end",   _val)

        # v0.7.0 TODO-42: fire master toggle (default OFF) + sweep values.
        s.use_fire = False
        s.show_fire = True
        for _attr, _val in (
            ("burning_rate",   0.75), ("flame_smoke",     1.0),
            ("flame_vorticity", 0.5), ("flame_max_temp",  1.7),
            ("flame_ignition",  1.5),
        ):
            setattr(s, _attr + "_step",  0)
            setattr(s, _attr + "_begin", _val)
            setattr(s, _attr + "_end",   _val)

        s.use_default_frames = True
        s.sim_frame_start    = 1
        s.sim_frame_end      = 250

        # ── Settings presets ──────────────────────────────────────────────────
        s.settings_file_path   = ""
        s.settings_search_path = ""
        s.settings_snapshot    = ""
        s.settings_file_enum   = _SETTINGS_ENUM_SENTINEL

        # ── Text objects ──────────────────────────────────────────────────────
        s.text_resolution = "Resolution_Text"
        s.text_noise      = "Noise_Text"
        s.text_dissolve   = "Dissolve_Text"
        s.text_time       = "Time_Text"

        # ── Render / export settings ──────────────────────────────────────────
        s.render_mode        = 'CYCLES'
        s.render_samples     = 16
        s.maintain_density   = True
        s.iteration_mode     = 'LIMITED'
        s.use_placeholders   = False
        s.use_existing_cache = False
        s.auto_retry_failed  = False
        s.render_simulation_result = True
        s.render_animation   = True
        s.show_results       = False

        # ── Utilities ─────────────────────────────────────────────────────────
        s.collect_crash_logs      = False
        s.collect_estimation_data = False
        s.collect_debug_log       = False

        # ── Batch / job-log state ─────────────────────────────────────────────
        s.last_export_info     = ""
        s.batch_summary_line1 = s.batch_summary_line2 = ""
        s.batch_summary_line3 = s.batch_summary_line4 = ""
        s.batch_progress         = ""
        s.show_job_log           = False
        # v0.7.0 TODO-44: reset collapsible section state on .blend load
        s.show_output            = True
        s.show_progress          = True
        s.job_log_auto_scroll    = True
        _job_statuses.clear()
        _job_log_rows.clear()
        s.job_log_items.clear()
        # v0.9.0 TODO-55: emitters are scene-specific; re-discovered on
        # domain-select / Refresh, so clear stale ones from the prior file.
        s.emitters.clear()
        s.show_emitters        = True
        s.batch_total          = 0
        s.batch_jobs_dir       = ""
        s.batch_overall_factor = 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0
        _bt_set("start_time", 0.0)
        s.batch_time_remaining = ""
        s.batch_job_log_key       = ""
        _bt_set("job_start_time", 0.0)
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        _bt_set("bake_start_time", 0.0)
        _bt_set("render_start_time", 0.0)
        _bt_set("still_start_time", 0.0)
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0
        s.batch_bake_frame_baseline   = -1
        s.batch_render_frame_baseline = -1


@bpy.app.handlers.persistent
def _restore_job_log_on_load(_dummy=None):
    """Repopulate _job_log_rows from saved job_log_items after a blend file loads.

    _reset_on_load clears _job_log_rows (module-level Python state, not saved
    in the blend file).  If _job_log_rows is somehow empty while job_log_items
    is not — e.g. due to addon reload ordering — this handler re-syncs them so
    draw_item never returns blank rows for existing items.
    """
    global _job_log_rows
    _job_log_rows.clear()
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        return
    for scene in scenes:
        s = getattr(scene, "smoke_settings", None)
        if s is None:
            continue
        for _item in s.job_log_items:
            _job_log_rows.append((_item.job_number, _item.job_name))
        if _job_log_rows:
            break


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    ValueItem,
    VelocityItem,
    EmitterSettings,
    SMOKE_UL_value_list,
    SMOKE_UL_velocity_list,
    SmokeJobItem,
    SMOKE_UL_job_log,
    SmokeSettings,
    SmokeSimLabPreferences,
    SMOKE_OT_export_batch,
    SMOKE_OT_run_batch,
    SMOKE_OT_save_settings,
    SMOKE_OT_load_settings,
    SMOKE_OT_add_value,
    SMOKE_OT_remove_value,
    SMOKE_OT_refresh_emitters,
    SMOKE_OT_add_emitter_value,
    SMOKE_OT_remove_emitter_value,
    SMOKE_OT_add_emitter_velocity,
    SMOKE_OT_remove_emitter_velocity,
    SMOKE_OT_open_docs,
    SMOKE_OT_retry_failed,
    SMOKE_OT_setup_results,
    SMOKE_OT_remove_all_jobs,
    SMOKE_OT_monitor_existing_jobs,
    SMOKE_OT_reset_to_defaults,
    SMOKE_PT_panel,
]


def register():
    """Register all classes and attach SmokeSettings to Scene."""
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.smoke_settings = bpy.props.PointerProperty(type=SmokeSettings)
    if _reset_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_reset_on_load)
    # Runs after _reset_on_load: re-syncs _job_log_rows from saved job_log_items
    # if any items survived the reset (e.g. due to addon-reload ordering).
    if _restore_job_log_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_restore_job_log_on_load)
    _reset_on_load()  # also reset when scripts are reloaded


def unregister():
    """Unregister all classes and remove the Scene property."""
    if _reset_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_reset_on_load)
    if _restore_job_log_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_restore_job_log_on_load)
    if bpy.app.timers.is_registered(_poll_batch_progress):
        bpy.app.timers.unregister(_poll_batch_progress)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "smoke_settings"):
        del bpy.types.Scene.smoke_settings
