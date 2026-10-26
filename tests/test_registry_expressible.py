"""Show that eleven registry packages need no hook: their trackers validate, run and populate ``App``.

For each package the committed format 2 tracker (``tests/fixtures/registry_100``) is validated
with ``check_directory`` (no error, no warning, no ``pkg.local``), run against the fixture
vendor responses, rendered to a pinned version directory that validates too, and then used to
populate an ``App`` from an archive built with the vendor's layout. Real services run
(providers, client, unpack, prepare); only the network is replaced. Two packages (obsidian and
pidgin) ship NSIS installers, which cannot be built offline, so their archive is a zip with the
same inner layout and the manifest's kind is overridden to ``zip``; the NSIS decoding itself is
in ``tests/manual_smoke.md``. Out of scope: verifying the artifact's checksum (issue 027).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from gupkg.extractors import find_7z
from gupkg.model.manifest import manifest_to_toml, render_pinned
from gupkg.pkgcheck import check_directory
from gupkg.services.populate import populate_app
from gupkg.services.track import run_tracker
from gupkg.system.fake import FakeSystem
from tests.archive_support import make_7z, make_tar, make_zip, tree
from tests.http_fixtures import REGISTRY2, environment, serve, tracker_manifest

Built = tuple[Path, str, dict[str, str], str | None]


def firefox(system: FakeSystem, work: Path) -> Built:
    """A 7z self-extractor with the browser under `core` next to the installer's own files."""
    payload = make_7z(system, work / "payload.7z", {"core/firefox.exe": "ff", "core/omni.ja": "o", "setup.exe": "s", "helper.exe": "h"})
    installer = work / "Firefox Setup 131.0.2.exe"
    installer.write_bytes(find_7z().read_bytes() + payload.read_bytes())
    return installer, installer.name, {"firefox.exe": "ff", "omni.ja": "o"}, None


def obsidian(system: FakeSystem, work: Path) -> Built:
    """An installer whose `$PLUGINSDIR` holds the real application as an inner 7z archive (zip stand-in for NSIS)."""
    inner = make_7z(system, work / "app-64.7z", {"Obsidian.exe": "app", "resources/app.asar": "asar"})
    outer = make_zip(work / "Obsidian-1.7.4.zip", {"$PLUGINSDIR/app-64.7z": inner.read_bytes(), "Uninstall Obsidian.exe": "u", "$R0/x": "x"})
    return outer, "Obsidian-1.7.4.exe", {"Obsidian.exe": "app", "resources/app.asar": "asar"}, "zip"


def pidgin(system: FakeSystem, work: Path) -> Built:
    """An offline installer with the program, a GTK runtime zip inside, and installer-only files (zip stand-in for NSIS)."""
    gtk = make_zip(work / "gtk.zip", {"Gtk/bin/libgtk.dll": "gtk"})
    outer = make_zip(work / "pidgin.zip", {
        "pidgin.exe": "p", "pidgin-uninst.exe": "u", "plugins/ssl.dll": "ssl",
        "$PLUGINSDIR/gtk.zip": gtk.read_bytes(), "$PLUGINSDIR/nsis.dll": "n",
    })
    return outer, "pidgin-2.14.13-offline.exe", {"pidgin.exe": "p", "plugins/ssl.dll": "ssl", "Gtk/bin/libgtk.dll": "gtk"}, "zip"


def keypirinha(system: FakeSystem, work: Path) -> Built:
    """A 7z with one wrapper folder and a `portable` marker folder: unpacking keeps it, `[user] prepare` remove (engine, issue 028) drops it outside portable mode."""
    archive = make_7z(system, work / "k.7z", {
        "keypirinha-2.26-x64-portable/keypirinha.exe": "k", "keypirinha-2.26-x64-portable/bin/x.dll": "d",
        "keypirinha-2.26-x64-portable/portable/.portable": "marker",
    })
    return archive, "keypirinha-2.26-x64-portable.7z", {"keypirinha.exe": "k", "bin/x.dll": "d", "portable/.portable": "marker"}, None


def lockhunter(system: FakeSystem, work: Path) -> Built:
    """A zip holding one executable whose name carries the version."""
    archive = make_zip(work / "lh.zip", {"lockhuntersetup_portable_3-4-3.exe": "lh"})
    return archive, "lockhuntersetup_portable_3-4-3.zip", {"LockHunter.exe": "lh"}, None


def volta(system: FakeSystem, work: Path) -> Built:
    """A flat zip with three executables; the shim is copied to the tool names."""
    archive = make_zip(work / "v.zip", {"volta.exe": "v", "volta-shim.exe": "shim", "volta-migrate.exe": "m"})
    names = ("node.exe", "npm.exe", "npx.exe", "pnpm.exe", "yarn.exe")
    return archive, "volta-2.0.1-windows.zip", {"volta.exe": "v", "volta-shim.exe": "shim", "volta-migrate.exe": "m", **{n: "shim" for n in names}}, None


