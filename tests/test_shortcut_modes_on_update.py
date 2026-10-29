"""Cover the removal on update of a shortcut whose ``modes`` no longer include the run's mode (issue 035 T08, T13).

Behavior covered: version 1.0.0 of a package has a shortcut for both modes and a second one for the system mode only; an install in user
mode creates the first shortcut and not the second. Version 2.0.0 limits the first shortcut to ``["system"]``: ``update`` in user mode
removes the shortcut the install recorded, leaves the shim and moves ``current``. Version 3.0.0 lists the user mode again and the
shortcut returns.

Real: the CLI, the engine, the install record and real folders. Mocked: ``FakeSystem`` (shortcuts are records of ``.lnk.json`` files).
Out of scope: the system mode (the same executor code; the mode only decides which shortcuts are desired).
"""

from __future__ import annotations

from pathlib import Path

from gupkg.system.port import Mode
from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


def shortcut(name: str, modes: str) -> str:
    """Return a ``[[shortcut]]`` row named *name* that is wanted in *modes*."""
    return f'[[shortcut]]\nname = "{name}"\ntarget = "$App\\\\app.exe"\nmodes = {modes}\n'


def names(world: CliWorld) -> list[str]:
    """Return the file names of the shortcuts that exist on the fake machine."""
    return sorted(path.name for path in world.fake.shortcuts())


def test_an_update_removes_the_shortcut_whose_modes_no_longer_include_the_run_and_a_later_one_restores_it(tmp_path: Path) -> None:
    """The recorded shortcut goes away when the new version excludes the user mode, and the shim stays."""
    world = CliWorld(tmp_path)
    shim = world.fake.scope_folders(Mode.USER).bin_dir / "tool.exe"
    world.publish("tool", "1.0.0", tables=SHIM + shortcut("Both", '["system", "user"]') + shortcut("SystemOnly", '["system"]'))
    installed = world.cli("install", "tool", "--yes")
    assert installed.code == 0, installed.err
    assert len(names(world)) == 1 and names(world)[0].startswith("Both"), names(world)

    world.publish("tool", "2.0.0", tables=SHIM + shortcut("Both", '["system"]'))
    narrowed = world.cli("update", "tool", "--yes")
    assert narrowed.code == 0, narrowed.err
    assert names(world) == [], names(world)
    assert shim.exists()

    world.publish("tool", "3.0.0", tables=SHIM + shortcut("Both", '["user"]'))
    widened = world.cli("update", "tool", "--yes")
    assert widened.code == 0, widened.err
    assert len(names(world)) == 1 and names(world)[0].startswith("Both"), names(world)
