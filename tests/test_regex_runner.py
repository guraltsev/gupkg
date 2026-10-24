"""Cover the regex worker process: the real bound on a manifest pattern applied to untrusted text.

``RegexRunner`` sends (pattern, flags, texts, operation) to ``gupkg/system/regex_worker.py`` through
``System.regex_worker`` and reads the matches back. Covers: identical results to in-process ``re`` for
the 50 registry patterns over every fixture page; a hostile corpus (the reviewer's patterns of four review
rounds) that must return or raise ``RegexTimeout`` within the deadline plus the start allowance (exponential patterns must raise), with the worker
gone afterwards and no process left; the fail-closed rules (a crashed, garbled, oversized or malformed
answer, a refused request, an exhausted total budget); the batch operations and the answer size cap.
Real child processes are used throughout (the fake system runs the worker for real unless a double is
registered). Out of scope: the transport and kill mechanics themselves (``test_system_contract``) and how
providers use the matches (``test_track_providers``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from gupkg.model.jsonpath import parse_path
from gupkg.model.manifest import ManifestKind, parse_manifest
from gupkg.model.patterns import PatternError, scan
from gupkg.services.checksum import ChecksumDiscoveryError
from gupkg.services.regexrun import RegexError, RegexRunner, RegexTimeout, RemoteMatch
from gupkg.services.track import TrackError, run_tracker
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult, WorkerDidNotStart
from tests.http_fixtures import HTTP_CASES, environment, serve, tracker_manifest

HERE = Path(__file__).resolve().parent
REAL = json.loads((HERE / "fixtures" / "registry_patterns.json").read_text(encoding="utf-8"))
DEADLINE = 0.8
WORKER_SOURCE = HERE.parent / "src" / "gupkg" / "system" / "regex_worker.py"
# A process start is slow on some machines (antivirus, the venv launcher) and spikes to seconds; the
# deadline counts from the worker's first line, so tests allow the start on top of deadline plus one second.
START_ALLOWANCE = 3.0


def fixture_bodies() -> list[str]:
    """Return every response body of the recorded provider fixtures."""
    bodies: list[str] = []
    for path in sorted((HERE / "fixtures" / "http").glob("*.json")):
        for response in json.loads(path.read_text(encoding="utf-8")).get("responses", []):
            if isinstance(response.get("body"), str):
                bodies.append(response["body"])
    return bodies


BODIES = fixture_bodies() + ["", "plain ascii 1.2.3", "café ☃ v2.0 \U0001f600", "a" * 5000, "line\r\nbreak\n1.0\n"]


def shape(match: re.Match[str] | RemoteMatch) -> tuple:
    """Return what the providers read from a match, in a form that compares equal across both kinds."""
    return (match.start(), match.end(), match.group(0), match.groups(), match.groupdict())


# -- identical results to in-process re ----------------------------------------------------------


@pytest.mark.parametrize("row", [pytest.param(row, id=f"{index:02d}-{row['source']}") for index, row in enumerate(REAL)])
def test_the_worker_finds_exactly_what_in_process_re_finds_for_a_real_registry_pattern(system: FakeSystem, row: dict[str, str]) -> None:
    """Over every fixture page (and awkward texts) the spans, groups and named groups are identical, in order."""
    compiled = re.compile(row["pattern"])

    remote = RegexRunner(system, per_call=60, total=600).finditer_many(compiled, BODIES, what="the fixtures")

    local = [[shape(match) for match in scan(compiled, body)] for body in BODIES]
    assert [[shape(match) for match in one] for one in remote] == local


def test_a_search_a_fullmatch_and_a_batch_agree_with_re(system: FakeSystem) -> None:
    """`search`, `fullmatch` and `fullmatch_many` return what `re` returns, and `None` for no match."""
    compiled = re.compile(r"(?P<name>tool)-(?P<version>\d+(?:\.\d+)*)\.zip")
    runner = RegexRunner(system)

    assert shape(runner.search(compiled, "get tool-1.2.zip now")) == shape(compiled.search("get tool-1.2.zip now"))
    assert runner.search(compiled, "nothing here") is None
    assert shape(runner.fullmatch(compiled, "tool-3.zip")) == shape(compiled.fullmatch("tool-3.zip"))
    assert runner.fullmatch(compiled, "xtool-3.zip") is None
    names = ["tool-1.zip", "other.zip", "tool-2.5.zip", "tool-1.zip.sig"]
    assert [m and shape(m) for m in runner.fullmatch_many(compiled, names)] == [
        (shape(compiled.fullmatch(name)) if compiled.fullmatch(name) else None) for name in names
    ]
    assert runner.fullmatches(compiled, names) == [True, False, True, False]
    assert runner.fullmatch_many(compiled, []) == [] and runner.finditer_many(compiled, []) == []


def test_non_ascii_text_and_a_lone_surrogate_survive_the_round_trip(system: FakeSystem) -> None:
    """The ASCII-only protocol carries any text: accents, symbols, emoji and a lone surrogate."""
    compiled = re.compile(r"(?P<word>\S+)")
    text = "café ☃ \U0001f600 \ud800x"

    remote = RegexRunner(system).finditer(compiled, text)

    assert [shape(m) for m in remote] == [shape(m) for m in compiled.finditer(text)]


def test_finditer_stops_at_the_limit_and_an_unmatched_group_is_none(system: FakeSystem) -> None:
    """At most `limit` matches are returned, and a group that did not take part is `None`."""
    runner = RegexRunner(system)

    assert len(runner.finditer(re.compile(r"v\d"), "v1 " * 50, limit=7)) == 7
    only = runner.finditer(re.compile(r"(?P<a>x)|(?P<b>y)"), "y")[0]
    assert only.groupdict() == {"a": None, "b": "y"} and only.groups() == (None, "y")
    assert only["b"] == "y" and only.group("a") is None and only.span() == (0, 1)


def test_an_empty_matching_pattern_returns_the_final_empty_match_like_re(system: FakeSystem) -> None:
    """`x*` over 99 characters of `y` matches 100 times in `re` (the end of text counts), and so in the worker."""
    compiled = re.compile(r"x*")

    assert len(RegexRunner(system).finditer(compiled, "y" * 99)) == len(list(compiled.finditer("y" * 99))) == 100


# -- the hostile corpus ---------------------------------------------------------------------------

# Patterns that hang Python's ``re`` for minutes on the page beside them (measured), collected over the
# review rounds. Several pass the static guard, which is exactly why the deadline exists.
HOSTILE = [
    pytest.param(r"(?:\w+\s?){3}z", "a" * 1000 + "!", id="words-and-optional-space-three-times"),
    pytest.param(r"(?:\w+\s?){1,3}(?:\w+\s?){1,3}z", "a" * 1000, id="two-bounded-word-groups"),
    pytest.param(r"(?:a+){3}b", "a" * 1000, id="plus-in-a-triple"),
    pytest.param(r"(?:(?:a+){3}){3}b", "a" * 1000, id="nine-fold-plus"),
    pytest.param(r"(?:.*){3}x", "a" * 1000, id="dot-star-in-a-triple"),
    pytest.param(r"(?:(?:a|b)+){3}c", "ab" * 500, id="alternation-plus-in-a-triple"),
    pytest.param(r"(?:x+x+x+){3}y", "x" * 1000, id="nine-adjacent-pluses"),
    pytest.param(r"\d+\d+\d+x", "1" * 1000, id="three-digit-runs"),
    pytest.param(r"\d+\d+\d+\d+x", "1" * 1000, id="four-digit-runs"),
    pytest.param(r"\d*\d*\d*\d*x", "1" * 1000, id="four-digit-stars"),
    pytest.param(r"\w+\d+\w+\d+x", "1" * 1000, id="words-and-digits-alternating"),
    pytest.param(r"(?P<n>a+)(?P<m>a+)(?P<o>a+)(?P<p>a+)b", "a" * 1000, id="four-named-runs"),
    pytest.param(r"(a+)+$", "a" * 40 + "!", id="classic-exponential"),
    pytest.param(r"(?:a|a)+b", "a" * 40, id="identical-alternatives"),
    pytest.param(r"(?:a|aa)+b", "a" * 60, id="prefix-alternatives"),
    pytest.param(r"(?:a{1,30}){1,30}b", "a" * 60, id="nested-bounded"),
    pytest.param(r"(?:\w+[-.]?){2,12}q", "a" * 60, id="bounded-words-with-separator"),
    pytest.param(r".*.*.*x", "a" * 1000, id="three-dot-stars"),
    pytest.param(r"(?P<a>.*)=(?P<b>.*)!", "=" * 1000, id="two-named-dot-stars"),
]
# Plausible patterns, and shapes that are slow only on some pages, on large hostile pages: a call returns
# (the page may be refused for its size) or times out; it never runs past the deadline. Pages are built
# lazily so that test ids stay short.
BENIGN_ON_HOSTILE = [
    pytest.param(r"(?P<version>\d+(?:\.\d+)+)", lambda: "1." * 40_000, id="dotted-version-on-ones-and-dots"),
    pytest.param(r"(?P<version>\d+(?:\.\d+)+)", lambda: "a" * 500_000, id="dotted-version-on-a-long-run"),
    pytest.param(r'href="(?P<href>[^"]+)"', lambda: '<a href="' * 50_000, id="attribute-on-repeated-opens"),
    pytest.param(r"Version: (?P<version>\d+(?:\.\d+)+)", lambda: "Version: " * 100_000, id="repeated-prefix"),
    pytest.param(r"(a?)+b", lambda: "a" * 40, id="optional-in-a-plus"),
    pytest.param(r"x.*y.*z.*w", lambda: "x" * 1000, id="dot-stars-between-literals"),
    pytest.param("a?" * 16 + "a" * 16 + "b", lambda: "a" * 15, id="sixteen-optionals"),
    pytest.param("a{1,3}" * 10 + "b", lambda: "a" * 25, id="ten-bounded-runs"),
]


def deadline_runner(system: FakeSystem) -> RegexRunner:
    """Return a runner whose per-call deadline is short and whose total is large, so each call is judged alone."""
    return RegexRunner(system, per_call=DEADLINE, total=1000.0)


# Patterns whose cost grows exponentially (at least 1.6 to the power of the page length, for pages of 40 or more
# characters): no CPU can finish them inside the deadline, so they prove the kill path deterministically.
ALWAYS_SLOW = [
    pytest.param(r"(a+)+$", "a" * 40 + "!", id="classic-exponential"),
    pytest.param(r"(?:a|a)+b", "a" * 40, id="identical-alternatives"),
    pytest.param(r"(?:a|aa)+b", "a" * 60, id="prefix-alternatives"),
    pytest.param(r"(?:a{1,30}){1,30}b", "a" * 60, id="nested-bounded"),
]


@pytest.mark.parametrize(("pattern", "page"), HOSTILE)
def test_a_hostile_pattern_on_its_page_is_bounded_by_the_deadline(system: FakeSystem, pattern: str, page: str) -> None:
    """Each pair returns, or raises RegexTimeout naming the field, and either way within the deadline plus the start allowance.

    How slow a polynomial pattern is depends on the CPU and its load, so this test does not require a timeout;
    ``test_an_exponential_pattern_is_killed_at_the_deadline`` proves the kill path.
    """
    started = time.monotonic()

    try:
        deadline_runner(system).finditer(re.compile(pattern), page, what="the hostile page")
    except RegexTimeout as error:
        assert re.search(r"took longer than 0\.8 seconds in the hostile page", str(error))

    assert time.monotonic() - started < DEADLINE + 1.0 + START_ALLOWANCE


@pytest.mark.parametrize(("pattern", "page"), ALWAYS_SLOW)
def test_an_exponential_pattern_is_killed_at_the_deadline(system: FakeSystem, pattern: str, page: str) -> None:
    """Each exponential pair always raises RegexTimeout naming the pattern and the field, within the deadline plus the start allowance."""
    started = time.monotonic()

    with pytest.raises(RegexTimeout, match=r"took longer than 0\.8 seconds in the hostile page"):
        deadline_runner(system).finditer(re.compile(pattern), page, what="the hostile page")

    assert time.monotonic() - started < DEADLINE + 1.0 + START_ALLOWANCE


@pytest.mark.parametrize(("pattern", "page"), BENIGN_ON_HOSTILE)
def test_large_hostile_pages_return_or_time_out_within_the_deadline(system: FakeSystem, pattern: str, page: Callable[[], str]) -> None:
    """A page that is refused for its size, answered, or too slow all end within the deadline plus one second."""
    started = time.monotonic()

    try:
        deadline_runner(system).finditer(re.compile(pattern), page())
    except (RegexTimeout, PatternError):
        pass

    assert time.monotonic() - started < DEADLINE + 1.0 + START_ALLOWANCE


def _count_processes_of(script_name: str) -> int:
    """Return how many python processes on the machine have *script_name* in their command line."""
    listing = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process | Where-Object {{ $_.Name -like 'python*' -and $_.CommandLine -like '*{script_name}*' }} | Measure-Object).Count"],
        capture_output=True, text=True, timeout=60,
    )
    return int(listing.stdout.strip())


@pytest.mark.windows_integration
def test_no_worker_process_is_left_after_the_hostile_corpus(system: FakeSystem, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After several kills no process of this test's own copy of the worker is running (other runs are not counted)."""
    unique = f"regex_worker_{uuid.uuid4().hex}.py"
    copy = tmp_path / unique
    shutil.copyfile(WORKER_SOURCE, copy)
    monkeypatch.setattr("gupkg.system.windows._REGEX_WORKER", copy)
    hostile = json.dumps({"op": "search", "pattern": "(a+)+$", "flags": 32, "texts": ["a" * 40 + "!"], "limit": 1, "cap": 100})
    lines: list[str] = []
    outcome: list[BaseException] = []

    def hang() -> None:
        """Run a worker that never finishes and note how it ends."""
        try:
            system.regex_worker(hostile, 3.0, on_line=lines.append)
        except TimeoutError as error:
            outcome.append(error)

    thread = threading.Thread(target=hang)
    thread.start()
    while not lines:
        time.sleep(0.05)
    running = _count_processes_of(unique)  # the positive control: the query does see a live worker
    thread.join(30)
    runner = deadline_runner(system)
    for pattern, page in (("(a+)+$", "a" * 40 + "!"), (r"(?:a|a)+b", "a" * 40), (r"(?:a|aa)+b", "a" * 60)):
        with pytest.raises(RegexTimeout):
            runner.finditer(re.compile(pattern), page)
    time.sleep(0.5)

    assert running >= 1 and outcome and _count_processes_of(unique) == 0


