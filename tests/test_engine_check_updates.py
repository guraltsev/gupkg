"""Cover ``check-updates``: who runs it, what it writes, what it never touches, and how a refused download is reported.

Behaviour covered: an administrator on a system package writes and populates the version in the system root; a normal shell
on a user package with ``--no-download`` writes only ``pkg.toml`` and the support files; the version directory is rendered from the tracker without any update-check code and with the pinned digest the
tracker discovered; a download the checksum rules refuse leaves the version written without ``App`` and
``update`` can then populate it; a later check finishes a version left without ``App``, or fails again, or warns when it may not
download; the tracker of a followed registry is copied in; a bundled install carries its gupkg forward.

Real: the engine, the json tracker provider, the catalog and real folders. Mocked: ``FakeSystem`` (URL fixtures, elevation flag).
Out of scope: the other providers, the command line.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gupkg.engine.receipts import read_receipt
from gupkg.engine.stateio import read_track_state
from gupkg.launchers import launcher_text
from gupkg.report.events import Warning, use_reporter
from gupkg.model.checksum import parse_skip_flag
from tests.support.engine import BASE_URL, World, current

pytestmark = pytest.mark.unit

# Table that declares an environment variable pointing at ``App``.
ENV_ONLY = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'


def installed(world: World, mode: str = "user", **options) -> World:
    """Publish tool 1.0.0 with a tracker in *mode*'s root and install it."""
    world.publish("tool", "1.0.0", mode=mode, tables=ENV_ONLY)
    world.tracker("tool", mode=mode, tables=ENV_ONLY)
    assert world.one("install", "tool", **options).ok
    return world


def test_an_administrator_checking_a_system_package_writes_and_populates_the_version(elevated_world: World) -> None:
    """Elevated, on a system package: the new version directory, populated, appears in the system root; the package stays on its old version."""
    installed(elevated_world, "system")
    elevated_world.announce("tool", "1.1.0")

    result = elevated_world.one("check-updates", "tool")

    package = elevated_world.system_root / "tool"
    assert result.ok and result.status == "new-version" and result.version == "1.1.0" and result.mode == "system"
    assert (package / "1.1.0" / "App" / "app.exe").is_file() and Path(os.path.realpath(package / "current")).name == "1.0.0"


def test_a_normal_shell_with_no_download_writes_only_the_manifest_and_support_files(world: World) -> None:
    """Non-elevated, on a user package, --no-download: pkg.toml, support files and the version's own launchers are written; nothing is downloaded."""
    installed(world)
    (world.user / "tool" / "_check_updates" / "icons").mkdir()
    (world.user / "tool" / "_check_updates" / "icons" / "tool.ico").write_text("icon", encoding="utf-8")
    world.announce("tool", "1.1.0")
    world.fake.fetch_log.clear()

    result = world.one("check-updates", "tool", no_download=True)

    version = world.user / "tool" / "1.1.0"
    assert result.ok and result.details["populated"] is False
    assert sorted(item.name for item in version.iterdir()) == ["icons", "install.cmd", "manage.cmd", "pkg.toml"]
    assert f"{BASE_URL}/tool-1.1.0.zip" not in world.fake.fetch_log


