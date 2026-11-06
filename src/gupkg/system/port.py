"""Define the single boundary through which gupkg touches the machine.

The :class:`System` protocol lists every side effect that depends on the
operating system or the network: directory links, environment and ``PATH``
values, shortcuts, elevation, child processes, and downloads. Engine and
service code receive a ``System`` explicitly, so the real implementation
(``WindowsSystem``) and the in-memory one (``FakeSystem``) are interchangeable.

Usage and API
-------------
Annotate parameters with ``System`` and call its methods. Use ``Mode`` to name
the user or machine target of an environment or shortcut operation. Use
``run_checked(system, argv, ...)`` for the pattern that every caller repeats. ``is_elevated_run(system)`` is
asked once, by ``gupkg.scope.process_mode``; every other place receives the run's ``Scope``.

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
import unicodedata
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence


class Mode(StrEnum):
    """Install mode of a gupkg run: where its environment, ``PATH`` and shortcuts go.

    ``USER`` targets per-user configuration locations. ``SYSTEM`` targets machine-wide
    configuration locations and therefore requires Administrator privileges. The members are
    strings (``"user"``, ``"system"``), so a mode compares equal to its name and prints as it.
    """

    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class EnvValue:
    """Hold one stored environment value and whether it expands ``%VAR%`` references.

    Attributes
    ----------
    value : str
        The stored text.
    expandable : bool
        Whether ``%VAR%`` references are expanded.
    """

    value: str
    expandable: bool


@dataclass(frozen=True)
class ShortcutSpec:
    """Hold the fields of one Windows shortcut.

    Attributes
    ----------
    target_path : str
        The target, unresolved.
    arguments : str
        The command-line arguments.
    working_directory : str
        The working directory.
    icon_location : str
        ``path,index`` of the icon; empty for none.
    description : str
        The shortcut's description.
    run_as_admin : bool
        Whether opening the link asks for elevation.

    Notes
    -----
    ``target_path`` is stored unresolved and ``%VAR%`` references stay
    unexpanded. ``icon_location`` is ``"path,index"``, or empty for no icon.
    Windows limits what a link can hold. Measured limits: a target longer than
    259 characters is refused when writing, and the working directory, the
    description and the icon path are cut at 260 characters when read back.
    ``run_as_admin`` sets the link flag ``SLDF_RUNAS_USER``: Windows shows the
    UAC prompt when the link is opened.
    """

    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""
    run_as_admin: bool = False


@dataclass(frozen=True)
class ScopeFolders:
    """Name where one scope's Start Menu shortcuts and command wrappers go.

    Attributes
    ----------
    shortcut_root : Path
        Where the scope's Start Menu shortcuts go.
    bin_dir : Path
        Where the scope's command wrappers go.
    """

    shortcut_root: Path
    bin_dir: Path


@dataclass(frozen=True)
class ProcessResult:
    """Report a finished child process: exit status and everything it printed.

    Attributes
    ----------
    returncode : int
        The exit status.
    stdout : str
        The standard output.
    stderr : str
        The standard error.
    """

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


# Characters that mean something to ``cmd.exe``: a ``.cmd`` launcher must never receive them.
UNSAFE_LAUNCHER_CHARACTERS = frozenset('&|<>^"%!`')


def unsafe_launcher_argument(value: str) -> str | None:
    """Return why *value* may not be passed to a ``.cmd`` launcher, or ``None`` when it is plain.

    The child is started without a shell, but the value also reaches log files and later shell commands, so the set
    stays the strict one: none of ``& | < > ^ " % ! ` ``, and no character of a Unicode category starting with ``C`` (control
    characters including C1, format characters such as the bidi overrides and isolates and the zero-width joiners, lone
    surrogates, private-use and unassigned code points) nor a line or paragraph separator (U+2028, U+2029).

    Parameters
    ----------
    value : str
        Candidate launcher argument.

    Returns
    -------
    str or None
        A short description of the first offending character, or ``None`` when *value* is plain.

    Examples
    --------
    Basic usage:

    >>> print(unsafe_launcher_argument('--version'))
    None
    >>> unsafe_launcher_argument('a&b')
    "the character & in 'a&b'"
    >>> unsafe_launcher_argument('50%')
    "the character % in '50%'"
    """
    # Report the first character that is a cmd.exe metacharacter, a control/format character or a line separator.
    for character in str(value):
        category = unicodedata.category(character)
        if character in UNSAFE_LAUNCHER_CHARACTERS or category[0] == "C" or category in ("Zl", "Zp"):
            shown = character if category[0] not in "CZ" else f"U+{ord(character):04X}"
            return f"the character {shown} in {str(value)[:60]!r}"
    return None


@dataclass(frozen=True)
class HttpResponse:
    """Hold one HTTP response whatever its status: final URL, lower-case headers and body.

    ``http_request`` never follows a redirect, so ``url`` is the URL that was
    requested and a 3xx response carries its ``location`` header. Repeated
    headers are joined with ``", "``.

    Attributes
    ----------
    status : int
        The HTTP status.
    url : str
        The URL that was requested.
    headers : mapping of str to str
        The lower-case response headers.
    body : bytes
        The response body.
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

    Attributes
    ----------
    status : int
        The HTTP status.
    url : str
        The URL that was requested.
    headers : mapping of str to str
        The lower-case response headers.
    written : int
        The bytes this call wrote to the file.
    resumed : bool
        Whether the bytes were appended to a partial file.
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

    Methods
    -------
    create_junction
        Create *link* as a junction to the existing directory *target*, replacing a junction.
    read_link
        Return the normalized target of the link at *path*, or ``None`` if it is not a readable link.
    is_link
        Return whether *path* is a directory link (a junction) rather than a real directory.
    remove_link
        Remove the link at *path* without touching its target; refuse anything that is not a link.
    hardlink
        Create *link* as a second name for the existing file *existing*.
    get_env
        Return one stored environment value, or ``None`` when it is not set (names are case-insensitive).
    set_env
        Store one environment value, replacing any value with the same (case-insensitive) name.
    delete_env
        Remove one environment value; removing an absent value is not an error.
    get_path
        Return the non-empty ``PATH`` entries of *scope* in stored order; a missing value is empty.
    set_path
        Store *entries* as the expandable ``PATH`` of *scope*.
    broadcast_environment_change
        Tell running programs that environment values changed; raises ``OSError`` when refused.
    write_shortcut
        Create or replace the shortcut at *path*, creating missing parent folders.
    read_shortcut
        Return the fields stored in the shortcut at *path*.
    delete_shortcut
        Delete the shortcut at *path*; deleting an absent shortcut is not an error.
    is_elevated
        Return whether this process may hold an administrator token (unknown counts as elevated).
    directory_is_admin_only
        Return whether only administrators can modify *path* and replace its parents.
    directory_untrusted_reason
        Return why *path* is not admin-only, or ``None`` when it is.
    scratch_directory
        Return a context manager that creates a private empty temporary folder, yields it, and deletes it on exit.
    process_id
        Return the id of this process (recorded in a package lock so another run can tell whether the holder lives).
    process_is_running
        Return whether a process with id *pid* is running; an id that cannot be judged counts as running.
    scope_folders
        Return the default shortcut and wrapper folders of *scope*.
    run
        Run a child process to completion and return its status and output.
    run_foreground
        Run a child process on this process's own console and return its exit status.
    open_path
        Open *path* with the program Windows associates with it (``gupkg logs --open`` opens a log in the editor).
    regex_worker
        Run the isolated regular-expression worker on *request* and return its status and output.
    fetch
        Download *url* to the file *dest* and return the byte count.
    fetch_text
        Return the UTF-8 text served at *url*.
    http_request
        Send one request and return the response of any status, without following redirects.
    http_download
        Stream one response body into *dest* and report how it went; redirects are not followed.
    """

    # -- links -------------------------------------------------------------

    def create_junction(self, link: Path, target: Path) -> None:
        """Create *link* as a junction to the existing directory *target*, replacing a junction.

        Raises ``RuntimeError`` when *target* is missing, or when *link* exists
        and is not a junction; nothing is changed in either case.

        Parameters
        ----------
        link : Path
            Location of the junction to create or replace.
        target : Path
            Existing directory the junction points to.
        """

    def read_link(self, path: Path) -> Path | None:
        """Return the normalized target of the link at *path*, or ``None`` if it is not a readable link.

        Parameters
        ----------
        path : Path
            Location to inspect.

        Returns
        -------
        Path or None
            Normalized link target, or ``None``.
        """

    def is_link(self, path: Path) -> bool:
        """Return whether *path* is a directory link (a junction) rather than a real directory.

        Parameters
        ----------
        path : Path
            Location to inspect.

        Returns
        -------
        bool
            True for a junction.
        """

    def remove_link(self, path: Path) -> None:
        """Remove the link at *path* without touching its target; refuse anything that is not a link.

        Parameters
        ----------
        path : Path
            Link to remove.
        """

    def hardlink(self, link: Path, existing: Path) -> None:
        """Create *link* as a second name for the existing file *existing*.

        Parameters
        ----------
        link : Path
            New name to create.
        existing : Path
            File that already exists.
        """

    # -- environment -------------------------------------------------------

    def get_env(self, scope: Mode, name: str) -> EnvValue | None:
        """Return one stored environment value, or ``None`` when it is not set (names are case-insensitive).

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is read.
        name : str
            Variable name.

        Returns
        -------
        EnvValue or None
            The stored value, or ``None``.
        """

    def set_env(self, scope: Mode, name: str, value: str, expandable: bool) -> None:
        """Store one environment value, replacing any value with the same (case-insensitive) name.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is written.
        name : str
            Variable name.
        value : str
            Value to store.
        expandable : bool
            Whether ``%VAR%`` references in the value are expanded when read.
        """

    def delete_env(self, scope: Mode, name: str) -> None:
        """Remove one environment value; removing an absent value is not an error.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is changed.
        name : str
            Variable name.
        """

    def get_path(self, scope: Mode) -> list[str]:
        """Return the non-empty ``PATH`` entries of *scope* in stored order; a missing value is empty.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine ``PATH`` is read.

        Returns
        -------
        list[str]
            The stored entries in order.
        """

    def set_path(self, scope: Mode, entries: Sequence[str]) -> None:
        """Store *entries* as the expandable ``PATH`` of *scope*.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine ``PATH`` is written.
        entries : Sequence[str]
            Folders to store, in order.
        """

    def broadcast_environment_change(self) -> None:
        """Tell running programs that environment values changed; raises ``OSError`` when refused."""

    # -- shortcuts ---------------------------------------------------------

    def write_shortcut(self, path: Path, spec: ShortcutSpec) -> None:
        """Create or replace the shortcut at *path*, creating missing parent folders.

        Parameters
        ----------
        path : Path
            Location of the shortcut file.
        spec : ShortcutSpec
            Fields the shortcut holds.
        """

    def read_shortcut(self, path: Path) -> ShortcutSpec:
        """Return the fields stored in the shortcut at *path*.

        Parameters
        ----------
        path : Path
            Location of the shortcut file.

        Returns
        -------
        ShortcutSpec
            The stored fields.
        """

    def delete_shortcut(self, path: Path) -> None:
        """Delete the shortcut at *path*; deleting an absent shortcut is not an error.

        Parameters
        ----------
        path : Path
            Location of the shortcut file.
        """

    # -- privilege ---------------------------------------------------------

    def is_elevated(self) -> bool:
        """Return whether this process may hold an administrator token (unknown counts as elevated).

        Returns
        -------
        bool
            True when elevated or unknown.
        """

    def directory_is_admin_only(self, path: Path) -> bool:
        """Return whether only administrators can modify *path* and replace its parents.

        Parameters
        ----------
        path : Path
            Directory to judge.

        Returns
        -------
        bool
            True when the directory is admin-only.
        """

    def directory_untrusted_reason(self, path: Path) -> str | None:
        """Return why *path* is not admin-only, or ``None`` when it is.

        Parameters
        ----------
        path : Path
            Directory to judge.

        Returns
        -------
        str or None
            The reason, or ``None`` when admin-only.
        """

    def scratch_directory(self, prefix: str) -> AbstractContextManager[Path]:
        """Return a context manager that creates a private empty temporary folder, yields it, and deletes it on exit.

        For work that must leave no trace in any root (a report-only run). A sandbox keeps it inside its own folder, so
        nothing is written to the real temporary folder.

        Parameters
        ----------
        prefix : str
            Prefix of the folder name.

        Returns
        -------
        AbstractContextManager[Path]
            Context manager yielding the folder.
        """

    def process_id(self) -> int:
        """Return the id of this process (recorded in a package lock so another run can tell whether the holder lives).

        Returns
        -------
        int
            The process id.
        """

    def process_is_running(self, pid: int) -> bool:
        """Return whether a process with id *pid* is running; an id that cannot be judged counts as running.

        Parameters
        ----------
        pid : int
            Process id to look up.

        Returns
        -------
        bool
            True when the process runs or cannot be judged.
        """

    # -- locations ---------------------------------------------------------

    def scope_folders(self, scope: Mode) -> ScopeFolders:
        """Return the default shortcut and wrapper folders of *scope*.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine folders are wanted.

        Returns
        -------
        ScopeFolders
            The shortcut root and the command wrapper folder.

        Raises
        ------
        ValueError
            When the machine does not identify them.
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

        Parameters
        ----------
        argv : Sequence[str]
            Program and arguments.
        cwd : Path or None
            Working directory, or the current one.
        env : Mapping[str, str] or None
            Complete child environment, or the inherited one.
        on_line : Callable[[str], None] or None
            Receiver of each output line.
        input_text : str or None
            Text sent on standard input.
        timeout : float or None
            Seconds after which the child is killed.
        watch : Callable[[], str | None] or None
            Abort check; returning text kills the child.

        Returns
        -------
        ProcessResult
            Exit status and captured output.

        Raises
        ------
        ProcessAborted
            When *watch* returned text.
        TimeoutError
            When *timeout* elapsed.
        """

    def run_foreground(self, argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        """Run a child process on this process's own console and return its exit status.

        The child inherits standard input, output and error, so an interactive program (the UI programs that
        ``gupkg tui`` and ``gupkg manager`` forward to) works as if it had been started directly. ``argv[0]`` may
        be a ``.cmd`` or ``.bat`` script, which the implementation starts through its interpreter. Output is not
        captured and not reported as events.

        Parameters
        ----------
        argv : Sequence[str]
            Program and arguments.
        cwd : Path or None
            Working directory, or the current one.
        env : Mapping[str, str] or None
            Complete child environment, or the inherited one.

        Returns
        -------
        int
            The child's exit status.

        Raises
        ------
        OSError
            When the program cannot be started.
        """

    def open_path(self, path: Path) -> None:
        """Open *path* with the program Windows associates with it (``gupkg logs --open`` opens a log in the editor).

        Parameters
        ----------
        path : Path
            File or folder to open.

        Raises
        ------
        OSError
            When nothing can open it.
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

        Parameters
        ----------
        request : str
            Request text sent on standard input.
        timeout : float
            Seconds the worker may run after it reported that it is up.
        on_line : Callable[[str], None] or None
            Receiver of each worker output line.

        Returns
        -------
        ProcessResult
            Exit status and output of the worker.

        Raises
        ------
        TimeoutError
            When the worker outlives *timeout*.
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

        Parameters
        ----------
        url : str
            Address to download.
        headers : Mapping[str, str] or None
            Request headers.
        dest : Path
            File to write.
        on_progress : Callable[[int, int | None], None] or None
            Progress receiver.
        timeout : float
            Idle seconds allowed per socket operation.

        Returns
        -------
        int
            Number of bytes written.
        """

    def fetch_text(
        self, url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 60
    ) -> str:
        """Return the UTF-8 text served at *url*.

        Parameters
        ----------
        url : str
            Address to read.
        headers : Mapping[str, str] or None
            Request headers.
        timeout : float
            Idle seconds allowed per socket operation.

        Returns
        -------
        str
            The decoded body.
        """

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

        Parameters
        ----------
        url : str
            Address to request.
        headers : Mapping[str, str] or None
            Request headers.
        method : str
            HTTP method.
        timeout : float
            Idle seconds allowed per socket operation.
        max_bytes : int or None
            Largest body to read; ``None`` for no limit, ``0`` for headers only.
        total_timeout : float or None
            Seconds the whole answer may take.

        Returns
        -------
        HttpResponse
            Status, headers and body of the answer.

        Raises
        ------
        ResponseTooLarge
            When the body exceeds *max_bytes*.
        OSError
            On network failures.
        TimeoutError
            When *total_timeout* elapses.
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

        Parameters
        ----------
        url : str
            Address to download.
        headers : Mapping[str, str] or None
            Request headers (a ``Range`` header when resuming).
        dest : Path
            File to write or append to.
        on_progress : Callable[[int, int | None], None] or None
            Progress receiver.
        timeout : float
            Idle seconds allowed per socket operation.
        append : bool
            Whether a 206 answer is appended to *dest*.
        on_headers : Callable[[int, Mapping[str, str]], None] or None
            Receiver of the status and headers before the first body byte.
        total_timeout : float or None
            Seconds the whole download may take.

        Returns
        -------
        DownloadResponse
            Status, headers and how the file was written.

        Raises
        ------
        OSError
            On network failures.
        TimeoutError
            When *total_timeout* elapses.
        """


