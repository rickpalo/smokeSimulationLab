"""SmokeSimLab Pre-flight Inspector
====================================
Paste this script into Blender's Scripting workspace and click "Run Script".
Output appears in this same text editor (as "preflight_report") and in the
System Console (Window > Toggle System Console on Windows).

What it checks:
  1. Every fluid domain in the scene — type, cache path, exists, file count
  2. SmokeSimLab addon status and current parameter settings
  3. Expected cache path for the current single-value settings (pre-export check)
  4. Whether domain cache_directory matches the expected SmokeSimLab path
  5. Cache directory contents for all existing Cache/ subdirs under output_path

Run BEFORE Export Batch to catch path mismatches before the batch starts.
Requires SmokeSimLab addon to be ENABLED in Preferences > Add-ons.
"""

import bpy
import os
import re
import sys


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_data_files(directory):
    count = 0
    for root, dirs, fnames in os.walk(directory):
        if os.path.basename(root) == 'config':
            continue
        count += sum(1 for f in fnames if re.search(r'_\d+\.(vdb|uni)$', f))
    return count


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _yesno(val):
    return "YES" if val else "NO"


def _section(lines, title):
    lines.append("")
    lines.append("=" * 60)
    lines.append(f"  {title}")
    lines.append("=" * 60)


# ---------------------------------------------------------------------------
# Addon detection
# ---------------------------------------------------------------------------

def _find_smokeSimLab():
    """Return (addon_module, smoke_settings_or_None, status_string)."""
    # Check if smoke_settings is registered on the scene
    scene = bpy.context.scene
    if hasattr(scene, 'smoke_settings'):
        s = scene.smoke_settings
        # Verify it's actually SmokeSettings, not an unrelated property
        if hasattr(s, 'output_path') and hasattr(s, 'job_log_items'):
            # Try to get the module
            mod = sys.modules.get('SmokeSimLab')
            return mod, s, "LOADED"

    # Not on scene — check if it's installed but not registered
    installed = any(
        'SmokeSimLab' in addon
        for addon in bpy.context.preferences.addons.keys()
    )
    if installed:
        return None, None, "INSTALLED_BUT_NOT_REGISTERED"

    # Check if module is importable (script-style install)
    try:
        import importlib
        mod = importlib.import_module('SmokeSimLab')
        return mod, None, "IMPORTABLE_NOT_REGISTERED"
    except ImportError:
        pass

    # Check if the scripts folder is in sys.path
    scripts_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'scripts', 'SmokeSimLab'
    )
    scripts_dir = os.path.normpath(scripts_dir)
    if os.path.isdir(scripts_dir):
        return None, None, f"FOUND_ON_DISK_NOT_IN_PATH ({scripts_dir})"

    return None, None, "NOT_FOUND"


# ---------------------------------------------------------------------------
# Main inspection
# ---------------------------------------------------------------------------

lines = []
lines.append("SmokeSimLab Pre-flight Inspector")
lines.append(f"Blend   : {bpy.data.filepath or '(unsaved)'}")
lines.append(f"Blender : {bpy.app.version_string}")
lines.append(f"Python  : {sys.version.split()[0]}")

# ── 1. Addon status ─────────────────────────────────────────────────────────
_section(lines, "1. SmokeSimLab Addon Status")

_addon_mod, _settings, _addon_status = _find_smokeSimLab()
lines.append(f"  Status : {_addon_status}")

if _addon_status == "INSTALLED_BUT_NOT_REGISTERED":
    lines.append("  ACTION : Enable SmokeSimLab in Preferences > Add-ons, then")
    lines.append("           re-run this script.")
elif _addon_status == "NOT_FOUND":
    lines.append("  ACTION : Install SmokeSimLab via Preferences > Add-ons > Install.")
elif _addon_status.startswith("FOUND_ON_DISK"):
    lines.append("  ACTION : Install SmokeSimLab as an addon or add its scripts/")
    lines.append("           folder to Blender's script paths.")

