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
| TODO-56 | Docs overhaul — README/DOCUMENTATION split, fix CSV/install/features/screenshot | MOSTLY DONE (v0.9.4) — hero screenshot recapture remains | — |
| TODO-57 | Declarative `PARAM_SPECS` registry (kills shotgun-surgery + positional `combo[N]`) | OPEN | — |
| TODO-58 | Split 6.1k-line `__init__.py` into a package | OPEN | — |
| TODO-59 | Decompose `_poll_batch_progress_impl` + finish `draw()` section helpers | OPEN (started — `_estimate_batch_remaining` extracted v0.9.4) | — |
| TODO-60 | Cleanup — extract `_with_slow_companion`/`_first_value`; archive `rename_to_v0_7_1.py` | PARTIAL (v0.9.4 — `_first_value` done) | — |
| TODO-62 | Job Log header shows worker version (+ caution icon if ≠ expected) when jobs exist | OPEN | — |

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

---

## TODO-56: Documentation overhaul — **MOSTLY DONE (v0.9.4)**

**DONE 2026-06-21 (v0.9.4):** README.md rewritten as a concise summary; new
**DOCUMENTATION.md** full reference (all params incl. emitters/fire/timing,
two-phase pipeline, render settings, output structure, **correct 23-column CSV
schema**, caching/resume, estimates, troubleshooting, limitations); install steps
switched to the extension-feed flow; `DOCS_URL` + `bl_info` `doc_url` now point at
DOCUMENTATION.md (HELP button); three review §G screenshots folded into
`documentation/images/` (job_log_failed / render_time_estimate /
slow_dissolve_comparison) and referenced. **REMAINING:** recapture the stale
2026-04-25 hero `documentation/images/SmokeSimLab_Panel.png` (needs Blender open
with the current collapsible UI + Emitters section) and add the other §G captures
(panel overview, sim-params expanded, emitters section, range-vs-list close-up,
job-log mid-run, results.csv in a spreadsheet, render with overlays).

**Original scope (filed 2026-06-21, review Part 2):**
- **README.md → concise summary** (what it is, hero screenshot, key features,
  install via the extension feed, quick-start, link to full docs).
- **New `DOCUMENTATION.md` → full reference** (every parameter incl. emitters/
  fire/timing, iteration modes, output structure, full CSV schema, text overlays,
  troubleshooting, limitations). Point `DOCS_URL` / the HELP button at it.
- **Fix factual errors:** results.csv is documented as **11 columns**, the worker
  writes **23** (`smoke_worker.py` ~L1326-1353); add Fire (v0.7.0) + Time Scale/
  Adaptive Timesteps (v0.7.0) + **Emitter/Flow batch params (v0.9.0)** to Features
  and Parameter Reference; replace the legacy "download the zip from Releases"
  install steps with the extension-feed flow (`docs/README.md`).
- **Screenshots:** refresh the stale 2026-04-25 hero (`documentation/images/
  SmokeSimLab_Panel.png`, pre-collapsible-UI, pre-emitters); fold the three
  untracked captures in `Screenshots/` (failedJob / renderTimeEstimation /
  slowDissolve) into `documentation/images/`; add the shortlist in review §G.
**Severity:** HIGH (docs actively mislead). **Effort:** medium.

---

## TODO-57: Declarative `PARAM_SPECS` parameter registry — **OPEN**

