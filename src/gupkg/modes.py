"""Resolve the install mode of one target into a ``ModeContext`` (design sections 6.2, 6.3 and 9.6).

Given a target (a name, ``mode:name``, ``registry/name`` or a path), the ``--mode`` flag and the settings
of the run, ``resolve_mode`` finds the install record and the containing root, applies the order of design
6.2 (flag, record, root, ``default_mode`` with its single meaning of ``auto``), checks the package's
``modes`` before any work, and returns the context every other subsystem reads. It also decides what an
elevated run may be given: package names and paths inside the admin-only system root; in a direct
administrator shell also a distribution folder outside every root, to be copied into the system root;
never a user or portable root, never the user's settings (D31, D18). For portable mode it implements D42
(an in-place ``<name>\\<version>\\pkg.toml`` folder, else the configured portable root, else a question that
``--no-input`` turns into exit code 5) and refuses a portable root that is, or lies in, the system root.

Usage and API
-------------
Build a loader with ``settings_loader(...)``, then call
``resolve_mode(ModeRequest(selector, ...), system=..., env=..., load=...)``. The result carries the
``ModeContext``, the ``ModeChoice`` (``system (auto: elevated)``), the catalog resolution, warnings (a
package of the same name in two modes shadows itself on ``PATH``), and, for a distribution folder, the
folder to copy in. ``scope_context(...)`` gives the 0.17 front end its destinations from the same data.
``ModeRefused`` and ``InputNeeded`` (from ``gupkg.model.mode``) carry the message and exit code.

Implementation Approach
-----------------------
Selection itself is the pure ``gupkg.model.mode.select_mode``; this module only gathers its inputs, always
in the order that keeps an elevated run away from user-writable places: the flag and the containing root
are checked before any package is read, an install record is read only from an admin-only folder in an
elevated run, and the user layer of the settings is never loaded when the run is elevated or acts on the
system root. Paths are compared by identity (real path, case, ``..``, ``\\\\?\\`` prefix, trailing dots,
junctions), so a junction cannot move a package into another root.

See Also
--------
gupkg.model.mode : the mode data and the selection rules.
gupkg.relaunch : the up-front elevation step for system mode.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from .catalog.catalog import Catalog, CatalogError, Resolved, Selector, UntrustedLocation, parse_selector
from .catalog.local import DATA_DIR_NAME, canonical_path, is_inside, strip_extended_prefix
from .model.manifest import MODES, ManifestError, parse_manifest
from .model.mode import (
    MODE_PORTABLE,
    MODE_SYSTEM,
    MODE_USER,
    DefaultFolders,
    InputNeeded,
    ModeChoice,
    ModeContext,
    ModeRefused,
    build_mode_context,
    select_mode,
)
from .model.settings import LAYER_DEFAULTS, Settings, default_layer, expand_env, merge_layers
from .model.version import MANIFEST_NAME, is_version_name
from .settingsload import LoadedSettings, load_settings, read_text_file
from .system.port import Scope, System, is_elevated_run

SettingsLoader = Callable[..., LoadedSettings]


@dataclass(frozen=True)
class ModeRequest:
    """One target and how the caller wants it treated.

    Attributes
    ----------
    selector : str
        ``name``, ``name@version``, ``mode:name``, ``registry/name`` or a path.
    explicit_mode : str, optional
        The ``--mode`` flag.
    in_place : bool
        ``--in-place``: keep a distribution folder where it is instead of copying it into the root.
    relaunched : bool
        This process is the elevated child that a non-elevated gupkg started; it accepts only package
        names and paths inside the system root (design 9.6 rule 2).
    no_input : bool
        ``--no-input``: a question becomes ``InputNeeded`` (exit code 5).
    ask_portable_root : callable, optional
        Asks the user for a portable root and returns it, or ``None`` when declined.
    """

    selector: str
    explicit_mode: str | None = None
    in_place: bool = False
    relaunched: bool = False
    no_input: bool = False
    ask_portable_root: Callable[[], Path | None] | None = None


@dataclass(frozen=True)
class ResolvedMode:
    """The outcome of ``resolve_mode``.

    ``source`` is set when the target is a distribution folder outside every root that has to be
    copied into ``context.root`` before the install (design 21.4); ``resolved`` is the catalog's view
    of the target, or ``None`` when it is not read (an elevated run never reads a folder that users can modify).
    """

    context: ModeContext
    choice: ModeChoice
    settings: Settings
    resolved: Resolved | None = None
    warnings: tuple[str, ...] = ()
    source: Path | None = None


# ---------------------------------------------------------------------------
# Resolving one target
# ---------------------------------------------------------------------------


def resolve_mode(
    request: ModeRequest,
    *,
    system: System,
    env: Mapping[str, str],
    load: SettingsLoader,
    elevated: bool | None = None,
) -> ResolvedMode:
    """Resolve the install mode, root and destinations of one target.

    Parameters
    ----------
    request : ModeRequest
        The target and the options that affect its mode.
    system : System
        The machine boundary (elevation, admin-only checks, default folders, junctions).
    env : mapping of str to str
        The environment used to expand configured paths.
    load : callable
        ``load(system_mode=False, portable_root=None)`` returns the ``LoadedSettings`` of the run
        (see ``settings_loader``).
    elevated : bool, optional
        Overrides the elevation reported by *system*.

    Returns
    -------
    ResolvedMode
        The context, the choice with its reason, the settings, the catalog resolution, warnings and
        the folder to copy in.

    Raises
    ------
    ModeRefused
        If the mode is not allowed (D31, unsupported ``modes``, a conflicting flag, an elevated run
        given a target it must not trust, a portable root in the system root).
    InputNeeded
        If a portable root is needed and none can be asked for (exit code 5).
    gupkg.catalog.catalog.CatalogError
        If the target is not found or is ambiguous.
    gupkg.model.settings.SettingsError
        If the settings cannot be used.
    """
    is_elevated = is_elevated_run(system) if elevated is None else elevated

    # Settings first: an elevated run reads only the admin-only system layers (the user layer is never loaded).
    first = load()
    settings = first.settings
    configured = {mode: Path(expand_env(settings[f"roots.{mode}"], env)) for mode in MODES}

    # Step 1 of 6.2: the flag or the selector's qualifier. A refusal of an elevated user request comes
    # before any package or path is looked at.
    parsed = parse_selector(request.selector, (entry.name for entry in settings.registries))
    explicit, source = request.explicit_mode, "--mode"
    if parsed.mode is not None:
        if explicit is not None and explicit != parsed.mode:
            raise ModeRefused(f"--mode {explicit} contradicts the qualifier of '{request.selector}'.")
        explicit, source = parsed.mode, "selector"
    if explicit is not None and explicit not in MODES:
        raise ModeRefused(f"Unknown install mode '{explicit}'; use one of {', '.join(MODES)}.")
    if is_elevated and explicit not in (None, MODE_SYSTEM):
        select_mode(explicit=explicit, explicit_source=source, elevated=True)

    # Steps 2 and 3: find the existing package (install record) or the root that contains the path.
    catalog = Catalog.from_settings(settings, system, env, elevated=is_elevated)
    selector_text = request.selector
    if parsed.kind != "path" and explicit is not None and parsed.mode is None:
        selector_text = f"{explicit}:{request.selector}"
    container: str | None = None
    outside = False
    if parsed.kind == "path":
        container = _container_mode(parsed.path, configured, catalog, is_elevated)
        if is_elevated and container not in (None, MODE_SYSTEM):
            select_mode(explicit=explicit, container=container, elevated=True)
        outside = container is None
    resolved: Resolved | None = None
    record: str | None = None
    record_trusted = True
    target_folder = _normalize(parsed.path) if parsed.kind == "path" else None

    if outside and is_elevated:
        # A folder outside every root is data from a place users can modify: it is neither read nor
        # trusted here. A direct administrator shell may copy it into the system root (design 21.4).
        if request.relaunched:
            raise ModeRefused(
                f"Refusing {target_folder}: the elevated gupkg accepts only package names and paths inside the "
                "system root from a non-elevated one (design 9.6). Install it from an administrator shell with "
                "the installed system gupkg, or name a package."
            )
    else:
        try:
            resolved = catalog.resolve(selector_text, for_mutation=True)
        except UntrustedLocation as exc:
            raise ModeRefused(f"{exc}. An elevated gupkg only acts on package names and paths inside the system root.") from exc
        view = resolved.view
        if view.local is not None:
            package_path = view.local.path
            record = view.record.mode if view.record is not None else None
            if parsed.kind != "path" and view.root is not None:
                container = view.root.mode
            if is_elevated and view.record is not None:
                reason = system.directory_untrusted_reason(package_path / DATA_DIR_NAME)
                record_trusted = reason is None

    # The package's own ``modes`` list, from the version that would be installed.
    supported = _supported_modes(resolved, target_folder if outside else None, is_elevated)

    # Step 4 and the rules after it.
    choice = select_mode(
        explicit=explicit, explicit_source=source, record=record, record_trusted=record_trusted,
        container=container, default_mode=settings["default_mode"], elevated=is_elevated, supported=supported,
    )

    # Where the package goes, and the settings layers of the final run.
    folder: Path | None = None
    root: Path | None = None
    in_place = False
    if choice.mode == MODE_PORTABLE:
        root, in_place = _portable_root(request, parsed, target_folder, container, resolved, settings, env, configured)
        _check_portable_root(root, configured)
        final = load(portable_root=root)
    elif choice.mode == MODE_SYSTEM and not is_elevated:
        final = load(system_mode=True)
        if {m: Path(expand_env(final.settings[f"roots.{m}"], env)) for m in MODES} != dict(configured):
            raise ModeRefused(
                "The roots differ between the system settings and the settings of a normal run (a user layer "
                "changes a root); fix the settings so that the system layer alone names the roots."
            )
    else:
        final = first
    if outside and not in_place:
        folder = _copy_source(request, target_folder, resolved, choice, system)

    context = build_mode_context(
        choice, final.settings, env, defaults=default_folders(system), root=root, in_place=in_place
    )
    warnings = _shadow_warnings(catalog, resolved, folder, choice, is_elevated)
    return ResolvedMode(context, choice, final.settings, resolved, warnings, folder)


# ---------------------------------------------------------------------------
# The 0.17 front end
# ---------------------------------------------------------------------------


def scope_context(
    scope: Scope,
    system: System | None,
    *,
    root: Path | None = None,
    bin_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ModeContext:
    """Return the destinations of a 0.17 ``Scope`` as a ``ModeContext`` (until issue 029 replaces that front end).

    Parameters
    ----------
    scope : Scope
        ``Scope.USER`` or ``Scope.MACHINE``.
    system : System or None
        The boundary that knows the machine's default Start Menu and ``bin`` folders; ``None`` uses the
        built-in ``%VAR%`` defaults of the settings.
    root : Path, optional
        The package root of the scope (the manager configuration names it); the built-in default otherwise.
    bin_dir : Path, optional
        The wrapper folder of the manager configuration, replacing the default one.
    env : mapping, optional
        The environment; the process environment by default.

    Returns
    -------
    ModeContext
        The context of the ``user`` or ``system`` mode.

    Raises
    ------
    ValueError
        If *scope* is ``Scope.AUTO``, or the machine does not identify a required location.
    """
    if scope == Scope.AUTO:
        raise ValueError("A scope context requires an explicit scope")
    mode = MODE_SYSTEM if scope == Scope.MACHINE else MODE_USER

    # The built-in defaults are the whole configuration here: the 0.17 manager supplies its own folders.
    settings = merge_layers([default_layer()])
    context = build_mode_context(
        ModeChoice(mode, f"--scope {scope.value}"), settings, os.environ if env is None else env,
        defaults=default_folders(system) if system is not None else None, root=root,
    )
    return replace(context, bin_dir=Path(bin_dir)) if bin_dir is not None else context


def settings_loader(
    *,
    system: System,
    env: Mapping[str, str],
    elevated: bool,
    app_dir: Path | None = None,
    installed_system_root: Path | None = None,
    settings_path: Path | None = None,
    read_text: Callable[[Path], str | None] = read_text_file,
) -> SettingsLoader:
    """Return the ``load(system_mode=False, portable_root=None)`` function that ``resolve_mode`` calls.

    The arguments are those of ``gupkg.settingsload.load_settings`` that stay fixed for a run.
    """

    def load(*, system_mode: bool = False, portable_root: Path | None = None) -> LoadedSettings:
        """Load the layers of this run for one mode."""
        return load_settings(
            system=system, env=env, elevated=elevated, system_mode=system_mode, app_dir=app_dir,
            installed_system_root=installed_system_root, settings_path=settings_path,
            portable_root=portable_root, read_text=read_text,
        )

    return load


def default_folders(system: System) -> dict[str, DefaultFolders]:
    """Return the machine's own default Start Menu and ``bin`` folders for ``system`` and ``user``.

    A mode whose folders the machine does not identify is left out; the settings' ``%VAR%`` defaults
    then apply and fail with a message that names the missing variable.
    """
    folders: dict[str, DefaultFolders] = {}
    for mode, scope in ((MODE_SYSTEM, Scope.MACHINE), (MODE_USER, Scope.USER)):
        try:
            found = system.scope_folders(scope)
        except ValueError:
            continue
        folders[mode] = DefaultFolders(found.shortcut_root, found.bin_dir)
    return folders


# ---------------------------------------------------------------------------
# Inputs of the selection
# ---------------------------------------------------------------------------


def _normalize(path: Path) -> Path:
    """Return *path* absolute, without ``..``, and without an extended-length prefix."""
    return Path(os.path.abspath(strip_extended_prefix(os.fspath(Path(path).expanduser()))))


def _container_mode(path: Path, configured: Mapping[str, Path], catalog: Catalog, elevated: bool) -> str | None:
    """Return the mode of the root that contains *path* by identity, or ``None`` when no root does.

    Configured roots are compared first (an elevated run needs the user and portable roots here, only to
    refuse them); a normal run then also considers the ad-hoc roots that ``state.toml`` records.
    """
    for mode, root in configured.items():
        if canonical_path(path) == canonical_path(root) or is_inside(path, root):
            return mode
    if elevated:
        return None
    for root in catalog.roots:
        if not root.adhoc:
            continue
        if root.single and canonical_path(root.path) == canonical_path(path):
            return root.mode
        if not root.single and (canonical_path(path) == canonical_path(root.path) or is_inside(path, root.path)):
            return root.mode
    return None


def _manifest_modes(manifest_path: Path) -> tuple[str, ...] | None:
    """Return the ``modes`` of the ``pkg.toml`` at *manifest_path*, or ``None`` when it cannot be read."""
    try:
        return parse_manifest(manifest_path.read_text(encoding="utf-8-sig")).modes
    except (OSError, UnicodeDecodeError, ManifestError, ValueError):
        return None


def _supported_modes(resolved: Resolved | None, folder: Path | None, elevated: bool) -> tuple[str, ...] | None:
    """Return the ``modes`` of the version that would be installed, or ``None`` when no version is known yet.

    An elevated run does not read a distribution folder before it is copied (the copy is checked instead).
    """
    if resolved is not None:
        view = resolved.view
        if view.local is not None and resolved.version is not None:
            version = view.local.version(resolved.version)
            if version is not None and version.manifest is not None:
                return version.manifest.modes
            return None
        if view.local is None and view.registry is not None and view.registry.versions:
            return _manifest_modes(view.registry.versions[-1].path / MANIFEST_NAME)
        return None
    if folder is not None and not elevated:
        return _manifest_modes(folder / MANIFEST_NAME)
    return None


# ---------------------------------------------------------------------------
# Portable mode (design 6.3, D42)
# ---------------------------------------------------------------------------


def _in_place_shape(folder: Path | None) -> bool:
    """Return whether *folder* is shaped ``<name>\\<version>\\pkg.toml`` (a version directory under a package folder)."""
    return (
        folder is not None
        and is_version_name(folder.name)
        and folder.parent.name != ""
        and folder.parent.parent != folder.parent
        and (folder / MANIFEST_NAME).is_file()
    )


def _portable_root(
    request: ModeRequest,
    parsed: Selector,
    folder: Path | None,
    container: str | None,
    resolved: Resolved | None,
    settings: Settings,
    env: Mapping[str, str],
    configured: Mapping[str, Path],
) -> tuple[Path, bool]:
    """Choose the portable root of an operation and say whether it comes from an in-place folder (D42)."""
    # A package that already lives in a portable root keeps that root.
    if container == MODE_PORTABLE and resolved is not None and resolved.view.root is not None:
        root = resolved.view.root.path
        return (root.parent if resolved.view.root.single else root), False

    # ``--mode portable`` on a folder shaped <name>\<version>\pkg.toml installs it in place: the folder above <name> is the root.
    if parsed.kind == "path" and container is None and _in_place_shape(folder):
        return folder.parent.parent, True

    # Otherwise the configured portable root. The built-in default is not "configured" (D42): gupkg asks.
    if settings.source_of("roots.portable") != LAYER_DEFAULTS:
        return configured[MODE_PORTABLE], False
    answer = None if request.no_input or request.ask_portable_root is None else request.ask_portable_root()
    if answer is None:
        raise InputNeeded(
            "No portable root is configured and none was given: set [roots].portable in settings.toml, or name a "
            "folder shaped <name>\\<version>\\pkg.toml with --mode portable to install it in place."
        )
    return _normalize(answer), False


def _check_portable_root(root: Path, configured: Mapping[str, Path]) -> None:
    """Refuse a portable root that could be machine-wide or that user-mode scanning would also see.

    Forbidden: a root that is the system root, lies inside it, or contains it (portable is never
    machine-wide, 6.3), and a root that is the user root or lies inside it (one folder, two modes).
    A root that merely contains the user root, such as the profile folder, is allowed.
    """
    system_root, user_root = configured[MODE_SYSTEM], configured[MODE_USER]
    same_system = canonical_path(root) == canonical_path(system_root)
    reasons = (
        (same_system, f"is the system root ({system_root})"),
        (is_inside(root, system_root), f"lies inside the system root ({system_root})"),
        (is_inside(system_root, root), f"contains the system root ({system_root})"),
    )
    for fired, text in reasons:
        if fired:
            raise ModeRefused(
                f"The portable root {root} {text}. Portable is never machine-wide: state kept inside the system "
                "root would be shared, writable state next to code that an elevated gupkg runs."
            )
    if canonical_path(root) == canonical_path(user_root) or is_inside(root, user_root):
        relation = "is" if canonical_path(root) == canonical_path(user_root) else "lies inside"
        raise ModeRefused(
            f"The portable root {root} {relation} the user root ({user_root}); packages there are user-mode "
            "packages. Choose a folder outside the user root (a folder that contains it is fine)."
        )


# ---------------------------------------------------------------------------
# Distribution folders and shadowing
# ---------------------------------------------------------------------------


def _copy_source(
    request: ModeRequest, folder: Path | None, resolved: Resolved | None, choice: ModeChoice, system: System
) -> Path | None:
    """Return the folder to copy into the root, or ``None`` for ``--in-place`` (design 21.4).

    A system install never keeps a user-writable folder in place, so ``--in-place`` with ``--mode system``
    needs a folder that is admin-only.
    """
    if folder is None:
        return None
    if request.in_place:
        if choice.mode == MODE_SYSTEM:
            reason = system.directory_untrusted_reason(folder)
            if reason is not None:
                raise ModeRefused(f"--in-place with system mode needs an admin-only folder, and {folder} is not: {reason}")
        return None
    if (folder / MANIFEST_NAME).is_file():
        return folder
    if resolved is not None and resolved.version_path is not None:
        return resolved.version_path
    raise ModeRefused(f"{folder} is outside every root and is not a version directory (no {MANIFEST_NAME}); name the version folder.")


def _shadow_warnings(
    catalog: Catalog, resolved: Resolved | None, folder: Path | None, choice: ModeChoice, elevated: bool
) -> tuple[str, ...]:
    """Warn when the package also exists in the other of the system and user roots (their shims shadow each other)."""
    if elevated or choice.mode not in (MODE_SYSTEM, MODE_USER):
        return ()
    if resolved is not None:
        name = resolved.view.name
    elif folder is not None:
        name = folder.parent.name
    else:
        return ()
    other = MODE_USER if choice.mode == MODE_SYSTEM else MODE_SYSTEM
    try:
        found = catalog.resolve(f"{other}:{name}")
    except CatalogError:
        return ()
    if found.view.local is None or (resolved is not None and resolved.view.path == found.view.path):
        return ()
    return (
        f"shadowing: '{name}' exists in both the system and the user root; their shims on PATH and entries in "
        "the Start Menu shadow each other",
    )
