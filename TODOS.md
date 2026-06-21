# BatchSimLab — Active Issues

> _Project renamed from "SmokeSimLab" to "BatchSimLab" at v0.6.3 (surface-only);
> all TODO-* IDs remain stable across the rename._

**This file lists only OPEN / IN-PROGRESS / PARTIAL work.**

- Long-horizon major-version plan → [ROADMAP.md](ROADMAP.md)
- Finished / rejected / cancelled tasks → [TODOS_COMPLETED.md](TODOS_COMPLETED.md)
- Per-version changelog → `RELEASING.md`; bugs → `BUG_TRACKER.md`

Repo is source of truth — verify line refs against current code before acting.

---

## Active TODO index

| ID | Title | Status | Target |
|----|-------|--------|--------|
| TODO-51 | Better time estimates — samples + noise up-res terms | IN PROGRESS (step 1 done) | v0.8.0 |
| TODO-46 | "All jobs" ETA doesn't model two-pass bake-then-render | DONE (v0.9.4) — calibration deferred to TODO-51 | v0.8.0 |
| TODO-36 | Monitor Existing Jobs — progress count wildly off mid-bake | OPEN | v0.8.0 |
| TODO-31 | RESUME progress bar should start at "(already-baked + 1) of total" | OPEN (needs decision) | v0.8.0 |
| TODO-27 | Restore crash dumps (relax Job Object kill window) | PARTIAL | — |
| TODO-24 | Per-frame bake timing not collected | OPEN | — |
| TODO-23 | Retry overall batch time estimate is unreliable | OPEN | — |
| TODO-22 | Crash timing inconsistency (5-min stall vs immediate) | INSTRUMENTED (root cause open) | — |
| TODO-61 | Finish the BatchSimLab rename — remaining `SmokeSimLab`/`smoke_*` names | OPEN | — |

Also pending (not a TODO): **tag `v0.7.6` on GitHub** (built + feed live, not yet tagged).

---

## TODO-61: Finish the BatchSimLab rename — remaining `SmokeSimLab` / `smoke_*` names — **OPEN**

The GitHub repo + source folder/package were renamed `SmokeSimLab → BatchSimLab`
in **v0.9.3** (repo renamed on GitHub, `scripts/SmokeSimLab/ → scripts/BatchSimLab/`,
all imports/paths/URLs updated, feed moved to
`https://rickpalo.github.io/BatchSimLab/index.json`). This TODO tracks the
**remaining** legacy names deliberately left behind, to be cleared "in a lull."

**Tier A — .blend / keymap-breaking (needs a migration plan, NOT a blind rename):**
- `scene.smoke_settings` PointerProperty → renaming orphans settings stored in
  existing `.blend` saves.
- `SMOKE_*` class prefix + `smoke.*` operator `bl_idname`s → renaming breaks user
  keymaps and any scripted calls.
- `.smokesettings` preset file extension → renaming orphans saved presets.
- Approach when picked up: add the new names alongside, with a one-time
  load_post migration (copy old PropertyGroup data → new) + back-compat aliases,
  then deprecate. Don't do this without that shim.

**Tier B — safe Python/identifier renames (no .blend impact):**
- `SmokeSimLabPreferences` class ([__init__.py](scripts/BatchSimLab/__init__.py)) → `BatchSimLabPreferences`.
- `smoke_worker.py` / `smoke_launcher.py` filenames (referenced in worker-copy
  logic + export + tests + `_EXPECTED_*_VERSION`) → `batch_worker.py` etc.

**Tier C — docs / cosmetics (no functional risk):**
- `tools/analyze_estim.py` + `tools/analyze_perf.py` print/docstring brand text.
- `scripts/preflight_inspector.py` titles + ACTION strings (+ its addon-key
  detection still looks for a legacy module key).
- Stale `documentation/SmokeSimLab_Documentation.html` (v0.1.38) +
  `documentation/images/SmokeSimLab_Panel.png` → fold into the TODO-56 docs
  overhaul; rename assets then.
- Legacy `SmokeSimLab.zip` artifact name in README/RELEASING install instructions
  (the extension build already produces `batchsimlab-<ver>.zip`).

---

## TODO-51: Better time-estimate formulas — add samples + noise up-res terms — **IN PROGRESS** (step 1 done, v0.7.3)

