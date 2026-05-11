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

## TODO-5: Job Log row goes blank after scrolling or job start

**Observed behaviour:**  
Job 1 is visible immediately after the job log is populated (Export Batch).
After either (a) scrolling the list to the bottom, or (b) the first job starts
running, the row for job 1 becomes blank (empty job number and name).

**Suspected causes (investigate in order):**

1. **`job_log_index` scroll interaction** — Blender's `template_list` tracks the
   active-item index in `job_log_index` (default 0 → item 0 = job 1).  When the
   user scrolls the viewport, Blender may advance `job_log_index` to keep it
   within the visible window, leaving item 0 "selected but off-screen."  If the
   list then redraws before the scroll settles, item 0 can flicker blank.
   *Try*: initialise `job_log_index = -1` (or 0 but with a guard in `draw_item`)
   so no item starts selected.

2. **Timer–draw race condition** — `_update_job_log_statuses` is called from the
   poll timer (a background thread context).  Writing to a `CollectionProperty`
   item's properties while Blender is mid-draw can cause a partial RNA
   invalidation; the item is still present but its `job_number` / `job_name`
   StringProperty/IntProperty values read as defaults (0 / "").
   *Try*: guard the poll call with `_redraw_panels()` only *after* all item
   writes are complete, and confirm the writes are atomic from Blender's
   perspective.

3. **`_item` name shadowing in `export_batch`** — the seed loop uses the local
   name `_item` for each `job_log_items.add()` result.  After the loop exits,
   `_item` still references the last item added.  If any later code in
   `export_batch` accidentally reassigns or clears through `_item`, item 0
   could be corrupted.  *Try*: rename the loop variable to `_log_row` to avoid
   any ambiguity.

4. **`make_name` differs between seed loop and job loop** — the seed loop and the
   per-job JSON loop both call `make_name(p, i)` independently.  If `make_name`
   is non-deterministic (e.g. depends on mutable state), the stored name may not
   match.  *Try*: compute `name = make_name(p, i)` once per iteration and reuse
   it in both places.

**Recommended first step:**  
Add `print(f"draw_item: job_number={item.job_number!r} job_name={item.job_name!r}")` at the top of `SMOKE_UL_job_log.draw_item` and reproduce; if the blank row prints `job_number=0, job_name=''`, the item's properties are genuinely zeroed (cause 2 or 3).  If the row is never printed at all, the item is scrolled out of the viewport (cause 1).

**Files to investigate:** `scripts/SmokeSimLab/__init__.py`
(`export_batch` seed loop, `_update_job_log_statuses`, `SMOKE_UL_job_log.draw_item`, `SMOKE_PT_panel.draw` `template_list` call).

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

