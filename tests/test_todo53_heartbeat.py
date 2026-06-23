"""TODO-53: launcher bake-progress heartbeat to the job log.

The worker is silent during the blocking bake_data()/bake_noise() calls, so the
launcher counts data/+noise/ VDB frames and appends a heartbeat to the job log —
but ONLY when the count grows, so a true hang still goes stale for the watchdog.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab"))

import smoke_launcher as sl

_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab")


def _src():
    with open(os.path.join(_SCRIPTS_DIR, "smoke_launcher.py"), encoding="utf-8") as fh:
        return fh.read()


def _make_cache(tmp_path, name="J", data=0, noise=0):
    base = tmp_path / "Cache" / name
    (base / "data").mkdir(parents=True)
    (base / "noise").mkdir(parents=True)
    for i in range(1, data + 1):
        (base / "data" / f"fluid_data_{i:04d}.vdb").write_bytes(b"")
    for i in range(1, noise + 1):
        (base / "noise" / f"fluid_noise_{i:04d}.vdb").write_bytes(b"")
    return str(tmp_path)


class TestCountCacheVdb:
    def test_empty_cache_is_zero(self, tmp_path):
        out = _make_cache(tmp_path)
        assert sl._count_cache_vdb(out, "J") == (0, 0)

    def test_counts_data_and_noise_separately(self, tmp_path):
        out = _make_cache(tmp_path, data=5, noise=2)
        assert sl._count_cache_vdb(out, "J") == (5, 2)

    def test_missing_cache_dir_returns_zero(self, tmp_path):
        # No Cache/ at all — OSError swallowed, never raises.
        assert sl._count_cache_vdb(str(tmp_path), "Nope") == (0, 0)

    def test_ignores_non_vdb_and_unnumbered(self, tmp_path):
        out = _make_cache(tmp_path, data=3)
        base = tmp_path / "Cache" / "J" / "data"
        (base / "config.uni").write_bytes(b"")          # not a vdb
        (base / "fluid_data.vdb").write_bytes(b"")       # no 4-digit frame number
        assert sl._count_cache_vdb(out, "J") == (3, 0)

    def test_counts_negatively_numbered_frames(self, tmp_path):
        # TODO-66 follow-up: a negative Frame Start can produce cache
        # filenames with a sign in the frame portion.
        out = _make_cache(tmp_path)
        base = tmp_path / "Cache" / "J" / "data"
        for n in ("-0049", "-0048", "0001"):
            (base / f"fluid_data_{n}.vdb").write_bytes(b"")
        assert sl._count_cache_vdb(out, "J") == (3, 0)


class TestVdbFrameRegex:
    def test_matches_data_and_noise_names(self):
        assert sl._VDB_FRAME_RE.search("fluid_data_0007.vdb")
        assert sl._VDB_FRAME_RE.search("fluid_noise_0123.vdb")

    def test_rejects_unnumbered(self):
        assert not sl._VDB_FRAME_RE.search("fluid_data.vdb")

    # TODO-66 follow-up: a negative Frame Start means Mantaflow may write a
    # cache filename with a sign in the frame portion (e.g. fluid_data_-0049.vdb).
    def test_matches_negative_frame_number(self):
        m = sl._VDB_FRAME_RE.search("fluid_data_-0049.vdb")
        assert m and m.group(1) == "-0049"


class TestHeartbeatScansSafely:
    """Guard the lessons baked into the implementation."""

    def test_uses_scandir_not_walk(self):
        # os.walk on the cache tree blocked on the Norton/Synology filter chain
        # (worker v0.5.3/0.5.4); the heartbeat must use os.scandir on the two
        # subdirs only.
        src = _src()
        m = re.search(r"def _count_cache_vdb\([\s\S]+?return _count", src)
        assert m, "_count_cache_vdb body not found"
        body = m.group(0)
        assert "os.scandir(" in body
        # The docstring mentions os.walk to explain why it's avoided; guard the
        # actual call form, not the prose.
        assert "os.walk(" not in body

    def test_heartbeat_is_throttled(self):
        assert sl._HEARTBEAT_INTERVAL >= 5
        src = _src()
        assert "_hb_next_check" in src and "_HEARTBEAT_INTERVAL" in src

    def test_heartbeat_writes_only_on_growth(self):
        # The whole point: write only when the frame count increased, so a hang
        # leaves the log stale and the watchdog still fires.
        src = _src()
        assert "if _total > _hb_total:" in src


class TestLauncherVersion:
    def test_launcher_version_bumped(self):
        m = re.search(r'^LAUNCHER_VERSION = "(\d+)\.(\d+)\.(\d+)"', _src(), re.MULTILINE)
        assert m, "LAUNCHER_VERSION constant missing"
        assert tuple(int(g) for g in m.groups()) >= (0, 6, 4)

    def test_addon_expected_matches_launcher(self):
        # The addon's version gate must match the shipped launcher.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        import BatchSimLab as ssl
        m = re.search(r'^LAUNCHER_VERSION = "(\d+\.\d+\.\d+)"', _src(), re.MULTILINE)
        assert ssl._EXPECTED_LAUNCHER_VERSION == m.group(1)
