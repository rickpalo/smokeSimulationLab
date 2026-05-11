"""
SmokeSimLab/__init__.py
=======================
Blender 4.x addon — SmokeLab tab in the 3D Viewport N-panel.

SmokeSimLab automates batch smoke simulation parameter sweeps.  For each
parameter combination it bakes the Mantaflow fluid simulation, renders an
OpenGL playblast animation (MP4), renders a final quality still frame (PNG),
and logs results to a CSV file for later comparison.

Installation
------------
1. Zip the SmokeSimLab folder (containing __init__.py and smoke_worker.py).
2. In Blender: Edit → Preferences → Add-ons → Install → select the zip.
3. Enable "SmokeSimLab" in the add-on list.

Workflow
--------
1. Set your fluid domain object and output directory in the SmokeLab panel.
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
    "name":        "SmokeSimLab",
    "author":      "Rick Palo",
    "version":     (0, 2, 6),
    "blender":     (4, 0, 0),
    "location":    "View3D > Sidebar > SmokeLab",
    "description": "Batch smoke simulation parameter sweeper with CSV logging",
    "doc_url":     "https://github.com/rickpalo/SmokeSimLab",
    "tracker_url": "https://github.com/rickpalo/SmokeSimLab/issues",
    "category":    "Fluid Simulation",
}

import bpy
import math
import os
import re
import shutil
import itertools
import json
import subprocess
import sys
import time

print(f"SmokeSimLab {'.'.join(str(v) for v in bl_info['version'])} loaded")


DOCS_URL = "https://github.com/rickpalo/SmokeSimLab"

# Expected version strings in the helper files exported to the output folder.
# When Run Batch detects a mismatch it warns the user to re-run Export Batch.
# Keep these in sync with WORKER_VERSION / LAUNCHER_VERSION in those files.
_EXPECTED_WORKER_VERSION   = "0.2.6"
_EXPECTED_LAUNCHER_VERSION = "0.2.6"


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
}

# Default per-frame / per-stage timing estimates used before real data is available.
_SETUP_SECS_DEFAULT  =  10.0   # seconds for setup / cache phase
_STILL_SECS_DEFAULT  =  30.0   # seconds for final still frame

# Bake estimate: bake_secs ≈ _BAKE_RATE_PER_RES3_FRAME × resolution³ × frames
# Derived from: 0.25 s/frame baseline at resolution=64 → 0.25/(64³) ≈ 9.54e-7
# Update _BAKE_RATE_PER_RES3_FRAME from perf_log.json once real data is available.
_BAKE_RATE_PER_RES3_FRAME = 6.6247e-07  # s / (res^3 * frame)  — placeholder

# Render estimate: render_secs ≈ rate × width × height × frames
# Derived from: 15 s/frame baseline at 1920×1080 → 15/2073600 ≈ 7.23e-9
# Update these from perf_log.json once real data is available.
_RENDER_RATE_CYCLES_PER_PIXEL_FRAME = 7.23e-9  # s / (pixel * frame) — placeholder
_RENDER_RATE_EEVEE_PER_PIXEL_FRAME  = 1.0425e-6  # s / (pixel * frame) — placeholder (2× faster guess)

# Legacy flat rates kept as fallback when resolution/dimensions are unknown.
_BAKE_RATE_DEFAULT   =   1.0  # s/frame at unspecified resolution
_RENDER_RATE_DEFAULT =  45.0   # s/frame at unspecified resolution


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


# ---------------------------------------------------------------------------
# Settings save/load — helper functions
# ---------------------------------------------------------------------------

_SWEEP_PARAMS = [
    "resolution", "vorticity", "alpha", "beta",
    "dissolve_speed", "noise_upres", "noise_strength", "noise_spatial_scale",
]


def _settings_dict(s):
    """Return a JSON-serialisable snapshot of all Simulation Parameter settings."""
    d = {
        "smokesettings_version": 2,
        "iteration_mode":        s.iteration_mode,
        "use_dissolve":          s.use_dissolve,
        "slow_dissolve":         s.slow_dissolve,
        "iterate_dissolve_both": getattr(s, "iterate_dissolve_both", False),
        "use_noise":             s.use_noise,
        "iterate_noise_both":    getattr(s, "iterate_noise_both", False),
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
    import json
    s.iteration_mode = data.get("iteration_mode", "LIMITED")
    s.use_dissolve   = data.get("use_dissolve",   False)
    s.slow_dissolve  = data.get("slow_dissolve",  False)
    if hasattr(s, "iterate_dissolve_both"):
        s.iterate_dissolve_both = data.get("iterate_dissolve_both", False)
    s.use_noise      = data.get("use_noise",       False)
    if hasattr(s, "iterate_noise_both"):
        s.iterate_noise_both = data.get("iterate_noise_both", False)
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
    import json, os
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _apply_settings_dict(s, data)
        s.settings_file_path   = os.path.normpath(path)
        s.settings_search_path = os.path.dirname(os.path.normpath(path))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"[SmokeSimLab] Failed to load settings from {path!r}: {exc}")


def _is_settings_dirty(s):
    """Return True if current settings differ from the last saved/loaded snapshot."""
    import json
    if not s.settings_file_path:
        return False
    snap = s.settings_snapshot
    if not snap:
        return True
    return json.dumps(_settings_dict(s), sort_keys=True) != snap


def _settings_files_enum_items(self, _context):
    """EnumProperty items — list .smokesettings files in the preset search path.

    The identifier for each item is the filename stem (no extension, no path).
    This avoids issues with spaces, backslashes, or long Windows paths being
    used as Blender EnumProperty identifiers.
    """
    import os
    folder = self.settings_search_path
    if not folder and self.output_path:
        folder = bpy.path.abspath(self.output_path)
    # First item: blank name so the button shows blank when no preset is active.
    items = [('', "", "")]
    if folder and os.path.isdir(folder):
        try:
            for fname in sorted(os.listdir(folder)):
                if fname.endswith(".smokesettings"):
                    stem = fname[: -len(".smokesettings")]
                    items.append((stem, stem, fname))
        except OSError:
            pass
    return items


def _on_settings_enum_update(self, _context):
    """Update callback for settings_file_enum — auto-load when selection changes."""
    import os
    stem = self.settings_file_enum
    if not stem:
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
        if step == 0:
            return [begin]
        vals, v = [], begin
        epsilon = step * 1e-6   # tiny tolerance for float boundary
        while v <= end + epsilon:
            vals.append(round(v, 6))  # round to avoid 0.200000000001 in names
            v += step
        return vals

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
    # Gas params and resolution are always available.
    # Dissolve and noise params only when their section is enabled.
    sweepable = ["resolution", "vorticity", "alpha", "beta"]
    if s.use_dissolve:
        sweepable.append("dissolve_speed")
    if s.use_noise:
        sweepable += ["noise_upres", "noise_strength", "noise_spatial_scale"]

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
            yield job

    # Iterate-both: append one comparison job with the feature toggled off.
    # Only fires when the feature is currently enabled (the checkbox is hidden
    # when the feature is off, so this path is only reached intentionally).
    if s.use_dissolve and s.iterate_dissolve_both:
        base = _default_job(s)
        job  = dict(base)
        job["use_dissolve"] = False
        yield job

    if s.use_noise and s.iterate_noise_both:
        base = _default_job(s)
        job  = dict(base)
        job["use_noise"] = False
        yield job


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
    if s.use_dissolve:
        dissolve_states = [(True, s.slow_dissolve, param("dissolve_speed"))]
        if s.iterate_dissolve_both:
            dissolve_states.append((False, s.slow_dissolve, [s.dissolve_speed]))
    else:
        dissolve_states = [(False, s.slow_dissolve, [s.dissolve_speed])]

    # Build the set of (use_noise, nu, ns, nss) states.
    if s.use_noise:
        noise_states = [(True,
                         param("noise_upres"),
                         param("noise_strength"),
                         param("noise_spatial_scale"))]
        if s.iterate_noise_both:
            noise_states.append((False,
                                  [s.noise_upres],
                                  [s.noise_strength],
                                  [s.noise_spatial_scale]))
    else:
        noise_states = [(False,
                         [s.noise_upres],
                         [s.noise_strength],
                         [s.noise_spatial_scale])]

    for (use_d, slow_d, dissolve) in dissolve_states:
        for (use_n, nu, ns, nss) in noise_states:
            for combo in itertools.product(res, vort, alpha, beta,
                                           dissolve, nu, ns, nss):
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
                }


def generate_jobs(s):
    """
    Dispatch to the appropriate job generator based on s.iteration_mode.

    Parameters
    ----------
    s : SmokeSettings

    Returns
    -------
    generator of job dicts
    """
    if s.iteration_mode == 'LIMITED':
        return generate_jobs_limited(s)
    return generate_jobs_all(s)


def make_name(p, index=0):
    """
    Build a unique, human-readable filename stem from a job-parameter dict.

    Format:
        R<res>_V<vort>_A<alpha>_B<beta>_D<dissolve|OFF>_N<noise|OFF>_NNNN

    The zero-padded index suffix guarantees uniqueness even when two
    parameter combinations would otherwise produce the same string
    (e.g. when rounding collapses close float values).

    Parameters
    ----------
    p     : dict  — job parameter dict from generate_jobs()
    index : int   — zero-based job index

    Returns
    -------
    str — filename stem without extension
    """
    dissolve_part = (
        f"D{int(p['dissolve_speed'])}" if p['use_dissolve'] else "D-OFF"
    )
    noise_part = (
        f"N{int(p['noise_upres'])}_"
        f"NS{round(p['noise_strength'], 2)}_"
        f"SC{round(p['noise_spatial_scale'], 2)}"
        if p['use_noise'] else "N-OFF"
    )
    return (
        f"R{int(p['resolution'])}_"
        f"V{round(p['vorticity'], 2)}_"
        f"A{round(p['alpha'], 2)}_"
        f"B{round(p['beta'], 2)}_"
        f"{dissolve_part}_"
        f"{noise_part}_"
        f"{index:04d}"
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
    (job_count, bat_path) : (int, str)

    Raises
    ------
    FileNotFoundError — if smoke_worker.py is missing from the addon folder
    """
    s = context.scene.smoke_settings

    output_path = bpy.path.abspath(s.output_path)
    jobs_dir    = os.path.join(output_path, "jobs")

    # Clear the jobs folder so stale jobs from a previous export don't linger.
    # Fall back to per-file deletion if rmtree hits a PermissionError (e.g. a
    # _retry.log held open by a still-running Blender process).
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
    # so it points to the SmokeSimLab folder, not the .blend file.
    addon_dir      = os.path.dirname(os.path.abspath(__file__))
    src_worker     = os.path.join(addon_dir, "smoke_worker.py")
    dest_worker    = os.path.join(output_path, "smoke_worker.py")
    src_launcher   = os.path.join(addon_dir, "smoke_launcher.py")
    dest_launcher  = os.path.join(output_path, "smoke_launcher.py")

    if not os.path.exists(src_worker):
        raise FileNotFoundError(
            f"smoke_worker.py not found in addon folder.\n"
            f"Expected: {src_worker}\n"
            f"Re-install the SmokeSimLab addon."
        )
    shutil.copy2(src_worker, dest_worker)
    if os.path.exists(src_launcher):
        shutil.copy2(src_launcher, dest_launcher)

    # ── Write .bat header ────────────────────────────────────────────────────
    bat_lines = [
        "@echo off",
        # Switch to the bat file's own directory so cmd always has a valid cwd,
        # regardless of what working directory Blender or the shell inherited.
        'cd /d "%~dp0"',
        "setlocal enabledelayedexpansion",
        f"echo SmokeSimLab batch - {len(jobs)} job(s)",
        "echo.",
        "set ERRORS=0",
        "",
    ]

    _dbg = s.collect_debug_log
    _debug_log(_dbg, output_path, "addon",
               f"export_batch: {len(jobs)} job(s)  blend={bpy.data.filepath!r}  "
               f"out={output_path!r}  bpy={bpy.app.version_string}")

    # ── Seed the Job Log list (one row per job, status=NOT_STARTED) ─────────
    s.job_log_items.clear()
    for i, p in enumerate(jobs):
        _log_row = s.job_log_items.add()
        _log_row.job_number = i + 1
        _log_row.job_name   = make_name(p, i)
        _log_row.status     = 'NOT_STARTED'

    # ── Write one JSON + one .bat entry per job ──────────────────────────────
    for i, p in enumerate(jobs):
        name      = make_name(p, i)  # computed once; reused in log row + JSON
        job_path  = os.path.join(jobs_dir, f"job_{i:04d}.json")
        log_path  = os.path.join(jobs_dir, f"job_{i:04d}.log")
        done_path = os.path.join(jobs_dir, f"job_{i:04d}.done")

        job_data = {
            "params":         p,
            "name":           name,
            "blend_file":     blend_file,
            "output_path":    output_path,
            "domain_name":    s.domain_obj.name,
            "frame_start":    frame_start,
            "frame_end":      frame_end,
            "render_mode":    s.render_mode,
            "render_samples": s.render_samples,
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
        _debug_log(_dbg, output_path, "addon", f"job {i}: {name}  params={p}")

        # smoke_launcher.py wraps Blender, detects crash dialogs (WerFault),
        # saves crash logs, and exits non-zero so the batch marks the job failed.
        # Falls back to calling Blender directly if the launcher was not exported.
        if os.path.exists(dest_launcher):
            run_cmd = f'"{python_exe}" "{dest_launcher}" "{blender_exe}" "{job_path}"'
        elif s.render_mode == "EEVEE":
            run_cmd = (
                f'"{blender_exe}" "{blend_file}" '
                f'--window-geometry 0 0 100 100 --factory-startup '
                f'--python "{dest_worker}" -- "{job_path}"'
            )
        else:
            run_cmd = (
                f'"{blender_exe}" "{blend_file}" '
                f'--background --factory-startup '
                f'--python "{dest_worker}" -- "{job_path}" 2>nul'
            )

        bat_lines += [
            f"echo === Job {i+1}/{len(jobs)}: {name} ===",
            run_cmd,
            "if errorlevel 1 (",
            "    echo   WARNING: job exited with error",
            "    set /a ERRORS+=1",
            f'    echo error exit !ERRORLEVEL! {name} %DATE% %TIME%>"{done_path}"',
            ") else (",
            f'    echo done {name} %DATE% %TIME%>"{done_path}"',
            ")",
            "echo.",
        ]

    # ── Write .bat footer ────────────────────────────────────────────────────
    bat_lines += [
        "echo ================================",
        "echo Batch complete.  Errors: %ERRORS%",
        f'echo Results: {os.path.join(output_path, "Renders", "results.csv")}',
        "echo ================================",
        "pause",
    ]

    bat_path = os.path.join(output_path, "run_smoke_batch.bat")
    with open(bat_path, "w") as fh:
        fh.write("\n".join(bat_lines))

    return len(jobs), bat_path


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


class SmokeJobItem(bpy.types.PropertyGroup):
    """One row in the Job Log panel section."""
    job_number: bpy.props.IntProperty(name="Job #",  default=0)
    job_name:   bpy.props.StringProperty(name="Name", default="")
    status:     bpy.props.EnumProperty(
        name="Status",
        items=[
            ('NOT_STARTED', "Not Started", ""),
            ('IN_PROGRESS', "In Progress",  ""),
            ('RETRYING',    "Retrying",     ""),
            ('COMPLETE',    "Complete",     ""),
            ('FAILED',      "Failed",       ""),
        ],
        default='NOT_STARTED',
    )


class SMOKE_UL_job_log(bpy.types.UIList):
    """Job Log list — one row per exported job, colour-coded by status."""

    _STATUS_ICONS = {
        'NOT_STARTED': 'RADIOBUT_OFF',
        'IN_PROGRESS': 'SEQUENCE_COLOR_07',  # blue  — active / running
        'RETRYING':    'SEQUENCE_COLOR_04',  # yellow — transient error, retrying
        'COMPLETE':    'SEQUENCE_COLOR_05',  # green  — success
        'FAILED':      'SEQUENCE_COLOR_01',  # red    — permanent failure
    }

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            split = layout.split(factor=0.10, align=True)
            split.label(icon=self._STATUS_ICONS.get(item.status, 'NONE'), text="")
            inner = split.split(factor=0.22, align=True)
            inner.label(text=str(item.job_number))
            inner.label(text=item.job_name)

    def draw_filter(self, context, layout):
        pass  # suppress the filter / sort bar


class SmokeSettings(bpy.types.PropertyGroup):
    """
    All user-facing settings for SmokeSimLab, stored on bpy.types.Scene.

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
        description="The Mantaflow fluid domain object to bake",
    )

    output_path: bpy.props.StringProperty(
        name="Output",
        description="Root output folder for cache, renders, and CSV",
        subtype='DIR_PATH',
        default="C:/tmp",
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
        default=False,
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
            "After all jobs finish, automatically re-run any that reported errors "
            "once, with Use Existing Cache and Use Placeholders both forced on. "
            "Does not re-retry an already-retried run"
        ),
        default=False,
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
    batch_start_time:     bpy.props.FloatProperty(default=0.0)
    batch_time_remaining: bpy.props.StringProperty(default="")
    batch_job_log_key:    bpy.props.StringProperty(default="")
    batch_job_start_time: bpy.props.FloatProperty(default=0.0)
    batch_frame_end:      bpy.props.IntProperty(default=0)
    batch_jobs_elapsed:      bpy.props.FloatProperty(default=0.0)
    batch_resolution:        bpy.props.IntProperty(default=0)
    batch_render_width:      bpy.props.IntProperty(default=0)
    batch_render_height:     bpy.props.IntProperty(default=0)
    batch_render_mode:       bpy.props.StringProperty(default="CYCLES")
    batch_bake_start_time:   bpy.props.FloatProperty(default=0.0)
    batch_render_start_time: bpy.props.FloatProperty(default=0.0)
    batch_still_start_time:  bpy.props.FloatProperty(default=0.0)
    batch_bake_secs_actual:  bpy.props.FloatProperty(default=-1.0)
    batch_render_secs_actual: bpy.props.FloatProperty(default=-1.0)
    show_results:         bpy.props.BoolProperty(
        name="Display Results When Finished",
        description="After all jobs complete, create a grid of result planes in a SmokeOutput collection",
        default=False,
    )

    # ── Status / UI state ────────────────────────────────────────────────────

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
        if any(v > limit for v in expand_param(s, "resolution")):
            return context.window_manager.invoke_props_dialog(self, width=420)
        return self.execute(context)

    def draw(self, context):
        prefs = context.preferences.addons.get(__name__)
        limit = prefs.preferences.resolution_caution if prefs else 1024
        col = self.layout.column(align=True)
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

        try:
            count, bat_path = export_batch(context)
        except FileNotFoundError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        msg = f"Exported {count} job(s) to {bat_path}"
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
        import json, os
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
        import os
        s = context.scene.smoke_settings
        _load_settings_from_path(s, self.filepath)
        if s.settings_file_path:
            stem = os.path.splitext(os.path.basename(s.settings_file_path))[0]
            s.settings_file_enum = stem
        return {'FINISHED'}


class SMOKE_OT_add_value(bpy.types.Operator):
    """Add a new entry to a parameter explicit-value list."""

    bl_idname = "smoke.add_value"
    bl_label  = "Add Value"

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

    bl_idname = "smoke.remove_value"
    bl_label  = "Remove Value"

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


# Four major sub-tasks: Setup, Baking, Animation, Still
# Each row: (log_keyword, bar-3a label, completed_subtasks_when_detected)
# completed_subtasks = number of major sub-tasks DONE when this keyword first appears.
_STAGES = (
    ("Job started",                "Starting",              0),
    ("Setting up cache",           "Setting up cache",      0),
    ("Freeing previous cache",     "Clearing cache",        0),
    ("Use Existing Cache enabled", "Using existing cache",  2),  # setup + bake both done
    ("Baking...",                  "Baking simulation",     1),  # setup done
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

    A job is considered finished if its log tail contains a done marker OR a
    .done file exists.  The done-marker check handles the sync-lag window where
    .done has not yet arrived on this machine even though the job completed.
    """
    try:
        all_files = set(os.listdir(jobs_dir))
    except OSError:
        return None
    for log_file in reversed(sorted(f for f in all_files if f.endswith(".log"))):
        if log_file[:-4] + ".done" in all_files:
            continue
        try:
            with open(os.path.join(jobs_dir, log_file), "r", errors="replace") as fh:
                tail = fh.read()[-4096:]
        except OSError:
            continue
        if any(marker in tail for marker in _LOG_DONE_MARKERS):
            continue
        return log_file, log_file[:-4], tail
    return None


def _count_vdb_frames(jobs_dir, log_stem):
    """Return (frames_baked, frame_end) by counting VDB files for log_stem, or None."""
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
    data_dir     = os.path.join(output_path, "Cache", name, "data")
    frames_baked = set()
    if os.path.isdir(data_dir):
        for f in os.listdir(data_dir):
            m = re.search(r'_(\d{4})\.vdb$', f)
            if m:
                frames_baked.add(int(m.group(1)))
    return len(frames_baked), frame_end


def _count_png_frames(jobs_dir, log_stem, since=0.0):
    """Return (frames_rendered, frame_end) for log_stem, or None.

    When since > 0 only counts PNG files whose mtime >= since, so re-render
    runs (where old frames exist on disk) report genuine progress instead of
    the inflated total from the previous run.
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
    frames_dir      = os.path.join(output_path, "Renders", f"{name}_frames")
    frames_rendered = 0
    if os.path.isdir(frames_dir):
        for f in os.listdir(frames_dir):
            if not re.match(r'frame_\d{4}\.png$', f):
                continue
            if since > 0:
                try:
                    if os.path.getmtime(os.path.join(frames_dir, f)) < since:
                        continue
                except OSError:
                    continue
            frames_rendered += 1
    return frames_rendered, frame_end


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


def _update_job_log_statuses(s, jobs_dir):
    """Refresh each SmokeJobItem status and drive auto-scroll."""
    global _last_auto_index

    # Detect manual scroll: if job_log_index moved since we last wrote it,
    # the user dragged the list — disable auto-scroll for this run.
    if s.job_log_auto_scroll and s.job_log_index != _last_auto_index:
        s.job_log_auto_scroll = False

    try:
        all_files = set(os.listdir(jobs_dir))
    except OSError:
        return

    active_index = -1
    for idx, item in enumerate(s.job_log_items):
        n          = f"{item.job_number - 1:04d}"   # job_number is 1-based; filenames are 0-based
        retry_done = f"job_{n}_retry.done"
        first_done = f"job_{n}.done"
        retry_log  = f"job_{n}_retry.log"
        first_log  = f"job_{n}.log"

        def _has_error(fname):
            try:
                with open(os.path.join(jobs_dir, fname)) as fh:
                    return "error" in fh.read().lower()
            except OSError:
                return False

        if retry_done in all_files:
            item.status = 'FAILED' if _has_error(retry_done) else 'COMPLETE'
        elif retry_log in all_files:
            item.status = 'RETRYING'
            if active_index < 0:
                active_index = idx
        elif first_done in all_files:
            item.status = 'FAILED' if _has_error(first_done) else 'COMPLETE'
        elif first_log in all_files:
            item.status = 'IN_PROGRESS'
            if active_index < 0:
                active_index = idx
        # else: leave as NOT_STARTED

    if s.job_log_auto_scroll and active_index >= 0:
        s.job_log_index  = active_index
        _last_auto_index = active_index


def _compute_batch_summary(jobs_dir, elapsed_secs):
    """Scan done files and return (line1, line2, line3, line4) summary strings.

    line3 and line4 are empty strings when the respective counts are zero.
    """
    try:
        all_files = os.listdir(jobs_dir)
    except OSError:
        all_files = []

    def _has_error(fname):
        try:
            with open(os.path.join(jobs_dir, fname)) as fh:
                return "error" in fh.read().lower()
        except OSError:
            return False

    first_dones = [f for f in all_files if f.endswith(".done") and "_retry" not in f]
    retry_dones = [f for f in all_files if f.endswith("_retry.done")]

    first_failed_stems = {f[:-5] for f in first_dones if _has_error(f)}

    retry_ok = retry_fail = 0
    for f in retry_dones:
        if _has_error(f):
            retry_fail += 1
        else:
            retry_ok   += 1

    no_retry_fail  = max(len(first_failed_stems) - (retry_ok + retry_fail), 0)
    clean_complete = len(first_dones) - len(first_failed_stems)
    total_complete = clean_complete + retry_ok
    total_failed   = retry_fail + no_retry_fail

    def _n(count, noun):
        return f"{count} {noun}{'s' if count != 1 else ''}"

    line1 = f"All Jobs Finished — {_format_elapsed(elapsed_secs)}"
    line2 = f"{_n(total_complete, 'Job')} Complete"
    line3 = f"{_n(retry_ok,     'Job')} Error, but Retried Successfully" if retry_ok    > 0 else ""
    line4 = f"{_n(total_failed, 'Job')} Failed"                          if total_failed > 0 else ""
    return line1, line2, line3, line4


# ---------------------------------------------------------------------------
# Job log auto-scroll: last index the timer wrote, so we can detect manual scrolls.
_last_auto_index: int = 0

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
    import json as _j
    op = _estim["output_path"]
    if not op:
        return
    record.setdefault("ts", round(time.time(), 2))
    try:
        with open(os.path.join(op, "estim_log.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(_j.dumps(record) + "\n")
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
        print(f"[SmokeSimLab] poll timer error — {_exc}")
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

        done_files = [f for f in os.listdir(jobs_dir) if f.endswith(".done")]
        done  = len(done_files)
        total = s.batch_total

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

            # Auto-retry fires once: only for the initial run (not a retry run).
            # A retry run always has at least one "_retry" in its .done filenames.
            is_retry_run = any("_retry" in f for f in done_files)
            will_auto_retry = (errors > 0 and s.auto_retry_failed and not is_retry_run)

            # Estimation log: batch complete.
            _estim_log({
                "event":        "batch_complete",
                "jobs":         total,
                "errors":       errors,
                "elapsed_secs": round(time.time() - s.batch_start_time, 1),
            })
            _estim["batch_logged"] = False   # allow logging for any future run

            if will_auto_retry:
                s.batch_summary_line1 = s.batch_summary_line2 = ""
                s.batch_summary_line3 = s.batch_summary_line4 = ""
            else:
                elapsed = time.time() - s.batch_start_time
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
            s.batch_job_start_time    = 0.0
            s.batch_frame_end         = 0
            s.batch_jobs_elapsed      = 0.0
            s.batch_resolution        = 0
            s.batch_render_width      = 0
            s.batch_render_height     = 0
            s.batch_render_mode       = "CYCLES"
            s.batch_bake_start_time   = 0.0
            s.batch_render_start_time = 0.0
            s.batch_still_start_time  = 0.0
            s.batch_bake_secs_actual  = -1.0
            s.batch_render_secs_actual = -1.0
            _redraw_panels()
            if will_auto_retry:
                bpy.app.timers.register(_auto_retry_deferred, first_interval=2.0)
            elif s.show_results:
                bpy.app.timers.register(_setup_results_deferred, first_interval=0.5)
            return None

        s.batch_overall_factor = done / total
        s.batch_progress       = f"{done} of {total} job(s) complete"

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
                if _estim["job_key"] and not _estim["job_done_logged"] and s.batch_job_start_time > 0:
                    _prev_elapsed = round(now - s.batch_job_start_time, 1)
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

                if s.batch_job_log_key and s.batch_job_start_time > 0:
                    s.batch_jobs_elapsed += max(now - s.batch_job_start_time, 0.0)
                s.batch_job_log_key    = log_file
                s.batch_job_start_time = now
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
                s.batch_bake_start_time   = 0.0
                s.batch_render_start_time = 0.0
                s.batch_still_start_time  = 0.0
                s.batch_bake_secs_actual  = -1.0
                s.batch_render_secs_actual = -1.0
                s.batch_subtask_text      = ""
                s.batch_subtask_factor    = 0.0
                s.batch_job_text          = ""
                s.batch_job_factor        = 0.0

            frame_end      = max(s.batch_frame_end, 1)
            elapsed_in_job = max(now - s.batch_job_start_time, 0.0) if s.batch_job_start_time > 0 else 0.0

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
            if stage_label == "Baking simulation" and s.batch_bake_start_time == 0.0:
                s.batch_bake_start_time = now
                if not _estim["bake_start_logged"]:
                    _estim["bake_start_logged"] = True
                    _setup_actual = round(now - s.batch_job_start_time, 1) if s.batch_job_start_time > 0 else None
                    _estim_log({
                        "event":            "bake_start",
                        "job":              _estim["job_name"],
                        "est_bake_secs":    _estim["est_bake_0"],
                        "setup_actual_secs": _setup_actual,
                        "setup_est_secs":   _SETUP_SECS_DEFAULT,
                    })
            if stage_label == "Rendering animation" and s.batch_render_start_time == 0.0:
                s.batch_render_start_time = now
                if not _estim["render_start_logged"]:
                    _estim["render_start_logged"] = True
                    _estim_log({
                        "event":           "render_start",
                        "job":             _estim["job_name"],
                        "est_render_secs": _estim["est_render_0"],
                        "bake_actual_secs": round(s.batch_bake_secs_actual, 1)
                                           if s.batch_bake_secs_actual >= 0 else None,
                    })
            if stage_label == "Rendering still" and s.batch_still_start_time == 0.0:
                s.batch_still_start_time = now
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
                elif stage_completed >= 2 and s.batch_bake_start_time > 0:
                    s.batch_bake_secs_actual = now - s.batch_bake_start_time
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
                    and s.batch_render_start_time > 0):
                s.batch_render_secs_actual = now - s.batch_render_start_time
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
                bake_info = _count_vdb_frames(jobs_dir, log_stem)
                if bake_info:
                    baked, total_frames = bake_info
                    frames_baked = baked
                    if total_frames > 0:
                        subtask_text   = f"Baking ({baked} of {total_frames})"
                        subtask_factor = baked / total_frames

            elif stage_label == "Rendering animation":
                # Use mtime-based count (since job start) so pre-existing PNGs
                # from a previous run are not counted as this run's progress.
                render_info = _count_png_frames(
                    jobs_dir, log_stem, since=s.batch_job_start_time)
                if render_info:
                    rendered, _ = render_info
                    if render_target > 0:
                        frames_rendered = min(rendered, render_target)
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
            if (s.batch_bake_start_time > 0 or s.batch_bake_secs_actual >= 0
                    or s.batch_render_start_time > 0 or s.batch_still_start_time > 0):
                setup_remaining = 0.0
            else:
                setup_remaining = max(_SETUP_SECS_DEFAULT - elapsed_in_job, 0.0)

            # Bake: actual → real-time rate estimate → default
            if s.batch_bake_secs_actual >= 0:
                bake_remaining = 0.0
            elif s.batch_bake_start_time > 0 and frames_baked > 0:
                elapsed_bake = max(now - s.batch_bake_start_time, 0.0)
                if elapsed_bake > 0:
                    rate           = elapsed_bake / frames_baked
                    bake_remaining = rate * max(frame_end - frames_baked, 0)
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
                bake_remaining = default_bake_secs

            # Render: actual → real-time rate → default.
            # Guard against frames_rendered == render_target (directory fully
            # pre-populated) while the stage is still active — use elapsed
            # time against the default estimate to avoid dropping to 0.
            if s.batch_render_secs_actual >= 0:
                render_remaining = 0.0
            elif s.batch_render_start_time > 0 and 0 < frames_rendered < render_target:
                elapsed_render = max(now - s.batch_render_start_time, 0.0)
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
                        now - s.batch_render_start_time
                        if s.batch_render_start_time > 0 else 0.0), 0.0)
            else:
                render_remaining = max(default_render_secs - (
                    now - s.batch_render_start_time
                    if s.batch_render_start_time > 0 else 0.0), 0.0)

            # Still: done once stage_completed >= 4; countdown if started; else default
            if stage_completed >= 4:
                still_remaining = 0.0
                # Estimation log: still and job complete (once each).
                if not _estim["still_done_logged"] and s.batch_still_start_time > 0:
                    _estim["still_done_logged"] = True
                    _still_actual = round(now - s.batch_still_start_time, 1)
                    _estim_log({
                        "event":       "still_actual",
                        "job":         _estim["job_name"],
                        "actual_secs": _still_actual,
                        "est_secs":    _STILL_SECS_DEFAULT,
                        "ratio":       round(_still_actual / _STILL_SECS_DEFAULT, 3),
                    })
                if not _estim["job_done_logged"]:
                    _estim["job_done_logged"] = True
                    _job_elapsed = round(now - s.batch_job_start_time, 1) if s.batch_job_start_time > 0 else 0.0
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
            elif s.batch_still_start_time > 0:
                still_remaining = max(
                    _STILL_SECS_DEFAULT - (now - s.batch_still_start_time), 0.0)
            else:
                still_remaining = _STILL_SECS_DEFAULT

            job_remaining = setup_remaining + bake_remaining + render_remaining + still_remaining
            if job_remaining < 0:
                import sys
                print(
                    f"[SmokeSimLab] WARNING: negative job_remaining={job_remaining:.1f}  "
                    f"setup={setup_remaining:.1f}  bake={bake_remaining:.1f}  "
                    f"render={render_remaining:.1f}  still={still_remaining:.1f}  "
                    f"bake_start={s.batch_bake_start_time:.0f}  frames_baked={frames_baked}  "
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

    def invoke(self, context, event):
        if bpy.data.is_dirty:
            return context.window_manager.invoke_props_dialog(self, width=380)
        return self.execute(context)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="The .blend file has unsaved changes.", icon='ERROR')
        col.label(text="It is recommended to save before running batch.")
        col.separator()
        col.label(text="Click OK to run anyway, or Cancel to save first.")

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

        # Remove old log files so the counter starts from zero.
        if os.path.isdir(jobs_dir):
            for f in os.listdir(jobs_dir):
                if f.endswith(".log") or f.endswith(".done"):
                    try:
                        os.remove(os.path.join(jobs_dir, f))
                    except OSError:
                        pass

        global _last_auto_index
        s.batch_summary_line1 = s.batch_summary_line2 = ""
        s.batch_summary_line3 = s.batch_summary_line4 = ""
        s.show_job_log           = True
        s.job_log_auto_scroll    = True
        _last_auto_index         = 0
        for _it in s.job_log_items:
            _it.status = 'NOT_STARTED'
        s.batch_total          = len(job_files)
        s.batch_jobs_dir       = jobs_dir
        s.batch_progress       = f"0 of {len(job_files)} job(s) complete"
        s.batch_overall_factor = 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0
        s.batch_start_time     = time.time()
        s.batch_time_remaining = "Estimating..."
        s.batch_job_log_key       = ""
        s.batch_job_start_time    = 0.0
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        s.batch_bake_start_time   = 0.0
        s.batch_render_start_time = 0.0
        s.batch_still_start_time  = 0.0
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0

        # Launch the bat in a new console window; returns immediately.
        # cwd is set to output_path so the new cmd starts with a valid directory.
        subprocess.Popen(
            ["cmd", "/c", "start", "SmokeSimLab Batch", bat_path],
            shell=False,
            cwd=output_path,
        )

        if not bpy.app.timers.is_registered(_poll_batch_progress):
            bpy.app.timers.register(_poll_batch_progress, first_interval=5.0)

        self.report({'INFO'}, f"Batch started — {len(job_files)} job(s) queued")
        return {'FINISHED'}


class SMOKE_OT_open_docs(bpy.types.Operator):
    """Open the SmokeSimLab documentation in a web browser."""

    bl_idname  = "smoke.open_docs"
    bl_label   = "Documentation"
    bl_description = "Open the SmokeSimLab documentation on GitHub"

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
            if not f.endswith(".done"):
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
            f"echo SmokeSimLab retry — {len(failed)} failed job(s)",
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
            "pause",
        ]

        bat_path = os.path.join(output_path, "run_retry_failed.bat")
        with open(bat_path, "w") as fh:
            fh.write("\n".join(bat_lines))

        # Remove all .done markers for the jobs being retried (both original and
        # any prior _retry) so they are counted as "in progress" by the poll timer.
        for base_stem, _ in failed:
            for suffix in ("", "_retry"):
                try:
                    os.remove(os.path.join(jobs_dir, base_stem + suffix + ".done"))
                except OSError:
                    pass

        # Reset progress tracking so the panel shows bars instead of the
        # "All N complete" message while the retry jobs are running.
        total_jobs = len([f for f in os.listdir(jobs_dir)
                          if f.endswith(".json") and "_retry" not in f])
        done_now   = len([f for f in os.listdir(jobs_dir) if f.endswith(".done")])

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
        s.batch_start_time     = time.time()
        s.batch_time_remaining = "Estimating..."
        s.batch_job_log_key       = ""
        s.batch_job_start_time    = 0.0
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        s.batch_bake_start_time   = 0.0
        s.batch_render_start_time = 0.0
        s.batch_still_start_time  = 0.0
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0

        if not bpy.app.timers.is_registered(_poll_batch_progress):
            bpy.app.timers.register(_poll_batch_progress, first_interval=5.0)

        _redraw_panels()

        subprocess.Popen(
            ["cmd", "/c", "start", "SmokeSimLab Retry", bat_path],
            shell=False,
            cwd=output_path,
        )
        self.report({'INFO'}, f"Retry started — {len(failed)} job(s) queued")
        return {'FINISHED'}


def _auto_retry_deferred():
    """Called from a timer; runs Retry Failed Jobs automatically."""
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
        s.job_log_items.clear()
        s.batch_total            = 0
        s.batch_jobs_dir         = ""
        s.batch_overall_factor   = 0.0
        s.batch_subtask_text     = ""
        s.batch_subtask_factor   = 0.0
        s.batch_job_text         = ""
        s.batch_job_factor       = 0.0
        s.batch_start_time       = 0.0
        s.batch_time_remaining   = ""
        s.batch_job_log_key      = ""
        s.batch_job_start_time   = 0.0
        s.batch_frame_end        = 0
        s.batch_jobs_elapsed     = 0.0
        s.batch_resolution       = 0
        s.batch_render_width     = 0
        s.batch_render_height    = 0
        s.batch_render_mode      = "CYCLES"
        s.batch_bake_start_time   = 0.0
        s.batch_render_start_time = 0.0
        s.batch_still_start_time  = 0.0
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0

        _redraw_panels()

        if skipped:
            self.report({'WARNING'}, f"Removed {len(deleted)} item(s); could not remove: {', '.join(skipped)}")
        else:
            self.report({'INFO'}, f"Removed {len(deleted)} exported item(s)")
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
    import os
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
    extra_props : list of (prop_name, label) tuples drawn before the default value
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
        for prop_name, prop_label in extra_props:
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

    _sub_param_ui(box, s, "vorticity", "Vorticity")
    _sub_param_ui(box, s, "alpha",     "Buoyancy Density")
    _sub_param_ui(box, s, "beta",      "Buoyancy Heat")


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


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class SMOKE_PT_panel(bpy.types.Panel):
    """
    Main SmokeLab panel in the 3D Viewport N-panel (Sidebar → SmokeLab tab).

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

    bl_label       = "Smoke Lab"
    bl_idname      = "SMOKE_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'SmokeLab'

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
        layout.label(text=f"SmokeSimLab v{version}", icon='TOOL_SETTINGS')

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

            _standalone_param_ui(box_sim, s, "dissolve_speed", "Dissolve",
                                 show_prop="show_dissolve",
                                 enable_prop="use_dissolve",
                                 extra_props=[
                                     ("iterate_dissolve_both", "Iterate Both On and Off"),
                                     ("slow_dissolve", "Slow Dissolve"),
                                 ])
            box_sim.separator()

            _noise_ui(box_sim, s)

        layout.separator()

        # ── Iteration mode + job count ────────────────────────────────────
        job_count = sum(1 for _ in generate_jobs(s))
        box  = layout.box()
        box.label(text="Iteration Mode:")
        box.prop(s, "iteration_mode", expand=True)
        box.label(text=f"{job_count} job(s) will be created")

        layout.separator()

        # ── Render settings ──────────────────────────────────────────────────
        layout.prop(s, "use_placeholders",   text="Use Placeholders")
        row_cache = layout.row()
        row_cache.separator(factor=2.0)
        sub_cache = row_cache.column()
        sub_cache.enabled = not s.use_placeholders
        sub_cache.prop(s, "use_existing_cache", text="Use Existing Cache")
        layout.prop(s, "auto_retry_failed",  text="Automatically Retry Failed Jobs")
        row = layout.row()
        row.prop(s, "render_mode",    text="Render Engine")
        row.prop(s, "render_samples", text="Samples")
        layout.operator(
            "smoke.export_batch",
            text=f"Export Batch  ({job_count} jobs)",
            icon='EXPORT',
        )

        # Status line from last export (word-wrapped at 60 chars)
        if s.last_export_info:
            col = layout.column(align=True)
            col.scale_y = 0.75
            info = s.last_export_info
            col.label(text=info[:60])
            if len(info) > 60:
                col.label(text=info[60:])

        layout.separator()
        layout.prop(s, "show_results")
        layout.operator("smoke.run_batch", text="Run Batch", icon='PLAY')

        if s.batch_summary_line1:
            layout.label(text=s.batch_summary_line1, icon='CHECKMARK')
            layout.label(text=s.batch_summary_line2)
            if s.batch_summary_line3:
                layout.label(text=s.batch_summary_line3)
            if s.batch_summary_line4:
                layout.label(text=s.batch_summary_line4)
                layout.operator("smoke.retry_failed", icon='FILE_REFRESH')
            layout.operator("smoke.setup_results", icon='IMAGE_DATA')
        elif s.batch_progress:
            # Bar 3a — current sub-task (what is happening right now)
            if s.batch_subtask_text:
                try:
                    layout.progress(factor=s.batch_subtask_factor, type='BAR',
                                    text=s.batch_subtask_text)
                except AttributeError:
                    layout.label(text=s.batch_subtask_text)

            # Bar 3b — job stage progress (how many sub-tasks are complete)
            if s.batch_job_text:
                try:
                    layout.progress(factor=s.batch_job_factor, type='BAR',
                                    text=s.batch_job_text)
                except AttributeError:
                    layout.label(text=s.batch_job_text)

            # Bar 3c — overall job count (X of Y jobs complete)
            try:
                layout.progress(factor=s.batch_overall_factor, type='BAR',
                                text=s.batch_progress)
            except AttributeError:
                layout.label(text=s.batch_progress, icon='TIME')

            if s.batch_time_remaining:
                layout.label(text=s.batch_time_remaining, icon='TIME')

        layout.separator()

        # ── Job Log (only shown once populated; auto-expands on Run Batch) ──────
        if s.job_log_items:
            box_log = layout.box()
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
            box_util.operator("smoke.remove_all_jobs", text="Remove All Jobs", icon='TRASH')


# ---------------------------------------------------------------------------
# File-load reset
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
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
        s.output_path = "C:/tmp"

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
        s.use_noise             = False
        s.iterate_noise_both    = False

        s.use_default_frames = True
        s.sim_frame_start    = 1
        s.sim_frame_end      = 250

        # ── Settings presets ──────────────────────────────────────────────────
        s.settings_file_path   = ""
        s.settings_search_path = ""
        s.settings_snapshot    = ""
        s.settings_file_enum   = ""

        # ── Text objects ──────────────────────────────────────────────────────
        s.text_resolution = "Resolution_Text"
        s.text_noise      = "Noise_Text"
        s.text_dissolve   = "Dissolve_Text"
        s.text_time       = "Time_Text"

        # ── Render / export settings ──────────────────────────────────────────
        s.render_mode        = 'CYCLES'
        s.render_samples     = 16
        s.maintain_density   = False
        s.iteration_mode     = 'LIMITED'
        s.use_placeholders   = False
        s.use_existing_cache = False
        s.auto_retry_failed  = False
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
        s.job_log_auto_scroll    = True
        s.job_log_items.clear()
        s.batch_total          = 0
        s.batch_jobs_dir       = ""
        s.batch_overall_factor = 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0
        s.batch_start_time     = 0.0
        s.batch_time_remaining = ""
        s.batch_job_log_key       = ""
        s.batch_job_start_time    = 0.0
        s.batch_frame_end         = 0
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        s.batch_bake_start_time   = 0.0
        s.batch_render_start_time = 0.0
        s.batch_still_start_time  = 0.0
        s.batch_bake_secs_actual  = -1.0
        s.batch_render_secs_actual = -1.0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    ValueItem,
    SMOKE_UL_value_list,
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
    SMOKE_OT_open_docs,
    SMOKE_OT_retry_failed,
    SMOKE_OT_setup_results,
    SMOKE_OT_remove_all_jobs,
    SMOKE_PT_panel,
]


def register():
    """Register all classes and attach SmokeSettings to Scene."""
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.smoke_settings = bpy.props.PointerProperty(type=SmokeSettings)
    if _reset_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_reset_on_load)
    _reset_on_load()  # also reset when scripts are reloaded


def unregister():
    """Unregister all classes and remove the Scene property."""
    if _reset_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_reset_on_load)
    if bpy.app.timers.is_registered(_poll_batch_progress):
        bpy.app.timers.unregister(_poll_batch_progress)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "smoke_settings"):
        del bpy.types.Scene.smoke_settings
