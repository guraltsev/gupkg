"""Cover how one target resolves inside the scope of a run (design 6.2, 9.6, D31, D55, issue 035 D47).

Behavior covered: a name or a path inside the scope's root resolves to that root's package, in user mode for a normal
process and in system mode for an elevated one, with no flag; a path outside the root is a distribution folder to copy in
(or, for an elevated child, refused), never a package of the scope; the package's ``modes`` list is enforced against the
scope's mode before any work (a portable package is user only); an elevated run does not trust an install record kept in a
folder users can modify, and never reads the user root or the user settings; a ``system:`` or ``user:`` prefix is refused;
``--in-place`` in system mode needs an admin-only folder.

Real: ``resolve_target``, the catalog, settings layering and install records on real folders and junctions below pytest's
temporary folder. Fake: ``FakeSystem`` for elevation and the admin-only check. Out of scope: the relaunch
(``test_mode_elevation.py``), the copy itself (``test_distribution_copy.py``) and what the engine does after resolving.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.catalog import CatalogError, parse_selector
from gupkg.model.scope import PORTABLE_REFUSAL, SYSTEM_ONLY_REFUSAL, USER_ONLY_REFUSAL, ModeRefused
from gupkg.system.port import Mode
from gupkg.targets import TargetRequest, resolve_target
from tests.catalog_support import build_registry, version_text
from tests.mode_support import Machine, pin_no_ambient_elevation, tree_snapshot  # noqa: F401


def resolve(machine: Machine, selector: str, **options):
    """Resolve *selector* on *machine* the way the engine will: inside the one scope of the run."""
    return resolve_target(TargetRequest(selector, **options), scope=machine.scope(), system=machine.fake, env=machine.env)


def registry_package(machine: Machine, name: str = "tool", modes: str | None = None) -> None:
    """Publish *name* in the registry cache of the machine's own root."""
    root = machine.system_root if machine.fake.elevated else machine.user_root
    cache = build_registry(root, {name: {"versions": ["1.0.0"]}})
    if modes is not None:
        manifest = next(cache.rglob("1.0.0")) / "pkg.toml"
        text = version_text("1.0.0").replace("format = 2\n", f"format = 2\nmodes = {modes}\n", 1)
        if "portable" in modes:
            text += '\n[portable]\npersist = ["config"]\n'
        manifest.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# The mode is the token's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("elevated", "mode"), [(False, Mode.USER), (True, Mode.SYSTEM)])
def test_a_name_resolves_in_the_scope_of_the_token_with_no_flag(tmp_path: Path, elevated: bool, mode: Mode) -> None:
    """The scope of the result is the process's: user for a normal token, system for an elevated one."""
    machine = Machine(tmp_path, elevated=elevated)
    registry_package(machine)

    result = resolve(machine, "tool")

    assert result.scope.mode is mode
    assert result.scope.root == (machine.system_root if elevated else machine.user_root)
    assert result.scope.requires_elevation is elevated


@pytest.mark.parametrize("elevated", [False, True])
def test_a_path_inside_the_scopes_root_resolves_that_package(tmp_path: Path, elevated: bool) -> None:
    """A path into the root of the run resolves to the package, by identity."""
    machine = Machine(tmp_path, elevated=elevated)
    root = machine.system_root if elevated else machine.user_root
    package = machine.package(root, "tool", installed=True)

    result = resolve(machine, str(package / "1.0.0"))

    assert result.resolved is not None and result.resolved.view.path == package
    assert result.source is None


def test_a_portable_package_resolves_in_a_normal_process_and_is_refused_in_an_elevated_one(tmp_path: Path) -> None:
    """A portable package installs for a single user: a system (elevated) run is refused with the portable message."""
    machine = Machine(tmp_path)
    registry_package(machine, modes='["portable"]')
    assert resolve(machine, "tool").scope.mode is Mode.USER

    elevated = Machine(tmp_path / "elevated", elevated=True)
    registry_package(elevated, modes='["portable"]')
    with pytest.raises(ModeRefused) as raised:
        resolve(elevated, "tool")

    assert str(raised.value) == PORTABLE_REFUSAL


def test_an_elevated_process_refuses_a_user_only_package(tmp_path: Path) -> None:
    """An elevated run is system mode; a user-only package is refused with the single-user message."""
    machine = Machine(tmp_path, elevated=True)
    registry_package(machine, modes='["user"]')

    with pytest.raises(ModeRefused) as raised:
        resolve(machine, "tool")

    assert str(raised.value) == USER_ONLY_REFUSAL


def test_a_system_only_package_is_refused_in_a_normal_run_with_the_command_and_the_name(tmp_path: Path) -> None:
    """The system-only refusal names ``gupkg install tool`` when the call site knows both (Q12)."""
    machine = Machine(tmp_path)
    package = machine.package(machine.user_root, "tool", modes='["system"]')
    before = tree_snapshot(tmp_path)

    with pytest.raises(ModeRefused) as raised:
        resolve(machine, str(package), operation="install")

    assert str(raised.value) == SYSTEM_ONLY_REFUSAL.replace("<command> <name>", "install tool")
    assert tree_snapshot(tmp_path) == before


def test_a_mode_prefix_is_not_part_of_a_target(tmp_path: Path) -> None:
    """``system:tool`` and ``user:tool`` are refused by the selector parser; a drive letter is still a path."""
    for text in ("system:tool", "user:tool", "USER:tool@1.0"):
        with pytest.raises(CatalogError, match="no longer names a mode"):
            parse_selector(text)
    assert parse_selector("C:\\opt\\tool").kind == "path"


