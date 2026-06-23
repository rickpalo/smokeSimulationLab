"""
BatchSimLab/engine.py
=====================
TODO-58 module #6b: the stateful batch-run / poll engine — the live progress
poller, the per-stage timing (_bt*) and estimate-logging (_estim*) machinery, the
job-log status updater, and the six operators that drive a run (run_batch,
retry_failed, monitor_existing_jobs, remove_all_jobs, setup_results,
reset_to_defaults) plus the two deferred timer callbacks.

This is the cluster that was deliberately kept OUT of the pure progress.py (module
#4): it owns the rebindable module globals (_last_auto_index, _auto_retry_count)
and the in-place batch state (_job_statuses, _job_log_rows, _batch_times, _estim,
_poll_state).  Every place that *rebinds* the two scalars lives here (the poller +
these operators + _auto_retry_deferred), so `global` stays valid within this one
module — no binding split.  The load handlers that stay in __init__ only mutate
_job_statuses / _job_log_rows IN PLACE (and call _bt_set), which works through the
re-import.

Imports are one-way: the pure helpers come from .progress, the no-camera check
from .operators; this module never imports __init__ at module scope.  A couple of
addon-metadata names that live in __init__ (ADDON_VERSION, _read_helper_version +
the _EXPECTED_*_VERSION constants) are pulled via a function-local deferred import
to avoid an engine->__init__ cycle.  Re-imported by __init__ so the classes = [...]
registration list, the panel, the load handlers, and the test suite resolve.
"""
import bpy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time

from .progress import (
    _SETUP_SECS_DEFAULT,
    _STILL_SECS_DEFAULT,
    _DONE_RE,
    _RETRY_DONE_RE,
    _find_running_log,
    _count_vdb_frames,
    _count_png_frames,
    _format_eta,
    _estimate_batch_remaining,
    _has_error,
    _compute_batch_summary,
)
from .operators import _scene_has_camera


# Bake estimate: bake_secs ≈ _BAKE_RATE_PER_RES3_FRAME × resolution³ × frames
# Calibrated from perf_log.json (May-2026, 164 samples, res 32–512).  The
# implied rate is not constant across resolutions (cv=75%, see analyze_estim):
# res=128 baseline media is the dominant workload, so we use its median.  At
# very low resolutions (32) the per-cell rate is ~5× higher because per-job
# overhead dominates — known model limitation.
_BAKE_RATE_PER_RES3_FRAME = 1.8261e-07  # s / (res^3 * frame); median of 119 res=128 samples

# TODO-51 (partial): noise sim runs an extra wavelet-turbulence up-res pass on top
# of the base data bake, which _BAKE_RATE_PER_RES3_FRAME alone doesn't capture.
# Calibrated from the 2026-06-22 AutoTest sweep (24 jobs, res 64/128/256,
# noise_upres 0/1/2; see `analyze_estim.py` BAKE table actual/default ratios,
# res=64 excluded — per-job overhead dominates there same as the base rate's
# known low-res limitation above). Upres 3+ unmeasured; falls back to the
# highest known bucket. Render time (EEVEE) also rises with noise_upres but
# isn't modelled yet — still open (needs the samples-term calibration batch
# from the TODO-51 plan).
_BAKE_NOISE_UPRES_MULTIPLIER = {0: 1.0, 1: 2.3, 2: 3.8}
_BAKE_NOISE_UPRES_MULTIPLIER_DEFAULT = 3.8


def _bake_noise_multiplier(use_noise, noise_upres):
    """Multiplier applied to the flat bake-rate estimate for noise sim cost."""
    if not use_noise:
        return 1.0
    return _BAKE_NOISE_UPRES_MULTIPLIER.get(noise_upres, _BAKE_NOISE_UPRES_MULTIPLIER_DEFAULT)


