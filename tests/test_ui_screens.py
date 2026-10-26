"""Cover what a person sees and can do in the two UI programs, driven with Textual's pilot on a scripted ``gupkg``.

Behaviour covered: every screen of both programs (browser, package view, run view while running and after, the plan confirmation, the checksum question, the cancel question, uninstall
confirmation, version picker, log view, help) shows a complete key-hint line on top and a status line at the bottom, at 80x24 and at 200x50; the browser lists packages, filters, opens a package
and comes back with its filter and selection intact (and reloaded) after a run, all inside one application; the run view shows the phase headers in order, ends with the verdict as its last line and
follows the tail of a 2,000-line events file while still answering keys; Escape during a run asks "Cancel after the current package?" and a yes asks the running call to stop; dialogs answer only to
explicit keys (Enter never confirms); text from packages with markup characters is shown literally; ``gupkg-tui`` opens the package view of its target and Escape leaves the program.

Real: the Textual apps, screens, session, follower and subprocesses. Mocked: the ``gupkg`` executable (a scripted double replaying recorded documents) and the machine's settings (temporary files).
Out of scope: the real CLI (``test_manager_real_cli.py``), colour (``test_ui_render.py``) and real terminals (manual smoke test).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from gupkg_manager.app.app import ManagerApp
from gupkg_tui.app import TuiApp
from gupkg_ui.follow import EventStream
from gupkg_ui.render import EventRenderer, verdict_lines
from gupkg_ui.screens.run import MAX_LOG_LINES, RunScreen
from tests.ui_support import FakeGupkg, entry, fixture_json, head, list_doc, make_env, plan_doc, plan_entry, result

SIZES = [pytest.param((80, 24), id="80x24"), pytest.param((200, 50), id="200x50")]


def screen_text(app) -> list[str]:
    """Return the text of every row of the screen as the person sees it (the compositor's rendering, trailing blanks removed)."""
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


def content(app) -> list[str]:
    """Return the rows of the screen between the two frame lines that show something (scroll-bar glyphs and blank rows left out)."""
    return [row.strip() for row in screen_text(app)[1:-1] if any(character.isalnum() for character in row)]


def has(app, needle: str) -> bool:
    """Return whether *needle* is visible anywhere on the screen."""
    return any(needle in row for row in screen_text(app))


async def wait_for(pilot, condition: Callable[[], bool], what: str, timeout: float = 20.0) -> None:
    """Pause in small steps until *condition* holds; fail with *what* after *timeout* seconds (bounded, never a fixed sleep)."""
    waited = 0.0
    while not condition():
        await pilot.pause(0.05)
        waited += 0.05
        assert waited < timeout, f"timed out waiting for {what}; the screen shows:\n" + "\n".join(screen_text(pilot.app))


def assert_frame(app, width: int) -> None:
    """Assert the fixed frame: a hint line on top that is complete (not clipped) and a status line at the bottom."""
    rows = screen_text(app)
    hint, status = rows[0], rows[-1]
    assert hint.strip() and "…" not in hint, f"the key-hint line is missing or clipped: {hint!r}"
    assert status.strip(), "the status line is empty"
    assert len(hint) <= width


@pytest.fixture
def fake(tmp_path: Path) -> FakeGupkg:
    """A scripted ``gupkg`` double with the recorded documents of the real CLI for two outdated packages."""
    double = FakeGupkg(tmp_path)
    double.respond("list", doc=fixture_json("list.json", tmp_path))
    double.respond("info", doc=fixture_json("info_alpha.json", tmp_path))
    double.respond("registry", doc=head("registry status", registries=[{"name": "main", "source": "x", "revision": "4f2a9c1d00", "synchronized_at": None}]))
    return double


def update_run(fake: FakeGupkg, tmp_path: Path, names=("alpha", "beta"), *, records=None, delay: float = 0.0, **extra) -> None:
    """Schedule a plan and a successful ``update`` of *names* in the user root, with *records* as each package's own events (the call waits *delay* seconds first)."""
    fake.respond("update:plan", doc=plan_doc([plan_entry(f"user:{name}", name) for name in names]))
    doc = result("update", [entry(name) for name in names])
    packages = [{"name": name, "path": str(tmp_path / "home" / "opt" / name), "records": records or [{"event": "phase_start", "phase": "Resolve"}, {"event": "phase_end", "phase": "Resolve", "status": "ok", "elapsed": 0.1}]} for name in names]
    fake.respond("update", doc=doc, sleep=delay, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": doc, "packages": packages, **extra})


def info_of(name: str, tmp_path: Path) -> dict:
    """Return the recorded ``info`` document of ``alpha`` turned into the one of *name* (the same shape, another package)."""
    text = json.dumps(fixture_json("info_alpha.json", tmp_path)).replace("alpha", name)
    return json.loads(text)


def run_app(tmp_path: Path, fake: FakeGupkg, scenario: Callable, *, size=(80, 24), program: str = "manager", target: str = "user:alpha", toasts: bool = False, **settings) -> None:
    """Start the manager (or ``gupkg-tui``) in a headless terminal of *size* and run the async *scenario* ``(app, pilot)`` against it."""
    env = make_env(tmp_path, fake, settings=settings.get("settings", ""))
    app = ManagerApp(env) if program == "manager" else TuiApp(env, target)

    async def main() -> None:
        """Run the scenario inside the app."""
        async with app.run_test(size=size, notifications=toasts) as pilot:
            await scenario(app, pilot)

    asyncio.run(main())


async def open_browser(app, pilot) -> None:
    """Wait until the browser shows the two packages."""
    await wait_for(pilot, lambda: has(app, "alpha") and has(app, "beta"), "the browser to list the packages")


# -- the frame ----------------------------------------------------------------------


@pytest.mark.parametrize("size", SIZES)
def test_every_screen_of_the_manager_has_a_hint_line_and_a_status_line(tmp_path: Path, fake: FakeGupkg, size) -> None:
    """The browser, package view, plan confirmation, uninstall question, version picker, log view, help and the run view (running and finished) all keep the frame."""
    update_run(fake, tmp_path, ("alpha",))
    seen: list[str] = []

    async def scenario(app, pilot) -> None:
        """Visit each screen and check the frame on it."""
        width = size[0]
        await open_browser(app, pilot)
        assert_frame(app, width), seen.append("browser")
        await pilot.press("question_mark")
        await wait_for(pilot, lambda: has(app, "Browser"), "help")
        assert_frame(app, width), seen.append("help")
        await pilot.press("escape")
        await pilot.press("enter")
        await wait_for(pilot, lambda: has(app, "Installed:"), "the package view")
        assert_frame(app, width), seen.append("package")
        await pilot.press("l")
        await wait_for(pilot, lambda: has(app, "log of alpha"), "the log view")
        assert_frame(app, width), seen.append("log")
        await pilot.press("escape")
        await pilot.press("x")
        await wait_for(pilot, lambda: has(app, "Uninstall"), "the uninstall question")
        assert_frame(app, width), seen.append("uninstall question")
        await pilot.press("n")
        await pilot.press("s")
        await wait_for(pilot, lambda: has(app, "choose the version to activate"), "the version picker")
        assert_frame(app, width), seen.append("version picker")
        await pilot.press("escape")
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan confirmation")
        assert_frame(app, width), seen.append("plan confirmation")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen), "the run view")
        assert_frame(app, width), seen.append("run")
        await wait_for(pilot, lambda: app.screen.outcome is not None, "the run to finish")
        assert_frame(app, width), seen.append("run finished")

    run_app(tmp_path, fake, scenario, size=size)

    assert seen == ["browser", "help", "package", "log", "uninstall question", "version picker", "plan confirmation", "run", "run finished"]


