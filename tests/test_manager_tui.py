"""Cover durable manager keyboard navigation through Textual's test driver.

The manager configuration, roots, and inventory are real temporary layouts.
Textual is exercised through its test driver; package providers and package
operations are out of scope because this module covers manager presentation
and handoff behavior.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg import cli
from gupkg.manager import discover_manager, load_manager_config
from gupkg.manager_tui import run_manager_tui


def _package(root: Path, selector: str) -> None:
    """Create one manifest-backed package row in a real collection root."""
    version = root / selector / "v1.0.0.l1"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(
        f'name = "{selector}"\nversion = "1.0.0"\nlocalVersion = 1\n',
        encoding="utf-8",
    )


def test_manager_browser_filters_and_handoff_keep_scope_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The browser reaches duplicate rows, filters them, and displays locked scope on handoff."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    for selector in ("alpha", "beta", "gamma", "delta"):
        _package(user, selector)
    _package(system, "alpha")
    config_path = manager_dir / "gupkg-config.toml"
    config_path.write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n"
        f"[bin]\nsystem = '{manager_dir / 'bin-system'}'\nuser = '{manager_dir / 'bin-user'}'\n"
        f"[registry]\ncache = '{manager_dir / 'cache'}'\n",
        encoding="utf-8",
    )
    config = load_manager_config(config_path)
    inventory = discover_manager(config)
    captured = []

    def capture_run(app, *args, **kwargs):
        captured.append(app)
        return None

    from textual.app import App

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui(config, inventory) == 0
    assert captured
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(42, 8)) as pilot:
            await pilot.press("enter")
            browser = app.screen
            assert "User" in str(browser.query_one("#package-options").get_option_at_index(3))
            await pilot.press("down", "down", "enter")
            details = next(widget for widget in app.screen.query("*") if type(widget).__name__ == "Static")
            assert "Scope: User" in str(details.render())
            assert "locked" in str(details.render())
            await pilot.press("escape")
            assert app.screen is browser

    asyncio.run(drive())


def test_unconfigured_manager_starts_with_one_init_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured manager mode shows a visible header and only initialization."""
    package_dir = tmp_path / "gupkg-package"
    package_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    appdata = tmp_path / "AppData" / "Roaming"
    userprofile = tmp_path / "UserProfile"
    localappdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("SYSTEMDRIVE", str(tmp_path / "SystemDrive"))
    captured = []

    from textual.app import App

    def capture_run(app, *args, **kwargs):
        captured.append(app)
        return None

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui() == 0
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(60, 12)) as pilot:
            assert str(app.query_one("#manager-mode-header").render()) == "MANAGER MODE"
            actions = app.screen.query_one("#manager-init-actions")
            assert len(actions.options) == 1
            assert "Init manager mode" in str(actions.get_option_at_index(0))

    asyncio.run(drive())


def test_manager_tui_discovers_package_directory_config_when_handed_off_from_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manager handoff discovers the configuration in the gupkg package directory before initialization."""
    appdata = tmp_path / "AppData" / "Roaming"
    manager_dir = tmp_path / "gupkg-package"
    manager_dir.mkdir(parents=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(manager_dir / "manager.py"))
    system = tmp_path / "system"
    user = tmp_path / "user"
    system.mkdir()
    user.mkdir()
    (manager_dir / "gupkg-config.toml").write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n"
        f"[bin]\nsystem = '{manager_dir / 'bin-system'}'\nuser = '{manager_dir / 'bin-user'}'\n"
        f"[registry]\ncache = '{manager_dir / 'cache'}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))
    captured = []

    from textual.app import App

    def capture_run(app, *args, **kwargs):
        captured.append(app)

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui() == 0
    assert captured

    async def drive() -> None:
        app = captured[0]
        async with app.run_test(size=(60, 12)):
            assert app.screen.query_one("#manager-title")

    asyncio.run(drive())


def test_manager_init_proceeds_with_displayed_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default-first initialization menu writes a valid config and opens the manager home."""
    package_dir = tmp_path / "gupkg-package"
    package_dir.mkdir(exist_ok=True)
    monkeypatch.setattr("gupkg.manager.__file__", str(package_dir / "manager.py"))
    appdata = tmp_path / "AppData" / "Roaming"
    userprofile = tmp_path / "UserProfile"
    localappdata = tmp_path / "AppData" / "Local"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(userprofile))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    monkeypatch.setenv("SYSTEMDRIVE", str(tmp_path / "SystemDrive"))
    captured = []

    from textual.app import App

    def capture_run(app, *args, **kwargs):
        captured.append(app)
        return None

    monkeypatch.setattr(App, "run", capture_run)
    assert run_manager_tui() == 0
    app = captured[0]
    config_path = package_dir / "gupkg-config.toml"

    async def drive() -> None:
        async with app.run_test(size=(80, 18)) as pilot:
            await pilot.press("enter")
            assert app.screen.query_one("#manager-init-options")
            await pilot.press("enter")
            assert app.screen.query_one("#manager-title")
            assert "MANAGER MODE" in str(app.query_one("#manager-mode-header").render())

    asyncio.run(drive())
    assert config_path.is_file()
    config = load_manager_config(config_path)
    assert config.schema_version == 2
    assert config.registry_cache == package_dir.parent / "App" / "registry"


