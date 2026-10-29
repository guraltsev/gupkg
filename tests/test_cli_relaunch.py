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

from gupkg.model.scope import InputNeeded, make_scope
from gupkg.model.settings import default_layer, merge_layers
from gupkg.relaunch import ElevationRefused, ensure_elevation
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ElevatedRun, Mode, elevated_command, unsafe_relaunch_argument
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
    return make_scope(Mode(mode), Path("C:\\opt"), "test", merge_layers([default_layer(mode)]), env)


def _refuse_to_ask(question: str) -> bool:
    """Fail the test: no question may be asked before a hostile command line is refused."""
    raise AssertionError(f"asked: {question}")


@pytest.mark.parametrize("value", [pytest.param(value, id=key) for key, value in HOSTILE.items()])
def test_the_parent_refuses_a_hostile_argument_before_asking_or_starting_anything(tmp_path: Path, value: str) -> None:
    """Every shell-significant or control character in the child's command line is exit code 2, with no question and no relaunch."""
    system = FakeSystem(tmp_path / "machine")

    with pytest.raises(ElevationRefused) as raised:
        ensure_elevation(
            [_context("system")], system=system, argv=["install", f"{value}", "--yes"],
            version_dir=tmp_path / "opt" / "gupkg" / "1.0.0", root=tmp_path / "opt", confirm=_refuse_to_ask,
        )

    assert raised.value.exit_code == 2 and "not allowed" in str(raised.value)
    assert system.relaunches == [] and system.relaunch_commands == []


@pytest.mark.parametrize("value", [pytest.param(value, id=key) for key, value in HOSTILE.items()])
def test_the_child_refuses_the_same_characters_in_its_own_command_line(world: CliWorld, value: str) -> None:
    """A relaunched child refuses a hostile target at argument validation, whatever the parent did, and changes nothing."""
    world.fake.elevated = True

    result = world.cli("install", f"{value}", "--relaunched", "--yes", "--json")

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


@pytest.mark.parametrize("target", ["--yes", "--no-input", "--mode=user", "-x", "-"])
def test_a_target_that_starts_with_a_dash_is_refused_by_the_parent_and_by_the_child(world: CliWorld, target: str) -> None:
    """A package folder named like an option is exit code 2 before any relaunch, and an elevated child refuses it as well."""
    world.publish(target, "1.0.0", mode="system")

    parent = world.cli("install", "--yes", "--json", "--", target)

    assert parent.code == 2, parent.out + parent.err
    assert world.fake.relaunches == []
    world.fake.elevated = True
    child = world.cli("install", "--relaunched", "--yes", "--", target)
    assert child.code == 2 and "never starts with '-'" in child.err


def test_a_command_line_that_is_too_long_is_refused_with_a_reason_on_the_fake_boundary_too(tmp_path: Path) -> None:
    """More than 30000 characters of arguments are `refused` (the CLI turns that into exit code 2), never `not accepted`."""
    fake = FakeSystem(tmp_path / "machine")

    over = fake.relaunch_elevated(["install", "x" * 31000])

    assert over.started is False and "characters long" in (over.refused or "") and fake.relaunches == []


@pytest.mark.parametrize(
    ("options", "pauses"),
    [
        pytest.param((), True, id="a-person-at-a-console-sees-the-window-stay"),
        pytest.param(("--yes",), False, id="yes-never-pauses"),
        pytest.param(("--no-input",), False, id="no-input-never-pauses"),
    ],
)
def test_the_elevated_child_pauses_only_for_a_person_at_a_console(
    world: CliWorld, monkeypatch: pytest.MonkeyPatch, options: tuple[str, ...], pauses: bool
) -> None:
    """A scripted `--yes` or `--no-input` run never waits for a key; a console run without them does (so the window stays readable)."""
    pressed: list[int] = []
    monkeypatch.setattr("gupkg.cli.wait_for_keypress", lambda: pressed.append(1))
    world.fake.elevated = True

    result = world.cli("install", "--relaunched", *options, "--", "tool", tty=True)

    assert result.code == 0, result.err
    assert pressed == ([1] if pauses else [])


def test_the_elevated_child_does_not_pause_when_standard_input_is_not_a_console(world: CliWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirected standard input is not a console: a scripted caller is never blocked."""
    pressed: list[int] = []
    monkeypatch.setattr("gupkg.cli.wait_for_keypress", lambda: pressed.append(1))
    world.fake.elevated = True

    result = world.cli("install", "--relaunched", "--", "tool", tty=False)

    assert result.code == 0 and pressed == []


def test_the_command_line_is_quoted_by_the_rules_of_createprocess() -> None:
    """A path with spaces is one quoted argument; nothing else is added or interpreted."""
    program, parameters, directory = elevated_command(Path("C:/Program Files/gupkg/1.0.0/gupkg"), ["install", "my tool"])

    assert program.name == "python.exe" and directory.name == "gupkg"
    assert parameters == '-I -s -B -S "C:\\Program Files\\gupkg\\1.0.0\\gupkg\\bootstrap.py" install "my tool"'


@pytest.mark.parametrize(("value", "plain"), [("Visual Studio", True), ("C:\\opt\\tool\\1.0.0", True), ("a&b", False), ("a b%", False)])
def test_plain_values_pass_and_only_shell_significant_ones_are_refused(value: str, plain: bool) -> None:
    """Spaces, drive colons and backslashes are plain; ``&`` and ``%`` are not."""
    assert (unsafe_relaunch_argument(value) is None) is plain


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

    accepted = world.cli("install", "tool", "--relaunched", "--yes", "--stop-file", str(good), "--events-file", "one.jsonl")
    ignored = world.cli("install", "tool", "--relaunched", "--yes", "--stop-file", str(other), "--events-file", "two.jsonl")

    first = json.loads((world.system_root / ".gupkg" / "logs" / "one.jsonl").read_text(encoding="utf-8").splitlines()[0])
    second = json.loads((world.system_root / ".gupkg" / "logs" / "two.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert accepted.code == 0 and ignored.code == 0
    assert first["stop_file"] == str(good) and second["stop_file"] not in (str(other), str(good))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("gupkg-stop-" + "a" * 31, id="one-digit-short"), pytest.param("gupkg-stop-" + "a" * 33, id="one-digit-long"),
        pytest.param("gupkg-stop-" + "A" * 32, id="upper-case-hex"), pytest.param("gupkg-stop-" + "g" * 32, id="not-hex"),
        pytest.param("other-stop-" + "a" * 32, id="other-prefix"), pytest.param("gupkg-stop-" + "a" * 32 + ".txt", id="extension"),
        pytest.param("gupkg-stop-" + "a" * 32 + " ", id="trailing-space"), pytest.param("gupkg-stop-", id="no-id"),
    ],
)
def test_the_child_ignores_a_stop_marker_whose_name_has_the_wrong_shape_even_in_its_own_folder(world: CliWorld, name: str) -> None:
    """Only `gupkg-stop-<32 lower-case hex>` is accepted from the parent; any other name in the right folder gets a fresh marker."""
    world.fake.elevated = True
    given = world.temp / name

    result = world.cli("install", "--relaunched", "--yes", "--events-file", "w.jsonl", "--stop-file", str(given), "--", "tool")

    first = json.loads((world.system_root / ".gupkg" / "logs" / "w.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert result.code == 0, result.err
    assert first["stop_file"] != str(given) and re.fullmatch(r"gupkg-stop-[0-9a-f]{32}", Path(first["stop_file"]).name)


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
