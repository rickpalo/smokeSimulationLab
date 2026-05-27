# SmokeSimLab — Bug Investigation Tracker

Each entry documents the full lifecycle of an issue: observed symptoms →
investigation attempts → confirmed root cause → fix applied → tests added →
verification status.  Entries are **never deleted** — if a fix regresses, a
new attempt is appended under the same issue.

**Status values:**
- `OPEN` — not yet fixed
- `IN PROGRESS` — actively being investigated or coded
- `DEPLOYED / UNVERIFIED` — code committed; no real-run confirmation yet
- `CONFIRMED FIXED` — user-verified in production batch
- `REGRESSED` — previously fixed, broken again; add new attempt

---

## BUG-001: Job Log Rows Go Blank

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.19 — stable icons + Unicode prefix + alert)  
**TODOS:** TODO-5, TODO-17, TODO-21  
**Files:** `__init__.py` — `SMOKE_UL_job_log.draw_item`, `_update_job_log_statuses`

### Symptoms
Rows in the Job Log UIList that are IN_PROGRESS or COMPLETE show no job
number, no job name, and no status dot — they appear completely blank.
Blank rows correlate with moments when a job transitions state (starts
running, completes).

### Investigation

**Attempt 1 — Scroll index (TODO-5 theory 1)**
- *Hypothesis:* `job_log_index` advances off-screen, causing item 0 to flicker.
- *Code examined:* `template_list` active-index interaction.
- *Conclusion:* Not ruled out but did not match the "completed jobs go blank" observation.
- *Action:* None.

**Attempt 2 — Status write timer-draw race (TODO-17 theory 2)**
- *Hypothesis:* `_update_job_log_statuses` writes `item.status = 'COMPLETE'` from
  the poll timer while Blender is mid-draw.  Writing any field on a
  `CollectionProperty` item may partially invalidate it, causing `job_number` and
  `job_name` to read back as RNA defaults (0 / "") during the same draw pass.
- *Code examined:* `_update_job_log_statuses` lines 1778–1790; `draw_item`.
- *Conclusion:* Strong circumstantial match — blanking tracks exactly with status
  transitions.
- *Action (v0.2.8):* Added `draw_item` guard:
  ```python
  if not item.job_name and item.job_number == 0:
      layout.label(text="…")
      return
  ```
- *Result:* **INADEQUATE.** Symptom guard, not root-cause fix.  User still
  reported blank rows.  Guard logic also wrong — items could be blank with
  `job_number != 0` if only `job_name` was zeroed.

**Attempt 3 — Move status to external dict (v0.2.9)**
- *Hypothesis:* Stop writing `item.status` from the timer entirely.  Store
  statuses in `_job_statuses: dict[int, str]` keyed by `job_number`.
  `draw_item` reads status from dict; timer never touches `item.*`.
- *Code examined:* All 5 places `item.status = ...` was written.
- *Action (v0.2.9):*
  - Added `_job_statuses = {}` module-level dict.
  - `_update_job_log_statuses` writes to `_job_statuses[item.job_number]`.
  - `draw_item` reads `_job_statuses.get(item.job_number, item.status)`.
  - `draw_item` guard updated: `if not item.job_name: return`.
  - `_job_statuses.clear()` added to all 3 reset paths.
- *Result:* **INADEQUATE.** User still reported blank rows in v0.2.9/v0.2.10.
- *Revised analysis:* We stopped writing `item.status` but `draw_item` still
  read `item.job_name` and `item.job_number` from RNA.  Any write to `SmokeSettings`
  from the timer (e.g. `s.batch_progress`, `s.job_log_index`, `s.job_log_auto_scroll`)
  may trigger Blender to re-evaluate the entire PropertyGroup, momentarily returning
  RNA defaults for ALL CollectionProperty item fields — not just `status`.

**Attempt 4 — Move ALL display data to module-level state (v0.2.11)**
- *Hypothesis:* The invariant must be: `draw_item` reads **nothing** that the
  timer writes.  The only safe RNA read is `item.job_number` (a simple integer
  used as a key; if 0, skip the row).  All text data must come from module-level
  Python state that the timer never modifies.