**Filed 2026-06-21** (review Part 1 #1). Adding/changing one sweepable parameter
touches ~9 sites (`SmokeSettings` quintuplet, `_default_job`, `expand_param`,
`generate_jobs_limited`, `generate_jobs_all`, `make_name`, worker apply, UI draw,
CSV header+row). The worst spot is `generate_jobs_all` (`__init__.py` ~L884-909),
which maps `itertools.product` to a dict by **positional index** `combo[0]…
combo[16]` — inserting a param mid-list silently shifts every index. Introduce a
single `PARAM_SPECS` table (name, default, min/max, section/enable-toggle, label,
CSV inclusion) and drive defaults/expand/both job-gens/make_name/CSV (and ideally
property registration + UI) from it. **Severity:** HIGH. **Effort:** large.
Natural to pair with TODO-58. Add tests proving job-gen output is unchanged.

---

## TODO-58: Split the 6.1k-line `__init__.py` into a package — **OPEN (groundwork done)**

**Filed 2026-06-21** (review Part 1 #2). Everything (props, job-gen, emitter
discovery, progress polling, operators, UI, handlers, register) lives in one
module. Split into `properties.py`, `jobgen.py`, `emitters.py`, `settings_io.py`,
`progress.py`, `operators.py`, `ui.py`, `__init__.py` (register only). Watch two
extension-specific risks: **registration order** and **relative imports under
`bl_ext.*`** (installed as `bl_ext.<repo>.batchsimlab`). Keep the existing
`from BatchSimLab import …` test entry points working by **re-exporting** moved
names from the package `__init__.py`. Pairs with TODO-61 Tier B (filename renames).

**Groundwork done 2026-06-21 (no code moved yet):**
- **CRITICAL GATE — the pytest suite stubs `bpy`, so green tests do NOT prove the
  add-on still registers.** Use this real-Blender smoke-test after EVERY
  extraction (passes against current code):
  ```sh
  "C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" --factory-startup \
    --background --python-expr "import sys; sys.path.insert(0,'scripts'); \
    import BatchSimLab as b; b.register(); print('REGISTER_OK'); b.unregister(); \
    print('UNREGISTER_OK')"
  ```
  Gate per module = 618 pytest **+** REGISTER_OK/UNREGISTER_OK.
- **First module mapped — `jobgen.py` (lowest risk, do first):** a self-contained
  PURE cluster, no `bpy` calls, no deps on the rest of `__init__` except
  `ITERABLE_PARAMS` (which moves with it). Contents: `ITERABLE_PARAMS` (~L124),
  then the contiguous block `expand_param`→`make_name` (~L559–1307): `expand_param`,
  `_first_value`, `_default_job`, `generate_jobs_limited/all/generate_jobs`,
  `_dedupe_jobs`, `_fmt_num`, `make_name`, the emitter job-gen helpers
  (`_emitter_*`, `_default_emitters`, `_emitter_combinations`) + their constants
  (`_EMITTER_SCALARS`, `_EMITTER_VELOCITY_SCALARS`, `_OFF_SUFFIX`,
  `_EMITTER_NAME_*`). **Exclude** `find_fluid_emitters`/`emitters_inside_domain`
  (~L1308+) — bpy scene-scan → `emitters.py` (module #2). `__init__` then does
  `from .jobgen import *` (or explicit) so the ~209 job-gen tests + UI sites
  (`ITERABLE_PARAMS` at ~L2928/5983) keep resolving.
- **Suggested module order** (leaf → trunk): jobgen → emitters → settings_io →
  progress (incl. `_estimate_batch_remaining`, already extracted) → properties →
  operators → ui → `__init__` (register only). Commit one module at a time.
**Severity:** medium. **Effort:** large; natural to pair with TODO-57.

---

## TODO-59: Decompose `_poll_batch_progress_impl` + finish `draw()` helpers — **OPEN (started)**

**Filed 2026-06-21** (review Part 1 #3). `_poll_batch_progress_impl` (~650 ln) is
the live bar/ETA engine that TODO-23/31/36/46/51 keep returning to; break it into
phase helpers (bake-bar, render-bar, all-jobs-ETA, summary). `SMOKE_PT_panel.draw`
(~370 ln) already uses some `_*_ui` helpers — finish the pattern so `draw` just
composes sections. **Started v0.9.4:** the all-jobs-ETA math was extracted to the
pure, unit-tested `_estimate_batch_remaining` (TODO-46) — continue that style.
**Severity:** medium. Bundle with the bar+ETA TODOs.

---

## TODO-60: Cleanup — dedup helpers + archive one-off migrator — **PARTIAL**

**Filed 2026-06-21** (review Part 1 #4/#5; the dead `tools/smoke_launcher.py` half
is already DONE, v0.9.3).
- **DONE (v0.9.4):** `_first_value(s, name)` extracted (beside `expand_param`) and
  applied to the 34 `expand_param(s, "X")[0]` sites in `_default_job` + both job
  generators. Behaviour identical — all 209 job-gen tests + full 618 pass. (The
  emitter-side `expand_param(em, p)[0]` dict-comprehension uses a variable key, a
  different idiom — left as-is.)
- **REMAINING:** extract `_with_slow_companion(job)` — the slow-dissolve "flip
  companion" block repeated ~4× in `generate_jobs_limited` + once in
  `generate_jobs_all`; archive `tools/rename_to_v0_7_1.py` once no pre-0.7.1
  caches remain. **Severity:** low. **Effort:** small.

---

## TODO-62: Job Log header shows worker version (+ caution if mismatch) — **OPEN**

**Filed 2026-06-21** (user request). When jobs exist in the Job Log, show the
worker version in the section header, e.g. `Job Log — Worker v0.9.1`. If the
exported worker version ≠ `_EXPECTED_WORKER_VERSION`, show a caution icon (`ERROR`/
` pre-existing alert) next to it. Reuse `_read_helper_version(worker_path,
"WORKER_VERSION")` (now fixed by BUG-017) against the batch `output_path`; only
read once per poll/transition, not every draw (draw must stay cheap — see BUG-015).
Consider also showing the launcher version. **Severity:** low (clarity/diagnostic).
**Effort:** small.
