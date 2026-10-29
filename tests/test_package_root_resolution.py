"""Cover how a user-supplied path resolves to one package version.

Behavior covered: a package root named like a version prefix (``volta``, ``vlc``) resolves to its version directory; a package
root with no ``current`` resolves to its newest version directory however many it holds (a root with two versions, or with
``_check_updates`` beside them, never fails with "multiple version directories"); a valid ``current`` wins and is reported;
a ``current`` that is a plain folder is refused, not reinterpreted.

Real: package roots and ``current`` junctions built under pytest's temporary folder, resolved through the catalog
(``Catalog.resolve`` of a scope whose root holds the package). Mocked: the machine boundary is ``FakeSystem``, whose links are real. Out of
scope: the catalog's own states (``test_catalog_*``) and installing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.catalog_support import catalog_over


def resolve_package_path(path: Path, system):
    """Resolve *path* in a catalog whose root is the folder that holds it."""
    return catalog_over(system, Path(path).parent).resolve(str(path))


def make_version(root: Path, name: str) -> Path:
    """Create ``<root>/<name>/pkg.toml`` and return the version directory."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text('format = 2\n[source]\ndownload_url = "https://example.org/x.zip"\n', encoding="utf-8")
    return folder


@pytest.mark.parametrize("root_name", ["volta", "vlc"])
def test_v_prefixed_package_root_resolves_to_its_version(tmp_path: Path, root_name: str, system) -> None:
    """A package root named like ``volta`` is a package root, not a version, and resolves to its only version directory."""
    root = tmp_path / root_name
    make_version(root, "1.0")

    resolved = resolve_package_path(root, system)

    assert resolved.view.name == root_name and resolved.version == "1.0" and not resolved.via_current


def test_package_root_with_two_versions_and_no_current_resolves_to_the_newest(tmp_path: Path, system) -> None:
    """Three version directories and no ``current`` resolve to the newest one instead of failing."""
    root = tmp_path / "tool"
    make_version(root, "1.0")
    make_version(root, "1.10")
    make_version(root, "1.2")

    resolved = resolve_package_path(root, system)

    assert resolved.version == "1.10" and resolved.version_path == root / "1.10" and not resolved.via_current


def test_the_tracker_beside_versions_never_wins(tmp_path: Path, system) -> None:
    """``_check_updates`` is reserved and is never chosen as the version."""
    root = tmp_path / "tool"
    make_version(root, "1.0")
    tracker = make_version(root, "_check_updates")
    assert tracker.is_dir()

    resolved = resolve_package_path(root, system)

    assert resolved.version == "1.0"


def test_valid_current_wins_over_a_newer_directory(tmp_path: Path, system) -> None:
    """A package root with a valid ``current`` resolves to the active version and reports it."""
    root = tmp_path / "tool"
    active = make_version(root, "1.0")
    make_version(root, "2.0")
    system.create_junction(root / "current", active)

    resolved = resolve_package_path(root, system)

    assert resolved.version == "1.0" and resolved.view.installed is not None and resolved.view.installed.version == "1.0"


def test_current_that_is_a_plain_folder_is_not_reinterpreted(tmp_path: Path, system) -> None:
    """A ``current`` that is a real folder (not a junction) is reported as a problem of the package and never chosen."""
    root = tmp_path / "tool"
    make_version(root, "1.0")
    (root / "current").mkdir()

    resolved = resolve_package_path(root, system)

    assert resolved.view.installed is None and resolved.view.status == "broken"
    assert any('"current" is not a junction' in reason for reason in resolved.view.broken_reasons)
