"""Cover how one target resolves to an install mode and context (design 6.2, 6.3, 9.6, D4, D31, D42).

Behavior covered: ``default_mode = "auto"`` resolves system for an elevated process and user otherwise, never
portable, with the printed reason; an install record wins over ``auto`` and a path inside a configured root keeps
that root's mode; an elevated process refuses user and portable work (an existing user package, a path inside a
user root however it is spelled or linked, ``--mode user``, ``user:name``, an untrusted record) and changes nothing
under the user root; the elevated run ignores user settings and a ``--settings`` file outside the system root;
the elevated child accepts only names and paths inside the system root, while a direct administrator shell may
name a distribution folder to copy in; a package's ``modes`` list is enforced before any work; ``--mode portable``
installs a ``<name>\\<version>`` folder in place, otherwise uses the configured portable root, or asks (exit 5
under ``--no-input``), and never uses the system root; a same-name package in two modes is reported as shadowing.

Real: ``resolve_mode``, the catalog, settings layering and install records on real folders and real junctions
below pytest's temporary folder. Fake: ``FakeSystem`` for elevation, the admin-only check and the machine's
default folders. Out of scope: the relaunch itself (``test_mode_elevation.py``), the copy of a distribution
(``test_distribution_copy.py``) and what the engine does after resolving (issue 028).
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from gupkg.catalog.catalog import CatalogError
from gupkg.model.mode import InputNeeded, ModeRefused
from gupkg.modes import ModeRequest, resolve_mode
from tests.catalog_support import build_registry, version_text
from tests.mode_support import Machine, tree_snapshot

DEFAULT_MODES_WITH_PORTABLE = '["system", "user", "portable"]'


@pytest.fixture(autouse=True)
def _no_ambient_elevation_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher's ``GUPKG_ELEVATED`` marker is pinned off so elevation comes only from the fake machine."""
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)


def resolve(machine: Machine, selector: str, **options):
    """Resolve *selector* on *machine* the way the engine will: through the settings loader and the catalog."""
    loader_options = {key: options.pop(key) for key in ("settings_path",) if key in options}
    return resolve_mode(
        ModeRequest(selector, **options), system=machine.fake, env=machine.env, load=machine.loader(**loader_options)
    )


