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
``broadcasts`` and ``relaunches`` afterwards. ``FakeSystem.sandbox(directory,
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
so a behavior test can assert that an install read nothing but ``PATH`` (D18,
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
    ElevatedRun,
    EnvValue,
    HttpResponse,
    ProcessAborted,
    ProcessNotAllowed,
    ProcessResult,
    ResponseTooLarge,
    Scope,
    ScopeFolders,
    ShortcutSpec,
    System,
    MAX_RELAUNCH_PARAMETERS,
    elevated_command,
    unsafe_relaunch_argument,
)
from ..selfmgmt.elevation import bundled_runtime_paths
from .windows import WindowsLinks, WindowsSystem


def _find_on_path(name: str) -> Path | None:
    """Return the executable *name* on ``PATH`` (never the working directory), fully resolved."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = entry.strip().strip('"')
        for candidate in (name + ".exe", name):
            path = Path(folder) / candidate if folder else None
            if path is not None and path.is_file():
                return Path(os.path.realpath(path))
    return None


class UnexpectedCall(OSError):
    """Report a URL, process or action that the fake was not set up to serve."""


class UnexpectedProcess(UnexpectedCall, ProcessNotAllowed):
    """Report a program that the fake has no double for, so the engine can say the package needs a real run."""


@dataclass(frozen=True)
class RegistryAccess:
    """Record one registry read, write or delete made through a ``FakeSystem``."""

    operation: str
    scope: str
    name: str
    value: str | None = None


@dataclass(frozen=True)
class FakeResponse:
    """Serve a URL fixture whose size the server may or may not announce."""

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
    """

    body: bytes | str = b""
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    ranges: bool = False
    fail_after: int | None = None
    report_size: bool = True


@dataclass(frozen=True)
class RecordedRequest:
    """Record one ``http_request`` or ``http_download`` call made to a ``FakeSystem``."""

    method: str
    url: str
    headers: Mapping[str, str]
    timeout: float
    max_bytes: int | None = None
    total_timeout: float | None = None


@dataclass(frozen=True)
class ProcessCall:
    """Describe one child-process request handed to a process double."""

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
    relaunch_result : bool
        Whether ``relaunch_elevated`` reports that the child started (False: the elevation prompt was declined).
    relaunch_exit_code : int, optional
        The exit code of the pretend child; ``None`` means the wait ended without one.
    relaunch_error : str, optional
        Pretend waiting for the started child failed with this message.
    relaunch_interrupt : bool
        Pretend the user pressed Ctrl-C while waiting: ``on_interrupt`` is called and the wait is abandoned.
    package_dir : Path
        The package folder the pretend elevated command is built from (``relaunch_commands``).
    pid : int
        What ``process_id`` reports.
    live_pids : set of int
        Other process ids that ``process_is_running`` reports as running (a lock held by a living second run).
    failures : dict, optional
        Method name to an exception that the next call of that method raises
        (once), to exercise failure paths.
    """

    root: Path
    elevated: bool = False
    urls: dict[str, Any] = field(default_factory=dict)
    processes: dict[str, Callable[[ProcessCall], ProcessResult]] = field(default_factory=dict)
    untrusted_dirs: set[Path] = field(default_factory=set)
    delegate: System | None = None
    real_7zip: bool = True
    state_file: Path | None = None
    relaunch_result: bool = True
    relaunch_exit_code: int | None = 0
    relaunch_interrupt: bool = False
    relaunch_error: str | None = None
    package_dir: Path = Path("C:/Program Files/gupkg/1.0.0/gupkg")
    pid: int = field(default_factory=os.getpid)
    live_pids: set[int] = field(default_factory=set)
    failures: dict[str, BaseException] = field(default_factory=dict)
    registry_log: list[RegistryAccess] = field(default_factory=list)
    broadcasts: int = 0
    relaunches: list[list[str]] = field(default_factory=list)
    relaunch_commands: list[tuple[str, str, str]] = field(default_factory=list)
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
        """
        directory = Path(directory)
        sandbox = cls(directory, delegate=delegate, state_file=directory / "system.json")
        # Processes stay on an identity allow-list: git (a package may clone its
        # origin) and, through ``real_7zip``, the bundled 7-Zip. Any other command
        # raises ``UnexpectedCall``.
        sandbox.allow_real("git")
        return sandbox

    # -- inspection helpers for tests ---------------------------------------

    def env_values(self, scope: Scope) -> dict[str, EnvValue]:
        """Return the stored environment values of *scope* by name, without logging an access."""
        return {name: value for name, value in self._env[scope.value].values()}

    def shortcuts(self) -> dict[Path, ShortcutSpec]:
        """Return every recorded shortcut by the path it was written to."""
        found: dict[Path, ShortcutSpec] = {}
        for record in sorted(self.root.rglob("*.lnk.json")):
            data = json.loads(record.read_text(encoding="utf-8"))
            found[Path(data["path"])] = ShortcutSpec(**data["spec"])
        return found

    def add_url(self, url: str, content: Any) -> None:
        """Serve *content* (see the ``urls`` parameter) at *url*."""
        self.urls[url] = content

    def allow_real(self, *programs: str) -> None:
        """Let the named programs run for real, for example ``git`` against a local fixture repository.

        Each name is resolved once, on ``PATH`` and never in the working
        directory, to one executable file. Afterwards only that identity runs: a
        bare name, or exactly that path. A name that is not found is not allowed.
        """
        for program in programs:
            found = _find_on_path(program)
            if found is not None:
                self.real_programs[program.lower()] = found

    # -- links: real links below the temporary root (inherited) --------------

    def create_junction(self, link: Path, target: Path) -> None:
        """Create a real junction; see :meth:`WindowsLinks.create_junction`."""
        self._maybe_fail("create_junction")
        super().create_junction(link, target)

    # -- environment ---------------------------------------------------------

    def get_env(self, scope: Scope, name: str) -> EnvValue | None:
        """Return one stored value (logged as a registry read)."""
        self._maybe_fail("get_env")
        self._log("read", scope, name)
        entry = self._env[self._scope_key(scope)].get(name.casefold())
        return entry[1] if entry else None

    def set_env(self, scope: Scope, name: str, value: str, expandable: bool) -> None:
        """Store one value (logged as a registry write)."""
        self._maybe_fail("set_env")
        self._log("write", scope, name, value)
        self._env[self._scope_key(scope)][name.casefold()] = (name, EnvValue(value, expandable))
        self._save()

    def delete_env(self, scope: Scope, name: str) -> None:
        """Remove one value (logged as a registry delete)."""
        self._maybe_fail("delete_env")
        self._log("delete", scope, name)
        self._env[self._scope_key(scope)].pop(name.casefold(), None)
        self._save()

    def get_path(self, scope: Scope) -> list[str]:
        """Return the non-empty ``PATH`` entries (logged as one registry read of ``Path``)."""
        stored = self.get_env(scope, "Path")
        if stored is None:
            return []
        return [item.strip() for item in stored.value.split(";") if item.strip()]

    def set_path(self, scope: Scope, entries: Sequence[str]) -> None:
        """Store *entries* as the expandable ``Path`` (logged as one registry write)."""
        self._maybe_fail("set_path")
        self.set_env(scope, "Path", ";".join(entries), True)

    def broadcast_environment_change(self) -> None:
        """Count one broadcast instead of sending it."""
        self._maybe_fail("broadcast_environment_change")
        self.broadcasts += 1
        self._save()

    # -- shortcuts -----------------------------------------------------------

    def write_shortcut(self, path: Path, spec: ShortcutSpec) -> None:
        """Record the shortcut as a ``.lnk.json`` file in place of a real ``.lnk``."""
        self._maybe_fail("write_shortcut")
        record = self._shortcut_record(Path(path))
        record.parent.mkdir(parents=True, exist_ok=True)
        payload = {"path": str(Path(path)), "spec": asdict(spec)}
        record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def read_shortcut(self, path: Path) -> ShortcutSpec:
        """Return the recorded fields; a missing record raises ``FileNotFoundError``."""
        record = self._shortcut_record(Path(path))
        if not record.is_file():
            raise FileNotFoundError(f"No shortcut recorded at {path}")
        return ShortcutSpec(**json.loads(record.read_text(encoding="utf-8"))["spec"])

    def delete_shortcut(self, path: Path) -> None:
        """Delete the record; an absent record is not an error."""
        self._shortcut_record(Path(path)).unlink(missing_ok=True)

    # -- privilege and locations ----------------------------------------------

    def is_elevated(self) -> bool:
        """Report the configured elevation flag."""
        return self.elevated

    @contextmanager
    def scratch_directory(self, prefix: str) -> Iterator[Path]:
        """Create a private folder below the fake's own root (never the real temporary folder), yield it, delete it on exit."""
        self._maybe_fail("scratch_directory")
        folder = self.root / "scratch" / f"{prefix}{uuid.uuid4().hex[:12]}"
        folder.mkdir(parents=True)
        try:
            yield folder
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def process_id(self) -> int:
        """Return the configured process id (the real one by default)."""
        return self.pid

    def process_is_running(self, pid: int) -> bool:
        """Report whether *pid* is this fake process or one of ``live_pids``; a sandbox asks the real system."""
        if pid == self.pid or pid in self.live_pids:
            return True
        return self.delegate.process_is_running(pid) if self.delegate is not None else False

    def relaunch_elevated(
        self, argv: Sequence[str], *, on_interrupt: Callable[[], None] | None = None, grace: float = 30.0,
    ) -> ElevatedRun:
        """Record the request and report the configured outcome; nothing is started.

        Like the real one, it refuses a value that could mean something to a shell (nothing is recorded), and records the
        program, parameters and folder the real call would pass to ``ShellExecuteEx`` in ``relaunch_commands``.
        """
        self._maybe_fail("relaunch_elevated")
        if any(unsafe_relaunch_argument(part) for part in argv):
            return ElevatedRun(False)
        _, parameters, _ = elevated_command(self.package_dir, argv)
        if len(parameters) > MAX_RELAUNCH_PARAMETERS:
            return ElevatedRun(False, refused=f"the command line is {len(parameters)} characters long; more than {MAX_RELAUNCH_PARAMETERS} cannot be started")
        for path in bundled_runtime_paths(self.package_dir):
            reason = self.directory_untrusted_reason(path) if path.exists() else None
            if reason is not None:
                return ElevatedRun(False, refused=f"{path}: {reason}")
        self.relaunches.append([str(part) for part in argv])
        program, parameters, directory = elevated_command(self.package_dir, argv)
        self.relaunch_commands.append((str(program), parameters, str(directory)))
        self._save()
        if not self.relaunch_result:
            return ElevatedRun(False)
        if self.relaunch_error is not None:
            return ElevatedRun(True, error=self.relaunch_error)
        if self.relaunch_interrupt:
            if on_interrupt is not None:
                on_interrupt()
            return ElevatedRun(True, None, True)
        return ElevatedRun(True, self.relaunch_exit_code)

    def directory_is_admin_only(self, path: Path) -> bool:
        """Report whether *path* lies outside every directory listed as untrusted."""
        return self.directory_untrusted_reason(path) is None

    def directory_untrusted_reason(self, path: Path) -> str | None:
        """Explain why *path* is untrusted, or ``None`` when it is admin-only."""
        candidate = Path(os.path.abspath(path))
        for untrusted in self.untrusted_dirs:
            if candidate == untrusted or untrusted in candidate.parents:
                return f"Users can modify it (fake: {untrusted} is untrusted)"
        return None

    def scope_folders(self, scope: Scope) -> ScopeFolders:
        """Return fake Start Menu and ``bin`` folders below the root."""
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
        """Run a registered double or a real 7-Zip; any other program raises ``UnexpectedCall``."""
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
        """Record the request and answer it with a registered double's status; any other program raises ``UnexpectedProcess``."""
        self._maybe_fail("run_foreground")
        command = [str(part) for part in argv]
        self.foreground_log.append(command)
        program = Path(command[0])
        double = self.processes.get(program.name.lower()) or self.processes.get(program.stem.lower())
        if double is None:
            raise UnexpectedProcess(f"FakeSystem has no process double for {command[0]!r}: {command}")
        return double(ProcessCall(tuple(command), cwd, env, None)).returncode

    def open_path(self, path: Path) -> None:
        """Record the request (nothing is opened); a path that does not exist raises ``FileNotFoundError`` like the real call."""
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
        """Write the fixture for *url* to *dest*, reporting progress in 64 KiB chunks."""
        self._maybe_fail("fetch")
        if url not in self.urls:
            if self.delegate is not None:
                self.fetch_log.append(url)
                return self.delegate.fetch(url, headers, dest, on_progress, timeout=timeout)
        body, report_size = self._served(url, headers)
        total = len(body) if report_size else None
        if on_progress is not None:
            on_progress(0, total)
        received = 0
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
        """Return the fixture for *url* decoded as UTF-8."""
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
        """Answer from the fixture for *url* (a ``HEAD`` gets no body); no redirect is followed."""
        self._maybe_fail("http_request")
        self.request_log.append(RecordedRequest(method, url, dict(headers or {}), timeout, max_bytes, total_timeout))
        if url not in self.urls and self.delegate is not None:
            self.fetch_log.append(url)
            return self.delegate.http_request(
                url, headers, method=method, timeout=timeout, max_bytes=max_bytes, total_timeout=total_timeout
            )
        fixture = self._http_fixture(url, headers)
        body = fixture.body if isinstance(fixture.body, bytes) else fixture.body.encode("utf-8")
        if max_bytes is not None and len(body) > max_bytes and method != "HEAD" and max_bytes > 0:
            raise ResponseTooLarge(f"The response is larger than {max_bytes} bytes")
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
        """Write the fixture for *url* to *dest*, honouring ``Range`` when the fixture allows it."""
        self._maybe_fail("http_download")
        self.request_log.append(RecordedRequest("GET", url, dict(headers or {}), timeout, None, total_timeout))
        if url not in self.urls and self.delegate is not None:
            self.fetch_log.append(url)
            return self.delegate.http_download(
                url, headers, dest, on_progress, timeout=timeout, append=append, on_headers=on_headers,
                total_timeout=total_timeout,
            )
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
        total = len(body) if fixture.report_size else None
        already = len(body) - len(payload)
        if on_progress is not None:
            on_progress(already, total)
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
        """Resolve a URL fixture to a :class:`FakeHttp` or raise its error."""
        self.fetch_log.append(url)
        if url not in self.urls:
            raise UnexpectedCall(f"FakeSystem has no fixture for URL {url}")
        content = self.urls[url]
        if callable(content):
            content = content(url, dict(headers or {}))
        if isinstance(content, BaseException):
            raise content
        if isinstance(content, FakeHttp):
            return content
        if isinstance(content, FakeResponse):
            return FakeHttp(content.body, report_size=content.report_size)
        if isinstance(content, str):
            return FakeHttp(content)
        return FakeHttp(bytes(content))

    def _served(self, url: str, headers: Mapping[str, str] | None) -> tuple[bytes, bool]:
        """Resolve a URL fixture to ``(body, report_size)`` or raise its error."""
        self.fetch_log.append(url)
        if url not in self.urls:
            raise UnexpectedCall(f"FakeSystem has no fixture for URL {url}")
        content = self.urls[url]
        if callable(content):
            content = content(url, dict(headers or {}))
        if isinstance(content, BaseException):
            raise content
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
        """Raise (once) the exception configured for *method*."""
        failure = self.failures.pop(method, None)
        if failure is not None:
            raise failure

    @staticmethod
    def _scope_key(scope: Scope) -> str:
        """Return ``"user"`` or ``"system"``; ``auto`` has no environment of its own."""
        if scope not in (Scope.USER, Scope.MACHINE):
            raise ValueError(f"Environment scope must be user or system, not {scope.value}")
        return scope.value

    def _log(self, operation: str, scope: Scope, name: str, value: str | None = None) -> None:
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
        state = {
            "elevated": self.elevated,
            "env": {
                scope: {name: asdict(value) for name, value in entries.values()}
                for scope, entries in self._env.items()
            },
            "registry_log": [asdict(item) for item in self.registry_log],
            "broadcasts": self.broadcasts,
            "relaunches": self.relaunches,
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
        self.relaunches = [list(item) for item in state.get("relaunches", [])]
