"""Build the one ``Scope`` of a gupkg run at process start (issue 035, design sections 6.1, 6.2 and 12).

The mode of a run is the mode of its token: an elevated process runs in system mode, every other process in user mode
(owner answer Q10: a machine where every process is elevated has only system mode, and there is no ``--user`` override).
Nothing selects a mode after this point; every subsystem receives the ``Scope`` that ``build_scope`` returns.

Usage and API
-------------
``build_scope(system=..., env=..., location=..., root_option=..., settings_path=..., read_text=...)`` returns the frozen
``gupkg.model.scope.Scope``; ``build_scope_and_layers`` also returns the report of each settings layer. It raises ``ModeRefused`` (exit code 2) when an elevated gupkg runs from a tree that standard
users can change, and ``gupkg.model.settings.SettingsError`` when a settings layer or ``--root`` is unusable.

Implementation Approach
-----------------------
An elevated run judges its own gupkg tree first and reads nothing before that, so a user-writable gupkg never lets
a file that a standard user can change decide anything for an administrator. Then the root comes from
``gupkg.settingsload.resolve_root`` (its fallback chain differs per mode) and the settings from
``gupkg.settingsload.load_mode_settings``. This is the one place that asks whether the process is elevated.

See Also
--------
gupkg.model.scope : the ``Scope`` data and the mode check of a package.
gupkg.settingsload : the root fallback chain and the settings layers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from .model.scope import DefaultFolders, ModeRefused, Scope, make_scope
from .selfmgmt.location import RunningGupkg
from .settingsload import LayerReport, load_mode_settings, read_root_layers, read_text_file, resolve_root
from .system.port import Mode, System, is_elevated_run

NOT_ADMIN_ONLY_REFUSAL = "an elevated gupkg must be installed in an admin-only root; run the system gupkg"


def default_folders(system: System, mode: Mode) -> DefaultFolders | None:
    """Return the machine's own default Start Menu ``opt`` folder and ``bin`` folder for *mode*, or ``None``.

    A mode whose folders the machine does not identify gives ``None``; the settings' ``%VAR%`` defaults then apply and
    fail with a message that names the missing variable.
    """
    try:
        found = system.scope_folders(mode)
    except ValueError:
        return None
    return DefaultFolders(found.shortcut_root, found.bin_dir)


def _require_admin_only_tree(location: RunningGupkg, system: System) -> None:
    """Refuse an elevated run whose gupkg tree (version directory, root, or the package folder) is not admin-only."""
    folders = [folder for folder in (location.version_dir, location.root) if folder is not None] or [location.package_dir]
    for folder in folders:
        try:
            reason = system.directory_untrusted_reason(folder)
        except OSError as error:
            reason = f"it could not be judged: {error}"
        if reason is not None:
            raise ModeRefused(f"{NOT_ADMIN_ONLY_REFUSAL} (this gupkg runs from {location.package_dir}; {folder} is not admin-only: {reason})")


def build_scope_and_layers(
    *,
    system: System,
    env: Mapping[str, str],
    location: RunningGupkg,
    root_option: Path | None = None,
    settings_path: Path | None = None,
    read_text: Callable[[Path], str | None] = read_text_file,
) -> tuple[Scope, tuple[LayerReport, ...]]:
    """Build the scope of this process and return it with the report of every settings layer.

    Parameters
    ----------
    system : System
        The machine boundary: elevation, admin-only checks and the default folders.
    env : mapping of str to str
        The environment used to expand ``%NAME%`` in configured paths.
    location : RunningGupkg
        Where the running gupkg lives (its ``$App`` folder, version directory and root).
    root_option : Path, optional
        ``--root DIR``.
    settings_path : Path, optional
        ``--settings FILE``; refused in system mode.
    read_text : callable, optional
        Returns a file's text, or ``None`` when it does not exist; tests record what is read.

    Returns
    -------
    tuple
        The frozen ``Scope`` every subsystem receives, and a ``LayerReport`` per settings layer (``settings path`` shows them).

    Raises
    ------
    ModeRefused
        If the process is elevated and the gupkg tree is not admin-only.
    gupkg.model.settings.SettingsError
        If a layer or ``--root`` is invalid, or ``--settings`` is given in system mode.
    """
    mode = Mode.SYSTEM if is_elevated_run(system) else Mode.USER
    if mode is Mode.SYSTEM:
        _require_admin_only_tree(location, system)
    installed_root = location.root if location.kind == "installed" else None
    app_layer, cli_layer = read_root_layers(
        mode, app_dir=location.app_dir, settings_path=settings_path, system=system, read_text=read_text,
    )
    choice = resolve_root(
        mode, root_option=root_option, installed_root=installed_root, app_layer=app_layer, cli_layer=cli_layer,
        env=env, system=system,
    )
    loaded = load_mode_settings(
        mode, choice.path, app_dir=location.app_dir, settings_path=settings_path, system=system, env=env, read_text=read_text,
    )
    scope = make_scope(mode, choice.path, choice.source, loaded.settings, env, defaults=default_folders(system, mode))
    return scope, loaded.layers


def build_scope(
    *,
    system: System,
    env: Mapping[str, str],
    location: RunningGupkg,
    root_option: Path | None = None,
    settings_path: Path | None = None,
    read_text: Callable[[Path], str | None] = read_text_file,
) -> Scope:
    """Build the scope of this process: the mode of its token, its root and its settings (see ``build_scope_and_layers``)."""
    return build_scope_and_layers(
        system=system, env=env, location=location, root_option=root_option, settings_path=settings_path, read_text=read_text,
    )[0]
