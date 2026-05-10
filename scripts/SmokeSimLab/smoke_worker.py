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


def _find_match(parent, prefix, suffix="", want_file=False):
    """Return the most-recently-modified path in *parent* whose name matches
    prefix_NNNN[suffix] (4-digit job index may differ from the current run).
    Returns None if no match found."""
    if not os.path.isdir(parent):
        return None
    pat   = re.compile(r'^' + re.escape(prefix) + r'_\d{4}' + re.escape(suffix) + r'$')
    check = os.path.isfile if want_file else os.path.isdir
    hits  = [
        os.path.join(parent, e) for e in os.listdir(parent)
        if pat.match(e) and check(os.path.join(parent, e))
    ]
    return max(hits, key=os.path.getmtime) if hits else None


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
name_prefix = name.rsplit('_', 1)[0]   # parameter key without job index
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

_log(f"[{name}] Setting up cache directory...")

# Resolve the effective cache directory.  When use_existing_cache is on, also
# search for a cache produced by a different job-number run with the same
# parameters (e.g. reuse Cache/R128_…_0002 when current job is _0001).
# NOTE: cache_dir (the current job's directory) was already created empty by
# os.makedirs above, so we cannot rely on mtime alone — we must verify that
# a candidate actually contains .vdb/.uni files before accepting it.
effective_cache_dir = cache_dir
if use_existing_cache:
    cache_base = os.path.join(output_path, "Cache")
    if os.path.isdir(cache_base):
        pat        = re.compile(r'^' + re.escape(name_prefix) + r'_\d{4}$')
        candidates = sorted(
            [os.path.join(cache_base, e) for e in os.listdir(cache_base)
             if pat.match(e) and os.path.isdir(os.path.join(cache_base, e))],
            key=os.path.getmtime, reverse=True,
        )
        for candidate in candidates:
            has_files = False
            for _root, _dirs, files in os.walk(candidate):
                if any(f.endswith('.vdb') or f.endswith('.uni') for f in files):
                    has_files = True
                    break
            if has_files:
                effective_cache_dir = candidate
                if candidate != cache_dir:
                    _log(f"[{name}] Found cache from different run: {candidate}")
                break

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

# Determine bake completeness by checking if the last required frame is cached.
# Three outcomes:
#   1. Complete cache (frame_end present)  → skip bake entirely
#   2. Partial cache (some frames, not all) → resume bake without freeing
#   3. No cache (or ignoring existing)     → free + fresh bake
baked_frames = set()
_all_cache_files = []
for _root, _dirs, files in os.walk(effective_cache_dir):
    for f in files:
        _all_cache_files.append(f)
        m = re.search(r'_(\d+)\.(vdb|uni)$', f)
        if m:
            baked_frames.add(int(m.group(1)))

if not baked_frames and _all_cache_files:
    _log(f"[{name}] WARNING: cache dir has {len(_all_cache_files)} file(s) but none matched "
         f"frame-number pattern — first few: {_all_cache_files[:5]}")

bake_complete = all(f in baked_frames for f in range(frame_start, frame_end + 1))
_dlog(f"cache check: effective_cache_dir={effective_cache_dir!r}  "
      f"baked_frames={len(baked_frames)}  bake_complete={bake_complete}")

# rebaked_frames: frame numbers whose cache was RECOMPUTED this run.
# Existing renders for these frames must NOT be used as placeholders, because
# the new bake may produce different smoke data than the old render.
rebaked_frames = set()

if use_existing_cache and bake_complete:
    _log(f"[{name}] Use Existing Cache enabled — all {frame_end - frame_start + 1} "
         f"frames ({frame_start}–{frame_end}) confirmed, skipping bake.")
    bake_seconds = 0.0
    bake_skipped = True
    # rebaked_frames stays empty — all cache pre-existing, renders still valid

elif use_existing_cache and baked_frames:
    # Partial bake from a previous crash — resume without freeing existing frames.
    # Only frames beyond the previous bake are recomputed.
    rebaked_frames = set(range(frame_start, frame_end + 1)) - baked_frames
    bake_skipped   = False
    _log(f"[{name}] Partial cache ({len(baked_frames)}/{frame_end} frames). Resuming bake...")
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
    # Full rebake — all frames recomputed, any existing renders are stale.
    # Reaches here when use_existing_cache is False, OR when it is True but
    # no VDB files were found (first run, or crash before any frames wrote).
    rebaked_frames = set(range(frame_start, frame_end + 1))
    bake_skipped   = False
    if effective_cache_dir != cache_dir:
        # No usable files in alt dir — fall back to this job's own cache dir
        effective_cache_dir = cache_dir
        d.cache_directory   = cache_dir
    if use_existing_cache:
        _log(f"[{name}] No existing cache found — performing full bake.")
    else:
        _log(f"[{name}] Freeing previous cache...")
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
png_sequence_dir = os.path.join(render_dir, f"{name}_frames")

# When use_placeholders is on, also accept a frames folder produced by
# a different job-number run with identical parameters.
effective_frames_dir = png_sequence_dir
if use_placeholders:
    alt_frames = _find_match(render_dir, name_prefix, suffix="_frames")
    if alt_frames:
        effective_frames_dir = alt_frames
        if alt_frames != png_sequence_dir:
            _log(f"[{name}] Found existing frames dir from different run: {alt_frames}")

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
bpy.ops.wm.quit_blender()