def registry_package(machine: Machine, name: str = "tool", modes: str | None = None) -> None:
    """Publish *name* in the registry cache of the system root (used by every mode when nothing is local)."""
    cache = build_registry(machine.system_root, {name: {"versions": ["1.0.0"]}})
    if modes is not None:
        manifest = next(cache.rglob("1.0.0")) / "pkg.toml"
        manifest.write_text(version_text("1.0.0").replace("format = 2\n", f"format = 2\nmodes = {modes}\n", 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# auto and the order of design 6.2
# ---------------------------------------------------------------------------


def test_auto_in_an_elevated_process_resolves_system_with_its_reason(tmp_path: Path) -> None:
    """An elevated process with no flag, record or containing root resolves ``system (auto: elevated)``."""
    machine = Machine(tmp_path, elevated=True)
    registry_package(machine)

    result = resolve(machine, "tool")

    assert str(result.choice) == "system (auto: elevated)"
    assert (result.context.mode, result.context.root, result.context.requires_elevation) == ("system", machine.system_root, True)
    assert result.context.reason == "auto: elevated"


def test_auto_in_a_normal_process_resolves_user(tmp_path: Path) -> None:
    """A non-elevated process with nothing else deciding resolves ``user (auto: not elevated)``."""
    machine = Machine(tmp_path)
    registry_package(machine)

    result = resolve(machine, "tool")

    assert str(result.choice) == "user (auto: not elevated)"
    assert (result.context.root, result.context.env_target, result.context.requires_elevation) == (machine.user_root, "HKCU", False)


def test_auto_never_resolves_portable_even_when_the_package_only_supports_it(tmp_path: Path) -> None:
    """A portable-only package is not installed portable by ``auto``: it is refused for user mode instead."""
    machine = Machine(tmp_path)
    registry_package(machine, modes='["portable"]')

    with pytest.raises(ModeRefused, match="does not support user mode"):
        resolve(machine, "tool")


def test_an_elevated_process_refuses_a_package_without_system_and_names_the_normal_shell(tmp_path: Path) -> None:
    """With ``auto`` an elevated run needs system; a user-only package is refused and told to use a normal shell."""
    machine = Machine(tmp_path, elevated=True)
    registry_package(machine, modes='["user"]')

    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        resolve(machine, "tool")


def test_default_mode_from_settings_decides_when_nothing_earlier_does(tmp_path: Path) -> None:
    """``default_mode = "system"`` replaces ``auto`` for a non-elevated run, and the reason says so."""
    machine = Machine(tmp_path)
    machine.write_app_settings(top='default_mode = "system"\n')
    registry_package(machine)

    result = resolve(machine, "tool")

    assert str(result.choice) == "system (default_mode)"
    assert result.context.requires_elevation


def test_an_existing_user_package_resolves_user_because_the_record_wins_over_auto(tmp_path: Path) -> None:
    """In a normal process an installed user-mode package stays user: its install record decides, not ``auto``."""
    machine = Machine(tmp_path)
    machine.package(machine.user_root, "tool", record="user", installed=True)
    machine.write_app_settings(top='default_mode = "system"\n')

    result = resolve(machine, "tool")

    assert (result.choice.mode, result.choice.reason) == ("user", "install record")
    assert result.context.root == machine.user_root


def test_an_existing_system_package_keeps_system_for_a_normal_process_and_needs_elevation(tmp_path: Path) -> None:
    """A system package named from a normal shell resolves system (not user) and says it needs elevation."""
    machine = Machine(tmp_path)
    machine.package(machine.system_root, "tool", record="system", installed=True)

    result = resolve(machine, "tool")

    assert (result.choice.mode, result.context.requires_elevation) == ("system", True)


@pytest.mark.parametrize("mode", ["system", "user", "portable"])
def test_a_path_inside_a_configured_root_keeps_that_roots_mode(tmp_path: Path, mode: str) -> None:
    """A path target in a configured root resolves that root's mode, whatever ``auto`` would say."""
    machine = Machine(tmp_path)
    root = {"system": machine.system_root, "user": machine.user_root, "portable": machine.portable_root}[mode]
    package = machine.package(root, "tool", modes=DEFAULT_MODES_WITH_PORTABLE)

    result = resolve(machine, str(package / "1.0.0"))

    assert (result.choice.mode, result.choice.reason) == (mode, f"inside the {mode} root")
    assert result.context.root == root


def test_a_flag_that_contradicts_the_packages_own_mode_is_refused(tmp_path: Path) -> None:
    """``--mode system`` on a package under the user root is refused: a package belongs to one mode."""
    machine = Machine(tmp_path)
    package = machine.package(machine.user_root, "tool", record="user")

    with pytest.raises(ModeRefused, match="belongs to one mode"):
        resolve(machine, str(package), explicit_mode="system")


def test_a_qualified_selector_selects_that_roots_package(tmp_path: Path) -> None:
    """``system:tool`` names the system root's package even when a user package of the same name exists."""
    machine = Machine(tmp_path)
    machine.package(machine.system_root, "tool", record="system")
    machine.package(machine.user_root, "tool", record="user")

    result = resolve(machine, "system:tool")

    assert (result.choice.mode, result.choice.reason) == ("system", "selector")
    assert result.resolved.view.path == machine.system_root / "tool"


def test_the_mode_flag_alone_chooses_the_mode_of_a_package_that_is_not_installed(tmp_path: Path) -> None:
    """``--mode system`` on a registry package resolves system without asking the elevation of the process."""
    machine = Machine(tmp_path)
    registry_package(machine)

    result = resolve(machine, "tool", explicit_mode="system")

    assert (str(result.choice), result.context.requires_elevation) == ("system (--mode)", True)


# ---------------------------------------------------------------------------
# D31: an elevated process never acts on a user root
# ---------------------------------------------------------------------------


def _user_package(machine: Machine) -> Path:
    """Create an installed user-mode package and return its folder."""
    return machine.package(machine.user_root, "tool", record="user", installed=True)


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(lambda m, p: str(p), id="plain-path"),
        pytest.param(lambda m, p: str(p / "1.0.0"), id="version-directory"),
        pytest.param(lambda m, p: str(p / "current"), id="current-junction"),
        pytest.param(lambda m, p: str(m.system_root / ".." / ".." / "home" / "opt" / "tool"), id="dotdot-from-the-system-root"),
        pytest.param(lambda m, p: "\\\\?\\" + str(p), id="extended-length-prefix"),
        pytest.param(lambda m, p: str(p).upper(), id="other-letter-case"),
        pytest.param(lambda m, p: str(p) + ".", id="trailing-dot"),
    ],
)
def test_an_elevated_process_refuses_a_path_inside_a_user_root_however_it_is_spelled(tmp_path: Path, spelling) -> None:
    """The root that contains a path is decided by identity, so no spelling reaches the user root from an elevated run."""
    machine = Machine(tmp_path, elevated=True)
    package = _user_package(machine)
    before = tree_snapshot(machine.user_root)

    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        resolve(machine, spelling(machine, package))

    assert tree_snapshot(machine.user_root) == before