**Step 1 (data collection) DONE in v0.7.3:** the worker `_perf` record now logs
`render_samples`, `use_noise`, and `noise_upres` (worker → 0.7.1).  **Next:** run
a calibration batch that *varies samples and noise_upres at a fixed resolution*
(see calibration note below), then do steps 2-3 (analysis + model).

**Two observations from the 2026-06-15 batch:**
1. **Render time rises with noise up-res even at the same output resolution** —
   higher `noise_upres` makes a denser volumetric grid, so EEVEE raymarches more
   steps per pixel.
2. **Render samples affect render time** and aren't in the model at all.

**Current model** (`__init__.py`, constants block ~line 160-180):
- Bake:   `bake_secs ≈ _BAKE_RATE_PER_RES3_FRAME × resolution³ × frames`
  (ignores the separate **noise bake pass** cost — see
  [[project_noise_bake_ceiling]]).
- Render: `render_secs ≈ rate × width × height × frames` (EEVEE rate = median of
  167 samples, cv=27%). **Independent of samples AND noise up-res** — exactly the
  scatter the user is seeing.

**Plan when picked up:**
1. **Worker (data):** _done_ — fields added in v0.7.3.  Still need the calibration
   batch (below).
2. **Analysis:** extend `tools/analyze_estim.py` to fit:
   - Render (EEVEE): `render_secs ≈ (a + b·samples) × pixels × frames ×
     f(noise_upres)` (samples ~linear + overhead; volume term growing with
     up-res — try linear then power; cost may track the up-res voxel count
     res×upres rather than output pixels).
   - Bake: add a noise-bake term so total ≈ data-bake(res³) +
     noise-bake(f(res, noise_upres)).
   - Re-check Cycles once real Cycles data exists (still a placeholder).
3. **Addon (model):** replace the flat `_RENDER_RATE_EEVEE_PER_PIXEL_FRAME` /
   `_BAKE_RATE_PER_RES3_FRAME` usage with multi-term formulas; keep the old
   constants as fallbacks when samples/upres are unknown. New estimator functions
   each get tests; pin a few (samples, upres) → expected-seconds regression rows.

**Acceptance:** estimate error (predicted vs actual in `estim_log.jsonl`) drops
materially for high-sample and high-upres jobs; EEVEE-rate cv falls once samples
/ noise are modelled instead of averaged over.

