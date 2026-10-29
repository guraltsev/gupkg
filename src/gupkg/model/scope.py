"""The scope of one gupkg run: its mode, its root and the machine folders it touches (design sections 6.1, 6.2 and 12).

A run has exactly one ``Scope``. The mode is the mode of the process token (an elevated run is ``system``, any other
run is ``user``), the root is found by ``gupkg.settingsload.resolve_root`` and the settings are read for that mode and
root only. Everything that works on a package (the catalog, the planner, the executor, the health check) receives the
``Scope`` and never selects a mode itself.

Usage and API
-------------
``build_scope`` (in ``gupkg.scope``) makes the ``Scope`` at process start. ``scope.package_dir(name)``,
``scope.package_log_dir(name)``, ``scope.package_data_dir(name)``, ``scope.data_dir``, ``scope.log_dir`` and
``scope.path_line()`` answer the questions the engine and the ``shell`` command have. ``check_supported(scope, supported)``
refuses a package whose ``modes`` lack the scope's mode. Failures are ``ModeRefused`` (exit code 2) and ``InputNeeded``
(exit code 5), which carry the message to print. This module performs no input or output.

Implementation Approach
-----------------------
``Scope`` is a frozen dataclass: nothing can change the mode or the root half way through a run. The folders come from
the settings (``[integration]``); a bin or Start Menu folder that no settings layer set takes the machine's own default
folder when the caller provides one (``DefaultFolders``), which keeps a sandbox off the real machine and is equal to
expanding the built-in ``%VAR%`` default on a real one. ``integrate = "none"`` is a property of the scope and holds in
both modes: shims go to ``<root>\\bin`` and neither the Start Menu nor the environment is touched.

See Also
--------
gupkg.scope : builds the scope of the running process.
gupkg.model.settings : the ``[roots]`` and ``[integration]`` tables the scope reads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..selfmgmt.elevation import UntrustedLocationError, untrusted_message
from ..system.port import Mode, System
from .manifest import PORTABLE
from .settings import LAYER_DEFAULTS, Settings, expand_env

# Exit codes of design 10.1 that mode handling can return.
EXIT_REFUSED = 2
EXIT_NEEDS_INPUT = 5

# Environment targets of a scope: the machine or the user environment of the registry, or none.
ENV_MACHINE = "HKLM"
ENV_USER = "HKCU"

_UNSAFE_PART = re.compile(r'[<>:"|?*\\]')

# The refusals of a package whose ``modes`` lack the scope's mode (issue 035, D55, owner answer Q12). ``<command> <name>`` is
# filled in by ``check_supported`` when the caller knows them.
SYSTEM_ONLY_REFUSAL = (
    'This package installs only system-wide. Run gupkg as administrator: use the Start Menu entry "gupkg manager '
    '(administrator)", or run in an administrator shell: gupkg <command> <name>'
)
USER_ONLY_REFUSAL = "This package installs only for a single user. Run gupkg without administrator rights."
PORTABLE_REFUSAL = "This is a portable package: it installs only for a single user. Run gupkg without administrator rights."


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
class Scope:
    """Everything a subsystem needs to know about the one mode and root of a run.

    Attributes
    ----------
    mode : Mode
        ``Mode.SYSTEM`` or ``Mode.USER``: the mode of the process token.
    root : Path
        The root that holds the packages.
    root_source : str
        What named the root (``--root``, ``$App\\settings.toml``, ``built-in default`` ...), as printed in the Resolve phase.
    bin_dir : Path
        Where command shims go.
    start_menu : Path or None
        The Start Menu ``opt`` folder, or ``None`` when no shortcuts are created.
    env_target : str or None
        ``HKLM``, ``HKCU``, or ``None`` when the environment and ``PATH`` are not touched.
    integrate : str
        ``full`` or ``none`` (``[integration] integrate``); a property of the scope in both modes.
    settings : Settings
        The effective settings of this run, each value with the layer that set it.
    system : System, optional
        The machine boundary that ``require_trusted`` asks; not part of the scope's identity. A system-mode scope without
        one refuses every location, because it cannot judge it.
    """

    mode: Mode
    root: Path
    root_source: str
    bin_dir: Path
    start_menu: Path | None
    env_target: str | None
    integrate: str
    settings: Settings
    system: System | None = field(default=None, compare=False, repr=False)

    def require_trusted(self, path: Path, what: str) -> None:
        """Refuse, in system mode, a *path* that standard users can change: the one trust check of an elevated run (T1, D19).

        In user mode this does nothing. In system mode an elevated process must not run code, read a cache or keep a lock
        in a place a standard user could have changed, so the location is judged before it is used.

        Parameters
        ----------
        path : Path
            The folder (or file) about to be trusted.
        what : str
            What it is, for the message (``"the work folder"``, ``"the hook folder"``).

        Raises
        ------
        gupkg.selfmgmt.elevation.UntrustedLocationError
            If the mode is ``system`` and *path* or one of its parents can be changed by anyone but administrators, or its
            permissions cannot be read.
        """
        if self.mode is not Mode.SYSTEM:
            return
        if self.system is None:
            raise UntrustedLocationError(f"{what}: {path} cannot be judged, because this scope has no machine boundary")
        reason = self.system.directory_untrusted_reason(Path(path))
        if reason is not None:
            raise UntrustedLocationError(f"{what}: {untrusted_message(Path(path), reason)}")

    @property
    def data_dir(self) -> Path:
        """Return gupkg's data folder of the root, ``<root>\\.gupkg`` (settings, state, registry cache)."""
        return self.root / ".gupkg"

    @property
    def log_dir(self) -> Path:
        """Return the root's log folder, ``<root>\\.gupkg\\logs``, which holds global run information only (D26)."""
        return self.data_dir / "logs"

    def package_dir(self, name: str) -> Path:
        """Return the package root of *name* in this scope's root.

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

    def path_line(self) -> str | None:
        r"""Return the ``PATH`` line to use for a root that does not touch the environment, else ``None``.

        With ``integrate = "none"`` shims go to ``<root>\bin`` and nothing adds that folder to ``PATH``;
        ``gupkg shell`` prints this line instead.
        """
        if self.env_target is not None:
            return None
        return f'set "PATH={self.bin_dir};%PATH%"'

    def describe(self) -> str:
        """Return the text of the Resolve phase and the run log: ``<mode> root <path> (<source>)``."""
        return f"{self.mode.value} root {self.root} ({self.root_source})"


def check_supported(
    scope: Scope, supported: Sequence[str], *, command: str | None = None, name: str | None = None
) -> None:
    """Refuse a package whose ``modes`` list does not allow the scope's mode, before any work (design 6.2, D55).

    Parameters
    ----------
    scope : Scope
        The scope of the run.
    supported : sequence of str
        The ``modes`` of the package: ``system`` and/or ``user``, or ``["portable"]`` (a portable package installs
        for a single user only).
    command, name : str, optional
        The command word and the package name, put into the hint of the system-only refusal in place of
        ``<command> <name>``.

    Raises
    ------
    ModeRefused
        If the package cannot be installed in the mode; the message says how to run gupkg instead.
    """
    mode = scope.mode
    if PORTABLE in supported:
        if mode is not Mode.USER:
            raise ModeRefused(PORTABLE_REFUSAL)
        return
    if mode.value in supported:
        return
    if mode is Mode.USER:
        text = SYSTEM_ONLY_REFUSAL
        if command is not None and name is not None:
            text = text.replace("<command> <name>", f"{command} {name}")
        raise ModeRefused(text)
    raise ModeRefused(USER_ONLY_REFUSAL)


def _folder(settings: Settings, key: str, env: Mapping[str, str], known: Path | None) -> Path:
    """Return a configured folder: the machine's own default when no layer set *key*, else the expanded value."""
    if known is not None and settings.source_of(key) == LAYER_DEFAULTS:
        return Path(known)
    return Path(expand_env(settings[key], env))


