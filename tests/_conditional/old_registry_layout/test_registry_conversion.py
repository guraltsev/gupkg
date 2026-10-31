"""Cover the conversion of a registry that still names its package launcher `gupkg-tui.cmd` instead of `manage.cmd`.

Such a registry has the old file in every version directory. The registry tool's `launchers` command removes it everywhere
(a tracker folder included), writes `manage.cmd`, and refreshes the fingerprint of each version it changed; a second run changes
nothing. `validate` names a version directory that still carries the old name and tells the maintainer to run `launchers`.
The behaviour belongs to old registries only; the folder README gives the condition that ends these tests.

Real: the registry tool and validator, real files. Out of scope: the install engine.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from gupkg.launchers import launcher_text
from gupkg.services.fingerprint import fingerprint_directory
from gupkg_registry.cli import main
from gupkg_registry.layout import read_registry_file, write_registry_file
from gupkg_registry.validate import validate_registry
from tests.http_fixtures import FIXTURES

VERSION = "qbittorrent/5.2.3"
TRACKER = "gh/_check_updates"


def old_layout_registry(tmp_path: Path) -> Path:
    """Copy the snapshot and give it the old launcher name in a version and in a tracker; return its root."""
    root = tmp_path / "registry"
    shutil.copytree(FIXTURES / "registry_100", root)
    (root / VERSION / "manage.cmd").rename(root / VERSION / "gupkg-tui.cmd")
    (root / TRACKER / "gupkg-tui.cmd").write_bytes(b"@echo off\r\n")
    settings = read_registry_file(root)
    settings["fingerprints"][VERSION] = fingerprint_directory(root / VERSION)
    write_registry_file(root, settings)
    return root


def test_validate_names_a_version_that_still_has_the_old_launcher_name(tmp_path: Path) -> None:
    """A version directory with `gupkg-tui.cmd` is reported with the fix, and its missing `manage.cmd` is reported too."""
    root = old_layout_registry(tmp_path)

    problems = validate_registry(root).problems

    assert f"{VERSION}/gupkg-tui.cmd: retired launcher name (now manage.cmd; run the registry tool: launchers)" in problems
    assert f"{VERSION}/manage.cmd: launcher is missing" in problems


def test_launchers_converts_an_old_layout_registry_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    """`launchers` removes the old name from versions and trackers, writes `manage.cmd` and the new fingerprint; running it again changes nothing."""
    root = old_layout_registry(tmp_path)

    assert main(["launchers", "--registry", str(root)]) == 0

    assert validate_registry(root).problems == ()
    assert not (root / VERSION / "gupkg-tui.cmd").exists() and not (root / TRACKER / "gupkg-tui.cmd").exists()
    assert (root / VERSION / "manage.cmd").read_bytes() == launcher_text("tui").encode("ascii")
    assert read_registry_file(root)["fingerprints"][VERSION] == fingerprint_directory(root / VERSION)
    before = (root / "_registry.toml").read_bytes()
    assert main(["launchers", "--registry", str(root)]) == 0
    assert (root / "_registry.toml").read_bytes() == before
