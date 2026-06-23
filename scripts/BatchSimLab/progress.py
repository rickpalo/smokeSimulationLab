"""
progress.py — pure progress/ETA helpers for BatchSimLab (TODO-58 module #4).

Extracted from ``__init__.py`` as the fourth module split.  This is the
**pure** half of the batch-progress machinery: filesystem scanners that read the
jobs directory, the phase-aware ETA estimator, and the human-readable
formatters.  Every function here takes its inputs as arguments (a jobs dir, a log
stem, counts, seconds) and returns a value — none of them touch ``bpy`` or rebind
any of the addon's module-level mutable state, so the whole surface is
unit-testable with a temp directory and plain values.

Deliberately LEFT BEHIND in ``__init__`` (the *stateful* half): the live poll
engine ``_poll_batch_progress`` / ``_poll_batch_progress_impl``, the
``_bt``/``_batch_times`` + ``_estim`` + ``_job_statuses`` / ``_job_log_rows`` /
``_last_auto_index`` / ``_auto_retry_count`` globals, and ``_update_job_log_statuses``.
Those globals are *rebound* (``global X; X = …``) from operators and load handlers
that stay in ``__init__``; moving the variable here while a rebind happens there
would split the binding across two modules and silently diverge.  Keeping the
mutable state and its mutators together in ``__init__`` is the safe boundary.

Contents:
  * Constants: ``_SETUP_SECS_DEFAULT`` / ``_STILL_SECS_DEFAULT`` (timing
    defaults), the sentinel-filename regexes ``_DONE_RE`` / ``_RETRY_DONE_RE`` /
    ``_CRASHED_RE``, and ``_LOG_DONE_MARKERS``.
  * Scanners: ``_find_running_log`` / ``_count_vdb_frames`` / ``_count_png_frames``
    / ``_has_error`` / ``_compute_batch_summary``.
  * Estimator + formatters: ``_estimate_batch_remaining`` / ``_format_eta`` /
    ``_format_elapsed``.

``__init__.py`` re-imports every public name from here so the poll engine's call
sites and the progress/ETA tests keep resolving against the package namespace.
"""
import json
import os
import re

# Default per-frame / per-stage timing estimates used before real data is
# available.  (The resolution-scaled bake/render rate constants stay in
# __init__ alongside the poll engine that consumes them.)
_SETUP_SECS_DEFAULT  =  10.0   # seconds for setup / cache phase
_STILL_SECS_DEFAULT  =  30.0   # seconds for final still frame

# Sentinel filename matchers used by the poll + summary code.  Defined as exact
# regexes (NOT endswith) so the two-pass pipeline's per-phase sentinels —
# job_NNNN.bake.done / .render.done / .bake.crashed / .render.crashed — are NOT
# counted as job completions or crashes (they're diagnostic-only; the .bat /
# launcher also write the unphased aliases here so the existing poll/summary
# logic keeps working unchanged).  The phased _BAKE_DONE_RE / _RENDER_DONE_RE
# matchers stay in __init__ with the overall-progress display that uses them.
_DONE_RE         = re.compile(r"^job_\d{4}\.done$")
_RETRY_DONE_RE   = re.compile(r"^job_\d{4}_retry\.done$")
_CRASHED_RE      = re.compile(r"^job_\d{4}\.crashed$")

