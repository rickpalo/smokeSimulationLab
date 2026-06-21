"""
jobgen.py — pure job-generation core for BatchSimLab (TODO-58 module #1).

Extracted verbatim from ``__init__.py`` (the 6.1k-line monolith) as the first,
lowest-risk module split: a self-contained, ``bpy``-free cluster that turns a
``SmokeSettings`` object into the list of per-job parameter dicts and their
cache-name stems.  No Blender API is touched here, so the whole surface is
unit-testable without a running Blender.

Contents:
  * ``ITERABLE_PARAMS`` — the sweepable domain-parameter names.
  * Velocity-text helpers (``_VELOCITY_DEFAULT`` / ``_parse_velocity_vector`` /
    ``_format_velocity_vector``) — shared by emitter job-gen here and by the
    emitters/UI code that re-imports them.  The UI-only ``_VELOCITY_FORMAT_HINT``
    stays in ``__init__``.
  * ``expand_param`` / ``_first_value`` / ``_default_job`` and the two domain
    generators, the emitter job-gen helpers, ``generate_jobs`` dispatch,
    ``_dedupe_jobs``, and the ``make_name`` cache-name builder.

``__init__.py`` re-imports every public name from here so existing
``from BatchSimLab import …`` entry points (and the ~209 job-gen tests) keep
resolving unchanged.
"""
import copy
import itertools
import json


ITERABLE_PARAMS = [
    "resolution",
    "vorticity",
    "alpha",
    "beta",
    "dissolve_speed",
    "noise_upres",
    "noise_strength",
    "noise_spatial_scale",
    # v0.7.0 TODO-41: gas-side simulation timing parameters
    "time_scale",
    "cfl_number",
    "timesteps_max",
    "timesteps_min",
    # v0.7.0 TODO-42: Fire Parameters (only applied when use_fire is True)
    "burning_rate",
    "flame_smoke",
    "flame_vorticity",
    "flame_max_temp",
    "flame_ignition",
]


# Velocity-text parsing shared by emitter job-gen (here) and the UI/operator
# code in __init__ (it re-imports these).  Kept pure so parsing is unit-testable
# without a running Blender.
_VELOCITY_DEFAULT = (0.0, 0.0, 0.0)


def _parse_velocity_vector(text):
    """Parse a user-entered velocity string into an (x, y, z) float tuple.

    Accepts comma- or whitespace-separated values with optional surrounding
    spaces or brackets, e.g. "0,0,1", " 1, 0, -2 ", "[0, 0, 5]".  Returns None
    when the text does not contain exactly three numbers — callers treat None
    as 'invalid: show the format hint / skip this entry'.
    """
    if text is None:
        return None
    cleaned = text.strip().strip("[](){}").strip()
    if not cleaned:
        return None
    parts = cleaned.split(",") if "," in cleaned else cleaned.split()
    if len(parts) != 3:
        return None
    try:
        return tuple(float(p) for p in parts)
    except (ValueError, TypeError):
        return None


def _format_velocity_vector(vec):
    """Format an (x, y, z) iterable back to a compact "x, y, z" string.

    Uses the same trailing-zero-trimming `_fmt_num` as filename/value display
    so "0, 0, 1" round-trips cleanly.
    """
    return ", ".join(_fmt_num(c) for c in vec)