@pytest.mark.windows_integration
def test_an_elevated_process_refuses_the_8_3_short_name_of_a_user_root_package(tmp_path: Path) -> None:
    """The short (8.3) spelling of a package folder in a user root is the same folder, so it is refused too.

    On a volume without short names the short spelling equals the long one and the test still holds.
    """
    machine = Machine(tmp_path, elevated=True)
    package = machine.package(machine.user_root, "averylongpackagename", record="user", installed=True)
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(package), buffer, 1024)

    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        resolve(machine, buffer.value or str(package))


def test_an_elevated_process_refuses_a_junction_that_leaves_the_system_root_for_a_user_root(tmp_path: Path) -> None:
    """A junction in the system root that points into the user root does not make the package a system package."""
    machine = Machine(tmp_path, elevated=True)
    package = _user_package(machine)
    machine.system_root.mkdir(parents=True)
    machine.fake.create_junction(machine.system_root / "alias", package)

    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        resolve(machine, str(machine.system_root / "alias"))


@pytest.mark.parametrize(
    ("selector", "options"),
    [
        pytest.param("tool", {"explicit_mode": "user"}, id="mode-user-flag"),
        pytest.param("user:tool", {}, id="qualified-selector"),
        pytest.param("tool", {"explicit_mode": "portable"}, id="mode-portable-flag"),
    ],
)
def test_an_elevated_process_refuses_an_explicit_user_or_portable_mode(tmp_path: Path, selector: str, options: dict) -> None:
    """``--mode user``, ``user:name`` and ``--mode portable`` are refused in an elevated process, before any lookup."""
    machine = Machine(tmp_path, elevated=True)
    _user_package(machine)
    before = tree_snapshot(machine.user_root)
    machine.reads.clear()

    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        resolve(machine, selector, **options)

    assert tree_snapshot(machine.user_root) == before
    assert not [path for path in machine.reads if machine.user_root in path.parents]


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
    user_settings.write_text("schema = 3\ndefault_mode = 'user'\n[integration.system]\nbin = 'C:\\\\evil'\n", encoding="utf-8")
    registry_package(machine)

    result = resolve(machine, "tool")

    assert user_settings not in machine.reads
    assert result.choice.mode == "system"
    assert result.context.bin_dir != Path("C:\\evil")


def test_an_elevated_process_ignores_a_settings_file_outside_the_system_root(tmp_path: Path) -> None:
    """``--settings`` outside the admin-only system root has no effect on an elevated run; inside it, it applies."""
    machine = Machine(tmp_path, elevated=True)
    registry_package(machine)
    outside = tmp_path / "elsewhere.toml"
    outside.write_text(f"schema = 3\n[integration.system]\nbin = '{tmp_path / 'outside-bin'}'\n", encoding="utf-8")
    inside = machine.system_root / "chosen.toml"
    inside.write_text(f"schema = 3\n[integration.system]\nbin = '{tmp_path / 'inside-bin'}'\n", encoding="utf-8")

    ignored = resolve(machine, "tool", settings_path=outside)
    applied = resolve(machine, "tool", settings_path=inside)

    assert ignored.context.bin_dir != tmp_path / "outside-bin"
    assert applied.context.bin_dir == tmp_path / "inside-bin"


