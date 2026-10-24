"""Cover the elevated relaunch: the command line that reaches the child, the characters refused on the way, and the wait.

Behavior covered: the elevated child is the bundled ``python.exe`` running ``bootstrap.py`` with fixed isolation flags, never
``cmd.exe`` (threat T1: a target such as ``zzz" & calc & "`` must not reach any shell); the parent refuses every target or option
value that holds ``& | < > ^ " % ! ```, a control character or a lone surrogate before anything is asked or started (exit code
2), and the child refuses the same characters in its own command line; the parent waits for the child and returns the child's
exit code, reports ``relaunched: true``, reports a missing ``run_end`` line in the events file as a failure, a declined
prompt as exit code 5, and an interrupted wait as exit code 5 after asking the child to stop through the stop marker.

Real: the argument parser, ``ensure_elevation``, the run session and the real wait loop (``wait_for_child``) with a scripted
process. Mocked: ``FakeSystem`` (elevation flag, the recorded relaunch command, the child's exit code).
Out of scope: the real ``ShellExecuteEx`` call and UAC (``tests/manual_smoke.md``, issue 029 item 11).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from gupkg.model.mode import InputNeeded, ModeChoice, build_mode_context
from gupkg.model.settings import default_layer, merge_layers
from gupkg.relaunch import ElevationRefused, ensure_elevation
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ElevatedRun, elevated_command, unsafe_relaunch_argument
from gupkg.system import windows
from gupkg.system.windows import WindowsSystem, wait_for_child
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

HOSTILE = {
    "quote-and-ampersand": 'zzz" & calc & "',
    "ampersand": "a&calc",
    "pipe": "a|b",
    "percent-variable": "%COMSPEC%",
    "caret": "^&",
    "delayed-expansion": "!x!",
    "backslash-before-quote": 'x\\" & calc',
    "newline": "a\nb",
    "nul": "a\x00b",
    "redirect-in": "a<b",
    "redirect-out": "a>b",
    "backtick": "a`b",
    "lone-surrogate": "a\ud800b",
    "tab": "a\tb",
    "c1-control": "a\u0085b",
    "bidi-override": "a\u202eb",
    "bidi-isolate": "a\u2066b",
    "line-separator": "a\u2028b",
    "paragraph-separator": "a\u2029b",
    "zero-width-joiner": "a\u200db",
    "private-use": "a\ue000b",
    "unassigned": "a\u0378b",
}
# Names that a folder can really have on Windows, so that a package of that name can exist and be targeted.
POSSIBLE_FOLDER_NAMES = {key: HOSTILE[key] for key in ("ampersand", "percent-variable", "caret", "delayed-expansion", "backtick")}


@pytest.fixture(autouse=True)
def never_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the one call that can show a real UAC prompt fail the test, so no test here can ever elevate for real."""
    def forbidden(info) -> bool:
        """Fail: a test must never reach ShellExecuteEx."""
        raise AssertionError("ShellExecuteEx was called: a test would have shown a real UAC prompt")

    monkeypatch.setattr(windows, "_shell_execute", forbidden)


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A normal (non-elevated) fake machine whose stand-in gupkg is installed in the admin-only system root."""
    machine = CliWorld(tmp_path)
    machine.publish("tool", "1.0.0", mode="system")
    return machine


def _context(mode: str):
    """Return the mode context of *mode* built from the built-in settings."""
    env = {"USERPROFILE": "C:\\Users\\pat", "APPDATA": "C:\\Users\\pat\\AppData\\Roaming", "ProgramData": "C:\\ProgramData"}
    return build_mode_context(ModeChoice(mode, "test"), merge_layers([default_layer()]), env)


def _refuse_to_ask(question: str) -> bool:
    """Fail the test: no question may be asked before a hostile command line is refused."""
    raise AssertionError(f"asked: {question}")


@pytest.mark.parametrize("value", [pytest.param(value, id=key) for key, value in HOSTILE.items()])
def test_the_parent_refuses_a_hostile_argument_before_asking_or_starting_anything(tmp_path: Path, value: str) -> None:
    """Every shell-significant or control character in the child's command line is exit code 2, with no question and no relaunch."""
    system = FakeSystem(tmp_path / "machine")

    with pytest.raises(ElevationRefused) as raised:
        ensure_elevation(
            [_context("system")], system=system, argv=["install", f"system:{value}", "--yes"],
            version_dir=tmp_path / "opt" / "gupkg" / "1.0.0", root=tmp_path / "opt", confirm=_refuse_to_ask,
        )

    assert raised.value.exit_code == 2 and "not allowed" in str(raised.value)
    assert system.relaunches == [] and system.relaunch_commands == []