def expand_param(s, name):
    """
    Return a list of values for iterable parameter *name* from SmokeSettings *s*.

    In all three modes the Begin field is the authoritative single value:

      1. Explicit list  — user-entered values in the UIList; falls back to
                          [begin] when the list is empty.
      2. Range          — begin/end/step sweep; step=0 returns [begin].
      3. Single value   — [begin] (Begin field shown as "Value" in the UI).

    Float ranges use a small epsilon tolerance on the end boundary to
    avoid off-by-one errors caused by IEEE 754 floating point accumulation
    (e.g. 0.2 * 5 = 1.0000000000000002 in Python, which would incorrectly
    fail the <= 1.0 check without the epsilon).

    Parameters
    ----------
    s    : SmokeSettings — the scene's smoke_settings property group
    name : str           — base parameter name, e.g. "vorticity"

    Returns
    -------
    list of float/int values to iterate over
    """
    # v0.7.0 defensive: real SmokeSettings always has every sweep param's
    # _begin attribute, but test SimpleNamespace fixtures may not enumerate
    # every property (especially after adding TODO-41/42 params).  Return a
    # single-element sentinel so callers' `[0]` index never crashes and so
    # _default_job stays robust when called from older fixtures.
    if not hasattr(s, name + "_begin"):
        return [0]
    begin = getattr(s, name + "_begin")

    # Mode 1: explicit list
    if getattr(s, name + "_use_list"):
        lst  = getattr(s, name + "_list")
        vals = [i.value for i in lst]
        return vals if vals else [begin]

    # Mode 2: range sweep
    if getattr(s, name + "_use_range"):
        end   = getattr(s, name + "_end")
        step  = getattr(s, name + "_step")
        if step == 0 or end == begin:
            return [begin]
        # v0.5.2: derive step sign from begin/end so a descending sweep
        # (begin > end) works without requiring the user to type a negative
        # step.  Previously the while-loop's `v <= end + epsilon` condition
        # was immediately false for descending ranges, returning [] and
        # crashing _default_job's `[0]` index — which silently aborted the
        # rest of the panel's draw callback.
        step_abs = abs(step)
        if end >= begin:
            stepped, cmp = step_abs, lambda v: v <= end + step_abs * 1e-6
        else:
            stepped, cmp = -step_abs, lambda v: v >= end - step_abs * 1e-6
        vals, v = [], begin
        while cmp(v):
            vals.append(round(v, 6))  # round to avoid 0.200000000001 in names
            v += stepped
        # Defensive: even after the direction fix, an exotic input (e.g.
        # NaN end) could yield an empty list.  Always return at least
        # [begin] so callers' `[0]` indexing never crashes the UI.
        return vals if vals else [begin]

    # Mode 3: single value — Begin field doubles as the fixed value
    return [begin]


def _first_value(s, name):
    """Return the first (default) value of iterable parameter *name*.

    Shorthand for the ``expand_param(s, name)[0]`` idiom used throughout job
    generation to hold a parameter at its default while another sweeps.
    ``expand_param`` always returns a non-empty list, so ``[0]`` is safe.
    (TODO-60 dedup.)
    """
    return expand_param(s, name)[0]


def _default_job(s):
    """
    Return a job-parameter dict using the effective default value for every
    parameter.  Used as the baseline in Limited Combinations mode.

    Uses expand_param()[0] rather than the raw base property so that a
    single-point range (begin=128, step=0) or a single-item list is
    honoured — the user's chosen value becomes the baseline for all other
    parameter sweeps rather than falling back to the raw default.
    """
    return {
        "resolution":          _first_value(s, "resolution"),
        "vorticity":           _first_value(s, "vorticity"),
        "alpha":               _first_value(s, "alpha"),
        "beta":                _first_value(s, "beta"),
        "dissolve_speed":      _first_value(s, "dissolve_speed"),
        "noise_upres":         _first_value(s, "noise_upres"),
        "noise_strength":      _first_value(s, "noise_strength"),
        "noise_spatial_scale": _first_value(s, "noise_spatial_scale"),
        "use_dissolve":        s.use_dissolve,
        "slow_dissolve":       s.slow_dissolve,
        "use_noise":           s.use_noise,
        # v0.7.0 TODO-41: gas timing parameters
        "time_scale":          _first_value(s, "time_scale"),
        "use_adaptive_timesteps": getattr(s, "use_adaptive_timesteps", True),
        "cfl_number":          _first_value(s, "cfl_number"),
        "timesteps_max":       _first_value(s, "timesteps_max"),
        "timesteps_min":       _first_value(s, "timesteps_min"),
        # v0.7.0 TODO-42: fire parameters
        "use_fire":            getattr(s, "use_fire", False),
        "burning_rate":        _first_value(s, "burning_rate"),
        "flame_smoke":         _first_value(s, "flame_smoke"),
        "flame_vorticity":     _first_value(s, "flame_vorticity"),
        "flame_max_temp":      _first_value(s, "flame_max_temp"),
        "flame_ignition":      _first_value(s, "flame_ignition"),
    }


