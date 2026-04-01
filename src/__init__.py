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
Full documentation: https://github.com/YOUR_USERNAME/SmokeSimLab

Requires Blender 4.x (tested on 4.5.5).
"""

# ---------------------------------------------------------------------------
# Blender addon metadata — required for proper addon registration.
# Blender reads bl_info to display the addon in Preferences → Add-ons.
# ---------------------------------------------------------------------------
bl_info = {
    "name":        "SmokeSimLab",
    "author":      "SmokeSimLab",
    "version":     (1, 2, 0),
    "blender":     (4, 0, 0),
    "location":    "View3D > Sidebar > SmokeLab",
    "description": "Batch smoke simulation parameter sweeper with CSV logging",
    "doc_url":     "https://github.com/YOUR_USERNAME/SmokeSimLab",
    "tracker_url": "https://github.com/YOUR_USERNAME/SmokeSimLab/issues",
    "category":    "Render",
}

import bpy
import os
import shutil
import itertools
import json

# Placeholder GitHub/documentation URL.  Update this when you have a real URL.
DOCS_URL = "https://github.com/YOUR_USERNAME/SmokeSimLab"

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


def _param_has_range(s, name):
    """
    Return True if parameter *name* has a non-trivial range or list defined
    (i.e. more than one value would be produced by expand_param).
    Used by generate_jobs_limited to decide which parameters to sweep.
    """
    vals = expand_param(s, name)
    return len(vals) > 1


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
        vals = expand_param(s, param_name)
        if len(vals) <= 1:
            continue  # no range defined for this param — skip

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
    os.makedirs(jobs_dir, exist_ok=True)

    blend_file  = bpy.data.filepath
    blender_exe = bpy.app.binary_path
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
        "setlocal enabledelayedexpansion",
        f"echo SmokeSimLab batch - {len(jobs)} job(s)",
        "echo.",
        "set ERRORS=0",
        "",
    ]

    # ── Write one JSON + one .bat entry per job ──────────────────────────────
    for i, p in enumerate(jobs):
        name     = make_name(p, i)
        job_data = {
            "params":      p,
            "name":        name,
            "output_path": output_path,
            "domain_name": s.domain_obj.name,
            "frame_end":   frame_end,
            "render_mode": s.render_mode,
            "text_objects": {
                "resolution": s.text_resolution,
                "noise":      s.text_noise,
                "dissolve":   s.text_dissolve,
                "time":       s.text_time,
            },
        }
        job_path = os.path.join(jobs_dir, f"job_{i:04d}.json")
        with open(job_path, "w") as fh:
            json.dump(job_data, fh, indent=2)

        log_path = os.path.join(jobs_dir, f"job_{i:04d}.log")

        # EEVEE requires a visible window for OpenGL context.
        # Cycles works reliably in --background mode.
        if s.render_mode == "EEVEE":
            blender_cmd = (
                f'"{blender_exe}" "{blend_file}" '
                f'--window-geometry 0 0 100 100 --factory-startup '
                f'--python "{dest_worker}" -- "{job_path}" '
            )
        else:
            blender_cmd = (
                f'"{blender_exe}" "{blend_file}" '
                f'--background --factory-startup '
                f'--python "{dest_worker}" -- "{job_path}" '
            )

        bat_lines += [
            f"echo === Job {i+1}/{len(jobs)}: {name} ===",
            f'{blender_cmd}> "{log_path}" 2>&1',
            "if errorlevel 1 (",
            "    echo   WARNING: job exited with error",
            "    set /a ERRORS+=1",
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

    The UIList template requires a PropertyGroup to hold each list item.
    int_value is reserved for future use (e.g. integer-only parameters).
    """
    value:     bpy.props.FloatProperty()
    int_value: bpy.props.IntProperty()


