# How to Release SmokeSimLab

## Repository structure

```
smokeSimulationLab/          ← repo root
├── SmokeSimLab/
│   ├── __init__.py          ← addon code
│   ├── smoke_worker.py      ← headless batch worker
│   └── smoke_launcher.py    ← crash-safe launcher wrapper
├── documentation/
├── tests/
├── README.md
├── LICENSE
└── .gitignore
```

Your local working copies live in `scripts/SmokeSimLab/` (ignored by git).
When ready to release, copy them into `SmokeSimLab/` at the repo root.

---

## Releasing a new version

### 1. Update the version number

Open `SmokeSimLab/__init__.py` and bump the version in `bl_info`:

```python
bl_info = {
    ...
    "version": (0, 2, 21),   # <-- change this
    ...
}
```

Use the format `(major, minor, patch)`:
- **patch** (0.2.x) — small bug fixes, test additions, documentation updates
- **minor** (0.x.0) — new features, backwards compatible
- **major** (x.0.0) — breaking changes

Also update `WORKER_VERSION` in `smoke_worker.py` and `LAUNCHER_VERSION` in
`smoke_launcher.py` if those files changed, and keep `_EXPECTED_WORKER_VERSION`
/ `_EXPECTED_LAUNCHER_VERSION` in `__init__.py` in sync with them.

### 2. Copy your updated scripts into the repo folder

```
scripts/SmokeSimLab/__init__.py       →   SmokeSimLab/__init__.py
scripts/SmokeSimLab/smoke_worker.py   →   SmokeSimLab/smoke_worker.py
scripts/SmokeSimLab/smoke_launcher.py →   SmokeSimLab/smoke_launcher.py
```

### 3. Run tests

```bash
python -m pytest tests/
```

All tests must pass before tagging a release.

### 4. Commit your changes

```bash
git add SmokeSimLab/__init__.py SmokeSimLab/smoke_worker.py SmokeSimLab/smoke_launcher.py
git commit -m "v0.2.21: description of what changed"
```

### 5. Tag the commit and push

```bash
git tag v0.2.21
git push origin main --tags
```

The tag must start with `v` followed by numbers, e.g. `v0.2.21`.
Pushing the tag triggers the GitHub Actions release workflow automatically.

### 6. Check the release on GitHub

Go to: https://github.com/rickpalo/SmokeSimLab/releases

GitHub Actions will create the release and attach `SmokeSimLab.zip`
(the installable Blender addon) within a minute or two.

---

## If you need to redo a release tag

```bash
git tag -d v0.2.21                    # delete local tag
git push origin :refs/tags/v0.2.21   # delete remote tag
# then re-tag and push as normal
```

---

## Version history

| Version | Notes |
|---------|-------|
| 0.1.0   | Initial internal release |
| 0.2.0   | Crash detection, launcher watchdog, log sentinel |
| 0.2.5   | stderr capture to blender_stderr.txt |
| 0.2.7   | config/ false-positive fix (BUG-006) |
| 0.2.10  | Param-derived names — no more _0000 suffix (BUG-005) |
| 0.2.12  | worker_done sentinel; CRASHED status distinct from FAILED |
| 0.2.13  | Startup + wall-clock watchdog timeouts |
| 0.2.14  | Job log blanking fix via as_pointer() row identification |
| 0.2.15  | BUG-004 SKIP BAKE cache wipe fix (path equality check) |
| 0.2.16  | Pre-flight inspector; improved addon detection |
| 0.2.17  | flt_flag fix for Blender 5.x draw_item signature |
| 0.2.18  | BUG-004 presave/rename approach replacing path-equality check |
| 0.2.19  | Stable icons (Blender 5.1.1); Unicode status prefix; stale-log detection; mtime-based render progress; Reset To Defaults operator |
| 0.2.20  | Fix iterate_both AttributeError; extract _has_error; remove redundant imports; bl_options on value list operators; make_name + iterate_both regression tests |
| 0.2.21  | BUG-007: dedupe identical jobs in overlapping sweep baselines |
| 0.2.22  | BUG-008: fix negative bake/setup times; calibrate rate constants |
| 0.2.23  | Filter low-res perf samples; Fix-2 cache-reuse diagnostics |
| 0.2.24  | Fix Reset All crash; baseline-fallback improvement; UX polish |
| 0.2.25  | Bake-time sidecar file so SKIP BAKE reports real bake time |
| 0.2.26  | BUG-009: retry progress bar showed wrong job's frame count; Monitor Existing Jobs button; crash-timing TODO in launcher |
| 0.2.27  | Auto-close batch console on completion; Collect Debug Logs checkbox now toggles pause |
| 0.2.28  | Fix Monitor Existing Jobs time estimate; Utilities button reorder |
| 0.2.29  | Mtime-filtered VDB counting so progress bar advances during full re-bake |
| 0.2.30  | Fix RESUME re-baking from frame 1 by setting cache_frame_pause_data after presave merge |
| 0.2.31  | Revert cache_frame_pause_data: setting it clears frames 1–395; re-bake from 1 is correct |
| 0.2.32  | RESUME save/reload .blend to trigger Mantaflow rescan + auto-resume (with diagnostic logging) |
