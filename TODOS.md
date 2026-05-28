# SmokeSimLab — Pending Issues

Items to address once file synchronization catches up (~5,000 PNGs behind as of 2026-05-06).

---

## TODO-36: Monitor Existing Jobs — progress count wildly off mid-bake — **OPEN**

**Filed 2026-05-28.** User switched the output folder back to AutoTest and
clicked **Monitor Existing Jobs** mid-bake.  Sub-task bar 1 displayed
`Baking 5 of 197` while the cache directory was actively writing
`frame_0471` (of 500).  Numbers:

- `5` ≈ "frames baked since Monitor was clicked".
- `197` ≈ `500 - 303` (frames still-to-bake at the moment Monitor started).
- Actual frame being written: 471 → 168 frames baked since Monitor start — but
  the bar froze at 5.

**Likely cause:** the bake-bar's baseline was captured correctly at
Monitor-click, but the per-poll re-count of VDB files isn't updating during
this scenario (or the `start_time` mtime filter from BUG-003 is dropping
recent frames).  The poll's `_count_vdb_frames` may be reading a cached or
stale view.  Inspect `_poll_batch_progress_impl`'s bake-bar section and the
Monitor-Existing-Jobs initial state seeding.

**Files:** `__init__.py` — `_count_vdb_frames`, the bake-bar progress section
in `_poll_batch_progress_impl`, `SMOKE_OT_monitor_existing_jobs.execute`.

---

## TODO-35: Evaluate background save-`.blend` + open resume approach — **DONE** (v0.5.0)

**Filed + Resolved 2026-05-28.**  Investigation by five probe scripts
(`scripts/experiments/bg_resume_probe_v[2-7].py`) ruled out the save+reload
approach and discovered the real fix: **cache_type='MODULAR' + bake_data()**.

**Probe trail (kept for reference — all in `scripts/experiments/`):**
- **v2:** user's original save+reload+move-back proposal → REBAKED-FROM-1.
- **v3:** added `cache_frame_pause_data` write to the saved .blend →
  REBAKED-FROM-1.  Finding: that property does NOT persist through
  `save_as_mainfile` (loads as 0 in fresh process).
- **v5:** switched to `cache_type='MODULAR'` + `bake_data()` with
  `cache_frame_start = last_preserved` → bake succeeded but pruned frames
  1..99 (Mantaflow honored the new lower bound as a cache range).
- **v6 (KEY):** same-process MODULAR + `bake_data()` with
  `cache_frame_start = 1` unchanged → **VERDICT RESUMED.** 99 frames
  preserved (mtime untouched), boundary frame 100 rewritten once,
  100 new frames baked in 5.5 s.  No save/reload required.
- **v7:** v6 + `use_noise=True` + `bake_noise()` → both layers resume
  identically.

**Resolution (v0.5.0):** Worker switched to `bake_data()` (+ `bake_noise()`
if `use_noise`) under `cache_type='MODULAR'` in BOTH the RESUME and FULL
bake branches.  See BUG-010 Attempt 5 in `BUG_TRACKER.md` for full details.

**Unlocks:**
- TODO-31 (RESUME progress bar) — previously blocked by no real resume; now
  the bar can show "Baking (already_baked+1) of total" because we know the
  baseline is preserved.
- A backlog of bake-and-restart workflows that were too slow to attempt.

---

## TODO-34: Render-phase fast-fail when bake didn't produce a usable cache — **DONE** (v0.4.8)

**Filed + Resolved 2026-05-28.**  In the two-pass pipeline, if the bake phase
crashed or left a partial cache, the render phase used to start its (expensive)
setup, hit the post-bake guard, and only then exit — wasting the launcher
overhead + GPU init.  Now the render-phase worker bails BEFORE the heavy setup:

1. Reads `<stem>.bake.done`; if missing or contains "error" → bail.
2. Counts `<cache_dir>` data files; if `< (frame_end - frame_start + 1)` → bail.
3. Before exiting (1), `shutil.rmtree(cache_dir)` wipes the partial cache so
   auto-retry's single-pass `--phase=both` sees an EMPTY cache → forces the
   FULL-bake decision (avoids RESUME-from-1 of broken data).