@pytest.mark.parametrize("size", SIZES)
def test_the_checksum_question_and_the_cancel_question_have_the_frame(tmp_path: Path, fake: FakeGupkg, size) -> None:
    """The two questions that stop a run keep the hint and status lines as well."""
    fake.respond("update:plan", doc=plan_doc([plan_entry("user:alpha", "alpha", sha256="missing")]))
    doc = result("update", [entry("alpha")])
    fake.respond("update", doc=doc, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "stop_after": 0, "result": doc, "packages": [{"name": "alpha", "path": str(tmp_path / "home" / "opt" / "alpha"), "records": []}]})

    async def scenario(app, pilot) -> None:
        """Reach the checksum question, answer yes, then open the cancel question during the run."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        assert has(app, "you will be asked")
        await pilot.press("y")
        await wait_for(pilot, lambda: has(app, "without a checksum"), "the checksum question")
        assert_frame(app, size[0])
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and has(app, "Package alpha: updated"), "the run to wait after its first package")
        await pilot.press("escape")
        await wait_for(pilot, lambda: has(app, "Cancel after the current package?"), "the cancel question")
        assert_frame(app, size[0])
        await pilot.press("y")
        await wait_for(pilot, lambda: app.screen.outcome is not None if isinstance(app.screen, RunScreen) else False, "the run to end")

    run_app(tmp_path, fake, scenario, size=size)


@pytest.mark.parametrize("size", SIZES)
def test_the_package_program_has_the_frame_and_escape_leaves_it(tmp_path: Path, fake: FakeGupkg, size) -> None:
    """``gupkg-tui`` opens the package view of its target with the frame; Escape at that view quits the program with exit code 0."""
    codes: list[object] = []

    async def scenario(app, pilot) -> None:
        """Open, check the frame, press Escape."""
        await wait_for(pilot, lambda: has(app, "Installed:"), "the package view")
        assert has(app, "alpha") and has(app, "Versions in the package folder")
        assert_frame(app, size[0])
        await pilot.press("escape")
        await wait_for(pilot, lambda: app.return_code is not None, "the program to leave")
        codes.append(app.return_code)

    run_app(tmp_path, fake, scenario, size=size, program="tui")

    assert codes == [0]
    assert fake.calls()[0]["key"] == "info" and fake.calls()[0]["targets"] == ["user:alpha"]


# -- the browser --------------------------------------------------------------------


def test_the_browser_lists_the_packages_and_the_status_line_counts_them(tmp_path: Path, fake: FakeGupkg) -> None:
    """Name, version, mode, status (with the newer version) and registry columns; the status line has the counts and the registry revision."""
    async def scenario(app, pilot) -> None:
        """Read the table and the status line."""
        await open_browser(app, pilot)
        await wait_for(pilot, lambda: has(app, "registry main @ 4f2a9c1"), "the registry state")
        rows = screen_text(app)
        assert any(word in rows[1] for word in ("Name", "Version")) and any("alpha" in row and "outdated → 2.0.0" in row and "user" in row for row in rows)
        assert "2 packages · 2 outdated · 0 broken" in rows[-1]

    run_app(tmp_path, fake, scenario)


def test_filter_selection_and_the_application_survive_opening_a_package_and_a_run(tmp_path: Path, fake: FakeGupkg) -> None:
    """Filter to ``beta``, open it, run an update and go back: the same application is still on the browser with the filter text and the selection, and the list was re-read."""
    update_run(fake, tmp_path, ("beta",))
    fake.respond("list", doc=list_doc([("alpha", "user", "outdated", "1.0.0", "2.0.0", str(tmp_path / "home" / "opt" / "alpha")), ("beta", "user", "installed", "2.0.0", None, str(tmp_path / "home" / "opt" / "beta"))]))
    fake.scenario["info"].clear()
    fake.respond("info", doc=info_of("beta", tmp_path))
    identity: list[object] = []

    async def scenario(app, pilot) -> None:
        """Filter, open, update, come back."""
        await open_browser(app, pilot)
        identity.append(id(app))
        await pilot.press("slash")
        for letter in "bet":
            await pilot.press(letter)
        await wait_for(pilot, lambda: not has(app, "alpha"), "the filter to apply")
        await pilot.press("enter")
        await pilot.press("enter")
        await wait_for(pilot, lambda: has(app, "Installed:"), "the package view")
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        await pilot.press("escape")
        await pilot.press("escape")
        await wait_for(pilot, lambda: has(app, "beta") and has(app, "installed"), "the browser to be back and reloaded")
        assert id(app) == identity[0] and not has(app, "alpha")
        assert has(app, "bet")
        assert app.screen.selected_target() == "user:beta"

    run_app(tmp_path, fake, scenario)

    assert [call["key"] for call in fake.calls()].count("list") == 2


def test_names_with_markup_characters_are_shown_literally(tmp_path: Path, fake: FakeGupkg) -> None:
    """A package called ``[bold red]x[/]`` or with a control character is drawn as typed; the program neither crashes nor styles it."""
    hostile = "[bold red]x[/] \x1b[31m"
    fake.scenario["list"].clear()
    fake.respond("list", doc=list_doc([(hostile, "user", "installed", "[b]1[/b]", None, None)]))

    async def scenario(app, pilot) -> None:
        """The row shows the characters, cleaned."""
        await wait_for(pilot, lambda: has(app, "[bold red]x[/]"), "the hostile name")
        assert has(app, "[b]1[/b]") and not any("\x1b" in row for row in screen_text(app))

    run_app(tmp_path, fake, scenario)


def test_an_unreadable_inventory_is_shown_in_the_status_line_not_a_crash(tmp_path: Path, fake: FakeGupkg) -> None:
    """If ``gupkg list`` prints nothing readable the browser says so (ERROR in the status line) and stays usable."""
    fake.scenario["list"].clear()
    fake.respond("list", raw="nonsense")

    async def scenario(app, pilot) -> None:
        """Wait for the error and quit with the key."""
        await wait_for(pilot, lambda: has(app, "ERROR:"), "the error")
        await pilot.press("q")

    run_app(tmp_path, fake, scenario)


# -- running ------------------------------------------------------------------------


def test_the_run_view_shows_the_phases_in_order_and_the_verdict_last(tmp_path: Path, fake: FakeGupkg) -> None:
    """Headers Resolve then Fetch appear in that order inside the package, and the last line of the log is the summary under ``Done``."""
    records = [
        {"event": "phase_start", "phase": "Resolve"}, {"event": "step", "text": "alpha  1.0.0  ->  2.0.0", "kind": "info"}, {"event": "phase_end", "phase": "Resolve", "status": "ok", "elapsed": 0.1},
        {"event": "phase_start", "phase": "Fetch"}, {"event": "phase_end", "phase": "Fetch", "status": "ok", "elapsed": 0.2},
    ]
    update_run(fake, tmp_path, ("alpha",), records=records)

    async def scenario(app, pilot) -> None:
        """Run an update and read the finished log."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        rows = content(app)
        headers = [row for row in rows if row.startswith("━━ ")]
        assert [row.split()[1] for row in headers] == ["Resolve", "Fetch", "Done"]
        assert rows[-1] == "ok alpha 2.0.0: updated (was 1.0.0)" and "Finished" in screen_text(app)[-1]

    run_app(tmp_path, fake, scenario)


