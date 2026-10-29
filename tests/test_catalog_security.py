"""Cover the catalog's fail-closed behaviour for links, path spellings and elevated runs.

Behavior covered: a ``current`` junction that leaves its package root (to another folder, to a
sibling package, to a folder inside a version, or to the package root itself) marks the package
``broken`` and is never followed; a version directory, tracker, ``App``, ``pkg.toml`` or
``.gupkg`` folder that is a junction is reported and never read; a package root that is a link is
skipped; the containment test recognises one folder under every spelling (``..``, ``\\\\?\\``
prefix, letter case, trailing dots and spaces, 8.3 names); an elevated run reads only the system
root, only when it is admin-only, refuses untrusted path targets and reads only the system root's
registry cache.

Real: junctions (``FakeSystem`` links are real; the ``windows_integration`` test uses
``WindowsSystem``), package roots on pytest's temporary folder, the catalog. Mocked: elevation and
the admin-only answer (``FakeSystem(elevated=..., untrusted_dirs=...)``). Out of scope: acting on
packages (issue 028).
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from gupkg.catalog.catalog import NeedsRepair, NotFound, UntrustedLocation
from gupkg.catalog.local import canonical_path, is_inside, read_current, scan_package
from gupkg.model.state import InstallRecord, Link
from gupkg.system.fake import FakeSystem
from gupkg.system.windows import WindowsSystem
from tests.catalog_support import (
    activate,
    build_registry,
    make_catalog,
    write_record,
    write_tracker,
    write_version,
)


def build_escape(system, kind: str, tmp_path: Path, package: Path) -> Path:
    """Create the folder that a hostile ``current`` should point at and return it."""
    if kind == "outside-folder":
        target = tmp_path / "outside" / "1.0"
        write_version(target.parent, "1.0", populated=True)
    elif kind == "sibling-package":
        target = write_version(package.parent / "other", "1.0", populated=True)
    elif kind == "a-version-that-is-a-junction":
        outside = tmp_path / "outside"
        write_version(outside, "payload", populated=True)
        package.mkdir(parents=True, exist_ok=True)
        system.create_junction(package / "1.0", outside / "payload")
        target = package / "1.0"
    elif kind == "inside-a-version":
        target = package / "1.0" / "App"
    else:
        target = package
    return target


@pytest.mark.parametrize(
    "kind", ["outside-folder", "sibling-package", "inside-a-version", "a-version-that-is-a-junction", "the-package-root-itself"]
)
def test_current_that_leaves_its_package_root_is_broken_and_never_followed(system, tmp_path: Path, kind: str) -> None:
    """A ``current`` junction to anything but a direct child of its package root is ``escapes``, so the package is broken."""
    roots = make_catalog(system, tmp_path)[1]
    package = roots["user"] / "foo"
    if kind != "a-version-that-is-a-junction":
        write_version(package, "1.0", populated=True)
    target = build_escape(system, kind, tmp_path, package)
    system.create_junction(package / "current", target)

    catalog = make_catalog(system, tmp_path)[0]
    view = catalog.describe("foo", next(root for root in catalog.roots if root.mode == "user"))

    assert view.local.current.status == "escapes"
    assert view.installed is None
    assert view.status == "broken"
    with pytest.raises(NeedsRepair):
        catalog.update_candidate(view)


def test_resolving_a_current_that_escapes_is_refused(system, tmp_path: Path) -> None:
    """Naming ``current`` of a package whose junction leaves the package root fails instead of returning the outside folder."""
    roots = make_catalog(system, tmp_path)[1]
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    system.create_junction(package / "current", build_escape(system, "outside-folder", tmp_path, package))

    catalog = make_catalog(system, tmp_path)[0]

    with pytest.raises(NotFound, match="outside the package root"):
        catalog.resolve(str(package / "current"))


def test_current_that_is_a_plain_folder_or_dangling_is_reported(system, tmp_path: Path) -> None:
    """A ``current`` that is a folder, or a junction to a deleted version, is ``not-a-link`` or ``dangling``."""
    roots = make_catalog(system, tmp_path)[1]
    folder_package, dangling_package = roots["user"] / "a", roots["user"] / "b"
    write_version(folder_package, "1.0")
    (folder_package / "current").mkdir()
    gone = write_version(dangling_package, "1.0")
    activate(system, dangling_package, "1.0")
    (gone / "pkg.toml").unlink()
    gone.rmdir()

    assert read_current(folder_package, system).status == "not-a-link"
    assert read_current(dangling_package, system).status == "dangling"


@pytest.mark.parametrize("name", ["1.0", "_check_updates"])
def test_version_or_tracker_that_is_a_junction_is_invalid_and_not_read(system, tmp_path: Path, name: str) -> None:
    """A version directory or tracker that is a junction out of the package root is invalid and its ``pkg.toml`` is not parsed."""
    package = tmp_path / "user" / "foo"
    package.mkdir(parents=True)
    outside = tmp_path / "outside"
    write_version(outside, "payload", populated=True)
    if name == "_check_updates":
        write_tracker(outside)
        outside = outside / "_check_updates"
    else:
        outside = outside / "payload"
    system.create_junction(package / name, outside)

    local = scan_package(package, system)

    flagged = local.tracker if name == "_check_updates" else local.version("1.0")
    assert flagged is not None and not flagged.valid
    assert "link" in flagged.problems[0]
    assert flagged.manifest is None
    assert local.valid_versions == ()


@pytest.mark.parametrize("child", ["App", "pkg.local"])
def test_app_or_hooks_folder_that_is_a_junction_makes_the_version_invalid(system, tmp_path: Path, child: str) -> None:
    """An ``App`` or ``pkg.local`` that links out of the version directory is reported and never counted as populated."""
    package = tmp_path / "user" / "foo"
    folder = write_version(package, "1.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.exe").write_text("x", encoding="utf-8")
    system.create_junction(folder / child, outside)

    version = scan_package(package, system).version("1.0")

    assert not version.valid and any("link" in problem for problem in version.problems)
    assert not version.populated


def test_install_record_in_a_linked_data_folder_is_not_read(system, tmp_path: Path) -> None:
    """A ``.gupkg`` folder that is a junction is not followed: the record behind it is ignored and the package is broken."""
    roots = make_catalog(system, tmp_path)[1]
    package = roots["user"] / "foo"
    write_version(package, "1.0")
    hostile = tmp_path / "hostile-package"
    write_record(hostile, InstallRecord(mode="user", link=Link(unlinked=True)))
    system.create_junction(package / ".gupkg", hostile / ".gupkg")

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.link is None and view.record is None
    assert view.status == "broken"
    assert any("link" in reason for reason in view.broken_reasons)


def test_package_root_that_is_a_junction_is_skipped_by_list(system, tmp_path: Path) -> None:
    """A package folder that is a junction to somewhere else is neither listed nor described, and the scan says so."""
    roots = make_catalog(system, tmp_path)[1]
    write_version(tmp_path / "outside" / "foo", "1.0")
    system.create_junction(roots["user"] / "foo", tmp_path / "outside" / "foo")

    catalog = make_catalog(system, tmp_path)[0]
    listing = catalog.list()

    assert listing.views == ()
    assert any("link" in warning for warning in listing.warnings)
    with pytest.raises(NotFound):
        catalog.describe("foo")


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        pytest.param("{root}\\child", True, id="plain"),
        pytest.param("{root}\\child\\..\\child\\leaf", True, id="dot-dot-inside"),
        pytest.param("{root}\\child\\..\\..\\other", False, id="dot-dot-escapes"),
        pytest.param("\\\\?\\{root}\\child", True, id="extended-length-prefix"),
        pytest.param("{ROOT}\\CHILD", True, id="letter-case"),
        pytest.param("{root}\\child. ", True, id="trailing-dot-and-space"),
        pytest.param("{root}. \\child", True, id="root-spelled-with-trailing-dot"),
        pytest.param("{root}", False, id="the-root-itself"),
        pytest.param("{root}-evil\\child", False, id="shared-prefix-sibling"),
        pytest.param("{root}\\..", False, id="parent-of-the-root"),
    ],
)
def test_containment_recognises_one_folder_under_every_spelling(tmp_path: Path, spelling: str, expected: bool) -> None:
    """``is_inside`` is true for a folder named inside the root in any spelling and false for the root, its parent and look-alikes."""
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    text = spelling.format(root=root, ROOT=str(root).upper())

    assert is_inside(text, root) is expected


def test_path_through_a_junction_that_leaves_the_root_is_outside(system, tmp_path: Path) -> None:
    """A path that goes through a junction pointing out of the root is outside it, whatever it looks like."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    system.create_junction(root / "door", tmp_path / "elsewhere")

    assert not is_inside(root / "door" / "x", root)
    assert canonical_path(root / "door") == canonical_path(tmp_path / "elsewhere")


