r"""
bg_resume_probe_v2.py — test the "save tmp .blend with cache_directory pointing
at a populated dir, then reload" resume approach (TODO-35; BUG-010).

REFINED 5-STEP FLOW (per user's "we need step 3.5"):
  1. Move existing cache files to a separate PRESERVE dir (out of harm's way).
  2. Assign d.cache_directory = target_dir (target_dir is empty at this point,
     so BUG-004's wipe-on-assignment has nothing to destroy).
  3. Save the .blend as a tmp .blend (captures cache_directory = target_dir).
  3.5. **Move the preserved files INTO target_dir.**  After the save, before
     the reload — so the .blend's recorded cache_directory will point at a
     populated dir when the next Blender opens it.
  4. Open the tmp .blend in a fresh --background Blender and bake_all() to a
     higher frame_end.  Mantaflow's init scan at LOAD time *should* detect the
     existing frames (this is how the UI "Resume Bake" works).

The first probe (bg_resume_probe.py) tested in-process presave/merge/bake
without a reload: REBAKED-FROM-1.  This probe is the untested variant.

HOW TO RUN — via the companion wrapper:
    scripts\experiments\run_bg_probe_v2.bat

Three Blender invocations sequentially (--background --factory-startup):

    PHASE      BLEND opened           WHAT IT DOES
    setup      <original .blend>      Bakes 100 fresh frames into <cache_dir>.
    prepare    <original .blend>      Steps 1-3.5: stash files in PRESERVE,
                                       assign cache_directory = target_dir
                                       (empty), save tmp.blend, then move
                                       PRESERVE → target_dir (step 3.5).
    test       <tmp.blend>            Bakes to 200; reports per-frame mtime
                                       preservation; prints final VERDICT.

Verdicts (from STEP C output):
  RESUMED         — frames 1-100 kept their mtimes; only 101-200 baked.
                    Mantaflow's load-time scan DID detect the existing cache.
                    Closes BUG-010 — worker RESUME branch can adopt this dance.
  REBAKED-FROM-1  — frames 1-100 rewritten.  Even with the populated target_dir
                    at load time, Mantaflow's init didn't pick up the cache.
  WIPED           — target_dir empty by test time.  Check STEP B output: did
                    the assign wipe the files we'd just placed?  (Unlikely
                    given the 3.5 step puts them in AFTER save, but possible.)
  NO PRIOR FRAMES — target_dir empty by test time, prepare reported nothing
                    moved.  Check STEP B output.
"""
import os
import re
import shutil
import sys
import time

import bpy

# Args after "--": <phase> <phase-specific args...>
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if not argv:
    print("[probe_v2] usage: -- <setup|prepare|test> <args>")
    sys.exit(2)
phase = argv[0]


# ── Helpers ──────────────────────────────────────────────────────────────────
_VDB_RE = re.compile(r"_(\d+)\.vdb$", re.IGNORECASE)


def _find_domain(name=""):
    obj = bpy.data.objects.get(name) if name else None
    if obj is None:
        for o in bpy.data.objects:
            if any(m.type == 'FLUID' and m.fluid_type == 'DOMAIN' for m in o.modifiers):
                obj = o
                break
    if obj is None:
        return None, None
    d = next((m.domain_settings for m in obj.modifiers
              if m.type == 'FLUID' and m.fluid_type == 'DOMAIN'), None)
    return obj, d


def _frame_mtimes(folder):
    """Return {frame_number: mtime} for fluid_data_*.vdb directly under folder."""
    out = {}
    if not os.path.isdir(folder):
        return out
    for f in os.listdir(folder):
        m = _VDB_RE.search(f)
        if m:
            try:
                out[int(m.group(1))] = os.path.getmtime(os.path.join(folder, f))
            except OSError:
                pass
    return out


def _say(*xs):
    print("[probe_v2 " + phase + "]", *xs, flush=True)


