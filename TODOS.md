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
| TODO-55 | Batch **emitter / flow object** parameters | OPEN (spec) | v0.9.0 |
| TODO-54 | RELEASING.md describes a release-copy layout that doesn't exist | OPEN (doc-only) | — |
| TODO-51 | Better time estimates — samples + noise up-res terms | IN PROGRESS (step 1 done) | v0.8.0 |
| TODO-46 | "All jobs" ETA doesn't model two-pass bake-then-render | OPEN | v0.8.0 |
| TODO-36 | Monitor Existing Jobs — progress count wildly off mid-bake | OPEN | v0.8.0 |
| TODO-31 | RESUME progress bar should start at "(already-baked + 1) of total" | OPEN (needs decision) | v0.8.0 |
| TODO-27 | Restore crash dumps (relax Job Object kill window) | PARTIAL | — |
| TODO-24 | Per-frame bake timing not collected | OPEN | — |
| TODO-23 | Retry overall batch time estimate is unreliable | OPEN | — |
| TODO-22 | Crash timing inconsistency (5-min stall vs immediate) | INSTRUMENTED (root cause open) | — |

Also pending (not a TODO): **tag `v0.7.6` on GitHub** (built + feed live, not yet tagged).

---

## TODO-55: Batch **emitter / flow object** parameters — **IN PROGRESS** — target v0.9.0

**Increment 1 DONE (2026-06-16):** discovery foundation landed (dormant, not yet
wired to UI). New pure helpers in `__init__.py`: `_is_flow_object`,
`find_fluid_emitters` (scan), `_world_aabb` / `_aabb_overlap` (bounds math),
`emitters_inside_domain` (filter), `find_emitters` (composed). tests in `tests/test_todo55_emitters.py` (now 42 total).

**Increment 2a DONE:** Initial Velocity vector parse/format helpers
(`_parse_velocity_vector` / `_format_velocity_vector`, list-of-vectors model).

**Increment 2b DONE:** state layer (dormant). `VelocityItem` + `EmitterSettings`
PropertyGroups registered; `EmitterSettings` carries the Range/List sextet for
each scalar (temperature, density, surface_distance, volume_density, plus
velocity_factor=Source / velocity_normal=Normal gated by `use_initial_velocity`)
so `expand_param()` works on it unchanged, plus `velocity_list` (the XYZ
vector list). `SmokeSettings.emitters` CollectionProperty added. Pure
`_emitter_sync_plan(existing, desired)` reconciler preserves in-progress sweep
config across a Refresh.

**Next: increment 2c** = wire it up — `_populate_emitters(s, scene)` +
`_seed_emitter_from_flow` (seed `_begin` from live flow settings), call from the
domain-select callback + a "Refresh Emitters" operator; per-emitter collapsible
UI section (section D) with the velocity-vector UIList + add/remove ops; clear
`emitters` in `_reset_on_load`. Then increment 3 (job-gen + make_name) and 4
(worker applies flow settings).

**Filed 2026-06-16.**  Today a batch sweeps **domain**-level settings only. This
TODO is the v0.9.0 scope expansion (see [ROADMAP.md](ROADMAP.md)): let a single
batch sweep **emitter (flow object)** properties too. A domain can have multiple
emitters; each gets its own collapsible sub-section in Simulation Parameters
(default collapsed) exposing the iterable properties with the same Range / List
choice the domain params already use.

### A. Emitter discovery (the "does the domain know its emitters?" question)

**Reality check:** Blender's fluid **domain** does *not* keep an explicit
backlink to its flow objects. Mantaflow includes every object in the scene that
has a `FLUID` modifier with `fluid_type == 'FLOW'` and that overlaps the domain
bounds. So "selecting the domain auto-populates its emitters" must be
implemented by **scanning the scene**, not by reading a list off the domain.

**Approach (user-approved 2026-06-16):**
1. Scan the scene for **all** fluid-emitter objects — those with a `FLUID`
   modifier whose `fluid_type == 'FLOW'`.
