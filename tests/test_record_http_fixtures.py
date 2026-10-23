"""Cover the fixture recorder: it rewrites a case file from a ``System`` and never half-writes one.

The recorder runs against ``FakeSystem`` (no network) on a copy of a committed fixture. Covers
what a re-recording changes (status, kept headers, body, date, origin note), that unwanted
headers are dropped, and that a failing request leaves the file exactly as it was. Out of
scope: talking to the real sites, which is the manual step in ``tests/manual_smoke.md``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import record_http_fixtures

from gupkg.system.fake import FakeHttp, FakeSystem
from tests.http_fixtures import HTTP_CASES


def copy_case(tmp_path: Path, name: str = "last_modified_autoruns") -> Path:
    """Copy a committed fixture case into *tmp_path* and return the copy."""
    target = tmp_path / f"{name}.json"
    shutil.copyfile(HTTP_CASES / f"{name}.json", target)
    return target


def test_recording_rewrites_the_responses_and_marks_the_case_as_recorded(system: FakeSystem, tmp_path: Path) -> None:
    """Status, the kept headers and the body replace the old ones; the case now says when and how it was recorded."""
    case = copy_case(tmp_path)
    url = json.loads(case.read_text(encoding="utf-8"))["responses"][0]["url"]
    system.urls[url] = FakeHttp("", 200, {"Last-Modified": "Mon, 07 Oct 2024 10:00:00 GMT", "Set-Cookie": "secret=1", "Server": "x"})

    lines = record_http_fixtures.record_case(case, system)

    entry = json.loads(case.read_text(encoding="utf-8"))
    assert entry["responses"][0]["headers"] == {"last-modified": "Mon, 07 Oct 2024 10:00:00 GMT", "content-length": "0"}
    assert entry["recorded"] and "tools/record_http_fixtures.py" in entry["origin"]
    assert lines == [f"HEAD {url} -> 200, 0 bytes"]
    assert system.request_log[0].headers["User-Agent"] == "gupkg-fixture-recorder/1"


def test_a_failing_request_leaves_the_case_file_untouched(system: FakeSystem, tmp_path: Path) -> None:
    """If a URL cannot be fetched, nothing is written: the old, working fixture stays."""
    case = copy_case(tmp_path)
    before = case.read_bytes()
    url = json.loads(case.read_text(encoding="utf-8"))["responses"][0]["url"]
    system.urls[url] = ConnectionResetError("reset")

    with pytest.raises(OSError):
        record_http_fixtures.record_case(case, system)

    assert case.read_bytes() == before


def test_a_dry_run_requests_but_writes_nothing(system: FakeSystem, tmp_path: Path) -> None:
    """With `write=False` the results are reported and the file is unchanged."""
    case = copy_case(tmp_path)
    before = case.read_bytes()
    url = json.loads(case.read_text(encoding="utf-8"))["responses"][0]["url"]
    system.urls[url] = FakeHttp("", 200, {"Last-Modified": "Mon, 07 Oct 2024 10:00:00 GMT"})

    lines = record_http_fixtures.record_case(case, system, write=False)

    assert lines and case.read_bytes() == before
