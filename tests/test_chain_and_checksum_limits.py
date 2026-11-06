"""Cover the limits and the honesty of the new tracker features: chain budget, one ``each``, optional steps and sources.

Behavior covered: a chain may have only one ``each`` step and a hostile page with a hundred links cannot make it
fetch more than 60 requests (the budget stops it with its own error, which no ``optional`` or ``each`` skips); an
``optional`` step whose pattern runs out of time raises instead of being taken for "absent"; an ``optional``
checksum source that holds nothing (not listed, not found, fetch failed) lets the version through, says which
absence it was in a warning and in the result's notes, but a malformed digest line is an error; a rendered pinned
version carries no ``[source.checksum]`` and the package checker rejects a version directory that has one; and the
registry tool prints the wait-for-review notes and the ``[UNVERSIONED]`` marker.
Real: the track services, the regex worker process, the manifest and checker, the registry tool, real files.
Mocked: ``FakeSystem`` serves synthetic pages. Out of scope: the live vendors (manual).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, manifest_to_toml, parse_manifest, render_pinned
from gupkg.pkgcheck import check_directory
from gupkg.report.events import Warning, use_reporter
from gupkg.services.checksum import ChecksumDiscoveryError
from gupkg.services.track import TrackError, run_tracker
from gupkg.services.track.page import MAX_CHAIN_REQUESTS, ChainBudgetExceeded
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg_registry.layout import write_registry_file
from tests.support.registry_tool import ANSWERS, SUMS, TRACKERS, Git, machine, registry_of, run_cli, zip_bytes
from tests.support.track import environment, parse_tracker, real_regex_worker

pytestmark = pytest.mark.process

# Top-level keys that every tracker of this file starts with.
HEADER = 'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n'


class Collect:
    """A reporter that keeps every event."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def handle(self, event: object) -> None:
        """Keep one event."""
        self.events.append(event)


def run(text: str, system: FakeSystem, tmp_path: Path):
    """Parse the tracker *text* and run it."""
    return run_tracker(parse_manifest(text, ManifestKind.TRACKER), environment(system, tmp_path))


# -- the chain: one each, a request budget -------------------------------------------------------------------

# A chain that tries each link of the page, so the request budget matters.
BUDGET_CHAIN = (
    HEADER + '[track]\nprovider = "chain"\ndownload = "${link}"\n'
    '[[track.fetch]]\nurl = "https://example.org/"\nselect = "each"\nmatch = \'(?P<link>https://example.org/f/\\d+)\'\n'
    '[[track.fetch]]\nurl = "${link}?a"\nmatch = \'(?P<one>ok)\'\n'
    '[[track.fetch]]\nurl = "${link}?b"\nmatch = \'(?P<two>ok)\'\n'
    '[[track.fetch]]\nurl = "${link}?c"\nmatch = \'(?P<version>never)\'\n'
    '[source]\ndownload_url = "${download_url}"\n'
)


def test_a_hostile_page_with_a_hundred_links_cannot_exceed_the_request_budget(tmp_path: Path) -> None:
    """With three later steps per candidate the walk would make 61 requests at the candidate cap; the budget stops it at 60."""
    system = FakeSystem(tmp_path / "machine")
    links = [f"https://example.org/f/{n}" for n in range(100)]
    system.urls["https://example.org/"] = FakeHttp(" ".join(links))
    for link in links:
        system.urls[f"{link}?a"] = FakeHttp("ok")
        system.urls[f"{link}?b"] = FakeHttp("ok")
        system.urls[f"{link}?c"] = FakeHttp("not a version")

    with pytest.raises(ChainBudgetExceeded, match=f"more than {MAX_CHAIN_REQUESTS} requests"):
        run(BUDGET_CHAIN, system, tmp_path)

    assert len(system.request_log) == MAX_CHAIN_REQUESTS == 60


def test_the_budget_error_is_not_skipped_by_an_optional_step(tmp_path: Path) -> None:
    """An optional step after the budget is spent does not hide the stop: the error still reaches the caller."""
    text = BUDGET_CHAIN.replace("(?P<version>never)'\n", "(?P<version>never)'\noptional = true\n")
    system = FakeSystem(tmp_path / "machine")
    links = [f"https://example.org/f/{n}" for n in range(100)]
    system.urls["https://example.org/"] = FakeHttp(" ".join(links))
    for link in links:
        for suffix in "abc":
            system.urls[f"{link}?{suffix}"] = FakeHttp("ok")

    with pytest.raises(TrackError):
        run(text, system, tmp_path)

    assert len(system.request_log) <= MAX_CHAIN_REQUESTS


