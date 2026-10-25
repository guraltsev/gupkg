"""Cover the lifecycle of one package end to end on a ``FakeSystem``: install, repair, check-updates, update, switch, uninstall, cleanup.

Behaviour covered: the resulting machine state of every operation (``current``, ``App``, shortcuts, shims, environment values,
``PATH``, receipt, install record), that install never runs a tracker, that nothing runs ``cleanup`` unless asked, that a
version which drops a shortcut and a shim removes both on update, that a failed population leaves the previous version
active and the machine unchanged, and that only the URLs written in ``pkg.toml`` are fetched.

Real: the engine, the catalog, the planner, the executor, real folders and junctions below a temporary directory, real zip
archives and the real HTTP client policy. Mocked: ``FakeSystem`` (registry, Start Menu, ``PATH``, URL fixtures).
Out of scope: the command line (issue 029), the managers (issue 030), real Windows.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from gupkg.catalog.record import read_install_record
from gupkg.engine import lifecycle
from gupkg.engine.receipts import read_receipt
from gupkg.model.plan import INSTALL_PHASES, Request
from gupkg.report.events import Error, PackageEnd, PhaseEnd, use_reporter
from gupkg.system.port import Scope
from tests.engine_support import BASE_URL, World, pin_no_ambient_elevation, pkg_text  # noqa: F401

FULL = """
[[shortcut]]
name = "Tool"
target = "$App\\\\app.exe"

[[shim]]
name = "tool"
target = "$App\\\\app.exe"

[[env]]
name = "TOOL_HOME"
value = "$App"

[[path]]
value = "$App"
"""

ENV_ONLY = """
[[env]]
name = "TOOL_HOME"
value = "$App"
"""


def bin_dir(world: World) -> Path:
    """Return the fake machine's user ``bin`` folder."""
    return world.fake.scope_folders(Scope.USER).bin_dir


def shims(world: World) -> list[str]:
    """Return the command shims in the user ``bin`` folder (the runtime companion files are not shims)."""
    folder = bin_dir(world)
    return sorted(item.name for item in folder.glob("*")) if folder.is_dir() and any(folder.glob("*.exe")) else []


def launchers(world: World) -> list[str]:
    """Return the names of the launcher ``.exe`` files in the user ``bin`` folder."""
    folder = bin_dir(world)
    return sorted(item.name for item in folder.glob("*.exe"))


def current_version(package: Path) -> str:
    """Return the version that ``<package>/current`` points at."""
    return Path(os.path.realpath(package / "current")).name


def user_path(world: World) -> list[str]:
    """Return the user ``PATH`` entries of the fake machine."""
    return [item for item in world.fake.env_values(Scope.USER).get("Path").value.split(";") if item]


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root, a system root and an engine."""
    return World(tmp_path)


def test_install_creates_the_whole_machine_state(world: World) -> None:
    """Installing a package populates its App, links `current`, applies every effect once and writes the receipt and the record."""
    world.publish("tool", "1.0.0", tables=FULL)

    result = world.one("install", "tool")

    package = world.user / "tool"
    assert result.ok and result.status == "installed" and (result.version, result.mode) == ("1.0.0", "user")
    assert current_version(package) == "1.0.0" and (package / "current" / "App" / "app.exe").is_file()
    assert [path.stem for path in world.fake.shortcuts()] == ["Tool"]
    assert launchers(world) == ["tool.exe"] and (bin_dir(world) / "tool.config.toml").is_file()
    assert world.fake.env_values(Scope.USER)["TOOL_HOME"].value == str(package / "current" / "App")
    assert str(bin_dir(world)) in user_path(world) and str(package / "current" / "App") in user_path(world)
    assert world.fake.broadcasts == 1

    # The receipt and the install record are written last and say what happened.
    record = read_install_record(package).record
    assert record.mode == "user" and sorted(effect.kind for effect in record.effects) == ["env", "path", "path", "shim", "shortcut"]
    receipt = read_receipt(package, "1.0.0").data
    assert receipt["checksum"]["decision"] == "verified" and receipt["run"]["operation"] == "install"
    assert receipt["app"]["count"] == 1 and receipt["app"]["files"] == {"app.exe": hashlib.sha256(b"tool 1.0.0\n").hexdigest()}


def test_install_again_is_a_repair_that_downloads_nothing(world: World) -> None:
    """A second install of an installed package re-applies nothing that is in place, fetches nothing and is reported as a repair."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.fake.fetch_log.clear()
    broadcasts = world.fake.broadcasts

    result = world.one("install", "tool")

    assert result.ok and result.status == "repaired" and world.fake.fetch_log == []
    assert world.fake.broadcasts == broadcasts and current_version(world.user / "tool") == "1.0.0"
    assert launchers(world) == ["tool.exe"]