# ---------------------------------------------------------------------------
# Elevated admission: names and system-root paths; a distribution folder from an administrator shell
# ---------------------------------------------------------------------------


def test_the_elevated_child_accepts_a_package_name_and_a_path_inside_the_system_root(tmp_path: Path) -> None:
    """The relaunched elevated gupkg resolves names and paths inside the admin-only system root."""
    machine = Machine(tmp_path, elevated=True)
    package = machine.package(machine.system_root, "tool", record="system", installed=True)

    by_name = resolve(machine, "tool", relaunched=True)
    by_path = resolve(machine, str(package / "1.0.0"), relaunched=True)

    assert by_name.context.mode == by_path.context.mode == "system"
    assert by_path.context.root == machine.system_root


def test_the_elevated_child_refuses_a_folder_outside_every_root(tmp_path: Path) -> None:
    """A path that is neither a name nor inside the system root is refused by the relaunched elevated gupkg (T1)."""
    machine = Machine(tmp_path, elevated=True)
    folder = machine.distribution()

    with pytest.raises(ModeRefused, match="only package names and paths inside the system root"):
        resolve(machine, str(folder), relaunched=True)


def test_the_elevated_child_refuses_a_user_writable_package_folder_in_the_system_root(tmp_path: Path) -> None:
    """A package folder inside the system root that users can modify is refused in an elevated run."""
    machine = Machine(tmp_path, elevated=True, untrusted={tmp_path / "sys" / "opt" / "tool"})
    package = machine.package(machine.system_root, "tool")

    with pytest.raises(ModeRefused, match="Refusing"):
        resolve(machine, str(package), relaunched=True)


def test_an_administrator_shell_may_name_a_folder_outside_every_root_to_copy_in(tmp_path: Path) -> None:
    """From an administrator shell a distribution folder resolves system mode, marked as a folder to copy into the root."""
    machine = Machine(tmp_path, elevated=True)
    folder = machine.distribution()

    result = resolve(machine, str(folder), explicit_mode="system")

    assert (str(result.choice), result.context.root) == ("system (--mode)", machine.system_root)
    assert result.source == folder
    assert result.resolved is None


def test_an_administrator_shell_cannot_keep_a_user_writable_folder_in_place_for_system_mode(tmp_path: Path) -> None:
    """``--in-place`` with system mode needs an admin-only folder; a user-writable one is refused."""
    downloads = tmp_path / "downloads"
    machine = Machine(tmp_path, elevated=True, untrusted={downloads})
    folder = machine.distribution()

    with pytest.raises(ModeRefused, match="admin-only"):
        resolve(machine, str(folder), in_place=True)


def test_a_normal_process_names_a_distribution_folder_for_user_mode_and_in_place_keeps_it(tmp_path: Path) -> None:
    """A distribution folder is copied into the mode's root unless ``--in-place`` is given."""
    machine = Machine(tmp_path)
    folder = machine.distribution()

    copied = resolve(machine, str(folder))
    kept = resolve(machine, str(folder), in_place=True)

    assert (copied.choice.mode, copied.source) == ("user", folder)
    assert (kept.choice.mode, kept.source) == ("user", None)


# ---------------------------------------------------------------------------
# The package's modes list
# ---------------------------------------------------------------------------


def test_an_unsupported_mode_is_refused_before_any_change(tmp_path: Path) -> None:
    """A package whose ``modes`` lack the resolved mode is refused up front, and nothing is created."""
    machine = Machine(tmp_path)
    package = machine.package(machine.user_root, "tool", modes='["system"]')
    before = tree_snapshot(tmp_path)

    with pytest.raises(ModeRefused, match="does not support user mode"):
        resolve(machine, str(package))

    assert tree_snapshot(tmp_path) == before


