"""Cover that a catalog reads exactly one root and joins exactly one registry cache (issue 035 §4.3, §4.6, §4.7).

Behavior covered: a catalog of one root never lists, describes or resolves the package of another root, by name or by path
(a path into the other root is refused with "not inside this scope's root"); a root whose registry cache was never
synchronised has no registry entries and does not see another root's cache; an install record that names another mode than
the run's makes the package ``broken`` ("moved between scopes"); a package folder with ``current`` and no install record is
``broken`` with the reinstall command and nothing in it changes; ``list --available`` marks a registry package that the mode
cannot install as ``system only`` or ``user only`` with the refusal text (owner's answer Q14); ``list --json`` carries
``mode`` and ``root`` once, at document level, and no ``mode`` in a row.

Real: package roots, junctions and registry caches on pytest's temporary folder, the catalog and the real command line
(``CliWorld``). Mocked: the machine boundary (``FakeSystem``). Out of scope: the planner's refusal of a mode (``test_targets``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.catalog import ListFilter, NotFound
from gupkg.model.scope import PORTABLE_REFUSAL, SYSTEM_ONLY_REFUSAL, USER_ONLY_REFUSAL
from gupkg.model.state import InstallRecord
from gupkg.system.fake import FakeSystem
from tests.catalog_support import activate, build_registry, make_catalog, version_text, write_record, write_version
from tests.cli_support import CliWorld
from tests.mode_support import tree_snapshot


def test_a_catalog_of_one_root_never_sees_the_package_of_the_other_root(system, tmp_path: Path) -> None:
    """Root A lists, describes and resolves only its own package; a path into root B is refused, by every spelling of the target."""
    user_catalog, roots = make_catalog(system, tmp_path, mode="user")
    system_catalog, _ = make_catalog(system, tmp_path, mode="system")
    write_version(roots["user"] / "mine", "1.0")
    write_version(roots["system"] / "theirs", "1.0")

    assert [view.name for view in user_catalog.list().views] == ["mine"]
    assert [view.name for view in system_catalog.list().views] == ["theirs"]
    with pytest.raises(NotFound, match="package 'theirs' was not found"):
        user_catalog.describe("theirs")
    with pytest.raises(NotFound, match="package 'theirs' was not found"):
        user_catalog.resolve("theirs")
    for path in (roots["system"] / "theirs", roots["system"] / "theirs" / "1.0", roots["system"] / "theirs" / "current"):
        with pytest.raises(NotFound, match="is not inside this scope's root") as refused:
            user_catalog.resolve(str(path))
        assert f"({roots['user']})" in str(refused.value)
    assert user_catalog.resolve(str(roots["user"] / "mine")).view.name == "mine"


def test_an_unsynchronised_root_has_no_registry_entries_and_does_not_see_another_roots_cache(system, tmp_path: Path) -> None:
    """The user root never synchronised its registry: it lists none and does not fall back to the system root's cache."""
    user_catalog, roots = make_catalog(system, tmp_path, mode="user")
    build_registry(roots["system"], {"sysfoo": {"versions": ["1.0"]}})

    assert user_catalog.list(ListFilter(include_available=True)).views == ()
    with pytest.raises(NotFound):
        user_catalog.describe("sysfoo")
    with pytest.raises(NotFound):
        user_catalog.resolve("sysfoo")
    with pytest.raises(NotFound):
        user_catalog.resolve("main/sysfoo")
    assert make_catalog(system, tmp_path, mode="system")[0].describe("sysfoo").status == "available"


def test_a_record_of_another_mode_is_broken_with_the_reinstall_hint(system, tmp_path: Path) -> None:
    """A record that says ``system`` inside a user scope means the package was moved between scopes: ``broken``."""
    catalog, roots = make_catalog(system, tmp_path, mode="user")
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    write_record(package, InstallRecord(mode="system"))

    view = catalog.describe("foo")

    assert view.status == "broken"
    assert view.broken_reasons == ("the install record says system; this package was moved between scopes — reinstall it",)


