"""
SmokeSimLab/__init__.py
=======================
Blender 4.x addon — SmokeLab tab in the 3D Viewport N-panel.

Installation
------------
1. Zip the SmokeSimLab folder (containing __init__.py and smoke_worker.py).
2. In Blender: Edit → Preferences → Add-ons → Install → select the zip.
3. Enable the addon in the list.

Workflow
--------
1. Set your domain object and output path in the SmokeLab panel.
2. Configure parameter ranges/lists for resolution, vorticity, dissolve, noise.
3. Save your .blend file.
4. Click "Export Batch" — writes to <output_path>:
       run_smoke_batch.bat     Windows batch launcher
       smoke_worker.py         copy of the worker script
       jobs/job_NNNN.json      one JSON config per parameter combination
5. Double-click run_smoke_batch.bat in Windows Explorer.
   Each job opens a fresh Blender instance, bakes, renders, writes CSV, exits.

Requires Blender 4.x (tested on 4.5.5).
"""

bl_info = {
    "name":        "SmokeSimLab",
    "author":      "SmokeSimLab",
    "version":     (1, 1, 0),
    "blender":     (4, 0, 0),
    "location":    "View3D > Sidebar > SmokeLab",
    "description": "Batch smoke simulation parameter sweeper with CSV logging",
    "category":    "Render",
}

import bpy
import os
import shutil
import itertools
import json


# ---------------------------------------------------------------------------
# Toggle helpers
# ---------------------------------------------------------------------------

def make_toggle_range(name):
    def update(self, context):
        if getattr(self, name + "_use_range"):
            setattr(self, name + "_use_list", False)
    return update


def make_toggle_list(name):
    def update(self, context):
        if getattr(self, name + "_use_list"):
            setattr(self, name + "_use_range", False)
    return update


# ---------------------------------------------------------------------------
# Parameter expansion and job generation
# ---------------------------------------------------------------------------

def expand_param(s, name):
    """
    Return list of values for *name* — list > range > base value.
    Uses epsilon tolerance on range end to handle floating point imprecision
    (e.g. 0.2 * 5 = 1.0000000000000002 in IEEE 754).
    """
    base = getattr(s, name)
    if getattr(s, name + "_use_list"):
        lst  = getattr(s, name + "_list")
        vals = [i.value for i in lst]
        return vals if vals else [base]
    if getattr(s, name + "_use_range"):
        begin = getattr(s, name + "_begin")
        end   = getattr(s, name + "_end")
        step  = getattr(s, name + "_step")
        if step == 0:
            return [begin]
        vals, v = [], begin
        epsilon = step * 1e-6  # tolerance for float boundary
        while v <= end + epsilon:
            vals.append(round(v, 6))  # avoid 0.200000000001 in filenames
            v += step
        return vals
    return [base]


def generate_jobs(s):
    """Yield one job-parameter dict per Cartesian combination."""
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


