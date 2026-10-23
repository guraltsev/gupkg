"""Compile and run the regular expressions of a ``pkg.toml`` against untrusted text.

Patterns come from administrator-written manifests, but the text they scan comes from the
network. Python's ``re`` backtracks and cannot be interrupted, so this module is a *mitigation*,
not a bound: a pattern is refused at validation time when its shape is known to blow up
(nested or empty-matching unbounded repeats, overlapping alternatives inside a repeat, several
wildcards, long runs of unbounded repeats with no literal between them), the scanned text has long
runs of one character class and long lines cut, and the scan itself runs over small line-aligned
windows with a time budget checked between windows, so a page that makes an accepted pattern slow
fails with an error after about a second instead of running for minutes.

What is NOT bounded: a single window can still cost as much as its pattern allows (a pattern with
exponential behavior that the shape rules miss can hang inside one window, and no window can be
interrupted); the budget is checked between windows only. The complete fix is a subprocess regex
runner with a deadline, which is owed to the execution engine (issue 028).

Usage and API
-------------
``compile_pattern(pattern, groups=...)`` returns a compiled pattern or raises ``PatternError``;
the *groups* must be named groups of the pattern. ``scan(compiled, text)`` returns the bounded
list of matches over the prepared text (a match may run into the next three lines past a window), and
``prepare_text(text)`` is the same preparation on its own. ``escape_template(pattern, values)``
substitutes ``${name}`` references inside a pattern with escaped values.

Implementation Approach
-----------------------
Validation walks the parsed pattern tree: nested and empty-matching unbounded repeats, alternation
inside a repeat, ``.``-style repeats that are not the only unbounded repeat, and the longest run of
unbounded repeats not separated by a required literal are measured. Preparation cuts word runs
and single-character runs to ``MAX_RUN`` and breaks lines at ``MAX_LINE``. Scanning uses
``finditer(text, pos, endpos)`` over windows of about ``WINDOW`` characters that end at line
breaks. The module is pure: no input or output.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from itertools import groupby
from typing import Any

try:  # Python 3.11 and later
    from re import _constants as _sre_constants
    from re import _parser as _sre_parser
except ImportError:  # pragma: no cover - the bundled runtime is 3.12
    _sre_constants = None
    _sre_parser = None

MAX_PATTERN_LENGTH = 2000
MAX_SCAN_BYTES = 1024 * 1024
MAX_RUN = 1024
MAX_MATCHES = 10_000
_UNBOUNDED = 1000
_MULTI = 4  # a repeat that can run this many times or more gets the nesting and alternation checks
_MAX_BROAD = 3
_MAX_UNBOUNDED = 24
_MAX_STREAK = 6
# Lines are cut to 1 KiB: the cost of a quadratic pattern per line is about 5 ms there, and the
# windows below are a few lines long, so the time budget is checked every few milliseconds.
MAX_LINE = 1024
WINDOW = 4096
OVERLAP_LINES = 3  # lines a match that starts near the end of a window may run on into
SCAN_SECONDS = 1.0

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# A run of word characters can only start where the previous character is not one, so the
# pattern is tried once per run and the substitution is linear in the length of the text.
_WORD_RUN = re.compile(r"(?<!\w)\w{%d,}" % (MAX_RUN + 1), re.ASCII)


class PatternError(ValueError):
    """Report a pattern that is invalid, unsafe or lacks a required named group."""


def compile_pattern(pattern: str, groups: tuple[str, ...] = (), flags: int = 0) -> re.Pattern[str]:
    """Compile *pattern* after checking its length, its shape and its named groups.

    Parameters
    ----------
    pattern : str
        The regular expression text.
    groups : tuple of str
        Named groups that the pattern must define.
    flags : int
        ``re`` flags such as ``re.IGNORECASE``.

    Returns
    -------
    re.Pattern
        The compiled pattern.

    Raises
    ------
    PatternError
        If the pattern is too long, does not compile, nests unbounded repetition, or
        lacks one of *groups*.
    """
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError(f"the pattern is longer than {MAX_PATTERN_LENGTH} characters")
    try:
        compiled = re.compile(pattern, flags)
    except (re.error, RecursionError, OverflowError) as error:
        raise PatternError(f"{pattern!r} is not a valid regular expression: {error}") from error
    missing = [name for name in groups if name not in compiled.groupindex]
    if missing:
        raise PatternError(f"{pattern!r} must define the named group(s) {', '.join('(?P<%s>...)' % n for n in missing)}")
    reason = _unsafe_reason(pattern)
    if reason is not None:
        raise PatternError(f"{pattern!r} is refused: {reason}")
    return compiled


def prepare_text(text: str) -> str:
    """Return *text* with every over-long run of word characters or of one repeated character cut to ``MAX_RUN``.

    Both cuts are linear: a run of ``[A-Za-z0-9_]`` longer than ``MAX_RUN`` is truncated by a
    regular expression that can only start at the beginning of a run, and a run of one
    repeated character is found by substring search first and collapsed only when present.

    Raises
    ------
    PatternError
        If the text, after the cuts, is longer than ``MAX_SCAN_BYTES`` characters.
    """
    text = _WORD_RUN.sub(lambda match: match.group(0)[:MAX_RUN], text)
    needle_hits = [character for character in set(text) if character * MAX_RUN in text]
    if needle_hits:
        pieces: list[str] = []
        for character, group in groupby(text):
            count = sum(1 for _ in group)
            pieces.append(character * min(count, MAX_RUN))
        text = "".join(pieces)
    text = _cut_long_lines(text)
    if len(text) > MAX_SCAN_BYTES:
        raise PatternError(f"the text is {len(text)} characters long; at most {MAX_SCAN_BYTES} are scanned")
    return text


def _cut_long_lines(text: str) -> str:
    """Break every line longer than ``MAX_LINE`` into lines of at most that length.

    A line is broken after the last ``>`` or space of its last half-window when there is one, so
    a tag or a word is rarely split; a match that spans a break is lost, which only happens on
    minified pages whose single line is longer than the window.
    """
    if len(text) <= MAX_LINE:
        return text
    lines = text.split("\n")
    if max(map(len, lines)) <= MAX_LINE:
        return text
    cut: list[str] = []
    for line in lines:
        start = 0
        while len(line) - start > MAX_LINE:
            end = start + MAX_LINE
            point = max(line.rfind(">", start + MAX_LINE // 2, end), line.rfind(" ", start + MAX_LINE // 2, end)) + 1 or end
            cut.append(line[start:point])
            start = point
        cut.append(line[start:])
    return "\n".join(cut)


def scan(compiled: re.Pattern[str], text: str, limit: int = MAX_MATCHES, budget: float = SCAN_SECONDS) -> list[re.Match[str]]:
    """Return up to *limit* matches of *compiled* in a bounded version of *text*.

    The prepared text is scanned in line-aligned windows of about ``WINDOW`` characters. The time
    spent is checked after each window, so a page that is expensive for the pattern is refused
    after about *budget* seconds. A match that begins in one window may run on for ``OVERLAP_LINES``
    lines into the next; a longer one is cut at that point.

    Raises
    ------
    PatternError
        If the text is longer than ``MAX_SCAN_BYTES`` characters after preparation, or the scan
        used more than *budget* seconds.
    """
    prepared = prepare_text(text)
    deadline = time.monotonic() + budget
    found: list[re.Match[str]] = []
    start, size, resume = 0, len(prepared), 0
    while start < size:
        end = _window_end(prepared, start)
        # A match may begin in this window and run on into the next lines, so the search may look a
        # few lines past ``end``; only matches that begin before ``end`` belong to this window.
        for match in compiled.finditer(prepared, max(start, resume), _overlap_end(prepared, end)):
            if match.start() >= end:
                break
            found.append(match)
            resume = match.end()
            if len(found) >= limit:
                return found
        if time.monotonic() > deadline:
            raise PatternError(f"scanning took longer than {budget:g} seconds; the page is refused for this pattern")
        start = end
    return found


def _window_end(text: str, start: int) -> int:
    """Return where the scan window that begins at *start* ends: the next line break after ``WINDOW`` characters."""
    end = start + WINDOW
    if end >= len(text):
        return len(text)
    newline = text.find("\n", end, end + MAX_LINE + 1)
    return newline + 1 if newline != -1 else end


def _overlap_end(text: str, end: int) -> int:
    """Return the end of the look-ahead that follows a window ending at *end*: ``OVERLAP_LINES`` more lines."""
    stop = end
    for _ in range(OVERLAP_LINES):
        newline = text.find("\n", stop, stop + MAX_LINE + 1)
        if newline == -1:
            return min(len(text), stop + MAX_LINE) if stop < len(text) else len(text)
        stop = newline + 1
    return stop


def escape_template(pattern: str, values: Mapping[str, str]) -> str:
    """Replace ``${name}`` in a pattern with the ``re.escape`` of its value; unknown names stay as written."""

    def replace(match: re.Match[str]) -> str:
        """Return the escaped value of one reference, or the reference itself."""
        name = match.group(1)
        return re.escape(str(values[name])) if name in values else match.group(0)

    return _REFERENCE.sub(replace, pattern)


# ---------------------------------------------------------------------------
# Shape analysis
# ---------------------------------------------------------------------------


def _children(value: Any) -> Iterator[Any]:
    """Yield the sub-patterns contained in one parsed element's argument."""
    if _sre_parser is not None and isinstance(value, _sre_parser.SubPattern):
        yield value
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _children(item)


