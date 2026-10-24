"""Cover the value validation of ``[track]`` tables, as a manifest author meets it.

Every case parses a tracker ``pkg.toml`` through ``parse_manifest`` (the public entry), so the
test sees the same error text a package author sees from ``gupkg pkg check``. Covers regular
expressions, JSONPath, templates, choices and locations for each provider. Out of scope:
running the providers (``test_track_providers``).
"""

from __future__ import annotations

import pytest

from gupkg.model.manifest import ManifestError, ManifestKind, parse_manifest


def parse(track: str, source: str = '[source]\ndownload_url = "${download_url}"\n'):
    """Parse a tracker made of the given `[track]` text and `[source]` text."""
    return parse_manifest(f"format = 2\n{track}\n{source}", ManifestKind.TRACKER)


@pytest.mark.parametrize(
    "track",
    [
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(?P<version>\\\\d+)"', id="page"),
        pytest.param('[track]\nprovider = "github"\nrepo = "o/r"\nasset = "t-${version}.zip"', id="github"),
        pytest.param('[track]\nprovider = "github"\nrepo_url = "https://github.com/o/r.git"', id="github-url"),
        pytest.param('[track]\nprovider = "gitlab"\nrepo = "https://gitlab.com/g/p"', id="gitlab-url"),
        pytest.param('[track]\nprovider = "json"\ncheck_url = "https://e.org/v.json"\nversion = "$.a"', id="json"),
        pytest.param('[track]\nprovider = "last_modified"\ncheck_url = "https://e.org/f.zip"\nformat = "%Y%m%d"', id="last-modified"),
        pytest.param('[track]\nprovider = "git"\nrepo_url = "https://github.com/o/r"\nref = "refs/heads/main"', id="git"),
    ],
)
def test_valid_tracks_are_accepted(track: str) -> None:
    """One minimal valid `[track]` of each kind passes."""
    assert parse(track).track is not None


@pytest.mark.parametrize(
    ("track", "message"),
    [
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(unclosed"',
                     "not a valid regular expression", id="page-regex-does-not-compile"),
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(a+)+b"',
                     "repeats a repeated group", id="page-regex-is-exponential"),
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "\\\\d+"',
                     "must define \\(\\?P<version>|pattern must define", id="page-without-a-version-group"),
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(?P<version>\\\\d+)"\nselect = "newest"',
                     "must be one of max, first, unique", id="page-select"),
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(?P<version>\\\\d+)"\ndownload = "${nope}"',
                     "refers to \\$\\{nope\\}", id="page-download-names-an-unknown-capture"),
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(?P<version>\\\\d+)"\n[track.headers]\n"Bad Name" = "x"',
                     "not a valid header name", id="page-header-name"),
        pytest.param('[track]\nprovider = "page"\ncheck_url = "https://e.org/"\nmatch = "(?P<version>\\\\d+)"\n[track.headers]\nX-A = "a\\nb"',
                     "control character", id="page-header-injection"),
        pytest.param('[track]\nprovider = "github"\nrepo = "not a repo"', "owner/name", id="github-repo"),
        pytest.param('[track]\nprovider = "github"\nrepo = "o/r"\nasset = "a"\nasset_pattern = "b"', "not both", id="github-asset-and-pattern"),
        pytest.param('[track]\nprovider = "github"\nrepo = "o/r"\nversion_from = "asset"\nasset = "a"',
                     "needs an asset_pattern", id="github-version-from-asset-needs-a-pattern"),
        pytest.param('[track]\nprovider = "github"\nrepo = "o/r"\nprerelease = true', "newest-matching", id="github-prerelease-with-latest"),
        pytest.param('[track]\nprovider = "github"\nrepo = "o/r"\nrelease = "oldest"', "must be one of latest, newest-matching", id="github-release"),
        pytest.param('[track]\nprovider = "json"\ncheck_url = "https://e.org/v.json"\nversion = "$..a"', "expected a key|unsupported|not a valid", id="json-recursive-path"),
        pytest.param('[track]\nprovider = "json"\ncheck_url = "https://e.org/v.json"\nversion = "$.a"\nfilter = "stable"',
                     "is not a condition", id="json-filter-without-an-operator"),
        pytest.param('[track]\nprovider = "json"\ncheck_url = "https://e.org/v.json"', "version.*is required", id="json-without-version"),
        pytest.param('[track]\nprovider = "last_modified"\ncheck_url = "https://e.org/f"\nformat = "%A %d"', "allows only", id="last-modified-directive"),
        pytest.param('[track]\nprovider = "last_modified"\ncheck_url = "https://e.org/f"\nformat = "../%Y"', "cannot appear in a version", id="last-modified-literal"),
        pytest.param('[track]\nprovider = "git"\nrepo_url = "https://github.com/o/r"\nref = "main"', "full refs/", id="git-short-ref"),
        pytest.param('[track]\nprovider = "git"\nrepo_url = "-oProxyCommand=evil"', "https://, ssh://|must be", id="git-url-looks-like-an-option"),
        pytest.param('[track]\nprovider = "index"\ncheck_url = "https://e.org/"', "at least one", id="index-without-levels"),
        pytest.param('[track]\nprovider = "index"\ncheck_url = "https://e.org/"\n[[track.level]]\nmatch = "(?P<version>\\\\d+)"\n[[track.level]]\nmatch = "x"',
                     "next.*is required", id="index-level-without-next"),
        pytest.param('[track]\nprovider = "chain"', "at least one", id="chain-without-steps"),
        pytest.param('[track]\nprovider = "chain"\n[[track.fetch]]\nurl = "${later}"\nmatch = "(?P<version>x)"\n[[track.fetch]]\nurl = "https://e.org/"\nmatch = "(?P<later>x)"',
                     "refers to \\$\\{later\\}", id="chain-step-uses-a-capture-from-a-later-step"),
    ],
)
def test_invalid_values_are_reported_with_the_key_and_the_reason(track: str, message: str) -> None:
    """A bad pattern, path, template, choice or location is a validation error that names the key."""
    with pytest.raises(ManifestError, match=message) as raised:
        parse(track)

    assert any(problem.startswith("track") for problem in raised.value.problems)


def test_all_problems_of_one_track_are_listed_together() -> None:
    """A track with two mistakes reports both, so the author fixes them in one pass."""
    with pytest.raises(ManifestError) as raised:
        parse('[track]\nprovider = "github"\nrepo = "bad"\nrelease = "oldest"')

    assert len(raised.value.problems) >= 2


def test_source_headers_must_be_plain_text_and_need_a_download_url() -> None:
    """`[source.headers]` is checked like `[track.headers]` and is refused without a download address to send them to."""
    with pytest.raises(ManifestError, match="control character"):
        parse_manifest('format = 2\n[source]\ndownload_url = "https://e.org/t.zip"\n[source.headers]\nX = "a\\r\\nb"\n')
    with pytest.raises(ManifestError, match="needs download_url"):
        parse_manifest('format = 2\n[source]\nshipped = true\n[source.headers]\nX = "a"\n')
