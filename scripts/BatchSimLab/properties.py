"""
BatchSimLab/properties.py
=========================
TODO-58 module #5: the ``bpy.props`` PropertyGroups (ValueItem, VelocityItem,
EmitterSettings, SmokeJobItem, SmokeSettings) and the class-body callback
factories/helpers they wire up (make_toggle_range/list, _sync_frame_defaults,
_import_domain_params + its _DOMAIN_IMPORT_MAP, _on_render_sim_result_update),
extracted verbatim from __init__.py with no behaviour change.

Re-imported by __init__.py ABOVE the registration code so existing
``from BatchSimLab import …`` entry points, the ``classes = [...]`` list, and the
test-suite resolve unchanged.  The two UIList classes that sit physically between
these PropertyGroups (SMOKE_UL_value_list, SMOKE_UL_job_log) are UI and stay in
__init__ for now (-> future ui.py).

Dependencies are one-way leaves: _import_domain_params calls _populate_emitters
(emitters.py) and SmokeSettings.settings_file_enum wires the two preset-dropdown
callbacks (settings_io.py); neither imports this module, so there is no cycle.
"""
import bpy

from .emitters import _populate_emitters
from .settings_io import _settings_files_enum_items, _on_settings_enum_update


# ---------------------------------------------------------------------------
# Toggle helpers
# ---------------------------------------------------------------------------

def make_toggle_range(name):
    """
    Return an update callback for <name>_use_range BoolProperty.
    When 'use_range' is enabled it automatically disables 'use_list' so
    only one input mode is active at a time.
    """
    def update(self, context):
        if getattr(self, name + "_use_range"):
            setattr(self, name + "_use_list", False)
    return update


def make_toggle_list(name):
    """
    Return an update callback for <name>_use_list BoolProperty.
    When 'use_list' is enabled it automatically disables 'use_range' so
    only one input mode is active at a time.
    """
    def update(self, context):
        if getattr(self, name + "_use_list"):
            setattr(self, name + "_use_range", False)
    return update


def _sync_frame_defaults(self, context):
    """Update callback for use_default_frames — copies scene range on uncheck."""
    if not self.use_default_frames and context and context.scene:
        self.sim_frame_start = context.scene.frame_start
        self.sim_frame_end   = context.scene.frame_end


# v0.7.0 TODO-40: when the user picks a fluid domain object in the addon
# panel, copy the domain's CURRENT settings into the addon's `_begin`
# (baseline) values + master toggles.  Only baseline values are touched —
# any `_end` / `_step` / `_use_range` / `_use_list` sweep configuration the
# user has already set up is preserved, so re-selecting a domain doesn't
# wipe an in-progress sweep design.
#
# Each property read is wrapped in try/except so older Blender builds or
# liquid-domain objects (missing fire/noise/dissolve attrs) don't crash
# the callback — the missing properties are just silently skipped.
#
# The Blender attribute names don't always match our addon naming:
#   d.cfl_condition           → s.cfl_number_begin
#   d.use_dissolve_smoke      → s.use_dissolve  (master toggle)
#   d.use_dissolve_smoke_log  → s.slow_dissolve (master toggle)
#   d.noise_scale             → s.noise_upres_begin
#   d.noise_pos_scale         → s.noise_spatial_scale_begin
# All others map by direct name with `_begin` suffix.
_DOMAIN_IMPORT_MAP = (
    # (Blender domain attr, addon param name).  None for the addon name
    # means the attr maps to a master toggle (handled separately).
    ("resolution_max",        "resolution"),
    ("vorticity",             "vorticity"),
    ("alpha",                 "alpha"),
    ("beta",                  "beta"),
    ("dissolve_speed",        "dissolve_speed"),
    ("noise_scale",           "noise_upres"),
    ("noise_strength",        "noise_strength"),
    ("noise_pos_scale",       "noise_spatial_scale"),
    # v0.7.0 TODO-41 gas timing
    ("time_scale",            "time_scale"),
    ("cfl_condition",         "cfl_number"),
    ("timesteps_max",         "timesteps_max"),
    ("timesteps_min",         "timesteps_min"),
    # v0.7.0 TODO-42 fire
    ("burning_rate",          "burning_rate"),
    ("flame_smoke",           "flame_smoke"),
    ("flame_vorticity",       "flame_vorticity"),
    ("flame_max_temp",        "flame_max_temp"),
    ("flame_ignition",        "flame_ignition"),
)


