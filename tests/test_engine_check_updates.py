"""Cover ``check-updates``: who runs it, what it writes, what it never touches, and how a refused download is reported.

Behaviour covered: an administrator on a system package writes and populates the version in the system root; a normal shell
on a user package with ``--no-download`` writes only ``pkg.toml`` and the support files; the version directory is rendered from the tracker without any update-check code and with the pinned digest the
tracker discovered; a download the checksum rules refuse leaves the version written without ``App`` (decision D46) and
``update`` can then populate it; the tracker of a followed registry is copied in; a bundled install carries its gupkg forward.

Real: the engine, the json tracker provider, the catalog and real folders. Mocked: ``FakeSystem`` (URL fixtures, elevation flag).
Out of scope: the other providers (issue 026), the command line (issue 029).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gupkg.engine.receipts import read_receipt
from gupkg.engine.stateio import read_track_state
from gupkg.launchers import launcher_text
from gupkg.report.events import Warning, use_reporter
from gupkg.model.checksum import parse_skip_flag
from tests.engine_support import BASE_URL, World, pin_no_ambient_elevation  # noqa: F401

ENV_ONLY = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'


def installed(world: World, mode: str = "user", **options) -> World:
    """Publish tool 1.0.0 with a tracker in *mode*'s root and install it."""
    world.publish("tool", "1.0.0", mode=mode, tables=ENV_ONLY)
    world.tracker("tool", mode=mode, tables=ENV_ONLY)
    assert world.one("install", "tool", **options).ok
    return world


def test_an_administrator_checking_a_system_package_writes_and_populates_the_version(tmp_path: Path) -> None:
    """Elevated, on a system package: the new version directory, populated, appears in the system root; the package stays on its old version."""
    world = World(tmp_path, elevated=True)
    installed(world, "system")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool")

    package = world.system_root / "tool"
    assert result.ok and result.status == "new-version" and result.version == "1.1.0" and result.mode == "system"
    assert (package / "1.1.0" / "App" / "app.exe").is_file() and Path(os.path.realpath(package / "current")).name == "1.0.0"


def test_a_normal_shell_with_no_download_writes_only_the_manifest_and_support_files(tmp_path: Path) -> None:
    """Non-elevated, on a user package, --no-download: pkg.toml, support files and the version's own launchers are written; nothing is downloaded."""
    world = World(tmp_path)
    installed(world)
    (world.user / "tool" / "_check_updates" / "icons").mkdir()
    (world.user / "tool" / "_check_updates" / "icons" / "tool.ico").write_text("icon", encoding="utf-8")
    world.announce("tool", "1.1.0")
    world.fake.fetch_log.clear()

    result = world.one("check-updates", "tool", no_download=True)

    version = world.user / "tool" / "1.1.0"
    assert result.ok and result.details["populated"] is False
    assert sorted(item.name for item in version.iterdir()) == ["gupkg-tui.cmd", "icons", "install.cmd", "pkg.toml"]
    assert f"{BASE_URL}/tool-1.1.0.zip" not in world.fake.fetch_log


def test_the_new_version_directory_is_pinned_and_carries_no_update_check_code(tmp_path: Path) -> None:
    """The version is rendered from the tracker: concrete URL, discovered digest, no [track], no track hook, support files kept."""
    world = World(tmp_path)
    installed(world)
    local = world.user / "tool" / "_check_updates" / "pkg.local"
    local.mkdir()
    (local / "track.py").write_text("PKG_MODULE_API = 2\ndef track(ctx):\n    return None\n", encoding="utf-8")
    (local / "helper.py").write_text("PKG_MODULE_API = 2\nVALUE = 1\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", no_download=True)

    text = (world.user / "tool" / "1.1.0" / "pkg.toml").read_text()
    assert result.ok and "[track]" not in text and "${" not in text
    assert f'download_url = "{BASE_URL}/tool-1.1.0.zip"' in text and 'sha256_from = "vendor-sums"' in text
    assert sorted(item.name for item in (world.user / "tool" / "1.1.0" / "pkg.local").iterdir()) == ["helper.py"]


def test_a_download_the_checksum_rules_refuse_leaves_the_version_without_app(tmp_path: Path) -> None:
    """With no digest and no --skip-checksum the directory stays written without App and the refusal is reported (D46); update can populate it with the flag."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables=ENV_ONLY, checksum=False)
    assert world.one("install", "tool").ok
    world.announce("tool", "1.1.0", sidecar=False)

    result = world.one("check-updates", "tool")

    package = world.user / "tool"
    assert not result.ok and result.exit_code == 3 and result.version == "1.1.0"
    assert result.error["code"] == "checksum-missing" and result.error["skippable"] is True
    assert (package / "1.1.0" / "pkg.toml").is_file() and not (package / "1.1.0" / "App").exists()
    assert read_track_state(package)["refused"] == "checksum-missing"

    refused = world.one("update", "tool")
    assert not refused.ok and refused.error["code"] == "checksum-missing" and not (package / "1.1.0" / "App").exists()
    allowed = world.one("update", "tool", skip=parse_skip_flag(""))
    assert allowed.ok and (package / "1.1.0" / "App" / "app.exe").is_file()
    assert read_receipt(package, "1.1.0").data["checksum"]["decision"] == "skipped-cli"


def test_check_updates_with_skip_checksum_populates_a_version_that_has_no_hash(tmp_path: Path) -> None:
    """With the flag the unhashed https download is populated and the receipt records the skip; without it D46 applies."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables=ENV_ONLY, checksum=False)
    world.one("install", "tool")
    world.announce("tool", "1.1.0", sidecar=False)

    result = world.one("check-updates", "tool", skip=parse_skip_flag("user-approved"))

    package = world.user / "tool"
    assert result.ok and (package / "1.1.0" / "App" / "app.exe").is_file()
    assert read_receipt(package, "1.1.0").data["checksum"]["decision"] == "user-approved"
    assert "sha256" not in (package / "1.1.0" / "pkg.toml").read_text()


