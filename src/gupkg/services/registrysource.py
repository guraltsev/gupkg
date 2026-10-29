"""Obtain a registry tree: a Git clone from a trusted Git, or the repository's ZIP archive.

Registry sources are untrusted input, and an elevated gupkg must never run code or read a cache that a
standard user can change. ``trusted_git`` therefore finds a ``git.exe`` only in an admin-only folder of
``PATH``, ``git_environment`` builds the scrubbed environment Git runs under, and ``extract_archive`` unpacks
a ZIP without following unsafe names or links. Nothing here prints; the Git output is reported line by line
as events.

Usage and API
-------------
``gupkg.catalog.registry.sync_registry`` calls these functions: ``is_git_source``, ``trusted_git``,
``git_remote_revision`` and ``git_environment`` for a clone, ``archive_url``, ``archive_revision`` and
``extract_archive`` for the archive route, and ``require_trusted_cache`` to refuse a cache folder that an
elevated run cannot trust.

Implementation Approach
-----------------------
Git is started through ``gupkg.services.gitenv`` (an allow-listed environment, user and system configuration
off, only network transports) and only from a folder that administrators alone can change. A source that no
trusted Git can fetch falls back to the ZIP archive that GitHub serves for a repository, whose entries are
checked one by one before anything is written.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

from ..core import ConfigValidationError
from ..report.events import subprocess_lines
from ..model.scope import Scope
from ..selfmgmt.elevation import UntrustedLocationError
from ..system.port import System, trusted_path_entries
from . import gitenv

# A registry archive that GitHub serves records its commit in the ZIP comment; other archives are named by digest.
_REVISION = re.compile(r"[0-9a-f]{40}")

# MS-DOS device names, which Windows opens as devices whatever the folder and the extension (``nul.txt`` is ``nul``).
_DEVICE_NAME = re.compile(r"(?i)(con|prn|aux|nul|conin\$|conout\$|(com|lpt)[0-9¹²³])[ ]*(\..*)?")

# Bounds on a registry archive (a registry is text files): a larger one is refused, whatever its entries claim about their size.
MAX_ARCHIVE_ENTRIES = 50_000
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def is_git_source(source: str) -> bool:
    """Return whether *source* names a Git repository rather than a ZIP archive."""
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme.lower() != "https":
        return False
    path = parsed.path.rstrip("/")
    if path.lower().endswith(".git"):
        return True
    return parsed.netloc.lower() == "github.com" and len([part for part in path.split("/") if part]) == 2


def trusted_git(system: System) -> str | None:
    """Return a ``git.exe`` that only administrators can modify, or ``None``.

    Only the admin-only, existing, absolute folders of ``PATH`` are searched, in order, so a user-writable copy
    earlier on ``PATH`` is skipped instead of run.
    """
    for folder in trusted_path_entries(system):
        candidate = Path(folder) / "git.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def git_environment(scope: Scope) -> dict[str, str]:
    """Return a child environment that ignores user Git settings and never prompts."""
    return gitenv.git_environment(scope.system, scope.mode)


def git_remote_revision(scope: Scope, git: str, source: str) -> str | None:
    """Return the remote ``HEAD`` commit of *source* without cloning, or ``None`` on failure."""
    command = [git, "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "ls-remote", source, "HEAD"]
    result = scope.system.run(command, env=git_environment(scope), timeout=60, on_line=subprocess_lines([git]))
    match = re.match(r"([0-9a-f]{40})\s", result.stdout) if result.returncode == 0 else None
    return match.group(1) if match else None


def archive_url(source: str) -> str:
    """Return the ZIP archive URL for *source*.

    A GitHub repository URL designates the archive of its default branch; every other source must already be the
    URL of a ZIP archive.

    Raises
    ------
    ConfigValidationError
        If *source* is a Git repository that is not on github.com, which has no known archive address.
    """
    if not is_git_source(source):
        return source
    parsed = urllib.parse.urlparse(source)
    if parsed.netloc.lower() != "github.com":
        raise ConfigValidationError(
            f"No trusted Git is available, and an archive URL is only known for github.com repositories: {source}"
        )
    repository = parsed.path.rstrip("/")
    repository = repository[: -len(".git")] if repository.lower().endswith(".git") else repository
    return f"https://github.com{repository}/archive/HEAD.zip"


def archive_revision(archive_path: Path) -> str:
    """Return a stable 40-hex revision for a registry archive.

    GitHub archives record their commit ID in the ZIP comment; any other archive is identified by the first 40
    hex digits of its SHA-256 digest.
    """
    with zipfile.ZipFile(archive_path) as archive:
        comment = archive.comment.decode("ascii", errors="ignore").strip().lower()
    if _REVISION.fullmatch(comment):
        return comment

    # No commit in the comment: the content digest names the revision.
    digest = hashlib.sha256()
    with open(archive_path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:40]


def extract_archive(archive_path: Path, destination: Path) -> Path:
    """Extract a registry archive safely and return the folder that holds the packages.

    Entries that are absolute, climb out with ``..``, are symlinks, or are named with a drive are rejected, and so are
    two entries with the same name (compared case-insensitively, as Windows does), more than ``MAX_ARCHIVE_ENTRIES``
    entries and more than ``MAX_ARCHIVE_BYTES`` of content (counted while copying, not from the sizes the entries claim).
    GitHub wraps a repository in one top-level folder; when the archive has exactly one, that folder is the
    registry root.

    Raises
    ------
    ConfigValidationError
        If an entry has an unsafe or repeated name, is a symbolic link, or the archive is too large.
    """
    destination.mkdir(parents=True)
    root = destination.resolve()
    seen: set[str] = set()
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ConfigValidationError(f"Registry archive has {len(entries)} entries; more than {MAX_ARCHIVE_ENTRIES} are refused")
        for info in entries:
            parts = PurePosixPath(info.filename)
            if parts.is_absolute() or ".." in parts.parts or ":" in info.filename or "\\" in info.filename:
                raise ConfigValidationError(f"Registry archive contains an unsafe path: {info.filename}")
            for component in info.filename.split("/"):
                # Windows drops trailing dots and spaces (".. " and "..." mean ".."), and a device name opens the device.
                if component not in ("", ".") and (component != component.rstrip(" .") or _DEVICE_NAME.fullmatch(component)):
                    raise ConfigValidationError(f"Registry archive contains a name Windows treats specially: {info.filename}")
            key = "/".join(parts.parts).casefold()
            if key in seen:
                raise ConfigValidationError(f"Registry archive contains the name {info.filename} twice")
            seen.add(key)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ConfigValidationError(f"Registry archive contains a symlink: {info.filename}")

            # The resolved target must stay inside the destination, whatever the name spells.
            target = (root / PurePosixPath(*parts.parts)).resolve() if parts.parts else root
            if not target.is_relative_to(root):
                raise ConfigValidationError(f"Registry archive contains an unsafe path: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, "wb") as sink:
                    while block := source.read(1 << 20):
                        total += len(block)
                        if total > MAX_ARCHIVE_BYTES:
                            raise ConfigValidationError(f"Registry archive holds more than {MAX_ARCHIVE_BYTES} bytes of content")
                        sink.write(block)
    children = list(root.iterdir())
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


def require_trusted_cache(cache_root: Path, scope: Scope) -> None:
    """Refuse a registry cache that a system-mode run cannot trust.

    The cache supplies package templates, download addresses and hashes that an elevated install would act on,
    so in system mode its folder (or, before it exists, the nearest existing parent) must be writable by
    administrators only.

    Raises
    ------
    ConfigValidationError
        If the run is in system mode and the location is not admin-only.
    """
    # A cache that does not exist yet will be created inside its nearest parent.
    probe = Path(cache_root).absolute()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        scope.require_trusted(probe, "the registry cache")
    except UntrustedLocationError as exc:
        raise ConfigValidationError(
            f"Refusing the registry cache {cache_root} for an elevated run: {exc} "
            "The registry cache lives in the .gupkg folder of the system root, which only administrators may change."
        ) from exc
