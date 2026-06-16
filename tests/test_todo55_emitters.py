"""TODO-55 (v0.9.0): emitter (flow object) discovery helpers.

Covers the pure discovery foundation:
  _is_flow_object / find_fluid_emitters  — scene modifier scan
  _world_aabb / _aabb_overlap            — world-space bounds math
  emitters_inside_domain / find_emitters — domain bounds filter (single-domain)
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import SmokeSimLab as ssl


# --- stubs ----------------------------------------------------------------

_IDENTITY = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _translate(tx, ty, tz):
    return [[1, 0, 0, tx], [0, 1, 0, ty], [0, 0, 1, tz], [0, 0, 0, 1]]


class _Mod:
    def __init__(self, mtype, fluid_type=None):
        self.type = mtype
        self.fluid_type = fluid_type


class _Obj:
    def __init__(self, name, mods=None, bound_box=None, matrix_world=None):
        self.name = name
        self.modifiers = mods if mods is not None else []
        if bound_box is not None:
            self.bound_box = bound_box
        if matrix_world is not None:
            self.matrix_world = matrix_world


def _bbox_corners(lo, hi):
    """8 corners of an axis-aligned box from lo=(x0,y0,z0) to hi=(x1,y1,z1)."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def _flow(name, lo=(-1, -1, -1), hi=(1, 1, 1), matrix_world=None):
    return _Obj(name, mods=[_Mod('FLUID', 'FLOW')],
                bound_box=_bbox_corners(lo, hi),
                matrix_world=matrix_world or _IDENTITY)


def _domain(name="Domain", lo=(-5, -5, -5), hi=(5, 5, 5), matrix_world=None):
    return _Obj(name, mods=[_Mod('FLUID', 'DOMAIN')],
                bound_box=_bbox_corners(lo, hi),
                matrix_world=matrix_world or _IDENTITY)


def _scene(objs):
    return types.SimpleNamespace(objects=objs)


# --- _is_flow_object ------------------------------------------------------

class TestIsFlowObject:
    def test_flow_modifier(self):
        assert ssl._is_flow_object(_Obj("E", [_Mod('FLUID', 'FLOW')])) is True

    def test_domain_modifier_is_not_flow(self):
        assert ssl._is_flow_object(_Obj("D", [_Mod('FLUID', 'DOMAIN')])) is False

    def test_no_fluid_modifier(self):
        assert ssl._is_flow_object(_Obj("M", [_Mod('SUBSURF')])) is False

    def test_no_modifiers(self):
        assert ssl._is_flow_object(_Obj("M", [])) is False

    def test_missing_modifiers_attr(self):
        assert ssl._is_flow_object(types.SimpleNamespace()) is False


# --- find_fluid_emitters --------------------------------------------------

class TestFindFluidEmitters:
    def test_returns_only_flow_objects(self):
        scene = _scene([
            _domain("Domain"),
            _flow("Emit_B"),
            _Obj("Mesh", [_Mod('SUBSURF')]),
            _flow("Emit_A"),
        ])
        names = [o.name for o in ssl.find_fluid_emitters(scene)]
        assert names == ["Emit_A", "Emit_B"]   # sorted by name, domain/mesh excluded

    def test_empty_scene(self):
        assert ssl.find_fluid_emitters(_scene([])) == []

    def test_scene_without_objects_attr(self):
        assert ssl.find_fluid_emitters(types.SimpleNamespace()) == []

    def test_deterministic_name_order(self):
        scene = _scene([_flow("z"), _flow("a"), _flow("m")])
        assert [o.name for o in ssl.find_fluid_emitters(scene)] == ["a", "m", "z"]


# --- _world_aabb ----------------------------------------------------------

class TestWorldAABB:
    def test_identity_matrix(self):
        obj = _flow("E", lo=(-1, -2, -3), hi=(4, 5, 6))
        assert ssl._world_aabb(obj) == ((-1, -2, -3), (4, 5, 6))

    def test_translation_matrix_shifts_bounds(self):
        obj = _flow("E", lo=(-1, -1, -1), hi=(1, 1, 1),
                    matrix_world=_translate(10, 0, 0))
        lo, hi = ssl._world_aabb(obj)
        assert lo == (9, -1, -1)
        assert hi == (11, 1, 1)

    def test_missing_bound_box(self):
        obj = _Obj("E", [_Mod('FLUID', 'FLOW')], matrix_world=_IDENTITY)
        assert ssl._world_aabb(obj) is None

    def test_none_object(self):
        assert ssl._world_aabb(None) is None


# --- _aabb_overlap --------------------------------------------------------

