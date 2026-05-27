"""
bg_resume_probe.py — does Mantaflow resume a partial bake in --background mode?
==============================================================================

Background
----------
SmokeSimLab's scripted RESUME re-bakes from frame 1 every time (confirmed by VDB
mtimes), so an interrupted high-res bake redoes all the frames it already had.
The v0.2.32 "save/reload to trigger resume" trick hung bake_all() in *windowed*
(EEVEE) mode and was removed in v0.3.1. The open question: in *--background*
mode (no UI event loop, bake_all runs synchronously) does Mantaflow resume from
existing cache frames the way the interactive "Resume Bake" button does?

This probe replicates the worker's RESUME setup (presave-rename + merge, so the
cache_directory reassignment can't wipe the data) and then bakes, and reports —
via file mtimes — whether the already-present frames were PRESERVED (true
resume) or REWRITTEN (re-bake from frame 1).

How to run (twice, at res 16 so it's seconds)
----------------------------------------------
Use a throwaway cache dir. Run 1 creates a partial cache; run 2 is the test.

    blender --background "E:\\...\\SmokeSimulatorForPiazzoSanMarco.blend" ^
        --python bg_resume_probe.py -- "E:\\...\\bg_probe_cache" 100 "Smoke Domain"

    blender --background "E:\\...\\SmokeSimulatorForPiazzoSanMarco.blend" ^
        --python bg_resume_probe.py -- "E:\\...\\bg_probe_cache" 200 "Smoke Domain"

Read the VERDICT line from run 2:
  RESUMED         -> frames 1-100 untouched, only 101-200 baked. Background
                     resume works -> justifies the two-phase (bg-bake) redesign.
  REBAKED-FROM-1  -> frames 1-100 rewritten. Background doesn't help resume;
                     redesign would buy efficiency only.
  WIPED-ON-ASSIGN -> assigning cache_directory deleted the data even after
                     presave/merge (unexpected; tells us the merge needs work).
"""
import os
import re
import sys
import time

import bpy

# ── Parse args after "--" ────────────────────────────────────────────────────
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(argv) < 2:
    print("[bg_probe] Usage: -- <cache_dir> <frame_end> [domain_name]")
    sys.exit(2)
cache_dir   = os.path.abspath(argv[0])
frame_end   = int(argv[1])
domain_name = argv[2] if len(argv) > 2 else ""
data_dir    = os.path.join(cache_dir, "data")

_VDB_RE = re.compile(r"_(\d+)\.vdb$", re.IGNORECASE)


def frame_mtimes(folder):
    """Return {frame_number: mtime} for fluid_data_*.vdb directly under folder/."""
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


# ── Locate the fluid domain ───────────────────────────────────────────────────
obj = bpy.data.objects.get(domain_name) if domain_name else None
if obj is None:
    for o in bpy.data.objects:
        if any(m.type == 'FLUID' and m.fluid_type == 'DOMAIN' for m in o.modifiers):
            obj = o
            break
if obj is None:
    print("[bg_probe] ERROR: no fluid DOMAIN object found")
    sys.exit(1)
d = next(m.domain_settings for m in obj.modifiers
         if m.type == 'FLUID' and m.fluid_type == 'DOMAIN')

print(f"[bg_probe] background={bpy.app.background}  blender={bpy.app.version_string}")
print(f"[bg_probe] domain={obj.name!r}  cache_dir={cache_dir!r}  frame_end={frame_end}")

# ── Keep it fast + deterministic ──────────────────────────────────────────────
d.resolution_max   = 16
d.use_noise        = False
d.use_dissolve_smoke = False
d.cache_data_format = 'OPENVDB'
d.cache_frame_start = 1
d.cache_frame_end   = frame_end
try:
    d.cache_resumable = True
except AttributeError:
    pass
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end   = frame_end

# ── Snapshot existing frames, then point the domain at the cache the way the
#    worker does (presave-rename so the reassignment can't wipe the data) ───────
before = frame_mtimes(data_dir)
print(f"[bg_probe] existing frames before assign: {len(before)}"
      + (f" (spans {min(before)}-{max(before)})" if before else ""))

presave = cache_dir + "_probe_presave"
did_presave = False
if before:
    if os.path.isdir(presave):
        import shutil; shutil.rmtree(presave, ignore_errors=True)
    os.rename(cache_dir, presave)
    did_presave = True

os.makedirs(cache_dir, exist_ok=True)
d.cache_directory = cache_dir
bpy.context.view_layer.update()
time.sleep(1.0)

if did_presave:
    # Merge the presaved VDB data files back into the fresh dir (skip config/),
    # exactly like the worker's RESUME merge.  os.replace preserves mtimes.
    import shutil
    moved = 0
    for proot, _pdirs, pfiles in os.walk(presave):
        if os.path.basename(proot) == "config":
            continue
        rel = os.path.relpath(proot, presave)
        dst = os.path.join(cache_dir, rel)
        os.makedirs(dst, exist_ok=True)
        for pf in pfiles:
            if re.search(r"_\d+\.(vdb|uni)$", pf, re.IGNORECASE):
                os.replace(os.path.join(proot, pf), os.path.join(dst, pf))
                moved += 1
    shutil.rmtree(presave, ignore_errors=True)
    print(f"[bg_probe] merged {moved} presaved data file(s) back")

merged = frame_mtimes(data_dir)
if before and not merged:
    print("[bg_probe] VERDICT: WIPED-ON-ASSIGN "
          "(cache_directory assignment destroyed the data despite presave)")
    sys.exit(0)

# ── Bake and time it ──────────────────────────────────────────────────────────
print(f"[bg_probe] baking 1-{frame_end} ...")
t0 = time.time()
result = bpy.ops.fluid.bake_all()
dt = time.time() - t0
after = frame_mtimes(data_dir)
print(f"[bg_probe] bake result={result}  took {dt:.1f}s  "
      f"frames now={len(after)} (expected {frame_end})")

# ── Verdict: were the pre-existing frames preserved or rewritten? ─────────────
if not before:
    print(f"[bg_probe] VERDICT: BASELINE — no prior frames; baked 1-{frame_end} "
          f"fresh. Re-run with a larger frame_end to test resume.")
    sys.exit(0)

EPS = 0.5  # seconds; mtime considered "changed" if it advanced by more than this
preserved = [n for n, mt in before.items()
             if n in after and abs(after[n] - mt) <= EPS]
rewritten = [n for n in before if n in after and n not in preserved]
print(f"[bg_probe] of {len(before)} prior frames: "
      f"{len(preserved)} preserved, {len(rewritten)} rewritten")
if rewritten:
    print(f"[bg_probe]   rewritten sample: {sorted(rewritten)[:10]}")

if len(preserved) >= 0.9 * len(before):
    print("[bg_probe] VERDICT: RESUMED — prior frames kept their mtimes; only the "
          "missing frames were baked. Background resume WORKS.")
else:
    print("[bg_probe] VERDICT: REBAKED-FROM-1 — prior frames were rewritten. "
          "Background does not give partial resume.")
