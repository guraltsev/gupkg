"""Record the HTTP fixtures that the provider tests replay offline.

This is a maintainer tool for the development tree; it is never shipped and the tests never
run it. Each file ``tests/fixtures/http/<case>.json`` lists the URLs one registry package's
provider reads (the ``responses`` array, with ``url`` and ``method``). Running the tool asks
the live sites for each URL and rewrites the status, the headers that matter and the body in
place, sets ``recorded`` to today's date and ``origin`` to say so. A file still marked
``"recorded": null`` was written by hand from the layout the registry hook expected, and is
the first thing to re-record.

Usage and API
-------------
``python tools/record_http_fixtures.py`` records every case; ``--case NAME`` (repeatable)
records some; ``--dry-run`` prints what would change and writes nothing. ``record_case(path,
system)`` records one file through any ``System``, which is how the tool is tested without a
network.

Implementation Approach
-----------------------
Requests go through ``System.http_request`` without following redirects, so a recorded
redirect stays a redirect and the replay exercises the same hops. Only headers that a
provider reads are kept, and a body larger than 4 MiB is refused, so a fixture stays a small,
reviewable file. Nothing is written when any request of a case fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gupkg.system.port import System  # noqa: E402
from gupkg.system.windows import WindowsSystem  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "http"
KEPT_HEADERS = ("content-type", "location", "last-modified", "content-disposition", "content-length", "etag")
MAX_BODY = 4 * 1024 * 1024
USER_AGENT = "gupkg-fixture-recorder/1"


def record_case(path: Path, system: System, *, write: bool = True) -> list[str]:
    """Record one fixture file through *system* and return a line per URL describing the result.

    Raises
    ------
    OSError
        If a request fails; the file is left as it was.
    ValueError
        If a body is larger than 4 MiB or is not text.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    summary: list[str] = []
    for entry in document["responses"]:
        method = entry.get("method", "GET")
        response = system.http_request(
            entry["url"], {"User-Agent": USER_AGENT}, method=method, timeout=60, max_bytes=MAX_BODY
        )
        entry["status"] = response.status
        entry["headers"] = {name: value for name, value in response.headers.items() if name in KEPT_HEADERS}
        entry["body"] = response.body.decode("utf-8") if method != "HEAD" else ""
        summary.append(f"{method} {entry['url']} -> {response.status}, {len(response.body)} bytes")
    document["recorded"] = date.today().isoformat()
    document["origin"] = f"recorded from the live sites on {document['recorded']} by tools/record_http_fixtures.py"
    if write:
        path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    """Record the selected cases against the live sites and report each result."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--case", action="append", help="record only this case (a file name without .json); repeatable")
    parser.add_argument("--dry-run", action="store_true", help="request the URLs and print results, write nothing")
    arguments = parser.parse_args(argv)

    names = arguments.case or sorted(path.stem for path in FIXTURES.glob("*.json"))
    system = WindowsSystem()
    failed = 0
    for name in names:
        path = FIXTURES / f"{name}.json"
        try:
            for line in record_case(path, system, write=not arguments.dry_run):
                print(f"{name}: {line}")
        except (OSError, ValueError) as error:
            failed += 1
            print(f"{name}: FAILED: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
