"""Cover the selector grammar of design 10.1 as the catalog resolves it.

Behavior covered: ``name``, ``name@version``, ``registry/name`` and paths (a version
directory, a package root, ``current``, ``_check_updates``) select the right package and version; a bare
name is unique inside the one root; a package in a grouping folder (``gupkg-dir.toml``) is selected by its
folder path; a registry-only name resolves to an ``available`` view; unknown names and non-package paths
fail with a clear error; a version directory without ``pkg.toml`` beside others never wins.

Real: package roots and junctions on pytest's temporary folder, registry caches, and the catalog.
Mocked: the machine boundary (``FakeSystem``). Out of scope: acting on the selection (issue 028) and the
command-line parsing (issue 029).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.catalog import CatalogError, NotFound, parse_selector
from tests.catalog_support import activate, build_registry, make_catalog, write_tracker, write_version


@pytest.mark.parametrize(
    ("text", "kind", "name", "version", "registry"),
    [
        pytest.param("firefox", "name", "firefox", None, None, id="name"),
        pytest.param("firefox@1.2", "name", "firefox", "1.2", None),
        pytest.param("main/firefox", "registry", "firefox", None, "main", id="registry-entry"),
        pytest.param("MAIN/firefox@1.0", "registry", "firefox", "1.0", "MAIN", id="registry-entry-with-version"),
        pytest.param("tools/firefox", "name", "tools/firefox", None, None, id="grouped-name"),
        pytest.param(".\\firefox", "path", None, None, None, id="relative-path"),
        pytest.param("C:\\opt\\firefox", "path", None, None, None, id="absolute-path"),
        pytest.param("..\\x", "path", None, None, None, id="parent-path"),
    ],
)
def test_selector_grammar(text: str, kind: str, name, version, registry) -> None:
    """Each selector form parses to its kind, name, version and registry."""
    parsed = parse_selector(text, ["main", "team"])

    assert (parsed.kind, parsed.name, parsed.version, parsed.registry) == (kind, name, version, registry)


@pytest.mark.parametrize("text", ["", "   ", "foo@", "@1.0"])
def test_malformed_selectors_are_refused(text: str) -> None:
    """An empty selector or one with an empty name or version is a clear error."""
    with pytest.raises(CatalogError):
        parse_selector(text)


def test_name_and_version_selector_names_the_version(system, tmp_path: Path) -> None:
    """``name@version`` returns the named version, and ``pick_version`` finds it among local and registry versions."""
    roots = make_catalog(system, tmp_path)[1]
    build_registry(roots["user"], {"foo": {"versions": ["1.0", "1.5"]}})
    write_version(roots["user"] / "foo", "1.2")
    catalog = make_catalog(system, tmp_path)[0]

    resolved = catalog.resolve("foo@1.5")
    picked = catalog.pick_version(resolved.view, resolved.version)

    assert resolved.version == "1.5" and resolved.named_version
    assert (picked.source, picked.version) == ("registry", "1.5")
    assert catalog.pick_version(resolved.view, "1.2").source == "local"
    with pytest.raises(NotFound, match="known versions: 1.0, 1.2, 1.5"):
        catalog.pick_version(resolved.view, "9.9")


def test_registry_only_name_resolves_to_an_available_view(system, tmp_path: Path) -> None:
    """A name that only the registry has is ``available`` and has no local root."""
    roots = make_catalog(system, tmp_path)[1]
    build_registry(roots["user"], {"foo": {"versions": ["1.0"]}})
    catalog = make_catalog(system, tmp_path)[0]

    plain = catalog.resolve("foo").view

    assert plain.status == "available" and plain.root is None


def test_registry_name_selector_follows_that_entry_for_an_existing_local_package(system, tmp_path: Path) -> None:
    """``main/foo`` for a package that exists locally returns the local package following that entry."""
    roots = make_catalog(system, tmp_path)[1]
    build_registry(roots["user"], {"foo": {"versions": ["1.0", "1.4"]}})
    write_version(roots["user"] / "foo", "1.0")
    catalog = make_catalog(system, tmp_path)[0]

    resolved = catalog.resolve("main/foo")

    assert resolved.view.status == "local" and resolved.view.following == "main/foo"
    with pytest.raises(NotFound, match="registry entry 'main/nope'"):
        catalog.resolve("main/nope")


def test_unknown_name_and_non_package_paths_fail_clearly(system, tmp_path: Path) -> None:
    """A name that nothing has, a missing path and an ordinary folder are ``NotFound`` with a message that says what failed."""
    roots = make_catalog(system, tmp_path)[1]
    (roots["user"] / "plain").mkdir()
    catalog = make_catalog(system, tmp_path)[0]

    with pytest.raises(NotFound, match="package 'ghost' was not found"):
        catalog.resolve("ghost")
    with pytest.raises(NotFound, match="does not exist"):
        catalog.resolve(str(roots["user"] / "missing"))
    with pytest.raises(NotFound, match="Not a package root"):
        catalog.resolve(str(roots["user"] / "plain"))


def test_paths_select_the_package_and_version(system, tmp_path: Path) -> None:
    """A package root, a version directory, ``current`` and ``_check_updates`` each resolve to the right package and version."""
    roots = make_catalog(system, tmp_path)[1]
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    write_version(package, "1.1")
    write_tracker(package)
    catalog = make_catalog(system, tmp_path)[0]

    not_installed = catalog.resolve(str(package))
    version_dir = catalog.resolve(str(package / "1.0"))
    tracker = catalog.resolve(str(package / "_check_updates"))
    activate(system, package, "1.0")
    installed = make_catalog(system, tmp_path)[0].resolve(str(package))
    via_current = make_catalog(system, tmp_path)[0].resolve(str(package / "current"))

    assert (not_installed.version, not_installed.via_current) == ("1.1", False)
    assert version_dir.version == "1.0" and version_dir.version_path == package / "1.0"
    assert tracker.is_tracker and tracker.version is None
    assert (installed.version, installed.via_current) == ("1.0", True)
    assert (via_current.version, via_current.via_current) == ("1.0", True)
    assert {item.view.name for item in (not_installed, version_dir, tracker, installed, via_current)} == {"foo"}


def test_package_in_a_grouping_folder_is_selected_by_its_folder_path(system, tmp_path: Path) -> None:
    """A folder marked with ``gupkg-dir.toml`` groups packages: ``group/tool`` selects one, and a path under the root finds it too."""
    roots = make_catalog(system, tmp_path)[1]
    (roots["user"] / "group").mkdir()
    (roots["user"] / "group" / "gupkg-dir.toml").write_text("", encoding="utf-8")
    write_version(roots["user"] / "group" / "tool", "1.0")
    catalog = make_catalog(system, tmp_path)[0]

    listed = [view.name for view in catalog.list().views]
    by_name = catalog.resolve("group/tool").view
    by_path = catalog.resolve(str(roots["user"] / "group" / "tool")).view

    assert listed == ["group/tool"]
    assert by_name.name == by_path.name == "group/tool"
    assert by_path.root.path == roots["user"]