def make_name(p, index=0):
    """Build a unique filename stem from job parameters."""
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
    Copy smoke_worker.py to output_path, write per-job JSON files, and
    generate run_smoke_batch.bat.  Returns (job_count, bat_path).
    """
    s = context.scene.smoke_settings

    output_path = bpy.path.abspath(s.output_path)
    jobs_dir    = os.path.join(output_path, "jobs")
    os.makedirs(jobs_dir, exist_ok=True)

    blend_file  = bpy.data.filepath
    blender_exe = bpy.app.binary_path
    frame_end   = context.scene.frame_end
    jobs        = list(generate_jobs(s))

    # Locate worker script next to this __init__.py inside the addon folder
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

    # Write per-job JSON files and bat launcher
    bat_lines = [
        "@echo off",
        "setlocal enabledelayedexpansion",
        f"echo SmokeSimLab batch - {len(jobs)} job(s)",
        "echo.",
        "set ERRORS=0",
        "",
    ]

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
    """Single float entry in a parameter value list."""
    value:     bpy.props.FloatProperty()
    int_value: bpy.props.IntProperty()


class SMOKE_UL_value_list(bpy.types.UIList):
    """Editable list row."""
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        layout.prop(item, "value", text="", emboss=True)


class SmokeSettings(bpy.types.PropertyGroup):
    """All user-facing settings stored on bpy.types.Scene."""

    domain_obj:  bpy.props.PointerProperty(type=bpy.types.Object)
    output_path: bpy.props.StringProperty(
        name="Output", subtype='DIR_PATH', default="C:/tmp")

    # Resolution
    resolution:           bpy.props.IntProperty(default=64)
    resolution_begin:     bpy.props.IntProperty(default=64)
    resolution_end:       bpy.props.IntProperty(default=128)
    resolution_step:      bpy.props.IntProperty(default=32)
    resolution_use_range: bpy.props.BoolProperty(
        default=True, update=make_toggle_range("resolution"))
    resolution_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("resolution"))
    resolution_list:      bpy.props.CollectionProperty(type=ValueItem)
    resolution_index:     bpy.props.IntProperty()

    # ── Gas Parameters ──────────────────────────────────────────────────────

    show_gas: bpy.props.BoolProperty(default=True)

    # Vorticity (d.vorticity)
    vorticity:           bpy.props.FloatProperty(default=1.0)
    vorticity_begin:     bpy.props.FloatProperty(default=0.5)
    vorticity_end:       bpy.props.FloatProperty(default=2.0)
    vorticity_step:      bpy.props.FloatProperty(default=0.5)
    vorticity_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("vorticity"))
    vorticity_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("vorticity"))
    vorticity_list:      bpy.props.CollectionProperty(type=ValueItem)
    vorticity_index:     bpy.props.IntProperty()

    # Buoyancy Density — alpha (d.alpha)
    alpha:           bpy.props.FloatProperty(default=1.0, min=-5.0, max=5.0)
    alpha_begin:     bpy.props.FloatProperty(default=0.0)
    alpha_end:       bpy.props.FloatProperty(default=2.0)
    alpha_step:      bpy.props.FloatProperty(default=0.5)
    alpha_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("alpha"))
    alpha_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("alpha"))
    alpha_list:      bpy.props.CollectionProperty(type=ValueItem)
    alpha_index:     bpy.props.IntProperty()

    # Buoyancy Heat — beta (d.beta)
    beta:           bpy.props.FloatProperty(default=1.0, min=-5.0, max=5.0)
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

    use_dissolve:             bpy.props.BoolProperty(default=True)
    slow_dissolve:            bpy.props.BoolProperty(default=False)
    dissolve_speed:           bpy.props.IntProperty(default=50)
    dissolve_speed_begin:     bpy.props.IntProperty(default=0)
    dissolve_speed_end:       bpy.props.IntProperty(default=100)
    dissolve_speed_step:      bpy.props.IntProperty(default=25)
    dissolve_speed_use_range: bpy.props.BoolProperty(
        default=True, update=make_toggle_range("dissolve_speed"))
    dissolve_speed_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("dissolve_speed"))
    dissolve_speed_list:      bpy.props.CollectionProperty(type=ValueItem)
    dissolve_speed_index:     bpy.props.IntProperty()

    # ── Noise ────────────────────────────────────────────────────────────────

    use_noise:                     bpy.props.BoolProperty(default=True)
    noise_upres:                   bpy.props.IntProperty(default=1)
    noise_upres_begin:             bpy.props.IntProperty(default=1)
    noise_upres_end:               bpy.props.IntProperty(default=3)
    noise_upres_step:              bpy.props.IntProperty(default=1)
    noise_upres_use_range:         bpy.props.BoolProperty(
        default=True, update=make_toggle_range("noise_upres"))
    noise_upres_use_list:          bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_upres"))
    noise_upres_list:              bpy.props.CollectionProperty(type=ValueItem)
    noise_upres_index:             bpy.props.IntProperty()

    noise_strength:                bpy.props.FloatProperty(default=1.0)
    noise_strength_begin:          bpy.props.FloatProperty(default=0.5)
    noise_strength_end:            bpy.props.FloatProperty(default=2.0)
    noise_strength_step:           bpy.props.FloatProperty(default=0.5)
    noise_strength_use_range:      bpy.props.BoolProperty(
        default=True, update=make_toggle_range("noise_strength"))
    noise_strength_use_list:       bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_strength"))
    noise_strength_list:           bpy.props.CollectionProperty(type=ValueItem)
    noise_strength_index:          bpy.props.IntProperty()

    noise_spatial_scale:           bpy.props.FloatProperty(default=1.0)
    noise_spatial_scale_begin:     bpy.props.FloatProperty(default=1.0)
    noise_spatial_scale_end:       bpy.props.FloatProperty(default=3.0)
    noise_spatial_scale_step:      bpy.props.FloatProperty(default=1.0)
    noise_spatial_scale_use_range: bpy.props.BoolProperty(
        default=True, update=make_toggle_range("noise_spatial_scale"))
    noise_spatial_scale_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_spatial_scale"))
    noise_spatial_scale_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_spatial_scale_index:     bpy.props.IntProperty()

    # ── Text object names ────────────────────────────────────────────────────

    text_resolution:   bpy.props.StringProperty(default="Resolution_Text")
    text_noise:        bpy.props.StringProperty(default="Noise_Text")
    text_dissolve:     bpy.props.StringProperty(default="Dissolve_Text")
    text_time:         bpy.props.StringProperty(default="Time_Text")
    show_text_objects: bpy.props.BoolProperty(default=False)

    # ── UI collapse toggles ──────────────────────────────────────────────────

    show_resolution: bpy.props.BoolProperty(default=True)
    show_dissolve:   bpy.props.BoolProperty(default=True)
    show_noise:      bpy.props.BoolProperty(default=True)

    # ── Render / export ──────────────────────────────────────────────────────

    render_mode: bpy.props.EnumProperty(
        name="Render Mode",
        description="Engine for final still render (EEVEE requires windowed mode)",
        items=[
            ('CYCLES', "Cycles GPU", "Reliable in background mode"),
            ('EEVEE',  "EEVEE",      "Faster but requires windowed mode"),
        ],
        default='CYCLES',
    )

    last_export_info: bpy.props.StringProperty(default="")


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class SMOKE_OT_export_batch(bpy.types.Operator):
    """Export .bat launcher, worker script, and per-job JSON files."""
    bl_idname = "smoke.export_batch"
    bl_label  = "Export Batch"

    def execute(self, context):
        s = context.scene.smoke_settings

        if not s.domain_obj:
            self.report({'ERROR'}, "No domain object selected")
            return {'CANCELLED'}

        if not bpy.data.filepath:
            self.report({'ERROR'},
                "Please save the .blend file first — "
                "the batch launcher needs its path")
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
    """Add a new entry to a parameter value list."""
    bl_idname = "smoke.add_value"
    bl_label  = "Add Value"
    param: bpy.props.StringProperty()

    def execute(self, context):
        s   = context.scene.smoke_settings
        lst = getattr(s, self.param + "_list")
        lst.add()
        return {'FINISHED'}


class SMOKE_OT_remove_value(bpy.types.Operator):
    """Remove the selected entry from a parameter value list."""
    bl_idname = "smoke.remove_value"
    bl_label  = "Remove Value"
    param: bpy.props.StringProperty()

    def execute(self, context):
        s   = context.scene.smoke_settings
        lst = getattr(s, self.param + "_list")
        idx = getattr(s, self.param + "_index")
        if len(lst) > 0:
            lst.remove(idx)
            setattr(s, self.param + "_index", max(min(idx, len(lst) - 1), 0))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------

# Maps parameter base name → show_prop name for non-standard cases
_SHOW_PROP_MAP = {
    "dissolve_speed": "show_dissolve",
    "noise_upres":    "show_noise",
    "vorticity":      "show_gas",
    "alpha":          "show_gas",
    "beta":           "show_gas",
}


def _param_ui(layout, s, name, label, enable_prop=None, extra_props=None):
    """
    Draw a collapsible parameter block with optional enable checkbox.
    Used for standalone sections (Resolution, Dissolve) and individual
    sub-params inside the Gas and Noise group boxes.
    """
    show_prop = _SHOW_PROP_MAP.get(name, f"show_{name}")

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

    box.prop(s, name, text="Base Value")

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


def _sub_param_ui(box, s, name, label):
    """
    Draw range/list controls for a sub-parameter inside an existing box.
    Used for Gas and Noise sub-params where the outer box already exists.
    """
    box.separator()
    box.label(text=f"{label}:")
    box.prop(s, name, text="Base Value")
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
    """Draw the Gas Parameters section: Vorticity, Buoyancy Density, Heat."""
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
    """Draw the Noise section with Scale, Strength, Position Scale."""
    box = layout.box()
    row = box.row()
    row.prop(s, "show_noise",
             icon='TRIA_DOWN' if s.show_noise else 'TRIA_RIGHT',
             emboss=False, text="")
    row.prop(s, "use_noise", text="")
    row.label(text="Noise")

    if not s.show_noise or not s.use_noise:
        return

    for sub_name, sub_label in [
        ("noise_upres",         "Scale"),
        ("noise_strength",      "Strength"),
        ("noise_spatial_scale", "Position Scale"),
    ]:
        _sub_param_ui(box, s, sub_name, sub_label)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------

class SMOKE_PT_panel(bpy.types.Panel):
    bl_label       = "Smoke Lab"
    bl_idname      = "SMOKE_PT_panel"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'SmokeLab'

    def draw(self, context):
        s      = context.scene.smoke_settings
        layout = self.layout

        layout.prop(s, "domain_obj", text="Domain Object")
        layout.prop(s, "output_path")
        layout.separator()

        _param_ui(layout, s, "resolution", "Resolution")
        layout.separator()
        _gas_ui(layout, s)
        layout.separator()
        _param_ui(layout, s, "dissolve_speed", "Dissolve",
                  enable_prop="use_dissolve",
                  extra_props=[("slow_dissolve", "Slow Dissolve")])
        layout.separator()
        _noise_ui(layout, s)
        layout.separator()

        # Text objects
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
        jobs = list(generate_jobs(s))
        layout.prop(s, "render_mode", text="Render Engine")
        layout.operator("smoke.export_batch",
                        text=f"Export Batch  ({len(jobs)} jobs)",
                        icon='EXPORT')
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
    SMOKE_PT_panel,
]

_LIST_PARAMS = [
    "resolution", "vorticity", "alpha", "beta",
    "dissolve_speed",
    "noise_upres", "noise_strength", "noise_spatial_scale",
]


@bpy.app.handlers.persistent
def _clear_lists(scene):
    """Clear stale list items — called after blend file loads."""
    if hasattr(scene, "smoke_settings"):
        s = scene.smoke_settings
        for param in _LIST_PARAMS:
            getattr(s, param + "_list").clear()


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.smoke_settings = bpy.props.PointerProperty(
        type=SmokeSettings)
    bpy.app.handlers.load_post.append(_clear_lists)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "smoke_settings"):
        del bpy.types.Scene.smoke_settings
    if _clear_lists in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_clear_lists)
