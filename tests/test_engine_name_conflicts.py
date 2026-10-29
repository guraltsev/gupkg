"""Cover name ownership: a package cannot take over a shim, wrapper, shortcut or environment variable of another installed package.

Behaviour covered: installing a package that would provide a name another installed package provides is refused before
anything is created, with a message that names both packages, the name and the way out (code ``name-conflict``, exit code 2),
for a shim, a shortcut, a wrapper file and an environment variable, whatever the letter case; a package keeps its own names
across ``update``, ``switch`` and a repeated ``install``; two targets of one command that provide the same name are both
refused; an update whose new version takes another package's name is refused and the package stays installed and ``outdated``;
``check-updates`` writes such a version without its App and reports the refusal; ``uninstall``, ``cleanup`` and ``check``
are never refused, and ``check`` reports a clash that is already on disk as a problem of both packages; a ``PATH`` entry
is shared, not a name; a pair of registry packages that declare the same names is refused.

Real: the engine, the catalog, real folders. Mocked: ``FakeSystem``. Out of scope: the manager screen (``test_ui_name_conflict.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.catalog import Catalog
from gupkg.catalog.record import write_install_record
from gupkg.model.state import Effect, InstallRecord
from gupkg.system.port import Mode
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401
from tests.mode_support import tree_snapshot

SHIM = '[[shim]]\nname = "{name}"\ntarget = "$App\\\\app.exe"\n'
SHORTCUT = '[[shortcut]]\nname = "{name}"\ntarget = "$App\\\\app.exe"\n'
WRAPPER = '[[shim]]\nname = "{name}"\ncontent = "@echo off"\n'
ENV = '[[env]]\nname = "{name}"\nvalue = "$App"\n'

# (id, table, name in the installed package, name in the new one, words of the message)
KINDS = [
    pytest.param(SHIM, "tool", "tool", "shim 'tool.exe'", id="shim"),
    pytest.param(SHIM, "Tool.exe", "tool.EXE", "shim 'tool.exe'", id="shim-letter-case"),
    pytest.param(SHORTCUT, "Tool", "Tool", "shortcut 'Tool.lnk'", id="shortcut"),
    pytest.param(SHORTCUT, "Tool", "tool", "shortcut 'tool.lnk'", id="shortcut-letter-case"),
    pytest.param(WRAPPER, "tool.cmd", "tool.cmd", "wrapper 'tool.cmd'", id="wrapper"),
    pytest.param(ENV, "TOOL_HOME", "TOOL_HOME", "environment variable 'TOOL_HOME'", id="environment-variable"),
    pytest.param(ENV, "TOOL_HOME", "tool_home", "environment variable 'tool_home'", id="environment-variable-letter-case"),
]


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root."""
    return World(tmp_path)


def state(world: World) -> dict[str, object]:
    """Return everything a refused run must leave alone: the root (without run logs), the bin folder, shortcuts, environment, PATH."""
    root = {key: value for key, value in tree_snapshot(world.user).items() if "logs" not in key}
    scope = Mode.USER
    return {
        "root": root, "bin": tree_snapshot(world.fake.scope_folders(scope).bin_dir), "shortcuts": sorted(map(str, world.fake.shortcuts())),
        "env": sorted(world.fake.env_values(scope)), "path": list(world.fake.get_path(scope)),
    }


def install_first(world: World, table: str, name: str) -> None:
    """Publish and install package ``first`` providing *name*."""
    world.publish("first", "1.0.0", tables=table.format(name=name))
    assert world.one("install", "first").ok


@pytest.mark.parametrize(("table", "first_name", "second_name", "words"), KINDS)
def test_a_package_cannot_take_over_a_name_of_another_installed_package(
    world: World, table: str, first_name: str, second_name: str, words: str
) -> None:
    """The second package is refused with both package names, the name and the way out; the root, bin and machine are as before."""
    install_first(world, table, first_name)
    world.publish("second", "1.0.0", tables=table.format(name=second_name))
    before = state(world)
    world.fake.fetch_log.clear()

    result = world.one("install", "second")

    assert not result.ok and result.exit_code == 2 and result.error["code"] == "name-conflict"
    message = result.error["message"]
    assert message.startswith(f"second cannot be installed: it would provide the {words} (")
    assert "which the installed package 'first' already provides. Uninstall 'first' first, or use a package definition with a different name." in message
    assert state(world) == before and not (world.user / "second" / "current").exists() and world.fake.fetch_log == []


