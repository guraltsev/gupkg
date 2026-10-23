"""Cover the static regex guard (the cheap first filter) against the registry's own patterns and nasty shapes.

The 50 distinct regular expressions that the 0.17 registry hooks use (extracted once into
``tests/fixtures/registry_patterns.json``) must all pass validation. The refusals pin each rule: wildcards
hidden in groups, bounded repeats that can blow up (checked in a child with a hard timeout, so a
regression fails instead of hanging), delimiters compared by character sets, alternatives that start
alike. The guard is not the safety property, the worker deadline is (``test_regex_runner``). Out of
scope: how providers use the matches.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gupkg.model.patterns import PatternError, compile_pattern

SOURCE = Path(__file__).resolve().parents[1] / "src"
REAL = json.loads((Path(__file__).resolve().parent / "fixtures" / "registry_patterns.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("row", [pytest.param(row, id=f"{index:02d}-{row['source']}") for index, row in enumerate(REAL)])
def test_every_regular_expression_of_the_real_registry_passes_validation(row: dict[str, str]) -> None:
    """A pattern that a real registry hook uses today is not refused by the guard."""
    assert compile_pattern(row["pattern"]).pattern == row["pattern"]


def test_the_real_pattern_list_is_the_one_the_registry_has() -> None:
    """The fixture holds the 50 distinct patterns of the 62 committed registry hooks."""
    assert len(REAL) == 50 and len({row["pattern"] for row in REAL}) == 50


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?P<a>.*)=(?P<b>.*)!", id="two-named-dot-stars"),
        pytest.param(r"(?:.*)=(?:.*)!", id="two-plain-group-dot-stars"),
        pytest.param(r"(?P<a>.*)(?P<b>\d+)", id="named-leading-dot-star-and-digits"),
        pytest.param(r"((?P<a>.*))x(.+)y", id="nested-group-dot-star"),
        pytest.param(r"(?P<a>[^=]*)(?P<b>[^!]*)(?P<c>[^?]*)(?P<d>[^#]*)", id="four-negated-classes"),
        pytest.param(r"(?P<a>[\s\S]*)=(?P<b>\d+)", id="match-everything-class"),
        pytest.param(r"(?P<a>[^>]*)(?P<v>\d+(?:\.\d+)+)", id="leading-negated-class-in-a-group-with-more-repeats"),
    ],
)
def test_wildcards_hidden_in_groups_are_counted_like_bare_ones(pattern: str) -> None:
    """Wrapping `.*` in a named or plain group does not hide it from the wildcard rules."""
    with pytest.raises(PatternError, match="refused"):
        compile_pattern(pattern)


def test_four_negated_class_repeats_are_refused_but_three_pass() -> None:
    """Negated-class repeats separated by literals are allowed up to three; the fourth is refused for that reason alone."""
    assert compile_pattern(r"a[^b]*b[^c]*c[^d]*d")
    with pytest.raises(PatternError, match="wildcard or negated-class"):
        compile_pattern(r"a[^b]*b[^c]*c[^d]*d[^e]*e")


def test_a_streak_of_unbounded_repeats_with_no_literal_between_is_refused() -> None:
    """Seven unbounded repeats in a row (digits, spaces, words) fail the streak rule; a literal between them resets it."""
    with pytest.raises(PatternError, match="in a row with no literal"):
        compile_pattern(r"\d+\s+\w+\s+\d+\s+\w+\s+\d+")
    assert compile_pattern(r"\d+\s+x\w+\s+x\d+\s+x\w+\s+x\d+")


def test_more_than_the_total_number_of_unbounded_repeats_is_refused() -> None:
    """Twenty-five unbounded repeats, each after a literal, exceed the total cap."""
    with pytest.raises(PatternError, match="more than 24 unbounded repeats"):
        compile_pattern("".join(rf"k{index}\d+" for index in range(25)))


BOUNDED_BLOWUPS = [
    pytest.param(r"(?:a?){28}a{28}b", id="optional-in-a-bounded-repeat"),
    pytest.param(r"(?:a|aa){1,60}b", id="overlapping-alternatives-bounded"),
    pytest.param(r"(?:a{1,30}){1,30}b", id="nested-bounded-repeats"),
    pytest.param(r"(?:a|a?){1,40}b", id="empty-alternative-bounded"),
    pytest.param(r"(?:a|a){1,999}b", id="identical-alternatives"),
    pytest.param(r"(a{1,2}){1,200}b", id="capturing-nested-bounded"),
    pytest.param(r"(?:a*?){1,100}b", id="lazy-empty-body"),
    pytest.param(r"(.?){30}.{30}b", id="optional-dots"),
    pytest.param(r"(?:\w+\s?){1,10}z", id="words-and-optional-space"),
    pytest.param(r"(?:\d+\.?){1,10}z", id="digits-and-optional-dot"),
    pytest.param(r"(?:[a-z]+-?){1,8}z", id="letters-and-optional-dash"),
    pytest.param(r"(?:\w+[-.]?){2,12}q", id="words-and-optional-separator"),
    pytest.param(r"x.*y.*z.*w", id="three-dot-stars-between-literals"),
]


@pytest.mark.parametrize("pattern", BOUNDED_BLOWUPS)
def test_a_bounded_repeat_that_can_blow_up_is_refused_at_validation(pattern: str) -> None:
    """Each of these hangs for tens of seconds on a short page of `a` if accepted; a child with a hard timeout proves it is refused."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SOURCE)!r})
        from gupkg.model.patterns import PatternError, compile_pattern, scan
        try:
            compiled = compile_pattern({pattern!r})
        except PatternError:
            print("refused")
            sys.exit(0)
        print("accepted")
        scan(compiled, "a" * 60 + "\\n" + "1." * 30 + "x " * 400)
        """
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)

    assert done.stdout.split() == ["refused"], done.stderr


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?:\d{1,3}\.){1,5}", id="digits-then-a-dot"),
        pytest.param(r"(?P<v>\d+(?:[._]\d+)*)", id="version-with-dot-or-underscore"),
        pytest.param(r"(?:\.\d+){1,8}", id="dot-then-digits-bounded"),
        pytest.param(r"(?:,\s*\d+){2,9}", id="comma-space-digits"),
        pytest.param(r"(?:[a-z]+-){1,8}z", id="letters-then-a-dash"),
        pytest.param(r"(?:a|b|c){1,30}x", id="distinct-alternatives"),
        pytest.param(r"(?i)(?:ab?){1,5}c", id="case-insensitive-delimiter"),
        pytest.param(r"(?:[^,;]+,){1,5}", id="fields-not-containing-the-comma"),
    ],
)
def test_a_repeat_whose_iterations_end_in_a_delimiter_the_inner_repeat_cannot_consume_is_accepted(pattern: str) -> None:
    """The delimiter fixes where an iteration ends, so the character sets of the inner repeat and the delimiter decide."""
    assert compile_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?:\d{1,3}[0-9]){1,5}", id="delimiter-is-also-a-digit"),
        pytest.param(r"(?:[a-z]+[a-c]){1,5}", id="delimiter-class-overlaps-the-inner-class"),
        pytest.param(r"(?i)(?:a+A){1,5}", id="delimiter-differs-only-in-case"),
        pytest.param(r"(?:\d+\s*\d*x){1,5}", id="two-inner-repeats-that-can-both-take-a-digit-or-nothing"),
        pytest.param(r"(?:\d+\d+x)*", id="adjacent-inner-repeats-on-the-same-characters"),
        pytest.param(r"(?:[^,;]+[a-c]){1,5}", id="negated-class-contains-the-delimiter"),
        pytest.param(r"(?:(?=a)){5}b", id="repeated-body-that-matches-nothing"),
    ],
)
def test_a_delimiter_that_the_inner_repeat_can_also_consume_does_not_make_the_repeat_safe(pattern: str) -> None:
    """Comparing character sets, not literals: an overlapping delimiter or inner repeats that compete are refused."""
    with pytest.raises(PatternError, match="refused"):
        compile_pattern(pattern)


def test_more_than_one_unbounded_dot_repeat_is_refused_even_between_literals() -> None:
    """`x.*y.*z.*w` is cubic on a long line; one `.*` stays allowed."""
    with pytest.raises(PatternError, match="more than one unbounded '.' repeat"):
        compile_pattern(r"x.*y.*z.*w")
    assert compile_pattern(r"title=(?P<t>.*)$")


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?:ab|[ac]d){4,}x", id="first-letters-overlap-through-a-class"),
        pytest.param(r"(?:[a-c]x|[b-d]y)+z", id="overlapping-ranges"),
        pytest.param(r"(?:a|[^b]){4}x", id="literal-and-a-negated-class"),
        pytest.param(r"(?:x|(?:y|x)z){1,9}", id="overlap-hidden-in-a-group"),
    ],
)
def test_alternatives_inside_a_repeat_must_start_with_different_characters_and_none_may_be_empty(pattern: str) -> None:
    """Each shape fails only because two alternatives can start with the same character, or one can be empty."""
    with pytest.raises(PatternError, match="alternatives that can overlap"):
        compile_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?:ab|cd){4,}x", id="two-literal-words"),
        pytest.param(r"(?:a|b|[0-9]){4}", id="letters-and-a-digit-class"),
        pytest.param(r"(?:[ab]x|[cd]y){4}z", id="two-classes"),
    ],
)
def test_alternatives_with_disjoint_first_characters_inside_a_repeat_are_accepted(pattern: str) -> None:
    """Deterministic alternatives cannot multiply the ways to match, so they pass the filter."""
    assert compile_pattern(pattern)