# ---------------------------------------------------------------------------
# Job generation — two modes
# ---------------------------------------------------------------------------

def generate_jobs_limited(s):
    """
    Limited Combinations mode.

    Yields one group of jobs per parameter that has a range or list defined.
    Within each group every other parameter is held at its default value.

    Example with vorticity range [0.5, 1.0, 1.5] and noise_strength range
    [0.5, 1.0]:
        Job 0: vorticity=0.5,  noise_strength=default  (vorticity sweep)
        Job 1: vorticity=1.0,  noise_strength=default
        Job 2: vorticity=1.5,  noise_strength=default
        Job 3: vorticity=default, noise_strength=0.5   (noise_strength sweep)
        Job 4: vorticity=default, noise_strength=1.0

    Parameters that are disabled (use_dissolve=False, use_noise=False) are
    included in the default job dict but their ranges are never swept.

    Parameters
    ----------
    s : SmokeSettings

    Yields
    ------
    dict — job parameter dict suitable for JSON serialisation
    """
    # Determine which parameters are enabled for sweeping.
    # Gas params, resolution, time_scale are always available.
    # Dissolve / noise / adaptive-timesteps / fire params only when their
    # section is enabled.
    sweepable = ["resolution", "vorticity", "alpha", "beta"]
    # v0.7.0 TODO-41: time_scale is always-on (no master enable).
    sweepable.append("time_scale")
    if s.use_dissolve:
        sweepable.append("dissolve_speed")
    if s.use_noise:
        sweepable += ["noise_upres", "noise_strength", "noise_spatial_scale"]
    # v0.7.0 TODO-41: CFL / timesteps only when adaptive timesteps are on.
    if getattr(s, "use_adaptive_timesteps", True):
        sweepable += ["cfl_number", "timesteps_max", "timesteps_min"]
    # v0.7.0 TODO-42: fire sub-params only when use_fire is on.
    if getattr(s, "use_fire", False):
        sweepable += ["burning_rate", "flame_smoke", "flame_vorticity",
                      "flame_max_temp", "flame_ignition"]

    yielded = False
    for param_name in sweepable:
        use_list  = getattr(s, param_name + "_use_list",  False)
        use_range = getattr(s, param_name + "_use_range", False)
        vals      = expand_param(s, param_name)

        # A list is always intentional even if it has only 1 item — the user
        # explicitly chose that value.  A range with step=0 collapses to a
        # single point and adds no variation; treat it as "no sweep".
        if use_list:
            is_explicit = True
        elif use_range:
            is_explicit = len(vals) > 1
        else:
            is_explicit = False

        if not is_explicit:
            continue

        # Sweep this parameter while all others stay at default
        base = _default_job(s)
        for v in vals:
            job = dict(base)
            job[param_name] = v
            yielded = True
            yield job
            # v0.7.0 TODO-45: Iterate Slow Dissolve — for each sweep job
            # that has use_dissolve on, also yield a companion with the
            # opposite slow_dissolve.  Skip when use_dissolve is False on
            # the job itself (e.g. an iterate_dissolve_both off-pass)
            # because slow doesn't apply there.
            if (s.use_dissolve and getattr(s, "iterate_slow_dissolve", False)
                    and job.get("use_dissolve")):
                flipped = dict(job)
                flipped["slow_dissolve"] = not job["slow_dissolve"]
                yield flipped

    # Iterate-both: append one comparison job with the feature toggled off.
    # Only fires when the feature is currently enabled (the checkbox is hidden
    # when the feature is off, so this path is only reached intentionally).
    if s.use_dissolve and s.iterate_dissolve_both:
        base = _default_job(s)
        job  = dict(base)
        job["use_dissolve"] = False
        yielded = True
        yield job
        # NOTE: no slow-flip companion here — this job has
        # use_dissolve=False so slow_dissolve doesn't apply.

    if s.use_noise and s.iterate_noise_both:
        base = _default_job(s)
        job  = dict(base)
        job["use_noise"] = False
        yielded = True
        yield job
        # v0.7.0 TODO-45: this noise-off job retains current use_dissolve
        # — if dissolve is on, also yield its slow-flipped companion.
        if (s.use_dissolve and getattr(s, "iterate_slow_dissolve", False)
                and job.get("use_dissolve")):
            flipped = dict(job)
            flipped["slow_dissolve"] = not job["slow_dissolve"]
            yield flipped

    # Fallback: if no axis sweep produced jobs and no iterate-both pass was
    # configured, emit a single baseline job. Otherwise a user with only
    # single-value parameters would see "0 jobs" and a disabled Export button,
    # which is surprising — testing one specific param combination is a valid
    # use case and should not require enabling All Combinations mode.
    if not yielded:
        base = _default_job(s)
        yield base
        # v0.7.0 TODO-45: fallback baseline also gets a slow companion
        # when iterate_slow_dissolve is on and use_dissolve is True.
        if (s.use_dissolve and getattr(s, "iterate_slow_dissolve", False)
                and base.get("use_dissolve")):
            flipped = dict(base)
            flipped["slow_dissolve"] = not base["slow_dissolve"]
            yield flipped


