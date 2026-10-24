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

``http_request`` returns the response of any status without following redirects, and
``http_download`` streams a body into a file (appending a ``206`` answer when asked); redirect,
retry and token policy live in ``gupkg.services.http``, not in the implementations.

Beyond the groups in the issue, the protocol has ``scope_folders`` (the Start Menu
``opt`` and ``bin`` folders of a scope, so the sandbox can keep them off the real
machine), ``directory_untrusted_reason`` (why a folder is not admin-only, next to
``directory_is_admin_only``), and ``run`` takes ``input_text`` and ``timeout`` and
returns a ``ProcessResult``. ``WindowsSystem.run`` starts ``.ps1``, ``.cmd`` and
``.bat`` paths through their interpreter itself, so no caller names a shell.

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
from contextlib import AbstractContextManager
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


class ProcessAborted(OSError):
    """Report a child process that gupkg killed because its watcher asked for it."""


class WorkerDidNotStart(TimeoutError):
    """Report that the regex worker never said it was up within its start limit (not a slow pattern)."""


class ProcessNotAllowed(OSError):
    """Report that a sandbox refused to start a program that it has no double for (the package needs a real run)."""


class ProcessFailed(RuntimeError):
    """Report that a child process that had to succeed exited with a failure status."""


@dataclass(frozen=True)
class HttpResponse:
    """Hold one HTTP response whatever its status: final URL, lower-case headers and body.

    ``http_request`` never follows a redirect, so ``url`` is the URL that was
    requested and a 3xx response carries its ``location`` header. Repeated
    headers are joined with ``", "``.
    """

    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass(frozen=True)
class DownloadResponse:
    """Report one ``http_download`` call: status, headers, and how the file was written.

    ``written`` counts the bytes this call wrote to the file. ``resumed`` is true
    only when the server answered a ``Range`` request with 206 and the bytes were
    appended. A 3xx or error status writes nothing.
    """

    status: int
    url: str
    headers: Mapping[str, str]
    written: int = 0
    resumed: bool = False


