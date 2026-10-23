"""Run a package's ``[[prepare]]`` steps against the staged ``App``.

The steps replace the small hook scripts that tidied an unpacked tree: ``remove`` deletes
paths, ``rename`` gives the one file matching a glob a stable name, ``copy`` copies a file to
several names, ``write`` creates a file, and ``module`` runs a package's own ``prepare(ctx)``
hook for what the declarative steps cannot say. Every path is relative to the staged
``App`` and confined to it: no ``..``, no absolute path, no link or junction on the way. The
built-in download script runs the steps after unpacking, and a custom populate script can
call the same function with its context (decision D27).

Usage and API
-------------
Call ``run_prepare(steps, app, context)`` with the parsed steps of
``Manifest.prepare``. A ``module`` step needs *context*, a ``HookContext`` whose
``paths.app`` is *app*. Failures raise ``PrepareError``; the caller discards the staged
``App``, so a failed step never leaves a half-prepared tree in use.

Implementation Approach
-----------------------
Patterns are matched with ``glob`` against the staged tree only. ``rename`` and ``copy``
never overwrite an existing destination, and ``rename`` and ``copy`` require exactly one
source match, so a vendor renaming a file fails loudly instead of changing what gets
installed. A link found at a target is removed as a link and never followed.
"""

from __future__ import annotations

import glob
import os
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path

from ..model.manifest import PrepareCopy, PrepareModule, PrepareRemove, PrepareRename, PrepareStep, PrepareWrite
from ..report.events import Step, emit
from .hooks import HookContext, HookError, call_prepare, load_hook
from .safepath import UnsafePath, check_member_name, confine, is_reparse_point, remove_tree

_MAX_MATCHES = 100_000
_GLOB_CHARACTERS = "*?["


class PrepareError(RuntimeError):
    """Report a ``[[prepare]]`` step that cannot be carried out."""


def run_prepare(steps: Sequence[PrepareStep], app: Path, context: HookContext | None = None) -> None:
    """Run *steps* in order against the staged *app*.

    Parameters
    ----------
    steps : sequence
        The ``PrepareRemove``, ``PrepareRename``, ``PrepareCopy``, ``PrepareWrite`` and
        ``PrepareModule`` objects of a manifest.
    app : Path
        The staged ``App`` folder; every step is confined to it.
    context : HookContext, optional
        Required by a ``module`` step.

    Raises
    ------
    PrepareError
        If a path is unsafe, a glob does not match exactly one item where one is needed, a
        destination exists, or a hook fails.
    """
    root = Path(app)
    if not root.is_dir():
        raise PrepareError(f"{root} is not a folder")
    for step in steps:
        try:
            if isinstance(step, PrepareRemove):
                _remove(root, step)
            elif isinstance(step, PrepareRename):
                _rename(root, step)
            elif isinstance(step, PrepareCopy):
                _copy(root, step)
            elif isinstance(step, PrepareWrite):
                _write(root, step)
            elif isinstance(step, PrepareModule):
                _module(root, step, context)
        except (UnsafePath, OSError, HookError, PrepareError) as error:
            raise PrepareError(f"{_describe(step)} failed: {error}") from error


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _remove(root: Path, step: PrepareRemove) -> None:
    """Delete every match of every pattern; a pattern with no match is not an error."""
    removed = 0
    for pattern in step.paths:
        for target in _matches(root, pattern):
            # An earlier match may have removed this one's parent folder.
            if not os.path.lexists(target):
                continue
            if is_reparse_point(target):
                # A link is removed itself, never its target.
                remove_tree(target)
            elif target.is_dir():
                remove_tree(target)
            else:
                os.chmod(target, stat.S_IWRITE)
                target.unlink()
            removed += 1
    emit(Step(f"Removed {removed} item(s) from App", "remove"))


