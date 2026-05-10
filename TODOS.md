# SmokeSimLab — Pending Issues

Items to address once file synchronization catches up (~5,000 PNGs behind as of 2026-05-06).

---

## ~~TODO-1~~: Crash log written to jobs folder — **DONE** (already implemented in launcher)

---

## ~~TODO-2~~: Retry job does not find partial bake cache — **DONE** (v0.2.3)

**Root cause (confirmed from job_0000_retry.log):**  
The `has_files` candidate check used `f.endswith('.vdb') or f.endswith('.uni')`,
which matches Mantaflow's config/metadata `.uni` files (e.g. `fluidsimulation.uni`)
that are created immediately on domain init — before any simulation frames are
baked.  A candidate directory with only config files passed `has_files = True`,
was selected as the effective cache dir, then produced empty `baked_frames` in
the frame-counting walk (those config files don't match `_\d+\.(vdb|uni)$`).
The code then fell into the full-rebake else-branch, switched back to the job's
own cache dir, called `bpy.ops.fluid.free_all()`, and **destroyed the complete
cache** before rebaking from scratch.

For job_0000 specifically: the bake was complete when it crashed (crash was on
render frame 0302). The `_0000` cache had all 500 VDB frames, but the retry
found `_0030` (config files only), fell through to full rebake, freed `_0000`,
and rebaked all 500 frames unnecessarily.

**Fix:** Changed the `has_files` check to use the same frame-number regex as
the frame-counting walk (`re.search(r'_\d+\.(vdb|uni)$', f)`), so config-only
directories are skipped and only candidates with actual simulation frame data
are accepted.

---

## ~~TODO-3~~: "Utilities" collapsible section — **DONE** (implemented in v0.1.x)

---

## ~~TODO-4~~: Hide Job Log section when not populated — **DONE** (v0.2.0)

---

## TODO-5: Job Log row goes blank after scrolling or job start

**Observed behaviour:**  
Job 1 is visible immediately after the job log is populated (Export Batch).
After either (a) scrolling the list to the bottom, or (b) the first job starts
running, the row for job 1 becomes blank (empty job number and name).

**Suspected causes (investigate in order):**

1. **`job_log_index` scroll interaction** — Blender's `template_list` tracks the
   active-item index in `job_log_index` (default 0 → item 0 = job 1).  When the
   user scrolls the viewport, Blender may advance `job_log_index` to keep it
   within the visible window, leaving item 0 "selected but off-screen."  If the
   list then redraws before the scroll settles, item 0 can flicker blank.
   *Try*: initialise `job_log_index = -1` (or 0 but with a guard in `draw_item`)
   so no item starts selected.

2. **Timer–draw race condition** — `_update_job_log_statuses` is called from the
   poll timer (a background thread context).  Writing to a `CollectionProperty`
   item's properties while Blender is mid-draw can cause a partial RNA
   invalidation; the item is still present but its `job_number` / `job_name`
   StringProperty/IntProperty values read as defaults (0 / "").
   *Try*: guard the poll call with `_redraw_panels()` only *after* all item
   writes are complete, and confirm the writes are atomic from Blender's
   perspective.

3. **`_item` name shadowing in `export_batch`** — the seed loop uses the local
   name `_item` for each `job_log_items.add()` result.  After the loop exits,
   `_item` still references the last item added.  If any later code in
   `export_batch` accidentally reassigns or clears through `_item`, item 0
   could be corrupted.  *Try*: rename the loop variable to `_log_row` to avoid
   any ambiguity.

4. **`make_name` differs between seed loop and job loop** — the seed loop and the
   per-job JSON loop both call `make_name(p, i)` independently.  If `make_name`
   is non-deterministic (e.g. depends on mutable state), the stored name may not
   match.  *Try*: compute `name = make_name(p, i)` once per iteration and reuse
   it in both places.

**Recommended first step:**  
Add `print(f"draw_item: job_number={item.job_number!r} job_name={item.job_name!r}")` at the top of `SMOKE_UL_job_log.draw_item` and reproduce; if the blank row prints `job_number=0, job_name=''`, the item's properties are genuinely zeroed (cause 2 or 3).  If the row is never printed at all, the item is scrolled out of the viewport (cause 1).

**Files to investigate:** `scripts/SmokeSimLab/__init__.py`
(`export_batch` seed loop, `_update_job_log_statuses`, `SMOKE_UL_job_log.draw_item`, `SMOKE_PT_panel.draw` `template_list` call).

---

## ~~TODO-6~~: Auto-scroll Job Log — **DONE** (v0.2.2)

---

## ~~TODO-7~~: Update default parameter values — **DONE** (all defaults were already correct)

---

## ~~TODO-8~~: Fix negative/zero RT_proj values — **DONE** (v0.2.1)

---

## ~~TODO-9~~: Sort exported jobs by resolution ascending — **DONE** (v0.2.1)

---

## ~~TODO-9~~: analyze_perf.py render tables — **DONE** (v0.2.1)

---

## ~~TODO-10~~: Debug log — **DONE** (v0.2.2)

---

