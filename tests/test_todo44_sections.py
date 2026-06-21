"""v0.7.0 TODO-44 regression tests: Output + Progress collapsible sections.

The Setup panel was getting tall (v0.7.0 will add TODO-41/42 sim params on
top of the existing layout).  TODO-44 wraps the Iteration Mode → Run Batch
rows in an "Output" collapsible and the progress bars + summary + Job Log
in a "Progress" collapsible that auto-expands while a batch is running.

These tests assert the source contains the expected structure, the
properties exist, and the auto-expand logic references the right state.
"""
import os
import re

import pytest

from addon_src import read_addon_source


def _addon_src():
    # TODO-58: read the whole addon package — properties/UI/operators now live in
    # sibling modules, so "source contains X" checks must span them all.
    return read_addon_source()


class TestNewBoolProperties:
    """show_output and show_progress must be registered on SmokeSettings
    with default=True (sections start expanded for new installs)."""

    def test_show_output_property_exists(self):
        src = _addon_src()
        assert "show_output: bpy.props.BoolProperty(" in src, (
            "show_output BoolProperty missing — required for the Output "
            "collapsible's persistent toggle state"
        )

    def test_show_progress_property_exists(self):
        src = _addon_src()
        assert "show_progress: bpy.props.BoolProperty(" in src

    def test_both_default_true(self):
        """First-time users see both sections expanded so they can find
        the controls.  Manual collapse persists per-.blend after that."""
        src = _addon_src()
        # Find each property block and confirm default=True.
        for prop in ("show_output", "show_progress"):
            m = re.search(
                rf"{prop}: bpy\.props\.BoolProperty\(([\s\S]+?)\)",
                src,
            )
            assert m, f"{prop} property block not found"
            assert "default=True" in m.group(1), (
                f"{prop} must default=True so first-time users see the "
                f"section expanded"
            )


class TestResetOnLoadCoversNewProps:
    """_reset_on_load resets every SmokeSettings property when a .blend
    file is opened.  show_output and show_progress must be in the list
    or they'll leak state across files."""

    def test_reset_on_load_sets_show_output(self):
        src = _addon_src()
        assert re.search(r"s\.show_output\s*=\s*True", src), (
            "_reset_on_load must reset show_output to True (the default)"
        )

    def test_reset_on_load_sets_show_progress(self):
        src = _addon_src()
        assert re.search(r"s\.show_progress\s*=\s*True", src)


class TestPanelStructure:
    """The Output and Progress sections must be drawn as collapsible boxes
    with the standard TRIA_DOWN / TRIA_RIGHT toggle convention used by the
    existing Setup / Simulation Parameters / Utilities sections."""

    def test_output_section_is_a_box(self):
        src = _addon_src()
        # Find the Output section header.
        assert 'row_out.label(text="Output")' in src, (
            "Output section header label missing"
        )
        # The header is inside a box that the section's content draws into.
        assert "box_out = layout.box()" in src

    def test_progress_section_is_a_box(self):
        src = _addon_src()
        assert 'row_prog.label(text="Progress")' in src
        assert "box_prog = layout.box()" in src

    def test_output_toggle_uses_tria_icons(self):
        """Same icon convention as Setup / Sim Params / Utilities so the
        UI feels consistent."""
        src = _addon_src()
        assert "icon='TRIA_DOWN' if s.show_output else 'TRIA_RIGHT'" in src

    def test_iteration_mode_inside_output_box(self):
        """Iteration Mode block moved inside the Output collapsible per
        user spec — was at top level before TODO-44."""
        src = _addon_src()
        # The Iteration Mode label must now be drawn into box_iter (which
        # is itself nested inside box_out, the Output collapsible).
        assert 'box_iter.label(text="Iteration Mode:")' in src, (
            "Iteration Mode must be drawn inside the Output section's box"
        )
        # And box_iter must be created from box_out, not directly from layout.
        assert "box_iter = box_out.box()" in src

    def test_run_batch_drawn_inside_output(self):
        """Run Batch button is the last row of the Output collapsible."""
        src = _addon_src()
        # The run_row must be box_out.row(), not layout.row().
        assert "run_row = box_out.row()" in src, (
            "Run Batch row must be inside the Output section"
        )

    def test_progress_bars_drawn_inside_progress_box(self):
        """The 3 progress bars must use box_prog (the Progress section's
        container), not layout directly."""
        src = _addon_src()
        # All three progress bar calls should reference box_prog.
        assert src.count("box_prog.progress(") >= 3, (
            f"expected 3 box_prog.progress(...) calls (sub-task, "
            f"job-stage, overall); found {src.count('box_prog.progress(')}"
        )


