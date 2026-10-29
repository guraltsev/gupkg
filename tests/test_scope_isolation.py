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
import traceback
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

    def __init__(self, folder: Path, monkeypatch: pytest.MonkeyPatch, *, program_root: Path | None = None) -> None:
        self.folder = str(folder).lower()
        # A user run judges the root its own gupkg program is installed in (is it admin-only?) to pick its root (issue 035 Q11): that
        # folder itself, and nothing below it, may be looked at. Its existence and access list are not data of the system mode.
        self.program_root = os.path.normcase(os.path.normpath(program_root)) if program_root is not None else None
        self.paths: list[str] = []
        self.active = True
        real_open, real_scandir, real_listdir = builtins.open, os.scandir, os.listdir

        def note(path: object) -> None:
            """Remember *path* when it lies in the watched folder."""
            try:
                text = os.fspath(path)  # type: ignore[arg-type]
            except TypeError:
                return
            if isinstance(text, bytes):
                text = os.fsdecode(text)
            if self.program_root is not None and os.path.normcase(os.path.normpath(text)) == self.program_root:
                return
            if self.active and str(text).lower().startswith(self.folder):
                where = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_stack()[-8:-2]]
                self.paths.append(f"{text} <- {where}")

        def watch(owner: object, name: str) -> None:
            """Replace ``owner.name`` by a function that notes its first argument and calls the original."""
            original = getattr(owner, name)

            def watched(first, *args, **kwargs):
                note(first)
                return original(first, *args, **kwargs)

            monkeypatch.setattr(owner, name, watched)

        for owner, name in (
            (builtins, "open"), (io, "open"), (os, "scandir"), (os, "listdir"), (os, "stat"), (os, "lstat"),
            (os.path, "exists"), (os.path, "isdir"), (os.path, "isfile"), (os.path, "islink"),
        ):
            watch(owner, name)
        for name in ("exists", "is_dir", "is_file", "stat", "lstat", "iterdir", "read_text", "read_bytes"):
            # A method of Path: the first argument is the path itself.
            watch(Path, name)

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
    touches = Touches(other_root, monkeypatch, program_root=world.location.root)
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
    touches.active = False
    assert touches.paths == [], touches.paths
    assert not marker.exists()
    assert tree_snapshot(other_root) == other_before
    assert not [call for call in world.fake.process_log if str(other_root) in " ".join(map(str, call))]


def test_the_recorder_sees_the_poison_when_the_poisoned_root_is_the_runs_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a run on ``--root <poisoned root>`` fails on the invalid TOML, and the recorder logs the open of that file."""
    world = CliWorld(tmp_path)
    root, _ = poison(world, "user")
    touches = Touches(root, monkeypatch)

    result = world.cli("--root", str(root), "list")

    touches.active = False
    assert result.code == 2 and "not valid TOML" in result.err, result.err
    assert any("settings.toml" in path for path in touches.paths), touches.paths


def test_a_mere_exists_of_the_other_root_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: ``Path.exists`` and ``os.stat`` of the watched folder are noted (the recorder hooks more than ``open``)."""
    world = CliWorld(tmp_path)
    root, _ = poison(world, "user")
    touches = Touches(root, monkeypatch)

    root.exists()
    os.stat(root)

    touches.active = False
    assert len(touches.paths) >= 2, touches.paths