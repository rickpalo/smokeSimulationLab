# BatchSimLab — Completed / Closed Issues (archive)

> _Project renamed from "SmokeSimLab" to "BatchSimLab" at v0.6.3 (surface-only);
> all TODO-* IDs remain stable across the rename._

This is the archive of **DONE / REJECTED / CANCELLED / substantially-addressed**
tasks, moved out of [TODOS.md](TODOS.md) so the active list stays short. Active
work lives in [TODOS.md](TODOS.md); long-horizon plans in [ROADMAP.md](ROADMAP.md).
Per-version changelog detail lives in `RELEASING.md`.

Ordered most-recent-first by TODO number where practical.

---

## TODO-55: Batch emitter / flow object parameters — **DONE** (v0.9.0)

**Filed + resolved 2026-06-16.**  A batch can now sweep per-emitter **flow
object** settings, not just domain settings (the v0.9.0 roadmap target — see
[ROADMAP.md](ROADMAP.md)).  Shipped in increments, all on `main`:

- **Discovery** — `find_emitters(scene, domain)` = `find_fluid_emitters` (scan
  for FLUID FLOW objects) → `emitters_inside_domain` (keep those whose world
  AABB overlaps the domain's; the domain has no backlink to its emitters) →
  sort by name.  **Single-domain addon** (documented).
- **State** — `EmitterSettings` + `VelocityItem` PropertyGroups;
  `SmokeSettings.emitters` CollectionProperty.  Each scalar carries the standard
  Range/List sextet so `expand_param` works on it unchanged.
- **UI** — collapsible **Emitters** section (one default-collapsed box per
  emitter), auto-populated from live `FluidFlowSettings` on domain-select +
  **Refresh Emitters**.  Params: Initial Temperature, Density, Surface Emission,
  Volume Emission; Initial Velocity toggle → Source/Normal scalars + an Initial
  X/Y/Z **list of vectors** (`x, y, z`, default `0,0,0`).
- **Job gen + cache safety** — `generate_jobs` layers emitters over the domain
  generators (LIMITED one-axis-at-a-time; ALL = domain × emitter product); the
  `emitters` block rides into job JSON via `job_data["params"]`; `make_name`
  encodes default-suppressed `E<i>` tokens, collision-safe (BUG-013 family,
  `TestEmitterNameNoCollisions`); emitter-free jobs keep byte-identical names.
- **Worker** — applies each job's emitters block to the matching flow object's
  `flow_settings` by name before baking (before the maintain_density pass, which
  keeps final say on density).
- **Text overlay** — per-emitter settings prepended as line(s) to the existing
  **Dissolve_Text** (lower-left) and **Time_Text** (lower-right) FONT objects
  (even-indexed emitters left, odd-indexed right), e.g.
  `Emitter1: Init Temp-1, Dens-1, SurfE-1.5, VolE-0`.

Tests: `test_todo55_emitters.py` (78) + `test_todo55_worker.py` (16).  Addon +
worker → 0.9.0; **re-export required**.  _(Roadmap note: v0.8.0's UI/ETA polish
bundle was not done first; this shipped the emitter feature ahead of it.)_

---

## TODO-54: RELEASING.md describes a release-copy layout that doesn't exist — **DONE** (2026-06-16)

**Doc-only.**  Rewrote RELEASING.md's "Repository structure" + numbered steps to
match reality: a single tracked source tree at `scripts/SmokeSimLab/`, no
`SmokeSimLab/` release-copy dir and no copy step (the old doc described a
two-directory workflow that never existed); releases build straight from
`scripts/SmokeSimLab/` into `dist/` (ignored) / `docs/` (feed).  No code, no
version bump.

---

## TODO-53: Launcher .vdb heartbeat to the job log — **DONE** (v0.7.5)

**Resolved 2026-06-15.**  The launcher's watchdog loop now appends a
`[heartbeat] baking <data|noise>: data X/N  noise Y/N` line to the job log,
throttled to once per `_HEARTBEAT_INTERVAL` (30 s) and **only when the combined
data+noise VDB count has grown**.  New `_count_cache_vdb(output_path, name)`
scans `Cache/<name>/data` + `noise` with `os.scandir` (never `os.walk` — the
Norton/Synology filter chain blocked recursive walks in worker v0.5.3/0.5.4),
swallowing any OSError so a heartbeat failure never kills the job.

Effect: a healthy bake refreshes the log mtime on every new frame, so the
existing 1800 s stale-log watchdog can't false-kill a slow-but-progressing
bake; a true hang produces no new frames → no heartbeat → log goes stale → the
watchdog fires as designed.  The noise-hang case (TODO-49 job 8) is now caught
cleanly: once the data bake fills to N, the combined total only grows if noise
frames appear, so a stuck noise bake leaves the log stale with a final
`data N/N  noise 0/N` heartbeat that pinpoints the phase.  Reuses the same
frame-numbered VDB pattern the worker's TODO-52 boundary line exposes.

Tests: `test_todo53_heartbeat.py` (11) — counting, scandir-not-walk guard,
write-only-on-growth guard, throttle, version match.  Launcher → 0.6.4;
addon → 0.7.5 (gate constant).

---

## TODO-52: Separate "Baking noise" stage in the progress model — **DONE** (v0.7.4)

**Resolved 2026-06-15.**  Implemented as a **sub-stage** of the Baking
macro-stage (label + subtask bar), not a full separate band: the worker logs
`Baking noise (bake_noise)...` between `bake_data()` and `bake_noise()` (both
branches; worker → 0.7.2), and `_STAGES` gains
`("Baking noise (", "Baking noise", 1)` at the **same** completed-rank as the
data bake — so the stage *label* flips to "Baking noise" (rightmost-keyword
match) and the subtask bar restarts 0→N counting the `noise/` subdir (via
`_count_vdb_frames(..., subdir="noise")`), while Bar-3 band math and
`_TOTAL_SUBTASKS` stay untouched.  A hung noise bake now shows
"Baking noise (0 of 500)" instead of a frozen data bar at "500 of 500"
(TODO-49).  **Deferred (intentionally):** giving noise its own time-estimate
*band* (dynamic `_TOTAL_SUBTASKS` 4↔5) — the user confirmed estimates aren't
vital, and a separate band risks the complex multi-bar poller for little gain.
Re-open that slice if per-phase ETA accuracy becomes important (ties to
TODO-51).  Tests: `test_todo52_noise_stage.py` (9) + the existing
`TestStagesMatchWorkerLog` guard now covers the noise keyword.  Addon → 0.7.4;
worker → 0.7.2.

---

## TODO-50: Per-job-line status + warning tooltip in the Job Log list — **CANCELLED** (2026-06-15)

**Cancelled the day it was filed.**  Blender's `UIList.label()` has no tooltip
API, and the user decided the inline-glyph fallback ("◐/⚠ + status word in the
row text") isn't worth it either.  The aggregate panel warning box from TODO-49
already covers the noise-ceiling case; per-row job status is visible enough via
the existing status icon + unicode prefix.  Re-open only if a concrete need for
per-row hover text resurfaces.

Original asks (for the record): (1) per-row status tooltip mapping
`_job_statuses` values to human text; (2) per-row noise-ceiling warning. Blocker:
`UIList.draw_item` uses `layout.label()`, which has no hover-tooltip API —
tooltips only attach to props/operators.

---

## TODO-49: Warn when noise up-res grid exceeds the safe ceiling — **DONE** (v0.7.2)

**Filed + Resolved 2026-06-15.**  During a 10-job batch on the i9-13900 /
128 GB machine (Blender 5.1.1), the 256-resolution jobs failed in the **noise**
bake (the data bake finished — progress bar reached "Baking 500 of 500" — then
`bpy.ops.fluid.bake_noise()` never returned):

| Job | res × upres | edge | outcome |
|-----|-------------|------|---------|
| many 128 jobs | 128×3 | 384³ | OK |
| job 6 | 256×2 | 512³ | OK |
| job 7 | 256×3 | 768³ | crash — `EXCEPTION_ACCESS_VIOLATION` in `tbbmalloc.dll`, exit 1 |
| job 8 | 256×4 | 1024³ | hang — stale-log watchdog killed it after 30 min |

Root cause is in Mantaflow's high-resolution noise bake (32-bit grid indexing
overflows once the vec3 element count passes 2³¹ near 1024³), **not** our code —
consistent with the existing finding that the stack-trace crashes trace to
Blender internals.  It is **not** a hard limit: after re-exporting and
restarting twice, every job (including 256×4) eventually completed.

**Fix (v0.7.2, addon-only):** new module-level helpers `noise_grid_edge()` and
`noise_grid_exceeds_ceiling()` + constant `_NOISE_UPRES_EDGE_WARN = 512`
(exclusive; the known-good 512³ case does not warn).  The Export Batch panel
counts jobs over the ceiling and shows a non-blocking red warning box.  Export
is **not** disabled.  Covered by `test_noise_ceiling.py` (8 tests).

---

## TODO-48: Compact filename format — trim trailing zeros + shorter OFF indicator — **DONE** (v0.7.1)

**Filed + Resolved 2026-06-02 → 2026-06-05.**  Bundled with TODO-47 in
v0.7.1.  New `_fmt_num()` helper trims trailing zeros via
`round(x, 3):g`; single-char `x` replaces `-OFF` (Dx / Nx / ATx).
See RELEASING.md v0.7.1 row + `test_v071_make_name.py` (33 tests).

Decisions baked in: lowercase `x` no-dash for OFF (`Dx`/`Nx`/`Fx`); fire-off
suffix SUPPRESSED for backwards-compat so pre-v0.7.1 cache names stay valid.

---

## TODO-47: Include v0.7.0 sim params in make_name() — **DONE** (v0.7.1)

**Filed + Resolved 2026-06-02 → 2026-06-05.**  Bundled with TODO-48 in
v0.7.1.  v0.7.0 params now appear in filenames with defaults-suppressed
format (`_TS<n>` only when time_scale ≠ 1.0, full `_F-Y_BR<n>...` block
only when use_fire is on, etc.).  `TestNoCacheCollisions` (5 tests)
proves every new param produces distinct filenames so cache collisions
of the BUG-013 / BUG-014 family can't recur.  See RELEASING.md v0.7.1.

Motivation: pre-fix, two jobs differing only in (e.g.) time_scale shared a cache
dir and the second silently SKIP-baked the first's cache — same class as
BUG-013.

---

## TODO-45: Iterate Slow Dissolve checkbox + audit no-dissolve-jobs-when-off — **DONE** (v0.6.2)

**Filed + Resolved 2026-06-01 → 2026-06-02.**  New `iterate_slow_dissolve`
BoolProperty + UI checkbox paired with Slow Dissolve on the same row in
the Dissolve section.  When checked AND `use_dissolve=True`, every
dissolve-using job in both LIMITED and ALL modes gets a companion with
the opposite slow_dissolve value.  Combines cleanly with
`iterate_dissolve_both`.  Part B audit confirmed (with regression test)
that no dissolve sweep jobs are created when `use_dissolve` is off.
Tests: 11 new in `test_todo45_iterate_slow.py`.  See RELEASING.md v0.6.2.

---

## TODO-44: Collapsible Output + Progress sections (UI reorg) — **DONE** (v0.6.1)

**Filed + Resolved 2026-05-29 → 2026-06-01.**  See RELEASING.md v0.6.1 row
and `tests/test_todo44_sections.py` (19 regression tests covering
property registration, panel structure, auto-expand logic, and Job Log
nesting inside Progress).  Reorganised the Setup panel into collapsible
**Output** (default open) and **Progress** (auto-expand when a batch is running
or a summary is showing) sections via `show_output` / `show_progress`
BoolProperties, prep for the v0.7.0 param influx (TODO-41/42).

---

## TODO-37: Reorder Gas Parameters to match Blender's tab order — **DONE** (v0.6.0)

**Filed + Resolved 2026-05-29.** Cosmetic UI fix.  The Gas Parameters section
previously drew **Vorticity → Buoyancy Density → Buoyancy Heat**, while Blender's
native Fluid Domain panel shows **Buoyancy Density → Buoyancy Heat → Vorticity**.
Resolution: swapped the three `_sub_param_ui()` calls in `_gas_ui()`; purely
visual, property names (vorticity/alpha/beta), job-dict serialisation, CSV
columns, and `make_name()` output all unchanged.  Test:
`test_v060_fixes.TestTodo37GasParamsOrder`.

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

Note: RESUME still re-bakes from frame 1 (Mantaflow has no scripted mid-range
resume) — that caveat is what TODO-31 (still open) has to work around.

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

Single-pass (`--phase=both`) and the bake phase are unaffected.  Tests:
`tests/test_run_batch_gating.py::TestRenderPhaseFastFail` (4).  Worker → 0.4.8.

---

## TODO-33: Render Animation checkbox — still-only mode — **DONE** (v0.4.5)

**Filed 2026-05-28.** New `render_animation` BoolProperty (default True) below
*Render Simulation Result*; greyed out when rendering is off.  When unchecked,
the worker skips the per-frame PNG sequence + ffmpeg MP4 mux and renders only
the final still PNG (`<name>.png`).

**Resolution (v0.4.5):**
- `SmokeSettings.render_animation` BoolProperty + UI row gated on
  `render_simulation_result`; reset-on-load default True.
- `export_batch` writes `render_animation` into each job JSON.
- Worker reads it (default True for pre-TODO-33 JSONs); when False: empties
  `frames_to_render`, gates the ffmpeg block, and the TODO-32 final-still copy
  falls back to rendering so `<name>.png` is still produced.
- Tests: `tests/test_run_batch_gating.py::TestRenderAnimationGate` (4).
  Worker → 0.4.5.

---

## TODO-32: Final still re-renders frame_end with identical settings — just copy it — **DONE** (v0.4.4)

**Resolution (v0.4.4):** worker's "Final still" block now `shutil.copy2`'s
`<name>_frames/frame_<frame_end>.png` → `<name>.png` when `frame_end` was in
`frames_to_render` this run AND the source file is present.  Falls back to the
original render path on copy failure OR when the frame was placeholder-skipped.
Tests: `tests/test_run_batch_gating.py::TestWorkerFinalStillCopy`.

Saved ~5 s/job at EEVEE 1440×1080 (more at higher res / Cycles) by not
re-rendering a pixel-identical final still.

---

## TODO-30: Allow renaming a job in the Job Log (double-click / F2) — **REJECTED** (2026-05-27)

**Rejected:** not worth the effort. The job `name` *is* the directory name
everywhere (`Cache/<name>/`, `Renders/<name>_frames/`, `<name>.mp4`,
`<name>.png`, the `name` column in `results.csv`, and the `d.cache_directory`
Mantaflow bakes in). A useful rename would require moving on-disk artifacts and
would not survive a re-export (names are parameter-derived via `make_name`, so
re-export regenerates them and breaks dedup/reuse). Low value for the
complexity. Kept for the record.

---

## TODO-29: Warn when rendering is on but the scene has no camera — **DONE** (v0.4.6)

**Resolution (v0.4.6):** `_scene_has_camera(scene)` helper added (pure;
`any(obj.type == 'CAMERA' for obj in scene.objects)`).  Both Export Batch and
Run Batch now check `render_simulation_result and not _scene_has_camera(...)`
in their `invoke` and show a confirmation dialog if the warning fires.  Export
Batch combines the camera warning with the existing high-resolution warning in
one dialog.  Cancel aborts; OK proceeds anyway.  Tests:
`tests/test_camera_check.py` (9).

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
  `SMOKE_PT_panel.draw`).
