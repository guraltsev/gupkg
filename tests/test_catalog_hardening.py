"""Cover how the catalog survives hostile or damaged files, links and elevated path targets.

Behavior covered: files that are not valid UTF-8 (a registry cache's ``state.toml`` and ``registry.toml``, a version's
``pkg.toml``) become warnings or invalid entries, never exceptions; a registry cache revision or index name that could
leave the cache folder is ignored; a package root or version that is reached through a junction out of the root is
refused (a path must lie inside the scope's root); an elevated run accepts a path only inside the system root and only
in an admin-only folder; a package whose versions are all invalid says so instead of "no version known yet".

Real: files and real junctions on pytest's temporary folder (``FakeSystem`` links are real; the
``windows_integration`` test uses ``WindowsSystem``), the real sync, and the catalog. Mocked: elevation and the
admin-only answer (``FakeSystem``). Out of scope: acting on packages (issue 028).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from gupkg.catalog.catalog import NoValidVersion, NotFound
from gupkg.catalog.registry import load_registry, registry_cache_dir, sync_registry
from gupkg.selfmgmt.elevation import UntrustedLocationError
from gupkg.system.fake import FakeSystem
from gupkg.system.windows import WindowsSystem
from gupkg.tomlio import dumps, write_toml
from tests.catalog_support import (
    REVISION,
    build_registry,
    make_catalog,
    version_text,
    write_version,
)
from tests.scope_support import scope_of

BAD_BYTES = b"\xff\xfe\x00 not utf-8 \xc3\x28"


def test_registry_cache_files_that_are_not_utf8_leave_the_registry_empty_not_crashing(system, tmp_path: Path) -> None:
    """Invalid UTF-8 in the cache ``state.toml`` means "never synchronised"; in ``registry.toml`` it is a reported problem."""
    cache = build_registry(tmp_path, {"foo": {"versions": ["1.0"]}})

    (cache / "trees" / REVISION / "registry.toml").write_bytes(BAD_BYTES)
    broken_registry = load_registry("main", cache, scope_of(system))
    (cache / "state.toml").write_bytes(BAD_BYTES)
    broken_state = load_registry("main", cache, scope_of(system))

    assert broken_registry.entries == {} and "Cannot read _registry.toml" in broken_registry.problems[0]
    assert broken_state.entries == {} and broken_state.revision is None


def test_registry_listing_survives_a_damaged_cache(system, tmp_path: Path) -> None:
    """``list`` with a damaged registry cache still lists the local packages."""
    roots = make_catalog(system, tmp_path)[1]
    cache = build_registry(roots["user"], {"foo": {"versions": ["1.0"]}})
    (cache / "trees" / REVISION / "registry.toml").write_bytes(BAD_BYTES)
    write_version(roots["user"] / "foo", "1.0")

    listing = make_catalog(system, tmp_path)[0].list()

    assert [(view.name, view.following) for view in listing.views] == [("foo", None)]


def test_version_and_tracker_files_that_are_not_utf8_are_invalid_not_fatal(system, tmp_path: Path) -> None:
    """A ``pkg.toml`` with invalid UTF-8 invalidates its version in a package root and in a registry, and nothing else."""
    roots = make_catalog(system, tmp_path)[1]
    folder = write_version(roots["user"] / "foo", "2.0")
    (folder / "pkg.toml").write_bytes(BAD_BYTES)
    write_version(roots["user"] / "foo", "1.0")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("r/_registry.toml", dumps({"format": 2}))
        zipped.writestr("r/foo/1.0/pkg.toml", version_text("1.0"))
        zipped.writestr("r/foo/2.0/pkg.toml", BAD_BYTES)
        zipped.writestr("r/foo/_check_updates/pkg.toml", BAD_BYTES)
    system.add_url("https://example.org/main.zip", archive.getvalue())

    result = sync_registry(registry_cache_dir(roots["user"], "main"), scope_of(system), name="main", source="https://example.org/main.zip")
    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert result.ok and any("foo/2.0" in problem for problem in result.problems)
    assert [(item.name, item.valid) for item in view.local.versions] == [("1.0", True), ("2.0", False)]
    assert [item.name for item in view.registry.versions] == ["1.0"] and view.registry.tracker is None


def test_cache_revision_that_could_leave_the_cache_folder_is_ignored(system, tmp_path: Path) -> None:
    """A ``state.toml`` revision such as ``..\\..\\x`` is not a 40-digit revision, so no folder outside the cache is read."""
    cache = registry_cache_dir(tmp_path / "root", "main")
    outside = cache.parent.parent.parent / "x"
    write_version(outside / "pkgs" / "evil", "1.0")
    write_toml(outside / "registry.toml", {"format": 2})
    cache.mkdir(parents=True)
    write_toml(cache / "state.toml", {"revision": "..\\..\\..\\..\\x", "source": "", "synchronized_at": "", "last_failure": ""})

    snapshot = load_registry("main", cache, scope_of(system))

    assert snapshot.entries == {} and snapshot.revision is None


def test_index_with_names_that_could_leave_the_tree_is_rebuilt(system, tmp_path: Path) -> None:
    """An index that names a package like ``..\\..\\evil`` is not trusted: the tree is validated again instead."""
    cache = build_registry(tmp_path, {"foo": {"versions": ["1.0"]}})
    tree = cache / "trees" / REVISION
    write_toml(tree / "index.toml", {
        "schema": 1, "revision": REVISION, "name": "main", "problem": [],
        "entry": [{"name": "..\\..\\evil", "version": [{"name": "1.0"}]}],
    })

    snapshot = load_registry("main", cache, scope_of(system))

    assert sorted(snapshot.entries) == ["foo"]


def build_linked_package(system, root: Path, tmp_path: Path) -> tuple[Path, Path]:
    """Create ``dev/foo`` with version 1.0 and the junction ``<root>/foo`` to it; return both package roots."""
    real = tmp_path / "dev" / "foo"
    write_version(real, "1.0", populated=True)
    root.mkdir(exist_ok=True)
    system.create_junction(root / "foo", real)
    return root / "foo", real


@pytest.mark.parametrize("suffix", ["", "1.0"], ids=["package-root", "version-directory"])
def test_explicit_path_through_a_package_junction_out_of_the_root_is_refused(system, tmp_path: Path, suffix: str) -> None:
    """``<root>\foo`` as a junction to ``dev\foo`` (and ``<root>\foo\1.0``) lies outside the root by identity."""
    catalog, roots = make_catalog(system, tmp_path)
    linked, _ = build_linked_package(system, roots["user"], tmp_path)
    path = linked / suffix if suffix else linked

    with pytest.raises(NotFound, match="not inside this scope's root"):
        catalog.resolve(str(path))


def test_package_junction_is_skipped_by_scanning_and_refused_by_path(tmp_path: Path) -> None:
    """A linked package root is not listed and a path through it is outside the system root by identity."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    catalog, roots = make_catalog(system, tmp_path, mode="system")
    real = tmp_path / "dev" / "foo"
    write_version(real, "1.0", populated=True)
    system.create_junction(roots["system"] / "foo", real)

    assert catalog.list().views == ()
    with pytest.raises(NotFound, match="not inside this scope's root"):
        catalog.resolve(str(roots["system"] / "foo"))