def test_the_total_budget_of_a_runner_is_a_hard_cap(system: FakeSystem) -> None:
    """After the total is used up the next call fails at once, without starting a worker."""
    runner = RegexRunner(system, per_call=DEADLINE, total=1.0)
    with pytest.raises(RegexTimeout):
        runner.finditer(re.compile(r"(a+)+$"), "a" * 40 + "!")
    with pytest.raises(RegexTimeout):
        runner.finditer(re.compile(r"(a+)+$"), "a" * 40 + "!")
    logged = len(system.regex_log)

    with pytest.raises(RegexTimeout, match="used their 1 seconds"):
        runner.finditer(re.compile(r"x"), "x", what="the third call")

    assert len(system.regex_log) == logged and runner.spent >= 1.0


def test_the_deadline_of_a_call_never_exceeds_what_is_left_of_the_total(system: FakeSystem) -> None:
    """With 0.5 seconds left of the total, a hostile call is stopped after about 0.5 seconds, not the per-call 2."""
    runner = RegexRunner(system, per_call=2.0, total=0.5)
    started = time.monotonic()

    with pytest.raises(RegexTimeout):
        runner.finditer(re.compile(r"(a+)+$"), "a" * 40 + "!")

    assert time.monotonic() - started < 2.0


# -- fail closed ------------------------------------------------------------------------------------


