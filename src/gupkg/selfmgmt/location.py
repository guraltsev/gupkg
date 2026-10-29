"""Find where the running gupkg lives: installed in a root, bundled with a package, or a source checkout.

The command line needs three facts about the gupkg that is running it: the ``$App`` folder whose
``settings.toml`` is a settings layer, the version directory and root that an elevated run must be able to trust,
and the folder that holds its sibling launchers (``gupkg-tui.cmd``, ``gupkg-manager.cmd``).
Nothing is read from ``PATH``, the registry or the current directory: the location is derived from where the gupkg
package itself was loaded from.

Usage and API
-------------
Call ``locate_running_gupkg()`` for the running gupkg, or pass the folder of a ``gupkg`` package to describe another
one. ``RunningGupkg.kind`` is ``installed`` (``<root>\\<package>\\<version>\\App\\gupkg``), ``bundled``
(``<version>\\gupkg\\gupkg``) or ``source`` (anything else, such as a source checkout).

Implementation Approach
-----------------------
The kind follows the folder layout of design section 21.2 and is confirmed by a ``pkg.toml`` in the version directory,
so a folder that merely has the right names is not taken for an installation. A ``source`` run knows neither a
version directory nor a root.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunningGupkg:
    """Where a gupkg package runs from.

    Attributes
    ----------
    kind : str
        ``installed``, ``bundled`` or ``source``.
    package_dir : Path
        The folder of the ``gupkg`` Python package.
    launcher_dirs : tuple of Path
        Folders searched, in order, for the sibling launchers of the same gupkg tree.
    version_dir : Path or None
        The version directory of the package that holds this gupkg (``installed`` and ``bundled`` only).
    root : Path or None
        The root that holds that package (``installed`` and ``bundled`` only).
    app_dir : Path or None
        ``$App`` of an installed gupkg, whose ``settings.toml`` is the second settings layer.
    """

    kind: str
    package_dir: Path
    launcher_dirs: tuple[Path, ...]
    version_dir: Path | None = None
    root: Path | None = None
    app_dir: Path | None = None

    def launcher(self, name: str) -> Path | None:
        """Return the sibling launcher file called *name* in this gupkg tree, or ``None`` when the tree has none."""
        for folder in self.launcher_dirs:
            candidate = folder / name
            if candidate.is_file():
                return candidate
        return None


def locate_running_gupkg(package_dir: Path | None = None) -> RunningGupkg:
    """Return the location of the gupkg whose Python package is in *package_dir* (the running one by default)."""
    folder = Path(package_dir or Path(__file__).resolve().parents[1]).resolve()
    tree = folder.parent
    version_dir = tree.parent

    # An installed gupkg runs from the package's App folder: root = three levels above the package folder.
    if tree.name.lower() == "app" and (version_dir / "pkg.toml").is_file():
        return RunningGupkg("installed", folder, (tree, folder), version_dir, version_dir.parent.parent, tree)

    # A bundled gupkg sits in the version directory's ``gupkg`` folder; its package is two levels up.
    if tree.name.lower() == "gupkg" and (version_dir / "pkg.toml").is_file():
        return RunningGupkg("bundled", folder, (tree, folder), version_dir, version_dir.parent.parent)

    # Anything else (a source checkout): the launchers sit beside the sources.
    return RunningGupkg("source", folder, (folder,))