def generate_jobs_all(s):
    """
    All Combinations mode (original behaviour).

    Yields one job per element of the Cartesian product of all parameter
    ranges.  The total job count is the product of all range lengths, which
    can grow very large when multiple parameters have wide ranges.

    When iterate_dissolve_both / iterate_noise_both are enabled the product
    is extended to include jobs with that feature disabled, giving a direct
    on-vs-off comparison within a single batch.

    Parameters
    ----------
    s : SmokeSettings

    Yields
    ------
    dict — job parameter dict suitable for JSON serialisation
    """
    def param(name):
        return expand_param(s, name)

    res   = param("resolution")
    vort  = param("vorticity")
    alpha = param("alpha")
    beta  = param("beta")

    # Build the set of (use_dissolve, slow_dissolve, dissolve_vals) states.
    # When iterate_dissolve_both is on, generate two passes: feature on then off.
    # v0.7.0 TODO-45: when iterate_slow_dissolve is on AND use_dissolve is on,
    # also generate a parallel state with the slow_dissolve flag flipped, so
    # every dissolve-enabled combo gets baked both slow and fast.
    if s.use_dissolve:
        dissolve_states = [(True, s.slow_dissolve, param("dissolve_speed"))]
        if getattr(s, "iterate_slow_dissolve", False):
            dissolve_states.append((True, not s.slow_dissolve, param("dissolve_speed")))
        if s.iterate_dissolve_both:
            # use_dissolve=False — slow doesn't apply, no slow-flip companion.
            dissolve_states.append((False, s.slow_dissolve, [_first_value(s, "dissolve_speed")]))
    else:
        dissolve_states = [(False, s.slow_dissolve, [_first_value(s, "dissolve_speed")])]

    # Build the set of (use_noise, nu, ns, nss) states.
    if s.use_noise:
        noise_states = [(True,
                         param("noise_upres"),
                         param("noise_strength"),
                         param("noise_spatial_scale"))]
        if s.iterate_noise_both:
            noise_states.append((False,
                                  [_first_value(s, "noise_upres")],
                                  [_first_value(s, "noise_strength")],
                                  [_first_value(s, "noise_spatial_scale")]))
    else:
        noise_states = [(False,
                         [_first_value(s, "noise_upres")],
                         [_first_value(s, "noise_strength")],
                         [_first_value(s, "noise_spatial_scale")])]

    # v0.7.0 TODO-41: gas-timing axes always present in the product.
    # Adaptive-only sub-params (cfl_number, timesteps_*) collapse to their
    # begin value when adaptive is off, since they have no effect then.
    use_adapt = getattr(s, "use_adaptive_timesteps", True)
    time_scale_vals    = param("time_scale")
    if use_adapt:
        cfl_vals           = param("cfl_number")
        timesteps_max_vals = param("timesteps_max")
        timesteps_min_vals = param("timesteps_min")
    else:
        cfl_vals           = [_first_value(s, "cfl_number")]
        timesteps_max_vals = [_first_value(s, "timesteps_max")]
        timesteps_min_vals = [_first_value(s, "timesteps_min")]

    # v0.7.0 TODO-42: fire sub-params collapse to single values when fire is off.
    use_fire = getattr(s, "use_fire", False)
    if use_fire:
        burning_rate_vals    = param("burning_rate")
        flame_smoke_vals     = param("flame_smoke")
        flame_vorticity_vals = param("flame_vorticity")
        flame_max_temp_vals  = param("flame_max_temp")
        flame_ignition_vals  = param("flame_ignition")
    else:
        burning_rate_vals    = [_first_value(s, "burning_rate")]
        flame_smoke_vals     = [_first_value(s, "flame_smoke")]
        flame_vorticity_vals = [_first_value(s, "flame_vorticity")]
        flame_max_temp_vals  = [_first_value(s, "flame_max_temp")]
        flame_ignition_vals  = [_first_value(s, "flame_ignition")]

    for (use_d, slow_d, dissolve) in dissolve_states:
        for (use_n, nu, ns, nss) in noise_states:
            for combo in itertools.product(
                    res, vort, alpha, beta,
                    dissolve, nu, ns, nss,
                    time_scale_vals, cfl_vals, timesteps_max_vals, timesteps_min_vals,
                    burning_rate_vals, flame_smoke_vals, flame_vorticity_vals,
                    flame_max_temp_vals, flame_ignition_vals):
                yield {
                    "resolution":          combo[0],
                    "vorticity":           combo[1],
                    "alpha":               combo[2],
                    "beta":                combo[3],
                    "dissolve_speed":      combo[4],
                    "noise_upres":         combo[5],
                    "noise_strength":      combo[6],
                    "noise_spatial_scale": combo[7],
                    "use_dissolve":        use_d,
                    "slow_dissolve":       slow_d,
                    "use_noise":           use_n,
                    # v0.7.0 TODO-41: gas timing params
                    "time_scale":          combo[8],
                    "use_adaptive_timesteps": use_adapt,
                    "cfl_number":          combo[9],
                    "timesteps_max":       combo[10],
                    "timesteps_min":       combo[11],
                    # v0.7.0 TODO-42: fire params
                    "use_fire":            use_fire,
                    "burning_rate":        combo[12],
                    "flame_smoke":         combo[13],
                    "flame_vorticity":     combo[14],
                    "flame_max_temp":      combo[15],
                    "flame_ignition":      combo[16],
                }


