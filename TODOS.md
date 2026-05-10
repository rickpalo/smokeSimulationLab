# SmokeSimLab — Pending Issues

Items to address once file synchronization catches up (~5,000 PNGs behind as of 2026-05-06).

---

## ~~TODO-1~~: Crash log written to jobs folder — **DONE** (already implemented in launcher)

---

## TODO-2: Retry job does not find partial bake cache from crashed job

**Observed behaviour:**  
A job crashed approximately halfway through baking.  The crash was correctly
detected and the `.crashed` marker was written.  When the batch was retried
(`auto_retry_failed` or manual retry), the retry job reported no existing cache
and started a full rebake from frame 1, discarding the ~50% already baked.

**Expected behaviour:**  
The retry should detect the partial VDB cache and resume baking from the last
good frame (this is what `use_existing_cache=True` + `cache_resumable=True`
is designed to do).

**Likely root causes to investigate:**
1. Cache directory name mismatch between the original job and the retry job —
   `make_name()` appends a run index; if the retry produces a different name
   the cache lookup misses entirely.
2. The worker's cache completeness check uses `frame_start`…`frame_end`; if the
   crash left the cache directory in a partially-written state the check may
   return "no cache" rather than "partial cache".
3. Confirm `d.cache_resumable = True` is actually being set before `bake_all()`
   on the retry path (check worker log for the "Resumable cache enabled" line).

**Files to investigate:** `scripts/SmokeSimLab/smoke_worker.py` (bake logic,
cache completeness check), `scripts/SmokeSimLab/__init__.py` (`make_name`,
`SMOKE_OT_retry_failed`).

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

