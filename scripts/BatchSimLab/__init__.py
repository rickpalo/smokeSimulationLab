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

Package layout (TODO-58 split, complete)
----------------------------------------
This module was decomposed into a package; the clusters below live in sibling
modules and are re-imported here so existing ``from BatchSimLab import …`` entry
points and the test-suite resolve unchanged.  This file now keeps only
registration, the load handlers, the addon metadata, and ``SmokeSimLabPreferences``.
  * ``jobgen.py``     — pure job-generation core (param expansion → make_name).
  * ``emitters.py``   — fluid-emitter discovery + sync (pure, duck-typed).
  * ``settings_io.py``— .smokesettings preset save/load + preset-dropdown enums.
  * ``progress.py``   — the PURE half of the bar/ETA machinery (scanners,
                        estimator, formatters).
  * ``properties.py`` — the 5 ``bpy.props`` PropertyGroups + class-body callbacks.
  * ``operators.py``  — CRUD/settings/emitter/export operators + their helpers.
  * ``engine.py``     — the STATEFUL run/poll engine (poller, timing/estimate
                        machinery, the 6 run operators) + its rebindable globals.
  * ``ui.py``         — the N-panel, the 3 UILists, and the ``_*_ui`` draw helpers.
See TODOS.md → TODO-58.

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
    "version":     (0, 9, 11),
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
    _JOB_JSON_RE,
    _jobs_needing_retry,
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

# TODO-58 module #7: the N-panel + UILists + _*_ui draw helpers (and the UI-only
# noise-ceiling validators + velocity-format hint) live in ui.py.  Re-imported
# here so the `classes = [...]` registration list (which names the panel + the
# three UILists) and the source/contract tests resolve against the package
# namespace unchanged.
from .ui import (
    _VELOCITY_FORMAT_HINT,
    _NOISE_UPRES_EDGE_WARN,
    noise_grid_edge,
    noise_grid_exceeds_ceiling,
    SMOKE_UL_value_list,
    SMOKE_UL_job_log,
    SMOKE_UL_velocity_list,
    _sub_param_ui,
    _settings_ui,
    _standalone_param_ui,
    _gas_ui,
    _noise_ui,
    _fire_ui,
    _emitter_sub_param_ui,
    _emitter_velocity_ui,
    _emitters_ui,
    SMOKE_PT_panel,
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
_EXPECTED_WORKER_VERSION   = "0.9.3"
_EXPECTED_LAUNCHER_VERSION = "0.6.6"


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
        s.batch_frame_start       = 1
        s.batch_jobs_elapsed      = 0.0
        s.batch_resolution        = 0
        s.batch_render_width      = 0
        s.batch_render_height     = 0
        s.batch_render_mode       = "CYCLES"
        s.batch_use_noise         = False
        s.batch_noise_upres       = 0
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
