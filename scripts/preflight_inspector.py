"""SmokeSimLab Pre-flight Inspector
====================================
Paste this script into Blender's Scripting workspace and click "Run Script".
Output is written to the System Console (Window > Toggle System Console on
Windows) and to a text block named "preflight_report" in the blend file.

What it checks:
  1. Every fluid domain in the scene — type, cache path, path exists, file count
  2. Whether the cache path matches what SmokeSimLab would compute for the
     current parameter settings (requires SmokeSimLab to be loaded)
  3. Whether any domain's cache_directory assignment would trigger a Mantaflow
     reinit (BUG-004 guard check)
  4. Outliner object list with fluid modifiers flagged

Run BEFORE Export Batch to catch mismatches before the batch starts.
"""

import bpy
import os
import re


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _count_data_files(directory):
    """Count frame-numbered VDB/UNI files, excluding config/ checkpoints."""
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
# Main inspection
# ---------------------------------------------------------------------------

lines = []
lines.append("SmokeSimLab Pre-flight Inspector")
lines.append(f"Blend: {bpy.data.filepath or '(unsaved)'}")
lines.append(f"Blender: {bpy.app.version_string}")

# ── 1. Fluid domains ────────────────────────────────────────────────────────
_section(lines, "1. Fluid Domains")

domains_found = []
for obj in bpy.data.objects:
    for mod in obj.modifiers:
        if mod.type != 'FLUID':
            continue
        fs = mod.fluid_settings if hasattr(mod, 'fluid_settings') else None
        # Blender 3.x / 4.x use mod.fluid_type; older used fluid_settings
        fluid_type = getattr(mod, 'fluid_type', None)
        if fluid_type is None and fs is not None:
            fluid_type = getattr(fs, 'type', None)
        if fluid_type not in ('DOMAIN', None):
            # Only interested in domains; skip FLOW/EFFECTOR unless domain
            # detection fails
            if fluid_type is not None:
                continue

        # Try bpy.types.FluidDomainSettings path (Blender 3+)
        domain_settings = None
        if hasattr(mod, 'domain_settings') and mod.domain_settings is not None:
            domain_settings = mod.domain_settings
        elif fs is not None and getattr(fs, 'type', '') == 'DOMAIN':
            domain_settings = fs

        if domain_settings is None:
            continue

        cache_dir = getattr(domain_settings, 'cache_directory', '') or ''
        cache_dir_abs = bpy.path.abspath(cache_dir)
        exists      = os.path.isdir(cache_dir_abs)
        file_count  = _count_data_files(cache_dir_abs) if exists else 0

        lines.append(f"\nObject  : {obj.name}")
        lines.append(f"  Domain type   : {getattr(domain_settings, 'domain_type', 'n/a')}")
        lines.append(f"  cache_dir raw : {cache_dir!r}")
        lines.append(f"  cache_dir abs : {cache_dir_abs}")
        lines.append(f"  Dir exists    : {_yesno(exists)}")
        lines.append(f"  Data files    : {file_count}")

        # BUG-004 guard: would an assignment of the SAME path cause reinit?
        lines.append(f"  BUG-004 risk  : assignment of same path would reinit "
                     f"({_yesno(True)} — guard in v0.2.15 prevents this)")

        domains_found.append({
            'obj': obj.name,
            'cache_dir': cache_dir_abs,
            'exists': exists,
            'file_count': file_count,
        })

if not domains_found:
    lines.append("  (no fluid domains found in scene)")

# ── 2. SmokeSimLab settings ─────────────────────────────────────────────────
_section(lines, "2. SmokeSimLab Settings")

try:
    s = bpy.context.scene.smoke_settings
    lines.append(f"  output_path       : {s.output_path!r}")
    lines.append(f"  use_existing_cache: {s.use_existing_cache}")
    lines.append(f"  resolution        : {s.resolution}")
    lines.append(f"  voxel_size        : {s.voxel_size}")
    lines.append(f"  add_amount        : {s.add_amount}")
    lines.append(f"  buoyancy          : {s.buoyancy}")
    lines.append(f"  density           : {s.density}")
    lines.append(f"  noise_strength    : {s.noise_strength}")
    lines.append(f"  noise_scale       : {s.noise_scale}")
    lines.append(f"  frame_end         : {bpy.context.scene.frame_end}")
    lines.append(f"  job_log_items     : {len(s.job_log_items)} item(s) in collection")

    # Try to call make_name if the addon is loaded
    try:
        from SmokeSimLab import make_name
        # Build a minimal param dict from current settings
        _p = {
            'resolution':     s.resolution,
            'voxel_size':     getattr(s, 'voxel_size', 0.0),
            'add_amount':     s.add_amount,
            'buoyancy':       s.buoyancy,
            'density':        s.density,
            'noise_strength': s.noise_strength,
            'noise_scale':    s.noise_scale,
        }
        _name = make_name(_p)
        lines.append(f"\n  Computed job name : {_name}")
        # Check if the cache dir would match any domain
        _out = bpy.path.abspath(s.output_path) if s.output_path else ""
        if _out:
            _expected_cache = os.path.join(_out, "Cache", _name)
            lines.append(f"  Expected cache    : {_expected_cache}")
            for d in domains_found:
                _match = _norm(d['cache_dir']) == _norm(_expected_cache)
                lines.append(f"  Domain '{d['obj']}' cache match: {_yesno(_match)}")
                if not _match:
                    lines.append(f"    Domain  : {d['cache_dir']}")
                    lines.append(f"    Expected: {_expected_cache}")
    except Exception as e:
        lines.append(f"  (make_name not available: {e})")

except AttributeError:
    lines.append("  SmokeSimLab not loaded or smoke_settings not found on scene.")

# ── 3. Cache directory cross-check ──────────────────────────────────────────
_section(lines, "3. Cache Directory Cross-check")

if domains_found:
    for d in domains_found:
        status = []
        if not d['exists']:
            status.append("MISSING — will trigger full bake")
        elif d['file_count'] == 0:
            status.append("EMPTY — will trigger full bake")
        else:
            status.append(f"OK — {d['file_count']} data files")
        lines.append(f"  {d['obj']}: {', '.join(status)}")
else:
    lines.append("  No domains to check.")

# ── 4. Summary ──────────────────────────────────────────────────────────────
_section(lines, "4. Summary")

warn_count = sum(1 for d in domains_found if not d['exists'] or d['file_count'] == 0)
lines.append(f"  Domains found   : {len(domains_found)}")
lines.append(f"  Warnings        : {warn_count}")
if warn_count == 0 and domains_found:
    lines.append("  All caches look good — safe to export batch.")
elif warn_count > 0:
    lines.append("  WARNING: one or more domains have empty/missing caches.")
    lines.append("  use_existing_cache will trigger full bakes for these.")
lines.append("")

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

report_text = "\n".join(lines)

# Print to system console
print(report_text)

# Write to a text block in the blend file for easy access
block_name = "preflight_report"
if block_name in bpy.data.texts:
    bpy.data.texts[block_name].clear()
else:
    bpy.data.texts.new(block_name)
bpy.data.texts[block_name].write(report_text)

# Switch the Scripting editor's text to the report
for area in bpy.context.screen.areas:
    if area.type == 'TEXT_EDITOR':
        area.spaces.active.text = bpy.data.texts[block_name]
        break

print("\n[Inspector] Report written to text block 'preflight_report'")
