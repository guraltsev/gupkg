"""Install modes as data: the mode of an operation and the immutable context built from it (design section 6).

An install mode (``system``, ``user`` or ``portable``) fixes where a package lives and which parts of the
machine gupkg touches for it. ``select_mode`` picks the mode of one operation by the order of design 6.2
(an explicit flag or qualified selector, the install record, the containing root, ``default_mode``) and
enforces the rules that no later step may weaken: an elevated gupkg never acts on a user or portable root
(D31), and a package is only installed in a mode its ``modes`` list supports. ``build_mode_context`` turns the
choice and the effective settings into a ``ModeContext``, which every other subsystem reads: root, bin and
Start Menu folders, environment target, log folders and the settings layers of the run.

Usage and API
-------------
Call ``select_mode(...)`` with what the caller found out about the target, then
``build_mode_context(choice, settings, env, ...)``. ``ModeContext`` is frozen; ``context.package_dir(name)``,
``context.package_log_dir(name)``, ``context.data_dir`` and ``context.path_line()`` answer the questions the
engine and the ``shell`` command have. Failures are ``ModeRefused`` (exit code 2) and ``InputNeeded``
(exit code 5), which carry the message to print. Finding the install record, the containing root and the
package's ``modes`` is the job of ``gupkg.modes``; this module performs no input or output.

Implementation Approach
-----------------------
Selection is a pure function of its arguments, so each rule of 6.2 is checked without a machine. Configured
values come from ``Settings``: a bin or Start Menu folder that no settings layer set takes the machine's own
default folder when the caller provides one (``DefaultFolders``), which keeps a sandbox off the real machine and
is equal to expanding the built-in ``%VAR%`` default on a real one.

See Also
--------
gupkg.modes : resolves a target to a mode and a context using the catalog and the system boundary.
gupkg.model.settings : the ``[roots]`` and ``[integration.*]`` tables the context reads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .manifest import MODES
from .settings import (
    LAYER_APP,
    LAYER_CLI,
    LAYER_DEFAULTS,
    LAYER_PORTABLE,
    LAYER_SYSTEM,
    LAYER_USER,
    Settings,
    expand_env,
)

MODE_SYSTEM, MODE_USER, MODE_PORTABLE = MODES

# Exit codes of design 10.1 that mode handling can return.
EXIT_REFUSED = 2
EXIT_NEEDS_INPUT = 5

# Environment targets of a context: the machine or the user environment of the registry, or none.
ENV_MACHINE = "HKLM"
ENV_USER = "HKCU"

AUTO_ELEVATED = "auto: elevated"
_UNSAFE_PART = re.compile(r'[<>:"|?*\\]')
NORMAL_SHELL_HINT = "Run it from a normal (non-elevated) shell."


class ModeRefused(ValueError):
    """Refuse an operation because of its install mode; the message says what to do instead."""

    exit_code = EXIT_REFUSED


class InputNeeded(ValueError):
    """Stop because an answer is needed that cannot be given (declined, or ``--no-input``); exit code 5."""

    exit_code = EXIT_NEEDS_INPUT


@dataclass(frozen=True)
class DefaultFolders:
    """The machine's own default Start Menu ``opt`` folder and ``bin`` folder for one mode."""

    start_menu: Path
    bin_dir: Path


@dataclass(frozen=True)
class ModeChoice:
    """A selected mode and the reason for it, such as ``system (auto: elevated)``."""

    mode: str
    reason: str

    def __str__(self) -> str:
        """Return the text that is printed and logged: ``<mode> (<reason>)``."""
        return f"{self.mode} ({self.reason})"