def test_the_new_version_directory_is_pinned_and_carries_no_update_check_code(world: World) -> None:
    """The version is rendered from the tracker: concrete URL, discovered digest, no [track], no track hook, support files kept."""
    # Arrange: an installed package whose tracker has update-check code, and a newer version at the vendor.
    installed(world)
    local = world.user / "tool" / "_check_updates" / "pkg.local"
    local.mkdir()
    (local / "track.py").write_text("PKG_MODULE_API = 2\ndef track(ctx):\n    return None\n", encoding="utf-8")
    (local / "helper.py").write_text("PKG_MODULE_API = 2\nVALUE = 1\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    # Act: check for updates without downloading.
    result = world.one("check-updates", "tool", no_download=True)

    # Assert: the new version is pinned and carries only the helper, not the update-check code.
    text = (world.user / "tool" / "1.1.0" / "pkg.toml").read_text()
    assert result.ok and "[track]" not in text and "${" not in text
    assert f'download_url = "{BASE_URL}/tool-1.1.0.zip"' in text and 'sha256_from = "vendor-sums"' in text
    assert sorted(item.name for item in (world.user / "tool" / "1.1.0" / "pkg.local").iterdir()) == ["helper.py"]


def test_a_download_the_checksum_rules_refuse_leaves_the_version_without_app(world: World) -> None:
    """With no digest and no --skip-checksum the directory stays written without App and the refusal is reported; update can populate it with the flag."""
    # Arrange: an installed package whose tracker has no checksum source, and a newer version without a sidecar.
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables=ENV_ONLY, checksum=False)
    assert world.one("install", "tool").ok
    world.announce("tool", "1.1.0", sidecar=False)

    # Act: check for updates.
    result = world.one("check-updates", "tool")

    # Assert: the checksum rules refuse the download and the version stays without ``App``.
    package = world.user / "tool"
    assert not result.ok and result.exit_code == 3 and result.version == "1.1.0"
    assert result.error["code"] == "checksum-missing" and result.error["skippable"] is True
    assert (package / "1.1.0" / "pkg.toml").is_file() and not (package / "1.1.0" / "App").exists()
    assert read_track_state(package)["refused"] == "checksum-missing"

    # Update is refused the same way, and skipping the checksum allows it.
    refused = world.one("update", "tool")
    assert not refused.ok and refused.error["code"] == "checksum-missing" and not (package / "1.1.0" / "App").exists()
    allowed = world.one("update", "tool", skip=parse_skip_flag(""))
    assert allowed.ok and (package / "1.1.0" / "App" / "app.exe").is_file()
    assert read_receipt(package, "1.1.0").data["checksum"]["decision"] == "skipped-cli"


def test_check_updates_with_skip_checksum_populates_a_version_that_has_no_hash(world: World) -> None:
    """With the flag the unhashed https download is populated and the receipt records the skip; without it the checksum rules refuse the download."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables=ENV_ONLY, checksum=False)
    world.one("install", "tool")
    world.announce("tool", "1.1.0", sidecar=False)

    result = world.one("check-updates", "tool", skip=parse_skip_flag("user-approved"))

    package = world.user / "tool"
    assert result.ok and (package / "1.1.0" / "App" / "app.exe").is_file()
    assert read_receipt(package, "1.1.0").data["checksum"]["decision"] == "user-approved"
    assert "sha256" not in (package / "1.1.0" / "pkg.toml").read_text()


def test_check_updates_reports_nothing_new_when_the_version_is_known(world: World) -> None:
    """A tracker result that equals the active version writes nothing and says so."""
    installed(world)
    world.announce("tool", "1.0.0")
    before = sorted(item.name for item in (world.user / "tool").iterdir())

    result = world.one("check-updates", "tool")

    assert result.ok and result.status == "no-new-version"
    assert sorted(item.name for item in (world.user / "tool").iterdir()) == before


def test_check_updates_of_a_package_that_is_not_local_is_an_error_with_a_hint(world: World) -> None:
    """A registry-only name has nothing to check: the error says to install it."""
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}}}})

    result = world.one("check-updates", "tool")

    assert not result.ok and "install" in result.error["message"]


def test_the_registrys_tracker_is_copied_in_and_run(tmp_path: Path, world: World) -> None:
    """A linked package with no tracker gets the registry's tracker copied in, and the copy's hash is recorded in the install record."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    tracker_text = world.tracker("tool", root=tmp_path / "scratch").joinpath("pkg.toml").read_text(encoding="utf-8")
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}}, "tracker": tracker_text}})
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", check_upstream=True)

    package = world.user / "tool"
    assert result.ok and result.version == "1.1.0" and (package / "_check_updates" / "pkg.toml").is_file()


def test_a_bundled_install_carries_its_own_gupkg_into_the_new_version(world: World) -> None:
    """In a root with no gupkg-location the package's bundled gupkg is copied into the new directory, next to launchers written fresh."""
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


