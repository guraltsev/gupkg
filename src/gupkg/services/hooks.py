"""Load package hooks (hook API 2) and give them one typed context.

A hook is a Python module below a package's ``pkg.local`` folder that declares
``PKG_MODULE_API = 2`` and defines one entry point: ``track(ctx)`` (in a tracker, returns
a ``TrackResult``), ``unpack(ctx)``, ``prepare(ctx)`` or ``populate(ctx)``. Every entry
point receives a ``HookContext`` that carries the package and version, the ``System``
boundary, the shared HTTP client, the paths of the work in progress, and a reporter.
Hooks are trusted code: Python cannot sandbox them, so a version-directory hook may use
the network (decision D28); only update discovery is forbidden there, and validation
(``gupkg.pkgcheck``) enforces that. A hook that declares another API version is rejected
like any other invalid hook.

Usage and API
-------------
``load_hook(root, "pkg.local/name.py", system)`` imports a hook and checks its API version; in an
elevated run it first refuses a folder that is not admin-only (``require_trusted_code``).
``call_track``, ``call_unpack``, ``call_prepare`` and ``call_populate`` run an entry point
and check what it returns. ``HookContext.to_json()`` is the document a non-Python populate
script receives on standard input. ``gupkg.hooklib`` re-exports the helpers hooks use.

Implementation Approach
-----------------------
The module is loaded under a unique name with bytecode writing disabled and its sibling
modules importable, then removed from ``sys.modules``, so one hook's imports never leak
into another's. The path is confined to ``pkg.local`` after resolving links. Progress and
messages go through the event reporter, never to standard output.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from ..extractors import find_7z
from ..model.manifest import LOCAL_DIR, TrackResult
from ..report.events import Step, Warning, emit, subprocess_lines
from ..system.port import System, is_elevated_run, run_checked
from .http import HttpClient
from .safepath import UnsafePath, confine

PKG_MODULE_API = 2


class HookError(RuntimeError):
    """Report a hook that cannot be loaded, declares the wrong API, or returns something invalid."""


class HookReporter:
    """Let a hook report what it does through the run's event stream."""

    def step(self, text: str, kind: str = "info") -> None:
        """Report one detail line (*kind* is ``info``, ``ok``, ``add`` or ``remove``)."""
        emit(Step(str(text), kind))

    def warning(self, text: str) -> None:
        """Report a problem that does not stop the work."""
        emit(Warning(str(text)))


@dataclass(frozen=True)
class HookPaths:
    """The folders and files a hook may work with.

    Attributes
    ----------
    package_root : Path
        The package's folder.
    version_root : Path
        The version directory (or ``_check_updates``) that holds ``pkg.toml`` and ``pkg.local``.
    app : Path or None
        The staged ``App`` folder an ``unpack``, ``prepare`` or ``populate`` hook fills or changes.
    artifact : Path or None
        The downloaded file, for an ``unpack`` hook.
    work : Path or None
        A scratch folder that is deleted after the run.
    cache : Path or None
        The download cache folder.
    """

    package_root: Path
    version_root: Path
    app: Path | None = None
    artifact: Path | None = None
    work: Path | None = None
    cache: Path | None = None


