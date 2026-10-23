"""Cover what ``FakeSystem`` promises tests and ``gupkg --sandbox``, and what the engine does through it.

Behavior covered: the registry access log lists every registry read and write
of a run in order, and an install or update reads nothing from the registry but
``PATH`` (decisions D18 and D38); shortcut records are ``.lnk.json`` files that
never leave the fake's temporary tree; URLs and programs that were not set up
raise instead of reaching the machine; sandbox state persists in
``system.json``; ``gupkg --sandbox DIR install`` creates no registry value,
shortcut or PATH change on the real machine; the helper functions that callers
use to ask about elevation, admin-only folders and mandatory child processes;
and a failed junction replacement leaves the active version in place.

Real: package trees, junctions, the CLI, the update workflow and 7-Zip. Fake:
the registry, shortcuts, elevation, URLs and processes. The sandbox CLI test
replaces the real ``WindowsSystem`` with a trap that fails on any machine
integration call, which is how it proves the real machine is never reached.
Out of scope: the real Windows implementation (``tests/test_system_contract.py``).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from gupkg import cli, commands
from gupkg import gupkg as workflows
from gupkg.core import PackageIdentity, Scope
from gupkg.extractors import find_7z
from gupkg.core import ActionResult
from gupkg.layout import update_current_junction_if_needed
from gupkg.manager import ManagedTarget, ManagerConfig, UpgradePlan, UpgradePlanEntry
from gupkg.selfmgmt.elevation import UntrustedLocationError
from gupkg.system.fake import FakeSystem, RegistryAccess, UnexpectedCall
from gupkg.system.port import (
    ProcessFailed,
    ProcessResult,
    ShortcutSpec,
    is_elevated_run,
    require_admin_only,
    run_checked,
)
from gupkg.system.windows import WindowsSystem


# ---------------------------------------------------------------------------
# The registry access log (D18, D38)
# ---------------------------------------------------------------------------


def test_registry_access_log_lists_each_read_write_and_delete_in_order(system: FakeSystem) -> None:
    """Every registry operation is logged once, in the order it happened, with the value written."""
    system.set_env(Scope.USER, "TOOL_HOME", r"C:\Tools", True)
    system.get_path(Scope.USER)
    system.set_path(Scope.USER, [r"C:\Tools\bin"])
    system.delete_env(Scope.MACHINE, "OLD_VALUE")
    system.get_env(Scope.USER, "tool_home")

    assert system.registry_log == [
        RegistryAccess("write", "user", "TOOL_HOME", r"C:\Tools"),
        RegistryAccess("read", "user", "Path"),
        RegistryAccess("write", "user", "Path", r"C:\Tools\bin"),
        RegistryAccess("delete", "system", "OLD_VALUE"),
        RegistryAccess("read", "user", "tool_home"),
    ]


def _package_with_every_component(root: Path) -> Path:
    """Create a ``Tool 1.0.0`` package with a shortcut, an environment value, a PATH entry and a wrapper."""
    version = root / "Tool" / "1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("tool", encoding="utf-8")
    (version / "pkg.toml").write_text(
        '[[shortcut]]\nname = "Tool"\ntargetPath = \'$App\\tool.exe\'\n\n'
        '[[environment]]\nName = "TOOL_HOME"\nValue = "$App"\n\n'
        '[[path]]\nvalue = "$App"\n\n'
        '[[bin]]\nname = "tool.cmd"\ncontent = "@echo off"\n',
        encoding="utf-8",
    )
    return version


def test_install_reads_nothing_from_the_registry_except_path(tmp_path: Path, system: FakeSystem) -> None:
    """An install writes its environment value and PATH, and its only registry reads are of ``Path`` (D18, D38)."""
    version = _package_with_every_component(tmp_path)

    result = workflows.install_package(version, system, scope=Scope.USER)

    assert result.ok, result.errors
    reads = [access for access in system.registry_log if access.operation == "read"]
    writes = [(access.name.lower(), access.operation) for access in system.registry_log if access.operation != "read"]
    assert reads and {(access.scope, access.name) for access in reads} == {("user", "Path")}
    assert ("tool_home", "write") in writes and ("path", "write") in writes
    assert system.env_values(Scope.USER)["TOOL_HOME"].value == str(version.parent / "current" / "App")


def test_update_reads_nothing_from_the_registry_except_path(tmp_path: Path, system: FakeSystem) -> None:
    """Checking, downloading and activating an update reads the registry only for ``Path``, and reinstalling PATH is idempotent."""
    version = _package_with_every_component(tmp_path)
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("tool.exe", "new tool")
    archive_bytes = archive_buffer.getvalue()
    (version / "pkg.toml").write_text(
        (version / "pkg.toml").read_text(encoding="utf-8")
        + '\n[origin]\nurl = "https://github.com/owner/tool"\n\n'
        '[update.check]\nmode = "github"\nassetName = "tool.zip"\n\n[update.payload]\nmode = "zip"\n',
        encoding="utf-8",
    )
    assert workflows.install_package(version, system, scope=Scope.USER).ok
    system.add_url(
        "https://api.github.com/repos/owner/tool/releases/latest",
        json.dumps({
            "id": 5,
            "tag_name": "2.0.0",
            "assets": [{
                "name": "tool.zip",
                "state": "uploaded",
                "browser_download_url": "https://example.invalid/tool.zip",
                "digest": "sha256:" + hashlib.sha256(archive_bytes).hexdigest(),
            }],
        }),
    )
    system.add_url("https://example.invalid/tool.zip", archive_bytes)
    first_path = system.get_path(Scope.USER)
    system.registry_log.clear()

    updated = workflows.full_package_upgrade(version, system, scope=Scope.USER)

    assert updated.ok, updated.errors
    assert {(access.scope, access.name) for access in system.registry_log if access.operation == "read"} == {("user", "Path")}
    assert system.get_path(Scope.USER) == first_path


# ---------------------------------------------------------------------------
# Shortcuts, URLs and processes
# ---------------------------------------------------------------------------


def test_shortcut_records_are_lnk_json_files_inside_the_fake_tree(tmp_path: Path) -> None:
    """A shortcut becomes a visible ``.lnk.json`` record, and a path outside the fake's root is redirected below it."""
    system = FakeSystem(tmp_path / "machine")
    inside = system.scope_folders(Scope.USER).shortcut_root / "Group" / "Tool.lnk"
    outside = tmp_path / "elsewhere" / "Other.lnk"
    spec = ShortcutSpec(r"C:\Tools\tool.exe", "--go", r"C:\Tools", r"C:\Tools\tool.ico,2", "A tool")

    system.write_shortcut(inside, spec)
    system.write_shortcut(outside, ShortcutSpec(r"C:\Other.exe"))

    record = json.loads(inside.with_name("Tool.lnk.json").read_text(encoding="utf-8"))
    assert record["spec"]["arguments"] == "--go" and record["path"] == str(inside)
    assert not outside.parent.exists()
    assert system.shortcuts() == {inside: spec, outside: ShortcutSpec(r"C:\Other.exe")}
    assert all((tmp_path / "machine") in path.parents for path in (tmp_path / "machine").rglob("*.json"))


