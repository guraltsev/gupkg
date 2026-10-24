"""Create, export and bundle package definitions (``gupkg pkg new``, ``pkg export`` and ``pkg bundle``).

These are the maintainer-side commands of the ``pkg`` group. ``new`` writes a starter version directory or tracker that
passes ``gupkg pkg check``; ``export`` writes a distribution ZIP of one version (``<name>\\<version>\\...`` plus the package's
``_check_updates`` when it has one) and reports its SHA-256; ``bundle`` copies the verified bundle of the running gupkg into a
version directory so the program can be installed without gupkg (design section 21). Nothing is overwritten, links are never
followed, and every command checks the directory it acts on first.

Usage and API
-------------
``new_package(parent, name, version=None, track=None)`` returns the files it wrote. ``export_distribution(version_dir, output)``
returns an ``ExportResult`` (path, SHA-256 and entry count). ``bundle_gupkg(version_dir, source)`` copies the bundle tree and
returns the number of files copied. ``AuthoringError`` carries the message of a refusal.

Implementation Approach
-----------------------
Every output goes to a new file or a new folder that must not exist yet. The ZIP is written in sorted order with a fixed
timestamp so the same version directory always gives the same bytes, and the digest is computed from the written file. The
bundle copy refuses a source or a destination that contains a link or a junction, and leaves out bytecode and per-install state.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .model.version import MANIFEST_NAME, TRACKER_DIR_NAME, is_version_name
from .pkgcheck import check_directory
from .services.safepath import is_reparse_point

# The time stored for every ZIP entry, so that an export is reproducible byte for byte.
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
_PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class AuthoringError(ValueError):
    """Report an authoring command that was refused, and why."""


@dataclass(frozen=True)
class ExportResult:
    """The ZIP ``export_distribution`` wrote: where, its SHA-256 and how many entries it holds."""

    path: Path
    sha256: str
    entries: int


# ---------------------------------------------------------------------------
# pkg new
# ---------------------------------------------------------------------------


def new_package(parent: Path, name: str, version: str | None = None, track: str | None = None) -> list[Path]:
    """Write a starter for package *name* below *parent* and return the files written.

    Parameters
    ----------
    parent : Path
        The folder (a root, or any working folder) that receives ``<name>\\``.
    name : str
        The package name.
    version : str, optional
        Write the pinned version directory ``<name>\\<version>\\pkg.toml``.
    track : str, optional
        ``github:owner/repo``: write a tracker that follows the releases of that repository. Without *version* and
        *track* a tracker with placeholders is written.

    Raises
    ------
    AuthoringError
        If the name, version or repository is not valid, or a file to write already exists.
    """
    if not _PACKAGE_NAME.fullmatch(name) or not is_version_name(name):
        raise AuthoringError(f"'{name}' cannot be a package name: use letters, digits, '.', '_' and '-'")
    if version is not None and not is_version_name(version):
        raise AuthoringError(f"'{version}' cannot be a version name")
    repository = "owner/repo"
    if track is not None:
        kind, _, spec = track.partition(":")
        if kind != "github" or not _GITHUB_REPO.fullmatch(spec):
            raise AuthoringError("--track takes github:owner/repo (the other providers are written by hand, see `pkg check`)")
        repository = spec

    # Decide every file first, so nothing is written when one of them exists.
    package = Path(parent) / name
    files: dict[Path, str] = {}
    if version is not None:
        files[package / version / MANIFEST_NAME] = _version_starter(name, version)
    if track is not None or version is None:
        files[package / TRACKER_DIR_NAME / MANIFEST_NAME] = _tracker_starter(name, repository)
    for path in files:
        if os.path.lexists(path):
            raise AuthoringError(f"{path} exists; gupkg never overwrites a package definition")
    if is_reparse_point(package) or is_reparse_point(Path(parent)):
        raise AuthoringError(f"{package} or its folder is a link; refusing to write through it")

    # Write each starter; the folders are created only now that every check has passed.
    written: list[Path] = []
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def _tracker_starter(name: str, repository: str) -> str:
    """Return the text of a starter tracker that follows the GitHub releases of *repository*."""
    return (
        "format = 2\n"
        f'description = "{name}"\n'
        f'project_url = "https://github.com/{repository}"\n'
        "\n"
        "# `gupkg check-updates` runs this tracker and writes each new version as a pinned directory.\n"
        "[track]\n"
        'provider = "github"\n'
        f'repo = "{repository}"\n'
        "# The release asset to download; ${version} is the version the tracker found.\n"
        f'asset = "{name}-${{version}}-win64.zip"\n'
        "\n"
        "[source]\n"
        'download_url = "${asset_url}"\n'
        "\n"
        "# Add shortcuts, environment variables, PATH entries and command shims here, for example:\n"
        "# [[shim]]\n"
        f'# name = "{name}"\n'
        f'# target = "$App\\\\{name}.exe"\n'
    )


def _version_starter(name: str, version: str) -> str:
    """Return the text of a starter pinned version directory for *name* *version*."""
    return (
        "format = 2\n"
        f'description = "{name} {version}"\n'
        "\n"
        "[source]\n"
        f'download_url = "https://example.org/{name}-{version}.zip"\n'
        "# gupkg refuses a download without a checksum: add the SHA-256 of the file once you have checked it.\n"
        '# sha256 = "..."\n'
        "\n"
        "# Add shortcuts, environment variables, PATH entries and command shims here, for example:\n"
        "# [[shim]]\n"
        f'# name = "{name}"\n'
        f'# target = "$App\\\\{name}.exe"\n'
    )


# ---------------------------------------------------------------------------
# pkg export
# ---------------------------------------------------------------------------


def export_distribution(version_dir: Path, output: Path) -> ExportResult:
    """Write a distribution ZIP of one version directory and return its path, SHA-256 and entry count.

    The ZIP holds ``<name>\\<version>\\...`` (the whole version directory, bundled gupkg and shipped ``App`` included) and the
    package's ``_check_updates`` folder when it has one, so it unpacks into a root as it stands (design 21.2). The version
    directory must pass ``gupkg pkg check`` and contain no link.

    Raises
    ------
    AuthoringError
        If the directory is not a valid version directory, holds a link, or *output* exists.
    """
    version_dir = Path(version_dir).resolve()
    report = check_directory(version_dir)
    if not report.ok:
        raise AuthoringError(f"{version_dir} is not a valid version directory: " + "; ".join(report.errors))
    if os.path.lexists(output):
        raise AuthoringError(f"{output} exists; gupkg never overwrites a file")
    name, version = version_dir.parent.name, version_dir.name
    tracker = version_dir.parent / TRACKER_DIR_NAME

    # Collect every entry first (sorted, no link followed) so a refusal leaves no half-written ZIP.
    entries: list[tuple[Path, str]] = []
    for folder, prefix in ((version_dir, f"{name}/{version}"), *(((tracker, f"{name}/{TRACKER_DIR_NAME}"),) if tracker.is_dir() else ())):
        for current, folders, files in os.walk(folder):
            here = Path(current)
            for item in [*folders, *files]:
                if is_reparse_point(here / item):
                    raise AuthoringError(f"{here / item} is a link; a distribution never holds links")
            folders[:] = sorted(item for item in folders if item != "__pycache__")
            for item in sorted(files):
                if item.endswith(".pyc"):
                    continue
                relative = (here / item).relative_to(folder).as_posix()
                entries.append((here / item, f"{prefix}/{relative}"))

    # Write the ZIP through a partial file and rename it, so the output appears whole or not at all.
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, arcname in entries:
            info = zipfile.ZipInfo(arcname, _ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(source, "rb") as handle, archive.open(info, "w") as sink:
                shutil.copyfileobj(handle, sink)
    os.replace(partial, output)
    digest = hashlib.sha256()
    with open(output, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return ExportResult(output, digest.hexdigest(), len(entries))


# ---------------------------------------------------------------------------
# pkg bundle
# ---------------------------------------------------------------------------


def bundle_gupkg(version_dir: Path, source: Path) -> int:
    """Copy the gupkg tree *source* into ``<version_dir>\\gupkg`` and return the number of files copied.

    The caller has verified *source* against its ``MANIFEST.sha256``. Links are refused in both places, bytecode and the
    per-install ``gupkg-config.toml`` are left out, and an existing ``gupkg`` folder is never replaced.

    Raises
    ------
    AuthoringError
        If *version_dir* is not a valid version directory, already has a ``gupkg`` folder, or *source* holds a link.
    """
    version_dir, source = Path(version_dir).resolve(), Path(source).resolve()
    report = check_directory(version_dir)
    if not report.ok:
        raise AuthoringError(f"{version_dir} is not a valid version directory: " + "; ".join(report.errors))
    destination = version_dir / "gupkg"
    if os.path.lexists(destination):
        raise AuthoringError(f"{destination} exists; gupkg never replaces a bundled copy")

    # Copy into a work folder beside the destination and rename it, so a failure leaves no partial bundle.
    work = version_dir / f".gupkg-bundle-{os.getpid()}"
    count = 0
    try:
        for current, folders, files in os.walk(source):
            here = Path(current)
            for item in [*folders, *files]:
                if is_reparse_point(here / item):
                    raise AuthoringError(f"{here / item} is a link; a bundle never holds links")
            folders[:] = [item for item in folders if item != "__pycache__"]
            relative = here.relative_to(source)
            (work / relative).mkdir(parents=True, exist_ok=True)
            for item in files:
                if item.endswith(".pyc") or item == "gupkg-config.toml":
                    continue
                shutil.copy2(here / item, work / relative / item)
                count += 1
        os.replace(work, destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return count
