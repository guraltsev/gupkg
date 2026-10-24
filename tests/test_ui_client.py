"""Cover the one module through which the UI programs run ``gupkg``: which command it runs, what it refuses, and how it distrusts the result.

Behaviour covered: the command is the ``gupkg.cmd`` of the program's own gupkg tree, never one found on ``PATH`` and never one that resolves outside the tree; an argument with a
character that could be misread (``& | < > ^ " % !``, a control character) is refused before anything starts; options go before ``--`` and targets after it; a result is believed only
when it is one JSON object with ``"api": 1``, an exit code that matches the process and (when an events file was written) a ``run_end``; a relaunched call needs the child's
``run_end``; output beyond the cap and a program that cannot start are failures; the stop request creates the announced marker only inside the program's temporary folder, also when the
marker is announced after the request was made.

Real: the client, real subprocesses (the double of ``tests/ui_support.py`` run by the test interpreter) and real files. Mocked: the ``gupkg`` executable itself.
Out of scope: what batches do with results (``test_ui_flows.py``) and the real launcher (``test_manager_cli.py``).
"""

from __future__ import annotations

import asyncio
import ctypes
from pathlib import Path

import pytest

from gupkg.selfmgmt.location import RunningGupkg
from gupkg_ui import client as client_module
from gupkg_ui.follow import EventStream
from gupkg_ui.client import Client, ClientError, StopRequest, check_document, refuse_unsafe, sibling_launcher, with_options
from tests.ui_support import FakeGupkg, entry, head, result, until


@pytest.fixture
def fake(tmp_path: Path) -> FakeGupkg:
    """A scripted ``gupkg`` double with its own folders under the test's temporary folder."""
    return FakeGupkg(tmp_path)


def call(client: Client, args, logs: Path | None, **options):
    """Run ``client.call`` to the end."""
    return asyncio.run(client.call(args, events_dir=logs, **options))


def one_package(tmp_path: Path, **extra) -> dict:
    """Return the ``events`` part of a double's response for one package ``p`` in a root under *tmp_path*."""
    return {"dir": str(tmp_path / "root" / ".gupkg" / "logs"), "packages": [{"name": "p", "path": str(tmp_path / "root" / "p"), "records": []}], **extra}


# -- which command ------------------------------------------------------------------


def test_the_launcher_is_the_one_in_the_programs_own_tree(tmp_path: Path) -> None:
    """In a source checkout the real launcher beside the sources is used, found without looking at ``PATH``."""
    package = tmp_path / "tree" / "gupkg"
    package.mkdir(parents=True)
    (package / "gupkg.cmd").write_text("@echo off\n", encoding="utf-8")

    assert sibling_launcher(RunningGupkg("source", package, (package,))) == (package / "gupkg.cmd").resolve()


