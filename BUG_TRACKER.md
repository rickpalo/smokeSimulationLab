# BatchSimLab — Bug Investigation Tracker

> _Tracker covers both pre-v0.6.3 "SmokeSimLab" history and post-rebrand "BatchSimLab" entries.  All BUG-* IDs remain stable across the rename._

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

**Status:** `CONFIRMED FIXED` (v0.2.19 — stable icons + Unicode prefix + alert; user-verified 2026-06-20 across many production batches through v0.9.0)  
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

**Status:** `PARTIALLY CONFIRMED` — stale-log watchdog auto-recovery VERIFIED in
production (2026-06-21); dialog *suppression* still gapped  
**TODOS:** TODO-20  
**Files:** `smoke_launcher.py`, `smoke_worker.py`

### Production verification (2026-06-21, screen recording)
Recording `tmpDataTransfer/2026-06-21 12-44-12.mp4` @ ~24:53 shows the recovery
path working end-to-end on a real crashed job:
- A Blender crash dialog ("Restart / View Crash Log / Close") appeared for the
  stuck job `job_0018` (so dialog suppression did NOT prevent this crash dialog —
  one of the still-open failure modes below).
- The launcher console logged: `[launcher/job_0018] stale watchdog: idle=1800s
  threshold=1800s` then `[smoke_launcher] No log activity for 1800s — killing
  stuck job job_0018`, and wrote `…\SmokeSimulatorForPiazzaSanMarco.crash.txt`.
- ~7 s later the crash dialog was gone and the **next job began initializing its
  cache** — the batch auto-recovered with no user action (cursor was nowhere near
  the dialog).
**Conclusion:** the stale-log watchdog (the "Hang (no exit)" recovery) is
CONFIRMED working. **Remaining gaps:** (1) crash-DIALOG suppression still fails for
some crash types (Job-Object/`SEM_NOGPFAULTERRORBOX` mode); (2) the 1800 s (30 min)
idle threshold is a long stall before recovery — consider a shorter threshold or
detecting the crash dialog / `.crash.txt` directly to recover faster.

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

**Status:** `CONFIRMED FIXED` (v0.2.19 — mtime-based counting; user-verified 2026-06-20 across many production batches through v0.9.0)  
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

**Status:** `CONFIRMED FIXED` (v0.2.15; user-verified 2026-06-20 across many production batches through v0.9.0)  
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

**Status:** `CONFIRMED FIXED` (v0.5.0 + v0.5.1 + v0.5.4; production-verified
in v0.5.4Test 2026-05-29 with a 500-frame res-512 EEVEE job that crashed
twice mid-render and auto-recovered both times)  
**Files:** `smoke_worker.py` — parameter-application block + RESUME/FULL bake branches

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

**Attempt 4 — save/reload hangs the bake; reverted (v0.3.1)**
- *Symptom (v0.3.0, res-512 production job):* RESUME merged 168/500 frames,
  saved + reloaded the temp `.blend`, logged `Baking...`, then **hung
  indefinitely**. The worker never returned from `bpy.ops.fluid.bake_all()`:
  Blender sat at ~0% CPU, 28/128 GB RAM, the windowed (EEVEE) UI showed **no
  bake in progress**, and no cache file was written for 30+ min. A clean FULL
  bake of the same 500 frames at res 512 finishes in ~90 min, so it was not
  slow or memory-bound — it was deadlocked.
- *Root cause:* `bpy.ops.wm.open_mainfile()` mid-script leaves the bake
  operator unable to run in windowed mode — the script blocks on `bake_all()`
  while the operator waits on an event loop that never pumps. The reload also
  never achieved a true resume (`cache_frame_pause_data` was 0 afterward), so
  it was pure downside.
- *Fix (v0.3.1):* Removed the entire save/reload block from the RESUME branch.
  RESUME now reverts to the v0.2.31 behavior: merge presaved VDBs, then
  `bake_all()` in the same process (re-bakes from frame 1, overwrites in place,
  all frames end present). No `open_mainfile`, no temp `.blend`.