@pytest.mark.parametrize("value", [pytest.param(value, id=key) for key, value in POSSIBLE_FOLDER_NAMES.items()])
def test_a_package_with_a_hostile_folder_name_is_refused_by_the_command_line_before_any_relaunch(world: CliWorld, value: str) -> None:
    """Through the real command line: a system package whose name holds ``& % ! ^`` is exit code 2 and nothing is relaunched."""
    world.publish(value, "1.0.0", mode="system")

    result = world.cli("install", f"system:{value}", "--yes", "--json")

    assert result.code == 2, result.out + result.err
    assert world.fake.relaunches == [] and world.fake.relaunch_commands == []
    assert "not allowed in a command that runs with administrator rights" in result.document()["results"][0]["error"]["message"]


@pytest.mark.parametrize("value", [pytest.param(value, id=key) for key, value in HOSTILE.items()])
def test_the_child_refuses_the_same_characters_in_its_own_command_line(world: CliWorld, value: str) -> None:
    """A relaunched child refuses a hostile target at argument validation, whatever the parent did, and changes nothing."""
    world.fake.elevated = True

    result = world.cli("install", f"system:{value}", "--relaunched", "--yes", "--json")

    assert result.code == 2 and "the elevated child refuses" in result.err
    assert not (world.system_root / "tool" / "current").exists() and world.fake.relaunches == []


@pytest.mark.parametrize("value", [pytest.param(value, id=key) for key, value in HOSTILE.items()])
def test_the_system_boundary_refuses_hostile_arguments_itself(tmp_path: Path, value: str) -> None:
    """``FakeSystem`` (like ``WindowsSystem``) starts nothing and records nothing for a hostile argument."""
    fake = FakeSystem(tmp_path / "machine")

    result = fake.relaunch_elevated(["install", value])

    assert result == ElevatedRun(False) and fake.relaunches == []
    assert WindowsSystem().relaunch_elevated(["install", value]) == ElevatedRun(False)


def _package_tree(base: Path) -> Path:
    """Write a bundled gupkg package folder (python, its dll and search-path file, site-packages, bootstrap.py) under *base*."""
    package = base / "pkg" / "gupkg"
    (package / "python" / "Lib" / "site-packages").mkdir(parents=True)
    for name in ("python.exe", "python312.dll", "python312._pth"):
        (package / "python" / name).write_text("x", encoding="utf-8")
    (package / "bootstrap.py").write_text("x", encoding="utf-8")
    return package


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param(".", id="package-folder"), pytest.param("..", id="its-parent"), pytest.param("python", id="runtime-folder"),
        pytest.param("python/Lib/site-packages", id="site-packages"), pytest.param("python/python.exe", id="python-exe"),
        pytest.param("python/python312.dll", id="python-dll"), pytest.param("python/python312._pth", id="search-path-file"),
        pytest.param("bootstrap.py", id="bootstrap"),
    ],
)
def test_every_file_the_child_loads_first_must_be_admin_only_before_anything_is_asked(world: CliWorld, tmp_path: Path, relative: str) -> None:
    """One user-writable entry of the child's first-loaded set is exit code 2 with no question and no relaunch recorded."""
    package = _package_tree(tmp_path)
    world.fake.package_dir = package
    world.fake.untrusted_dirs.add((package / relative).resolve())

    result = world.cli("install", "system:tool", "--yes", "--json")

    assert result.code == 2, result.out + result.err
    assert "The elevated gupkg is not started" in result.document()["results"][0]["error"]["message"]
    assert world.fake.relaunches == [] and world.fake.relaunch_commands == []


def test_a_complete_admin_only_package_tree_is_accepted(world: CliWorld, tmp_path: Path) -> None:
    """The same tree with nothing untrusted starts the child (so the refusals above are about the entries, not the tree)."""
    world.fake.package_dir = _package_tree(tmp_path)

    result = world.cli("install", "system:tool", "--yes")

    assert result.code == 0, result.out + result.err
    assert len(world.fake.relaunches) == 1