def _rename(root: Path, step: PrepareRename) -> None:
    """Rename the one match of ``from`` to ``to``, never overwriting."""
    source = _only_match(root, step.source)
    target = confine(root, step.target)
    if os.path.lexists(target):
        raise PrepareError(f"{step.target!r} already exists; rename never overwrites")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, target)
    emit(Step(f"Renamed {source.name} to {step.target}"))


def _copy(root: Path, step: PrepareCopy) -> None:
    """Copy the one file matching ``from`` to every target, never overwriting."""
    source = _only_match(root, step.source)
    if not source.is_file() or is_reparse_point(source):
        raise PrepareError(f"{step.source!r} is not a regular file")

    # Check every target before writing any, so a collision leaves nothing half-copied.
    targets = [confine(root, name) for name in step.targets]
    for name, target in zip(step.targets, targets):
        if os.path.lexists(target):
            raise PrepareError(f"{name!r} already exists; copy never overwrites")
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    emit(Step(f"Copied {source.name} to {', '.join(step.targets)}", "add"))


def _write(root: Path, step: PrepareWrite) -> None:
    """Write the file ``path`` with ``content`` (UTF-8), replacing a regular file but never a link."""
    target = confine(root, step.path)
    if is_reparse_point(target):
        raise PrepareError(f"{step.path!r} is a link")
    if target.is_dir():
        raise PrepareError(f"{step.path!r} is a folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(step.content.encode("utf-8"))
    emit(Step(f"Wrote {step.path}", "add"))


def _module(root: Path, step: PrepareModule, context: HookContext | None) -> None:
    """Run the ``prepare(ctx)`` hook of a ``pkg.local`` module."""
    if context is None:
        raise PrepareError("a module step needs a hook context")
    module = load_hook(context.paths.version_root, step.module)
    call_prepare(module, context)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _matches(root: Path, pattern: str) -> list[Path]:
    """Return the paths below *root* that match *pattern*, all confined to *root*."""
    # Judge the shape of the pattern first, with wildcards replaced by plain letters.
    plain = pattern
    for character in _GLOB_CHARACTERS + "]":
        plain = plain.replace(character, "x")
    check_member_name(plain)
    if any(character in pattern for character in _GLOB_CHARACTERS):
        found = sorted(glob.glob(os.path.join(glob.escape(str(root)), pattern.replace("/", os.sep)), recursive=True))
        if len(found) > _MAX_MATCHES:
            raise PrepareError(f"{pattern!r} matches too many items")
        paths = [Path(item) for item in found]
    else:
        candidate = confine(root, pattern, allow_link_leaf=True)
        paths = [candidate] if os.path.lexists(candidate) else []
    for path in paths:
        if not _inside(path, root):
            raise UnsafePath(f"{pattern!r} reaches outside App")
    return paths


def _only_match(root: Path, pattern: str) -> Path:
    """Return the single item matching *pattern*; zero or several matches are an error."""
    found = _matches(root, pattern)
    if len(found) != 1:
        raise PrepareError(f"{pattern!r} matches {len(found)} items; exactly one is required")
    return found[0]


def _inside(path: Path, root: Path) -> bool:
    """Return whether *path* is *root* or below it, judged on the parent's real path.

    The final component may itself be a link (that is the thing removed), so only the
    folder that holds it is resolved.
    """
    parent = os.path.normcase(os.path.realpath(path.parent))
    base = os.path.normcase(os.path.realpath(root))
    return parent == base or parent.startswith(base.rstrip("\\/") + os.sep)


def _describe(step: PrepareStep) -> str:
    """Return a short name for a step, for messages."""
    if isinstance(step, PrepareRemove):
        return f"[[prepare]] remove {list(step.paths)}"
    if isinstance(step, PrepareRename):
        return f"[[prepare]] rename {step.source!r}"
    if isinstance(step, PrepareCopy):
        return f"[[prepare]] copy {step.source!r}"
    if isinstance(step, PrepareWrite):
        return f"[[prepare]] write {step.path!r}"
    return f"[[prepare]] module {step.module!r}"