- *Code examined:* Every write to `s.*` in the timer; every RNA read in `draw_item`.
- *Action (v0.2.11):*
  - Added `_job_log_rows: list[(int, str)]` populated once in `export_batch`.
  - `draw_item` reads `item.job_number` (1-based index), then:
    - `idx = job_number - 1`; if `idx >= len(_job_log_rows)`, skip.
    - `job_name = _job_log_rows[idx][1]` — never from RNA.
    - `status = _job_statuses.get(job_number, item.status)`.
  - `_job_log_rows.clear()` added to all 3 reset paths.
- *Result:* **INADEQUATE.** User confirmed blank rows persisted in v0.2.11.
- *Revised analysis:* `draw_item` still read `item.job_number` from RNA to use
  as the index into `_job_log_rows`.  When the timer wrote any `SmokeSettings`
  property (e.g. `s.batch_progress`, `s.job_log_index`), Blender re-evaluated
  the entire PropertyGroup, zeroing `item.job_number` transiently during the
  draw pass.  `job_number == 0` triggered the early-return guard, blanking the row.

**Attempt 5 — as_pointer() for row identification (v0.2.14)**
- *Hypothesis:* Reading `item.job_number` from RNA is the final remaining
  unsafe read in `draw_item`.  The only RNA value completely immune to
  re-evaluation zeroing is the raw C pointer returned by `item.as_pointer()`.
  Use the pointer to find the item's index into `_job_log_rows`; never read
  any RNA property value for display logic.
- *Code examined:* `draw_item` in `SMOKE_UL_job_log`.
- *Action (v0.2.14):*
  - `draw_item` captures `item_ptr = item.as_pointer()` before any RNA reads.
  - Linear search `_job_log_rows` by pointer: `next(i for i, it in enumerate(data.job_log_items) if it.as_pointer() == item_ptr, -1)`.
  - `job_number, job_name = _job_log_rows[idx]` — neither is read from RNA.
  - `status = _job_statuses.get(job_number, 'NOT_STARTED')` — no RNA fallback.
- *Result:* **INADEQUATE.** User confirmed blank rows still occurred.
- *Revised analysis:* The pointer comparison `item.as_pointer() == it.as_pointer()`
  never matches.  Blender's UIList implementation either passes a copy of the
  item's C data to `draw_item` (not the original collection node), or the
  Python wrappers for `data.job_log_items` produce different C addresses than
  the `item` passed by the UIList draw loop.  The linear scan always returns
  `idx = -1` → early return → blank row.
  Secondary issue: `_update_job_log_statuses` still read `item.job_number` from
  RNA to key `_job_statuses`, so status entries were written under key 0 when
  RNA zeroing occurred.

**Attempt 6 — UIList `index` parameter (v0.2.16)**
- *Root cause (final):* Blender's UIList passes an `index` parameter to
  `draw_item` — the item's 0-based position in the displayed list.  Since we
  apply no filtering, this equals the collection index.  Every previous attempt
  tried to RE-DERIVE this index from RNA or C pointers, all of which are
  unreliable.  Blender already computed the correct index and offers it for free.
- *Code examined:* Blender Python API `UIList.draw_item` signature.
- *Action (v0.2.16):*
  - `draw_item` signature extended to `(..., index=0)`.
  - `index` used directly: `_job_log_rows[index]` — zero RNA reads, zero
    pointer comparisons, zero search loops.
  - `_update_job_log_statuses` converted to `for idx in range(len(s.job_log_items))`
    with `job_number = _job_log_rows[idx][0]` — eliminates the last remaining
    `item.job_number` RNA read in the hot path.
- *Status:* **DEPLOYED / UNVERIFIED.** Awaiting user confirmation.

**Attempt 7 — Blender 5.1.1 icon availability (v0.2.17 + v0.2.19)**
- *New symptom in Blender 5.1.1:* `SEQUENCE_COLOR_XX` icons do not exist in this
  version.  Using them causes `draw_item` to raise silently, leaving rows blank again.
- *Fix (v0.2.17):*
  - `draw_item` signature extended to `(..., index=0, _flt_flag=0)` — Blender 5.x
    passes `flt_flag` as a 9th positional argument; without `_flt_flag`, the call
    raised `TypeError` on every row.
