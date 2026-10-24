"""Cover the static regex guard, the cheap first filter in front of the worker process.

Pure functions, no boundary: unsafe shapes are refused with a message, ordinary patterns pass, the text
shrinking is linear, and the in-process reference ``scan`` is bounded in matches. The guard is NOT the
safety property (the worker deadline is): see ``test_regex_runner`` and ``test_regex_guard``. Out of scope:
how providers use the matches (``test_track_providers``).
"""

from __future__ import annotations

import re

import pytest

from gupkg.model.patterns import MAX_RUN, PatternError, compile_pattern, escape_template, prepare_text, scan


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(?P<version>\d+(?:\.\d+)+)", id="dotted-version"),
        pytest.param(r'href="(?P<href>[^"]+)"', id="attribute"),
        pytest.param(r"WinSCP-(?P<version>\d+(?:\.\d+)+)-Portable\.zip\b(?!-)", id="lookahead"),
        pytest.param(r"(?:foo|bar)+(?P<version>\d+)", id="alternation-in-a-repeat"),
        pytest.param(r"(?:\.[0-9]+)+", id="repeat-that-starts-with-a-literal"),
    ],
)
def test_ordinary_patterns_are_accepted(pattern: str) -> None:
    """The shapes the registry's scrapers use compile without complaint."""
    assert compile_pattern(pattern).pattern == pattern


@pytest.mark.parametrize(
    "pattern",
    [
        pytest.param(r"(a+)+", id="nested-plus"),
        pytest.param(r"(\d+)+x", id="nested-digits"),
        pytest.param(r"(\w+\s?)+", id="word-and-space"),
        pytest.param(r"(?:\.[\d.]+)+", id="inner-class-contains-the-leading-literal"),
        pytest.param(r"(.*a){20}x*(.+)*", id="repeat-of-dot-star"),
    ],
)
def test_patterns_that_repeat_a_repeated_group_without_a_bound_are_refused(pattern: str) -> None:
    """The classic exponential shapes are rejected when the manifest is validated."""
    with pytest.raises(PatternError, match="refused"):
        compile_pattern(pattern)


def test_a_pattern_must_define_the_named_groups_it_is_asked_for() -> None:
    """A provider that needs `(?P<version>...)` gets a message naming the missing group."""
    with pytest.raises(PatternError, match=r"\(\?P<version>"):
        compile_pattern(r"\d+", groups=("version",))


@pytest.mark.parametrize("pattern", ["(", "[a-", "x{2,1}", "a" * 2500], ids=["unclosed", "bad-class", "bad-range", "too-long"])
def test_an_invalid_or_oversized_pattern_is_a_pattern_error(pattern: str) -> None:
    """Syntax errors and absurd lengths are reported as PatternError, never as a raw `re.error`."""
    with pytest.raises(PatternError):
        compile_pattern(pattern)


def test_escape_template_escapes_substituted_values_and_keeps_unknown_names() -> None:
    """A capture is inserted as literal text, so it cannot change the pattern's shape; unknown references stay."""
    result = escape_template(r"v${version}-${other}", {"version": "1.2+(x)"})

    assert result == r"v1\.2\+\(x\)-${other}"


def test_text_is_cut_to_bounded_runs_without_touching_ordinary_content() -> None:
    """Long runs of word characters or of one repeated character are shortened; a normal page is unchanged."""
    page = '<a href="/files/tool-1.2.3.zip">tool</a>\n' * 50

    assert prepare_text(page) == page
    assert len(prepare_text("a" * 100_000)) == MAX_RUN
    assert len(prepare_text("12" * 100_000)) == MAX_RUN
    assert len(prepare_text("-" * 100_000)) == MAX_RUN
    assert prepare_text("x" * 10 + " " + "y" * 3000).replace("\n", "") == "x" * 10 + " " + "y" * MAX_RUN


def test_a_page_that_stays_huge_after_the_cuts_is_refused() -> None:
    """More than a mebibyte of varied text is refused instead of scanned."""
    with pytest.raises(PatternError, match="at most"):
        prepare_text("ab cd " * 400_000)


def test_scan_returns_a_bounded_number_of_matches() -> None:
    """A page with endless matches yields at most the limit."""
    found = scan(compile_pattern(r"v\d+"), "v1 " * 50_000, limit=100)

    assert len(found) == 100


REFUSED = [
    pytest.param(r".*(?P<version>\d+\.\d+)", id="leading-dot-star-with-a-version"),
    pytest.param(r".*.*.*x", id="three-leading-wildcards"),
    pytest.param(r"(?:a|a)+b", id="overlapping-alternatives"),
    pytest.param(r"(?:a|aa)+b", id="prefix-alternatives"),
    pytest.param(r"(a?)+b", id="repeat-of-an-optional-group"),
    pytest.param(r".*(?P<version>\d+)zip", id="leading-dot-star-spaces"),
]


@pytest.mark.parametrize("pattern", REFUSED)
def test_patterns_that_are_quadratic_or_exponential_on_a_hostile_page_are_refused_at_validation(pattern: str) -> None:
    """The obviously dangerous shapes get a clear refusal when the manifest is validated (the deadline covers the rest)."""
    with pytest.raises(PatternError, match="refused"):
        compile_pattern(pattern)


def test_inline_and_argument_flags_are_honoured_by_the_shape_check() -> None:
    """`re.I` given as a flag folds case exactly like `(?i)` in the text: a delimiter that differs only in case is refused."""
    assert compile_pattern(r"(?:a+A){1,5}x")
    with pytest.raises(PatternError, match="refused"):
        compile_pattern(r"(?:a+A){1,5}x", flags=re.IGNORECASE)
    with pytest.raises(PatternError, match="refused"):
        compile_pattern(r"(?i)(?:a+A){1,5}x")


def test_the_verbose_flag_is_parsed_with_the_shape_check() -> None:
    """With `re.X` a comment is not pattern text, so `(?:a|a){4}` inside it is not judged; without the flag it is a refused shape."""
    assert compile_pattern("x  # (?:a|a){4}", flags=re.VERBOSE)
    with pytest.raises(PatternError, match="refused"):
        compile_pattern("x  # (?:a|a){4}")
