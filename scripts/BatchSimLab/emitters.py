"""
emitters.py — fluid-emitter discovery + sync for BatchSimLab (TODO-58 module #2).

Extracted from ``__init__.py`` as the second module split.  Despite being the
"scene-scan" layer, every helper here is duck-typed on the scene/object/settings
arguments it is passed (``obj.modifiers``, ``obj.bound_box``, ``obj.matrix_world``,
``s.emitters``) and never touches the ``bpy`` module directly — so, like
``jobgen``, the whole surface is unit-testable with stub objects and no running
Blender.

Contents:
  * ``_blend_domain_resolution`` — the domain's saved resolution_max (density
    scaling denominator).
  * Discovery: ``_is_flow_object`` / ``find_fluid_emitters`` / ``_world_aabb`` /
    ``_aabb_overlap`` / ``emitters_inside_domain`` / ``find_emitters``.
  * Sync: ``_emitter_sync_plan`` (pure reconcile) + ``_flow_settings_of`` /
    ``_seed_emitter_from_flow`` / ``_populate_emitters`` (collection mutation),
    driven by the ``_EMITTER_FLOW_IMPORT_MAP``.

Depends only on the leaf ``jobgen`` module for the shared velocity-text helpers.
``__init__.py`` re-imports every public name from here so existing call sites and
the TODO-55 emitter tests keep resolving against the package namespace.
"""
from .jobgen import _VELOCITY_DEFAULT, _format_velocity_vector


def _blend_domain_resolution(domain_obj):
    """Return the domain's current resolution_max as stored in the .blend file.

    This is the resolution the scene was saved at — used as the denominator
    when scaling emitter density for different-resolution jobs.  Returns 0 if
    the object has no FLUID DOMAIN modifier (caller should treat 0 as 'unknown').
    """
    if domain_obj:
        for mod in domain_obj.modifiers:
            if mod.type == 'FLUID' and mod.fluid_type == 'DOMAIN':
                return mod.domain_settings.resolution_max
    return 0


# ---------------------------------------------------------------------------
# v0.9.0 TODO-55: emitter (flow object) discovery
#
# A fluid DOMAIN keeps NO backlink to its flow objects — Mantaflow simply
# includes every scene object that has a FLUID modifier of fluid_type 'FLOW'
# and whose geometry overlaps the domain.  So we discover emitters by scanning
# the scene (find_fluid_emitters), then filter to those whose world-space
# bounding box overlaps the domain's (emitters_inside_domain).  BatchSimLab is
# SINGLE-DOMAIN only — one domain per scene; a second domain would need
# per-domain emitter attribution and is intentionally out of scope.
#
# These helpers are deliberately pure (no bpy / mathutils import) so they are
# unit-testable with stub objects:
#   obj.modifiers     — iterable of objects with .type / .fluid_type
#   obj.bound_box     — 8 (x, y, z) corners in local space
#   obj.matrix_world  — any 4x4 row-indexable matrix (mathutils.Matrix at
#                       runtime; a nested list in tests)
# ---------------------------------------------------------------------------

def _is_flow_object(obj):
    """True if *obj* has a FLUID modifier configured as a flow/emitter."""
    try:
        return any(m.type == 'FLUID' and m.fluid_type == 'FLOW'
                   for m in obj.modifiers)
    except AttributeError:
        return False


def find_fluid_emitters(scene):
    """Return all fluid-emitter (FLOW) objects in *scene*, sorted by name.

    Step 1 of TODO-55 discovery: a pure modifier scan, NOT yet filtered to a
    domain.  Deterministic name order keeps the per-emitter UI sections and
    job-dict keys stable across re-exports.
    """
    try:
        objs = list(scene.objects)
    except AttributeError:
        return []
    emitters = [o for o in objs if _is_flow_object(o)]
    emitters.sort(key=lambda o: getattr(o, "name", ""))
    return emitters


def _world_aabb(obj):
    """Return (min_xyz, max_xyz) world-space axis-aligned bounds for *obj*.

    Transforms the 8 local-space `bound_box` corners by `matrix_world` (a plain
    affine multiply, so any 4x4 row-indexable matrix works) and takes the
    component-wise min/max.  Returns None when bounds can't be computed.
    """
    try:
        corners = list(obj.bound_box)
        m = obj.matrix_world
    except AttributeError:
        return None
    if not corners or m is None:
        return None
    xs, ys, zs = [], [], []
    for c in corners:
        x, y, z = c[0], c[1], c[2]
        xs.append(m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3])
        ys.append(m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3])
        zs.append(m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3])
    return ((min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs)))


def _aabb_overlap(a, b):
    """True if two world-space AABBs intersect on all three axes.

    Each AABB is ((minx, miny, minz), (maxx, maxy, maxz)).  Touching faces
    count as overlapping.  None for either box → False.
    """
    if a is None or b is None:
        return False
    (amin, amax) = a
    (bmin, bmax) = b
    return all(amin[i] <= bmax[i] and bmin[i] <= amax[i] for i in range(3))


