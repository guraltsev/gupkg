"""Define the single boundary through which gupkg touches the machine.

The :class:`System` protocol lists every side effect that depends on the
operating system or the network: directory links, environment and ``PATH``
values, shortcuts, elevation, child processes, and downloads. Engine and
service code receive a ``System`` explicitly, so the real implementation
(``WindowsSystem``) and the in-memory one (``FakeSystem``) are interchangeable.

Usage and API
-------------
Annotate parameters with ``System`` and call its methods. Use ``Scope`` to name
the user or machine target of an environment or shortcut operation. Use
``is_elevated_run(system)``, ``require_admin_only(system, directory)`` and
``run_checked(system, argv, ...)`` for the three patterns that every caller
repeats.

Implementation Approach
-----------------------
The protocol carries only data types defined here (plain dataclasses), so a
caller never needs to know which implementation it holds. The registry is a
write target: ``get_env`` and ``get_path`` exist so a change can be a
read-modify-write of one value, never to learn about the machine (D18).

See Also
--------
gupkg.system.windows : the real implementation.
gupkg.system.fake : the test and sandbox implementation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from ..selfmgmt.elevation import UntrustedLocationError, untrusted_message


class Scope(Enum):
    """Installation scope supported by ``gupkg``.

    ``AUTO`` selects machine scope for administrators when the package permits
    it and user scope otherwise. ``USER`` targets per-user configuration
    locations. ``MACHINE`` targets machine-wide configuration locations and
    therefore usually requires Administrator privileges.
    """

    AUTO = "auto"
    USER = "user"
    MACHINE = "system"


@dataclass(frozen=True)
class EnvValue:
    """Hold one stored environment value and whether it expands ``%VAR%`` references."""

    value: str
    expandable: bool


@dataclass(frozen=True)
class ShortcutSpec:
    """Hold the fields of one Windows shortcut.

    Notes
    -----
    ``target_path`` is stored unresolved and ``%VAR%`` references stay
    unexpanded. ``icon_location`` is ``"path,index"``, or empty for no icon.
    Windows limits what a link can hold. Measured limits: a target longer than
    259 characters is refused when writing, and the working directory, the
    description and the icon path are cut at 260 characters when read back.
    """

    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""


@dataclass(frozen=True)
class ScopeFolders:
    """Name where one scope's Start Menu shortcuts and command wrappers go."""

    shortcut_root: Path
    bin_dir: Path