4. `sys.exit(1)` → `.render.done` reports error → auto-retry triggers.

Single-pass (`--phase=both`) and the bake phase are unaffected — they own their
own cache decisions.  Tests: `tests/test_run_batch_gating.py::TestRenderPhaseFastFail`
(4).  Worker → 0.4.8; re-export required.

---

## TODO-33: Render Animation checkbox — still-only mode — **DONE** (v0.4.5)

**Filed 2026-05-28.** New `render_animation` BoolProperty (default True) below
*Render Simulation Result*; greyed out when rendering is off.  When unchecked,
the worker skips the per-frame PNG sequence + ffmpeg MP4 mux and renders only
the final still PNG (`<name>.png`).  Useful when you only need the result image
and don't want to spend N×frame render time on the animation.

**Resolution (v0.4.5):**
- `SmokeSettings.render_animation` BoolProperty + UI row gated on
  `render_simulation_result`; reset-on-load default True.
- `export_batch` writes `render_animation` into each job JSON.
- Worker reads it (default True for pre-TODO-33 JSONs); when False:
  - empties `frames_to_render` → animation render loop is a no-op;
  - `if render_animation:` gates the ffmpeg block;
  - the TODO-32 final-still copy naturally falls back to rendering (`frame_end`
    isn't in `frames_to_render`) so `<name>.png` is still produced.
- Tests: `tests/test_run_batch_gating.py::TestRenderAnimationGate` (4).
- Worker → 0.4.5; re-export required.

---

## TODO-32: Final still re-renders frame_end with identical settings — just copy it — **DONE** (v0.4.4)

**Resolution (v0.4.4):** worker's "Final still" block now `shutil.copy2`'s
`<name>_frames/frame_<frame_end>.png` → `<name>.png` when `frame_end` was in
`frames_to_render` this run AND the source file is present.  Falls back to the
original render path on copy failure OR when the frame was placeholder-skipped
(source file might be stale from a prior run with different settings).  Tests:
`tests/test_run_batch_gating.py::TestWorkerFinalStillCopy`.



**Filed 2026-05-28.** The worker renders the PNG sequence (`frame_0001 …
frame_<frame_end>.png` into `Renders/<name>_frames/`), then renders the LAST
frame AGAIN as the standalone `Renders/<name>.png` (used by Setup Results
Viewer).  Both renders use the same `render_mode` + `render_samples` + scene
state, so they produce a pixel-identical image — the second render is purely
file-placement.  Saves ~5 s/job at EEVEE 1440×1080; more at higher res / Cycles.

**Proposed fix:** in the "Final still" block of `smoke_worker.py`, replace the
second `bpy.ops.render.render(write_still=True)` with
`shutil.copy2(<frames_dir>/frame_<frame_end>.png, <name>.png)`.

**Edge cases to handle:**
- **Missing source frame** — if `<frames_dir>/frame_<frame_end>.png` doesn't
  exist (animation render was skipped because all frames already existed but
  somehow `<name>.png` doesn't, or `frame_<frame_end>.png` was placeholder-
  skipped without being baked), fall back to the existing render-still path.
- **Placeholder/rebake mismatch** — if `use_placeholders` skipped re-rendering
  `frame_<frame_end>.png` but the bake actually recomputed that frame, the
  copy would be stale.  Either re-render in that case, or invalidate the
  placeholder for `frame_end` whenever `frame_end in rebaked_frames`.
- **Bake-only mode** — already handled (final-still block runs only when
  `render_simulation_result` is True).
- **Perf accounting** — `render_seconds` currently includes the final-still
  render time.  After the change, it would include only the animation render;
  decide whether `render_secs_per_frame` should be unchanged (it's already
  computed over `frames_actually_rendered`, which excludes the duplicate) or
  whether to add a separate `still_seconds` field (probably skip — they were
  always the same render).
- **CSV column for the still** — currently none; no change needed.

**Files:** `scripts/SmokeSimLab/smoke_worker.py` — "Final still — last frame
only" block.  **Tests:** unit-test a small helper that picks copy-or-render
based on source-frame existence + `rebaked_frames` membership.

---

## TODO-31: RESUME progress bar should start at "(already-baked + 1) of total"

**Filed 2026-05-27.** On a resume with e.g. 100/500 frames already baked, the
bar should read **"101 of 500"** at the start, not "0 of 332" (332 = missing).
The completed frames should count toward the full total.

**Dependency / caveat (important):** RESUME currently **re-bakes from frame 1**
(Mantaflow limitation, BUG-010 — there's no scripted API to truly resume
mid-range). So while it re-bakes frames 1–100 (overwriting in place), there is
no honest way to show "101 of 500" advancing — those frames are being redone.
Options:
- (a) **Display-only:** show `max(already_baked, frames_this_run) of total`, so
  it opens at "100 of 500" and holds there until the re-bake passes frame 100,
  then climbs. Matches the user's mental model but sits still during the 1–100
  re-bake (looks stalled, esp. at high res).
- (b) Honest "re-baking N of 500" climbing from 1 (current mtime-based count vs
  full total) — accurate but doesn't match the request.
- Truly starting at 101 requires **real partial resume**, which BUG-010 says is
  not achievable in scripted Mantaflow. So this TODO is blocked on that unless
  we accept option (a)'s cosmetic behavior.

**Files:** `__init__.py` — `_poll_batch_progress_impl` bake-bar section,
`batch_bake_frame_baseline`. **Decision needed:** accept option (a)?

---

## TODO-29: Warn when rendering is on but the scene has no camera — **DONE** (v0.4.6)

**Resolution (v0.4.6):** `_scene_has_camera(scene)` helper added (pure;
`any(obj.type == 'CAMERA' for obj in scene.objects)`).  Both Export Batch and
Run Batch now check `render_simulation_result and not _scene_has_camera(...)`
in their `invoke` and show a confirmation dialog if the warning fires.  Export
Batch combines the camera warning with the existing high-resolution warning in
one dialog (operator instance caches both flags as `_warn_cam` / `_warn_res`).
Cancel aborts; OK proceeds anyway (so the user can dismiss with a single click
if they know what they're doing).  Tests: `tests/test_camera_check.py` (9).

**Filed 2026-05-27 (test run feedback).** If the scene has no camera and
**Render Simulation Result** is enabled, the renders will be black / fail. On
Run Batch, warn the user and let them cancel.

**Design note (important):** `render_simulation_result` is baked into each
`job_NNNN.json` at **Export** time, not Run time. So a Run-Batch warning can
offer "Cancel" but cannot truly "disable renders" for the existing batch without
re-exporting. Two clean options:
- (A) Do the camera check at **Export** time: if no camera + render on, warn and
  offer to export as bake-only (`render_simulation_result=False`). Simple,
  correct, but not where the user asked.
- (B) Do it at **Run Batch**: a dialog with "Cancel" + a checkbox "I added a
  camera / run anyway". Choosing cancel can also flip the panel's
  `render_simulation_result` off so a re-export is bake-only.
- Recommended: **both** — a cheap `_scene_has_camera(scene)` helper used at
  Export (offer bake-only) and a Run-Batch guard (offer cancel).

**Files:** `__init__.py` — `SMOKE_OT_run_batch.invoke/draw`, `SMOKE_OT_export_batch`,
a `_scene_has_camera` helper. **Tests:** `_scene_has_camera` unit test.

---

## TODO-30: Allow renaming a job in the Job Log (double-click / F2) — **REJECTED** (2026-05-27)

**Rejected:** not worth the effort. The job name is parameter-derived and
coupled to the cache/render directory names (see analysis below), so a useful
rename would require moving on-disk artifacts and would not survive a re-export.
Low value for the complexity. Kept for the record.

**Filed 2026-05-27 (test run feedback).** Let the user rename a job in the Job
Log list, only while no batch is running (before or after a run).

**Answer to "will renaming reset the sim / change the cache dir?": YES — it is
not a cosmetic label.** The job `name` *is* the directory name everywhere:
`Cache/<name>/`, `Renders/<name>_frames/`, `Renders/<name>.mp4`, `<name>.png`,
the `name` column in `results.csv`, and the `d.cache_directory` Mantaflow bakes
into. Renaming therefore implies, while no job runs:
1. Rename on disk: `Cache/<old>` → `Cache/<new>`, `Renders/<old>_frames` →
   `<new>_frames`, and the `<old>.mp4` / `<old>.png` files. Otherwise the next
   run bakes fresh into `Cache/<new>` (i.e. it *does* reset the sim) and the old
   artifacts orphan.
2. Update the job's JSON `name`, `_job_log_rows`, and `job_log_items[idx]`.
3. `results.csv` already-written rows still hold the old name (leave or rewrite?).

**Design tension with BUG-005:** names are **parameter-derived** (`make_name`)
so "same params → same dirs" holds. A manual name will NOT survive a re-export
(export regenerates the parameter-derived name) and breaks dedup/reuse for
identical params. So a manual rename is inherently fragile.

**Decision needed before implementing:** (a) full rename that moves all artifacts
on disk (most work, "does the right thing"), or (b) a display-only label stored
separately from the parameter-derived dir name (keeps BUG-005 invariant; dirs
keep the param name; only the Job Log shows the friendly label).

**Files:** `__init__.py` — `SMOKE_UL_job_log` (rename op), `make_name` coupling,
`export_batch`, `_job_log_rows`. **Tests:** rename helper + artifact-move helper.

---

## TODO-28: Append mode overwrites run_smoke_batch.bat instead of extending it — **DONE** (v0.3.0)

**Resolution (v0.3.0):**
- `_existing_jobs_for_bat(jobs_dir, job_start_index)` reads each prior
  `job_NNNN.json` (name + render_mode) for indices below the append start.
- `export_batch` now re-lists those existing jobs in the .bat (via the shared
  `_job_run_cmd` / `_job_bat_block` helpers) *before* the newly appended ones,
  so rewriting the .bat in "w" mode no longer drops them.  Re-listed jobs are
  cheap on a second run (SKIP BAKE / placeholders).
- Safeguard: the Export/Append toggle and the Export Batch + Run Batch buttons
  are disabled while a batch is running (`_batch_is_running()` in
  `SMOKE_PT_panel.draw`) — the running cmd.exe already parsed the .bat, so
  editing it mid-run can't help and only invites confusion.
- Tests: `tests/test_export_append.py` — `TestExistingJobsForBat`,
  `TestJobRunCmd`, `TestJobBatBlock`.

**Observed (2026-05-27 ClaudeTest run):** User exported job_0000 (250 frames),
clicked Run Batch, then while it was running appended job_0001 (500 frames)
and job_0002 (200 frames).  Only job_0000 actually ran (the .bat that was
loaded by cmd.exe when Run Batch was clicked contained only job_0000).
Job_0001 and job_0002 were created in jobs/ but their entries were never
in any .bat file that was executed.

**Root cause:** `export_batch()` in `__init__.py` (around line 914):
```python
bat_path = os.path.join(output_path, "run_smoke_batch.bat")
with open(bat_path, "w") as fh:    # "w" = truncate
    fh.write("\n".join(bat_lines))
```
On Append, `bat_lines` is built from only the NEW jobs (the loop at line 830
iterates `jobs`, which is the new batch only).  The .bat is then OVERWRITTEN,
dropping all previously-exported jobs.

**Two related symptoms:**
1. Each Append produces a .bat that contains only the most-recently-appended
   jobs.  Running it skips all jobs from earlier Exports/Appends.
2. The addon's job log (UI list) DOES contain all jobs because the seed loop
   at line 821 only clears `job_log_items` in REPLACE mode.  So the UI shows
   N jobs but only the last batch's worth ever run, leaving the rest as
   `NOT_STARTED` (open circle) forever — and the auto-retry mechanism may
   then misinterpret these never-started jobs as crashes and try to retry
   them oddly (see ClaudeTest where job_0002 got auto-retried but job_0000
   /0001 did not match expectations).

**Proposed fix:**
- In Append mode, before the new-jobs loop, iterate existing
  `job_NNNN.json` files (indices `0..job_start_index-1`) and emit their
  .bat entries first.  Read the `name` and `render_mode` from each JSON to
  produce the correct launcher command.
- Result: the new .bat runs all existing jobs (which will SKIP BAKE / SKIP
  RENDER if their caches+renders are already complete) followed by the new
  ones.

**Additional safeguard (separate fix):** Disable Export Batch (both modes)
and Append while a batch is running.  This prevents the racier scenario the
user hit where Append happened after Run Batch was already executing — the
running cmd.exe has already parsed the .bat into memory and won't see the
update regardless of how it's written.

**Files:** `scripts/SmokeSimLab/__init__.py` — `export_batch()` lines
820–915; also UI guards in `SMOKE_PT_panel.draw`.

---

## TODO-27: Restore crash dumps (relax Job Object kill window) — **PARTIAL** (v0.2.33)

**v0.2.33 fix:** Added `_CRASH_DUMP_GRACE_SECS = 15` to the launcher.
`_save_crash_log` now waits up to 15 s for `blender.crash.txt` to appear
in `%TEMP%` with `mtime >= launch_time` before deciding the dump is
missing.  Stale dumps from previous crashes are filtered out by mtime.
This gives Blender's SEH handler some grace to flush the dump even when
the Job Object's `DIE_ON_UNHANDLED_EXCEPTION` fires.

**Remaining work:** If 15 s isn't enough — i.e., the Job Object truly
terminates Blender before the SEH handler runs at all — we'd need the
deeper changes below (options 1/2 from the original proposal).  Re-evaluate
after the next production batch shows whether dumps start landing again.

**Observed:** From 2026-05-15 onward, all crashes in `crash_log.txt` show
`[no blender.crash.txt found in %TEMP%]` — only the dated header and the
"no dump" line.  Crashes from 2026-05-07 through 2026-05-11 had full Blender
crash dumps with Exception Records and stack traces.

**Root cause (analyzed 2026-05-27):** Our launcher's Windows Job Object
`JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` (added v0.2.6 for WerFault
dialog suppression) terminates Blender before its SEH crash handler can
write `blender.crash.txt` to `%TEMP%`.  We traded crash dialogs for crash
diagnostics — a poor trade now that the dialog blocking problem has other
mitigations and crash dumps are valuable for diagnosis.

Until 2026-05-11, Blender's crash handler was still managing to write the
dump in the brief window before Job Object kill.  Something changed
mid-May (possibly a Blender update changing crash handler latency) that
now reliably loses dumps.

**Proposed fix:** Give Blender's crash handler a grace window after the
unhandled exception before the Job Object kills the process.

Approaches to consider:
1. **Replace `DIE_ON_UNHANDLED_EXCEPTION` with a SetUnhandledExceptionFilter
   approach** that writes a minidump first, then terminates.
2. **Use `WerFault` registration cleanup** (Windows API
   `WerReportSubmit` with no-UI flag) so dumps are written via WER but no
   dialog appears.
3. **Post-exit `%TEMP%\\blender.crash.txt` polling** with a longer window
   (currently checked once; poll for ~30 s after Blender exit).
4. **Capture stderr/stdout to a `.crash_stderr` file** so even without the
   structured dump we have the last 100 lines of Python output.

Option 3 is the least invasive and probably catches most dumps without
re-introducing dialog blocking.

**Files:** `scripts/SmokeSimLab/smoke_launcher.py` —
`_create_crash_suppression_job`, `_save_crash_log`,
post-exit polling section.

**Related:** See project memory `project_crash_root_cause.md` — the actual
crashes are in Blender 5.1.1's glTF/numpy import, not in our code; better
dumps will confirm whether new crash signatures match the same root cause.

---

## TODO-26: "Render Simulation Result" checkbox to skip rendering entirely — **DONE** (v0.3.0)

**Resolution (v0.3.0):**
- `SmokeSettings.render_simulation_result` BoolProperty (default `True`), with an
  update callback `_on_render_sim_result_update` that clears `show_results` when
  rendering is turned off (avoids a meaningless "display results" in bake-only).
- Panel: checkbox below "Automatically Retry Failed Jobs"; Render Engine + Samples
  row and the "Display Results When Finished" row are greyed out when it is off.
- `export_batch` writes `render_simulation_result` into each `job_NNNN.json`.
- `smoke_worker.py` reads the flag (default `True` for pre-TODO-26 JSONs) and
  skips the whole MP4 + still render block when `False`; results.csv and
  perf_log records still run so the job is recorded as complete.
- Tests: `tests/test_run_batch_gating.py` — `TestRenderSimResultUpdate`,
  `TestWorkerRenderGuard`.

**Goal:** Allow users to run a bake-only batch (no MP4 / PNG render) for cases
where they want to validate the simulation cache before committing render time,
or where rendering will be done later by hand with different settings.

**UI changes:**
- Add a new BoolProperty `render_simulation_result` (default `True`) on
  `SmokeSettings`.
- In `SMOKE_PT_panel.draw`, place a checkbox labeled **"Render Simulation Result"**
  *below* the "Automatically Retry Failed Jobs" checkbox and *above* the
  Render Engine selector.
- When `render_simulation_result` is `False`:
  - Disable the Render Engine selector (`row.enabled = False`).
  - Disable the Samples field.
  - Uncheck *and* disable the "Display Results When Finished" checkbox
    (set `display_results_when_finished = False` and gray it out).

**Behaviour changes:**
- In `smoke_worker.py`, when the job config has `render_simulation_result = False`:
  - Skip the playblast/MP4 render step.
  - Skip the final still PNG render step.
  - Still write the `results.csv` row (with empty/null values for render-related
    columns, or omit them — TBD).
  - Still write `.done` / `.worker_done` sentinels so the launcher treats the
    job as complete.
- In `__init__.py`, when `render_simulation_result = False`:
  - Skip the "show renders" / display step at batch completion.
  - The progress bar should NOT show "Rendering animation" / "Rendering still"
    stages (the worker will not log them).

**Export changes:**
- `SMOKE_OT_export_batch` should include `render_simulation_result` in each
  `job_NNNN.json` so the worker knows whether to render.

**Files:** `__init__.py` — `SmokeSettings`, `SMOKE_PT_panel.draw`, `export_batch`;
`smoke_worker.py` — render section guards.

**Tests:** Add a test that confirms the worker exits cleanly without rendering
when `render_simulation_result = False`; add a UI test (if practical) that
confirms the Render Engine / Samples / Display Results controls become disabled.

---

## TODO-25: Run Batch button should be disabled until there are jobs to run — **DONE** (v0.3.0)

**Resolution (v0.3.0):**
- `_batch_ready(output_path)` returns True only when both `run_smoke_batch.bat`
  and at least one `job_NNNN.json` exist.  Computed on the fly in
  `SMOKE_PT_panel.draw` (no `batch_ready` property to keep in sync), so it is
  correct after Export, Remove All Jobs, reset, or reopening a session with
  jobs already on disk.
- Run Batch is gated with `_batch_ready(...) and not _batch_is_running()`.
- "Monitor Existing Jobs" is also greyed out when the jobs folder has no JSONs.
- Tests: `tests/test_run_batch_gating.py` — `TestBatchReady`.

**Observed:** The "Run Batch" button is always enabled, even when no jobs have
been exported yet.  Clicking it in that state launches `run_smoke_batch.bat`
which doesn't exist (or runs an empty batch), producing a confusing error.

**Desired behaviour:** Disable the Run Batch button when there are no jobs to
run.  Enable it in either of these cases:
1. The user clicks Export Batch (jobs are written to `<output_path>/jobs/`).
2. The panel is first drawn and the output directory already contains exported
   jobs from a previous session (`<output_path>/jobs/*.json` exists and
   `run_smoke_batch.bat` is present).

**Implementation hints:**
- Add a `batch_ready` BoolProperty on `SmokeSettings` (or compute it on the fly
  in `draw()` from `os.path.isfile(...)`).
- In `SMOKE_OT_export_batch.execute`, set `batch_ready = True` after the batch
  is written successfully.
- In `_reset_on_load` (and after Remove All Jobs), set `batch_ready = False`.
- In `SMOKE_PT_panel.draw`, gate the Run Batch button with `row.enabled = batch_ready`.
- Consider also disabling the Monitor Existing Jobs button when no jobs folder
  exists (separate but related condition).

**Files:** `__init__.py` — `SmokeSettings`, `SMOKE_OT_export_batch`,
`SMOKE_OT_remove_all_jobs`, `_reset_on_load`, `SMOKE_PT_panel.draw`.

---

## TODO-22: Crash timing inconsistency — one crash stalled ~5 min, another moved immediately — **INSTRUMENTED** (v0.3.0)

**v0.3.0 (diagnostics):** The launcher now logs `pid`, `exit_code`, and
`time_to_exit` for *every* job (printed to the per-job .log, not just debug
mode), plus `werfault_poll_secs` on a crash.  These distinguish the two
candidate causes: a large `time_to_exit` with small `werfault_poll_secs` means
Blender genuinely ran/hung that long; a small `time_to_exit` with a large
`werfault_poll_secs` means the stall was the post-exit WerFault poll.  **Root
cause still unconfirmed — revisit once a stalled crash is captured with this
data**, then decide whether `_POST_EXIT_WERFAULT_SECS`/`_STALE_LOG_TIMEOUT`
need tuning.

**Observed (v0.2.26 batch):** Two crashes in the same batch behaved differently.
The first crash stalled for roughly 5 minutes before the launcher moved on; the
second crash was detected almost immediately.

**Possible causes:**
1. WerFault appeared for the first crash but `_find_werfault_for_pid` missed it
   (process-tree mismatch or timing gap between exit and WerFault spawn), leaving
   Blender's process lingering for several minutes before the stale-log watchdog
   fired.
2. Blender hung for several minutes before actually exiting with a non-zero code.

**Proposed investigation:**
- Log `proc.pid`, `exit_code`, and time-from-launch-to-exit for each job.
- Check whether `_POST_EXIT_WERFAULT_SECS` (currently 30 s) should be extended,
  or whether a shorter `_STALE_LOG_TIMEOUT` is needed for the hung-process case.

**Files:** `smoke_launcher.py` — near WerFault post-exit poll.  TODO comment added in v0.2.26.

---

## TODO-23: Retry overall batch time estimate is unreliable

**Observed:** When a retry batch starts, `batch_jobs_elapsed` resets to 0.  The
per-job estimate (bake + render for the single retry job) is correct, but the
overall estimate across all retry jobs uses stale or zeroed elapsed time.

**Proposed fix:** Carry `batch_jobs_elapsed` across retry starts, or compute the
overall estimate purely from the per-job ETA multiplied by remaining jobs.

**Files:** `__init__.py` — `_poll_batch_progress_impl` overall-time-estimate section.

---

## TODO-24: Per-frame bake timing not collected

**Observation:** `perf_log.json` stores total bake time + total frames but not
per-frame timings.  Longer-running frames (high smoke density late in simulation)
are not distinguishable from early frames.  A per-frame rate model could improve
ETA accuracy for jobs with high frame counts.

**Constraint:** `bpy.ops.fluid.bake_all()` is blocking — no way to hook per-frame
completion without a `frame_change_post` handler or a monitor thread in the worker.

**Proposed approach:** Register a `bpy.app.handlers.frame_change_post` handler
before calling `bake_all()` that records a timestamp each time Mantaflow advances
a frame; write the per-frame data to `perf_log.json` on completion.

**Files:** `smoke_worker.py` — bake section; `perf_log.json` schema.

---

## ~~TODO-20~~ (substantially addressed v0.2.12–v0.2.13): Crashes not being caught / logged

**Observed (2026-05-11):** Blender crashes are still occurring during batch runs and
are not being recorded — no crash log written, launcher does not detect the crash,
and the job log UI shows no FAILED indicator.

**Current crash-suppression stack (v0.2.6+):**
- Windows Job Object with `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` (prevents
  WerFault dialog from blocking the launcher)
- `_find_werfault_for_pid` polls for `WerFault.exe` / `WerFaultSecure.exe`
- `_save_crash_log` called on any non-zero exit code
- `_POLLER_STALE_SECS = 35 min` stale-detection in the UI timer

**Possible failure modes to investigate:**
1. **Blender exits with code 0 on crash** — Python `sys.exit(0)` paths inside
   Blender report success; launcher only flags non-zero exits.  A worker Python
   exception that is caught internally and then `sys.exit(0)` is called will look
   like a clean finish.
2. **Blender hangs (no exit)** — Job Object limit only fires on unhandled exceptions;
   a deadlock or infinite loop produces no exit at all.  The 35-min stale marker
   in the UI is the only safeguard, and it requires Blender's log to stop updating.
3. **Job Object creation fails silently** — `_create_crash_suppression_job` returns
   `None` on any `OSError`; the launcher falls back to `SEM_NOGPFAULTERRORBOX` only.
   If the Job Object failed, WerFault may still block.
4. **Crash before log file is created** — if Blender crashes before the worker writes
   a single log line, the launcher has no log to associate with the crash.

**Proposed investigation steps:**
- Add launcher logging: write the Job Object handle value (or "FAILED") to stderr at
  startup.
- Add a per-job **timeout**: if the launcher process is still alive after
  `(estimated_bake_secs + estimated_render_secs) × N` seconds, kill it and mark as
  CRASHED.  This handles the hang case.
- On non-zero exit *or* timeout, write the Blender `returncode` and last N lines of
  `blender_stderr.txt` to `crash_log.txt`.
- Consider writing a `.crashed` sentinel file (distinct from `.done`) that the UI
  timer shows as a red CRASHED state in the job log.

**Resolution (v0.2.12–v0.2.13):**
- `job_NNNN.worker_done` sentinel written by the worker before `quit_blender()`;
  absence on exit-0 → CRASHED status in the UI (v0.2.12).
- Startup timeout (120 s) kills Blender if log never appears; wall-clock timeout
  (4 h) kills if job runs forever (v0.2.13).
- CRASHED (unexpected crash) shown as `ERROR` icon + ⚠ prefix, distinct from
  FAILED (controlled error exit) shown as `CANCEL` icon + ✗ prefix (v0.2.13/v0.2.19).
- Remaining gap: Blender-restarts-itself scenario still undetected. Documented in
  BUG_TRACKER.md BUG-002.

---

## ~~TODO-21 / TODO-5 / TODO-17~~: Job Log rows blank for in-progress and completed jobs — **DONE** (v0.2.16+, v0.2.19)

**Observed (2026-05-11):** After v0.2.9's `_job_statuses` dict fix (which moved
`item.status` writes out of the poll timer), rows for IN_PROGRESS and COMPLETE jobs
still go entirely blank — no job number, no job name, no status dot.

**Root cause (revised):** The v0.2.9 fix only moved `item.status` to a module-level
dict.  `draw_item` still reads `item.job_name` and `item.job_number` directly from
the `CollectionProperty` item.  Any RNA write to the parent `SmokeSettings`
PropertyGroup from the poll timer (e.g. `s.batch_progress`, `s.job_log_index`,
`s.job_log_auto_scroll`) may trigger Blender to re-evaluate the PropertyGroup,
momentarily returning default values (0 / "") for CollectionProperty item fields
during the same draw pass.

**Correct fix:** Store ALL display data (job_number, job_name) in a module-level list
`_job_log_rows: list[(int, str)]` populated at export time.  `draw_item` reads only
`item.job_number` from RNA (as a collection-index proxy; if 0, skip the row) and
looks up job_name from `_job_log_rows`.  The poll timer never writes to any
CollectionProperty item field — not status, not number, not name.

**Files:** `__init__.py` — `_job_log_rows`, `export_batch`, `draw_item`,
`SMOKE_OT_remove_all_jobs.execute`, `_reset_on_load`.

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

## ~~TODO-5 / TODO-17~~: Job Log rows go blank on status transition — **DONE** (see TODO-21 above)

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

**Resolution:**
- v0.2.16: `draw_item` uses Blender's `index` parameter directly (no RNA reads for
  job number); `_update_job_log_statuses` keyed off `_job_log_rows[idx]` not RNA.
- v0.2.17: `_flt_flag=0` added to `draw_item` signature for Blender 5.x compat.
- v0.2.19: `SEQUENCE_COLOR_XX` icons replaced with stable alternatives; Unicode
  status prefix added; `layout.alert = True` for error rows.
  Status: DEPLOYED / UNVERIFIED — awaiting production batch run confirmation.

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