@pytest.mark.parametrize("target", ["--yes", "--no-input", "--mode=user", "-x", "-"])
def test_a_target_that_starts_with_a_dash_is_refused_by_the_parent_and_by_the_child(world: CliWorld, target: str) -> None:
    """A package folder named like an option is exit code 2 before any relaunch, and an elevated child refuses it as well."""
    world.publish(target, "1.0.0", mode="system")

    parent = world.cli("install", "--mode", "system", "--yes", "--json", "--", target)

    assert parent.code == 2, parent.out + parent.err
    assert world.fake.relaunches == []
    world.fake.elevated = True
    child = world.cli("install", "--mode", "system", "--relaunched", "--yes", "--", target)
    assert child.code == 2 and "never starts with '-'" in child.err


def test_the_targets_follow_a_double_dash_so_they_cannot_be_options(world: CliWorld) -> None:
    """The child's command line is the options first, then `--`, then the targets."""
    world.cli("install", "system:tool", "--yes")

    [argv] = world.fake.relaunches
    separator = argv.index("--")
    assert argv[separator + 1:] == ["system:tool"] and not [item for item in argv[:separator] if item.startswith("system:")]


def test_a_failure_while_waiting_after_the_child_started_is_exit_3_with_a_clear_message(world: CliWorld) -> None:
    """The child may still be running: the call is a failure (exit 3) that says so, never `the prompt was not accepted`."""
    world.fake.relaunch_error = "waiting for the elevated gupkg failed (WaitForSingleObject returned 0xffffffff)"

    result = world.cli("install", "system:tool", "--yes", "--events-file", "run.jsonl", "--json")

    message = result.document()["results"][0]["error"]["message"]
    assert result.code == 3 and "may still be running" in message and "not accepted" not in message
    assert "gupkg-stop-" in message and "run.jsonl" in message


def test_a_command_line_that_is_too_long_is_refused_with_a_reason_on_the_fake_boundary_too(tmp_path: Path) -> None:
    """More than 30000 characters of arguments are `refused` (the CLI turns that into exit code 2), never `not accepted`."""
    fake = FakeSystem(tmp_path / "machine")

    over = fake.relaunch_elevated(["install", "x" * 31000])

    assert over.started is False and "characters long" in (over.refused or "") and fake.relaunches == []


def test_the_abandoned_wait_names_the_stop_marker_and_the_events_file(world: CliWorld) -> None:
    """When the wait is given up the message says where the child's marker and events file are."""
    world.fake.relaunch_interrupt = True

    result = world.cli("install", "system:tool", "--yes", "--events-file", "run.jsonl", "--json")

    message = result.document()["results"][0]["error"]["message"]
    assert result.code == 5 and "gupkg-stop-" in message and "run.jsonl" in message


def test_the_elevated_child_is_the_bundled_python_running_bootstrap_and_never_cmd(world: CliWorld) -> None:
    """The program, parameters and folder the ShellExecute call would receive: bundled python.exe, isolation flags, bootstrap.py."""
    result = world.cli("install", "system:tool", "--yes")

    assert result.code == 0, result.out + result.err
    [(program, parameters, directory)] = world.fake.relaunch_commands
    package = world.fake.package_dir
    assert Path(program) == package / "python" / "python.exe" and Path(directory) == package
    assert parameters.startswith(f'-I -s -B -S "{package / "bootstrap.py"}" install --yes') and parameters.endswith(" -- system:tool")
    assert "cmd" not in Path(program).name.lower() and "/c" not in parameters.split() and "cmd.exe" not in parameters.lower()


def test_the_command_line_is_quoted_by_the_rules_of_createprocess() -> None:
    """A path with spaces is one quoted argument; nothing else is added or interpreted."""
    program, parameters, directory = elevated_command(Path("C:/Program Files/gupkg/1.0.0/gupkg"), ["install", "system:my tool"])

    assert program.name == "python.exe" and directory.name == "gupkg"
    assert parameters == '-I -s -B -S "C:\\Program Files\\gupkg\\1.0.0\\gupkg\\bootstrap.py" install "system:my tool"'


@pytest.mark.parametrize(("value", "plain"), [("system:Visual Studio", True), ("C:\\opt\\tool\\1.0.0", True), ("a&b", False), ("a b%", False)])
def test_plain_values_pass_and_only_shell_significant_ones_are_refused(value: str, plain: bool) -> None:
    """Spaces, drive colons and backslashes are plain; ``&`` and ``%`` are not."""
    assert (unsafe_relaunch_argument(value) is None) is plain


