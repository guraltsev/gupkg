"""Cover the self-contained release ZIP and the integrity data computed at build time.

Behavior covered: ``tools/build_standalone.py`` writes ``gupkg-<version>-win-amd64.zip`` as a package-root fragment of the
gupkg package (``gupkg\\<version>\\`` with ``pkg.toml``, ``pkg.local``, the launchers and the bundled tree ``gupkg\\``, and
``gupkg\\_check_updates\\``); the version's ``pkg.toml`` is pinned and has no download address; the tracker keeps its hook and
its ``check-updates.cmd`` and the version never receives them; ``MANIFEST.sha256`` lists every file of the bundled tree with
its correct digest; ``RELEASE.txt`` records the version and commit; the runtime imports from its own folder; nothing that
belongs to one install (a manager configuration, the source dump) and no bytecode ships; the build stops when the CPython
archive does not match the pinned digest; a tree is checked against its manifest (modified, missing and extra files are
reported, files outside the tree are not), and ``gupkg self status`` run from the bundle reports an edited file.

Real: the build tool run as a subprocess, the ZIP it writes, the bundle's own launcher and bundled Python for the
``self status`` test. Mocked: nothing. The dependencies are not installed (``--skip-deps``), so no network is used. Out of
scope: the bootstrap script and self-update, covered by their own modules.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from gupkg.integrity import verify_tree, verify_version_directory
from gupkg.launchers import launcher_text
from gupkg.model.manifest import ManifestKind, parse_manifest
from gupkg.pkgcheck import check_directory

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / "tools" / "build_standalone.py"


@pytest.fixture
def installed(tmp_path: Path, release_bundle) -> Path:
    """The release ZIP extracted; the version directory (the folder with pkg.toml and the bundled tree ``gupkg``)."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        archive.extractall(tmp_path / "unpacked")
    return tmp_path / "unpacked" / "gupkg" / release_bundle.version


def test_zip_is_a_package_root_fragment_of_the_gupkg_package(release_bundle) -> None:
    """The ZIP holds `gupkg/<version>/{pkg.toml, pkg.local, gupkg, install.cmd, gupkg-tui.cmd}` and `gupkg/_check_updates/{pkg.toml, pkg.local, check-updates.cmd}` only."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        names = archive.namelist()
    version = release_bundle.version

    top = {"/".join(name.split("/")[:3]) for name in names}
    assert top == {
        f"gupkg/{version}/pkg.toml", f"gupkg/{version}/pkg.local", f"gupkg/{version}/gupkg", f"gupkg/{version}/install.cmd",
        f"gupkg/{version}/gupkg-tui.cmd", "gupkg/_check_updates/pkg.toml", "gupkg/_check_updates/pkg.local",
        "gupkg/_check_updates/check-updates.cmd",
    }


def test_the_versions_pkg_toml_is_pinned_without_a_download_address_and_the_hooks_are_where_they_belong(release_bundle, installed: Path) -> None:
    """The version has no [track] and no download_url, declares the populate script and the root forwarder; only the tracker has track.py."""
    manifest = parse_manifest((installed / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.VERSION)
    tracker = parse_manifest((installed.parent / "_check_updates" / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)

    assert manifest.track is None and manifest.source.download_url is None
    assert manifest.source.script == "pkg.local/populate.py" and manifest.root_forwarder is not None
    assert [shim.name for shim in manifest.shims] == ["gupkg", "gupkg-tui", "gupkg-manager"]
    assert tracker.track is not None and tracker.root_forwarder == manifest.root_forwarder
    assert sorted(path.name for path in (installed / "pkg.local").iterdir()) == ["populate.py", "releases.py"]
    assert sorted(path.name for path in (installed.parent / "_check_updates" / "pkg.local").iterdir()) == ["populate.py", "releases.py", "track.py"]
    assert check_directory(installed).ok and check_directory(installed.parent / "_check_updates").ok


def test_the_launchers_are_the_generated_ones(installed: Path) -> None:
    """install.cmd and gupkg-tui.cmd of the version, and check-updates.cmd of the tracker, equal the template output."""
    assert (installed / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (installed / "gupkg-tui.cmd").read_bytes() == launcher_text("tui").encode("ascii")
    assert (installed.parent / "_check_updates" / "check-updates.cmd").read_bytes() == launcher_text("check-updates").encode("ascii")


def test_manifest_lists_every_file_of_the_tree_with_its_correct_digest(release_bundle) -> None:
    """MANIFEST.sha256 has one sorted '<sha256>  <path>' line per file of the bundled tree except itself, each digest correct."""
    prefix = f"gupkg/{release_bundle.version}/"
    with zipfile.ZipFile(release_bundle.zip) as archive:
        manifest = archive.read(f"{prefix}gupkg/MANIFEST.sha256").decode("utf-8").splitlines()
        files = {
            name.removeprefix(prefix): hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
            if name.startswith(f"{prefix}gupkg/") and not name.endswith("/") and name != f"{prefix}gupkg/MANIFEST.sha256"
        }

    listed = [tuple(line.split("  ", 1)) for line in manifest]
    assert [path for _, path in listed] == sorted(files)
    assert {path: digest for digest, path in listed} == files


def test_release_zip_is_named_for_the_version_and_records_its_commit(release_bundle) -> None:
    """The tree carries RELEASE.txt and VERSION with the version and the build commit, and the ZIP's digest is published beside it."""
    prefix = f"gupkg/{release_bundle.version}/gupkg/"
    with zipfile.ZipFile(release_bundle.zip) as archive:
        release_note = archive.read(f"{prefix}RELEASE.txt").decode("utf-8")
        version_file = archive.read(f"{prefix}VERSION").decode("utf-8")
        python_path = archive.read(f"{prefix}gupkg/python/python312._pth").decode("ascii")
        names = archive.namelist()

    assert release_note == f"gupkg {release_bundle.version}\ncommit {release_bundle.commit}\n"
    assert version_file == f"{release_bundle.version}\n"
    assert "Lib/site-packages" in python_path
    assert f"{prefix}gupkg/python/python.exe" in names
    assert not any(name.endswith(("gupkg-config.toml", "digest.txt")) for name in names)
    sidecar = release_bundle.zip.with_name(release_bundle.zip.name + ".sha256").read_text(encoding="ascii")
    assert sidecar.split()[0] == release_bundle.sha256


