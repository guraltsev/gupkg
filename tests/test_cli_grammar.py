"""Cover the grammar of the 1.0 command line: the commands, the options after them, help and the forwarders.

Behaviour covered: every command and subcommand of design 10.2 parses and has an example in its help; the help lists the commands
in task order; ``config-check``, ``config-fix``, ``doctor``, ``repair-deps`` and ``update-deps`` are unknown commands, and
``--all``, ``--check``, ``--check-only``, ``--scope`` and ``--no-checksum`` are unrecognized arguments (exit 2);
options go after the command and are never abbreviated; ``--skip-checksum`` takes no target as its reason
and accepts only the reasons ``setting`` and ``user-approved``; ``gupkg tui`` and ``gupkg manager`` start the
launchers of the same gupkg tree with the rest of the command line and return the program's exit code, refuse an elevated shell,
and fail with a clear message when the program is not there.

Real: the argument parser and (for the forwarders) a real child process running fake ``.cmd`` launchers. Mocked: ``FakeSystem``
for everything else.
Out of scope: what each command does (the other ``test_cli_*`` modules) and the programs behind the forwarders (issue 030).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg._version import __version__
from gupkg import cli
from gupkg.engine.receipts import read_receipt
from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ProcessResult
from gupkg.system.windows import WindowsSystem
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

COMMANDS = [
    "check-updates", "install", "update", "switch", "uninstall", "check", "list", "info", "search", "cleanup", "link", "logs",
    "shell", "registry", "settings", "pkg", "self", "tui", "manager",
]
SUBCOMMANDS = [
    ("registry", "sync"), ("registry", "status"), ("registry", "list"), ("registry", "add"), ("registry", "remove"),
    ("pkg", "new"), ("pkg", "check"), ("pkg", "pin"), ("pkg", "bundle"), ("pkg", "export"),
    ("settings", "show"), ("settings", "path"), ("settings", "init"), ("settings", "set"),
    ("self", "status"), ("self", "repair"), ("self", "check-updates"), ("self", "update"),
]


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A fake machine for the commands that need one."""
    return CliWorld(tmp_path)


def test_version_flag_reports_the_version(world: CliWorld) -> None:
    """--version prints the running version and exits 0."""
    result = world.cli("--version")

    assert result.code == 0 and result.out.strip() == f"gupkg {__version__}"


def test_no_command_prints_the_help_and_is_a_usage_error(world: CliWorld) -> None:
    """Running gupkg alone shows the command list (on standard error) and exits 2."""
    result = world.cli()

    assert result.code == 2 and "check-updates" in result.err


def test_the_help_lists_the_commands_in_task_order(world: CliWorld) -> None:
    """check-updates, install, update, list and info come first, then maintenance (check among it), authoring and self."""
    text = world.cli("--help").out

    order = [text.index(marker) for marker in ("  check-updates ", "  install ", "  update ", "  list ", "  info ", "maintenance:", "  check ", "authoring:", "  pkg ", "gupkg itself:", "  self ")]
    assert order == sorted(order)
    for command in COMMANDS:
        assert f"  {command} " in text


@pytest.mark.parametrize("command", [pytest.param(name, id=name) for name in COMMANDS if name not in ("tui", "manager")])
def test_every_command_has_an_example_in_its_help(world: CliWorld, command: str) -> None:
    """gupkg <command> --help shows the options and an example, and exits 0."""
    result = world.cli(command, "--help")

    assert result.code == 0 and "example:" in result.out and f"gupkg {command}" in result.out


@pytest.mark.parametrize(("group", "action"), [pytest.param(*pair, id=" ".join(pair)) for pair in SUBCOMMANDS])
def test_every_subcommand_has_an_example_in_its_help(world: CliWorld, group: str, action: str) -> None:
    """gupkg <group> <action> --help shows an example, and exits 0."""
    result = world.cli(group, action, "--help")

    assert result.code == 0 and "example:" in result.out and f"gupkg {group} {action}" in result.out


@pytest.mark.parametrize("word", [pytest.param(word, id=word.replace("-", "_")) for word in ("config-check", "config-fix", "doctor", "repair-deps", "update-deps")])
def test_the_old_commands_are_unknown(world: CliWorld, word: str) -> None:
    """0.17 command names are not aliases (D33): argparse reports an invalid choice and nothing runs."""
    result = world.cli(word, ".")

    assert result.code == 2 and "invalid choice" in result.err and word in result.err


@pytest.mark.parametrize("argv", [("registry", "sync"), ("list",), ("settings", "show"), ("pkg", "check", ".")], ids=" ".join)
def test_only_engine_commands_can_be_started_as_a_relaunch(world: CliWorld, argv: tuple[str, ...]) -> None:
    """The hidden `--relaunched` marker exists on the engine commands only: elsewhere it is a usage error (exit 2)."""
    result = world.cli(*argv, "--relaunched")

    assert result.code == 2 and "--relaunched" in result.err