def test_portable_requires_the_package_to_declare_it(tmp_path: Path) -> None:
    """``--mode portable`` on a package with the default ``modes`` is refused: portable is opt-in."""
    machine = Machine(tmp_path)
    registry_package(machine)

    with pytest.raises(ModeRefused, match="does not support portable mode"):
        resolve(machine, "tool", explicit_mode="portable")


# ---------------------------------------------------------------------------
# D42: the portable target
# ---------------------------------------------------------------------------


def test_portable_on_a_name_version_folder_installs_it_in_place_and_copies_nothing(tmp_path: Path) -> None:
    """``--mode portable`` on ``<name>\\<version>\\pkg.toml`` makes the folder above ``<name>`` the portable root."""
    machine = Machine(tmp_path)
    folder = machine.distribution("tool", "2.0.0", modes=DEFAULT_MODES_WITH_PORTABLE)
    before = tree_snapshot(tmp_path)

    result = resolve(machine, str(folder), explicit_mode="portable")

    assert result.context.root == folder.parent.parent
    assert result.context.in_place and result.source is None
    assert result.context.bin_dir == folder.parent.parent / "bin"
    assert tree_snapshot(tmp_path) == before


def test_portable_on_any_other_target_uses_the_configured_portable_root(tmp_path: Path) -> None:
    """A name (not a folder shaped like a version directory) goes to the configured ``[roots].portable``."""
    machine = Machine(tmp_path)
    registry_package(machine, modes=DEFAULT_MODES_WITH_PORTABLE)

    result = resolve(machine, "tool", explicit_mode="portable")

    assert (result.context.mode, result.context.root, result.context.in_place) == ("portable", machine.portable_root, False)


def test_portable_with_no_configured_root_asks_for_one(tmp_path: Path) -> None:
    """With no portable root configured, gupkg asks, and uses the answer."""
    machine = Machine(tmp_path, portable_configured=False)
    registry_package(machine, modes=DEFAULT_MODES_WITH_PORTABLE)
    stick = tmp_path / "stick"

    result = resolve(machine, "tool", explicit_mode="portable", ask_portable_root=lambda: stick)

    assert result.context.root == stick


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"no_input": True, "ask_portable_root": lambda: Path("unused")}, id="no-input-never-asks"),
        pytest.param({}, id="nobody-can-be-asked"),
        pytest.param({"ask_portable_root": lambda: None}, id="the-question-is-declined"),
    ],
)
def test_portable_with_no_root_and_no_answer_fails_with_exit_5_and_changes_nothing(tmp_path: Path, options: dict) -> None:
    """Without a configured root, ``--no-input`` (or a declined question) is exit code 5 with no change."""
    machine = Machine(tmp_path, portable_configured=False)
    registry_package(machine, modes=DEFAULT_MODES_WITH_PORTABLE)
    before = tree_snapshot(tmp_path)

    with pytest.raises(InputNeeded) as raised:
        resolve(machine, "tool", explicit_mode="portable", **options)

    assert raised.value.exit_code == 5
    assert tree_snapshot(tmp_path) == before


def test_without_the_portable_flag_a_name_version_folder_is_not_installed_in_place(tmp_path: Path) -> None:
    """The in-place rule needs ``--mode portable``; ``auto`` treats the folder as a distribution for user mode."""
    machine = Machine(tmp_path)
    folder = machine.distribution("tool", "2.0.0", modes=DEFAULT_MODES_WITH_PORTABLE)

    result = resolve(machine, str(folder))

    assert (result.context.mode, result.context.in_place, result.source) == ("user", False, folder)


