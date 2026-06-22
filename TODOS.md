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
| TODO-58 | Split 6.1k-line `__init__.py` into a package | IN PROGRESS — modules #1 `jobgen` + #2 `emitters` + #3 `settings_io` + #4 `progress` + #5 `properties` + #6 `operators` + #6b `engine` (run/poll) extracted; `__init__` 6207→1429 ln; remaining: #7 `ui` (panel + UILists) | — |
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

## TODO-58: Split the 6.1k-line `__init__.py` into a package — **IN PROGRESS (modules #1–4 done, UNCOMMITTED)**

**Module #1 `jobgen.py` EXTRACTED (this session, no behaviour change):** the pure,
`bpy`-free job-gen cluster moved to `scripts/BatchSimLab/jobgen.py` (779 ln) —
`ITERABLE_PARAMS`, the `expand_param`→`make_name` block, the emitter job-gen
helpers, plus the two pure velocity-text deps it needed (`_VELOCITY_DEFAULT`,
`_parse_velocity_vector`; the inverse `_format_velocity_vector` + UI
`_VELOCITY_FORMAT_HINT` stayed). `__init__.py` 6207→5486 ln; re-imports all 23
names from `.jobgen` so `from BatchSimLab import …` + the ~209 job-gen tests
resolve unchanged. Now-unused `import copy`/`import itertools` dropped from
`__init__`. Gate passed: **689 pytest** (was 618 + 71 new in `test_jobgen_module.py`,
which locks the re-export contract: each name lives in `jobgen` AND is the same
object on the package) **+ REGISTER_OK/UNREGISTER_OK** in real Blender 5.1.
**Module #2 `emitters.py` EXTRACTED (this session):** the fluid-emitter
discovery + sync cluster moved to `scripts/BatchSimLab/emitters.py` (238 ln) —
`_blend_domain_resolution`, the discovery chain (`_is_flow_object`,
`find_fluid_emitters`, `_world_aabb`, `_aabb_overlap`, `emitters_inside_domain`,
`find_emitters`), and the sync side (`_emitter_sync_plan`, `_EMITTER_FLOW_IMPORT_MAP`,
`_flow_settings_of`, `_seed_emitter_from_flow`, `_populate_emitters`).  Turned out
to be pure too (every helper is duck-typed on its scene/object/settings args — no
`bpy.` calls), so it stays unit-testable.  To break the one cross-dep
(`_seed_emitter_from_flow` needs the velocity-text helpers), `_format_velocity_vector`
was also moved into `jobgen.py` alongside `_VELOCITY_DEFAULT`/`_parse_velocity_vector`;
`emitters.py` imports those from the leaf `jobgen` (no cycle).  The UI-only
`_VELOCITY_FORMAT_HINT` stayed in `__init__`.  `__init__.py` now 5486→5277 ln;
re-imports all 12 emitter names + `_format_velocity_vector`.  Gate: **732 pytest**
(+43 in `test_emitters_module.py`, incl. a stub-scene `find_emitters` end-to-end +
the same-object re-export contract) **+ REGISTER_OK/UNREGISTER_OK**.

**Module #3 `settings_io.py` EXTRACTED (this session):** the `.smokesettings`
preset save/load cluster moved to `scripts/BatchSimLab/settings_io.py` (192 ln) —
`_SWEEP_PARAMS` (moved, used only by this cluster + tests), `_settings_dict`,
`_apply_settings_dict`, `_load_settings_from_path`, `_is_settings_dirty`, the
`_SETTINGS_ENUM_SENTINEL` + `_settings_items_cache` constants, and the two dynamic
preset-dropdown callbacks `_settings_files_enum_items` / `_on_settings_enum_update`.
**First bpy-touching module** (the enum callbacks call `bpy.path.abspath`), so it
imports `bpy`/`json`/`os`; conftest stubs `bpy.path.abspath` for pytest, the
REGISTER gate covers real Blender. No dep on jobgen/emitters (near-leaf). The
registration-order gotcha held: the two callbacks are referenced at CLASS-BODY
level in `SmokeSettings` (`items=…`/`update=…`), and the `from .settings_io import …`
re-import sits ABOVE the class defs, so it's covered. `__init__.py` 5277→5131 ln;
re-imports all 9 names. `json` stays (still used elsewhere in `__init__`). Gate:
**762 pytest** (+30 in `test_settings_io_module.py`: defined-in-module + same-object
re-export contract + a snapshot→apply round-trip through the package) **+
REGISTER_OK/UNREGISTER_OK** in real Blender 5.1.

