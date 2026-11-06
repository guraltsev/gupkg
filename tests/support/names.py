"""The package tables and the machine snapshot that the name-ownership and unowned-file tests share.

``SHIM``, ``SHORTCUT``, ``WRAPPER`` and ``ENV`` are ``pkg.toml`` table templates (``{name}`` is filled in) for a package
that provides a shim, a Start Menu shortcut, a wrapper file or an environment variable. ``state`` reads everything on the
fake machine that a refused install must leave alone.
"""

from __future__ import annotations

from gupkg.system.port import Mode
from tests.support.engine import World
from tests.support.mode import tree_snapshot

# Table template for a package that provides a shim.
SHIM = '[[shim]]\nname = "{name}"\ntarget = "$App\\\\app.exe"\n'
# Table template for a package that provides a Start Menu shortcut.
SHORTCUT = '[[shortcut]]\nname = "{name}"\ntarget = "$App\\\\app.exe"\n'
# Table template for a package that provides a wrapper file (a shim with content).
WRAPPER = '[[shim]]\nname = "{name}"\ncontent = "@echo off"\n'
# Table template for a package that provides an environment variable.
ENV = '[[env]]\nname = "{name}"\nvalue = "$App"\n'


def state(world: World) -> dict[str, object]:
    """Return everything a refused run must leave alone: the root (without run logs), the bin folder, shortcuts, environment, PATH.

    Parameters
    ----------
    world : World
        The world whose user-mode state is captured.

    Returns
    -------
    dict[str, object]
        Snapshots of the user root, bin folder, shortcuts, environment variables and PATH.
    """
    root = {key: value for key, value in tree_snapshot(world.user).items() if "logs" not in key}
    scope = Mode.USER
    return {
        "root": root, "bin": tree_snapshot(world.fake.scope_folders(scope).bin_dir), "shortcuts": sorted(map(str, world.fake.shortcuts())),
        "env": sorted(world.fake.env_values(scope)), "path": list(world.fake.get_path(scope)),
    }