- *Status:* **DEPLOYED / UNVERIFIED.** RESUME still re-bakes from frame 1 (the
  underlying Mantaflow limitation is unchanged) — but it no longer hangs.
- *CONFIRMED re-bake-from-1 (2026-05-27, ResumeTesting/):* A 4-job chain at
  res 16 sharing one cache (frames 1-20 → 1-50 → 1-75 → 1-200) ended with the
  correct file counts (so it "appeared to resume"), but the VDB **mtimes prove
  no resume**: after the final 1-200 job, every frame 1..200 carried that job's
  timestamps (14:44:42→:51, monotonic), not the earlier jobs' (14:43:50 etc.).
  Each RESUME job re-baked the whole range from frame 1. The save/reload
  (v0.3.0) never changed this — it gave the same re-bake plus a windowed hang.
  True partial resume is still unsolved; the promising untried path is to bake
  in `--background` mode, where `open_mainfile`+`bake_all` are synchronous and
  Mantaflow's native cache rescan (the UI "Resume Bake" path) should work.

**Attempt 5 — cache_type='MODULAR' + bake_data() (v0.5.0) ✓ ROOT CAUSE FOUND**
- *Investigation method:* Five probe scripts in `scripts/experiments/` run
  Blender headless with `--factory-startup` to isolate the resume mechanism
  from addon code.  Probes v3 → v7 progressively eliminated wrong hypotheses
  and converged on the true cause.
- *Root cause:* The worker (and all prior probes) used
  `bpy.ops.fluid.bake_all()` under the .blend's default `cache_type='ALL'`.
  Under `cache_type='ALL'`, `bake_all()` ignores existing on-disk cache when
  invoked from scripted Python and re-bakes from frame 1.  Mantaflow's UI
  "Resume Bake" button calls a different path that includes a cache rescan.
- *Discovery path (probes):*
  - **Probe v3:** save+reload with `cache_frame_pause_data` written to the
    .blend → REBAKED-FROM-1.  Finding: `cache_frame_pause_data` does NOT
    persist through `save_as_mainfile`; loads as 0 in a fresh process.
  - **Probe v5:** switched to `cache_type='MODULAR'` + `bake_data()` with
    `cache_frame_start = last_preserved` → wrote 101 frames (boundary +
    100 new) but pruned frames 1..99 from disk.  *Mantaflow honored
    `cache_frame_start` as the new cache lower bound and deleted outside it.*
    The user spotted this from the cache directory contents — the verdict's
    "WIPED" was actually a successful partial resume with wrong range setup.
  - **Probe v6:** same-process MODULAR + `bake_data()` with
    `cache_frame_start = 1` unchanged → **VERDICT RESUMED**.  99 frames
    preserved (mtimes untouched), frame 100 rewritten as boundary, 100
    new frames in 5.5 s.  Mtime gap of 3.2 s at frame 100 — exactly the
    ResumtTest2 UI Resume pattern.  No save, no reload, no `open_mainfile`.
  - **Probe v7:** v6 plus `use_noise=True` + `bake_noise()` → both layers
    resumed identically (99/1/0 in both data/ and noise/).
- *Fix (v0.5.0):*
  - **Parameter block** (`smoke_worker.py`): force `d.cache_type = 'MODULAR'`
    (wrapped in try/except) right after `cache_data_format`/`cache_noise_format`.
  - **RESUME branch:** call `bpy.ops.fluid.bake_data()` instead of
    `bake_all()`; if `use_noise`, also call `bpy.ops.fluid.bake_noise()`.
    The presave/merge dance is retained (still needed for BUG-004
    protection against `cache_directory` reassignment wipe), but the v0.3.1
    commentary about Mantaflow always re-baking from frame 1 is gone — under
    MODULAR that's no longer true.
  - **FULL branch:** same operator swap for consistency.  Every cache the
    worker writes is now MODULAR so the next run's RESUME path can resume it.
    Both ALL and MODULAR are resumable on-disk formats (REPLAY is not), so
    pre-v0.5.0 caches baked under ALL remain readable.
  - No save/reload, no `open_mainfile`, no `cache_frame_pause_data`
    manipulation — purely a `cache_type` + operator-name swap.
