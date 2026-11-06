"""Compile and run the regular expressions of a ``pkg.toml`` against untrusted text.

Patterns come from administrator-written manifests, but the text they scan comes from the
network. Python's ``re`` backtracks and cannot be interrupted, so **nothing in this module is the
safety property**. The safety property is the deadline of the worker process that runs every
match on untrusted text (``gupkg.services.regexrun`` and ``gupkg.system.regex_worker``): the
worker is killed when the deadline passes and the call fails. This module is a cheap first filter
in front of it: a pattern is refused when its shape is known to blow up (nested or
empty-matching repeats, overlapping alternatives inside a repeat, several wildcards, long runs of
repeats with no literal between them) so that the author gets a clear message at validation time
instead of a timeout later. A pattern that passes can still be slow, and that is expected.

Usage and API
-------------
``compile_pattern(pattern, groups=..., flags=...)`` returns a compiled pattern or raises
``PatternError``; the *groups* must be named groups of the pattern. ``prepare_text(text)`` shrinks
long runs of one character class (the runner applies it before sending a text to the worker).
``scan(compiled, text)`` is the in-process reference implementation, for trusted text and for
tests only: it has no deadline. ``escape_template(pattern, values)`` substitutes ``${name}``
references inside a pattern with escaped values.

Implementation Approach
-----------------------
Validation walks the parsed pattern tree (with the flags, so ``(?i)`` and ``re.X`` are honoured):
nested and empty-matching repeats, alternation inside a repeat, ``.``-style repeats, and the
longest run of unbounded repeats not separated by a required literal are measured, comparing the
character sets that two parts of the pattern can consume. Preparation cuts word runs and
single-character runs to ``MAX_RUN``. The module is pure: no input or output.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from itertools import groupby, islice
from typing import Any

try:  # Python 3.11 and later
    from re import _constants as _sre_constants
    from re import _parser as _sre_parser
except ImportError:  # pragma: no cover - the bundled runtime is 3.12
    _sre_constants = None
    _sre_parser = None

# The longest pattern text that is accepted.
MAX_PATTERN_LENGTH = 2000
# The longest text (in characters, after preparation) that one scan accepts.
MAX_SCAN_BYTES = 1024 * 1024
# Longest run of word characters or of one repeated character kept by ``prepare_text``.
MAX_RUN = 1024
# Default cap on the matches ``scan`` returns.
MAX_MATCHES = 10_000
# A repeat whose upper bound reaches this counts as unbounded.
_UNBOUNDED = 1000
_MULTI = 4  # a repeat that can run this many times or more gets the nesting and alternation checks
# Most unbounded wildcard or negated-class repeats a pattern may hold.
_MAX_BROAD = 3
# Most unbounded repeats of any kind a pattern may hold.
_MAX_UNBOUNDED = 24
# Most unbounded repeats in a row with no required literal between them.
_MAX_STREAK = 6

# A ``${name}`` reference inside a pattern template.
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

    Examples
    --------
    Basic usage:

    >>> compiled = compile_pattern(r"v(?P<version>[0-9]+(?:[.][0-9]+)*)", groups=("version",))
    >>> compiled.search("release v1.2.3").group("version")
    '1.2.3'

    Edge cases:

    >>> compile_pattern("v[0-9]+", groups=("version",))
    Traceback (most recent call last):
        ...
    gupkg.model.patterns.PatternError: 'v[0-9]+' must define the named group(s) (?P<version>...)
    >>> compile_pattern("(unclosed")  # doctest: +ELLIPSIS
    Traceback (most recent call last):
        ...
    gupkg.model.patterns.PatternError: '(unclosed' is not a valid regular expression: ...
    """
    # Refuse over-long text before the compiler sees it, then compile.
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise PatternError(f"the pattern is longer than {MAX_PATTERN_LENGTH} characters")
    try:
        compiled = re.compile(pattern, flags)
    except (re.error, RecursionError, OverflowError) as error:
        raise PatternError(f"{pattern!r} is not a valid regular expression: {error}") from error
    # Require the named groups the caller depends on.
    missing = [name for name in groups if name not in compiled.groupindex]
    if missing:
        raise PatternError(f"{pattern!r} must define the named group(s) {', '.join('(?P<%s>...)' % n for n in missing)}")
    # Finally apply the shape filter that refuses patterns known to blow up.
    reason = _unsafe_reason(pattern, flags)
    if reason is not None:
        raise PatternError(f"{pattern!r} is refused: {reason}")
    return compiled