# ── Phase: SETUP — bake N frames fresh into <cache> ──────────────────────────
if phase == "setup":
    if len(argv) < 3:
        _say("usage: -- setup <cache_dir> <frame_end> [domain_name]")
        sys.exit(2)
    cache_dir   = os.path.abspath(argv[1])
    frame_end   = int(argv[2])
    domain_name = argv[3] if len(argv) > 3 else ""

    obj, d = _find_domain(domain_name)
    if d is None:
        _say("ERROR: no fluid DOMAIN found")
        sys.exit(1)

    # Fast + deterministic
    d.resolution_max     = 16
    d.use_noise          = False
    d.use_dissolve_smoke = False
    d.cache_data_format  = 'OPENVDB'
    d.cache_frame_start  = 1
    d.cache_frame_end    = frame_end
    try:
        d.cache_resumable = True
    except AttributeError:
        pass

    # Start fresh
    if os.path.isdir(cache_dir):
        shutil.rmtree(cache_dir, ignore_errors=True)
    os.makedirs(cache_dir, exist_ok=True)

    d.cache_directory = cache_dir
    bpy.context.view_layer.update()
    time.sleep(1.0)

    _say(f"baking 1-{frame_end} into {cache_dir!r}")
    t0 = time.time()
    result = bpy.ops.fluid.bake_all()
    _say(f"bake result={result}  took {time.time() - t0:.1f}s")

    files = _frame_mtimes(os.path.join(cache_dir, "data"))
    _say(f"cache has {len(files)} frames after setup")
    if len(files) != frame_end:
        _say(f"WARNING: expected {frame_end} frames, got {len(files)}")


# ── Phase: PREPARE — 5 steps (1, 2, 3, 3.5; user's refined proposal) ────────
elif phase == "prepare":
    if len(argv) < 5:
        _say("usage: -- prepare <orig_cache_dir> <preserve_dir> <target_cache_dir> "
             "<tmp_blend> [domain_name]")
        sys.exit(2)
    orig_cache_dir   = os.path.abspath(argv[1])
    preserve_dir     = os.path.abspath(argv[2])
    target_cache_dir = os.path.abspath(argv[3])
    tmp_blend        = os.path.abspath(argv[4])
    domain_name      = argv[5] if len(argv) > 5 else ""

    obj, d = _find_domain(domain_name)
    if d is None:
        _say("ERROR: no fluid DOMAIN found")
        sys.exit(1)

    _say(f"d.cache_directory at script start (from .blend): {d.cache_directory!r}")

    # STEP 1: Move existing cache files OUT of the way into PRESERVE dir.
    if os.path.isdir(preserve_dir):
        _say(f"cleaning stale preserve {preserve_dir!r}")
        shutil.rmtree(preserve_dir, ignore_errors=True)
    if not os.path.isdir(orig_cache_dir):
        _say(f"ERROR: orig cache dir doesn't exist: {orig_cache_dir!r}")
        sys.exit(1)
    _say(f"STEP 1: rename {orig_cache_dir} → {preserve_dir}")
    os.rename(orig_cache_dir, preserve_dir)
    preserved = _frame_mtimes(os.path.join(preserve_dir, "data"))
    _say(f"STEP 1 done: {len(preserved)} frames stashed in preserve_dir")

    # STEP 2: Assign d.cache_directory = target_cache_dir.  Target is EMPTY
    # (or non-existent) so BUG-004's wipe-on-assign has nothing to destroy.
    if os.path.isdir(target_cache_dir):
        _say(f"cleaning stale target {target_cache_dir!r}")
        shutil.rmtree(target_cache_dir, ignore_errors=True)
    os.makedirs(target_cache_dir, exist_ok=True)
    _say(f"STEP 2: assigning d.cache_directory = {target_cache_dir!r} (empty)")
    files_before_assign = _frame_mtimes(os.path.join(target_cache_dir, "data"))
    d.cache_directory = target_cache_dir
    files_after_assign = _frame_mtimes(os.path.join(target_cache_dir, "data"))
    _say(f"STEP 2 done: target {len(files_before_assign)} → {len(files_after_assign)} "
         f"(wipe was harmless on empty dir)")

    # STEP 3: save as tmp .blend (copy=True keeps current session on the original).
    _say(f"STEP 3: save .blend as {tmp_blend!r}")
    bpy.ops.wm.save_as_mainfile(filepath=tmp_blend, copy=True)
    _say(f"STEP 3 done: tmp.blend present={os.path.isfile(tmp_blend)}")

    # STEP 3.5: Move preserved files INTO target_cache_dir.  After the .blend
    # save, before any reload — so the .blend's recorded cache_directory will
    # point at a populated dir when the next Blender opens it.
    _say(f"STEP 3.5: move preserve → target ({preserve_dir} → {target_cache_dir})")
    _moved = 0
    if os.path.isdir(preserve_dir):
        for _item in os.listdir(preserve_dir):
            _src = os.path.join(preserve_dir, _item)
            _dst = os.path.join(target_cache_dir, _item)
            # os.replace handles file-or-dir overwrite atomically per item;
            # for subdirs (data/, config/, …) use shutil.move which copies
            # contents into existing dst if present.
            if os.path.isdir(_src):
                # data/ likely already exists (Mantaflow may have created it
                # on assign); merge contents.
                os.makedirs(_dst, exist_ok=True)
                for _f in os.listdir(_src):
                    os.replace(os.path.join(_src, _f), os.path.join(_dst, _f))
                    _moved += 1
                shutil.rmtree(_src, ignore_errors=True)
            else:
                os.replace(_src, _dst)
                _moved += 1
        shutil.rmtree(preserve_dir, ignore_errors=True)
    files_after_move = _frame_mtimes(os.path.join(target_cache_dir, "data"))
    _say(f"STEP 3.5 done: moved {_moved} file(s); target now has "
         f"{len(files_after_move)} frame(s)")

    if len(files_after_move) == 0:
        _say("VERDICT (early): NO PRIOR FRAMES — target empty after move-back; "
             "check the preserve→target step.")