- *Why the prior attempts failed:* Attempts 1–4 all used `cache_type='ALL'`
  + `bake_all()`.  In that combination Mantaflow has no resume path
  reachable from scripted Python.  Attempt 3 came closest (UI-Resume
  reproduction via save+reload), but it still fell back to `bake_all()`
  which doesn't engage on-disk scan logic the way `bake_data()` does.
- *Status:* **CONFIRMED FIXED** (v0.5.4Test, 2026-05-29).  User ran a
  res-512 EEVEE 500-frame job to completion on production hardware.  The
  bake phase SKIP-baked from an existing cache, the render phase crashed
  TWICE mid-render, auto-retry fired each time, and the job finished
  successfully without manual intervention.  The combination of
  (v0.5.0) MODULAR cache_type + bake_data, (v0.5.1) presave-rename retry,
  (v0.5.2) user-cancel atexit + descending-range UI, and (v0.5.4)
  single-file final-frame check now constitutes a working end-to-end
  resume pipeline at production scale.  Probe v6/v7 (scripts/experiments/,
  not committed) verified the underlying mechanism at res 16; the
  production run validated it at res 512 with realistic GPU/filesystem
  stress.

### Tests Added
- `tests/test_run_batch_gating.py::TestWorkerResumeNoReload` — asserts the
  worker RESUME path no longer calls `open_mainfile` / `save_as_mainfile`
  (regression guard for the v0.3.0 hang).
- `tests/test_modular_resume.py` (v0.5.0, 8 tests) — asserts:
  - `d.cache_type = 'MODULAR'` is set in a try/except block
  - `bpy.ops.fluid.bake_all(` no longer appears in non-comment code
  - `bpy.ops.fluid.bake_data()` is called in both RESUME and FULL branches
  - `bpy.ops.fluid.bake_noise()` is called in both branches, conditionally
    guarded by `p["use_noise"]`
  - Each operator call has a `'FINISHED'` check + `sys.exit(1)` on failure
  - `WORKER_VERSION` is ≥ `0.5.0`

---

## BUG-011: Bake Ignores Job Frame Range (Bakes Whole .blend Range)

**Status:** `DEPLOYED / UNVERIFIED` (v0.3.0)
**Files:** `smoke_worker.py` — parameter-application block

### Symptoms
A job configured for frames 1–20 baked **500** frames. From a v0.3.0 test run
(`TestPlan001/SessionA/jobs/job_0000.log`): the worker logged
`Frame range needed : 1–20 (20 frames)` at the bake decision, then
`Cache data files found: 500` after baking. The bake progress bar showed
"XX of 20" while the count climbed to 500. Every job over-baked by ~25×,
wasting bake time and cache space.

### Root Cause
`bpy.ops.fluid.bake_all()` bakes the **domain's** `cache_frame_start` /
`cache_frame_end`, *not* the scene frame range. The worker set
`scene.frame_start/end` (so the cache *decision* and the *render* used 1–20) but
never set the domain's cache frame range, so Mantaflow baked whatever was saved
in the .blend (500).

### Fix (v0.3.0)
In the parameter-application block, set
`d.cache_frame_start = frame_start` and `d.cache_frame_end = frame_end`
(wrapped in try/except for older API), before any bake decision or bake. Applies
to all three bake paths (FULL / RESUME / SKIP). Also added a startup
`Blender <version>` log line.

### Tests Added
`tests/test_run_batch_gating.py::TestWorkerBakeFrameRange` — source-level
assertions that the worker sets cache_frame_start/end from the job range
(worker can't be imported — it's a Blender script).

