# How to Release SmokeSimLab

## Repository structure

```
smokeSimulationLab/          ← repo root
├── SmokeSimLab/
│   ├── __init__.py          ← addon code
│   ├── smoke_worker.py      ← headless batch worker
│   └── smoke_launcher.py    ← crash-safe launcher wrapper
├── documentation/
├── tests/
├── README.md
├── LICENSE
└── .gitignore
```

Your local working copies live in `scripts/SmokeSimLab/` (ignored by git).
When ready to release, copy them into `SmokeSimLab/` at the repo root.

---

## Releasing a new version

### 1. Update the version number

Open `SmokeSimLab/__init__.py` and bump the version in `bl_info`:

```python
bl_info = {
    ...
    "version": (0, 2, 21),   # <-- change this
    ...
}
```

Use the format `(major, minor, patch)`:
- **patch** (0.2.x) — small bug fixes, test additions, documentation updates
- **minor** (0.x.0) — new features, backwards compatible
- **major** (x.0.0) — breaking changes

Also update `WORKER_VERSION` in `smoke_worker.py` and `LAUNCHER_VERSION` in
`smoke_launcher.py` if those files changed, and keep `_EXPECTED_WORKER_VERSION`
/ `_EXPECTED_LAUNCHER_VERSION` in `__init__.py` in sync with them.

### 2. Copy your updated scripts into the repo folder

```
scripts/SmokeSimLab/__init__.py       →   SmokeSimLab/__init__.py
scripts/SmokeSimLab/smoke_worker.py   →   SmokeSimLab/smoke_worker.py
scripts/SmokeSimLab/smoke_launcher.py →   SmokeSimLab/smoke_launcher.py
```

### 3. Run tests

```bash
python -m pytest tests/
```

All tests must pass before tagging a release.

### 4. Commit your changes

```bash
git add SmokeSimLab/__init__.py SmokeSimLab/smoke_worker.py SmokeSimLab/smoke_launcher.py
git commit -m "v0.2.21: description of what changed"
```

### 5. Tag the commit and push

```bash
git tag v0.2.21
git push origin main --tags
```

The tag must start with `v` followed by numbers, e.g. `v0.2.21`.
Pushing the tag triggers the GitHub Actions release workflow automatically.

### 6. Check the release on GitHub

Go to: https://github.com/rickpalo/SmokeSimLab/releases

GitHub Actions will create the release and attach `SmokeSimLab.zip`
(the installable Blender addon) within a minute or two.

---

## If you need to redo a release tag

```bash
git tag -d v0.2.21                    # delete local tag
git push origin :refs/tags/v0.2.21   # delete remote tag
# then re-tag and push as normal
```

---

## Version history