def double_answering(stdout: str, returncode: int = 0, stderr: str = "") -> FakeSystem:
    """Return a FakeSystem whose regex worker is a double that always answers *stdout*."""
    return FakeSystem(Path("."), processes={"regex-worker": lambda call: ProcessResult(returncode, stdout, stderr)})


GOOD = {"ok": True, "names": {}, "results": [[[0, 1, ["x"]]]]}


@pytest.mark.parametrize(
    ("stdout", "returncode", "message"),
    [
        pytest.param("", 1, "unreadable answer", id="crashed-with-no-output"),
        pytest.param("Traceback (most recent call last)", 1, "unreadable answer", id="traceback-text"),
        pytest.param("not json at all", 0, "unreadable answer", id="garbage-with-status-zero"),
        pytest.param("[1, 2, 3]", 0, "unexpected answer", id="a-list-instead-of-an-object"),
        pytest.param(json.dumps({"ok": "yes"}), 0, "unexpected answer", id="ok-is-not-a-boolean"),
        pytest.param(json.dumps({"ok": False, "error": "bad request: re.error"}), 2, "could not be run.*bad request", id="the-worker-refused"),
        pytest.param(json.dumps({"ok": False, "error": "the matches hold more than 5 characters"}), 3, "more than 5 characters", id="the-worker-hit-its-size-cap"),
        pytest.param(json.dumps(GOOD), 1, "could not be run", id="success-body-with-a-failing-status"),
        pytest.param(json.dumps({**GOOD, "results": []}), 0, "malformed answer", id="too-few-results"),
        pytest.param(json.dumps({**GOOD, "results": [[[0, 1, ["x"]]], []]}), 0, "malformed answer", id="too-many-results"),
        pytest.param(json.dumps({**GOOD, "results": ["x"]}), 0, "malformed answer", id="results-not-lists"),
        pytest.param(json.dumps({**GOOD, "names": {"a": 5}}), 0, "malformed answer", id="group-number-out-of-range"),
        pytest.param(json.dumps({**GOOD, "names": []}), 0, "malformed answer", id="names-not-a-mapping"),
        pytest.param(json.dumps({**GOOD, "results": [[[0, 1]]]}), 0, "malformed match", id="match-missing-groups"),
        pytest.param(json.dumps({**GOOD, "results": [[[2, 1, ["x"]]]]}), 0, "malformed match", id="span-reversed"),
        pytest.param(json.dumps({**GOOD, "results": [[[0, 1, ["x", "y"]]]]}), 0, "malformed match", id="more-groups-than-the-pattern-has"),
        pytest.param(json.dumps({**GOOD, "results": [[[0, 1, [None]]]]}), 0, "malformed match", id="whole-match-is-null"),
        pytest.param(json.dumps({**GOOD, "results": [[[0, 1, [7]]]]}), 0, "malformed match", id="group-not-text"),
        pytest.param(" " * 8_100_000, 0, "oversized answer", id="oversized-output"),
    ],
)
def test_an_answer_that_is_not_exactly_right_is_an_error_never_no_match(tmp_path: Path, stdout: str, returncode: int, message: str) -> None:
    """Anything off in the worker's answer raises RegexError: it is never turned into 'nothing matched'."""
    system = FakeSystem(tmp_path / "machine", processes={"regex-worker": lambda call: ProcessResult(returncode, stdout)})

    with pytest.raises(RegexError, match=message):
        RegexRunner(system).finditer(re.compile(r"x"), "x", what="the page")