def _import_domain_params(self, context):
    """PointerProperty update callback for `domain_obj`.

    When the user picks a new fluid domain object, copy its current
    FluidDomainSettings into the addon's `_begin` (baseline) values and
    master toggles.  Sweep config (`_end` / `_step` / `_use_range` /
    `_use_list` / `_list`) is left untouched so an in-progress sweep
    design isn't blown away by re-selecting the same domain.

    No-op when the new selection is None, has no Fluid modifier, or has
    a non-DOMAIN fluid_type (e.g. an emitter/flow object).
    """
    obj = self.domain_obj
    if obj is None:
        return
    mod = next((m for m in obj.modifiers if m.type == 'FLUID'), None)
    if mod is None or mod.fluid_type != 'DOMAIN':
        return
    d = mod.domain_settings
    if d is None:
        return

    # Direct mapping: copy each domain attr to s.<addon_name>_begin.
    for _battr, _paddon in _DOMAIN_IMPORT_MAP:
        try:
            _val = getattr(d, _battr)
        except AttributeError:
            continue  # property absent in this Blender version
        # The addon's _begin properties are Int / Float depending on the
        # param; pass-through works because RNA does the coercion.
        try:
            setattr(self, _paddon + "_begin", _val)
        except (AttributeError, TypeError):
            continue

    # Master toggles (separately because addon attr names differ from
    # the Blender attr names).
    for _battr, _paddon in (
        ("use_dissolve_smoke",     "use_dissolve"),
        ("use_dissolve_smoke_log", "slow_dissolve"),
        ("use_noise",              "use_noise"),
        ("use_adaptive_timesteps", "use_adaptive_timesteps"),
    ):
        try:
            setattr(self, _paddon, bool(getattr(d, _battr)))
        except (AttributeError, TypeError):
            continue

    # use_fire is an addon-side override flag, not a Blender domain
    # attribute (fire is driven by flow_type on flow objects).  Probe
    # whether the domain has any fire characteristics: a non-default
    # burning_rate or flame_ignition value suggests the user intends to
    # use fire — flip use_fire on so the imported fire values get applied
    # at bake time.  False positives here are harmless (user can uncheck).
    try:
        self.use_fire = (
            float(getattr(d, "burning_rate", 0.75)) != 0.75
            or float(getattr(d, "flame_ignition", 1.5)) != 1.5
        )
    except (AttributeError, TypeError):
        pass

    # v0.9.0 TODO-55: refresh the per-emitter sections for the new domain.
    # Update callbacks must never raise, so guard broadly.
    try:
        scene = getattr(context, "scene", None) if context else None
        if scene is not None:
            _populate_emitters(self, scene)
    except Exception:
        pass


def _on_render_sim_result_update(self, _context):
    """Update callback for render_simulation_result (TODO-26).

    A bake-only run produces no renders, so "Display Results When Finished" has
    nothing to display — clear it when rendering is turned off.  Writing the
    property here (rather than in draw()) keeps the value mutation out of the
    draw pass, where RNA writes are unsafe.
    """
    if not self.render_simulation_result:
        self.show_results = False


class ValueItem(bpy.types.PropertyGroup):
    """
    Single float entry in a parameter explicit-value list.

    min_bound / max_bound are set by SMOKE_OT_add_value from the RNA hard
    limits so manually-typed values outside the parameter's allowed range
    are clamped automatically on edit.  0/0 means no limit active.
    """
    def _clamp_value(self, context):
        lo, hi = self.min_bound, self.max_bound
        if lo < hi:
            self.value = max(lo, min(hi, self.value))
        elif lo > 0 and self.value < lo:
            self.value = lo

    value:     bpy.props.FloatProperty(update=_clamp_value)
    int_value: bpy.props.IntProperty()
    marked:    bpy.props.BoolProperty(default=False)
    min_bound: bpy.props.FloatProperty(default=0.0)
    max_bound: bpy.props.FloatProperty(default=0.0)


class VelocityItem(bpy.types.PropertyGroup):
    """One Initial-Velocity vector entry, stored as an "x, y, z" string.

    v0.9.0 TODO-55: an emitter's Initial Velocity is swept as a LIST of explicit
    XYZ vectors.  Each entry holds the raw user text (validated against
    `_parse_velocity_vector`); `marked` flags the row for deletion in the UIList,
    mirroring ValueItem.
    """
    text:   bpy.props.StringProperty(
        name="Velocity",
        description='Initial velocity vector, format "x, y, z" (e.g. 0, 0, 1)',
        default="0, 0, 0",
    )
    marked: bpy.props.BoolProperty(default=False)