2. **Filter out any emitter that is not inside the selected domain**, using the
   domain object's world-space bounding box. An emitter counts as "inside" when
   its world-space bounds intersect the domain's world-space bounds (use a
   bounding-box overlap test on `obj.bound_box` transformed by
   `obj.matrix_world`; a generous AABB-intersection is fine — exact mesh
   containment is unnecessary and Mantaflow itself works on the bounds).
3. Return the survivors ordered deterministically (by object name) so the
   per-emitter UI sections and job-dict keys are stable across re-exports.

**This addon is documented as SINGLE-DOMAIN only.** With one domain in the
scene, the inside-domain filter cleanly attributes every relevant emitter to it.
Multi-domain scenes are explicitly out of scope (a second domain would need
per-domain emitter attribution; not supported). State this in the README /
panel help so the assumption is visible to users.

- Discovery helper (pure, testable): split into
  `find_fluid_emitters(scene) -> list[Object]` (step 1, modifier scan) and
  `emitters_inside_domain(emitters, domain_obj) -> list[Object]` (step 2,
  bounds filter) so each is unit-testable without a full scene. A thin
  `find_emitters(scene, domain_obj)` composes them.

### B. Auto-populate on domain select

When the user picks the domain (existing `domain_obj` pointer), populate a
per-emitter UI state seeded with each emitter's **current** flow-setting values
(so the Range/List defaults start at the live scene values, not arbitrary
constants). Re-scan when the domain pointer changes or on an explicit "Refresh
Emitters" action (and on `_reset_on_load`). Keep emitter identity keyed by
object **name** (matches how jobs/caches are keyed elsewhere).

### C. Initial iterable property set

Map the user's terms to `bpy.types.FluidFlowSettings` (verify exact attr names
against the running Blender 5.x before coding — these are the expected mappings):