# ---------------------------------------------------------------------------
# v0.9.0 TODO-55: emitter sweep layering (increment 3)
#
# The domain generators above stay emitter-agnostic.  generate_jobs() layers
# per-emitter values onto each domain job: in LIMITED mode every domain job gets
# the baseline emitters and each explicitly-swept emitter axis adds a row (one
# axis varied, everything else baseline); in ALL mode each domain job is crossed
# with the full emitter cartesian product.  The job dict gains an "emitters" key
# {name: {temperature, density, surface_distance, volume_density,
# use_initial_velocity, velocity_factor, velocity_normal, velocity_coord}} which
# rides into job JSON via job_data["params"] and into make_name().
# ---------------------------------------------------------------------------

# Emitter scalar params always sweepable; the velocity scalars only when
# use_initial_velocity is on (mirrors the use_dissolve / use_noise gating).
_EMITTER_SCALARS = ("temperature", "density", "surface_distance", "volume_density")
_EMITTER_VELOCITY_SCALARS = ("velocity_factor", "velocity_normal")


def _emitter_velocity_vectors(em):
    """Parsed (x, y, z) tuples from an emitter's velocity_list.

    Skips malformed entries (the UI red-tints them; export validation reports
    them).  Falls back to [_VELOCITY_DEFAULT] when empty / all-invalid so the
    baseline always has one vector.
    """
    vecs = []
    for item in getattr(em, "velocity_list", []):
        v = _parse_velocity_vector(getattr(item, "text", ""))
        if v is not None:
            vecs.append(v)
    return vecs if vecs else [_VELOCITY_DEFAULT]