def test_release_zip_carries_the_ui_programs_their_launchers_and_shims(release_bundle) -> None:
    """The UI programs ship in the tree: the three packages beside ``gupkg``, a launcher, a configuration and (when the shim exists) an ``.exe`` for each, and the tree's own commands."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        names = set(archive.namelist())
    tree = f"gupkg/{release_bundle.version}/gupkg"

    for package in ("gupkg_ui/client.py", "gupkg_tui/cli.py", "gupkg_manager/cli.py"):
        assert f"{tree}/{package}" in names
    for program in ("gupkg-tui", "gupkg-manager"):
        assert f"{tree}/gupkg/{program}.cmd" in names and f"{tree}/gupkg/{program}.config.toml" in names
    for command in ("gupkg.cmd", "gupkg-tui.cmd", "gupkg-manager.cmd"):
        assert f"{tree}/{command}" in names
    assert not [name for name in names if name.endswith(".pyc") or "__pycache__" in name]


def test_the_trees_commands_search_nothing(release_bundle) -> None:
    """gupkg.cmd, gupkg-tui.cmd and gupkg-manager.cmd of the tree never search PATH (design 21.3)."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        for command in ("gupkg.cmd", "gupkg-tui.cmd", "gupkg-manager.cmd"):
            text = archive.read(f"gupkg/{release_bundle.version}/gupkg/{command}").decode("ascii")
            assert "%PATH" not in text and "PATH:" not in text