def test_check_updates_prepares_a_version_and_activates_nothing(world: World) -> None:
    """check-updates writes the version directory, downloads and populates it, and leaves `current` and integration unchanged."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.tracker("tool", tables=FULL)
    world.one("install", "tool")
    shortcuts, env, path = dict(world.fake.shortcuts()), dict(world.fake.env_values(Scope.USER)), user_path(world)
    world.announce("tool", "1.1.0")

    result = world.one("check-updates", "tool")

    package = world.user / "tool"
    assert result.ok and result.status == "new-version" and result.version == "1.1.0"
    assert (package / "1.1.0" / "pkg.toml").is_file() and (package / "1.1.0" / "App" / "app.exe").is_file()
    assert current_version(package) == "1.0.0"
    assert (dict(world.fake.shortcuts()), dict(world.fake.env_values(Scope.USER)), user_path(world)) == (shortcuts, env, path)
    assert read_receipt(package, "1.1.0").data["checksum"]["decision"] == "verified"


def test_update_activates_a_prepared_version_without_downloading(world: World) -> None:
    """A version that check-updates populated is activated by update with an empty fetch log."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.tracker("tool", tables=FULL)
    world.one("install", "tool")
    world.announce("tool", "1.1.0")
    world.one("check-updates", "tool")
    world.fake.fetch_log.clear()

    result = world.one("update", "tool")

    assert result.ok and result.status == "updated" and (result.version, result.previous) == ("1.1.0", "1.0.0")
    assert world.fake.fetch_log == [] and current_version(world.user / "tool") == "1.1.0"
    assert read_receipt(world.user / "tool", "1.1.0").data["run"]["activated"] is True


def test_check_updates_without_download_then_update_populates_and_activates(world: World) -> None:
    """check-updates --no-download writes only pkg.toml; update then downloads, populates and activates."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.tracker("tool", tables=FULL)
    world.one("install", "tool")
    world.announce("tool", "1.1.0")
    world.fake.fetch_log.clear()

    checked = world.one("check-updates", "tool", no_download=True)

    package = world.user / "tool"
    assert checked.ok and checked.status == "new-version"
    assert not (package / "1.1.0" / "App").exists() and f"{BASE_URL}/tool-1.1.0.zip" not in world.fake.fetch_log
    updated = world.one("update", "tool")
    assert updated.ok and current_version(package) == "1.1.0" and (package / "1.1.0" / "App" / "app.exe").is_file()
    assert world.fake.fetch_log.count(f"{BASE_URL}/tool-1.1.0.zip") == 1


def test_update_via_the_registry_copies_in_the_newest_pinned_version(world: World) -> None:
    """A newer pinned version of the followed registry is copied into the package root and activated."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}, "1.2.0": {"app.exe": "registry 1.2.0"}}, "tables": FULL}})

    result = world.one("update", "tool")

    package = world.user / "tool"
    assert result.ok and result.version == "1.2.0" and (package / "1.2.0" / "pkg.toml").is_file()
    assert (package / "current" / "App" / "app.exe").read_text() == "registry 1.2.0"
    assert read_receipt(package, "1.2.0").data["origin"]["kind"] == "registry"


def test_switch_back_activates_the_older_version_and_its_effects(world: World) -> None:
    """switch activates another known version, applies its effects and removes the ones it does not declare."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    world.one("update", "tool")
    assert launchers(world) == [] and world.fake.shortcuts() == {}

    result = world.one("switch", "tool", version="1.0.0")

    assert result.ok and result.status == "switched" and (result.version, result.previous) == ("1.0.0", "1.1.0")
    assert current_version(world.user / "tool") == "1.0.0" and launchers(world) == ["tool.exe"]
    assert [path.stem for path in world.fake.shortcuts()] == ["Tool"]


def test_uninstall_removes_every_effect_and_keeps_the_version_directories(world: World) -> None:
    """uninstall removes shortcuts, shims, environment values and PATH entries it added, and `current`; versions stay."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")

    result = world.one("uninstall", "tool")

    package = world.user / "tool"
    assert result.ok and result.status == "uninstalled"
    assert not (package / "current").exists() and (package / "1.0.0" / "App" / "app.exe").is_file()
    assert world.fake.shortcuts() == {} and launchers(world) == []
    assert "TOOL_HOME" not in world.fake.env_values(Scope.USER)
    assert str(package / "current" / "App") not in user_path(world)
    assert read_install_record(package).record.effects == ()


