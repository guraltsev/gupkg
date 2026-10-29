"""Find, filter and read the settings layers of one run (design sections 6.1, 6.2 and 11).

Usage and API
-------------
``resolve_root(mode, ...)`` returns the ``RootChoice`` of a run: the root folder and the source that named it.
``load_mode_settings(mode, root, ...)`` returns a ``LoadedSettings``: the merged ``Settings`` (each value knows its
layer) and a ``LayerReport`` per layer that says whether it was applied or missing. ``settings_file(root)`` names the
settings file of a root. ``read_root_layers`` reads the two layers that name a root before the root is known.

Implementation Approach
-----------------------
Each mode finds its root by a fixed chain of fallbacks (``resolve_root``), reads only its own keys, and never looks at the
other mode's root. The layers are read lowest priority first: built-in defaults of the mode, ``$App\\settings.toml`` of
the running gupkg, ``<root>\\.gupkg\\settings.toml`` and, in user mode only, ``--settings``. ``[roots]`` is allowed in
the second and fourth layer and refused in a root's own file. Nothing is ever read from AppData or ProgramData. In system
mode every file must sit in an admin-only folder (``System.directory_untrusted_reason``): a layer that fails that check
stops the run, because an elevated run must not trust a folder a standard user can change. Reading goes through an
injectable function so that tests never touch a real machine.
"""

from __future__ import annotations

import ntpath
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .system.port import Mode, System
from .model.settings import (
    LAYER_APP,
    LAYER_CLI,
    LAYER_DEFAULTS,
    LAYER_ROOT,
    Layer,
    Settings,
    SettingsError,
    default_layer,
    expand_env,
    merge_layers,
)

DATA_DIR_NAME = ".gupkg"
SETTINGS_FILE_NAME = "settings.toml"

SOURCE_ROOT_OPTION = "--root"
SOURCE_INSTALLED_SYSTEM = "the installed system gupkg"
SOURCE_INSTALLED_USER = "the root this gupkg is installed in"
SOURCE_SETTINGS_OPTION = "--settings"
SOURCE_APP = "$App\\settings.toml"
SOURCE_BUILT_IN = "built-in default"


@dataclass(frozen=True)
class LayerReport:
    """What happened to one layer: ``applied``, ``missing`` or ``ignored`` (with the reason)."""

    name: str
    path: Path | None
    status: str
    reason: str = ""


@dataclass(frozen=True)
class LoadedSettings:
    """The merged settings and a report of every layer."""

    settings: Settings
    layers: tuple[LayerReport, ...]

    def root(self, key: str, env: Mapping[str, str]) -> Path:
        """Return the expanded path of a ``roots.*`` or ``integration.*`` setting."""
        return Path(expand_env(self.settings[key], env))


@dataclass(frozen=True)
class RootChoice:
    """The root of a run and what named it (``--root``, ``$App\\settings.toml``, ``built-in default`` ...)."""

    path: Path
    source: str


def settings_file(root: Path) -> Path:
    """Return the settings file of *root*: ``<root>\\.gupkg\\settings.toml``."""
    return Path(root) / DATA_DIR_NAME / SETTINGS_FILE_NAME


def read_text_file(path: Path) -> str | None:
    """Read a UTF-8 text file, returning ``None`` when it does not exist."""
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None


def _untrusted_reason(folder: Path, system: System) -> str | None:
    """Return why *folder* (or its nearest existing parent) is not admin-only, or ``None`` when it is.

    Raises
    ------
    OSError
        If the folder cannot be judged.
    """
    while not folder.exists() and folder.parent != folder:
        folder = folder.parent
    return system.directory_untrusted_reason(folder)


def _admin_only(folder: Path, system: System) -> bool:
    """Return whether *folder* is admin-only; a folder that cannot be judged counts as not admin-only."""
    try:
        return _untrusted_reason(folder, system) is None
    except OSError:
        return False


def _absolute(path: Path, what: str) -> Path:
    """Return *path* when it is absolute with a drive, else refuse it."""
    text = str(path)
    if not ntpath.isabs(text) or not ntpath.splitdrive(text)[0]:
        raise SettingsError(f"{what} must be an absolute path with a drive, not '{text}'")
    return path


