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
    "author":      "SmokeSimLab",
    "version":     (0, 1, 18),
    "blender":     (4, 0, 0),
    "location":    "View3D > Sidebar > SmokeLab",
    "description": "Batch smoke simulation parameter sweeper with CSV logging",
    "doc_url":     "https://github.com/rickpalo/SmokeSimLab",
    "tracker_url": "https://github.com/rickpalo/SmokeSimLab/issues",
    "category":    "Render",
}

import bpy
import math
import os
import shutil
import itertools
import json
import subprocess

print(f"SmokeSimLab {'.'.join(str(v) for v in bl_info['version'])} loaded")

# Placeholder GitHub/documentation URL.  Update this when you have a real URL.
DOCS_URL = "https://github.com/rickpalo/SmokeSimLab"

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


# ---------------------------------------------------------------------------
# Parameter expansion
# ---------------------------------------------------------------------------

def expand_param(s, name):
    """
    Return a list of values for iterable parameter *name* from SmokeSettings *s*.

    Priority order:
      1. Explicit list  — user-entered values in the UIList
      2. Range          — begin/end/step sweep
      3. Base value     — single default value (no iteration)

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
    base = getattr(s, name)

    # Mode 1: explicit list
    if getattr(s, name + "_use_list"):
        lst  = getattr(s, name + "_list")
        vals = [i.value for i in lst]
        return vals if vals else [base]

    # Mode 2: range sweep
    if getattr(s, name + "_use_range"):
        begin = getattr(s, name + "_begin")
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

    # Mode 3: single default
    return [base]


def _default_job(s):
    """
    Return a job-parameter dict using only the default (base) value for
    every parameter.  Used as the baseline in Limited Combinations mode.
    """
    return {
        "resolution":          s.resolution,
        "vorticity":           s.vorticity,
        "alpha":               s.alpha,
        "beta":                s.beta,
        "dissolve_speed":      s.dissolve_speed,
        "noise_upres":         s.noise_upres,
        "noise_strength":      s.noise_strength,
        "noise_spatial_scale": s.noise_spatial_scale,
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


def generate_jobs_all(s):
    """
    All Combinations mode (original behaviour).

    Yields one job per element of the Cartesian product of all parameter
    ranges.  The total job count is the product of all range lengths, which
    can grow very large when multiple parameters have wide ranges.

    Parameters
    ----------
    s : SmokeSettings

    Yields
    ------
    dict — job parameter dict suitable for JSON serialisation
    """
    def param(name):
        return expand_param(s, name)

    res      = param("resolution")
    vort     = param("vorticity")
    alpha    = param("alpha")
    beta     = param("beta")
    dissolve = param("dissolve_speed") if s.use_dissolve else [s.dissolve_speed]

    if s.use_noise:
        nu  = param("noise_upres")
        ns  = param("noise_strength")
        nss = param("noise_spatial_scale")
    else:
        nu  = [s.noise_upres]
        ns  = [s.noise_strength]
        nss = [s.noise_spatial_scale]

    for combo in itertools.product(res, vort, alpha, beta, dissolve, nu, ns, nss):
        yield {
            "resolution":          combo[0],
            "vorticity":           combo[1],
            "alpha":               combo[2],
            "beta":                combo[3],
            "dissolve_speed":      combo[4],
            "noise_upres":         combo[5],
            "noise_strength":      combo[6],
            "noise_spatial_scale": combo[7],
            "use_dissolve":        s.use_dissolve,
            "slow_dissolve":       s.slow_dissolve,
            "use_noise":           s.use_noise,
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
    if os.path.isdir(jobs_dir):
        shutil.rmtree(jobs_dir)
    os.makedirs(jobs_dir)

    # Use the currently running Blender instance
    blender_exe = bpy.app.binary_path
    blend_file  = bpy.data.filepath
    frame_end   = context.scene.frame_end
    jobs        = list(generate_jobs(s))

    # ── Locate and copy worker script ────────────────────────────────────────
    # __file__ is reliable here because we are installed as a proper addon,
    # so it points to the SmokeSimLab folder, not the .blend file.
    addon_dir   = os.path.dirname(os.path.abspath(__file__))
    src_worker  = os.path.join(addon_dir, "smoke_worker.py")
    dest_worker = os.path.join(output_path, "smoke_worker.py")

    if not os.path.exists(src_worker):
        raise FileNotFoundError(
            f"smoke_worker.py not found in addon folder.\n"
            f"Expected: {src_worker}\n"
            f"Re-install the SmokeSimLab addon."
        )
    shutil.copy2(src_worker, dest_worker)

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

    # ── Write one JSON + one .bat entry per job ──────────────────────────────
    for i, p in enumerate(jobs):
        name      = make_name(p, i)
        job_path  = os.path.join(jobs_dir, f"job_{i:04d}.json")
        log_path  = os.path.join(jobs_dir, f"job_{i:04d}.log")
        done_path = os.path.join(jobs_dir, f"job_{i:04d}.done")

        job_data = {
            "params":         p,
            "name":           name,
            "output_path":    output_path,
            "domain_name":    s.domain_obj.name,
            "frame_end":      frame_end,
            "render_mode":    s.render_mode,
            "use_placeholders": s.use_placeholders,
            "use_existing_cache": s.use_existing_cache,
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

        if s.render_mode == "EEVEE":
            blender_cmd = (
                f'"{blender_exe}" "{blend_file}" '
                f'--window-geometry 0 0 100 100 --factory-startup '
                f'--python "{dest_worker}" -- "{job_path}"'
            )
        else:
            blender_cmd = (
                f'"{blender_exe}" "{blend_file}" '
                f'--background --factory-startup '
                f'--python "{dest_worker}" -- "{job_path}"'
            )

        # stdout is NOT redirected so live output appears in the batch window.
        # stderr (Blender C++ startup noise) is suppressed with 2>nul.
        # The worker writes its own log file via _log() for progress tracking.
        bat_lines += [
            f"echo === Job {i+1}/{len(jobs)}: {name} ===",
            f'{blender_cmd} 2>nul',
            "if errorlevel 1 (",
            "    echo   WARNING: job exited with error",
            "    set /a ERRORS+=1",
            f'    echo error>"{done_path}"',
            ") else (",
            f'    echo done>"{done_path}"',
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

    # ── Resolution ───────────────────────────────────────────────────────────

    show_resolution: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Resolution section",
    )
    resolution:           bpy.props.IntProperty(
        default=64, min=8,
        description="Default resolution (longest domain side). "
                    "Blender default is 32; 64 is a common starting point",
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

    # Vorticity — d.vorticity — adds turbulent detail to smoke
    vorticity:           bpy.props.FloatProperty(
        default=1.0,
        description="Default vorticity. Controls turbulent swirling detail. "
                    "Blender default is 0.0; higher = more swirl",
    )
    vorticity_begin:     bpy.props.FloatProperty(default=1.0)
    vorticity_end:       bpy.props.FloatProperty(default=1.0)
    vorticity_step:      bpy.props.FloatProperty(default=0)
    vorticity_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("vorticity"))
    vorticity_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("vorticity"))
    vorticity_list:      bpy.props.CollectionProperty(type=ValueItem)
    vorticity_index:     bpy.props.IntProperty()

    # Alpha — d.alpha — buoyancy based on smoke density
    alpha:           bpy.props.FloatProperty(
        default=1.0, min=-5.0, max=5.0,
        description="Default buoyancy density (alpha). Positive = smoke rises. "
                    "Blender default is 1.0",
    )
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
    beta:           bpy.props.FloatProperty(
        default=1.0, min=-5.0, max=5.0,
        description="Default buoyancy heat (beta). Controls how temperature "
                    "affects rising. Blender default is 1.0",
    )
    beta_begin:     bpy.props.FloatProperty(default=1.0)
    beta_end:       bpy.props.FloatProperty(default=1.0)
    beta_step:      bpy.props.FloatProperty(default=0)
    beta_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("beta"))
    beta_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("beta"))
    beta_list:      bpy.props.CollectionProperty(type=ValueItem)
    beta_index:     bpy.props.IntProperty()

    # ── Dissolve ─────────────────────────────────────────────────────────────

    show_dissolve: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Dissolve section",
    )
    use_dissolve: bpy.props.BoolProperty(
        default=False,
        description="Enable smoke dissolve (smoke fades out over time)",
    )
    slow_dissolve: bpy.props.BoolProperty(
        default=False,
        description="Use logarithmic (slow) dissolve instead of linear",
    )
    dissolve_speed: bpy.props.IntProperty(
        default=50,
        description="Default dissolve speed in frames. Blender default is 5",
    )
    dissolve_speed_begin:     bpy.props.IntProperty(default=50)
    dissolve_speed_end:       bpy.props.IntProperty(default=50)
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

    # Noise scale — d.noise_scale — upres factor
    noise_upres: bpy.props.IntProperty(
        default=2,
        description="Default noise upres factor. Blender default is 2",
    )
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
    noise_strength: bpy.props.FloatProperty(
        default=1.0,
        description="Default noise strength. Blender default is 1.0",
    )
    noise_strength_begin:     bpy.props.FloatProperty(default=1.0)
    noise_strength_end:       bpy.props.FloatProperty(default=1.0)
    noise_strength_step:      bpy.props.FloatProperty(default=0)
    noise_strength_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_strength"))
    noise_strength_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_strength"))
    noise_strength_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_strength_index:     bpy.props.IntProperty()

    # Noise position scale — d.noise_pos_scale
    noise_spatial_scale: bpy.props.FloatProperty(
        default=1.0,
        description="Default noise position scale. Blender default is 1.0",
    )
    noise_spatial_scale_begin:     bpy.props.FloatProperty(default=1.0)
    noise_spatial_scale_end:       bpy.props.FloatProperty(default=1.0)
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

    use_placeholders: bpy.props.BoolProperty(
        name="Use Placeholders",
        description="Skip rendering frames that already exist. Useful for resuming interrupted batch jobs",
        default=False,
    )

    use_existing_cache: bpy.props.BoolProperty(
        name="Use Existing Cache",
        description="Skip baking if cache files already exist for this job. Useful when Blender crashes during testing",
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
    batch_complete_msg:   bpy.props.StringProperty(default="")
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


class SMOKE_OT_add_value(bpy.types.Operator):
    """Add a new entry to a parameter explicit-value list."""

    bl_idname = "smoke.add_value"
    bl_label  = "Add Value"

    param: bpy.props.StringProperty()

    def execute(self, context):
        s       = context.scene.smoke_settings
        lst     = getattr(s, self.param + "_list")
        default = float(getattr(s, self.param))

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
    ("Playblasting",               "Rendering animation",   2),
    ("Anim Reviewer complete",     "Animation complete",    2),
    ("frame sequence complete",    "Animation complete",    2),
    ("MP4 conversion complete",    "Encoding MP4",          2),
    ("Rendering final frame",      "Rendering still",       3),  # animation done
    ("PNG render complete",        "Still complete",        4),  # still done
    ("Done. Results",              "Writing results",       4),
)
_TOTAL_SUBTASKS = 4


def _read_job_stage(jobs_dir):
    """Return (subtask_label, subtask_factor, job_factor, job_text) for the running job."""
    try:
        all_files = set(os.listdir(jobs_dir))
    except OSError:
        return "", 0.0, 0.0, ""
    for log_file in reversed(sorted(f for f in all_files if f.endswith(".log"))):
        if log_file[:-4] + ".done" in all_files:
            continue
        try:
            with open(os.path.join(jobs_dir, log_file), "r", errors="replace") as fh:
                tail = fh.read()[-4096:]
        except OSError:
            continue
        for keyword, label, completed in reversed(_STAGES):
            if keyword in tail:
                subtask_factor = min((completed + 0.5) / _TOTAL_SUBTASKS, 1.0)
                job_factor     = completed / _TOTAL_SUBTASKS
                job_text       = f"Stage {completed} of {_TOTAL_SUBTASKS} complete"
                return label, subtask_factor, job_factor, job_text
        return "Starting", 0.1, 0.0, f"Stage 0 of {_TOTAL_SUBTASKS} complete"
    return "", 0.0, 0.0, ""


def _poll_batch_progress():
    """
    Timer callback — updates overall and sub-task progress bars.
    Registered when Run Batch is clicked; unregisters itself when all jobs are done.
    Returns the next interval (seconds) or None to stop.
    """
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

        if done >= total:
            errors = 0
            for df in done_files:
                try:
                    with open(os.path.join(jobs_dir, df), "r") as fh:
                        if "error" in fh.read().lower():
                            errors += 1
                except OSError:
                    pass
            err_txt = f"  ({errors} error(s))" if errors else ""
            s.batch_complete_msg   = f"All {total} job(s) complete{err_txt}"
            s.batch_progress       = ""
            s.batch_overall_factor = 0.0
            s.batch_subtask_text   = ""
            s.batch_subtask_factor = 0.0
            s.batch_job_text       = ""
            s.batch_job_factor     = 0.0
            s.batch_jobs_dir       = ""
            _redraw_panels()
            if s.show_results:
                bpy.app.timers.register(_setup_results_deferred, first_interval=0.5)
            return None

        s.batch_overall_factor = done / total
        s.batch_progress       = f"{done} of {total} job(s) complete"
        (s.batch_subtask_text, s.batch_subtask_factor,
         s.batch_job_factor, s.batch_job_text) = _read_job_stage(jobs_dir)
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

        # Remove old log files so the counter starts from zero.
        if os.path.isdir(jobs_dir):
            for f in os.listdir(jobs_dir):
                if f.endswith(".log") or f.endswith(".done"):
                    try:
                        os.remove(os.path.join(jobs_dir, f))
                    except OSError:
                        pass

        s.batch_complete_msg   = ""
        s.batch_total          = len(job_files)
        s.batch_jobs_dir       = jobs_dir
        s.batch_progress       = f"0 of {len(job_files)} job(s) complete"
        s.batch_overall_factor = 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0

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

        failed = []
        for f in sorted(os.listdir(jobs_dir)):
            if not f.endswith(".done") or f.endswith("_retry.done"):
                continue
            try:
                with open(os.path.join(jobs_dir, f)) as fh:
                    if "error" not in fh.read().lower():
                        continue
                stem     = f[:-5]
                job_json = os.path.join(jobs_dir, stem + ".json")
                if os.path.exists(job_json):
                    failed.append((stem, job_json))
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

        for stem, job_json in failed:
            with open(job_json) as fh:
                job_data = json.load(fh)

            job_data["use_placeholders"]   = True
            job_data["use_existing_cache"] = True

            log_path  = os.path.join(jobs_dir, stem + "_retry.log")
            done_path = os.path.join(jobs_dir, stem + "_retry.done")
            job_data["log_path"] = log_path

            retry_json = os.path.join(jobs_dir, stem + "_retry.json")
            with open(retry_json, "w") as fh:
                json.dump(job_data, fh, indent=2)

            name        = job_data.get("name", stem)
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

        subprocess.Popen(
            ["cmd", "/c", "start", "SmokeSimLab Retry", bat_path],
            shell=False,
            cwd=output_path,
        )
        self.report({'INFO'}, f"Retry started — {len(failed)} job(s) queued")
        return {'FINISHED'}


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
        pw   = 1.0
        ph   = aspect
        gap  = 0.05

        planes = []
        for idx, png_path in enumerate(png_files):
            row = idx // cols
            col = idx % cols
            x   = col * (pw + gap)
            y   = -row * (ph + gap)

            bpy.ops.mesh.primitive_plane_add(size=1.0, location=(x, y, 0.0))
            obj = context.active_object
            obj.scale = (pw / 2.0, ph / 2.0, 1.0)
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

        # Switch active 3D viewport to top view, frame planes, material preview
        for area in context.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            bpy.ops.object.select_all(action='DESELECT')
            for ob in planes:
                ob.select_set(True)
            if planes:
                context.view_layer.objects.active = planes[0]
            with context.temp_override(area=area):
                bpy.ops.view3d.view_axis(type='TOP')
                bpy.ops.view3d.view_selected()
                area.spaces.active.shading.type = 'MATERIAL'
            break

        self.report({'INFO'}, f"SmokeOutput: {len(planes)} result plane(s) created")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------

def _sub_param_ui(box, s, name, label):
    """
    Draw range/list controls for a sub-parameter inside an existing box.

    Used for Gas sub-params (vorticity, alpha, beta) and Noise sub-params
    where the outer collapsible box already exists and we only need to draw
    the Default value + Range/List controls.

    Parameters
    ----------
    box   : bpy UILayout — the enclosing box to draw into
    s     : SmokeSettings
    name  : str — base parameter name, e.g. "vorticity"
    label : str — human-readable label shown above the controls
    """
    box.separator()
    box.label(text=f"{label}:")
    box.prop(s, name, text="Default")    # renamed from "Base Value"

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
        return
    if enable_prop and not getattr(s, enable_prop):
        return

    if extra_props:
        for prop_name, prop_label in extra_props:
            box.prop(s, prop_name, text=prop_label)

    box.prop(s, name, text="Default")   # renamed from "Base Value"

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

        # ── Domain and output ─────────────────────────────────────────────
        layout.prop(s, "domain_obj", text="Domain Object")
        layout.prop(s, "output_path")

        layout.separator()

        # ── Parameter sections ────────────────────────────────────────────
        _standalone_param_ui(layout, s, "resolution", "Resolution",
                             show_prop="show_resolution")
        layout.separator()

        _gas_ui(layout, s)
        layout.separator()

        _standalone_param_ui(layout, s, "dissolve_speed", "Dissolve",
                             show_prop="show_dissolve",
                             enable_prop="use_dissolve",
                             extra_props=[("slow_dissolve", "Slow Dissolve")])
        layout.separator()

        _noise_ui(layout, s)
        layout.separator()

        # ── Text Objects ──────────────────────────────────────────────────
        box = layout.box()
        row = box.row()
        row.prop(s, "show_text_objects",
                 icon='TRIA_DOWN' if s.show_text_objects else 'TRIA_RIGHT',
                 emboss=False, text="")
        row.label(text="Text Objects")
        if s.show_text_objects:
            box.prop(s, "text_resolution", text="Resolution")
            box.prop(s, "text_noise",      text="Noise")
            box.prop(s, "text_dissolve",   text="Dissolve")
            box.prop(s, "text_time",       text="Bake Time")

        layout.separator()

        # ── Iteration mode + job count ────────────────────────────────────
        job_count = sum(1 for _ in generate_jobs(s))
        box  = layout.box()
        box.label(text="Iteration Mode:")
        box.prop(s, "iteration_mode", expand=True)   # radio buttons
        box.label(text=f"{job_count} job(s) will be created")

        layout.separator()

        # ── Render settings ──────────────────────────────────────────────────
        layout.prop(s, "use_placeholders", text="Use Placeholders")
        layout.prop(s, "use_existing_cache", text="Use Existing Cache")
        layout.prop(s, "render_mode", text="Render Engine")
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

        if s.batch_complete_msg:
            layout.label(text=s.batch_complete_msg, icon='CHECKMARK')
            if "error" in s.batch_complete_msg:
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


# ---------------------------------------------------------------------------
# File-load reset
# ---------------------------------------------------------------------------

@bpy.app.handlers.persistent
def _reset_on_load(dummy=None):
    """
    Clear all transient parameter state whenever a .blend file is opened.

    Resets: lists, use_range/use_list flags, step values, iteration mode,
    render options, export/batch status.
    Preserves: domain_obj, output_path, render_mode, text object names,
    and the base default values for each parameter.
    """
    # bpy.data is a _RestrictData object during addon install — bail out early.
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        return

    for scene in scenes:
        s = getattr(scene, "smoke_settings", None)
        if s is None:
            continue

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
        s.vorticity_begin          = 1.0
        s.vorticity_end            = 1.0
        s.alpha_step               = 0.0
        s.alpha_begin              = 1.0
        s.alpha_end                = 1.0
        s.beta_step                = 0.0
        s.beta_begin               = 1.0
        s.beta_end                 = 1.0
        s.dissolve_speed_step      = 0
        s.dissolve_speed_begin     = 50
        s.dissolve_speed_end       = 50
        s.noise_upres_step         = 0
        s.noise_upres_begin        = 2
        s.noise_upres_end          = 2
        s.noise_strength_step      = 0.0
        s.noise_strength_begin     = 1.0
        s.noise_strength_end       = 1.0
        s.noise_spatial_scale_step  = 0.0
        s.noise_spatial_scale_begin = 1.0
        s.noise_spatial_scale_end   = 1.0

        s.iteration_mode     = 'LIMITED'
        s.use_placeholders   = False
        s.use_existing_cache = False
        s.show_results       = False

        s.last_export_info     = ""
        s.batch_complete_msg   = ""
        s.batch_progress       = ""
        s.batch_total          = 0
        s.batch_jobs_dir       = ""
        s.batch_overall_factor = 0.0
        s.batch_subtask_text   = ""
        s.batch_subtask_factor = 0.0
        s.batch_job_text       = ""
        s.batch_job_factor     = 0.0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    ValueItem,
    SMOKE_UL_value_list,
    SmokeSettings,
    SmokeSimLabPreferences,
    SMOKE_OT_export_batch,
    SMOKE_OT_run_batch,
    SMOKE_OT_add_value,
    SMOKE_OT_remove_value,
    SMOKE_OT_open_docs,
    SMOKE_OT_retry_failed,
    SMOKE_OT_setup_results,
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