def test_urls_and_programs_that_were_not_set_up_raise_instead_of_reaching_the_machine(tmp_path: Path) -> None:
    """An unknown URL or program raises ``UnexpectedCall`` (an ``OSError``); registered doubles run and stream their lines."""
    seen: list[str] = []
    system = FakeSystem(
        tmp_path / "machine",
        urls={"https://example.invalid/known": b"ok"},
        processes={"tool": lambda call: ProcessResult(4, "line one\nline two\n", "warn\n")},
    )

    with pytest.raises(UnexpectedCall):
        system.fetch("https://example.invalid/unknown", None, tmp_path / "never")
    with pytest.raises(UnexpectedCall):
        system.fetch_text("https://example.invalid/unknown")
    with pytest.raises(UnexpectedCall):
        system.run(["unlisted.exe", "--flag"])
    result = system.run([r"C:\Anywhere\Tool.exe"], on_line=seen.append)

    assert not (tmp_path / "never").exists()
    assert (result.returncode, seen) == (4, ["line one", "line two", "warn"])
    assert system.fetch_text("https://example.invalid/known") == "ok"
    assert [command[0] for command in system.process_log] == ["unlisted.exe", r"C:\Anywhere\Tool.exe"]


def test_a_url_fixture_can_fail_or_be_computed_per_request(tmp_path: Path) -> None:
    """A fixture may be an exception to raise, or a function of the URL and headers, so a test can stage outages and changes."""
    calls: list[dict[str, str]] = []
    system = FakeSystem(
        tmp_path / "machine",
        urls={
            "https://example.invalid/down": OSError("connection reset"),
            "https://example.invalid/echo": lambda url, headers: (calls.append(headers), f"hello {headers['X-Who']}")[1],
        },
    )

    with pytest.raises(OSError, match="connection reset"):
        system.fetch_text("https://example.invalid/down")
    assert system.fetch_text("https://example.invalid/echo", {"X-Who": "gupkg"}) == "hello gupkg"
    assert calls == [{"X-Who": "gupkg"}]