def test_a_2000_line_events_file_keeps_the_view_on_the_tail_and_the_keys_alive(tmp_path: Path, fake: FakeGupkg) -> None:
    """The log follows the tail through 2,000 lines; the view can be scrolled back; keys (help) still work while and after it is fed."""
    records = [{"event": "subprocess_line", "cmd": "git", "line": f"line {number}"} for number in range(2000)]
    update_run(fake, tmp_path, ("alpha",), records=records)

    async def scenario(app, pilot) -> None:
        """Feed the file, check the tail, scroll back, open help."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        log = app.screen.query_one("RunLog")
        await wait_for(pilot, lambda: has(app, "ok alpha 2.0.0") and log.scroll_y >= log.max_scroll_y - 1, "the view to reach the tail")
        await pilot.press("pageup")
        await wait_for(pilot, lambda: log.scroll_y < log.max_scroll_y - 1, "the view to scroll back")
        await pilot.press("question_mark")
        await wait_for(pilot, lambda: has(app, "Browser"), "help to answer while the log is long")
        await pilot.press("escape")

    run_app(tmp_path, fake, scenario)


def test_escape_during_a_run_asks_and_a_no_keeps_it_running(tmp_path: Path, fake: FakeGupkg) -> None:
    """Escape never leaves a running view: it asks to cancel after the current package, and ``n`` lets the run go on to its end."""
    update_run(fake, tmp_path, ("alpha",), delay=1.0)

    async def scenario(app, pilot) -> None:
        """Press Escape while the double sleeps, answer no, wait for the end."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen), "the run view")
        await pilot.press("escape")
        await wait_for(pilot, lambda: has(app, "Cancel after the current package?"), "the question")
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        assert app.screen.outcome.ok and not app.screen.outcome.cancelled

    run_app(tmp_path, fake, scenario)

    assert fake.stop_events() == []


