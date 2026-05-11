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

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.11)  
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

**Attempt 4 — Move ALL display data to module-level state (v0.2.11, current)**
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
- *Status:* **DEPLOYED / UNVERIFIED.** Awaiting user confirmation in a real batch.

### Root Cause (confirmed hypothesis)
Any RNA property write on `SmokeSettings` from the poll timer can cause Blender
to re-evaluate the PropertyGroup, returning RNA defaults for `CollectionProperty`
item sub-fields during the concurrent draw pass.  The only complete fix is to
ensure `draw_item` reads nothing from RNA items except the minimum integer key
needed to look up module-level display data.

### Tests Added
None — this is a Blender-internal draw/RNA issue with no testable pure-Python
surface.  Manual verification only: run a batch and confirm all rows remain
visible throughout.

### Open Questions
- Does the fix hold when `s.job_log_index` is written for auto-scroll?  That
  write is still happening from the timer (line ~1793).  If blanking recurs,
  move auto-scroll index to a module-level variable and apply it from a 0-second
  one-shot timer scheduled at the end of each poll.
- Should `draw_item` skip the `item.status` fallback entirely and default to
  `'NOT_STARTED'` when not in `_job_statuses`?  The fallback reads RNA, which
  could return "" and be absent from `_STATUS_ICONS`.

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

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.8); diagnostic logging added v0.2.9  
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

### Root Cause
Partially confirmed for the "0 of 500" case (mtime filter).  "0 of 4" root
cause not yet confirmed — requires `debug_log.txt` analysis.

### Tests Added / Updated
- `test_since_zero_behaves_like_no_since`, `test_since_filters_old_frames`,
  `test_since_future_returns_zero` — replaced with baseline-subtraction tests:
  `test_counts_all_frames_regardless_of_mtime`, `test_baseline_subtraction_gives_new_frames`,
  `test_zero_baseline_means_all_frames_new` (in `tests/test_progress_helpers.py`).

### Open Questions
- Is the frames directory path computed correctly for the active log stem?
  (confirmed matches CMD output in one case, but worth re-checking for retry logs
  where `log_stem` = `job_0001_retry` but JSON is `job_0001.json`)
- Does `_count_png_frames` return `None` for retry-log stems?  If so, the baseline
  is never set and `rendered_new` stays 0.

---

## BUG-004: baked_frames = 0 Despite data_files = 1000

**Status:** `DEPLOYED / UNVERIFIED` (v0.2.9)  
**Files:** `smoke_worker.py` — bake decision block

### Symptoms
Cache search log reports `ACCEPT ... data_files=1000` (confirmed 500 VDB pairs
exist).  Immediately after, bake decision log reports `Data frames found: 0`,
`Cache dir is empty`, and takes `FULL BAKE` path, destroying and rebaking a
complete 500-frame cache.

### Investigation

**Single attempt — d.cache_directory clears VDB files**
- *Hypothesis:* Setting `d.cache_directory = effective_cache_dir` (line 442 at
  the time) triggers Blender/Mantaflow to reinitialize the fluid domain, which
  deletes the existing VDB data files in that directory as part of initialization.
  `_count_data_files` ran BEFORE the assignment (during cache search) and found
  1000 files.  The `baked_frames` walk ran AFTER the assignment (after
  `view_layer.update()` + `sleep(2.0)`) and found an empty directory.
- *Evidence:* Chronological ordering in the log matches exactly — "ACCEPT ... 1000"
  then "Data frames found: 0".  No other code runs between those log lines that
  could explain the discrepancy.
- *Fix (v0.2.9):* Moved the `baked_frames` `os.walk` to BEFORE
  `d.cache_directory = effective_cache_dir`.

### Root Cause
**Confirmed (by code inspection and log analysis):** Mantaflow reinitializes
and clears the cache directory when `d.cache_directory` is assigned.  Frame count
must be captured before domain property assignment.

### Tests Added
None — requires Blender runtime to test.  Manual verification: re-run a job with
`use_existing_cache = True` on a complete cache and confirm "SKIP BAKE" is logged.

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

*Document created 2026-05-11.  Append new attempts to existing issues rather than
creating duplicate entries.*
