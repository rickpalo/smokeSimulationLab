"""
smoke_worker.py
===============
Headless per-job worker for SmokeSimLab.

Called by run_smoke_batch.bat as:
    blender.exe "<blend>" --background --factory-startup
        --python "<path>/smoke_worker.py" -- "<path>/job_NNNN.json"

Keep this file in the SmokeSimLab addon folder alongside __init__.py.
export_batch() copies it to the output folder automatically.

Applies fluid parameters, bakes, renders playblast MP4 + final still PNG,
appends a row to Renders/results.csv, then quits Blender.
"""

WORKER_VERSION = "0.4.2"

import bpy
import sys
import os
import re
import json
import time as _time
import datetime
import atexit
import subprocess
import shutil

sys.stdout.reconfigure(line_buffering=True)

# _log_file is opened after the job config is loaded (it carries the path).
_log_file = None

def _log(msg):
    """Write msg to stdout (batch window) and the per-job log file."""
    print(msg)
    if _log_file is not None:
        _log_file.write(msg + "\n")
        _log_file.flush()




def _close_log():
    """Flush and close the per-job log file. Registered with atexit so it runs
    on all exit paths — sys.exit(), unhandled exception, and clean quit."""
    global _log_file
    if _log_file is not None:
        try:
            _log_file.flush()
            _log_file.close()
        except OSError:
            pass
        _log_file = None

atexit.register(_close_log)


# ---------------------------------------------------------------------------
# Performance data logging
# ---------------------------------------------------------------------------

