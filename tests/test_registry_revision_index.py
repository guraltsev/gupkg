"""Cover how a registry source naming a revision index selects the archive to download.

Behavior covered: a plain archive URL is used as given; a ``REGISTRY.md``-style
index on raw.githubusercontent.com selects the repository archive of the commit
in its first (newest) row; and other hosts or empty indexes are rejected with a
clear error. Mocked boundary: the HTTP request for the index. Out of scope:
downloading or validating the archive itself.
"""

from __future__ import annotations

import io
from unittest import mock

import pytest

from gupkg.core import ConfigValidationError
from gupkg.registry import OFFICIAL_SOURCE, resolve_archive_source

NEW = "a" * 40
OLD = "b" * 40
INDEX = (
    "# gupkg registry revisions\n\n| Revision | Commit | Date | Notes |\n|---|---|---|---|\n"
    f"| 2 | `{NEW}` | 2026-10-17 | newer |\n| 1 | `{OLD}` | 2026-10-16 | older |\n"
)


def _serve(text: str):
    """Return a urlopen replacement that serves *text* as the index body."""

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return mock.patch("urllib.request.urlopen", return_value=Response(text.encode("utf-8")))


def test_archive_url_is_used_as_given() -> None:
    """A source that is already a ZIP URL is not looked up."""
    url = "https://example.invalid/registry.zip"
    with mock.patch("urllib.request.urlopen") as opened:
        assert resolve_archive_source(url) == url
    opened.assert_not_called()


def test_official_source_selects_the_newest_listed_commit() -> None:
    """The default source downloads the commit in the first table row, not a branch or tag."""
    with _serve(INDEX):
        assert resolve_archive_source(OFFICIAL_SOURCE) == f"https://github.com/guraltsev/pkg/archive/{NEW}.zip"


def test_index_without_a_revision_is_rejected() -> None:
    """An index with no table rows yet fails clearly instead of downloading anything."""
    with _serve("# gupkg registry revisions\n\n| Revision | Commit | Date | Notes |\n|---|---|---|---|\n"):
        with pytest.raises(ConfigValidationError, match="lists no revision"):
            resolve_archive_source(OFFICIAL_SOURCE)


def test_index_on_an_unknown_host_is_rejected() -> None:
    """Only raw.githubusercontent.com indexes are accepted, because only their archive links are known."""
    with pytest.raises(ConfigValidationError, match="raw.githubusercontent.com"):
        resolve_archive_source("https://example.invalid/owner/repo/main/REGISTRY.md")
