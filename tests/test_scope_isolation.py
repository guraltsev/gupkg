"""Cover issue 035 acceptance item 1: a run never considers the other root, even when that root is poisoned.

Behavior covered: with the other mode's root holding a ``settings.toml`` that is not TOML, a ``state.toml`` of junk, a package whose
populate script would write a marker file and a registry cache of another revision, the commands ``list``, ``check``, ``install``,
``update``, ``cleanup`` and ``registry sync`` and a manager started on the browser all succeed, open no file and list no folder
below the other root, print nothing that names it, start no process of it and leave the marker unwritten. It holds both ways: an
elevated run (system root) with a poisoned user root, and a normal run (user root) with a poisoned system root.

Real: the argument parser, the engine, the real in-process reads of the manager, the Textual browser (headless) and real folders.
Mocked: ``FakeSystem`` (elevation flag, downloads, processes), and the ``gupkg`` calls of the manager (a recording double).
Out of scope: the real UAC elevation and a real elevated shell (``tests/manual_smoke.md``, ``035-T13-*``).
"""

from __future__ import annotations

import asyncio
import builtins
import io
import os
from pathlib import Path

import pytest

from gupkg.system.fake import ProcessCall
from gupkg.system.port import ProcessResult
from gupkg_manager.app.app import ManagerApp
from gupkg_ui.startup import UiEnvironment, load_ui_settings
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation, tree_snapshot  # noqa: F401
from tests.test_cli_tools import SOURCE, registry_zip, version_files
from tests.test_ui_screens import screen_text, wait_for
from tests.ui_support import FakeGupkg


class Touches:
    """Record every path a test opens, lists or inspects below one folder, while it is active."""

    def __init__(self, folder: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.folder = str(folder).lower()
        self.paths: list[str] = []
        real_open, real_scandir, real_listdir = builtins.open, os.scandir, os.listdir

        def note(path: object) -> None:
            """Remember *path* when it lies in the watched folder."""
            try:
                text = os.fspath(path)  # type: ignore[arg-type]
            except TypeError:
                return
            if isinstance(text, bytes):
                text = os.fsdecode(text)
            if str(text).lower().startswith(self.folder):
                self.paths.append(str(text))

        def watched_open(file, *args, **kwargs):
            note(file)
            return real_open(file, *args, **kwargs)

        def watched_scandir(path="."):
            note(path)
            return real_scandir(path)

        def watched_listdir(path="."):
            note(path)
            return real_listdir(path)

        monkeypatch.setattr(builtins, "open", watched_open)
        monkeypatch.setattr(io, "open", watched_open)
        monkeypatch.setattr(os, "scandir", watched_scandir)
        monkeypatch.setattr(os, "listdir", watched_listdir)


def poison(world: CliWorld, mode: str) -> tuple[Path, Path]:
    """Poison the root of *mode*; return the root and the marker that a hook of it would write."""
    root = world.root(mode)
    marker = world.base / "MARKER-HOOK-RAN"
    (root / ".gupkg").mkdir(parents=True, exist_ok=True)
    (root / ".gupkg" / "settings.toml").write_text("schema = [\nthis is not toml {{{\n", encoding="utf-8")
    (root / ".gupkg" / "state.toml").write_text("\x00junk = = =\n[[[\n", encoding="utf-8")
    world.publish_script("poison", "1.0.0", mode=mode)

    def hook(call: ProcessCall) -> ProcessResult:
        """Play the poisoned populate script: it writes the marker."""
        marker.write_text("the hook of the other root ran", encoding="utf-8")
        return ProcessResult(0, "", "")

    world.fake.processes["populate.cmd"] = hook
    world.registry({"poisonpkg": {"versions": {"9.9.9": {"app.exe": "x"}}}}, mode=mode, revision="b" * 40)
    return root, marker


@pytest.mark.parametrize("elevated", [True, False], ids=["elevated-system-run", "normal-user-run"])
def test_a_run_never_considers_the_poisoned_root_of_the_other_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, elevated: bool) -> None:
    """Every command and the manager succeed, touch nothing below the other root, never name it and never run its hook."""
    world = CliWorld(tmp_path, elevated=elevated)
    own, other = ("system", "user") if elevated else ("user", "system")
    world.publish("tool", "1.0.0", mode=own)
    world.fake.add_url(SOURCE, registry_zip(version_files("gamma", "1.0")))
    other_root, marker = poison(world, other)
    other_before = tree_snapshot(other_root)
    touches = Touches(other_root, monkeypatch)
    outputs: list[str] = []

    def run(*argv: str) -> None:
        """Run one command; it must exit 0 and its output joins *outputs*."""
        result = world.cli(*argv)
        assert result.code == 0, (argv, result.out, result.err)
        outputs.append(result.out + result.err)

    run("registry", "remove", "main")
    run("registry", "add", "main", SOURCE)
    run("registry", "sync")
    run("list")
    run("install", "tool", "--yes")
    world.publish("tool", "2.0.0", mode=own)
    run("update", "tool", "--yes")
    run("check")
    run("cleanup", "--yes")

    document = world.cli("list", "--json").document()
    assert document["mode"] == own and Path(document["root"]) == world.root(own)
    assert [item["name"] for item in document["packages"]].count("poison") == 0

    # The manager browser, on the real in-process reads of the run's own scope.
    fake = FakeGupkg(tmp_path)
    settings = load_ui_settings(settings_path=None, env=world.env, location=world.location, system=world.fake)
    environment = UiEnvironment(fake.client(), settings, world.location, world.fake, world.env, None)
    shown: list[str] = []

    async def scenario() -> None:
        """Open the browser and wait until it lists the run's own package."""
        app = ManagerApp(environment)
        async with app.run_test(size=(100, 30)) as pilot:
            await wait_for(pilot, lambda: any("tool" in row for row in screen_text(app)), "the browser to list the run's package")
            shown.extend(screen_text(app))

    asyncio.run(scenario())

    text = "\n".join(outputs + shown).lower()
    assert str(other_root).lower() not in text and "poison" not in text and "marker" not in text
    assert touches.paths == [], touches.paths
    assert not marker.exists()
    assert tree_snapshot(other_root) == other_before
    assert not [call for call in world.fake.process_log if str(other_root) in " ".join(map(str, call))]