def _emitter_baseline(em):
    """One emitter's baseline param dict — every axis at its first value."""
    d = {p: expand_param(em, p)[0] for p in _EMITTER_SCALARS}
    d["use_initial_velocity"] = bool(getattr(em, "use_initial_velocity", False))
    d["velocity_factor"] = expand_param(em, "velocity_factor")[0]
    d["velocity_normal"] = expand_param(em, "velocity_normal")[0]
    d["velocity_coord"]  = list(_emitter_velocity_vectors(em)[0])
    return d


def _default_emitters(s):
    """Baseline {emitter_name: param_dict} for all of s.emitters."""
    return {em.name: _emitter_baseline(em) for em in getattr(s, "emitters", [])}


def _emitter_sweep_axes(s):
    """Return [(emitter_name, param_key, values), ...] for every emitter param
    the user explicitly swept (list, or range with >1 value; velocity vector
    list with >1 vector).  Mirrors the is_explicit rule used for domain axes."""
    axes = []
    for em in getattr(s, "emitters", []):
        scalars = list(_EMITTER_SCALARS)
        if getattr(em, "use_initial_velocity", False):
            scalars += list(_EMITTER_VELOCITY_SCALARS)
        for p in scalars:
            use_list  = getattr(em, p + "_use_list",  False)
            use_range = getattr(em, p + "_use_range", False)
            vals = expand_param(em, p)
            if use_list:
                explicit = True
            elif use_range:
                explicit = len(vals) > 1
            else:
                explicit = False
            if explicit:
                axes.append((em.name, p, vals))
        # Initial X/Y/Z vector list — a sweep when >1 distinct vector entered.
        if getattr(em, "use_initial_velocity", False):
            vecs = _emitter_velocity_vectors(em)
            if len(vecs) > 1:
                axes.append((em.name, "velocity_coord", [list(v) for v in vecs]))
    return axes


def _emitter_combinations(s):
    """ALL-combinations: cartesian product over every swept emitter axis.

    Returns a list of complete emitters dicts.  When no emitter axis is swept,
    returns a single baseline combo so the domain product is unchanged.
    """
    default = _default_emitters(s)
    axes = _emitter_sweep_axes(s)
    if not axes:
        return [copy.deepcopy(default)]
    combos = []
    for combo in itertools.product(*[vals for (_, _, vals) in axes]):
        em = copy.deepcopy(default)
        for (ename, pkey, _), v in zip(axes, combo):
            em[ename][pkey] = v
        combos.append(em)
    return combos


def generate_jobs(s):
    """
    Dispatch to the appropriate domain generator based on s.iteration_mode,
    then layer per-emitter values (TODO-55) onto every job.

    Returns a generator of job dicts (each with an "emitters" block).
    """
    default_emitters = _default_emitters(s)

    if s.iteration_mode == 'LIMITED':
        # Domain sweeps: each domain job keeps emitters at baseline.
        for job in generate_jobs_limited(s):
            job["emitters"] = copy.deepcopy(default_emitters)
            yield job
        # Emitter sweeps: one emitter axis varied at a time, domain + other
        # emitters held at baseline (mirrors the domain Limited pattern).
        if default_emitters:
            domain_base = _default_job(s)
            for (ename, pkey, vals) in _emitter_sweep_axes(s):
                for v in vals:
                    job = dict(domain_base)
                    job["emitters"] = copy.deepcopy(default_emitters)
                    job["emitters"][ename][pkey] = v
                    yield job
    else:  # ALL — cross every domain job with every emitter combination.
        emitter_combos = _emitter_combinations(s)
        for job in generate_jobs_all(s):
            for em in emitter_combos:
                j = dict(job)
                j["emitters"] = copy.deepcopy(em)
                yield j