class SMOKE_UL_value_list(bpy.types.UIList):
    """
    Custom UIList that renders each ValueItem as an editable float field.
    Using emboss=True makes the field look like a standard input rather than
    a plain label, so users can click to edit values directly.
    """
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        layout.prop(item, "value", text="", emboss=True)


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
        default=64,
        description="Default resolution (longest domain side). "
                    "Blender default is 32; 64 is a common starting point",
    )
    resolution_begin:     bpy.props.IntProperty(default=64)
    resolution_end:       bpy.props.IntProperty(default=128)
    resolution_step:      bpy.props.IntProperty(default=32)
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
    vorticity_begin:     bpy.props.FloatProperty(default=0.5)
    vorticity_end:       bpy.props.FloatProperty(default=2.0)
    vorticity_step:      bpy.props.FloatProperty(default=0.5)
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
    alpha_begin:     bpy.props.FloatProperty(default=0.0)
    alpha_end:       bpy.props.FloatProperty(default=2.0)
    alpha_step:      bpy.props.FloatProperty(default=0.5)
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
    beta_begin:     bpy.props.FloatProperty(default=0.0)
    beta_end:       bpy.props.FloatProperty(default=2.0)
    beta_step:      bpy.props.FloatProperty(default=0.5)
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
    dissolve_speed_begin:     bpy.props.IntProperty(default=0)
    dissolve_speed_end:       bpy.props.IntProperty(default=100)
    dissolve_speed_step:      bpy.props.IntProperty(default=25)
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
    noise_upres_begin:     bpy.props.IntProperty(default=1)
    noise_upres_end:       bpy.props.IntProperty(default=3)
    noise_upres_step:      bpy.props.IntProperty(default=1)
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
    noise_strength_begin:     bpy.props.FloatProperty(default=0.5)
    noise_strength_end:       bpy.props.FloatProperty(default=2.0)
    noise_strength_step:      bpy.props.FloatProperty(default=0.5)
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
    noise_spatial_scale_end:       bpy.props.FloatProperty(default=3.0)
    noise_spatial_scale_step:      bpy.props.FloatProperty(default=1.0)
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

    # ── Status / UI state ────────────────────────────────────────────────────

    last_export_info: bpy.props.StringProperty(
        default="",
        description="Status message shown after the last Export Batch operation",
    )


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

    def execute(self, context):
        s = context.scene.smoke_settings

        if not s.domain_obj:
            self.report({'ERROR'}, "No domain object selected")
            return {'CANCELLED'}

        if not bpy.data.filepath:
            self.report({'ERROR'},
                "Please save the .blend file first — "
                "the batch launcher needs its absolute path")
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


class SMOKE_OT_add_value(bpy.types.Operator):
    """Add a new entry to a parameter explicit-value list."""

    bl_idname = "smoke.add_value"
    bl_label  = "Add Value"

    # Which parameter list to append to — set by the UI button
    param: bpy.props.StringProperty()

    def execute(self, context):
        s   = context.scene.smoke_settings
        lst = getattr(s, self.param + "_list")
        lst.add()
        return {'FINISHED'}


class SMOKE_OT_remove_value(bpy.types.Operator):
    """Remove the selected entry from a parameter explicit-value list."""

    bl_idname = "smoke.remove_value"
    bl_label  = "Remove Value"

    # Which parameter list to remove from — set by the UI button
    param: bpy.props.StringProperty()

    def execute(self, context):
        s   = context.scene.smoke_settings
        lst = getattr(s, self.param + "_list")
        idx = getattr(s, self.param + "_index")
        if len(lst) > 0:
            lst.remove(idx)
            # Clamp index so it stays within the (now shorter) list
            setattr(s, self.param + "_index", max(min(idx, len(lst) - 1), 0))
        return {'FINISHED'}


class SMOKE_OT_open_docs(bpy.types.Operator):
    """Open the SmokeSimLab documentation in a web browser."""

    bl_idname  = "smoke.open_docs"
    bl_label   = "Documentation"
    bl_description = "Open the SmokeSimLab documentation on GitHub"

    def execute(self, context):
        bpy.ops.wm.url_open(url=DOCS_URL)
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
        jobs = list(generate_jobs(s))
        box  = layout.box()
        box.label(text="Iteration Mode:")
        box.prop(s, "iteration_mode", expand=True)   # radio buttons
        box.label(text=f"{len(jobs)} job(s) will be created")

        layout.separator()

        # ── Render engine + export button ─────────────────────────────────
        layout.prop(s, "render_mode", text="Render Engine")
        layout.operator(
            "smoke.export_batch",
            text=f"Export Batch  ({len(jobs)} jobs)",
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


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = [
    ValueItem,
    SMOKE_UL_value_list,
    SmokeSettings,
    SMOKE_OT_export_batch,
    SMOKE_OT_add_value,
    SMOKE_OT_remove_value,
    SMOKE_OT_open_docs,
    SMOKE_PT_panel,
]


@bpy.app.handlers.persistent
def _clear_lists(scene):
    """
    Clear all parameter value lists after a .blend file loads.

    This prevents stale list entries from a previous session from carrying
    over into a new file.  Registered as a load_post handler so it runs
    after the file is fully loaded (not during restricted registration context).

    Parameters
    ----------
    scene : bpy.types.Scene — the active scene after load (may be None on
            some Blender versions; the hasattr guard handles this)
    """
    if hasattr(scene, "smoke_settings"):
        s = scene.smoke_settings
        for param in ITERABLE_PARAMS:
            getattr(s, param + "_list").clear()


def register():
    """
    Register all classes, attach SmokeSettings to Scene, and add the
    load_post handler that clears stale list items.

    Note: bpy.context.scene must NOT be accessed here — the context is
    restricted during addon installation.  Use _clear_lists (via load_post)
    for any per-scene initialisation instead.
    """
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.smoke_settings = bpy.props.PointerProperty(
        type=SmokeSettings)
    bpy.app.handlers.load_post.append(_clear_lists)


def unregister():
    """
    Unregister all classes, remove the Scene property, and clean up the
    load_post handler.  Called when the addon is disabled or Blender exits.
    """
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "smoke_settings"):
        del bpy.types.Scene.smoke_settings
    if _clear_lists in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_clear_lists)
