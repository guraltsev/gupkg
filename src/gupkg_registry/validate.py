"""Validate a whole registry: what CI runs on every push (issue 031, scope item 5).

Usage and API
-------------
``validate_registry(root)`` returns a ``ValidationReport`` whose ``problems`` are strings (empty
means the registry is sound). It checks ``_registry.toml``, then every package: the tracker and
every pinned version with ``gupkg.pkgcheck.check_directory`` (zero errors and zero warnings),
explicit ``modes``, a ``sha256`` on every version (except the ones ``_registry.toml`` lists under
``unpinned``), a fingerprint that matches the files, the launchers generated from the template,
and the hook files (at most ``MAX_HOOK_FILES``, each with a ``Why no service`` note).

Implementation Approach
-----------------------
Every check reads only files below *root* and never follows a link: a tree with a link is a
problem, and the fingerprint refuses it. Problems name the package and file so a maintainer can
act on them. Nothing is written.
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from gupkg.model.manifest import ManifestKind
from gupkg.model.version import MANIFEST_NAME, TRACKER_DIR_NAME, is_version_name
from gupkg.pkgcheck import CheckReport, check_directory
from gupkg.services.fingerprint import FingerprintError, fingerprint_directory
from gupkg.services.safepath import is_reparse_point
from gupkg.launchers import LAUNCHERS, launcher_text

from .layout import LAUNCHER_NAMES, RETIRED_LAUNCHER_NAMES, TRACKER_LAUNCHER_NAME, RegistryFileError, package_dirs, read_registry_file

MAX_HOOK_FILES = 13
WHY_NO_SERVICE = "Why no service"
# Everything except launchers (.cmd) is stored with LF, so one fingerprint is the same on every checkout.
_LF_SUFFIXES = frozenset({".toml", ".py", ".json", ".md", ".txt", ".yml", ".yaml", ".ini", ".xml"})


@dataclass(frozen=True)
class ValidationReport:
    """The outcome of ``validate_registry``: every problem found, and counts for the summary."""

    problems: tuple[str, ...] = ()
    packages: int = 0
    versions: int = 0
    hook_files: tuple[str, ...] = ()
    unpinned: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """Return whether no problem was found."""
        return not self.problems


def _report_problems(label: str, report: CheckReport) -> list[str]:
    """Turn a ``CheckReport`` into problem lines; warnings count as problems here."""
    return [f"{label}: {line}" for line in (*report.errors, *(f"warning: {w}" for w in report.warnings))]


def _hook_files(package: Path) -> list[Path]:
    """Return the Python files below any ``pkg.local`` folder of *package*."""
    return sorted(p for p in package.rglob("*.py") if "pkg.local" in p.relative_to(package).parts)


def _hook_note_problem(label: str, path: Path) -> str | None:
    """Return a problem when the hook's module docstring has no ``Why no service`` note."""
    try:
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8-sig"))) or ""
    except (SyntaxError, ValueError, OSError) as error:
        return f"{label}: cannot be read as Python ({error})"
    return None if WHY_NO_SERVICE in doc else f"{label}: the module docstring has no '{WHY_NO_SERVICE}: <reason>' note"


def _tree_problems(label: str, folder: Path) -> list[str]:
    """Return a problem for every link below *folder* and for every stray ``.bak`` file."""
    problems: list[str] = []
    for path in [folder, *folder.rglob("*")]:
        relative = path.relative_to(folder).as_posix() or "."
        if is_reparse_point(path):
            problems.append(f"{label}/{relative}: is a link or junction")
        elif path.suffix.lower() == ".bak":
            problems.append(f"{label}/{relative}: backup files do not belong in a registry")
        elif path.suffix.lower() in _LF_SUFFIXES and b"\r" in path.read_bytes():
            problems.append(f"{label}/{relative}: has CR line endings; the registry stores {path.suffix} files with LF")
    return problems


def _stray_problems(name: str, package: Path) -> list[str]:
    """Return a problem for each entry of a package folder that is not the tracker or a pinned version.

    A version directory needs a version-safe name (no Windows device name, control character, ``%``,
    leading dash...) and a ``pkg.toml``; two versions that differ only in letter case cannot both be
    checked out on Windows; a stray file or a leftover ``.new-*`` staging folder is never expected.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    for entry in sorted(package.iterdir(), key=lambda item: item.name):
        label = f"{name}/{entry.name}"
        if not entry.is_dir():
            problems.append(f"{label}: a file in a package folder (only the tracker and version directories belong there)")
            continue
        if entry.name == TRACKER_DIR_NAME:
            continue
        if not (entry / MANIFEST_NAME).is_file():
            problems.append(f"{label}: a directory without {MANIFEST_NAME} (stray or leftover staging folder)")
        if not is_version_name(entry.name):
            problems.append(f"{label}: not a usable version name (reserved, device or unsafe)")
        folded = entry.name.casefold()
        if folded in seen:
            problems.append(f"{label}: differs only in letter case from {name}/{seen[folded]}")
        seen.setdefault(folded, entry.name)
    return problems


def _modes_problem(label: str, directory: Path) -> str | None:
    """Return a problem when ``pkg.toml`` does not state ``modes`` itself (every package declares them)."""
    try:
        data = tomllib.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None  # check_directory reports the unreadable file
    return None if "modes" in data else f"{label}: 'modes' is not declared (every package states its modes)"


def _launcher_problems(label: str, version: Path) -> list[str]:
    """Return a problem for each launcher that is missing or differs from the template."""
    problems = []
    for name in LAUNCHER_NAMES:
        target = version / name
        expected = launcher_text(LAUNCHERS[name]).encode("ascii")
        if not target.is_file():
            problems.append(f"{label}/{name}: launcher is missing")
        elif target.read_bytes() != expected:
            problems.append(f"{label}/{name}: differs from the launcher template")
    problems.extend(f"{label}/{name}: retired launcher name (now manage.cmd; run the registry tool: launchers)"
                    for name in RETIRED_LAUNCHER_NAMES if (version / name).exists())
    return problems


def _tracker_launcher_problems(label: str, tracker: Path) -> list[str]:
    """Return a problem when the tracker's ``check-updates.cmd`` is missing or differs from the template."""
    target = tracker / TRACKER_LAUNCHER_NAME
    if not target.is_file():
        return [f"{label}/{TRACKER_LAUNCHER_NAME}: launcher is missing (run the registry tool: launchers)"]
    if target.read_bytes() != launcher_text(LAUNCHERS[TRACKER_LAUNCHER_NAME]).encode("ascii"):
        return [f"{label}/{TRACKER_LAUNCHER_NAME}: differs from the launcher template"]
    return []