def _append_perf_record(output_path, record):
    """Append one performance record to <output_path>/perf_log.json.

    Each record captures timing data from a single job run so the caller can
    later fit the scaling constants:
      bake_secs ≈ K_bake  × resolution³ × frames
      render_secs ≈ K_render × width × height × frames
    """
    perf_path = os.path.join(output_path, "perf_log.json")
    records = []
    if os.path.exists(perf_path):
        try:
            with open(perf_path, "r", encoding="utf-8") as fh:
                records = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    record["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")
    records.append(record)
    try:
        with open(perf_path, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
    except OSError as e:
        _log(f"  WARNING: could not write perf_log.json: {e}")


# ---------------------------------------------------------------------------
# Cache bake-time sidecar
# ---------------------------------------------------------------------------
# Stored at <cache_dir>/bake_time.json so the time travels with the cache.
# Read on SKIP BAKE to show the original bake time in the rendered text
# overlay instead of "Bake: 0 sec"; updated on FULL or RESUME bake so future
# reuses can read it.  For RESUME we accumulate (prev + this run) since the
# total time it took to produce the current cache is what's interesting.

_BAKE_TIME_FILENAME = "bake_time.json"

def _read_stored_bake_time(cache_dir):
    """Return prior bake_seconds for this cache, or None if no sidecar."""
    path = os.path.join(cache_dir, _BAKE_TIME_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            return float(json.load(fh)["bake_seconds"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _write_stored_bake_time(cache_dir, bake_seconds, frames, resolution):
    """Persist the cumulative bake_seconds for this cache as a sidecar."""
    path = os.path.join(cache_dir, _BAKE_TIME_FILENAME)
    record = {
        "bake_seconds": round(float(bake_seconds), 2),
        "frames":       int(frames),
        "resolution":   int(resolution),
        "timestamp":    datetime.datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    except OSError as e:
        _log(f"  WARNING: could not write {_BAKE_TIME_FILENAME}: {e}")


# ---------------------------------------------------------------------------
# Text object helpers
# ---------------------------------------------------------------------------

def _set_text(obj_name, value_str):
    """Set a FONT object's body text, with error reporting."""
    if not obj_name:
        return
    obj = bpy.data.objects.get(obj_name)
    if obj and obj.type == 'FONT':
        obj.data.body = value_str
        _log(f"  Text '{obj_name}' -> '{value_str}'")
    else:
        _log(f"  WARNING: '{obj_name}' not found or not a FONT object")


def update_text_objects(text_map, params, bake_seconds=None):
    """
    Update scene FONT objects with current job parameter values.

    Parameters
    ----------
    text_map     : dict mapping keys ('resolution','noise','dissolve','time')
                   to Blender object names
    params       : job parameter dict from JSON
    bake_seconds : elapsed bake time in seconds, or None (omits time update)
    """
    # Resolution + gas parameters
    _set_text(text_map.get("resolution", ""),
              f"Res: {int(params['resolution'])}\n"
              f"Vort: {round(float(params['vorticity']), 1)}, "
              f"Dens: {round(float(params['alpha']), 1)}, "
              f"Heat: {round(float(params['beta']), 1)}")

    # Noise — combined string or "Noise-None"
    if params["use_noise"]:
        noise_str = (
            f"Noise: U-{int(params['noise_upres'])} | "
            f"St-{round(params['noise_strength'], 2)} | "
            f"Scale-{round(params['noise_spatial_scale'], 2)}"
        )
    else:
        noise_str = "Noise-None"
    _set_text(text_map.get("noise", ""), noise_str)

    # Dissolve — combined string or "Dissolve-None"
    if params["use_dissolve"]:
        slow = "Yes" if params["slow_dissolve"] else "No"
        dissolve_str = (
            f"Dissolve:  Time: {int(params['dissolve_speed'])} | "
            f"Slow-{slow}"
        )
    else:
        dissolve_str = "Dissolve-None"
    _set_text(text_map.get("dissolve", ""), dissolve_str)

    # Bake time — only written after bake completes
    if bake_seconds is not None:
        hrs  = int(bake_seconds // 3600)
        mins = int((bake_seconds % 3600) // 60)
        secs = int(bake_seconds % 60)
        if hrs > 0:
            time_str = f"Bake: {hrs} hrs {mins} min"
        elif mins > 0:
            time_str = f"Bake: {mins} min {secs} sec"
        else:
            time_str = f"Bake: {secs} sec"
        _set_text(text_map.get("time", ""), time_str)


# ---------------------------------------------------------------------------
# Render engine helpers
# ---------------------------------------------------------------------------

def enable_gpu_rendering(scene):
    """
    Enable Cycles GPU rendering (OptiX > CUDA > HIP), fall back to CPU.
    Safe to call with --factory-startup (guards against missing cycles addon).
    """
    prefs = bpy.context.preferences

    if 'cycles' not in prefs.addons:
        _log("  Cycles addon not available — using CPU")
        scene.cycles.device = 'CPU'
        return False

    cycles_prefs = prefs.addons['cycles'].preferences

    for device_type in ('OPTIX', 'CUDA', 'HIP'):
        try:
            cycles_prefs.compute_device_type = device_type
            cycles_prefs.refresh_devices()
            devices = cycles_prefs.get_devices_for_type(device_type)
            if devices:
                for device in devices:
                    device.use = True
                scene.cycles.device = 'GPU'
                _log(f"  GPU rendering enabled: {device_type}")
                return True
        except Exception as e:
            _log(f"  {device_type} not available: {e}")
            continue

    _log("  No GPU available — falling back to CPU")
    scene.cycles.device = 'CPU'
    return False


def setup_eevee(scene):
    """
    Switch to EEVEE for rendering.  Returns True if EEVEE is available,
    False if it falls back to Cycles CPU.

    EEVEE requires an OpenGL context which is not available in --background
    mode.  This function is provided for windowed mode use (no --background).
    In background mode use setup_cycles() instead.
    """
    available = scene.render.bl_rna.properties['engine'].enum_items.keys()
    if 'BLENDER_EEVEE_NEXT' in available:
        scene.render.engine = 'BLENDER_EEVEE_NEXT'
        _log("  Render engine: EEVEE Next")
        return True
    elif 'BLENDER_EEVEE' in available:
        scene.render.engine = 'BLENDER_EEVEE'
        _log("  Render engine: EEVEE")
        return True
    _log("  EEVEE not available — falling back to Cycles")
    return False


def setup_cycles(scene, samples=32):
    """Switch to Cycles and enable GPU, with given sample count."""
    scene.render.engine  = 'CYCLES'
    scene.cycles.samples = samples
    enable_gpu_rendering(scene)


# ---------------------------------------------------------------------------
# Parse CLI arguments
# ---------------------------------------------------------------------------

argv = sys.argv
try:
    sep      = argv.index("--")
    job_path = argv[sep + 1]
except (ValueError, IndexError):
    _log("ERROR: expected path to job JSON after --")
    sys.exit(1)

# Optional --phase {bake,render,both} for the two-phase pipeline.  Accepts
# "--phase bake" or "--phase=bake" among the args after the job JSON.  Default
# "both" = bake + render in one process (the original single-pass behavior).
phase = "both"
_phase_args = argv[sep + 2:]
for _i, _tok in enumerate(_phase_args):
    if _tok.startswith("--phase="):
        phase = _tok.split("=", 1)[1]
    elif _tok == "--phase" and _i + 1 < len(_phase_args):
        phase = _phase_args[_i + 1]
phase = phase.strip().lower()
if phase not in ("bake", "render", "both"):
    phase = "both"
do_bake   = phase in ("bake", "both")
do_render = phase in ("render", "both")

with open(job_path) as fh:
    cfg = json.load(fh)

_log_path = cfg.get("log_path")
if _log_path:
    # Append mode so the bake-phase + render-phase processes share one log per
    # job in the two-pass pipeline (run_batch clears old .log files at run-start,
    # so the file is empty when bake phase opens it).
    _log_file = open(_log_path, "a", buffering=1)

p           = cfg["params"]
name        = cfg["name"]
output_path = cfg["output_path"]
domain_name = cfg["domain_name"]
frame_end   = cfg["frame_end"]
frame_start = cfg.get("frame_start", 1)
text_map    = cfg.get("text_objects", {})

# Render mode from job config — defaults to 'CYCLES'
# Set to 'EEVEE' in job JSON or export settings for windowed mode
render_mode        = cfg.get("render_mode", "CYCLES")
render_samples     = cfg.get("render_samples", 16)
use_placeholders   = cfg.get("use_placeholders", False)
use_existing_cache = cfg.get("use_existing_cache", False)
# Version that exported this job (stamped into perf_log/results.csv for later
# cross-version comparison); WORKER_VERSION is this script's own version.
addon_version      = cfg.get("addon_version", "?")
# Bake-only mode (TODO-26): when False, skip the MP4 + still render entirely.
# Defaults True so pre-TODO-26 job JSONs still render as before.
render_simulation_result = cfg.get("render_simulation_result", True)

# Emitter densities pre-computed at export time: {object_name: scaled_density}
emitter_densities       = cfg.get("emitter_densities", {})
collect_estimation_data = cfg.get("collect_estimation_data", False)
collect_debug_log       = cfg.get("collect_debug_log", False)

_debug_out = os.path.join(output_path, "debug_log.txt")

def _dlog(msg):
    """Append one timestamped line to debug_log.txt. No-op when flag is off."""
    if not collect_debug_log:
        return
    ts   = _time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  [worker/{name}]  {msg}"
    _log(line)
    try:
        with open(_debug_out, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass

render_dir = os.path.join(output_path, "Renders")
cache_dir  = os.path.join(output_path, "Cache", name)
os.makedirs(render_dir, exist_ok=True)
os.makedirs(cache_dir,  exist_ok=True)

_log(f"[{name}] Job started.")
_log(f"[{name}] Blender {bpy.app.version_string}  |  SmokeSimLab worker {WORKER_VERSION} (addon {addon_version})")
_log(f"[{name}] Phase: {phase}  (bake={do_bake}, render={do_render})")
_dlog(f"cfg: {cfg}")
_log(f"[{name}] Cache dir: {cache_dir}")
_log(f"[{name}] Render dir: {render_dir}")
_log(f"[{name}] Render mode: {render_mode}")

# ---------------------------------------------------------------------------
# Locate domain object and fluid modifier
# ---------------------------------------------------------------------------

_dlog(f"objects in scene: {[o.name for o in bpy.data.objects]}")
obj = bpy.data.objects.get(domain_name)
if not obj:
    _log(f'ERROR: object "{domain_name}" not found in scene')
    sys.exit(1)

mod = next((m for m in obj.modifiers if m.type == 'FLUID'), None)
if not mod:
    _log(f'ERROR: no Fluid modifier (type FLUID) on "{domain_name}" '
         f'(modifiers found: {[m.name for m in obj.modifiers]})')
    sys.exit(1)
_dlog(f"domain: {domain_name}  res={obj.modifiers[0].domain_settings.resolution_max if mod else '?'}  cache_dir={cache_dir}")

d = mod.domain_settings

# Fix-2 diagnostic: log d.cache_directory as the .blend was saved, before any
# worker assignment.  If across-batch reuse misses because Mantaflow nests
# files under a hashed sub-path (cache_fluid_<hash>/data/), the saved path
# will reveal that pattern.  Also log all top-level entries in the target
# cache dir so we can see what Mantaflow actually left there last time.
_log(f"[{name}] Domain cache_directory at startup (from .blend): {d.cache_directory!r}")
try:
    _existing = sorted(os.listdir(cache_dir)) if os.path.isdir(cache_dir) else []
    _log(f"[{name}] Target cache_dir top-level entries before any change: {_existing}")
    for _sub in _existing:
        _full = os.path.join(cache_dir, _sub)
        if os.path.isdir(_full):
            try:
                _kids = sorted(os.listdir(_full))[:6]
                _log(f"[{name}]   {_sub}/ -> {len(_kids)} entries (first few): {_kids}")
            except OSError:
                pass
except OSError as _e:
    _log(f"[{name}] (could not list cache_dir: {_e})")

# ---------------------------------------------------------------------------
# Apply simulation parameters
# ---------------------------------------------------------------------------

d.resolution_max     = int(p["resolution"])
d.vorticity          = float(p["vorticity"])
d.alpha              = float(p["alpha"])    # buoyancy density
d.beta               = float(p["beta"])     # buoyancy heat
d.use_dissolve_smoke = bool(p["use_dissolve"])
if p["use_dissolve"]:
    d.dissolve_speed         = int(p["dissolve_speed"])
    d.use_dissolve_smoke_log = bool(p["slow_dissolve"])
d.use_noise = bool(p["use_noise"])
if p["use_noise"]:
    d.noise_scale     = int(p["noise_upres"])
    d.noise_strength  = float(p["noise_strength"])
    d.noise_pos_scale = float(p["noise_spatial_scale"])

# Constrain the bake to this job's frame range.  bpy.ops.fluid.bake_all() bakes
# the domain's cache_frame_start/end, NOT the scene frame range — so without
# this the bake uses whatever range is saved in the .blend (e.g. 500 frames),
# ignoring the job's requested range and wasting enormous bake time / cache.
try:
    d.cache_frame_start = frame_start
    d.cache_frame_end   = frame_end
    _log(f"[{name}] Cache frame range set to {frame_start}-{frame_end}")
except (AttributeError, TypeError) as _e:
    _log(f"[{name}] WARNING: could not set cache frame range ({_e})")

# OpenVDB + Blosc: smaller files, faster reads, industry-standard format
d.cache_data_format  = 'OPENVDB'
d.cache_noise_format = 'OPENVDB'
try:
    d.openvdb_cache_compress_type = 'BLOSC'
except TypeError:
    d.openvdb_cache_compress_type = 'ZIP'

bpy.context.view_layer.objects.active = obj
obj.select_set(True)

# Let Mantaflow reinitialize with new settings before baking
bpy.context.view_layer.update()
_time.sleep(3.0)

# ---------------------------------------------------------------------------
# Apply pre-computed emitter densities (scaled at export time, not here)
# ---------------------------------------------------------------------------

if emitter_densities:
    for em_obj in bpy.data.objects:
        if em_obj.name not in emitter_densities:
            continue
        for mod in em_obj.modifiers:
            if mod.type == 'FLUID' and mod.fluid_type == 'FLOW':
                try:
                    old_dens = mod.flow_settings.density
                    mod.flow_settings.density = emitter_densities[em_obj.name]
                    _log(f"[{name}] Density: '{em_obj.name}' "
                         f"{old_dens:.4f} → {mod.flow_settings.density:.4f}")
                except AttributeError as exc:
                    _log(f"[{name}] WARNING: density set failed on '{em_obj.name}': {exc}")

# ---------------------------------------------------------------------------
# Update text objects (before bake — no time available yet)
# ---------------------------------------------------------------------------

update_text_objects(text_map, p)
_log(f"[{name}] Text objects updated (pre-bake).")

# ---------------------------------------------------------------------------
# Bake
# ---------------------------------------------------------------------------

_log(f"[{name}] --- Cache search ---")
_log(f"[{name}]   This job's cache dir : {cache_dir}")
_log(f"[{name}]   use_existing_cache   : {use_existing_cache}")

# Read the stored bake_time sidecar before any presave/rename touches the
# directory.  Kept in a Python variable so it survives even when RESUME
# discards _presave_dir (which doesn't move non-VDB files) or FULL BAKE
# rmtree's it entirely.
_prev_stored_bake = _read_stored_bake_time(cache_dir)
if _prev_stored_bake is not None:
    _log(f"[{name}]   Stored bake_time : {_prev_stored_bake:.1f}s (from prior bake)")

effective_cache_dir = cache_dir

def _count_data_files(directory):
    """Return the number of frame-numbered VDB/UNI data files in *directory*,
    excluding Mantaflow's config/ subdirectory (which holds per-frame checkpoint
    .uni files that look like data but contain no simulation output)."""
    count = 0
    for _root, _dirs, _fnames in os.walk(directory):
        if os.path.basename(_root) == 'config':
            continue
        count += sum(1 for f in _fnames if re.search(r'_\d+\.(vdb|uni)$', f))
    return count

if use_existing_cache:
    # Name is now derived purely from parameters, so every run with the same
    # params uses the exact same cache directory — no cross-job-number search
    # needed.  Just check if this job's own dir has usable data.
    n_data = _count_data_files(cache_dir)
    if n_data > 0:
        _log(f"[{name}]   data_files={n_data} — will attempt to reuse")
    else:
        n_cfg = sum(
            1 for _r, _d, _fs in os.walk(cache_dir)
            if os.path.basename(_r) == 'config'
            for f in _fs if re.search(r'_\d+\.uni$', f)
        )
        _log(f"[{name}]   Cache dir empty  config_uni={n_cfg} — will bake from scratch")
        # Fix-2 diagnostic: enumerate EVERY file under cache_dir, grouped by
        # extension and parent.  If our regex r'_\d+\.(vdb|uni)$' misses real
        # bake output (e.g. files Mantaflow named differently in a newer
        # Blender), this listing will surface them.
        _all = []
        for _root, _dirs, _files in os.walk(cache_dir):
            for _f in _files:
                _all.append(os.path.relpath(os.path.join(_root, _f), cache_dir))
        if _all:
            _log(f"[{name}]   Diagnostic: {len(_all)} file(s) present at cache_dir but none match regex:")
            for _p in _all[:20]:
                _log(f"[{name}]     {_p}")
            if len(_all) > 20:
                _log(f"[{name}]     ... and {len(_all)-20} more")
        else:
            _log(f"[{name}]   Diagnostic: cache_dir contains zero files of any kind")

_log(f"[{name}]   Effective cache dir  : {effective_cache_dir}")

# ---------------------------------------------------------------------------
# Pre-assignment presave
# ---------------------------------------------------------------------------
# Mantaflow physically deletes VDB files at the target path whenever
# d.cache_directory is assigned — even to the same value.  The path-equality
# guard below skips the assignment when paths are already equal (no wipe).
# When paths DIFFER the assignment is unavoidable, but we can protect
# existing data by renaming the target directory out of the way first.
#
# Restore strategy (chosen after the bake decision below):
#   SKIP BAKE  →  rename _presave_dir straight back           (instant, no I/O)
#   RESUME     →  move VDB files from _presave_dir into the
#                 newly-created effective_cache_dir            (keep fresh config/)
#   FULL BAKE  →  discard _presave_dir

_norm_cur = os.path.normcase(os.path.normpath(d.cache_directory))
_norm_eff = os.path.normcase(os.path.normpath(effective_cache_dir))
_paths_differ = (_norm_cur != _norm_eff)

_presave_dir    = effective_cache_dir + "_presave"
_presave_active = False

# Presave the existing cache when re-using it (use_existing_cache) OR when this
# is the render phase of the two-pass pipeline (do_bake is False — the cache was
# just baked in the bake-phase process and must NOT be wiped by Mantaflow's
# reinitialisation on cache_directory reassignment).  Without the render-phase
# guard, a use_existing_cache=False render-phase invocation wipes the bake-phase
# cache and renders empty smoke (observed in v0.4.0Test 2026-05-28).
if _paths_differ and (use_existing_cache or not do_bake):
    _existing_count = (
        _count_data_files(effective_cache_dir)
        if os.path.isdir(effective_cache_dir) else 0
    )
    if _existing_count > 0:
        _log(f"[{name}] Cache presave: paths differ — renaming existing cache to protect "
             f"{_existing_count} data file(s) from Mantaflow domain-reassignment wipe")
        _log(f"[{name}]   FROM : {effective_cache_dir}")
        _log(f"[{name}]   TO   : {_presave_dir}")
        if os.path.isdir(_presave_dir):
            _log(f"[{name}]   NOTE: stale presave directory found — removing it first")
            shutil.rmtree(_presave_dir, ignore_errors=True)
        try:
            os.rename(effective_cache_dir, _presave_dir)
            _presave_active = True
            _log(f"[{name}] Cache presave: rename complete — existing data is safe")
        except OSError as _e:
            _log(f"[{name}] WARNING: Cache presave rename failed ({_e}) — "
                 f"Mantaflow may wipe existing cache on reassignment")
    else:
        _log(f"[{name}] Cache presave: no existing data files at target path — presave not needed")

# Walk for baked_frames.  If presave is active the data is in _presave_dir;
# otherwise walk the target directory as usual.
_walk_dir = _presave_dir if _presave_active else effective_cache_dir
_log(f"[{name}] Walking for existing frames: {_walk_dir}")
baked_frames    = set()
_all_cache_files = []
for _root, _dirs, files in os.walk(_walk_dir):
    subdir = os.path.basename(_root)
    for f in files:
        _all_cache_files.append(f)
        if subdir == 'config':
            continue  # checkpoint files, not simulation data
        m = re.search(r'_(\d+)\.(vdb|uni)$', f)
        if m:
            baked_frames.add(int(m.group(1)))
_log(f"[{name}]   Found {len(baked_frames)} baked frame(s)")

# ---------------------------------------------------------------------------
# Assign cache directory
# ---------------------------------------------------------------------------
# If presave is active, the existing data has been moved aside so the
# assignment now initialises a fresh empty directory — nothing useful to wipe.
if _paths_differ:
    d.cache_directory = effective_cache_dir
    # Fix-2 diagnostic: read back d.cache_directory to confirm Mantaflow
    # accepted the value verbatim (some Blender builds rewrite it to an
    # absolute path with hash, breaking subsequent path-equality checks).
    _readback = d.cache_directory
    _log(f"[{name}] Domain cache_directory assigned → {effective_cache_dir}")
    if _readback != effective_cache_dir:
        _log(f"[{name}]   WARNING: read-back differs from assignment: {_readback!r}")
    if _presave_active:
        _log(f"[{name}]   Presaved data was moved aside; Mantaflow initialised a fresh empty directory")
    else:
        _log(f"[{name}]   No presave active — Mantaflow reinitialised the domain at this path")
else:
    _log(f"[{name}] Domain cache_directory unchanged (paths equal) — skipping assignment to preserve VDB files")

# Enable resumable baking so a mid-bake crash can be continued on the next run.
try:
    d.cache_resumable = True
except AttributeError:
    _log(f"[{name}] WARNING: cache_resumable property not found — partial resume may not work")

bpy.context.view_layer.update()
_time.sleep(2.0)

# ---------------------------------------------------------------------------
# Bake decision
# ---------------------------------------------------------------------------
_log(f"[{name}] --- Bake decision ---")
_log(f"[{name}]   Frame range needed : {frame_start}–{frame_end} "
     f"({frame_end - frame_start + 1} frames)")
_log(f"[{name}]   Data frames found  : {len(baked_frames)}")

if baked_frames:
    _min_f, _max_f = min(baked_frames), max(baked_frames)
    _missing = sorted(set(range(frame_start, frame_end + 1)) - baked_frames)
    _log(f"[{name}]   Frame data spans   : {_min_f}–{_max_f}")
    if _missing:
        _missing_preview = _missing[:10]
        _log(f"[{name}]   Missing frames     : {len(_missing)} "
             f"(first few: {_missing_preview}{'…' if len(_missing) > 10 else ''})")
    else:
        _log(f"[{name}]   Missing frames     : none — cache complete")
elif _all_cache_files:
    _log(f"[{name}]   WARNING: {len(_all_cache_files)} file(s) in cache dir but none are "
         f"data files (config/ only?) — first few: {_all_cache_files[:5]}")
else:
    _log(f"[{name}]   Cache dir is empty")

bake_complete = all(f in baked_frames for f in range(frame_start, frame_end + 1))

# rebaked_frames: frame numbers whose cache was RECOMPUTED this run.
# Existing renders for these frames must NOT be reused as placeholders.
rebaked_frames = set()

_bake_decision = None   # set to "SKIP" / "RESUME" / "FULL" by the branch that fires

if (use_existing_cache and bake_complete) or not do_bake:
    _bake_decision = "SKIP"
    if not do_bake:
        _log(f"[{name}]   Decision : SKIP BAKE — render phase (bake ran in phase 1)")
    else:
        _log(f"[{name}]   Decision : SKIP BAKE — all {frame_end - frame_start + 1} frames confirmed")
    if _presave_active:
        # Restore the presaved cache so the render engine can read the VDB files.
        # The render engine reads VDB files directly; Mantaflow's internal state
        # after the assignment is irrelevant for rendering.
        # Step 1: remove the fresh empty directory Mantaflow just created.
        _log(f"[{name}] SKIP BAKE restore: removing Mantaflow-created empty directory")
        _log(f"[{name}]   Removing : {effective_cache_dir}")
        try:
            if os.path.isdir(effective_cache_dir):
                shutil.rmtree(effective_cache_dir)
        except OSError as _e:
            _log(f"[{name}] ERROR: Could not remove empty cache directory ({_e})")
            sys.exit(1)
        # Step 2: rename presave back to the expected path.
        _log(f"[{name}] SKIP BAKE restore: renaming presave back to cache path")
        _log(f"[{name}]   FROM : {_presave_dir}")
        _log(f"[{name}]   TO   : {effective_cache_dir}")
        try:
            os.rename(_presave_dir, effective_cache_dir)
        except OSError as _e:
            _log(f"[{name}] ERROR: Could not rename presave back to cache path ({_e})")
            sys.exit(1)
        _restored_count = _count_data_files(effective_cache_dir)
        _log(f"[{name}] SKIP BAKE restore: complete — {_restored_count} data file(s) ready for render")
    bake_seconds = 0.0
    bake_skipped = True

elif use_existing_cache and baked_frames:
    _bake_decision = "RESUME"
    rebaked_frames = set(range(frame_start, frame_end + 1)) - baked_frames
    bake_skipped   = False
    _log(f"[{name}]   Decision : RESUME — {len(baked_frames)} frames present, "
         f"{len(rebaked_frames)} to bake")
    if _presave_active:
        # Merge presaved VDB data files into the newly-initialised directory.
        # We keep the fresh Mantaflow config/ (it knows the domain is at this
        # path) and restore the VDB files so Mantaflow can detect them and
        # resume from the last baked frame rather than starting over.
        _log(f"[{name}] RESUME: merging presaved VDB files into new cache directory")
        _log(f"[{name}]   FROM : {_presave_dir}  (VDB data files only — config/ excluded)")
        _log(f"[{name}]   INTO : {effective_cache_dir}  (fresh Mantaflow config/ preserved)")
        _merge_count = 0
        try:
            for _proot, _pdirs, _pfiles in os.walk(_presave_dir):
                if os.path.basename(_proot) == 'config':
                    continue  # keep the fresh config/ Mantaflow just wrote
                for _pf in _pfiles:
                    if re.search(r'_\d+\.(vdb|uni)$', _pf):
                        _psrc    = os.path.join(_proot, _pf)
                        _prel    = os.path.relpath(_proot, _presave_dir)
                        _pdstdir = os.path.join(effective_cache_dir, _prel)
                        os.makedirs(_pdstdir, exist_ok=True)
                        os.replace(_psrc, os.path.join(_pdstdir, _pf))
                        _merge_count += 1
            shutil.rmtree(_presave_dir, ignore_errors=True)
            _log(f"[{name}] RESUME: merge complete — moved {_merge_count} data file(s) to cache dir")
            _log(f"[{name}]   NOTE: Mantaflow will attempt to resume from existing frames; "
                 f"if it re-bakes from scratch the result will still be correct (just slower)")
        except OSError as _e:
            _log(f"[{name}] WARNING: Presave merge failed ({_e}) — "
                 f"bake will proceed; Mantaflow may re-bake all frames from scratch")
    # Re-bake in place — do NOT save/reload the .blend.  In scripted mode
    # Mantaflow re-bakes from frame 1 regardless (assigning d.cache_directory
    # resets its internal frame tracking and there is no API to resume mid-range
    # without clearing).  It overwrites the merged presave files in place, so all
    # frames end present and correct — just slower than a true partial resume.
    #
    # v0.3.1: the v0.2.32 save+reload-to-resume trick was removed.  An
    # open_mainfile() mid-script leaves bpy.ops.fluid.bake_all() unable to run in
    # windowed (EEVEE) mode — Blender goes idle and the worker hangs forever on
    # the bake call (observed on a res-512 job: ~0% CPU, UI showing no bake, no
    # cache writes for 30+ min).  The reload never achieved a true resume anyway
    # (cache_frame_pause_data was 0 afterward), so it was pure downside.
    bpy.context.view_layer.update()
    _time.sleep(2.0)

    _log(f"[{name}] Baking...")
    bake_start   = _time.time()
    _bake_result = bpy.ops.fluid.bake_all()
    bake_seconds = _time.time() - bake_start

    # Diagnostic: how many files in cache now?  If far fewer than expected,
    # Mantaflow cleared the merged frames (the v0.2.30 failure mode).
    _bake_files = _count_data_files(effective_cache_dir)
    _expected_total = frame_end - frame_start + 1
    _log(f"[{name}] RESUME post-bake: cache dir has {_bake_files} data files "
         f"(expected {_expected_total})")

    if 'FINISHED' not in _bake_result:
        _log(f"[{name}] ERROR: Bake did not finish normally (result: {_bake_result})")
        sys.exit(1)
    _log(f"[{name}] Bake complete in {bake_seconds:.0f}s.")
    _time.sleep(2.0)

else:
    _bake_decision = "FULL"
    rebaked_frames = set(range(frame_start, frame_end + 1))
    bake_skipped   = False
    if _presave_active:
        _log(f"[{name}]   Decision : FULL BAKE — discarding presave (no usable existing frames)")
        shutil.rmtree(_presave_dir, ignore_errors=True)
        _log(f"[{name}]   Presave discarded: {_presave_dir}")
    elif use_existing_cache:
        _log(f"[{name}]   Decision : FULL BAKE — use_existing_cache on but no data frames found")
    else:
        _log(f"[{name}]   Decision : FULL BAKE — use_existing_cache disabled")
    _log(f"[{name}] Freeing previous cache and baking from scratch...")
    bpy.ops.fluid.free_all()
    _time.sleep(2.0)
    _log(f"[{name}] Baking...")
    bake_start   = _time.time()
    _bake_result = bpy.ops.fluid.bake_all()
    bake_seconds = _time.time() - bake_start
    if 'FINISHED' not in _bake_result:
        _log(f"[{name}] ERROR: Bake did not finish normally (result: {_bake_result})")
        sys.exit(1)
    _log(f"[{name}] Bake complete in {bake_seconds:.0f}s.")
    _time.sleep(2.0)

# Verify cache is populated before proceeding to render.
_post_bake_count = _count_data_files(effective_cache_dir)
_log(f"[{name}] Cache data files found: {_post_bake_count}")
if _post_bake_count == 0:
    _log(f"[{name}] ERROR: No cache files under {effective_cache_dir} — cannot render")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Bake-time sidecar — keep the "this cache took N seconds to produce" record
# ---------------------------------------------------------------------------
# SKIP   : display the stored time so the rendered overlay doesn't read
#          "Bake: 0 sec".  Nothing to write (we didn't bake).
# RESUME : the cache now reflects (prior bake) + (this run), so we accumulate.
# FULL   : fresh cache, write only this run's time.
if _bake_decision == "SKIP":
    display_bake_seconds = _prev_stored_bake   # may be None if no sidecar
elif _bake_decision == "RESUME":
    display_bake_seconds = (_prev_stored_bake or 0.0) + bake_seconds
    _write_stored_bake_time(effective_cache_dir, display_bake_seconds,
                            frame_end, int(p["resolution"]))
else:   # FULL
    display_bake_seconds = bake_seconds
    _write_stored_bake_time(effective_cache_dir, display_bake_seconds,
                            frame_end, int(p["resolution"]))

if display_bake_seconds is not None:
    _log(f"[{name}] Display bake_time: {display_bake_seconds:.1f}s "
         f"(decision={_bake_decision})")
else:
    _log(f"[{name}] Display bake_time: (no sidecar yet — overlay will skip the time line)")

# ---------------------------------------------------------------------------
# Update text objects (after bake — includes bake time)
# ---------------------------------------------------------------------------

update_text_objects(text_map, p, bake_seconds=display_bake_seconds)
_log(f"[{name}] Text objects updated (post-bake).")

# ---------------------------------------------------------------------------
# Two-phase pipeline: in the bake-only phase, stop here.  Render + CSV happen in
# the separate render-phase process (Increment 3 wires the two passes; until then
# export passes no --phase, so phase=="both" and this is skipped).
# ---------------------------------------------------------------------------
if not do_render:
    _log(f"[{name}] phase=bake complete — skipping render and CSV.")
    _close_log()
    _bake_jobs_dir  = os.path.dirname(job_path)
    _bake_job_stem  = os.path.splitext(os.path.basename(job_path))[0]
    # Phased worker_done so the bake-phase sentinel doesn't collide with the
    # render-phase one (the launcher reads <stem>.<phase>.worker_done).
    _bake_sentinel  = _bake_job_stem + ".bake.worker_done"
    try:
        with open(os.path.join(_bake_jobs_dir, _bake_sentinel), "w") as _wdf:
            _wdf.write(datetime.datetime.now().isoformat() + "\n")
    except OSError:
        pass
    bpy.ops.wm.quit_blender()
    sys.exit(0)

# ---------------------------------------------------------------------------
# Scene setup — used by both render passes
# ---------------------------------------------------------------------------

scene             = bpy.context.scene
scene.frame_start = frame_start
scene.frame_end   = frame_end

# ---------------------------------------------------------------------------
# Render passes — animation MP4 + final still PNG.
# Skipped entirely in bake-only mode (render_simulation_result = False, TODO-26):
# validate the simulation cache now, render later by hand.  CSV + perf records
# below still run so the job is recorded as complete.
# ---------------------------------------------------------------------------

frames_actually_rendered = 0
render_seconds           = 0.0

if not render_simulation_result:
    _log(f"[{name}] Render Simulation Result disabled — bake-only run, "
         f"skipping animation and still render.")
else:
    # ---- Playblast — full animation ----
    mp4 = os.path.join(render_dir, name + ".mp4")

    bpy.context.view_layer.update()

    # Always render a PNG sequence first, then convert with external ffmpeg.
    # This allows resuming after a crash and gives frame-level progress tracking.
    # Name is parameter-derived, so the frames dir is always the same for the
    # same parameter combination — no fuzzy cross-run search needed.
    png_sequence_dir     = os.path.join(render_dir, f"{name}_frames")
    effective_frames_dir = png_sequence_dir

    os.makedirs(effective_frames_dir, exist_ok=True)
    if render_mode == "EEVEE":
        setup_eevee(scene)
    else:
        setup_cycles(scene, samples=render_samples)
    scene.render.image_settings.file_format = "PNG"

    # Check for existing frames if use_placeholders is enabled.
    # Frames in rebaked_frames are always re-rendered even if a PNG exists, because
    # the new bake may produce different smoke than the render that was previously made.
    frames_to_render = set(range(scene.frame_start, frame_end + 1))
    if use_placeholders:
        existing_frames = set()
        for frame_num in frames_to_render:
            if frame_num in rebaked_frames:
                continue  # cache was recomputed — must re-render
            frame_file = os.path.join(effective_frames_dir, f"frame_{frame_num:04d}.png")
            if os.path.exists(frame_file):
                existing_frames.add(frame_num)

        frames_to_render -= existing_frames
        if existing_frames:
            _log(f"[{name}] Found {len(existing_frames)} existing frame(s), skipping those")
        if rebaked_frames and use_placeholders:
            _log(f"[{name}] {len(rebaked_frames)} frame(s) were rebaked — re-rendering those regardless of placeholders")
        if not frames_to_render:
            _log(f"[{name}] All frames already exist, skipping animation render")

    # Render frames individually to support partial resume
    frames_actually_rendered = len(frames_to_render)
    if frames_to_render:
        _dlog(f"render start: engine={render_mode}  frames={len(frames_to_render)}  dir={effective_frames_dir!r}")
        _log(f"[{name}] Rendering animation ({len(frames_to_render)} frame(s)) -> {effective_frames_dir}")
        render_start = _time.time()
        for frame_num in sorted(frames_to_render):
            scene.frame_set(frame_num)
            frame_file = os.path.join(effective_frames_dir, f"frame_{frame_num:04d}.png")
            scene.render.filepath = frame_file
            _render_result = bpy.ops.render.render(write_still=True)
            if 'FINISHED' not in _render_result:
                _log(f"[{name}] ERROR: Frame {frame_num} render did not finish (result: {_render_result})")
                sys.exit(1)
        render_seconds = _time.time() - render_start
        _log(f"[{name}] Playblast frame sequence complete.")
    else:
        _log(f"[{name}] No frames to render (all exist or skipped)")

    # Convert PNG sequence to MP4 using FFmpeg
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",
        "-framerate", str(scene.render.fps),
        "-i", os.path.join(effective_frames_dir, "frame_%04d.png"),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        mp4
    ]
    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
        _log(f"[{name}] MP4 conversion complete -> {mp4}")
    except subprocess.CalledProcessError as e:
        _log(f"[{name}] FFmpeg conversion failed: {e.stderr.decode()}")
    except FileNotFoundError:
        _log(f"[{name}] FFmpeg not found on system PATH. PNG frames saved to {png_sequence_dir}")

    # ---- Final still — last frame only ----
    png = os.path.join(render_dir, name + ".png")

    if render_mode == "EEVEE":
        # EEVEE — only works in windowed mode (no --background)
        if not setup_eevee(scene):
            setup_cycles(scene, samples=render_samples)
    else:
        # Cycles GPU — default, works in background mode
        setup_cycles(scene, samples=render_samples)

    # Must reset to PNG after FFMPEG playblast
    scene.render.image_settings.file_format = 'PNG'

    scene.frame_set(frame_end)
    scene.render.filepath = png
    _log(f"[{name}] Rendering final frame ({scene.render.engine}) -> {png}")
    _render_result = bpy.ops.render.render(write_still=True)
    if 'FINISHED' not in _render_result:
        _log(f"[{name}] ERROR: Still render did not finish (result: {_render_result})")
        sys.exit(1)
    _log(f"[{name}] PNG render complete. File exists: {os.path.exists(png)}")

# ---------------------------------------------------------------------------
# CSV — append one row per job, including dissolve/noise enabled flags
# ---------------------------------------------------------------------------

csv_path = os.path.join(render_dir, "results.csv")
header   = [
    "name",
    "resolution",
    "vorticity",
    "alpha",
    "beta",
    "dissolve_speed",
    "slow_dissolve",
    "noise_upres",
    "noise_strength",
    "noise_spatial_scale",
    "bake_seconds",
    "version",   # addon version that produced this row (appended last so older
                 # readers/columns stay aligned); for cross-version comparison
]
write_header = not os.path.exists(csv_path)
with open(csv_path, "a", encoding="utf-8") as fh:
    if write_header:
        fh.write(",".join(header) + "\n")
    fh.write(",".join(str(x) for x in [
        name,
        p["resolution"],
        p["vorticity"],
        p["alpha"],
        p["beta"],
        p["dissolve_speed"] if p["use_dissolve"] else "OFF",
        p["slow_dissolve"]  if p["use_dissolve"] else "OFF",
        p["noise_upres"]         if p["use_noise"] else "OFF",
        p["noise_strength"]      if p["use_noise"] else "OFF",
        p["noise_spatial_scale"] if p["use_noise"] else "OFF",
        int(bake_seconds),
        addon_version,
    ]) + "\n")

_log(f"[{name}] Done. Results -> {csv_path}")

# ---------------------------------------------------------------------------
# Performance data — append one record to perf_log.json
# ---------------------------------------------------------------------------

_res       = int(p["resolution"])
_res3      = _res ** 3
_rx        = scene.render.resolution_x
_ry        = scene.render.resolution_y
_pixels    = _rx * _ry

_perf = {
    "addon_version": addon_version,
    "worker_version": WORKER_VERSION,
    "job_name":    name,
    "resolution":  _res,
    "frame_end":   frame_end,
    # Bake
    "bake_skipped":            bake_skipped,
    "bake_seconds":            round(bake_seconds, 2) if not bake_skipped else None,
    "bake_secs_per_frame":     round(bake_seconds / frame_end, 6) if not bake_skipped and frame_end > 0 else None,
    "bake_secs_per_res3_frame": round(bake_seconds / (_res3 * frame_end), 12) if not bake_skipped and _res3 > 0 and frame_end > 0 else None,
    # Render
    "render_engine":           render_mode,
    "render_width":            _rx,
    "render_height":           _ry,
    "frames_rendered":         frames_actually_rendered,
    "render_seconds":          round(render_seconds, 2) if frames_actually_rendered > 0 else None,
    "render_secs_per_frame":   round(render_seconds / frames_actually_rendered, 6) if frames_actually_rendered > 0 else None,
    "render_secs_per_pixel_frame": round(render_seconds / (_pixels * frames_actually_rendered), 12) if frames_actually_rendered > 0 and _pixels > 0 else None,
}
# Skip low-resolution data: per-job overhead dominates at res<=64, so the
# implied rate is ~5x higher than at res=128 and contaminates the fitted
# constants. Below this threshold the absolute time saved by a better
# estimate is small anyway, so we just accept the placeholder error.
_PERF_MIN_RESOLUTION = 64  # samples are kept when resolution > this
if collect_estimation_data and _res > _PERF_MIN_RESOLUTION:
    _append_perf_record(output_path, _perf)
    _log(f"[{name}] Performance record written to perf_log.json")
elif collect_estimation_data:
    _log(f"[{name}] perf_log.json: skipped (res={_res} <= {_PERF_MIN_RESOLUTION})")

# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------

_dlog(f"exit: bake_seconds={bake_seconds:.1f}  render_seconds={render_seconds:.1f}  frames_rendered={frames_actually_rendered}")
_log(f"[{name}] Exiting Blender.")
_close_log()

# Write completion sentinel so the launcher can detect exit-code-0 crashes.
# The launcher treats a missing sentinel as a crash even when Blender exits 0.
_jobs_dir  = os.path.dirname(job_path)
_job_stem  = os.path.splitext(os.path.basename(job_path))[0]
# Phased sentinel name so the bake-phase and render-phase processes don't clobber
# each other's worker_done.  Single-pass ("both") keeps the original bare name
# so legacy launchers / poll code that pre-date the two-pass pipeline still see it.
_phase_suffix = "" if phase == "both" else f".{phase}"
try:
    with open(os.path.join(_jobs_dir, _job_stem + _phase_suffix + ".worker_done"), "w") as _wdf:
        _wdf.write(datetime.datetime.now().isoformat() + "\n")
except OSError:
    pass

bpy.ops.wm.quit_blender()
