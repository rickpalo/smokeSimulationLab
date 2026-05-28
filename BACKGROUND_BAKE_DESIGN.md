# Background-Bake / Two-Phase Pipeline — Design + Build Plan

Status: **APPROVED, building incrementally (2026-05-27).** Resume is out of scope
(Mantaflow re-bakes from frame 1 in background too — see `BUG_TRACKER.md`
BUG-010 and memory `background-bake-plan`).

## LOCKED DECISIONS (2026-05-27)
- **Render phase = per-job processes** (NOT one windowed session). Bake all jobs
  headless, then render each job as its own process (EEVEE windowed / Cycles
  headless). Keeps per-job crash isolation + existing crash detection; avoids the
  in-session-state-hygiene risk. Trade-off accepted: EEVEE opens a window per
  render job (no "load UI once" saving).
- **Replace the current single-pass flow** (no opt-in toggle). git history
  (v0.3.3 = last single-pass version) is the fallback if the new flow misbehaves.

## BUILD PLAN (incremental; each increment keeps the tree working)
- **Increment 1 — worker `--phase {bake,render,both}`** — **DONE v0.3.4.**
  Default `both` = exact current behavior; render phase forces SKIP-bake; bake
  phase exits before render. Dormant (export passes no `--phase`).
- **Increment 2 — launcher `--phase`** — **DONE v0.3.5.** Forces `--background`
  for the bake phase; passes `--phase` through; windowed only for EEVEE render.
  Dormant.
- **Increment 3 — export_batch two-pass .bat (FLIPS the flow)** — **DONE v0.4.0.**
  Built in six sub-increments (3a helpers, 3b two-pass `.bat`, 3c worker
  append-mode log + phased worker_done, 3d launcher phased crashed,
  3e/3f addon regex matchers + phased-aware progress bar). The simplification
  that minimized addon rework: the `.bat` writes **per-phase** sentinels for
  diagnostics PLUS **unphased aliases** (`<stem>.done` after the final pass,
  `<stem>.crashed` whenever any phase crashes) so the existing poll/summary
  consumers work without change beyond exact-match regexes that exclude the
  phased variants. 267 tests pass.
- **Increment 4 — UI/progress polish**: Job Log per-phase status (BAKING /
  BAKED / RENDERING) and a phase dot per row; phase-aware time estimates.
  Still TODO.

## Increment 3 design (proposed — review before building)
- **.bat layout:** one sequential file, reordered into two passes —
  `echo === BAKE PASS ===` then all jobs `--phase bake`, then
  `echo === RENDER PASS ===` then all jobs `--phase render`. (2N launcher calls
  for N jobs.) Reuse `_job_run_cmd`/`_job_bat_block` with a `phase` arg.
- **Per-phase sentinels (avoid the two passes clobbering each other):**
  `job_NNNN.bake.done` / `job_NNNN.render.done`; worker writes
  `job_NNNN.bake.worker_done` / `.render.worker_done`; launcher writes
  `job_NNNN.bake.crashed` / `.render.crashed`. Job is COMPLETE only after
  `render.done` == success. (Today's single `.done`/`.worker_done` get a phase
  segment; update every producer/consumer.)
- **Per-phase logs:** `job_NNNN.bake.log` / `job_NNNN.render.log` (worker opens
  log_path "w" — without per-phase names the render pass would overwrite the
  bake log). `log_path` is set by export; make it phase-aware or have the worker
  derive `<stem>.<phase>.log`.
- **Poll / progress (the hard part):** the poll observes ALL bakes, then ALL
  renders. Overall bar: "Baking job X/N" during pass 1, "Rendering job X/N"
  during pass 2. Job Log row status gains phase: NOT_STARTED → BAKING → BAKED →
  RENDERING → COMPLETE (or two booleans baked/rendered). `_find_running_log`
  must pick the active phase's log; the bake/render frame-progress sub-bars key
  off the right per-phase log.
- **Crash + auto-retry:** crash detection is per phase; a bake crash skips/fails
  that job's render; auto-retry (now 3 rounds) retries the failed phase.
- **bake-only mode (render_simulation_result=False):** emit only the BAKE pass;
  no render pass.
- **Risk mitigation:** git v0.3.5 is the last single-pass build (the chosen
  "replace" fallback). Land increment 3 behind a focused test of the .bat
  generation + a real low-res 2-job batch run before trusting a 512 batch.