def test_uninstall_purge_needs_a_confirmation_and_then_deletes_the_package_root(world: World) -> None:
    """--purge deletes nothing without a confirmation (exit 5); once confirmed it deletes the root and unlinks nothing outside it."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    package = world.user / "tool"

    refused = world.one("uninstall", "tool", purge=True)

    assert not refused.ok and refused.exit_code == 5 and (package / "1.0.0" / "App").is_dir()
    assert launchers(world) == ["tool.exe"]
    asked: list[str] = []
    world.engine.confirm = lambda question: asked.append(question) or True
    purged = world.one("uninstall", "tool", purge=True)
    assert purged.ok and purged.status == "purged" and not package.exists() and "data" in asked[0]
    assert world.fake.shortcuts() == {} and launchers(world) == []


def test_cleanup_drops_old_recoverable_apps_and_keeps_current_and_one_previous(world: World) -> None:
    """cleanup removes the App of older versions beyond --keep, never the current one, and keeps their pkg.toml."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    for version in ("1.1.0", "1.2.0"):
        world.publish("tool", version, tables=ENV_ONLY)
        assert world.one("update", "tool").version == version
    package = world.user / "tool"

    result = world.one("cleanup", "tool")

    assert result.ok and result.details["apps"] == ["1.0.0"]
    assert not (package / "1.0.0" / "App").exists() and (package / "1.0.0" / "pkg.toml").is_file()
    assert (package / "1.1.0" / "App").is_dir() and (package / "1.2.0" / "App").is_dir()
    assert current_version(package) == "1.2.0"


def test_cleanup_removes_receipts_of_versions_that_are_gone_and_leftover_work_folders(world: World) -> None:
    """A receipt whose version directory was deleted and anything left in .gupkg\\work go; the receipt of a version that exists stays."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    world.one("update", "tool")
    package = world.user / "tool"
    shutil.rmtree(package / "1.0.0")
    leftover = package / ".gupkg" / "work" / "populate-crashed"
    leftover.mkdir(parents=True)
    (leftover / "half.bin").write_text("x", encoding="utf-8")
    old = leftover.stat().st_mtime - 3 * 24 * 3600
    os.utime(leftover, (old, old))

    result = world.one("cleanup", "tool")

    assert result.ok and result.details["receipts"] == ["1.0.0"] and result.details["work"] == ["populate-crashed"]
    assert not (package / ".gupkg" / "receipts" / "1.0.0.toml").exists() and (package / ".gupkg" / "receipts" / "1.1.0.toml").is_file()
    assert not leftover.exists()


def test_cleanup_root_prunes_logs_beyond_retention_and_registry_trees_that_are_not_active(world: World) -> None:
    """The root's logs and every package's keep `[logs] keep_runs` runs; only the active registry revision stays."""
    world.machine.write_app_settings(tables="[logs]\nkeep_runs = 2\nkeep_days = 3650\n")
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    context = world.mode_context()
    for logs in (context.log_dir, world.user / "tool" / ".gupkg" / "logs"):
        logs.mkdir(parents=True, exist_ok=True)
        for day in (10, 11, 12, 13):
            for suffix in ("log", "jsonl"):
                (logs / f"202610{day}T000000Z-install.{suffix}").write_text("x", encoding="utf-8")
    cache = world.registry({"tool": {"versions": {"1.0.0": {"app.exe": "x"}}}})
    (cache / "trees" / ("b" * 40)).mkdir()
    (cache / "trees" / ("b" * 40) / "leftover.txt").write_text("old", encoding="utf-8")
    settings = world.engine.load().settings

    done = lifecycle.cleanup_root(world.engine, context, settings)

    assert sorted(item.name for item in context.log_dir.glob("*.log")) == ["20261012T000000Z-install.log", "20261013T000000Z-install.log"]
    assert sorted(item.name for item in (world.user / "tool" / ".gupkg" / "logs").glob("*.log")) == [
        "20261012T000000Z-install.log", "20261013T000000Z-install.log"]
    assert [path.name for path in (cache / "trees").iterdir()] == ["a" * 40]
    assert done["registry_trees"] == [str(cache / "trees" / ("b" * 40))] and len(done["logs"]) == 2


def test_nothing_runs_cleanup_unless_asked(world: World) -> None:
    """After install, update and uninstall the old version directories and their App folders are still present."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    world.one("update", "tool")
    world.one("uninstall", "tool")

    package = world.user / "tool"
    assert (package / "1.0.0" / "App" / "app.exe").is_file() and (package / "1.1.0" / "App" / "app.exe").is_file()