- Tests: `tests/test_export_append.py` — `TestExistingJobsForBat`,
  `TestJobRunCmd`, `TestJobBatBlock`.

**Observed bug:** appending jobs while a batch ran left only the first job in the
executed .bat (the others were created in jobs/ but never listed in any executed
.bat), leaving them NOT_STARTED forever and confusing auto-retry.

---

## TODO-26: "Render Simulation Result" checkbox to skip rendering entirely — **DONE** (v0.3.0)

**Resolution (v0.3.0):**
- `SmokeSettings.render_simulation_result` BoolProperty (default `True`), with an
  update callback `_on_render_sim_result_update` that clears `show_results` when
  rendering is turned off.
- Panel: checkbox below "Automatically Retry Failed Jobs"; Render Engine +
  Samples row and "Display Results When Finished" greyed out when off.
- `export_batch` writes `render_simulation_result` into each `job_NNNN.json`.
- `smoke_worker.py` reads the flag (default `True` for pre-TODO-26 JSONs) and
  skips the whole MP4 + still render block when `False`; results.csv and
  perf_log records still run so the job is recorded as complete.
- Tests: `tests/test_run_batch_gating.py` — `TestRenderSimResultUpdate`,
  `TestWorkerRenderGuard`.

Goal: bake-only batches (validate the cache before committing render time, or
render later by hand with different settings).

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

