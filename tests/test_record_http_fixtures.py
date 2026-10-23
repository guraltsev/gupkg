"""Cover the fixture recorder: it records the whole tracker walk and never half-writes a case.

The recorder runs the real tracker of a copy of a committed fixture against ``FakeSystem`` (no
network) acting as the "live" site. Covers what a re-recording changes (the visited responses,
kept headers, date, origin note and the `expected` block), that an index walk is recorded page by
page and pages it no longer visits are dropped, and that a failing run leaves the file as it was.
Out of scope: talking to the real sites, which is a manual step in ``tests/manual_smoke.md``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import record_http_fixtures

from gupkg.system.fake import FakeHttp, FakeSystem
from tests.http_fixtures import HTTP_CASES, load_case


def copy_case(tmp_path: Path, name: str) -> Path:
    """Copy a committed fixture case into *tmp_path* and return the copy."""
    target = tmp_path / f"{name}.json"
    shutil.copyfile(HTTP_CASES / f"{name}.json", target)
    return target


def test_recording_rewrites_the_responses_and_the_expected_block(system: FakeSystem, tmp_path: Path) -> None:
    """A new `Last-Modified` on the live site becomes the case's response, its version, and a recording date; secrets are not kept."""
    case = copy_case(tmp_path, "last_modified_autoruns")
    url = json.loads(case.read_text(encoding="utf-8"))["responses"][0]["url"]
    system.urls[url] = FakeHttp("", 200, {"Last-Modified": "Mon, 07 Oct 2024 10:00:00 GMT", "Set-Cookie": "secret=1", "Server": "x"})

    lines = record_http_fixtures.record_case(case, system)

    document = json.loads(case.read_text(encoding="utf-8"))
    assert document["responses"][0]["headers"] == {"last-modified": "Mon, 07 Oct 2024 10:00:00 GMT", "content-length": "0"}
    assert document["expected"]["version"] == "20241007"
    assert document["recorded"] and document["origin"].startswith("recorded from the live sites")
    assert lines == [f"HEAD {url} -> 200, 0 bytes"]
    assert system.request_log[0].headers["User-Agent"] == "gupkg-fixture-recorder/1"


def test_an_index_walk_is_recorded_page_by_page_and_unvisited_pages_are_dropped(system: FakeSystem, tmp_path: Path) -> None:
    """Re-recording pidgin records the root listing and the two folders the walk visits, and drops a stale extra URL."""
    case = copy_case(tmp_path, "index_pidgin")
    document = json.loads(case.read_text(encoding="utf-8"))
    document["responses"].append({"url": "https://example.org/stale", "method": "GET", "status": 200, "headers": {}, "body": "old"})
    case.write_text(json.dumps(document), encoding="utf-8")
    system.urls.update(load_case("index_pidgin"))

    record_http_fixtures.record_case(case, system)

    recorded = json.loads(case.read_text(encoding="utf-8"))
    urls = [entry["url"] for entry in recorded["responses"]]
    assert urls == [
        "https://sourceforge.net/projects/pidgin/files/Pidgin/",
        "https://sourceforge.net/projects/pidgin/files/Pidgin/2.14.14/",
        "https://sourceforge.net/projects/pidgin/files/Pidgin/2.14.13/",
    ]
    assert recorded["expected"]["version"] == "2.14.13"


def test_a_failing_run_leaves_the_case_file_untouched(system: FakeSystem, tmp_path: Path) -> None:
    """If a URL cannot be fetched, nothing is written: the old, working fixture stays."""
    case = copy_case(tmp_path, "last_modified_autoruns")
    before = case.read_bytes()
    url = json.loads(case.read_text(encoding="utf-8"))["responses"][0]["url"]
    system.urls[url] = ConnectionResetError("reset")

    with pytest.raises(Exception):
        record_http_fixtures.record_case(case, system)

    assert case.read_bytes() == before


def test_a_dry_run_requests_but_writes_nothing(system: FakeSystem, tmp_path: Path) -> None:
    """With `write=False` the requests are reported and the file is unchanged."""
    case = copy_case(tmp_path, "last_modified_autoruns")
    before = case.read_bytes()
    url = json.loads(case.read_text(encoding="utf-8"))["responses"][0]["url"]
    system.urls[url] = FakeHttp("", 200, {"Last-Modified": "Mon, 07 Oct 2024 10:00:00 GMT"})

    lines = record_http_fixtures.record_case(case, system, write=False)

    assert lines and case.read_bytes() == before