def test_a_package_keeps_its_own_names_across_update_switch_and_reinstall(world: World) -> None:
    """A different version of the same package is not a conflict, and neither is installing it again."""
    install_first(world, SHIM, "tool")
    world.publish("first", "2.0.0", tables=SHIM.format(name="tool"))

    assert world.one("update", "first").ok
    assert world.one("switch", "first", version="1.0.0").ok
    assert world.one("install", "first").ok
    assert world.one("repair", "first").ok


def test_reinstalling_the_same_package_and_rebuilding_its_app_is_not_a_conflict(world: World) -> None:
    """``install`` of an installed package and ``install --refresh`` (the nearest thing to a forced reinstall) keep its own names."""
    install_first(world, SHIM + SHORTCUT.format(name="Tool") + ENV.format(name="TOOL_HOME"), "tool")

    assert world.one("install", "first").ok
    refreshed = world.one("install", "first", refresh=True)

    assert refreshed.ok, refreshed.error


def test_the_configuration_file_of_a_shim_is_a_name_too(world: World) -> None:
    """A wrapper named ``tool.config.toml`` cannot overwrite the configuration of the shim ``tool`` of another package, nor the reverse."""
    install_first(world, SHIM, "tool")
    config = WRAPPER.format(name="tool.config.toml")
    world.publish("second", "1.0.0", tables=config)
    before = state(world)

    result = world.one("install", "second")

    assert not result.ok and result.error["code"] == "name-conflict"
    assert "it would provide the wrapper 'tool.config.toml'" in result.error["message"] and "'first'" in result.error["message"]
    assert state(world) == before

    # The reverse: the wrapper is installed first, then a shim whose configuration file has that name.
    assert world.one("uninstall", "first").ok
    assert world.one("install", "second").ok
    world.publish("third", "1.0.0", tables=SHIM.format(name="tool"))
    reverse = world.one("install", "third")
    assert not reverse.ok and reverse.error["code"] == "name-conflict" and "'second'" in reverse.error["message"]


def test_a_refused_install_does_not_ask_the_downgrade_question(world: World) -> None:
    """The name check comes before the questions: nothing is asked of a user whose install is then refused."""
    install_first(world, SHIM, "tool")
    world.publish("second", "1.0.0", tables=SHIM.format(name="other"))
    assert world.one("install", "second").ok
    world.publish("second", "2.0.0", tables=SHIM.format(name="other"))
    assert world.one("update", "second").ok
    world.publish("second", "1.0.0", tables=SHIM.format(name="tool"))
    asked: list[str] = []
    world.engine.confirm = lambda question: asked.append(question) or True

    result = world.one("install", "second@1.0.0")

    assert not result.ok and result.error["code"] == "name-conflict"
    assert asked == []


