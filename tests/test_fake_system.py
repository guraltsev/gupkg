"""Cover what ``FakeSystem`` promises tests and ``gupkg --sandbox``.

Behavior covered: the registry access log lists every registry read and write in order; shortcut records are ``.lnk.json``
files that never leave the fake's temporary tree; URLs and programs that were not set up raise instead of reaching the machine;
sandbox state persists in ``system.json``; ``gupkg --sandbox DIR install`` creates no registry value, shortcut or PATH change on
the real machine, and a system-mode sandbox install follows the elevated flag in ``system.json``; the sandbox runs programs by
identity, never by name; the helper functions that callers use to ask about elevation, admin-only folders and mandatory child
processes. The registry rule of the engine (install, update and uninstall read nothing but ``PATH``) is in
``test_engine_batch.py``.

Real: the command line, the engine, package trees and 7-Zip. Fake: the registry, shortcuts, elevation, URLs and processes. The
sandbox CLI tests pass the command line a real-system stand-in that fails on any machine integration call, which is how they
prove the real machine is never reached.
Out of scope: the real Windows implementation (``tests/test_system_contract.py``).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from gupkg import cli
from gupkg.extractors import find_7z
from gupkg.selfmgmt.elevation import UntrustedLocationError
from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.fake import FakeSystem, RegistryAccess, UnexpectedCall
from gupkg.system.port import (
    ProcessFailed,
    ProcessResult,
    Mode,
    ShortcutSpec,
    is_elevated_run,
    run_checked,
)
from gupkg.system.windows import WindowsSystem
from tests.mode_support import Machine, pin_no_ambient_elevation  # noqa: F401
from tests.scope_support import scope_of


# ---------------------------------------------------------------------------
# The registry access log (D18, D38)
# ---------------------------------------------------------------------------


def test_registry_access_log_lists_each_read_write_and_delete_in_order(system: FakeSystem) -> None:
    """Every registry operation is logged once, in the order it happened, with the value written."""
    system.set_env(Mode.USER, "TOOL_HOME", r"C:\Tools", True)
    system.get_path(Mode.USER)
    system.set_path(Mode.USER, [r"C:\Tools\bin"])
    system.delete_env(Mode.SYSTEM, "OLD_VALUE")
    system.get_env(Mode.USER, "tool_home")

    assert system.registry_log == [
        RegistryAccess("write", "user", "TOOL_HOME", r"C:\Tools"),
        RegistryAccess("read", "user", "Path"),
        RegistryAccess("write", "user", "Path", r"C:\Tools\bin"),
        RegistryAccess("delete", "system", "OLD_VALUE"),
        RegistryAccess("read", "user", "tool_home"),
    ]


# ---------------------------------------------------------------------------
# Shortcuts, URLs and processes
# ---------------------------------------------------------------------------


def test_shortcut_records_are_lnk_json_files_inside_the_fake_tree(tmp_path: Path) -> None:
    """A shortcut becomes a visible ``.lnk.json`` record, and a path outside the fake's root is redirected below it."""
    system = FakeSystem(tmp_path / "machine")
    inside = system.scope_folders(Mode.USER).shortcut_root / "Group" / "Tool.lnk"
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
    """Registry values, their log, broadcasts survive in ``system.json`` and are read by the next run."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    first = FakeSystem.sandbox(sandbox, FakeSystem(tmp_path / "delegate"))
    first.set_env(Mode.USER, "TOOL_HOME", "home", True)
    first.set_path(Mode.USER, [r"C:\a", r"C:\b"])
    first.broadcast_environment_change()

    second = FakeSystem.sandbox(sandbox, FakeSystem(tmp_path / "delegate"))

    assert second.registry_log == first.registry_log
    assert second.env_values(Mode.USER)["TOOL_HOME"].value == "home"
    assert second.broadcasts == 1
    assert second.get_path(Mode.USER) == [r"C:\a", r"C:\b"]
    assert json.loads((sandbox / "system.json").read_text(encoding="utf-8"))["env"]["user"]["Path"]["expandable"] is True


def real_machine_trap() -> WindowsSystem:
    """Return a real-system stand-in that fails on every machine-integration call."""
    class Trap(WindowsSystem):
        """Fail on any call that would change or read the real machine's integration points."""

    for name in (
        "get_env", "set_env", "delete_env", "get_path", "set_path", "broadcast_environment_change",
        "write_shortcut", "read_shortcut", "delete_shortcut", "is_elevated",
        "scope_folders",
    ):
        def trapped(self, *args, _name=name, **kwargs):
            raise AssertionError(f"the sandbox reached the real machine through {_name}")

        setattr(Trap, name, trapped)
    trap = Trap()
    with pytest.raises(AssertionError, match="reached the real machine"):
        trap.get_path(Mode.USER)  # the trap is armed
    return trap