### Verification
Re-run a short batch (e.g. frames 1–20) and confirm `Cache data files found`
equals the requested frame count, and the bake bar reads "N of 20".

---

## BUG-014: Bake/Render phased counts include failed jobs (Bake 13/13 with 1 crashed bake)

**Status:** `DEPLOYED / UNVERIFIED` (v0.6.1)
**Files:** `__init__.py` — `_poll_batch_progress_impl` phased-count block

### Symptoms (filed 2026-06-01)
User observed during a 13-job batch with 1 bake crash: the live progress
display showed `Bake 13/13  Render 0/13  (0/13 done)` even though one of
those 13 bakes had crashed (job 5 with the ⚠ icon in the Job Log).  The
crash didn't reduce the bake count.

### Root Cause
Same family as BUG-012, but for the phased counters.  v0.6.0 fixed the
unphased `(N done)` count to read `.done` content and exclude `"error"`
files.  The phased counters at the same call site were left as:

```python
_bake_done_n   = sum(1 for _f in _all_files if _BAKE_DONE_RE.match(_f))
_render_done_n = sum(1 for _f in _all_files if _RENDER_DONE_RE.match(_f))
```

The `.bat` writes `.bake.done` for BOTH success (`done <stem>`) and
failure (`error exit N <stem>`), so naive matching counts crashed bakes
as completed.  The unphased fix (BUG-012) caught one display string; this
one caught the other.

### Fix (v0.6.1)
Extracted a `_count_phase_success(_re)` helper that reads each matching
file's content and excludes `"error"`.  Both `_bake_done_n` and
`_render_done_n` use it.

### Tests Added
`tests/test_todo44_sections.py::TestBug014PhasedCountsExcludeFailed`
(4 tests): helper exists, helper reads content, both counters use the
helper, naive regex-only counting is gone.

### Verification
Re-run a batch with an intentional bake failure and confirm the Bake N/M
counter drops by 1 when the crash happens (instead of staying at 13/13).

---

## BUG-013: slow=False jobs inherit slow=True caches (v0.6.0 TODO-39 backwards-compat collision)

**Status:** `CONFIRMED FIXED` (v0.6.2 — make_name uses -Slow/-Fast explicitly; user-verified 2026-06-20 across many production batches through v0.9.0)
**Files:** `__init__.py` — `make_name()` (~line 660)
**Filed:** 2026-06-01 from user observation in 13-job batch
**Resolved:** 2026-06-02 — option (B) implemented per the recommended approach

### Symptoms
User ran a job with addon setting `slow_dissolve` UNCHECKED.  Worker cfg
log confirms `'slow_dissolve': False`.  Filename produced is correctly
`R128_V0.0_A0.0_B0.0_D100_N-OFF` (no `-Slow` suffix, per TODO-39
backwards-compat).  But the rendered output shows the smoke physics
behavior of a slow=True bake, and the user observed "the text wasn't
changed" suggesting the cache was reused from an earlier slow=True run.

### Root Cause (v0.6.0 regression from TODO-39)
TODO-39 added the `-Slow` filename suffix ONLY when `slow_dissolve=True`,
to preserve backwards-compat with existing on-disk caches from pre-v0.6.0.
The asymmetry creates a collision:

| Era              | slow=True name | slow=False name |
|------------------|----------------|-----------------|
| v0.5.x and prior | `D100`         | `D100`          |
| v0.6.0+          | `D100-Slow`    | `D100`          |

A v0.6.0 slow=False job with `use_existing_cache=True` finds and
SKIP-bakes a v0.5.x cache that was actually produced with slow=True
— the underlying bake data is wrong, but the addon doesn't know.

### Fix options
**(A) Always add an explicit indicator (recommended).**  Change
`make_name()` so use_dissolve=True jobs always include a Slow/Fast
indicator.  E.g. `D100-Slow` / `D100-Fast`.  The plain `D100` form is
retired.  All pre-v0.6.0 caches become orphaned (no longer match any
new job name) — user must wipe and re-bake.  Names also fully
disambiguate slow vs fast going forward; no more collision.