| Version | Notes |
|---------|-------|
| 0.1.0   | Initial internal release |
| 0.2.0   | Crash detection, launcher watchdog, log sentinel |
| 0.2.5   | stderr capture to blender_stderr.txt |
| 0.2.7   | config/ false-positive fix (BUG-006) |
| 0.2.10  | Param-derived names — no more _0000 suffix (BUG-005) |
| 0.2.12  | worker_done sentinel; CRASHED status distinct from FAILED |
| 0.2.13  | Startup + wall-clock watchdog timeouts |
| 0.2.14  | Job log blanking fix via as_pointer() row identification |
| 0.2.15  | BUG-004 SKIP BAKE cache wipe fix (path equality check) |
| 0.2.16  | Pre-flight inspector; improved addon detection |
| 0.2.17  | flt_flag fix for Blender 5.x draw_item signature |
| 0.2.18  | BUG-004 presave/rename approach replacing path-equality check |
| 0.2.19  | Stable icons (Blender 5.1.1); Unicode status prefix; stale-log detection; mtime-based render progress; Reset To Defaults operator |
| 0.2.20  | Fix iterate_both AttributeError; extract _has_error; remove redundant imports; bl_options on value list operators; make_name + iterate_both regression tests |
| 0.2.21  | BUG-007: dedupe identical jobs in overlapping sweep baselines |
| 0.2.22  | BUG-008: fix negative bake/setup times; calibrate rate constants |
| 0.2.23  | Filter low-res perf samples; Fix-2 cache-reuse diagnostics |
| 0.2.24  | Fix Reset All crash; baseline-fallback improvement; UX polish |
| 0.2.25  | Bake-time sidecar file so SKIP BAKE reports real bake time |
| 0.2.26  | BUG-009: retry progress bar showed wrong job's frame count; Monitor Existing Jobs button; crash-timing TODO in launcher |
| 0.2.27  | Auto-close batch console on completion; Collect Debug Logs checkbox now toggles pause |
| 0.2.28  | Fix Monitor Existing Jobs time estimate; Utilities button reorder |
| 0.2.29  | Mtime-filtered VDB counting so progress bar advances during full re-bake |
| 0.2.30  | Fix RESUME re-baking from frame 1 by setting cache_frame_pause_data after presave merge |
| 0.2.31  | Revert cache_frame_pause_data: setting it clears frames 1–395; re-bake from 1 is correct |
| 0.2.32  | RESUME save/reload .blend to trigger Mantaflow rescan + auto-resume (with diagnostic logging) |
| 0.2.33  | Launcher grace period for crash dumps (partial TODO-27); sync WORKER/LAUNCHER versions |
| 0.3.0   | **Minor release.** TODO-25 Run Batch gating; TODO-26 Render Simulation Result (bake-only mode); TODO-28 append .bat re-lists prior jobs + mid-run export/run guard; BUG-011 bake honours the job frame range (set cache_frame_start/end — was baking the .blend's full 500-frame range); TODO-22 crash-timing diagnostics (time_to_exit via debug_log); crash_log + worker log now record the Blender version |
| 0.3.1   | BUG-010 fix: RESUME no longer save/reloads the .blend (open_mainfile mid-script hung bake_all in windowed/EEVEE mode — observed deadlocking a res-512 job). Reverts to merge + bake-from-1 in-process |
| 0.3.2   | Version stamping for cross-version comparison: addon_version in job JSON; perf_log records carry addon_version + worker_version; results.csv gains a trailing `version` column; estim_log.jsonl records carry addon_version; crash_log header records addon + launcher version; worker/launcher log their versions. Adds BACKGROUND_BAKE_DESIGN.md (scoping doc). |
| 0.3.3   | Auto-retry now repeats up to 3 rounds per batch (was a single round), re-running only still-failing jobs (`_should_auto_retry` + `_auto_retry_count`). Addon-only change — no re-export needed (worker/launcher stay 0.3.2). |
| 0.3.4   | Two-phase pipeline Increment 1: worker `--phase {bake,render,both}` (default `both` = original single-pass). Render phase forces SKIP-bake; bake phase exits before render. Dormant until export wires two passes (Increment 3). See BACKGROUND_BAKE_DESIGN.md. Worker bumped → re-export needed; behavior unchanged. |
| 0.3.5   | Two-phase pipeline Increment 2: launcher accepts `--phase`, forces `--background` for the bake phase (headless even for EEVEE jobs), and passes `--phase` through to the worker. Still dormant — export passes no `--phase` yet (Increment 3 flips the flow). Launcher bumped → re-export; behavior unchanged. |
| 0.4.0   | **Minor release. Two-phase pipeline FLIPPED ON (Increment 3).** `export_batch` emits a two-pass `run_smoke_batch.bat`: all jobs bake headless (`--phase bake`, `--background`), then all jobs render (`--phase render`, per-engine mode). Per-phase sentinels (`<stem>.bake.done` / `<stem>.render.done` / `<stem>.bake.worker_done` / `<stem>.render.worker_done` / `<stem>.bake.crashed` / `<stem>.render.crashed`) for diagnostics + unphased aliases (`<stem>.done` / `<stem>.crashed`) so the existing addon poll/summary works unchanged. Worker opens log in append mode so both phases share `<stem>.log`. Overall progress bar shows bake-pass progress: factor = (bake_done + render_done) / 2N, text "Bake X/N Render Y/N". Bake-only mode (`render_simulation_result=False`) skips the render pass. Auto-retry continues to use single-pass `_retry`. Worker/launcher/addon all bumped to 0.4.0 → re-export required. |
| 0.4.1   | Removed the "save before running batch" confirmation dialog from Run Batch. Export Batch flips `bpy.data.is_dirty`, so the warning fired on essentially every post-export Run, drowning out the cases where it actually mattered. Addon-only — no re-export needed. |
| 0.4.2   | Two-pass fixes from v0.4.0Test: (a) **render-phase cache wipe** — render phase now presave-protects the cache regardless of `use_existing_cache` (was only protected when reused, so a 2-pass run with the flag off let Mantaflow wipe the bake-phase cache on the render process's `cache_directory` reassignment). (b) **Job Log "both jobs running"** — `_update_job_log_statuses` now uses `_find_running_log` to identify THE one active job (sequential .bat) and marks the rest BAKED / NOT_STARTED / COMPLETE; new `BAKED` status (`◐`, CHECKBOX_HLT icon) for bake-done-waiting-render. (c) **`<stem>.done` alias write hardening** — `_job_bat_block` now sets the sentinel line inside the if/else then redirects ONCE per file at the top scope (the previous "two echo+redirect lines nested in an `if () else ()` block" pattern silently dropped the alias on at least one Windows setup; retry rescued it). Worker+addon → 0.4.2; launcher stays 0.4.0. Re-export required. |
| 0.4.3   | Two-pass status fix (EEVEE test): `_find_running_log` now sorts candidate logs by **mtime descending** (not reversed-alphabetical) with a filename tiebreak. Previously, after the bake pass touched every job's `<stem>.log`, the alphabetical-highest log was picked even while the render pass was working on an earlier one — so the wrong job showed IN_PROGRESS and the subtask text parsed the wrong log's stale stage. Addon-only — no re-export needed. |
| 0.4.4   | TODO-32: final-still copy instead of duplicate render. The worker's "Final still" block now `shutil.copy2`'s `<name>_frames/frame_<frame_end>.png` → `<name>.png` when the animation sequence rendered that frame this run with identical settings (saves a full frame render per job — ~5 s at EEVEE 1440×1080). Falls back to the original render path on copy failure or placeholder-skipped staleness. Worker → 0.4.4; re-export required. |
| 0.4.5   | TODO-33: **Render Animation** checkbox added below *Render Simulation Result* (greyed when rendering off). When unchecked, the worker still produces `<name>.png` but skips the per-frame PNG sequence + MP4 mux. Empties `frames_to_render`; ffmpeg block gated by `if render_animation`; the TODO-32 copy gate naturally falls back to a render for the still. Worker → 0.4.5; re-export required. |
| 0.4.6   | TODO-29: no-camera warning at Export Batch + Run Batch. Helper `_scene_has_camera`. Export Batch combines the new warning with the existing high-resolution warning in one dialog (op caches `_warn_cam` / `_warn_res`). Run Batch's narrow invoke fires only on the genuine-failure case (rendering on + no camera). Cancel aborts; OK proceeds anyway. Addon-only — no re-export needed. |
| 0.4.7   | Increment 4 (Job Log phase awareness): new `RENDERING` status (◉, RENDER_ANIMATION icon) for the active job during the render phase, distinct from `IN_PROGRESS` (▶, baking).  `_update_job_log_statuses` picks between them by checking whether `<stem>.bake.done` exists for the active job (per `_find_running_log`).  IN_PROGRESS enum label updated to "Baking" for clarity.  Addon-only — no re-export needed. |
| 0.4.8   | TODO-34: render-phase **fast-fail** when the bake phase didn't leave a usable cache. Render-phase worker now reads `<stem>.bake.done` (treats missing or "error" as failure) and counts cache data files vs `frame_end − frame_start + 1`; on failure it `shutil.rmtree`s the partial cache and `sys.exit(1)`s BEFORE any GPU/render setup, so auto-retry's single-pass run takes the FULL-bake path instead of RESUME-from-1. Worker → 0.4.8; re-export required. |
| 0.5.0   | **Minor release. BUG-010 root-caused and fixed.** Worker switches from `bpy.ops.fluid.bake_all()` under cache_type='ALL' to `bpy.ops.fluid.bake_data()` (+ `bake_noise()` if `use_noise`) under cache_type='MODULAR'. Probes v6 + v7 (`scripts/experiments/bg_resume_probe_v[67].py`) proved this gives true in-process partial resume: 99 frames preserved (mtime untouched), boundary frame rewritten once, only missing frames newly baked. No save/reload, no `open_mainfile` hang (v0.3.0), no `cache_frame_pause_data` manipulation (v0.2.30). Both data and noise layers resume identically. ALL/MODULAR are both resumable on-disk formats so pre-v0.5.0 caches remain readable. Tests: `tests/test_modular_resume.py` (8 regression tests). Worker → 0.5.0; re-export required. |
| 0.5.1   | v0.5.0 production fix: presave rename now retries 5× with 1-second backoff to survive transient Windows file-handle locks (Synology Drive sync agent, killed-Blender lingering handles, antivirus mid-scan). Without the retry, a single Access-Denied caused `_presave_active=False`, the next `cache_directory` assignment triggered BUG-004 wipe, and the cache was destroyed — defeating the v0.5.0 MODULAR RESUME (v0.5.0Test observed losing 34 baked frames). Also adds a defensive re-walk after the assignment when no presave was active: if the post-assignment frame count drops, baked_frames is downgraded so the bake decision reflects on-disk reality. Tests: 4 new in `TestPresaveRenameRetry` + `TestPostAssignmentRewalk`. Worker → 0.5.1; re-export required. |
| 0.5.2   | Three fixes from v0.5.1Test (2026-05-29): (a) **User-cancel crash detection** — launcher now registers an `atexit` handler that writes `.crashed` if neither `.worker_done` nor `.crashed` exists at exit time. Without this, closing the cmd window mid-bake left the addon unaware of the failure: the existing `_write_crashed_marker()` calls all live inside specific failure branches (wall-clock / stale-log / WerFault / nonzero-exit), so a CTRL_CLOSE_EVENT skipped all of them. Now the addon detects user-cancel within Windows' ~5-second atexit grace window and auto-retry can fire. (b) **Descending-range UI fix** — `expand_param` is now direction-aware. Previously `begin=512, end=32, step=2` returned `[]`, which crashed `_default_job`'s `[0]` index and silently aborted the Setup panel's draw mid-way (entire bottom half of the addon UI disappeared until the user fixed the range). (c) **Diagnostic logging for the render-phase EEVEE hang observed in v0.5.1Test**: `_log()` writes to the per-job file BEFORE stdout (so a stdout block from Blender's main thread can't swallow diagnostics), plus 5 new `_log` calls inside the TODO-34 fast-fail check to pinpoint exactly where it locks. The render hang is **not yet fixed**, only instrumented — root cause requires diagnostic data from a re-run. Tests: 6 new for descending ranges + 3 for atexit handler + 0 for diagnostics (only new log lines). Worker + launcher + addon → 0.5.2; re-export required. |
| 0.5.3   | **Render-phase hang FIXED.** v0.5.2's diagnostics pinned the hang to `_count_data_files` — specifically `os.walk` of the cache directory after the bake-phase rename/restore. The Windows file-system filter chain (Norton + Synology Drive mount + Windows Search indexer) serialised kernel calls during catalog updates, so the recursive `os.walk` (which opens directory handles for `config/`, `data/`, `noise/`, `guiding/`, `mesh/`, `particles/` — six subdirs) blocked indefinitely. Fix: `_count_data_files` now uses `os.scandir` on `data/` and `noise/` only (the only places `.vdb` data files live for a smoke domain), skipping the four irrelevant subdirs. ~4× fewer kernel calls and faster on a healthy filesystem too. Tests: 4 new in `TestCountDataFilesFastScan` (uses scandir, scans only data/+noise/, preserves regex semantics, guards missing subdirs). Worker → 0.5.3; addon → 0.5.3; launcher stays 0.5.2. Re-export required. |
| 0.5.4   | **Render-phase hang FIXED (for real this time).** v0.5.3's `os.scandir` rewrite still hung for 3+ min at 0% CPU (v0.5.3Test, 2026-05-29) — confirming the Windows filter chain (Norton + Synology mount + Search indexer) locks the directory entry at the kernel level, not just `os.walk`. Even `os.scandir` on a single subdir is blocked. Fix: TODO-34 no longer scans the cache directory at all. Instead, a single `os.path.isfile` checks for `data/fluid_data_{frame_end:04d}.vdb` — if the final frame's file exists, the bake reached the end and the cache is usable. ~1000× fewer kernel calls than `os.scandir` on a populated directory, much less surface area for the filter chain to block. The diagnostic-only data count is intentionally absent (replaced by the cheap final-frame check) and `_r34_existing = -1` as a sentinel. Tests: updated `test_run_batch_gating.TestRenderPhaseFastFail.test_checks_cache_frame_completeness` to assert the new single-file approach + asserts `_count_data_files(cache_dir)` is NO LONGER called from TODO-34. Worker + addon → 0.5.4; launcher stays 0.5.2. Re-export required. |
| 0.5.5   | **Progress bar text stage detection fixed** — addon-only patch, no re-export needed. v0.5.0 changed the worker's bake-start log line from `"Baking..."` to `"Baking (MODULAR resume — bake_data)..."` / `"Baking (MODULAR full — bake_data)..."`. The addon's `_STAGES` tuple still searched for the literal substring `"Baking..."`, which never matched the new format — so the stage label never advanced to "Baking simulation". FULL bakes were stuck on "Clearing cache" (the previous match) for the entire bake; SKIP bakes were stuck on "Starting" because the dead `"Use Existing Cache enabled"` keyword had never matched any actual worker log. Fix: change `"Baking..."` → `"Baking ("` and `"Use Existing Cache enabled"` → `"Decision : SKIP BAKE"`. Tests: 4 new in `TestStagesMatchWorkerLog` — (a) **regression guard** asserts every `_STAGES` keyword actually appears in `smoke_worker.py`, so a future log-message rewrite can't silently break stage detection again; (b) explicit shape checks on the two fixed keywords; (c) end-to-end test that feeds three sample log tails through the same `find()` loop the poller uses and verifies the label advances to "Baking simulation" / "Using existing cache" / "Baking simulation" for FULL / SKIP / RESUME respectively. Addon → 0.5.5; worker stays 0.5.4; launcher stays 0.5.2. **No re-export needed.** |
| 0.6.0   | **Minor release. Polish + UX cleanups.** Four user-reported issues: (a) **BUG-012** Failed jobs were counted as "done" in the live progress display (`Bake 11/11 Render 9/11 (9/11 done)` — but 1 of those 9 was actually failed). Poll loop now reads each `.done` file's content and splits into `done_success` vs `done_failed` buckets; display shows `(N done)` when clean, `(N done, F failed)` when failures present. (b) **TODO-37** Gas Parameters UI redrawn in Blender's native order: Density → Heat → Vorticity (was Vorticity → Density → Heat). Purely visual — property names, sweep order, CSV columns, `make_name()` output all unchanged. (c) **TODO-38** Text-object precision: Vort/Dens/Heat were `round(x, 1)` → user-entered values like `0.25` displayed as `0.2`. Now `round(x, 3):g` — preserves up to 3 decimals, trims trailing zeros so `1.0` still displays as `1`. (d) **TODO-39** `make_name()` now appends `-Slow` to the dissolve part when `slow_dissolve=True` (e.g. `D5-Slow`); jobs with `slow_dissolve=False` keep the original `D5` form for backwards-compat with existing on-disk caches. Two jobs differing only in the Slow checkbox now get distinct cache/render directories and filenames. Tests: 13 new in `test_v060_fixes.py`. Addon + worker → 0.6.0; launcher stays 0.5.2. **Re-export required** (worker text precision change). |
