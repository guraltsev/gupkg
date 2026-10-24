"""Report on the running gupkg itself: where it runs from, which interpreter runs it and whether its files are intact.

``gupkg self status`` answers three questions without changing anything. Where does this gupkg live (installed in a
root, bundled with a package, or a source checkout)? Which interpreter is running it, and is that the isolated bundled
Python that gupkg insists on (issue 018)? Do the files of its version directory still match the ``MANIFEST.sha256`` the
release was built with (issue 016)? A damaged install is never repaired here: ``gupkg self repair`` re-applies the
integration and a release ZIP restores the files.

Usage and API
-------------
Call ``self_status(location, ...)`` with a ``RunningGupkg`` (``gupkg.selfmgmt.location``); the returned ``SelfStatus`` has
``to_data()`` for ``--json`` and ``problems()`` for the human text. ``SelfStatus.ok`` is false when the manifest check
found a difference or the interpreter is not the bundled one.

Implementation Approach
-----------------------
The version root whose manifest is checked is the folder that holds ``pkg.toml`` and ``gupkg\\MANIFEST.sha256`` beside or
above the gupkg package. A source checkout has none, which is reported as not applicable rather than as a failure. The
interpreter check compares the real path of the running interpreter with ``<package>\\python\\python.exe`` and reads the
isolation flags of the process.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._version import __version__
from ..integrity import MANIFEST_RELATIVE_PATH, IntegrityReport, ManifestError, verify_version_directory
from .location import RunningGupkg


@dataclass(frozen=True)
class SelfStatus:
    """What ``gupkg self status`` found about the running gupkg.

    Attributes
    ----------
    version : str
        The gupkg version.
    kind : str
        ``installed``, ``bundled`` or ``source``.
    location : str
        The folder of the gupkg package.
    version_root : str or None
        The version directory whose manifest was checked, or ``None`` when there is none (a source checkout).
    interpreter : str
        The running interpreter.
    interpreter_bundled : bool
        Whether it is the isolated Python inside this gupkg (``python\\python.exe``, started with ``-I`` and ``-S``).
    integrity : IntegrityReport or None
        The manifest comparison, ``None`` when there was nothing to compare with.
    manifest_problem : str, optional
        Why the manifest could not be read.
    """

    version: str
    kind: str
    location: str
    version_root: str | None
    interpreter: str
    interpreter_bundled: bool
    integrity: IntegrityReport | None = None
    manifest_problem: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def problems(self) -> list[str]:
        """Return one line per finding that makes the status not ok."""
        found: list[str] = []
        if self.kind != "source" and not self.interpreter_bundled:
            found.append(f"the running interpreter {self.interpreter} is not the isolated bundled Python of this gupkg")
        if self.manifest_problem:
            found.append(self.manifest_problem)
        elif self.integrity is not None:
            found.extend(self.integrity.problems())
        return found

    @property
    def ok(self) -> bool:
        """Return whether the interpreter is the bundled one and every file matches its manifest."""
        return not self.problems()

    def to_data(self) -> dict[str, Any]:
        """Return the JSON-ready description (``integrity`` lists modified, missing and extra files)."""
        integrity = None
        if self.integrity is not None:
            integrity = {
                "manifest_found": self.integrity.manifest_found, "ok": self.integrity.ok,
                "modified": list(self.integrity.modified), "missing": list(self.integrity.missing),
                "extra": list(self.integrity.extra),
            }
        return {
            "version": self.version, "kind": self.kind, "location": self.location, "version_root": self.version_root,
            "interpreter": self.interpreter, "interpreter_bundled": self.interpreter_bundled, "integrity": integrity,
            "problems": self.problems(), "notes": list(self.notes),
        }


def self_status(location: RunningGupkg) -> SelfStatus:
    """Return the status of the gupkg described by *location*, reading only its own files and the process state.

    The manifest of a source checkout does not exist, so its integrity is reported as not applicable.
    """
    package_dir = location.package_dir
    expected = package_dir / "python" / "python.exe"
    bundled = (
        os.path.normcase(os.path.realpath(sys.executable)) == os.path.normcase(os.path.realpath(expected))
        and bool(sys.flags.isolated) and bool(sys.flags.no_site)
    )

    # The version root holds pkg.toml and gupkg\MANIFEST.sha256; the first folder beside or above the package that does.
    version_root = next(
        (
            folder for folder in (package_dir.parent, location.version_dir)
            if folder is not None and (folder / "pkg.toml").is_file() and (folder / MANIFEST_RELATIVE_PATH).is_file()
        ),
        None,
    )
    integrity: IntegrityReport | None = None
    problem: str | None = None
    notes: list[str] = []
    if version_root is None:
        notes.append("no MANIFEST.sha256 beside this gupkg (a source checkout): its files are not checked")
    else:
        try:
            integrity = verify_version_directory(version_root)
        except (ManifestError, OSError) as error:
            problem = f"cannot check the files against {MANIFEST_RELATIVE_PATH}: {error}"
    return SelfStatus(
        __version__, location.kind, str(package_dir), str(version_root) if version_root else None, sys.executable, bundled,
        integrity, problem, tuple(notes),
    )