def test_every_new_version_directory_gets_the_launchers_also_in_a_root_with_a_gupkg_location(world: World) -> None:
    """With an installed gupkg in the root (a gupkg-location) the new version still carries its own install.cmd and manage.cmd, and no bundled gupkg."""
    installed(world)
    (world.user / ".gupkg").mkdir(exist_ok=True)
    (world.user / ".gupkg" / "gupkg-location").write_text("gupkg\\current\\App\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", no_download=True)

    new = world.user / "tool" / "1.1.0"
    assert result.ok and not (new / "gupkg").exists()
    assert (new / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (new / "manage.cmd").read_bytes() == launcher_text("tui").encode("ascii")


def test_the_trackers_launcher_is_never_copied_into_a_version_directory(world: World) -> None:
    """`check-updates.cmd` of `_check_updates` stays there; the new version has install.cmd and manage.cmd instead."""
    installed(world)
    (world.user / "tool" / "_check_updates" / "check-updates.cmd").write_text("@echo off\n", encoding="utf-8")
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool", no_download=True)

    assert result.ok and not (world.user / "tool" / "1.1.0" / "check-updates.cmd").exists()


def test_a_waived_optional_checksum_source_is_shown_returned_and_recorded(world: World) -> None:
    """`[source.checksum] optional = true` with nothing behind it: the version is written unpinned and the note reaches the events, the result and the receipt."""
    # Arrange: an installed package whose tracker may waive its checksum source, and a newer version without one.
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables="optional = true\n" + ENV_ONLY)
    assert world.one("install", "tool").ok
    world.announce("tool", "1.1.0", sidecar=False)
    seen: list[str] = []

    # A reporter that keeps the warnings.
    class Collect:
        """Keep the warnings."""

        def handle(self, event: object) -> None:
            """Keep the text of one warning."""
            if isinstance(event, Warning):
                seen.append(event.text)

    # Act: check for updates without downloading.
    with use_reporter(Collect()):
        result = world.one("check-updates", "tool", no_download=True)

    # Assert: the waiver is shown, returned and recorded, and no App was written.
    text = (world.user / "tool" / "1.1.0" / "pkg.toml").read_text(encoding="utf-8")
    assert result.ok and result.version == "1.1.0" and "sha256" not in text and "[source.checksum]" not in text
    assert len(result.warnings) == 1 and "optional checksum source" in result.warnings[0] and result.details["notes"] == list(result.warnings)
    assert any("optional checksum source" in line for line in seen)
    assert not (world.user / "tool" / "1.1.0" / "App.info").exists()


@pytest.mark.parametrize("version", ["con", "NUL.txt", "1.0 ", "..", "a%b"])
def test_a_discovered_version_that_cannot_name_a_directory_writes_nothing(world: World, version: str) -> None:
    """A tracker that finds a device name or an unsafe string as a version is refused before any directory is made."""
    installed(world)
    world.fake.add_url(f"{BASE_URL}/tool-latest.json", f'{{"version": "{version}"}}')
    before = sorted(item.name for item in (world.user / "tool").iterdir())

    result = world.one("check-updates", "tool")

    assert not result.ok and sorted(item.name for item in (world.user / "tool").iterdir()) == before


def tracker_only_package(world: World, *registry_versions: str) -> None:
    """Make a package root that holds only its tracker and follows a registry entry pinning *registry_versions*."""
    world.tracker("tool", tables=ENV_ONLY)
    if registry_versions:
        world.registry({"tool": {"versions": {version: {"app.exe": "x"} for version in registry_versions}}})


def version_folders(world: World) -> list[str]:
    """Return the names of the version directories in the package root."""
    return sorted(item.name for item in (world.user / "tool").iterdir() if item.name not in ("_check_updates", ".gupkg"))


def test_an_empty_package_gets_the_registrys_version_written_and_populated(world: World) -> None:
    """By default the registry is the source: its pinned version is copied in and populated, and upstream is not contacted."""
    tracker_only_package(world, "1.0.0")

    result = world.one("check-updates", "tool")

    assert result.ok and result.status == "new-version" and result.version == "1.0.0" and result.details["source"] == "registry"
    assert (world.user / "tool" / "1.0.0" / "App" / "app.exe").is_file()
    assert f"{BASE_URL}/tool-latest.json" not in world.fake.fetch_log


