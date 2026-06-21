"""Phase-aware all-jobs ETA (TODO-46).

Regression for the "frozen 5h55m" bug: the two-pass pipeline bakes every job,
then renders every job, so the unphased `.done` count stays 0 for the whole
bake phase.  The old ETA (`jobs_not_started = total - done - 1`) therefore never
moved until rendering began.  `_estimate_batch_remaining` keys off the per-phase
`.bake.done` / `.render.done` counts so it counts down in both phases.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab"))

from BatchSimLab import _estimate_batch_remaining

# A realistic EEVEE batch: cheap bakes, expensive renders (mirrors the video).
COMMON = dict(
    total=25,
    setup_remaining=0.0,
    bake_remaining=60.0,
    render_remaining=300.0,
    still_remaining=30.0,
    default_bake_secs=120.0,
    default_render_secs=600.0,
    setup_secs=10.0,
    still_secs=30.0,
)
BAKE_JOB_COST   = 10.0 + 120.0          # setup + bake
RENDER_JOB_COST = 10.0 + 600.0 + 30.0   # setup + render + still


def _bake_phase(bake_done_n, **over):
    kw = dict(COMMON, current_job_baked=False, bake_only=False,
              bake_done_n=bake_done_n, render_done_n=0)
    kw.update(over)
    return _estimate_batch_remaining(**kw)


def _render_phase(render_done_n, **over):
    kw = dict(COMMON, current_job_baked=True, bake_only=False,
              bake_done_n=25, render_done_n=render_done_n)
    kw.update(over)
    return _estimate_batch_remaining(**kw)


class TestBakePhaseCountsDown:
    def test_eta_drops_as_bakes_complete(self):
        # THE regression: more completed bakes => smaller ETA (was frozen).
        assert _bake_phase(10) < _bake_phase(3)

    def test_drop_equals_completed_bakes_times_bake_cost(self):
        delta = _bake_phase(3) - _bake_phase(10)
        assert abs(delta - 7 * BAKE_JOB_COST) < 1e-6

    def test_render_block_is_full_during_bake_phase(self):
        # All 25 renders are still pending and dominate the estimate.
        eta = _bake_phase(3)
        # current(60) + (22-1)*130 + 25*640
        assert abs(eta - (60.0 + 21 * BAKE_JOB_COST + 25 * RENDER_JOB_COST)) < 1e-6

    def test_cached_bake_bumps_down_only_on_completion(self):
        # A fast SKIP-BAKE shows up as bake_done_n incrementing; the ETA is not
        # pre-discounted (corrupt-cache safe) but drops once .bake.done lands.
        before = _bake_phase(5)
        after  = _bake_phase(6)
        assert before - after == BAKE_JOB_COST


class TestRenderPhaseCountsDown:
    def test_eta_drops_as_renders_complete(self):
        assert _render_phase(12) < _render_phase(5)

    def test_no_bake_cost_in_render_phase(self):
        # Only renders remain; current job + remaining render jobs.
        eta = _render_phase(5)
        current = COMMON["render_remaining"] + COMMON["still_remaining"]  # setup_remaining=0
        assert abs(eta - (current + (25 - 5 - 1) * RENDER_JOB_COST)) < 1e-6


class TestBakeOnly:
    def test_bake_only_ignores_render_cost(self):
        eta = _estimate_batch_remaining(
            **dict(COMMON, current_job_baked=False, bake_only=True,
                   bake_done_n=4, render_done_n=0))
        current = COMMON["bake_remaining"]  # setup_remaining=0
        assert abs(eta - (current + (25 - 4 - 1) * BAKE_JOB_COST)) < 1e-6

    def test_bake_only_counts_down(self):
        a = _estimate_batch_remaining(
            **dict(COMMON, current_job_baked=False, bake_only=True,
                   bake_done_n=4, render_done_n=0))
        b = _estimate_batch_remaining(
            **dict(COMMON, current_job_baked=False, bake_only=True,
                   bake_done_n=5, render_done_n=0))
        assert a - b == BAKE_JOB_COST


class TestEdgeCases:
    def test_never_negative(self):
        assert _estimate_batch_remaining(
            total=1, bake_done_n=1, render_done_n=1, current_job_baked=True,
            bake_only=False, setup_remaining=0, bake_remaining=0,
            render_remaining=0, still_remaining=0,
            default_bake_secs=100, default_render_secs=100) >= 0.0

    def test_last_render_job_only_charges_current(self):
        # render_done_n=24, current is the 25th and last → no "other" jobs.
        eta = _render_phase(24)
        current = COMMON["render_remaining"] + COMMON["still_remaining"]
        assert abs(eta - current) < 1e-6

    def test_render_phase_cheaper_than_bake_phase_start(self):
        # Sanity: deep into rendering is far less than the whole batch ahead.
        assert _render_phase(20) < _bake_phase(0)