def _configured_root(data: Mapping[str, Any] | None, mode: str, env: Mapping[str, str], where: str) -> Path | None:
    """Return ``[roots].<mode>`` of a parsed layer, expanded, or ``None`` when the layer does not set it."""
    roots = (data or {}).get("roots")
    if not isinstance(roots, Mapping) or mode not in roots:
        return None
    value = roots[mode]
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{where}: roots.{mode} must be a non-empty string")
    return _absolute(Path(expand_env(value, env)), f"roots.{mode} of {where}")


def resolve_root(
    mode: str,
    *,
    root_option: Path | None,
    installed_root: Path | None,
    app_layer: Mapping[str, Any] | None,
    cli_layer: Mapping[str, Any] | None,
    env: Mapping[str, str],
    system: System,
) -> RootChoice:
    """Return the root of a run in *mode*: the first source of its fallback chain that applies (design 6.1, D48).

    System mode: ``--root``, the installed system gupkg's root, ``[roots].system`` of ``$App\\settings.toml``, ``C:\\opt``.
    User mode: ``--root``, ``[roots].user`` of ``--settings``, the root the running gupkg is installed in (when it is not
    admin-only), ``[roots].user`` of ``$App\\settings.toml``, ``%USERPROFILE%\\opt``.

    Parameters
    ----------
    mode : str
        ``system`` or ``user``.
    root_option : Path, optional
        ``--root DIR``.
    installed_root : Path, optional
        The root the running gupkg is installed in, or ``None`` (a source tree or a bundled copy has none). System mode
        uses it only when it is admin-only, user mode only when it is not.
    app_layer, cli_layer : mapping, optional
        The parsed ``$App\\settings.toml`` and ``--settings`` files, or ``None`` when absent.
    env : mapping of str to str
        The environment, used to expand ``%NAME%``.
    system : System
        The boundary that says whether a folder is admin-only.

    Returns
    -------
    RootChoice
        The root and its source.

    Raises
    ------
    SettingsError
        If a root is not an absolute path with a drive, or ``--root`` in system mode is not admin-only.
    ValueError
        If *mode* is unknown.
    """
    if mode not in (Mode.SYSTEM, Mode.USER):
        raise ValueError(f"unknown mode {mode!r}; use 'system' or 'user'")
    if root_option is not None:
        chosen = _absolute(Path(root_option), "--root")
        if mode == Mode.SYSTEM:
            try:
                reason = _untrusted_reason(chosen, system)
            except OSError as exc:
                raise SettingsError(f"--root {chosen} could not be judged admin-only: {exc}") from exc
            if reason is not None:
                raise SettingsError(f"--root {chosen} is not admin-only, so it cannot be the system root: {reason}")
        return RootChoice(chosen, SOURCE_ROOT_OPTION)
    if mode == Mode.SYSTEM:
        if installed_root is not None and _admin_only(Path(installed_root), system):
            return RootChoice(Path(installed_root), SOURCE_INSTALLED_SYSTEM)
    else:
        found = _configured_root(cli_layer, mode, env, SOURCE_SETTINGS_OPTION)
        if found is not None:
            return RootChoice(found, SOURCE_SETTINGS_OPTION)
        if installed_root is not None and not _admin_only(Path(installed_root), system):
            return RootChoice(Path(installed_root), SOURCE_INSTALLED_USER)
    found = _configured_root(app_layer, mode, env, SOURCE_APP)
    if found is not None:
        return RootChoice(found, SOURCE_APP)
    built_in = merge_layers([default_layer(mode)])[f"roots.{mode}"]
    return RootChoice(Path(expand_env(built_in, env)), SOURCE_BUILT_IN)


def _read_layer(
    name: str, path: Path, *, strict: bool, system: System, read_text: Callable[[Path], str | None], required: bool = False,
) -> tuple[Layer | None, LayerReport]:
    """Read one file layer: its ``Layer`` (``None`` when the file is missing) and its report."""
    # A system-mode run checks the folder before it reads anything from it. A folder that does not exist
    # yet is judged by its nearest existing parent.
    if strict:
        folder = path.parent
        try:
            reason = _untrusted_reason(folder, system)
        except OSError as exc:
            raise SettingsError(
                f"Refusing the {name} settings {path} for this run: {folder} could not be judged admin-only: {exc}"
            ) from exc
        if reason is not None:
            raise SettingsError(f"Refusing the {name} settings {path} for this run: {folder} is not admin-only: {reason}")

    # Any failure to read is an error, never a silently skipped layer.
    try:
        text = read_text(path)
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(f"cannot read the {name} settings {path}: {exc}") from exc
    if text is None:
        if required:
            raise SettingsError(f"the settings file {path} does not exist")
        return None, LayerReport(name, path, "missing")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{name} settings {path}: not valid TOML: {exc}") from exc
    return Layer(name, data, str(path)), LayerReport(name, path, "applied")