def test_the_parent_waits_and_returns_the_childs_exit_code(world: CliWorld) -> None:
    """A child that ends with exit code 3 makes the parent's exit code 3, and the document says relaunched with that code."""
    world.fake.relaunch_exit_code = 3

    result = world.cli("install", "system:tool", "--yes", "--json")

    document = result.document()
    assert result.code == 3 and document["relaunched"] is True and document["exit_code"] == 3 and document["ok"] is False
    assert "exit code 3" in result.err


@pytest.mark.parametrize(
    ("raw", "mapped"),
    [pytest.param(code, code, id=f"passes-{code}") for code in range(0, 7)]
    + [
        pytest.param(7, 4, id="7"), pytest.param(259, 4, id="still-active-259"), pytest.param(0xC000012D, 4, id="out-of-resources"),
        pytest.param(0xFFFFFFFF, 4, id="all-ones"), pytest.param(-1, 4, id="minus-one"),
    ],
)
def test_the_childs_exit_code_is_clamped_to_gupkgs_codes_and_the_raw_code_is_kept(world: CliWorld, raw: int, mapped: int) -> None:
    """Only 0 to 6 pass through; any other process exit code is exit code 4 (the raw value is in `child_raw_exit_code`)."""
    world.fake.relaunch_exit_code = raw

    result = world.cli("install", "system:tool", "--yes", "--json")

    document = result.document()
    assert result.code == mapped and document["exit_code"] == mapped and document["child_raw_exit_code"] == raw
    assert document["relaunched"] is True and document["ok"] is (mapped == 0)


def test_a_child_that_ends_well_is_exit_0_when_it_wrote_its_run_end(world: CliWorld) -> None:
    """With ``--events-file`` the parent reads the child's last line: a ``run_end`` makes the success a success."""
    events = world.system_root / ".gupkg" / "logs" / "run.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps({"event": "run_start"}) + "\n" + json.dumps({"event": "run_end", "exit_code": 0}) + "\n", encoding="utf-8")

    result = world.cli("install", "system:tool", "--yes", "--events-file", "run.jsonl", "--json")

    document = result.document()
    assert result.code == 0 and document["relaunched"] is True and "errors" not in document


@pytest.mark.parametrize("content", [None, "", json.dumps({"event": "phase_start"}) + "\n", "not json\n"], ids=["absent", "empty", "no-run-end", "garbage"])
def test_a_child_that_left_no_run_end_is_a_failure_with_a_clear_error(world: CliWorld, content: str | None) -> None:
    """A missing, empty or unfinished events file after the child exited means the run is not known to have finished: exit code 3."""
    if content is not None:
        events = world.system_root / ".gupkg" / "logs" / "run.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(content, encoding="utf-8")

    result = world.cli("install", "system:tool", "--yes", "--events-file", "run.jsonl", "--json")

    document = result.document()
    assert result.code == 3 and document["ok"] is False and document["errors"][0]["code"] == "child-no-result"


def test_a_child_that_was_killed_is_its_exit_code(world: CliWorld) -> None:
    """A killed child ends with a non-zero code that the parent passes on (here 1), not with success."""
    world.fake.relaunch_exit_code = 1

    result = world.cli("install", "system:tool", "--yes", "--json")

    assert result.code == 1
    assert result.document()["relaunched"] is True and result.document()["exit_code"] == 1


def test_a_declined_uac_prompt_is_exit_5_and_nothing_is_waited_for(world: CliWorld) -> None:
    """When the elevation prompt is not accepted the call is exit code 5 with the message, and no result of a child exists."""
    world.fake.relaunch_result = False

    result = world.cli("install", "system:tool", "--yes", "--json")

    assert result.code == 5 and result.document()["relaunched"] is False
    assert "elevation prompt was not accepted" in result.document()["results"][0]["error"]["message"]


def test_an_interrupted_wait_asks_the_child_to_stop_and_gives_up_with_exit_5(world: CliWorld) -> None:
    """Ctrl-C while waiting: the parent's stop marker (announced to the child as --stop-file) is created, and the wait ends as exit 5."""
    world.fake.relaunch_interrupt = True

    result = world.cli("install", "system:tool", "--yes", "--json")

    [argv] = world.fake.relaunches
    marker = Path(argv[argv.index("--stop-file") + 1])
    assert re.fullmatch(r"gupkg-stop-[0-9a-f]{32}", marker.name) and marker.parent == world.temp
    assert result.code == 5 and "Stopped waiting for the elevated gupkg" in result.document()["results"][0]["error"]["message"]


