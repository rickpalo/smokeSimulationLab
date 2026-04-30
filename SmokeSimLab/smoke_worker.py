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
text_map    = cfg.get("text_objects", {})

# Render mode from job config — defaults to 'CYCLES'
# Set to 'EEVEE' in job JSON or export settings for windowed mode
render_mode = cfg.get("render_mode", "CYCLES")
use_placeholders   = cfg.get("use_placeholders", False)
use_existing_cache = cfg.get("use_existing_cache", False)

render_dir = os.path.join(output_path, "Renders")
cache_dir  = os.path.join(output_path, "Cache", name)
os.makedirs(render_dir, exist_ok=True)
os.makedirs(cache_dir,  exist_ok=True)

_log(f"[{name}] Job started.")
_log(f"[{name}] Cache dir: {cache_dir}")
_log(f"[{name}] Render dir: {render_dir}")
_log(f"[{name}] Render mode: {render_mode}")

# ---------------------------------------------------------------------------
# Locate domain object and fluid modifier
# ---------------------------------------------------------------------------

obj = bpy.data.objects.get(domain_name)
if not obj:
    _log(f'ERROR: object "{domain_name}" not found in scene')
    sys.exit(1)

mod = obj.modifiers.get("Fluid")
if not mod:
    _log(f'ERROR: no Fluid modifier on "{domain_name}"')
    sys.exit(1)

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
bpy.context.view_layer.update()
_time.sleep(2.0)

# Check whether usable cache files already exist in the resolved directory
existing_cache_files = []
for root, dirs, files in os.walk(effective_cache_dir):
    for f in files:
        if f.endswith('.vdb') or f.endswith('.uni'):
            existing_cache_files.append(os.path.join(root, f))

if use_existing_cache and existing_cache_files:
    _log(f"[{name}] Use Existing Cache enabled — found {len(existing_cache_files)} cache file(s), skipping bake.")
    bake_seconds = 0.0
else:
    if effective_cache_dir != cache_dir:
        # No files in alt dir — fall back to current job's fresh cache
        effective_cache_dir = cache_dir
        d.cache_directory   = cache_dir
    _log(f"[{name}] Freeing previous cache...")
    bpy.ops.fluid.free_all()
    _time.sleep(2.0)

    _log(f"[{name}] Baking...")
    bake_start   = _time.time()
    bpy.ops.fluid.bake_all()
    bake_seconds = _time.time() - bake_start
    _log(f"[{name}] Bake complete in {bake_seconds:.0f}s.")

    _time.sleep(2.0)

    # Verify cache wrote successfully
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
scene.frame_start = 1
scene.frame_end   = frame_end

# ---------------------------------------------------------------------------
# Playblast — full animation
# ---------------------------------------------------------------------------

mp4 = os.path.join(render_dir, name + ".mp4")

bpy.context.view_layer.update()
_time.sleep(1.0)

# Try Anim Reviewer addon first (fast viewport render if available)
anim_reviewer_used = False
if 'bl_ext.blender_org.anim_reviewer' in bpy.context.preferences.addons or \
   'anim_reviewer' in bpy.context.preferences.addons:
    try:
        scene.render.filepath                   = mp4
        scene.render.image_settings.file_format = "FFMPEG"
        scene.render.ffmpeg.format              = "MPEG4"
        scene.render.ffmpeg.codec               = "H264"
        bpy.ops.animreview.render_animation()
        anim_reviewer_used = True
        _log(f"[{name}] Playblast via Anim Reviewer complete.")
    except Exception as e:
        _log(f"[{name}] Anim Reviewer failed ({e}), falling back to Cycles.")

if not anim_reviewer_used:
    # Cycles GPU playblast — works reliably in background mode
    setup_cycles(scene, samples=32)
    scene.render.filepath                   = mp4
    scene.render.ffmpeg.format              = "MPEG4"
    scene.render.ffmpeg.codec               = "H264"

    # Try native FFMPEG format (Blender < 5.1)
    ffmpeg_supported = False
    try:
        scene.render.image_settings.file_format = "FFMPEG"
        ffmpeg_supported = True
        _log(f"[{name}] Playblasting (Cycles {scene.cycles.samples} samples) -> {mp4}")
        bpy.ops.render.render(animation=True)
        _log(f"[{name}] Playblast complete.")
    except TypeError:
        # FFMPEG format not supported (Blender 5.1+), fall back to PNG sequence
        _log(f"[{name}] FFMPEG format not supported, rendering to PNG sequence instead.")
        ffmpeg_supported = False

    if not ffmpeg_supported:
        # Render to PNG sequence, then convert with external FFmpeg
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
        scene.render.image_settings.file_format = "PNG"

        # Check for existing frames if use_placeholders is enabled
        frames_to_render = set(range(scene.frame_start, frame_end + 1))
        if use_placeholders:
            existing_frames = set()
            for frame_num in frames_to_render:
                frame_file = os.path.join(effective_frames_dir, f"frame_{frame_num:04d}.png")
                if os.path.exists(frame_file):
                    existing_frames.add(frame_num)

            frames_to_render -= existing_frames
            if existing_frames:
                _log(f"[{name}] Found {len(existing_frames)} existing frame(s), skipping those")
            if not frames_to_render:
                _log(f"[{name}] All frames already exist, skipping animation render")

        # Render frames individually to support skipping
        if frames_to_render:
            _log(f"[{name}] Playblasting (Cycles {scene.cycles.samples} samples) -> {effective_frames_dir}")
            for frame_num in sorted(frames_to_render):
                scene.frame_set(frame_num)
                frame_file = os.path.join(effective_frames_dir, f"frame_{frame_num:04d}.png")
                scene.render.filepath = frame_file
                bpy.ops.render.render(write_still=True)
            _log(f"[{name}] Playblast frame sequence complete.")
        else:
            _log(f"[{name}] No frames to render (all exist or skipped)")

        # Convert PNG sequence to MP4 using FFmpeg
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",  # Overwrite output file
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
        setup_cycles(scene, samples=128)
else:
    # Cycles GPU — default, works in background mode
    setup_cycles(scene, samples=128)

# Must reset to PNG after FFMPEG playblast
scene.render.image_settings.file_format = 'PNG'

scene.frame_set(frame_end)
scene.render.filepath = png
_log(f"[{name}] Rendering final frame ({scene.render.engine}) -> {png}")
bpy.ops.render.render(write_still=True)
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
# Exit
# ---------------------------------------------------------------------------

_log(f"[{name}] Exiting Blender.")
_close_log()
bpy.ops.wm.quit_blender()