class TestAABBOverlap:
    def test_overlapping(self):
        a = ((0, 0, 0), (2, 2, 2))
        b = ((1, 1, 1), (3, 3, 3))
        assert ssl._aabb_overlap(a, b) is True

    def test_disjoint_on_x(self):
        a = ((0, 0, 0), (1, 1, 1))
        b = ((5, 0, 0), (6, 1, 1))
        assert ssl._aabb_overlap(a, b) is False

    def test_touching_faces_count_as_overlap(self):
        a = ((0, 0, 0), (1, 1, 1))
        b = ((1, 0, 0), (2, 1, 1))
        assert ssl._aabb_overlap(a, b) is True

    def test_none_box(self):
        assert ssl._aabb_overlap(None, ((0, 0, 0), (1, 1, 1))) is False
        assert ssl._aabb_overlap(((0, 0, 0), (1, 1, 1)), None) is False


# --- emitters_inside_domain -----------------------------------------------

class TestEmittersInsideDomain:
    def test_inside_kept_outside_dropped(self):
        dom = _domain(lo=(-5, -5, -5), hi=(5, 5, 5))
        inside = _flow("Inside", lo=(-1, -1, -1), hi=(1, 1, 1))
        outside = _flow("Outside", lo=(20, 20, 20), hi=(22, 22, 22))
        kept = ssl.emitters_inside_domain([inside, outside], dom)
        assert [o.name for o in kept] == ["Inside"]

    def test_matrix_world_moves_emitter_out(self):
        dom = _domain(lo=(-5, -5, -5), hi=(5, 5, 5))
        # Local bounds would be inside, but matrix_world translates it far away.
        moved = _flow("Moved", lo=(-1, -1, -1), hi=(1, 1, 1),
                      matrix_world=_translate(100, 0, 0))
        assert ssl.emitters_inside_domain([moved], dom) == []

    def test_domain_without_bounds_keeps_all(self):
        # Can't measure containment → over-include rather than drop everything.
        dom = _Obj("Domain", [_Mod('FLUID', 'DOMAIN')], matrix_world=_IDENTITY)
        e1, e2 = _flow("A"), _flow("B")
        assert ssl.emitters_inside_domain([e1, e2], dom) == [e1, e2]

    def test_none_domain_keeps_all(self):
        e1, e2 = _flow("A"), _flow("B")
        assert ssl.emitters_inside_domain([e1, e2], None) == [e1, e2]


# --- find_emitters (end to end) -------------------------------------------

class TestFindEmitters:
    def test_scan_filter_and_sort(self):
        dom = _domain("Domain", lo=(-5, -5, -5), hi=(5, 5, 5))
        near = _flow("Near", lo=(-1, -1, -1), hi=(1, 1, 1))
        far = _flow("Far", lo=(50, 50, 50), hi=(52, 52, 52))
        mesh = _Obj("Mesh", [_Mod('SUBSURF')])
        scene = _scene([dom, far, near, mesh])
        result = ssl.find_emitters(scene, dom)
        assert [o.name for o in result] == ["Near"]   # Far filtered, sorted

    def test_no_domain_returns_all_flow_objects(self):
        scene = _scene([_flow("B"), _flow("A")])
        assert [o.name for o in ssl.find_emitters(scene, None)] == ["A", "B"]


# --- Initial Velocity vector parsing (list-of-vectors sweep) --------------

class TestParseVelocityVector:
    def test_basic_comma(self):
        assert ssl._parse_velocity_vector("0, 0, 1") == (0.0, 0.0, 1.0)

    def test_no_spaces(self):
        assert ssl._parse_velocity_vector("1,2,3") == (1.0, 2.0, 3.0)

    def test_negative_and_decimals(self):
        assert ssl._parse_velocity_vector(" 1.5, 0, -2 ") == (1.5, 0.0, -2.0)

    def test_brackets_tolerated(self):
        assert ssl._parse_velocity_vector("[0, 0, 5]") == (0.0, 0.0, 5.0)

    def test_whitespace_separated_fallback(self):
        assert ssl._parse_velocity_vector("1 0 2") == (1.0, 0.0, 2.0)

    def test_wrong_count_returns_none(self):
        assert ssl._parse_velocity_vector("1, 2") is None
        assert ssl._parse_velocity_vector("1, 2, 3, 4") is None

    def test_non_numeric_returns_none(self):
        assert ssl._parse_velocity_vector("a, b, c") is None

    def test_empty_and_none(self):
        assert ssl._parse_velocity_vector("") is None
        assert ssl._parse_velocity_vector("   ") is None
        assert ssl._parse_velocity_vector(None) is None

    def test_default_constant(self):
        assert ssl._VELOCITY_DEFAULT == (0.0, 0.0, 0.0)