**(B) Add `-Fast` for slow=False only.**  Like (A) but slow=True jobs
keep the `-Slow` suffix from v0.6.0 (already shipping).  Same outcome
as (A); cosmetically the v0.6.0 slow=True caches survive the
transition while pre-v0.6.0 slow=True caches become orphaned.

**(C) Status quo + user education.**  Document that mixing v0.5.x
caches with v0.6.0 slow=False jobs is unsafe; rely on user to wipe
Cache/ before mixing.  Worst option — sets up future bug reports.

### Recommended approach
Option (B).  Worker change is one line in make_name; new test asserts
slow=False produces `-Fast`.  v0.6.0 slow=True caches (with `-Slow`
names) survive intact; the user just needs to acknowledge orphaning
old pre-v0.6.0 caches (or re-bake them).

### Reproduction recipe
1. Run a job in v0.5.5 with `slow_dissolve=True, dissolve_speed=100`,
   `use_dissolve=True`.  Confirm cache at `Cache/R..._D100_..._N-OFF/`.
2. Update to v0.6.0.  Same params except `slow_dissolve=False`.
   `use_existing_cache=True`.  Export + Run Batch.
3. Worker logs "SKIP BAKE — all N frames confirmed" reusing the v0.5.x
   cache.  Render uses slow=True physics with slow=False text overlay.

### Workaround until fix
Before running v0.6.0 batches with slow=False, manually delete or rename
any `Cache/...D<N>_...` directories from pre-v0.6.0 runs that were
actually slow=True.  Or use `use_existing_cache=False` to force re-bake.

---

## BUG-012: Failed Jobs Counted as "Done" in Live Progress Display

**Status:** `CONFIRMED FIXED` (v0.6.0; user-verified 2026-06-20 across many production batches through v0.9.0)
**Files:** `__init__.py` — `_poll_batch_progress_impl` done-count block

### Symptoms
Live progress display read e.g. `Bake 11/11 Render 9/11 (9/11 done)` while
1 of the 9 was actually FAILED (the launcher had exited non-zero, the `.bat`
wrote `error exit N <stem>` content into `<stem>.done`, the addon's Job Log
row showed the ⚠ icon, but the parenthetical "9/11 done" counted that
failed job as completed).  User observation (2026-05-29 screenshot):
"When a job fails, it should not be included in the completed jobs."

### Root Cause
`done_files = [f for f in os.listdir(jobs_dir) if _DONE_RE.match(f) or
_RETRY_DONE_RE.match(f)]` matches every `.done` file regardless of content.
The `.bat` writes `.done` files for BOTH success (`done <stem> <date>`)
and failure (`error exit <code> <stem> <date>`) paths — both are "the
launcher exited", but only one means the job actually succeeded.  The
batch-completion summary code at the same location already discriminated
by reading `.done` content for `"error"`; the live display just hadn't
picked up that distinction.

### Fix (v0.6.0)
Split the done count in `_poll_batch_progress_impl` into two buckets:
- `done_success` = `.done` files with no `"error"` in content
- `done_failed`  = `.done` files containing `"error"`

Display string becomes:
- `(N done)` when `done_failed == 0` (clean run — no visual noise)
- `(N done, F failed)` when `done_failed > 0`

Both the bake-only branch and the two-pass branch use the same `_done_str`
helper.  The batch-completion trigger (`if done >= total:`) still uses the
total count so the batch-summary post-processing fires correctly when
every job has reached a terminal state, regardless of success/failure.

Reading 10–30 small `.done` files on every 5s poll is ~10ms — negligible.

