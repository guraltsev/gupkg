"""Cover the lifecycle commands of the 1.0 command line: install, check-updates, update, switch, uninstall, check, cleanup.

Behaviour covered, always through ``gupkg.cli.main`` (the real argparse grammar): a name and a path give identical
integration destinations; the mode is chosen as design 6.2 says and printed as ``system (auto: elevated)``;
``check-updates`` writes a prepared version and changes nothing else, and ``--no-download`` writes no ``App`` and asks only
the tracker; ``update`` activates a known version without a tracker and, with nothing newer, reports "up to date" without any
request; ``self check-updates`` and ``check-updates gupkg`` give one result document; ``check`` reports a deleted shim as
drift and ``--fix`` restores it; ``uninstall --purge`` and a plan with several targets are questions that ``--yes`` answers
and ``--no-input`` turns into exit code 5; ``switch`` goes back; ``cleanup`` removes old Apps only when asked.

Real: the argument parser, the engine, the catalog, the run session and log files, real folders and junctions below a
temporary directory. Mocked: ``FakeSystem`` (registry, Start Menu, ``PATH``, URL fixtures, elevation) and the environment
mapping, which is built by the test.
Out of scope: the document shapes (``test_cli_machine_interface.py``) and
the reading commands (``test_cli_reading.py``).
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from gupkg.catalog.record import read_install_record
from gupkg.engine.locks import Holder, lock_path, render_lock
from gupkg.system.port import Mode
from tests.support.cli import CliWorld
from tests.support.engine import World

pytestmark = pytest.mark.unit

FULL = """
[[shortcut]]
name = "Tool"
target = "$App\\\\app.exe"

[[shim]]
name = "tool"
target = "$App\\\\app.exe"
"""


def current(package: Path) -> str:
    """Return the version that ``<package>/current`` points at."""
    return Path(os.path.realpath(package / "current")).name


def requests_made(world: World) -> int:
    """Return how many network requests the fake machine served."""
    return len(world.fake.fetch_log) + len(world.fake.request_log)


def test_a_name_and_a_path_install_to_identical_destinations(cli_world: CliWorld) -> None:
    """Installing by name and by the package's path put the same shortcut, shim and PATH entries in place."""
    cli_world.publish("tool", "1.0.0", tables=FULL)
    package = cli_world.user / "tool"

    def destinations() -> tuple[list[str], list[str], set[tuple[str, str]]]:
        """Return where the install put its shortcuts and shims, and the effects its install record lists."""
        bin_dir = cli_world.fake.scope_folders(Mode.USER).bin_dir
        recorded = {
            (effect.kind, str(effect.fields.get("path") or effect.fields.get("entry")))
            for effect in read_install_record(package).record.effects
        }
        return sorted(str(path) for path in cli_world.fake.shortcuts()), sorted(path.name for path in bin_dir.glob("*.exe")), recorded

    by_name = cli_world.cli("install", "tool")
    first = destinations()
    assert by_name.code == 0, by_name.err
    assert cli_world.cli("uninstall", "tool", "--yes").code == 0 and not cli_world.fake.shortcuts()

    by_path = cli_world.cli("install", str(package))

    assert by_path.code == 0, by_path.err
    assert destinations() == first and first[0] and first[1] == ["tool.exe"]


def test_the_mode_is_the_tokens_and_the_resolve_line_names_mode_root_and_source(tmp_path: Path) -> None:
    """An elevated process installs in the system root, a normal shell in the user root; each prints ``<mode> root <path> (<source>)``."""
    elevated = CliWorld(tmp_path / "a", elevated=True)
    elevated.publish("tool", "1.0.0", mode="system")
    normal = CliWorld(tmp_path / "b")
    normal.publish("tool", "1.0.0", mode="user")

    system_run = elevated.cli("install", "tool")
    auto_run = elevated.cli("install", "tool", "--no-input")
    user_run = normal.cli("install", "tool")

    assert system_run.code == 0, system_run.err
    assert f"system root {elevated.system_root} (the installed system gupkg)" in system_run.out
    assert auto_run.code == 0 and f"system root {elevated.system_root}" in auto_run.out
    assert user_run.code == 0 and f"user root {normal.user} (" in user_run.out
    assert (elevated.system_root / "tool" / "current").is_dir() and not (elevated.user / "tool" / "current").exists()