def test_the_interrupt_callback_is_what_creates_the_stop_marker(tmp_path: Path) -> None:
    """``ensure_elevation`` hands the engine's ``request_stop`` to the system call, which runs it on an interrupt."""
    system = FakeSystem(tmp_path / "machine", relaunch_interrupt=True)
    asked: list[str] = []

    with pytest.raises(InputNeeded):
        ensure_elevation(
            [_context("system")], system=system, argv=["install", "tool"], version_dir=tmp_path / "opt" / "gupkg" / "1.0.0",
            root=tmp_path / "opt", yes=True, on_interrupt=lambda: asked.append("stop"),
        )

    assert asked == ["stop"]


def test_the_child_accepts_the_parents_stop_marker_only_as_a_fresh_name_in_its_own_temp_folder(world: CliWorld, tmp_path: Path) -> None:
    """A relaunched child uses ``--stop-file`` when it is ``gupkg-stop-<32 hex>`` in its own TEMP, and ignores any other path."""
    world.fake.elevated = True
    good = world.temp / ("gupkg-stop-" + "a" * 32)
    other = tmp_path / ("gupkg-stop-" + "b" * 32)

    accepted = world.cli("install", "system:tool", "--relaunched", "--yes", "--stop-file", str(good), "--events-file", "one.jsonl")
    ignored = world.cli("install", "system:tool", "--relaunched", "--yes", "--stop-file", str(other), "--events-file", "two.jsonl")

    first = json.loads((world.system_root / ".gupkg" / "logs" / "one.jsonl").read_text(encoding="utf-8").splitlines()[0])
    second = json.loads((world.system_root / ".gupkg" / "logs" / "two.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert accepted.code == 0 and ignored.code == 0
    assert first["stop_file"] == str(good) and second["stop_file"] not in (str(other), str(good))


class _Process:
    """A scripted child process: ``done`` says it ended after *steps* waits; *interrupts* raise KeyboardInterrupt on given waits."""

    def __init__(self, steps: int, interrupts: tuple[int, ...] = ()) -> None:
        """Script a child that ends after *steps* waits, interrupted on the waits in *interrupts*."""
        self.steps, self.interrupts, self.waits = steps, interrupts, 0
        self.now = 0.0

    def done(self, seconds: float) -> bool:
        """Wait once (the clock moves by *seconds*) and report whether the child ended."""
        self.waits += 1
        self.now += seconds
        if self.waits in self.interrupts:
            raise KeyboardInterrupt
        return self.waits >= self.steps


def test_the_wait_loop_returns_the_exit_code_when_the_child_ends() -> None:
    """Waiting ends when the child does, and the exit code is read exactly then."""
    process = _Process(steps=3)

    run = wait_for_child(process.done, lambda: 7, on_interrupt=None, grace=5.0, clock=lambda: process.now)

    assert run == ElevatedRun(True, 7) and process.waits == 3


def test_the_first_interrupt_asks_for_a_stop_and_the_child_may_still_end_inside_the_grace_period() -> None:
    """Ctrl-C calls ``on_interrupt`` once and keeps waiting: a child that stops in time gives its own exit code."""
    process = _Process(steps=6, interrupts=(2,))
    asked: list[int] = []

    run = wait_for_child(process.done, lambda: 5, on_interrupt=lambda: asked.append(1), grace=5.0, clock=lambda: process.now)

    assert run == ElevatedRun(True, 5) and asked == [1]


def test_a_second_interrupt_or_the_end_of_the_grace_period_abandons_the_wait() -> None:
    """The wait is bounded: a second Ctrl-C or the grace period running out returns ``abandoned`` with no exit code."""
    second = _Process(steps=10**6, interrupts=(2, 3))
    slow = _Process(steps=10**6, interrupts=(2,))

    after_second = wait_for_child(second.done, lambda: 0, on_interrupt=lambda: None, grace=60.0, clock=lambda: second.now)
    after_grace = wait_for_child(slow.done, lambda: 0, on_interrupt=lambda: None, grace=1.0, clock=lambda: slow.now)

    assert after_second == ElevatedRun(True, None, True) and after_grace == ElevatedRun(True, None, True)
    assert slow.waits < 10