@pytest.mark.windows_integration
def test_real_junction_package_root_out_of_the_root_is_refused(tmp_path: Path) -> None:
    """With ``WindowsSystem`` junctions a package root that is a junction out of the root is refused by path."""
    real = WindowsSystem()
    catalog, roots = make_catalog(real, tmp_path)
    linked, _ = build_linked_package(real, roots["user"], tmp_path)

    with pytest.raises(NotFound, match="not inside this scope's root"):
        catalog.resolve(str(linked))


def test_elevated_path_must_be_in_the_system_root_even_when_admin_only(tmp_path: Path) -> None:
    """An admin-only folder outside the system root is refused in an elevated run, whatever its install record says."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    catalog, roots = make_catalog(system, tmp_path, mode="system")
    outside = tmp_path / "admin-only-elsewhere" / "foo"
    write_version(outside, "1.0")
    inside = roots["system"] / "foo"
    write_version(inside, "1.0")

    with pytest.raises(NotFound, match="not inside this scope's root"):
        catalog.resolve(str(outside))
    assert catalog.resolve(str(inside)).view.name == "foo"


def test_elevated_run_refuses_a_path_in_a_folder_that_users_can_modify(tmp_path: Path) -> None:
    """A package folder inside the system root that standard users can modify is refused in an elevated run."""
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={tmp_path / "system" / "bad"})
    catalog, roots = make_catalog(system, tmp_path, mode="system")
    write_version(roots["system"] / "good", "1.0")
    write_version(roots["system"] / "bad", "1.0")

    assert catalog.resolve(str(roots["system"] / "good")).version == "1.0"
    with pytest.raises(UntrustedLocationError):
        catalog.resolve(str(roots["system"] / "bad"))


def test_package_with_only_invalid_versions_says_so_instead_of_asking_for_check_updates(system, tmp_path: Path) -> None:
    """Install of a package whose every version is invalid names the invalid versions."""
    roots = make_catalog(system, tmp_path)[1]
    write_version(roots["user"] / "foo", "1.0", text="format = 1\n")
    catalog = make_catalog(system, tmp_path)[0]

    with pytest.raises(NoValidVersion, match="no valid version; see the invalid version problems: 1.0"):
        catalog.install_candidate(catalog.describe("foo"))


@pytest.mark.parametrize("mode", ["user", "system"])
def test_extended_length_path_resolves_to_the_same_package(tmp_path: Path, mode: str) -> None:
    """A path written with the extended-length prefix selects the package by its folder name, as the plain spelling does."""
    system = FakeSystem(tmp_path / "machine", elevated=mode == "system")
    catalog, roots = make_catalog(system, tmp_path, mode=mode)
    write_version(roots[mode] / "foo", "1.0")

    plain = catalog.resolve(str(roots[mode] / "foo"))
    prefixed = catalog.resolve("\\\\?\\" + str(roots[mode] / "foo"))

    assert prefixed.view.name == plain.view.name == "foo"
    assert prefixed.version == "1.0"


@pytest.mark.parametrize("child", ["version", "app"])
def test_junction_version_or_app_inside_an_admin_only_system_package_is_a_problem(tmp_path: Path, child: str) -> None:
    """In an elevated run a version directory or ``App`` that is a junction is reported invalid and never counted as valid or populated."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    catalog, roots = make_catalog(system, tmp_path, mode="system")
    package = roots["system"] / "foo"
    write_version(package, "1.0")
    outside = tmp_path / "outside"
    write_version(outside, "payload", populated=True)
    if child == "version":
        system.create_junction(package / "2.0", outside / "payload")
    else:
        system.create_junction(package / "1.0" / "App", outside / "payload" / "App")

    view = catalog.describe("foo")

    flagged = view.local.version("2.0" if child == "version" else "1.0")
    assert not flagged.valid and any("link" in problem for problem in flagged.problems)
    assert not flagged.populated