@dataclass(frozen=True)
class ModeContext:
    """Everything a subsystem needs to know about the install mode of one operation.

    Attributes
    ----------
    mode : str
        ``system``, ``user`` or ``portable``.
    root : Path
        The root that holds the package.
    bin_dir : Path
        Where command shims go.
    start_menu : Path or None
        The Start Menu ``opt`` folder, or ``None`` when no shortcuts are created.
    env_target : str or None
        ``HKLM``, ``HKCU``, or ``None`` when the environment and ``PATH`` are not touched.
    requires_elevation : bool
        Whether the mode needs an administrator token.
    integrate : str
        ``user`` or ``none``; ``none`` is possible only in portable mode.
    layers : tuple of str
        The settings layers the run reads, lowest priority first (design 6.1).
    reason : str
        Why the mode was chosen, as printed in the Resolve phase.
    in_place : bool
        Whether the portable root is the folder above an installed ``<name>\\<version>`` folder (D42).
    """

    mode: str
    root: Path
    bin_dir: Path
    start_menu: Path | None
    env_target: str | None
    requires_elevation: bool
    integrate: str
    layers: tuple[str, ...]
    reason: str = ""
    in_place: bool = False

    @property
    def data_dir(self) -> Path:
        """Return gupkg's data folder of the root, ``<root>\\.gupkg`` (settings, state, registry cache)."""
        return self.root / ".gupkg"

    @property
    def log_dir(self) -> Path:
        """Return the root's log folder, ``<root>\\.gupkg\\logs``, which holds global run information only (D26)."""
        return self.data_dir / "logs"

    def package_dir(self, name: str) -> Path:
        """Return the package root of *name* in this context's root.

        Raises
        ------
        ValueError
            If a part of *name* (folders are joined by ``/``) is empty, ``.`` or ``..``, or holds a path
            separator, a drive colon or another character Windows does not allow in a name.
        """
        parts = name.split("/")
        for part in parts:
            if part in ("", ".", "..") or _UNSAFE_PART.search(part) or part != part.rstrip(" ."):
                raise ValueError(f"'{name}' is not a safe package name: it would leave the root or is not a valid folder name")
        return self.root.joinpath(*parts)

    def package_log_dir(self, name: str) -> Path:
        """Return the log folder of one package, ``<root>\\<name>\\.gupkg\\logs`` (D26)."""
        return self.package_dir(name) / ".gupkg" / "logs"

    def package_data_dir(self, name: str) -> Path:
        """Return ``<package>\\data``, where a portable package keeps its settings and state (design 6.3)."""
        return self.package_dir(name) / "data"

    @property
    def choice(self) -> ModeChoice:
        """Return the mode and its reason."""
        return ModeChoice(self.mode, self.reason)

    def path_line(self) -> str | None:
        r"""Return the ``PATH`` line to use for a root that does not touch the environment, else ``None``.

        With ``integrate = "none"`` shims go to ``<root>\bin`` and nothing adds that folder to ``PATH``;
        ``gupkg shell`` prints this line instead.
        """
        if self.env_target is not None:
            return None
        return f'set "PATH={self.bin_dir};%PATH%"'

    def as_scope_paths(self, shim_linkage: str = "dynamic", *, root: bool = True) -> dict[str, Path | str]:
        """Return the destinations in the form the 0.17 install code takes (until issue 029 replaces it).

        Parameters
        ----------
        shim_linkage : str, default="dynamic"
            Native launcher linkage for generated wrappers.
        root : bool, default=True
            Whether to include the root as ``collection_root`` (the ``$ScopeRoot`` variable).
        """
        paths: dict[str, Path | str] = {"bin_dir": self.bin_dir, "shim_linkage": shim_linkage}
        if self.start_menu is not None:
            paths["shortcut_root"] = self.start_menu
        if root:
            paths["collection_root"] = self.root
        return paths


def _supported_text(supported: Sequence[str]) -> str:
    """Return the supported modes as ``a, b``."""
    return ", ".join(supported)


