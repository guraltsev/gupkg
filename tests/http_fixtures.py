"""Load recorded HTTP cases and registry-2 manifests, and build the service environment, for tests.

``tests/fixtures/http/<case>.json`` holds the responses of one registry package's provider
(see ``tools/record_http_fixtures.py``). ``tests/fixtures/registry2/<package>/_check_updates``
holds the format 2 tracker that expresses that package with no hook. The helpers here turn
them into a ``FakeSystem`` that serves the recorded answers, a shared ``HttpClient`` that
never sleeps and never reads the ambient environment, and a parsed tracker manifest.
"""

from __future__ import annotations

from pathlib import Path

import json

from gupkg.model.manifest import Manifest, ManifestKind, parse_manifest
from gupkg.services.http import HttpClient
from gupkg.services.track import TrackEnvironment
from gupkg.system.fake import FakeHttp, FakeSystem

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HTTP_CASES = FIXTURES / "http"
REGISTRY2 = FIXTURES / "registry2"


def load_case(name: str) -> dict[str, FakeHttp]:
    """Return ``{url: FakeHttp}`` for the recorded case *name*."""
    document = json.loads((HTTP_CASES / f"{name}.json").read_text(encoding="utf-8"))
    return {
        entry["url"]: FakeHttp(entry.get("body", ""), entry.get("status", 200), entry.get("headers", {}))
        for entry in document["responses"]
    }


def serve(system: FakeSystem, *cases: str) -> FakeSystem:
    """Make *system* serve every response of the recorded *cases*; return it."""
    for case in cases:
        system.urls.update(load_case(case))
    return system


def environment(system: FakeSystem, tmp_path: Path, *, directory: Path | None = None, package: str = "pkg") -> TrackEnvironment:
    """Return a ``TrackEnvironment`` over *system* with a client that never sleeps and ignores the process environment."""
    client = HttpClient(system, env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache")
    return TrackEnvironment(system, client, work_dir=tmp_path / "work", directory=directory, package=package)


def tracker_manifest(package: str) -> Manifest:
    """Parse the committed tracker ``registry2/<package>/_check_updates/pkg.toml``."""
    text = (REGISTRY2 / package / "_check_updates" / "pkg.toml").read_text(encoding="utf-8")
    return parse_manifest(text, ManifestKind.TRACKER)