**Module #4 `progress.py` EXTRACTED (this session) — only the PURE half.** The
risk with progress was that the poll engine shares *rebindable* module globals
(`_last_auto_index`, `_auto_retry_count`, `_job_log_rows`, `_job_statuses`,
`_batch_times`, `_estim`, `_poll_state`) with operators/load-handlers that stay in
`__init__`; a `global X; X = …` rebind from `__init__` against a variable that
*lived* in `progress` would split the binding across two modules and silently
diverge. So the split drew the line at purity: `scripts/BatchSimLab/progress.py`
(382 ln) got the 8 stateless helpers that take all inputs as args and rebind
nothing — `_find_running_log`, `_count_vdb_frames`, `_count_png_frames`,
`_format_eta`, `_estimate_batch_remaining`, `_format_elapsed`, `_has_error`,
`_compute_batch_summary` — plus the constants they own (`_SETUP_SECS_DEFAULT`,
`_STILL_SECS_DEFAULT`, the unphased sentinel regexes `_DONE_RE`/`_RETRY_DONE_RE`/
`_CRASHED_RE`, `_LOG_DONE_MARKERS`). Pure / bpy-free (imports only `json`/`os`/`re`);
no dep on the other modules. **LEFT in `__init__` on purpose:** `_poll_batch_progress`
/ `_poll_batch_progress_impl`, `_update_job_log_statuses`, `_redraw_panels`, the
`_bt*`/`_estim*` helpers, and ALL the rebindable globals above (kept with the
phased `_BAKE_DONE_RE`/`_RENDER_DONE_RE` regexes that only the engine uses). The
functions were non-contiguous (`_update_job_log_statuses` interleaves) + constants
scattered (L212/447/2252), so extraction was surgical (`sed -n` two ranges out,
`sed -i '…d'` four ranges deleted). `__init__.py` 5131→4813 ln; re-imports all 14
names. `re`/`json`/`os` all still used in `__init__`. Gate: **810 pytest** (+48 in
`test_progress_module.py`: defined-in-module + same-object re-export + a guard that
the stateful engine/globals did NOT leak here + format/estimate/find-log behaviour)
**+ REGISTER_OK/UNREGISTER_OK** in real Blender 5.1.

