"""Read the settings the UI programs act on: the ``[manager]`` policy and display options, ``[cleanup] auto`` and the root.

The CLI ignores the ``[manager]`` table (design section 11), so the UI programs read the effective settings themselves, through the
same layered loader the CLI uses (``--settings`` included). Reading is all they do with settings; changing one is
``gupkg settings set``.

Usage and API
-------------
``load_ui_settings(...)`` returns a ``UiSettings``: ``checksum_policy`` is the effective ``ask``, ``refuse`` or ``skip`` of the run's
scope, ``keep_screen`` and ``cleanup_auto`` are the booleans, ``scope`` is the run's ``Scope`` and ``root`` is its root.

Implementation Approach
-----------------------
The settings are those of the run's own scope (``gupkg.scope.build_scope``), read exactly as the CLI of the same gupkg reads them:
the policy is read from the layers the run itself reads, so a setting a standard user can edit never reaches an administrator run (an
elevated scope reads only admin-only files). ``WindowsSystem`` is imported only to *read* (whether the process is elevated and whether a
folder is admin-only): the ``System`` protocol has no implementation apart from it and the sandbox's ``FakeSystem``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gupkg.model.manager_policy import manager_checksum_policy
from gupkg.model.scope import Scope
from gupkg.model.settings import Settings, SettingsError
from gupkg.scope import build_scope
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
    scope : Scope
        The scope of the run; its root is ``root``.
    """

    settings: Settings
    env: Mapping[str, str]
    scope: Scope

    @property
    def checksum_policy(self) -> str:
        """Return ``ask``, ``refuse`` or ``skip``: ``[manager] checksum_missing`` as the layers of the run itself set it."""
        return manager_checksum_policy(self.settings)

    @property
    def colour(self) -> bool:
        """Whether the programs may use colour: not with ``[ui] color = "never"`` and not when ``NO_COLOR`` is set in the program's environment."""
        return self.settings["ui.color"] != "never" and not self.env.get("NO_COLOR")

    @property
    def keep_screen(self) -> bool:
        """Whether the last run's summary and log path are printed to the console after the program closes."""
        return bool(self.settings["manager.keep_screen"])

    @property
    def cleanup_auto(self) -> bool:
        """Whether ``update --all`` ends with a cleanup (decision D43)."""
        return bool(self.settings["cleanup.auto"])

    @property
    def root(self) -> Path:
        """Return the root of the run's scope."""
        return Path(self.scope.root)


def load_ui_settings(
    *,
    settings_path: Path | None = None,
    root_option: Path | None = None,
    env: Mapping[str, str] | None = None,
    location: RunningGupkg | None = None,
    system: System | None = None,
) -> UiSettings:
    """Load the effective settings the way the CLI of the same gupkg does.

    Parameters
    ----------
    settings_path : Path, optional
        ``--settings PATH``, the highest layer.
    root_option : Path, optional
        ``--root DIR``, the first source of the root chain.
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
    gupkg.model.scope.ModeRefused
        If the process is elevated and this gupkg's tree is not admin-only.
    """
    where = location or locate_running_gupkg()
    machine = system or WindowsSystem()
    environment = dict(os.environ if env is None else env)

    scope = build_scope(system=machine, env=environment, location=where, root_option=root_option, settings_path=settings_path)
    return UiSettings(scope.settings, environment, scope)


__all__ = ["SettingsError", "UiSettings", "load_ui_settings"]