def hwinfo(system: FakeSystem, work: Path) -> Built:
    """A plain zip."""
    archive = make_zip(work / "h.zip", {"HWiNFO64.EXE": "h", "HWiNFO64.INI": "i"})
    return archive, "hwi_1002.zip", {"HWiNFO64.EXE": "h", "HWiNFO64.INI": "i"}, None


def crystaldiskinfo(system: FakeSystem, work: Path) -> Built:
    """A plain zip."""
    archive = make_zip(work / "c.zip", {"DiskInfo64.exe": "d", "Resource/theme.ini": "t"})
    return archive, "CrystalDiskInfo9_4_2.zip", {"DiskInfo64.exe": "d", "Resource/theme.ini": "t"}, None


def autoruns(system: FakeSystem, work: Path) -> Built:
    """A plain zip."""
    archive = make_zip(work / "a.zip", {"Autoruns64.exe": "a", "autoruns.chm": "c"})
    return archive, "Autoruns.zip", {"Autoruns64.exe": "a", "autoruns.chm": "c"}, None


def msys2(system: FakeSystem, work: Path) -> Built:
    """A `.tar.xz` with everything below an `msys64` wrapper."""
    archive = make_tar(work / "m.tar.xz", [
        ("msys64/usr/bin/bash.exe", b"bash", "file"), ("msys64/msys2.exe", b"m", "file"), ("msys64/etc/profile", b"p", "file"),
    ], "w:xz")
    return archive, "msys2-base-x86_64-20240925.tar.xz", {"usr/bin/bash.exe": "bash", "msys2.exe": "m", "etc/profile": "p"}, None


def julia(system: FakeSystem, work: Path) -> Built:
    """A zip with the distribution below `julia-<version>`."""
    archive = make_zip(work / "j.zip", {"julia-1.11.0/bin/julia.exe": "j", "julia-1.11.0/lib/libjulia.dll": "l"})
    return archive, "julia-1.11.0-win64.zip", {"bin/julia.exe": "j", "lib/libjulia.dll": "l"}, None


PACKAGES: list[tuple[str, list[str], Callable[[FakeSystem, Path], Built]]] = [
    ("firefox", ["firefox"], firefox),
    ("obsidian", ["github_obsidian"], obsidian),
    ("pidgin", ["index_pidgin"], pidgin),
    ("keypirinha", ["github_keypirinha"], keypirinha),
    ("lockhunter", ["page_lockhunter"], lockhunter),
    ("volta", ["github_volta"], volta),
    ("hwinfo", ["page_hwinfo"], hwinfo),
    ("crystaldiskinfo", ["redirect_crystaldiskinfo"], crystaldiskinfo),
    ("autoruns", ["last_modified_autoruns"], autoruns),
    ("msys2", ["github_msys2"], msys2),
    ("julia", ["julia"], julia),
]


@pytest.mark.parametrize(("package", "cases", "build"), [pytest.param(*row, id=row[0]) for row in PACKAGES])
def test_the_package_is_expressed_with_no_hook_and_populates_its_app(
    system: FakeSystem, tmp_path: Path, package: str, cases: list[str], build: Callable[[FakeSystem, Path], Built]
) -> None:
    """The tracker validates cleanly with no `pkg.local`; its pinned version validates; populating gives the expected `App`."""
    directory = REGISTRY2 / package / "_check_updates"
    report = check_directory(directory)
    assert report.errors == () and report.warnings == (), (report.errors, report.warnings)
    assert not (directory / "pkg.local").exists()

    # Discover the newest version from the fixture responses and write the pinned manifest.
    serve(system, *cases)
    manifest = tracker_manifest(package)
    result = run_tracker(manifest, environment(system, tmp_path, package=package))
    pinned = render_pinned(manifest, result)
    version_dir = tmp_path / package / result.version
    version_dir.mkdir(parents=True)
    (version_dir / "pkg.toml").write_text(manifest_to_toml(pinned), encoding="utf-8")
    assert check_directory(version_dir).ok, check_directory(version_dir).errors

    # Populate an App from an archive with the vendor's layout, as the built-in download script does.
    work = tmp_path / "artifacts"
    work.mkdir()
    artifact, filename, expected, stand_in_kind = build(system, work)
    if stand_in_kind is not None:
        pinned = dataclasses.replace(pinned, source=dataclasses.replace(pinned.source, kind=stand_in_kind))
    stage = tmp_path / "stage"
    stage.mkdir()

    populate_app(system, pinned, artifact, stage / "App", filename=filename)

    assert tree(stage / "App") == expected
    assert list(stage.iterdir()) == [stage / "App"]
