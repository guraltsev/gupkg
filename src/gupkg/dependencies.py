"""Install gupkg runtime dependencies while protecting package-local hooks.

The ``gupkg`` runtime installs its declared third-party dependencies into a
per-user site-packages directory without changing the interpreter that launches
``gupkg``. Package-local hooks do not install imports by default: callers must
explicitly opt in before their missing modules can be installed. A normal
interpreter installs into ``%LOCALAPPDATA%\\gupkg\\embedded\\site-packages``;
the bundled embeddable CPython installs into its adjacent
``python\\Lib\\site-packages`` directory. Each directory is added to the current
process before pip is invoked.

An elevated run never installs anything. It imports dependencies only from the
bundled ``python\\Lib\\site-packages`` directory, after confirming that the
directory is writable by administrators alone, and reports the repair command
(``gupkg manager self repair-deps``) when a dependency is missing or damaged.
"""

from __future__ import annotations

import importlib
import os
import site
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .elevation import elevated_mode, is_elevated, require_trusted_directory

__all__ = [
    "MissingLocalDependencyError",
    "ensure_dependency",
    "ensure_runtime_dependencies",
    "install_missing_dependency",
    "is_elevated",
    "run_with_missing_dependencies",
]


class MissingLocalDependencyError(RuntimeError):
    """Report a package-local dependency that gupkg deliberately did not install."""


# Keep each optional gupkg feature's trusted dependencies explicit and auditable.
_RUNTIME_DEPENDENCIES = {"tui": ("textual",)}

# A bare top-level import can succeed for an empty or damaged package directory
# (it becomes a namespace package), so these imports prove the install is usable.
_IMPORT_PROBES = {"textual": "textual.app"}


def ensure_runtime_dependencies(feature: str) -> None:
    """Make every declared dependency for one gupkg feature importable.

    Parameters
    ----------
    feature : str
        Name of the gupkg feature whose declared dependencies are required.
    """
    for module_name in _RUNTIME_DEPENDENCIES.get(feature, ()):
        ensure_dependency(module_name)


def run_with_missing_dependencies(
    callback: Callable[..., Any], *args: Any, autoinstall: bool = False
) -> Any:
    """Run a hook and optionally install missing third-party imports before retrying it.

    Parameters
    ----------
    callback : Callable[..., Any]
        Trusted package-local hook to invoke.
    *args : Any
        Positional arguments forwarded to *callback*.
    autoinstall : bool, default=False
        Whether missing package-local imports may be installed before retrying.

    Returns
    -------
    Any
        The value returned by *callback*.

    Raises
    ------
    MissingLocalDependencyError
        If a hook needs an unavailable import and automatic installation is off.
    RuntimeError
        If the user dependency environment cannot be prepared or populated.
    """
    # A hook might import several independent dependencies, but a finite retry
    # limit prevents an invalid import name from repeatedly invoking installers.
    for _ in range(3):
        try:
            return callback(*args)
        except ModuleNotFoundError as exc:
            dependency = exc.name
            if not dependency:
                raise

            # Package-local code is trusted but still package-owned. Do not let
            # it trigger network installs unless the caller explicitly opted in.
            if not autoinstall:
                raise MissingLocalDependencyError(
                    f"Package-local dependency unavailable: {dependency}. "
                    "Install it yourself or rerun with --allow-hook-dependency-install."
                ) from exc

            # Install only the missing top-level import because package indexes
            # identify distributions at that level rather than by dotted module.
            install_missing_dependency(dependency.split(".", maxsplit=1)[0])
    raise RuntimeError("A package-local hook required more than three missing dependencies")


def ensure_dependency(module_name: str) -> None:
    """Make one runtime dependency importable by the current gupkg process.

    Parameters
    ----------
    module_name : str
        Top-level Python import required by gupkg itself.

    Raises
    ------
    RuntimeError
        If the isolated dependency environment cannot install the dependency.
    """
    probe = _IMPORT_PROBES.get(module_name, module_name)

    # An elevated run uses only the admin-only bundled directory and never
    # installs, so it takes a separate path that fails with a repair command.
    if elevated_mode():
        _use_trusted_dependency(module_name, probe)
        return
    if _module_is_importable(probe):
        return
    if probe == module_name:
        install_missing_dependency(module_name)
    else:
        install_missing_dependency(module_name, probe=probe)