| UI label (user's term) | FluidFlowSettings attr | Notes |
|------------------------|------------------------|-------|
| Initial Temperature | `temperature` | "Initial Temperature" in the Flow panel |
| Density | `density` | smoke density |
| Surface Emission | `surface_distance` | |
| Volume Emission | `volume_density` | |
| Initial Velocity (enable) | `use_initial_velocity` | gates the four below |
| → Source | `velocity_factor` | only when initial-velocity on |
| → Normal | `velocity_normal` | only when initial-velocity on |
| → Initial X / Y / Z | `velocity_coord[0..2]` | 3-float vector; only when on |

The velocity terms are only sweepable when `use_initial_velocity` is on (mirror
how dissolve_speed is excluded when `use_dissolve` is off — the `sweepable`
gating in `generate_jobs_*`).

**Velocity sweep model (decided 2026-06-16):** Initial Velocity is entered as a
**list of explicit `x, y, z` vectors** — the user adds as many vectors as they
want to compare; each vector is one swept value. Default `0, 0, 0`. The entry
widget shows the format hint (`_VELOCITY_FORMAT_HINT`). NOT 3 independent axes
(avoids product blow-up) and NOT magnitude-only. Pure parse/format helpers
`_parse_velocity_vector` / `_format_velocity_vector` landed in increment 2a
(12 tests) so the widget and job generation share one definition.

### D. UI — per-emitter section in Simulation Parameters

- One collapsible box **per discovered emitter**, default **collapsed**,
  labelled with the emitter object name. Lives inside the existing Simulation
  Parameters area alongside the domain param sections (reuse the
  `show_*`/TRIA collapse pattern from TODO-44).
- Inside each: the Range / List choice for every iterable property in (C), using
  the **same** `_sub_param_ui()` / range-or-list widgets the domain params use,
  so behaviour and look are consistent.
- A dynamic number of emitters means dynamic UI + dynamic properties — likely a
  `CollectionProperty` of an `EmitterSettings` PropertyGroup (one element per
  emitter), since Blender properties must be statically registered. Each element
  carries the per-property min/max/step (Range) or value-list (List) + the
  enable flag, keyed by emitter name.

### E. Job generation, worker, naming (the hard part — cache safety)

This is why it's a major version, not a point release:

1. **Job dict schema.** Each `job_NNNN.json` gains an `emitters` block:
   `{ "<emitter_name>": { "temperature": v, "density": v, ... , "use_initial_velocity": bool, "velocity_factor": v, "velocity_normal": v, "velocity_coord": [x,y,z] } }`.
   `generate_jobs_limited` / `generate_jobs_all` must take the **cartesian product
   across domain params × every swept emitter property** — guard the combinatorial
   blow-up (a job-count preview + warning, like the existing "N jobs will be
   created" label).
2. **Worker applies them.** `smoke_worker.py` must, before baking, look up each
   flow object by name and set its `flow_settings` from the job's `emitters`
   block (skip/warn if a named emitter is missing in the scene). Worker version
   bump + re-export.
3. **make_name() / cache safety (CRITICAL — BUG-013 / BUG-014 family).** Emitter
   params **must** be encoded into the job name so two jobs differing only in an
   emitter value don't share a cache dir and silently SKIP-bake the wrong data.
   Use the defaults-suppressed compact encoding (TODO-47/48): emit a per-emitter
   suffix only for properties that differ from the live/default value. Multiple
   emitters → namespaced suffix (e.g. `E1.T1.5_E1.D1_E2.T2`). A
   `TestNoCacheCollisions`-style test must prove every swept emitter property
   yields distinct names.
4. **results.csv** gains the emitter columns; **perf_log / estimates** may later
   need an emitter-density term (defer; ties to TODO-51).

### Open design questions (resolve when picked up)

- Multi-domain attribution (deferred — see A).
- Property registration for a *dynamic* emitter count (CollectionProperty vs
  rebuilding properties on refresh).
- Velocity vector as one List axis vs three independent axes (3 independent
  axes explodes the product fast — maybe sweep magnitude + keep direction, or
  treat the XYZ vector as a single List of presets).
- Whether emitter sweeps combine with domain sweeps by full cartesian product
  (powerful but huge) or a "vary one axis at a time" Limited mode.

### Tests
`find_emitters` (discovery + ordering); auto-populate seeding from flow settings;
the velocity-gated `sweepable` exclusion; job-generation product (count +
content); worker flow-settings application (by name, missing-emitter guard);
`make_name` emitter encoding + no-collision regression.

**Files:** `__init__.py` (EmitterSettings PropertyGroup + CollectionProperty,
`find_emitters`, domain-select populate, per-emitter UI, `generate_jobs_*`,
`make_name`, `export_batch`); `smoke_worker.py` (apply flow settings, version
bump); tests.

---

## TODO-54: RELEASING.md describes a release-copy layout that doesn't exist — **OPEN**

**Filed 2026-06-15.**  RELEASING.md (repo structure section + "Your local
working copies live in `scripts/SmokeSimLab/` (ignored by git) … copy them into
`SmokeSimLab/` at the repo root") describes a two-directory workflow that is
**not** how the repo actually works:
- `scripts/SmokeSimLab/` is **tracked** (it's the real source — `__init__.py`,
  `smoke_worker.py`, `smoke_launcher.py`, and now `blender_manifest.toml`), not
  git-ignored.
- There is **no** `SmokeSimLab/` release-copy dir on disk or in git.

So the "copy to `SmokeSimLab/` before release" step is stale fiction — releases
are built straight from `scripts/SmokeSimLab/` (see the v0.7.6 extension build:
`--source-dir scripts/SmokeSimLab`).

**Fix:** rewrite RELEASING.md's "Repository structure" + step-1 wording to match
reality — single tracked source at `scripts/SmokeSimLab/`, extension built/zipped
from there into `dist/` (ignored) or `docs/` (feed).  Drop the
working-copy/release-copy split entirely.  Doc-only; no code or version bump.

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

## TODO-46: Time estimate doesn't account for two-pass bake-then-render — **OPEN** (v0.7.0)

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
