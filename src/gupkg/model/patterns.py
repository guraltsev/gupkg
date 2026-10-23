"""Compile and run the regular expressions of a ``pkg.toml`` against untrusted text.

Patterns come from administrator-written manifests, but the text they scan comes from
the network. Python's ``re`` backtracks, so a careless pattern on a hostile page can
run for hours. This module narrows the exposure in three ways: a pattern is rejected
at validation time when it nests unbounded repetition (``(a+)+``), the scanned text
has any run of one repeated character cut to a bounded length, and the amount of
text and the number of matches are capped.

Usage and API
-------------
``compile_pattern(pattern, groups=...)`` returns a compiled pattern or raises
``PatternError``; ``required groups`` must be named groups of the pattern.
``scan(compiled, text)`` returns the bounded list of matches of a prepared text, and
``prepare_text(text)`` is the same preparation on its own. ``escape_template(pattern,
values)`` substitutes ``${name}`` references inside a pattern with escaped values.

Implementation Approach
-----------------------
Validation walks the parsed pattern tree. A repeat with no upper bound that contains
another repeat able to repeat more than once is refused, which covers the classic
exponential shapes. The remaining risk is polynomial (a leading ``.*`` on a very long line
of mixed punctuation), which the text cap and the run cuts bound but cannot remove: Python's
``re`` cannot be interrupted, so a page designed against a specific pattern can still keep one
check busy for seconds. The module is pure: no input or output.
"""

from __future__ import annotations

import re
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
_MAX_BROAD = 3
_MAX_UNBOUNDED = 8
MAX_LINE = 8192

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
        while len(line) > MAX_LINE:
            window = line[:MAX_LINE]
            point = max(window.rfind(">", MAX_LINE // 2), window.rfind(" ", MAX_LINE // 2)) + 1 or MAX_LINE
            cut.append(line[:point])
            line = line[point:]
        cut.append(line)
    return "\n".join(cut)


def scan(compiled: re.Pattern[str], text: str, limit: int = MAX_MATCHES) -> list[re.Match[str]]:
    """Return up to *limit* matches of *compiled* in a bounded version of *text*.

    Raises
    ------
    PatternError
        If the text is longer than ``MAX_SCAN_BYTES`` characters.
    """
    prepared = prepare_text(text)
    found: list[re.Match[str]] = []
    for match in compiled.finditer(prepared):
        found.append(match)
        if len(found) >= limit:
            break
    return found


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

    def repeats_inside(sub: Any, guard: str | None) -> bool:
        """Return whether *sub* holds a repeat, able to run more than once, that could also consume *guard*."""
        for operation, argument in sub.data:
            if operation in repeats and argument[1] > 1 and _may_match(argument[2], guard):
                return True
            if any(repeats_inside(child, guard) for child in _children(argument)):
                return True
        return False

    unbounded = {"count": 0, "broad": 0}
    reasons: list[str] = []

    def is_broad(body: Any) -> bool:
        """Return whether a repeated body starts with a wildcard or a negated class (``.``, ``[^"]``)."""
        first = body.data[0] if body.data else None
        if first is None:
            return False
        if first[0] in (_sre_constants.ANY, _sre_constants.NOT_LITERAL):
            return True
        return first[0] == _sre_constants.IN and any(op == _sre_constants.NEGATE for op, _arg in first[1])

    def distinct_literals(alternatives: Any) -> bool:
        """Return whether every alternative starts with a different literal character."""
        starts = []
        for alternative in alternatives:
            first = alternative.data[0] if alternative.data else None
            if first is None or first[0] != _sre_constants.LITERAL:
                return False
            starts.append(first[1])
        return len(set(starts)) == len(starts)

    def walk(sub: Any, inside: bool) -> None:
        """Collect the reasons why repeats in *sub* are unsafe; *inside* means within an unbounded repeat."""
        for operation, argument in sub.data:
            if operation in repeats:
                body = argument[2]
                unlimited = argument[1] >= _UNBOUNDED
                if unlimited:
                    unbounded["count"] += 1
                    unbounded["broad"] += is_broad(body)
                    first = body.data[0] if body.data else None
                    guard = chr(first[1]) if first is not None and first[0] == _sre_constants.LITERAL else None
                    if repeats_inside(body, guard):
                        reasons.append("it repeats a repeated group without a bound, which can take exponential time on a hostile page")
                    if body.getwidth()[0] == 0:
                        reasons.append("it repeats, without a bound, something that can match nothing")
                walk(body, inside or unlimited)
            elif operation == _sre_constants.BRANCH:
                if inside and not distinct_literals(argument[1]):
                    reasons.append("it has alternatives that can overlap inside an unbounded repeat, which can take exponential time")
                for alternative in argument[1]:
                    walk(alternative, inside)
            else:
                for child in _children(argument):
                    walk(child, inside)

    walk(tree, False)

    # Wildcard repeats multiply: a leading ``.*`` with any other unbounded repeat, or many wildcards, are refused.
    first = tree.data[0] if tree.data else None
    leading_broad = first is not None and first[0] in repeats and first[1][1] >= _UNBOUNDED and is_broad(first[1][2])
    if leading_broad and unbounded["count"] > 1:
        reasons.append("it starts with an unbounded wildcard and has more unbounded repeats, which is quadratic on a long line")
    if unbounded["broad"] > _MAX_BROAD:
        reasons.append(f"it has more than {_MAX_BROAD} unbounded wildcard or negated-class repeats")
    if unbounded["count"] > _MAX_UNBOUNDED:
        reasons.append(f"it has more than {_MAX_UNBOUNDED} unbounded repeats")
    return reasons[0] if reasons else None


def _may_match(item: Any, guard: str | None) -> bool:
    """Return whether the repeated *item* can consume the character *guard* (``None`` means any)."""
    if guard is None or _sre_constants is None:
        return True
    code = ord(guard)
    for operation, argument in item.data[:1]:
        if operation == _sre_constants.LITERAL:
            return argument == code
        if operation == _sre_constants.NOT_LITERAL:
            return argument != code
        if operation == _sre_constants.IN:
            return _class_may_match(argument, guard)
    return True


def _class_may_match(members: Any, guard: str) -> bool:
    """Return whether a parsed character class can match *guard*; unknown members count as matching."""
    code = ord(guard)
    negated = False
    hit = False
    for operation, argument in members:
        if operation == _sre_constants.NEGATE:
            negated = True
        elif operation == _sre_constants.LITERAL:
            hit = hit or argument == code
        elif operation == _sre_constants.RANGE:
            hit = hit or argument[0] <= code <= argument[1]
        elif operation == _sre_constants.CATEGORY:
            name = str(argument)
            if "DIGIT" in name:
                matched = guard.isdigit()
            elif "SPACE" in name:
                matched = guard.isspace()
            elif "WORD" in name:
                matched = guard.isalnum() or guard == "_"
            else:
                return True
            hit = hit or (matched != ("NOT" in name))
        else:
            return True
    return hit != negated