# ---------------------------------------------------------------------------
# Shared patterns
# ---------------------------------------------------------------------------


# The only variables a child process such as git inherits: machine-level paths, never user settings.
_SAFE_ENVIRONMENT_NAMES = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PROGRAMDATA")


def safe_process_environment(
    extra: Mapping[str, str] | None = None, *, system: "System | None", mode: Mode
) -> dict[str, str]:
    """Return a minimal environment for a child process: machine-level variables plus *extra*.

    Nothing of the user's own settings is passed on (decision D19: an elevated gupkg ignores them,
    and a normal one gives a child process no more than it needs). When *system* is given and *mode* is ``Mode.SYSTEM`` (an elevated run), ``PATH``
    holds only the folders of the process ``PATH`` that only administrators can change (``trusted_path_entries``), because
    the process ``PATH`` can hold the user's own writable folders and an elevated child must not find a program there.

    Parameters
    ----------
    extra : Mapping[str, str] or None
        Variables added on top of the inherited machine-level ones.
    system : System or None
        Machine boundary used to judge folders; ``None`` skips the elevated treatment.
    mode : Mode
        ``Mode.SYSTEM`` selects the strict, admin-only environment.

    Returns
    -------
    dict[str, str]
        The environment to give the child.
    """
    # Start from the machine-level variables of the process environment.
    kept = {name: value for name, value in os.environ.items() if name.upper() in _SAFE_ENVIRONMENT_NAMES}
    if system is not None and mode is Mode.SYSTEM:
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

    Parameters
    ----------
    system : System
        Machine boundary that judges each folder.

    Returns
    -------
    list[str]
        Trusted folders in ``PATH`` order, without duplicates.
    """
    # Walk the entries in order, keeping each distinct, existing, admin-only absolute folder once.
    trusted: list[str] = []
    seen: set[str] = set()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = entry.strip().strip('"')

        # Only an absolute local folder that exists: a relative entry or `.` is the current directory, `%VAR%` is expanded
        # by whoever reads it, and a folder that is missing can be created later by anyone.
        if not folder or "%" in folder or folder.startswith(("\\\\", "//")):
            continue
        # Judge the folder itself: skip duplicates and anything a standard user could modify.
        path = Path(folder)
        try:
            if not path.is_absolute() or not path.is_dir():
                continue
            key = os.path.normcase(os.path.normpath(folder)).rstrip("\\/")
            if key in seen or system.directory_untrusted_reason(path) is not None:
                continue
        except OSError:
            continue
        # Remember the folder so a repeated spelling of it is skipped, and keep it.
        seen.add(key)
        trusted.append(folder)
    return trusted


def trusted_system_root(system: System) -> Path | None:
    """Return the Windows folder named by ``SYSTEMROOT`` or ``WINDIR`` when it is an existing admin-only folder, else ``None``.

    The process environment can be set by a standard user, so the name is only a candidate: the folder itself is judged.

    Parameters
    ----------
    system : System
        Machine boundary that judges the folder.

    Returns
    -------
    Path or None
        The trusted Windows folder, or ``None`` when neither variable names one.
    """
    # Accept the first candidate that is an existing absolute folder only administrators can change.
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name, "").strip()
        if not value or "%" in value:
            continue
        # The candidate counts only when the folder itself passes the admin-only test.
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

    Parameters
    ----------
    system : System
        Machine boundary asked for the token's elevation.

    Returns
    -------
    bool
        True when the run must behave as elevated.
    """
    return os.environ.get("GUPKG_ELEVATED") == "1" or system.is_elevated()


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

    Parameters
    ----------
    system : System
        Machine boundary that starts the process.
    argv : Sequence[str]
        Program and arguments.
    cwd : Path or None
        Working directory, or the current one.
    env : Mapping[str, str] or None
        Complete child environment, or the inherited one.
    on_line : Callable[[str], None] or None
        Receiver of each output line.
    timeout : float or None
        Seconds after which the child is killed.

    Returns
    -------
    str
        The child's standard output with surrounding whitespace removed.

    Raises
    ------
    ProcessFailed
        If the process exits with a non-zero status; the message names the
        command and includes its error output.
    """
    # Run once, then turn any failure status into an exception that carries the command and its error output.
    result = system.run(argv, cwd, env, on_line, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ProcessFailed(
            f"Command {' '.join(str(part) for part in argv)!r} exited with status "
            f"{result.returncode}" + (f": {detail}" if detail else "")
        )
    return result.stdout.strip()