def test_a_worker_that_cannot_start_and_a_deadline_from_the_system_are_errors(tmp_path: Path) -> None:
    """An OSError from the boundary is a RegexError; its TimeoutError is a RegexTimeout; neither is 'no match'."""

    def broken(call: ProcessCall) -> ProcessResult:
        """Fail to start."""
        raise FileNotFoundError("no interpreter")

    def slow(call: ProcessCall) -> ProcessResult:
        """Report the deadline."""
        raise TimeoutError("did not finish")

    with pytest.raises(RegexError, match="could not run.*no interpreter"):
        RegexRunner(FakeSystem(tmp_path / "a", processes={"regex-worker": broken})).search(re.compile("x"), "x")
    with pytest.raises(RegexTimeout, match="took longer"):
        RegexRunner(FakeSystem(tmp_path / "b", processes={"regex-worker": slow})).search(re.compile("x"), "x")


def test_the_request_the_parent_sends_is_ascii_json_with_the_pattern_flags_and_prepared_texts(tmp_path: Path) -> None:
    """Pattern, flags and limit travel as ASCII JSON; texts are shrunk first (a 5000-character run becomes 1024)."""
    seen: list[str] = []

    def spy(call: ProcessCall) -> ProcessResult:
        """Record the request and answer with no matches."""
        seen.append(call.input_text or "")
        return ProcessResult(0, json.dumps({"ok": True, "names": {}, "results": [[]]}))

    system = FakeSystem(tmp_path / "machine", processes={"regex-worker": spy})

    RegexRunner(system).finditer(re.compile(r"café", re.IGNORECASE), "a" * 5000 + " é", limit=3)

    request = json.loads(seen[0])
    assert seen[0].isascii() and request["op"] == "finditer" and request["limit"] == 3
    assert request["flags"] & re.IGNORECASE and request["pattern"] == "café"
    assert len(request["texts"][0]) < 1100 and request["cap"] == 1_000_000