def test_a_version_that_drops_a_shortcut_and_a_shim_removes_both_on_update(world: World) -> None:
    """Updating to a version that no longer declares a shortcut and a shim removes both from the system."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    assert launchers(world) == ["tool.exe"] and len(world.fake.shortcuts()) == 1

    result = world.one("update", "tool")

    assert result.ok
    assert launchers(world) == [] and not (bin_dir(world) / "tool.config.toml").exists()
    assert world.fake.shortcuts() == {}
    assert [effect.kind for effect in read_install_record(world.user / "tool").record.effects] == ["env"]


def test_the_download_script_fetches_only_the_urls_written_in_pkg_toml(world: World) -> None:
    """Installing a version populated by the built-in download script, with no hooks, requests nothing but its own URL."""
    folder = world.publish("tool", "1.0.0", tables=FULL)
    world.tracker("tool")
    world.fake.fetch_log.clear()
    world.fake.request_log.clear()

    assert world.one("install", "tool").ok

    assert world.fake.fetch_log == [f"{BASE_URL}/tool-1.0.0.zip"]
    assert [item.url for item in world.fake.request_log] == [f"{BASE_URL}/tool-1.0.0.zip"]
    assert folder.is_dir()


def test_install_and_update_make_no_tracker_request(world: World) -> None:
    """A package with only a _check_updates tracker makes no tracker request on install and update: the fetch log stays empty."""
    world.tracker("tool")
    world.fake.fetch_log.clear()

    refused = world.one("install", "tool")

    assert not refused.ok and "check-updates" in refused.error["message"] and world.fake.fetch_log == []
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    world.fake.fetch_log.clear()
    assert world.one("update", "tool").status == "up-to-date" and world.fake.fetch_log == []


def test_a_failed_population_leaves_the_previous_version_and_the_machine_unchanged(world: World) -> None:
    """When the unpack fails after the download, `current`, the effects and the install record are exactly as before."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY, source='require = ["missing.exe"]\n')
    package = world.user / "tool"
    before = (
        dict(world.fake.shortcuts()), dict(world.fake.env_values(Scope.USER)), launchers(world), world.fake.broadcasts,
        (package / ".gupkg" / "package.toml").read_text(),
    )

    result = world.one("update", "tool")

    assert not result.ok and result.exit_code == 3 and result.error["code"] == "unpack-failed"
    assert current_version(package) == "1.0.0" and not (package / "1.1.0" / "App").exists()
    assert before == (
        dict(world.fake.shortcuts()), dict(world.fake.env_values(Scope.USER)), launchers(world), world.fake.broadcasts,
        (package / ".gupkg" / "package.toml").read_text(),
    )
    assert not (package / ".gupkg" / "work").exists() or list((package / ".gupkg" / "work").iterdir()) == []


def test_a_download_only_update_prepares_the_version_and_changes_no_integration(world: World) -> None:
    """update --download-only populates the newest known version and stops: current, effects and install record are as before."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    package = world.user / "tool"
    record = (package / ".gupkg" / "package.toml").read_text()
    shortcuts = dict(world.fake.shortcuts())

    result = world.one("update", "tool", download_only=True)

    assert result.ok and result.status == "prepared" and current_version(package) == "1.0.0"
    assert (package / "1.1.0" / "App" / "app.exe").is_file() and (package / ".gupkg" / "package.toml").read_text() == record
    assert dict(world.fake.shortcuts()) == shortcuts and read_receipt(package, "1.1.0").data["run"]["activated"] is False
    assert world.one("update", "tool").status == "updated"


def test_switching_to_a_version_without_an_app_rebuilds_it_from_its_source(world: World) -> None:
    """switch to a version whose App was dropped by cleanup downloads and populates it first."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    world.one("update", "tool")
    world.one("cleanup", "tool", keep=0)
    package = world.user / "tool"
    assert not (package / "1.0.0" / "App").exists()

    result = world.one("switch", "tool", version="1.0.0")

    assert result.ok and (package / "1.0.0" / "App" / "app.exe").is_file() and current_version(package) == "1.0.0"


