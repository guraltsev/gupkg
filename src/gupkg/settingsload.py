"""Find, filter and read the settings layers of one run (design sections 9.4, 9.7 and 11).

Usage and API
-------------
Call ``load_settings(env=..., elevated=..., system_mode=..., app_dir=...)`` to get
a ``LoadedSettings``: the merged ``Settings`` (each value knows its layer) and a
``LayerReport`` per layer that says whether it was applied, missing or ignored,
and why. ``settings_file(root)`` names the settings file of a root.

Implementation Approach
-----------------------
The layers are read lowest priority first: built-in defaults, ``$App\\settings.toml``
of the running gupkg, the system root's ``.gupkg\\settings.toml``, the user root's
``.gupkg\\settings.toml`` and ``--settings``. The user root is only known once the
system layer has been merged, because the system layer may change ``[roots]``.
Nothing is ever read from AppData or ProgramData. The user layer is skipped when the
run is elevated or in system mode, ``--settings`` in an elevated run must lie inside
the system root, and in those runs every file must sit in an admin-only folder
(``elevation.require_trusted_directory``): a layer that fails that check stops the
run, because an elevated run must not trust a folder a standard user can change.
Reading goes through an injectable function so that tests never touch a real machine.
"""

from __future__ import annotations

import ntpath
import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .elevation import UntrustedLocationError, require_trusted_directory
from .model.settings import (
    LAYER_APP,
    LAYER_CLI,
    LAYER_DEFAULTS,
    LAYER_SYSTEM,
    LAYER_USER,
    Layer,
    Settings,
    SettingsError,
    default_layer,
    expand_env,
    merge_layers,
)

DATA_DIR_NAME = ".gupkg"
SETTINGS_FILE_NAME = "settings.toml"


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


def settings_file(root: Path) -> Path:
    """Return the settings file of *root*: ``<root>\\.gupkg\\settings.toml``."""
    return Path(root) / DATA_DIR_NAME / SETTINGS_FILE_NAME


def read_text_file(path: Path) -> str | None:
    """Read a UTF-8 text file, returning ``None`` when it does not exist."""
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None


def _is_inside(path: Path, root: Path) -> bool:
    """Return whether *path* lies inside *root*, comparing real paths without letter case."""
    real_path = os.path.normcase(os.path.realpath(path))
    real_root = os.path.normcase(os.path.realpath(root))
    return real_path == real_root or real_path.startswith(real_root.rstrip("\\/") + os.sep)


def _check_roots(settings: Settings, env: Mapping[str, str]) -> None:
    """Require absolute roots that are distinct and not nested in one another."""
    roots = {}
    for name in ("system", "user", "portable"):
        text = expand_env(settings[f"roots.{name}"], env)
        if not ntpath.isabs(text) or not ntpath.splitdrive(text)[0]:
            raise SettingsError(f"roots.{name} must be an absolute path with a drive, not '{text}'")
        roots[name] = ntpath.normcase(ntpath.normpath(text)).rstrip("\\")
    names = list(roots)
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            a, b = roots[first], roots[second]
            if a == b or a.startswith(b + "\\") or b.startswith(a + "\\"):
                raise SettingsError(f"the {first} and {second} roots must be distinct and not nested")


def load_settings(
    *,
    env: Mapping[str, str],
    elevated: bool,
    system_mode: bool,
    app_dir: Path | None,
    installed_system_root: Path | None = None,
    settings_path: Path | None = None,
    read_text: Callable[[Path], str | None] = read_text_file,
    trust_check: Callable[[Path], None] = require_trusted_directory,
) -> LoadedSettings:
    """Read the layers a run may use and merge them.

    Parameters
    ----------
    env : mapping of str to str
        The environment, used to expand ``%NAME%`` in configured roots.
    elevated : bool
        Whether the process is elevated.
    system_mode : bool
        Whether the run acts on the system root.
    app_dir : Path or None
        ``$App`` of the running gupkg; its ``settings.toml`` is the second layer.
    installed_system_root : Path, optional
        The root in which the system gupkg is installed; the configured system root
        when omitted.
    settings_path : Path, optional
        The ``--settings`` file.
    read_text : callable, optional
        Returns a file's text, or ``None`` when it does not exist.
    trust_check : callable, optional
        Raises ``UntrustedLocationError`` unless a folder is admin-only.

    Returns
    -------
    LoadedSettings
        The effective settings and a report of every layer.

    Raises
    ------
    SettingsError
        If a layer is invalid, ``--settings`` is missing, or an elevated or
        system-mode run would read a file from a folder that is not admin-only.
    """
    strict = elevated or system_mode
    layers: list[Layer] = [default_layer()]
    reports: list[LayerReport] = [LayerReport(LAYER_DEFAULTS, None, "applied", "built in")]

    def read_layer(name: str, path: Path, *, required: bool = False) -> None:
        """Read one file layer into *layers*, or record why it was not applied."""
        text = read_text(path)
        if text is None:
            if required:
                raise SettingsError(f"the settings file {path} does not exist")
            reports.append(LayerReport(name, path, "missing"))
            return
        if strict:
            try:
                trust_check(path.parent)
            except UntrustedLocationError as exc:
                raise SettingsError(f"Refusing the {name} settings {path} for this run: {exc}") from exc
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise SettingsError(f"{name} settings {path}: not valid TOML: {exc}") from exc
        layers.append(Layer(name, data, str(path)))
        reports.append(LayerReport(name, path, "applied"))

    if app_dir is not None:
        read_layer(LAYER_APP, Path(app_dir) / SETTINGS_FILE_NAME)

    # The system layer lives in the root of the installed system gupkg, or in the
    # configured system root. Merging first keeps a [roots] change of the app layer.
    system_root = installed_system_root
    if system_root is None:
        system_root = Path(expand_env(merge_layers(layers)["roots.system"], env))
    read_layer(LAYER_SYSTEM, settings_file(system_root))

    # The system layer may rename the roots, so the user layer's place is known only now.
    merged = merge_layers(layers)
    if strict:
        reports.append(LayerReport(
            LAYER_USER, None, "ignored",
            "the user layer is ignored in elevated and system-mode runs",
        ))
    else:
        read_layer(LAYER_USER, settings_file(Path(expand_env(merged["roots.user"], env))))

    if settings_path is not None:
        explicit = Path(settings_path)
        effective_system_root = Path(expand_env(merged["roots.system"], env))
        if elevated and not _is_inside(explicit, effective_system_root):
            reports.append(LayerReport(
                LAYER_CLI, explicit, "ignored",
                "--settings is ignored in an elevated run unless the file is inside the system root",
            ))
        else:
            read_layer(LAYER_CLI, explicit, required=True)

    settings = merge_layers(layers)
    _check_roots(settings, env)
    return LoadedSettings(settings, tuple(reports))
