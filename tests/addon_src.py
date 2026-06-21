"""Shared source-text reader for the BatchSimLab addon package.

TODO-58 split the monolithic ``__init__.py`` into sibling modules
(``jobgen``/``emitters``/``settings_io``/``progress``/``properties``/… with
``operators``/``ui`` still to come).  Many regression tests assert "the addon
source contains X" by reading the file and grepping the text.  Those checks must
stay valid no matter which module a given line ends up in, so they read the whole
addon package concatenated rather than ``__init__.py`` alone.

The two deployable helper scripts shipped inside the package folder
(``smoke_worker.py`` / ``smoke_launcher.py``) are NOT part of the addon source —
they are tested separately via their own readers — so any ``smoke_*.py`` is
excluded here.  This keeps ``... not in src`` assertions and version-stamp checks
scoped to the addon itself.
"""
import glob
import os

_PKG_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts", "BatchSimLab")


def read_addon_source():
    """Return every addon-package ``*.py`` module concatenated (worker/launcher
    excluded).  Stable ordering so failures point at reproducible text."""
    parts = []
    for path in sorted(glob.glob(os.path.join(_PKG_DIR, "*.py"))):
        if os.path.basename(path).startswith("smoke_"):
            continue  # smoke_worker.py / smoke_launcher.py are separate deployables
        with open(path, encoding="utf-8") as fh:
            parts.append(fh.read())
    return "\n".join(parts)
