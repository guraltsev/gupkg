"""Cover the real converted registry: all 79 packages in format 2, onto the services, with nothing lost from 0.17.

Behavior covered: every tracker and pinned version of the committed snapshot of the real registry
(``tests/fixtures/registry_100``) passes the package checker with no error and no warning; the
registry rules hold (hashes, fingerprints, launchers, explicit modes); no manifest or script says
``ignore_checksum``; at most 13 hook files remain and each says why; the portable-capable packages
declare ``portable`` in their modes; the package-specific faults of the issue are fixed (qbittorrent,
zotero, msys2, GIMP, nano); every 0.17 package's shortcuts, shims, environment variables, PATH entries
and requirements survive the rewrite, apart from five differences listed here with their reasons; every
pattern compiles under the pattern guard; and every tracker but the three Git ones has a fixture case.
Real: the snapshot, the package checker, the migration tool's reading of the 0.17 originals
(``tests/fixtures/registry_017``), the registry validation. Mocked: nothing. Out of scope: running the
trackers (``test_track_providers.py``, ``test_registry_expressible.py``) and the live vendors (manual).
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from gupkg.model.patterns import compile_pattern
from gupkg.pkgcheck import check_directory
from gupkg_migrate.package import plan_package
from gupkg_registry.layout import package_dirs
from gupkg_registry.validate import MAX_HOOK_FILES, validate_registry
from tests.http_fixtures import FIXTURES, HTTP_CASES

REGISTRY = FIXTURES / "registry_100"
OLD = FIXTURES / "registry_017"

PACKAGES = [path.name for path in package_dirs(REGISTRY)]
DIRECTORIES = [
    path.relative_to(REGISTRY).as_posix()
    for package in package_dirs(REGISTRY)
    for path in sorted(package.iterdir())
    if (path / "pkg.toml").is_file()
]
PORTABLE_CAPABLE = (
    "tixati", "SumatraPDF-portable", "notepadplusplus", "hwinfo", "crystaldiskinfo", "crystaldiskmark", "rufus",
    "keepassxc", "winscp", "winmerge", "vscode", "obsstudio", "firefox", "keypirinha", "systeminformer",
)
GIT_TRACKED = {"latex-toolkit", "overleaf-sync", "windebloattools"}


def tracker(package: str) -> dict[str, Any]:
    """Return the parsed tracker of *package*."""
    return tomllib.loads((REGISTRY / package / "_check_updates" / "pkg.toml").read_text(encoding="utf-8"))


def test_the_registry_holds_every_package_of_0_17() -> None:
    """The 79 packages of the 0.17 snapshot are all here, and each has a tracker."""
    assert PACKAGES == [path.name for path in package_dirs(OLD)] and len(PACKAGES) == 79
    assert all((REGISTRY / name / "_check_updates" / "pkg.toml").is_file() for name in PACKAGES)


@pytest.mark.parametrize("directory", [pytest.param(path, id=path) for path in DIRECTORIES])
def test_the_directory_passes_the_package_checker_with_no_error_and_no_warning(directory: str) -> None:
    """Each tracker and pinned version is valid format 2, with no leftover hook that a service could replace."""
    report = check_directory(REGISTRY / directory)

    assert report.errors == () and report.warnings == ()


def test_the_registry_rules_hold() -> None:
    """Hashes, fingerprints, launchers, explicit modes and hook notes are all in order."""
    report = validate_registry(REGISTRY)

    assert report.problems == ()
    assert report.packages == 79
    assert report.unpinned == ("qbittorrent/5.2.3",)


def test_no_package_uses_the_checksum_bypass() -> None:
    """``ignore_checksum`` appears in no file of any package."""
    offenders = [
        path.relative_to(REGISTRY).as_posix()
        for path in REGISTRY.rglob("*")
        if path.is_file() and "ignore_checksum" in path.read_text(encoding="utf-8", errors="replace")
    ]

    assert offenders == []


def test_at_most_thirteen_hook_files_remain() -> None:
    """The migration leaves no more hook files than the limit; the validation enforces the notes."""
    hooks = [p for p in REGISTRY.glob("*/*/pkg.local/**/*.py")]

    assert len(hooks) <= MAX_HOOK_FILES


@pytest.mark.parametrize("package", [pytest.param(name, id=name) for name in PORTABLE_CAPABLE])
def test_a_portable_capable_package_declares_portable_mode(package: str) -> None:
    """Each package on the issue's portable-capable list has ``portable`` in its modes and a non-empty ``[portable]`` table."""
    data = tracker(package)

    assert "portable" in data["modes"]
    assert data["portable"], "a portable-capable package declares how it enters portable mode"


def test_sqlitebrowser_declares_how_its_settings_survive_an_update() -> None:
    """The portable-only package keeps its settings file in ``data`` (an unverified declaration, listed as such in the registry)."""
    data = tracker("sqlitebrowser")

    assert data["modes"] == ["portable"] and data["portable"]["persist"] == ["DB4S-settings.ini"]


def test_ordinary_packages_do_not_claim_portable_mode() -> None:
    """Only the portable-capable packages (and nobody else) list ``portable`` in their modes."""
    claimed = sorted(name for name in PACKAGES if "portable" in tracker(name)["modes"])
    expected = sorted({*PORTABLE_CAPABLE, "sqlitebrowser"})

    assert claimed == expected