def test_a_version_prepared_from_the_registry_can_then_be_installed(world: World) -> None:
    """A version that check-updates copied from the registry is an ordinary local version: install activates it."""
    tracker_only_package(world, "1.0.0")
    assert world.one("check-updates", "tool").ok

    assert world.one("install", "tool").ok and current(world.user / "tool") == "1.0.0"


def test_a_registry_version_that_is_not_newer_than_the_local_ones_writes_nothing(world: World) -> None:
    """The registry only offers what is newer than every local version directory."""
    installed(world)
    world.registry({"tool": {"versions": {"0.9.0": {"app.exe": "x"}}}})

    result = world.one("check-updates", "tool")

    assert result.ok and result.status == "no-new-version" and version_folders(world) == ["1.0.0", "current"]


@pytest.mark.parametrize(
    ("switches", "answer", "expected"),
    [
        pytest.param({"no_input": True}, True, "1.0.0", id="no-input-takes-the-registry-without-asking"),
        pytest.param({"yes": True}, True, "1.0.0", id="yes-takes-the-registry-without-asking"),
        pytest.param({}, False, "1.0.0", id="declined-keeps-the-registry"),
        pytest.param({}, True, "2.0.0", id="accepted-takes-upstream"),
    ],
)
def test_the_registry_wins_a_disagreement_unless_the_user_chooses_upstream(
    world: World, switches: dict[str, bool], answer: bool, expected: str
) -> None:
    """With both sources on and different versions the user is asked; without an answer the registry is authoritative."""
    tracker_only_package(world, "1.0.0")
    world.announce("tool", "2.0.0")
    world.engine.confirm = lambda question: answer
    for name, value in switches.items():
        setattr(world.engine, name, value)

    result = world.one("check-updates", "tool", check_upstream=True)

    assert result.ok and result.version == expected and version_folders(world) == [expected]


def test_both_sources_offering_one_version_use_the_registrys_copy_without_asking(world: World) -> None:
    """When registry and upstream agree there is nothing to decide, and the registry's pinned copy is the one written."""
    tracker_only_package(world, "1.0.0")
    world.announce("tool", "1.0.0", members={"app.exe": "x"})
    world.engine.confirm = lambda question: pytest.fail("nothing to decide, so nothing is asked")

    result = world.one("check-updates", "tool", check_upstream=True)

    assert result.ok and result.details["source"] == "registry"


@pytest.mark.parametrize("options", [
    pytest.param({"check_registry": False}, id="registry-off-leaves-upstream"),
    pytest.param({"check_registry": False, "check_upstream": True}, id="upstream-only"),
])
def test_with_the_registry_off_a_version_it_also_pins_is_still_written_from_upstream(world: World, options: dict[str, bool]) -> None:
    """Upstream is compared with the local versions only, so a version the registry pins does not hide it."""
    tracker_only_package(world, "1.0.0")
    world.announce("tool", "1.0.0")

    result = world.one("check-updates", "tool", **options)

    assert result.ok and result.status == "new-version" and result.details["source"] == "upstream"
    assert (world.user / "tool" / "1.0.0" / "App" / "app.exe").is_file()


def test_check_only_reports_what_both_sources_offer_and_writes_no_version(world: World) -> None:
    """--check-only tells what is on offer from each source, asks nothing and leaves the package root without a version."""
    tracker_only_package(world, "1.0.0")
    world.announce("tool", "2.0.0")
    world.engine.confirm = lambda question: pytest.fail("a check only asks nothing")

    result = world.one("check-updates", "tool", check_upstream=True, check_only=True)

    assert result.ok and result.status == "update-available"
    assert result.details["registry"] == "1.0.0" and result.details["upstream"] == "2.0.0"
    assert version_folders(world) == []


def test_a_package_left_with_no_version_is_a_failure_that_names_the_way_out(world: World) -> None:
    """A check that ends with an empty package root never reports success: the error says how to get a version."""
    tracker_only_package(world)
    world.announce("tool", "1.0.0")

    result = world.one("check-updates", "tool", check_upstream=False)

    assert not result.ok and result.error["code"] == "no-version-known" and "--check-upstream" in result.error["message"]
    assert version_folders(world) == []