def test_install_with_a_version_activates_that_version_of_an_installed_package(world: World) -> None:
    """`install name@version` activates the named version even when another one is installed."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)

    result = world.one("install", "tool@1.1.0")

    assert result.ok and result.version == "1.1.0" and result.previous == "1.0.0" and current_version(world.user / "tool") == "1.1.0"


def test_a_name_in_two_roots_is_ambiguous_until_qualified(tmp_path: Path) -> None:
    """A bare name that exists in the system and the user root is refused for a mutating command and says how to qualify it."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", mode="user", tables=ENV_ONLY)
    world.publish("tool", "1.0.0", mode="system", tables=ENV_ONLY)

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "ambiguous" and "user:tool" in result.error["message"]
    assert world.one("install", "user:tool").ok


def test_a_failed_effect_is_reported_and_recorded_truthfully_and_a_repeat_converges(world: World) -> None:
    """When the shortcut cannot be written the others are applied, the failure is named, the record lists only what exists, and install again completes it."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.fake.failures["write_shortcut"] = OSError("access denied")

    failed = world.one("install", "tool")

    package = world.user / "tool"
    assert not failed.ok and failed.exit_code == 3 and failed.error["code"] == "integrate-failed"
    assert "access denied" in failed.error["message"] and len(failed.details["failed"]) == 1
    assert failed.details["failed"][0].startswith("shortcut Tool.lnk: ") and len(failed.details["applied"]) == 4
    assert sorted(effect.kind for effect in read_install_record(package).record.effects) == ["env", "path", "path", "shim"]
    assert current_version(package) == "1.0.0" and launchers(world) == ["tool.exe"] and world.fake.shortcuts() == {}

    repeated = world.one("install", "tool")

    assert repeated.ok and repeated.status == "repaired" and [path.stem for path in world.fake.shortcuts()] == ["Tool"]


def test_a_junction_that_cannot_be_switched_leaves_the_previous_version_active(world: World) -> None:
    """Replacing a junction removes the old one first; when creating the new one fails the old one is put back."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV_ONLY)
    world.fake.failures["create_junction"] = RuntimeError("Windows refused")

    result = world.one("update", "tool")

    package = world.user / "tool"
    assert not result.ok and "could not be switched" in result.error["message"]
    assert current_version(package) == "1.0.0" and (package / "1.1.0" / "App" / "app.exe").is_file()


def test_a_failed_removal_stays_in_the_record_and_uninstall_can_be_repeated(world: World) -> None:
    """If an environment value cannot be deleted the uninstall reports it, keeps that effect recorded and succeeds on the next run."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.one("install", "tool")
    world.fake.failures["delete_env"] = OSError("locked")

    failed = world.one("uninstall", "tool")

    package = world.user / "tool"
    assert not failed.ok and "TOOL_HOME" in failed.error["message"]
    assert [effect.kind for effect in read_install_record(package).record.effects] == ["env"]
    assert world.one("uninstall", "tool").ok and read_install_record(package).record.effects == ()
    assert "TOOL_HOME" not in world.fake.env_values(Scope.USER)


def test_purge_refuses_a_package_folder_that_holds_the_root(world: World) -> None:
    """A package installed in place in the folder that also holds the user root cannot be purged: the root would go with it."""
    package = world.base / "home"
    url, digest = world.serve("tool", "1.0.0")
    version = package / "1.0.0"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(pkg_text(url, sha256=digest, tables=ENV_ONLY), encoding="utf-8")
    assert world.one("install", str(version), in_place=True).ok
    world.engine.yes = True

    result = world.one("uninstall", str(package), purge=True)

    assert not result.ok and "Refusing to purge" in result.error["message"]
    assert (package / "1.0.0" / "App" / "app.exe").is_file() and world.user.exists()


def test_a_refused_environment_broadcast_is_a_warning_not_a_failure(world: World) -> None:
    """The change is made; only the notification failed, so the install succeeds and says that new programs see it later."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)
    world.fake.failures["broadcast_environment_change"] = OSError("refused")

    result = world.one("install", "tool")

    assert result.ok and any("could not be broadcast" in warning for warning in result.warnings)
    assert world.fake.env_values(Scope.USER)["TOOL_HOME"].value.endswith("App")


