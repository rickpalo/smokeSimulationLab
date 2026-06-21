"""Regression guards for the doc/metadata quick-win fixes (2026-06-21).

Two outright bugs were found in the design+docs review:
  * Every repo link pointed at the wrong slug `github.com/rickpalo/SmokeSimLab`;
    the real remote is `github.com/rickpalo/BatchSimLab`. The in-addon
    HELP/tracker buttons therefore 404'd.
  * README declared an MIT license, contradicting the GPL-2.0-or-later LICENSE
    + manifest.

These tests fail if either regresses. NOTE: we only forbid the wrong *repo URL*
(`rickpalo/SmokeSimLab`). Legacy lowercase runtime identifiers (`smoke_settings`,
`SMOKE_*`, `.smokesettings`) are intentional and are NOT what we guard against.
"""
import os
import re
import tomllib

_ROOT = os.path.join(os.path.dirname(__file__), "..")

WRONG_REPO_URL = "github.com/rickpalo/SmokeSimLab"
RIGHT_REPO_URL = "github.com/rickpalo/BatchSimLab"

# Source/doc files that ship the repo URL to users. Generated artifacts
# (docs/index.json) and the review doc that quotes the bug are excluded.
_URL_FILES = [
    "README.md",
    os.path.join("scripts", "BatchSimLab", "__init__.py"),
    os.path.join("scripts", "BatchSimLab", "blender_manifest.toml"),
    os.path.join("documentation", "SmokeSimLab_Documentation.html"),
    "RELEASING.md",
]


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestRepoUrl:
    def test_no_wrong_slug_in_shipped_files(self):
        for rel in _URL_FILES:
            text = _read(rel)
            assert WRONG_REPO_URL not in text, (
                f"{rel} still contains the wrong repo URL {WRONG_REPO_URL!r}"
            )

    def test_manifest_website_is_correct(self):
        with open(os.path.join(_ROOT, "scripts", "BatchSimLab",
                               "blender_manifest.toml"), "rb") as fh:
            m = tomllib.load(fh)
        assert m["website"] == "https://" + RIGHT_REPO_URL

    def test_addon_doc_urls_are_correct(self):
        src = _read(os.path.join("scripts", "BatchSimLab", "__init__.py"))
        # HELP button + doc_url point at the full reference (TODO-56);
        # tracker_url at the issues page. All on the BatchSimLab repo.
        docs = f"https://{RIGHT_REPO_URL}/blob/main/DOCUMENTATION.md"
        assert f'DOCS_URL = "{docs}"' in src
        assert f'"doc_url":     "{docs}"' in src
        assert f'"tracker_url": "https://{RIGHT_REPO_URL}/issues"' in src


class TestLicense:
    def test_readme_states_gpl_not_mit(self):
        readme = _read("README.md")
        # The License section must name GPL and must not claim MIT.
        m = re.search(r"## License\s+(.+)", readme)
        assert m, "README has no License section"
        license_line = m.group(1)
        assert "GPL-2.0-or-later" in license_line
        assert "MIT" not in license_line

    def test_manifest_license_matches(self):
        with open(os.path.join(_ROOT, "scripts", "BatchSimLab",
                               "blender_manifest.toml"), "rb") as fh:
            m = tomllib.load(fh)
        assert m["license"] == ["SPDX:GPL-2.0-or-later"]