def test_an_elevated_gupkg_installs_a_registry_package_in_the_system_root_and_says_so(cli_elevated_world: CliWorld) -> None:
    """With no flag an elevated gupkg installs a package that exists nowhere yet into the system root, and prints the root and its source."""
    cli_elevated_world.registry({"tool": {"versions": {"1.0.0": None}}}, mode="system")

    result = cli_elevated_world.cli("install", "tool")

    assert result.code == 0, result.err
    assert f"system root {cli_elevated_world.system_root} (the installed system gupkg)" in result.out
    assert (cli_elevated_world.system_root / "tool" / "current").is_dir()


def test_check_updates_writes_a_prepared_version_and_changes_nothing_else(cli_world: CliWorld) -> None:
    """check-updates writes the new version with its App; current, the install record and the integration stay as they were."""
    cli_world.tracker("tool", tables=FULL)
    cli_world.announce("tool", "1.0.0")
    cli_world.publish("tool", "1.0.0", tables=FULL)
    assert cli_world.cli("install", "tool").code == 0
    package = cli_world.user / "tool"
    record_before = (package / ".gupkg" / "package.toml").read_text(encoding="utf-8")
    state_before = (sorted(path.name for path in cli_world.fake.shortcuts()), dict(cli_world.fake.env_values(Mode.USER)), len(cli_world.fake.registry_log))
    cli_world.announce("tool", "1.1.0")

    result = cli_world.cli("check-updates", "tool")

    assert result.code == 0, result.err
    assert (package / "1.1.0" / "App" / "app.exe").is_file() and (package / "1.1.0" / "pkg.toml").is_file()
    assert current(package) == "1.0.0"
    assert (package / ".gupkg" / "package.toml").read_text(encoding="utf-8") == record_before
    assert (sorted(path.name for path in cli_world.fake.shortcuts()), dict(cli_world.fake.env_values(Mode.USER)), len(cli_world.fake.registry_log)) == state_before


def test_check_updates_no_download_writes_no_app_and_asks_only_the_tracker(cli_world: CliWorld) -> None:
    """With --no-download the directory has pkg.toml and no App, and only the tracker's own URL was requested."""
    cli_world.tracker("tool")
    cli_world.announce("tool", "1.0.0")
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    cli_world.announce("tool", "1.1.0")
    cli_world.fake.fetch_log.clear()
    cli_world.fake.request_log.clear()

    result = cli_world.cli("check-updates", "tool", "--no-download")

    package = cli_world.user / "tool"
    assert result.code == 0, result.err
    assert (package / "1.1.0" / "pkg.toml").is_file() and not (package / "1.1.0" / "App").exists()
    requested = set(cli_world.fake.fetch_log) | {item.url for item in cli_world.fake.request_log}
    assert requested and all(url.endswith("tool-latest.json") or url.endswith(".sha256") for url in requested), requested


def test_update_after_check_updates_no_download_fetches_only_what_the_new_pkg_toml_names(cli_world: CliWorld) -> None:
    """update activates the version that check-updates wrote and contacts only the download URL written in its pkg.toml."""
    cli_world.tracker("tool")
    cli_world.announce("tool", "1.0.0")
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    cli_world.announce("tool", "1.1.0")
    assert cli_world.cli("check-updates", "tool", "--no-download").code == 0
    cli_world.fake.fetch_log.clear()
    cli_world.fake.request_log.clear()

    result = cli_world.cli("update", "tool")

    package = cli_world.user / "tool"
    assert result.code == 0, result.err
    assert current(package) == "1.1.0" and (package / "1.1.0" / "App" / "app.exe").is_file()
    written = (package / "1.1.0" / "pkg.toml").read_text(encoding="utf-8")
    contacted = set(cli_world.fake.fetch_log) | {item.url for item in cli_world.fake.request_log}
    assert contacted and all(url in written for url in contacted), contacted