@pytest.mark.skipif(os.name != "nt", reason="the bundled 7-Zip is a Windows executable")
def test_a_real_seven_zip_runs_even_though_other_programs_do_not(tmp_path: Path) -> None:
    """The bundled 7-Zip executable is allowed to run for real, because extraction touches only files."""
    system = FakeSystem(tmp_path / "machine")

    result = system.run([str(find_7z()), "i"])

    assert result.returncode == 0 and "7-Zip" in result.stdout
    with pytest.raises(UnexpectedCall):
        system.run([str(tmp_path / "other-tool.exe")])


# ---------------------------------------------------------------------------
# Sandbox mode
# ---------------------------------------------------------------------------


def test_sandbox_state_persists_in_system_json_across_runs(tmp_path: Path) -> None:
    """Registry values, their log, broadcasts and relaunch requests survive in ``system.json`` and are read by the next run."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    first = FakeSystem.sandbox(sandbox, FakeSystem(tmp_path / "delegate"))
    first.set_env(Scope.USER, "TOOL_HOME", "home", True)
    first.set_path(Scope.USER, [r"C:\a", r"C:\b"])
    first.broadcast_environment_change()
    first.relaunch_elevated(["manager", "update"])

    second = FakeSystem.sandbox(sandbox, FakeSystem(tmp_path / "delegate"))

    assert second.registry_log == first.registry_log
    assert second.env_values(Scope.USER)["TOOL_HOME"].value == "home"
    assert second.broadcasts == 1 and second.relaunches == [["manager", "update"]]
    assert second.get_path(Scope.USER) == [r"C:\a", r"C:\b"]
    assert json.loads((sandbox / "system.json").read_text(encoding="utf-8"))["env"]["user"]["Path"]["expandable"] is True


def test_sandbox_install_leaves_the_real_machine_alone_and_records_what_it_would_have_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``gupkg --sandbox DIR install`` records its shortcut, environment value and PATH change in DIR and touches no machine integration.

    The real Windows implementation is swapped for one that fails on every
    registry, shortcut, broadcast or elevation call, so reaching the machine
    would fail the run.
    """
    class Trap(WindowsSystem):
        """Fail on any call that would change or read the real machine's integration points."""

    for name in (
        "get_env", "set_env", "delete_env", "get_path", "set_path", "broadcast_environment_change",
        "write_shortcut", "read_shortcut", "delete_shortcut", "is_elevated", "relaunch_elevated",
        "scope_folders",
    ):
        def trapped(self, *args, _name=name, **kwargs):
            raise AssertionError(f"the sandbox reached the real machine through {_name}")

        setattr(Trap, name, trapped)
    monkeypatch.setattr(cli, "WindowsSystem", Trap)
    with pytest.raises(AssertionError, match="reached the real machine"):
        Trap().get_path(Scope.USER)  # the trap is armed
    version = _package_with_every_component(tmp_path)
    sandbox = tmp_path / "sandbox"

    code = cli.main(["--sandbox", str(sandbox), "--scope", "user", "install", str(version)])

    captured = capsys.readouterr()
    state = json.loads((sandbox / "system.json").read_text(encoding="utf-8"))
    app = str(version.parent / "current" / "App")
    assert code == 0, captured.out + captured.err
    assert state["env"]["user"]["TOOL_HOME"]["value"] == app
    assert app in state["env"]["user"]["Path"]["value"]
    assert {entry["name"] for entry in state["registry_log"] if entry["operation"] == "read"} == {"Path"}
    shortcut_records = list(sandbox.rglob("*.lnk.json"))
    assert [record.name for record in shortcut_records] == ["Tool.lnk.json"]
    assert json.loads(shortcut_records[0].read_text(encoding="utf-8"))["spec"]["target_path"] == app + "\\tool.exe"
    assert (sandbox / "bin" / "user" / "tool.cmd").is_file()


# ---------------------------------------------------------------------------
# Helpers callers use on the boundary
# ---------------------------------------------------------------------------


