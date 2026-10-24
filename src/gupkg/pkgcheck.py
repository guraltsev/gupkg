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
from dataclasses import dataclass
from pathlib import Path

from .model.manifest import (
    APP_MISSING_ERROR,
    LOCAL_DIR,
    Manifest,
    ManifestError,
    ManifestKind,
    manifest_to_data,
    parse_manifest,
)
from .model.template import TemplateError, references
from .model.trackspec import TrackSpecError, parse_spec, provider_captures
from .model.version import MANIFEST_NAME, TRACKER_DIR_NAME, is_version_name
from .services.hints import scan_directory
from .services.hooks import PKG_MODULE_API


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

    # ``global track`` anywhere (even in a function that runs at import) can bind the module-level name.
    if any(isinstance(node, ast.Global) and "track" in node.names for node in ast.walk(tree)):
        return True
    finder = _TrackBinding()
    finder.visit(tree)
    return finder.found


class _TrackBinding(ast.NodeVisitor):
    """Find any statement or expression that binds the module-level name ``track``.

    Function and class bodies are separate scopes and are skipped (their decorators, defaults and bases are not).
    Every way of binding a name counts: assignment, augmented and annotated assignment with a value, ``for`` and
    ``with`` targets, the walrus operator, starred and tuple targets, ``except ... as``, ``match`` captures,
    imports, ``def``, ``class`` and ``type``. A bare annotation (``track: int``) binds nothing.
    """

    def __init__(self) -> None:
        self.found = False

    def _name(self, name: str | None) -> None:
        """Record a bound name."""
        if name == "track":
            self.found = True

    def visit_Name(self, node: ast.Name) -> None:
        """Count a store or delete of ``track``."""
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._name(node.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """A bare annotation does not bind; one with a value does."""
        if node.value is not None:
            self.visit(node.target)
            self.visit(node.value)
        self.visit(node.annotation)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Count the name, and visit what is evaluated in the enclosing scope (decorators, defaults, annotations)."""
        self._name(node.name)
        for expression in (*node.decorator_list, *node.args.defaults, *(item for item in node.args.kw_defaults if item)):
            self.visit(expression)

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """Visit the defaults of a lambda; its body is its own scope."""
        for expression in (*node.args.defaults, *(item for item in node.args.kw_defaults if item)):
            self.visit(expression)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Count the name and visit decorators, bases and keywords; the body is a class scope."""
        self._name(node.name)
        for expression in (*node.decorator_list, *node.bases, *(item.value for item in node.keywords)):
            self.visit(expression)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        """A comprehension's loop variable is local to it; what it iterates and tests can still bind (walrus)."""
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)

    def visit_Import(self, node: ast.Import) -> None:
        """Count ``import track`` and ``import x as track``."""
        for alias in node.names:
            self._name((alias.asname or alias.name).split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Count ``from x import track`` and ``from x import y as track``."""
        for alias in node.names:
            self._name(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Count ``except E as track``."""
        self._name(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        """Count a ``match`` capture pattern."""
        self._name(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        """Count a ``match`` star capture."""
        self._name(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        """Count the ``**rest`` capture of a mapping pattern."""
        self._name(node.rest)
        self.generic_visit(node)


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

    # The root forwarder is how the installed gupkg is found; no other package may write one.
    if manifest.root_forwarder is not None and path.resolve().parent.name.casefold() != "gupkg":
        errors.append("[root_forwarder] is valid only in the gupkg package")

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

    # Hooks must be API 2 modules; a hook that matches a built-in service gets a hint.
    errors.extend(_hook_api_errors(path, named))
    for hint in scan_directory(path):
        warnings.append(f"{hint.file} looks like the built-in service '{hint.service}'; replace it with: {hint.suggestion}")

    # A tracker's [source] may refer only to values its provider produces.
    if kind is ManifestKind.TRACKER:
        errors.extend(_capture_errors(manifest))

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


def _declared_api(source: str) -> object:
    """Return the constant a module assigns to ``PKG_MODULE_API`` at module level, or ``None``."""
    for node in ast.parse(source).body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if any(isinstance(target, ast.Name) and target.id == "PKG_MODULE_API" for target in targets):
            return value.value if isinstance(value, ast.Constant) else None
    return None


def _hook_api_errors(path: Path, named: list[str]) -> list[str]:
    """Return an error for every named hook that does not declare ``PKG_MODULE_API = 2``."""
    errors: list[str] = []
    for name in dict.fromkeys(named):
        target = path / name
        if not target.is_file() or not name.lower().endswith(".py"):
            continue
        try:
            declared = _declared_api(target.read_text(encoding="utf-8-sig"))
        except (SyntaxError, ValueError, OSError) as exc:
            errors.append(f"{name} cannot be read as a Python hook ({exc})")
            continue
        if declared != PKG_MODULE_API:
            errors.append(f"{name} must declare PKG_MODULE_API = {PKG_MODULE_API}")
    return errors


def _capture_errors(manifest: Manifest) -> list[str]:
    """Return an error for every ``${name}`` in ``[source]`` that the tracker's provider never produces."""
    track = manifest.track
    if track is None:
        return []
    try:
        spec = parse_spec(
            track.provider, {"check_url": track.check_url, "repo": track.repo, "repo_url": track.repo_url}, track.options
        )
    except TrackSpecError:
        return []
    known = provider_captures(spec)
    if known is None:
        return []
    errors: list[str] = []
    source = manifest_to_data(manifest).get("source", {})
    stack: list[object] = [source]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            try:
                names = {reference.name for reference in references(item)}
            except TemplateError:
                continue
            for name in sorted(names - known):
                errors.append(
                    f"[source] refers to ${{{name}}}, which the '{track.provider}' provider does not produce; "
                    f"it produces: {', '.join(sorted(known))}"
                )
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return list(dict.fromkeys(errors))


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