def test_update_with_nothing_newer_says_up_to_date_and_makes_no_request(cli_world: CliWorld) -> None:
    """update of a package with no newer known version reports it and makes no network request at all."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    cli_world.fake.fetch_log.clear()
    cli_world.fake.request_log.clear()

    result = cli_world.cli("update", "tool")

    assert result.code == 0 and "up-to-date" in result.out
    assert requests_made(cli_world) == 0


def test_update_never_runs_a_tracker(cli_world: CliWorld) -> None:
    """A tracker that would find a newer version is not run by update: the version stays and nothing is requested."""
    cli_world.tracker("tool")
    cli_world.announce("tool", "1.0.0")
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    cli_world.announce("tool", "2.0.0")
    cli_world.fake.fetch_log.clear()
    cli_world.fake.request_log.clear()

    result = cli_world.cli("update", "tool")

    assert result.code == 0 and current(cli_world.user / "tool") == "1.0.0" and requests_made(cli_world) == 0


def test_self_check_updates_and_check_updates_gupkg_give_the_same_result_document(tmp_path: Path) -> None:
    """The self alias is the lifecycle command applied to the package gupkg: the two result documents are equal."""
    first, second = CliWorld(tmp_path / "a", installed=False), CliWorld(tmp_path / "b", installed=False)
    for world in (first, second):
        world.tracker("gupkg")
        world.announce("gupkg", "1.0.0")
        world.publish("gupkg", "1.0.0")
        assert world.cli("install", "gupkg").code == 0
        world.announce("gupkg", "1.1.0")

    alias = first.cli("self", "check-updates", "--json")
    direct = second.cli("check-updates", "gupkg", "--json")

    assert alias.code == direct.code == 0, (alias.err, direct.err)

    def comparable(document: dict, base: Path) -> str:
        """Return the document as text with the test's own folder replaced, so two machines can be compared."""
        return json.dumps(document, sort_keys=True).replace(str(base).replace("\\", "\\\\"), "<base>")

    one, two = alias.document(), direct.document()
    for document in (one, two):
        for item in document["results"]:
            item.pop("log"), item.pop("receipt")
    assert comparable(one, tmp_path / "a") == comparable(two, tmp_path / "b")
    assert one["command"] == "check-updates"


def test_check_reports_a_deleted_shim_as_drift_and_fix_restores_it(cli_world: CliWorld) -> None:
    """check names the deleted shim and changes nothing; check --fix puts it back and then reports healthy."""
    cli_world.publish("tool", "1.0.0", tables=FULL)
    assert cli_world.cli("install", "tool").code == 0
    shim = cli_world.fake.scope_folders(Mode.USER).bin_dir / "tool.exe"
    shim.unlink()

    report = cli_world.cli("check", "tool")

    assert report.code == 3 and "tool.exe" in report.out + report.err
    assert not shim.exists()
    fixed = cli_world.cli("check", "tool", "--fix", "--yes")
    assert fixed.code == 0, fixed.err
    assert shim.is_file()
    assert cli_world.cli("check", "tool").code == 0


def test_check_without_targets_checks_every_package(cli_world: CliWorld) -> None:
    """With no target check covers every package of the roots and reports one result per package."""
    cli_world.publish("one", "1.0.0")
    cli_world.publish("two", "1.0.0")
    assert cli_world.cli("install", "one", "two", "--yes").code == 0

    result = cli_world.cli("check", "--json")

    assert result.code == 0
    assert sorted(item["package"] for item in result.document()["results"]) == ["one", "two"]