def package_with_every_effect(machine: Machine) -> Path:
    """Write a distribution folder ``Tool 1.0.0`` that ships its App and declares a shortcut, an environment value, a PATH entry and a shim."""
    version = machine.base / "downloads" / "tool" / "1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("tool", encoding="utf-8")
    (version / "pkg.toml").write_text(
        "format = 2\n[source]\nshipped = true\n\n"
        '[[shortcut]]\nname = "Tool"\ntarget = "$App\\\\tool.exe"\n\n'
        '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n\n'
        '[[path]]\nvalue = "$App"\n\n'
        '[[shim]]\nname = "tool"\ntarget = "$App\\\\tool.exe"\n',
        encoding="utf-8",
    )
    return version


def sandboxed(machine: Machine, sandbox: Path, *argv: str) -> int:
    """Run ``gupkg --sandbox <sandbox> <argv>`` on the machine's settings with the real system replaced by the trap."""
    location = RunningGupkg("source", machine.base / "tree" / "gupkg", (machine.base / "tree",), app_dir=machine.app)
    return cli.main(["--sandbox", str(sandbox), *argv], system=real_machine_trap(), env=machine.env, location=location)


def test_sandbox_install_leaves_the_real_machine_alone_and_records_what_it_would_have_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``gupkg --sandbox DIR install`` records its shortcut, environment value and PATH change in DIR and touches no machine integration.

    The real Windows implementation is swapped for one that fails on every registry, shortcut, broadcast or
    elevation call, so reaching the machine would fail the run.
    """
    machine = Machine(tmp_path)
    version = package_with_every_effect(machine)
    sandbox = tmp_path / "sandbox"

    code = sandboxed(machine, sandbox, "install", str(version))

    captured = capsys.readouterr()
    state = json.loads((sandbox / "system.json").read_text(encoding="utf-8"))
    app = str(machine.user_root / "tool" / "current" / "App")
    assert code == 0, captured.out + captured.err
    assert state["env"]["user"]["TOOL_HOME"]["value"] == app
    assert app in state["env"]["user"]["Path"]["value"]
    assert {entry["name"] for entry in state["registry_log"] if entry["operation"] == "read"} == {"Path"}
    shortcut_records = list(sandbox.rglob("*.lnk.json"))
    assert [record.name for record in shortcut_records] == ["Tool.lnk.json"]
    assert json.loads(shortcut_records[0].read_text(encoding="utf-8"))["spec"]["target_path"] == app + "\\tool.exe"
    assert (sandbox / "bin" / "user" / "tool.exe").is_file()


def test_sandbox_install_follows_the_elevated_flag_in_system_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sandbox install is a user-mode run until ``system.json`` says elevated, then a system-mode run that records machine-scope state.

    Elevation in the sandbox is the flag in ``system.json``, never the real token.
    """
    machine = Machine(tmp_path)
    version = package_with_every_effect(machine)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    normal = sandboxed(machine, sandbox, "install", str(version), "--yes")
    assert normal == 0, capsys.readouterr()
    assert not (machine.system_root / "tool").exists() and (machine.user_root / "tool" / "current").exists()

    (sandbox / "system.json").write_text('{"elevated": true}\n', encoding="utf-8")
    code = sandboxed(machine, sandbox, "install", str(version), "--yes")

    state = json.loads((sandbox / "system.json").read_text(encoding="utf-8"))
    assert code == 0, capsys.readouterr()
    assert "TOOL_HOME" in state["env"]["system"] and (machine.system_root / "tool" / "current").exists()
    assert (sandbox / "bin" / "system" / "tool.exe").is_file()