@pytest.mark.windows_integration
def test_containment_recognises_8_3_short_names(tmp_path: Path) -> None:
    """A folder named by its 8.3 short name is the same folder as its long name."""
    root = tmp_path / "a root with a long name"
    child = root / "a child folder with a long name"
    child.mkdir(parents=True)
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(child), buffer, 1024) or buffer.value.lower() == str(child).lower():
        pytest.skip("this volume has no 8.3 short names")

    assert is_inside(buffer.value, root)
    assert canonical_path(buffer.value) == canonical_path(child)


@pytest.mark.windows_integration
def test_real_junctions_are_read_the_same_way(tmp_path: Path) -> None:
    """With ``WindowsSystem`` junctions, a valid ``current`` is accepted and one that escapes is refused."""
    real = WindowsSystem()
    package = tmp_path / "foo"
    write_version(package, "1.0", populated=True)
    real.create_junction(package / "current", package / "1.0")
    assert read_current(package, real).status == "valid"

    other = tmp_path / "bar"
    write_version(other, "1.0", populated=True)
    real.create_junction(other / "current", package / "1.0")
    assert read_current(other, real).status == "escapes"


# ---------------------------------------------------------------------------
# Elevated runs
# ---------------------------------------------------------------------------


def test_elevated_run_reads_only_the_system_root(tmp_path: Path) -> None:
    """An elevated run lists only the system root (decision D31) and never touches the user root."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    roots = make_catalog(system, tmp_path, elevated=True)[1]
    write_version(roots["system"] / "sysapp", "1.0")
    write_version(roots["user"] / "userapp", "1.0")

    catalog = make_catalog(system, tmp_path)[0]

    assert [view.name for view in catalog.list().views] == ["sysapp"]
    with pytest.raises(NotFound):
        catalog.describe("userapp")


def test_elevated_run_skips_a_system_root_that_users_can_modify(tmp_path: Path) -> None:
    """When the system root is not admin-only, an elevated run reads nothing from it and says why."""
    untrusted = tmp_path / "system"
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={untrusted})
    roots = make_catalog(system, tmp_path)[1]
    write_version(roots["system"] / "sysapp", "1.0")

    listing = make_catalog(system, tmp_path)[0].list()

    assert listing.views == ()
    assert any("Skipped" in warning and "elevated" in warning for warning in listing.warnings)


def test_elevated_run_refuses_a_path_in_an_untrusted_folder(tmp_path: Path) -> None:
    """A path target outside the admin-only locations is refused in an elevated run instead of being read."""
    ordinary = tmp_path / "downloads"
    write_version(ordinary / "foo", "1.0")
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={ordinary})
    catalog = make_catalog(system, tmp_path)[0]

    with pytest.raises(UntrustedLocation, match="elevated"):
        catalog.resolve(str(ordinary / "foo"))


def test_elevated_run_uses_only_the_system_roots_registry_cache(tmp_path: Path) -> None:
    """A user root's registry cache is ignored by an elevated run: only the admin-only system cache supplies entries."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    roots = make_catalog(system, tmp_path)[1]
    build_registry(roots["user"], {"userfoo": {"versions": ["9.9"]}})
    build_registry(roots["system"], {"sysfoo": {"versions": ["1.0"]}})

    catalog = make_catalog(system, tmp_path)[0]

    assert catalog.describe("sysfoo").status == "available"
    with pytest.raises(NotFound):
        catalog.describe("userfoo")


def test_registry_cache_in_an_untrusted_folder_is_not_read_in_an_elevated_run(tmp_path: Path) -> None:
    """An elevated run refuses a registry cache that users can modify: the registry contributes nothing."""
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={tmp_path / "system" / ".gupkg"})
    roots = make_catalog(system, tmp_path)[1]
    build_registry(roots["system"], {"sysfoo": {"versions": ["1.0"]}})

    catalog = make_catalog(system, tmp_path)[0]

    with pytest.raises(NotFound):
        catalog.describe("sysfoo")


def test_record_that_claims_a_user_package_inside_the_system_root_is_broken(tmp_path: Path) -> None:
    """An install record naming another mode than its root is distrusted: the package is broken, not installed."""
    system = FakeSystem(tmp_path / "machine", elevated=True)
    roots = make_catalog(system, tmp_path)[1]
    package = roots["system"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    write_record(package, InstallRecord(mode="user"))

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.status == "broken"