class Collect:
    """A reporter that keeps every event."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def handle(self, event: object) -> None:
        """Keep one event."""
        self.events.append(event)


def test_an_install_reports_its_phases_in_order_and_skipped_ones_as_skipped(world: World) -> None:
    """The event stream has a start and an end for each phase of design 13.1, in order, inside one package scope."""
    world.publish("tool", "1.0.0", tables=FULL)
    collected = Collect()

    with use_reporter(collected):
        world.one("install", "tool")
        world.one("install", "tool")

    ends = [(event.phase, event.status) for event in collected.events if isinstance(event, PhaseEnd)]
    first, second = ends[:8], ends[8:]
    assert [name for name, _ in first] == list(INSTALL_PHASES) and {status for _, status in first} == {"ok"}
    assert dict(second) == {"Resolve": "ok", "Fetch": "skipped", "Verify": "skipped", "Unpack": "skipped", "Prepare": "skipped",
                            "Activate": "skipped", "Integrate": "skipped", "Finish": "ok"}
    kinds = [type(event).__name__ for event in collected.events]
    assert kinds[0] == "PackageStart" and kinds.count("PackageStart") == kinds.count("PackageEnd") == 2


def test_a_failure_is_reported_as_an_error_event_and_the_package_scope_is_closed(world: World) -> None:
    """A failing package emits an Error event and still ends its scope; a target that cannot be resolved emits an Error too."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY, source='require = ["missing.exe"]\n')
    collected = Collect()

    with use_reporter(collected):
        failed = world.one("install", "tool")
        unknown = world.one("install", "nothing-here")

    assert not failed.ok and not unknown.ok
    tail = [event for event in collected.events if isinstance(event, (Error, PackageEnd))]
    assert [type(event).__name__ for event in tail] == ["Error", "PackageEnd", "Error"]
    assert "missing.exe" in tail[0].text and tail[1].result["ok"] is False and "nothing-here" in tail[2].text


def test_every_result_and_plan_is_json_ready(world: World) -> None:
    """The result document of every operation, and the plan document, survive `json.dumps` (the machine interface of design 10.4)."""
    world.publish("tool", "1.0.0", tables=FULL)
    world.tracker("tool")
    world.announce("tool", "1.1.0")
    documents = []
    for operation, options in (
        ("install", {}), ("check", {}), ("check-updates", {}), ("update", {}), ("cleanup", {"keep": 0}), ("switch", {"version": "1.0.0"}),
        ("uninstall", {}),
    ):
        batch = world.run(operation, "tool", **options)
        documents.append(batch.to_data())
        assert batch.summary()
    plans = lifecycle.plan(world.engine, [Request("install", "tool"), Request("update", "tool"), Request("cleanup", "tool")])
    documents += [item.to_data() for item in plans]

    assert all(json.loads(json.dumps(document)) == document for document in documents)


def test_requests_refuse_an_unknown_operation_and_a_negative_keep() -> None:
    """The request is validated when it is built, so a front end cannot send nonsense to the engine."""
    with pytest.raises(ValueError, match="unknown operation"):
        Request("explode", "tool")
    with pytest.raises(ValueError, match="keep"):
        Request("cleanup", "tool", keep=-1)


def test_update_of_a_package_that_is_not_installed_is_refused(world: World) -> None:
    """update never installs (decision D9): a local package with no `current` is refused with a hint."""
    world.publish("tool", "1.0.0", tables=ENV_ONLY)

    result = world.one("update", "tool")

    assert not result.ok and result.error["code"] == "not-installed" and "install" in result.error["message"]
    assert not (world.user / "tool" / "current").exists()


@pytest.mark.parametrize("spelling", ["", "\\", "\\."])
def test_install_with_the_path_of_a_local_version_directory_activates_that_version(world: World, spelling: str) -> None:
    """`install <root>\\tool\\20261017-local` (with or without a trailing separator or dot) activates that version; it does not repair the active one."""
    world.publish("tool", "2026.3.7")
    world.publish("tool", "20261017-local")
    world.one("install", "tool@2026.3.7")
    folder = world.user / "tool" / "20261017-local"

    result = world.one("install", str(folder) + spelling)

    assert result.ok and result.version == "20261017-local" and result.status == "installed"
    assert current_version(world.user / "tool") == "20261017-local"