@pytest.mark.parametrize(
    "where",
    [
        pytest.param("asked-inside", id="asked-root-inside-the-system-root"),
        pytest.param("asked-the-root", id="asked-root-is-the-system-root"),
        pytest.param("in-place-above", id="in-place-root-contains-the-system-root"),
    ],
)
def test_a_portable_root_is_never_the_system_root_or_inside_it(tmp_path: Path, where: str) -> None:
    """Portable state is never machine-wide: a portable root that is, lies in, or contains the system root is refused."""
    machine = Machine(tmp_path, portable_configured=False)
    registry_package(machine, modes=DEFAULT_MODES_WITH_PORTABLE)
    if where == "in-place-above":
        folder = tmp_path / "sys" / "tool" / "1.0.0"
        folder.mkdir(parents=True)
        (folder / "pkg.toml").write_text(
            version_text("1.0.0").replace("format = 2\n", f"format = 2\nmodes = {DEFAULT_MODES_WITH_PORTABLE}\n", 1), encoding="utf-8"
        )
        selector, options = str(folder), {}
    else:
        answer = machine.system_root / "port" if where == "asked-inside" else machine.system_root
        selector, options = "tool", {"ask_portable_root": lambda: answer}

    with pytest.raises(ModeRefused, match="must not be the system root or lie inside it"):
        resolve(machine, selector, explicit_mode="portable", **options)


def test_a_portable_run_reads_the_settings_of_its_portable_root(tmp_path: Path) -> None:
    """``integrate = "none"`` in the portable root's own settings reaches the context (a USB-stick root)."""
    machine = Machine(tmp_path)
    folder = machine.distribution("tool", "2.0.0", modes=DEFAULT_MODES_WITH_PORTABLE)
    stick = folder.parent.parent
    settings = stick / ".gupkg" / "settings.toml"
    settings.parent.mkdir(parents=True)
    settings.write_text("schema = 3\n[integration.portable]\nintegrate = 'none'\n", encoding="utf-8")

    result = resolve(machine, str(folder), explicit_mode="portable")

    assert (result.context.integrate, result.context.start_menu, result.context.env_target) == ("none", None, None)
    assert result.context.path_line() is not None


# ---------------------------------------------------------------------------
# Warnings and settings consistency
# ---------------------------------------------------------------------------


def test_a_package_in_both_the_system_and_user_root_is_reported_as_shadowing(tmp_path: Path) -> None:
    """Resolving one of two same-name packages warns that their shims and Start Menu entries shadow each other."""
    machine = Machine(tmp_path)
    machine.package(machine.system_root, "tool", record="system")
    machine.package(machine.user_root, "tool", record="user")

    result = resolve(machine, "user:tool")

    assert any("shadowing: 'tool'" in warning for warning in result.warnings)


def test_a_package_that_exists_in_one_root_only_has_no_shadowing_warning(tmp_path: Path) -> None:
    """No warning is produced when the other mode's root has no package of that name."""
    machine = Machine(tmp_path)
    machine.package(machine.user_root, "tool", record="user")

    assert resolve(machine, "tool").warnings == ()


def test_a_distribution_installed_for_user_warns_when_a_system_package_has_its_name(tmp_path: Path) -> None:
    """Installing ``tool`` for the user while a system ``tool`` exists reports the shadowing before any work."""
    machine = Machine(tmp_path)
    machine.package(machine.system_root, "tool", record="system")
    folder = machine.distribution("tool")

    result = resolve(machine, str(folder))

    assert any("shadowing: 'tool'" in warning for warning in result.warnings)


def test_a_user_layer_that_moves_the_system_root_cannot_steer_a_system_run(tmp_path: Path) -> None:
    """If the user settings name another system root than the system layer would, a system-mode run is refused."""
    machine = Machine(tmp_path)
    machine.package(tmp_path / "other-system", "tool")
    user_settings = machine.user_root / ".gupkg" / "settings.toml"
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text(
        f"schema = 3\n[roots]\nsystem = '{tmp_path / 'other-system'}'\nuser = '{machine.user_root}'\n"
        f"portable = '{machine.portable_root}'\n", encoding="utf-8",
    )

    with pytest.raises(ModeRefused, match="roots differ"):
        resolve(machine, "tool", explicit_mode="system")


def test_an_unknown_name_is_reported_by_the_catalog(tmp_path: Path) -> None:
    """A name that no root and no registry has is a catalog error, not a silently chosen mode."""
    machine = Machine(tmp_path)

    with pytest.raises(CatalogError, match="not found"):
        resolve(machine, "ghost")