class ResponseTooLarge(OSError):
    """Report that a response body is larger than the size the caller allows."""


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

    def scratch_directory(self, prefix: str) -> AbstractContextManager[Path]:
        """Return a context manager that creates a private empty temporary folder, yields it, and deletes it on exit.

        For work that must leave no trace in any root (a report-only run). A sandbox keeps it inside its own folder, so
        nothing is written to the real temporary folder.
        """

    def process_id(self) -> int:
        """Return the id of this process (recorded in a package lock so another run can tell whether the holder lives)."""

    def process_is_running(self, pid: int) -> bool:
        """Return whether a process with id *pid* is running; an id that cannot be judged counts as running."""

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
        watch: Callable[[], str | None] | None = None,
    ) -> ProcessResult:
        """Run a child process to completion and return its status and output.

        ``on_line`` receives each output line (both streams) as it is read.
        ``argv[0]`` may be a ``.ps1``, ``.cmd`` or ``.bat`` script, which the
        implementation starts through its interpreter. A non-zero exit status
        is returned, not raised. ``watch`` is called about five times a second while the
        process runs; when it returns text the process is killed and ``ProcessAborted`` is
        raised with that text (how a caller bounds what a child writes to disk).
        """

    def regex_worker(
        self,
        request: str,
        timeout: float,
        *,
        on_line: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Run the isolated regular-expression worker on *request* and return its status and output.

        The worker is ``gupkg/system/regex_worker.py`` started with the running interpreter and
        ``-I -s -B -S``, a minimal environment and an empty temporary folder as its working
        directory. *request* is sent on standard input; ``stdout`` of the result is the answer
        (see the worker for the protocol). When the worker is still running *timeout* seconds
        after it reported that it is up (its first line, ``pid <number>``, so a slow process
        start does not count) its whole process tree is killed and ``TimeoutError`` is raised,
        so a pattern that makes ``re`` run for ever costs the caller about *timeout* seconds. ``on_line``
        receives each line the worker writes, the first being ``pid <number>``.
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

    def http_request(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        method: str = "GET",
        timeout: float = 60,
        max_bytes: int | None = None,
        total_timeout: float | None = None,
    ) -> HttpResponse:
        """Send one request and return the response of any status, without following redirects.

        Only ``http://``, ``https://`` and ``file://`` URLs are served. At most
        *max_bytes* of the body are read; a longer body raises
        ``ResponseTooLarge``, and ``max_bytes=0`` returns the headers only. Network
        failures raise ``OSError``; *timeout* is the idle time of each socket operation and
        *total_timeout* the time the whole answer may take (``TimeoutError``). Redirect, retry and header policy belong to
        ``gupkg.services.http``, which calls this method once per hop.
        """

    def http_download(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        dest: Path,
        on_progress: Callable[[int, int | None], None] | None = None,
        *,
        timeout: float = 60,
        append: bool = False,
        on_headers: Callable[[int, Mapping[str, str]], None] | None = None,
        total_timeout: float | None = None,
    ) -> DownloadResponse:
        """Stream one response body into *dest* and report how it went; redirects are not followed.

        A 2xx answer is written to *dest*. With ``append=True`` the caller has sent
        a ``Range`` header: a 206 answer is appended to *dest*, and any other 2xx
        answer replaces it (the server ignored the range). A 3xx, 4xx or 5xx answer
        leaves *dest* untouched and writes nothing. ``on_progress(received, total)``
        counts the bytes of the whole file, resumed ones included. ``on_headers(status,
        headers)`` is called once, before the first body byte, for every answer, so a
        caller can remember the validators (``etag``, ``last-modified``) of a file whose
        body is then interrupted.
        """


# ---------------------------------------------------------------------------
# Shared patterns
# ---------------------------------------------------------------------------


# The only variables a child process such as git inherits: machine-level paths, never user settings.
_SAFE_ENVIRONMENT_NAMES = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PROGRAMDATA")


def safe_process_environment(extra: Mapping[str, str] | None = None, *, system: "System | None" = None) -> dict[str, str]:
    """Return a minimal environment for a child process: machine-level variables plus *extra*.

    Nothing of the user's own settings is passed on (decision D19: an elevated gupkg ignores them,
    and a normal one gives a child process no more than it needs). When *system* is given and the run is elevated, ``PATH``
    holds only the folders of the process ``PATH`` that only administrators can change (``trusted_path_entries``), because
    the process ``PATH`` can hold the user's own writable folders and an elevated child must not find a program there.
    """
    kept = {name: value for name, value in os.environ.items() if name.upper() in _SAFE_ENVIRONMENT_NAMES}
    if system is not None and is_elevated_run(system):
        # Nothing here may come from the process environment, which a standard user can influence: ``PATH`` is the admin-only
        # folders, the Windows folder values come from the trusted Windows folder (and are left out when there is none),
        # ``PATHEXT`` is the fixed default, and ``TEMP``/``TMP``/``PROGRAMDATA`` are left out unless the caller gives them.
        kept = {}
        kept["PATH"] = os.pathsep.join(trusted_path_entries(system))
        root = trusted_system_root(system)
        if root is not None:
            kept.update({"SYSTEMROOT": str(root), "WINDIR": str(root), "COMSPEC": str(root / "System32" / "cmd.exe")})
        kept["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    kept.update(extra or {})
    return kept


def trusted_path_entries(system: System) -> list[str]:
    """Return the folders of the process ``PATH`` that only administrators can change, in order.

    An elevated run looks tools up only here (``git``, for example), never in a folder a standard user can
    write: the process ``PATH`` can hold the user's own entries.
    """
    trusted: list[str] = []
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = entry.strip().strip('"')

        # Only an absolute local folder that exists: a relative entry or `.` is the current directory, `%VAR%` is expanded
        # by whoever reads it, and a folder that is missing can be created later by anyone.
        if not folder or "%" in folder or folder.startswith(("\\\\", "//")):
            continue
        path = Path(folder)
        try:
            if not path.is_absolute() or not path.is_dir():
                continue
            key = os.path.normcase(os.path.normpath(folder)).rstrip("\\/")
            if key in seen or system.directory_untrusted_reason(path) is not None:
                continue
        except OSError:
            continue
        seen.add(key)
        trusted.append(folder)
    return trusted


def trusted_system_root(system: System) -> Path | None:
    """Return the Windows folder named by ``SYSTEMROOT`` or ``WINDIR`` when it is an existing admin-only folder, else ``None``.

    The process environment can be set by a standard user, so the name is only a candidate: the folder itself is judged.
    """
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name, "").strip()
        if not value or "%" in value:
            continue
        path = Path(value)
        try:
            if path.is_absolute() and path.is_dir() and system.directory_untrusted_reason(path) is None:
                return path
        except OSError:
            continue
    return None


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
