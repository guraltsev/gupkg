"""Discover the newest stable GNU Emacs 64-bit UCRT ZIP for Windows.

GNU publishes Windows builds in one folder per major version. This module
finds the newest folder, picks the full UCRT64 ZIP (the one that bundles its
libraries), and reads the SHA-256 digest from the checksum file beside it
when GNU publishes one.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, archive URL, and digest.

Implementation Approach
-----------------------
The directory indexes on ``ftp.gnu.org`` are parsed for link names. Release
numbers such as ``31.1_1`` (a rebuild of 31.1) are ordered by their numeric
components. Dependency-free (``nodeps``), installer, and source files are
ignored. Pretest builds on ``alpha.gnu.org`` are intentionally not followed.
"""

from __future__ import annotations

import re
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_ROOT = "https://ftp.gnu.org/gnu/emacs/windows/"
_MAJOR_DIRECTORY = re.compile(r'href="emacs-(?P<major>\d+)/"')
_ARCHIVE = re.compile(r"emacs-(?P<version>\d+(?:\.\d+)*(?:_\d+)?)-UCRT64\.zip")
_SHA256_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64}) \*?(?P<name>\S+)$", re.MULTILINE)


def _read(url: str) -> str:
    """Return the text of one GNU download page or checksum file."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def _version_key(version: str) -> tuple[int, ...]:
    """Return numeric components suitable for ordering Emacs versions."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest stable Emacs UCRT64 ZIP when it is not installed.

    Parameters
    ----------
    context : dict[str, Any]
        Update context containing the current package identity.

    Returns
    -------
    dict[str, str] | None
        Candidate metadata, or ``None`` when the newest release is installed
        and its payload is healthy.

    Raises
    ------
    RuntimeError
        A GNU page cannot be read or lists no Windows build.
    """
    # The newest major-version folder holds the newest builds.
    majors = {int(m.group("major")) for m in _MAJOR_DIRECTORY.finditer(_read(_ROOT))}
    if not majors:
        raise RuntimeError("GNU Emacs Windows index lists no version folders")
    directory = f"{_ROOT}emacs-{max(majors)}/"

    # Choose the highest release that ships the full UCRT64 ZIP.
    listing = _read(directory)
    versions = {m.group("version") for m in _ARCHIVE.finditer(listing)}
    if not versions:
        raise RuntimeError(f"{directory} contains no UCRT64 ZIP")
    version = max(versions, key=_version_key)

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    # Look for the digest GNU publishes for this exact file. The checksum file
    # is named ``sha256sums`` for newer builds and ``sha256sum`` for older ones.
    file_name = f"emacs-{version}-UCRT64.zip"
    digest = None
    for sums_name in (f"emacs-{version}-sha256sums.txt", f"emacs-{version}-sha256sum.txt"):
        try:
            text = _read(directory + sums_name)
        except RuntimeError:
            continue
        digest = next(
            (m.group("digest") for m in _SHA256_LINE.finditer(text) if m.group("name") == file_name),
            None,
        )
        if digest is not None:
            break

    candidate = {
        "candidateId": f"emacs:{version}",
        "version": version,
        "url": directory + file_name,
        "fileName": file_name,
    }
    # GNU does not publish a checksum file for every build, so the digest is
    # attached only when one exists for this exact archive.
    if digest is not None:
        candidate["sha256"] = digest.lower()
    return candidate
