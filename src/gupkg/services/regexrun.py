"""Apply manifest patterns to untrusted text in a worker process with a hard deadline.

Python's ``re`` backtracks and cannot be interrupted, and no static check of a pattern can bound
it. So every match of a manifest-provided pattern against text from the network (a page, an
index, a feed, a JSON value, a tag or asset name) runs in a separate worker process
(``gupkg.system.regex_worker``, started through ``System.regex_worker``). The parent waits at most a
deadline per call and a total per runner; when the deadline passes the worker's whole process tree
is killed and the call raises ``RegexTimeout``.

What this guarantees: a call returns or fails within its deadline plus the cost of killing a
process; a worker that is killed, crashes, prints something malformed or oversized is an error
(**fail closed**: it is never reported as "no match", so "no version found" cannot hide an
attack); the worker runs the same interpreter with ``-I -s -B -S``, a minimal environment and an
empty temporary folder, and never imports gupkg. What it does not guarantee: memory (a pattern
cannot use unbounded time, but ``re`` can still allocate during the deadline), and no network
access is enforced by the operating system (the worker simply never uses a socket).

Usage and API
-------------
``RegexRunner(system)`` is made once per provider run. ``finditer(compiled, text, limit=, what=)``
returns the matches (at most *limit*), ``search`` and ``fullmatch`` one match or ``None``,
``fullmatch_many(compiled, texts, what=)`` one result per text, and ``fullmatches(compiled, texts)``
a list of booleans (the *matcher* that ``gupkg.model.jsonpath`` takes). A match is a
``RemoteMatch``, which offers the part of ``re.Match`` that the providers use. *what* names the
field or URL for the error message. Every failure is a ``RegexError`` (a ``PatternError``), a
deadline a ``RegexTimeout``.

Implementation Approach
-----------------------
The request is one JSON document (ASCII) on the worker's standard input; the answer is one JSON
document on its standard output, checked strictly before use. Texts are shrunk with
``prepare_text`` first. One worker process serves one call (a batch of texts for ``fullmatch_many``),
which keeps the protocol free of long-lived state; a process start costs a few tens of milliseconds.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..model.patterns import MAX_MATCHES, PatternError, prepare_text
from ..system.port import System, WorkerDidNotStart

PER_CALL_SECONDS = 2.0
TOTAL_SECONDS = 10.0
# A separate cap on the wall-clock time of all calls of one runner, process starts included: a run over
# many releases starts a worker per call, and this stops it with a clear error instead of running on.
WALL_SECONDS = 120.0
MAX_ANSWER_CHARACTERS = 1_000_000
_ANSWER_ALLOWANCE = 8_000_000  # the parent refuses an answer larger than this before parsing it


class RegexError(PatternError):
    """Report that the worker could not give a trustworthy answer (it crashed, or sent something malformed)."""


class RegexTimeout(RegexError):
    """Report that a pattern ran past its deadline (or the runner's total) and the worker was killed."""


class RemoteMatch:
    """The result of one match, with the parts of ``re.Match`` that the providers use.

    Parameters
    ----------
    start, end : int
        Where the whole match begins and ends in the text that was sent.
    groups : sequence
        The text of group 0 (the whole match) and of every numbered group, ``None`` for a group
        that did not take part.
    names : mapping
        Group name to group number.
    """

    __slots__ = ("_start", "_end", "_groups", "_names")

    def __init__(self, start: int, end: int, groups: Sequence[str | None], names: Mapping[str, int]) -> None:
        """Keep the span, the group texts and the names."""
        self._start, self._end = start, end
        self._groups = tuple(groups)
        self._names = dict(names)

    def group(self, *keys: int | str) -> Any:
        """Return one group's text (the whole match for no key), or a tuple for several keys."""
        if not keys:
            return self._groups[0]
        values = tuple(self._groups[self._index(key)] for key in keys)
        return values[0] if len(values) == 1 else values

    def __getitem__(self, key: int | str) -> str | None:
        """Return one group's text."""
        return self._groups[self._index(key)]

    def groups(self, default: str | None = None) -> tuple[str | None, ...]:
        """Return the text of every numbered group (not the whole match)."""
        return tuple(default if value is None else value for value in self._groups[1:])

    def groupdict(self, default: str | None = None) -> dict[str, str | None]:
        """Return the text of every named group by name."""
        return {name: default if self._groups[index] is None else self._groups[index] for name, index in self._names.items()}

    def start(self) -> int:
        """Return where the whole match begins."""
        return self._start

    def end(self) -> int:
        """Return where the whole match ends."""
        return self._end

    def span(self) -> tuple[int, int]:
        """Return where the whole match begins and ends."""
        return self._start, self._end

    def __bool__(self) -> bool:
        """A match is true, like ``re.Match``."""
        return True

    def _index(self, key: int | str) -> int:
        """Return the group number for a number or a name, raising ``IndexError`` for an unknown one."""
        if isinstance(key, str):
            if key not in self._names:
                raise IndexError(f"no such group: {key!r}")
            return self._names[key]
        if not 0 <= key < len(self._groups):
            raise IndexError(f"no such group: {key!r}")
        return key


class RegexRunner:
    """Run pattern matches against untrusted text in a worker process with a deadline.

    Parameters
    ----------
    system : System
        The boundary that starts the worker (``System.regex_worker``).
    per_call : float
        Seconds one call may take before the worker is killed.
    total : float
        Seconds all calls of this runner may take together; further calls fail at once. A runner
        belongs to one provider run, so one slow page cannot starve the next package.
    wall : float
        Seconds of wall-clock time all calls may take together, process starts included (``WALL_SECONDS``).
    clock : callable
        Replaces ``time.monotonic`` in tests.
    """

    def __init__(
        self,
        system: System,
        *,
        per_call: float = PER_CALL_SECONDS,
        total: float = TOTAL_SECONDS,
        wall: float = WALL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Remember the boundary and the limits; no process is started."""
        self.system = system
        self.per_call = per_call
        self.total = total
        self.wall = wall
        self.spent = 0.0
        self.elapsed = 0.0
        self._clock = clock

    def finditer(self, compiled: re.Pattern[str], text: str, *, limit: int = MAX_MATCHES, what: str = "") -> list[RemoteMatch]:
        """Return up to *limit* matches of *compiled* in *text*.

        Raises
        ------
        RegexTimeout
            If the deadline passes.
        RegexError
            If the worker fails or the text is too large after preparation.
        """
        return self._call("finditer", compiled, [text], limit, what)[0]

    def finditer_many(
        self, compiled: re.Pattern[str], texts: Sequence[str], *, limit: int = MAX_MATCHES, what: str = ""
    ) -> list[list[RemoteMatch]]:
        """Return, for every text, up to *limit* matches of *compiled*; one worker serves all the texts."""
        if not texts:
            return []
        return self._call("finditer", compiled, list(texts), limit, what)

    def search(self, compiled: re.Pattern[str], text: str, *, what: str = "") -> RemoteMatch | None:
        """Return the first match of *compiled* anywhere in *text*, or ``None``."""
        found = self._call("search", compiled, [text], 1, what)[0]
        return found[0] if found else None

    def fullmatch(self, compiled: re.Pattern[str], text: str, *, what: str = "") -> RemoteMatch | None:
        """Return the match of *compiled* against the whole of *text*, or ``None``."""
        found = self._call("fullmatch", compiled, [text], 1, what)[0]
        return found[0] if found else None

    def fullmatch_many(self, compiled: re.Pattern[str], texts: Sequence[str], *, what: str = "") -> list[RemoteMatch | None]:
        """Return, for every text, the match of *compiled* against the whole of it, or ``None``; one worker serves all."""
        if not texts:
            return []
        return [found[0] if found else None for found in self._call("fullmatch", compiled, list(texts), 1, what)]

    def fullmatches(self, compiled: re.Pattern[str], texts: Sequence[str]) -> list[bool]:
        """Return, for every text, whether *compiled* matches the whole of it (the matcher ``jsonpath`` takes)."""
        return [found is not None for found in self.fullmatch_many(compiled, texts, what="a JSON filter")]

    # -- the call --------------------------------------------------------------

    def _call(self, operation: str, compiled: re.Pattern[str], texts: list[str], limit: int, what: str) -> list[list[RemoteMatch]]:
        """Send one request to a worker and return its checked answer, or raise."""
        where = f" in {what}" if what else ""
        remaining = self.total - self.spent
        if remaining <= 0:
            raise RegexTimeout(f"regular expressions have used their {self.total:g} seconds for this run{where}; the pattern {compiled.pattern!r} was not run")
        if self.elapsed >= self.wall:
            raise RegexTimeout(
                f"regular expressions have taken {self.wall:g} seconds of wall-clock time for this run{where}, "
                f"starting a worker for each call; the pattern {compiled.pattern!r} was not run"
            )
        deadline = min(self.per_call, remaining)
        prepared = [prepare_text(text) for text in texts]
        request = json.dumps(
            {
                "op": operation,
                "pattern": compiled.pattern,
                "flags": compiled.flags,
                "texts": prepared,
                "limit": limit,
                "cap": MAX_ANSWER_CHARACTERS,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        started = self._clock()
        try:
            result = self.system.regex_worker(request, deadline)
        except WorkerDidNotStart as error:
            self.elapsed += self._clock() - started
            raise RegexError(f"the regular expression worker did not start{where}: {error}") from error
        except TimeoutError as error:
            self.elapsed += self._clock() - started
            self.spent += deadline
            raise RegexTimeout(
                f"the pattern {compiled.pattern!r} took longer than {deadline:g} seconds{where} and was stopped; "
                "it is too slow on this text (a hostile page can do this to a pattern that is slow in the worst case)"
            ) from error
        except OSError as error:
            self.elapsed += self._clock() - started
            self.spent += self._clock() - started
            raise RegexError(f"could not run the regular expression worker{where}: {error}") from error
        elapsed = self._clock() - started
        self.elapsed += elapsed
        converted, matching = self._checked(result.returncode, result.stdout, compiled, [len(text) for text in prepared], where)
        # The budget counts the time the matching took (as the worker measured it, within what the wall clock
        # allows), not the time to start a process, which is slow on some machines and no fault of the pattern.
        self.spent += min(elapsed, matching) if matching is not None else elapsed
        return converted

    @staticmethod
    def _checked(
        returncode: int, stdout: str, compiled: re.Pattern[str], lengths: list[int], where: str
    ) -> tuple[list[list[RemoteMatch]], float | None]:
        """Parse and validate the worker's answer into matches and the matching time; anything unexpected is a ``RegexError``."""
        pattern = compiled.pattern
        if len(stdout) > _ANSWER_ALLOWANCE:
            raise RegexError(f"the regular expression worker sent an oversized answer{where}")
        try:
            document = json.loads(stdout)
        except ValueError as error:
            raise RegexError(f"the regular expression worker for {pattern!r} gave an unreadable answer{where} (exit status {returncode})") from error
        if not isinstance(document, dict) or not isinstance(document.get("ok"), bool):
            raise RegexError(f"the regular expression worker for {pattern!r} gave an unexpected answer{where}")
        if not document["ok"] or returncode != 0:
            reason = document.get("error") if isinstance(document.get("error"), str) else f"exit status {returncode}"
            raise RegexError(f"the regular expression {pattern!r} could not be run{where}: {reason[:200]}")
        names, results = document.get("names"), document.get("results")
        if (
            not isinstance(names, dict)
            or not all(isinstance(name, str) and type(index) is int and 0 < index <= compiled.groups for name, index in names.items())
            or not isinstance(results, list)
            or len(results) != len(lengths)
        ):
            raise RegexError(f"the regular expression worker for {pattern!r} gave a malformed answer{where}")
        converted: list[list[RemoteMatch]] = []
        for one_text, length in zip(results, lengths):
            if not isinstance(one_text, list):
                raise RegexError(f"the regular expression worker for {pattern!r} gave a malformed answer{where}")
            matches: list[RemoteMatch] = []
            for item in one_text:
                if not RegexRunner._well_formed(item, compiled.groups, length):
                    raise RegexError(f"the regular expression worker for {pattern!r} gave a malformed match{where}")
                matches.append(RemoteMatch(item[0], item[1], item[2], names))
            converted.append(matches)
        seconds = document.get("seconds")
        return converted, float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0 else None

    @staticmethod
    def _well_formed(item: Any, groups: int, length: int) -> bool:
        """Return whether *item* is ``[start, end, [group texts]]`` inside a text of *length* characters, with one text per group and the whole match."""
        return (
            isinstance(item, list)
            and len(item) == 3
            and type(item[0]) is int
            and type(item[1]) is int
            and 0 <= item[0] <= item[1] <= length
            and isinstance(item[2], list)
            and len(item[2]) == groups + 1
            and item[2][0] is not None
            and all(value is None or isinstance(value, str) for value in item[2])
        )