### Tests Added
`tests/test_v060_fixes.TestBug012DoneCountExcludesFailed` — 4 tests:
- `done_files` is split into both buckets
- the split reads each file's content for `"error"`
- the user-visible string references `_done_str` (not raw `len(done_files)`)
- the `, 0 failed` suffix is never displayed (only shown when > 0)

### Verification
Run a batch with intentional failures (e.g. a job with a bogus emitter name)
and confirm: while jobs are running the display reads `(N done, F failed)`
with F = number of failed jobs; clean batches show only `(N done)`.

---

## BUG-015: N-Panel Body Blank After Remote Extension Install (bl_info NameError)

**Status:** `CONFIRMED FIXED` (v0.9.1; user-verified 2026-06-20 on remote extension install)
**Files:** `__init__.py` — `SMOKE_PT_panel.draw`

### Symptoms
After installing v0.9.0 from the remote repository (rickpalo.github.io), the
addon enabled normally and printed `BatchSimLab 0.9.0 loaded`, but the N-panel
(Sidebar → BatchLab tab) showed only the panel title bar with an empty body.
User console (2026-06-20):
`line 5510, in draw / version = ".".join(str(v) for v in bl_info["version"]) /
NameError: name 'bl_info' is not defined.`
Never reproduced under legacy (<4.2) add-on installs — only as an *extension*.

The user initially suspected a conflict from running a second addon
(AssetDoctor) hosted on the same GitHub Pages domain in a different directory.
Ruled out: Blender keys extensions by unique `id` (`batchsimlab` vs
`assetdoctor`), and multiple remote repos on one domain are fully supported.

### Root Cause
Blender deletes `bl_info` from an extension's module namespace **after import**
(4.2+ extensions are driven by `blender_manifest.toml`, not `bl_info`).  The
module-level `ADDON_VERSION = ".".join(...bl_info["version"])` at import time
succeeds (bl_info still present then), but `SMOKE_PT_panel.draw` re-derived the
version from `bl_info` on every repaint — long after Blender removed it.  The
NameError raised inside `draw()`, so Blender rendered the panel header from
`bl_label` and aborted the body → title-only panel.

### Fix (v0.9.1)
`draw()` now uses the import-time `ADDON_VERSION` module constant (which
survives, since Blender only removes `bl_info`) instead of re-reading
`bl_info["version"]`.  Single line change; behaviour identical under legacy
add-on installs.

### Tests Added
`tests/test_version_stamps.TestPanelDrawUsesAddonVersion` — 2 tests:
- `SMOKE_PT_panel.draw` source contains no executable `bl_info` reference
  (comment lines stripped before the check)
- `SMOKE_PT_panel.draw` uses `ADDON_VERSION`

### Verification
Install the v0.9.1 extension from the remote repo, open Sidebar → BatchLab,
and confirm the full panel body renders with `BatchSimLab v0.9.1` at the top.

---

## BUG-016: EEVEE Renders Ignore Per-Job render_samples (used .blend value)

**Status:** `DEPLOYED / UNVERIFIED` (v0.9.2 — fixed in worker 0.9.1)
**Files:** `smoke_worker.py` — `setup_eevee()` + its two render-path call sites

### Symptoms
Found by inspection (2026-06-20) while answering "do samples get saved with the
job export?" `render_samples` is frozen per-job at export (`__init__.py` job_data
`"render_samples": s.render_samples`) and read by the worker, but EEVEE renders
used whatever sample count was saved in the **.blend**, not the exported value.
Worst part: the per-job value was still written to `perf_log.json`/`results.csv`
as if applied — a silent data lie.

### Root Cause
`setup_eevee(scene)` only switched the render engine; it never set
`scene.eevee.taa_render_samples`.  `setup_cycles(scene, samples=...)` correctly
applied `scene.cycles.samples`, so Cycles respected the per-job value but EEVEE
did not.  The two EEVEE render-path call sites passed no sample count.

### Impact
Directly invalidated the TODO-51 EEVEE samples calibration: every "different
samples" EEVEE job would render identically while the log claimed they differed,
so the model would be fit on fabricated variance.  Fixed *before* running that
sweep.

