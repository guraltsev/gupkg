"""Cover how version directory names are recognised and ordered.

Behavior covered: real versions order exactly as in 0.17 (numeric segments, prereleases
before releases, local revisions after the plain version), reserved names (`current`,
`_check_updates`, `data`, anything starting with `.` or `_`) never appear as versions or
as the newest version, and a version merely named `bootstrap2` is ordinary.

Real: the pure ``gupkg.model.version`` functions and the 0.17 comparison kept in
``gupkg.core``. Mocked: nothing. Out of scope: reading directories from disk.
"""

from __future__ import annotations

import pytest

from gupkg.core import compare_package_versions
from gupkg.model.version import (
    compare_versions,
    is_tracker_dir,
    is_version_dir,
    is_version_name,
    newest_version,
    sort_versions,
)


@pytest.mark.parametrize(
    ("older", "newer"),
    [
        pytest.param("1.2.3", "1.10.0", id="numeric-segments"),
        pytest.param("1.0", "1.0.1", id="missing-segment-is-zero"),
        pytest.param("2.0.0-beta.2", "2.0.0", id="prerelease-before-release"),
        pytest.param("2.0.0-beta.2", "2.0.0-beta.10", id="numeric-prerelease"),
        pytest.param("1.2.3", "1.2.3.local.1", id="local-after-plain"),
        pytest.param("1.2.3.local.000002", "1.2.3.local.000010", id="local-revisions"),
        pytest.param("1.9", "2024.05.01", id="calendar-versions"),
    ],
)
def test_real_versions_order_as_in_0_17(older: str, newer: str) -> None:
    """Each pair keeps its 0.17 order, and the legacy comparison function agrees."""
    assert compare_versions(older, newer) == -1
    assert compare_versions(newer, older) == 1
    assert compare_package_versions(older, newer) == -1


def test_equal_versions_compare_equal() -> None:
    """A version equals itself and its zero-padded form."""
    assert compare_versions("1.2", "1.2.0") == 0


def test_reserved_names_never_take_part_in_ordering() -> None:
    """Sorting a package folder's children drops every reserved name and keeps real versions in order."""
    children = ["current", "_check_updates", "data", "_bootstrap", ".gupkg", "1.10.0", "1.2.0", "current.__new__", "Data"]

    assert sort_versions(children) == ["1.2.0", "1.10.0"]
    assert newest_version(children) == "1.10.0"


def test_no_version_means_no_newest_version() -> None:
    """A folder with only reserved children has no newest version."""
    assert newest_version(["_check_updates", "current", "data"]) is None


def test_underscore_bootstrap_is_reserved_but_bootstrap2_is_a_version() -> None:
    """0.17's `_bootstrap` gets no special handling (it starts with `_`), and `bootstrap2` is an ordinary version."""
    assert not is_version_name("_bootstrap")
    assert is_version_name("bootstrap2")
    assert newest_version(["_bootstrap", "bootstrap2", "0.1"]) == "bootstrap2"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param(" 1.0", id="leading-space"),
        pytest.param("1.0.", id="trailing-dot"),
        pytest.param("1:0", id="colon"),
        pytest.param("a/b", id="separator"),
        pytest.param("1.0\x01", id="control-character"),
    ],
)
def test_unsafe_names_are_not_versions(name: str) -> None:
    """Names that cannot be a safe Windows folder name are never versions."""
    assert not is_version_name(name)


def test_version_and_tracker_directories_need_a_manifest() -> None:
    """A directory counts as a version (or tracker) only when it holds pkg.toml."""
    assert is_version_dir("1.2.3", ["pkg.toml", "App"])
    assert not is_version_dir("1.2.3", ["App"])
    assert not is_version_dir("_check_updates", ["pkg.toml"])
    assert is_tracker_dir("_check_updates", ["pkg.toml"])
    assert not is_tracker_dir("_check_updates", [])
    assert not is_tracker_dir("1.2.3", ["pkg.toml"])