---

## TODO-22: Crash timing inconsistency — **INSTRUMENTED** (v0.3.0) — _diagnostics in; root cause open, tracked in TODOS.md_

> Note: this entry remains **active** (waiting on a captured stalled crash to
> confirm root cause). The live copy is in [TODOS.md](TODOS.md); kept here only
> as a pointer. Do not treat as closed.

---

## ~~TODO-20~~ (substantially addressed v0.2.12–v0.2.13): Crashes not being caught / logged

**Resolution (v0.2.12–v0.2.13):**
- `job_NNNN.worker_done` sentinel written by the worker before `quit_blender()`;
  absence on exit-0 → CRASHED status in the UI (v0.2.12).
- Startup timeout (120 s) kills Blender if log never appears; wall-clock timeout
  (4 h) kills if job runs forever (v0.2.13).
- CRASHED (unexpected crash) shown as `ERROR` icon + ⚠ prefix, distinct from
  FAILED (controlled error exit) shown as `CANCEL` icon + ✗ prefix
  (v0.2.13/v0.2.19).
- Remaining gap: Blender-restarts-itself scenario still undetected. Documented in
  BUG_TRACKER.md BUG-002.

---

## ~~TODO-21 / TODO-5 / TODO-17~~: Job Log rows blank for in-progress and completed jobs — **DONE** (v0.2.16+, v0.2.19)