def test_check_updates_reports_nothing_new_when_the_version_is_known(tmp_path: Path) -> None:
    """A tracker result that equals the active version writes nothing and says so."""
    world = World(tmp_path)
    installed(world)
    world.announce("tool", "1.0.0")
    before = sorted(item.name for item in (world.user / "tool").iterdir())

    result = world.one("check-updates", "tool")

    assert result.ok and result.status == "no-new-version"
    assert sorted(item.name for item in (world.user / "tool").iterdir()) == before


def test_check_updates_of_a_package_that_is_not_local_is_an_error_with_a_hint(tmp_path: Path) -> None:
    """A registry-only name has nothing to check: the error says to install it."""
    world = World(tmp_path)
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}}}})

    result = world.one("check-updates", "tool")

    assert not result.ok and "install" in result.error["message"]


def test_the_registrys_tracker_is_copied_in_and_run(tmp_path: Path) -> None:
    """A linked package with no tracker gets the registry's tracker copied in, and the copy's hash is recorded in the install record."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    tracker_text = world.tracker("tool", root=tmp_path / "scratch").joinpath("pkg.toml").read_text(encoding="utf-8")
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}}, "tracker": tracker_text}})
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool")

    package = world.user / "tool"
    assert result.ok and result.version == "1.1.0" and (package / "_check_updates" / "pkg.toml").is_file()


def test_a_bundled_install_carries_its_own_gupkg_into_the_new_version(tmp_path: Path) -> None:
    """In a root with no forwarder the package's bundled gupkg is copied into the new directory, next to launchers written fresh (decision D32)."""
    world = World(tmp_path)
    installed(world)
    version = world.user / "tool" / "1.0.0"
    (version / "gupkg").mkdir()
    (version / "gupkg" / "gupkg.cmd").write_text("@echo off\n", encoding="utf-8")
    (version / "install.cmd").write_text("@echo off\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", no_download=True)

    new = world.user / "tool" / "1.1.0"
    assert result.ok and (new / "gupkg" / "gupkg.cmd").read_text(encoding="utf-8") == "@echo off\n"
    assert (new / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")


def test_every_new_version_directory_gets_the_launchers_also_in_a_root_with_a_forwarder(tmp_path: Path) -> None:
    """With an installed gupkg in the root (a forwarder) the new version still carries its own install.cmd and gupkg-tui.cmd, and no bundled gupkg."""
    world = World(tmp_path)
    installed(world)
    (world.user / ".gupkg").mkdir(exist_ok=True)
    (world.user / ".gupkg" / "gupkg.cmd").write_text("@echo off\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", no_download=True)

    new = world.user / "tool" / "1.1.0"
    assert result.ok and not (new / "gupkg").exists()
    assert (new / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (new / "gupkg-tui.cmd").read_bytes() == launcher_text("tui").encode("ascii")


def test_the_trackers_launcher_is_never_copied_into_a_version_directory(tmp_path: Path) -> None:
    """`check-updates.cmd` of `_check_updates` stays there; the new version has install.cmd and gupkg-tui.cmd instead."""
    world = World(tmp_path)
    installed(world)
    (world.user / "tool" / "_check_updates" / "check-updates.cmd").write_text("@echo off\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", no_download=True)

    assert result.ok and not (world.user / "tool" / "1.1.0" / "check-updates.cmd").exists()


def test_a_waived_optional_checksum_source_is_shown_returned_and_recorded(tmp_path: Path) -> None:
    """`[source.checksum] optional = true` with nothing behind it: the version is written unpinned and the note reaches the events, the result and the receipt."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables="optional = true\n" + ENV_ONLY)
    assert world.one("install", "tool").ok
    world.announce("tool", "1.1.0", sidecar=False)
    seen: list[str] = []

    class Collect:
        """Keep the warnings."""

        def handle(self, event: object) -> None:
            if isinstance(event, Warning):
                seen.append(event.text)

    with use_reporter(Collect()):
        result = world.one("check-updates", "tool", no_download=True)

    text = (world.user / "tool" / "1.1.0" / "pkg.toml").read_text(encoding="utf-8")
    assert result.ok and result.version == "1.1.0" and "sha256" not in text and "[source.checksum]" not in text
    assert len(result.warnings) == 1 and "optional checksum source" in result.warnings[0] and result.details["notes"] == list(result.warnings)
    assert any("optional checksum source" in line for line in seen)
    assert not (world.user / "tool" / "1.1.0" / "App.info").exists()


@pytest.mark.parametrize("version", ["con", "NUL.txt", "1.0 ", "..", "a%b"])
def test_a_discovered_version_that_cannot_name_a_directory_writes_nothing(tmp_path: Path, version: str) -> None:
    """A tracker that finds a device name or an unsafe string as a version is refused before any directory is made."""
    world = World(tmp_path)
    installed(world)
    world.fake.add_url(f"{BASE_URL}/tool-latest.json", f'{{"version": "{version}"}}')
    before = sorted(item.name for item in (world.user / "tool").iterdir())

    result = world.one("check-updates", "tool")

    assert not result.ok and sorted(item.name for item in (world.user / "tool").iterdir()) == before