class TestProgressAutoExpand:
    """Progress section auto-expands when a batch is running OR a
    post-batch summary is being displayed OR there's an active progress
    string OR job log items exist.  The user's manual collapse choice
    (show_progress) is honoured only when nothing's active."""

    def test_auto_expand_helper_exists(self):
        src = _addon_src()
        assert "_progress_active" in src, (
            "auto-expand requires a _progress_active boolean"
        )
        assert "_effective_show_progress" in src, (
            "_effective_show_progress must combine user toggle + auto-expand"
        )

    def test_auto_expand_checks_batch_is_running(self):
        """The primary trigger is _batch_is_running() — while a batch is
        active the section must be force-open."""
        src = _addon_src()
        # Find the _progress_active assignment.
        # Match the multi-line `_progress_active = ( ... )` block — non-greedy
        # `[^)]+?` is wrong because the body contains bool(...) calls; instead
        # grab up to `_effective_show_progress` which immediately follows.
        m = re.search(
            r"_progress_active\s*=\s*\([\s\S]+?\)\s*\n\s*_effective_show_progress",
            src,
        )
        assert m, "_progress_active assignment not found"
        body = m.group(0)
        assert "_running" in body, (
            "_progress_active must include _running (from _batch_is_running())"
        )

    def test_auto_expand_checks_summary(self):
        """When the batch finishes and the summary is showing, the
        section stays open so the user can read the summary."""
        src = _addon_src()
        # Match the multi-line `_progress_active = ( ... )` block — non-greedy
        # `[^)]+?` is wrong because the body contains bool(...) calls; instead
        # grab up to `_effective_show_progress` which immediately follows.
        m = re.search(
            r"_progress_active\s*=\s*\([\s\S]+?\)\s*\n\s*_effective_show_progress",
            src,
        )
        assert m, "_progress_active = (...) block not found before _effective_show_progress"
        body = m.group(0)
        assert "batch_summary_line1" in body, (
            "_progress_active must include batch_summary_line1 so the "
            "post-batch summary remains visible"
        )

    def test_auto_expand_checks_job_log_items(self):
        """If there are job_log_items (mid-batch or recently completed),
        the Progress section should be visible."""
        src = _addon_src()
        # Match the multi-line `_progress_active = ( ... )` block — non-greedy
        # `[^)]+?` is wrong because the body contains bool(...) calls; instead
        # grab up to `_effective_show_progress` which immediately follows.
        m = re.search(
            r"_progress_active\s*=\s*\([\s\S]+?\)\s*\n\s*_effective_show_progress",
            src,
        )
        assert m, "_progress_active = (...) block not found before _effective_show_progress"
        body = m.group(0)
        assert "job_log_items" in body, (
            "_progress_active must include job_log_items so the section "
            "stays open while the Job Log has rows"
        )

    def test_effective_show_combines_user_toggle_and_auto_expand(self):
        """The OR ensures: manual collapse is honoured ONLY when
        _progress_active is False (no batch, no summary, no job log)."""
        src = _addon_src()
        assert "s.show_progress or _progress_active" in src, (
            "_effective_show_progress must be `s.show_progress or _progress_active` — "
            "OR ensures auto-expand overrides manual collapse when active"
        )

    def test_progress_section_body_gated_on_effective_show(self):
        """The progress bars + summary draw only when _effective_show_progress
        is True, not the raw s.show_progress (otherwise auto-expand is
        defeated)."""
        src = _addon_src()
        assert "if _effective_show_progress:" in src

    def test_progress_toggle_still_binds_to_show_progress(self):
        """The clickable TRIA_DOWN/RIGHT toggle must bind to show_progress
        (the persistent state) — NOT _effective_show_progress (a derived
        value).  Otherwise the user's manual click would be ignored when
        auto-expand is active."""
        src = _addon_src()
        # The prop call should reference show_progress as the property name.
        assert 'row_prog.prop(s, "show_progress",' in src, (
            "Progress section toggle must bind to s.show_progress so user "
            "click persists; effective show is computed separately"
        )


class TestBug014PhasedCountsExcludeFailed:
    """v0.7.0 BUG-014: the Bake X/N and Render Y/N phased counters must
    exclude failed jobs.  v0.6.0 fixed the unphased (N done) count
    (BUG-012) but missed the phased counters — they still showed
    "Bake 13/13" when 1 of those 13 bakes had crashed."""

    def test_phase_counter_helper_exists(self):
        src = _addon_src()
        assert "_count_phase_success" in src, (
            "BUG-014 fix requires a helper that reads each .bake.done / "
            ".render.done content and excludes 'error' files"
        )

    def test_helper_reads_file_content(self):
        """The helper must open each matching file and check for 'error' —
        not just count regex matches."""
        src = _addon_src()
        m = re.search(
            r"def _count_phase_success[\s\S]+?return n",
            src,
        )
        assert m, "_count_phase_success function not found"
        body = m.group(0)
        assert "open(" in body
        assert '"error"' in body
        assert "_fh.read()" in body

    def test_bake_done_uses_helper(self):
        """The naive `sum(1 for _f in _all_files if _BAKE_DONE_RE.match(_f))`
        pattern must be GONE — it was the BUG-014 root cause."""
        src = _addon_src()
        assert "_bake_done_n   = _count_phase_success(_BAKE_DONE_RE)" in src
        # And the old naive form must not appear.
        assert "_bake_done_n   = sum(1 for _f in _all_files if _BAKE_DONE_RE.match(_f))" not in src

    def test_render_done_uses_helper(self):
        src = _addon_src()
        assert "_render_done_n = _count_phase_success(_RENDER_DONE_RE)" in src
        assert "_render_done_n = sum(1 for _f in _all_files if _RENDER_DONE_RE.match(_f))" not in src


class TestJobLogNestedInsideProgress:
    """Per user spec, the Job Log lives inside the Progress section
    (was a top-level collapsible before TODO-44)."""

    def test_job_log_uses_box_prog_not_layout(self):
        src = _addon_src()
        # The Job Log box should be created from box_prog (Progress
        # section's container), not layout directly.
        assert "box_log = box_prog.box()" in src, (
            "Job Log box must be drawn inside the Progress section, "
            "not at top level"
        )
