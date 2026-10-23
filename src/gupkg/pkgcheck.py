"""Validate one format 2 version or tracker directory (the library behind ``gupkg pkg check``).

Usage and API
-------------
``check_directory(path)`` reads ``<path>\\pkg.toml``, validates it and checks the
directory around it, and returns a ``CheckReport`` (errors, warnings, the parsed
manifest and the kind of ``App``). A directory named ``_check_updates`` is checked
as a tracker, every other directory as a version. ``defines_track(source)`` tells
whether Python source defines ``track``.

Implementation Approach
-----------------------
The manifest rules live in ``gupkg.model.manifest``. This module adds what needs the
directory: the files that ``[source]``, ``[[prepare]]`` and ``[track]`` name must exist
inside it, a version directory's ``pkg.local`` must hold no module that defines
``track`` (update discovery lives only in ``_check_updates``, design section 9.3), and
``App`` must be present or recoverable. A hook that imports ``urllib`` or any other
module is not inspected: version-directory scripts may use the network (decision D28).
The ``track`` scan reads each Python file's syntax tree and fails closed: a file that
cannot be parsed is an error, because it could not be shown to define no ``track``.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

from .model.manifest import (
    APP_MISSING_ERROR,
    LOCAL_DIR,
    Manifest,
    ManifestError,
    ManifestKind,
    parse_manifest,
)
from .model.version import MANIFEST_NAME, TRACKER_DIR_NAME, is_version_name


@dataclass(frozen=True)
class CheckReport:
    """The result of checking one directory."""

    path: Path
    kind: ManifestKind | None
    manifest: Manifest | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    app_kind: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the directory has no errors."""
        return not self.errors


def defines_track(source: str) -> bool:
    """Return whether Python *source* defines or imports a name ``track`` at module level.

    Parameters
    ----------
    source : str
        Python source text.

    Returns
    -------
    bool
        ``True`` when a function, class, assignment or import binds ``track`` outside
        any function or class body.

    Raises
    ------
    SyntaxError
        If the source cannot be parsed.
    """
    tree = ast.parse(source)
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == "track":
                return True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any((alias.asname or alias.name).split(".")[0] == "track" for alias in node.names):
                return True
        for target in _assigned_names(node):
            if target == "track":
                return True
        stack.extend(child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt))
    return False


def _assigned_names(node: ast.AST) -> list[str]:
    """Return the plain names that a module-level statement binds."""
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    names: list[str] = []
    stack = list(targets)
    while stack:
        target = stack.pop()
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            stack.extend(target.elts)
    return names


def _inside(path: Path, root: Path) -> bool:
    """Return whether the real *path* lies inside the real *root*."""
    real_path, real_root = os.path.realpath(path), os.path.realpath(root)
    return os.path.normcase(real_path).startswith(os.path.normcase(real_root).rstrip("\\/") + os.sep)


def check_directory(path: Path) -> CheckReport:
    """Validate a version directory or a ``_check_updates`` directory.

    Parameters
    ----------
    path : Path
        The directory that holds ``pkg.toml``.

    Returns
    -------
    CheckReport
        Every problem found. An unreadable or invalid ``pkg.toml`` stops the check
        after its own errors, because the other checks depend on the manifest.
    """
    path = Path(path)
    kind = ManifestKind.TRACKER if path.name == TRACKER_DIR_NAME else ManifestKind.VERSION
    errors: list[str] = []
    warnings: list[str] = []

    if not path.is_dir():
        return CheckReport(path, kind, errors=(f"{path} is not a directory",))
    if kind is ManifestKind.VERSION and not is_version_name(path.name):
        errors.append(
            f"'{path.name}' cannot be a version directory: the name is reserved or not a safe folder name"
        )
    manifest_path = path / MANIFEST_NAME
    try:
        text = manifest_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return CheckReport(path, kind, errors=(*errors, f"Cannot read {manifest_path}: {exc}"))
    try:
        manifest = parse_manifest(text, kind)
    except ManifestError as exc:
        return CheckReport(path, kind, errors=(*errors, *exc.problems))

    # The files the manifest names must exist and must stay inside this directory.
    named = list(manifest.referenced_hooks())
    if manifest.track is not None and manifest.track.provider == "module":
        named.append(str(manifest.track.options["module"]))
    for name in named:
        target = path / name
        if not target.is_file():
            errors.append(f"{name} is named in pkg.toml but does not exist")
        elif not _inside(target, path):
            errors.append(f"{name} resolves to a place outside this directory")

    if kind is ManifestKind.VERSION:
        errors.extend(_track_hook_errors(path))
        app_error = _app_error(path, manifest)
        if app_error is not None:
            errors.append(app_error)
        elif manifest.source.shipped and manifest.source.populate != "none" and not _has_content(path / "App"):
            warnings.append("App is missing; it will be recovered from its source")

    return CheckReport(
        path, kind, manifest, tuple(errors), tuple(warnings),
        app_kind=manifest.source.app_kind if kind is ManifestKind.VERSION else None,
    )


def _has_content(folder: Path) -> bool:
    """Return whether *folder* is a directory with at least one entry."""
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


def _app_error(path: Path, manifest: Manifest) -> str | None:
    """Return the error for an ``App`` that is missing or empty and cannot be recovered."""
    if _has_content(path / "App"):
        return None
    source = manifest.source
    if source.populate == "download":
        return None
    if source.populate == "script" and source.script is not None and (path / source.script).is_file():
        return None
    return APP_MISSING_ERROR


def _track_hook_errors(path: Path) -> list[str]:
    """Return an error for every ``pkg.local`` module that defines ``track``."""
    local = path / LOCAL_DIR
    if not local.is_dir():
        return []
    errors: list[str] = []
    for module in sorted(local.rglob("*.py")):
        relative = module.relative_to(path).as_posix()
        try:
            defines = defines_track(module.read_text(encoding="utf-8-sig"))
        except (SyntaxError, ValueError, OSError) as exc:
            errors.append(f"{relative} cannot be checked for a track hook ({exc}); update discovery lives only in {TRACKER_DIR_NAME}")
            continue
        if defines:
            errors.append(
                f"{relative} defines track: a version directory has no update discovery; "
                f"it lives only in {TRACKER_DIR_NAME}"
            )
    return errors
