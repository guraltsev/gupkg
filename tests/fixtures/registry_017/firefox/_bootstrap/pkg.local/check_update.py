"""Discover the newest stable Mozilla Firefox 64-bit Windows installer.

Mozilla publishes the current release number in its product-details feed and
keeps every release under ``archive.mozilla.org``. Each release folder has a
``SHA256SUMS`` file covering the installers.

Usage and API
-------------
The package manager calls ``check_update(context)`` during a module update
check. The returned candidate holds the version, installer URL, and digest.

Implementation Approach
-----------------------
The ``LATEST_FIREFOX_VERSION`` field names the newest stable release (not
beta, ESR, or nightly). The en-US ``win64`` installer URL is built from it
and the matching line of ``SHA256SUMS`` supplies the digest.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any


PKG_MODULE_API = 1

_VERSIONS_URL = "https://product-details.mozilla.org/1.0/firefox_versions.json"
_RELEASES = "https://archive.mozilla.org/pub/firefox/releases/"
_VERSION = re.compile(r"\d+(?:\.\d+)+")
_SHA256_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64})  (?P<name>.+)$", re.MULTILINE)


def _read(url: str) -> str:
    """Return the text of one Mozilla page or checksum file."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"Could not read {url}: {exc}") from exc


def check_update(context: dict[str, Any]) -> dict[str, str] | None:
    """Return the newest stable Firefox installer when it is not installed.

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
        A Mozilla page cannot be read or the release number is malformed.
    """
    try:
        version = str(json.loads(_read(_VERSIONS_URL))["LATEST_FIREFOX_VERSION"])
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"Mozilla version feed has no stable Firefox release: {exc}") from exc
    if not _VERSION.fullmatch(version):
        raise RuntimeError(f"Unexpected Firefox version: {version!r}")

    current = context.get("current", {})
    if current.get("version") == version and current.get("appReady", True):
        return None

    # The checksum file lists installers by their path inside the release folder.
    directory = f"{_RELEASES}{version}/"
    file_name = f"Firefox Setup {version}.exe"
    sums_name = f"win64/en-US/{file_name}"
    digest = next(
        (m.group("digest") for m in _SHA256_LINE.finditer(_read(directory + "SHA256SUMS"))
         if m.group("name").strip() == sums_name),
        None,
    )

    candidate = {
        "candidateId": f"firefox:{version}",
        "version": version,
        "url": directory + urllib.parse.quote(sums_name),
        "fileName": file_name,
    }
    if digest is not None:
        candidate["sha256"] = digest.lower()
    return candidate