def test_purge_is_a_question_that_yes_answers_and_no_input_refuses(cli_world: CliWorld) -> None:
    """uninstall --purge --no-input exits 5 with a structured error and deletes nothing; --yes proceeds without reading stdin."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    package = cli_world.user / "tool"

    refused = cli_world.cli("uninstall", "tool", "--purge", "--no-input", "--json")

    assert refused.code == 5 and package.is_dir()
    entry = refused.document()["results"][0]
    assert entry["ok"] is False and entry["error"]["code"] == "input-needed"

    answered = cli_world.cli("uninstall", "tool", "--purge", "--yes", "--no-input")
    assert answered.code == 0 and not package.exists()


def test_a_plan_with_several_targets_is_a_question_too(cli_world: CliWorld) -> None:
    """install of two packages asks once; --no-input is exit code 5 and installs nothing, --yes installs both."""
    cli_world.publish("one", "1.0.0")
    cli_world.publish("two", "1.0.0")

    refused = cli_world.cli("install", "one", "two", "--no-input", "--json")

    assert refused.code == 5 and refused.document()["errors"][0]["code"] == "input-needed"
    assert not (cli_world.user / "one" / "current").exists() and not (cli_world.user / "two" / "current").exists()
    accepted = cli_world.cli("install", "one", "two", "--yes", "--no-input")
    assert accepted.code == 0 and (cli_world.user / "one" / "current").is_dir() and (cli_world.user / "two" / "current").is_dir()


def test_the_several_target_question_can_be_answered_on_the_terminal(cli_world: CliWorld) -> None:
    """Without --yes and --no-input the question is asked on standard input; yes installs, anything else declines."""
    cli_world.publish("one", "1.0.0")
    cli_world.publish("two", "1.0.0")

    declined = cli_world.cli("install", "one", "two", stdin="n\n")
    assert declined.code == 5 and not (cli_world.user / "one" / "current").exists()

    accepted = cli_world.cli("install", "one", "two", stdin="y\n")
    assert accepted.code == 0 and (cli_world.user / "one" / "current").is_dir()


def test_an_install_never_rewrites_a_path_that_cannot_be_read(cli_world: CliWorld) -> None:
    """An unreadable registry PATH fails the install (exit 3, naming the path effect) and nothing is written to the registry."""
    cli_world.publish("tool", "1.0.0", tables='\n[[path]]\nvalue = "$App"\n')
    cli_world.fake.failures["get_env"] = PermissionError("denied")

    result = cli_world.cli("install", "tool", "--json")

    assert result.code == 3, result.out + result.err
    assert "path " in result.document()["results"][0]["error"]["message"] and "denied" in result.document()["results"][0]["error"]["message"]
    assert [access for access in cli_world.fake.registry_log if access.operation != "read"] == []


def test_a_plan_is_printed_before_the_closing_line(cli_world: CliWorld) -> None:
    """The plan text comes first and `==> Done` last, so the answer is not buried under a finished-looking line."""
    cli_world.publish("tool", "1.0.0")

    result = cli_world.cli("install", "tool", "--plan")

    assert result.code == 0, result.err
    lines = result.out.splitlines()
    done = lines.index("==> Done")
    assert any("tool" in line and "1.0.0" in line for line in lines[:done]) and not any("1.0.0" in line for line in lines[done:])


def test_no_input_never_asks_even_when_standard_input_holds_an_answer(cli_world: CliWorld) -> None:
    """--no-input is exit code 5 with a structured error for the several-target question and for --purge, although stdin says yes."""
    cli_world.publish("one", "1.0.0")
    cli_world.publish("two", "1.0.0")
    assert cli_world.cli("install", "one", "--yes").code == 0

    several = cli_world.cli("install", "one", "two", "--no-input", "--json", stdin="y\ny\n")
    purge = cli_world.cli("uninstall", "one", "--purge", "--no-input", "--json", stdin="y\ny\n")

    assert several.code == 5 and several.document()["errors"][0]["code"] == "input-needed"
    assert not (cli_world.user / "two" / "current").exists()
    assert purge.code == 5 and purge.document()["results"][0]["error"]["code"] == "input-needed"
    assert (cli_world.user / "one" / "current").is_dir()


def test_yes_never_reads_standard_input(cli_world: CliWorld) -> None:
    """--yes answers every consequential question: a standard input that fails when read is never touched."""
    cli_world.publish("one", "1.0.0")
    cli_world.publish("two", "1.0.0")

    several = cli_world.cli("install", "one", "two", "--yes", stdin=None)
    purge = cli_world.cli("uninstall", "one", "--purge", "--yes", stdin=None)

    assert several.code == 0 and purge.code == 0, several.err + purge.err
    assert (cli_world.user / "two" / "current").is_dir() and not (cli_world.user / "one").exists()


def test_a_failing_target_does_not_stop_the_rest_and_the_exit_code_is_the_failure(cli_world: CliWorld) -> None:
    """The run continues past a failure: the other package is installed, the summary names the failure, exit code 2 or 3."""
    cli_world.publish("one", "1.0.0")
    cli_world.publish("two", "1.0.0", source='require = ["missing.exe"]\n')
    cli_world.publish("three", "1.0.0")

    result = cli_world.cli("install", "one", "two", "three", "--yes", "--json")

    assert result.code == 3
    assert [item["ok"] for item in result.document()["results"]] == [True, False, True]
    assert (cli_world.user / "three" / "current").is_dir() and not (cli_world.user / "two" / "current").exists()
    assert "two" in result.err + result.out


def test_switch_goes_back_to_another_local_version(cli_world: CliWorld) -> None:
    """switch activates an older local version of an installed package and plain update would not."""
    cli_world.publish("tool", "1.0.0")
    cli_world.publish("tool", "2.0.0")
    assert cli_world.cli("install", "tool@1.0.0").code == 0
    assert cli_world.cli("update", "tool").code == 0 and current(cli_world.user / "tool") == "2.0.0"

    result = cli_world.cli("switch", "tool", "1.0.0")

    assert result.code == 0, result.err
    assert current(cli_world.user / "tool") == "1.0.0"
    assert cli_world.cli("update", "tool").code == 0 and current(cli_world.user / "tool") == "2.0.0"


def test_cleanup_removes_old_apps_only_when_asked(cli_world: CliWorld) -> None:
    """install and update leave old Apps alone; cleanup --keep 0 removes the non-current App and keeps the version directory."""
    cli_world.publish("tool", "1.0.0")
    cli_world.publish("tool", "2.0.0")
    assert cli_world.cli("install", "tool@1.0.0").code == 0
    assert cli_world.cli("update", "tool").code == 0
    package = cli_world.user / "tool"
    assert (package / "1.0.0" / "App").is_dir()

    result = cli_world.cli("cleanup", "tool", "--keep", "0")

    assert result.code == 0, result.err
    assert not (package / "1.0.0" / "App").exists() and (package / "1.0.0" / "pkg.toml").is_file()
    assert (package / "2.0.0" / "App").is_dir()


def test_a_root_wide_cleanup_leaves_a_busy_package_alone_but_a_named_one_fails(cli_world: CliWorld) -> None:
    """Only a cleanup that names no package skips a package another run holds; naming it is package-busy (exit 3)."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    lock = lock_path(cli_world.user / "tool")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(render_lock(Holder(424242, socket.gethostname(), "update", "2026-10-24T10:00:00", "othertoken")), encoding="utf-8")
    cli_world.fake.live_pids.add(424242)

    named = cli_world.cli("cleanup", "tool", "--json")
    sweep = cli_world.cli("cleanup", "--json")

    assert named.code == 3 and named.document()["results"][0]["error"]["code"] == "package-busy"
    assert sweep.code == 0 and sweep.document()["results"][0]["status"] == "locked"