def _unsafe_reason(pattern: str) -> str | None:
    """Return why the shape of *pattern* is unsafe, or ``None`` (``None`` also when it cannot be analysed)."""
    if _sre_parser is None or _sre_constants is None:
        return None
    try:
        tree = _sre_parser.parse(pattern)
    except (re.error, RecursionError):
        return None
    repeats = (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT, getattr(_sre_constants, "POSSESSIVE_REPEAT", None))

    fold = bool(getattr(getattr(tree, "state", None), "flags", 0) & re.IGNORECASE)

    def disjoint(left: frozenset[str], right: frozenset[str]) -> bool:
        """Return whether no character is in both sets (case-insensitively under ``(?i)``)."""
        if fold:
            left, right = _fold(left), _fold(right)
        return left.isdisjoint(right)

    def first_chars(sub: Any) -> tuple[frozenset[str], bool]:
        """Return the characters *sub* can start with and whether it can match the empty string."""
        chars: set[str] = set()
        for item in flatten(sub):
            operation, argument = item
            if operation in (_sre_constants.AT, _sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
                continue
            if operation in repeats:
                inner, nullable = first_chars(argument[2])
                chars |= inner
                if argument[0] == 0 or nullable:
                    continue
                return frozenset(chars), False
            if operation == _sre_constants.BRANCH:
                nullable = False
                for alternative in argument[1]:
                    inner, empty = first_chars(alternative)
                    chars |= inner
                    nullable = nullable or empty
                if nullable:
                    continue
                return frozenset(chars), False
            chars |= _item_chars(item)
            return frozenset(chars), False
        return frozenset(chars), True

    def distinct_starts(alternatives: Any) -> bool:
        """Return whether the alternatives start with pairwise different characters and none can be empty."""
        seen: frozenset[str] = frozenset()
        for alternative in alternatives:
            chars, nullable = first_chars(alternative)
            if nullable or not disjoint(seen, chars):
                return False
            seen |= chars
        return True

    def inner_repeat_chars(sub: Any) -> list[frozenset[str]]:
        """Return, for the repeats directly in *sub*, the characters each can consume (optional wrappers are looked through)."""
        found: list[frozenset[str]] = []
        for operation, argument in flatten(sub):
            if operation in repeats:
                deeper = inner_repeat_chars(argument[2]) if argument[1] == 1 else []
                found += deeper or [_sub_chars(argument[2])]
            elif operation == _sre_constants.BRANCH:
                for alternative in argument[1]:
                    found += inner_repeat_chars(alternative)
        return found

    def ambiguous(body: Any) -> bool:
        """Return whether iterations of a repeated *body* with repeats inside can be split in many ways.

        It is safe only when the repeats inside consume pairwise different characters and the body begins
        or ends with a required single character that none of them can consume (``\\.`` in
        ``(?:\\d{1,3}\\.){1,5}``, ``[._]`` in ``(?:[._]\\d+)*``): the delimiter fixes where each iteration ends.
        """
        inner = inner_repeat_chars(body)
        if not inner:
            return False
        if any(not disjoint(inner[i], inner[j]) for i in range(len(inner)) for j in range(i + 1, len(inner))):
            return True
        union = frozenset().union(*inner)
        items = list(flatten(body))
        edges = (items[0], items[-1]) if items else ()
        singles = (_sre_constants.LITERAL, _sre_constants.IN, _sre_constants.NOT_LITERAL)
        return not any(item[0] in singles and disjoint(_item_chars(item), union) for item in edges)

    unbounded = {"count": 0, "broad": 0, "any": 0}
    reasons: list[str] = []

    def flatten(sub: Any):
        """Yield the items of *sub* in order, entering groups (capturing or not) in place."""
        for item in sub.data:
            operation, argument = item
            if operation == _sre_constants.SUBPATTERN:
                yield from flatten(argument[3])
            elif operation == getattr(_sre_constants, "ATOMIC_GROUP", None):
                yield from flatten(argument)
            else:
                yield item

    def is_any(body: Any) -> bool:
        """Return whether a repeated body starts with ``.`` or a class that matches everything."""
        first = next(flatten(body), None)
        if first is None:
            return False
        if first[0] == _sre_constants.ANY:
            return True
        if first[0] == _sre_constants.IN:
            names = {str(argument) for operation, argument in first[1] if operation == _sre_constants.CATEGORY}
            return any(
                f"CATEGORY_{kind}" in names and f"CATEGORY_NOT_{kind}" in names for kind in ("SPACE", "DIGIT", "WORD")
            )
        return False

    def is_broad(body: Any) -> bool:
        """Return whether a repeated body starts with a wildcard or a negated class (``.``, ``[^"]``)."""
        first = next(flatten(body), None)
        if first is None:
            return False
        if first[0] in (_sre_constants.ANY, _sre_constants.NOT_LITERAL):
            return True
        return first[0] == _sre_constants.IN and any(op == _sre_constants.NEGATE for op, _arg in first[1])

    def streak(sub: Any) -> int:
        """Return the most unbounded repeats in a row, with no required literal between, inside *sub*.

        A repeat counts for one plus the streak inside its body, so a dot-and-digits group repeated
        without a bound is two.
        """
        best = current = 0
        for operation, argument in flatten(sub):
            if operation in repeats:
                inner = streak(argument[2])
                current += (1 if argument[1] >= _UNBOUNDED else 0) + inner
            elif operation == _sre_constants.LITERAL:
                current = 0
            elif operation == _sre_constants.BRANCH:
                current += max((streak(alternative) for alternative in argument[1]), default=0)
            best = max(best, current)
        return best

    def walk(sub: Any, inside: bool) -> None:
        """Collect the reasons why repeats in *sub* are unsafe; *inside* means within a repeat of four or more."""
        for operation, argument in sub.data:
            if operation in repeats:
                body = argument[2]
                multi = argument[1] >= _MULTI
                if argument[1] >= _UNBOUNDED:
                    unbounded["count"] += 1
                    unbounded["broad"] += is_broad(body)
                    unbounded["any"] += is_any(body)
                if multi and ambiguous(body):
                    reasons.append("it repeats a repeated group that can be split in many ways, which can take exponential time on a hostile page")
                if multi and body.getwidth()[0] == 0:
                    reasons.append("it repeats something that can match nothing, which can take exponential time on a hostile page")
                walk(body, inside or multi)
            elif operation == _sre_constants.BRANCH:
                if inside and not distinct_starts(argument[1]):
                    reasons.append("it has alternatives that can overlap inside a repeat, which can take exponential time")
                for alternative in argument[1]:
                    walk(alternative, inside)
            else:
                for child in _children(argument):
                    walk(child, inside)

    walk(tree, False)

    # Wildcards multiply: ``.*`` is allowed only as the one unbounded repeat of the pattern, a
    # leading negated class with other unbounded repeats is refused, and so are many wildcards,
    # long runs of repeats with no literal between them, and very many repeats.
    first = next(flatten(tree), None)
    leading_broad = first is not None and first[0] in repeats and first[1][1] >= _UNBOUNDED and is_broad(first[1][2])
    if unbounded["any"] and unbounded["count"] > unbounded["any"]:
        reasons.append("it has an unbounded '.' repeat next to other unbounded repeats, which is quadratic or worse on a long line")
    if unbounded["any"] > 1:
        reasons.append("it has more than one unbounded '.' repeat, which is cubic or worse on a long line")
    if leading_broad and unbounded["count"] > 1:
        reasons.append("it starts with an unbounded wildcard and has more unbounded repeats, which is quadratic on a long line")
    if unbounded["broad"] > _MAX_BROAD:
        reasons.append(f"it has more than {_MAX_BROAD} unbounded wildcard or negated-class repeats")
    if streak(tree) > _MAX_STREAK:
        reasons.append(f"it has more than {_MAX_STREAK} unbounded repeats in a row with no literal between them")
    if unbounded["count"] > _MAX_UNBOUNDED:
        reasons.append(f"it has more than {_MAX_UNBOUNDED} unbounded repeats")
    return reasons[0] if reasons else None


# The characters used to compare what two parts of a pattern can consume: ASCII plus a few that
# ``\w``, ``\d``, ``\s`` and case folding treat specially. Sets are compared, never enumerated
# over the text, so this is only a model of "which parts could compete for the same character".
_ALPHABET = frozenset(map(chr, range(128))) | frozenset("é٣ İK")


def _fold(chars: frozenset[str]) -> frozenset[str]:
    """Return *chars* with the upper and lower case of each character added."""
    return frozenset(chars | {c.lower() for c in chars} | {c.upper() for c in chars})


def _category_matches(name: str, char: str) -> bool:
    """Return whether the regex category *name* (``CATEGORY_DIGIT``, ``CATEGORY_NOT_SPACE``...) matches *char*."""
    if "DIGIT" in name:
        matched = char.isdecimal()
    elif "SPACE" in name:
        matched = char.isspace()
    elif "WORD" in name:
        matched = char.isalnum() or char == "_"
    else:
        return True
    return matched != ("NOT" in name)


def _class_chars(members: Any) -> frozenset[str]:
    """Return the characters of the alphabet that a parsed character class matches; unknown members match everything."""
    negated = any(operation == _sre_constants.NEGATE for operation, _argument in members)
    matching: set[str] = set()
    for char in _ALPHABET:
        hit = False
        for operation, argument in members:
            if operation == _sre_constants.LITERAL:
                hit = hit or ord(char) == argument
            elif operation == _sre_constants.RANGE:
                hit = hit or argument[0] <= ord(char) <= argument[1]
            elif operation == _sre_constants.CATEGORY:
                hit = hit or _category_matches(str(argument), char)
            elif operation != _sre_constants.NEGATE:
                return _ALPHABET
        if hit != negated:
            matching.add(char)
    return frozenset(matching)


def _sub_chars(sub: Any) -> frozenset[str]:
    """Return every character that any part of the parsed pattern *sub* can consume."""
    chars: set[str] = set()
    for item in sub.data:
        chars |= _item_chars(item)
    return frozenset(chars)


def _item_chars(item: Any) -> frozenset[str]:
    """Return every character one parsed element can consume (anywhere inside it); zero-width elements consume none."""
    operation, argument = item
    if operation == _sre_constants.LITERAL:
        return frozenset({chr(argument)})
    if operation == _sre_constants.NOT_LITERAL:
        return _ALPHABET - {chr(argument)}
    if operation == _sre_constants.IN:
        return _class_chars(argument)
    if operation == _sre_constants.SUBPATTERN:
        return _sub_chars(argument[3])
    if operation == _sre_constants.BRANCH:
        return frozenset().union(*(_sub_chars(alternative) for alternative in argument[1]))
    if operation in (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT, getattr(_sre_constants, "POSSESSIVE_REPEAT", None)):
        return _sub_chars(argument[2])
    if operation == getattr(_sre_constants, "ATOMIC_GROUP", None):
        return _sub_chars(argument)
    if operation in (_sre_constants.AT, _sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
        return frozenset()
    return _ALPHABET
