"""BUG-016 regression: per-job render_samples must be applied to EEVEE.

setup_eevee() previously only switched the engine and never set
scene.eevee.taa_render_samples, so EEVEE renders silently used whatever
sample count was saved in the .blend — the per-job value was logged but not
applied (a silent data lie that also invalidated the TODO-51 samples
calibration).  These are source-inspection tests, like the other worker
checks, because setup_eevee() needs a live bpy/EEVEE context to execute.
"""
import os
import re


def _worker_src():
    p = os.path.join(os.path.dirname(__file__), "..",
                     "scripts", "SmokeSimLab", "smoke_worker.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class TestBug016EeveeSamples:
    def test_setup_eevee_accepts_samples_param(self):
        assert re.search(r"def setup_eevee\(scene,\s*samples", _worker_src()), \
            "setup_eevee must accept a samples parameter"

    def test_setup_eevee_applies_taa_render_samples(self):
        # Both the EEVEE Next and legacy EEVEE branches must apply it.
        assert _worker_src().count(
            "scene.eevee.taa_render_samples = samples") >= 2, \
            "setup_eevee must set taa_render_samples in both EEVEE branches"

    def test_no_eevee_callsite_ignores_samples(self):
        src = _worker_src()
        # The old bug was bare `setup_eevee(scene)` calls that dropped the
        # per-job sample count.  Every render-path call must forward it.
        bare = re.findall(r"setup_eevee\(scene\)", src)
        assert not bare, \
            f"setup_eevee called without samples at {len(bare)} site(s)"
        assert "setup_eevee(scene, samples=render_samples)" in src