def test_a_path_that_is_no_package_is_a_user_error_that_names_it(cli_world: CliWorld, tmp_path: Path) -> None:
    """install of a folder that is not a package is exit code 2 with a message naming the path, and nothing is created."""
    result = cli_world.cli("install", str(tmp_path / "nothing-here"), "--json")

    entry = result.document()["results"][0]
    assert result.code == 2 and entry["error"]["code"] == "not-found" and "nothing-here" in entry["error"]["message"]
    assert not (cli_world.user / "nothing-here").exists()


def test_a_populate_hook_that_needs_an_unavailable_module_fails_naming_it_and_starts_no_process(cli_world: CliWorld) -> None:
    """A hook that imports a module that is not there fails the install (exit 3) with the module's name; no process starts, nothing is installed."""
    folder = cli_world.user / "tool" / "1.0.0"
    (folder / "pkg.local").mkdir(parents=True)
    hook = "PKG_MODULE_API = 2\nimport gupkg_absent_module_for_tests\ndef populate(ctx):\n    pass\n"
    (folder / "pkg.local" / "populate.py").write_text(hook, encoding="utf-8")
    manifest = 'format = 2\n[source]\nscript = "pkg.local/populate.py"\n'
    (folder / "pkg.toml").write_text(manifest, encoding="utf-8")

    result = cli_world.cli("install", "tool", "--json")

    entry = result.document()["results"][0]
    assert result.code == 3 and entry["error"]["code"] == "hook-error" and "gupkg_absent_module_for_tests" in entry["error"]["message"]
    assert cli_world.fake.process_log == [] and not (cli_world.user / "tool" / "current").exists()