def load_mode_settings(
    mode: str,
    root: Path,
    *,
    app_dir: Path | None,
    settings_path: Path | None,
    system: System,
    env: Mapping[str, str],
    read_text: Callable[[Path], str | None] = read_text_file,
) -> LoadedSettings:
    """Read the layers of a run in *mode* on *root* and merge them (design 6.2, 11).

    The layers, lowest priority first: the mode's built-in defaults, ``$App\\settings.toml``,
    ``<root>\\.gupkg\\settings.toml``, and ``--settings`` (user mode only).

    Parameters
    ----------
    mode : str
        ``system`` or ``user``.
    root : Path
        The run's root (``resolve_root``).
    app_dir : Path or None
        ``$App`` of the running gupkg; its ``settings.toml`` is the second layer.
    settings_path : Path, optional
        The ``--settings`` file; refused in system mode.
    system : System
        The boundary that says whether a folder is admin-only (``directory_untrusted_reason``); a ``FakeSystem`` in tests.
    env : mapping of str to str
        The environment. The settings are returned unexpanded, so it is accepted only for symmetry with ``resolve_root``.
    read_text : callable, optional
        Returns a file's text, or ``None`` when it does not exist.

    Returns
    -------
    LoadedSettings
        The effective settings and a report of every layer.

    Raises
    ------
    SettingsError
        If a layer is invalid (schema 3 included), ``[roots]`` is in the root's own file, ``--settings`` is missing or
        given in system mode, or a system-mode run would read a file from a folder that is not admin-only.
    """
    if mode not in (Mode.SYSTEM, Mode.USER):
        raise ValueError(f"unknown mode {mode!r}; use 'system' or 'user'")
    if mode == Mode.SYSTEM and settings_path is not None:
        raise SettingsError("--settings is not accepted in system mode: an elevated run reads only admin-only files")
    strict = mode == Mode.SYSTEM
    layers: list[Layer] = [default_layer(mode)]
    reports: list[LayerReport] = [LayerReport(LAYER_DEFAULTS, None, "applied", "built in")]
    wanted: list[tuple[str, Path, bool]] = []
    if app_dir is not None:
        wanted.append((LAYER_APP, Path(app_dir) / SETTINGS_FILE_NAME, False))
    wanted.append((LAYER_ROOT, settings_file(root), False))
    if settings_path is not None:
        wanted.append((LAYER_CLI, Path(settings_path), True))
    for name, path, required in wanted:
        layer, report = _read_layer(name, path, strict=strict, system=system, read_text=read_text, required=required)
        reports.append(report)
        if layer is not None:
            layers.append(layer)
    return LoadedSettings(merge_layers(layers), tuple(reports))


def read_root_layers(
    mode: str,
    *,
    app_dir: Path | None,
    settings_path: Path | None,
    system: System,
    read_text: Callable[[Path], str | None] = read_text_file,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Read the two layers that name a root before the root is known: ``$App\\settings.toml`` and ``--settings``.

    ``resolve_root`` needs their ``[roots]`` tables; ``load_mode_settings`` reads them again, together with the root's own
    file, once the root is known. A system-mode run reads ``$App\\settings.toml`` only from an admin-only folder and
    never reads ``--settings``.

    Returns
    -------
    tuple
        The parsed ``$App\\settings.toml`` and the parsed ``--settings`` file, each ``None`` when absent.

    Raises
    ------
    SettingsError
        If a file cannot be read or is not valid TOML, ``--settings`` is missing, or a system-mode run would read a file
        from a folder that is not admin-only.
    """
    strict = mode == Mode.SYSTEM
    app_data = None
    if app_dir is not None:
        layer, _ = _read_layer(LAYER_APP, Path(app_dir) / SETTINGS_FILE_NAME, strict=strict, system=system, read_text=read_text)
        app_data = layer.data if layer is not None else None
    cli_data = None
    if mode == Mode.USER and settings_path is not None:
        layer, _ = _read_layer(LAYER_CLI, Path(settings_path), strict=False, system=system, read_text=read_text, required=True)
        cli_data = layer.data if layer is not None else None
    return app_data, cli_data