def test_build_stops_when_the_runtime_archive_does_not_match_the_pinned_digest(tmp_path: Path) -> None:
    """A CPython archive whose SHA-256 differs from the expected one stops the build and writes no ZIP."""
    runtime = tmp_path / "runtime.zip"
    with zipfile.ZipFile(runtime, "w") as archive:
        archive.writestr("python312._pth", "python312.zip\n")
    output = tmp_path / "out.zip"

    result = subprocess.run(
        [sys.executable, str(BUILD), "--skip-deps", "--runtime", str(runtime), "--runtime-sha256", "0" * 64,
         "--commit", "a" * 40, "--output", str(output)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert not output.exists()


def test_build_ships_no_bytecode_and_the_built_tree_verifies(tmp_path: Path, runtime_zip_factory) -> None:
    """A runtime tree that contains __pycache__/.pyc files yields a ZIP with none, and its tree passes the integrity check."""
    runtime = tmp_path / "runtime.zip"
    digest = runtime_zip_factory(
        runtime,
        {
            "Lib/site-packages/dep/__init__.py": "x = 1\n",
            "Lib/site-packages/dep/__pycache__/__init__.cpython-312.pyc": b"\0",
            "Lib/site-packages/stray.pyc": b"\0",
        },
    )
    output = tmp_path / "out.zip"

    subprocess.run(
        [sys.executable, str(BUILD), "--skip-deps", "--runtime", str(runtime),
         "--runtime-sha256", digest,
         "--commit", "a" * 40, "--output", str(output)],
        check=True, capture_output=True,
    )

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        archive.extractall(tmp_path / "unpacked")
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    assert any(name.endswith("dep/__init__.py") for name in names)
    (version,) = [path for path in (tmp_path / "unpacked" / "gupkg").iterdir() if path.name != "_check_updates"]
    assert verify_version_directory(version).ok


def test_planted_bytecode_is_reported_as_extra(installed: Path) -> None:
    """A __pycache__ file in the tree is not exempt: it is listed as an extra file."""
    cache = installed / "gupkg" / "gupkg" / "__pycache__"
    cache.mkdir()
    (cache / "cli.cpython-312.pyc").write_bytes(b"\0")

    report = verify_version_directory(installed)

    assert report.extra == ("gupkg/gupkg/__pycache__/cli.cpython-312.pyc",)


def test_manifest_with_a_duplicate_path_is_rejected(installed: Path) -> None:
    """A manifest that lists one path twice is malformed, so it cannot hide a second digest."""
    from gupkg.integrity import ManifestError, read_manifest

    manifest = installed / "gupkg" / "MANIFEST.sha256"
    first = manifest.read_text(encoding="utf-8").splitlines()[0]
    manifest.write_text(first + "\n" + first + "\n", encoding="utf-8", newline="\n")

    with pytest.raises(ManifestError, match="duplicate"):
        read_manifest(manifest)


def test_untouched_version_directory_matches_its_manifest(installed: Path) -> None:
    """A freshly unpacked release has no modified, missing or extra file."""
    assert verify_version_directory(installed).ok


def test_files_outside_the_tree_are_not_covered_and_an_app_copy_is_checked_like_the_tree(installed: Path, tmp_path: Path) -> None:
    """pkg.toml, pkg.local, the launchers and App (install state) never make the tree look edited; a copy of the tree as App is verified against the same manifest."""
    (installed / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
    (installed / "App").mkdir()
    (installed / "App" / "RELEASE.txt").write_text("marker", encoding="utf-8")
    (installed / "install.cmd").write_text("changed", encoding="utf-8")
    assert verify_version_directory(installed).ok

    import shutil

    shutil.copytree(installed / "gupkg", installed / "App2")
    assert verify_tree(installed / "App2", installed / "gupkg" / "MANIFEST.sha256").ok
    (installed / "App2" / "gupkg" / "cli.py").write_text("# edited\n", encoding="utf-8")
    assert verify_tree(installed / "App2", installed / "gupkg" / "MANIFEST.sha256").modified == ("gupkg/gupkg/cli.py",)


def test_modified_missing_and_extra_files_are_listed(installed: Path) -> None:
    """One edited, one deleted and one added file are each reported under their own heading."""
    (installed / "gupkg" / "gupkg" / "cli.py").write_text("# edited\n", encoding="utf-8")
    (installed / "gupkg" / "gupkg" / "tls.py").unlink()
    (installed / "gupkg" / "gupkg" / "planted.py").write_text("x", encoding="utf-8")

    report = verify_version_directory(installed)

    assert not report.ok
    assert report.modified == ("gupkg/gupkg/cli.py",)
    assert report.missing == ("gupkg/gupkg/tls.py",)
    assert report.extra == ("gupkg/gupkg/planted.py",)


def test_version_directory_without_a_manifest_is_reported(installed: Path) -> None:
    """A version directory that has no MANIFEST.sha256 is not healthy, and says so."""
    (installed / "gupkg" / "MANIFEST.sha256").unlink()

    report = verify_version_directory(installed)

    assert not report.ok and not report.manifest_found
    assert "MANIFEST.sha256 is missing" in report.problems()[0]


def _elevated() -> bool:
    """Return whether this test process runs with an administrator token."""
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())


@pytest.mark.skipif(os.name != "nt" or _elevated(), reason="runs the bundle's launcher; an elevated run demands admin-only folders")
def test_self_status_reports_the_edited_file(installed: Path) -> None:
    """`gupkg self status` is healthy on the bundle, and names the file after one is edited."""
    environment = os.environ.copy()
    command = [str(installed / "gupkg" / "gupkg.cmd"), "self", "status"]

    healthy = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)
    assert verify_version_directory(installed).ok, "running gupkg with -B must not write bytecode into the tree"
    (installed / "gupkg" / "gupkg" / "README.md").write_text("# edited\n", encoding="utf-8")
    damaged = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=120)

    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert damaged.returncode == 3, damaged.stdout + damaged.stderr
    assert "modified: gupkg/gupkg/README.md" in damaged.stdout + damaged.stderr