class EmitterSettings(bpy.types.PropertyGroup):
    """Per-emitter sweep settings — one element per discovered flow object.

    v0.9.0 TODO-55.  Held in `SmokeSettings.emitters` (a CollectionProperty),
    keyed by the flow object's `name`.  Each SCALAR emitter property reuses the
    exact same Range/List sextet as the domain params, so `expand_param()` works
    on an EmitterSettings instance with no changes:

        <p>_begin / _end / _step / _use_range / _use_list / _list / _index

    Scalars always available (map to bpy.types.FluidFlowSettings):
        temperature       → temperature        (Initial Temperature)
        density           → density            (Density)
        surface_distance  → surface_distance   (Surface Emission)
        volume_density    → volume_density     (Volume Emission)

    Gated by `use_initial_velocity` (mirrors the use_dissolve / use_noise
    gating in generate_jobs_*):
        velocity_factor   → velocity_factor    (Source — scalar)
        velocity_normal   → velocity_normal    (Normal — scalar)
        velocity_list     → velocity_coord     (Initial X/Y/Z — list of vectors)
    """
    name: bpy.props.StringProperty(default="")
    show: bpy.props.BoolProperty(
        default=False,
        description="Expand or collapse this emitter's parameters",
    )

    # Initial Temperature — flow_settings.temperature
    temperature_begin:     bpy.props.FloatProperty(default=1.0)
    temperature_end:       bpy.props.FloatProperty(default=1.0)
    temperature_step:      bpy.props.FloatProperty(default=0)
    temperature_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("temperature"))
    temperature_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("temperature"))
    temperature_list:      bpy.props.CollectionProperty(type=ValueItem)
    temperature_index:     bpy.props.IntProperty()

    # Density — flow_settings.density
    density_begin:     bpy.props.FloatProperty(default=1.0)
    density_end:       bpy.props.FloatProperty(default=1.0)
    density_step:      bpy.props.FloatProperty(default=0)
    density_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("density"))
    density_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("density"))
    density_list:      bpy.props.CollectionProperty(type=ValueItem)
    density_index:     bpy.props.IntProperty()

    # Surface Emission — flow_settings.surface_distance
    surface_distance_begin:     bpy.props.FloatProperty(default=1.5)
    surface_distance_end:       bpy.props.FloatProperty(default=1.5)
    surface_distance_step:      bpy.props.FloatProperty(default=0)
    surface_distance_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("surface_distance"))
    surface_distance_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("surface_distance"))
    surface_distance_list:      bpy.props.CollectionProperty(type=ValueItem)
    surface_distance_index:     bpy.props.IntProperty()

    # Volume Emission — flow_settings.volume_density
    volume_density_begin:     bpy.props.FloatProperty(default=0.0)
    volume_density_end:       bpy.props.FloatProperty(default=0.0)
    volume_density_step:      bpy.props.FloatProperty(default=0)
    volume_density_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("volume_density"))
    volume_density_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("volume_density"))
    volume_density_list:      bpy.props.CollectionProperty(type=ValueItem)
    volume_density_index:     bpy.props.IntProperty()

    # ── Initial Velocity (master toggle gates the three below) ───────────────
    use_initial_velocity: bpy.props.BoolProperty(
        default=False,
        description=(
            "Sweep this emitter's initial velocity — Source (factor), Normal, "
            "and the Initial X/Y/Z vector list"
        ),
    )

    # Source — flow_settings.velocity_factor (scalar)
    velocity_factor_begin:     bpy.props.FloatProperty(default=0.0)
    velocity_factor_end:       bpy.props.FloatProperty(default=0.0)
    velocity_factor_step:      bpy.props.FloatProperty(default=0)
    velocity_factor_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("velocity_factor"))
    velocity_factor_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("velocity_factor"))
    velocity_factor_list:      bpy.props.CollectionProperty(type=ValueItem)
    velocity_factor_index:     bpy.props.IntProperty()

    # Normal — flow_settings.velocity_normal (scalar)
    velocity_normal_begin:     bpy.props.FloatProperty(default=0.0)
    velocity_normal_end:       bpy.props.FloatProperty(default=0.0)
    velocity_normal_step:      bpy.props.FloatProperty(default=0)
    velocity_normal_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("velocity_normal"))
    velocity_normal_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("velocity_normal"))
    velocity_normal_list:      bpy.props.CollectionProperty(type=ValueItem)
    velocity_normal_index:     bpy.props.IntProperty()

    # Initial X/Y/Z — flow_settings.velocity_coord (list of XYZ vectors)
    velocity_list:  bpy.props.CollectionProperty(type=VelocityItem)
    velocity_index: bpy.props.IntProperty()


class SmokeJobItem(bpy.types.PropertyGroup):
    """One row in the Job Log panel section."""
    job_number: bpy.props.IntProperty(name="Job #",  default=0)
    job_name:   bpy.props.StringProperty(name="Name", default="")
    status:     bpy.props.EnumProperty(
        name="Status",
        items=[
            ('NOT_STARTED', "Not Started", ""),
            ('IN_PROGRESS', "Baking",      ""),  # active during the bake phase
            ('BAKED',       "Baked (awaiting render)", ""),
            ('RENDERING',   "Rendering",   ""),  # active during the render phase
            ('RETRYING',    "Retrying",     ""),
            ('COMPLETE',    "Complete",     ""),
            ('FAILED',      "Failed",       ""),
            ('CRASHED',     "Crashed",      ""),
        ],
        default='NOT_STARTED',
    )