if _settings is not None:
    s = _settings
    lines.append(f"\n  output_path       : {s.output_path!r}")
    lines.append(f"  use_existing_cache: {s.use_existing_cache}")
    lines.append(f"  resolution        : {s.resolution}")
    lines.append(f"  voxel_size        : {getattr(s, 'voxel_size', 'n/a')}")
    lines.append(f"  add_amount        : {s.add_amount}")
    lines.append(f"  buoyancy          : {s.buoyancy}")
    lines.append(f"  density           : {s.density}")
    lines.append(f"  noise_strength    : {s.noise_strength}")
    lines.append(f"  noise_scale       : {s.noise_scale}")
    lines.append(f"  frame_start/end   : {bpy.context.scene.frame_start} – {bpy.context.scene.frame_end}")
    lines.append(f"  job_log_items     : {len(s.job_log_items)} saved")

    # Compute expected job name from current (single) settings
    if _addon_mod and hasattr(_addon_mod, 'make_name'):
        try:
            _p = {
                'resolution':     s.resolution,
                'voxel_size':     getattr(s, 'voxel_size', 0.0),
                'add_amount':     s.add_amount,
                'buoyancy':       s.buoyancy,
                'density':        s.density,
                'noise_strength': s.noise_strength,
                'noise_scale':    s.noise_scale,
            }
            _expected_name  = _addon_mod.make_name(_p)
            _out_abs        = bpy.path.abspath(s.output_path) if s.output_path else ""
            _expected_cache = os.path.join(_out_abs, "Cache", _expected_name) if _out_abs else "(no output_path)"
            lines.append(f"\n  Expected job name : {_expected_name}")
            lines.append(f"  Expected cache    : {_expected_cache}")
        except Exception as e:
            lines.append(f"\n  (could not compute expected job name: {e})")
            _expected_name  = None
            _expected_cache = None
    else:
        _expected_name  = None
        _expected_cache = None
else:
    _expected_name  = None
    _expected_cache = None
    s               = None

# ── 2. Fluid domains ────────────────────────────────────────────────────────
_section(lines, "2. Fluid Domains in Scene")

domains_found = []
for obj in bpy.data.objects:
    for mod in obj.modifiers:
        if mod.type != 'FLUID':
            continue

        # Blender 3+ API: mod.fluid_type
        fluid_type = getattr(mod, 'fluid_type', None)
        # Older fallback via fluid_settings
        if fluid_type is None:
            fs = getattr(mod, 'fluid_settings', None)
            fluid_type = getattr(fs, 'type', None) if fs else None

        if fluid_type not in ('DOMAIN', None):
            continue  # skip FLOW / EFFECTOR / etc.

        domain_settings = getattr(mod, 'domain_settings', None)
        if domain_settings is None:
            # Older API fallback
            fs = getattr(mod, 'fluid_settings', None)
            if fs and getattr(fs, 'type', '') == 'DOMAIN':
                domain_settings = fs

        if domain_settings is None:
            continue

        cache_dir     = getattr(domain_settings, 'cache_directory', '') or ''
        cache_dir_abs = bpy.path.abspath(cache_dir)
        exists        = os.path.isdir(cache_dir_abs)
        file_count    = _count_data_files(cache_dir_abs) if exists else 0

        # BUG-004: would reassigning the same path reinitialize Mantaflow?
        #   v0.2.15+ guard skips assignment when paths are equal — but only
        #   when the worker's effective_cache_dir equals cache_directory.
        #   If SmokeSimLab will assign a DIFFERENT path (e.g. current is 'm2'
        #   but job needs 'R128_V0.0_...'), the assignment is necessary and
        #   will destroy any files currently at cache_dir_abs.
        if _expected_cache:
            _same_as_expected = _norm(cache_dir_abs) == _norm(_expected_cache)
            _bug004_note = (
                "same path — BUG-004 guard will SKIP assignment (VDB safe)"
                if _same_as_expected
                else "DIFFERENT path — assignment will run, VDB files at current dir may be deleted"
            )
        else:
            _bug004_note = "cannot compare — addon settings not loaded"

        lines.append(f"\nObject        : {obj.name}")
        lines.append(f"  Modifier      : {mod.name} ({mod.type})")
        lines.append(f"  Domain type   : {getattr(domain_settings, 'domain_type', 'n/a')}")
        lines.append(f"  cache_dir raw : {cache_dir!r}")
        lines.append(f"  cache_dir abs : {cache_dir_abs}")
        lines.append(f"  Dir exists    : {_yesno(exists)}")
        lines.append(f"  Data files    : {file_count}")
        lines.append(f"  BUG-004       : {_bug004_note}")

        if _expected_cache:
            match = _norm(cache_dir_abs) == _norm(_expected_cache)
            lines.append(f"  Path match    : {_yesno(match)} vs expected {_expected_cache}")

        domains_found.append({
            'obj':        obj.name,
            'cache_dir':  cache_dir_abs,
            'exists':     exists,
            'file_count': file_count,
        })

