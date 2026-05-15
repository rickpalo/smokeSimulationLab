"""SmokeSimLab Pre-flight Inspector
====================================
Paste this script into Blender's Scripting workspace and click "Run Script".
Output appears in this same text editor (as "preflight_report") and in the
System Console (Window > Toggle System Console on Windows).

What it checks:
  1. SmokeSimLab addon status and current parameter settings
  2. Expected cache path for the current (single-value) settings
  3. Every fluid domain in the scene — type, cache path, exists, file count
  4. Whether domain cache_directory matches the expected SmokeSimLab path
  5. Cache directory inventory (all subdirs under output_path/Cache/)

Requires SmokeSimLab to be ENABLED in Preferences > Add-ons.
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
    """Return (addon_module_or_None, smoke_settings_or_None, status_string)."""
    scene = bpy.context.scene

    # Primary check: is the addon registered on the scene?
    if hasattr(scene, 'smoke_settings'):
        s = scene.smoke_settings
        if hasattr(s, 'output_path') and hasattr(s, 'job_log_items'):
            mod = sys.modules.get('SmokeSimLab')
            return mod, s, "LOADED"

    # Installed but not registered (disabled in Preferences)
    if any('SmokeSimLab' in k for k in bpy.context.preferences.addons.keys()):
        return None, None, "INSTALLED_BUT_NOT_REGISTERED"

    # Importable via sys.path (not installed as an addon)
    try:
        import importlib
        mod = importlib.import_module('SmokeSimLab')
        return mod, None, "IMPORTABLE_NOT_REGISTERED"
    except ImportError:
        pass

    return None, None, "NOT_FOUND"


def _baseline_params(s, mod):
    """Build a single-value param dict from current SmokeSettings.

    Uses expand_param() if the module is available, otherwise falls back to
    reading the *_begin attribute directly (same as expand_param does when no
    range/list mode is active).
    """
    _SWEEP = [
        "resolution", "vorticity", "alpha", "beta",
        "dissolve_speed", "noise_upres", "noise_strength", "noise_spatial_scale",
    ]
    if mod and hasattr(mod, 'expand_param'):
        vals = {name: mod.expand_param(s, name)[0] for name in _SWEEP}
    else:
        vals = {name: getattr(s, name + "_begin") for name in _SWEEP}

    vals["use_dissolve"]  = s.use_dissolve
    vals["slow_dissolve"] = getattr(s, 'slow_dissolve', False)
    vals["use_noise"]     = s.use_noise
    return vals


# ---------------------------------------------------------------------------
# Main inspection
# ---------------------------------------------------------------------------

lines = []
lines.append("SmokeSimLab Pre-flight Inspector")
lines.append(f"Blend   : {bpy.data.filepath or '(unsaved)'}")
lines.append(f"Blender : {bpy.app.version_string}")
lines.append(f"Python  : {sys.version.split()[0]}")

# ── 1. Addon / settings ─────────────────────────────────────────────────────
_section(lines, "1. SmokeSimLab Addon & Current Settings")

_mod, _s, _status = _find_smokeSimLab()
lines.append(f"  Addon status : {_status}")

if _status == "INSTALLED_BUT_NOT_REGISTERED":
    lines.append("  ACTION : Enable SmokeSimLab in Preferences > Add-ons, then re-run.")
elif _status == "NOT_FOUND":
    lines.append("  ACTION : Install SmokeSimLab via Preferences > Add-ons > Install.")

_expected_cache = None
_expected_name  = None

if _s is not None:
    s = _s
    lines.append(f"  output_path      : {s.output_path!r}")
    lines.append(f"  use_existing_cache: {s.use_existing_cache}")
    lines.append(f"  frame range      : {bpy.context.scene.frame_start} – {bpy.context.scene.frame_end}")
    lines.append(f"  job_log_items    : {len(s.job_log_items)} saved")
    lines.append("")

    # Current single-value parameters (using _begin attributes)
    _DISPLAY = [
        ("resolution",         "resolution_begin",         "int"),
        ("vorticity",          "vorticity_begin",          "float"),
        ("alpha",              "alpha_begin",              "float"),
        ("beta",               "beta_begin",               "float"),
        ("use_dissolve",       "use_dissolve",             "bool"),
        ("dissolve_speed",     "dissolve_speed_begin",     "int"),
        ("use_noise",          "use_noise",                "bool"),
        ("noise_upres",        "noise_upres_begin",        "int"),
        ("noise_strength",     "noise_strength_begin",     "float"),
        ("noise_spatial_scale","noise_spatial_scale_begin","float"),
    ]
    for label, attr, kind in _DISPLAY:
        val = getattr(s, attr, "n/a")
        lines.append(f"  {label:22s}: {val}")

    # Compute expected job name and cache path
    try:
        p = _baseline_params(s, _mod)
        if _mod and hasattr(_mod, 'make_name'):
            _expected_name = _mod.make_name(p)
        else:
            # Inline make_name for when the module isn't accessible
            _dissolve = (f"D{int(p['dissolve_speed'])}" if p['use_dissolve'] else "D-OFF")
            _noise    = (
                f"N{int(p['noise_upres'])}_NS{round(p['noise_strength'],2)}_SC{round(p['noise_spatial_scale'],2)}"
                if p['use_noise'] else "N-OFF"
            )
            _expected_name = (
                f"R{int(p['resolution'])}_V{round(p['vorticity'],2)}_"
                f"A{round(p['alpha'],2)}_B{round(p['beta'],2)}_"
                f"{_dissolve}_{_noise}"
            )

        _out_abs = bpy.path.abspath(s.output_path) if s.output_path else ""
        _expected_cache = os.path.join(_out_abs, "Cache", _expected_name) if _out_abs else None

        lines.append("")
        lines.append(f"  Expected job name : {_expected_name}")
        lines.append(f"  Expected cache    : {_expected_cache or '(no output_path)'}")
    except Exception as e:
        lines.append(f"\n  WARNING: could not compute expected job name: {e}")
else:
    s = None

# ── 2. Expected cache status ─────────────────────────────────────────────────
_section(lines, "2. Expected Cache Status")

# The worker sets cache_directory at runtime; the domain's current path is
# irrelevant here.  What matters is whether the expected path already has files.

domains_found = []
if _expected_cache:
    _exp_exists     = os.path.isdir(_expected_cache)
    _exp_file_count = _count_data_files(_expected_cache) if _exp_exists else 0
    lines.append(f"  Expected cache path : {_expected_cache}")
    lines.append(f"  Directory exists    : {_yesno(_exp_exists)}")
    lines.append(f"  Data files found    : {_exp_file_count}")
    if _exp_file_count > 0:
        lines.append(f"  → With 'Use Existing Cache' ON:  worker will attempt to reuse / resume")
    else:
        lines.append(f"  → Worker will run a full bake (no reusable data found)")
else:
    lines.append("  (cannot compute — addon not loaded or output_path not set)")

# Collect domain info (informational only — path set by worker at runtime).
lines.append("")
lines.append("  Fluid domain objects (informational — cache_directory set by worker):")
for obj in bpy.data.objects:
    for mod in obj.modifiers:
        if mod.type != 'FLUID':
            continue

        fluid_type = getattr(mod, 'fluid_type', None)
        if fluid_type is None:
            fs = getattr(mod, 'fluid_settings', None)
            fluid_type = getattr(fs, 'type', None) if fs else None

        if fluid_type not in ('DOMAIN', None):
            continue

        domain_settings = getattr(mod, 'domain_settings', None)
        if domain_settings is None:
            fs = getattr(mod, 'fluid_settings', None)
            if fs and getattr(fs, 'type', '') == 'DOMAIN':
                domain_settings = fs

        if domain_settings is None:
            continue

        cache_dir     = getattr(domain_settings, 'cache_directory', '') or ''
        cache_dir_abs = bpy.path.abspath(cache_dir)

        lines.append(f"    {obj.name} [{mod.name}]  current cache_dir: {cache_dir_abs!r}")

        domains_found.append({
            'obj':        obj.name,
            'cache_dir':  cache_dir_abs,
            'exists':     os.path.isdir(cache_dir_abs),
            'file_count': _count_data_files(cache_dir_abs) if os.path.isdir(cache_dir_abs) else 0,
        })

if not domains_found:
    lines.append("    (no fluid domains found in scene)")

# ── 3. Cache directory inventory ────────────────────────────────────────────
_section(lines, "3. Cache Directory Inventory")

_out_abs = bpy.path.abspath(s.output_path) if (s and s.output_path) else ""
if _out_abs:
    _cache_root = os.path.join(_out_abs, "Cache")
    if os.path.isdir(_cache_root):
        _subdirs = sorted(
            d for d in os.listdir(_cache_root)
            if os.path.isdir(os.path.join(_cache_root, d))
        )
        if _subdirs:
            lines.append(f"  Root: {_cache_root}")
            for sub in _subdirs:
                sub_path = os.path.join(_cache_root, sub)
                sub_cnt  = _count_data_files(sub_path)
                tags = []
                if any(_norm(d['cache_dir']) == _norm(sub_path) for d in domains_found):
                    tags.append("← domain")
                if _expected_cache and _norm(sub_path) == _norm(_expected_cache):
                    tags.append("← expected")
                tag_str = "  " + "  ".join(tags) if tags else ""
                lines.append(f"    {sub:<44s} {sub_cnt:5d} files{tag_str}")
        else:
            lines.append(f"  Cache root is empty: {_cache_root}")
    else:
        lines.append(f"  Cache root not yet created: {_cache_root}")
        lines.append("  (first batch run will create it)")
else:
    lines.append("  (output_path not set — cannot locate Cache root)")

# ── 4. Summary ──────────────────────────────────────────────────────────────
_section(lines, "4. Summary")

_warn = 0
if _status != "LOADED":
    lines.append(f"  WARN  Addon not loaded ({_status}) — parameter check skipped")
    _warn += 1

if not domains_found:
    lines.append("  WARN  No fluid domains found in scene")
    _warn += 1

if _expected_cache:
    if _exp_file_count > 0:
        lines.append(f"  OK    Expected cache has {_exp_file_count} data files — bake can be reused")
    else:
        lines.append(f"  INFO  Expected cache is empty or missing — full bake will run")
else:
    lines.append(f"  INFO  Expected cache path unknown — cannot assess bake status")

lines.append("")
if _warn == 0:
    lines.append("  Ready for export batch.")
else:
    lines.append(f"  {_warn} warning(s) — see above.")
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