**Root cause:** writing `item.status` (and any RNA write to the parent
`SmokeSettings` PropertyGroup) from the poll timer momentarily invalidated the
CollectionProperty item, so `draw_item` read back default `job_number=0` /
`job_name=""` during the same draw pass.

**Resolution:**
- All display data (job_number, job_name) moved to a module-level
  `_job_log_rows: list[(int, str)]` populated at export time.  `draw_item` uses
  Blender's `index` parameter directly and looks up the name from `_job_log_rows`;
  the poll timer never writes to any CollectionProperty item field (v0.2.16).
- `_flt_flag=0` added to `draw_item` signature for Blender 5.x compat (v0.2.17).
- `SEQUENCE_COLOR_XX` icons replaced with stable alternatives; Unicode status
  prefix added; `layout.alert = True` for error rows (v0.2.19).

---

## ~~TODO-19~~: Progress bars show 0 during bake / render — **DONE** (v0.2.8)

**Observed:** Subtask bar showed "Rendering (0 of 500)" while frame_0497 was
actively rendering; stuck at 0 throughout.

**Root cause (render bar):** `_count_png_frames` used `since=batch_job_start_time`
to exclude prior-run frames; if the log wasn't detected until late, all
already-rendered frames had mtime before the baseline and were filtered out.
**Root cause (bake bar):** `_count_vdb_frames` always looked in
`Cache/<current_job_name>/data/`; when baking into an alternate cache dir the
count stayed 0.