@dataclass(frozen=True)
class ProcessResult:
    """Report a finished child process: exit status and everything it printed."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class ProcessFailed(RuntimeError):
    """Report that a child process that had to succeed exited with a failure status."""


class System(Protocol):
    """List every machine side effect gupkg performs.

    Implementations must be safe to call from worker threads. Methods raise
    ``OSError`` (or a subclass such as ``PermissionError``) for failures of the
    machine, and never print.
    """

    # -- links -------------------------------------------------------------

    def create_junction(self, link: Path, target: Path) -> None:
        """Create *link* as a junction to the existing directory *target*, replacing a junction.

        Raises ``RuntimeError`` when *target* is missing, or when *link* exists
        and is not a junction; nothing is changed in either case.
        """

    def read_link(self, path: Path) -> Path | None:
        """Return the normalized target of the link at *path*, or ``None`` if it is not a readable link."""

    def is_link(self, path: Path) -> bool:
        """Return whether *path* is a directory link (a junction) rather than a real directory."""

    def remove_link(self, path: Path) -> None:
        """Remove the link at *path* without touching its target; refuse anything that is not a link."""

    def hardlink(self, link: Path, existing: Path) -> None:
        """Create *link* as a second name for the existing file *existing*."""

    # -- environment -------------------------------------------------------

    def get_env(self, scope: Scope, name: str) -> EnvValue | None:
        """Return one stored environment value, or ``None`` when it is not set (names are case-insensitive)."""

    def set_env(self, scope: Scope, name: str, value: str, expandable: bool) -> None:
        """Store one environment value, replacing any value with the same (case-insensitive) name."""

    def delete_env(self, scope: Scope, name: str) -> None:
        """Remove one environment value; removing an absent value is not an error."""

    def get_path(self, scope: Scope) -> list[str]:
        """Return the non-empty ``PATH`` entries of *scope* in stored order; a missing value is empty."""

    def set_path(self, scope: Scope, entries: Sequence[str]) -> None:
        """Store *entries* as the expandable ``PATH`` of *scope*."""

    def broadcast_environment_change(self) -> None:
        """Tell running programs that environment values changed; raises ``OSError`` when refused."""

    # -- shortcuts ---------------------------------------------------------

    def write_shortcut(self, path: Path, spec: ShortcutSpec) -> None:
        """Create or replace the shortcut at *path*, creating missing parent folders."""

    def read_shortcut(self, path: Path) -> ShortcutSpec:
        """Return the fields stored in the shortcut at *path*."""

    def delete_shortcut(self, path: Path) -> None:
        """Delete the shortcut at *path*; deleting an absent shortcut is not an error."""

    # -- privilege ---------------------------------------------------------

    def is_elevated(self) -> bool:
        """Return whether this process may hold an administrator token (unknown counts as elevated)."""

    def relaunch_elevated(self, argv: Sequence[str]) -> bool:
        """Ask for one elevated run of the bundled launcher with *argv*; return whether it was started."""

    def directory_is_admin_only(self, path: Path) -> bool:
        """Return whether only administrators can modify *path* and replace its parents."""

    def directory_untrusted_reason(self, path: Path) -> str | None:
        """Return why *path* is not admin-only, or ``None`` when it is."""

    # -- locations ---------------------------------------------------------

    def scope_folders(self, scope: Scope) -> ScopeFolders:
        """Return the default shortcut and wrapper folders of *scope*.

        Raises ``ValueError`` when the machine does not identify them.
        """

    # -- processes ---------------------------------------------------------

    def run(
        self,
        argv: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_line: Callable[[str], None] | None = None,
        *,
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> ProcessResult:
        """Run a child process to completion and return its status and output.

        ``on_line`` receives each output line (both streams) as it is read.
        ``argv[0]`` may be a ``.ps1``, ``.cmd`` or ``.bat`` script, which the
        implementation starts through its interpreter. A non-zero exit status
        is returned, not raised.
        """

    # -- network -----------------------------------------------------------

    def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        dest: Path,
        on_progress: Callable[[int, int | None], None] | None = None,
        *,
        timeout: float = 60,
    ) -> int:
        """Download *url* to the file *dest* and return the byte count.

        ``on_progress(received, total)`` is called with ``received == 0`` once
        before the first byte and then after each chunk; ``total`` is ``None``
        when the server gives no size.
        """

    def fetch_text(
        self, url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 60
    ) -> str:
        """Return the UTF-8 text served at *url*."""


# ---------------------------------------------------------------------------
# Shared patterns
# ---------------------------------------------------------------------------


def is_elevated_run(system: System) -> bool:
    """Return whether this run must use only trusted, non-implicit behavior.

    The launcher's ``GUPKG_ELEVATED=1`` marker can only make behavior stricter,
    so it counts even when the token says otherwise.
    """
    return os.environ.get("GUPKG_ELEVATED") == "1" or system.is_elevated()


def require_admin_only(system: System, directory: Path) -> None:
    """Raise ``UntrustedLocationError`` unless only administrators can modify *directory*.

    Parameters
    ----------
    system : System
        Boundary that answers the question.
    directory : Path
        Directory an elevated run is about to trust.

    Raises
    ------
    UntrustedLocationError
        If *directory* or one of its parents can be changed by anyone else, or
        its permissions cannot be read.
    """
    reason = system.directory_untrusted_reason(directory)
    if reason is not None:
        raise UntrustedLocationError(untrusted_message(Path(directory), reason))


def run_checked(
    system: System,
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    timeout: float | None = None,
) -> str:
    """Run a child process that must succeed and return its stripped standard output.

    Raises
    ------
    ProcessFailed
        If the process exits with a non-zero status; the message names the
        command and includes its error output.
    """
    result = system.run(argv, cwd, env, on_line, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProcessFailed(
            f"Command {' '.join(str(part) for part in argv)!r} exited with status "
            f"{result.returncode}" + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()
