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

from gupkg.model.patterns import PatternError, compile_pattern, scan

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


def test_matches_are_found_across_windows_and_ordinary_pages_are_unchanged() -> None:
    """A page much larger than one window still yields every match, in order."""
    page = "".join(f"<li>tool-1.{index}.zip</li>\n" for index in range(5000))

    found = scan(compile_pattern(r"tool-(?P<v>\d+\.\d+)\.zip"), page)

    assert [match["v"] for match in found] == [f"1.{index}" for index in range(5000)]