# -- the real worker's own limits ------------------------------------------------------------------


def test_the_worker_refuses_an_answer_larger_than_its_cap(system: FakeSystem) -> None:
    """A thousand matches of a thousand characters each (more than a million characters) is an error, not a huge answer."""
    page = "ab cd " * 170_000

    with pytest.raises(RegexError, match="more than 1000000 characters"):
        RegexRunner(system).finditer(re.compile(r".{1000}"), page)


def test_a_bad_request_to_the_real_worker_is_reported_as_an_error(system: FakeSystem) -> None:
    """An unknown operation or a pattern that does not compile comes back as `ok: false`, exit status 2."""
    request = json.dumps({"op": "nope", "pattern": "x", "flags": 32, "texts": ["x"], "limit": 1, "cap": 10})
    bad_pattern = json.dumps({"op": "search", "pattern": "(", "flags": 32, "texts": ["x"], "limit": 1, "cap": 10})

    for text in (request, bad_pattern, "not json"):
        result = system.regex_worker(text, 10)
        assert result.returncode == 2 and json.loads(result.stdout)["ok"] is False


def test_the_text_size_cap_applies_before_anything_is_sent(system: FakeSystem) -> None:
    """A page that is still over a mebibyte after shrinking is refused by the parent without starting a worker."""
    with pytest.raises(PatternError, match="at most"):
        RegexRunner(system).finditer(re.compile("x"), "ab cd " * 400_000)

    assert system.regex_log == []


