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
v3.0.0 (fluid) roadmap.  The source folder/package was renamed
SmokeSimLab -> BatchSimLab at v0.9.3.  Lowercase runtime identifiers and
operator IDs retain the "smoke*" prefix (e.g. scene.smoke_settings, SMOKE_*
classes, .smokesettings presets) for backwards compatibility with existing
.blend saves and keymaps; that part of the rename is deliberately deferred.

Installation
------------
1. Zip the BatchSimLab folder (containing __init__.py + smoke_worker.py +
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

Package layout (TODO-58 split, in progress)
--------------------------------------------
This file is being decomposed into a package.  Pure/leaf clusters have been
extracted into sibling modules and are re-imported below so existing
``from BatchSimLab import …`` entry points and the test-suite resolve unchanged:
  * ``jobgen.py``     — pure job-generation core (param expansion → make_name).
  * ``emitters.py``   — fluid-emitter discovery + sync (pure, duck-typed).
  * ``settings_io.py``— .smokesettings preset save/load + preset-dropdown enums.
  * ``progress.py``   — the PURE half of the bar/ETA machinery (scanners,
                        estimator, formatters).  The STATEFUL poll engine and its
                        rebindable module globals deliberately stay in this file.
Remaining to extract: ``properties.py`` (PropertyGroups), ``operators.py``,
``ui.py`` — then this file keeps only registration.  See TODOS.md → TODO-58.

Documentation
-------------
Full documentation: https://github.com/rickpalo/BatchSimLab

Requires Blender 4.x (tested on 4.5.5 and 5.1.1) on Windows 10/11.  May work on other OSes but the batch export is
"""

# ---------------------------------------------------------------------------
# Blender addon metadata — required for proper addon registration.
# Blender reads bl_info to display the addon in Preferences → Add-ons.
# ---------------------------------------------------------------------------
bl_info = {
    "name":        "BatchSimLab",
    "author":      "Rick Palo",
    "version":     (0, 9, 5),
    "blender":     (4, 0, 0),
    "location":    "View3D > Sidebar > BatchLab",
    "description": "Batch smoke simulation parameter sweeper with CSV logging "
                   "(roadmap: fire @ v2.0.0, fluid @ v3.0.0)",
    "doc_url":     "https://github.com/rickpalo/BatchSimLab/blob/main/DOCUMENTATION.md",
    "tracker_url": "https://github.com/rickpalo/BatchSimLab/issues",
    "category":    "Fluid Simulation",
}

import bpy
import math
import os
import re
import shutil
import json
import subprocess
import sys
import time

# TODO-58 module #1: the pure, bpy-free job-generation core lives in jobgen.py.
# Re-imported here so existing `from BatchSimLab import …` entry points and the
# job-gen tests keep resolving against the package namespace unchanged.
from .jobgen import (
    ITERABLE_PARAMS,
    _VELOCITY_DEFAULT,
    _parse_velocity_vector,
    expand_param,
    _first_value,
    _default_job,
    generate_jobs_limited,
    generate_jobs_all,
    _EMITTER_SCALARS,
    _EMITTER_VELOCITY_SCALARS,
    _emitter_velocity_vectors,
    _emitter_baseline,
    _default_emitters,
    _emitter_sweep_axes,
    _emitter_combinations,
    generate_jobs,
    _dedupe_jobs,
    _fmt_num,
    _OFF_SUFFIX,
    _EMITTER_NAME_DEFAULTS,
    _EMITTER_NAME_ABBR,
    _emitter_name_tokens,
    make_name,
    _format_velocity_vector,
)

# TODO-58 module #2: fluid-emitter discovery + sync (also pure / bpy-free; it is
# duck-typed on the scene/object/settings it's handed).  Re-imported here so the
# operators/UI call sites and the TODO-55 emitter tests keep resolving.
from .emitters import (
    _blend_domain_resolution,
    _is_flow_object,
    find_fluid_emitters,
    _world_aabb,
    _aabb_overlap,
    emitters_inside_domain,
    find_emitters,
    _emitter_sync_plan,
    _EMITTER_FLOW_IMPORT_MAP,
    _flow_settings_of,
    _seed_emitter_from_flow,
    _populate_emitters,
)

# TODO-58 module #3: .smokesettings preset save/load + the dynamic preset-file
# EnumProperty callbacks.  Re-imported here (ABOVE the SmokeSettings class, whose
# class body references the two enum callbacks) so the operators/UI call sites and
# the settings tests keep resolving against the package namespace.
from .settings_io import (
    _SWEEP_PARAMS,
    _settings_dict,
    _apply_settings_dict,
    _load_settings_from_path,
    _is_settings_dirty,
    _SETTINGS_ENUM_SENTINEL,
    _settings_items_cache,
    _settings_files_enum_items,
    _on_settings_enum_update,
)

# TODO-58 module #4: the PURE half of the batch-progress machinery — filesystem
# scanners, the phase-aware ETA estimator, and the formatters (+ their constants).
# Re-imported here so the stateful poll engine (which stays in this module,
# together with its rebindable _bt/_estim/_job_* globals) and the progress/ETA
# tests keep resolving against the package namespace.
from .progress import (
    _SETUP_SECS_DEFAULT,
    _STILL_SECS_DEFAULT,
    _DONE_RE,
    _RETRY_DONE_RE,
    _CRASHED_RE,
    _LOG_DONE_MARKERS,
    _find_running_log,
    _count_vdb_frames,
    _count_png_frames,
    _format_eta,
    _estimate_batch_remaining,
    _format_elapsed,
    _has_error,
    _compute_batch_summary,
)

# TODO-58 module #5: the bpy.props PropertyGroups + their class-body callback
# factories.  Re-imported here ABOVE the registration code (the classes = [...]
# list and register()) so SmokeSettings et al. resolve against the package
# namespace and existing `from BatchSimLab import …` call sites + the property/
# section tests keep working unchanged.
from .properties import (
    make_toggle_range,
    make_toggle_list,
    _sync_frame_defaults,
    _DOMAIN_IMPORT_MAP,
    _import_domain_params,
    _on_render_sim_result_update,
    ValueItem,
    VelocityItem,
    EmitterSettings,
    SmokeJobItem,
    SmokeSettings,
)

# TODO-58 module #6 (Tiers 1+2): the CRUD / settings / emitter / export operators
# + their helpers (and _PARAM_BOUNDS).  Re-imported here so the classes = [...]
# registration list, the panel's operator call-sites, and the test suite resolve
# against the package namespace.  The stateful run/poll engine + its operators
# stay in this module (see operators.py docstring).
from .operators import (
    _PARAM_BOUNDS,
    _scene_has_camera,
    _find_next_job_index,
    _existing_jobs_for_bat,
    _job_run_cmd,
    _job_bat_block,
    _batch_ready,
    export_batch,
    _next_list_value,
    _emitter_of,
    SMOKE_OT_export_batch,
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
)

# TODO-58 module #6b: the stateful run/poll engine + its six run operators.
# Re-imported here (AFTER .operators, which engine.py depends on) so the
# classes = [...] registration list, the panel, the load handlers, and the test
# suite resolve against the package namespace.  The in-place batch state
# (_job_statuses/_job_log_rows/_batch_times/_estim/_poll_state) and the rebindable
# scalars now live in engine.py; staying code mutates the state in place through
# these same objects.  The two REBINDABLE scalars (_last_auto_index /
# _auto_retry_count) are deliberately NOT re-exported: a re-imported int is a stale
# snapshot once engine rebinds it, and nothing outside engine reads them.
from .engine import (
    _BAKE_DONE_RE,
    _RENDER_DONE_RE,
    _batch_is_running,
    _STAGES,
    _TOTAL_SUBTASKS,
    _update_job_log_statuses,
    _batch_times,
    _bt,
    _bt_set,
    _bt_reset_all,
    _job_statuses,
    _job_log_rows,
    _MAX_AUTO_RETRIES,
    _should_auto_retry,
    _estim,
    _debug_log,
    _estim_log,
    _estim_reset_job,
    _POLLER_STALE_SECS,
    _poll_state,
    _poll_batch_progress,
    _poll_batch_progress_impl,
    _redraw_panels,
    _auto_retry_deferred,
    _setup_results_deferred,
    SMOKE_OT_run_batch,
    SMOKE_OT_retry_failed,
    SMOKE_OT_setup_results,
    SMOKE_OT_remove_all_jobs,
    SMOKE_OT_monitor_existing_jobs,
    SMOKE_OT_reset_to_defaults,
)

ADDON_VERSION = ".".join(str(v) for v in bl_info["version"])
print(f"BatchSimLab {ADDON_VERSION} loaded")


# In-addon HELP button target — the full reference (TODO-56).  The repository
# and source folder are now "BatchSimLab"; only the lowercase runtime identifiers
# (smoke_settings, SMOKE_*, .smokesettings) keep the legacy "smoke" prefix for
# .blend/keymap compatibility.
DOCS_URL = "https://github.com/rickpalo/BatchSimLab/blob/main/DOCUMENTATION.md"

# Expected version strings in the helper files exported to the output folder.
# When Run Batch detects a mismatch it warns the user to re-run Export Batch.
# Keep these in sync with WORKER_VERSION / LAUNCHER_VERSION in those files.
_EXPECTED_WORKER_VERSION   = "0.9.1"
_EXPECTED_LAUNCHER_VERSION = "0.6.4"


def _read_helper_version(path: str, var_name: str) -> str:
    """Return the version string for var_name from the first 200 lines of path.

    Returns "" if the file is missing or the variable is not found.

    BUG-017: the scan was capped at 30 lines, but smoke_launcher.py's module
    docstring pushes ``LAUNCHER_VERSION`` to line 33 — so Run Batch always
    reported the launcher version as '' and warned of a phantom mismatch.  The
    cap is generous now so a longer docstring can't reintroduce the false
    positive; these helper files are small so reading further is cheap.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i >= 200:
                    break
                line = line.strip()
                if line.startswith(var_name + " ="):
                    m = re.search(r'["\']([^"\']+)["\']', line)
                    if m:
                        return m.group(1)
    except OSError:
        pass
    return ""

# ITERABLE_PARAMS (the sweepable base-parameter names) now lives in jobgen.py and
# is re-imported above.  Any parameter added there must also have corresponding
# properties in SmokeSettings (name, name_begin, name_end, name_step,
# name_use_range, name_use_list, name_list, name_index).


# Bake/render estimation RATE constants moved to engine.py (module #6b — used only
# by the poll engine's per-job time estimator).


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
# Batch export
# ---------------------------------------------------------------------------


# v0.9.0 TODO-55: emitter Initial Velocity is swept as a list of "x, y, z"
# vectors.  The velocity-text helpers (_VELOCITY_DEFAULT / _parse_velocity_vector
# / _format_velocity_vector) now live in jobgen.py and are re-imported above so
# the UI and job generation share one definition; only this UI-only entry-format
# hint stays here.
_VELOCITY_FORMAT_HINT = "x, y, z  (e.g. 0, 0, 1)"


# ---------------------------------------------------------------------------
# Property groups
# ---------------------------------------------------------------------------


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

        # Use the module-level ADDON_VERSION captured at import time.  Blender
        # deletes `bl_info` from the module namespace after loading a package as
        # an *extension* (4.2+), so referencing it here (runtime/draw) raises
        # NameError and leaves the panel body blank.  ADDON_VERSION survives.
        layout.label(text=f"BatchSimLab v{ADDON_VERSION}", icon='TOOL_SETTINGS')

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