def select_mode(
    *,
    explicit: str | None = None,
    explicit_source: str = "--mode",
    record: str | None = None,
    record_trusted: bool = True,
    container: str | None = None,
    default_mode: str = "auto",
    elevated: bool = False,
    supported: Sequence[str] | None = None,
) -> ModeChoice:
    """Select the install mode of one operation (design 6.2) and apply the rules that follow it.

    Parameters
    ----------
    explicit : str, optional
        The mode of ``--mode`` or of a qualified selector such as ``user:firefox``.
    explicit_source : str, default="--mode"
        How the explicit mode was given, for the reason text.
    record : str, optional
        The mode in the install record of an existing package.
    record_trusted : bool, default=True
        ``False`` when the record was read from a folder users can modify in an elevated run.
    container : str, optional
        The mode of the configured root that contains a path target.
    default_mode : str, default="auto"
        ``default_mode`` from the settings: ``auto``, ``system`` or ``user``.
    elevated : bool, default=False
        Whether the process is elevated (unknown counts as elevated).
    supported : sequence of str, optional
        The ``modes`` of the package; ``None`` when no package is known yet.

    Returns
    -------
    ModeChoice
        The mode and the reason, for example ``system (auto: elevated)``.

    Raises
    ------
    ModeRefused
        If an explicit mode contradicts the install record or the containing root; if an elevated
        run is asked to act on a user or portable root (D31); if a record from an untrusted place
        would decide an elevated run; or if the package does not support the chosen mode.
    """
    for label, value in (("--mode", explicit), ("install record", record), ("containing root", container),
                         ("default_mode", None if default_mode == "auto" else default_mode)):
        if value is not None and value not in MODES:
            raise ModeRefused(f"Unknown install mode '{value}' ({label}); use one of {_supported_text(MODES)}.")

    # An elevated gupkg never acts on a user or portable root (D31, design 9.6), whichever step names it:
    # the flag, the install record or the root that contains the path. This comes first so that nothing
    # below can turn such a request into anything else.
    if elevated:
        for value in (explicit, record, container):
            if value is not None and value != MODE_SYSTEM:
                raise ModeRefused(
                    f"Refusing {value} mode in an elevated shell: an elevated gupkg never acts on a {value} root "
                    "(a standard user could use it to gain administrator rights). " + NORMAL_SHELL_HINT
                )

    # Steps 1-3 of 6.2 name the mode of an existing package or path. They must agree: an explicit mode
    # never overrides the package's own, because a package belongs to one scope (design 6.2).
    named = [(label, value) for label, value in (
        ("--mode or the selector", explicit), ("the install record", record), ("the root that contains it", container),
    ) if value is not None]
    for (first_label, first), (second_label, second) in zip(named, named[1:]):
        if first != second:
            raise ModeRefused(
                f"{first_label} says {first}, but {second_label} says {second}. A package belongs to one mode: "
                "name it with its own mode, or remove it and install it again."
            )

    # Pick the first step of 6.2 that decides; ``auto`` applies only when none of them does.
    if explicit is not None:
        chosen, reason = explicit, explicit_source
    elif record is not None:
        if not record_trusted:
            raise ModeRefused(
                "Refusing the install record of this package: it lies in a folder that standard users can "
                "modify, and an elevated gupkg does not trust it. " + NORMAL_SHELL_HINT
            )
        chosen, reason = record, "install record"
    elif container is not None:
        chosen, reason = container, f"inside the {container} root"
    elif default_mode != "auto":
        chosen, reason = default_mode, "default_mode"
    elif elevated:
        chosen, reason = MODE_SYSTEM, AUTO_ELEVATED
    else:
        chosen, reason = MODE_USER, "auto: not elevated"

    # An elevated run can only have reached a non-system mode through default_mode.
    if elevated and chosen != MODE_SYSTEM:
        raise ModeRefused(
            f"Refusing {chosen} mode in an elevated shell: an elevated gupkg never acts on a {chosen} root. "
            + NORMAL_SHELL_HINT
        )

    choice = ModeChoice(chosen, reason)
    if supported is not None:
        check_supported(choice, supported)
    return choice