def test_check_only_on_an_empty_package_is_not_a_failure(world: World) -> None:
    """Only --check-only may leave a package root without a version: it reports and succeeds."""
    tracker_only_package(world)
    world.announce("tool", "1.0.0")

    result = world.one("check-updates", "tool", check_upstream=False, check_only=True)

    assert result.ok and result.status == "no-new-version" and version_folders(world) == []


def a_version_that_lost_its_download(world: World) -> str:
    """Leave version 1.0.0 written without its App because the download failed; return the address that failed, served again."""
    tracker_only_package(world, "1.0.0")
    url = f"{BASE_URL}/tool-1.0.0.zip"
    served = world.fake.urls.pop(url)
    failed = world.one("check-updates", "tool")
    assert not failed.ok and not (world.user / "tool" / "1.0.0" / "App").exists()
    world.fake.urls[url] = served
    return url


def test_a_rerun_after_a_failed_download_finishes_the_version(world: World) -> None:
    """A version written without its App is not "nothing newer": the next check downloads it and says so."""
    a_version_that_lost_its_download(world)

    result = world.one("check-updates", "tool")

    assert result.ok and result.status == "new-version" and result.version == "1.0.0" and result.details["populated"] is True
    assert result.details["source"] == "local" and (world.user / "tool" / "1.0.0" / "App" / "app.exe").is_file()
    assert read_receipt(world.user / "tool", "1.0.0") is not None


def test_a_rerun_whose_download_fails_again_is_a_failure(world: World) -> None:
    """A check that cannot finish the version fails each time; it never reports that all is well."""
    url = a_version_that_lost_its_download(world)
    del world.fake.urls[url]

    result = world.one("check-updates", "tool")

    assert not result.ok and result.exit_code != 0 and not (world.user / "tool" / "1.0.0" / "App").exists()


def test_a_rerun_after_a_refused_checksum_is_refused_again_until_the_user_approves(world: World) -> None:
    """The manager re-runs a refused version with an approval: that run must populate the version it was refused."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.tracker("tool", tables=ENV_ONLY, checksum=False)
    assert world.one("install", "tool").ok
    world.announce("tool", "1.1.0", sidecar=False)
    assert not world.one("check-updates", "tool").ok

    refused = world.one("check-updates", "tool")
    approved = world.one("check-updates", "tool", skip=parse_skip_flag(""))

    assert not refused.ok and refused.exit_code == 3 and refused.error["code"] == "checksum-missing"
    assert approved.ok and (world.user / "tool" / "1.1.0" / "App" / "app.exe").is_file()


def test_a_version_prepared_with_no_download_is_finished_by_a_plain_check(world: World) -> None:
    """--no-download leaves the App for later, and the next plain check is that later."""
    tracker_only_package(world, "1.0.0")
    assert world.one("check-updates", "tool", no_download=True).ok

    result = world.one("check-updates", "tool")

    assert result.ok and (world.user / "tool" / "1.0.0" / "App" / "app.exe").is_file()


@pytest.mark.parametrize("options", [{"no_download": True}, {"check_only": True}], ids=["no-download", "check-only"])
def test_a_run_that_may_not_download_says_the_newest_version_has_no_app(world: World, options: dict[str, bool]) -> None:
    """A run that must not download leaves the version alone, and still says what is wrong with it."""
    a_version_that_lost_its_download(world)

    result = world.one("check-updates", "tool", **options)

    assert result.ok and result.status == "no-new-version" and any("1.0.0 has no App" in text for text in result.warnings)
    assert not (world.user / "tool" / "1.0.0" / "App").exists()


def test_a_version_that_ships_its_app_is_not_downloaded_again(world: World) -> None:
    """A version with nothing to populate it from is left alone: there is no download to retry."""
    world.publish_shipped("tool", "1.0.0")
    world.tracker("tool")
    world.announce("tool", "1.0.0")
    shutil.rmtree(world.user / "tool" / "1.0.0" / "App")

    result = world.one("check-updates", "tool")

    assert result.ok and result.status == "no-new-version" and not result.warnings
