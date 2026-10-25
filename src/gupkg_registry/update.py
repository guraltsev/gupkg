"""Run the trackers and write new pinned version directories (design 17, decision D36).

For each package (or the named ones) and each new upstream version the tool:

1. runs the tracker (``gupkg.services.track.run_tracker``) and stops for a version that already
   has a directory (**every version is kept**: nothing is ever deleted or overwritten);
2. downloads the artifact under the checksum policy: against the checksum the tracker discovered, or,
   when the vendor publishes none, over ``https://`` only, computing the hash itself and writing
   ``sha256_from = "registry"`` so a maintainer sees which hashes are the tool's own (D8);
3. runs unpack and prepare into a staging folder in the *sandbox* folder (never in the registry),
   proving the package installs, and discards it;
4. writes ``<name>/<version>/`` (``pkg.toml`` with ``sha256``, the tracker's support files without
   any update-check code, and the launchers), computes the fingerprint of that directory and
   records it in ``_registry.toml``;
5. stops. The tool commits nothing and pushes nothing (``publish.py`` does that, only on request).

Usage and API
-------------
``update_registry(root, system, sandbox=..., names=None, environ=None)`` returns an
``UpdateReport`` with one ``Outcome`` per package. ``pin_version(root, system, "name/version",
sandbox=...)`` gives a version that has no hash its first one. Both take the ``System`` boundary,
so tests run them against ``FakeSystem`` and recorded responses.

Implementation Approach
-----------------------
A package is the unit of failure: any error (network, checksum, unpack, validation) is reported for
that package, leaves the registry untouched for it, and the other packages go on. The new directory is
built beside its final place and moved with one rename after it validated, so a failed run leaves no
half-written version. Archives and pages are data: this module only passes them to the services that
validate them. The sandbox folder receives the HTTP cache and the staged ``App``; the staged ``App``
is removed afterwards.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gupkg.model.checksum import SKIPPED_CLI
from gupkg.model.manifest import ManifestKind, manifest_to_toml, parse_manifest, render_pinned
from gupkg.model.version import MANIFEST_NAME, TRACKER_DIR_NAME
from gupkg.report.events import Warning, emit
from gupkg.pkgcheck import check_directory, defines_track
from gupkg.services.artifact import fetch_artifact, reverify
from gupkg.services.fingerprint import fingerprint_directory
from gupkg.services.hooks import HookContext, HookPaths, HookReporter
from gupkg.services.http import HttpClient, github_environment
from gupkg.services.populate import populate_app, populate_git
from gupkg.services.safepath import is_reparse_point, remove_tree
from gupkg.services.track import TrackEnvironment, run_tracker
from gupkg.system.port import System
from gupkg.launchers import write_launchers

from .layout import LAUNCHER_NAMES, TRACKER_LAUNCHER_NAME, RegistryFileError, package_dirs, read_registry_file, write_registry_file, write_text_bytes

REGISTRY_SHA256_FROM = "registry"
LOCK_NAME = ".registry-tool.lock"


@dataclass(frozen=True)
class Outcome:
    """What happened to one package: ``added``, ``current``, ``pinned`` or ``failed``.

    ``review`` is true when the version's hash was computed by this tool (the vendor published
    none), which a maintainer must look at before pushing.
    """

    package: str
    status: str
    version: str | None = None
    message: str = ""
    review: bool = False
    path: Path | None = None
    unversioned: bool = False


@dataclass(frozen=True)
class UpdateReport:
    """The outcomes of one run, in package order."""

    outcomes: tuple[Outcome, ...] = ()

    @property
    def ok(self) -> bool:
        """Return whether no package failed."""
        return all(outcome.status != "failed" for outcome in self.outcomes)

    @property
    def written(self) -> tuple[Outcome, ...]:
        """Return the outcomes that created or changed a version directory."""
        return tuple(o for o in self.outcomes if o.status in ("added", "pinned"))


@dataclass
class _Run:
    """Shared state of one run: the boundary, the sandbox and the one HTTP client."""

    root: Path
    system: System
    sandbox: Path
    client: HttpClient
    settings: dict = field(default_factory=dict)


def _client(system: System, sandbox: Path, environ: Mapping[str, str] | None) -> HttpClient:
    """Return the shared client: its cache is in the sandbox, its only credential is ``GITHUB_TOKEN``."""
    return HttpClient(system, cache_dir=sandbox / "cache", env=github_environment(system, environ or {}))


def update_registry(
    root: Path,
    system: System,
    *,
    sandbox: Path,
    names: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    on_start: Callable[[str, int, int], None] | None = None,
    on_outcome: Callable[[Outcome], None] | None = None,
) -> UpdateReport:
    """Run the trackers of *names* (default: every package) and write what is new.

    Parameters
    ----------
    root : Path
        The registry checkout; it needs a valid ``_registry.toml``.
    system : System
        The machine boundary (network, processes).
    sandbox : Path
        A scratch folder for downloads and the staged ``App`` (created if needed).
    names : sequence of str, optional
        Only these packages. An unknown name is a failed outcome, not an error.
    environ : mapping, optional
        The environment the tool passes explicitly; only ``GITHUB_TOKEN`` is used.
    on_start : callable, optional
        Called as ``on_start(name, number, total)`` before each package is checked (progress feedback).
    on_outcome : callable, optional
        Called with each package's ``Outcome`` as soon as it is known.

    Returns
    -------
    UpdateReport
        One outcome per package. Nothing is committed or pushed here.

    Raises
    ------
    RegistryFileError
        If ``_registry.toml`` is missing or malformed (nothing is written).
    """
    root, sandbox = Path(root), Path(sandbox)
    settings = read_registry_file(root)
    sandbox.mkdir(parents=True, exist_ok=True)
    lock = root / LOCK_NAME
    try:
        lock.open("x").close()
    except FileExistsError:
        raise RegistryFileError(
            f"another registry tool run is using {root} (lock file {LOCK_NAME}); wait for it, or delete the file if that run crashed"
        ) from None
    try:
        return _update_all(root, system, sandbox, names, environ, on_start, on_outcome, settings)
    finally:
        lock.unlink(missing_ok=True)


def _update_all(root, system, sandbox, names, environ, on_start, on_outcome, settings) -> UpdateReport:
    """Run the trackers of *names* while the caller holds the lock (see ``update_registry``)."""
    run = _Run(root, system, sandbox, _client(system, sandbox, environ), settings)
    available = {p.name: p for p in package_dirs(root)}
    _remove_stale_staging(available.values())
    wanted = list(dict.fromkeys(names)) if names else sorted(available)
    outcomes: list[Outcome] = []

    def record(outcome: Outcome) -> None:
        """Keep *outcome* and tell the caller at once."""
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)

    for number, name in enumerate(wanted, start=1):
        if on_start is not None:
            on_start(name, number, len(wanted))
        package = available.get(name)
        if package is None:
            record(Outcome(name, "failed", message="no such package in the registry"))
            continue
        if not (package / TRACKER_DIR_NAME / MANIFEST_NAME).is_file():
            continue  # a package without a tracker has nothing to check
        try:
            record(_update_package(run, package))
        except Exception as error:  # noqa: BLE001 - the package is the unit of failure; the message names the cause
            record(Outcome(name, "failed", message=f"{type(error).__name__}: {error}"))
    write_registry_file(root, run.settings)
    return UpdateReport(tuple(outcomes))


STALE_STAGING_SECONDS = 3600
_STAGING = re.compile(r"\.new-[0-9a-f]{8}")


def _remove_stale_staging(packages) -> None:
    """Delete the ``.new-xxxxxxxx`` staging folders an interrupted earlier run left in the package folders.

    Only folders whose whole name is ``.new-`` and eight hexadecimal digits, which this tool alone creates,
    are removed, never a link, and only when they are older than ``STALE_STAGING_SECONDS`` (an hour), so a
    run that is still working in another process keeps its folder. A folder that cannot be inspected or
    removed is reported as a warning and the run goes on.
    """
    for package in packages:
        for entry in package.iterdir():
            if not _STAGING.fullmatch(entry.name):
                continue
            try:
                if entry.is_dir() and not is_reparse_point(entry) and time.time() - entry.stat().st_mtime >= STALE_STAGING_SECONDS:
                    remove_tree(entry)
            except OSError as error:
                emit(Warning(f"Could not remove the leftover staging folder {entry}: {error}"))


def pin_version(
    root: Path, system: System, key: str, *, sandbox: Path, environ: Mapping[str, str] | None = None
) -> Outcome:
    """Give the existing version ``name/version`` that has no ``sha256`` its first hash.

    The artifact is downloaded over ``https://`` only, unpacked in the sandbox to prove the version
    installs, and the observed hash is written with ``sha256_from = "registry"``. The fingerprint
    is recomputed (``pkg.toml`` changed) and the version leaves the ``unpinned`` list.
    """
    root, sandbox = Path(root), Path(sandbox)
    settings = read_registry_file(root)
    name, _, version = key.partition("/")
    directory = root / name / version
    try:
        if not name or not version or not (directory / MANIFEST_NAME).is_file():
            raise ValueError(f"{key} is not a version directory of this registry")
        report = check_directory(directory)
        if not report.ok or report.manifest is None:
            raise ValueError("; ".join(report.errors) or "invalid version")
        manifest = report.manifest
        if manifest.source.download_url is None or manifest.source.git is not None:
            raise ValueError("only a version with a download_url can be pinned")
        if manifest.source.sha256 is not None:
            raise ValueError("the version already has a sha256; the registry never replaces one")
        sandbox.mkdir(parents=True, exist_ok=True)
        run = _Run(root, system, sandbox, _client(system, sandbox, environ), settings)
        digest = _fetch_and_prove(run, name, version, manifest, None)
        pinned = dataclasses.replace(
            manifest, source=dataclasses.replace(manifest.source, sha256=digest, sha256_from=REGISTRY_SHA256_FROM)
        )
        write_text_bytes(directory / MANIFEST_NAME, manifest_to_toml(pinned, explicit_modes=True))
        after = check_directory(directory)
        if not after.ok or after.warnings:
            raise ValueError("; ".join((*after.errors, *after.warnings)))
        settings.setdefault("fingerprints", {})[key] = fingerprint_directory(directory)
        settings["unpinned"] = [item for item in settings.get("unpinned", []) if item != key]
        write_registry_file(root, settings)
    except Exception as error:  # noqa: BLE001 - reported, not raised: the caller prints one line
        return Outcome(name, "failed", version or None, f"{type(error).__name__}: {error}")
    return Outcome(name, "pinned", version, f"sha256 {digest} computed by the registry tool: review it", True, directory)


def _fetch_and_prove(run: _Run, name: str, version: str, manifest, expected: str | None) -> str | None:
    """Download and unpack *manifest*'s source in the sandbox; return the hash the version should carry.

    With *expected* (a discovered checksum) the download is verified against it. Without one, the
    download is allowed over ``https://`` only and the observed hash is returned. For a git source
    nothing is downloaded: the pinned commit is the anchor and ``None`` is returned.
    """
    stage = run.sandbox / "stage" / f"{name}-{version}-{uuid.uuid4().hex[:8]}"
    stage.mkdir(parents=True)
    try:
        context = HookContext(
            package=name, version=version, system=run.system, http=run.client,
            paths=HookPaths(run.root / name, run.root / name / version, stage / "App", None, stage / "work", run.sandbox / "cache"),
            options={}, captures={}, reporter=HookReporter(),
        )
        source = manifest.source
        if source.git is not None:
            populate_git(run.system, manifest, stage / "App", context=context)
            return None
        fetched = fetch_artifact(
            run.client, source.download_url, expected_sha256=expected,
            skip=SKIPPED_CLI if expected is None else None,
            sha256_from=source.sha256_from if expected is not None else REGISTRY_SHA256_FROM,
            headers=source.headers or None,
        )
        reverify(fetched)
        populate_app(run.system, manifest, fetched.download.path, stage / "App", context=context, filename=fetched.download.filename)
        return fetched.record.observed_sha256 if expected is None else expected
    finally:
        remove_tree(stage)


def _update_package(run: _Run, package: Path) -> Outcome:
    """Check one package's tracker and write its new version, if there is one."""
    name = package.name
    tracker = package / TRACKER_DIR_NAME
    report = check_directory(tracker)
    if not report.ok or report.warnings:
        return Outcome(name, "failed", message="the tracker does not validate: " + "; ".join((*report.errors, *report.warnings)))
    manifest = parse_manifest((tracker / MANIFEST_NAME).read_text(encoding="utf-8-sig"), ManifestKind.TRACKER)
    environment = TrackEnvironment(
        run.system, run.client, work_dir=run.sandbox / "track" / name, directory=tracker, package=name
    )
    result = run_tracker(manifest, environment)
    key = f"{name}/{result.version}"
    destination = package / result.version
    if destination.exists() or any(c.name.casefold() == result.version.casefold() for c in package.iterdir()):
        return Outcome(name, "current", result.version, "already in the registry", path=destination)
    pinned = render_pinned(manifest, result)
    if pinned.source.populate != "download" and pinned.source.git is None:
        return Outcome(name, "failed", result.version, "only downloaded or git sources can be pinned by the registry tool")

    # Download and prove it installs; a discovered checksum is verified, otherwise the tool computes one.
    expected = pinned.source.sha256
    digest = _fetch_and_prove(run, name, result.version, pinned, expected)
    computed = pinned.source.git is None and expected is None
    if digest is not None:
        pinned = dataclasses.replace(
            pinned, source=dataclasses.replace(
                pinned.source, sha256=digest, sha256_from=pinned.source.sha256_from if not computed else REGISTRY_SHA256_FROM
            ),
        )

    staging = package / f".new-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir()
        write_text_bytes(staging / MANIFEST_NAME, manifest_to_toml(pinned, explicit_modes=True))
        _copy_support_files(tracker, staging)
        write_launchers(staging, LAUNCHER_NAMES)
        # The directory is checked under its final name: the check reads the version from it.
        staging.rename(destination)
    except BaseException:
        remove_tree(staging)
        raise
    try:
        after = check_directory(destination)
        if not after.ok or after.warnings:
            raise ValueError("the new version does not validate: " + "; ".join((*after.errors, *after.warnings)))
        run.settings.setdefault("fingerprints", {})[key] = fingerprint_directory(destination)
    except BaseException:
        remove_tree(destination)
        raise
    note = "sha256 computed by the registry tool: review it" if computed else "verified against the discovered checksum"
    if result.notes:
        note += "; " + "; ".join(result.notes)
    unversioned = name in run.settings.get("unversioned", [])
    if unversioned:
        note += "; the download address has no version in it, so this version stops verifying when the vendor replaces the file"
    return Outcome(name, "added", result.version, note, computed, destination, unversioned)


