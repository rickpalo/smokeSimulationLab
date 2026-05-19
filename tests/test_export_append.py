"""Tests for append/replace export mode helpers."""
import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import _find_next_job_index


class TestFindNextJobIndex:
    def test_missing_dir_returns_zero(self, tmp_path):
        assert _find_next_job_index(str(tmp_path / "nonexistent")) == 0

    def test_empty_dir_returns_zero(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        assert _find_next_job_index(str(d)) == 0

    def test_single_job_returns_one(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        assert _find_next_job_index(str(d)) == 1

    def test_sequential_jobs(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        for i in range(5):
            (d / f"job_{i:04d}.json").write_text("{}")
        # 5 jobs (0-4) → next is 5
        assert _find_next_job_index(str(d)) == 5

    def test_non_sequential_uses_max(self, tmp_path):
        # If jobs 0, 2, 7 exist (gap), next is 8.
        d = tmp_path / "jobs"
        d.mkdir()
        for i in (0, 2, 7):
            (d / f"job_{i:04d}.json").write_text("{}")
        assert _find_next_job_index(str(d)) == 8

    def test_ignores_non_json_files(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        (d / "job_0001.log").write_text("log")
        (d / "job_0002.done").write_text("done")
        # Only .json files count
        assert _find_next_job_index(str(d)) == 1

    def test_ignores_non_job_json_files(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        (d / "config.json").write_text("{}")      # no "job_NNNN" pattern
        (d / "job_abc.json").write_text("{}")     # non-numeric suffix
        assert _find_next_job_index(str(d)) == 1

    def test_retry_log_ignored(self, tmp_path):
        # job_0000_retry.log must not be mistaken for a job JSON.
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_0000.json").write_text("{}")
        (d / "job_0000_retry.log").write_text("log")
        assert _find_next_job_index(str(d)) == 1

    def test_large_index(self, tmp_path):
        d = tmp_path / "jobs"
        d.mkdir()
        (d / "job_9999.json").write_text("{}")
        assert _find_next_job_index(str(d)) == 10000

    # ── Semantic tests: append numbering starts correctly ────────────────────

    def test_append_after_five_jobs_starts_at_five(self, tmp_path):
        """Simulates: first batch had 5 jobs; append should start at index 5."""
        d = tmp_path / "jobs"
        d.mkdir()
        for i in range(5):
            (d / f"job_{i:04d}.json").write_text(json.dumps({"name": f"job_{i}"}))
        next_idx = _find_next_job_index(str(d))
        assert next_idx == 5
        # New jobs should be job_0005.json, job_0006.json, ...
        first_new = f"job_{next_idx:04d}.json"
        assert first_new == "job_0005.json"
