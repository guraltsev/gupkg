"""Cover package-operation TUI scope labels, operation routing, and status handoff.

The package layout and Textual test driver are real; administrator detection is
a ``FakeSystem`` flag and the core package operation
(``gupkg.commands.run_package_command``) is mocked at its boundary, so these
tests protect what the user selects and the exit status they get back.
Manager-mode presentation is out of scope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg import commands
from gupkg.core import ActionResult, Scope
from gupkg.outcome import Outcome
from gupkg.report.events import Error, Progress, Step, SubprocessLine, emit
from gupkg.tui import run_tui


def _package_version(root: Path) -> Path:
    """Create a minimal package version that the TUI can inspect."""
    version = root / "ScopeApp" / "1.0.0.local.1"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(
        'name = "ScopeApp"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )
    return version


@pytest.fixture
def operations(monkeypatch: pytest.MonkeyPatch) -> list[commands.PackageRequest]:
    """Record each package request the TUI runs and answer with a failing status 7."""
    requests: list[commands.PackageRequest] = []

    def run(request, system):
        requests.append(request)
        return Outcome(request.command, ActionResult(False, errors=["failed"], exit_code=7))

    monkeypatch.setattr(commands, "run_package_command", run)
    return requests


def _drive(monkeypatch: pytest.MonkeyPatch, steps) -> None:
    """Replace ``App.run`` with a Textual test-driver session that performs *steps*."""
    from textual.app import App

    def run_app(app, *args, **kwargs):
        async def drive() -> None:
            async with app.run_test(size=(80, 12)) as pilot:
                await steps(app, pilot)
                for _ in range(10):
                    await pilot.pause(0.1)

        asyncio.run(drive())

    monkeypatch.setattr(App, "run", run_app)


def test_administrator_install_shows_system_scope_and_requests_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operations, system
) -> None:
    """An administrator install displays System and runs the install with system scope."""
    version = _package_version(tmp_path)
    system.elevated = True

    async def steps(app, pilot) -> None:
        await pilot.press("enter")
        rendered = str(app.screen.query_one("#command-options").get_option_at_index(2))
        assert "Installation Scope: System" in rendered
        assert "Machine" not in rendered
        await pilot.press("enter")
        assert "--scope system install" in str(app.screen.query_one("Label").render())

    _drive(monkeypatch, steps)
    run_tui(system, str(version))

    assert [(r.command, r.scope) for r in operations] == [("install", Scope.MACHINE)]


def test_update_selection_runs_a_full_update_and_returns_its_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operations, system
) -> None:
    """The update selection runs the full update and the process status is the operation's."""
    version = _package_version(tmp_path)

    async def steps(app, pilot) -> None:
        # Home order is Install, update check, update download, update.
        await pilot.press("down", "down", "down", "enter")
        await pilot.press("enter")

    _drive(monkeypatch, steps)

    assert run_tui(system, str(version)) == 7
    [request] = operations
    assert (request.command, request.check_only, request.download_only) == ("update", False, False)
    assert request.scope == Scope.USER
    assert request.path == version


def test_manager_selected_system_scope_stays_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operations, system
) -> None:
    """A manager handoff displays its system scope as locked and runs with it."""
    version = _package_version(tmp_path)

    async def steps(app, pilot) -> None:
        await pilot.press("enter")
        rendered = str(app.screen.query_one("#command-options").get_option_at_index(2))
        assert "Installation Scope: System (locked)" in rendered
        await pilot.press("enter")
        assert "--scope system install" in str(app.screen.query_one("Label").render())

    _drive(monkeypatch, steps)
    run_tui(system, str(version), forced_scope=Scope.MACHINE)

    assert [(r.command, r.scope) for r in operations] == [("install", Scope.MACHINE)]


def _drive_sized(monkeypatch: pytest.MonkeyPatch, size: tuple[int, int], steps) -> None:
    """Like ``_drive`` but with a small terminal so output overflows the screen."""
    from textual.app import App

    def run_app(app, *args, **kwargs):
        async def drive() -> None:
            async with app.run_test(size=size) as pilot:
                await steps(app, pilot)

        asyncio.run(drive())

    monkeypatch.setattr(App, "run", run_app)


async def _wait_until(pilot, condition, *, seconds: float = 5.0) -> None:
    """Pause the Textual driver until *condition()* holds, failing after *seconds*."""
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached")