def install_missing_dependency(module_name: str, probe: str | None = None) -> None:
    """Install one importable dependency into gupkg's per-user environment.

    Parameters
    ----------
    module_name : str
        Top-level import name reported by Python.
    probe : str, optional
        Import that must succeed for the installation to count as usable;
        defaults to *module_name*. Installation upgrades in place so a damaged
        existing copy is replaced rather than skipped by pip.

    Raises
    ------
    RuntimeError
        If dependency installation fails, or the run is elevated (elevated
        runs never install implicitly).
    """
    if elevated_mode():
        raise RuntimeError(
            f"Elevated runs never install dependencies implicitly ({module_name!r} is missing). "
            "Run from an elevated prompt: gupkg manager self repair-deps"
        )
    distribution = _distribution_name(module_name)
    site_packages = _dependency_site_packages()
    probe = probe or module_name

    # Keep system and bundled interpreters isolated, without modifying the
    # system interpreter or writing bundled dependencies outside its package.
    site_packages.mkdir(parents=True, exist_ok=True)
    site.addsitedir(str(site_packages))
    if _module_is_importable(probe):
        return

    # Target the owned directory directly instead of changing the selected
    # interpreter. The bundled runtime includes pip for this exact workflow.
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--target",
        str(site_packages),
        distribution,
    ]

    # Invoke the selected installer only after the dependency directory has
    # been put on the current process's import path for immediate retrying.
    print(f"[gupkg] Installing missing dependency with pip: {distribution}")
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Could not install missing dependency {distribution!r} with pip")

    site.addsitedir(str(site_packages))
    importlib.invalidate_caches()
    if not _module_is_importable(probe):
        raise RuntimeError(
            f"Installed {distribution!r}, but Python still cannot import {probe!r}"
        )


def _use_trusted_dependency(module_name: str, probe: str) -> None:
    """Make a locked dependency importable from the admin-only bundled directory."""
    site_packages = _dependency_site_packages()

    # Never import from a directory that non-administrators can modify.
    if site_packages.exists():
        require_trusted_directory(site_packages)

    # Append the directory as a plain path entry: unlike ``site.addsitedir`` it
    # cannot execute ``.pth`` files, and it never shadows the standard library.
    if str(site_packages) not in sys.path:
        sys.path.append(str(site_packages))
        importlib.invalidate_caches()
    if not _module_is_importable(probe):
        raise RuntimeError(
            f"Dependency {_distribution_name(module_name)!r} is missing or damaged: "
            f"cannot import {probe!r} from {site_packages}. "
            "Run from an elevated prompt: gupkg manager self repair-deps"
        )


def _dependency_site_packages() -> Path:
    """Return the package directory for the active interpreter kind."""
    if elevated_mode() or os.environ.get("GUPKG_BUNDLED_RUNTIME") == "1":
        # The marker is set by the local bootstrap. Derive the target from this
        # module rather than from sys.executable or a caller-provided path.
        return Path(__file__).resolve().parent / "python" / "Lib" / "site-packages"

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base_directory = Path(local_app_data) / "gupkg"
    else:
        base_directory = Path.home() / "AppData" / "Local" / "gupkg"
    return base_directory / "embedded" / "site-packages"


def _module_is_importable(module_name: str) -> bool:
    """Return whether the current process can import one module name."""
    try:
        __import__(module_name)
    except ImportError:
        return False
    return True


def _distribution_name(module_name: str) -> str:
    """Return the usual package-index distribution name for an import name."""
    # These projects intentionally expose import names different from their
    # package-index distribution names; ordinary imports install as themselves.
    aliases = {
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "cv2": "opencv-python",
        "dateutil": "python-dateutil",
        "yaml": "PyYAML",
    }
    return aliases.get(module_name, module_name)