- *Fix (v0.2.19):*
  - `_STATUS_ICONS` dict replaced `SEQUENCE_COLOR_XX` with stable alternatives:
    `PLAY`, `FILE_REFRESH`, `CHECKMARK`, `CANCEL`, `ERROR`, `RADIOBUT_OFF`.
  - Added `_STATUS_PREFIX` dict with Unicode status characters (`▶ `, `↻ `, `✓ `,
    `✗ `, `⚠ `) prepended to the job name — visible even if the icon fails.
  - `layout.alert = True` applied for FAILED/CRASHED rows (red background tint as
    additional indicator independent of icon rendering).
  - `item.status` RNA fallback removed from `_job_statuses.get()` call (defaults to
    `'NOT_STARTED'` — no RNA read).
- *Status:* **DEPLOYED / UNVERIFIED.** Run a batch in Blender 5.1.1 and confirm rows
  show status icons/prefix throughout.

### Root Cause (confirmed hypothesis)
Any RNA property write on `SmokeSettings` from the poll timer can cause Blender
to re-evaluate the PropertyGroup, returning RNA defaults for `CollectionProperty`
item sub-fields during the concurrent draw pass.  The correct fix is to use the
`index` parameter that Blender's UIList passes to `draw_item` — it is computed
by Blender's internal draw loop and does not touch RNA property values.  In
Blender 5.x, additionally `SEQUENCE_COLOR_XX` icons are unavailable and must be
replaced with stable alternatives.

### Tests Added
None — this is a Blender-internal draw/RNA issue with no testable pure-Python
surface.  Manual verification only: run a batch and confirm all rows remain
visible throughout.

### Open Questions
- Does the fix hold when `s.job_log_index` is written for auto-scroll?  That
  write is still happening from the timer.  If blanking recurs, move auto-scroll
  index to a module-level variable and apply it from a 0-second one-shot timer.

---

## BUG-002: Crashes Not Detected / Logged

**Status:** `DEPLOYED / UNVERIFIED` (partial fix v0.2.12); hang timeout not yet implemented  
**TODOS:** TODO-20  
**Files:** `smoke_launcher.py`, `smoke_worker.py`

### Symptoms
Blender crashes during a batch run.  No crash log is written, the launcher
does not flag the job as failed, the job log UI shows no FAILED indicator,
and the batch silently continues (or hangs).

### Investigation

**Attempt 1 — WerFault suppression (v0.2.6)**
- *Problem:* WerFault crash dialogs blocked the launcher from detecting the exit.
- *Action (v0.2.6):*
  - Windows Job Object with `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION`.
  - `_find_werfault_for_pid` polls `WerFault.exe` + `WerFaultSecure.exe`.
  - Poll interval reduced 2.0 s → 0.5 s; post-exit WerFault window 30 s.
  - `_save_crash_log` now called unconditionally on non-zero exit.
- *Result:* **PARTIALLY EFFECTIVE** — dialogs suppressed; non-zero exit crashes caught
  (confirmed exit_code=11 logged in debug_log.txt, 2026-05-11).  Exit-code-0 crashes
  still silently marked COMPLETE.

**Attempt 2 — Worker-done sentinel (v0.2.12)**
- *Root cause for exit-code-0 misses:* Launcher only checks `returncode != 0`.
  If Blender catches a Python exception internally and still exits 0 (or if an
  unhandled exception happens after all `sys.exit(1)` guards), the job appears
  successful even though nothing was produced.