### Fix (worker 0.9.1 / addon v0.9.2)
`setup_eevee(scene, samples=64)` now sets `scene.eevee.taa_render_samples =
samples` in both the EEVEE Next and legacy EEVEE branches (the property both
engines use for final-frame samples).  Both render-path call sites now pass
`samples=render_samples`, mirroring `setup_cycles`.

### Tests Added
`tests/test_bug016_eevee_samples.TestBug016EeveeSamples` — 3 source-inspection
tests (setup_eevee needs a live bpy/EEVEE context to run):
- `setup_eevee` accepts a `samples` parameter
- `taa_render_samples = samples` set in both EEVEE branches
- no bare `setup_eevee(scene)` call remains; render path forwards `render_samples`

### Verification
Run two EEVEE jobs with different samples (e.g. 2 and 64) over the same scene and
confirm render times/quality differ and `perf_log.json` render times track the
sample count (this is also the TODO-51 samples sweep).

---

## BUG-017: Phantom "Helper file version mismatch" for smoke_launcher.py on every Run Batch

**Status:** `DEPLOYED / UNVERIFIED` (fixed v0.9.4, commit pending)
**Files:** `__init__.py` — `_read_helper_version`; `SMOKE_OT_run_batch` version check (~L4600)

### Symptoms
Immediately after Export Batch, Blender's Info window showed a WARNING:
`Helper file version mismatch — re-run Export Batch to update: smoke_launcher.py
(found '', expected '0.6.4')` (reported by user on v0.9.0). The worker was never
flagged — only the launcher — and re-running Export did not clear it.

### Root cause
`_read_helper_version(path, var_name)` scans only the **first 30 lines**
(`if i >= 30: break`). `smoke_launcher.py`'s module docstring runs to line 31, so
`LAUNCHER_VERSION = "0.6.4"` lands on **line 33** — past the cap — and the reader
returns `""`. `smoke_worker.py` declares `WORKER_VERSION` on line 18 (within the
cap), so the worker parsed fine. The mismatch was therefore a **false positive
that fired on every batch** regardless of the actual exported version, because the
exported launcher is byte-identical to the source (also line 33). `found ''`
(empty, not a stale version) was the tell: a parse miss, not a real mismatch.