@pytest.mark.parametrize(
    "option", ["--all", "--check", "--check-only", "--scope", "--no-checksum", "--refresh-app", "--shim-linkage", "--format", "--pause", "--download-only"]
)
def test_the_other_removed_options_are_plain_unrecognized_arguments(world: CliWorld, option: str) -> None:
    """Core keeps no legacy-specific code (D33): every removed spelling is argparse's own error."""
    result = world.cli("install", "tool", option)

    assert result.code == 2 and "unrecognized arguments" in result.err and option in result.err
    assert "no longer" not in result.err and "use --" not in result.err
    assert not (world.user / "tool").exists()


def test_options_go_after_the_command_and_are_not_abbreviated(world: CliWorld) -> None:
    """An option before the command and an abbreviated option are refused; --mode no longer exists in any position (exit code 2)."""
    world.publish("tool", "1.0.0")

    assert world.cli("--yes", "install", "tool").code == 2
    assert world.cli("install", "tool", "--ye").code == 2
    assert world.cli("--mode", "user", "install", "tool").code == 2
    assert world.cli("install", "tool", "--mode", "user").code == 2
    assert world.cli("install", "tool").code == 0


def test_a_target_is_required_by_the_lifecycle_commands(world: CliWorld) -> None:
    """install, update and the others take at least one target: there is no implicit 'everything' (no --all)."""
    for command in ("check-updates", "install", "update", "uninstall", "info"):
        assert world.cli(command).code == 2, command


@pytest.mark.parametrize(
    ("flag", "accepted"),
    [
        pytest.param("--skip-checksum", True, id="bare"),
        pytest.param("--skip-checksum=cli", True, id="cli"),
        pytest.param("--skip-checksum=setting", True, id="setting"),
        pytest.param("--skip-checksum=user-approved", True, id="user-approved"),
        pytest.param("--skip-checksum=whatever", False, id="unknown-reason"),
        pytest.param("--skip-checksum=", True, id="empty-is-bare"),
    ],
)
def test_skip_checksum_takes_only_the_known_reasons(world: CliWorld, flag: str, accepted: bool) -> None:
    """A reason other than cli, setting and user-approved is refused with exit code 2 and installs nothing."""
    world.publish("tool", "1.0.0", hashed=False)

    result = world.cli("install", "tool", flag)

    assert (result.code == 0) is accepted and ((world.user / "tool" / "current").is_dir() is accepted)
    if not accepted:
        assert result.code == 2


def test_a_bare_skip_checksum_does_not_swallow_the_next_word(world: CliWorld) -> None:
    """`install --skip-checksum tool` installs `tool` unverified: the flag takes no target as its reason."""
    world.publish("tool", "1.0.0", hashed=False)

    result = world.cli("install", "--skip-checksum", "tool")

    assert result.code == 0 and (world.user / "tool" / "current").is_dir()


def test_the_recorded_decision_names_who_allowed_the_skip(world: CliWorld) -> None:
    """The receipt records skipped-cli for the bare flag and skipped-setting / user-approved for the manager's reasons."""
    for index, (flag, decision) in enumerate((("--skip-checksum", "skipped-cli"), ("--skip-checksum=setting", "skipped-setting"),
                                              ("--skip-checksum=user-approved", "user-approved"))):
        name = f"tool{index}"
        world.publish(name, "1.0.0", hashed=False)
        assert world.cli("install", name, flag).code == 0
        assert read_receipt(world.user / name, "1.0.0").data["checksum"]["decision"] == decision


def test_a_missing_checksum_is_refused_with_a_structured_error_and_never_asked_about(world: CliWorld) -> None:
    """Without the flag a download with no hash fails (exit 3, code checksum-missing with its URL), even interactively."""
    world.publish("tool", "1.0.0", hashed=False)

    result = world.cli("install", "tool", "--json", stdin="y\n")

    error = result.document()["results"][0]["error"]
    assert result.code == 3 and error["code"] == "checksum-missing" and error["url"].endswith("tool-1.0.0.zip")


# ---------------------------------------------------------------------------
# The forwarders
# ---------------------------------------------------------------------------


def stand_in(tmp_path: Path, **launchers: str) -> tuple[RunningGupkg, Path]:
    """Write a gupkg tree with fake launchers (each records its arguments and exits with its code) and describe it."""
    tree = tmp_path / "tree"
    tree.mkdir()
    record = tmp_path / "record.txt"
    for name, code in launchers.items():
        (tree / f"{name}.cmd").write_text(f'@echo off\r\n>> "{record}" echo {name} %*\r\nexit /b {code}\r\n', encoding="ascii")
    return RunningGupkg("source", tree / "gupkg", (tree,)), record