def test_a_run_is_elevated_when_the_token_or_the_launcher_marker_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GUPKG_ELEVATED=1`` can only make a run stricter, so either signal alone makes it elevated."""
    ordinary = FakeSystem(tmp_path / "a", elevated=False)
    elevated = FakeSystem(tmp_path / "b", elevated=True)
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)
    assert (is_elevated_run(ordinary), is_elevated_run(elevated)) == (False, True)

    monkeypatch.setenv("GUPKG_ELEVATED", "1")
    assert is_elevated_run(ordinary) is True


def test_a_folder_users_can_modify_is_refused_with_the_reason_and_a_repair_command(tmp_path: Path) -> None:
    """Asking for an admin-only folder that is not one raises ``UntrustedLocationError`` naming the folder, reason and fix."""
    folder = tmp_path / "shared"
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={folder})

    require_admin_only(system, tmp_path / "private")
    with pytest.raises(UntrustedLocationError) as caught:
        require_admin_only(system, folder / "child")

    message = str(caught.value)
    assert str(folder / "child") in message and "Users can modify it" in message and "icacls" in message


def test_a_mandatory_process_that_fails_raises_with_its_error_output(tmp_path: Path) -> None:
    """``run_checked`` returns trimmed output on success and raises ``ProcessFailed`` carrying the error text on failure."""
    system = FakeSystem(
        tmp_path / "machine",
        processes={
            "good": lambda call: ProcessResult(0, "  answer\n"),
            "bad": lambda call: ProcessResult(2, "", "fatal: no such ref\n"),
        },
    )

    assert run_checked(system, ["good", "x"]) == "answer"
    with pytest.raises(ProcessFailed, match="fatal: no such ref"):
        run_checked(system, ["bad", "y"])


def _plan_for(scope: Scope, tmp_path: Path) -> UpgradePlan:
    """Return a one-entry upgrade plan whose only eligible target lives in *scope*."""
    from gupkg.collection import DiscoveredPackage

    package = DiscoveredPackage(selector="tool", root=tmp_path / "tool", manifests=[], diagnostics=[])
    target = ManagedTarget(f"{scope.value}:tool", scope, package, "installed", "1.0.0", "1.0.0", "healthy")
    return UpgradePlan([UpgradePlanEntry(target, "eligible", None, ActionResult(True))])


def test_a_bulk_update_asks_for_elevation_only_when_it_changes_the_system_scope_unelevated(tmp_path: Path) -> None:
    """Elevation is needed for an eligible system-scope target in an ordinary process, and for nothing else."""
    ordinary = FakeSystem(tmp_path / "a", elevated=False)
    elevated = FakeSystem(tmp_path / "b", elevated=True)
    system_plan = _plan_for(Scope.MACHINE, tmp_path)
    user_plan = _plan_for(Scope.USER, tmp_path)

    assert commands.needs_elevation(system_plan, ordinary) is True
    assert commands.needs_elevation(system_plan, elevated) is False
    assert commands.needs_elevation(user_plan, ordinary) is False


def test_the_elevated_relaunch_repeats_the_confirmed_update_through_the_system_boundary(tmp_path: Path) -> None:
    """The confirmed bulk update is handed to ``relaunch_elevated`` as a manager command, and a declined prompt is reported."""
    config = ManagerConfig(tmp_path / "gupkg-config.toml", tmp_path / "s", tmp_path / "u")
    accepted = FakeSystem(tmp_path / "a", relaunch_result=True)
    declined = FakeSystem(tmp_path / "b", relaunch_result=False)

    assert commands.relaunch_elevated_update(config, accepted, no_checksum=True) is True
    assert commands.relaunch_elevated_update(config, declined, no_checksum=False) is False

    assert accepted.relaunches == [
        ["manager", "--config", str(config.path), "update", "--yes", "--no-checksum"]
    ]
    assert declined.relaunches == [["manager", "--config", str(config.path), "update", "--yes"]]


# ---------------------------------------------------------------------------
# Activating a version
# ---------------------------------------------------------------------------


def _two_versions(root: Path) -> tuple[PackageIdentity, PackageIdentity]:
    """Create ``Tool\\1.0.0`` and ``Tool\\2.0.0`` and return their identities."""
    identities = []
    for version in ("1.0.0", "2.0.0"):
        path = root / "Tool" / version
        (path / "App").mkdir(parents=True)
        (path / "pkg.toml").write_text("", encoding="utf-8")
        identities.append(PackageIdentity.from_version_path(root / "Tool", path, is_current=False))
    return identities[0], identities[1]


def test_activating_a_version_repoints_current_and_a_failed_activation_keeps_the_old_one(
    tmp_path: Path, system: FakeSystem
) -> None:
    """A successful activation repoints ``current``; one that fails to build its junction leaves the active version and no temporary entries."""
    old, new = _two_versions(tmp_path)
    assert update_current_junction_if_needed(old, system)
    current = old.package_root / "current"

    system.failures["create_junction"] = RuntimeError("disk full")
    with pytest.raises(RuntimeError, match="disk full"):
        update_current_junction_if_needed(new, system)
    assert system.read_link(current) == old.version_path.resolve()
    assert sorted(path.name for path in old.package_root.iterdir()) == ["1.0.0", "2.0.0", "current"]

    assert update_current_junction_if_needed(new, system)
    assert system.read_link(current) == new.version_path.resolve()
    assert sorted(path.name for path in old.package_root.iterdir()) == ["1.0.0", "2.0.0", "current"]