**Calibration-batch design (own checklist item — the existing corpus is almost
all EEVEE @ 16 samples, so there's no variance to fit until this runs):**
- Fix resolution (e.g. 128) and frame count low (e.g. 60) to keep it cheap.
- Sweep `render_samples` (e.g. 4, 16, 32, 64, 128) with noise off → isolates the
  samples term.
- Sweep `noise_upres` (1, 2, 3) at one fixed sample count → isolates the noise
  term in both render and bake.
- One cross cell (high samples × high upres) to check the terms compose.
- Re-run per machine (rates are hardware-specific); tag perf records so
  analyze_estim can fit per-machine.

---

## TODO-46: Time estimate doesn't account for two-pass bake-then-render — **DONE (v0.9.4)**

**DONE 2026-06-21 (v0.9.4):** Confirmed root cause from a 38-min screen recording +
the `estim_log.jsonl` (1,134 jobs): the old ETA used `jobs_not_started = total -
done - 1` where `done` counts only the **unphased** `.done`, which stays 0 through
the entire bake phase (bake pass emits `.bake.done`) — so the "All jobs" estimate
sat frozen at `total × (bake + render)` until rendering began (matched the user's
"stuck at ~5h55m"). Fix: extracted pure helper `_estimate_batch_remaining(...)`
(next to `_format_eta`) driven by the per-phase `_bake_done_n` / `_render_done_n`
counts + the current job's `.bake.done` marker to pick bake-vs-render phase; the
poller's "All jobs:" block now calls it. Counts down in BOTH phases; cached/SKIP
bakes are NOT pre-discounted (corrupt-cache safe) — a job drops out only when its
`.bake.done` actually lands. 11 regression tests in `test_phase_aware_eta.py`.
**Deferred to TODO-51 (the planned Cycles+EEVEE calibration sweep):** rolling
per-batch averages and recalibrating `_BAKE_RATE_*` / `_RENDER_RATE_*` (current
data showed bake ~2.4× high, Cycles render ~212× LOW on 2 samples, EEVEE ~ok).
Bar 2 "this job" estimate left as-is per user (bake+render shown during bake is OK).

**Filed 2026-06-01.** User observation from a 13-job batch: the addon showed
`Job stage 3 of 4 (~15 min remaining this job)` and `All jobs: ~25 min remaining`
— but with 8 jobs left to render at ~15 min each, 8 × 15 = 120 min, not 25.

**Additional data point (2026-06-02, v0.6.3):** a 76-job batch (most SKIP-bake)
showed an initial "All jobs" estimate of 17 h 50 min that **stayed at exactly
17 h 50 min through the first 24 jobs** despite many completing in seconds via
SKIP BAKE.  So the estimate is **constant**, not just wrong-direction:
- The "All jobs" remaining estimate is a static per-job-time × jobs-remaining
  product where per-job-time comes from a default rate constant (not from elapsed
  time of completed jobs in THIS batch).
- Rolling-average from completed jobs is either not collected or not feeding the
  display.
- For SKIP-bake jobs the per-job estimate is wildly wrong (defaults assume a full
  bake + render).

**Fix sketch:**
- Determine current phase by checking whether `_bake_done_n == total`.
- All bakes done → remaining = sum of render times for not-yet-rendered jobs.
- Else → remaining = sum of (bake + render) for not-yet-baked + sum of render for
  baked-but-not-rendered.
- Use rolling averages from completed jobs in the current batch when available;
  fall back to `_BAKE_RATE_PER_RES3_FRAME` / `_RENDER_RATE_*` defaults otherwise.

**Files:** `__init__.py` — `_poll_batch_progress_impl` "All jobs:" ETA block;
possibly `_format_eta` callers and `_estim_log` consumers.  **Priority:** medium
— affects confidence in estimates but blocks no workflow. Bundle with TODO-31 /
TODO-36 / TODO-51 (one cleanup pass over the bar+ETA system).

---

## TODO-36: Monitor Existing Jobs — progress count wildly off mid-bake — **OPEN**

**Filed 2026-05-28.** User switched the output folder back to AutoTest and
clicked **Monitor Existing Jobs** mid-bake.  Sub-task bar 1 displayed
`Baking 5 of 197` while the cache directory was actively writing `frame_0471`
(of 500).  Numbers:

- `5` ≈ "frames baked since Monitor was clicked".
- `197` ≈ `500 - 303` (frames still-to-bake at the moment Monitor started).
- Actual frame being written: 471 → 168 frames baked since Monitor start — but
  the bar froze at 5.

**Likely cause:** the bake-bar baseline was captured correctly at Monitor-click,
but the per-poll re-count of VDB files isn't updating in this scenario (or the
`start_time` mtime filter from BUG-003 is dropping recent frames). The poll's
`_count_vdb_frames` may be reading a cached/stale view. Inspect
`_poll_batch_progress_impl`'s bake-bar section and the Monitor-Existing-Jobs
initial state seeding.

**Files:** `__init__.py` — `_count_vdb_frames`, the bake-bar progress section in
`_poll_batch_progress_impl`, `SMOKE_OT_monitor_existing_jobs.execute`.

---

## TODO-31: RESUME progress bar should start at "(already-baked + 1) of total" — **OPEN (needs decision)**

**Filed 2026-05-27.** On a resume with e.g. 100/500 frames already baked, the bar
should read **"101 of 500"** at the start, not "0 of 332" (332 = missing). The
completed frames should count toward the full total.

**Dependency / caveat (important):** RESUME currently **re-bakes from frame 1**
(Mantaflow limitation, BUG-010 — no scripted API to truly resume mid-range). So
while it re-bakes frames 1–100 (overwriting in place), there is no honest way to
show "101 of 500" advancing — those frames are being redone. Options:
- (a) **Display-only:** show `max(already_baked, frames_this_run) of total`, so it
  opens at "100 of 500" and holds until the re-bake passes frame 100, then climbs.
  Matches the mental model but sits still during the 1–100 re-bake (looks stalled,
  esp. at high res).