# -- wiring: every provider goes through the worker, and a broken worker fails the provider --------

CASES = sorted(HTTP_CASES.glob("*.json"))


@pytest.mark.parametrize("case", [pytest.param(path, id=path.stem) for path in CASES])
def test_a_provider_that_uses_a_pattern_asks_the_worker_and_fails_closed_when_the_worker_breaks(
    system: FakeSystem, tmp_path: Path, case: Path
) -> None:
    """With the real worker a tracker finds its expected version; with a crashing worker it raises, never 'no version found'."""
    document = json.loads(case.read_text(encoding="utf-8"))
    serve(system, case.stem)
    manifest = tracker_manifest(document["tracker"])

    result = run_tracker(manifest, environment(system, tmp_path, package=document["tracker"]))

    assert result.version == document["expected"]["version"]
    if not system.regex_log:
        return  # a provider with no pattern at all (a header date, a redirect without a match)
    broken = FakeSystem(tmp_path / "broken", processes={"regex-worker": lambda call: ProcessResult(1, "")})
    serve(broken, case.stem)
    with pytest.raises((TrackError, ChecksumDiscoveryError), match="regular expression"):
        run_tracker(manifest, environment(broken, tmp_path / "again", package=document["tracker"]))


def test_a_json_filter_with_a_regular_expression_is_decided_by_the_worker(system: FakeSystem) -> None:
    """`[?name=~...]` sends the texts of all children in one request and keeps those the worker says match."""
    documents = [{"name": "tool-1.zip"}, {"name": "other"}, {"name": "tool-2.zip"}]
    runner = RegexRunner(system)

    kept = parse_path(r"$[?name=~tool-\d\.zip].name").values(documents, runner.fullmatches)

    assert kept == ["tool-1.zip", "tool-2.zip"] and len(system.regex_log) == 1
    assert json.loads(system.regex_log[0])["texts"] == ["tool-1.zip", "other", "tool-2.zip"]


# -- cost and strictness ---------------------------------------------------------------------------