def test_a_program_outside_the_allow_list_raises_in_a_sandbox_run(tmp_path: Path) -> None:
    """In sandbox mode only git and 7-Zip may run; any other program, such as a package script, raises ``UnexpectedCall``."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    system = FakeSystem.sandbox(sandbox, WindowsSystem())
    marker = tmp_path / "ran.txt"
    script = tmp_path / "script.cmd"
    script.write_text(f'@echo off\r\necho x> "{marker}"\r\n', encoding="ascii")

    with pytest.raises(UnexpectedCall):
        system.run([str(script)])
    with pytest.raises(UnexpectedCall):
        system.run(["cmd", "/c", f"echo x> {marker}"])

    assert not marker.exists()
    assert system.run(["git", "--version"]).returncode == 0
    if os.name == "nt":
        assert system.run([str(find_7z()), "i"]).returncode == 0


def _impostor(tmp_path: Path, name: str, marker: Path) -> list[str]:
    """Create a file called *name* that would write *marker* if it ran, and return the command that starts it."""
    path = tmp_path / "impostors" / name
    path.parent.mkdir(exist_ok=True)
    if name.endswith(".exe"):
        # A real executable (cmd.exe) renamed: run as ``<name> /c echo ...`` it writes the marker.
        shutil.copy(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", path)
        return [str(path), "/c", f"echo x> {marker}"]
    if name.endswith(".ps1"):
        path.write_text(f"Set-Content -Path '{marker}' -Value x\r\n", encoding="ascii")
    else:
        path.write_text(f'@echo off\r\necho x> "{marker}"\r\n', encoding="ascii")
    return [str(path)]


@pytest.mark.skipif(os.name != "nt", reason="the impostors are Windows scripts and executables")
@pytest.mark.parametrize(
    "name",
    ["git.cmd", "git.bat", "git.ps1", "7z.cmd", "git.exe", "7z.exe"],
    ids=["git.cmd", "git.bat", "git.ps1", "7z.cmd", "git.exe-copy", "7z.exe-copy"],
)
def test_the_sandbox_allows_programs_by_identity_not_by_name(tmp_path: Path, name: str) -> None:
    """A script or executable that is merely named like git or 7-Zip raises ``UnexpectedCall`` and never runs."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    system = FakeSystem.sandbox(sandbox, WindowsSystem())
    marker = tmp_path / "ran.txt"
    command = _impostor(tmp_path, name, marker)

    with pytest.raises(UnexpectedCall):
        system.run(command)

    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="the bundled 7-Zip is a Windows executable")
def test_the_sandbox_runs_the_real_git_and_seven_zip_by_bare_name_or_resolved_path(tmp_path: Path) -> None:
    """The git found on PATH (bare, or by its exact path) and the bundled 7-Zip (by its exact path) run for real."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    system = FakeSystem.sandbox(sandbox, WindowsSystem())
    git = system.real_programs["git"]

    assert system.run(["git", "--version"]).returncode == 0
    assert system.run([str(git), "--version"]).returncode == 0
    assert system.run([str(find_7z()), "i"]).returncode == 0
    with pytest.raises(UnexpectedCall):
        system.run(["7z", "i"])


def test_a_real_allowed_program_streams_lines_and_honours_the_timeout(tmp_path: Path) -> None:
    """An allowed real program streams its lines to ``on_line`` and is killed by ``timeout``, as ``System.run`` promises."""
    system = FakeSystem(tmp_path / "machine")
    system.real_programs["python"] = Path(os.path.realpath(sys.executable))
    lines: list[str] = []

    result = system.run(["python", "-c", "print('streamed')"], on_line=lines.append)
    assert (result.returncode, lines) == (0, ["streamed"])
    with pytest.raises(TimeoutError):
        system.run(["python", "-c", "import time; time.sleep(30)"], timeout=0.5)


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


def test_a_folder_users_can_modify_is_refused_in_system_mode_with_the_reason_and_a_repair_command(tmp_path: Path) -> None:
    """Asking for an admin-only folder that is not one raises ``UntrustedLocationError`` naming the folder, reason and fix."""
    folder = tmp_path / "shared"
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={folder})
    scope = scope_of(system)

    scope.require_trusted(tmp_path / "private", "the folder")
    with pytest.raises(UntrustedLocationError) as caught:
        scope.require_trusted(folder / "child", "the folder")

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