# Tail markers that mean "this job's log shows it finished" (used by
# _find_running_log to skip a completed-but-not-yet-.done log).
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
    """Return (frames_counted, frame_count) by counting VDB files for log_stem, or None.

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
    # "frame_end" is checked for key presence, not truthiness — TODO-66 allows
    # a negative frame_start, so a legitimate range (e.g. -10..0) can have
    # frame_end == 0, which `and frame_end` would wrongly treat as missing.
    if "frame_end" not in job_data:
        return None
    frame_end   = job_data["frame_end"]
    # frame_start can be negative, so the frame COUNT is (end - start + 1),
    # not the bare frame_end the caller used to receive.
    frame_start = job_data.get("frame_start", 1)
    frame_count = frame_end - frame_start + 1
    output_path = job_data.get("output_path", "")
    name        = job_data.get("name", "")
    if not (output_path and name):
        return None

    mtime_cutoff = (start_time - 10.0) if start_time else None

    def _vdb_count_from_dir(data_dir):
        counted = set()
        if not os.path.isdir(data_dir):
            return counted
        for f in os.listdir(data_dir):
            # Optional leading "-" handles a negative frame number in the
            # cache filename (Mantaflow pads the sign into the frame width).
            m = re.search(r'_(-?\d+)\.vdb$', f)
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
                return len(frames_baked), frame_count

    # Fall back: look in this job's own cache dir.
    frames_baked = _vdb_count_from_dir(os.path.join(output_path, "Cache", name, subdir))
    return len(frames_baked), frame_count


def _count_png_frames(jobs_dir, log_stem, start_time=None):
    """Return (frames_rendered, frame_count) for log_stem, or None.

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
    # "frame_end" is checked for key presence, not truthiness — TODO-66 allows
    # a negative frame_start, so a legitimate range (e.g. -10..0) can have
    # frame_end == 0, which `and frame_end` would wrongly treat as missing.
    if "frame_end" not in job_data:
        return None
    frame_end   = job_data["frame_end"]
    frame_start = job_data.get("frame_start", 1)
    frame_count = frame_end - frame_start + 1
    output_path = job_data.get("output_path", "")
    name        = job_data.get("name", "")
    if not (output_path and name):
        return None
    frames_dir = os.path.join(output_path, "Renders", f"{name}_frames")
    count = 0
    if os.path.isdir(frames_dir):
        mtime_cutoff = (start_time - 10.0) if start_time else None
        for f in os.listdir(frames_dir):
            # Optional leading "-" handles a negative frame number — the
            # worker names PNGs with Python's f"{frame_num:04d}", which pads
            # the sign into the width (e.g. frame_-049.png).
            if re.match(r'frame_-?\d+\.png$', f):
                if mtime_cutoff is not None:
                    try:
                        if os.path.getmtime(os.path.join(frames_dir, f)) < mtime_cutoff:
                            continue
                    except OSError:
                        continue
                count += 1
    return count, frame_count


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


def _estimate_batch_remaining(
    *, total, bake_done_n, render_done_n, current_job_baked, bake_only,
    setup_remaining, bake_remaining, render_remaining, still_remaining,
    default_bake_secs, default_render_secs,
    setup_secs=_SETUP_SECS_DEFAULT, still_secs=_STILL_SECS_DEFAULT,
):
    """Phase-aware "all jobs remaining" estimate for the two-pass pipeline.

    The batch bakes every job first, then renders every job (see
    ``_emit_pass``), so completion must be tracked per phase: ``bake_done_n``
    counts finished bakes (``.bake.done``) and ``render_done_n`` finished
    renders (``.render.done``).  The previous estimate keyed off the unphased
    ``.done`` count, which stays 0 for the entire bake phase — so the ETA sat
    frozen at ``total × (bake + render)`` until rendering began (TODO-46).

    Model (each Blender launch pays ``setup_secs``):
      bake job   = setup + bake
      render job = setup + render + still

    The *current* job's real-time ``*_remaining`` values refine its own phase;
    every other not-yet-finished job is charged its flat phase cost.  No job is
    pre-discounted for a cached bake — once its bake actually completes (the
    fast SKIP-BAKE path, or a real re-bake if the cache was corrupt) its
    ``.bake.done`` appears and ``bake_done_n`` drops it out of the estimate.

    Returns seconds (float), never negative.
    """
    bake_job_cost   = setup_secs + default_bake_secs
    render_job_cost = setup_secs + default_render_secs + still_secs

    if bake_only:
        bakes_left = max(total - bake_done_n, 0)
        current    = setup_remaining + bake_remaining
        return max(current + max(bakes_left - 1, 0) * bake_job_cost, 0.0)

    if not current_job_baked:
        # Bake phase: current job is baking; all renders are still pending.
        bakes_left   = max(total - bake_done_n, 0)   # includes the current job
        renders_left = max(total - render_done_n, 0)  # == total during bake phase
        current      = setup_remaining + bake_remaining
        return max(
            current
            + max(bakes_left - 1, 0) * bake_job_cost
            + renders_left * render_job_cost,
            0.0,
        )

    # Render phase: current job's bake is done; only renders remain.
    renders_left = max(total - render_done_n, 0)      # includes the current job
    current      = setup_remaining + render_remaining + still_remaining
    return max(current + max(renders_left - 1, 0) * render_job_cost, 0.0)


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