def test_manager_tui_persists_the_selected_shim_linkage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manager home exposes a linkage choice that persists to its TOML file."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    manager_dir.mkdir()
    system.mkdir()
    user.mkdir()
    config_path = manager_dir / "gupkg-config.toml"
    config_path.write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n"
        f"[bin]\nsystem = '{manager_dir / 'bin-system'}'\nuser = '{manager_dir / 'bin-user'}'\n"
        f"[registry]\ncache = '{manager_dir / 'cache'}'\n",
        encoding="utf-8",
    )
    captured = []

    from textual.app import App

    monkeypatch.setattr(App, "run", lambda app, *args, **kwargs: captured.append(app))
    config = load_manager_config(config_path)
    assert run_manager_tui(config, discover_manager(config)) == 0
    app = captured[0]

    async def drive() -> None:
        async with app.run_test(size=(70, 14)) as pilot:
            await pilot.press("down", "down", "down", "enter")
            assert app.screen.query_one("#shim-linkage-options")
            await pilot.press("down", "enter")

    asyncio.run(drive())
    assert load_manager_config(config_path).shim_linkage == "static"


def test_tui_outside_package_directory_opens_manager_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare TUI command selects manager mode when the current directory is not a package."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    selected = []
    manager_dir = tmp_path / "gupkg-package"
    manager_dir.mkdir()
    monkeypatch.setattr("gupkg.manager.__file__", str(manager_dir / "manager.py"))

    def capture_manager(*args, **kwargs):
        selected.append((args, kwargs))
        return 0

    system = tmp_path / "system"
    user = tmp_path / "user"
    system.mkdir()
    user.mkdir()
    (manager_dir / "gupkg-config.toml").write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n"
        f"[bin]\nsystem = '{manager_dir / 'bin-system'}'\nuser = '{manager_dir / 'bin-user'}'\n"
        f"[registry]\ncache = '{manager_dir / 'cache'}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gupkg.manager_tui.run_manager_tui", capture_manager)
    assert cli.main(["manager", "tui"]) == 0
    assert selected and selected[0][0][0].path == manager_dir / "gupkg-config.toml"


def _manager(tmp_path: Path, selectors: tuple[str, ...]):
    """Create a real manager config with *selectors* as user packages and return its inventory."""
    manager_dir = tmp_path / "manager"
    system = tmp_path / "system"
    user = tmp_path / "user"
    for directory in (manager_dir, system, user):
        directory.mkdir()
    for selector in selectors:
        _package(user, selector)
    config_path = manager_dir / "gupkg-config.toml"
    config_path.write_text(
        'mode = "manager"\nschema_version = 2\n[packages]\n'
        f"system = '{system}'\nuser = '{user}'\n"
        f"[bin]\nsystem = '{manager_dir / 'bin-system'}'\nuser = '{manager_dir / 'bin-user'}'\n"
        f"[registry]\ncache = '{manager_dir / 'cache'}'\n",
        encoding="utf-8",
    )
    config = load_manager_config(config_path)
    return config, discover_manager(config), user


def _capture_app(monkeypatch: pytest.MonkeyPatch) -> list:
    """Make ``App.run`` record the app instead of running it."""
    from textual.app import App

    captured: list = []
    monkeypatch.setattr(App, "run", lambda app, *args, **kwargs: captured.append(app))
    return captured


async def _wait_until(pilot, condition, *, seconds: float = 5.0) -> None:
    """Pause the Textual driver until *condition()* holds, failing after *seconds*."""
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not reached")


