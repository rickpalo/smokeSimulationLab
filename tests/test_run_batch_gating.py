"""Tests for Run Batch gating (TODO-25) and Render Simulation Result (TODO-26)."""
import sys
import os
import re
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from SmokeSimLab import _batch_ready, _on_render_sim_result_update


# ── TODO-25: Run Batch button enabled only when a runnable batch exists ───────

class TestBatchReady:
    def _make_batch(self, root, with_bat=True, job_indices=(0,)):
        out = root / "out"; out.mkdir()
        if with_bat:
            (out / "run_smoke_batch.bat").write_text("@echo off")
        jobs = out / "jobs"; jobs.mkdir()
        for i in job_indices:
            (jobs / f"job_{i:04d}.json").write_text("{}")
        return str(out)

    def test_ready_when_bat_and_jobs_present(self, tmp_path):
        out = self._make_batch(tmp_path)
        assert _batch_ready(out) is True

    def test_not_ready_without_bat(self, tmp_path):
        out = self._make_batch(tmp_path, with_bat=False)
        assert _batch_ready(out) is False

    def test_not_ready_without_jobs_dir(self, tmp_path):
        out = tmp_path / "out"; out.mkdir()
        (out / "run_smoke_batch.bat").write_text("@echo off")
        assert _batch_ready(str(out)) is False

    def test_not_ready_with_empty_jobs_dir(self, tmp_path):
        out = self._make_batch(tmp_path, job_indices=())
        assert _batch_ready(out) is False

    def test_not_ready_when_only_non_job_files(self, tmp_path):
        out = tmp_path / "out"; out.mkdir()
        (out / "run_smoke_batch.bat").write_text("@echo off")
        jobs = out / "jobs"; jobs.mkdir()
        (jobs / "config.json").write_text("{}")     # not a job_NNNN.json
        (jobs / "job_0000.log").write_text("log")
        assert _batch_ready(str(out)) is False

    def test_missing_output_path(self, tmp_path):
        assert _batch_ready(str(tmp_path / "does_not_exist")) is False


# ── TODO-26: bake-only mode clears "Display Results When Finished" ────────────

class TestRenderSimResultUpdate:
    def test_disabling_render_clears_show_results(self):
        s = types.SimpleNamespace(render_simulation_result=False, show_results=True)
        _on_render_sim_result_update(s, None)
        assert s.show_results is False

    def test_enabling_render_leaves_show_results_untouched(self):
        s = types.SimpleNamespace(render_simulation_result=True, show_results=True)
        _on_render_sim_result_update(s, None)
        assert s.show_results is True


# ── TODO-26: worker honours render_simulation_result with a safe default ──────

class TestWorkerRenderGuard:
    """The worker is a flat script importing bpy at module scope, so it can't be
    imported; assert the guard exists in source instead (regression for the
    bake-only skip and its backwards-compatible default)."""
    def _worker_src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_reads_flag_with_default_true(self):
        src = self._worker_src()
        assert re.search(
            r'render_simulation_result\s*=\s*cfg\.get\(\s*"render_simulation_result"\s*,\s*True\s*\)',
            src,
        )

    def test_has_bake_only_skip_branch(self):
        src = self._worker_src()
        assert "if not render_simulation_result:" in src


class TestWorkerBakeFrameRange:
    """Regression: the worker must constrain the bake to the job's frame range.

    bpy.ops.fluid.bake_all() bakes the domain's cache_frame_start/end, not the
    scene range — so without setting them the worker baked the .blend's full
    range (observed: 500 frames for a 1-20 job). Worker can't be imported, so
    assert the assignment exists in source."""
    def _worker_src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_sets_cache_frame_start_from_job(self):
        assert re.search(r"d\.cache_frame_start\s*=\s*frame_start", self._worker_src())

    def test_sets_cache_frame_end_from_job(self):
        assert re.search(r"d\.cache_frame_end\s*=\s*frame_end", self._worker_src())