def test_a_gupkg_on_path_is_never_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree without a launcher is an error even when ``PATH`` holds a ``gupkg.cmd``: the programs run only their own gupkg."""
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    (elsewhere / "gupkg.cmd").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(elsewhere))
    package = tmp_path / "tree" / "gupkg"
    package.mkdir(parents=True)

    with pytest.raises(ClientError, match="not found in this gupkg tree"):
        sibling_launcher(RunningGupkg("source", package, (package,)))


def test_a_launcher_that_resolves_outside_the_tree_is_refused(tmp_path: Path) -> None:
    """A launcher folder that leads out of the tree (a planted link) is refused, not run."""
    package = tmp_path / "tree" / "App" / "gupkg"
    package.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "gupkg.cmd").write_text("@echo evil\n", encoding="utf-8")
    where = RunningGupkg("installed", package, (outside,), tmp_path / "tree", tmp_path, tmp_path / "tree" / "App")

    with pytest.raises(ClientError, match="outside this gupkg tree"):
        sibling_launcher(where)


# -- what is refused ----------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a&b", "a|b", "a<b", "a>b", "a^b", 'a"b', "a%b", "a!b", "a`b", "a\nb", "a\x00b", "a‮b"])
def test_an_argument_that_a_launcher_could_misread_is_refused(bad: str) -> None:
    """The ``.cmd`` launcher is not a safe place for these characters, so the client never passes them (the CLI refuses them for the elevated child too)."""
    with pytest.raises(ClientError, match="cannot be passed to gupkg safely"):
        refuse_unsafe(["fine", bad])


def test_a_refused_argument_starts_nothing(fake: FakeGupkg, tmp_path: Path) -> None:
    """A hostile target raises before any process is started."""
    fake.respond("update", doc=head("update"))

    with pytest.raises(ClientError):
        call(fake.client(), ("update", "--", "user:evil&calc"), None)

    assert fake.calls() == []


def test_options_go_before_the_double_dash_and_targets_after_it() -> None:
    """A target such as ``--yes`` can never be read as an option because everything after ``--`` is a target."""
    assert with_options(["update", "--yes", "--", "--yes", "x"], ["--json"]) == ["update", "--yes", "--json", "--", "--yes", "x"]
    assert with_options(["list"], ["--json"]) == ["list", "--json"]


# -- what is believed ---------------------------------------------------------------


def test_a_good_call_is_ok_and_its_entries_come_from_the_document(fake: FakeGupkg, tmp_path: Path) -> None:
    """A document with ``api`` 1, the matching exit code and an events file that ends with ``run_end`` is trusted."""
    doc = result("update", [entry("p")])
    fake.respond("update", doc=doc, events=one_package(tmp_path, result=doc))

    outcome = call(fake.client(), ("update", "--", "user:p"), tmp_path / "root" / ".gupkg" / "logs")

    assert outcome.ok and outcome.problems == () and [item["package"] for item in outcome.entries()] == ["p"]
    assert outcome.argv[-4:-1] == ("--events-file", outcome.events_path.name, "--")


def test_a_result_that_is_not_json_is_a_failure(fake: FakeGupkg) -> None:
    """Text on standard output is never read as success."""
    fake.respond("update", raw="all done!\n", exit=0)

    outcome = call(fake.client(), ("update", "--", "user:p"), None)

    assert not outcome.ok and "no readable result" in outcome.problems[0] and outcome.document is None


@pytest.mark.parametrize(
    ("document", "message"),
    [
        pytest.param({"api": 2, "command": "x", "ok": True, "exit_code": 0}, "api 2", id="newer-api"),
        pytest.param({"command": "x", "ok": True, "exit_code": 0}, "api None", id="no-api"),
        pytest.param({"api": 1, "command": "x", "ok": "yes", "exit_code": 0}, "lacks command, ok or exit_code", id="ok-not-boolean"),
        pytest.param({"api": 1, "command": "x", "ok": True, "exit_code": True}, "lacks command, ok or exit_code", id="exit-code-boolean"),
    ],
)
def test_a_document_of_another_interface_version_or_shape_is_refused(document, message) -> None:
    """The ``api`` field is checked and a mismatch refuses the document; so does a head with the wrong types."""
    with pytest.raises(ClientError, match=message):
        check_document(document)
    with pytest.raises(ClientError, match="not a JSON object"):
        check_document([1])


def test_an_exit_code_that_disagrees_with_the_document_is_a_failure(fake: FakeGupkg) -> None:
    """A document that says exit 0 from a process that exited 4 is not believed."""
    fake.respond("update", doc=head("update"), exit=4)

    outcome = call(fake.client(), ("update", "--", "user:p"), None)

    assert not outcome.ok and "disagrees" in outcome.problems[0]


def test_an_events_file_without_run_end_is_a_failure(fake: FakeGupkg, tmp_path: Path) -> None:
    """A run whose events file stops without ``run_end`` did not finish, whatever its standard output says."""
    fake.respond("update", doc=result("update", [entry("p")]), events=one_package(tmp_path, run_end=False))

    outcome = call(fake.client(), ("update", "--", "user:p"), tmp_path / "root" / ".gupkg" / "logs")

    assert not outcome.ok and "no run_end" in outcome.problems[0]


def test_a_relaunched_call_takes_its_entries_from_the_childs_run_end(fake: FakeGupkg, tmp_path: Path) -> None:
    """The parent of an elevated child lists no results of its own; the child's document (the ``run_end`` of its events file) holds them."""
    child = result("update", [entry("sys", mode="system")])
    parent = result("update", [], relaunched=True)
    fake.respond("update", doc=parent, events=one_package(tmp_path, result=child, elevated=True))

    outcome = call(fake.client(), ("update", "--", "system:sys"), tmp_path / "root" / ".gupkg" / "logs")

    assert outcome.ok and outcome.relaunched and [item["package"] for item in outcome.entries()] == ["sys"]


def test_a_relaunched_call_without_the_childs_run_end_is_a_failure(fake: FakeGupkg, tmp_path: Path) -> None:
    """If the elevated child left no ``run_end`` the call is failed (a killed child must not look like success)."""
    fake.respond("update", doc=result("update", [], relaunched=True), events=one_package(tmp_path, run_end=False))

    outcome = call(fake.client(), ("update", "--", "system:sys"), tmp_path / "root" / ".gupkg" / "logs")

    assert not outcome.ok and "elevated run left no run_end" in outcome.problems[0]


def test_output_beyond_the_cap_is_a_failure(fake: FakeGupkg, monkeypatch: pytest.MonkeyPatch) -> None:
    """A result larger than the cap is not read (bounded memory), and says so."""
    monkeypatch.setattr(client_module, "MAX_DOCUMENT_BYTES", 100)
    fake.respond("update", raw="[" + "1," * 200 + "1]", exit=0)

    outcome = call(fake.client(), ("update", "--", "user:p"), None)

    assert not outcome.ok and "larger than the limit" in outcome.problems[0]


