"""TODO-52: separate "Baking noise" stage in the progress model.

Covers (a) the new _STAGES entry advances the label when the worker's noise
boundary line appears, without the data-bake keyword false-matching it, and
(b) _count_vdb_frames can count the noise/ subdir as well as data/.
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab"))

from BatchSimLab import _STAGES, _count_vdb_frames


def _stage_for(tail):
    """Mirror the poller's rightmost-keyword stage selection (rfind)."""
    label, best = "Starting", -1
    for kw, lbl, _c in _STAGES:
        pos = tail.rfind(kw)
        if pos > best:
            best, label = pos, lbl
    return label


class TestNoiseStageEntry:
    def test_noise_stage_present_at_bake_rank(self):
        noise = [(kw, c) for kw, lbl, c in _STAGES if lbl == "Baking noise"]
        assert len(noise) == 1, f"expected one 'Baking noise' stage, got {noise}"
        kw, completed = noise[0]
        assert kw == "Baking noise ("
        # Same completed-rank as the data bake (1) — deliberately a sub-stage, so
        # the Bar-3 band math / _TOTAL_SUBTASKS stay untouched.
        data = [c for kw_, lbl, c in _STAGES if lbl == "Baking simulation"]
        assert data == [completed] == [1]

    def test_noise_keyword_does_not_contain_data_keyword(self):
        # The guard that lets both coexist: "Baking noise (" must NOT contain the
        # substring "Baking (", or every noise line would also match the data
        # stage and rank ties would be ambiguous.
        assert "Baking (" not in "Baking noise ("


class TestStageAdvancement:
    # The worker's full-bake line includes "+ bake_noise" when noise is on; the
    # boundary line is logged just before bake_noise() runs.
    FULL_NOISE_TAIL = (
        "[J] Baking (MODULAR full — bake_data + bake_noise)...\n"
        "[J] Baking noise (bake_noise)...\n"
    )
    DATA_ONLY_TAIL = "[J] Baking (MODULAR full — bake_data)...\n"
    NOISE_DONE_TAIL = FULL_NOISE_TAIL + "[J] Bake complete in 812s.\n"

    def test_data_phase_shows_baking_simulation(self):
        assert _stage_for(self.DATA_ONLY_TAIL) == "Baking simulation"

    def test_noise_phase_shows_baking_noise(self):
        # Once the boundary line appears it is rightmost → label advances.
        assert _stage_for(self.FULL_NOISE_TAIL) == "Baking noise"

    def test_bake_complete_supersedes_noise(self):
        assert _stage_for(self.NOISE_DONE_TAIL) == "Verifying cache"


def _make_cache_job(tmp_path, name="J", frame_end=5, frame_start=None):
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    job_data = {"frame_end": frame_end, "output_path": str(tmp_path), "name": name}
    if frame_start is not None:
        job_data["frame_start"] = frame_start
    (jobs_dir / "job_0000.json").write_text(json.dumps(job_data))
    return jobs_dir


class TestCountVdbSubdir:
    def test_counts_data_subdir_by_default(self, tmp_path):
        jobs_dir = _make_cache_job(tmp_path)
        data = tmp_path / "Cache" / "J" / "data"
        data.mkdir(parents=True)
        for i in range(1, 4):
            (data / f"fluid_data_{i:04d}.vdb").write_bytes(b"")
        count, total = _count_vdb_frames(str(jobs_dir), "job_0000")
        assert (count, total) == (3, 5)

    def test_counts_noise_subdir_when_requested(self, tmp_path):
        jobs_dir = _make_cache_job(tmp_path)
        (tmp_path / "Cache" / "J" / "data").mkdir(parents=True)
        noise = tmp_path / "Cache" / "J" / "noise"
        noise.mkdir(parents=True)
        for i in range(1, 5):
            (noise / f"fluid_noise_{i:04d}.vdb").write_bytes(b"")
        count, total = _count_vdb_frames(str(jobs_dir), "job_0000", subdir="noise")
        assert (count, total) == (4, 5)

    def test_noise_count_independent_of_data(self, tmp_path):
        # A full data bake + partial noise bake: noise count reflects only noise/.
        jobs_dir = _make_cache_job(tmp_path)
        data = tmp_path / "Cache" / "J" / "data"
        data.mkdir(parents=True)
        for i in range(1, 6):
            (data / f"fluid_data_{i:04d}.vdb").write_bytes(b"")
        noise = tmp_path / "Cache" / "J" / "noise"
        noise.mkdir(parents=True)
        (noise / "fluid_noise_0001.vdb").write_bytes(b"")
        assert _count_vdb_frames(str(jobs_dir), "job_0000", subdir="data")[0] == 5
        assert _count_vdb_frames(str(jobs_dir), "job_0000", subdir="noise")[0] == 1


class TestCountVdbNegativeFrameStart:
    """TODO-66 follow-up: frame_end alone is not the frame count once
    frame_start can be negative, and Mantaflow's cache filenames may carry
    a sign for negative frame numbers."""

    def test_total_uses_frame_start_when_present(self, tmp_path):
        jobs_dir = _make_cache_job(tmp_path, frame_end=200, frame_start=-50)
        data = tmp_path / "Cache" / "J" / "data"
        data.mkdir(parents=True)
        count, total = _count_vdb_frames(str(jobs_dir), "job_0000")
        assert (count, total) == (0, 251)   # -50..200 inclusive

    def test_total_defaults_frame_start_to_one(self, tmp_path):
        jobs_dir = _make_cache_job(tmp_path, frame_end=5)
        data = tmp_path / "Cache" / "J" / "data"
        data.mkdir(parents=True)
        _, total = _count_vdb_frames(str(jobs_dir), "job_0000")
        assert total == 5

    def test_counts_vdb_named_with_negative_frame_number(self, tmp_path):
        jobs_dir = _make_cache_job(tmp_path, frame_end=-40, frame_start=-49)
        data = tmp_path / "Cache" / "J" / "data"
        data.mkdir(parents=True)
        for n in ("-049", "-048", "-047"):
            (data / f"fluid_data_{n}.vdb").write_bytes(b"")
        count, total = _count_vdb_frames(str(jobs_dir), "job_0000")
        assert count == 3
        assert total == 10   # -49..-40 inclusive

    def test_frame_end_zero_is_not_treated_as_missing(self, tmp_path):
        jobs_dir = _make_cache_job(tmp_path, frame_end=0, frame_start=-10)
        data = tmp_path / "Cache" / "J" / "data"
        data.mkdir(parents=True)
        (data / "fluid_data_-010.vdb").write_bytes(b"")
        count, total = _count_vdb_frames(str(jobs_dir), "job_0000")
        assert count == 1
        assert total == 11   # -10..0 inclusive
