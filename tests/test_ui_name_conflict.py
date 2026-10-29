"""Cover how the manager shows a name-ownership refusal: the CLI's ``name-conflict`` failure is on the run screen, in words.

Behaviour covered: an update that the CLI refuses with the error code ``name-conflict`` ends on the run screen with the whole
message (both package names, the name, the way out), the verdict line and the one-line failure summary, under the hint line
and above the status line, at 80x24.

Real: the Textual manager, its run screen, session and follower. Mocked: the ``gupkg`` executable (a scripted double).
Out of scope: the refusal itself (``test_engine_name_conflicts.py``).
"""

from __future__ import annotations

from pathlib import Path

from gupkg_ui.screens.run import RunScreen
from tests.test_ui_screens import fake, has, open_browser, run_app, screen_text, update_run, wait_for  # noqa: F401
from tests.ui_support import FakeGupkg, entry, result

MESSAGE = (
    "alpha cannot be installed: it would provide the shim 'tool.exe' (C:\\Users\\me\\opt\\bin\\tool.exe), which the installed "
    "package 'beta' already provides. Uninstall 'beta' first, or use a package definition with a different name."
)


def test_the_run_screen_shows_the_name_conflict_refusal(tmp_path: Path, fake: FakeGupkg) -> None:
    """The refusal text (both packages, the name, what to do) is on the finished run screen at 80x24, with the hint and status lines."""
    update_run(fake, tmp_path, ("alpha",), records=[
        {"event": "phase_start", "phase": "Resolve"}, {"event": "error", "text": MESSAGE},
        {"event": "phase_end", "phase": "Resolve", "status": "failed", "elapsed": 0.1},
    ])
    failing = result("update", [entry("alpha", ok=False, status="failed", code=2, error={"code": "name-conflict", "message": MESSAGE})])
    fake.scenario["update"][0]["doc"] = failing
    fake.scenario["update"][0]["events"]["result"] = failing
    fake.respond("update", **fake.scenario["update"].pop(0))
    shown: list[list[str]] = []

    async def scenario(app, pilot) -> None:
        """Run the refused update and keep what the screen shows."""
        await open_browser(app, pilot)
        await pilot.press("u")
        await wait_for(pilot, lambda: has(app, "Update 1 package"), "the plan")
        await pilot.press("y")
        await wait_for(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.outcome is not None, "the run to finish")
        shown.append(screen_text(app))

    run_app(tmp_path, fake, scenario, size=(80, 24))

    text = " ".join(" ".join(shown[0]).split())
    assert "alpha cannot be installed: it would provide the shim 'tool.exe'" in text
    assert "which the installed package 'beta' already provides. Uninstall 'beta' first" in text
    assert "or use a package definition with a different name." in text
    assert any("Failed (exit code 2)" in row for row in shown[0]) and shown[0][0].strip() and shown[0][-1].strip()
    print("\n".join(shown[0]))
