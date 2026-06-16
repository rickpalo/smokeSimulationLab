# BatchSimLab — Roadmap

> _Project renamed from "SmokeSimLab" to "BatchSimLab" at v0.6.3 (surface-only);
> all TODO-* IDs remain stable across the rename._

This is the long-horizon plan. Day-to-day tasks live in [TODOS.md](TODOS.md);
finished work is archived in [TODOS_COMPLETED.md](TODOS_COMPLETED.md).

The addon is being repositioned from a **smoke-only** batch tool into a
**general batch simulation** tool. Each major version broadens what a single
batch can sweep, moving outward from domain settings → emitter settings → fire →
fluid.

---

## Where we are today (v0.7.x)

A batch sweeps **domain-level** settings only: resolution, dissolve (+ slow/fast),
noise (+ up-res), fire parameters (as domain params — see v0.7.0 / TODO-42),
time scale / adaptive timestep / CFL, and the gas params (vorticity, buoyancy
density/heat). Emitter (flow object) properties are **not** batchable yet — every
job uses whatever the flow objects already have in the scene.

Two-pass pipeline (bake-all then render-all), MODULAR-cache resume, crash/stale
watchdog, self-hosted extension update feed.

---

## v0.8.0 — UI/UX polish bundle

Tighten the existing feature set before broadening scope.

- **TODO-43** — "Create Default Setup" operator (one-click domain + emitter +
  camera starter scene).
- **TODO-36** — Monitor Existing Jobs progress-count refactor (count is wildly
  off mid-bake). _Open — see TODOS.md._
- **TODO-31** — RESUME progress-bar baseline ("(already-baked + 1) of total").
  _Open — see TODOS.md._
- Time-estimate cleanup bundle (**TODO-46 / TODO-51 / TODO-23**): two-pass-aware
  ETA, samples + noise-upres terms, rolling-average from completed jobs.

---

## v0.9.0 — Batch **emitter** parameters  ← next major scope expansion

Today the addon only batches **domain**-level settings. v0.9.0 adds sweep
machinery for **emitter / flow object** properties so a single batch can compare
emitter configurations rather than just domain configurations.

A domain can have **multiple** flow objects. v0.9.0 introduces a per-emitter
section in Simulation Parameters (one collapsible sub-section per emitter,
default collapsed), each exposing the iterable emitter properties with the
standard Range / List choice that domain params already use.

**Initial iterable set (TODO-55):** Initial Temperature, Density, Surface
Emission, Volume Emission, and Initial Velocity (when enabled: Source, Normal,
Initial X/Y/Z).

See **TODO-55** in [TODOS.md](TODOS.md) for the full spec, the
Blender-property mapping, and the open design questions (emitter discovery,
job-dict schema, make_name() encoding, cache-collision safety).

---

## v1.0.0 — Smoke simulation feature-complete

All planned **smoke**-related parameters (domain + emitter + noise + dissolve +
cache) are batch-able. First stable release; docs and tests considered
production-ready. The addon may be renamed to a non-"smoke"-specific name at
v1.0.0 in preparation for v2.0.0.

---

## v2.0.0 — Smoke + Fire

Builds on v1.0.0 by promoting fire to a first-class simulation type. The v0.7.0
Fire Parameters (today domain-level params) become a full section with:

- emitter fire modes (`flow_type='FIRE'` / `'BOTH'`),
- fire-specific cache management,
- fire-specific text overlays,
- fire-only / smoke-only / both render modes.

---

## v3.0.0 — Smoke + Fire + Fluid (liquid)

Adds liquid domain support (`domain_type='LIQUID'`):

- mesh + particles cache layers,
- liquid-specific batch params (surface tension, viscosity, diffusion, particle
  radius / number),
- liquid render modes (mesh + flip particles),
- corresponding worker bake stages.

---

## Cross-cutting themes (apply across versions)

- **Cache-collision safety** — every new batchable param must appear in
  `make_name()` so two jobs differing only in that param don't share a cache
  dir (the BUG-013 / BUG-014 family). New params follow the
  defaults-suppressed encoding so older cache names stay valid.
- **Estimate accuracy** — each new cost driver (samples, noise up-res, emitter
  density, fire, liquid particles) needs a term in the time model + a
  calibration batch (see TODO-51).
- **Testing** — every new function gets a test; every bug gets a regression
  test in the same commit.
