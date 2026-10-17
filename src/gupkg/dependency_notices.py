"""Advise when a pinned runtime dependency has a newer stable release on PyPI.

``dependency_notices()`` compares every distribution in ``gupkg-deps.lock`` with
the newest stable, non-yanked release PyPI lists and returns one warning line
per outdated pin. The check is advisory: it never installs, never edits the
lockfile, never blocks start-up, and stays silent when offline or when PyPI
answers with anything unexpected.

Usage and API
-------------
Call ``dependency_notices(...)`` when starting an interactive interface or
reporting ``gupkg manager self status``, and print the returned lines.

Implementation Approach
-----------------------
Requests go over HTTPS to the PyPI JSON API with a short timeout and an overall
deadline. The result is cached for a day beside the lockfile, but only when
that directory is writable; the cache is never moved to a user-writable
location. Every name and version taken from a response or from the cache must
match a strict pattern before it can reach the terminal, so response text cannot
inject control sequences.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

from .locked_deps import LOCK_NAME, read_lock

CACHE_NAME = "gupkg-deps.check.json"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STABLE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def dependency_notices(
    package_directory: Path | None = None,
    *,
    enabled: bool = True,
    now: float | None = None,
    timeout: float = 3.0,
    deadline: float = 8.0,
    fetch=None,
) -> list[str]:
    """Return warning lines for locked dependencies that have a newer stable release.

    Parameters
    ----------
    package_directory : Path, optional
        Directory of the ``gupkg`` package; defaults to the running package.
    enabled : bool, default=True
        Set to ``False`` to skip the check entirely; the environment variable
        ``GUPKG_NO_DEPENDENCY_UPDATE_CHECK=1`` also disables it.
    now : float, optional
        Current time in seconds since the epoch; defaults to the system clock.
    timeout : float, default=3.0
        Seconds allowed for one request.
    deadline : float, default=8.0
        Seconds allowed for all requests together.
    fetch : callable, optional
        Replacement for the network request: receives a distribution name and a
        timeout and returns the decoded PyPI JSON document. Used by tests.

    Returns
    -------
    list of str
        Lines such as ``textual 8.2.8 is pinned; 8.3.0 is available. Run:
        gupkg manager self update-deps``. Empty on any failure.
    """
    if not enabled or os.environ.get("GUPKG_NO_DEPENDENCY_UPDATE_CHECK") == "1":
        return []
    try:
        package = Path(package_directory or Path(__file__).resolve().parent)
        locked = {item.name: item.version for item in read_lock(package / LOCK_NAME)}
        current_time = time.time() if now is None else now
        latest = _latest_versions(package, locked, current_time, timeout, deadline, fetch or _fetch_pypi)
        return [
            f"{name} {pinned} is pinned; {latest[name]} is available. Run: gupkg manager self update-deps"
            for name, pinned in sorted(locked.items())
            if name in latest and _is_newer(latest[name], pinned)
        ]
    except Exception:
        # Advice is optional: offline use and bad data must never block start-up.
        return []


# ---------------------------------------------------------------------------
# Version lookup and cache
# ---------------------------------------------------------------------------


def _latest_versions(package, locked, now, timeout, deadline, fetch) -> dict[str, str]:
    """Return validated newest-stable versions, from the day-old cache or PyPI."""
    cache_path = package / CACHE_NAME
    cached = _read_cache(cache_path, now, locked)
    if cached is not None:
        return cached

    # Ask PyPI for each distribution until the overall deadline; the first
    # failure ends the attempt so an offline machine waits only once.
    latest: dict[str, str] = {}
    started = time.monotonic()
    try:
        for name in locked:
            if time.monotonic() - started > deadline:
                break
            version = _newest_stable(fetch(name, timeout))
            if version is not None:
                latest[name] = version
    except Exception:
        pass

    # Record the attempt (even a failed one) so a day passes before retrying.
    _write_cache(cache_path, now, locked, latest)
    return latest


def _read_cache(path: Path, now: float, locked: dict[str, str]) -> dict[str, str] | None:
    """Return cached versions younger than a day for the same pins, or ``None``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked = float(data["checked"])
        if not 0 <= now - checked < 24 * 3600 or data["locked"] != locked:
            return None
        return {
            name: version
            for name, version in data["latest"].items()
            if _NAME.match(name) and isinstance(version, str) and _STABLE.match(version)
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_cache(path: Path, now: float, locked: dict[str, str], latest: dict[str, str]) -> None:
    """Write the cache beside the lockfile; skip silently if that is not possible."""
    try:
        path.write_text(json.dumps({"checked": now, "locked": locked, "latest": latest}), encoding="utf-8")
    except OSError:
        pass


def _fetch_pypi(name: str, timeout: float) -> dict:
    """Download one distribution's PyPI JSON document over HTTPS."""
    if not _NAME.match(name):
        raise ValueError("unexpected distribution name")
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{name}/json", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return json.loads(body)


def _newest_stable(document: dict) -> str | None:
    """Return the highest stable version that has at least one non-yanked file."""
    best: tuple[int, ...] | None = None
    best_text: str | None = None
    for version, files in document.get("releases", {}).items():
        if not (isinstance(version, str) and _STABLE.match(version) and isinstance(files, list)):
            continue
        if not any(isinstance(item, dict) and not item.get("yanked") for item in files):
            continue
        key = _key(version)
        if best is None or key > best:
            best, best_text = key, version
    return best_text


def _key(version: str) -> tuple[int, ...]:
    """Return a comparison key with trailing zeros removed, so 1.0 equals 1.0.0."""
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _is_newer(candidate: str, pinned: str) -> bool:
    """Return whether *candidate* is a strictly newer stable version than *pinned*."""
    return bool(_STABLE.match(pinned)) and _key(candidate) > _key(pinned)
