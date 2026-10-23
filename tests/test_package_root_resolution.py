"""Cover how the 0.17 commands resolve a user-supplied path to one package version.

Behavior covered: a package root named like a version prefix (``volta``, ``vlc``) resolves to
its version directory; a package root with no ``current`` resolves to its newest version
directory however many it holds (a root with two versions, or with ``_check_updates`` beside
them, no longer fails with "multiple version directories"); a valid ``current`` wins; a
``current`` that is a plain folder is refused, not reinterpreted.

Real: package roots and ``current`` junctions built under pytest's temporary folder, resolved
through the catalog (``gupkg.layout.identity_from_path`` is the 0.17 adapter over it).
Mocked: the machine boundary is ``FakeSystem``, whose links are real. Out of scope: the
catalog's own states (``test_catalog_*``) and installing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.layout import identity_from_path


def make_version(root: Path, name: str) -> Path:
    """Create ``<root>/<name>/pkg.toml`` and return the version directory."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text('format = 2\n[source]\ndownload_url = "https://example.org/x.zip"\n', encoding="utf-8")
    return folder


@pytest.mark.parametrize("root_name", ["volta", "vlc"])
def test_v_prefixed_package_root_resolves_to_its_bootstrap_version(
    tmp_path: Path, root_name: str, system
) -> None:
    """A package root named like ``volta`` resolves to its only version directory (0.17's template folder)."""
    root = tmp_path / root_name
    (root / "_bootstrap").mkdir(parents=True)

    identity, from_current = identity_from_path(root, system)

    assert identity.name == root_name
    assert identity.version_path == root / "_bootstrap"
    assert not from_current


def test_package_root_with_two_versions_and_no_current_resolves_to_the_newest(tmp_path: Path, system) -> None:
    """Two version directories and no ``current`` resolve to the newest one instead of failing."""
    root = tmp_path / "tool"
    make_version(root, "1.0")
    newest = make_version(root, "1.10")
    make_version(root, "1.2")

    identity, from_current = identity_from_path(root, system)

    assert identity.version_path == newest
    assert not from_current


def test_tracker_and_bootstrap_beside_versions_never_win(tmp_path: Path, system) -> None:
    """``_check_updates`` and 0.17's ``_bootstrap`` are reserved and are never chosen as the version."""
    root = tmp_path / "tool"
    make_version(root, "1.0")
    make_version(root, "_check_updates")
    (root / "_bootstrap").mkdir()
    (root / "_bootstrap" / "pkg.toml").write_text("", encoding="utf-8")

    identity, _ = identity_from_path(root, system)

    assert identity.version_string == "1.0"


def test_valid_current_wins_over_a_newer_directory(tmp_path: Path, system) -> None:
    """A package root with a valid ``current`` resolves to the active version and reports it."""
    root = tmp_path / "tool"
    active = make_version(root, "1.0")
    make_version(root, "2.0")
    system.create_junction(root / "current", active)

    identity, from_current = identity_from_path(root, system)

    assert identity.version_string == "1.0"
    assert identity.is_current and from_current


def test_current_that_is_a_plain_folder_is_an_error(tmp_path: Path, system) -> None:
    """A ``current`` that is a real folder (not a junction) is refused rather than reinterpreted."""
    root = tmp_path / "tool"
    make_version(root, "1.0")
    (root / "current").mkdir()

    with pytest.raises(ValueError, match='"current"'):
        identity_from_path(root, system)