### Fix (v0.9.4)
Raise the scan cap 30 → 200 lines in `_read_helper_version` (helper files are
small; a longer docstring can't reintroduce the miss).

### Tests
`test_version_stamps.py::TestReadHelperVersion` — reads the real launcher/worker
versions (== `_EXPECTED_*`, not `''`); synthetic file with the VAR past line 30 is
found; a usage-only (f-string) line isn't mis-parsed as the assignment.

### Verification
Export then Run Batch in Blender 4.5/5.1 and confirm no "Helper file version
mismatch" warning appears when the installed addon matches the exported files.

---

## BUG-018: Editing a bounded sweep List value crashes Blender (stack overflow, no log)

**Status:** `FIXED` (v0.9.6) — reproduced + fixed + verified in real Blender 5.1
**Files:** `properties.py` — `ValueItem._clamp_value`

### Symptoms
User set a **List** value for **Buoyancy Heat** to `0.1` — Blender closed instantly,
every time (3/3).  No crash log, no `.crash.txt`, no Python traceback.  Entering
`0.1` directly in the Buoyancy Heat *Range* (`_begin`) field worked fine.

### Root cause
`ValueItem.value` is a `FloatProperty(update=_clamp_value)`, and `_clamp_value`
assigned `self.value` **unconditionally**:
```python
if lo < hi:
    self.value = max(lo, min(hi, self.value))   # writes value from value's own update cb
```
Writing a property inside its own update callback re-fires the callback.  With the
old code the rewrite happened even when the value was already in range (the clamp
returns the same number), so it recursed forever → **C stack overflow**, which
takes down the process hard with no Python traceback and no crash log.

It only bit params whose bounds give `lo < hi` — `_PARAM_BOUNDS` Buoyancy Heat
(`beta` = -5..5), Buoyancy Density (`alpha`), `cfl_number`, `timesteps_*`, and the
fire params.  Params with effective `(0,0)` bounds (vorticity, dissolve_speed, …)
never enter the assignment branch, so List mode there never crashed — which is why
it went unnoticed.  **Adding** a value didn't crash because `SMOKE_OT_add_value`
sets `value` *before* `min_bound`/`max_bound` (still 0/0 at that point); the crash
only fired on the later edit, once the bounds were -5/5.

Reproduced headlessly (Blender 5.1, `--background`): adding a `beta_list` item with
`min_bound=-5, max_bound=5` then `item.value = 0.1` → `EXCEPTION_STACK_OVERFLOW`,
exit 127, no `AFTER_SET`.

### Fix (v0.9.6)
Compute the clamp, then write **only when it actually changes**:
```python
if lo < hi:        clamped = max(lo, min(hi, self.value))
elif lo > 0 and self.value < lo:   clamped = lo
else:              return
if clamped != self.value:
    self.value = clamped
```
An in-range edit is now a no-op (no re-fire); an out-of-range edit clamps exactly
once (the corrected value is in range, so its single re-fire is itself a no-op).
Same headless repro after the fix: `0.1`→0.1, `9.0`→5.0, `-9.0`→-5.0, exit 0.

### Tests
`test_properties_module.py` — a write-counting `_ClampTracker` stub asserts an
in-range value produces **zero** writes (the recursion guard), out-of-range exactly
one, `(0,0)` bounds never write, and min-only bounds clamp below / no-op at-or-above.
(Addon-only — worker/launcher unchanged, no re-export.)

---

## BUG-019: Export Batch crashes with NameError (`_dedupe_jobs` not defined) — 0.9.5 regression

**Status:** `FIXED` (v0.9.6) — regression from the TODO-58 package split (0.9.5)
**Files:** `operators.py` — imports

### Symptoms
`line 507 in operators.py  NameError: '_dedupe_jobs' is not defined` when running
Export Batch.  Export Batch was **completely broken in 0.9.5** for every user.

### Root cause
The TODO-58 split (v0.9.5) moved `export_batch` from `__init__.py` into
`operators.py` (module #6).  In `__init__` those helpers were resolved via the
package-level re-imports; in the new module they must be imported explicitly.
`operators.py` imported most of what `export_batch` uses but missed **two**:
`_dedupe_jobs` (jobgen) and `_blend_domain_resolution` (emitters).

Why the gates missed it: the pytest suite stubs `bpy` and the real-Blender REGISTER
smoke-test only *imports* the package — neither calls `export_batch`'s body, so the
NameError only surfaced at Export time in production.  I ran an AST unbound-name
analysis on engine.py (module #6b) which caught its analogous misses, but did NOT
run it on operators.py (#6).

### Fix (v0.9.6)
Add the two missing imports to `operators.py`:
`from .jobgen import …, _dedupe_jobs` and
`from .emitters import _populate_emitters, _blend_domain_resolution`.
Verified end-to-end headlessly (Blender 5.1): a real fluid-domain export of a
2-job resolution sweep wrote both job JSONs + `run_smoke_batch.bat` + copied
`smoke_worker.py`, exit 0.

### Tests (prevents the whole class going forward)
New `tests/test_no_unbound_names.py` runs the AST unbound-name analysis on every
addon-package module and fails on any name used-but-bound-nowhere — i.e. exactly
this missing-import class, which the bpy-stubbed suite and the REGISTER smoke-test
cannot catch.  Would have flagged both `export_batch` misses (and the engine.py
ones).  Addon-only — worker/launcher unchanged, no re-export.

---

*Document created 2026-05-11.  Append new attempts to existing issues rather than
creating duplicate entries.*
