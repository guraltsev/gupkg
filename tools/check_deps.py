"""Report which locked gupkg dependencies have a newer stable release on PyPI.

This is a maintainer tool for the development tree; it is never shipped and
gupkg never runs it. Dependency freshness is a maintainer concern: end users
get exactly the versions the release was built with.

Usage and API
-------------
``python tools/check_deps.py`` compares every distribution in
``tools/gupkg-deps.lock`` with the newest stable, non-yanked release PyPI lists
and prints one line per outdated pin. It exits ``0`` when everything is
current, ``1`` when something can be updated (run ``tools/update_deps.py``) and
``2`` when PyPI could not be asked. ``outdated_dependencies(...)`` is the same
comparison as a function.

Implementation Approach
-----------------------
Requests go over HTTPS to the PyPI JSON API with a short timeout. Every name
and version taken from a response must match a strict pattern before it is
printed, so response text cannot inject control sequences.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_deps import DEFAULT_LOCK, read_lock  # noqa: E402  (a sibling script)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_STABLE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    """Print the outdated pins of the lock and return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)
    try:
        outdated = outdated_dependencies(args.lock)
    except RuntimeError as error:
        print(f"[check-deps] {error}", file=sys.stderr)
        return 2
    for name, pinned, latest in outdated:
        print(f"{name} {pinned} is pinned; {latest} is available. Run: python tools/update_deps.py")
    if not outdated:
        print("[check-deps] Every locked dependency is current")
    return 1 if outdated else 0


def outdated_dependencies(
    lock: Path = DEFAULT_LOCK,
    *,
    timeout: float = 10.0,
    fetch: Callable[[str, float], dict] | None = None,
) -> list[tuple[str, str, str]]:
    """Return ``(name, pinned, newest)`` for every lock entry with a newer stable release.

    Parameters
    ----------
    lock : Path
        Lock file whose pins are compared.
    timeout : float, default=10.0
        Seconds allowed for one request.
    fetch : callable, optional
        Replacement for the network request: receives a distribution name and a
        timeout and returns the decoded PyPI JSON document. Used by tests.

    Raises
    ------
    RuntimeError
        If PyPI cannot be asked for a distribution.
    """
    ask = fetch or _fetch_pypi
    outdated = []
    for item in sorted(read_lock(lock), key=lambda entry: entry.name.casefold()):
        # Any failure to ask is reported, so an offline check is never mistaken for "current".
        try:
            newest = _newest_stable(ask(item.name, timeout))
        except Exception as error:
            raise RuntimeError(f"Could not ask PyPI about {item.name}: {error}") from error
        if newest is not None and _STABLE.match(item.version) and _key(newest) > _key(item.version):
            outdated.append((item.name, item.version, newest))
    return outdated


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


if __name__ == "__main__":
    raise SystemExit(main())