**Fix:**
- Removed the `since` mtime filter from `_count_png_frames`; added
  `batch_render_frame_baseline` set once when the "Rendering animation" stage is
  first detected. Progress = (current_count − baseline), capped at render_target.
- `_count_vdb_frames` extracts the effective cache dir from the log tail (the
  "Effective cache dir" line added in v0.2.7), falling back to
  `Cache/<name>/data/`.
- `batch_bake_frame_baseline` set when baking starts; bake subtask shows
  "(new_frames of to_bake)" so a partial resume reads "Baking (30 of 250)".

---

## ~~TODO-18~~: Cache search logging and config-file false-positive — **DONE** (v0.2.7)

**Observed:** Jobs with "Use Existing Cache" enabled still baked from scratch even
when a complete cache existed.

**Root cause:** Mantaflow writes per-frame config checkpoints
(`config/config_0001.uni`, …) to every cache dir on domain init — before any
data. These matched `r'_\d+\.(vdb|uni)$'`, so a config-only directory passed
`has_files` and was selected, then counted in `baked_frames`, making
`bake_complete=True` and skipping the bake even though no real VDB data existed.

**Fix:** Skip the `config/` subdirectory when walking candidate directories for
both `has_files` and the `baked_frames` count.  Plus a full `_log` cache-search
section (search path, regex, every candidate accept/reject + reason, chosen
effective dir, frame range, missing frames, bake path SKIP/RESUME/FULL).