def prepare_text(text: str) -> str:
    """Return *text* with every over-long run of word characters or of one repeated character cut to ``MAX_RUN``.

    Both cuts are linear in the length of the text.

    Parameters
    ----------
    text : str
        The untrusted text to shrink.

    Returns
    -------
    str
        The text with no run longer than ``MAX_RUN``.

    Raises
    ------
    PatternError
        If the text, after the cuts, is longer than ``MAX_SCAN_BYTES`` characters.

    Examples
    --------
    Basic usage:

    >>> prepare_text("v1.2.3")
    'v1.2.3'

    Edge cases:

    >>> len(prepare_text("a" * 10_000)) == MAX_RUN
    True
    """
    # Cut word runs with a pattern that can only start at the beginning of a run.
    text = _WORD_RUN.sub(lambda match: match.group(0)[:MAX_RUN], text)
    # Find runs of one repeated character by substring search first, and collapse them only when present.
    needle_hits = [character for character in set(text) if character * MAX_RUN in text]
    if needle_hits:
        pieces: list[str] = []
        for character, group in groupby(text):
            count = sum(1 for _ in group)
            pieces.append(character * min(count, MAX_RUN))
        text = "".join(pieces)
    # Refuse a text that is still too long after the cuts.
    if len(text) > MAX_SCAN_BYTES:
        raise PatternError(f"the text is {len(text)} characters long; at most {MAX_SCAN_BYTES} are scanned")
    return text


def scan(compiled: re.Pattern[str], text: str, limit: int = MAX_MATCHES) -> list[re.Match[str]]:
    """Return up to *limit* matches of *compiled* in the prepared *text*, in process and with no deadline.

    This is the reference the worker process is tested against and a convenience for trusted text.
    Untrusted text goes through ``gupkg.services.regexrun.RegexRunner`` instead, because ``re``
    cannot be interrupted.

    Parameters
    ----------
    compiled : re.Pattern
        A pattern from ``compile_pattern``.
    text : str
        The text to scan; it is prepared with ``prepare_text`` first.
    limit : int
        The most matches to return.

    Returns
    -------
    list of re.Match
        The matches, in order.

    Raises
    ------
    PatternError
        If the text is longer than ``MAX_SCAN_BYTES`` characters after preparation.

    Examples
    --------
    Basic usage:

    >>> compiled = compile_pattern(r"v(?P<version>[0-9]+(?:[.][0-9]+)*)")
    >>> [match.group("version") for match in scan(compiled, "v1.2 and v3.4.5")]
    ['1.2', '3.4.5']
    >>> [match.group("version") for match in scan(compiled, "v1.2 and v3.4.5", limit=1)]
    ['1.2']
    """
    return list(islice(compiled.finditer(prepare_text(text)), limit))


def escape_template(pattern: str, values: Mapping[str, str]) -> str:
    """Replace ``${name}`` in a pattern with the ``re.escape`` of its value.

    Parameters
    ----------
    pattern : str
        The pattern text holding ``${name}`` references.
    values : Mapping
        The value of each name; a name that is not present stays as written.

    Returns
    -------
    str
        The pattern with the known references substituted.

    Examples
    --------
    Basic usage:

    >>> escape_template("tool-${version}.zip", {"version": "1.2+b"})
    'tool-1\\\\.2\\\\+b.zip'

    Edge cases:

    >>> escape_template("${unknown}-${version}", {"version": "1"})
    '${unknown}-1'
    """

    def _replace(match: re.Match[str]) -> str:
        """Return the escaped value of one reference, or the reference itself."""
        name = match.group(1)
        return re.escape(str(values[name])) if name in values else match.group(0)

    # Substitute every reference through the escaping callback.
    return _REFERENCE.sub(_replace, pattern)


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