**Module #5 `properties.py` EXTRACTED (this session) — the highest-register-risk
module, gated clean.** The five `bpy.props` PropertyGroups moved to
`scripts/BatchSimLab/properties.py` (1002 ln) — `ValueItem` (+ nested
`_clamp_value`), `VelocityItem`, `EmitterSettings`, `SmokeJobItem`, and the
~650-line `SmokeSettings` monster — together with the class-body callback
factories/helpers they wire up: `make_toggle_range`/`make_toggle_list`,
`_sync_frame_defaults`, `_import_domain_params` (+ its `_DOMAIN_IMPORT_MAP`), and
`_on_render_sim_result_update`. **Cycle resolved as predicted:** moving
`_on_render_sim_result_update` (a pure SmokeSettings toggle-clear callback) into
properties.py avoided properties→`__init__` import. The module imports `bpy`,
`_populate_emitters` (from `.emitters`), and `_settings_files_enum_items` /
`_on_settings_enum_update` (from `.settings_io`) — both one-way leaves, no cycle.
**INTERLEAVING handled:** the two UIList classes that sit physically between the
PropertyGroups (`SMOKE_UL_value_list`, `SMOKE_UL_job_log`) are UI → LEFT in
`__init__` (→ #7); extraction was the non-contiguous PropertyGroup ranges around
them (one defensive Python script, boundary-asserted). `classes=[…]` + `register()`
stay in `__init__`; the new `from .properties import (…11 names…)` re-import sits
ABOVE them. `__init__.py` 4813→3868 ln; re-imports all 11 names.

**Source-text test infra change (new this module, per user decision "read whole
package"):** seven source-grep regression assertions read `__init__.py` and assert
on now-moved property/helper defs. Added `tests/addon_src.py::read_addon_source()`
— concatenates every addon-package `*.py` (EXCLUDES `smoke_worker.py` /
`smoke_launcher.py`, which are separate deployables tested via their own readers) —
and repointed the three broken helpers (`test_todo44_sections._addon_src`,
`test_todo55_emitters._src`, `test_v070_param_expansion._addon_src`) at it. The
other `__init__`-only source readers (jobgen re-export contract, bl_info doc-URL
checks, AST version-stamp parsing) were LEFT as-is; convert the next ones to
`read_addon_source()` in #6/#7 *as their targets actually move* (re-verifying each
`... not in src` / version assertion). Gate: **851 pytest** (+41 in
`test_properties_module.py`: defined-in-module + same-object re-export + a guard
the UILists/panel did NOT leak here + make_toggle/sync-frames/render-toggle/
domain-import behaviour) **+ REGISTER_OK/UNREGISTER_OK** in real Blender 5.1.

**Module #6 `operators.py` EXTRACTED (this session) — Tiers 1+2 only (per user
decision).** Moved the operators that DON'T touch the run/poll engine, into
`scripts/BatchSimLab/operators.py` (899 ln): `SMOKE_OT_export_batch` +
`export_batch()` + its 6 pure export helpers (`_scene_has_camera`,
`_find_next_job_index`, `_existing_jobs_for_bat`, `_job_run_cmd`, `_job_bat_block`,
`_batch_ready`), `_PARAM_BOUNDS`, the preset `save_settings`/`load_settings`, the
value `add/remove` ops + `_next_list_value`, the emitter `refresh`/`add`/`remove`
value+velocity ops + `_emitter_of`, and `open_docs`. Imports are one-way leaves
(`.jobgen`/`.emitters`/`.settings_io`); operators.py NEVER imports `__init__` at
module scope. **The few `__init__`-resident names export/open_docs need** —
`_job_log_rows`, `_job_statuses` (in-place mutate only, never rebound), `_debug_log`,
`ADDON_VERSION`, `DOCS_URL` — are pulled via a **function-local deferred import**
(`from . import …`) to avoid an operators→`__init__` cycle. `__init__.py` 3868→3052
ln; re-imports all 21 moved names.