---

## ~~TODO-16~~: Reliable crash-dialog suppression — **DONE** (v0.2.6)

`SEM_NOGPFAULTERRORBOX` was unreliable (Blender resets the process error mode at
startup; doesn't cover `WerFaultSecure.exe`). **Fix:**
1. `smoke_launcher.py` creates a Windows Job Object with
   `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` before spawning Blender and
   assigns Blender to it immediately after `Popen` (can't be overridden by the
   child).
2. `_find_werfault_for_pid` checks both `WerFault.exe` and `WerFaultSecure.exe`.
3. `_POLL_INTERVAL` 2.0 s → 0.5 s.
4. Post-exit WerFault poll 3 s → `_POST_EXIT_WERFAULT_SECS = 30` s.
5. `_save_crash_log` called unconditionally on any non-zero exit.
6. `LAUNCHER_VERSION` → `"0.2.6"`.

_(Note: the Job Object kill window later turned out to eat crash dumps — see the
still-open TODO-27 in TODOS.md.)_

---

## ~~TODO-15~~: "Remove All Jobs" button in Utilities section — **DONE** (v0.2.6)

New operator `SMOKE_OT_remove_all_jobs` (`bl_idname = "smoke.remove_all_jobs"`).
`invoke()` confirms before deleting. Deletes the `jobs/` folder (`shutil.rmtree`
→ per-file fallback on `PermissionError`), `run_smoke_batch.bat`,
`smoke_worker.py`, `smoke_launcher.py` from `output_path`. Clears job-log and
batch-progress state; stops the poll timer. Leaves `domain_obj`, `output_path`,
and sim parameters untouched. `TRASH` icon.

---

## ~~TODO-14~~: File versioning for helper scripts — **DONE** (v0.2.5)

No mechanism existed to detect that an exported `smoke_worker.py` /
`smoke_launcher.py` was from an older addon version. **Fix:** `WORKER_VERSION` /
`LAUNCHER_VERSION` strings in the helpers; `_EXPECTED_WORKER_VERSION` /
`_EXPECTED_LAUNCHER_VERSION` in `__init__.py`; `_read_helper_version()` reads the
string from the first 30 lines without importing; `SMOKE_OT_run_batch.execute`
warns + prompts re-export on mismatch.

---

## ~~TODO-13~~: Crashed job freezes progress bars — **DONE** (v0.2.5)

`_poll_batch_progress` had no exception guard; an unhandled error inside the timer
silently killed it (Blender unregisters a raising timer). **Fix:**
1. `_poll_batch_progress` → `_poll_batch_progress_impl` wrapper: outer catches all
   exceptions, prints a warning, returns `5.0` to keep the timer alive.
2. `_poll_state` + `_POLLER_STALE_SECS = 35 * 60`: when the active job's log mtime
   is unchanged for 35 min, the subtask label shows "No log activity for N min —
   job may be frozen."

---

## ~~TODO-12~~: Full addon reset on .blend load — **DONE** (v0.2.5)

`_reset_on_load` previously preserved several properties, so job log rows
persisted into the next session. **Fix:** it now resets every property to its
factory default (`domain_obj = None`, `output_path = "C:/tmp"`, all Utilities
flags, all UI toggles); the polling timer is unregistered at the top of the
handler so it can't fire between resets.

---

## ~~TODO-11~~: Settings dropdown shows stale name on load — **DONE** (v0.2.4)

On .blend load the preset dropdown showed a `.smokesettings` filename while the
panel settings were actually from the blend file. **Root cause:** `_reset_on_load`
cleared `settings_file_path` / `settings_snapshot` but not `settings_file_enum`;
Blender restored the saved enum value and `_on_settings_enum_update` doesn't fire
during RNA restoration. **Fix:** added `s.settings_file_enum = ""` to
`_reset_on_load` (callback fires but returns early on empty stem).

---

## ~~TODO-2~~: Retry job does not find partial bake cache — **DONE** (v0.2.3)

**Root cause:** the `has_files` candidate check used
`f.endswith('.vdb') or f.endswith('.uni')`, which matched Mantaflow's
config/metadata `.uni` files created on domain init. A config-only directory
passed `has_files=True`, produced empty `baked_frames`, fell into the
full-rebake else-branch, switched back to the job's own cache dir, called
`free_all()`, and **destroyed the complete cache** before rebaking. (job_0000:
the `_0000` cache had all 500 frames; retry found config-only `_0030`, freed
`_0000`, rebaked all 500 unnecessarily.) **Fix:** `has_files` uses the same
frame-number regex (`re.search(r'_\d+\.(vdb|uni)$', f)`) as the counting walk.

---

## Older one-line DONEs

- **~~TODO-1~~**: Crash log written to jobs folder — **DONE** (already implemented in launcher).
- **~~TODO-3~~**: "Utilities" collapsible section — **DONE** (v0.1.x).
- **~~TODO-4~~**: Hide Job Log section when not populated — **DONE** (v0.2.0).
- **~~TODO-6~~**: Auto-scroll Job Log — **DONE** (v0.2.2).
- **~~TODO-7~~**: Update default parameter values — **DONE** (defaults were already correct).
- **~~TODO-8~~**: Fix negative/zero RT_proj values — **DONE** (v0.2.1).
- **~~TODO-9~~**: Sort exported jobs by resolution ascending — **DONE** (v0.2.1).
- **~~TODO-9~~**: analyze_perf.py render tables — **DONE** (v0.2.1).
- **~~TODO-10~~**: Debug log — **DONE** (v0.2.2).