@dataclass(frozen=True)
class HookContext:
    """Everything a hook entry point receives.

    Attributes
    ----------
    package : str
        The package name.
    version : str or None
        The version being processed; ``None`` in a tracker.
    system : System
        The side-effect boundary; run child processes through ``run`` or ``system.run``.
    http : HttpClient
        The shared HTTP client: gupkg's User-Agent, timeout, retry and token policy.
    paths : HookPaths
        Where the hook may read and write.
    options : mapping
        Free-form values from the manifest (the ``[track]`` options, for a track hook).
    captures : mapping
        The named values found so far (the version and the provider captures).
    reporter : HookReporter
        Reports messages and warnings to the run's events.
    """

    package: str
    version: str | None
    system: System
    http: HttpClient
    paths: HookPaths
    options: Mapping[str, Any] = field(default_factory=dict)
    captures: Mapping[str, str] = field(default_factory=dict)
    reporter: HookReporter = field(default_factory=HookReporter)

    @property
    def api(self) -> int:
        """Return the hook API version this context implements."""
        return PKG_MODULE_API

    def run(self, argv: Sequence[Any], cwd: Path | None = None, env: Mapping[str, str] | None = None,
            timeout: float | None = None) -> str:
        """Run a child process that must succeed and return its stripped standard output.

        Its output lines are reported as events. Raises ``ProcessFailed`` on a non-zero status.
        """
        command = [str(part) for part in argv]
        return run_checked(self.system, command, cwd=cwd, env=env, on_line=subprocess_lines(command), timeout=timeout)

    def seven_zip(self) -> Path:
        """Return the bundled 7-Zip executable (or a system one when the bundle lacks it)."""
        return find_7z()

    def to_json(self) -> str:
        """Return the context as the JSON document a non-Python populate script reads on standard input."""
        paths = {name: (str(value) if value is not None else None) for name, value in vars(self.paths).items()}
        return json.dumps(
            {"api": self.api, "package": self.package, "version": self.version, "paths": paths,
             "options": _jsonable(self.options), "captures": dict(self.captures)},
            sort_keys=True,
        )


def require_trusted_code(system: System, root: Path, script: Path | None = None) -> None:
    """Refuse to run package code from *root* in an elevated run unless only administrators can change it.

    An elevated gupkg runs no code that a standard user can modify (decision D19). The check
    fails closed: when the run is elevated (or marked so), nothing is imported or started unless
    *root*, its ``pkg.local`` folder and, when *script* is given, every folder between the script
    and *root* and the script file itself are admin-only (a file moved into an admin-only folder
    keeps its own user-writable permissions, so the file is judged too). A normal run is not
    restricted: the code is the user's own. Call it once with the version directory before any
    file is read, and again with *script* once its path is known.

    Raises
    ------
    HookError
        In an elevated run, if any of those paths is not admin-only or cannot be judged.
    """
    if not is_elevated_run(system):
        return
    places = [Path(root), Path(root) / LOCAL_DIR]
    if script is not None:
        script = Path(script)
        folders = [parent for parent in script.parents if _is_below(parent, Path(root))]
        places += [*reversed(folders), script]
    for place in places:
        reason = system.directory_untrusted_reason(place)
        if reason is not None:
            raise HookError(
                f"Refusing to run code from {place} in an elevated run: it is not admin-only ({reason})"
            )