def count_scans(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count how often the root's install records are read for name ownership; the counter is the list's length."""
    calls: list[int] = []
    original = Catalog.installed_effects

    def counting(self: Catalog):
        calls.append(1)
        return original(self)

    monkeypatch.setattr(Catalog, "installed_effects", counting)
    return calls


def test_check_of_many_packages_reads_the_records_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``check`` call over N packages reads the root's install records once, not once per package."""
    for index in range(12):
        world.publish(f"pkg{index}", "1.0.0", tables=SHIM.format(name=f"cmd{index}"))
        assert world.one("install", f"pkg{index}").ok
    scans = count_scans(monkeypatch)

    batch = world.run("check", *[f"pkg{index}" for index in range(12)])

    assert all(result.ok for result in batch.results) and len(scans) == 1


def test_a_batch_install_of_many_packages_reads_the_records_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """One ``install`` call over N packages scans the root once, not once per package."""
    for index in range(12):
        world.publish(f"pkg{index}", "1.0.0", tables=SHIM.format(name=f"cmd{index}"))
    scans = count_scans(monkeypatch)

    batch = world.run("install", *[f"pkg{index}" for index in range(12)])

    assert all(result.ok for result in batch.results) and len(scans) == 1


def test_a_package_installed_earlier_in_the_session_owns_its_names_after_it_is_read_again(world: World) -> None:
    """The index follows one package at a time: after an install it owns its names, after an uninstall it does not."""
    from gupkg.engine.lifecycle import _RootNames

    names = _RootNames(Catalog(world.scope, world.fake))
    assert dict(names.index().owners("x")) == {}
    install_first(world, SHIM, "tool")
    names.reload(world.scope, "first")
    assert len(names.index().owners("x")) == 2 and len(names.index().owners("first")) == 0
    assert world.one("uninstall", "first").ok
    names.reload(world.scope, "first")
    assert dict(names.index().owners("x")) == {}


def test_a_shared_path_entry_is_not_a_name(world: World) -> None:
    """Two packages with shims of their own both add the bin folder to PATH, and neither is refused."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="one"))
    world.publish("second", "1.0.0", tables=SHIM.format(name="two"))

    assert world.one("install", "first").ok and world.one("install", "second").ok
    assert world.fake.get_path(Mode.USER).count(str(world.fake.scope_folders(Mode.USER).bin_dir)) == 1


def test_two_targets_of_one_command_that_provide_one_name_are_both_refused_before_anything_is_created(world: World) -> None:
    """``install first second`` with a shared shim: both targets fail naming both packages, and nothing of either is installed."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    world.publish("second", "1.0.0", tables=SHIM.format(name="tool"))
    world.publish("third", "1.0.0", tables=SHIM.format(name="other"))
    before = state(world)
    world.fake.fetch_log.clear()

    batch = world.run("install", "first", "second", "third")

    first, second, third = batch.results
    for result in (first, second):
        assert not result.ok and result.exit_code == 2 and result.error["code"] == "name-conflict"
        assert result.error["message"] == (
            f"first and second both provide the shim 'tool.exe' ({world.fake.scope_folders(Mode.USER).bin_dir / 'tool.exe'}). "
            "Install one of them, or give one of them a package definition with a different name."
        )
    assert third.ok, third.error
    assert not (world.user / "first" / "current").exists() and not (world.user / "second" / "current").exists()
    assert not (world.fake.scope_folders(Mode.USER).bin_dir / "tool.exe").exists()
    assert state(world)["shortcuts"] == before["shortcuts"] and all(url.endswith("third-1.0.0.zip") for url in world.fake.fetch_log)


def test_an_update_that_takes_a_name_of_another_package_is_refused_and_the_package_stays_outdated(world: World) -> None:
    """The installed version keeps working; the update fails with the refusal; the package is listed as outdated."""
    install_first(world, SHIM, "tool")
    world.publish("second", "1.0.0", tables=SHIM.format(name="other"))
    assert world.one("install", "second").ok
    world.publish("second", "2.0.0", tables=SHIM.format(name="tool"))
    before = state(world)
    world.fake.fetch_log.clear()

    result = world.one("update", "second")

    assert not result.ok and result.exit_code == 2 and result.error["code"] == "name-conflict"
    assert "second cannot be installed: it would provide the shim 'tool.exe'" in result.error["message"]
    view = Catalog(world.scope, world.fake).describe("second")
    assert view.status == "outdated" and view.installed.version == "1.0.0"
    assert (world.user / "second" / "current" / "App" / "app.exe").is_file()
    assert (world.fake.scope_folders(Mode.USER).bin_dir / "other.exe").is_file()
    assert state(world) == before and world.fake.fetch_log == []


def test_check_updates_writes_a_version_that_takes_a_name_but_does_not_download_it(world: World) -> None:
    """The new version stays on disk without App (the package is outdated); the result carries the refusal, and update refuses too."""
    install_first(world, SHIM, "tool")
    world.publish("second", "1.0.0", tables=SHIM.format(name="other"))
    world.tracker("second", tables=SHIM.format(name="tool"))
    assert world.one("install", "second").ok
    world.announce("second", "1.1.0")
    world.fake.fetch_log.clear()

    result = world.one("check-updates", "second")

    assert not result.ok and result.error["code"] == "name-conflict" and result.details["populated"] is False
    assert (world.user / "second" / "1.1.0" / "pkg.toml").is_file() and not (world.user / "second" / "1.1.0" / "App").exists()
    assert not any(url.endswith("second-1.1.0.zip") for url in world.fake.fetch_log)
    assert Catalog(world.scope, world.fake).describe("second").status == "outdated"
    assert world.one("update", "second").error["code"] == "name-conflict"


def claim(world: World, package: str, other: str) -> None:
    """Make the install record of *package* also claim every name of *other* (two records on disk that clash)."""
    catalog = Catalog(world.scope, world.fake)
    view, source = catalog.describe(package), catalog.describe(other)
    effects = tuple(item for item in source.record.effects if item.kind in ("shim", "file", "shortcut", "env"))
    write_install_record(view.path, InstallRecord(view.record.mode, view.record.link, (*view.record.effects, *effects)))


@pytest.fixture
def clashing(world: World) -> World:
    """Two installed packages whose records claim the same shim (the second claimed it by hand)."""
    install_first(world, SHIM, "tool")
    world.publish("second", "1.0.0", tables=SHIM.format(name="other"))
    assert world.one("install", "second").ok
    claim(world, "second", "first")
    return world


def test_check_reports_a_name_that_two_records_claim_as_a_problem_of_both_and_changes_nothing(clashing: World) -> None:
    """``check`` of either package names both, with the way out; the exit status is unhealthy and the state is unchanged."""
    before = state(clashing)
    path = clashing.fake.scope_folders(Mode.USER).bin_dir / "tool.exe"

    for package, other in (("first", "second"), ("second", "first")):
        result = clashing.one("check", package)
        problems = [item for item in result.details["problems"] if item["kind"] == "name-conflict"]
        assert not result.ok and len(problems) == 1, result.details["problems"]
        assert problems[0]["message"] == (
            f"{package} and {other} both provide the shim 'tool.exe' ({path}). Uninstall one of them, or use a package "
            "definition with a different name."
        )
    assert state(clashing) == before


def test_uninstall_cleanup_and_check_fix_are_never_refused_by_a_name_conflict(clashing: World) -> None:
    """A conflicting package can always be cleaned up and removed."""
    assert clashing.one("cleanup", "second").ok
    fixed = clashing.one("check", "second", fix=True)
    assert fixed.error is None or fixed.error["code"] != "name-conflict"
    result = clashing.one("uninstall", "second")

    assert result.ok, result.error
    assert not (clashing.user / "second" / "current").exists()


def test_a_package_that_is_installed_and_conflicting_is_not_refused_when_it_is_uninstalled_and_the_other_is_installable(world: World) -> None:
    """After the owner is uninstalled the name is free again: a refused package installs."""
    install_first(world, SHIM, "tool")
    world.publish("second", "1.0.0", tables=SHIM.format(name="tool"))
    assert not world.one("install", "second").ok

    assert world.one("uninstall", "first").ok
    assert world.one("install", "second").ok


def test_a_pair_of_registry_packages_with_the_same_names_is_refused(world: World) -> None:
    """``copyq`` then ``copyq-portable`` from a registry, both declaring shim ``copyq`` and shortcut ``CopyQ``: the second is refused."""
    tables = SHIM.format(name="copyq") + SHORTCUT.format(name="CopyQ")
    world.registry({
        "copyq": {"versions": {"1.0.0": {"app.exe": "app"}}, "tables": tables},
        "copyq-portable": {"versions": {"1.0.0": {"app.exe": "app"}}, "tables": tables},
    })
    assert world.one("install", "copyq").ok
    before = state(world)

    result = world.one("install", "copyq-portable")

    assert not result.ok and result.error["code"] == "name-conflict"
    assert result.error["message"].startswith("copyq-portable cannot be installed: it would provide the shortcut 'CopyQ.lnk' (")
    assert "which the installed package 'copyq' already provides" in result.error["message"]
    assert state(world) == before and not (world.user / "copyq-portable").exists()


def test_the_name_helpers_compare_letter_case_and_ignore_path_entries() -> None:
    """Unit view: ``Tool.exe`` and ``tool.EXE`` are one name, an environment variable is per scope, a PATH effect provides none."""
    from gupkg.engine.planner import check_names_between, name_owners, provided_names

    shim = lambda path: Effect("shim", {"path": path})  # noqa: E731
    assert provided_names([shim(r"C:\bin\Tool.exe")]).keys() == provided_names([shim(r"c:\BIN\tool.EXE")]).keys()
    user, system = Effect("env", {"scope": "user", "name": "X"}), Effect("env", {"scope": "system", "name": "X"})
    assert provided_names([user]).keys() != provided_names([system]).keys()
    assert provided_names([Effect("path", {"scope": "user", "entry": r"C:\bin"})]) == {}
    assert name_owners({"a": [shim(r"C:\bin\t.exe")], "b": []}, exclude="A") == {}
    assert check_names_between({"a": [shim(r"C:\bin\t.exe")], "b": [shim(r"C:\BIN\T.EXE")]}).keys() == {"a", "b"}