def sync_launchers(root: Path) -> list[str]:
    """Write the generated launchers into the whole registry at *root*; return one line per file changed.

    Every pinned version directory gets ``install.cmd`` and ``gupkg-tui.cmd``, every tracker folder gets ``check-updates.cmd``
    and loses a stale ``install.cmd`` or ``gupkg-tui.cmd`` (a tracker never installs). Because the fingerprint of a version covers
    its launchers, the fingerprint of each version that changed is recomputed in ``_registry.toml``. A registry that is already in
    step is left byte for byte as it was.
    """
    root = Path(root)
    data = read_registry_file(root)
    fingerprints = dict(data.get("fingerprints", {}))
    changed: list[str] = []
    for package in package_dirs(root):
        for child in sorted(item for item in package.iterdir() if item.is_dir()):
            if child.name == TRACKER_DIR_NAME:
                tracker = child
                for stale in LAUNCHER_NAMES:
                    if (tracker / stale).is_file():
                        (tracker / stale).unlink()
                        changed.append(f"{package.name}/{TRACKER_DIR_NAME}/{stale}: removed (a tracker never installs)")
                if _write_if_different(tracker, (TRACKER_LAUNCHER_NAME,)):
                    changed.append(f"{package.name}/{TRACKER_DIR_NAME}/{TRACKER_LAUNCHER_NAME}: written")
            elif (child / MANIFEST_NAME).is_file():
                written = _write_if_different(child, LAUNCHER_NAMES)
                if written:
                    changed.extend(f"{package.name}/{child.name}/{name}: written" for name in written)
                    key = f"{package.name}/{child.name}"
                    fingerprints[key] = fingerprint_directory(child)
    if any(line.endswith(": written") and "/_check_updates/" not in line for line in changed):
        write_registry_file(root, {**data, "fingerprints": fingerprints})
    return changed