def test_a_program_that_cannot_start_is_a_failure_not_an_exception(tmp_path: Path) -> None:
    """A missing launcher gives a failed result the caller can show."""
    missing = Client((str(tmp_path / "nope.exe"),))

    outcome = asyncio.run(missing.call(("update", "--", "user:p"), events_dir=None))

    assert not outcome.ok and "could not be started" in outcome.problems[0] and outcome.exit_code is None


def test_query_returns_the_document_even_for_a_nonzero_exit(fake: FakeGupkg) -> None:
    """A plan with a failed target exits 2 but still has a document: the caller reads it."""
    fake.respond("update:plan", doc=head("update", ok=False, code=2, plan=True, plans=[]))

    document = asyncio.run(fake.client().query(("update", "--plan", "--", "user:p")))

    assert document["exit_code"] == 2 and fake.calls()[0]["options"][-2:] == ["--json", "--no-input"]


def test_root_options_travel_before_the_command(fake: FakeGupkg) -> None:
    """``--settings`` and ``--sandbox`` are relayed to every call, first on the command line."""
    fake.respond("list", doc=head("list", packages=[]))

    asyncio.run(fake.client("--settings", "S.toml", "--sandbox", "SB").query(["list"]))

    assert fake.calls()[0]["argv"][:4] == ["--settings", "S.toml", "--sandbox", "SB"]


# -- stopping -----------------------------------------------------------------------


def stopped_run(fake: FakeGupkg, tmp_path: Path) -> dict:
    """Schedule a double that waits for its stop marker after its first of two packages."""
    doc = result("update", [entry("a"), entry("b", status="skipped")])
    events = {
        "dir": str(tmp_path / "root" / ".gupkg" / "logs"), "stop_after": 0, "result": doc,
        "packages": [{"name": name, "path": str(tmp_path / "root" / name), "records": []} for name in ("a", "b")],
    }
    fake.respond("update", doc=doc, events=events)
    return doc


def test_a_stop_request_creates_the_announced_marker_and_the_run_stops(fake: FakeGupkg, tmp_path: Path) -> None:
    """The request made while the run waits creates ``gupkg-stop-<hex>`` in the program's temporary folder; the double sees it and stops after package one."""
    stopped_run(fake, tmp_path)
    stop = StopRequest(fake.temp)

    async def scenario():
        """Start the call, ask to stop once the first package is in, and return the result."""
        seen: list[dict] = []
        task = asyncio.ensure_future(fake.client().call(("update", "--", "user:a", "user:b"), events_dir=tmp_path / "root" / ".gupkg" / "logs", on_records=seen.extend, stop=stop))
        await until(lambda: any(item["event"] == "package_end" for item in seen), "package one to end")
        message = stop.request()
        return message, await task

    message, outcome = asyncio.run(scenario())

    assert "stop request is sent" in message and outcome.ok
    assert fake.stop_events()[0]["saw_stop"] is True
    assert [item["status"] for item in outcome.entries()] == ["updated", "skipped"]


def test_the_same_temporary_folder_in_its_short_and_long_spelling_is_accepted(tmp_path: Path) -> None:
    """The marker is compared by folder identity, not by text: a TEMP given in its 8.3 short form still allows the CLI's marker (named by the long form)."""
    long_folder = tmp_path / "A Temporary Folder With A Long Name"
    long_folder.mkdir()
    buffer = ctypes.create_unicode_buffer(500)
    ctypes.windll.kernel32.GetShortPathNameW(str(long_folder), buffer, 500)
    assert buffer.value and buffer.value != str(long_folder), "this machine has no short names to compare"
    stream = EventStream(tmp_path / "x.jsonl")
    stream.stop_file = str(long_folder / ("gupkg-stop-" + "a" * 32))
    stop = StopRequest(Path(buffer.value))
    stop.stream = stream

    assert "stop request is sent" in stop.request() and Path(stream.stop_file).exists()


def test_a_stop_marker_outside_the_temporary_folder_is_never_created(tmp_path: Path) -> None:
    """A hostile events file cannot make the UI create files elsewhere: only ``gupkg-stop-<32 hex>`` inside the program's temp folder is accepted."""
    folder = tmp_path / "temp"
    folder.mkdir()
    stream = EventStream(tmp_path / "x.jsonl")
    stop = StopRequest(folder)
    stop.stream = stream
    for hostile in (str(tmp_path / "elsewhere" / ("gupkg-stop-" + "a" * 32)), str(folder / "other-name"), str(folder / ("gupkg-stop-" + "g" * 32))):
        stream.stop_file = hostile
        stop.created = False
        assert "outside this program's temporary folder" in stop.request()
        assert not Path(hostile).exists()
    assert list(folder.iterdir()) == []