class SmokeSettings(bpy.types.PropertyGroup):
    """
    All user-facing settings for BatchSimLab, stored on bpy.types.Scene.

    Storing settings on the Scene means they are saved with the .blend file
    and persist across Blender sessions.  Each iterable parameter follows
    the same naming pattern:

        <name>           — default/base value
        <name>_begin     — range start
        <name>_end       — range end
        <name>_step      — range step
        <name>_use_range — enable range mode (mutually exclusive with list)
        <name>_use_list  — enable list mode  (mutually exclusive with range)
        <name>_list      — CollectionProperty of ValueItem
        <name>_index     — active item index in the UIList
    """

    # ── Scene setup ──────────────────────────────────────────────────────────

    domain_obj: bpy.props.PointerProperty(
        type=bpy.types.Object,
        name="Domain Object",
        description=(
            "The Mantaflow fluid domain object to bake.  "
            "v0.7.0 TODO-40: selecting a domain auto-imports its current "
            "settings into the addon's baseline (_begin) values — sweep "
            "config (range/list/step) is preserved"
        ),
        update=_import_domain_params,
    )

    output_path: bpy.props.StringProperty(
        name="Output",
        description="Root output folder for cache, renders, and CSV.  Defaults "
                    "to the current .blend file's folder on load; change it to "
                    "any folder (e.g. a fast local scratch disk)",
        subtype='DIR_PATH',
        # Empty until a .blend loads; _reset_on_load fills it with the blend's
        # own folder (resolved absolute — see _default_output_path).  Replaces the
        # old hard-coded "C:/tmp"; a Python StringProperty can't store the literal
        # "//" token (Blender 5.x warns "does not support blend relative // prefix"),
        # so we store the resolved path instead.
        default="",
    )

    # ── Iteration mode ───────────────────────────────────────────────────────

    iteration_mode: bpy.props.EnumProperty(
        name="Iteration Mode",
        description=(
            "Limited Combinations: vary one parameter at a time while all "
            "others stay at their default value.  Produces far fewer jobs "
            "than All Combinations.\n\n"
            "All Combinations: full Cartesian product of all ranges.  Job "
            "count = product of all range lengths."
        ),
        items=[
            ('LIMITED', "Limited Combinations",
             "Vary one parameter at a time; all others at default"),
            ('ALL',     "All Combinations",
             "Full Cartesian product — can produce very many jobs"),
        ],
        default='LIMITED',
    )

    # ── Simulation Parameters outer collapse ─────────────────────────────────

    show_sim_params: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the entire Simulation Parameters section",
    )

    # ── v0.9.0 TODO-55: per-emitter sweep settings ───────────────────────────
    # One EmitterSettings element per flow object discovered inside the domain
    # (see find_emitters / _populate_emitters).  Populated on domain-select and
    # via the Refresh Emitters button; each gets its own collapsible UI section.
    emitters:        bpy.props.CollectionProperty(type=EmitterSettings)
    emitters_index:  bpy.props.IntProperty()
    show_emitters:   bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Emitters section",
    )

    # ── Frame range ───────────────────────────────────────────────────────────

    use_default_frames: bpy.props.BoolProperty(
        name="Use Default Frames",
        default=True,
        description="Use the .blend scene frame range; uncheck to override",
        update=_sync_frame_defaults,
    )
    sim_frame_start: bpy.props.IntProperty(
        name="Frame Start",
        default=1, min=1,
        description="First frame to bake and render",
    )
    sim_frame_end: bpy.props.IntProperty(
        name="Frame End",
        default=250, min=1,
        description="Last frame to bake and render",
    )

    # ── Settings save/load ────────────────────────────────────────────────────

    settings_file_path:   bpy.props.StringProperty(default="")
    settings_search_path: bpy.props.StringProperty(default="")
    settings_snapshot:    bpy.props.StringProperty(default="")
    settings_file_enum:   bpy.props.EnumProperty(
        name="Preset",
        items=_settings_files_enum_items,
        update=_on_settings_enum_update,
    )

    # ── Resolution ───────────────────────────────────────────────────────────

    show_resolution: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Resolution section",
    )
    resolution_begin:     bpy.props.IntProperty(default=64, min=8)
    resolution_end:       bpy.props.IntProperty(default=64, min=8)
    resolution_step:      bpy.props.IntProperty(default=0)
    resolution_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("resolution"))
    resolution_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("resolution"))
    resolution_list:      bpy.props.CollectionProperty(type=ValueItem)
    resolution_index:     bpy.props.IntProperty()

    # ── Gas Parameters ───────────────────────────────────────────────────────

    show_gas: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Gas Parameters section",
    )



    # Alpha — d.alpha — buoyancy based on smoke density
    alpha_begin:     bpy.props.FloatProperty(default=1.0)
    alpha_end:       bpy.props.FloatProperty(default=1.0)
    alpha_step:      bpy.props.FloatProperty(default=0)
    alpha_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("alpha"))
    alpha_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("alpha"))
    alpha_list:      bpy.props.CollectionProperty(type=ValueItem)
    alpha_index:     bpy.props.IntProperty()

    # Beta — d.beta — buoyancy based on smoke heat/temperature
    beta_begin:     bpy.props.FloatProperty(default=1.0)
    beta_end:       bpy.props.FloatProperty(default=1.0)
    beta_step:      bpy.props.FloatProperty(default=0)
    beta_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("beta"))
    beta_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("beta"))
    beta_list:      bpy.props.CollectionProperty(type=ValueItem)
    beta_index:     bpy.props.IntProperty()

    # Vorticity — d.vorticity — adds turbulent detail to smoke
    vorticity_begin:     bpy.props.FloatProperty(default=0.0)
    vorticity_end:       bpy.props.FloatProperty(default=0.0)
    vorticity_step:      bpy.props.FloatProperty(default=0)
    vorticity_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("vorticity"))
    vorticity_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("vorticity"))
    vorticity_list:      bpy.props.CollectionProperty(type=ValueItem)
    vorticity_index:     bpy.props.IntProperty()

    # ── Dissolve ─────────────────────────────────────────────────────────────

    show_dissolve: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Dissolve section",
    )
    use_dissolve: bpy.props.BoolProperty(
        default=False,
        description="Enable smoke dissolve (smoke fades out over time)",
    )
    iterate_dissolve_both: bpy.props.BoolProperty(
        name="Iterate Both On and Off",
        description=(
            "In addition to the Dissolve-enabled jobs, also generate one job "
            "with Dissolve disabled so you can compare with and without dissolve "
            "in the same batch"
        ),
        default=False,
    )
    slow_dissolve: bpy.props.BoolProperty(
        default=False,
        description="Use logarithmic (slow) dissolve instead of linear",
    )
    # v0.7.0 TODO-45: Iterate Slow Dissolve.  When checked, every job
    # produced by the dissolve sweep also gets a companion job with the
    # opposite slow_dissolve value (slow ↔ fast).  Mirrors the existing
    # iterate_dissolve_both pattern but at one more level of nesting
    # (the slow/fast axis within use_dissolve=True jobs).  Only
    # meaningful when use_dissolve is True (greyed out otherwise).
    iterate_slow_dissolve: bpy.props.BoolProperty(
        name="Iterate Slow Dissolve",
        description=(
            "For each dissolve job, also generate a companion job with "
            "the opposite Slow Dissolve setting (slow ↔ fast), so you can "
            "compare both dissolve modes in the same batch.  Only "
            "applies when Use Dissolve is enabled."
        ),
        default=False,
    )
    dissolve_speed_begin:     bpy.props.IntProperty(default=5)
    dissolve_speed_end:       bpy.props.IntProperty(default=5)
    dissolve_speed_step:      bpy.props.IntProperty(default=0)
    dissolve_speed_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("dissolve_speed"))
    dissolve_speed_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("dissolve_speed"))
    dissolve_speed_list:      bpy.props.CollectionProperty(type=ValueItem)
    dissolve_speed_index:     bpy.props.IntProperty()

    # ── Noise ────────────────────────────────────────────────────────────────

    show_noise: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Noise section",
    )
    use_noise: bpy.props.BoolProperty(
        default=False,
        description="Enable high-resolution noise for added smoke detail",
    )
    iterate_noise_both: bpy.props.BoolProperty(
        name="Iterate Both On and Off",
        description=(
            "In addition to the Noise-enabled jobs, also generate one job "
            "with Noise disabled so you can compare with and without noise "
            "in the same batch"
        ),
        default=False,
    )

    # Noise scale — d.noise_scale — upres factor
    noise_upres_begin:     bpy.props.IntProperty(default=2)
    noise_upres_end:       bpy.props.IntProperty(default=2)
    noise_upres_step:      bpy.props.IntProperty(default=0)
    noise_upres_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_upres"))
    noise_upres_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_upres"))
    noise_upres_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_upres_index:     bpy.props.IntProperty()

    # Noise strength — d.noise_strength
    noise_strength_begin:     bpy.props.FloatProperty(default=2.0)
    noise_strength_end:       bpy.props.FloatProperty(default=2.0)
    noise_strength_step:      bpy.props.FloatProperty(default=0)
    noise_strength_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_strength"))
    noise_strength_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_strength"))
    noise_strength_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_strength_index:     bpy.props.IntProperty()

    # Noise position scale — d.noise_pos_scale
    noise_spatial_scale_begin:     bpy.props.FloatProperty(default=2.0)
    noise_spatial_scale_end:       bpy.props.FloatProperty(default=2.0)
    noise_spatial_scale_step:      bpy.props.FloatProperty(default=0)
    noise_spatial_scale_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("noise_spatial_scale"))
    noise_spatial_scale_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("noise_spatial_scale"))
    noise_spatial_scale_list:      bpy.props.CollectionProperty(type=ValueItem)
    noise_spatial_scale_index:     bpy.props.IntProperty()

    # ── Time + Adaptive Timesteps  (v0.7.0 TODO-41) ──────────────────────────
    # Time Scale — d.time_scale (always-on global sim speed multiplier).
    # Adaptive Timesteps + CFL + Timesteps Max/Min — Blender's adaptive
    # timestep system; CFL/max/min only matter when adaptive is on.
    show_time: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Time / Adaptive Timesteps section",
    )
    use_adaptive_timesteps: bpy.props.BoolProperty(
        name="Adaptive Time Step",
        description=(
            "Enable Blender's adaptive timestepping (uses CFL Number, "
            "Timesteps Max, Timesteps Min).  When off, simulation runs "
            "at a fixed substep count"
        ),
        default=True,
    )

    # time_scale — d.time_scale
    time_scale_begin:     bpy.props.FloatProperty(default=1.0)
    time_scale_end:       bpy.props.FloatProperty(default=1.0)
    time_scale_step:      bpy.props.FloatProperty(default=0)
    time_scale_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("time_scale"))
    time_scale_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("time_scale"))
    time_scale_list:      bpy.props.CollectionProperty(type=ValueItem)
    time_scale_index:     bpy.props.IntProperty()

    # cfl_number — d.cfl_condition
    cfl_number_begin:     bpy.props.FloatProperty(default=4.0)
    cfl_number_end:       bpy.props.FloatProperty(default=4.0)
    cfl_number_step:      bpy.props.FloatProperty(default=0)
    cfl_number_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("cfl_number"))
    cfl_number_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("cfl_number"))
    cfl_number_list:      bpy.props.CollectionProperty(type=ValueItem)
    cfl_number_index:     bpy.props.IntProperty()

    # timesteps_max — d.timesteps_max
    timesteps_max_begin:     bpy.props.IntProperty(default=4)
    timesteps_max_end:       bpy.props.IntProperty(default=4)
    timesteps_max_step:      bpy.props.IntProperty(default=0)
    timesteps_max_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("timesteps_max"))
    timesteps_max_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("timesteps_max"))
    timesteps_max_list:      bpy.props.CollectionProperty(type=ValueItem)
    timesteps_max_index:     bpy.props.IntProperty()

    # timesteps_min — d.timesteps_min
    timesteps_min_begin:     bpy.props.IntProperty(default=1)
    timesteps_min_end:       bpy.props.IntProperty(default=1)
    timesteps_min_step:      bpy.props.IntProperty(default=0)
    timesteps_min_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("timesteps_min"))
    timesteps_min_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("timesteps_min"))
    timesteps_min_list:      bpy.props.CollectionProperty(type=ValueItem)
    timesteps_min_index:     bpy.props.IntProperty()

    # ── Fire Parameters  (v0.7.0 TODO-42) ────────────────────────────────────
    # Fire is enabled per-flow-object in the .blend; the addon's use_fire
    # checkbox controls whether the worker APPLIES the addon's fire-tuning
    # values to the domain (when off, the .blend's existing fire settings
    # are left untouched — same model as use_noise).
    show_fire: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Fire Parameters section",
    )
    use_fire: bpy.props.BoolProperty(
        name="Use Fire",
        description=(
            "When enabled, the addon writes its Fire Parameters into the "
            "domain.  When disabled, the .blend's existing fire settings "
            "are left as-is"
        ),
        default=False,
    )

    # burning_rate — d.burning_rate (UI label "Reaction Speed")
    burning_rate_begin:     bpy.props.FloatProperty(default=0.75)
    burning_rate_end:       bpy.props.FloatProperty(default=0.75)
    burning_rate_step:      bpy.props.FloatProperty(default=0)
    burning_rate_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("burning_rate"))
    burning_rate_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("burning_rate"))
    burning_rate_list:      bpy.props.CollectionProperty(type=ValueItem)
    burning_rate_index:     bpy.props.IntProperty()

    # flame_smoke — d.flame_smoke (UI label "Flames Smoke")
    flame_smoke_begin:     bpy.props.FloatProperty(default=1.0)
    flame_smoke_end:       bpy.props.FloatProperty(default=1.0)
    flame_smoke_step:      bpy.props.FloatProperty(default=0)
    flame_smoke_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_smoke"))
    flame_smoke_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_smoke"))
    flame_smoke_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_smoke_index:     bpy.props.IntProperty()

    # flame_vorticity — d.flame_vorticity (separate from gas vorticity!)
    flame_vorticity_begin:     bpy.props.FloatProperty(default=0.5)
    flame_vorticity_end:       bpy.props.FloatProperty(default=0.5)
    flame_vorticity_step:      bpy.props.FloatProperty(default=0)
    flame_vorticity_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_vorticity"))
    flame_vorticity_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_vorticity"))
    flame_vorticity_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_vorticity_index:     bpy.props.IntProperty()

    # flame_max_temp — d.flame_max_temp (UI label "Temp Max")
    flame_max_temp_begin:     bpy.props.FloatProperty(default=1.7)
    flame_max_temp_end:       bpy.props.FloatProperty(default=1.7)
    flame_max_temp_step:      bpy.props.FloatProperty(default=0)
    flame_max_temp_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_max_temp"))
    flame_max_temp_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_max_temp"))
    flame_max_temp_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_max_temp_index:     bpy.props.IntProperty()

    # flame_ignition — d.flame_ignition (UI label "Temp Min" / ignition temp)
    flame_ignition_begin:     bpy.props.FloatProperty(default=1.5)
    flame_ignition_end:       bpy.props.FloatProperty(default=1.5)
    flame_ignition_step:      bpy.props.FloatProperty(default=0)
    flame_ignition_use_range: bpy.props.BoolProperty(
        default=False, update=make_toggle_range("flame_ignition"))
    flame_ignition_use_list:  bpy.props.BoolProperty(
        default=False, update=make_toggle_list("flame_ignition"))
    flame_ignition_list:      bpy.props.CollectionProperty(type=ValueItem)
    flame_ignition_index:     bpy.props.IntProperty()

    # ── Text object names for in-render parameter labels ─────────────────────

    show_text_objects: bpy.props.BoolProperty(
        default=False,
        description="Expand or collapse the Text Objects section",
    )
    text_resolution: bpy.props.StringProperty(
        default="Resolution_Text",
        description="Name of the FONT object in the scene that displays resolution",
    )
    text_noise: bpy.props.StringProperty(
        default="Noise_Text",
        description="Name of the FONT object that displays noise parameters",
    )
    text_dissolve: bpy.props.StringProperty(
        default="Dissolve_Text",
        description="Name of the FONT object that displays dissolve parameters",
    )
    text_time: bpy.props.StringProperty(
        default="Time_Text",
        description="Name of the FONT object that displays bake time",
    )

    # ── Render settings ──────────────────────────────────────────────────────

    render_mode: bpy.props.EnumProperty(
        name="Render Mode",
        description=(
            "Cycles GPU: reliable in --background mode, works without a display.\n"
            "EEVEE: faster but requires a visible Blender window (windowed mode)."
        ),
        items=[
            ('CYCLES', "Cycles GPU",
             "Reliable in background mode; uses OptiX/CUDA if available"),
            ('EEVEE',  "EEVEE",
             "Faster renders but requires windowed mode (no --background)"),
        ],
        default='CYCLES',
    )

    render_samples: bpy.props.IntProperty(
        name="Render Samples",
        description=(
            "Number of render samples for both the animation frame sequence "
            "and the final still frame. Cycles only — EEVEE ignores this."
        ),
        default=16,
        min=1,
        max=4096,
    )

    show_setup: bpy.props.BoolProperty(
        default=True,
        description="Expand or collapse the Setup section",
    )

    # ── Density ───────────────────────────────────────────────────────────────

    maintain_density: bpy.props.BoolProperty(
        name="Maintain Consistent Density",
        description=(
            "Scale emitter fluid density proportionally to keep visual density "
            "consistent as resolution changes. "
            "Formula: density = base_density × (job_resolution / default_resolution)"
        ),
        default=True,
    )

    use_placeholders: bpy.props.BoolProperty(
        name="Use Placeholders",
        description=(
            "If a rendered frame PNG already exists it will not be re-rendered, "
            "saving time when resuming an interrupted batch. "
            "Enabling this also forces Use Existing Cache on"
        ),
        default=False,
        update=lambda self, _ctx: setattr(self, "use_existing_cache", True)
                                  if self.use_placeholders else None,
    )

    use_existing_cache: bpy.props.BoolProperty(
        name="Use Existing Cache",
        description=(
            "Skip baking frames whose cache files are already present on disk. "
            "Automatically enabled when Use Placeholders is on. "
            "Auto-retry always uses existing cache"
        ),
        default=False,
    )

    auto_retry_failed: bpy.props.BoolProperty(
        name="Automatically Retry Failed Jobs",
        description=(
            "After all jobs finish, automatically re-run any that reported errors, "
            "with Use Existing Cache and Use Placeholders both forced on. "
            "Repeats up to 3 times per batch, re-running only the still-failing jobs"
        ),
        default=False,
    )
    render_simulation_result: bpy.props.BoolProperty(
        name="Render Simulation Result",
        description=(
            "When enabled, each job renders an MP4 animation and a final still "
            "PNG after baking. Disable for a bake-only batch — validate the "
            "simulation cache first, or render later by hand with other settings"
        ),
        default=True,
        update=_on_render_sim_result_update,
    )
    render_animation: bpy.props.BoolProperty(
        name="Render Animation",
        description=(
            "When enabled, render the full PNG sequence and mux it to MP4. "
            "Disable to render only the final still PNG (skips the per-frame "
            "render pass — useful when you only need the result image). "
            "Has no effect when Render Simulation Result is off"
        ),
        default=True,
    )

    # ── Output (collapsible: Iteration Mode + render settings + Run Batch) ────
    # v0.7.0 TODO-44: groups Iteration Mode through Run Batch into a single
    # collapsible section so the panel doesn't sprawl as v0.7.0 / v0.8.0
    # add more rows.
    show_output: bpy.props.BoolProperty(
        name="Output",
        default=True,
        description="Expand or collapse the Output section "
                    "(Iteration Mode, render settings, Export/Run Batch)",
    )

    # ── Progress (collapsible: progress bars + Job Log + summary) ─────────────
    # v0.7.0 TODO-44: groups all in-flight / post-batch display into one
    # collapsible.  Force-opens whenever a batch is running or a post-batch
    # summary is visible — the draw code overrides the user's collapse state
    # in those cases so they can't accidentally hide active progress.
    show_progress: bpy.props.BoolProperty(
        name="Progress",
        default=True,
        description="Expand or collapse the Progress section "
                    "(force-opened while a batch is running)",
    )

    # ── Job Log ───────────────────────────────────────────────────────────────

    show_job_log: bpy.props.BoolProperty(
        name="Job Log",
        default=False,
        description="Expand or collapse the Job Log section",
    )
    job_log_items:       bpy.props.CollectionProperty(type=SmokeJobItem)
    job_log_index:       bpy.props.IntProperty(default=0)
    job_log_auto_scroll: bpy.props.BoolProperty(default=True)

    # ── Utilities ─────────────────────────────────────────────────────────────

    show_utilities: bpy.props.BoolProperty(
        default=False,
        description="Expand or collapse the Utilities section",
    )
    collect_crash_logs: bpy.props.BoolProperty(
        name="Collect Crash Logs",
        description=(
            "Append each Blender crash log to crash_log.txt in the output folder. "
            "When unchecked, crash detection still stops the job but no log is written"
        ),
        default=False,
    )
    collect_estimation_data: bpy.props.BoolProperty(
        name="Collect Estimation Data",
        description=(
            "Write estim_log.jsonl (timing estimates vs actuals) and perf_log.json "
            "(per-job bake/render rates). Disable when not actively calibrating estimates"
        ),
        default=False,
    )
    collect_debug_log: bpy.props.BoolProperty(
        name="Collect Debug Log",
        description=(
            "Write verbose diagnostic info to debug_log.txt in the output folder. "
            "Enable when investigating problems on a new machine. "
            "Nothing is written unless this checkbox is checked"
        ),
        default=False,
    )

    # ── Batch run status ─────────────────────────────────────────────────────

    batch_progress:       bpy.props.StringProperty(default="")
    batch_total:          bpy.props.IntProperty(default=0)
    batch_jobs_dir:       bpy.props.StringProperty(default="")
    batch_overall_factor: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    batch_subtask_text:   bpy.props.StringProperty(default="")
    batch_subtask_factor: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    batch_job_text:       bpy.props.StringProperty(default="")
    batch_job_factor:     bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0)
    batch_summary_line1:  bpy.props.StringProperty(default="")
    batch_summary_line2:  bpy.props.StringProperty(default="")
    batch_summary_line3:  bpy.props.StringProperty(default="")
    batch_summary_line4:  bpy.props.StringProperty(default="")
    # Per-stage absolute Unix timestamps live in _batch_times (module-level
    # dict), not RNA — bpy.props.FloatProperty is single-precision and loses
    # ~64 sec of precision on a 10-digit Unix epoch, producing negative deltas
    # in estim_log.  See _bt / _bt_set helpers.
    batch_time_remaining: bpy.props.StringProperty(default="")
    batch_job_log_key:    bpy.props.StringProperty(default="")
    batch_frame_end:      bpy.props.IntProperty(default=0)
    batch_jobs_elapsed:      bpy.props.FloatProperty(default=0.0)
    batch_resolution:        bpy.props.IntProperty(default=0)
    batch_render_width:      bpy.props.IntProperty(default=0)
    batch_render_height:     bpy.props.IntProperty(default=0)
    batch_render_mode:       bpy.props.StringProperty(default="CYCLES")
    batch_bake_secs_actual:  bpy.props.FloatProperty(default=-1.0)
    batch_bake_secs_actual:  bpy.props.FloatProperty(default=-1.0)
    batch_render_secs_actual: bpy.props.FloatProperty(default=-1.0)
    batch_bake_frame_baseline:   bpy.props.IntProperty(default=-1)
    batch_render_frame_baseline: bpy.props.IntProperty(default=-1)
    show_results:         bpy.props.BoolProperty(
        name="Display Results When Finished",
        description="After all jobs complete, create a grid of result planes in a SmokeOutput collection",
        default=False,
    )

    # ── Status / UI state ────────────────────────────────────────────────────

    export_mode: bpy.props.EnumProperty(
        name="Export Mode",
        description="Whether to replace all existing jobs or add new ones after them",
        items=[
            ('REPLACE', "Replace", "Clear all existing jobs and start fresh"),
            ('APPEND',  "Append",  "Add new jobs after the existing ones, keeping previous results"),
        ],
        default='REPLACE',
    )

    last_export_info: bpy.props.StringProperty(
        default="",
        description="Status message shown after the last Export Batch operation",
    )