def make_scope(
    mode: Mode,
    root: Path,
    root_source: str,
    settings: Settings,
    env: Mapping[str, str],
    *,
    defaults: DefaultFolders | None = None,
    system: System | None = None,
) -> Scope:
    """Build the ``Scope`` of *mode* on *root* from the effective settings (design 6.1).

    Parameters
    ----------
    mode : Mode
        The mode of the run.
    root : Path
        The run's root.
    root_source : str
        What named the root.
    settings : Settings
        The merged settings of the run (``[integration]``).
    env : mapping of str to str
        The environment used to expand ``%NAME%`` in configured paths.
    defaults : DefaultFolders, optional
        The machine's own default folders for *mode*; used where no settings layer set the bin or Start Menu folder.
    system : System, optional
        The machine boundary the scope's ``require_trusted`` asks.

    Returns
    -------
    Scope
        The immutable scope.

    Raises
    ------
    gupkg.model.settings.SettingsError
        If a configured path refers to an undefined variable.
    """
    root = Path(root)
    integrate = settings["integration.integrate"]
    if integrate == "none":
        # Shims live in ``<root>\bin``; neither the Start Menu nor the environment is touched.
        return Scope(mode, root, root_source, root / "bin", None, None, integrate, settings, system)
    return Scope(
        mode, root, root_source,
        _folder(settings, "integration.bin", env, defaults.bin_dir if defaults else None),
        _folder(settings, "integration.start_menu", env, defaults.start_menu if defaults else None),
        ENV_MACHINE if mode is Mode.SYSTEM else ENV_USER, integrate, settings, system,
    )