def validate_registry(root: Path) -> ValidationReport:
    """Validate the registry at *root* and report every problem (nothing is changed).

    Parameters
    ----------
    root : Path
        The registry checkout.

    Returns
    -------
    ValidationReport
        The problems, the number of packages and pinned versions, the hook files and the unpinned
        versions. An unreadable ``_registry.toml`` stops the check after its own problem.
    """
    root = Path(root)
    try:
        settings = read_registry_file(root)
    except RegistryFileError as error:
        return ValidationReport((str(error),))
    fingerprints: dict[str, str] = settings.get("fingerprints", {})
    unpinned = set(settings.get("unpinned", []))
    problems: list[str] = []
    hooks: list[Path] = []
    seen_versions: set[str] = set()
    versions = 0

    for package in package_dirs(root):
        name = package.name
        problems.extend(_tree_problems(name, package))
        tracker = package / TRACKER_DIR_NAME
        children = [c for c in sorted(package.iterdir()) if c.is_dir()]
        problems.extend(_stray_problems(name, package))
        if not (tracker / MANIFEST_NAME).is_file() and not any((c / MANIFEST_NAME).is_file() for c in children):
            problems.append(f"{name}: has neither a tracker nor a pinned version")
        if (tracker / MANIFEST_NAME).is_file():
            label = f"{name}/{TRACKER_DIR_NAME}"
            problems.extend(_report_problems(label, check_directory(tracker)))
            mode_problem = _modes_problem(label, tracker)
            problems.extend([mode_problem] if mode_problem else [])
            problems.extend(f"{label}/{n}: a tracker never installs, so it carries no {n}" for n in (*LAUNCHER_NAMES, *RETIRED_LAUNCHER_NAMES) if (tracker / n).exists())
            problems.extend(_tracker_launcher_problems(label, tracker))
        for child in children:
            if child.name == TRACKER_DIR_NAME or not (child / MANIFEST_NAME).is_file():
                continue
            versions += 1
            key = f"{name}/{child.name}"
            seen_versions.add(key)
            report = check_directory(child)
            problems.extend(_report_problems(key, report))
            mode_problem = _modes_problem(key, child)
            problems.extend([mode_problem] if mode_problem else [])
            manifest = report.manifest
            if manifest is not None and manifest.kind is ManifestKind.VERSION:
                source = manifest.source
                if source.populate == "download" and source.git is None:
                    if source.sha256 is None and key not in unpinned:
                        problems.append(f"{key}: has no sha256 (run the registry tool: pin {key})")
                    if source.sha256 is not None and key in unpinned:
                        problems.append(f"{key}: has a sha256 but is still listed as unpinned in _registry.toml")
                    if source.sha256 is not None and source.sha256_from is None:
                        problems.append(f"{key}: sha256 has no sha256_from provenance")
                elif source.populate == "script":
                    problems.append(f"{key}: a registry version is populated by the built-in download, not a script")
            problems.extend(_launcher_problems(key, child))
            published = fingerprints.get(key)
            if published is None:
                problems.append(f"{key}: no fingerprint in {'_registry.toml'}")
            else:
                try:
                    actual = fingerprint_directory(child)
                except FingerprintError as error:
                    problems.append(f"{key}: cannot be fingerprinted ({error})")
                else:
                    if actual != published:
                        problems.append(f"{key}: fingerprint {actual} does not match the published {published}")
        hooks.extend(_hook_files(package))

    problems.extend(f"fingerprint of '{key}' names no version directory" for key in sorted(set(fingerprints) - seen_versions))
    package_names = {p.name for p in package_dirs(root)}
    for name in sorted(settings.get("unversioned", [])):
        if name not in package_names:
            problems.append(f"'{name}' is listed as unversioned but is not a package")
    for key in sorted(unpinned):
        if key not in seen_versions:
            problems.append(f"'{key}' is listed as unpinned but is not a version directory")
    if len(hooks) > MAX_HOOK_FILES:
        problems.append(f"{len(hooks)} hook files remain; at most {MAX_HOOK_FILES} are allowed")
    for hook in hooks:
        note = _hook_note_problem(hook.relative_to(root).as_posix(), hook)
        if note:
            problems.append(note)
    return ValidationReport(
        tuple(problems), len(package_dirs(root)), versions,
        tuple(h.relative_to(root).as_posix() for h in hooks), tuple(sorted(unpinned)),
    )