- *Fix (v0.2.12):*
  - **Worker** writes `job_NNNN.worker_done` (ISO timestamp) right before
    `bpy.ops.wm.quit_blender()`.  The sentinel is ONLY written on the happy
    path — any `sys.exit(1)` branch or unhandled exception will bypass it.
  - **Launcher** checks for `job_stem + ".worker_done"` after exit code 0.
    If absent → writes `.crashed`, logs reason (including Python traceback
    detection in `blender_stderr.txt` for this job's section), exits 1.
  - **Launcher** records `_stderr_start_pos` before Popen so only this job's
    stderr output is scanned (appended file shared across all jobs).
  - **Retry cleanup** in `__init__.py` deletes `.worker_done` alongside `.done`
    so retries produce fresh sentinels.
  - **Export cleanup** (`export_batch`) also deletes `.worker_done` files.
  - `LAUNCHER_VERSION` bumped to `"0.2.12"`.
  - `WORKER_VERSION` bumped to `"0.2.12"`.
- *Status:* **DEPLOYED / UNVERIFIED.** Covers exit-code-0 silent failures.

### Remaining Failure Modes (open)

| Mode | Trigger | Why current code still misses it |
|------|---------|----------------------------------|
| **Hang (no exit)** | Deadlock, Mantaflow infinite loop, GPU driver stall | Stale-log watchdog fires at 1800 s; but only after log was created |
| **Job Object creation fails** | `ctypes.WinError`, insufficient privilege | Falls back to `SEM_NOGPFAULTERRORBOX` — may not suppress all dialogs |
| **Blender exits and restarts** | Possible with some crash types | Launcher sees a new PID; original crash goes undetected |
| **Pre-log hang** | Blender starts but worker never writes first log line | `stale_since` never set because log file never appears |

### Tests Added
- `TestWorkerDoneSentinel` in `tests/test_smoke_launcher.py` (v0.2.12):
  - `test_missing_sentinel_would_be_treated_as_crash` — documents exit-code-0 behavior
  - `test_sentinel_present_means_clean_exit` — verifies no false positives
  - `test_sentinel_has_iso_timestamp` — verifies sentinel content format
  - `test_retry_cleanup_removes_worker_done` — verifies cleanup on retry

**Attempt 3 — Startup timeout, wall-clock timeout, CRASHED UI status (v0.2.13)**
- *Gaps addressed:*
  - Pre-log hang: Blender starts but worker never writes the first log line.
  - Absolute hang: Blender and worker are alive but job runs forever.
  - UI blindspot: CRASHED jobs shown same red as FAILED; no distinct indicator.
- *Fix (v0.2.13):*
  - Launcher `_STARTUP_TIMEOUT = 120 s`: if log file never appears within 120 s,
    kill Blender, write `.crashed`, exit 1.
  - Launcher `_WALL_CLOCK_TIMEOUT = 14400 s (4 h)`: absolute per-job ceiling
    regardless of log activity; checked every poll.
  - Addon `SMOKE_UL_job_log._STATUS_ICONS`: added `'CRASHED': 'SEQUENCE_COLOR_02'`
    (orange — distinct from FAILED red `SEQUENCE_COLOR_01`).
  - Addon `SmokeJobLogItem.status` enum: added `'CRASHED'` value.
  - Addon `_update_job_log_statuses`: sets CRASHED when `.crashed` is present
    alongside an error `.done`; also when `.crashed` exists without any `.done`
    (rare: launcher itself crashed before batch wrote `.done`).  A successful
    retry supersedes the crash and shows COMPLETE.
  - Addon `_compute_batch_summary`: counts CRASHED separately from FAILED in the
    4-line summary; "X Jobs Crashed" appears on line3 with priority over the
    "retried successfully" message.
  - 12 new tests across `TestWatchdogConstants` and `TestCrashedStatusDetection`.
- *Status:* **DEPLOYED / UNVERIFIED.**

### Remaining Failure Modes (open)

| Mode | Trigger | Why current code still misses it |
|------|---------|----------------------------------|
| **Job Object creation fails** | `ctypes.WinError`, insufficient privilege | Falls back to `SEM_NOGPFAULTERRORBOX` — may not suppress all dialogs |
| **Blender exits and restarts** | Possible with some crash types | Launcher sees a new PID; original crash goes undetected |

---

## BUG-003: Render Progress Bar Stuck at 0

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.19 — mtime-based counting)  
**TODOS:** TODO-19  
**Files:** `__init__.py` — `_poll_batch_progress_impl`, `_count_png_frames`

### Symptoms
Subtask bar shows "Rendering (0 of N)" while frames are actively being saved
to disk (visible in CMD window).  Bar resets correctly when job completes.
Observed as "0 of 500" in one run and "0 of 4" (placeholder run) in another.

### Investigation

**Attempt 1 — mtime filter too aggressive (v0.2.8)**
- *Root cause:* `_count_png_frames` filtered by `since=batch_job_start_time`.
  If `batch_job_start_time` was set after frames already existed (Blender restart,
  cached bake → immediate render, sync lag), all rendered frames were excluded.
- *Fix (v0.2.8):*
  - Removed `since` mtime filter entirely.
  - Added `batch_render_frame_baseline` property: set once when "Rendering animation"
    is first detected; progress = `(current_count − baseline)`.
  - Same baseline approach for bake bar via `batch_bake_frame_baseline`.
- *Result:* **PARTIALLY EFFECTIVE** — "0 of 500" resolved, but "0 of 4" observed in
  subsequent run (placeholder scenario).

