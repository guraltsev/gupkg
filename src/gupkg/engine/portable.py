"""Keep a portable package's own settings and state outside ``App`` so they survive updates (design 6.3, D7).

A portable application writes its settings inside its folder. Every version has a fresh ``App``, so the
files the package declares in ``[portable] persist`` live in ``<package>\\data`` and are linked into each
version's ``App``: a persisted folder becomes a junction to ``data\\<item>``; a persisted file is copied in
when a version is activated and copied back when it is switched away from. The first population into
portable mode migrates the vendor's default configuration into ``data``. Markers the application needs in
order to enter portable mode are created in a freshly populated ``App``, and the files that
``[nonportable] remove`` names are removed from an ``App`` that is not installed portable.

Usage and API
-------------
``hand_back(system, app, data_dir, persist)`` copies persisted state out of the outgoing ``App`` into ``data``;
``hand_over(system, app, data_dir, persist)`` links or copies it into the incoming ``App``;
``apply_mode_adjustments(app, manifest, portable)`` creates the markers or removes the non-portable files of a
staged ``App``. ``PortableError`` reports a persisted path that would leave ``App`` or ``data``.

Implementation Approach
-----------------------
Every persisted path is confined: it must stay inside ``App`` and inside ``data`` after resolving links, and no
step follows a link out of either folder. State always wins over the vendor's defaults: a persisted item that
exists in ``data`` replaces the one in ``App``. Directories are linked, files are copied, so the application never
holds a link where it expects a file.
"""

from __future__ import annotations

import glob
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path

from ..catalog.local import canonical_path, is_inside, is_reparse_point
from ..core import write_bytes_atomic
from ..model.manifest import Manifest
from ..services.safepath import remove_tree
from ..system.port import System


class PortableError(ValueError):
    """Report a persisted path or marker that would leave ``App`` or ``data``."""


def _confined(base: Path, relative: str) -> Path:
    """Return ``base\\relative`` after requiring it to stay inside *base* (no ``..``, no drive, no link in between)."""
    parts = [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise PortableError(f"'{relative}' must be a relative path inside {base}")
    path = Path(base).joinpath(*parts)
    folder = Path(base)

    # A link above the leaf would let a path leave the folder; the leaf itself may be a link (it is never followed).
    for part in parts[:-1]:
        folder = folder / part
        if is_reparse_point(folder):
            raise PortableError(f"'{relative}' passes through the link {folder}")
    parent = path.parent
    if canonical_path(parent) != canonical_path(base) and not is_inside(parent, base):
        raise PortableError(f"'{relative}' would leave {base}")
    return path


def hand_back(system: System, app: Path, data_dir: Path, persist: Sequence[str]) -> list[str]:
    """Copy persisted state from the outgoing *app* into *data_dir*, and return what was saved.

    A persisted file is copied (replacing the saved one); a persisted folder that is a real folder (the application
    replaced the junction) is merged into ``data`` when ``data`` has none. A junction needs nothing: it already is
    the saved state.
    """
    saved: list[str] = []
    for item in persist:
        source = _confined(app, item)
        target = _confined(data_dir, item)
        if not os.path.lexists(source) or is_reparse_point(source):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            write_bytes_atomic(target, source.read_bytes())
            saved.append(item)
        elif source.is_dir() and not target.exists():
            shutil.copytree(source, target, symlinks=True)
            saved.append(item)
    return saved


def hand_over(system: System, app: Path, data_dir: Path, persist: Sequence[str]) -> list[str]:
    """Make *app* use the saved state in *data_dir*, and return what was linked or copied in.

    The first population migrates the vendor's default: an item that exists in ``app`` and not in ``data`` moves
    into ``data``. An item that exists in both keeps the saved state (the one in ``app`` is replaced). A saved
    folder becomes a junction inside ``app``, a saved file is copied in.
    """
    handled: list[str] = []
    for item in persist:
        link = _confined(app, item)
        saved = _confined(data_dir, item)

        # First population: the vendor's default configuration becomes the saved state.
        if os.path.lexists(link) and not is_reparse_point(link) and not os.path.lexists(saved):
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(link), str(saved))
        if not os.path.lexists(saved):
            continue

        # Saved state wins over whatever the new App brought.
        if os.path.lexists(link):
            if is_reparse_point(link):
                if saved.is_dir() and canonical_path(system.read_link(link) or link) == canonical_path(saved):
                    handled.append(item)
                    continue
                system.remove_link(link)
            else:
                remove_tree(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        if saved.is_dir():
            system.create_junction(link, saved)
        else:
            write_bytes_atomic(link, saved.read_bytes())
        handled.append(item)
    return handled


def apply_mode_adjustments(app: Path, manifest: Manifest, portable: bool) -> list[str]:
    """Create the portable markers, or remove the non-portable files, in the staged *app*; return what was done.

    In portable mode every ``[portable] markers`` file is created (an existing one is kept). In any other mode
    every path that ``[nonportable] remove`` names (a glob) is removed; a link is removed itself, never its target.
    """
    done: list[str] = []
    if portable:
        for marker in manifest.portable.markers if manifest.portable else ():
            path = _confined(app, marker.path)
            if os.path.lexists(path):
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(marker.content or "", encoding="utf-8", newline="")
            done.append(f"marker {marker.path}")
        return done
    for pattern in manifest.nonportable.remove if manifest.nonportable else ():
        _confined(app, pattern.replace("*", "x").replace("?", "x").replace("[", "x"))
        for match in sorted(glob.glob(pattern, root_dir=str(app))):
            path = Path(app) / match
            if not is_inside(path, app) or canonical_path(path) == canonical_path(app):
                continue
            remove_tree(path)
            done.append(f"removed {path.relative_to(app).as_posix()}")
    return done


def scratch_name(prefix: str) -> str:
    """Return a unique folder name for a staging or backup folder."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
