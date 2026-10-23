"""Cover the regex guard against the registry's own patterns and a reviewer's matrix of hostile pages.

The 50 distinct regular expressions that the 0.17 registry hooks use (extracted once into
``tests/fixtures/registry_patterns.json``) must all pass validation. Twenty plausible patterns are
scanned over hostile one-mebibyte pages in a child interpreter with a hard timeout: each pair is
refused at validation, refused by the scan budget, or finishes quickly; a hang fails the test by the
child's timeout. The guard is a mitigation and not a bound (see ``gupkg.model.patterns``); these tests
pin what it does achieve. Out of scope: how providers use the matches.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gupkg.model.patterns import PatternError, compile_pattern, prepare_text, scan

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


PATTERNS = [
    r"(?P<version>\d+(?:\.\d+)*)-setup\.exe",
    r"(?P<version>\d+(?:\.\d+)+)\.win64\.zip",
    r'href="(?P<u>[^"]+)"[^>]*>[^<]*download',
    r"(?P<n>[A-Za-z]+(?:-[A-Za-z]+)*)-win64\.zip",
    r"(?P<v>[\d.]+)-(?P<w>[\d.]+)-end",
    r"(?P<v>[\w.-]+)@(?P<w>[\w.-]+)\.zip",
    r"(?P<v>\d+(?:\.\d+)+)(?!\.zip)-",
    r"(?P<v>\d{1,2000}(?:\.\d{1,5}){1,900})-q",
    r"(?P<a>.*)=(?P<b>.*)!",
    r".*(?P<version>\d+\.\d+)",
    r'<a[^>]*href="([^"]*)"[^>]*>(?:[^<]|<(?!/a))*</a>',
    r"(?P<version>(?:\d+\.)+\d+)$",
    r".*.*.*x",
    r"(?:a|a)+b",
    r"(?:a|aa)+b",
    r"Download\s+7-Zip\s+(?P<version>\d+(?:\.\d+)+)\s*\([^)]*\)\s*for\s+Windows\s+x64",
    r"(?P<version>\d+(?:\.\d+)+)",
    r'href="(?P<h>[^"]+)"',
    r"v(?P<version>\d+\.\d+\.\d+)",
    r"(?P<v>[0-9a-f]{64})",
]
PAGES = {
    "dots-8000": '("1." * 4000 + "\\n") * 140',
    "href-open": '"<a href=\\"" * 110_000',
    "a-run": '"a" * (1 << 20)',
    "dashes-dots": '("1.1.1.1.1.1.-" * 70 + "\\n") * 1000',
    "equals-lines": '("=" * 1000 + "\\n") * 1000',
    "at-words": '("a.b-c@" * 140 + "\\n") * 1000',
    "tag-soup": '("<a href=\\"x\\" class=\\"y\\" " * 40 + "\\n") * 1000',
}
BOUND_SECONDS = 2.0


@pytest.mark.parametrize("pattern", [pytest.param(pattern, id=f"{index:02d}") for index, pattern in enumerate(PATTERNS)])
def test_a_plausible_pattern_on_every_hostile_page_is_refused_or_finishes_in_about_a_second(pattern: str) -> None:
    """Refused at validation, or each of seven megabyte-scale hostile pages is scanned or refused in under two seconds."""
    code = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(SOURCE)!r})
        from gupkg.model.patterns import PatternError, compile_pattern, scan
        try:
            compiled = compile_pattern({pattern!r})
        except PatternError:
            print("refused")
            sys.exit(0)
        for name, expression in {PAGES!r}.items():
            page = eval(expression)
            started = time.monotonic()
            try:
                scan(compiled, page)
            except PatternError:
                pass
            print(name, round(time.monotonic() - started, 2))
        """
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    lines = done.stdout.split()
    assert lines == ["refused"] or all(float(value) < BOUND_SECONDS for value in lines[1::2])


def test_the_scan_budget_refuses_a_page_that_is_too_costly_for_an_accepted_pattern() -> None:
    """An accepted pattern that is slow on a hostile page raises PatternError after the budget instead of running on."""
    compiled = compile_pattern(r"(?P<v>[\w.-]+)@(?P<w>[\w.-]+)\.zip")
    page = ("1.1.1.1.1.1.-" * 78 + "\n") * 1000

    with pytest.raises(PatternError, match="took longer than 0.2 seconds"):
        scan(compiled, page, budget=0.2)


def test_a_match_that_runs_over_a_window_boundary_is_found_once() -> None:
    """Two-line matches placed all over a multi-window page are all found, none twice (the windows overlap)."""
    lines = []
    for index in range(300):
        lines += [f"key{index}", f"value{index}", "x" * (40 + index % 37)]
    page = "\n".join(lines) + "\n"

    found = scan(compile_pattern(r"key(?P<n>\d+)\nvalue(?P=n)\n"), page)

    assert [match["n"] for match in found] == [str(index) for index in range(300)]
    assert len(page) > 5 * 4096


def test_a_match_that_starts_exactly_at_the_last_line_of_a_window_is_found_and_matches_never_overlap() -> None:
    """Lines of 100 characters put the window end after line 41: a pair of lines straddles it; pairs are not found twice."""
    line = "a" * 99 + "\n"
    straddling = "".join([line] * 40) + "key0" + "x" * 110 + "\nvalue0\n" + "".join([line] * 10)

    assert [match["n"] for match in scan(compile_pattern(r"key(?P<n>\d+)x*\nvalue(?P=n)\n"), straddling)] == ["0"]
    assert len(scan(compile_pattern(r"[a-z]+\n[a-z]+\n"), line * 200)) == 100


def test_a_long_line_is_cut_at_one_kibibyte_and_the_cut_keeps_the_matches_inside_each_piece() -> None:
    """A 5000-character line of tags becomes lines of at most 1 KiB, and a tag on each piece is still found."""
    page = "<b>item</b>" * 500

    prepared = prepare_text(page)

    assert max(len(line) for line in prepared.split("\n")) <= 1024 and prepared.count("\n") >= 4
    assert len(scan(compile_pattern(r"<b>item</b>"), page)) >= 490


def test_matches_are_found_across_windows_and_ordinary_pages_are_unchanged() -> None:
    """A page much larger than one window still yields every match, in order."""
    page = "".join(f"<li>tool-1.{index}.zip</li>\n" for index in range(5000))

    found = scan(compile_pattern(r"tool-(?P<v>\d+\.\d+)\.zip"), page)

    assert [match["v"] for match in found] == [f"1.{index}" for index in range(5000)]