def check_supported(choice: ModeChoice, supported: Sequence[str]) -> None:
    """Refuse a mode that the package's ``modes`` list does not allow, before any work (design 6.2).

    Parameters
    ----------
    choice : ModeChoice
        The selected mode.
    supported : sequence of str
        The ``modes`` of the package.

    Raises
    ------
    ModeRefused
        If *supported* lacks the mode; an elevated ``auto`` choice says to use a normal shell.
    """
    if choice.mode in supported:
        return
    if choice.reason == AUTO_ELEVATED:
        raise ModeRefused(
            f"This package does not support system mode (it supports {_supported_text(supported)}), and "
            "an elevated gupkg only acts on the system root. " + NORMAL_SHELL_HINT
        )
    raise ModeRefused(
        f"This package does not support {choice.mode} mode (it supports {_supported_text(supported)}); "
        "its pkg.toml lists the modes it allows."
    )


def _folder(settings: Settings, key: str, env: Mapping[str, str], known: Path | None) -> Path:
    """Return a configured folder: the machine's own default when no layer set *key*, else the expanded value."""
    if known is not None and settings.source_of(key) == LAYER_DEFAULTS:
        return Path(known)
    return Path(expand_env(settings[key], env))


def layers_of(mode: str) -> tuple[str, ...]:
    """Return the settings layers a run in *mode* reads, lowest priority first (design 6.1)."""
    middle = {MODE_SYSTEM: (), MODE_USER: (LAYER_USER,), MODE_PORTABLE: (LAYER_PORTABLE,)}[mode]
    return (LAYER_DEFAULTS, LAYER_APP, LAYER_SYSTEM, *middle, LAYER_CLI)


def build_mode_context(
    choice: ModeChoice,
    settings: Settings,
    env: Mapping[str, str],
    *,
    defaults: Mapping[str, DefaultFolders] | None = None,
    root: Path | None = None,
    in_place: bool = False,
) -> ModeContext:
    """Build the context of a chosen mode from the effective settings (design 6.1).

    Parameters
    ----------
    choice : ModeChoice
        The selected mode and reason.
    settings : Settings
        The merged settings of the run (``[roots]`` and ``[integration.*]``).
    env : mapping of str to str
        The environment used to expand ``%NAME%`` in configured paths.
    defaults : mapping, optional
        The machine's own default folders for ``system`` and ``user``; used where no settings
        layer set the bin or Start Menu folder.
    root : Path, optional
        The root to use instead of the configured one: the portable root of ``--mode portable`` on a
        folder or of a root the user named, or the manager's root for the 0.17 front end.
    in_place : bool, default=False
        Whether the portable root comes from the shape of an installed folder (D42).

    Returns
    -------
    ModeContext
        The immutable context.

    Raises
    ------
    gupkg.model.settings.SettingsError
        If a configured path refers to an undefined variable.
    """
    mode = choice.mode
    known = defaults or {}
    if mode == MODE_SYSTEM:
        sys_defaults = known.get(MODE_SYSTEM)
        return ModeContext(
            mode, Path(root) if root is not None else Path(expand_env(settings["roots.system"], env)),
            _folder(settings, "integration.system.bin", env, sys_defaults.bin_dir if sys_defaults else None),
            _folder(settings, "integration.system.start_menu", env, sys_defaults.start_menu if sys_defaults else None),
            ENV_MACHINE, True, "user", layers_of(mode), choice.reason,
        )
    user_defaults = known.get(MODE_USER)

    def user_start_menu() -> Path:
        """Return the user's Start Menu ``opt`` folder."""
        return _folder(
            settings, "integration.user.start_menu", env, user_defaults.start_menu if user_defaults else None
        )

    if mode == MODE_USER:
        return ModeContext(
            mode, Path(root) if root is not None else Path(expand_env(settings["roots.user"], env)),
            _folder(settings, "integration.user.bin", env, user_defaults.bin_dir if user_defaults else None),
            user_start_menu(), ENV_USER, False, "user", layers_of(mode), choice.reason,
        )

    # Portable: shims live in the root; the user's profile is touched unless integrate = "none".
    portable = Path(root) if root is not None else Path(expand_env(settings["roots.portable"], env))
    integrate = settings["integration.portable.integrate"]
    integrated = integrate == "user"
    return ModeContext(
        mode, portable, portable / "bin", user_start_menu() if integrated else None, ENV_USER if integrated else None,
        False, integrate, layers_of(mode), choice.reason, in_place,
    )