## Open implementation notes
- Render phase must still point `d.cache_directory` at the job cache to load the
  VDBs for rendering — same presave/merge wipe-protection as the bake path
  (effectively the SKIP-BAKE path, then render).
- Sentinel/poll rework (increment 3) is the crux; design it before coding.

## Why (the wins — all independent of resume)
- **Reliability:** background `bake_all()` is synchronous and never hits the
  windowed save/reload hang (BUG-010 v0.2.32).
- **Speed:** headless bake avoids window/viewport overhead.
- **Fewer crashes:** the glTF/numpy startup crash fires on *every* Blender
  launch (`--factory-startup` does NOT skip glTF — it's bundled). Today = one
  launch per job = N exposures. Two-phase = ~2 launches → far fewer.
- **Cycles can be fully headless** (no window at all). EEVEE still needs a window
  to render.

## Proposed shape
**Phase 1 — bake all jobs in `--background`.** Baking is engine-independent.
One process could loop all jobs (fewest launches) OR one process per job (keeps
crash isolation). Decide based on the crash-vs-isolation trade.

**Phase 2 — render.**
- `render_mode == CYCLES`: render in `--background` too → entire pipeline
  headless, no window ever.
- `render_mode == EEVEE`: render in ONE windowed session looping jobs (pay
  Blender startup once, not N times). Loses per-job process isolation for the
  render phase.

## Open questions / risks to resolve before building
1. **Loop-in-one-process vs process-per-job** for each phase — crash isolation
   vs launch count. (Resume being unavailable means a crashed bake just re-bakes
   from 1 on retry, so isolation matters less than it seems.)
2. **In-session state hygiene** (the big one for a looping render session): reset
   `cache_directory`, `resolution_max`, emitter densities, text objects, frame
   range, render engine/samples between jobs with NO leakage. The worker does
   all this per-process today; in-loop needs careful teardown/setup.
3. **Progress/UI model** becomes two-phase (bake phase, then render phase) — see
   Job Log notes below.
4. **Orchestration**: today one `.bat` runs N launcher calls. New model needs a
   bake `.bat`/pass and a render `.bat`/pass (or one launcher that does both
   phases).
5. **Crash detection** currently keys off per-job `.worker_done` + non-zero exit
   in a one-process-per-job model. A looping process needs per-job sentinels
   written mid-loop and a different "which job was running" recovery story.

## Effort estimate (rough)
Medium. Worker splits into bake-entry and render-entry (or a `--phase` arg);
`export_batch` writes two `.bat` passes; addon poll/Job-Log gains a phase notion.
Reuses existing per-job JSON, cache search, density/text setup, perf logging.

---

## UI changes IF we also remove the RESUME option entirely (NOTES ONLY — do not build)
Context: RESUME currently = (use_existing_cache + partial cache) → merge presave
+ bake (which re-bakes from 1 anyway). "Removing resume" would mean a partial
cache just takes the FULL-bake path; the SKIP path (complete cache) stays.

Things that would need touching (none designed yet):
- **`smoke_worker.py`**: drop the `elif use_existing_cache and baked_frames:`
  RESUME branch + its presave-merge; partial cache falls through to FULL BAKE.
  Keep SKIP (complete cache) and the presave wipe-protection for SKIP.
- **Bake-decision logging**: remove "RESUME — N present, M to bake"; the decision
  becomes SKIP / FULL only.
- **`__init__.py` panel**: `use_existing_cache` checkbox stays (it still drives
  SKIP), but its tooltip says "resume or skip" — reword to just "skip complete
  caches". No new/removed widgets strictly required.
- **Progress bar**: TODO-31 ("101 of 500" on resume) becomes moot — close/reject
  it, since there's no resume to show.
- **Estimation**: `bake_remaining` logic that scaled by remaining-frames for a
  retry (v0.2.28) can simplify (a partial cache → full bake → full estimate).
- **Docs**: README "Use Existing Cache" bullet + TODOS/BUG_TRACKER notes.
- **Tests**: drop/adjust any RESUME-path assertions (e.g.
  `TestWorkerResumeNoReload` stays relevant only while the branch exists).

Net: removing resume is mostly *subtractive* in the worker + a tooltip/doc
reword; no major UI restructure. Revisit if/when we commit to it.
