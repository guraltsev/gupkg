"""Provide an in-memory System for tests and for ``gupkg --sandbox``.

``FakeSystem`` implements the same protocol as ``WindowsSystem`` without
touching the machine's registry, Start Menu, ``PATH`` or privileges. Files and
directory links are real, but live below one temporary directory; environment
values live in memory; shortcuts are records written as ``.lnk.json`` files;
elevation is a flag; downloads come from a URL fixture map; child processes
run only from an allow-list of test doubles (plus a real 7-Zip when present).

Usage and API
-------------
Build ``FakeSystem(root, elevated=False, urls={...}, processes={...})`` in a
test and inspect ``registry_log``, ``env_values(...)``, ``shortcuts()``,
and ``broadcasts`` afterwards. ``FakeSystem.sandbox(directory,
delegate)`` is what ``--sandbox DIR`` uses: it keeps its registry state in
``DIR/system.json`` across runs and sends network and process calls that have
no fixture to the real ``delegate``. Child processes reach the real system only
when the command is one allowed *by identity*: ``git`` (a bare ``git`` or the exact
resolved ``git.exe`` found on ``PATH``) and the bundled 7-Zip (the exact path
``find_7z()`` returns). Any other command, including a script or a copy named
``git.exe`` or ``7z.exe``, raises ``UnexpectedCall``.

Implementation Approach
-----------------------
Every registry read, write and delete is appended to ``registry_log`` in order,
so a behavior test can assert that an install touched nothing but ``PATH`` (D18,
D38). Anything the fixtures do not cover raises ``UnexpectedCall`` (an
``OSError``), so a test that forgets a fixture fails loudly instead of reaching
the machine.

See Also
--------
gupkg.system.port : the protocol this class implements.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..extractors import find_7z
from .port import (
    DownloadResponse,
    EnvValue,
    HttpResponse,
    ProcessAborted,
    ProcessNotAllowed,
    ProcessResult,
    ResponseTooLarge,
    Mode,
    ScopeFolders,
    ShortcutSpec,
    System,
)
from .windows import WindowsLinks, WindowsSystem


class UnexpectedCall(OSError):
    """Report a URL, process or action that the fake was not set up to serve."""


class UnexpectedProcess(UnexpectedCall, ProcessNotAllowed):
    """Report a program that the fake has no double for, so the engine can say the package needs a real run."""


@dataclass(frozen=True)
class RegistryAccess:
    """Record one registry read, write or delete made through a ``FakeSystem``.

    Attributes
    ----------
    operation : str
        ``read``, ``write`` or ``delete``.
    scope : str
        The scope whose environment was touched.
    name : str
        The value name.
    value : str or None
        The value written; ``None`` for a read or delete.
    """

    operation: str
    scope: str
    name: str
    value: str | None = None


@dataclass(frozen=True)
class FakeResponse:
    """Serve a URL fixture whose size the server may or may not announce.

    Attributes
    ----------
    body : bytes
        The body served.
    report_size : bool
        Whether the server announces the size.
    """

    body: bytes
    report_size: bool = True


@dataclass(frozen=True)
class FakeHttp:
    """Serve a URL fixture with a chosen status and headers (``http_request``, ``http_download``).

    ``headers`` are the response headers (any letter case). With ``ranges`` true the
    fixture honours ``Range: bytes=N-`` with a 206 answer, as a resumable server does.
    ``fail_after`` makes ``http_download`` drop the connection (``OSError``) after that
    many body bytes, leaving the partial file behind. With ``report_size`` false the server
    does not announce the size, so ``http_download`` reports progress with an unknown total.

    Attributes
    ----------
    body : bytes or str
        The body served.
    status : int
        The HTTP status.
    headers : mapping of str to str
        The response headers.
    ranges : bool
        Whether ``Range`` requests are honoured.
    fail_after : int or None
        Body bytes after which a download drops the connection.
    report_size : bool
        Whether the server announces the size.
    """

    body: bytes | str = b""
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    ranges: bool = False
    fail_after: int | None = None
    report_size: bool = True


@dataclass(frozen=True)
class RecordedRequest:
    """Record one ``http_request`` or ``http_download`` call made to a ``FakeSystem``.

    Attributes
    ----------
    method : str
        The HTTP method.
    url : str
        The requested URL.
    headers : mapping of str to str
        The request headers.
    timeout : float
        The per-request timeout.
    max_bytes : int or None
        The largest body accepted.
    total_timeout : float or None
        The deadline for the whole call.
    """

    method: str
    url: str
    headers: Mapping[str, str]
    timeout: float
    max_bytes: int | None = None
    total_timeout: float | None = None


@dataclass(frozen=True)
class ProcessCall:
    """Describe one child-process request handed to a process double.

    Attributes
    ----------
    argv : tuple of str
        The command line.
    cwd : Path or None
        The working directory.
    env : mapping or None
        The environment.
    input_text : str or None
        The text sent on standard input.
    """

    argv: tuple[str, ...]
    cwd: Path | None
    env: Mapping[str, str] | None
    input_text: str | None


@dataclass
class FakeSystem(WindowsLinks):
    """Keep a pretend machine below *root*; see the module docstring.

    Parameters
    ----------
    root : Path
        Directory that holds the fake Start Menu, ``bin`` folders and shortcut
        records. It is created when missing.
    elevated : bool
        What ``is_elevated`` reports.
    urls : mapping, optional
        URL to ``bytes``, ``str``, :class:`FakeResponse`, an exception instance
        to raise, or a callable ``(url, headers)`` returning one of those.
    processes : mapping, optional
        Program name (``"git"``, ``"origin.ps1"``) to a callable taking a
        :class:`ProcessCall` and returning a :class:`ProcessResult`.
    untrusted_dirs : iterable of Path, optional
        Directories (and everything below them) that are not admin-only; every
        other directory counts as admin-only.
    delegate : System, optional
        Real system that serves network and process calls with no fixture.
    real_7zip : bool
        Whether a 7-Zip executable that exists may run for real.
    state_file : Path, optional
        JSON file that persists the registry, its log and the counters.
    pid : int
        What ``process_id`` reports.
    live_pids : set of int
        Other process ids that ``process_is_running`` reports as running (a lock held by a living second run).
    failures : dict, optional
        Method name to an exception that the next call of that method raises
        (once), to exercise failure paths.

    Methods
    -------
    sandbox
        Open (or start) the sandbox kept in *directory*, with network and processes real.
    env_values
        Return the stored environment values of *scope* by name, without logging an access.
    shortcuts
        Return every recorded shortcut by the path it was written to.
    add_url
        Serve *content* (see the ``urls`` parameter) at *url*.
    allow_real
        Let the named programs run for real, for example ``git`` against a local fixture repository.
    create_junction
        Create a real junction; see :meth:`WindowsLinks.create_junction`.
    get_env
        Return one stored value (logged as a registry read).
    set_env
        Store one value (logged as a registry write).
    delete_env
        Remove one value (logged as a registry delete).
    get_path
        Return the non-empty ``PATH`` entries (logged as one registry read of ``Path``).
    set_path
        Store *entries* as the expandable ``Path`` (logged as one registry write).
    broadcast_environment_change
        Count one broadcast instead of sending it.
    write_shortcut
        Record the shortcut as a ``.lnk.json`` file in place of a real ``.lnk``.
    read_shortcut
        Return the recorded fields; a missing record raises ``FileNotFoundError``.
    delete_shortcut
        Delete the record; an absent record is not an error.
    is_elevated
        Report the configured elevation flag.
    scratch_directory
        Create a private folder below the fake's own root (never the real temporary folder), yield it, delete it on exit.
    process_id
        Return the configured process id (the real one by default).
    process_is_running
        Report whether *pid* is this fake process or one of ``live_pids``; a sandbox asks the real system.
    directory_is_admin_only
        Report whether *path* lies outside every directory listed as untrusted.
    directory_untrusted_reason
        Explain why *path* is untrusted, or ``None`` when it is admin-only.
    scope_folders
        Return fake Start Menu and ``bin`` folders below the root.
    run
        Run a registered double or a real 7-Zip; any other program raises ``UnexpectedCall``.
    run_foreground
        Record the request and answer it with a registered double's status; any other program raises ``UnexpectedProcess``.
    open_path
        Record the request (nothing is opened); a path that does not exist raises ``FileNotFoundError`` like the real call.
    regex_worker
        Run the regex worker for real (it only computes), unless a ``regex-worker`` double is registered.
    fetch
        Write the fixture for *url* to *dest*, reporting progress in 64 KiB chunks.
    fetch_text
        Return the fixture for *url* decoded as UTF-8.
    http_request
        Answer from the fixture for *url* (a ``HEAD`` gets no body); no redirect is followed.
    http_download
        Write the fixture for *url* to *dest*, honouring ``Range`` when the fixture allows it.
    """

    root: Path
    elevated: bool = False
    urls: dict[str, Any] = field(default_factory=dict)
    processes: dict[str, Callable[[ProcessCall], ProcessResult]] = field(default_factory=dict)
    untrusted_dirs: set[Path] = field(default_factory=set)
    delegate: System | None = None
    real_7zip: bool = True
    state_file: Path | None = None
    pid: int = field(default_factory=os.getpid)
    live_pids: set[int] = field(default_factory=set)
    failures: dict[str, BaseException] = field(default_factory=dict)
    registry_log: list[RegistryAccess] = field(default_factory=list)
    broadcasts: int = 0
    fetch_log: list[str] = field(default_factory=list)
    request_log: list[RecordedRequest] = field(default_factory=list)
    process_log: list[list[str]] = field(default_factory=list)
    foreground_log: list[list[str]] = field(default_factory=list)
    opened: list[Path] = field(default_factory=list)
    regex_log: list[str] = field(default_factory=list)
    real_programs: dict[str, Path] = field(default_factory=dict)
    _env: dict[str, dict[str, tuple[str, EnvValue]]] = field(
        default_factory=lambda: {"user": {}, "system": {}}
    )

    def __post_init__(self) -> None:
        """Create the root, normalise the untrusted set and load any saved state."""
        self.root = Path(self.root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        self.untrusted_dirs = {Path(os.path.abspath(path)) for path in self.untrusted_dirs}
        if self.state_file is not None and Path(self.state_file).is_file():
            self._load()

    @classmethod
    def sandbox(cls, directory: Path, delegate: System) -> "FakeSystem":
        """Open (or start) the sandbox kept in *directory*, with network and processes real.

        State persists in ``directory/system.json``; machine integration
        (environment, shortcuts, elevation) stays fake.

        Parameters
        ----------
        directory : Path
            Folder that holds the sandbox state and files.
        delegate : System
            Real system that serves network and process calls with no fixture.

        Returns
        -------
        FakeSystem
            The sandbox system, with ``git`` allowed to run for real.
        """
        directory = Path(directory)
        sandbox = cls(directory, delegate=delegate, state_file=directory / "system.json")
        # Processes stay on an identity allow-list: git (a package may clone its
        # origin) and, through ``real_7zip``, the bundled 7-Zip. Any other command
        # raises ``UnexpectedCall``.
        sandbox.allow_real("git")
        return sandbox

    # -- inspection helpers for tests ---------------------------------------

    def env_values(self, scope: Mode) -> dict[str, EnvValue]:
        """Return the stored environment values of *scope* by name, without logging an access.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.

        Returns
        -------
        dict[str, EnvValue]
            Stored values keyed by their original name.
        """
        return {name: value for name, value in self._env[scope.value].values()}

    def shortcuts(self) -> dict[Path, ShortcutSpec]:
        """Return every recorded shortcut by the path it was written to.

        Returns
        -------
        dict[Path, ShortcutSpec]
            Recorded shortcuts keyed by the path they were written to.
        """
        found: dict[Path, ShortcutSpec] = {}
        for record in sorted(self.root.rglob("*.lnk.json")):
            data = json.loads(record.read_text(encoding="utf-8"))
            found[Path(data["path"])] = ShortcutSpec(**data["spec"])
        return found

    def add_url(self, url: str, content: Any) -> None:
        """Serve *content* (see the ``urls`` parameter) at *url*.

        Parameters
        ----------
        url : str
            Address to serve.
        content : Any
            Fixture served at *url*.
        """
        self.urls[url] = content

    def allow_real(self, *programs: str) -> None:
        """Let the named programs run for real, for example ``git`` against a local fixture repository.

        Each name is resolved once, on ``PATH`` and never in the working
        directory, to one executable file. Afterwards only that identity runs: a
        bare name, or exactly that path. A name that is not found is not allowed.

        Parameters
        ----------
        programs : str
            Program names to resolve and allow.
        """
        for program in programs:
            found = _find_on_path(program)
            if found is not None:
                self.real_programs[program.lower()] = found

    # -- links: real links below the temporary root (inherited) --------------

    def create_junction(self, link: Path, target: Path) -> None:
        """Create a real junction; see :meth:`WindowsLinks.create_junction`.

        Parameters
        ----------
        link : Path
            Junction to create or replace.
        target : Path
            Existing directory it points to.
        """
        self._maybe_fail("create_junction")
        super().create_junction(link, target)

    # -- environment ---------------------------------------------------------

    def get_env(self, scope: Mode, name: str) -> EnvValue | None:
        """Return one stored value (logged as a registry read).

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.
        name : str
            Variable name (case-insensitive).

        Returns
        -------
        EnvValue or None
            The stored value, or ``None``.
        """
        self._maybe_fail("get_env")
        self._log("read", scope, name)
        entry = self._env[self._scope_key(scope)].get(name.casefold())
        return entry[1] if entry else None

    def set_env(self, scope: Mode, name: str, value: str, expandable: bool) -> None:
        """Store one value (logged as a registry write).

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.
        name : str
            Variable name.
        value : str
            Value to store.
        expandable : bool
            Whether the value expands ``%VAR%`` references.
        """
        self._maybe_fail("set_env")
        self._log("write", scope, name, value)
        self._env[self._scope_key(scope)][name.casefold()] = (name, EnvValue(value, expandable))
        self._save()

    def delete_env(self, scope: Mode, name: str) -> None:
        """Remove one value (logged as a registry delete).

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.
        name : str
            Variable name.
        """
        self._maybe_fail("delete_env")
        self._log("delete", scope, name)
        self._env[self._scope_key(scope)].pop(name.casefold(), None)
        self._save()

    def get_path(self, scope: Mode) -> list[str]:
        """Return the non-empty ``PATH`` entries (logged as one registry read of ``Path``).

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.

        Returns
        -------
        list[str]
            The stored ``PATH`` entries.
        """
        stored = self.get_env(scope, "Path")
        if stored is None:
            return []
        return [item.strip() for item in stored.value.split(";") if item.strip()]

    def set_path(self, scope: Mode, entries: Sequence[str]) -> None:
        """Store *entries* as the expandable ``Path`` (logged as one registry write).

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.
        entries : Sequence[str]
            Folders to store, in order.
        """
        self._maybe_fail("set_path")
        self.set_env(scope, "Path", ";".join(entries), True)

    def broadcast_environment_change(self) -> None:
        """Count one broadcast instead of sending it."""
        self._maybe_fail("broadcast_environment_change")
        self.broadcasts += 1
        self._save()

    # -- shortcuts -----------------------------------------------------------

    def write_shortcut(self, path: Path, spec: ShortcutSpec) -> None:
        """Record the shortcut as a ``.lnk.json`` file in place of a real ``.lnk``.

        Parameters
        ----------
        path : Path
            Location the shortcut stands for.
        spec : ShortcutSpec
            Fields to record.
        """
        self._maybe_fail("write_shortcut")
        record = self._shortcut_record(Path(path))
        record.parent.mkdir(parents=True, exist_ok=True)
        payload = {"path": str(Path(path)), "spec": asdict(spec)}
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def read_shortcut(self, path: Path) -> ShortcutSpec:
        """Return the recorded fields; a missing record raises ``FileNotFoundError``.

        Parameters
        ----------
        path : Path
            Location the shortcut stands for.

        Returns
        -------
        ShortcutSpec
            The recorded fields.

        Raises
        ------
        FileNotFoundError
            When no record exists for *path*.
        """
        record = self._shortcut_record(Path(path))
        if not record.is_file():
            raise FileNotFoundError(f"No shortcut recorded at {path}")
        return ShortcutSpec(**json.loads(record.read_text(encoding="utf-8"))["spec"])

    def delete_shortcut(self, path: Path) -> None:
        """Delete the record; an absent record is not an error.

        Parameters
        ----------
        path : Path
            Location the shortcut stands for.
        """
        self._shortcut_record(Path(path)).unlink(missing_ok=True)

    # -- privilege and locations ----------------------------------------------

    def is_elevated(self) -> bool:
        """Report the configured elevation flag.

        Returns
        -------
        bool
            The configured elevation flag.
        """
        return self.elevated

    @contextmanager
    def scratch_directory(self, prefix: str) -> Iterator[Path]:
        """Create a private folder below the fake's own root (never the real temporary folder), yield it, delete it on exit.

        Parameters
        ----------
        prefix : str
            Prefix of the folder name.
        """
        self._maybe_fail("scratch_directory")
        folder = self.root / "scratch" / f"{prefix}{uuid.uuid4().hex[:12]}"
        folder.mkdir(parents=True)
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def process_id(self) -> int:
        """Return the configured process id (the real one by default).

        Returns
        -------
        int
            The configured process id.
        """
        return self.pid

    def process_is_running(self, pid: int) -> bool:
        """Report whether *pid* is this fake process or one of ``live_pids``; a sandbox asks the real system.

        Parameters
        ----------
        pid : int
            Process id to look up.

        Returns
        -------
        bool
            True when *pid* is known to run.
        """
        if pid == self.pid or pid in self.live_pids:
            return True
        return self.delegate.process_is_running(pid) if self.delegate is not None else False

    def directory_is_admin_only(self, path: Path) -> bool:
        """Report whether *path* lies outside every directory listed as untrusted.

        Parameters
        ----------
        path : Path
            Directory to judge.

        Returns
        -------
        bool
            True when *path* is not under an untrusted directory.
        """
        return self.directory_untrusted_reason(path) is None

    def directory_untrusted_reason(self, path: Path) -> str | None:
        """Explain why *path* is untrusted, or ``None`` when it is admin-only.

        Parameters
        ----------
        path : Path
            Directory to judge.

        Returns
        -------
        str or None
            The reason, or ``None`` when admin-only.
        """
        candidate = Path(os.path.abspath(path))
        for untrusted in self.untrusted_dirs:
            if candidate == untrusted or untrusted in candidate.parents:
                return f"Users can modify it (fake: {untrusted} is untrusted)"
        return None

    def scope_folders(self, scope: Mode) -> ScopeFolders:
        """Return fake Start Menu and ``bin`` folders below the root.

        Parameters
        ----------
        scope : Mode
            Whether the user or the machine store is used.

        Returns
        -------
        ScopeFolders
            Fake shortcut root and ``bin`` folder.
        """
        key = self._scope_key(scope)
        return ScopeFolders(self.root / "StartMenu" / key / "opt", self.root / "bin" / key)

    # -- processes -------------------------------------------------------------

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
        """Run a registered double or a real 7-Zip; any other program raises ``UnexpectedCall``.

        Parameters
        ----------
        argv : Sequence[str]
            Program and arguments.
        cwd : Path or None
            Working directory handed to the double.
        env : Mapping[str, str] or None
            Environment handed to the double.
        on_line : Callable or None
            Receiver of each output line.
        input_text : str or None
            Text handed to the double as standard input.
        timeout : float or None
            Passed on when the program runs for real.
        watch : Callable or None
            Abort check, asked once after a double finishes.

        Returns
        -------
        ProcessResult
            The double's or the real program's result.

        Raises
        ------
        ProcessAborted
            When *watch* returns text.
        UnexpectedProcess
            When the program has no double and is not an allowed real identity.
        """
        self._maybe_fail("run")
        command = [str(part) for part in argv]
        self.process_log.append(command)
        program = Path(command[0])

        # A registered double wins; match the full file name before the stem.
        double = self.processes.get(program.name.lower()) or self.processes.get(program.stem.lower())
        if double is not None:
            result = double(ProcessCall(tuple(command), cwd, env, input_text))
            # A double finishes at once, so its watcher is asked once, after it has written.
            reason = watch() if watch is not None else None
            if reason is not None:
                raise ProcessAborted(reason)
            if on_line is not None:
                for line in (result.stdout + result.stderr).splitlines():
                    on_line(line)
            return result

        # Only an allowed identity (git on PATH, the bundled 7-Zip) runs for real,
        # and it runs from its resolved path, so nothing named like it can stand in.
        target = self._real_target(command[0])
        if target is not None:
            return (self.delegate or WindowsSystem()).run(
                [str(target), *command[1:]], cwd, env, on_line, input_text=input_text, timeout=timeout, watch=watch
            )
        raise UnexpectedProcess(f"FakeSystem has no process double for {command[0]!r}: {command}")

    def run_foreground(self, argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        """Record the request and answer it with a registered double's status; any other program raises ``UnexpectedProcess``.

        Parameters
        ----------
        argv : Sequence[str]
            Program and arguments.
        cwd : Path or None
            Working directory handed to the double.
        env : Mapping[str, str] or None
            Environment handed to the double.

        Returns
        -------
        int
            The exit status the double returns.

        Raises
        ------
        UnexpectedProcess
            When the program has no registered double.
        """
        self._maybe_fail("run_foreground")
        command = [str(part) for part in argv]
        self.foreground_log.append(command)
        program = Path(command[0])
        double = self.processes.get(program.name.lower()) or self.processes.get(program.stem.lower())
        if double is None:
            raise UnexpectedProcess(f"FakeSystem has no process double for {command[0]!r}: {command}")
        return double(ProcessCall(tuple(command), cwd, env, None)).returncode

    def open_path(self, path: Path) -> None:
        """Record the request (nothing is opened); a path that does not exist raises ``FileNotFoundError`` like the real call.

        Parameters
        ----------
        path : Path
            File or folder that would be opened.

        Raises
        ------
        FileNotFoundError
            When *path* does not exist.
        """
        self._maybe_fail("open_path")
        if not Path(path).exists():
            raise FileNotFoundError(f"{path} does not exist")
        self.opened.append(Path(path))

    def regex_worker(
        self,
        request: str,
        timeout: float,
        *,
        on_line: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Run the regex worker for real (it only computes), unless a ``regex-worker`` double is registered.

        A double receives the request as ``input_text`` and returns the :class:`ProcessResult`
        to use, so a test can simulate a crashed, garbled or oversized worker; a double that
        raises ``TimeoutError`` simulates a deadline. Every request is kept in ``regex_log``.

        Parameters
        ----------
        request : str
            Request text for the worker.
        timeout : float
            Seconds the real worker may run.
        on_line : Callable or None
            Receiver of each worker output line.

        Returns
        -------
        ProcessResult
            The double's or the real worker's result.
        """
        self._maybe_fail("regex_worker")
        self.regex_log.append(request)
        double = self.processes.get("regex-worker")
        if double is not None:
            return double(ProcessCall(("regex-worker",), None, None, request))
        return WindowsSystem().regex_worker(request, timeout, on_line=on_line)

    # -- network -----------------------------------------------------------------

    def fetch(
        self,
        url: str,
        headers: Mapping[str, str] | None,
        dest: Path,
        on_progress: Callable[[int, int | None], None] | None = None,
        *,
        timeout: float = 60,
    ) -> int:
        """Write the fixture for *url* to *dest*, reporting progress in 64 KiB chunks.

        Parameters
        ----------
        url : str
            Address to download.
        headers : Mapping[str, str] or None
            Request headers passed to a function fixture.
        dest : Path
            File to write.
        on_progress : Callable or None
            Progress receiver.
        timeout : float
            Passed to the delegate when one serves the URL.

        Returns
        -------
        int
            Number of bytes written.
        """
        self._maybe_fail("fetch")

        # A URL with no fixture goes to the delegate when there is one.
        if url not in self.urls:
            if self.delegate is not None:
                self.fetch_log.append(url)
                return self.delegate.fetch(url, headers, dest, on_progress, timeout=timeout)
        # Announce the size, then write the body in chunks, reporting progress after each.
        body, report_size = self._served(url, headers)
        total = len(body) if report_size else None
        if on_progress is not None:
            on_progress(0, total)
        received = 0
        # Write the body to the destination one 64 KiB chunk at a time.
        with open(dest, "wb") as handle:
            for start in range(0, len(body), 64 * 1024):
                chunk = body[start : start + 64 * 1024]
                handle.write(chunk)
                received += len(chunk)
                if on_progress is not None:
                    on_progress(received, total)
        return received

    def fetch_text(
        self, url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 60
    ) -> str:
        """Return the fixture for *url* decoded as UTF-8.

        Parameters
        ----------
        url : str
            Address to read.
        headers : Mapping[str, str] or None
            Request headers passed to a function fixture.
        timeout : float
            Passed to the delegate when one serves the URL.

        Returns
        -------
        str
            The decoded fixture body.
        """
        self._maybe_fail("fetch_text")
        if url not in self.urls and self.delegate is not None:
            self.fetch_log.append(url)
            return self.delegate.fetch_text(url, headers, timeout=timeout)
        return self._served(url, headers)[0].decode("utf-8")

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
        """Answer from the fixture for *url* (a ``HEAD`` gets no body); no redirect is followed.

        Parameters
        ----------
        url : str
            Address to request.
        headers : Mapping[str, str] or None
            Request headers.
        method : str
            HTTP method; ``HEAD`` gets no body.
        timeout : float
            Recorded in the request log.
        max_bytes : int or None
            Largest body to return.
        total_timeout : float or None
            Recorded in the request log.

        Returns
        -------
        HttpResponse
            Status, lower-case headers and body of the fixture.

        Raises
        ------
        ResponseTooLarge
            When the body is longer than *max_bytes*.
        """
        self._maybe_fail("http_request")
        self.request_log.append(RecordedRequest(method, url, dict(headers or {}), timeout, max_bytes, total_timeout))
        if url not in self.urls and self.delegate is not None:
            self.fetch_log.append(url)
            return self.delegate.http_request(
                url, headers, method=method, timeout=timeout, max_bytes=max_bytes, total_timeout=total_timeout
            )
        # Serve the fixture, enforcing the body limit the way the real client does.
        fixture = self._http_fixture(url, headers)
        body = fixture.body if isinstance(fixture.body, bytes) else fixture.body.encode("utf-8")
        if max_bytes is not None and len(body) > max_bytes and method != "HEAD" and max_bytes > 0:
            raise ResponseTooLarge(f"The response is larger than {max_bytes} bytes")
        # Build the answer with lower-case headers; a HEAD or zero limit returns no body.
        answer_headers = {name.lower(): value for name, value in fixture.headers.items()}
        answer_headers.setdefault("content-length", str(len(body)))
        return HttpResponse(fixture.status, url, answer_headers, b"" if method == "HEAD" or max_bytes == 0 else body)

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
        """Write the fixture for *url* to *dest*, honouring ``Range`` when the fixture allows it.

        Parameters
        ----------
        url : str
            Address to download.
        headers : Mapping[str, str] or None
            Request headers, including any ``Range``.
        dest : Path
            File to write or append to.
        on_progress : Callable or None
            Progress receiver.
        timeout : float
            Recorded in the request log.
        append : bool
            Whether a satisfiable range is appended to *dest*.
        on_headers : Callable or None
            Receiver of the status and headers.
        total_timeout : float or None
            Recorded in the request log.

        Returns
        -------
        DownloadResponse
            Status, headers and how the file was written.

        Raises
        ------
        ConnectionResetError
            When the fixture's ``fail_after`` cuts the body short.
        """
        # Record the request, then let the delegate serve a URL that has no fixture.
        self._maybe_fail("http_download")
        self.request_log.append(RecordedRequest("GET", url, dict(headers or {}), timeout, None, total_timeout))
        if url not in self.urls and self.delegate is not None:
            self.fetch_log.append(url)
            return self.delegate.http_download(
                url, headers, dest, on_progress, timeout=timeout, append=append, on_headers=on_headers,
                total_timeout=total_timeout,
            )
        # Report the status and headers first; a non-2xx answer writes nothing.
        fixture = self._http_fixture(url, headers)
        body = fixture.body if isinstance(fixture.body, bytes) else fixture.body.encode("utf-8")
        answer_headers = {name.lower(): value for name, value in fixture.headers.items()}
        if on_headers is not None:
            on_headers(fixture.status, answer_headers)
        if not 200 <= fixture.status < 300:
            return DownloadResponse(fixture.status, url, answer_headers)

        # A range request is honoured only by a fixture that says it is resumable.
        status, resumed, payload = fixture.status, False, body
        wanted = {name.lower(): value for name, value in (headers or {}).items()}.get("range", "")
        if append and fixture.ranges and wanted.startswith("bytes=") and wanted.endswith("-"):
            start = int(wanted[6:-1])
            if start >= len(body):
                return DownloadResponse(416, url, answer_headers)
            status, resumed, payload = 206, True, body[start:]
        # Report the resumed bytes as already received before the new bytes arrive.
        total = len(body) if fixture.report_size else None
        already = len(body) - len(payload)
        if on_progress is not None:
            on_progress(already, total)
        # Write the payload in chunks, stopping early when the fixture simulates a dropped connection.
        cut = payload if fixture.fail_after is None else payload[: fixture.fail_after]
        with open(dest, "ab" if resumed else "wb") as handle:
            for begin in range(0, len(cut), 64 * 1024):
                chunk = cut[begin : begin + 64 * 1024]
                handle.write(chunk)
                if on_progress is not None:
                    on_progress(already + begin + len(chunk), total)
        if len(cut) < len(payload):
            raise ConnectionResetError(f"FakeSystem dropped the connection to {url}")
        return DownloadResponse(status, url, answer_headers, len(payload), resumed)

    # -- internals -----------------------------------------------------------------

    def _http_fixture(self, url: str, headers: Mapping[str, str] | None) -> FakeHttp:
        """Resolve a URL fixture to a :class:`FakeHttp` for the ``http_*`` methods.

        Raises
        ------
        UnexpectedCall
            When no fixture exists for *url*; a fixture that is an exception is raised.
        """
        # Look the fixture up (calling it when it is a function) and normalise every kind to FakeHttp.
        self.fetch_log.append(url)
        if url not in self.urls:
            raise UnexpectedCall(f"FakeSystem has no fixture for URL {url}")
        content = self.urls[url]
        if callable(content):
            content = content(url, dict(headers or {}))
        if isinstance(content, BaseException):
            raise content
        # Each accepted fixture kind maps to a FakeHttp; plain bodies use the defaults.
        if isinstance(content, FakeHttp):
            return content
        if isinstance(content, FakeResponse):
            return FakeHttp(content.body, report_size=content.report_size)
        if isinstance(content, str):
            return FakeHttp(content)
        return FakeHttp(bytes(content))

    def _served(self, url: str, headers: Mapping[str, str] | None) -> tuple[bytes, bool]:
        """Resolve a URL fixture to ``(body, report_size)`` for ``fetch`` and ``fetch_text``.

        Raises
        ------
        UnexpectedCall
            When no fixture exists for *url*; a fixture that is an exception is raised.
        """
        # Look the fixture up (calling it when it is a function) and reduce it to bytes plus the size flag.
        self.fetch_log.append(url)
        if url not in self.urls:
            raise UnexpectedCall(f"FakeSystem has no fixture for URL {url}")
        content = self.urls[url]
        if callable(content):
            content = content(url, dict(headers or {}))
        if isinstance(content, BaseException):
            raise content
        # Each accepted fixture kind maps to bytes; plain bodies always announce their size.
        if isinstance(content, FakeResponse):
            return content.body, content.report_size
        if isinstance(content, str):
            return content.encode("utf-8"), True
        return bytes(content), True

    def _real_target(self, program: str) -> Path | None:
        """Return the executable that *program* may run as, or ``None`` when it is not an allowed identity."""
        # Only executables: a script or any other suffix is never "the real git".
        if Path(program).suffix.lower() not in ("", ".exe"):
            return None

        # A bare name means the program resolved when it was allowed.
        if not any(separator in program for separator in "\\/"):
            return self.real_programs.get(Path(program).stem.lower())

        # A path must be exactly an allowed executable.
        allowed = list(self.real_programs.values())
        if self.real_7zip:
            try:
                allowed.append(Path(os.path.realpath(find_7z())))
            except FileNotFoundError:
                pass
        given = os.path.normcase(os.path.realpath(program))
        return next((path for path in allowed if os.path.normcase(str(path)) == given), None)

    def _maybe_fail(self, method: str) -> None:
        """Raise (once) the exception configured for *method*.

        Raises
        ------
        BaseException
            The exception stored in ``failures`` for *method*.
        """
        failure = self.failures.pop(method, None)
        if failure is not None:
            raise failure

    @staticmethod
    def _scope_key(scope: Mode) -> str:
        """Return ``"user"`` or ``"system"``; ``auto`` has no environment of its own.

        Raises
        ------
        ValueError
            When *scope* is neither user nor system.
        """
        if scope not in (Mode.USER, Mode.SYSTEM):
            raise ValueError(f"Environment scope must be user or system, not {scope.value}")
        return scope.value

    def _log(self, operation: str, scope: Mode, name: str, value: str | None = None) -> None:
        """Append one registry access to the ordered log."""
        self.registry_log.append(RegistryAccess(operation, self._scope_key(scope), name, value))

    def _shortcut_record(self, path: Path) -> Path:
        """Return the ``.lnk.json`` record file for a shortcut path, always below the root."""
        absolute = Path(os.path.abspath(path))
        if absolute == self.root or self.root in absolute.parents:
            return absolute.with_name(absolute.name + ".json")
        flattened = str(absolute).replace(":", "").replace("\\", "_").replace("/", "_")
        return self.root / "shortcuts-outside" / (flattened + ".json")

    def _save(self) -> None:
        """Persist registry state when a state file is configured."""
        if self.state_file is None:
            return
        # Serialise the registry state, its log and the counters in one JSON document.
        state = {
            "elevated": self.elevated,
            "env": {
                scope: {name: asdict(value) for name, value in entries.values()}
                for scope, entries in self._env.items()
            },
            "registry_log": [asdict(item) for item in self.registry_log],
            "broadcasts": self.broadcasts,
        }
        Path(self.state_file).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def _load(self) -> None:
        """Restore state written by an earlier run."""
        state = json.loads(Path(self.state_file).read_text(encoding="utf-8"))
        self.elevated = bool(state.get("elevated", self.elevated))
        for scope, entries in state.get("env", {}).items():
            self._env[scope] = {
                name.casefold(): (name, EnvValue(**value)) for name, value in entries.items()
            }
        self.registry_log = [RegistryAccess(**item) for item in state.get("registry_log", [])]
        self.broadcasts = int(state.get("broadcasts", 0))


# --- supporting helpers ---


def _find_on_path(name: str) -> Path | None:
    """Return the executable *name* on ``PATH`` (never the working directory), fully resolved."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = entry.strip().strip('"')
        for candidate in (name + ".exe", name):
            path = Path(folder) / candidate if folder else None
            if path is not None and path.is_file():
                return Path(os.path.realpath(path))
    return None