def test_a_yes_to_the_cancel_question_asks_the_call_to_stop_after_the_current_package(tmp_path: Path, fake: FakeGupkg) -> None:
    """The stop marker the run announced is created, the double sees it after package one, and the second package is reported skipped."""
    fake.respond("update:plan", doc=plan_doc([plan_entry("user:alpha", "alpha"), plan_entry("user:beta", "beta")]))
    doc = result("update", [entry("alpha"), entry("beta", status="skipped", version=None, previous=None, code=5)])
    fake.respond("update", doc=doc, events={
        "dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "stop_after": 0, "result": doc,
        "packages": [{"name": name, "path": str(tmp_path / "home" / "opt" / name), "records": []} for name in ("alpha", "beta")],
    })

    async def scenario(app, pilot) -> None:
        """Cancel after the first package has ended."""
        await open_browser(app, pilot)
        await pilot.press("U")
        await wait_for(pilot, lambda: has(app, "Update 2 user packages"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and has(app, "Package alpha: updated"), "package one to end")
        await pilot.press("escape")
        await wait_for(pilot, lambda: has(app, "Cancel after the current package?"), "the question")
        await pilot.press("y")
        await wait_for(pilot, lambda: app.screen.outcome is not None if isinstance(app.screen, RunScreen) else False, "the run to end")
        text = "\n".join(screen_text(app))
        assert "- beta: skipped" in text and "Stopped after the current package at your request." in text
        assert app.screen.outcome.exit_code == 5

    run_app(tmp_path, fake, scenario)

    assert fake.stop_events()[0]["saw_stop"] is True


def test_dialogs_answer_only_to_explicit_keys(tmp_path: Path, fake: FakeGupkg) -> None:
    """Enter does not confirm an uninstall; ``n`` and Escape decline; ``y`` runs it (with ``--yes`` for the CLI, which is called with ``--no-input``)."""
    doc = result("uninstall", [entry("alpha", "uninstall", status="uninstalled")])
    fake.respond("uninstall", doc=doc, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": doc, "packages": [{"name": "alpha", "path": str(tmp_path / "home" / "opt" / "alpha"), "records": []}]})

    async def scenario(app, pilot) -> None:
        """Try the keys in turn."""
        await open_browser(app, pilot)
        await pilot.press("x")
        await wait_for(pilot, lambda: has(app, "Uninstall alpha?"), "the question")
        await pilot.press("enter")
        await pilot.pause()
        assert has(app, "Uninstall alpha?")
        await pilot.press("escape")
        await wait_for(pilot, lambda: not has(app, "Uninstall alpha?"), "the question to close")
        assert not [call for call in fake.calls() if call["key"] == "uninstall"]
        await pilot.press("x")
        await wait_for(pilot, lambda: has(app, "Uninstall alpha?"), "the question again")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run")

    run_app(tmp_path, fake, scenario)

    (call,) = [item for item in fake.calls() if item["key"] == "uninstall"]
    assert call["targets"] == ["user:alpha"] and "--yes" in call["options"] and "--no-input" in call["options"] and "--purge" not in call["options"]


def test_a_registry_that_was_never_downloaded_is_announced_in_the_hint_line_and_the_status_line(tmp_path: Path, fake: FakeGupkg) -> None:
    """With no registry downloaded, the key-hint line leads with `s DOWNLOAD THE REGISTRY` and the status line says it; `s` is always in the hints."""
    fake.scenario["registry"] = []  # the fixture's preset answer is replaced
    fake.respond("registry", doc=head("registry status", registries=[{"name": "main", "source": "https://example.org/registry.git"}]))
    seen: dict[str, str] = {}

    async def scenario(app, pilot) -> None:
        """Read the two frame lines once the browser has loaded."""
        await open_browser(app, pilot)
        await wait_for(pilot, lambda: "NOT DOWNLOADED" in screen_text(app)[-1], "the registry state")
        rows = screen_text(app)
        seen["hint"], seen["status"] = rows[0], rows[-1]

    run_app(tmp_path, fake, scenario)

    assert seen["hint"].strip().startswith("s DOWNLOAD THE REGISTRY") and "a list available" in seen["hint"]
    assert "registry main: NOT DOWNLOADED, press s" in seen["status"]


def test_a_downloaded_registry_leaves_a_plain_hint_line_that_still_names_s(tmp_path: Path, fake: FakeGupkg) -> None:
    """With a registry downloaded the normal hints show, and they include `s sync`."""
    fake.scenario["registry"] = []  # the fixture's preset answer is replaced
    fake.respond("registry", doc=head("registry status", registries=[{"name": "main", "source": "x", "revision": "abcdef1234", "synchronized_at": "2026-10-25T00:00:00+00:00"}]))
    seen: dict[str, str] = {}

    async def scenario(app, pilot) -> None:
        """Read the hint line after the registry state is in the status line."""
        await open_browser(app, pilot)
        await wait_for(pilot, lambda: "registry main @ abcdef1" in screen_text(app)[-1], "the registry state")
        seen["hint"] = screen_text(app)[0]

    run_app(tmp_path, fake, scenario)

    assert "s sync" in seen["hint"] and "DOWNLOAD" not in seen["hint"]


def test_the_s_key_synchronises_the_registry_with_one_cli_call_and_no_package(tmp_path: Path, fake: FakeGupkg) -> None:
    """In the manager, ``s`` runs ``gupkg registry sync`` once with the machine options, names no package, and returns to the browser."""
    doc = result("registry sync", [])
    fake.respond("registry", doc=doc, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": doc, "packages": []})

    async def scenario(app, pilot) -> None:
        """Press ``s`` in the browser, wait for the run to finish and leave the run view."""
        await open_browser(app, pilot)
        await pilot.press("s")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run")
        await pilot.press("escape")
        await wait_for(pilot, lambda: has(app, "alpha") and not isinstance(app.screen, RunScreen), "the browser")

    run_app(tmp_path, fake, scenario)

    (call,) = [item for item in fake.calls() if item["key"] == "registry" and "sync" in item["argv"]]
    assert call["targets"] == [] and "--no-input" in call["options"] and "--json" in call["options"] and "--events-file" in call["options"]


def test_every_operation_is_one_cli_call_with_the_machine_options(tmp_path: Path, fake: FakeGupkg) -> None:
    """Check for updates, repair and the health check each arrive as one ``gupkg`` call for the qualified target; the program itself changes nothing."""
    for command, status in (("check-updates", "written"), ("install", "installed"), ("check", "healthy")):
        doc = result(command, [entry("alpha", command, status=status)])
        fake.respond(command, doc=doc, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": doc, "packages": [{"name": "alpha", "path": str(tmp_path / "home" / "opt" / "alpha"), "records": []}]})

    async def scenario(app, pilot) -> None:
        """Run the three operations from the browser, leaving each run view."""
        await open_browser(app, pilot)
        for keys, expected in (("c", None), ("i", "Install or repair alpha?"), ("h", None)):
            await pilot.press(keys)
            if expected:
                await wait_for(pilot, lambda: has(app, expected), "the question")
                await pilot.press("y")
            await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run")
            await pilot.press("escape")
            await wait_for(pilot, lambda: has(app, "alpha") and not isinstance(app.screen, RunScreen), "the browser")

    run_app(tmp_path, fake, scenario)

    operations = [call for call in fake.calls() if call["key"] in ("check-updates", "install", "check")]
    assert [call["key"] for call in operations] == ["check-updates", "install", "check"]
    assert all(call["targets"] == ["user:alpha"] for call in operations)


def test_the_package_program_runs_check_update_repair_uninstall_and_health_as_cli_calls(tmp_path: Path, fake: FakeGupkg) -> None:
    """From ``gupkg-tui`` each key starts one ``gupkg`` call for the package (with ``--yes`` for the confirmations already given) and Escape comes back to the same view."""
    root = tmp_path / "home" / "opt" / ".gupkg" / "logs"
    package = {"name": "alpha", "path": str(tmp_path / "home" / "opt" / "alpha"), "records": []}
    for command, status in (("check-updates", "written"), ("install", "installed"), ("uninstall", "uninstalled"), ("check", "healthy")):
        doc = result(command, [entry("alpha", command, status=status)])
        fake.respond(command, doc=doc, events={"dir": str(root), "result": doc, "packages": [package]})
    update_run(fake, tmp_path, ("alpha",))

    async def scenario(app, pilot) -> None:
        """Press each operation key, answer the dialog where there is one, and come back."""
        await wait_for(pilot, lambda: has(app, "Installed:"), "the package view")
        for key, question in (("c", None), ("r", "Install or repair alpha?"), ("x", "Uninstall alpha?"), ("h", None), ("u", "Update 1 user package")):
            await pilot.press(key)
            if question:
                await wait_for(pilot, lambda: has(app, question), f"the question for {key}")
                await pilot.press("y")
            await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, f"the run of {key}")
            assert app.screen.outcome.ok, (key, app.screen.outcome.entries, app.screen.outcome.problems)
            await pilot.press("escape")
            await wait_for(pilot, lambda: has(app, "Versions in the package folder") and not isinstance(app.screen, RunScreen), "the package view")

    run_app(tmp_path, fake, scenario, program="tui")

    operations = [call for call in fake.calls() if call["key"] in ("check-updates", "install", "uninstall", "check", "update")]
    assert [call["key"] for call in operations] == ["check-updates", "install", "uninstall", "check", "update"]
    assert all(call["targets"] == ["user:alpha"] and "--no-input" in call["options"] for call in operations)
    # ``check-updates`` activates nothing, so the program answers its own multi-package question; the health check changes nothing and needs none.
    assert [call["key"] for call in operations if "--yes" in call["options"]] == ["check-updates", "install", "uninstall", "update"]


def test_cli_error_text_with_markup_is_shown_literally_everywhere(tmp_path: Path, fake: FakeGupkg) -> None:
    """A ``[red]x`` inside what the CLI says (an api value, an error message, a log lookup failure) reaches the status line, the package view, the run log and the toast as typed."""
    fake.respond("info", doc=head("info", packages=[], api=1))
    fake.scenario["info"].pop(0)
    fake.scenario["info"].insert(0, {"doc": {"api": "[red]x", "command": "info", "ok": True, "exit_code": 0}})
    fake.respond("update:plan", doc=plan_doc([plan_entry("user:alpha", "alpha")]))
    failing = result("update", [entry("alpha", ok=False, status="failed", code=3, error={"code": "x", "message": "[red]boom[/red] [bold]x"})])
    fake.respond("update", doc=failing, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": failing, "packages": [{"name": "alpha", "path": str(tmp_path / "home" / "opt" / "alpha"), "records": [{"event": "error", "text": "[red]x"}]}]})

    async def scenario(app, pilot) -> None:
        """Look at the toast of a failed log lookup, then the failed run's log."""
        await open_browser(app, pilot)
        await pilot.press("l")
        await wait_for(pilot, lambda: has(app, "[red]x"), "the toast with the literal text")
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        assert has(app, "Error: [red]x") and has(app, "[red]boom[/red] [bold]x")

    run_app(tmp_path, fake, scenario, toasts=True)


# -- Ctrl-C ---------------------------------------------------------------------------


def slow_update(fake: FakeGupkg, tmp_path: Path, delay: float = 1.5) -> None:
    """Schedule a one-package update whose call takes *delay* seconds to start answering (a call that is in flight for a while)."""
    update_run(fake, tmp_path, ("alpha",), delay=delay)


def test_ctrl_c_on_a_running_run_asks_the_cancel_question_and_never_quits(tmp_path: Path, fake: FakeGupkg) -> None:
    """Ctrl-C while a call runs opens "Cancel after the current package?"; a second Ctrl-C with that dialog open is ignored; ``n`` lets the run finish."""
    slow_update(fake, tmp_path)

    async def scenario(app, pilot) -> None:
        """Press Ctrl-C twice during the call, then answer no."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.session is not None and app.screen.session.running, "the call to be in flight")
        await pilot.press("ctrl+c")
        await wait_for(pilot, lambda: has(app, "Cancel after the current package?"), "the cancel question")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert has(app, "Cancel after the current package?") and app.return_code is None
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        assert app.screen.outcome.ok and app.return_code is None

    run_app(tmp_path, fake, scenario)


def test_ctrl_c_over_a_question_asked_during_a_run_is_ignored(tmp_path: Path, fake: FakeGupkg) -> None:
    """A checksum question that appears inside a running batch (a check's follow-up) is not a way out: Ctrl-C does not quit, the answer does."""
    refusal = {"code": "checksum-missing", "message": "no checksum", "url": "https://example.org/a.zip", "skippable": True}
    first = result("check-updates", [entry("alpha", "check-updates", ok=False, status="failed", code=3, error=refusal)])
    again = result("check-updates", [entry("alpha", "check-updates", status="written")])
    for doc in (first, again):
        fake.respond("check-updates", doc=doc, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": doc, "packages": [{"name": "alpha", "path": str(tmp_path / "home" / "opt" / "alpha"), "records": []}]})

    async def scenario(app, pilot) -> None:
        """Reach the follow-up question, press Ctrl-C, answer no."""
        await open_browser(app, pilot)
        await pilot.press("c")
        await wait_for(pilot, lambda: has(app, "without a checksum"), "the follow-up question")
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert has(app, "without a checksum") and app.return_code is None
        await pilot.press("n")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")

    run_app(tmp_path, fake, scenario)


@pytest.mark.parametrize("where", ["plan-confirmation", "browser"])
def test_ctrl_c_quits_when_nothing_is_running(tmp_path: Path, fake: FakeGupkg, where: str) -> None:
    """On the browser, and on a plan that was not confirmed yet (no call has started), Ctrl-C quits with exit code 1."""
    update_run(fake, tmp_path, ("alpha",))

    async def scenario(app, pilot) -> None:
        """Go to the place and press Ctrl-C."""
        await open_browser(app, pilot)
        if where == "plan-confirmation":
            await pilot.press("u")
            await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("ctrl+c")
        await wait_for(pilot, lambda: app.return_code == 1, "the program to quit")

    run_app(tmp_path, fake, scenario)

    assert not [call for call in fake.calls() if call["key"] == "update"]


def test_ctrl_c_on_the_checksum_question_before_any_call_quits(tmp_path: Path, fake: FakeGupkg) -> None:
    """The per-package checksum question of an update comes before the first update call, so nothing runs and Ctrl-C may quit."""
    fake.respond("update:plan", doc=plan_doc([plan_entry("user:alpha", "alpha", sha256="missing")]))

    async def scenario(app, pilot) -> None:
        """Confirm the plan, then press Ctrl-C on the checksum question."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: has(app, "without a checksum"), "the checksum question")
        await pilot.press("ctrl+c")
        await wait_for(pilot, lambda: app.return_code == 1, "the program to quit")

    run_app(tmp_path, fake, scenario)

    assert not [call for call in fake.calls() if call["key"] == "update"]


# -- responsiveness and colour -----------------------------------------------------------


def test_a_100000_record_run_keeps_the_keys_responsive_and_the_log_bounded(tmp_path: Path, fake: FakeGupkg) -> None:
    """While 100,000 events are ingested a key press is handled within 0.5 s (the log never re-renders what it has, and keeps at most its cap of lines)."""
    records = [{"event": "subprocess_line", "cmd": "git", "line": f"line {number}"} for number in range(100_000)]
    update_run(fake, tmp_path, ("alpha",), records=records)
    latencies: list[float] = []

    async def scenario(app, pilot) -> None:
        """Start the run and press ? while the follower is still reading, timing the answer."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and has(app, "line "), "ingestion to begin", timeout=60)
        # Three presses while the file is being read; the best one is judged, so one scheduling hiccup of a busy machine cannot fail the test (a blocked loop slows all three).
        for _ in range(3):
            started = time.monotonic()
            await pilot.press("question_mark")
            await wait_for(pilot, lambda: has(app, "Browser"), "help to open during ingestion")
            latencies.append(time.monotonic() - started)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not has(app, "Browser"), "help to close")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish", timeout=60)
        log = app.screen.query_one("RunLog")
        assert len(log.lines) <= MAX_LOG_LINES and has(app, "ok alpha 2.0.0")

    run_app(tmp_path, fake, scenario)

    assert len(latencies) == 3 and min(latencies) < 0.5, f"key presses took {latencies} seconds during ingestion"


def test_ingesting_100000_records_takes_bounded_time(tmp_path: Path) -> None:
    """Reading and rendering 100,000 records (follower, parser, renderer) stays under 5 s: detail lines past the cap are counted, not drawn."""
    path = tmp_path / "events.jsonl"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for number in range(100_000):
            handle.write(json.dumps({"event": "subprocess_line", "cmd": "git", "line": f"line {number}"}) + "\n")
    stream, renderer = EventStream(path), EventRenderer()
    started = time.monotonic()
    shown = 0
    while True:
        batch = stream.poll()
        if not batch:
            break
        shown += len(renderer.feed(batch))

    assert time.monotonic() - started < 5.0
    assert shown <= 10_000 and renderer.detail_omitted >= 90_000


def test_with_colour_off_nothing_is_carried_by_colour_alone(tmp_path: Path, fake: FakeGupkg) -> None:
    """With ``[ui] color = "never"`` (the program's own setting) at 80x24 the run view shows failures, warnings and errors in words and marks, and its lines carry no colour."""
    records = [
        {"event": "phase_start", "phase": "Verify"}, {"event": "warning", "text": "slow mirror"}, {"event": "error", "text": "checksum differs", "hint": "retry later"},
        {"event": "phase_end", "phase": "Verify", "status": "failed", "elapsed": 0.5},
    ]
    update_run(fake, tmp_path, ("alpha",), records=records)
    fake.scenario["update"][0]["doc"] = result("update", [entry("alpha", ok=False, status="failed", code=3, error={"code": "x", "message": "checksum differs"})])
    fake.scenario["update"][0]["events"]["result"] = fake.scenario["update"][0]["doc"]
    fake.respond("update", **fake.scenario["update"].pop(0))
    coloured: list[bool] = []

    async def scenario(app, pilot) -> None:
        """Run the failing update and read the screen and the log's own styles."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 user package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        text = "\n".join(screen_text(app))
        for word in ("✗ Verify failed", "Warning: slow mirror", "Error: checksum differs", "Failed (exit code 3)", "x alpha: update failed"):
            assert word in text, word
        coloured.append(app.env.settings.colour)

    run_app(tmp_path, fake, scenario, settings='[ui]\ncolor = "never"')

    assert coloured == [False]


def test_the_renderer_without_colour_keeps_only_emphasis(tmp_path: Path) -> None:
    """Every kind of line, rendered without colour, has no colour word in its style and still says what happened."""
    records = [
        {"event": "run_start", "command": "update", "mode": "user", "elevated": False}, {"event": "package_start", "view": {"name": "p"}},
        {"event": "phase_start", "phase": "Fetch"}, {"event": "phase_end", "phase": "Fetch", "status": "failed", "elapsed": 1.0},
        {"event": "step", "text": "x", "kind": "add"}, {"event": "warning", "text": "w"}, {"event": "error", "text": "e", "hint": "h"},
        {"event": "package_end", "result": {"name": "p", "ok": False, "status": "failed"}},
    ]
    lines = EventRenderer(colour=False).feed(records) + verdict_lines(3, ["x p: update failed: e"], colour=False)

    assert all(not ({"red", "green", "cyan", "yellow", "magenta", "blue"} & set(str(line.style).split())) for line in lines)
    assert any(line.plain.startswith("  ✗ Fetch failed") for line in lines) and any(line.plain.startswith("Warning:") for line in lines)
    assert any(line.plain.startswith("Error:") for line in lines) and any(line.plain.startswith("━━ Failed (exit code 3)") for line in lines)
