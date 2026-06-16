"""TODO-55 increment 4 (worker): emitter flow-settings application + the
emitter text overlay.

The worker is a top-level Blender script (not importable — its body calls
sys.exit without argv), so the pure overlay helpers are extracted via exec (the
same pattern as test_bake_time_sidecar.py) and the inline flow-settings block is
checked at the source level (cf. test_smoke_launcher.py)."""
import pathlib

_WORKER_SRC = (pathlib.Path(__file__).resolve().parent.parent
               / "scripts" / "SmokeSimLab" / "smoke_worker.py")


def _load_overlay():
    """Extract and exec the three pure overlay helpers from the worker."""
    src = _WORKER_SRC.read_text(encoding="utf-8")
    start = src.index("def _emitter_overlay_line(")
    end = src.index("def update_text_objects(")
    ns: dict = {}
    exec(src[start:end], ns)
    return ns


_NS      = _load_overlay()
_line    = _NS["_emitter_overlay_line"]
_fmt     = _NS["_format_emitter_overlay"]
_prepend = _NS["_prepend"]


def _ep(**over):
    d = {"temperature": 1.0, "density": 1.0, "surface_distance": 1.5,
         "volume_density": 0.0, "use_initial_velocity": False,
         "velocity_factor": 0.0, "velocity_normal": 0.0,
         "velocity_coord": [0.0, 0.0, 0.0]}
    d.update(over)
    return d


class TestEmitterOverlayLine:
    def test_basic_scalars(self):
        line = _line("Emitter1", _ep())
        assert line == "Emitter1: Init Temp-1, Dens-1, SurfE-1.5, VolE-0"

    def test_velocity_off_no_vel(self):
        assert "Vel" not in _line("E", _ep(use_initial_velocity=False))

    def test_velocity_on_appends_vel(self):
        line = _line("E", _ep(use_initial_velocity=True,
                              velocity_factor=2.0, velocity_normal=1.0,
                              velocity_coord=[0.0, 0.0, 5.0]))
        assert "Vel S-2 N-1 (0,0,5)" in line

    def test_trims_trailing_zeros(self):
        assert "Temp-2.5" in _line("E", _ep(temperature=2.5))


class TestFormatEmitterOverlay:
    def test_empty(self):
        assert _fmt({}) == ("", "")

    def test_single_goes_left(self):
        left, right = _fmt({"A": _ep()})
        assert left.startswith("A:")
        assert right == ""

    def test_two_split_left_right(self):
        left, right = _fmt({"A": _ep(), "B": _ep()})
        assert left.startswith("A:")     # even index → left
        assert right.startswith("B:")    # odd index → right

    def test_three_even_odd_split(self):
        left, right = _fmt({"A": _ep(), "B": _ep(), "C": _ep()})
        # sorted A,B,C → A(0) left, B(1) right, C(2) left
        assert "A:" in left and "C:" in left
        assert "B:" in right and "B:" not in left

    def test_sorted_by_name(self):
        # Insertion order Zeta,Alpha — output must be name-sorted (Alpha first).
        left, _ = _fmt({"Zeta": _ep(), "Alpha": _ep()})
        assert left.startswith("Alpha:")


class TestPrepend:
    def test_with_extra(self):
        assert _prepend("EM", "Dissolve-None") == "EM\nDissolve-None"

    def test_empty_extra_returns_base(self):
        assert _prepend("", "Dissolve-None") == "Dissolve-None"


class TestWorkerSource:
    """Source-level checks for the version bump and the inline flow-settings
    application block (an inline script section, not a function)."""

    def _src(self):
        return _WORKER_SRC.read_text(encoding="utf-8")

    def test_worker_version_bumped(self):
        assert 'WORKER_VERSION = "0.9.0"' in self._src()

    def test_applies_emitter_block(self):
        src = self._src()
        assert 'job_emitters = p.get("emitters", {})' in src
        assert "fluid_type == 'FLOW'" in src
        assert ".flow_settings" in src

    def test_applies_each_flow_attr(self):
        src = self._src()
        # the four scalars are set via setattr over a tuple of attr names
        for attr in ("temperature", "density", "surface_distance", "volume_density"):
            assert attr in src
        assert "use_initial_velocity" in src
        assert "velocity_coord" in src

    def test_overlay_prepended_to_dissolve_and_time(self):
        src = self._src()
        assert '_set_text(text_map.get("dissolve", ""), _prepend(left_str' in src
        assert '_set_text(text_map.get("time", ""), _prepend(right_str' in src

    def test_emitter_apply_runs_before_maintain_density(self):
        # maintain_density (emitter_densities) must keep final say on density.
        src = self._src()
        assert src.index('job_emitters = p.get("emitters"') < src.index("if emitter_densities:")