def test_the_wall_clock_cap_stops_a_run_that_starts_too_many_workers(tmp_path: Path) -> None:
    """Calls whose process starts add up past `wall` seconds: the next one fails with a clear error and starts nothing."""
    ticks = iter(range(0, 10_000, 100))
    ok = ProcessResult(0, json.dumps({"ok": True, "names": {}, "results": [[]], "seconds": 0.0}))
    system = FakeSystem(tmp_path / "machine", processes={"regex-worker": lambda call: ok})
    runner = RegexRunner(system, wall=150.0, clock=lambda: float(next(ticks)))

    runner.finditer(re.compile("x"), "y")
    runner.finditer(re.compile("x"), "y")
    with pytest.raises(RegexTimeout, match="150 seconds of wall-clock time"):
        runner.finditer(re.compile("x"), "y", what="the third call")

    assert len(system.regex_log) == 2


def test_a_worker_that_did_not_start_is_reported_as_that_not_as_a_slow_pattern(tmp_path: Path) -> None:
    """The message names the start failure, never 'took longer than 2 seconds'."""

    def never_started(call: ProcessCall) -> ProcessResult:
        """Report the start limit."""
        raise WorkerDidNotStart("the regex worker did not start within 30 seconds")

    system = FakeSystem(tmp_path / "machine", processes={"regex-worker": never_started})

    with pytest.raises(RegexError, match="did not start") as caught:
        RegexRunner(system).search(re.compile("x"), "x")

    assert not isinstance(caught.value, RegexTimeout) and "took longer" not in str(caught.value)


@pytest.mark.parametrize(
    "item",
    [
        pytest.param([0, 5, ["abcde"]], id="span-longer-than-the-text"),
        pytest.param([True, 1, ["x"]], id="boolean-start"),
        pytest.param([0, True, ["x"]], id="boolean-end"),
        pytest.param([0.0, 1, ["x"]], id="float-start"),
    ],
)
def test_a_match_outside_the_text_or_with_non_integer_positions_is_rejected(tmp_path: Path, item: list) -> None:
    """The text sent is one character long; a span past it, a boolean or a float position is a malformed match."""
    answer = ProcessResult(0, json.dumps({"ok": True, "names": {}, "results": [[item]]}))
    system = FakeSystem(tmp_path / "machine", processes={"regex-worker": lambda call: answer})

    with pytest.raises(RegexError, match="malformed match"):
        RegexRunner(system).finditer(re.compile(r"x"), "x")


def test_a_boolean_group_number_in_the_names_is_rejected(tmp_path: Path) -> None:
    """`true` is not a group number even though it is an int in Python."""
    answer = ProcessResult(0, json.dumps({"ok": True, "names": {"a": True}, "results": [[]]}))
    system = FakeSystem(tmp_path / "machine", processes={"regex-worker": lambda call: answer})

    with pytest.raises(RegexError, match="malformed answer"):
        RegexRunner(system).finditer(re.compile(r"(?P<a>x)"), "x")


RELEASES_TOML = """
format = 2
description = "x"
project_url = "https://example.com/"

[track]
provider = "github"
repo = "o/r"
release = "newest-matching"
tag_pattern = 'v(?P<version>\\d+\\.\\d+)'
asset_pattern = 'tool-\\d+\\.\\d+\\.zip'

[source]
download_url = "${asset_url}"
"""


def test_a_release_run_needs_two_worker_calls_not_two_per_release_and_finds_the_same_version(system: FakeSystem, tmp_path: Path) -> None:
    """Thirty releases: one call matches all tags, one all asset names; the newest release with its asset is found."""
    base = "https://github.com/o/r/releases"
    rows = [
        {
            "tag_name": f"v1.{number}",
            "draft": False,
            "prerelease": False,
            "html_url": f"{base}/tag/v1.{number}",
            "assets": [
                {"name": f"tool-1.{number}.zip", "state": "uploaded", "browser_download_url": f"{base}/download/v1.{number}/tool-1.{number}.zip"},
                {"name": f"tool-1.{number}.zip.sig", "state": "uploaded", "browser_download_url": f"{base}/download/v1.{number}/tool-1.{number}.zip.sig"},
            ],
        }
        for number in range(30)
    ]
    system.urls["https://api.github.com/repos/o/r/releases?per_page=100"] = FakeHttp(json.dumps(rows), 200, {"Content-Type": "application/json"})
    manifest = parse_manifest(RELEASES_TOML, ManifestKind.TRACKER)

    result = run_tracker(manifest, environment(system, tmp_path))

    assert result.version == "1.29" and result.captures["asset_name"] == "tool-1.29.zip"
    assert len(system.regex_log) == 2
