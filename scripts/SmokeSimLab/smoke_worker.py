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

WORKER_VERSION = "0.2.12"

import bpy
import sys
import os
import re
import json
import time as _time
import datetime
import atexit
import subprocess

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

with open(job_path) as fh:
    cfg = json.load(fh)

_log_path = cfg.get("log_path")
if _log_path:
    _log_file = open(_log_path, "w", buffering=1)

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

_log(f"[{name}]   Effective cache dir  : {effective_cache_dir}")

# Count baked frames BEFORE assigning d.cache_directory — Blender may
# reinitialize (and clear) the Mantaflow domain when the cache path is set,
# which would make the directory appear empty in a post-assignment walk.
# Three outcomes:
#   1. Complete cache (all required frames present) → skip bake entirely
#   2. Partial cache (some frames, not all)         → resume without freeing
#   3. No data frames (or use_existing_cache off)  → free + fresh bake
baked_frames = set()
_all_cache_files = []
for _root, _dirs, files in os.walk(effective_cache_dir):
    subdir = os.path.basename(_root)
    for f in files:
        _all_cache_files.append(f)
        if subdir == 'config':
            continue  # checkpoint files, not simulation data
        m = re.search(r'_(\d+)\.(vdb|uni)$', f)
        if m:
            baked_frames.add(int(m.group(1)))

d.cache_directory = effective_cache_dir

# Enable resumable baking so a mid-bake crash can be continued on the next run.
# Without this Blender does not store the solver checkpoint data alongside the
# VDB output files, and bake_all() cannot resume from a partial cache.
try:
    d.cache_resumable = True
except AttributeError:
    _log(f"[{name}] WARNING: cache_resumable property not found — partial resume may not work")

bpy.context.view_layer.update()
_time.sleep(2.0)

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
# Existing renders for these frames must NOT be used as placeholders, because
# the new bake may produce different smoke data than the old render.
rebaked_frames = set()

if use_existing_cache and bake_complete:
    _log(f"[{name}]   Decision           : SKIP BAKE — all {frame_end - frame_start + 1} frames confirmed")
    bake_seconds = 0.0
    bake_skipped = True

elif use_existing_cache and baked_frames:
    rebaked_frames = set(range(frame_start, frame_end + 1)) - baked_frames
    bake_skipped   = False
    _log(f"[{name}]   Decision           : RESUME — {len(baked_frames)} frames present, "
         f"{len(rebaked_frames)} to bake")
    _log(f"[{name}] Baking...")
    bake_start   = _time.time()
    _bake_result = bpy.ops.fluid.bake_all()
    bake_seconds = _time.time() - bake_start
    if 'FINISHED' not in _bake_result:
        _log(f"[{name}] ERROR: Bake did not finish normally (result: {_bake_result})")
        sys.exit(1)
    _log(f"[{name}] Bake complete in {bake_seconds:.0f}s.")
    _time.sleep(2.0)

else:
    rebaked_frames = set(range(frame_start, frame_end + 1))
    bake_skipped   = False
    if effective_cache_dir != cache_dir:
        effective_cache_dir = cache_dir
        d.cache_directory   = cache_dir
    if use_existing_cache:
        _log(f"[{name}]   Decision           : FULL BAKE — use_existing_cache on but no data frames found")
    else:
        _log(f"[{name}]   Decision           : FULL BAKE — use_existing_cache disabled")
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

# Verify cache is populated before proceeding to render
existing_cache_files = []
for root, dirs, files in os.walk(effective_cache_dir):
    for f in files:
        if f.endswith('.vdb') or f.endswith('.uni'):
            existing_cache_files.append(os.path.join(root, f))

_log(f"[{name}] Cache files found: {len(existing_cache_files)}")
if len(existing_cache_files) == 0:
    _log(f"[{name}] ERROR: No cache files under {effective_cache_dir} — skipping render")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Update text objects (after bake — includes bake time)
# ---------------------------------------------------------------------------

update_text_objects(text_map, p, bake_seconds=bake_seconds)
_log(f"[{name}] Text objects updated (post-bake).")

# ---------------------------------------------------------------------------
# Scene setup — used by both render passes
# ---------------------------------------------------------------------------

scene             = bpy.context.scene
scene.frame_start = frame_start
scene.frame_end   = frame_end

# ---------------------------------------------------------------------------
# Playblast — full animation
# ---------------------------------------------------------------------------

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
render_seconds = 0.0
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

# ---------------------------------------------------------------------------
# Final still — last frame only
# ---------------------------------------------------------------------------

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
if collect_estimation_data:
    _append_perf_record(output_path, _perf)
    _log(f"[{name}] Performance record written to perf_log.json")

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
try:
    with open(os.path.join(_jobs_dir, _job_stem + ".worker_done"), "w") as _wdf:
        _wdf.write(datetime.datetime.now().isoformat() + "\n")
except OSError:
    pass

bpy.ops.wm.quit_blender()