def test_an_unknown_name_is_reported_by_the_catalog(tmp_path: Path) -> None:
    """A name that the root and the registry do not have is a catalog error."""
    with pytest.raises(CatalogError, match="not found"):
        resolve(Machine(tmp_path), "ghost")


# ---------------------------------------------------------------------------
# A path outside the root is a distribution folder, never a package of the scope
# ---------------------------------------------------------------------------


def test_a_path_into_the_other_modes_root_is_a_folder_to_copy_in_not_a_package_of_this_scope(tmp_path: Path) -> None:
    """A package folder in the system root, named from a user run, is outside the user root: the scope stays the user's."""
    machine = Machine(tmp_path)
    package = machine.package(machine.system_root, "tool", installed=True)

    result = resolve(machine, str(package))

    assert result.scope.root == machine.user_root and result.scope.mode is Mode.USER
    assert result.source == package / "1.0.0"


def test_a_normal_process_names_a_distribution_folder_and_in_place_keeps_it(tmp_path: Path) -> None:
    """A distribution folder is copied into the root unless ``--in-place`` is given."""
    machine = Machine(tmp_path)
    folder = machine.distribution()

    copied = resolve(machine, str(folder))
    kept = resolve(machine, str(folder), in_place=True)

    assert (copied.scope.mode, copied.source) == (Mode.USER, folder)
    assert (kept.scope.mode, kept.source) == (Mode.USER, None)


def test_an_administrator_shell_may_name_a_folder_outside_the_root_to_copy_in(tmp_path: Path) -> None:
    """From an administrator shell a distribution folder is marked as a folder to copy into the system root, and is not read."""
    machine = Machine(tmp_path, elevated=True)
    folder = machine.distribution()

    result = resolve(machine, str(folder))

    assert (result.scope.mode, result.scope.root) == (Mode.SYSTEM, machine.system_root)
    assert result.source == folder and result.resolved is None


def test_an_administrator_shell_cannot_keep_a_user_writable_folder_in_place(tmp_path: Path) -> None:
    """``--in-place`` in system mode needs an admin-only folder; a user-writable one is refused."""
    downloads = tmp_path / "downloads"
    machine = Machine(tmp_path, elevated=True, untrusted={downloads})
    folder = machine.distribution()

    with pytest.raises(ModeRefused, match="admin-only"):
        resolve(machine, str(folder), in_place=True)


# ---------------------------------------------------------------------------
# D31: what an elevated run trusts
# ---------------------------------------------------------------------------


def test_the_elevated_child_accepts_a_package_name_and_a_path_inside_the_system_root(tmp_path: Path) -> None:
    """The relaunched elevated gupkg resolves names and paths inside the admin-only system root."""
    machine = Machine(tmp_path, elevated=True)
    package = machine.package(machine.system_root, "tool", record="system", installed=True)

    by_name = resolve(machine, "tool", relaunched=True)
    by_path = resolve(machine, str(package / "1.0.0"), relaunched=True)

    assert by_name.scope.mode is by_path.scope.mode is Mode.SYSTEM
    assert by_path.scope.root == machine.system_root


def test_the_elevated_child_refuses_a_folder_outside_the_root(tmp_path: Path) -> None:
    """A path that is neither a name nor inside the system root is refused by the relaunched elevated gupkg (T1)."""
    machine = Machine(tmp_path, elevated=True)

    with pytest.raises(ModeRefused, match="only package names and paths inside the system root"):
        resolve(machine, str(machine.distribution()), relaunched=True)


def test_the_elevated_child_refuses_a_user_writable_package_folder_in_the_system_root(tmp_path: Path) -> None:
    """A package folder inside the system root that users can modify is refused in an elevated run."""
    machine = Machine(tmp_path, elevated=True, untrusted={tmp_path / "sys" / "opt" / "tool"})
    package = machine.package(machine.system_root, "tool")

    with pytest.raises(ModeRefused, match="Refusing"):
        resolve(machine, str(package), relaunched=True)


def test_an_elevated_process_refuses_a_package_whose_install_record_says_user(tmp_path: Path) -> None:
    """A package in the system root whose record says ``user`` is refused in an elevated run (D31)."""
    machine = Machine(tmp_path, elevated=True)
    machine.package(machine.system_root, "tool", record="user")

    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        resolve(machine, "tool")


def test_an_elevated_process_does_not_trust_a_record_in_a_folder_users_can_modify(tmp_path: Path) -> None:
    """A system record kept in a folder that is not admin-only is refused rather than obeyed."""
    untrusted = tmp_path / "sys" / "opt" / "tool" / ".gupkg"
    machine = Machine(tmp_path, elevated=True, untrusted={untrusted})
    machine.package(machine.system_root, "tool", record="system")

    with pytest.raises(ModeRefused, match="does not trust it"):
        resolve(machine, "tool")


def test_an_elevated_process_never_reads_the_user_settings_or_the_user_root(tmp_path: Path) -> None:
    """The user layer is not loaded and the user root is not scanned when the process is elevated (design 9.4, D18)."""
    machine = Machine(tmp_path, elevated=True)
    user_settings = machine.user_root / ".gupkg" / "settings.toml"
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text("schema = 4\n[integration]\nbin = 'C:\\\\evil'\n", encoding="utf-8")
    machine.package(machine.user_root, "tool", record="user", installed=True)
    registry_package(machine)

    result = resolve(machine, "tool")

    assert not [path for path in machine.reads if machine.user_root in (path, *path.parents)]
    assert result.scope.mode is Mode.SYSTEM and result.scope.bin_dir != Path("C:\\evil")
    assert result.resolved is not None and result.resolved.view.local is None