def test_a_chain_may_try_the_candidates_of_one_step_only() -> None:
    """A second ``select = "each"`` is refused when the manifest is read, so candidates cannot multiply."""
    text = (
        HEADER + '[track]\nprovider = "chain"\ndownload = "${link}"\n'
        '[[track.fetch]]\nurl = "https://example.org/"\nselect = "each"\nmatch = \'(?P<link>https://example.org/f/\\d+)\'\n'
        '[[track.fetch]]\nurl = "${link}"\nselect = "each"\nmatch = \'(?P<other>x)\'\n'
        '[[track.fetch]]\nurl = "${link}"\nmatch = \'(?P<version>\\d+)\'\n[source]\ndownload_url = "${download_url}"\n'
    )

    with pytest.raises(Exception, match="second step"):
        parse_manifest(text, ManifestKind.TRACKER)


def test_an_optional_step_whose_pattern_runs_out_of_time_stops_the_check(tmp_path: Path) -> None:
    """A regex deadline in an optional step is an error, never taken for "this step has nothing"."""
    # Arrange: an optional step that finds a second value in the next page.
    text = (
        HEADER + '[track]\nprovider = "chain"\n'
        '[[track.fetch]]\nurl = "https://example.org/"\nmatch = \'v(?P<version>\\d+)\'\n'
        '[[track.fetch]]\nurl = "https://example.org/next"\noptional = true\nmatch = \'(?P<extra>x+)\'\n'
        '[source]\ndownload_url = "https://example.org/${version}.zip"\n'
    )
    # Serve both pages.
    system = FakeSystem(tmp_path / "machine")
    system.urls["https://example.org/"] = FakeHttp("v7")
    system.urls["https://example.org/next"] = FakeHttp("xxx")
    calls: list[int] = []

    def worker(call: ProcessCall) -> ProcessResult:
        """Play the regex worker: the first pattern runs for real and the second one runs out of time."""
        calls.append(1)
        if len(calls) >= 2:
            raise TimeoutError("deadline")
        return real_regex_worker(call)

    # Install the double.
    system.processes["regex-worker"] = worker

    # Act and assert: the timeout stops the check.
    with pytest.raises(TrackError, match="was stopped"):
        run(text, system, tmp_path)


# -- the optional checksum source ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        pytest.param(FakeHttp("", 404), "not found", id="404"),
        pytest.param(FakeHttp("", 500), "fetch failed", id="500"),
        pytest.param(FakeHttp(f"{'a' * 64}  editor-30.2-other.zip\n"), "not listed", id="archive-not-listed"),
    ],
)
def test_an_optional_checksum_that_holds_nothing_is_waived_and_says_how(tmp_path: Path, answer: FakeHttp, kind: str) -> None:
    """The version is found without a checksum; the kind of absence is in a warning and in the result's notes."""
    system = FakeSystem(tmp_path / "machine")
    system.urls.update(ANSWERS["index_editor"])
    system.urls[SUMS] = answer
    reporter = Collect()

    with use_reporter(reporter):
        result = run_tracker(parse_tracker(TRACKERS["editor"]), environment(system, tmp_path, package="editor"))

    assert result.version == "30.2" and result.checksum is None
    assert len(result.notes) == 1 and f"({kind})" in result.notes[0]
    assert any(isinstance(event, Warning) and f"({kind})" in event.text for event in reporter.events)


def test_a_fetch_failure_without_a_status_is_reported_as_a_failed_fetch(tmp_path: Path) -> None:
    """A source that cannot be reached at all (the fake has no answer for the address) is waived as ``fetch failed``."""
    system = FakeSystem(tmp_path / "machine")
    system.urls.update(ANSWERS["index_editor"])
    del system.urls[SUMS]

    result = run_tracker(parse_tracker(TRACKERS["editor"]), environment(system, tmp_path, package="editor"))

    assert result.checksum is None and "(fetch failed)" in result.notes[0]