def _write_if_different(folder: Path, names: tuple[str, ...]) -> tuple[str, ...]:
    """Write the launchers *names* into *folder* when they are missing or differ; return the names written."""
    from gupkg.launchers import LAUNCHERS, launcher_text

    written = []
    for name in names:
        expected = launcher_text(LAUNCHERS[name]).encode("ascii")
        target = folder / name
        if not target.is_file() or target.read_bytes() != expected:
            write_launchers(folder, (name,))
            written.append(name)
    return tuple(written)


def _copy_support_files(tracker: Path, destination: Path) -> None:
    """Copy the tracker's support files into *destination*, leaving out ``pkg.toml``, launchers and update-check code.

    A Python module that defines ``track`` is never copied (design 9.3). A link is refused.
    """
    for path in sorted(tracker.rglob("*")):
        relative = path.relative_to(tracker)
        if is_reparse_point(path):
            raise ValueError(f"{relative.as_posix()} in the tracker is a link; refused")
        if path.is_dir() or relative.as_posix() == MANIFEST_NAME or relative.name in (*LAUNCHER_NAMES, TRACKER_LAUNCHER_NAME):
            continue
        if "__pycache__" in relative.parts or path.suffix.lower() in (".pyc", ".bak"):
            continue
        if path.suffix.lower() == ".py" and defines_track(path.read_text(encoding="utf-8-sig")):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