def test_portable_only_packages_say_so() -> None:
    """The three packages that were ``only_portable`` in 0.17 install only in portable mode."""
    assert {name: tracker(name)["modes"] for name in ("tixati", "SumatraPDF-portable", "sqlitebrowser")} == {
        "tixati": ["portable"], "SumatraPDF-portable": ["portable"], "sqlitebrowser": ["portable"],
    }


def test_qbittorrent_has_no_template_leftovers_and_no_forward_slash() -> None:
    """Both the tracker and the pinned 5.2.3 launch ``$App\\qbittorrent.exe`` and carry no generated-file boilerplate."""
    for path in (REGISTRY / "qbittorrent" / "_check_updates" / "pkg.toml", REGISTRY / "qbittorrent" / "5.2.3" / "pkg.toml"):
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)

        assert [row["target"] for row in data["shortcut"]] == ["$App\\qbittorrent.exe"]
        assert "Auto-generated" not in text and "Example" not in text and "$App/" not in text


def test_zotero_removes_the_installer_debris_from_app() -> None:
    """The NSIS leftovers are removed by a prepare step, and no hook is left to describe the wrong archive type."""
    data = tracker("zotero")

    removed = [item for step in data["prepare"] if "remove" in step for item in step["remove"]]
    assert {"$PLUGINSDIR", "uninstall.exe", "setup.exe"} <= set(removed)
    assert not (REGISTRY / "zotero" / "_check_updates" / "pkg.local").exists()


def test_msys2_uses_the_tar_release_and_opts_out_of_the_file_manifest() -> None:
    """MSYS2 is unpacked from its ``.tar.xz`` with the ``tar`` kind, not by running the vendor self-extractor."""
    data = tracker("msys2")

    assert data["source"]["kind"] == "tar" and data["source"]["strip"] == 1
    assert data["track"]["asset"].endswith(".tar.xz") and data["record_manifest"] is False
    assert "sfx" not in json.dumps(data["source"])


def test_gimp_runs_its_vendor_installer_and_the_package_says_so() -> None:
    """GIMP has no ``inno`` kind: its tracker runs the vendor installer explicitly."""
    data = tracker("gimp")

    assert data["source"]["installer"]["run"] and data["source"]["kind"] == "exe"


def test_nano_has_no_hook_whose_docstring_could_describe_the_wrong_archive() -> None:
    """The wrong-archive-type docstrings are gone because the hooks are."""
    assert not (REGISTRY / "nano" / "_check_updates" / "pkg.local").exists()


# Differences from the converted 0.17 originals that were chosen on purpose, with the reason.
INTENTIONAL = {
    ("backrest", "shortcut"): "the 0.17 icon file never existed in the registry; the shortcut uses the program's own icon",
    ("graphviz", "description"): "shorter text",
    ("hwinfo", "shortcut"): "0.17 had no shortcut, so the package could not be started from the Start menu",
    ("qbittorrent", "shortcut"): "the forward slash in the target is fixed (issue 031)",
    ("testdisk", "description"): "shorter text",
}
COMPARED = ("shortcut", "shim", "env", "path", "requires", "description", "project_url")


def rows(data: dict[str, Any], key: str) -> Any:
    """Return the value of *key* with tables sorted, so order does not matter."""
    value = data.get(key)
    if isinstance(value, list):
        return sorted((sorted(item.items()) if isinstance(item, dict) else item for item in value), key=str)
    return value


@pytest.mark.parametrize("package", [pytest.param(name, id=name) for name in PACKAGES])
def test_nothing_the_0_17_package_declared_is_lost(package: str) -> None:
    """Shortcuts, shims, environment variables, PATH entries, requirements and texts equal what the migration tool made of the original.

    The comparison is against the tool's own conversion of the 0.17 file, so the only differences left are the
    hand-made ones, and they are listed in ``INTENTIONAL``.
    """
    seed = next(d for d in (OLD / package).iterdir() if d.is_dir() and (d / "pkg.toml").is_file())
    original = tomllib.loads(plan_package(seed).new_text)
    rewritten = tracker(package)

    differing = {key for key in COMPARED if rows(original, key) != rows(rewritten, key)}

    assert differing == {key for (name, key) in INTENTIONAL if name == package}


def test_every_pattern_in_every_tracker_passes_the_pattern_guard() -> None:
    """All regular expressions of all trackers compile under ``compile_pattern`` (templates are checked after expansion elsewhere)."""
    keys = {"match", "asset_pattern", "tag_pattern", "page_match"}
    checked = 0

    def walk(value: Any, where: str) -> None:
        nonlocal checked
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and "${" not in item:
                    compile_pattern(item)
                    checked += 1
                walk(item, f"{where}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{where}[{index}]")

    for name in PACKAGES:
        walk(tracker(name).get("track", {}), name)

    assert checked >= 40


def test_every_tracker_except_the_git_ones_has_a_fixture_case() -> None:
    """The vendor-response fixtures cover 76 trackers; the three Git trackers are covered with a Git double instead."""
    covered = {json.loads(path.read_text(encoding="utf-8"))["tracker"] for path in HTTP_CASES.glob("*.json")}

    assert set(PACKAGES) - covered == GIT_TRACKED


def test_the_snapshot_holds_nothing_but_the_registry_packages() -> None:
    """Only package folders and ``_registry.toml`` are in the snapshot (no backups, caches or tests of the registry)."""
    names = sorted(p.name for p in REGISTRY.iterdir())

    assert names == sorted([*PACKAGES, "_registry.toml"])
    assert not [p for p in REGISTRY.rglob("*") if re.search(r"\.(bak|pyc)$|__pycache__", p.name)]