@pytest.mark.parametrize("line", ["not-a-digest  editor-30.2-win.zip", f"{'z' * 64}  editor-30.2-win.zip", f"{'a' * 63}  editor-30.2-win.zip"])
def test_a_malformed_digest_line_is_an_error_even_when_the_source_is_optional(tmp_path: Path, line: str) -> None:
    """Optional means the source may be absent, not that garbage is accepted: the check stops and names the entry."""
    system = FakeSystem(tmp_path / "machine")
    system.urls.update(ANSWERS["index_editor"])
    system.urls[SUMS] = FakeHttp(line + "\n")

    with pytest.raises(ChecksumDiscoveryError, match="not a SHA-256"):
        run_tracker(parse_tracker(TRACKERS["editor"]), environment(system, tmp_path, package="editor"))


# -- no tracker keys in a version -----------------------------------------------------------------------------------------


def test_a_rendered_version_has_no_checksum_table_and_the_checker_refuses_one_that_has(tmp_path: Path) -> None:
    """The pinned manifest drops the tracker-only ``[source.checksum]``; a version directory that carries one is an error."""
    # Arrange: the editor tracker and the vendor answers it reads.
    system = FakeSystem(tmp_path / "machine")
    system.urls.update(ANSWERS["index_editor"])
    manifest = parse_tracker(TRACKERS["editor"])
    result = run_tracker(manifest, environment(system, tmp_path, package="editor"))

    # Act: render the pinned version.
    pinned = render_pinned(manifest, result)
    text = manifest_to_toml(pinned)

    # Assert: the pinned manifest has no checksum table, and the checker rejects a version directory that has one.
    assert "checksum" not in tomllib.loads(text)["source"] and pinned.source.sha256 is not None
    version = tmp_path / "editor" / "30.2"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(text, encoding="utf-8", newline="\n")
    assert check_directory(version).ok

    # Add the tracker-only table to the version and check it again.
    (version / "pkg.toml").write_text(
        text.replace("[source]\n", '[source]\n', 1) + '\n[source.checksum]\nsums_file = "https://e.org/s"\nentry = "x"\n',
        encoding="utf-8", newline="\n",
    )
    assert any("belongs to a tracker" in error for error in check_directory(version).errors)


# -- the registry tool's output --------------------------------------------------------------------------------------------


def test_update_prints_the_unversioned_marker_and_the_commit_message_names_the_package(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """For a package listed as unversioned the output says so and the auto-push commit message has its own section."""
    git = Git()
    root = registry_of(tmp_path, "stamped")
    write_registry_file(root, {"format": 2, "name": "t", "description": "t", "unversioned": ["stamped"]})
    body = zip_bytes(tmp_path, "a.zip", {"Stamped64.exe": "a", "stamped.chm": "c"})
    system = machine(tmp_path, "last_modified_stamped", git=git)
    url = "https://download.example/files/Stamped.zip"
    system.urls[url] = FakeHttp(body, 200, ANSWERS["last_modified_stamped"][url].headers)  # the same address answers the date and the file

    status = run_cli(root, system, tmp_path, "update", "stamped", "--auto-push", "--allow-computed")

    assert status == 0
    assert "[UNVERSIONED]" in capsys.readouterr().out
    message = next(call.argv[3] for call in git.calls if call.argv[1] == "commit")
    assert "has no version in it" in message and "- stamped " in message


def test_a_waived_checksum_note_reaches_the_registry_tool_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When an optional checksum source held nothing, the tool's line for the version says which absence it was."""
    # Arrange: a registry with a tracker, a sums file that is missing and the artifact.
    root = registry_of(tmp_path, "editor")
    write_registry_file(root, {"format": 2, "name": "t", "description": "t"})
    system = FakeSystem(tmp_path / "machine")
    system.urls.update(ANSWERS["index_editor"])
    system.urls[SUMS] = FakeHttp("", 404)
    system.urls["https://ftp.example/editor/editor-30/editor-30.2-win.zip"] = FakeHttp(
        zip_bytes(tmp_path, "e.zip", {"bin/editorw.exe": "r", "bin/editor.exe": "e"})
    )

    # Act: update the registry with the command-line tool.
    status = run_cli(root, system, tmp_path, "update", "editor")

    # Assert: the output says the checksum was not found and needs review.
    output = capsys.readouterr().out
    assert status == 0 and "(not found)" in output and "[REVIEW]" in output