def _is_below(path: Path, root: Path) -> bool:
    """Return whether *path* is *root* or inside it (by name, no file system access)."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_hook(root: Path, reference: str, system: System, *, forbid_track: bool = False) -> ModuleType:
    """Import the hook module *reference* (``pkg.local/name.py``) found below *root*.

    Parameters
    ----------
    root : Path
        The version directory or tracker directory that owns ``pkg.local``.
    reference : str
        The module path relative to *root*; it must be a ``.py`` file below ``pkg.local``.
    system : System
        The boundary that answers whether the run is elevated and whether *root* is admin-only;
        in an elevated run an untrusted folder is refused before anything is imported.
    forbid_track : bool, default=False
        Refuse a module that exposes a name ``track`` once it is loaded. A version directory's hooks have no
        update discovery (design 9.3); the static scan of ``gupkg.pkgcheck`` cannot see a name that is built at
        run time, this check can.

    Returns
    -------
    ModuleType
        The executed module, which declares ``PKG_MODULE_API = 2``.

    Raises
    ------
    HookError
        If the folder is untrusted in an elevated run, the path leaves ``pkg.local`` or crosses a
        link, the file is missing, its top-level code raises anything, or it declares another API
        version.
    """
    require_trusted_code(system, Path(root))
    normal = reference.replace("\\", "/")
    if not normal.startswith(f"{LOCAL_DIR}/") or not normal.lower().endswith(".py"):
        raise HookError(f"{reference} must be a .py file below {LOCAL_DIR}/")
    try:
        path = confine(Path(root), normal)
    except UnsafePath as error:
        raise HookError(f"{reference}: {error}") from error
    local = Path(os.path.realpath(Path(root) / LOCAL_DIR))
    real = Path(os.path.realpath(path))
    try:
        inside = os.path.commonpath([os.path.normcase(local), os.path.normcase(real)]) == os.path.normcase(local)
    except ValueError:  # another drive
        inside = False
    if not path.is_file() or not inside:
        raise HookError(f"{reference} does not exist inside {LOCAL_DIR}")
    require_trusted_code(system, Path(root), path)

    # A unique name per file revision keeps unrelated hooks from sharing import state.
    name = "_gupkg_hook_" + hashlib.sha256(f"{real}{real.stat().st_mtime_ns}".encode()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, real, submodule_search_locations=[str(local)])
    if spec is None or spec.loader is None:
        raise HookError(f"Cannot load {reference}")
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.modules[name] = module
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    except ImportError as error:
        raise HookError(
            f"{reference} cannot import {error.name or error!s}; hooks may import only the standard "
            f"library, what the gupkg bundle ships, and modules in their own {LOCAL_DIR} folder"
        ) from error
    except KeyboardInterrupt:
        raise
    except BaseException as error:  # noqa: BLE001 - a hook's import-time code may fail, or call sys.exit(), in any way
        raise HookError(f"{reference} failed while loading: {type(error).__name__}: {error}") from error
    finally:
        sys.dont_write_bytecode = previous
        for imported in list(sys.modules):
            if imported == name or imported.startswith(name + "."):
                sys.modules.pop(imported, None)
    if getattr(module, "PKG_MODULE_API", None) != PKG_MODULE_API:
        raise HookError(f"{reference} must declare PKG_MODULE_API = {PKG_MODULE_API}")
    if forbid_track and hasattr(module, "track"):
        raise HookError(f"{reference} exposes track: a version directory has no update discovery; it lives only in _check_updates")
    return module


def call_track(module: ModuleType, context: HookContext) -> TrackResult:
    """Run ``track(ctx)`` and return its ``TrackResult``.

    Raises
    ------
    HookError
        If the module has no ``track`` function or it returns anything but a ``TrackResult``.
    """
    result = _call(module, "track", context)
    if not isinstance(result, TrackResult):
        raise HookError(f"track(ctx) must return a TrackResult, not {type(result).__name__}")
    return result


def call_unpack(module: ModuleType, context: HookContext) -> None:
    """Run ``unpack(ctx)``, which fills ``ctx.paths.app`` from ``ctx.paths.artifact``."""
    _call(module, "unpack", context)


def call_prepare(module: ModuleType, context: HookContext) -> None:
    """Run ``prepare(ctx)``, which adjusts the staged ``ctx.paths.app``."""
    _call(module, "prepare", context)


def call_populate(module: ModuleType, context: HookContext) -> None:
    """Run ``populate(ctx)``, a custom populate script that fills ``ctx.paths.app`` by itself."""
    _call(module, "populate", context)


def _call(module: ModuleType, name: str, context: HookContext) -> Any:
    """Run the entry point *name*; a ``sys.exit`` inside it is a ``HookError`` (it would end gupkg itself)."""
    try:
        return _entry(module, name)(context)
    except SystemExit as error:
        raise HookError(f"{name}(ctx) called sys.exit({error.code!r})") from error


def _entry(module: ModuleType, name: str):
    """Return the callable entry point *name* of *module*."""
    function = getattr(module, name, None)
    if not callable(function):
        raise HookError(f"The hook does not define {name}(ctx)")
    return function


def _jsonable(value: Any) -> Any:
    """Return *value* with paths and other objects turned into JSON-safe text."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
