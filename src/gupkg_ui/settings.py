"""Read the settings the UI programs act on: the ``[manager]`` policy and display options, ``[cleanup] auto`` and the roots.

The CLI ignores the ``[manager]`` table (design section 11), so the UI programs read the effective settings themselves, through the
same layered loader the CLI uses (``--settings`` included). Reading is all they do with settings; changing one is
``gupkg settings set``.

Usage and API
-------------
``load_ui_settings(...)`` returns a ``UiSettings``: ``checksum_policy(mode)`` is the effective ``ask``, ``refuse`` or ``skip`` for a
target of that mode, ``keep_screen`` and ``cleanup_auto`` are the booleans, and ``root_of(mode)`` is the configured root.

Implementation Approach
-----------------------
The loader runs as a normal (non-elevated) process. Because a system target is run by an elevated child, the policy for a system
target is evaluated as an elevated run would see it: a value that comes from the user or portable layer does not count, so a
setting a standard user can edit never lets an administrator-run install skip a missing checksum (design 9.6).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gupkg.model.manager_policy import manager_checksum_policy
from gupkg.model.settings import Settings, SettingsError, expand_env
from gupkg.modes import settings_loader
from gupkg.selfmgmt.location import RunningGupkg, locate_running_gupkg
from gupkg.system.port import System
from gupkg.system.windows import WindowsSystem


@dataclass(frozen=True)
class UiSettings:
    """The effective settings of one run of a UI program.

    Attributes
    ----------
    settings : Settings
        The merged settings with the layer of every value.
    env : mapping
        The environment used to expand ``%NAME%`` in the roots.
    """

    settings: Settings
    env: Mapping[str, str]

    def checksum_policy(self, mode: str | None) -> str:
        """Return ``ask``, ``refuse`` or ``skip`` for a target of *mode*; a system target is judged as an elevated run would judge it."""
        return manager_checksum_policy(self.settings, elevated=mode == "system")

    @property
    def keep_screen(self) -> bool:
        """Whether the last run's summary and log path are printed to the console after the program closes."""
        return bool(self.settings["manager.keep_screen"])

    @property
    def cleanup_auto(self) -> bool:
        """Whether ``update --all`` ends with a cleanup (decision D43)."""
        return bool(self.settings["cleanup.auto"])

    def root_of(self, mode: str) -> Path:
        """Return the configured root of *mode* (``system``, ``user`` or ``portable``)."""
        return Path(expand_env(self.settings[f"roots.{mode}"], self.env))

    @property
    def default_mode(self) -> str:
        """The mode a new install uses when nothing says otherwise (``auto`` means the user root for a normal process)."""
        value = self.settings["default_mode"]
        return "user" if value == "auto" else str(value)


def load_ui_settings(
    *,
    settings_path: Path | None = None,
    env: Mapping[str, str] | None = None,
    location: RunningGupkg | None = None,
    system: System | None = None,
) -> UiSettings:
    """Load the effective settings the way the CLI of the same gupkg does.

    Parameters
    ----------
    settings_path : Path, optional
        ``--settings PATH``, the highest layer.
    env : mapping, optional
        The environment; ``os.environ`` by default.
    location : RunningGupkg, optional
        Where this gupkg lives (for ``$App\\settings.toml`` and the installed system root); found by default.
    system : System, optional
        The machine boundary that judges folders; the real one by default.

    Raises
    ------
    SettingsError
        If a settings layer is unreadable or invalid.
    """
    where = location or locate_running_gupkg()
    machine = system or WindowsSystem()
    environment = dict(os.environ if env is None else env)

    # The system root counts as the installed one only when this gupkg really sits in an admin-only root.
    installed_root = where.root if where.kind == "installed" and where.root is not None and machine.directory_is_admin_only(where.root) else None
    loaded = settings_loader(
        system=machine, env=environment, elevated=False, app_dir=where.app_dir, installed_system_root=installed_root,
        settings_path=settings_path,
    )()
    return UiSettings(loaded.settings, environment)


__all__ = ["SettingsError", "UiSettings", "load_ui_settings"]
