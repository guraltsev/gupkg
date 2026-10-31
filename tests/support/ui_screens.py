"""Drive the two Textual programs in a headless terminal and read their screens, for the UI program tests.

``run_app`` starts the manager (or ``gupkg-tui``) on a scripted ``gupkg`` double and runs an async scenario against it with
Textual's pilot; ``screen_text`` returns the rows of the screen as the person sees them; ``wait_for`` pauses in small steps
until something shows (never a fixed sleep); ``update_run`` schedules a plan and a successful ``update`` on the double.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from gupkg_manager.app.app import ManagerApp
from gupkg_tui.app import TuiApp
from tests.support.ui import FakeGupkg, entry, make_env, plan_doc, plan_entry, result


def screen_text(app) -> list[str]:
    """Return the text of every row of the screen as the person sees it (the compositor's rendering, trailing blanks removed)."""
    return [strip.text.rstrip() for strip in app.screen._compositor.render_strips()]


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


def update_run(fake: FakeGupkg, tmp_path: Path, names=("alpha", "beta"), *, records=None, delay: float = 0.0, **extra) -> None:
    """Schedule a plan and a successful ``update`` of *names* in the user root, with *records* as each package's own events (the call waits *delay* seconds first)."""
    fake.respond("update:plan", doc=plan_doc([plan_entry(f"{name}", name) for name in names]))
    doc = result("update", [entry(name) for name in names])
    packages = [{"name": name, "path": str(tmp_path / "home" / "opt" / name), "records": records or [{"event": "phase_start", "phase": "Resolve"}, {"event": "phase_end", "phase": "Resolve", "status": "ok", "elapsed": 0.1}]} for name in names]
    fake.respond("update", doc=doc, sleep=delay, events={"dir": str(tmp_path / "home" / "opt" / ".gupkg" / "logs"), "result": doc, "packages": packages, **extra})


def run_app(tmp_path: Path, fake: FakeGupkg, scenario: Callable, *, size=(80, 24), program: str = "manager", target: str = "alpha", toasts: bool = False, root: Path | None = None, **settings) -> None:
    """Start the manager (or ``gupkg-tui``) in a headless terminal of *size* and run the async *scenario* ``(app, pilot)`` against it."""
    env = make_env(tmp_path, fake, settings=settings.get("settings", ""), root=root)
    app = ManagerApp(env) if program == "manager" else TuiApp(env, target)

    async def main() -> None:
        """Run the scenario inside the app."""
        async with app.run_test(size=size, notifications=toasts) as pilot:
            await scenario(app, pilot)

    asyncio.run(main())


async def open_browser(app, pilot) -> None:
    """Wait until the browser shows the two packages."""
    await wait_for(pilot, lambda: has(app, "alpha") and has(app, "beta"), "the browser to list the packages")
