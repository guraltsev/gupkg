"""Cover the regular-expression guard: unsafe shapes are refused and hostile pages cannot hang a scan.

Pure functions, no boundary. The hostile-input tests run the scan in a child interpreter with a
hard timeout, because Python's ``re`` cannot be interrupted: a regression would otherwise hang
the whole test run instead of failing. Out of scope: how providers use the matches
(``test_track_providers``).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gupkg.model.patterns import MAX_RUN, PatternError, compile_pattern, escape_template, prepare_text, scan

SOURCE = Path(__file__).resolve().parents[1] / "src"


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
    assert prepare_text("x" * 10 + " " + "y" * 3000) == "x" * 10 + " " + "y" * MAX_RUN


def test_a_page_that_stays_huge_after_the_cuts_is_refused() -> None:
    """More than a mebibyte of varied text is refused instead of scanned."""
    with pytest.raises(PatternError, match="at most"):
        prepare_text("ab cd " * 400_000)


def test_scan_returns_a_bounded_number_of_matches() -> None:
    """A page with endless matches yields at most the limit."""
    found = scan(compile_pattern(r"v\d+"), "v1 " * 50_000, limit=100)

    assert len(found) == 100


HOSTILE = [
    pytest.param(r"(?P<version>\d+(?:\.\d+)+)", "'a' * 5_000_000", id="a-5mb-dotted-version"),
    pytest.param(r"(?P<version>\d+(?:\.\d+)+)", "'1' * 5_000_000", id="ones-5mb-dotted-version"),
    pytest.param(r"(?P<version>\d+(?:\.\d+)+)", "'12' * 1_000_000", id="alternating-digits-dotted-version"),
    pytest.param(r'href="(?P<href>[^"]+)"', "'x' * 5_000_000", id="x-5mb-attribute"),
    pytest.param(r"(?P<version>\d+)\.zip", "'-' * 5_000_000", id="dashes-5mb"),
    pytest.param(r".*(?P<version>\d+)zip", "' ' * 4_000_000", id="spaces-leading-dot-star"),
    pytest.param(r"Version: (?P<version>\d+(?:\.\d+)+)", "'Version: ' * 400_000", id="repeated-prefix"),
]


@pytest.mark.parametrize(("pattern", "make"), HOSTILE)
def test_hostile_pages_finish_quickly_under_a_hard_timeout(pattern: str, make: str) -> None:
    """A multi-megabyte page of one repeated pattern is scanned in seconds; a hang fails the test by timeout."""
    code = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(SOURCE)!r})
        from gupkg.model.patterns import PatternError, compile_pattern, scan
        compiled = compile_pattern({pattern!r})
        try:
            print(len(scan(compiled, {make})))
        except PatternError as error:
            print("refused")
        """
    )

    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() in ("0", "refused") or done.stdout.strip().isdigit()
