"""
BatchSimLab/operators.py
========================
TODO-58 module #6 (Tiers 1+2): the CRUD / settings / emitter / export operators
and their private helpers, extracted verbatim from __init__.py with no behaviour
change.

DELIBERATELY LEFT in __init__ (the stateful "batch-run / poll engine" cluster,
plus the UI it shares): SMOKE_OT_run_batch / retry_failed / monitor_existing_jobs
/ remove_all_jobs / setup_results / reset_to_defaults, the poll loop and its
rebindable _bt/_estim/_job_*/_last_auto_index/_auto_retry_count globals, the
load/handlers, the three UILists, and SmokeSimLabPreferences.  Those operators
rebind module globals shared with the poll engine, so co-locating them is a
later, separate extraction (see TODOS.md TODO-58).

Re-imported by __init__.py so the ``classes = [...]`` registration list, the
panel's bl_idname call-sites, and ``from BatchSimLab import …`` (+ the test
suite) resolve unchanged.

Imports are one-way leaves (.jobgen / .emitters / .settings_io); this module
never imports __init__ at module scope.  ``export_batch`` and ``open_docs`` touch
a few names that live with the run engine in __init__ (the job-log lists, the
debug logger, ADDON_VERSION, DOCS_URL) — those are pulled in with a function-local
deferred import to avoid an operators->__init__ import cycle (read / in-place
mutate only; never rebound here).
"""
import bpy
import json
import os
import re
import shutil
import sys

from .jobgen import (
    ITERABLE_PARAMS,
    expand_param,
    generate_jobs,
    make_name,
    _VELOCITY_DEFAULT,
    _format_velocity_vector,
)
from .emitters import _populate_emitters
from .settings_io import _settings_dict, _load_settings_from_path


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
    # These live with the batch-run/poll engine in __init__; imported lazily here
    # to avoid an operators->__init__ import cycle.  The two job-log lists are
    # mutated IN PLACE (.clear()/.append()), never rebound; the others are read.
    from . import _job_log_rows, _job_statuses, _debug_log, ADDON_VERSION

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
    # so it points to the addon's folder (`BatchSimLab/` on disk), not the
    # .blend file.
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


class SMOKE_OT_open_docs(bpy.types.Operator):
    """Open the BatchSimLab documentation in a web browser."""

    bl_idname  = "smoke.open_docs"
    bl_label   = "Documentation"
    bl_description = "Open the BatchSimLab documentation on GitHub"

    def execute(self, context):
        from . import DOCS_URL  # defined with the addon metadata in __init__
        bpy.ops.wm.url_open(url=DOCS_URL)
        return {'FINISHED'}