if not domains_found:
    lines.append("  (no fluid domains found in scene)")

# ── 3. Cache directory inventory ────────────────────────────────────────────
_section(lines, "3. Cache Directory Inventory")

_out_abs = ""
if s and s.output_path:
    _out_abs = bpy.path.abspath(s.output_path)

if _out_abs:
    _cache_root = os.path.join(_out_abs, "Cache")
    if os.path.isdir(_cache_root):
        _subdirs = sorted(os.listdir(_cache_root))
        if _subdirs:
            lines.append(f"  Cache root: {_cache_root}")
            for sub in _subdirs:
                sub_path   = os.path.join(_cache_root, sub)
                sub_count  = _count_data_files(sub_path)
                marker     = " ← current domain" if any(
                    _norm(d['cache_dir']) == _norm(sub_path) for d in domains_found
                ) else ""
                expected_m = " ← expected for current settings" if (
                    _expected_cache and _norm(sub_path) == _norm(_expected_cache)
                ) else ""
                lines.append(f"    {sub:40s}  {sub_count:5d} files{marker}{expected_m}")
        else:
            lines.append(f"  Cache root exists but is empty: {_cache_root}")
    else:
        lines.append(f"  Cache root does not exist yet: {_cache_root}")
        lines.append("  (first batch run will create it)")
else:
    lines.append("  (output_path not set — cannot locate Cache root)")

# ── 4. Summary ──────────────────────────────────────────────────────────────
_section(lines, "4. Summary")

_warn = 0
for d in domains_found:
    if not d['exists']:
        lines.append(f"  WARN: {d['obj']} cache dir missing — full bake will run")
        _warn += 1
    elif d['file_count'] == 0:
        lines.append(f"  INFO: {d['obj']} cache is empty — full bake will run")
        _warn += 1
    else:
        lines.append(f"  OK  : {d['obj']} cache has {d['file_count']} data files")

if not domains_found:
    lines.append("  WARN: No fluid domains found in scene")
    _warn += 1

if _addon_status != "LOADED":
    lines.append(f"  WARN: SmokeSimLab addon not loaded — parameter check skipped ({_addon_status})")
    _warn += 1

lines.append("")
lines.append(f"  {'Ready for export batch.' if _warn == 0 else f'{_warn} item(s) need attention — see above.'}")
lines.append("")

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

report_text = "\n".join(lines)
print(report_text)

block_name = "preflight_report"
if block_name in bpy.data.texts:
    bpy.data.texts[block_name].clear()
else:
    bpy.data.texts.new(block_name)
bpy.data.texts[block_name].write(report_text)

for area in bpy.context.screen.areas:
    if area.type == 'TEXT_EDITOR':
        area.spaces.active.text = bpy.data.texts[block_name]
        break

print("[Inspector] Done — report in 'preflight_report' text block")
