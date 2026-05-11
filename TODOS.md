# SmokeSimLab — Pending Issues

Items to address once file synchronization catches up (~5,000 PNGs behind as of 2026-05-06).

---

## ~~TODO-1~~: Crash log written to jobs folder — **DONE** (already implemented in launcher)

---

## ~~TODO-2~~: Retry job does not find partial bake cache — **DONE** (v0.2.3)

**Root cause (confirmed from job_0000_retry.log):**  
The `has_files` candidate check used `f.endswith('.vdb') or f.endswith('.uni')`,
which matches Mantaflow's config/metadata `.uni` files (e.g. `fluidsimulation.uni`)
that are created immediately on domain init — before any simulation frames are
baked.  A candidate directory with only config files passed `has_files = True`,
was selected as the effective cache dir, then produced empty `baked_frames` in
the frame-counting walk (those config files don't match `_\d+\.(vdb|uni)$`).
The code then fell into the full-rebake else-branch, switched back to the job's
own cache dir, called `bpy.ops.fluid.free_all()`, and **destroyed the complete
cache** before rebaking from scratch.

For job_0000 specifically: the bake was complete when it crashed (crash was on
render frame 0302). The `_0000` cache had all 500 VDB frames, but the retry
found `_0030` (config files only), fell through to full rebake, freed `_0000`,
and rebaked all 500 frames unnecessarily.

**Fix:** Changed the `has_files` check to use the same frame-number regex as
the frame-counting walk (`re.search(r'_\d+\.(vdb|uni)$', f)`), so config-only
directories are skipped and only candidates with actual simulation frame data
are accepted.

---

## ~~TODO-3~~: "Utilities" collapsible section — **DONE** (implemented in v0.1.x)

---

## ~~TODO-4~~: Hide Job Log section when not populated — **DONE** (v0.2.0)

---

## ~~TODO-12~~: Full addon reset on .blend load — **DONE** (v0.2.5)

`_reset_on_load` previously preserved `domain_obj`, `output_path`, `render_mode`,
`use_dissolve`, `use_noise`, text object names, and the Utilities flags.  After
exporting a batch run the job log rows persisted into the next session.

**Fix:** `_reset_on_load` now resets every property to its factory default,
including `domain_obj = None`, `output_path = "C:/tmp"`, all Utilities flags, and
all UI toggle states.  The polling timer is also unregistered at the top of the
handler so it cannot fire between property resets.

---

## ~~TODO-13~~: Crashed job freezes progress bars — **DONE** (v0.2.5)

`_poll_batch_progress` had no exception guard; an unhandled error inside the timer
silently killed it mid-batch (Blender unregisters a timer that raises).  Also, if
the launcher process died without writing a `.done`/`.crashed` marker the UI gave
no indication.

**Fix:**
1. The timer is wrapped in a `_poll_batch_progress` → `_poll_batch_progress_impl`
   pattern: the outer function catches all exceptions, prints a warning, and
   returns `5.0` to keep the timer alive.
2. Added `_poll_state` + `_POLLER_STALE_SECS = 35 * 60` for timer-side stale
   detection.  When the active job's log file mtime is unchanged for 35 min the
   `batch_subtask_text` label is set to "No log activity for N min — job may be
   frozen."

---

## ~~TODO-14~~: File versioning for helper scripts — **DONE** (v0.2.5)

No mechanism existed to detect that `smoke_worker.py` or `smoke_launcher.py` in
the output folder were exported from an older addon version.  The v0.2.5
IndentationError was invisible partly because the stale worker was silently used.

**Fix:**
- `WORKER_VERSION = "0.2.5"` added to `smoke_worker.py`.
- `LAUNCHER_VERSION = "0.2.5"` added to `smoke_launcher.py`.
- `_EXPECTED_WORKER_VERSION` / `_EXPECTED_LAUNCHER_VERSION` constants in `__init__.py`.
- `_read_helper_version()` reads the version string from the first 30 lines of a
  file without importing it (avoids the `import bpy` constraint).
- `SMOKE_OT_run_batch.execute` checks both files before starting and emits a
  `WARNING` panel message if any version is wrong, prompting a re-export.

---

## TODO-5 / TODO-17: Job Log rows go blank on status transition

**Observed behaviour (TODO-5, original):**  
Job 1 is visible immediately after Export Batch.  After scrolling the list or
after the first job starts running, the row for job 1 becomes blank (empty job
number and name).

**Additional observation (TODO-17, confirmed in a real batch run):**  
Rows for jobs that have *completed* (COMPLETE or FAILED — i.e. a `.done` file
exists) also go blank.  The in-progress job may blank too.  The status dot
should remain visible and reflect the current status even after the job
finishes.  The two observations together strongly implicate the timer–draw race
(cause 2 below): the blank rows correlate with the moment `_update_job_log_statuses`
writes `item.status`, which suggests the write partially invalidates the RNA
item and causes Blender to return default values (0 / "") for the other fields
during the same draw pass.

**Suspected causes (investigate in order):**

1. **`job_log_index` scroll interaction** — Blender's `template_list` tracks the
   active-item index in `job_log_index`.  Scrolling may advance the index off-
   screen, causing item 0 to flicker blank.
   *Try*: initialise `job_log_index = -1` (or add a guard in `draw_item`).

2. **Timer–draw race condition (most likely)** — `_update_job_log_statuses` runs
   inside the poll timer.  Writing `item.status` while Blender is mid-draw can
   partially invalidate the RNA item; `job_number` / `job_name` read back as
   defaults (0 / "") during the same frame.  The fact that blanking tracks with
   status transitions (job starts, job completes) strongly supports this.
   *Try*: build a pending-status dict in the timer (`{idx: new_status}`) and
   apply writes only inside a `_redraw_panels()` call or a `bpy.app.timers`
   one-shot scheduled at 0 s from the main thread — RNA writes must not race
   the draw thread.

3. **`_item` name shadowing in `export_batch`** — already renamed to `_log_row`
   in the seed loop; verify no other path reuses `_item`.

4. **`make_name` non-determinism** — seed loop and per-job JSON loop call
   `make_name` independently; if non-deterministic, stored names may differ.

**Recommended first step:**  
Add `print(f"draw_item: {item.job_number!r} {item.job_name!r} {item.status!r}")` at
the top of `SMOKE_UL_job_log.draw_item` and reproduce.  If blank rows print
`job_number=0, job_name=''`, properties are genuinely zeroed (cause 2).
Correlate the print timestamps with timer-poll firings to confirm.

**Files:** `scripts/SmokeSimLab/__init__.py` — `_update_job_log_statuses`,
`SMOKE_UL_job_log.draw_item`, `_poll_batch_progress_impl`, `SMOKE_PT_panel.draw`.

---

## ~~TODO-19~~: Progress bars show 0 during bake / render — **DONE** (v0.2.8)

**Observed:** Subtask bar shows "Rendering (0 of 500)" while frame_0497 is actively
rendering.  Progress was stuck at 0 throughout the entire render; bars reset
correctly when the job finished.

**Root cause (render bar):** `_count_png_frames` used `since=batch_job_start_time`
to exclude frames from previous runs.  `batch_job_start_time` is set when the
poller first detects the log file.  If the log file wasn't detected until late
(Blender restarted mid-batch, bake was skipped so render started almost
immediately, or the file arrived via sync after many frames were rendered), all
already-rendered frames had an mtime *before* `batch_job_start_time` and were
filtered out.

**Root cause (bake bar):** `_count_vdb_frames` always looked in
`Cache/<current_job_name>/data/`.  When the worker bakes into an *alternate* cache
directory (use_existing_cache + partial cache from a different job number), VDB
files are written to that other directory and the count was always 0.

**Fix:**
- Removed the `since` mtime filter from `_count_png_frames` entirely.  Instead,
  a `batch_render_frame_baseline` property is set (once) when the "Rendering
  animation" stage is first detected.  Progress = (current_count − baseline),
  capped at `render_target`.  Works correctly for: full renders (baseline=0),
  re-renders with placeholders (baseline=existing frames), and Blender restarts
  mid-render (baseline=frames already on disk).
- `_count_vdb_frames` now extracts the effective cache dir from the log tail
  (the "Effective cache dir" line added in v0.2.7) when available, so it counts
  VDB files in the directory the worker is actually baking into.  Falls back to
  `Cache/<name>/data/` when no log is available.
- A `batch_bake_frame_baseline` property is set when baking starts and the bake
  subtask shows "(new_frames of to_bake)" rather than a total count, so a partial
  resume correctly shows e.g. "Baking (30 of 250)" rather than "Baking (280 of 500)".
- The bake ETA uses `bake_to_go` (remaining frames adjusted for baseline) instead
  of `frame_end − frames_baked`.

---

## ~~TODO-18~~: Cache search logging and config-file false-positive — **DONE** (v0.2.7)

**Observed:** Jobs with "Use Existing Cache" enabled still baked from scratch even
when a complete cache existed from a previous run.

**Root cause (config false-positive):** Mantaflow writes per-frame config
checkpoints (`config/config_0001.uni`, `config_0002.uni`, …) to every cache
directory immediately on domain init — before any simulation data is written.
These files matched `r'_\d+\.(vdb|uni)$'`, so a directory containing *only*
config checkpoints (no actual VDB data) passed `has_files` and was selected
as the effective cache.  Those same filenames were then counted in `baked_frames`,
making `bake_complete = True`, causing the bake to be skipped — but no real VDB
data existed, so the render failed or produced the wrong frame.

**Fix:** Skip the `config/` subdirectory when walking candidate directories for
both the `has_files` check and the `baked_frames` count.  This preserves
compatibility with the UNI data format (pre-VDB caches) while excluding
per-frame config checkpoints.

**Logging improvements:** Replaced sparse `_dlog`-only output with a full
`_log` cache-search section that always records: the search path, the regex
pattern used, every candidate evaluated (accept/reject + reason), the chosen
effective cache dir, the frame range found, any missing frames, and the bake
path taken (SKIP / RESUME / FULL).

---

## ~~TODO-15~~: "Remove All Jobs" button in Utilities section — **DONE** (v0.2.6)

New operator `SMOKE_OT_remove_all_jobs` (`bl_idname = "smoke.remove_all_jobs"`).
`invoke()` shows a confirmation dialog before deleting anything.  Deletes the
`jobs/` folder (with `shutil.rmtree` → per-file fallback on `PermissionError`),
`run_smoke_batch.bat`, `smoke_worker.py`, and `smoke_launcher.py` from
`output_path`.  Clears all job-log and batch-progress state; stops the poll timer.
Leaves `domain_obj`, `output_path`, and all simulation parameters untouched.
Button appears in the Utilities section with a `TRASH` icon.

---

## ~~TODO-16~~: Reliable crash-dialog suppression — **DONE** (v0.2.6)

`SEM_NOGPFAULTERRORBOX` was unreliable because Blender resets the process error
mode during startup, and it does not cover `WerFaultSecure.exe`.  The previous
post-exit WerFault poll also covered only 3 seconds.  `collect_crash_logs`
defaulted to `False` and was reset by `_reset_on_load`, so `crash_log.txt` was
never written in practice.

**Fix:**
1. `smoke_launcher.py` now creates a Windows Job Object with
   `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` before spawning Blender and
   assigns Blender to it immediately after `Popen`.  The Job Object limit cannot
   be overridden by the child process, so crash dialogs are suppressed at the OS
   level without relying on inherited error-mode flags.
2. `_find_werfault_for_pid` now checks both `WerFault.exe` and
   `WerFaultSecure.exe`.
3. `_POLL_INTERVAL` reduced from 2.0 s to 0.5 s for faster WerFault detection.
4. Post-exit WerFault poll extended from 3 s to `_POST_EXIT_WERFAULT_SECS = 30` s.
5. `_save_crash_log` is now called unconditionally on any non-zero exit (no longer
   gated on `collect_crash_logs`).
6. `LAUNCHER_VERSION` bumped to `"0.2.6"`.

---

## ~~TODO-6~~: Auto-scroll Job Log — **DONE** (v0.2.2)

---

## ~~TODO-7~~: Update default parameter values — **DONE** (all defaults were already correct)

---

## ~~TODO-8~~: Fix negative/zero RT_proj values — **DONE** (v0.2.1)

---

## ~~TODO-9~~: Sort exported jobs by resolution ascending — **DONE** (v0.2.1)

---

## ~~TODO-9~~: analyze_perf.py render tables — **DONE** (v0.2.1)

---

## ~~TODO-10~~: Debug log — **DONE** (v0.2.2)

---

## ~~TODO-11~~: Settings dropdown shows stale name on load — **DONE** (v0.2.4)

**Observed behaviour:**  
When the .blend file (and addon) first loads, the settings preset dropdown shows
a `.smokesettings` filename, but the settings displayed in the panel are from
the blend file — not re-loaded from that preset file.  The dropdown should be
blank on load; a name should only appear after the user explicitly loads or saves
a preset.

**Root cause:**  
`_reset_on_load` clears `settings_file_path` and `settings_snapshot` but never
resets `settings_file_enum`.  Blender restores the saved `EnumProperty` value
(e.g. "default") from the blend file, but the `_on_settings_enum_update`
callback does **not** fire during RNA restoration on file load — so the dropdown
shows the old name while the settings remain whatever is stored in the blend
file.

**Fix:** Added `s.settings_file_enum = ""` to `_reset_on_load`, immediately
after the other `settings_*` clears.  The update callback fires but returns
immediately (stem is empty), leaving the dropdown blank and the in-blend
settings untouched.

---