# Render estimate: render_secs ≈ rate × width × height × frames
# CYCLES: no real data yet — kept as placeholder derived from 15 s/frame at 1920×1080.
# EEVEE:  calibrated from 167 samples (cv=27%, median 7.92e-07).
_RENDER_RATE_CYCLES_PER_PIXEL_FRAME = 7.23e-9    # s / (pixel * frame) — placeholder
_RENDER_RATE_EEVEE_PER_PIXEL_FRAME  = 7.9239e-07 # s / (pixel * frame); median of 167 EEVEE samples

# Legacy flat rates kept as fallback when resolution/dimensions are unknown.
_BAKE_RATE_DEFAULT   =   1.0  # s/frame at unspecified resolution
_RENDER_RATE_DEFAULT =  45.0   # s/frame at unspecified resolution


# Phased completion markers — counted only by the overall-progress display so
# the bar advances during the bake pass (before any unphased <stem>.done is
# written).  Final job-complete trigger still uses _DONE_RE / _RETRY_DONE_RE.
_BAKE_DONE_RE    = re.compile(r"^job_\d{4}\.bake\.done$")
_RENDER_DONE_RE  = re.compile(r"^job_\d{4}\.render\.done$")

# A base job's spec file (excludes the per-retry job_NNNN_retry.json).
_JOB_JSON_RE     = re.compile(r"^job_\d{4}\.json$")


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
    from . import ADDON_VERSION  # addon metadata lives in __init__ (deferred: no cycle)
    record.setdefault("ts", round(time.time(), 2))
    record.setdefault("addon_version", ADDON_VERSION)
    try:
        with open(os.path.join(op, "estim_log.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


# TODO-63 Part B — periodic all-jobs ETA snapshots.  The displayed "All jobs: …"
# figure (_estimate_batch_remaining) is recomputed every ~5 s poll but never
# persisted, so there's no way to spot-check after a sweep whether that estimate
# was in the ballpark, updated, and converged toward the true wall-clock total.
# We log one `batch_eta_tick` to estim_log.jsonl at most every _ETA_TICK_MIN_SECS
# (the poll itself fires far too often to log every tick).  Gated, like every
# other estimation record, by collect_estimation_data via _estim_log's no-op path.
_ETA_TICK_MIN_SECS: float = 300.0   # ≥5 min between snapshots


def _log_eta_tick(*, now, elapsed, jobs_done, total, phase,
                  remaining_secs, job_remaining_secs, force=False):
    """Throttled all-jobs ETA snapshot → estim_log (event=batch_eta_tick).

    `force` bypasses the throttle — used for the initial tick (first poll with an
    active job, captures the starting estimate) and the final tick at
    batch_complete (so analyze_estim can overlay the true total on the same axis).
    Returns True if a tick was logged, False if throttled.  No-op for the file
    itself when collect_estimation_data is off (_estim_log gates on output_path).
    """
    last = _poll_state.get("eta_tick_ts", 0.0)
    if not force and (now - last) < _ETA_TICK_MIN_SECS:
        return False
    _poll_state["eta_tick_ts"] = now
    _estim_log({
        "event":              "batch_eta_tick",
        "elapsed":            round(elapsed, 1),
        "jobs_done":          jobs_done,
        "jobs_total":         total,
        "phase":              phase,
        "remaining_secs":     round(remaining_secs, 1),
        "job_remaining_secs": round(job_remaining_secs, 1),
    })
    return True


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
_poll_state: dict = {"log_key": "", "log_mtime": 0.0, "stale_since": 0.0,
                     "eta_tick_ts": 0.0}   # TODO-63 Part B: last batch_eta_tick time


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
            _poll_state["eta_tick_ts"] = 0.0   # TODO-63 Part B: arm initial tick
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
            _final_elapsed = round(time.time() - _bt("start_time"), 1)
            # TODO-63 Part B: final ETA tick (remaining=0) so analyze_estim can plot
            # the estimate trajectory ending exactly on the true total wall-clock.
            # Emitted BEFORE batch_complete so it falls inside this batch's segment
            # (analyze_estim._split_batches closes the segment on batch_complete).
            _log_eta_tick(now=time.time(), elapsed=_final_elapsed,
                          jobs_done=max(total - errors, 0), total=total,
                          phase="done", remaining_secs=0.0,
                          job_remaining_secs=0.0, force=True)
            _estim_log({
                "event":        "batch_complete",
                "jobs":         total,
                "errors":       errors,
                "elapsed_secs": _final_elapsed,
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
                    s.batch_frame_start   = jd.get("frame_start", 1)
                    s.batch_resolution    = int(jd.get("params", {}).get("resolution", 0))
                    s.batch_render_width  = jd.get("render_resolution_x", 0)
                    s.batch_render_height = jd.get("render_resolution_y", 0)
                    s.batch_render_mode   = jd.get("render_mode", "CYCLES")
                    s.batch_use_noise     = bool(jd.get("params", {}).get("use_noise", False))
                    s.batch_noise_upres   = int(jd.get("params", {}).get("noise_upres", 0))
                    _estim["job_name"]    = jd.get("name", log_stem)
                except (OSError, json.JSONDecodeError):
                    s.batch_frame_end     = 0
                    s.batch_frame_start   = 1
                    s.batch_resolution    = 0
                    s.batch_render_width  = 0
                    s.batch_render_height = 0
                    s.batch_render_mode   = "CYCLES"
                    s.batch_use_noise     = False
                    s.batch_noise_upres   = 0
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

            # frame_end here is the FRAME COUNT (end - start + 1), not the
            # absolute end-frame number — TODO-66 allows a negative
            # batch_frame_start, so batch_frame_end alone no longer equals
            # the frame count the rate-based estimates below need.
            frame_end      = max(s.batch_frame_end - s.batch_frame_start + 1, 1)
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
            noise_mult = _bake_noise_multiplier(s.batch_use_noise, s.batch_noise_upres)
            default_bake_secs   = (_BAKE_RATE_PER_RES3_FRAME * (batch_res ** 3) * frame_end * noise_mult
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

            # --- ETA: phase-aware all-jobs remaining (TODO-46) ---
            # Two-pass pipeline bakes every job, then renders every job, so the
            # estimate must count bake/render completions separately (the
            # unphased `done` count stays 0 through the whole bake phase, which
            # used to freeze this ETA).  The current job's `.bake.done` tells us
            # whether it is baking or rendering.
            current_job_baked = (log_stem + ".bake.done") in set(_all_files)
            remaining = _estimate_batch_remaining(
                total=total,
                bake_done_n=_bake_done_n,
                render_done_n=_render_done_n,
                current_job_baked=current_job_baked,
                bake_only=_bake_only,
                setup_remaining=setup_remaining,
                bake_remaining=bake_remaining,
                render_remaining=render_remaining,
                still_remaining=still_remaining,
                default_bake_secs=default_bake_secs,
                default_render_secs=default_render_secs,
            )
            s.batch_time_remaining = f"All jobs: {_format_eta(remaining)}"

            # TODO-63 Part B: snapshot the live all-jobs ETA on a throttle.  The
            # first poll with an active job forces a tick (initial estimate, since
            # `remaining` isn't computable in the early batch_start block); the
            # rest are throttled to _ETA_TICK_MIN_SECS.  Batch-level phase: still
            # baking until every job's bake is done (bake-only stays "bake").
            _eta_phase = "bake" if (_bake_only or _bake_done_n < total) else "render"
            _log_eta_tick(
                now=now, elapsed=now - _bt("start_time"),
                jobs_done=done_success, total=total, phase=_eta_phase,
                remaining_secs=remaining, job_remaining_secs=job_remaining,
                force=(_poll_state["eta_tick_ts"] == 0.0),
            )

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
        # These version helpers/constants live with the addon metadata in __init__;
        # imported here lazily to avoid an engine->__init__ cycle.
        from . import (_read_helper_version, _EXPECTED_WORKER_VERSION,
                       _EXPECTED_LAUNCHER_VERSION)
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


def _jobs_needing_retry(jobs_dir):
    """Return ``[(base_stem, job_json_path), ...]`` for every job that is not
    cleanly complete, sorted by job index.

    A job needs a retry when its final unphased completion marker reports an
    error, OR when it has no final unphased marker at all (interrupted, killed
    mid-batch, or never started).  The phased ``.bake.done`` / ``.render.done``
    markers are diagnostic and never count as a final result.

    The latest attempt wins: a ``_retry.done`` reflects a more recent run than
    the original ``.done``, so a successful retry clears an earlier failure (and
    a failed retry overrides an earlier success).
    """
    out = []
    try:
        names = sorted(os.listdir(jobs_dir))
    except OSError:
        return out
    for f in names:
        if not _JOB_JSON_RE.match(f):
            continue
        base_stem  = f[:-len(".json")]
        retry_done = os.path.join(jobs_dir, base_stem + "_retry.done")
        orig_done  = os.path.join(jobs_dir, base_stem + ".done")
        final = retry_done if os.path.exists(retry_done) else orig_done
        if os.path.exists(final):
            try:
                with open(final) as fh:
                    if "error" not in fh.read().lower():
                        continue          # cleanly finished — leave it alone
            except OSError:
                pass                       # unreadable marker → treat as needs-retry
        out.append((base_stem, os.path.join(jobs_dir, f)))
    return out


class SMOKE_OT_retry_failed(bpy.types.Operator):
    """Re-run failed or unfinished jobs with Use Placeholders and Use Existing
    Cache forced on."""

    bl_idname     = "smoke.retry_failed"
    bl_label      = "Retry Failed Jobs"
    bl_description = (
        "Write and launch run_retry_failed.bat containing the failed jobs and "
        "any that never finished (no final .done — e.g. interrupted mid-batch). "
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

        # Failed jobs (final marker says "error") plus any that never finished
        # (no final unphased .done) — see _jobs_needing_retry.
        failed = _jobs_needing_retry(jobs_dir)

        if not failed:
            self.report({'INFO'}, "No failed or unfinished jobs found")
            return {'CANCELLED'}

        blender_exe = bpy.app.binary_path
        blend_file  = bpy.data.filepath
        dest_worker = os.path.join(output_path, "smoke_worker.py")

        bat_lines = [
            "@echo off",
            'cd /d "%~dp0"',
            "setlocal enabledelayedexpansion",
            f"echo BatchSimLab retry — {len(failed)} job(s)",
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

            # TODO-63 Part A: capture the retry run's full console (it runs Blender
            # directly, no launcher — so its stdout+stderr would otherwise vanish/
            # be discarded by `2>nul`).  Per-job `<retry_stem>.console.log`.
            if s.collect_debug_log:
                _retry_console = os.path.splitext(retry_json)[0] + ".console.log"
                _retry_tail = f' > "{_retry_console}" 2>&1'
            else:
                _retry_tail = " 2>nul"

            bat_lines += [
                f"echo === Retrying: {name} ===",
                f'{blender_cmd}{_retry_tail}',
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
        s.batch_frame_start      = 1
        s.batch_jobs_elapsed     = 0.0
        s.batch_resolution       = 0
        s.batch_render_width     = 0
        s.batch_render_height    = 0
        s.batch_render_mode      = "CYCLES"
        s.batch_use_noise         = False
        s.batch_noise_upres       = 0
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
        from . import _reset_on_load  # load handler lives in __init__ (deferred: no cycle)
        _reset_on_load()
        _redraw_panels()
        self.report({'INFO'}, "BatchSimLab: all settings reset to defaults")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel helpers
# ---------------------------------------------------------------------------
