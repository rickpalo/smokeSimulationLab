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
import json
import time as _time


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
        print(f"  Text '{obj_name}' -> '{value_str}'")
    else:
        print(f"  WARNING: '{obj_name}' not found or not a FONT object")


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
    # Resolution
    _set_text(text_map.get("resolution", ""),
              f"Res: {int(params['resolution'])}")

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
        print("  Cycles addon not available — using CPU")
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
                print(f"  GPU rendering enabled: {device_type}")
                return True
        except Exception as e:
            print(f"  {device_type} not available: {e}")
            continue

    print("  No GPU available — falling back to CPU")
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
        print("  Render engine: EEVEE Next")
        return True
    elif 'BLENDER_EEVEE' in available:
        scene.render.engine = 'BLENDER_EEVEE'
        print("  Render engine: EEVEE")
        return True
    print("  EEVEE not available — falling back to Cycles")
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
    print("ERROR: expected path to job JSON after --")
    sys.exit(1)

with open(job_path) as fh:
    cfg = json.load(fh)

p           = cfg["params"]
name        = cfg["name"]
output_path = cfg["output_path"]
domain_name = cfg["domain_name"]
frame_end   = cfg["frame_end"]
text_map    = cfg.get("text_objects", {})

# Render mode from job config — defaults to 'CYCLES'
# Set to 'EEVEE' in job JSON or export settings for windowed mode
render_mode = cfg.get("render_mode", "CYCLES")

render_dir = os.path.join(output_path, "Renders")
cache_dir  = os.path.join(output_path, "Cache", name)
os.makedirs(render_dir, exist_ok=True)
os.makedirs(cache_dir,  exist_ok=True)

print(f"[{name}] Job started.")
print(f"[{name}] Cache dir: {cache_dir}")
print(f"[{name}] Render dir: {render_dir}")
print(f"[{name}] Render mode: {render_mode}")

# ---------------------------------------------------------------------------
# Locate domain object and fluid modifier
# ---------------------------------------------------------------------------

obj = bpy.data.objects.get(domain_name)
if not obj:
    print(f'ERROR: object "{domain_name}" not found in scene')
    sys.exit(1)

mod = obj.modifiers.get("Fluid")
if not mod:
    print(f'ERROR: no Fluid modifier on "{domain_name}"')
    sys.exit(1)

d = mod.domain_settings

# ---------------------------------------------------------------------------
# Apply simulation parameters
# ---------------------------------------------------------------------------

d.cache_directory    = cache_dir
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
d.cache_data_format           = 'OPENVDB'
d.cache_noise_format          = 'OPENVDB'
d.openvdb_cache_compress_type = 'BLOSC'

bpy.context.view_layer.objects.active = obj
obj.select_set(True)

# Let Mantaflow reinitialize with new settings before baking
bpy.context.view_layer.update()
_time.sleep(3.0)

# ---------------------------------------------------------------------------
# Update text objects (before bake — no time available yet)
# ---------------------------------------------------------------------------

update_text_objects(text_map, p)
print(f"[{name}] Text objects updated (pre-bake).")

# ---------------------------------------------------------------------------
# Bake
# ---------------------------------------------------------------------------

print(f"[{name}] Setting up cache directory...")
d.cache_directory = cache_dir
bpy.context.view_layer.update()
_time.sleep(2.0)

print(f"[{name}] Freeing previous cache...")
bpy.ops.fluid.free_all()
_time.sleep(2.0)

print(f"[{name}] Baking...")
bake_start   = _time.time()
bpy.ops.fluid.bake_all()
bake_seconds = _time.time() - bake_start
print(f"[{name}] Bake complete in {bake_seconds:.0f}s.")

_time.sleep(2.0)

# ---------------------------------------------------------------------------
# Verify cache wrote successfully
# ---------------------------------------------------------------------------

cache_files = []
for root, dirs, files in os.walk(cache_dir):
    for f in files:
        if f.endswith('.vdb') or f.endswith('.uni'):
            cache_files.append(os.path.join(root, f))

print(f"[{name}] Cache files found: {len(cache_files)}")
if len(cache_files) == 0:
    print(f"[{name}] ERROR: No cache files under {cache_dir} — skipping render")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Update text objects (after bake — includes bake time)
# ---------------------------------------------------------------------------

update_text_objects(text_map, p, bake_seconds=bake_seconds)
print(f"[{name}] Text objects updated (post-bake).")

# ---------------------------------------------------------------------------
# Scene setup — used by both render passes
# ---------------------------------------------------------------------------

scene             = bpy.context.scene
scene.frame_start = 1
scene.frame_end   = frame_end
prev_engine       = scene.render.engine

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
        print(f"[{name}] Playblast via Anim Reviewer complete.")
    except Exception as e:
        print(f"[{name}] Anim Reviewer failed ({e}), falling back to Cycles.")

if not anim_reviewer_used:
    # Cycles GPU playblast — works reliably in background mode
    setup_cycles(scene, samples=32)
    scene.render.filepath                   = mp4
    scene.render.image_settings.file_format = "FFMPEG"
    scene.render.ffmpeg.format              = "MPEG4"
    scene.render.ffmpeg.codec               = "H264"
    print(f"[{name}] Playblasting (Cycles {scene.cycles.samples} samples) -> {mp4}")
    bpy.ops.render.render(animation=True)
    print(f"[{name}] Playblast complete.")

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
print(f"[{name}] Rendering final frame ({scene.render.engine}) -> {png}")
bpy.ops.render.render(write_still=True)
scene.render.engine = prev_engine
print(f"[{name}] PNG render complete. File exists: {os.path.exists(png)}")

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
with open(csv_path, "a") as fh:
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

print(f"[{name}] Done. Results -> {csv_path}")

# ---------------------------------------------------------------------------
# Exit
# ---------------------------------------------------------------------------

print(f"[{name}] Exiting Blender.")
bpy.ops.wm.quit_blender()