**"0 of 4" investigation (open)**
- *Observed:* screenshot shows `frame_0017` through `frame_0045` actively rendering
  in CMD, but panel shows "Rendering (0 of 4)".
- *render_target = 4* is correct for a placeholder run (only 4 frames missing).
- *rendered_new = 0* throughout implies either:
  (a) `_count_png_frames` returns the same value as the baseline throughout, OR
  (b) `batch_render_frame_baseline` was re-set after frames were already rendered.
- *Diagnostic added (v0.2.9):* `_debug_log` writes baseline value, raw count,
  and `rendered_new` on every render-progress poll when "Collect Debug Log" is on.
- *Status:* **Awaiting debug_log.txt from next run with debug logging enabled.**

**Attempt 2 — start_time mtime filter (v0.2.19)**
- *Root cause for "0 of 4":* When re-rendering existing PNGs (overwrite, not add),
  the total PNG count in the frames directory never changes — so baseline subtraction
  always yields 0.  The "0 of 500" fix (Attempt 1) did not cover this scenario.
- *Fix (v0.2.19):*
  - `_count_png_frames(jobs_dir, log_stem, start_time=None)` — added optional
    `start_time` parameter.  When provided, only PNGs with `mtime >= start_time - 10.0`
    are counted (10 s buffer for filesystem clock skew).
  - Render-poll call passes `start_time=s.batch_job_start_time` when `> 0`.
  - When `start_time` is active, the count IS the "new frames" count directly — no
    baseline subtraction needed; baseline subtraction removed from this path.
  - When `start_time` is None (baseline-setting call), all PNGs are counted regardless
    of mtime — existing behavior preserved.
- *Status:* **DEPLOYED / UNVERIFIED.**

### Root Cause
Confirmed for the "0 of 500" case: mtime filter was too aggressive (excluded recent
frames).  Confirmed root cause for "0 of 4" case: PNG overwrite leaves total count
unchanged; baseline subtraction gives 0 forever.  Both cases now handled by the
`start_time` mtime filter in v0.2.19.

### Tests Added / Updated (v0.2.19)
- `test_start_time_filters_old_files` — files with mtime < start_time-10 not counted
- `test_start_time_counts_new_files_only` — only recently-written files counted
- `test_start_time_overwrite_scenario` — regression for "0 of 500" overwrite case
- `test_counts_all_frames_when_no_start_time` — without start_time, all files counted
  (baseline-setting behavior unchanged)
(All in `tests/test_progress_helpers.py`.)

---

## BUG-004: baked_frames = 0 Despite data_files = 1000

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.15)  
**Files:** `smoke_worker.py` — bake decision block, post-bake verification block

### Symptoms
Cache search log reports `ACCEPT ... data_files=1000` (confirmed 500 VDB pairs
exist).  Immediately after, bake decision log reports `Data frames found: 0`,
`Cache dir is empty`, and takes `FULL BAKE` path, destroying and rebaking a
complete 500-frame cache.

Second recurrence (2026-05-11): SKIP BAKE decision correctly logged, but
post-bake verification found 0 files and `sys.exit(1)` was called.  The cache
was present at decision time and gone by the render phase.

### Investigation

**Attempt 1 — d.cache_directory clears VDB files (v0.2.9)**
- *Hypothesis:* Setting `d.cache_directory = effective_cache_dir` (line 442 at
  the time) triggers Blender/Mantaflow to reinitialize the fluid domain, which
  deletes the existing VDB data files in that directory as part of initialization.
  `_count_data_files` ran BEFORE the assignment (during cache search) and found
  1000 files.  The `baked_frames` walk ran AFTER the assignment (after
  `view_layer.update()` + `sleep(2.0)`) and found an empty directory.
- *Evidence:* Chronological ordering in the log matches exactly — "ACCEPT ... 1000"
  then "Data frames found: 0".  No other code runs between those log lines.
- *Fix (v0.2.9):* Moved the `baked_frames` `os.walk` to BEFORE
  `d.cache_directory = effective_cache_dir`.
- *Result:* **REGRESSED.** Fix corrected the bake DECISION (now correctly says
  "SKIP BAKE"), but a second `d.cache_directory =` assignment still existed later
  in the SKIP BAKE path (the unconditional assignment at the old line 424), which
  again triggered Mantaflow reinitialization.  Post-bake verification found 0 files.