def _blocking_checks(monkeypatch: pytest.MonkeyPatch):
    """Replace the per-target check with one that blocks until released; return its controls."""
    import threading

    from gupkg import commands
    from gupkg.core import ActionResult

    started: list[str] = []
    release = threading.Event()

    def check(target, **_kwargs):
        started.append(target.target_id)
        if len(started) == 1:
            release.wait(5)
        target.update_status = "available" if len(started) == 1 else "current"
        target.candidate_version = "2.0.0" if len(started) == 1 else None
        return ActionResult(True, status=target.update_status)

    monkeypatch.setattr(commands, "manager_update_target", check)
    return started, release


def test_refresh_reports_progress_and_each_result_while_checking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh names the target being checked and lists each result as soon as its check finishes."""
    config, inventory, _ = _manager(tmp_path, ("alpha", "beta"))
    started, release = _blocking_checks(monkeypatch)
    app = _capture_app(monkeypatch)
    assert run_manager_tui(config, inventory) == 0

    async def drive() -> None:
        async with app[0].run_test(size=(70, 12)) as pilot:
            await pilot.press("down", "enter")
            status = lambda: str(app[0].screen.query_one("#refresh-status").render())  # noqa: E731
            text = lambda: str(app[0].screen.query_one("#output-text").render())  # noqa: E731
            await _wait_until(pilot, lambda: status() == "Checking 1 of 2: user:alpha")
            assert text() == ""
            release.set()
            await _wait_until(pilot, lambda: "user:alpha: Update Available 2.0.0" in text())
            await _wait_until(pilot, lambda: status().startswith("Refresh complete"))
            assert status() == "Refresh complete: 1 available, 1 current, 0 errors."
            assert "user:beta: Update Current" in text()

    asyncio.run(drive())


def test_escape_during_refresh_stops_before_the_next_target_and_keeps_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Escape lets the running check finish, skips the rest, and keeps the finished result."""
    config, inventory, _ = _manager(tmp_path, ("alpha", "beta"))
    started, release = _blocking_checks(monkeypatch)
    app = _capture_app(monkeypatch)
    assert run_manager_tui(config, inventory) == 0

    async def drive() -> None:
        async with app[0].run_test(size=(70, 12)) as pilot:
            await pilot.press("down", "enter")
            status = lambda: str(app[0].screen.query_one("#refresh-status").render())  # noqa: E731
            await _wait_until(pilot, lambda: status() == "Checking 1 of 2: user:alpha")
            await pilot.press("escape")
            release.set()
            await _wait_until(pilot, lambda: status() == "Cancelled after 1 of 2.")
            assert started == ["user:alpha"]
            assert "user:alpha: Update Available 2.0.0" in str(app[0].screen.query_one("#output-text").render())
            await pilot.press("escape")
            assert type(app[0].screen).__name__ == "HomeScreen"

    asyncio.run(drive())


def test_saved_update_status_survives_opening_a_package_and_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a package check in the package interface, the reopened browser shows it at the same place."""
    from textual.app import App

    import gupkg.tui

    config, inventory, user = _manager(tmp_path, ("alpha", "beta"))
    opened: list[str] = []

    def fake_package_tui(path: str, *, forced_scope=None):
        # The package interface records a check result exactly as a real check does.
        opened.append(path)
        state = Path(path) / ".gupkg" / "state" / "update.toml"
        state.parent.mkdir(parents=True)
        state.write_text(
            'lastSuccessfulCheck = "2026-10-17T14:02:00+00:00"\n'
            'lastStatus = "available"\nlastCandidateVersion = "1.5.0"\n',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(gupkg.tui, "run_tui", fake_package_tui)
    launches: list[str] = []

    def run_app(app, *args, **kwargs):
        launches.append("run")

        async def drive() -> None:
            async with app.run_test(size=(80, 12)) as pilot:
                if len(launches) == 1:
                    # Browse, filter to User, select beta, open its operations.
                    await pilot.press("enter", "enter", "down", "down", "down", "enter", "enter")
                    await pilot.pause(0.2)
                    return
                browser = app.screen
                assert type(browser).__name__ == "BrowserScreen"
                options = browser.query_one("#package-options")
                assert browser.scope_filter == "User"
                assert options.highlighted is not None
                assert "beta" in str(options.get_option_at_index(options.highlighted))
                labels = [str(options.get_option_at_index(i)) for i in range(options.option_count)]
                assert any("beta" in label and "Update Available 1.5.0" in label for label in labels)
                await pilot.press("escape")
                assert type(app.screen).__name__ == "HomeScreen"
                app.exit()

        asyncio.run(drive())
        return app.return_value

    monkeypatch.setattr(App, "run", run_app)

    run_manager_tui(config, inventory)

    assert opened == [str(user / "beta")]
    assert len(launches) == 2