def test_long_output_follows_new_lines_and_shows_the_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system
) -> None:
    """Output taller than the screen stays scrolled to its end, including the final failure text."""
    version = _package_version(tmp_path)

    def run(request, system):
        for number in range(50):
            emit(Step(f"progress line {number}"))
        emit(Error("the final failure"))
        return Outcome(request.command, ActionResult(False, errors=["the final failure"], exit_code=7))

    monkeypatch.setattr(commands, "run_package_command", run)

    async def steps(app, pilot) -> None:
        await pilot.press("enter", "enter")
        await _wait_until(pilot, lambda: "Failed with exit code 7" in str(app.screen.query_one("#status").render()))
        await pilot.pause(0.3)
        view = app.screen.query_one("#output")
        assert view.is_vertical_scroll_end
        assert view.max_scroll_y > 0
        assert "ERROR: the final failure" in str(view.query_one("#output-text").render())

    _drive_sized(monkeypatch, (60, 8), steps)
    run_tui(system, str(version))


def test_output_with_markup_characters_is_shown_literally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system
) -> None:
    """Bracketed text from a package is displayed as written and never breaks the screen."""
    version = _package_version(tmp_path)

    def run(request, system):
        emit(Error("[bold] hook [/x] failed [update.check]"))
        emit(SubprocessLine("git", "fatal: [red]not a repository[/red]"))
        return Outcome(request.command, ActionResult(True, exit_code=0))

    monkeypatch.setattr(commands, "run_package_command", run)

    async def steps(app, pilot) -> None:
        await pilot.press("enter", "enter")
        await _wait_until(pilot, lambda: "Completed successfully" in str(app.screen.query_one("#status").render()))
        text = str(app.screen.query_one("#output-text").render())
        assert "ERROR: [bold] hook [/x] failed [update.check]" in text
        assert "[git] fatal: [red]not a repository[/red]" in text

    _drive_sized(monkeypatch, (80, 12), steps)
    run_tui(system, str(version))


def test_transient_progress_appears_on_the_status_line_not_in_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system
) -> None:
    """Live download progress replaces itself on the status line and only the final line joins the output."""
    import threading

    version = _package_version(tmp_path)
    release = threading.Event()

    def run(request, system):
        emit(Progress("Downloading update", 1024 * 1024, 2 * 1024 * 1024))
        release.wait(5)
        emit(Progress("Downloading update", 2 * 1024 * 1024, 2 * 1024 * 1024))
        return Outcome(request.command, ActionResult(True, exit_code=0))

    monkeypatch.setattr(commands, "run_package_command", run)

    async def steps(app, pilot) -> None:
        await pilot.press("enter", "enter")
        status = lambda: str(app.screen.query_one("#status").render())  # noqa: E731
        text = lambda: str(app.screen.query_one("#output-text").render())  # noqa: E731
        await _wait_until(pilot, lambda: "Running: Downloading update: 1.0 MiB / 2.0 MiB (50%)" in status())
        assert "50%" not in text()
        release.set()
        await _wait_until(pilot, lambda: "(100%)" in text())
        assert text().count("(100%)") == 1

    _drive_sized(monkeypatch, (80, 12), steps)
    run_tui(system, str(version))


def test_a_tui_session_leaves_a_log_and_shows_the_log_path_in_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, system
) -> None:
    """An install started from the TUI is logged like a CLI run: the package's log holds its phases and the screen ends with the log path."""
    version = _package_version(tmp_path)

    async def steps(app, pilot) -> None:
        await pilot.press("enter", "enter")
        await _wait_until(pilot, lambda: "Completed successfully" in str(app.screen.query_one("#status").render()))
        text = str(app.screen.query_one("#output-text").render())
        assert "==> Resolve" in text
        assert text.rstrip().splitlines()[-1].startswith("Log of ScopeApp: ")

    _drive_sized(monkeypatch, (100, 30), steps)
    assert run_tui(system, str(version)) == 0

    [log] = (tmp_path / "ScopeApp" / ".gupkg" / "logs").glob("2*-install.log")
    assert "==> Resolve" in log.read_text(encoding="utf-8")
    assert list((tmp_path / ".gupkg" / "logs").glob("2*-install-ScopeApp.log")), "the run log is in the root"