- (b) Honest "re-baking N of 500" climbing from 1 — accurate but doesn't match the
  request.
- Truly starting at 101 requires **real partial resume**, which BUG-010 says is
  not achievable in scripted Mantaflow.

**Files:** `__init__.py` — `_poll_batch_progress_impl` bake-bar section,
`batch_bake_frame_baseline`. **Decision needed:** accept option (a)?

---

## TODO-27: Restore crash dumps (relax Job Object kill window) — **PARTIAL** (v0.2.33)

**v0.2.33 fix:** Added `_CRASH_DUMP_GRACE_SECS = 15` to the launcher.
`_save_crash_log` now waits up to 15 s for `blender.crash.txt` to appear in
`%TEMP%` with `mtime >= launch_time` before deciding the dump is missing. Stale
dumps from previous crashes are filtered out by mtime.

**Remaining work:** If 15 s isn't enough — i.e., the Job Object truly terminates
Blender before the SEH handler runs — we'd need deeper changes:
1. Replace `DIE_ON_UNHANDLED_EXCEPTION` with a `SetUnhandledExceptionFilter`
   approach that writes a minidump first, then terminates.
2. `WerReportSubmit` with a no-UI flag (dumps via WER, no dialog).
3. Longer post-exit `%TEMP%\blender.crash.txt` polling (~30 s).
4. Capture stderr/stdout to a `.crash_stderr` file (last 100 lines of Python
   output even without a structured dump).

Option 3 is least invasive. Re-evaluate after the next production batch shows
whether dumps start landing again.

**Files:** `smoke_launcher.py` — `_create_crash_suppression_job`,
`_save_crash_log`, post-exit polling. **Related:** `project_crash_root_cause` —
the actual crashes are in Blender 5.1.1's glTF/numpy import, not our code; better
dumps will confirm new signatures match.

---

## TODO-24: Per-frame bake timing not collected — **OPEN**

**Observation:** `perf_log.json` stores total bake time + total frames but not
per-frame timings.  Longer-running frames (high smoke density late in the sim)
aren't distinguishable from early frames.  A per-frame rate model could improve
ETA accuracy for high-frame-count jobs.

**Constraint:** `bpy.ops.fluid.bake_all()` is blocking — no per-frame hook without
a `frame_change_post` handler or a monitor thread in the worker.

**Proposed approach:** register a `bpy.app.handlers.frame_change_post` handler
before `bake_all()` that timestamps each frame advance; write per-frame data to
`perf_log.json` on completion.  **Files:** `smoke_worker.py` — bake section;
`perf_log.json` schema.  Ties to TODO-51.

---

## TODO-23: Retry overall batch time estimate is unreliable — **OPEN**

**Observed:** When a retry batch starts, `batch_jobs_elapsed` resets to 0. The
per-job estimate (bake + render for the single retry job) is correct, but the
overall estimate across all retry jobs uses stale or zeroed elapsed time.

**Proposed fix:** carry `batch_jobs_elapsed` across retry starts, or compute the
overall estimate purely from per-job ETA × remaining jobs.  **Files:**
`__init__.py` — `_poll_batch_progress_impl` overall-time-estimate section.  Bundle
with TODO-46.

---

## TODO-22: Crash timing inconsistency — one crash stalled ~5 min, another moved immediately — **INSTRUMENTED** (v0.3.0)

**v0.3.0 (diagnostics):** The launcher now logs `pid`, `exit_code`, and
`time_to_exit` for *every* job (printed to the per-job .log), plus
`werfault_poll_secs` on a crash. These distinguish the two candidate causes: a
large `time_to_exit` with small `werfault_poll_secs` means Blender genuinely
ran/hung; a small `time_to_exit` with a large `werfault_poll_secs` means the
stall was the post-exit WerFault poll. **Root cause still unconfirmed — revisit
once a stalled crash is captured with this data**, then decide whether
`_POST_EXIT_WERFAULT_SECS` / `_STALE_LOG_TIMEOUT` need tuning.

**Observed (v0.2.26 batch):** Two crashes in the same batch behaved differently —
the first stalled ~5 min before the launcher moved on; the second was detected
almost immediately.  **Files:** `smoke_launcher.py` — near WerFault post-exit poll.