def _unsafe_reason(pattern: str, flags: int = 0) -> str | None:
    """Return why the shape of *pattern* is unsafe, or ``None`` (``None`` also when it cannot be analysed)."""
    # Without the parser internals, or for text the parser rejects, there is nothing to analyse.
    if _sre_parser is None or _sre_constants is None:
        return None
    try:
        tree = _sre_parser.parse(pattern, flags)
    except (re.error, RecursionError):
        return None
    # The repeat operations, and whether ``(?i)`` makes character sets compare case-insensitively.
    repeats = (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT, getattr(_sre_constants, "POSSESSIVE_REPEAT", None))

    # The local helpers below share ``repeats`` and ``fold``: they compare the characters that
    # parts of the pattern can consume, and ``walk`` collects the reasons a repeat is unsafe.
    fold = bool(getattr(getattr(tree, "state", None), "flags", 0) & re.IGNORECASE)

    # Character-set comparison.
    def _disjoint(left: frozenset[str], right: frozenset[str]) -> bool:
        """Return whether no character is in both sets (case-insensitively under ``(?i)``)."""
        if fold:
            left, right = _fold(left), _fold(right)
        return left.isdisjoint(right)

    # First-character analysis, used to tell whether alternatives can overlap.
    def _first_chars(sub: Any) -> tuple[frozenset[str], bool]:
        """Return the characters *sub* can start with and whether it can match the empty string."""
        # Walk the items in order; zero-width items are skipped and the first required item ends the scan.
        chars: set[str] = set()
        for item in _flatten(sub):
            operation, argument = item
            if operation in (_sre_constants.AT, _sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
                continue
            # A repeat contributes its body's first characters and lets the scan continue when it can be skipped.
            if operation in repeats:
                inner, nullable = _first_chars(argument[2])
                chars |= inner
                if argument[0] == 0 or nullable:
                    continue
                return frozenset(chars), False
            # A branch contributes every alternative's first characters and continues only if one can be empty.
            if operation == _sre_constants.BRANCH:
                nullable = False
                for alternative in argument[1]:
                    inner, empty = _first_chars(alternative)
                    chars |= inner
                    nullable = nullable or empty
                if nullable:
                    continue
                return frozenset(chars), False
            # Any other element is required: its characters end the scan.
            chars |= _item_chars(item)
            return frozenset(chars), False
        return frozenset(chars), True

    # Alternatives that start differently cannot overlap.
    def _distinct_starts(alternatives: Any) -> bool:
        """Return whether the alternatives start with pairwise different characters and none can be empty."""
        seen: frozenset[str] = frozenset()
        for alternative in alternatives:
            chars, nullable = _first_chars(alternative)
            if nullable or not _disjoint(seen, chars):
                return False
            seen |= chars
        return True

    # Ambiguity analysis of repeated groups that contain repeats.
    def _inner_repeat_chars(sub: Any) -> list[frozenset[str]]:
        """Return, for the repeats directly in *sub*, the characters each can consume (optional wrappers are looked through)."""
        found: list[frozenset[str]] = []
        for operation, argument in _flatten(sub):
            # A repeat of exactly one pass is looked through to the repeats it wraps.
            if operation in repeats:
                deeper = _inner_repeat_chars(argument[2]) if argument[1] == 1 else []
                found += deeper or [_sub_chars(argument[2])]
            elif operation == _sre_constants.BRANCH:
                # Each alternative of a branch may hold repeats of its own.
                for alternative in argument[1]:
                    found += _inner_repeat_chars(alternative)
        return found

    def _ambiguous(body: Any) -> bool:
        """Return whether iterations of a repeated *body* with repeats inside can be split in many ways.

        It is safe only when the repeats inside consume pairwise different characters and the body begins
        or ends with a required single character that none of them can consume (``\\.`` in
        ``(?:\\d{1,3}\\.){1,5}``, ``[._]`` in ``(?:[._]\\d+)*``): the delimiter fixes where each iteration ends.
        """
        # Inner repeats that compete for the same characters are ambiguous at once.
        inner = _inner_repeat_chars(body)
        if not inner:
            return False
        if any(not _disjoint(inner[i], inner[j]) for i in range(len(inner)) for j in range(i + 1, len(inner))):
            return True
        # Otherwise a required single-character delimiter at an edge must fix where each iteration ends.
        union = frozenset().union(*inner)
        # The first and last elements of the body are the places such a delimiter can stand.
        items = list(_flatten(body))
        edges = (items[0], items[-1]) if items else ()
        singles = (_sre_constants.LITERAL, _sre_constants.IN, _sre_constants.NOT_LITERAL)
        return not any(item[0] in singles and _disjoint(_item_chars(item), union) for item in edges)

    # Tallies and findings that ``walk`` fills in and the checks after it read.
    unbounded = {"count": 0, "broad": 0, "any": 0}
    reasons: list[str] = []

    # Traversal of the parsed tree.
    def _flatten(sub: Any):
        """Yield the items of *sub* in order, entering groups (capturing or not) in place."""
        # Groups are transparent for analysis: yield their contents in place of the group.
        for item in sub.data:
            operation, argument = item
            if operation == _sre_constants.SUBPATTERN:
                yield from _flatten(argument[3])
            elif operation == getattr(_sre_constants, "ATOMIC_GROUP", None):
                yield from _flatten(argument)
            # Everything else is a leaf of the analysis.
            else:
                yield item

    # Classification of wildcard repeats.
    def _is_any(body: Any) -> bool:
        """Return whether a repeated body starts with ``.`` or a class that matches everything."""
        # Only the first element of the body decides whether it is a wildcard.
        first = next(_flatten(body), None)
        if first is None:
            return False
        if first[0] == _sre_constants.ANY:
            return True
        # A class holding both a category and its negation matches every character.
        if first[0] == _sre_constants.IN:
            names = {str(argument) for operation, argument in first[1] if operation == _sre_constants.CATEGORY}
            return any(
                f"CATEGORY_{kind}" in names and f"CATEGORY_NOT_{kind}" in names for kind in ("SPACE", "DIGIT", "WORD")
            )
        return False

    # Classification of negated-class repeats.
    def _is_broad(body: Any) -> bool:
        """Return whether a repeated body starts with a wildcard or a negated class (``.``, ``[^"]``)."""
        # Only the first element of the body decides whether it is broad.
        first = next(_flatten(body), None)
        if first is None:
            return False
        # ``.`` and a negated single character are broad, and so is any negated class.
        if first[0] in (_sre_constants.ANY, _sre_constants.NOT_LITERAL):
            return True
        return first[0] == _sre_constants.IN and any(op == _sre_constants.NEGATE for op, _arg in first[1])

    # Measurement of adjacent unbounded repeats.
    def _streak(sub: Any) -> int:
        """Return the most unbounded repeats in a row, with no required literal between, inside *sub*.

        A repeat counts for one plus the streak inside its body, so a dot-and-digits group repeated without a bound is two."""
        # A required literal resets the streak; repeats and branches extend it.
        best = current = 0
        for operation, argument in _flatten(sub):
            # A repeat counts itself (when unbounded) plus the streak inside its body.
            if operation in repeats:
                inner = _streak(argument[2])
                current += (1 if argument[1] >= _UNBOUNDED else 0) + inner
            elif operation == _sre_constants.LITERAL:
                current = 0
            # A branch adds the worst of its alternatives.
            elif operation == _sre_constants.BRANCH:
                current += max((_streak(alternative) for alternative in argument[1]), default=0)
            best = max(best, current)
        return best

    # The main traversal that records why a pattern is unsafe.
    def _walk(sub: Any, inside: bool) -> None:
        """Collect the reasons why repeats in *sub* are unsafe; *inside* means within a repeat of four or more."""
        for operation, argument in sub.data:
            if operation in repeats:
                # Count unbounded repeats and flag nested or empty-matching ones.
                body = argument[2]
                multi = argument[1] >= _MULTI
                if argument[1] >= _UNBOUNDED:
                    unbounded["count"] += 1
                    unbounded["broad"] += _is_broad(body)
                    unbounded["any"] += _is_any(body)
                # Repeats that can run many times must not be ambiguous or able to match nothing.
                if multi and _ambiguous(body):
                    reasons.append("it repeats a repeated group that can be split in many ways, which can take exponential time on a hostile page")
                if multi and body.getwidth()[0] == 0:
                    reasons.append("it repeats something that can match nothing, which can take exponential time on a hostile page")
                _walk(body, inside or multi)
            elif operation == _sre_constants.BRANCH:
                # Alternatives inside a repeat must start differently, or they may overlap exponentially.
                if inside and not _distinct_starts(argument[1]):
                    reasons.append("it has alternatives that can overlap inside a repeat, which can take exponential time")
                for alternative in argument[1]:
                    _walk(alternative, inside)
            # Any other element may still hold sub-patterns (lookarounds, groups).
            else:
                for child in _children(argument):
                    _walk(child, inside)

    # Collect the per-repeat findings, then apply the whole-pattern limits below.
    _walk(tree, False)

    # Wildcards multiply: ``.*`` is allowed only as the one unbounded repeat of the pattern, a
    # leading negated class with other unbounded repeats is refused, and so are many wildcards,
    # long runs of repeats with no literal between them, and very many repeats.
    first = next(_flatten(tree), None)
    leading_broad = first is not None and first[0] in repeats and first[1][1] >= _UNBOUNDED and _is_broad(first[1][2])
    if unbounded["any"] and unbounded["count"] > unbounded["any"]:
        reasons.append("it has an unbounded '.' repeat next to other unbounded repeats, which is quadratic or worse on a long line")
    if unbounded["any"] > 1:
        reasons.append("it has more than one unbounded '.' repeat, which is cubic or worse on a long line")
    if leading_broad and unbounded["count"] > 1:
        reasons.append("it starts with an unbounded wildcard and has more unbounded repeats, which is quadratic on a long line")
    if unbounded["broad"] > _MAX_BROAD:
        reasons.append(f"it has more than {_MAX_BROAD} unbounded wildcard or negated-class repeats")
    # Streaks of adjacent unbounded repeats and the total count are bounded as well.
    if _streak(tree) > _MAX_STREAK:
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
    # Test every alphabet character against the members; a member of unknown kind matches everything.
    negated = any(operation == _sre_constants.NEGATE for operation, _argument in members)
    matching: set[str] = set()
    for char in _ALPHABET:
        hit = False
        for operation, argument in members:
            # A character matches when any member (literal, range or category) matches it.
            if operation == _sre_constants.LITERAL:
                hit = hit or ord(char) == argument
            elif operation == _sre_constants.RANGE:
                hit = hit or argument[0] <= ord(char) <= argument[1]
            elif operation == _sre_constants.CATEGORY:
                hit = hit or _category_matches(str(argument), char)
            elif operation != _sre_constants.NEGATE:
                return _ALPHABET
        # A negated class keeps the characters that no member matched.
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
    # Dispatch on the element kind: single characters, classes, groups, branches, repeats, then zero-width items.
    operation, argument = item
    if operation == _sre_constants.LITERAL:
        return frozenset({chr(argument)})
    if operation == _sre_constants.NOT_LITERAL:
        return _ALPHABET - {chr(argument)}
    if operation == _sre_constants.IN:
        return _class_chars(argument)
    # Groups, branches and repeats consume whatever their contents can consume.
    if operation == _sre_constants.SUBPATTERN:
        return _sub_chars(argument[3])
    if operation == _sre_constants.BRANCH:
        return frozenset().union(*(_sub_chars(alternative) for alternative in argument[1]))
    if operation in (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT, getattr(_sre_constants, "POSSESSIVE_REPEAT", None)):
        return _sub_chars(argument[2])
    if operation == getattr(_sre_constants, "ATOMIC_GROUP", None):
        return _sub_chars(argument)
    # Anchors and lookarounds consume nothing; anything unrecognised may consume anything.
    if operation in (_sre_constants.AT, _sre_constants.ASSERT, _sre_constants.ASSERT_NOT):
        return frozenset()
    return _ALPHABET