@pytest.mark.windows_integration
@pytest.mark.parametrize(
    ("argv", "recorded", "code"),
    [
        pytest.param(("tui", "C:\\opt\\tool\\1.0"), "gupkg-tui C:\\opt\\tool\\1.0", 7, id="tui"),
        pytest.param(("manager", "tui"), "gupkg-manager tui", 3, id="manager-tui"),
        pytest.param(("manager", "update", "--all", "--mode", "user", "--yes"), "gupkg-manager update --all --mode user --yes", 0, id="manager-update"),
        pytest.param(("manager", "--help"), "gupkg-manager --help", 0, id="help-goes-to-the-program"),
    ],
)
def test_the_forwarders_start_the_launcher_with_the_arguments_and_pass_the_exit_code(
    tmp_path: Path, argv: tuple[str, ...], recorded: str, code: int
) -> None:
    """gupkg <program> <args> runs <program>.cmd of the same tree with those arguments and returns its exit code."""
    location, record = stand_in(tmp_path, **{"gupkg-tui": code, "gupkg-manager": code})

    result = cli.main(list(argv), system=WindowsSystem(), env={}, location=location)

    assert result == code
    assert record.read_text(encoding="ascii").strip() == recorded


def test_a_forwarder_records_the_launcher_and_passes_the_exit_code_on_the_fake_machine(tmp_path: Path) -> None:
    """On the fake machine the launcher path and arguments are recorded and a double's exit status is returned."""
    location, _ = stand_in(tmp_path, **{"gupkg-tui": 0})
    system = FakeSystem(tmp_path / "machine", processes={"gupkg-tui.cmd": lambda call: ProcessResult(9)})

    code = cli.main(["tui", "somewhere"], system=system, env={}, location=location)

    assert code == 9 and system.foreground_log == [[str(location.launcher_dirs[0] / "gupkg-tui.cmd"), "somewhere"]]


@pytest.mark.parametrize("word", [pytest.param(word, id=f"gupkg-{word}") for word in ("tui", "manager")])
def test_a_missing_program_is_a_clear_message_and_exit_2(tmp_path: Path, word: str, capsys: pytest.CaptureFixture[str]) -> None:
    """When the launcher is not in the gupkg tree the command says which file is missing, and starts nothing."""
    location, _ = stand_in(tmp_path)
    system = FakeSystem(tmp_path / "machine")

    code = cli.main([word, "x"], system=system, env={}, location=location)

    assert code == 2 and f"gupkg-{word}.cmd was not found" in capsys.readouterr().err
    assert system.foreground_log == []


def test_a_forwarder_is_not_started_from_an_elevated_shell(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The programs ask for administrator rights themselves, once per batch: an elevated shell does not start them."""
    location, _ = stand_in(tmp_path, **{"gupkg-manager": 0})
    system = FakeSystem(tmp_path / "machine", elevated=True)

    code = cli.main(["manager", "tui"], system=system, env={}, location=location)

    assert code == 2 and "not started from an elevated shell" in capsys.readouterr().err
    assert system.foreground_log == []


def test_a_sandbox_that_is_an_existing_file_is_a_usage_error_not_a_traceback(world: CliWorld, tmp_path: Path) -> None:
    """--sandbox naming a file cannot be made a folder: exit code 2 with a message (and a document under --json), never exit 1."""
    sandbox = tmp_path / "a-file"
    sandbox.write_text("x", encoding="utf-8")

    plain = world.cli("--sandbox", str(sandbox), "list")
    machine = world.cli("--sandbox", str(sandbox), "list", "--json")

    assert plain.code == 2 and "cannot set up the run in --sandbox" in plain.err and "Traceback" not in plain.err
    assert machine.code == 2 and machine.document()["errors"][0]["code"] == "usage"


@pytest.mark.parametrize("word", ["tui", "manager"])
def test_each_ui_forwarder_refuses_an_elevated_shell(tmp_path: Path, capsys: pytest.CaptureFixture[str], word: str) -> None:
    """Both UI programs refuse an elevated shell."""
    location, _ = stand_in(tmp_path, **{f"gupkg-{word}": 0})
    system = FakeSystem(tmp_path / "machine", elevated=True)

    code = cli.main([word, "x"], system=system, env={}, location=location)

    assert code == 2 and "not started from an elevated shell" in capsys.readouterr().err and system.foreground_log == []


def test_root_options_are_not_passed_to_a_forwarded_program(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--settings and --sandbox before a forwarder would silently not reach the program, so they are refused."""
    location, _ = stand_in(tmp_path, **{"gupkg-manager": 0})
    system = FakeSystem(tmp_path / "machine")

    code = cli.main(["--sandbox", str(tmp_path / "sb"), "manager", "tui"], system=system, env={}, location=location)

    assert code == 2 and "not passed on" in capsys.readouterr().err and system.foreground_log == []
