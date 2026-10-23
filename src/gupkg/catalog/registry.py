"""Synchronise, validate and read registry 2 caches (design section 17).

A registry is a Git repository or ZIP archive with a ``_registry.toml`` (``format = 2``) and
one folder per package: pinned ``<version>\\`` directories (each with the fingerprint that
``_registry.toml`` publishes under ``[fingerprints]``, when it does) and an optional
``_check_updates\\`` tracker. Each configured registry is cached in its own folder inside
the root data directory, ``<root>\\.gupkg\\registry\\<name>\\`` (decision D20), so the cache
survives updates of gupkg. Core reads no 0.17 seeds (decision D33): a tree whose
``_registry.toml`` is missing or not format 2 is refused.

Usage and API
-------------
``sync_registry(cache_dir, system, name=..., source=...)`` refreshes one cache and returns a
``SyncResult``; it is the only function here that writes, and it writes only below
*cache_dir*. ``load_registry(name, cache_dir, system)`` returns the ``RegistrySnapshot`` of
the active revision, which the catalog joins with local packages. ``registry_cache_dir(root,
name)`` names a cache folder, and ``search_snapshot(snapshot, query)`` filters entries.

Implementation Approach
-----------------------
A new revision is obtained into a work area, validated completely, copied into a
revision-addressed folder with a validated index, and only then published by rewriting the
state file, so a failed sync leaves the previous validated tree active. Validation is per
package: an entry with an unsafe tree is skipped, a version or tracker that fails the format 2
checks is dropped, and every problem is reported. The index records the result once per
revision, so reading the registry never re-validates the whole tree; a missing or stale index
is rebuilt in memory and never written by a reader. Transport (trusted Git, ZIP extraction)
is shared with ``gupkg.registry`` and prints nothing. An elevated run refuses a cache that is
not in an admin-only folder.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cmp_to_key
from pathlib import Path

from ..core import ConfigValidationError
from ..model.state import RegistryEntry, RegistryTracker, RegistryVersion
from ..model.version import MANIFEST_NAME, TRACKER_DIR_NAME, compare_versions, is_version_name
from ..pkgcheck import check_directory
from ..registry import (
    archive_revision,
    archive_url,
    extract_archive,
    git_environment,
    git_remote_revision,
    is_git_source,
    require_trusted_cache,
    trusted_git,
)
from ..system.port import System
from ..tomlio import read_toml, write_toml
from .local import DATA_DIR_NAME, manifest_text_hash, is_reparse_point, tree_hash

REGISTRY_FORMAT = 2
REGISTRY_FILE_NAME = "_registry.toml"

# The schema number of the per-revision index, so an index of another layout is ignored.
INDEX_SCHEMA = 1
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CacheState:
    """The active cached revision of one registry and the outcome of its last sync."""

    cache_dir: Path
    revision: str | None = None
    source: str | None = None
    synchronized_at: str | None = None
    last_failure: str | None = None

    @property
    def tree_dir(self) -> Path | None:
        """Return the folder of the active revision (``pkgs``, ``registry.toml``, index), if it exists."""
        if self.revision is None:
            return None
        candidate = self.cache_dir / "trees" / self.revision
        return candidate if (candidate / "pkgs").is_dir() else None


@dataclass(frozen=True)
class RegistrySnapshot:
    """The valid entries of one registry revision, and every problem validation found."""

    name: str
    revision: str | None
    entries: Mapping[str, RegistryEntry]
    problems: tuple[str, ...] = ()
    description: str | None = None

    def entry(self, package: str) -> RegistryEntry | None:
        """Return the entry called *package* (letter case ignored), or ``None``."""
        return self.entries.get(package.casefold())


@dataclass(frozen=True)
class SyncResult:
    """The outcome of a sync: ``ok``, its ``status`` (``synced``, ``current`` or ``offline``) and problems."""

    ok: bool
    status: str = ""
    revision: str | None = None
    problems: tuple[str, ...] = ()
    error: str | None = None


def registry_cache_dir(root: Path, name: str) -> Path:
    """Return the cache folder of registry *name* in the root data directory of *root*."""
    return Path(root) / DATA_DIR_NAME / "registry" / name


def cache_state(cache_dir: Path, system: System) -> CacheState:
    """Read a cache's state without contacting Git or creating folders.

    Parameters
    ----------
    cache_dir : Path
        The registry's cache folder.
    system : System
        Boundary for the admin-only check of an elevated run.

    Returns
    -------
    CacheState
        An unreadable or missing ``state.toml`` means "never synchronised".

    Raises
    ------
    ConfigValidationError
        If the run is elevated and the cache is not in an admin-only folder.
    """
    require_trusted_cache(cache_dir, system)
    state_path = Path(cache_dir) / "state.toml"
    if not state_path.is_file() or is_reparse_point(state_path):
        return CacheState(Path(cache_dir))
    try:
        values = read_toml(state_path)
    except (OSError, tomllib.TOMLDecodeError):
        return CacheState(Path(cache_dir))
    return CacheState(
        Path(cache_dir),
        revision=values.get("revision") or None,
        source=values.get("source") or None,
        synchronized_at=values.get("synchronized_at") or None,
        last_failure=values.get("last_failure") or None,
    )


# ---------------------------------------------------------------------------
# Reading a registry
# ---------------------------------------------------------------------------


def load_registry(name: str, cache_dir: Path, system: System) -> RegistrySnapshot:
    """Return the snapshot of the active revision of a registry cache.

    Parameters
    ----------
    name : str
        The registry's name in the settings.
    cache_dir : Path
        The registry's cache folder.
    system : System
        Boundary for the admin-only check of an elevated run.

    Returns
    -------
    RegistrySnapshot
        Empty, with a problem, when the cache is untrusted, missing or invalid. The index
        written by the sync is used when it matches the revision; otherwise the tree is
        validated in memory (nothing is written).
    """
    try:
        state = cache_state(cache_dir, system)
    except ConfigValidationError as exc:
        return RegistrySnapshot(name, None, {}, (str(exc),))
    tree = state.tree_dir
    if tree is None:
        return RegistrySnapshot(name, None, {}, () if state.revision is None else (f"The cache of registry {name} has no tree for {state.revision}",))
    if any(is_reparse_point(path) for path in (tree, tree / "pkgs")):
        return RegistrySnapshot(name, state.revision, {}, (f"The cache of registry {name} contains a link and is not read",))

    # A matching index spares re-validating the tree; anything else is rebuilt in memory.
    indexed = _read_index(name, tree, state.revision)
    if indexed is not None:
        return indexed
    try:
        return build_snapshot(name, tree, state.revision)
    except ConfigValidationError as exc:
        return RegistrySnapshot(name, state.revision, {}, (str(exc),))


def search_snapshot(snapshot: RegistrySnapshot, query: str = "") -> list[RegistryEntry]:
    """Return the entries whose name contains *query* (case-insensitive), sorted by name."""
    needle = query.casefold()
    return [entry for key, entry in sorted(snapshot.entries.items()) if needle in key]


def build_snapshot(name: str, tree: Path, revision: str | None) -> RegistrySnapshot:
    """Validate the registry tree of one revision and return its snapshot.

    Parameters
    ----------
    name : str
        The registry's name in the settings.
    tree : Path
        A folder holding ``pkgs\\`` (the registry's packages) and ``registry.toml`` (a copy
        of ``_registry.toml``).
    revision : str or None
        The revision the tree belongs to.

    Returns
    -------
    RegistrySnapshot
        The valid entries; every skipped entry, dropped version or dropped tracker is a problem.

    Raises
    ------
    ConfigValidationError
        If the registry as a whole is not usable: ``_registry.toml`` is missing, is not
        format 2, or its fingerprints are malformed.
    """
    tree = Path(tree)
    settings_path = tree / "registry.toml"
    try:
        settings = read_toml(settings_path)
    except FileNotFoundError:
        raise ConfigValidationError(f"{REGISTRY_FILE_NAME} is missing: unsupported registry format") from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigValidationError(f"Cannot read {REGISTRY_FILE_NAME}: {exc}") from exc
    if settings.get("format") != REGISTRY_FORMAT:
        raise ConfigValidationError(
            f"unsupported registry format ({settings.get('format', 'missing')!r}); this gupkg reads format {REGISTRY_FORMAT}"
        )
    fingerprints = settings.get("fingerprints", {})
    if not isinstance(fingerprints, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not _SHA256.fullmatch(value)
        for key, value in fingerprints.items()
    ):
        raise ConfigValidationError(f"[fingerprints] of {REGISTRY_FILE_NAME} must map 'name/version' to a SHA-256")
    wanted = {key.casefold(): value for key, value in fingerprints.items()}

    # One entry per package folder; a folder that cannot be trusted is skipped, with its reason.
    pkgs = tree / "pkgs"
    problems: list[str] = []
    entries: dict[str, RegistryEntry] = {}
    skipped: set[str] = set()
    for folder in sorted(pkgs.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
        if folder.name.startswith((".", "_")) or not (folder.is_dir() or is_reparse_point(folder)):
            continue
        key = folder.name.casefold()
        if key in entries or key in skipped:
            entries.pop(key, None)
            skipped.add(key)
            problems.append(f"{folder.name}: duplicate registry name (letter case differs); skipped")
            continue
        entry, entry_problems = _build_entry(name, revision, folder, wanted)
        problems.extend(entry_problems)
        if entry is None:
            skipped.add(key)
        else:
            entries[key] = entry
    description = settings.get("description")
    return RegistrySnapshot(name, revision, entries, tuple(problems), description if isinstance(description, str) else None)


def _build_entry(
    registry: str, revision: str | None, folder: Path, fingerprints: Mapping[str, str]
) -> tuple[RegistryEntry | None, list[str]]:
    """Validate one package folder; return its entry (or ``None`` when skipped) and the problems."""
    name = folder.name
    if not is_version_name(name):
        return None, [f"{name}: not a usable package name; skipped"]
    unsafe = _unsafe_entry(folder)
    if unsafe is not None:
        return None, [f"{name}: {unsafe}; skipped"]

    # Pinned versions: every one that fails the format 2 checks is dropped and reported.
    problems: list[str] = []
    versions: list[RegistryVersion] = []
    tracker: RegistryTracker | None = None
    for child in sorted(folder.iterdir(), key=lambda item: item.name):
        if not child.is_dir() or not (child / MANIFEST_NAME).is_file():
            continue
        if child.name == TRACKER_DIR_NAME:
            report = check_directory(child)
            if report.ok:
                tracker = RegistryTracker(child, tree_hash(child))
            else:
                problems.extend(f"{name}/{TRACKER_DIR_NAME}: {message}; tracker dropped" for message in report.errors)
        elif is_version_name(child.name):
            report = check_directory(child)
            if report.ok:
                versions.append(RegistryVersion(
                    child.name, child, fingerprints.get(f"{name}/{child.name}".casefold()), manifest_text_hash(child / MANIFEST_NAME)
                ))
            else:
                problems.extend(f"{name}/{child.name}: {message}; version dropped" for message in report.errors)
    versions.sort(key=cmp_to_key(lambda left, right: compare_versions(left.name, right.name)))
    if not versions and tracker is None:
        return None, [*problems, f"{name}: no valid version or tracker; skipped"]
    return RegistryEntry(registry, name, revision, folder, tuple(versions), tracker), problems


def _unsafe_entry(folder: Path) -> str | None:
    """Return why the tree below *folder* cannot be trusted (a link, an unsafe name, reserved state), or ``None``."""
    if is_reparse_point(folder):
        return "it is a link"
    stack = [folder]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            relative = child.relative_to(folder)
            if is_reparse_point(child):
                return f"{relative} is a link"
            if any(part in {".", ".."} or ":" in part for part in relative.parts):
                return f"unsafe path {relative}"
            if child.name.casefold() in {"current", DATA_DIR_NAME}:
                return f"it contains reserved state {relative}"
            if child.is_dir():
                stack.append(child)
    return None


# ---------------------------------------------------------------------------
# The per-revision index
# ---------------------------------------------------------------------------


def _write_index(tree: Path, snapshot: RegistrySnapshot) -> None:
    """Write the validated index of *snapshot* next to its tree."""
    entries = []
    for entry in snapshot.entries.values():
        item: dict[str, object] = {"name": entry.name}
        if entry.tracker is not None:
            item["tracker_sha256"] = entry.tracker.sha256
        item["version"] = [
            {key: value for key, value in (
                ("name", version.name), ("fingerprint", version.fingerprint), ("pkg_toml_sha256", version.pkg_toml_sha256),
            ) if value is not None}
            for version in entry.versions
        ]
        entries.append(item)
    data: dict[str, object] = {
        "schema": INDEX_SCHEMA,
        "revision": snapshot.revision or "",
        "name": snapshot.name,
        "problem": list(snapshot.problems),
        "entry": entries,
    }
    if snapshot.description is not None:
        data["description"] = snapshot.description
    write_toml(tree / "index.toml", data)


def _read_index(name: str, tree: Path, revision: str | None) -> RegistrySnapshot | None:
    """Return the snapshot recorded in the index of *tree*, or ``None`` when it is missing, stale or malformed."""
    path = tree / "index.toml"
    if is_reparse_point(path) or not path.is_file():
        return None
    try:
        data = read_toml(path)
        if data.get("schema") != INDEX_SCHEMA or data.get("revision") != (revision or "") or data.get("name") != name:
            return None
        entries: dict[str, RegistryEntry] = {}
        for item in data["entry"]:
            folder = tree / "pkgs" / item["name"]
            tracker = None
            if "tracker_sha256" in item:
                tracker = RegistryTracker(folder / TRACKER_DIR_NAME, str(item["tracker_sha256"]))
            versions = tuple(
                RegistryVersion(str(row["name"]), folder / str(row["name"]), row.get("fingerprint"), row.get("pkg_toml_sha256"))
                for row in item["version"]
            )
            entries[str(item["name"]).casefold()] = RegistryEntry(name, str(item["name"]), revision, folder, versions, tracker)
        description = data.get("description")
        return RegistrySnapshot(
            name, revision, entries, tuple(str(problem) for problem in data["problem"]),
            description if isinstance(description, str) else None,
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Synchronising
# ---------------------------------------------------------------------------


def sync_registry(
    cache_dir: Path,
    system: System,
    *,
    name: str,
    source: str,
    offline: bool = False,
) -> SyncResult:
    """Fetch a registry and publish its validated tree in its cache.

    Parameters
    ----------
    cache_dir : Path
        The registry's cache folder, ``<root>\\.gupkg\\registry\\<name>`` (see ``registry_cache_dir``).
    system : System
        Boundary for downloads, Git and the admin-only checks.
    name : str
        The registry's name in the settings.
    source : str
        An ``https://`` Git repository URL (cloned with a trusted Git, otherwise downloaded
        as its GitHub ZIP archive), or the ``http(s)://`` or ``file:`` URL of a ZIP archive.
    offline : bool, default=False
        Report the existing cache without any download.

    Returns
    -------
    SyncResult
        ``synced``, ``current`` (the fetched revision is already cached) or ``offline``.
        On failure the previous validated tree stays active and ``error`` says why.
    """
    cache_dir = Path(cache_dir)
    try:
        current = cache_state(cache_dir, system)
    except ConfigValidationError as exc:
        return SyncResult(False, error=str(exc))
    if offline:
        if current.tree_dir is None:
            return SyncResult(False, error="No validated registry cache is available in offline mode")
        return SyncResult(True, "offline", current.revision)

    work_parent = cache_dir / ".work"
    work_parent.mkdir(parents=True, exist_ok=True)
    published: Path | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="sync-", dir=str(work_parent)) as work_name:
            work = Path(work_name)

            # A trusted Git can name the remote revision without transferring it.
            git = trusted_git(system) if is_git_source(source) else None
            if git is not None and current.tree_dir is not None:
                remote = git_remote_revision(system, git, source)
                if remote is not None and remote == current.revision:
                    _write_state(cache_dir, current, source=source, last_failure=None)
                    return SyncResult(True, "current", current.revision)

            registry, revision = _obtain(source, git, work, system)
            if current.revision == revision and current.tree_dir is not None:
                _write_state(cache_dir, current, source=source, last_failure=None)
                return SyncResult(True, "current", revision)

            # Copy the packages and the registry file (never Git metadata or other furniture).
            def furniture(directory: str, names: list[str]) -> list[str]:
                """Leave out Git metadata and top-level repository furniture (names starting with "_")."""
                top_level = Path(directory) == registry
                return [item for item in names if item == ".git" or (top_level and item.startswith("_"))]

            staged = work / "tree"
            shutil.copytree(registry, staged / "pkgs", ignore=furniture)
            registry_file = registry / REGISTRY_FILE_NAME
            if not registry_file.is_file():
                raise ConfigValidationError(f"{REGISTRY_FILE_NAME} is missing: unsupported registry format")
            shutil.copyfile(registry_file, staged / "registry.toml")

            # Validate the staged tree completely before it becomes visible.
            snapshot = build_snapshot(name, staged, revision)
            if not snapshot.entries:
                raise ConfigValidationError("The registry contains no valid packages")
            target = cache_dir / "trees" / revision
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged, target)
            published = target
            published_snapshot = build_snapshot(name, target, revision)
            _write_index(target, published_snapshot)
        _write_state(cache_dir, CacheState(cache_dir, revision, source, datetime.now(timezone.utc).isoformat()),
                     source=source, last_failure=None)
        return SyncResult(True, "synced", revision, published_snapshot.problems)
    except Exception as exc:
        if published is not None:
            shutil.rmtree(published, ignore_errors=True)
        _write_state(cache_dir, current, source=current.source or source, last_failure=str(exc))
        return SyncResult(False, error=f"Registry synchronization failed: {exc}; the previous validated tree remains active.")
    finally:
        shutil.rmtree(work_parent, ignore_errors=True)


def _obtain(source: str, git: str | None, work: Path, system: System) -> tuple[Path, str]:
    """Fetch the registry into *work* and return its root folder and 40-hex revision."""
    if git is not None and is_git_source(source):
        destination = work / "clone"
        command = [
            git, "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
            "-c", "core.symlinks=false", "-c", f"core.hooksPath={os.devnull}",
            "clone", "--depth", "1", "--single-branch", "--no-tags", "--quiet", source, str(destination),
        ]
        result = system.run(command, env=git_environment(), timeout=600)
        if result.returncode != 0:
            raise ConfigValidationError(f"git clone failed: {result.stderr.strip()}")
        head = system.run([git, "-C", str(destination), "rev-parse", "HEAD"], env=git_environment())
        revision = head.stdout.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ConfigValidationError("git clone did not report a commit")
        return destination, revision

    # No trusted Git: download the ZIP archive through the system boundary.
    url = archive_url(source)
    if urllib.parse.urlparse(url).scheme.lower() not in {"http", "https", "file"}:
        raise ConfigValidationError(f"Registry source must be an http(s):// or file: URL to a ZIP archive: {source}")
    archive_path = work / "registry.zip"
    system.fetch(url, None, archive_path)
    revision = archive_revision(archive_path)
    return extract_archive(archive_path, work / "archive"), revision


def _write_state(cache_dir: Path, state: CacheState, *, source: str, last_failure: str | None) -> None:
    """Atomically publish cache metadata while retaining the active tree."""
    write_toml(cache_dir / "state.toml", {
        "revision": state.revision or "",
        "source": source,
        "synchronized_at": state.synchronized_at or "",
        "last_failure": last_failure or "",
    })