# ── Phase: TEST — opens tmp.blend; bake_all to a higher frame_end ────────────
elif phase == "test":
    if len(argv) < 3:
        _say("usage: -- test <tmp_cache_dir> <target_frame_end>")
        sys.exit(2)
    tmp_cache_dir = os.path.abspath(argv[1])
    target        = int(argv[2])

    obj, d = _find_domain("")
    if d is None:
        _say("ERROR: no fluid DOMAIN after reload")
        sys.exit(1)

    _say(f"loaded blend's d.cache_directory = {d.cache_directory!r}")
    try:
        _say(f"d.cache_resumable = {d.cache_resumable}")
    except AttributeError:
        pass
    try:
        _say(f"d.cache_frame_pause_data = {d.cache_frame_pause_data}")
    except AttributeError:
        pass

    before = _frame_mtimes(os.path.join(tmp_cache_dir, "data"))
    _say(f"before bake: {len(before)} frames in tmp cache")
    if not before:
        _say("VERDICT: NO PRIOR FRAMES — nothing to evaluate. Check the prepare-phase output above.")
        sys.exit(0)

    d.cache_frame_start = 1
    d.cache_frame_end   = target
    try:
        d.cache_resumable = True
    except AttributeError:
        pass
    bpy.context.view_layer.update()
    time.sleep(1.0)

    _say(f"baking 1-{target} ...")
    t0 = time.time()
    result = bpy.ops.fluid.bake_all()
    dt = time.time() - t0
    after = _frame_mtimes(os.path.join(tmp_cache_dir, "data"))
    _say(f"bake result={result}  took {dt:.1f}s; cache now has {len(after)} frames "
         f"(expected {target})")

    EPS = 0.5
    preserved = [n for n, mt in before.items()
                 if n in after and abs(after[n] - mt) <= EPS]
    rewritten = [n for n in before if n in after and n not in preserved]
    missing   = [n for n in before if n not in after]
    _say(f"of {len(before)} prior frames: {len(preserved)} preserved, "
         f"{len(rewritten)} rewritten, {len(missing)} missing")
    if rewritten:
        _say(f"  rewritten sample: {sorted(rewritten)[:10]}")

    if len(missing) > 0.5 * len(before):
        _say("VERDICT: WIPED — most prior frames vanished. Inspect prepare output.")
    elif len(preserved) >= 0.9 * len(before):
        _say("VERDICT: RESUMED — Mantaflow detected existing frames at LOAD time "
             "and continued from where they left off.  TODO-35 path works.")
    else:
        _say("VERDICT: REBAKED-FROM-1 — Mantaflow re-baked the existing frames "
             "despite the load-time scan. The save+reload tmp-blend approach "
             "does NOT give true partial resume.")


else:
    _say(f"unknown phase: {phase!r}")
    sys.exit(2)