class TestWorkerResumeNoReload:
    """Regression for BUG-010 attempt 4 (v0.3.1): the RESUME branch must NOT
    save/reload the .blend. open_mainfile() mid-script leaves bake_all() unable
    to run in windowed (EEVEE) mode and the worker hangs forever on the bake."""
    def _worker_src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_no_open_mainfile_call(self):
        # The explanatory comment may mention open_mainfile(); only the actual
        # bpy operator call is forbidden.
        assert "bpy.ops.wm.open_mainfile" not in self._worker_src()

    def test_no_save_as_mainfile_call(self):
        assert "bpy.ops.wm.save_as_mainfile" not in self._worker_src()


class TestWorkerPhaseSplit:
    """Increment 1 of the two-phase pipeline: worker honours --phase {bake,render,
    both}, defaulting to 'both' (= original single-pass behavior). Worker can't be
    imported; assert the gating exists in source."""
    def _src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_parses_phase_with_both_default(self):
        src = self._src()
        assert 'phase = "both"' in src
        assert 'do_bake   = phase in ("bake", "both")' in src
        assert 'do_render = phase in ("render", "both")' in src

    def test_render_phase_forces_skip_bake(self):
        # SKIP decision must also fire when do_bake is False (render phase).
        assert "if (use_existing_cache and bake_complete) or not do_bake:" in self._src()

    def test_bake_phase_exits_before_render(self):
        src = self._src()
        assert "if not do_render:" in src
        assert "phase=bake complete — skipping render and CSV." in src


class TestWorkerFinalStillCopy:
    """TODO-32: when the animation sequence already rendered frame_end this run
    with identical settings, the final still should be a shutil.copy2 from the
    sequence rather than a duplicate render."""
    def _src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_copy_gated_on_frame_rendered_this_run_and_file_present(self):
        # Only copy when frame_end was in this run's frames_to_render AND the
        # source PNG exists — guards against placeholder-skipped stale frames.
        src = self._src()
        assert "_can_copy = (frame_end in frames_to_render) and os.path.isfile(_src_png)" in src

    def test_copy_uses_shutil_copy2(self):
        # copy2 preserves the mtime; preserves the "this is fresh" signal.
        assert "shutil.copy2(_src_png, png)" in self._src()

    def test_falls_back_to_render_on_copy_failure(self):
        src = self._src()
        # The fallback is the original render path, gated by `if not _can_copy:`.
        assert "if not _can_copy:" in src
        # And copy failures explicitly clear the flag so the render fires.
        assert "_can_copy = False" in src


class TestRenderAnimationGate:
    """TODO-33: still-only mode — Render Animation off skips the PNG sequence
    + MP4 but still produces <name>.png via the final-still path."""
    def _src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_worker_reads_render_animation_default_true(self):
        # Default True preserves the original behaviour for pre-TODO-33 JSONs.
        assert 'render_animation         = cfg.get("render_animation", True)' in self._src()

    def test_animation_short_circuits_frames_to_render(self):
        src = self._src()
        assert "if not render_animation:" in src
        assert "frames_to_render = set()" in src

    def test_ffmpeg_gated_on_render_animation(self):
        assert "if render_animation:" in self._src()

    def test_export_writes_render_animation_field(self):
        import inspect
        import SmokeSimLab as ssl
        src = inspect.getsource(ssl.export_batch)
        assert '"render_animation":         s.render_animation' in src


# ── Increment 4: per-phase status (RENDERING vs IN_PROGRESS) ─────────────────