def test_a_record_of_the_runs_own_mode_is_not_a_problem(system, tmp_path: Path) -> None:
    """The check compares with the run's mode: a ``system`` record is fine in a system scope."""
    catalog, roots = make_catalog(system, tmp_path, mode="system")
    package = roots["system"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    write_record(package, InstallRecord(mode="system"))

    assert catalog.describe("foo").status == "installed"


def test_a_package_with_current_and_no_install_record_is_broken_and_left_alone(system, tmp_path: Path) -> None:
    """An old-layout folder (``current``, no record) is ``broken`` with the reinstall command; listing and describing change nothing."""
    catalog, roots = make_catalog(system, tmp_path, mode="user")
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    system.create_junction(package / "current", package / "1.0")
    before = tree_snapshot(roots["user"])

    view = catalog.describe("foo")
    listed = catalog.list().views

    assert view.status == "broken" and [item.status for item in listed] == ["broken"]
    assert view.broken_reasons == (
        "no install record (made by an older gupkg); reinstall with: gupkg install foo --force",
    )
    assert tree_snapshot(roots["user"]) == before
    assert not (package / ".gupkg").exists()


def test_a_package_with_versions_only_and_no_record_is_not_broken(system, tmp_path: Path) -> None:
    """Only a folder that claims to be installed (``current``) needs a record: a folder of versions is ``local``."""
    catalog, roots = make_catalog(system, tmp_path, mode="user")
    write_version(roots["user"] / "foo", "1.0")

    assert catalog.describe("foo").status == "local"


def write_modes(cache: Path, name: str, modes: str) -> None:
    """Rewrite the pinned ``pkg.toml`` of registry package *name* so that it declares *modes*."""
    manifest = next(cache.rglob(f"pkgs/{name}/1.0.0")) / "pkg.toml"
    text = version_text("1.0.0").replace("format = 2\n", f"format = 2\nmodes = {modes}\n", 1)
    if "portable" in modes:
        text += '\n[portable]\npersist = ["config"]\n'
    manifest.write_text(text, encoding="utf-8")


def test_list_available_marks_what_the_user_mode_cannot_install(system, tmp_path: Path) -> None:
    """In user mode a ``system`` package is listed ``available`` marked ``system only`` with the refusal text; others carry no mark."""
    catalog, roots = make_catalog(system, tmp_path, mode="user")
    cache = build_registry(roots["user"], {"both": {"versions": ["1.0.0"]}, "drv": {"versions": ["1.0.0"]}, "me": {"versions": ["1.0.0"]}})
    write_modes(cache, "drv", '["system"]')
    write_modes(cache, "me", '["user"]')

    views = {view.name: view for view in catalog.list(ListFilter(include_available=True)).views}

    assert {name: view.status for name, view in views.items()} == {"both": "available", "drv": "available", "me": "available"}
    assert views["drv"].restriction == "system only"
    assert views["drv"].restriction_reason == SYSTEM_ONLY_REFUSAL.replace("<command> <name>", "install drv")
    assert views["both"].restriction is None and views["me"].restriction is None


def test_list_available_marks_what_the_system_mode_cannot_install(tmp_path: Path) -> None:
    """In system mode a ``user`` package and a portable one are marked ``user only`` with the refusal text."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    catalog, roots = make_catalog(system, tmp_path, mode="system")
    cache = build_registry(roots["system"], {"tool": {"versions": ["1.0.0"]}, "mine": {"versions": ["1.0.0"]}, "port": {"versions": ["1.0.0"]}})
    write_modes(cache, "mine", '["user"]')
    write_modes(cache, "port", '["portable"]')

    views = {view.name: view for view in catalog.list(ListFilter(include_available=True)).views}

    assert (views["mine"].restriction, views["mine"].restriction_reason) == ("user only", USER_ONLY_REFUSAL)
    assert (views["port"].restriction, views["port"].restriction_reason) == ("user only", PORTABLE_REFUSAL)
    assert views["tool"].restriction is None


def test_list_json_has_mode_and_root_once_and_no_mode_in_a_row(tmp_path: Path) -> None:
    """The list document names the run's ``mode`` and ``root`` at document level; no row carries a ``mode``."""
    world = CliWorld(tmp_path)
    world.publish("tool", "1.0.0")
    assert world.cli("install", "tool").code == 0

    document = world.cli("list", "--json").document()

    assert document["mode"] == "user" and document["root"] == str(world.user)
    assert [row["name"] for row in document["packages"]] == ["tool"]
    assert all("mode" not in row for row in document["packages"])