def _dedupe_jobs(jobs):
    """
    Return a new list with duplicate jobs removed, preserving first-seen order.

    Every axis sweep in generate_jobs_limited starts from the baseline (all
    other params at default), so when a sweep value equals that axis's default
    the baseline combination is emitted again.  An 8-axis sweep produces up to
    8 baseline duplicates per batch — each one targets the same cache directory
    (make_name is param-only) and would re-bake redundantly, with each FULL
    BAKE branch calling bpy.ops.fluid.free_all() to wipe the previous bake.

    Two jobs are duplicates iff every param value is equal.
    """
    seen   = set()
    unique = []
    for j in jobs:
        # v0.9.0 TODO-55: jobs now carry a nested "emitters" dict, so the old
        # tuple(sorted(items())) key is unhashable.  A sort_keys JSON dump is a
        # stable, hashable signature that handles nested dicts/lists too.
        key = json.dumps(j, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        unique.append(j)
    return unique


def _fmt_num(x):
    """Compact float formatting for filename use (v0.7.1 TODO-48 A).

    Trims trailing zeros and unnecessary decimal points so:
        0.0   → "0"
        1.0   → "1"
        0.50  → "0.5"
        2.25  → "2.25"
        0.123456789 → "0.123" (rounded to 3 decimals)
    """
    return f"{round(float(x), 3):g}"


# v0.7.1 TODO-48 B: Single-character "OFF" indicator used by make_name().
# Lowercase 'x' chosen as the most-compact distinct marker — no value
# letter (D / N / F / etc.) could legitimately be followed by an 'x', so
# 'Dx' / 'Nx' / 'Fx' read unambiguously as "feature off" without needing
# the verbose "-OFF" suffix.
_OFF_SUFFIX = "x"


# v0.9.0 TODO-55: emitter param encoding for make_name().  Suffixes are
# default-suppressed against the documented FluidFlowSettings defaults — a value
# at its default contributes no token, so cache names stay short.  This is
# collision-SAFE because only the exact-default value is dropped: any differing
# value is encoded, so two jobs that differ in an emitter param always get
# distinct names (BUG-013 family).  Each token is namespaced by the emitter's
# sorted-order index (E0, E1, ...) so multiple emitters never clash.
_EMITTER_NAME_DEFAULTS = {
    "temperature": 1.0, "density": 1.0,
    "surface_distance": 1.5, "volume_density": 0.0,
    "velocity_factor": 0.0, "velocity_normal": 0.0,
}
_EMITTER_NAME_ABBR = {
    "temperature": "T", "density": "D",
    "surface_distance": "SE", "volume_density": "VE",
    "velocity_factor": "VS", "velocity_normal": "VN",
}


def _emitter_name_tokens(emitters):
    """Return make_name() tokens for the per-emitter params (default-suppressed,
    namespaced by sorted-order emitter index).  Pure / testable."""
    tokens = []
    for i, ename in enumerate(sorted(emitters or {})):
        em = emitters[ename]
        for key in _EMITTER_SCALARS:
            val = float(em.get(key, _EMITTER_NAME_DEFAULTS[key]))
            if val != _EMITTER_NAME_DEFAULTS[key]:
                tokens.append(f"E{i}{_EMITTER_NAME_ABBR[key]}{_fmt_num(val)}")
        # Velocity block — only when initial velocity is on.  The "Vy" marker
        # guarantees on/off differ even when every velocity value is at default.
        if em.get("use_initial_velocity"):
            tokens.append(f"E{i}Vy")
            for key in _EMITTER_VELOCITY_SCALARS:
                val = float(em.get(key, 0.0))
                if val != 0.0:
                    tokens.append(f"E{i}{_EMITTER_NAME_ABBR[key]}{_fmt_num(val)}")
            vc = list(em.get("velocity_coord") or [0.0, 0.0, 0.0])
            if [float(c) for c in vc] != [0.0, 0.0, 0.0]:
                comps = "c".join(_fmt_num(c) for c in vc)
                tokens.append(f"E{i}VC{comps}")
    return tokens


def make_name(p):
    """
    Build a human-readable filename stem from a job-parameter dict.

    Format (v0.7.1 — compact + default-suppressed):
        R<res>_V<vort>_A<alpha>_B<beta>_<dissolve>_<noise>[_<extras>]

    Where:
        <dissolve> = D<speed>-Slow|D<speed>-Fast  when use_dissolve else 'Dx'
        <noise>    = N<upres>_NS<str>_SC<scale>   when use_noise    else 'Nx'

    Optional extras (appended ONLY when the value differs from Blender's
    default — keeps v0.6.x cache names unchanged when nothing v0.7.x
    related has been touched):
        TS<n>         time_scale != 1.0
        ATx           use_adaptive_timesteps False (skip when True == default)
        CFL<n>        cfl_number != 4.0 AND adaptive on
        TMx<n>        timesteps_max != 4 AND adaptive on
        TMn<n>        timesteps_min != 1 AND adaptive on
        F-Y_BR<n>_FS<n>_FV<n>_TMax<n>_TIgn<n>   when use_fire (Fx omitted)

    Numbers are formatted with :g via _fmt_num() — trailing zeros stripped.

    The name is derived purely from simulation parameters so that identical
    parameter combinations always map to the same cache directory, render
    directory, and output files — regardless of job order or batch index.

    Parameters
    ----------
    p : dict — job parameter dict from generate_jobs()

    Returns
    -------
    str — filename stem without extension
    """
    # ── Existing core params (always present) ────────────────────────────
    # v0.7.0 BUG-013: explicit slow/fast indicator when use_dissolve.
    # v0.7.1 TODO-48 B: 'Dx' / 'Nx' replace '-OFF' suffixes.
    dissolve_part = (
        (f"D{int(p['dissolve_speed'])}-Slow" if p.get('slow_dissolve')
         else f"D{int(p['dissolve_speed'])}-Fast")
        if p['use_dissolve'] else f"D{_OFF_SUFFIX}"
    )
    noise_part = (
        f"N{int(p['noise_upres'])}_"
        f"NS{_fmt_num(p['noise_strength'])}_"
        f"SC{_fmt_num(p['noise_spatial_scale'])}"
        if p['use_noise'] else f"N{_OFF_SUFFIX}"
    )

    # ── v0.7.0 TODO-47: extras for new params (default-suppressed) ───────
    # Each suffix is appended ONLY when the value differs from Blender's
    # documented default, so jobs with nothing v0.7.x touched keep their
    # v0.6.x cache names unchanged.  Defaults: time_scale=1.0,
    # use_adaptive_timesteps=True, cfl=4.0, timesteps_max=4,
    # timesteps_min=1, use_fire=False (all 5 fire sub-params irrelevant
    # when off).
    extras = []

    # Time scale — suffix only when != 1.0
    _ts = float(p.get("time_scale", 1.0))
    if _ts != 1.0:
        extras.append(f"TS{_fmt_num(_ts)}")

    # Adaptive timesteps — when OFF, append a marker (default is ON).
    # When ON, CFL/timesteps sub-params each get their own suffix if
    # non-default.
    _adapt = bool(p.get("use_adaptive_timesteps", True))
    if not _adapt:
        extras.append(f"AT{_OFF_SUFFIX}")   # ATx
    else:
        _cfl = float(p.get("cfl_number", 4.0))
        if _cfl != 4.0:
            extras.append(f"CFL{_fmt_num(_cfl)}")
        _tmax = int(p.get("timesteps_max", 4))
        if _tmax != 4:
            extras.append(f"TMx{_tmax}")
        _tmin = int(p.get("timesteps_min", 1))
        if _tmin != 1:
            extras.append(f"TMn{_tmin}")

    # Fire — when ON, append F-Y plus the 5 sub-params.  When OFF
    # (default), suppress the suffix entirely so v0.6.x cache names
    # match exactly for any job that never touched fire.
    if p.get("use_fire", False):
        extras.append("F-Y")
        extras.append(f"BR{_fmt_num(p['burning_rate'])}")
        extras.append(f"FS{_fmt_num(p['flame_smoke'])}")
        extras.append(f"FV{_fmt_num(p['flame_vorticity'])}")
        extras.append(f"TMax{_fmt_num(p['flame_max_temp'])}")
        extras.append(f"TIgn{_fmt_num(p['flame_ignition'])}")

    # v0.9.0 TODO-55: per-emitter param tokens (default-suppressed, namespaced).
    extras.extend(_emitter_name_tokens(p.get("emitters")))

    extras_suffix = ("_" + "_".join(extras)) if extras else ""

    return (
        f"R{int(p['resolution'])}_"
        f"V{_fmt_num(p['vorticity'])}_"
        f"A{_fmt_num(p['alpha'])}_"
        f"B{_fmt_num(p['beta'])}_"
        f"{dissolve_part}_"
        f"{noise_part}"
        f"{extras_suffix}"
    )