def emitters_inside_domain(emitters, domain_obj):
    """Step 2 of TODO-55 discovery: keep only emitters whose world bounds
    overlap the domain's world bounds.

    If the domain's bounds can't be computed (no bound_box / matrix_world),
    return *emitters* unchanged — better to over-include than to silently drop
    every emitter when containment can't be measured.
    """
    dom = _world_aabb(domain_obj) if domain_obj is not None else None
    if dom is None:
        return list(emitters)
    return [e for e in emitters if _aabb_overlap(_world_aabb(e), dom)]


def find_emitters(scene, domain_obj):
    """TODO-55 discovery composed: all scene FLOW objects, filtered to those
    inside *domain_obj*'s bounds, sorted by name (single-domain addon)."""
    return emitters_inside_domain(find_fluid_emitters(scene), domain_obj)


def _emitter_sync_plan(existing_names, desired_names):
    """Reconcile the `emitters` collection with the currently-discovered set.

    Returns (to_add, to_remove): emitter names to append (newly discovered) and
    names to drop (object gone / no longer inside the domain).  EXISTING
    elements are preserved so a user's in-progress sweep config survives a
    Refresh — only genuinely new/stale emitters are touched.  `to_add` follows
    `desired_names` order; `to_remove` follows `existing_names` order.  Pure /
    unit-testable; the bpy collection mutation lives in `_populate_emitters`.
    """
    existing = list(existing_names)
    desired  = list(desired_names)
    to_add    = [n for n in desired  if n not in existing]
    to_remove = [n for n in existing if n not in desired]
    return to_add, to_remove


# Map: addon EmitterSettings scalar name → bpy.types.FluidFlowSettings attr.
# (Names happen to match 1:1, but keep the map explicit so a future rename or a
# differently-named Blender attr is a one-line change.)
_EMITTER_FLOW_IMPORT_MAP = (
    ("temperature",      "temperature"),       # Initial Temperature
    ("density",          "density"),           # Density
    ("surface_distance", "surface_distance"),  # Surface Emission
    ("volume_density",   "volume_density"),    # Volume Emission
    ("velocity_factor",  "velocity_factor"),   # Source (velocity)
    ("velocity_normal",  "velocity_normal"),   # Normal (velocity)
)


def _flow_settings_of(obj):
    """Return the FluidFlowSettings of *obj*'s FLOW modifier, or None."""
    try:
        for m in obj.modifiers:
            if m.type == 'FLUID' and m.fluid_type == 'FLOW':
                return m.flow_settings
    except AttributeError:
        pass
    return None


def _seed_emitter_from_flow(em, flow):
    """Seed an EmitterSettings element's baseline (_begin) values from the
    emitter's CURRENT flow settings (TODO-55 section B auto-populate).

    Sweep config (_end/_step/_use_range/_use_list/_list) is left at defaults —
    only baselines + the velocity seed are written.  Each read is guarded so a
    liquid flow object or an older Blender build missing an attr is skipped
    rather than crashing.  No-op when *flow* is None.
    """
    if flow is None:
        return
    for addon_name, flow_attr in _EMITTER_FLOW_IMPORT_MAP:
        try:
            setattr(em, addon_name + "_begin", float(getattr(flow, flow_attr)))
        except (AttributeError, TypeError, ValueError):
            continue
    try:
        em.use_initial_velocity = bool(getattr(flow, "use_initial_velocity"))
    except (AttributeError, TypeError):
        pass
    # Seed the velocity vector list with the emitter's current Initial X/Y/Z.
    try:
        coord = getattr(flow, "velocity_coord")
        vec = (float(coord[0]), float(coord[1]), float(coord[2]))
    except (AttributeError, TypeError, IndexError, ValueError):
        vec = _VELOCITY_DEFAULT
    em.velocity_list.clear()
    item = em.velocity_list.add()
    item.text = _format_velocity_vector(vec)


def _populate_emitters(s, scene):
    """Sync `s.emitters` with the flow objects discovered inside the domain.

    Adds an element per newly-discovered emitter (seeded from its live flow
    settings), removes elements whose object is gone / no longer inside the
    domain, and leaves existing elements — and their in-progress sweep config —
    untouched.  Safe to call repeatedly (Refresh Emitters button + domain
    select).  No-op-ish when there's no scene.
    """
    domain = getattr(s, "domain_obj", None)
    objs = find_emitters(scene, domain) if scene is not None else []
    by_name = {o.name: o for o in objs}
    to_add, to_remove = _emitter_sync_plan(
        [em.name for em in s.emitters], list(by_name.keys()))

    if to_remove:
        remove_set = set(to_remove)
        for i in range(len(s.emitters) - 1, -1, -1):
            if s.emitters[i].name in remove_set:
                s.emitters.remove(i)

    for name in to_add:
        em = s.emitters.add()
        em.name = name
        _seed_emitter_from_flow(em, _flow_settings_of(by_name[name]))