**Attempt 2 — Path-equality guard, Change 1 (v0.2.15)**
- *Root cause (recurring):* `d.cache_directory = effective_cache_dir` ran
  unconditionally even when the path was already set to the correct value.
  Mantaflow reinitializes the domain (deleting VDB files) on every assignment,
  even to the same path.
- *Evidence:* job_0000.log (2026-05-11): `data_files=1000` → SKIP BAKE →
  `Cache files found: 0` → ERROR.  Assignment at ~line 424 is the only code
  between those log lines.
- *Fix (v0.2.15, Change 1):* Added path-equality guard before the assignment:
  ```python
  _norm_cur = os.path.normcase(os.path.normpath(d.cache_directory))
  _norm_eff = os.path.normcase(os.path.normpath(effective_cache_dir))
  if _norm_cur != _norm_eff:
      d.cache_directory = effective_cache_dir
  ```
  Assignment skipped when paths are already equal → Mantaflow does not reinitialize
  → VDB files preserved.

**Attempt 2 — Fallback bake, Change 2 (v0.2.15)**
- *Risk:* If paths were genuinely different (domain pointed at a wrong directory),
  Change 1 allows the assignment, which will again delete VDB files.
  The SKIP BAKE decision was made against the correct cache dir but the domain
  now points at empty space.
- *Fix (v0.2.15, Change 2):* Replaced post-bake `sys.exit(1)` with recovery logic:
  - Count post-bake files using `_count_data_files(effective_cache_dir)`.
  - If count == 0 AND `bake_skipped` is True: log warning, set `rebaked_frames`
    to all frames, clear `bake_skipped`, call `free_all()` + `bake_all()`,
    re-verify.  If still 0 after fallback bake → `sys.exit(1)`.
  - If count == 0 AND `bake_skipped` is False: `sys.exit(1)` (regular bake ran
    but produced no output — unrecoverable without manual intervention).

### Root Cause
**Confirmed:** Mantaflow reinitializes and clears the cache directory on every
`d.cache_directory = ...` assignment, even if the value is identical to the
current one.  All `baked_frames` capture and SKIP BAKE decisions must complete
before any domain property assignment.

### Tests Added
`TestBug004PathEqualityGuard` (5 tests, v0.2.15):
- Equal paths → guard fires (no assignment).
- Different paths → assignment allowed.
- Trailing slash normalized.
- Case-insensitive on Windows.
- Double-slash normalized.

`TestBug004FallbackBakeLogic` (5 tests, v0.2.15):
- SKIP BAKE + 0 files → fallback triggered.
- Full bake + 0 files → sys.exit.
- Non-zero files → no intervention.
- `_count_data_files` excludes `config/` dir.
- `_count_data_files` returns 0 for empty dir.

---

## BUG-005: Same Parameters Produce Different Cache/Render Directories

**Status:** `CONFIRMED FIXED` (v0.2.10)  
**Files:** `__init__.py` — `make_name`; `smoke_worker.py` — `name_prefix`, `_find_match`

### Symptoms
Running the same parameter combination in two different batch exports produced
different cache directories (e.g. `R128_V0.1_..._0002` vs `R128_V0.1_..._0007`).
`use_existing_cache` could not find the previous run's baked data because the
directory name differed.

### Investigation

**Single attempt**
- *Root cause:* `make_name(p, index)` appended `_{index:04d}` to the job name.
  The index is a position in the current batch, not a property of the simulation.
  Two runs of the same params at different positions got different names.
- *Workaround that existed:* `name_prefix = name.rsplit('_', 1)[0]` stripped the
  suffix; cache search used `re.compile(r'^' + re.escape(name_prefix) + r'_\d{4}$')`
  to scan for any matching directory.  `_find_match` did the same for render frames.
- *Problem with workaround:* Cache search still picked the WRONG directory in v0.2.9
  (BUG-004 root cause was partly that `d.cache_directory` was set to a different dir
  than expected).

### Fix (v0.2.10)
- `make_name(p)` — removed `index` parameter and `_{index:04d}` suffix entirely.
- `name` is now purely parameter-derived.  Same params → same `Cache/<name>/` →
  same `Renders/<name>_frames/` → same `Renders/<name>.mp4` in every batch.
- `name_prefix`, `_find_match`, and candidate-scan loop removed from worker.
- Cache search simplified to single `_count_data_files(cache_dir)` check.

