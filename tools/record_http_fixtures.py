"""Record the HTTP fixtures that the provider tests replay offline.

This is a maintainer tool for the development tree; it is never shipped and the tests never
run it. Each file ``tests/fixtures/http/<case>.json`` names a ``tracker`` (a folder of
``tests/fixtures/registry_100``), holds the ``responses`` that tracker's provider reads, and the
``expected`` result the replay test asserts. Recording runs the real tracker against the live
sites through a ``System`` that logs every request and answer, so the **whole walk** is
recorded: an index walk or chain that now visits other pages records those pages, and pages the
walk no longer visits disappear from the file. The case's ``recorded`` date, ``origin`` note and
``expected`` block (version, download address, discovered checksum) are rewritten from what the
run found. A file still marked ``"recorded": null`` is a synthetic fixture modelled on the
registry hooks' regular expressions and is the first thing to re-record.

Usage and API
-------------
``python tools/record_http_fixtures.py`` records every case; ``--case NAME`` (repeatable)
records some; ``--dry-run`` prints what would change and writes nothing. ``record_case(path,
system)`` records one file through any ``System``, which is how the tool is tested without a
network. Review the diff before committing: a changed ``expected`` block is a vendor change.

Implementation Approach
-----------------------
The live ``System`` is wrapped so ``http_request`` is logged; redirects are not followed by the
transport, so each hop is a logged request and the replay exercises the same hops. Only headers
that a provider reads are kept, and a body larger than 4 MiB is refused, so a fixture stays a
small, reviewable file. Nothing is written when the run fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gupkg.model.manifest import ManifestKind, parse_manifest, render_pinned  # noqa: E402
from gupkg.model.scope import make_scope  # noqa: E402
from gupkg.model.settings import LAYER_APP, Layer, default_layer, merge_layers  # noqa: E402
from gupkg.scope import process_mode  # noqa: E402
from gupkg.services.http import HttpClient  # noqa: E402
from gupkg.services.track import TrackEnvironment, run_tracker  # noqa: E402
from gupkg.system.port import HttpResponse, System  # noqa: E402
from gupkg.system.windows import WindowsSystem  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "http"
TRACKERS = ROOT / "tests" / "fixtures" / "registry_100"
KEPT_HEADERS = ("content-type", "location", "last-modified", "content-disposition", "content-length", "etag")
MAX_BODY = 4 * 1024 * 1024
USER_AGENT = "gupkg-fixture-recorder/1"


class _Logging:
    """Wrap a ``System`` so every ``http_request`` is logged; everything else is passed through."""

    def __init__(self, live: System) -> None:
        """Remember the live system and start an empty log."""
        self._live = live
        self.log: list[tuple[str, str, HttpResponse]] = []

    def __getattr__(self, name: str) -> Any:
        """Delegate any other attribute to the live system."""
        return getattr(self._live, name)

    def http_request(self, url: str, headers=None, *, method: str = "GET", **options: Any) -> HttpResponse:
        """Make the request on the live system and log the answer."""
        response = self._live.http_request(url, headers, method=method, **options)
        self.log.append((method, url, response))
        return response


def record_case(path: Path, system: System, *, write: bool = True, trackers: Path = TRACKERS) -> list[str]:
    """Record one fixture file by running its tracker through *system*; return a line per request.

    Raises
    ------
    OSError
        If a request fails; the file is left as it was.
    ValueError
        If a body is larger than 4 MiB or is not UTF-8 text.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    tracker_dir = trackers / document["tracker"] / "_check_updates"
    manifest = parse_manifest((tracker_dir / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)
    logging = _Logging(system)
    with tempfile.TemporaryDirectory(prefix="gupkg-record-") as work:
        client = HttpClient(logging, user_agent=USER_AGENT, env={}, sleep=time.sleep)
        mode = process_mode(logging)
        scope = make_scope(
            mode, Path(work), "record_http_fixtures",
            merge_layers([default_layer(mode.value), Layer(LAYER_APP, {"schema": 4, "integration": {"integrate": "none"}})]), {}, system=logging,
        )
        environment = TrackEnvironment(logging, scope, client, work_dir=Path(work), directory=tracker_dir, package=document["tracker"])
        result = run_tracker(manifest, environment)
    pinned = render_pinned(manifest, result)

    # Rewrite the responses from the walk, one entry per (method, url), the last answer winning.
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    summary: list[str] = []
    for method, url, response in logging.log:
        if len(response.body) > MAX_BODY:
            raise ValueError(f"{url} is larger than {MAX_BODY} bytes")
        entries[(method, url)] = {
            "url": url, "method": method, "status": response.status,
            "headers": {name: value for name, value in response.headers.items() if name in KEPT_HEADERS},
            "body": response.body.decode("utf-8") if method != "HEAD" else "",
        }
        summary.append(f"{method} {url} -> {response.status}, {len(response.body)} bytes")
    today = date.today().isoformat()
    document["responses"] = list(entries.values())
    document["recorded"] = today
    document["origin"] = f"recorded from the live sites on {today} by tools/record_http_fixtures.py"
    document["expected"] = {
        "version": result.version, "download_url": pinned.source.download_url,
        "sha256": pinned.source.sha256, "sha256_from": pinned.source.sha256_from,
    }
    if write:
        path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8", newline="\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    """Record the selected cases against the live sites and report each result."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--case", action="append", help="record only this case (a file name without .json); repeatable")
    parser.add_argument("--dry-run", action="store_true", help="run the trackers and print the requests, write nothing")
    arguments = parser.parse_args(argv)

    names = arguments.case or sorted(path.stem for path in FIXTURES.glob("*.json"))
    system = WindowsSystem()
    failed = 0
    for name in names:
        path = FIXTURES / f"{name}.json"
        try:
            for line in record_case(path, system, write=not arguments.dry_run):
                print(f"{name}: {line}")
        except Exception as error:  # noqa: BLE001 - one failing case must not stop the others
            failed += 1
            print(f"{name}: FAILED: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