**⚠ EXTRACTION GOTCHA (fixed, note for #6b/#7):** a `^def ` grep HID three classes
(`SMOKE_UL_value_list`, `SMOKE_UL_job_log`, `SmokeSimLabPreferences`) sitting
*between* `export_batch()` and `class SMOKE_OT_export_batch` — they got swept into
the export_batch range and wrongly moved. Boundary asserts (which only check the
ENDS of a range) passed anyway. Caught by the register/import gate (NameError +
`SmokeSimLabPreferences.bl_idname = __name__` must = package name). Fixed forward
(moved them back to `__init__`). **Lesson: map ranges against `^class|^def`
TOGETHER, and assert NO unexpected `class `/`def ` appears INSIDE each moving
range, not just at its boundaries.**

**STILL in `__init__` (Tier 3 — the cohesive run/poll engine):** `SMOKE_OT_run_batch`
/ `retry_failed` / `monitor_existing_jobs` / `remove_all_jobs` / `setup_results` /
`reset_to_defaults`, the poll loop (`_poll_batch_progress*`, `_update_job_log_statuses`,
`_redraw_panels`, `_bt*`/`_estim*`/`_debug_log`), the rebindable scalars
`_last_auto_index`/`_auto_retry_count` + in-place state `_job_log_rows`/`_job_statuses`/
`_batch_times`/`_estim`/`_poll_state`, `_batch_is_running`, the load handlers, the 3
UILists, `SmokeSimLabPreferences`. Gate: **924 pytest** (+73 in
`test_operators_module.py`: defined-in-module + same-object re-export + guards that
the engine/UILists/Prefs did NOT leak here, that the rebindable state is NOT an
operators module-global, and that the deferred-import targets stay reachable) **+
REGISTER_OK/UNREGISTER_OK** in real Blender 5.1. Also converted `test_camera_check`'s
two `_src()` readers to `read_addon_source()` (export wiring moved).

**Module #6b `engine.py` EXTRACTED (this session) — the stateful run/poll engine.**
Moved the Tier-3 cluster into `scripts/BatchSimLab/engine.py` (1723 ln): the poller
(`_poll_batch_progress`/`_impl`), `_update_job_log_statuses`, the `_bt*` timing +
`_estim*` estimate-logging helpers, `_debug_log`, `_redraw_panels`, `_should_auto_retry`,
the phased regexes `_BAKE_DONE_RE`/`_RENDER_DONE_RE`, `_STAGES`/`_TOTAL_SUBTASKS`,
`_batch_is_running`, the bake/render RATE constants, the in-place state
(`_job_statuses`/`_job_log_rows`/`_batch_times`/`_estim`/`_poll_state`), the rebindable
scalars (`_last_auto_index`/`_auto_retry_count`), and the 6 run operators
(run_batch/retry_failed/setup_results/remove_all_jobs/monitor/reset_to_defaults) +
`_auto_retry_deferred`/`_setup_results_deferred`. **No container refactor needed:**
every rebinder of the two scalars moved together, so `global` stays valid within
engine.py; the staying load handlers only mutate `_job_*` IN PLACE (works through the
re-import). Imports `from .progress` (pure helpers) + `from .operators import
_scene_has_camera`; NEVER imports `__init__` at module scope. **Deferred (function-local)
`from . import …`** for the 5 `__init__`-resident names engine reaches: `ADDON_VERSION`
(in `_estim_log`), `_read_helper_version`+`_EXPECTED_*` (in run_batch), `_reset_on_load`
(in reset_to_defaults). The 2 rebindable scalars are engine-owned but **NOT re-exported**
(a re-imported int is a stale snapshot; nothing outside engine reads them — verified).

**KEY SAFETY STEP (do this for #7 too):** pytest+REGISTER do NOT call the run operators,
so a missing import inside an engine *function* would NOT be caught. Ran an **AST
unbound-name analysis** (collect every `Name(Load)` minus all bound names minus builtins)
on engine.py → caught 11 real misses the gates missed: `import math`, the 5 RATE
constants (engine-only — moved in), and the 5 deferred-import targets. Re-ran until
clean. `__init__.py` 3052→1429 ln. Gate: **1036 pytest** (+112 in `test_engine_module.py`:
same-object re-export contract, UI-didn't-leak guard, scalars-not-re-exported guard,
deferred-target reachability, `_should_auto_retry`/`_bt` behaviour, and a real
`_estim_log`→ADDON_VERSION deferred-import exercise) **+ REGISTER_OK/UNREGISTER_OK** in
real Blender 5.1. Also converted `test_v060_fixes._addon_src` to `read_addon_source()`.

### ▶ RESUME POINT (fresh session) — LAST module: #7 `ui.py`

**Baseline now = 1068 pytest + REGISTER_OK/UNREGISTER_OK** (v0.9.7: added the manual
**Retry Failed Jobs** button to the Utilities box below Monitor Existing Jobs, AND
broadened `SMOKE_OT_retry_failed` to also re-run jobs that never finished — no final
unphased `.done` (interrupted/never-started), not just error-marked ones. Detection
extracted into pure tested `engine._jobs_needing_retry`; verified it now catches all 24
unfinished jobs in the AutoTest sweep (was 14). v0.9.6 hotfix BUG-018 clamp-recursion
crash + BUG-019 export NameError regression). **A new
permanent guard `tests/test_no_unbound_names.py` runs AST unbound-name analysis on
every package module — it WILL cover ui.py, but still also headless-exercise the
panel `draw()` when done (the AST guard catches missing imports, not draw-time logic
or RNA misuse).** Modules #1–6b complete,
gated green, still UNCOMMITTED (per user: ONE commit at the very END of the split, off a
branch — `main` is the current branch). Working-tree files that must NOT be lost: new
`scripts/BatchSimLab/{jobgen,emitters,settings_io,progress,properties,operators,engine}.py`,
`tests/{addon_src.py,test_*_module.py ×7}`; modified `scripts/BatchSimLab/__init__.py`,
`TODOS.md`, five repointed source-grep tests (`test_todo44_sections`, `test_todo55_emitters`,
`test_v070_param_expansion`, `test_camera_check`, `test_v060_fixes`). Don't
`git stash`/`reset`/`checkout` these.

**#7 `ui.py` (LAST) — checkpoint with user.** Move: the panel `SMOKE_PT_panel` + the
`_*_ui` draw helpers (`_sub_param_ui`/`_settings_ui`/`_standalone_param_ui`/`_gas_ui`/
`_noise_ui`/`_fire_ui`/`_emitter_sub_param_ui`/`_emitter_velocity_ui`/`_emitters_ui`),
the 3 UILists (`SMOKE_UL_value_list`/`SMOKE_UL_job_log`/`SMOKE_UL_velocity_list`), the
UI-only `_VELOCITY_FORMAT_HINT`, and the panel-private helpers (`_progress_active`/
`_effective_show_progress` if they exist). The panel is heavily source-tested but the
readers already read the whole package (`read_addon_source`), so re-export covers. Panel
draw reads `ADDON_VERSION`/`_batch_is_running`/`_batch_ready`/`generate_jobs`/
`noise_grid_exceeds_ceiling`/`_job_log_rows`/`_job_statuses`/the `_*_ui` helpers — trace
each (ui.py→__init__ for ADDON_VERSION = deferred import; the rest are
jobgen/operators/engine, import direct). **STILL apply: AST unbound-name analysis on
ui.py before gating**, and assert no stray non-UI `class`/`def` in moving ranges.
SmokeSimLabPreferences + the load handlers + register/metadata STAY in `__init__`.

Then `__init__` keeps only register/handlers/metadata. Final step: ONE commit on a new
branch. **Follow the proven methodology below.**

#### Historical pre-map for #5 (now DONE — kept for methodology reference)

#### Historical pre-map for #5 (now DONE — kept for methodology reference)



**STATE OF THE TREE — READ FIRST.** Modules #1–4 are **complete, gated green, and
UNCOMMITTED** (per user: one single commit at the very END of the whole split, off
a branch — `main` is the current branch). Uncommitted working-tree files that must
NOT be lost: new `scripts/BatchSimLab/{jobgen,emitters,settings_io,progress}.py`,
`tests/test_{jobgen,emitters,settings_io,progress}_module.py`; modified
`scripts/BatchSimLab/__init__.py`, `TODOS.md`. Don't `git stash`/`reset`/`checkout`
these. Re-confirm green before starting #5 (baseline = **810 pytest +
REGISTER_OK/UNREGISTER_OK**).

**The proven methodology (repeat for every remaining module):**
1. Map the cluster's CURRENT line numbers (they shift after each extraction).
2. Trace deps BOTH ways: external names the cluster references (→ must import or
   move) and cluster names referenced elsewhere/in tests (→ must re-export). Grep
   tests for `monkeypatch`/`mock.patch` of the names (none so far — keep checking).
   **For #5+ also trace rebindable module globals** (`global X; X = …`): if a global
   is rebound from BOTH the moving cluster and code staying in `__init__`, the
   *variable* must NOT move (see #4 — split the binding = silent divergence). Move
   only the pure / read-or-in-place-mutate surface; keep rebound state put.
3. Write `<mod>.py` (header + imports), append the exact block via
   `sed -n 'A,Bp' __init__.py >> <mod>.py` for fidelity (a linter may reflow — fine).
4. In `__init__.py`: add an explicit `from .<mod> import (…ALL names…)` at the TOP
   import block, then delete the original defs (`sed -i 'A,Bd'`). Drop any stdlib
   import that's now unused in `__init__`.
5. Add `tests/test_<mod>_module.py` mirroring the existing four (defined-in-module +
   re-exported-as-SAME-object contract; a small behavioural sanity test).
6. GATE: full pytest **and** the real-Blender REGISTER_OK smoke-test (the command
   block above — pytest stubs `bpy`, so green pytest alone does NOT prove the addon
   still registers). Update this section's line counts + status.

**⚠ Module #5 `properties.py` — CHECKPOINT WITH THE USER BEFORE STARTING (per the
plan). Highest registration-order risk of the whole split.** This is the
`bpy.props` PropertyGroups + their class-body property factories/callbacks.

**PRE-MAPPED (verified 2026-06-21 after #4; line #s WILL shift — re-grep fresh):**
- **Classes that MOVE (the 5 PropertyGroups):** `ValueItem` (~L894), `VelocityItem`
  (~L929), `EmitterSettings` (~L945), `SmokeJobItem` (~L1053), `SmokeSettings`
  (~L1133–1787, the ~654-line monster with all the per-param `*_use_range`/`_begin`/
  `_end`/`_step`/`_list` blocks).
- **⚠ INTERLEAVING — non-contiguous extraction.** Two UIList classes sit BETWEEN the
  PropertyGroups: `SMOKE_UL_value_list` (~L916, between ValueItem & VelocityItem) and
  `SMOKE_UL_job_log` (~L1073, between SmokeJobItem & SmokeSettings). UILists are UI →
  they belong to #7 `ui.py`, so SKIP them (leave in `__init__`); extract the
  PropertyGroup ranges around them. (`SMOKE_UL_velocity_list` ~L2093 is down with the
  operators — also #7, untouched by #5.)
- **Class-body helpers the PropertyGroups depend on → MOVE WITH THEM (re-export):**
  `make_toggle_range`/`make_toggle_list` (~L295/307, used as `update=` in ~40 toggle
  props + by tests), `_sync_frame_defaults` (~L319, `update=` on frame props),
  `_import_domain_params` (~L369, `update=` on the domain pointer). `_clamp_value` is
  a nested def INSIDE `ValueItem` (~L902) → moves automatically with the class.
  **GOOD NEWS:** none of these four rebind a module global (every `global` stmt in
  `__init__` is in the progress/operator/handler code, NOT in these callbacks), so the
  property cluster is rebind-safe — unlike #4, nothing here is forced to stay.
- **Already-moved deps referenced at CLASS BODY (keep imports ABOVE the class — the
  top re-import block already does):** `_settings_files_enum_items` /
  `_on_settings_enum_update` (settings_io, `items=`/`update=` ~L1243-44). Also
  `_on_render_sim_result_update` (still in `__init__`, ~L568-orig) is `update=` on a
  SmokeSettings bool — if properties.py moves and `__init__` still defines that
  callback, properties.py must import it FROM `__init__`… which is a CYCLE. Resolve by
  moving `_on_render_sim_result_update` into properties.py too (it's a small pure
  toggle-clear callback), or into a shared leaf. CHECK THIS before extracting.
- **`noise_grid_edge`/`noise_grid_exceeds_ceiling` (~L272/282):** pure validators used
  by `_noise_ui` (→#7) + tests; NOT class-body deps. Optional to move; simplest to
  leave in `__init__` for now (or hand to a later utils module).
- **Registration:** the `classes = [...]` list (~L4757) + `register()` (`PointerProperty(
  type=SmokeSettings)` on `Scene.smoke_settings`) STAY in `__init__`; the top re-import
  makes `SmokeSettings` et al. resolve. PropertyGroups must register before the panel/
  operators that reference them — order in `classes` is already correct; don't reorder.
- **Tests to keep green (import via package root, so re-export covers them):**
  `test_v070_param_expansion` (make_toggle_*), `test_todo55_emitters`
  (_import_domain_params/EmitterSettings), `test_noise_ceiling` (noise_grid_*). No
  monkeypatching of any of these (checked). Lean hard on the REGISTER gate — this is
  the module most likely to surface a registration-order break.

**Remaining order after #4:** #5 `properties.py` → #6 `operators.py` → #7 `ui.py`
→ `__init__` (register only). #5–7 touch the most-debugged machinery
(PropertyGroups, operators, panel) — checkpoint with the user before #5. Final step
once the split is done: ONE commit on a new branch, then optionally TODO-61 Tier B
filename renames.



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