### Tests Added
None for `make_name` specifically.  Verified manually via Python REPL that the
new function produces the expected string without index suffix.

### Open Questions
- Old `_NNNN`-suffixed directories are orphaned.  User should manually delete
  `Cache/` and `Renders/` dirs with `_NNNN` suffixes when convenient.
- Two jobs in the same batch with identical parameters are now no-ops after the
  first (bake skipped, renders skipped via placeholders).  This is correct behavior
  but may be surprising.

---

## BUG-006: Cache Found via use_existing_cache Still Rebakes (config/ False Positive)

**Status:** `CONFIRMED FIXED` (v0.2.7, then extended in v0.2.9)  
**Files:** `smoke_worker.py` — `has_files` check, `baked_frames` walk

### Symptoms
With `use_existing_cache = True`, jobs rebaked from scratch even when a complete
500-frame VDB cache existed.

### Investigation

**Attempt 1 — has_files regex matched config checkpoints (v0.2.3)**
- *Root cause:* `has_files` check used `f.endswith('.vdb') or f.endswith('.uni')`,
  which matched Mantaflow's `config/config_0001.uni` etc.  A directory with only
  config files passed `has_files = True`, was selected as effective cache, then
  produced 0 `baked_frames` (config files don't match `_\d+\.(vdb|uni)$`).
- *Fix:* Changed `has_files` to use frame-number regex.

**Attempt 2 — config/ walk still counted in baked_frames (v0.2.7)**
- *Root cause:* The `baked_frames` walk and `_count_data_files` did not skip
  Mantaflow's `config/` subdirectory, which holds per-frame checkpoint `.uni` files
  that match `_\d+\.(vdb|uni)$` but contain no simulation output.
- *Fix (v0.2.7):* Skip `config/` subdirectory in all walks.
- *Also:* Full cache-search section rewritten with detailed per-step logging.

### Tests Added
None — requires Blender runtime to test.

---

## BUG-007: Identical Jobs Produced Duplicate Results (Overlapping Sweep Baselines)

**Status:** `CONFIRMED FIXED` (v0.2.21)  
**Files:** `__init__.py` — `export_batch`, job deduplication logic

### Symptoms
Sweep batches with overlapping parameter ranges produced duplicate job entries
in results.csv — two rows with identical parameters where only one was expected.

### Fix (v0.2.21)
Deduplication pass in `export_batch` filters jobs whose parameter dict is a
duplicate of an already-queued entry.  Confirmed fixed; no regression tests
added (pure Python dict comparison, no Blender runtime needed).

---

## BUG-008: Negative Bake / Setup Times in Time Estimate

**Status:** `CONFIRMED FIXED` (v0.2.22)  
**Files:** `__init__.py` — time-estimate code in `_poll_batch_progress_impl`

### Symptoms
Time estimate showed negative values (e.g. "−2 min remaining") for bake or
setup phases, particularly at the start of a job before enough elapsed time
had accumulated.

### Fix (v0.2.22)
All elapsed-time and remaining-time values clamped to `max(..., 0)`.  Rate
constants also recalibrated against production batch data to reduce early
overestimates.

---

## BUG-009: Retry Progress Bar Showed Wrong Job's Frame Count

**Status:** `CONFIRMED FIXED` (v0.2.26)  
**Files:** `__init__.py` — `_find_running_log`

### Symptoms
During retry, progress bar read "Baking 0 of 331" — the frame count from a
*previously crashed* job, not the retry job.  Bar never advanced from 0.

### Investigation
`_find_running_log` used a single alphabetical sort across all log files.
`"job_0003_retry.log"` sorts before `"job_0010.log"` alphabetically, so
the sequential-done check (`any(s > log_stem for s in done_stems)`) saw
`job_0010.done` and skipped `job_0003_retry.log`, returning `job_0010.log`
(the wrong crashed job) as the "running" log.

### Fix (v0.2.26)
Two-pass search: retry logs (ending `_retry.log`) are checked first against
only retry done-stems (`_retry.done`); first-run logs are checked second and
skip any with a matching `.crashed` file.  Retry logs always take priority
over earlier completed/crashed first-run jobs.

---

## BUG-010: RESUME Bake Always Starts from Frame 1

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.31 — re-bake-from-1 accepted as correct)  
**Files:** `smoke_worker.py` — RESUME bake decision branch

