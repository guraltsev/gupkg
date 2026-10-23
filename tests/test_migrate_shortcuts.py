"""Cover ``gupkg-migrate shortcuts``: `.lnk` files in `_shortcuts` become `[[shortcut]]` tables.

Behavior covered: each shortcut file becomes a shortcut named by its place under `_shortcuts`;
package-owned paths become `$App` and `$VersionRoot` paths; an existing shortcut of the same
name is replaced and the others are kept; imported files are renamed to `.lnk.imported`, the
old `pkg.toml` is backed up, and `--check` changes nothing; a format 1 file is refused.

Real: the command line, and in the `real-lnk` variants (`windows_integration`) real `.lnk` files
read through the Windows Shell API. Fake: ``FakeSystem`` in the other variants, with placeholder
files for the folder scan. Out of scope: Start Menu integration and shortcuts outside `_shortcuts`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gupkg.model.manifest import parse_manifest
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ShortcutSpec
from gupkg.system.windows import WindowsSystem
from gupkg_migrate.cli import main


MANIFEST = """\
format = 2
[source]
shipped = true

[[shortcut]]
name = "Tool"
target = "$App\\\\old.exe"

[[shortcut]]
name = "Kept"
target = "$App\\\\kept.exe"
"""


@pytest.fixture(params=[
    pytest.param("fake", id="fake-system"),
    pytest.param("windows", id="real-lnk", marks=[
        pytest.mark.windows_integration,
        pytest.mark.skipif(os.name != "nt", reason="reads Windows shortcuts"),
    ]),
])
def system(request: pytest.FixtureRequest, tmp_path: Path):
    """The boundary that reads shortcuts: a FakeSystem with placeholder files, or the real Windows Shell API."""
    return FakeSystem(tmp_path / "fake-machine") if request.param == "fake" else WindowsSystem()


@pytest.fixture
def version(tmp_path: Path, system) -> Path:
    """A format 2 version directory with an App and two shortcut files in `_shortcuts` written through *system*."""
    folder = tmp_path / "tool" / "1.0"
    (folder / "App").mkdir(parents=True)
    (folder / "App" / "tool.exe").write_text("x", encoding="utf-8")
    (folder / "pkg.toml").write_text(MANIFEST, encoding="utf-8")
    shortcuts = folder / "_shortcuts"
    (shortcuts / "Sub").mkdir(parents=True)
    system.write_shortcut(shortcuts / "Tool.lnk", ShortcutSpec(
        target_path=str(folder / "App" / "tool.exe"), working_directory=str(folder / "App"),
        icon_location=f"{folder / 'Icons' / 'tool.ico'},0", description="The tool",
    ))
    system.write_shortcut(shortcuts / "Sub" / "Tool Settings.lnk", ShortcutSpec(
        target_path=str(folder / "App" / "tool.exe"), arguments="--settings",
    ))
    # A FakeSystem keeps records elsewhere; a placeholder file lets the folder scan find the shortcut.
    for link in ("Tool.lnk", "Sub/Tool Settings.lnk"):
        (shortcuts / link).touch()
    return folder


def test_shortcut_files_become_shortcut_tables_with_package_variables(version: Path, system) -> None:
    """Names follow the folder layout, App and Icons paths become variables, and the old same-named shortcut is replaced."""
    assert main(["shortcuts", str(version)], system=system) == 0

    manifest = parse_manifest((version / "pkg.toml").read_text(encoding="utf-8"))
    rows = {row.name: row for row in manifest.shortcuts}
    assert set(rows) == {"Kept", "Tool", "Sub\\Tool Settings"}
    assert rows["Kept"].target == "$App\\kept.exe"
    assert rows["Tool"].target == "$App\\tool.exe"
    assert rows["Tool"].working_dir == "$App"
    assert rows["Tool"].icon_location == "$VersionRoot\\Icons\\tool.ico,0"
    assert rows["Tool"].description == "The tool"
    assert rows["Sub\\Tool Settings"].arguments == "--settings"


def test_imported_files_are_archived_and_the_old_manifest_is_backed_up(version: Path, system) -> None:
    """Imported `.lnk` files get the `.lnk.imported` suffix and `pkg.toml.bak` holds the old text."""
    assert main(["shortcuts", str(version)], system=system) == 0

    assert not list((version / "_shortcuts").rglob("*.lnk"))
    assert len(list((version / "_shortcuts").rglob("*.lnk.imported"))) == 2
    assert (version / "pkg.toml.bak").read_text(encoding="utf-8") == MANIFEST


def test_check_mode_changes_nothing(version: Path, system, capsys: pytest.CaptureFixture[str]) -> None:
    """`--check` prints the new `pkg.toml` and leaves the manifest and the shortcut files alone."""
    assert main(["shortcuts", str(version), "--check"], system=system) == 0

    assert (version / "pkg.toml").read_text(encoding="utf-8") == MANIFEST
    assert len(list((version / "_shortcuts").rglob("*.lnk"))) == 2
    assert 'name = "Sub\\\\Tool Settings"' in capsys.readouterr().out


def test_format_1_manifest_is_refused(version: Path, system, capsys: pytest.CaptureFixture[str]) -> None:
    """The shortcut import works on format 2 files; a format 1 file is refused with an instruction."""
    (version / "pkg.toml").write_text('description = "old"\n', encoding="utf-8")

    assert main(["shortcuts", str(version)], system=system) == 1

    assert "convert the package first" in capsys.readouterr().err
    assert len(list((version / "_shortcuts").rglob("*.lnk"))) == 2