class TestJobLogPhaseAwareStatus:
    """Active job is BAKING (IN_PROGRESS) before .bake.done exists, and
    RENDERING after it does — so the user sees the phase the job is in."""

    def _setup(self, tmp_path, files_by_job):
        """Build a jobs dir, populate _job_log_rows for N jobs, return a stub
        SmokeSettings that _update_job_log_statuses will accept."""
        import time as _t
        import types as _types
        import SmokeSimLab as ssl

        jobs = tmp_path / "jobs"
        jobs.mkdir()
        # Write each requested file with a controllable mtime so the
        # most-recently-touched .log can drive the active-job selection.
        now = _t.time()
        for delta, (filename, body) in files_by_job:
            p = jobs / filename
            p.write_text(body)
            mt = now + delta
            import os as _os
            _os.utime(p, (mt, mt))

        # Reset module-level state then seed for as many jobs as we mention.
        ssl._job_log_rows.clear()
        n_jobs = max(int(f.split("_")[1][:4]) for _, (f, _b) in files_by_job) + 1
        for i in range(n_jobs):
            ssl._job_log_rows.append((i + 1, f"job_{i:04d}"))
        ssl._job_statuses.clear()

        s = _types.SimpleNamespace(
            job_log_items=[None] * n_jobs,
            job_log_auto_scroll=False,
            job_log_index=0,
        )
        return ssl, s, jobs

    def test_active_with_bake_done_is_RENDERING(self, tmp_path):
        # Job 0 actively rendering (newest .log mtime, bake.done present).
        # Job 1 baked but render hasn't reached it yet.
        ssl, s, jobs = self._setup(tmp_path, [
            ( 0, ("job_0000.log",       "Rendering animation (20 frames)\n")),
            (-1, ("job_0000.bake.done", "done")),
            (-30, ("job_0001.log",      "phase=bake complete\n")),
            (-31, ("job_0001.bake.done","done")),
        ])
        ssl._update_job_log_statuses(s, str(jobs))
        assert ssl._job_statuses[1] == 'RENDERING'
        assert ssl._job_statuses[2] == 'BAKED'

    def test_active_without_bake_done_is_IN_PROGRESS(self, tmp_path):
        # Bake phase: active job has NO bake.done yet → baking → IN_PROGRESS.
        ssl, s, jobs = self._setup(tmp_path, [
            ( 0, ("job_0000.log", "Baking...\n")),
        ])
        ssl._update_job_log_statuses(s, str(jobs))
        assert ssl._job_statuses[1] == 'IN_PROGRESS'

    def test_status_icons_include_rendering(self):
        import SmokeSimLab as ssl
        icons = ssl.SMOKE_UL_job_log._STATUS_ICONS
        prefix = ssl.SMOKE_UL_job_log._STATUS_PREFIX
        assert 'RENDERING' in icons
        assert 'RENDERING' in prefix
        # Distinct icon from IN_PROGRESS so the user sees the phase change.
        assert icons['RENDERING'] != icons['IN_PROGRESS']
        assert prefix['RENDERING'] != prefix['IN_PROGRESS']


class TestRenderPhaseFastFail:
    """TODO-34: render-phase early-exit when bake didn't leave a usable cache,
    plus wipe the partial cache so auto-retry takes the FULL-bake path."""
    def _src(self):
        path = os.path.join(os.path.dirname(__file__), "..",
                            "scripts", "SmokeSimLab", "smoke_worker.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_gated_on_render_phase_only(self):
        # Only the render-phase process should bail — single-pass (`both`) and
        # the bake-phase process own their own cache decisions.
        src = self._src()
        # Find the TODO-34 block opener.
        assert "TODO-34: render-phase fast-fail." in src
        assert "if not do_bake:" in src

    def test_checks_bake_done_for_error(self):
        src = self._src()
        assert '_r34_bake_done = os.path.join(_r34_jobs_dir, _r34_stem + ".bake.done")' in src
        # Missing bake.done is treated as failure (defensive default True).
        assert "_r34_bake_failed = True" in src
        assert '"error" in fh.read().lower()' in src

    def test_checks_cache_frame_completeness(self):
        src = self._src()
        assert "_count_data_files(cache_dir)" in src
        assert "_r34_expected = frame_end - frame_start + 1" in src
        assert "_r34_incomplete = (_r34_existing < _r34_expected)" in src

    def test_wipes_cache_to_force_full_rebake(self):
        import re
        src = self._src()
        # shutil.rmtree on the cache dir forces auto-retry's FULL bake path
        # (use_existing_cache + empty dir → FULL).  Followed by sys.exit(1) so
        # the .bat / addon count the job as failed and auto-retry triggers.
        # DOTALL: rmtree and sys.exit are on separate lines.
        assert re.search(r"shutil\.rmtree\(cache_dir\).*sys\.exit\(1\)",
                         src, re.DOTALL)