### Symptoms
When retrying a job that crashed mid-bake (e.g. 395 of 500 frames done), the
worker correctly identified the 395 existing frames and chose RESUME, but
Mantaflow started baking from `fluid_data_0001.vdb` anyway — ignoring the
395 existing files and re-baking all 500 frames from scratch.  The mtime-
filtered progress bar (v0.2.29) then counted all frames as new, showing
"Baking (500 of 500)" instead of "Baking (105 of 105)".

### Investigation

**Attempt 1 — cache_frame_pause_data (v0.2.30)**
- *Hypothesis:* Assigning `d.cache_directory` resets Mantaflow's internal
  "last baked frame" counter (`cache_frame_pause_data`) to 0.  Setting it to
  `max(baked_frames)` before `bake_all()` should cause Mantaflow to start
  from the next missing frame.
- *Fix (v0.2.30):* Set `d.cache_frame_pause_data = max(baked_frames)` and
  `d.cache_frame_pause_noise = max(baked_frames)` after the presave merge.
- *Result:* **REGRESSED.** Mantaflow started baking from frame 396 (correct
  start) BUT cleared the entire cache directory before beginning — deleting
  the merged presave files (1–395).  Only frames 396–500 were present after
  the bake.  The render phase had no VDB data for frames 1–395.  Confirmed
  in production: "I looked in the cache directory. It had started at frame
  396, but the 1-395 files were not there."

**Attempt 2 — accept full re-bake, ensure all frames present (v0.2.31)**
- *Root cause (revised):* `cache_frame_pause_data` controls the *start
  frame* but Mantaflow also clears the cache directory before baking.  There
  is no known Python API to make Mantaflow resume without clearing.
- *Fix (v0.2.31):* Revert the `cache_frame_pause_data` change.  Leave the
  counter at 0 (default after `d.cache_directory` reassignment).  Mantaflow
  re-bakes from frame 1, overwrites the presave-merged files 1–395 in-place
  (giving them new mtimes), then adds frames 396–500.  All 500 frames are
  present when the bake finishes.
- *Tradeoff:* RESUME re-bakes all 500 frames rather than only the 105 missing
  ones — defeats the partial-cache optimization.

**Attempt 3 — save and reload .blend to trigger UI-equivalent resume (v0.2.32)**
- *Hypothesis:* In the Blender UI, clicking "Resume Bake" on a .blend opened
  with existing cache files works correctly — Mantaflow scans the cache dir
  during init, detects the existing frames, and continues from the next
  missing frame.  Scripted bake doesn't get this behavior because
  `d.cache_directory` is assigned *during* the script run, which clears
  Mantaflow's internal tracking.  Saving the .blend AFTER the presave merge
  (files in place) and reloading it should put Mantaflow in the same state
  as the UI workflow — files on disk, fresh init, ready to resume.
- *Fix (v0.2.32):* After the presave merge in the RESUME branch:
  1. `bpy.ops.wm.save_as_mainfile(filepath=_resume_blend_path, copy=True)`
  2. `bpy.ops.wm.open_mainfile(filepath=_resume_blend_path)`
  3. Re-establish `obj` and `d` references (Python locals survive reload;
     bpy data references do not).
  4. Log diagnostics: pre-save file count, post-reload file count,
     `d.cache_directory`, `d.cache_resumable`, `d.cache_frame_pause_data`.
  5. Call `bake_all()` and log post-bake file count + bake time.
  6. Delete the temp `.blend`.
- *Failure mode handling:* If save/reload throws, fall back to the v0.2.31
  re-bake-from-1 path (correct but slow).  If the bake doesn't finish, exit 1.
- *Verification metrics to watch in the worker log:*
  - `post-reload: d.cache_frame_pause_data` — if non-zero, Mantaflow detected
    the existing frames.
  - `post-bake: cache dir has N data files` vs expected 500 — confirms no
    cache wipe occurred.
  - `Bake complete in Ns` — if << full bake time, only missing frames were
    baked (success).  If ≈ full bake time, Mantaflow re-baked everything
    (still correct via overwrite, but the reload optimization didn't help).
- *Status:* **DEPLOYED / UNVERIFIED.**  First production run is also the
  diagnostic — log output reveals which path Mantaflow took.

### Tests Added
None — requires Blender runtime.

---

*Document created 2026-05-11.  Append new attempts to existing issues rather than
creating duplicate entries.*