class TestFormatVelocityVector:
    def test_trims_trailing_zeros(self):
        assert ssl._format_velocity_vector((0.0, 0.0, 1.0)) == "0, 0, 1"

    def test_decimals_preserved(self):
        assert ssl._format_velocity_vector((1.5, 0.0, -2.25)) == "1.5, 0, -2.25"

    def test_round_trips_with_parse(self):
        vec = (1.5, 0.0, -2.0)
        assert ssl._parse_velocity_vector(ssl._format_velocity_vector(vec)) == vec


# --- _emitter_sync_plan (collection reconciliation) -----------------------

class TestEmitterSyncPlan:
    def test_fresh_adds_all(self):
        add, remove = ssl._emitter_sync_plan([], ["A", "B"])
        assert add == ["A", "B"]
        assert remove == []

    def test_all_present_no_change(self):
        add, remove = ssl._emitter_sync_plan(["A", "B"], ["A", "B"])
        assert add == []
        assert remove == []

    def test_new_emitter_added(self):
        add, remove = ssl._emitter_sync_plan(["A"], ["A", "B"])
        assert add == ["B"]
        assert remove == []

    def test_stale_emitter_removed(self):
        add, remove = ssl._emitter_sync_plan(["A", "B"], ["A"])
        assert add == []
        assert remove == ["B"]

    def test_mixed_add_and_remove(self):
        # B vanished, C appeared; A preserved (not in either list).
        add, remove = ssl._emitter_sync_plan(["A", "B"], ["A", "C"])
        assert add == ["C"]
        assert remove == ["B"]

    def test_add_order_follows_desired(self):
        add, _ = ssl._emitter_sync_plan([], ["z", "a", "m"])
        assert add == ["z", "a", "m"]   # plan preserves caller's (sorted) order

    def test_existing_preserved_implicitly(self):
        # An existing emitter still desired is never in add or remove → kept.
        add, remove = ssl._emitter_sync_plan(["Keep"], ["Keep", "New"])
        assert "Keep" not in add and "Keep" not in remove


# --- EmitterSettings PropertyGroup schema ---------------------------------

class TestEmitterSettingsSchema:
    """The scalar emitter params must carry the full Range/List sextet so
    expand_param() works on an EmitterSettings instance unchanged."""

    _SCALARS = [
        "temperature", "density", "surface_distance", "volume_density",
        "velocity_factor", "velocity_normal",
    ]
    _SEXTET = ("_begin", "_end", "_step", "_use_range", "_use_list",
               "_list", "_index")

    def test_each_scalar_has_full_sextet(self):
        ann = ssl.EmitterSettings.__annotations__
        for p in self._SCALARS:
            for suffix in self._SEXTET:
                assert p + suffix in ann, f"missing {p}{suffix}"

    def test_velocity_and_toggle_present(self):
        ann = ssl.EmitterSettings.__annotations__
        assert "use_initial_velocity" in ann
        assert "velocity_list" in ann
        assert "velocity_index" in ann
        assert "name" in ann and "show" in ann

    def test_velocity_item_fields(self):
        ann = ssl.VelocityItem.__annotations__
        assert "text" in ann and "marked" in ann

    def test_emitters_collection_on_settings(self):
        ann = ssl.SmokeSettings.__annotations__
        assert "emitters" in ann
        assert "show_emitters" in ann


# --- wiring (source-level, like test_camera_check) ------------------------

def _src():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "scripts", "SmokeSimLab", "__init__.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestEmitterWiring:
    def test_new_classes_registered(self):
        for cls in (
            ssl.VelocityItem, ssl.EmitterSettings, ssl.SMOKE_UL_velocity_list,
            ssl.SMOKE_OT_refresh_emitters, ssl.SMOKE_OT_add_emitter_value,
            ssl.SMOKE_OT_remove_emitter_value, ssl.SMOKE_OT_add_emitter_velocity,
            ssl.SMOKE_OT_remove_emitter_velocity,
        ):
            assert cls in ssl.classes, f"{cls.__name__} not registered"

    def test_propertygroups_registered_before_settings(self):
        # CollectionProperty(type=EmitterSettings) needs the element type
        # registered first.
        idx = {c: i for i, c in enumerate(ssl.classes)}
        assert idx[ssl.ValueItem] < idx[ssl.EmitterSettings]
        assert idx[ssl.VelocityItem] < idx[ssl.EmitterSettings]
        assert idx[ssl.EmitterSettings] < idx[ssl.SmokeSettings]

    def test_panel_draws_emitters_section(self):
        assert "_emitters_ui(box_sim, s)" in _src()

    def test_domain_select_populates_emitters(self):
        # _import_domain_params calls _populate_emitters at the end.
        src = _src()
        assert "_populate_emitters(self, scene)" in src

    def test_reset_on_load_clears_emitters(self):
        assert "s.emitters.clear()" in _src()
