"""Implement the System boundary for a real Windows machine.

``WindowsSystem`` performs every side effect gupkg has: NTFS junctions through
``DeviceIoControl``, environment and ``PATH`` values in the registry, shortcuts
through the Shell COM API, the ``WM_SETTINGCHANGE`` broadcast, elevation checks
and requests, child processes, and downloads. No operation starts a child
process to do something Windows can do in process.

Usage and API
-------------
Create one ``WindowsSystem()`` at process start and pass it to the engine. Pass
``user_environment=(hive, subkey)`` or ``machine_environment=(hive, subkey)`` to
point the environment operations at another registry key; the contract tests
use a throwaway ``HKCU\\Software\\gupkg-test-<uuid>`` key this way.
``enable_virtual_terminal(...)`` and ``wait_for_keypress()`` are console helpers
for the command line.

Implementation Approach
-----------------------
The class is a thin, behavior-preserving wrapper over the Windows APIs. The
environment target is a constructor parameter so tests never touch the real
``Environment`` keys. The elevation signals and the access-control reader come
from ``gupkg.selfmgmt.elevation``, the single implementation shared with the
launcher's pre-import trust check. Elevated runs resolve ``powershell`` and
``cmd`` under ``%SystemRoot%`` and never search ``PATH``.

See Also
--------
gupkg.system.port : the protocol this class implements.
"""

from __future__ import annotations

import ctypes
import http.client
import locale
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..selfmgmt import elevation
from ..tls import enable_partial_chain_verification
from .port import (
    DownloadResponse,
    ElevatedRun,
    EnvValue,
    HttpResponse,
    ProcessAborted,
    ProcessResult,
    ResponseTooLarge,
    WorkerDidNotStart,
    Scope,
    ScopeFolders,
    ShortcutSpec,
    MAX_RELAUNCH_PARAMETERS,
    elevated_command,
    unsafe_relaunch_argument,
)

_REGEX_WORKER = Path(__file__).with_name("regex_worker.py")
_WORKER_START_SECONDS = 30.0  # how long a regex worker may take to say it is up

if os.name == "nt":
    import winreg
else:
    winreg = None  # type: ignore[assignment]


class WindowsLinks:
    """Provide directory links on the local file system through NTFS junctions.

    The class is shared by ``WindowsSystem`` and ``FakeSystem``: the files of a
    fake install are real files in a real temporary directory, so its links are
    real links too.
    """

    def create_junction(self, link: Path, target: Path) -> None:
        r"""Create or replace one NTFS junction.

        Parameters
        ----------
        link : Path
            Path where the junction should be created.
        target : Path
            Existing directory the junction should reference.

        Raises
        ------
        RuntimeError
            If the junction cannot be created safely: the target is missing,
            *link* is an existing non-junction, or Windows refuses.
        """
        link, target = Path(link), Path(target)
        if not target.exists() or not target.is_dir():
            raise RuntimeError(f"Junction target does not exist or is not a directory: {target}")

        # An existing junction is replaced; a real directory or file never is.
        replacing = False
        if os.path.lexists(str(link)):
            if self.is_link(link):
                replacing = True
            else:
                raise RuntimeError(f"{link} already exists and is not a junction; refusing to overwrite.")

        # Create the junction; callers see one RuntimeError for any failure.
        try:
            if os.name == "nt":
                _make_junction(link, target, replace=replacing)
            else:
                if replacing:
                    os.rmdir(str(link))
                os.symlink(str(target), str(link), target_is_directory=True)
        except OSError as exc:
            raise RuntimeError(f"Cannot create junction {link} -> {target}: {exc}") from exc

    def read_link(self, path: Path) -> Path | None:
        """Return the normalized target of the link at *path*, or ``None`` when unreadable."""
        try:
            text = os.readlink(str(path))
        except (OSError, AttributeError):
            return None
        if text.startswith("\\\\?\\"):
            text = text[4:]
        return Path(text).resolve()

    def is_link(self, path: Path) -> bool:
        """Return whether *path* is a junction (a symlink on other platforms)."""
        try:
            if os.name == "nt":
                return os.path.isjunction(str(path))
            return os.path.islink(str(path))
        except OSError:
            return False

    def remove_link(self, path: Path) -> None:
        """Remove the link at *path* without touching its target.

        Raises
        ------
        ValueError
            If *path* is not a link, so a real directory is never removed.
        """
        if not self.is_link(path):
            raise ValueError(f"{path} is not a link; refusing to remove it.")
        os.rmdir(str(path))

    def hardlink(self, link: Path, existing: Path) -> None:
        """Create *link* as another name for the existing file *existing*."""
        os.link(str(existing), str(link))


class WindowsSystem(WindowsLinks):
    """Perform every machine side effect of gupkg on a real Windows computer.

    Parameters
    ----------
    user_environment, machine_environment : tuple, optional
        ``(registry hive, subkey)`` that holds the environment values of that
        scope. The defaults are ``HKCU\\Environment`` and the Session Manager
        ``Environment`` key of ``HKLM``.
    """

    def __init__(
        self,
        *,
        user_environment: tuple[Any, str] | None = None,
        machine_environment: tuple[Any, str] | None = None,
    ) -> None:
        self._user_environment = user_environment
        self._machine_environment = machine_environment

    # -- environment -------------------------------------------------------

    def _environment_location(self, scope: Scope) -> tuple[Any, str]:
        """Return the registry ``(hive, subkey)`` holding one scope's environment."""
        if winreg is None:
            raise OSError("The Windows registry is only available on Windows.")
        if scope == Scope.USER:
            return self._user_environment or (winreg.HKEY_CURRENT_USER, "Environment")
        if scope == Scope.MACHINE:
            return self._machine_environment or (
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            )
        raise ValueError(f"Environment scope must be user or system, not {scope.value}")

    def get_env(self, scope: Scope, name: str) -> EnvValue | None:
        """Return one stored environment value, or ``None`` when it is not a string value."""
        hive, subkey = self._environment_location(scope)
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ) as key:
                value, kind = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            return None
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            return None
        return EnvValue(str(value), kind == winreg.REG_EXPAND_SZ)

    def set_env(self, scope: Scope, name: str, value: str, expandable: bool) -> None:
        """Store one environment value, creating its key when it does not exist."""
        hive, subkey = self._environment_location(scope)
        kind = winreg.REG_EXPAND_SZ if expandable else winreg.REG_SZ
        with winreg.CreateKeyEx(hive, subkey, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, kind, value)

    def delete_env(self, scope: Scope, name: str) -> None:
        """Remove one environment value; an absent value or key is not an error."""
        hive, subkey = self._environment_location(scope)
        try:
            with winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return

    def get_path(self, scope: Scope) -> list[str]:
        """Return the non-empty ``PATH`` entries of *scope*; a missing value is an empty list."""
        stored = self.get_env(scope, "Path")
        if stored is None:
            return []
        return [item.strip() for item in stored.value.split(";") if item.strip()]

    def set_path(self, scope: Scope, entries: Sequence[str]) -> None:
        """Store *entries* as the expandable ``PATH`` of *scope*."""
        self.set_env(scope, "Path", ";".join(entries), True)

    def broadcast_environment_change(self) -> None:
        """Notify Windows that environment values changed.

        Raises
        ------
        OSError
            If Windows does not accept the broadcast notification.
        """
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        smto_abortifhung = 0x0002

        send_message_timeout = ctypes.windll.user32.SendMessageTimeoutW
        send_message_timeout.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        send_message_timeout.restype = wintypes.LPARAM

        # SendMessageTimeoutW writes a DWORD_PTR result. ctypes.c_size_t is the
        # portable pointer-sized unsigned type, unlike wintypes.ULONG_PTR, which
        # is absent from some supported Python builds.
        result = ctypes.c_size_t(0)

        # LPARAM is an integer type, so obtain the Environment string address via
        # c_void_p rather than casting directly to a non-pointer ctypes type.
        environment_name = ctypes.c_wchar_p("Environment")
        environment_lparam = ctypes.cast(environment_name, ctypes.c_void_p).value
        ok = send_message_timeout(
            hwnd_broadcast,
            wm_settingchange,
            0,
            environment_lparam,
            smto_abortifhung,
            5000,
            ctypes.byref(result),
        )
        if not ok:
            raise OSError("SendMessageTimeoutW failed")

    # -- shortcuts ---------------------------------------------------------

    def write_shortcut(self, path: Path, spec: ShortcutSpec) -> None:
        """Create or replace one ``.lnk`` file, creating its folder first.

        Raises
        ------
        OSError
            If the shortcut cannot be written. The error names *path*.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_shortcut_file(path, spec)

    def read_shortcut(self, path: Path) -> ShortcutSpec:
        """Read one ``.lnk`` file; the target is the raw stored text, ``%VAR%`` unexpanded."""
        return _read_shortcut_file(Path(path))

    def delete_shortcut(self, path: Path) -> None:
        """Delete one ``.lnk`` file; an absent file is not an error."""
        Path(path).unlink(missing_ok=True)

    # -- privilege ---------------------------------------------------------

    def is_elevated(self) -> bool:
        """Return whether this process may hold an elevated token; unreadable signals count as elevated."""
        return elevation.is_elevated()

    @contextmanager
    def scratch_directory(self, prefix: str) -> Iterator[Path]:
        """Create a private temporary folder, yield it, and delete it (and whatever is left in it) on exit."""
        with tempfile.TemporaryDirectory(prefix=prefix, ignore_cleanup_errors=True) as folder:
            yield Path(folder)

    def process_id(self) -> int:
        """Return the id of this process."""
        return os.getpid()

    def process_is_running(self, pid: int) -> bool:
        """Return whether process *pid* is still running (``OpenProcess`` and its exit code); doubt counts as running."""
        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except OSError:
                return True
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # ERROR_INVALID_PARAMETER (87): no such process. Anything else (access denied) means it exists.
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def relaunch_elevated(
        self, argv: Sequence[str], *, on_interrupt: Callable[[], None] | None = None, grace: float = 30.0,
    ) -> ElevatedRun:
        """Run the bundled gupkg once elevated with *argv*, wait for it and return how it ended.

        The child is ``python.exe -I -s -B -S bootstrap.py`` of this package, started with the ``runas`` verb through
        ``ShellExecuteEx`` and no shell, so nothing in *argv* is read by ``cmd.exe`` (T1). Both paths come from the
        location of this module. Before the prompt every file the child loads first (``bundled_runtime_paths``: the
        runtime folder, ``python.exe``, ``python*.dll``, ``python*._pth``, ``Lib\\site-packages``, ``bootstrap.py`` and the
        folders above) must be admin-only, else the call is ``refused`` and nothing is asked. A value that could mean
        something to a shell, or a command line too long for ``ShellExecuteEx``, is refused the same way.

        A check-then-launch race (TOCTOU) remains: a file could change between the check and the load. It is bounded by the
        admin-only requirement itself, because only an administrator can change what the check accepted, and the child
        checks the same set again once it runs (``enforce_bundled_runtime``).

        After the child started this call never reports "not started": a failure while waiting is ``error`` and the process
        handle is closed on every path.
        """
        if os.name != "nt" or any(unsafe_relaunch_argument(part) for part in argv):
            return ElevatedRun(False)
        try:
            package = Path(__file__).resolve().parent.parent
            program, parameters, directory = elevated_command(package, argv)
            if len(parameters) > _MAX_PARAMETERS:
                return ElevatedRun(
                    False, refused=f"the command line is {len(parameters)} characters long; more than {_MAX_PARAMETERS} cannot be started"
                )
            if not program.is_file() or not (package / "bootstrap.py").is_file():
                return ElevatedRun(False)
            for path in elevation.bundled_runtime_paths(package):
                if path.exists():
                    reason = elevation.untrusted_reason(path)
                    if reason is not None:
                        return ElevatedRun(False, refused=f"{path}: {reason}")
            info = _ShellExecuteInfo()
            info.cbSize = ctypes.sizeof(_ShellExecuteInfo)
            info.fMask = _SEE_MASK_NOCLOSEPROCESS
            info.lpVerb = "runas"
            info.lpFile = str(program)
            info.lpParameters = parameters
            info.lpDirectory = str(directory)
            info.nShow = 1
            if not _shell_execute(info) or not info.hProcess:
                return ElevatedRun(False)
        except Exception:
            return ElevatedRun(False)

        # The child runs now: from here on the answer is never "not started", and the handle is always closed.
        handle = info.hProcess
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

            def done(seconds: float) -> bool:
                """Wait up to *seconds* for the child; report whether it ended, and raise when the wait itself failed."""
                state = kernel32.WaitForSingleObject(handle, int(seconds * 1000))
                if state == 0:
                    return True
                if state == _WAIT_TIMEOUT:
                    return False
                raise OSError(f"waiting for the elevated gupkg failed (WaitForSingleObject returned {state:#x})")

            def exit_code() -> int:
                """Return the child's exit code."""
                code = wintypes.DWORD()
                return int(code.value) if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) else 3

            return wait_for_child(done, exit_code, on_interrupt=on_interrupt, grace=grace)
        except Exception as error:  # noqa: BLE001 - the child is running; say so instead of "not started"
            return ElevatedRun(True, error=str(error) or type(error).__name__)
        finally:
            _close_handle(handle)

    def directory_is_admin_only(self, path: Path) -> bool:
        """Return whether only administrators can modify *path* and replace its parents."""
        return self.directory_untrusted_reason(path) is None

    def directory_untrusted_reason(self, path: Path) -> str | None:
        """Return why *path* is not admin-only, or ``None``; unreadable permissions give a reason."""
        return elevation.untrusted_reason(Path(path))

    # -- locations ---------------------------------------------------------

    def scope_folders(self, scope: Scope) -> ScopeFolders:
        """Resolve the default Start Menu ``opt`` folder and ``bin`` folder of one scope.

        Raises
        ------
        ValueError
            If a required environment variable such as ``APPDATA`` is missing.
        """
        def env(name: str, purpose: str) -> str:
            """Return a required environment variable or explain why it is needed."""
            value = os.environ.get(name)
            if not value:
                raise ValueError(f"{name} is not set; cannot compute {purpose}.")
            return value

        start_menu = Path("Microsoft") / "Windows" / "Start Menu" / "opt"
        if scope == Scope.USER:
            return ScopeFolders(
                Path(env("APPDATA", "User-scope shortcut directory")) / start_menu,
                Path(env("USERPROFILE", "User-scope bin directory")) / "bin",
            )
        shortcut_root = Path(env("PROGRAMDATA", "Machine-scope shortcut directory")) / start_menu
        system_drive = env("SYSTEMDRIVE", "Machine-scope bin directory")
        if len(system_drive) == 2 and system_drive[0].isalpha() and system_drive[1] == ":":
            system_drive += "\\"
        return ScopeFolders(shortcut_root, Path(system_drive) / "bin")

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
        """Run a child process to completion, streaming each output line to *on_line*.

        Raises
        ------
        OSError
            If the program cannot be started; ``TimeoutError`` when *timeout*
            seconds pass first (the process is killed); ``ProcessAborted`` when *watch*
            returns a reason (the process is killed).
        """
        return self._run(argv, cwd, env, on_line, input_text=input_text, timeout=timeout, watch=watch, kill_on_close=False)

    def run_foreground(self, argv: Sequence[str], cwd: Path | None = None, env: Mapping[str, str] | None = None) -> int:
        """Run a child on this console with inherited standard streams and return its exit status.

        Raises
        ------
        OSError
            If the program cannot be started.
        """
        process = subprocess.Popen(
            _script_command(argv), cwd=str(cwd) if cwd is not None else None, env=dict(env) if env is not None else None,
        )
        return process.wait()

    def open_path(self, path: Path) -> None:
        """Open *path* with its associated program, as double-clicking it in Explorer would.

        Raises
        ------
        OSError
            If *path* does not exist or no program is associated with it.
        """
        os.startfile(str(path))  # noqa: S606 - the path names a file gupkg itself wrote (a log)

    def _run(
        self,
        argv: Sequence[str],
        cwd: Path | None,
        env: Mapping[str, str] | None,
        on_line: Callable[[str], None] | None,
        *,
        input_text: str | None,
        timeout: float | None,
        watch: Callable[[], str | None] | None,
        kill_on_close: bool,
    ) -> ProcessResult:
        """Implement ``run``; with *kill_on_close* the child's job object also ends it (and its helpers) when the job handle closes, even if this process is killed first."""
        command = _script_command(argv)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        # Read both streams on their own threads so a full pipe never blocks
        # the child, and report lines under one lock so callers see whole lines.
        lock = threading.Lock()
        collected: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def read(name: str, stream: Any) -> None:
            """Collect one stream line by line and forward each line."""
            for line, terminated, continued in _output_lines(stream):
                with lock:
                    collected[name].append(line + ("\n" if terminated else ""))
                    if on_line is not None:
                        on_line(line + (" [line continues]" if continued else ""))

        readers = [
            threading.Thread(target=read, args=(name, stream), daemon=True)
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for reader in readers:
            reader.start()

        # Feed standard input from its own thread, so a child that never reads it cannot
        # keep this call from enforcing the timeout; then wait. A timeout kills the whole child.
        job: _ProcessJob | None = None
        feeder = None
        try:
            # Every process the child starts is tracked by a job object, so a kill reaches a helper
            # that outlived its parent (``taskkill /T`` cannot find the children of a dead process).
            # A failure here is inside the try: the child is killed and the error reaches the caller.
            job = _ProcessJob.around(process, kill_on_close=kill_on_close)
            if input_text is not None:
                payload = input_text.encode(locale.getpreferredencoding(False), errors="replace")

                def feed() -> None:
                    """Write the input and close the pipe; a child that is gone closes it for us."""
                    try:
                        process.stdin.write(payload)
                        process.stdin.close()
                    except OSError:
                        pass

                feeder = threading.Thread(target=feed, daemon=True)
                feeder.start()
            # Wait in short slices when a watcher is given, so it can stop a runaway child.
            started = time.monotonic()
            while True:
                slice_seconds = 0.2 if watch is not None else timeout
                if timeout is not None and slice_seconds is not None:
                    slice_seconds = min(slice_seconds, max(timeout - (time.monotonic() - started), 0.0))
                try:
                    returncode = process.wait(timeout=slice_seconds)
                    break
                except subprocess.TimeoutExpired:
                    if timeout is not None and time.monotonic() - started >= timeout:
                        raise TimeoutError(f"{command[0]} did not finish within {timeout} seconds") from None
                    reason = watch() if watch is not None else None
                    if reason is not None:
                        raise ProcessAborted(reason) from None
            # Like the test double, ask the watcher once more after the child has finished.
            reason = watch() if watch is not None else None
            if reason is not None:
                raise ProcessAborted(reason)
        except BaseException:
            # A timeout, an abort, a raising watcher, Ctrl-C or any other failure must not leave the
            # child (or a helper it started, even one that outlived it) running, because the
            # reader threads below would wait for it.
            _kill_tree(process, job)
            process.wait()
            raise
        finally:
            for reader in readers:
                reader.join()
            if feeder is not None:
                feeder.join(5)
            if job is not None:
                job.close()
        return ProcessResult(returncode, "".join(collected["stdout"]), "".join(collected["stderr"]))

    def regex_worker(
        self,
        request: str,
        timeout: float,
        *,
        on_line: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        """Run ``regex_worker.py`` on *request* in an isolated child with a hard deadline (see the port).

        The deadline counts from the moment the worker reports ``pid <n>`` (the interpreter and its
        imports are up), so a slow process start (antivirus, a cold disk) is not mistaken for a slow
        pattern; the start itself has its own generous limit. The watcher polls five times a second,
        so the kill happens within about 0.2 seconds of the deadline.
        """
        command = [sys.executable, "-I", "-s", "-B", "-S", str(_REGEX_WORKER)]
        environment = {"SystemRoot": os.environ.get("SystemRoot", r"C:\Windows")}
        began = time.monotonic()
        ready: list[float] = []

        def heard(line: str) -> None:
            """Note when the worker says it is up, and pass the line on."""
            if not ready and line.startswith("pid "):
                ready.append(time.monotonic())
            if on_line is not None:
                on_line(line)

        def late() -> str | None:
            """Return a reason once the worker has overrun its start limit or its deadline."""
            now = time.monotonic()
            if not ready:
                return "start" if now - began > _WORKER_START_SECONDS else None
            return "deadline" if now - ready[0] > timeout else None

        with tempfile.TemporaryDirectory(prefix="gupkg-regex-", ignore_cleanup_errors=True) as folder:
            try:
                return self._run(
                    command, Path(folder), environment, heard,
                    input_text=request, timeout=None, watch=late, kill_on_close=True,
                )
            except ProcessAborted as error:
                if str(error) == "start":
                    raise WorkerDidNotStart(f"the regex worker did not start within {_WORKER_START_SECONDS:g} seconds") from None
                raise TimeoutError(f"the regex worker was still running {timeout:g} seconds after it started") from None

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
        """Download *url* to *dest* in 64 KiB chunks and return the byte count."""
        request = urllib.request.Request(url, headers=dict(headers or {}))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = _content_length(response)
            if on_progress is not None:
                on_progress(0, total)
            received = 0
            with open(dest, "wb") as handle:
                while chunk := response.read(64 * 1024):
                    handle.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)
        return received

    def fetch_text(
        self, url: str, headers: Mapping[str, str] | None = None, *, timeout: float = 60
    ) -> str:
        """Return the UTF-8 text served at *url*."""
        request = urllib.request.Request(url, headers=dict(headers or {}))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")

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
        """Send one request without following redirects and return the response of any status."""
        deadline = _Deadline(total_timeout)
        try:
            with _open_without_redirects(url, headers, method, timeout, deadline) as response:
                status, final_url, response_headers = _response_parts(response, url)
                if 200 <= status < 300:
                    _refuse_encoding(response_headers)
                expected = _expected_length(response, method, status)
                body = _read_limited(response, max_bytes, deadline, expected)
        except http.client.HTTPException as error:
            raise _transport_error(deadline, error) from error
        except OSError as error:
            if deadline.hit and not isinstance(error, TimeoutError):
                raise _transport_error(deadline, error) from error
            raise
        finally:
            deadline.finish()
        return HttpResponse(status, final_url, response_headers, body)

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
        """Stream a 2xx response into *dest* (appending a 206 answer when asked); write nothing otherwise."""
        deadline = _Deadline(total_timeout)
        try:
            with _open_without_redirects(url, headers, "GET", timeout, deadline) as response:
                status, final_url, response_headers = _response_parts(response, url)
                if on_headers is not None:
                    on_headers(status, response_headers)
                if not 200 <= status < 300:
                    return DownloadResponse(status, final_url, response_headers)
                _refuse_encoding(response_headers)

                # A 206 answer to a range request continues the existing file; any other
                # 2xx answer is the whole body and replaces it.
                resumed = append and status == 206
                already = Path(dest).stat().st_size if resumed else 0
                length = _expected_length(response, "GET", status)
                total = already + length if length is not None else None
                if on_progress is not None:
                    on_progress(already, total)
                received = already
                with open(dest, "ab" if resumed else "wb") as handle:
                    while chunk := _read_some(response):
                        handle.write(chunk)
                        received += len(chunk)
                        if on_progress is not None:
                            on_progress(received, total)
                        if deadline.expired():
                            raise TimeoutError(f"{url} took longer than {total_timeout} seconds in total")
                if length is not None and received - already != length:
                    raise OSError(f"{url} sent {received - already} of {length} bytes: the transfer was cut short")
        except http.client.HTTPException as error:
            raise _transport_error(deadline, error) from error
        except OSError as error:
            if deadline.hit and not isinstance(error, TimeoutError):
                raise _transport_error(deadline, error) from error
            raise
        finally:
            deadline.finish()
        return DownloadResponse(status, final_url, response_headers, received - already, resumed)


# ---------------------------------------------------------------------------
# Console helpers (used by the command line, not part of System)
# ---------------------------------------------------------------------------


def enable_virtual_terminal(fileno: int) -> bool:
    """Turn on ANSI processing for a Windows console handle; report success."""
    import msvcrt

    try:
        kernel32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(fileno)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, AttributeError):
        return False


def wait_for_keypress() -> None:
    """Pause for a keypress using the Windows console when possible."""
    try:
        import msvcrt

        msvcrt.getch()
    except ImportError:
        input("Press Enter to continue...")


# ---------------------------------------------------------------------------
# Helpers behind the System methods
# ---------------------------------------------------------------------------


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_TIMEOUT = 0x102
_MAX_PARAMETERS = MAX_RELAUNCH_PARAMETERS


def _shell_execute(info: "_ShellExecuteInfo") -> bool:
    """Call ``ShellExecuteExW`` (the one place that can show the UAC prompt); return whether the process was started."""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    return bool(shell32.ShellExecuteExW(ctypes.byref(info)))


def _close_handle(handle: Any) -> None:
    """Close a process handle, ignoring a failure to do so."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - nothing more can be done about a handle that will not close
        pass


class _ShellExecuteInfo(ctypes.Structure):
    """The ``SHELLEXECUTEINFOW`` structure of ``ShellExecuteExW``."""

    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def wait_for_child(
    done: Callable[[float], bool], exit_code: Callable[[], int], *,
    on_interrupt: Callable[[], None] | None, grace: float, clock: Callable[[], float] = time.monotonic,
) -> ElevatedRun:
    """Wait until ``done(seconds)`` says the child ended and return its exit code, handling interrupts.

    The first interrupt (Ctrl-C) calls *on_interrupt* once (it asks the child to stop) and the wait goes on for at most
    *grace* seconds; a second interrupt or the end of the grace period gives up and returns ``abandoned``.
    """
    interrupted_at: float | None = None
    while True:
        try:
            if done(0.25):
                return ElevatedRun(True, exit_code())
            if interrupted_at is not None and clock() - interrupted_at >= grace:
                return ElevatedRun(True, None, True)
        except KeyboardInterrupt:
            if interrupted_at is not None:
                return ElevatedRun(True, None, True)
            interrupted_at = clock()
            if on_interrupt is not None:
                try:
                    on_interrupt()
                except OSError:
                    pass


def _system_tool(name: str) -> str:
    """Return the absolute path of a Windows system tool under ``%SystemRoot%``.

    Parameters
    ----------
    name : {"powershell", "cmd"}
        Tool to locate.

    Returns
    -------
    str
        Absolute path when the tool exists. Otherwise the bare name is returned
        for an ordinary run, and ``OSError`` is raised in an elevated run, which
        never searches ``PATH``.
    """
    relative = {
        "powershell": Path("System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "cmd": Path("System32", "cmd.exe"),
    }[name]
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if root and (Path(root) / relative).is_file():
        return str(Path(root) / relative)
    if elevation.elevated_mode():
        raise OSError(f"Cannot find {name} under %SystemRoot%; elevated runs do not search PATH")
    return name


def _decode_output(data: bytes) -> str:
    """Decode one line of child output: UTF-8 when it is valid, else the system code page.

    Modern tools (Git, Python) write UTF-8 whatever the code page is; older
    console tools write the code page. Invalid bytes become replacement
    characters instead of failing the run.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


# Longest piece of a child's line that is reported at once; a longer line is split.
_MAX_LINE = 64 * 1024
# What ends a line of child output: a line feed or a carriage return.
_LINE_END = re.compile(rb"[\r\n]")


def _output_lines(stream: Any) -> Iterator[tuple[str, bool, bool]]:
    """Yield ``(line, terminated, continued)`` for a child's binary output stream, as soon as each line is complete.

    A line ends at a line feed, a carriage return plus line feed, or a lone
    carriage return (the way progress meters redraw), so carriage-return
    updates arrive as separate lines at once. The last line may have no
    terminator. A line longer than 64 KiB is cut into pieces (never inside a
    UTF-8 character); every piece but the last has ``continued`` true and no
    terminator, so the pieces concatenate to the original line. Each read is
    scanned once, so a very long line costs time proportional to its length.
    """
    pending = bytearray()
    index = 0
    while True:
        chunk = stream.read1(8192)
        final = not chunk
        pending += chunk

        # Report every complete line; ``index`` is where scanning resumes, so
        # bytes seen before are never searched again.
        start = 0
        while True:
            match = _LINE_END.search(pending, index)
            end = match.start() if match else len(pending)

            # Cut a very long line into bounded pieces, backing up so a UTF-8
            # character is never split between two pieces.
            while end - start > _MAX_LINE:
                # A UTF-8 character has at most 4 bytes, so backing up 3 at most
                # finds its start; bytes that are not UTF-8 are cut as they are.
                cut, back = start + _MAX_LINE, 0
                while back < 3 and pending[cut] & 0xC0 == 0x80:
                    cut -= 1
                    back += 1
                yield _decode_output(bytes(pending[start:cut])), False, True
                start = cut
            if match is None:
                index = len(pending)
                break

            # A carriage return at the end of the data waits for the next
            # read, because a line feed may follow it.
            if pending[end] == 13 and end + 1 >= len(pending) and not final:
                index = end
                break
            yield _decode_output(bytes(pending[start:end])), True, False
            follows_lf = pending[end] == 13 and pending[end + 1 : end + 2] == b"\n"
            index = start = end + (2 if follows_lf else 1)

        # Keep only the unreported tail; it is shorter than one piece.
        del pending[:start]
        index -= start
        if final:
            if pending:
                yield _decode_output(bytes(pending)), False, False
            return


def _script_command(argv: Sequence[str]) -> list[str]:
    """Expand a script path in ``argv[0]`` to the interpreter command that runs it."""
    program = str(argv[0])
    rest = [str(part) for part in argv[1:]]
    suffix = Path(program).suffix.lower()
    if suffix == ".ps1":
        return [_system_tool("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", program, *rest]
    if suffix in {".cmd", ".bat"}:
        return [_system_tool("cmd"), "/c", program, *rest]
    return [program, *rest]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx answer to the caller instead of following it."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        """Decline every redirect, so urllib reports the 3xx response itself."""
        return None


_ALLOWED_SCHEMES = ("http", "https", "file")


class _Deadline:
    """An absolute time limit for one request, enforced from a timer thread as well as between reads.

    ``socket`` timeouts only bound the idle time of each operation, so a server that drips header
    lines or body bytes could keep a request alive for ever. When the limit passes, the timer shuts
    the connection's sockets down, which wakes a blocked read with an error.
    """

    def __init__(self, seconds: float | None) -> None:
        """Start the clock (no limit when *seconds* is falsy)."""
        self.seconds = seconds
        self.expires = time.monotonic() + seconds if seconds else None
        self.hit = False
        self._sockets: list[socket.socket] = []
        self._timer: threading.Timer | None = None
        if seconds:
            self._timer = threading.Timer(seconds, self._expire)
            self._timer.daemon = True
            self._timer.start()

    def expired(self) -> bool:
        """Return whether the limit has passed."""
        return self.expires is not None and time.monotonic() > self.expires

    def register(self, sock: socket.socket) -> None:
        """Watch *sock*; if the limit already passed, shut it down at once."""
        self._sockets.append(sock)
        if self.hit:
            self._shutdown(sock)

    def finish(self) -> None:
        """Stop the timer; the request is over."""
        if self._timer is not None:
            self._timer.cancel()

    def _expire(self) -> None:
        """Mark the limit as hit and wake every blocked read."""
        self.hit = True
        for sock in list(self._sockets):
            self._shutdown(sock)

    @staticmethod
    def _shutdown(sock: socket.socket) -> None:
        """Shut a socket down in both directions, ignoring a socket that is already closed."""
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _CutHeaders(http.client.HTTPException):
    """The server closed the connection in the middle of the response headers."""


class _EofWatch:
    """Wrap a response file to note whether a line read came back empty (end of file) inside the headers."""

    def __init__(self, inner: Any) -> None:
        """Wrap *inner*."""
        self.inner = inner
        self.hit_eof = False

    def readline(self, *args: Any) -> bytes:
        """Read a line and remember an end of file."""
        line = self.inner.readline(*args)
        if not line:
            self.hit_eof = True
        return line

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the wrapped file."""
        return getattr(self.inner, name)


class _StrictResponse(http.client.HTTPResponse):
    """An HTTP response that fails when the headers end by end of file instead of by a blank line.

    The standard library treats a connection closed in the middle of the headers as a normal end of
    the headers, so a cut-short answer would look like a successful (empty) one.
    """

    def begin(self) -> None:
        """Read the status line and headers; refuse a connection that closed before the blank line."""
        watch = _EofWatch(self.fp)
        self.fp = watch
        try:
            super().begin()
        finally:
            self.fp = watch.inner
        if watch.hit_eof:
            raise _CutHeaders("the server closed the connection in the middle of the response headers")


def _timed(base: type, deadline: _Deadline):
    """Return a connection class like *base* whose socket is watched by *deadline*."""

    class Timed(base):  # type: ignore[misc, valid-type]
        """A connection that registers its socket with the request's deadline."""

        response_class = _StrictResponse

        def connect(self) -> None:
            """Connect, then hand the socket to the deadline."""
            super().connect()
            deadline.register(self.sock)

    return Timed


class _DeadlineHTTP(urllib.request.HTTPHandler):
    """Open ``http`` URLs with connections that obey a request deadline."""

    def __init__(self, deadline: _Deadline) -> None:
        """Remember the deadline."""
        super().__init__()
        self._deadline = deadline

    def http_open(self, req: Any) -> Any:
        """Open *req* on a watched connection."""
        return self.do_open(_timed(http.client.HTTPConnection, self._deadline), req)


class _DeadlineHTTPS(urllib.request.HTTPSHandler):
    """Open ``https`` URLs with connections that obey a request deadline."""

    def __init__(self, deadline: _Deadline) -> None:
        """Remember the deadline."""
        super().__init__()
        self._deadline = deadline

    def https_open(self, req: Any) -> Any:
        """Open *req* on a watched connection."""
        return self.do_open(_timed(http.client.HTTPSConnection, self._deadline), req, context=self._context)


def _transport_error(deadline: _Deadline, error: BaseException) -> OSError:
    """Map a protocol exception (not an ``OSError`` in the standard library) to the port's error type."""
    if deadline.hit:
        return TimeoutError(f"The request took longer than {deadline.seconds} seconds in total")
    if isinstance(error, OSError):
        return error
    return OSError(f"HTTP protocol error: {type(error).__name__}: {error}")


def _refuse_encoding(headers: Mapping[str, str]) -> None:
    """Refuse a body with a ``Content-Encoding`` other than identity: gupkg never decompresses a response."""
    encoding = headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise OSError(f"The response uses Content-Encoding {encoding!r}, which is not accepted")


def _open_without_redirects(url: str, headers: Mapping[str, str] | None, method: str, timeout: float,
                            deadline: _Deadline | None = None):
    """Open *url* and return its response object, a 3xx or error answer included.

    Only ``http``, ``https`` and ``file`` URLs are opened. HTTPS verification
    uses the system store with partial-chain support (``gupkg.tls``).
    """
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise OSError(f"Only http, https and file URLs are fetched, not {url!r}")
    enable_partial_chain_verification()
    watched = deadline or _Deadline(None)
    opener = urllib.request.build_opener(_NoRedirect, _DeadlineHTTP(watched), _DeadlineHTTPS(watched))
    request = urllib.request.Request(url, headers=dict(headers or {}), method=method)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        # An HTTPError is itself a readable response; the caller maps it like any other.
        return error


def _response_parts(response: Any, requested: str) -> tuple[int, str, dict[str, str]]:
    """Return ``(status, url, lower-case headers)`` of a urllib response or ``HTTPError``."""
    status = getattr(response, "status", None) or getattr(response, "code", None) or 200
    collected: dict[str, str] = {}
    for name, value in response.headers.items():
        key = name.lower()
        collected[key] = f"{collected[key]}, {value}" if key in collected else value
    return int(status), requested, collected


class _JobBasicLimits(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION``."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobIoCounters(ctypes.Structure):
    """``IO_COUNTERS``."""

    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JobExtendedLimits(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``."""

    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimits),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ProcessJob:
    """A Windows job object holding a child process and every process it starts.

    ``TerminateJobObject`` ends all of them, including a helper whose parent has already exited,
    which ``taskkill /T`` cannot find. By default the job does not kill anything when it is closed,
    so a helper that is meant to outlive a normal run keeps running; with ``kill_on_close`` closing
    the last handle (also when this process is killed) ends everything in the job, which is what
    the regex worker wants. A process started in the instant before it is assigned to the job is
    the one gap; the child needs far longer than that to start a helper of its own.
    """

    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMIT_INFORMATION = 9

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def __init__(self, handle: Any, kernel32: Any) -> None:
        """Hold the job *handle* of the ``kernel32`` library."""
        self._handle, self._kernel32 = handle, kernel32

    @classmethod
    def around(cls, process: "subprocess.Popen[bytes]", *, kill_on_close: bool = False) -> "_ProcessJob | None":
        """Put *process* in a new job; ``None`` when Windows refuses (the caller falls back to ``taskkill``).

        With *kill_on_close* the job ends all its processes when its last handle closes. If that
        limit cannot be set the job is not used (``None``): the caller must not believe in a guarantee it lacks.
        """
        if os.name != "nt":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        if kill_on_close:
            limits = _JobExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = cls._KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job, cls._EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                kernel32.CloseHandle(job)
                return None
        member = kernel32.OpenProcess(cls._PROCESS_SET_QUOTA | cls._PROCESS_TERMINATE, False, process.pid)
        assigned = bool(member) and bool(kernel32.AssignProcessToJobObject(job, member))
        if member:
            kernel32.CloseHandle(member)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return cls(job, kernel32)

    def terminate(self) -> bool:
        """End every process of the job; report whether Windows did it."""
        return bool(self._kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        """Release the job handle; processes still in the job keep running unless the job was made with ``kill_on_close``."""
        self._kernel32.CloseHandle(self._handle)


def _kill_tree(process: "subprocess.Popen[bytes]", job: "_ProcessJob | None" = None) -> None:
    """Kill *process* and every process it started: through its job object when it has one, else ``taskkill /T``, then the process itself.

    ``Popen.kill`` ends only the child, so a script that started a helper would leave it running.
    """
    if job is not None and job.terminate():
        process.kill()
        return
    try:
        subprocess.run(
            [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"),
             "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    process.kill()


def _read_some(response: Any) -> bytes:
    """Return up to 64 KiB as soon as some bytes are available (``read1``), so a deadline can be checked between reads."""
    reader = getattr(response, "read1", None)
    return reader(64 * 1024) if reader is not None else response.read(64 * 1024)


def _read_limited(response: Any, max_bytes: int | None, deadline: _Deadline | None = None,
                  expected: int | None = None) -> bytes:
    """Read the body in blocks.

    Fails with ``ResponseTooLarge`` past *max_bytes*, ``TimeoutError`` past the deadline, and ``OSError``
    when the body ends before the *expected* ``Content-Length`` (a cut-short transfer is never data).
    """
    if max_bytes == 0:
        return b""
    pieces: list[bytes] = []
    size = 0
    while True:
        block = _read_some(response)
        if not block:
            if expected is not None and size != expected:
                raise OSError(f"The response ended after {size} of {expected} bytes: the transfer was cut short")
            return b"".join(pieces)
        size += len(block)
        if max_bytes is not None and size > max_bytes:
            raise ResponseTooLarge(f"The response is larger than {max_bytes} bytes")
        pieces.append(block)
        if deadline is not None and deadline.expired():
            raise TimeoutError("The response took longer than its total time limit")


def _expected_length(response: Any, method: str, status: int) -> int | None:
    """Return the body size to verify, or ``None`` when the answer has no body (HEAD, 1xx, 204, 304).

    A body without a ``Content-Length`` (HTTP/1.0 or ``Connection: close``) cannot be verified;
    like a browser, gupkg accepts it, and the checksum rules cover integrity.
    """
    if method == "HEAD" or status < 200 or status in (204, 304):
        return None
    return _content_length(response)


def _content_length(response: Any) -> int | None:
    """Return a valid response size when the HTTP response exposes one."""
    headers = getattr(response, "headers", None)
    raw_value = headers.get("Content-Length") if headers is not None else None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _make_junction(source: Path, target: Path, *, replace: bool = False) -> None:
    r"""Create *source* as an NTFS junction to *target* without a child process.

    Parameters
    ----------
    source : Path
        Path that must not exist yet, or an existing junction when *replace* is set.
    target : Path
        Existing directory the junction references.
    replace : bool, optional
        Remove the existing junction *source* first. The removal happens only after everything that could import a module
        is done: gupkg can be running from the very junction it replaces (``current``), and once that is gone the
        standard library behind it cannot be read, so any later first-time import (a codec, a ctypes helper) fails.

    Raises
    ------
    OSError
        If the directory or the reparse point cannot be created.
    """
    generic_write = 0x40000000
    open_existing = 3
    file_share_all = 0x00000007
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    fsctl_set_reparse_point = 0x000900A4
    io_reparse_tag_mount_point = 0xA0000003

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # Build the MOUNT_POINT_REPARSE_BUFFER: the substitute name is the NT path
    # ``\??\<absolute target>``, the print name is the plain absolute path.
    # A ``\\?\`` prefix is dropped so the NT name is not doubled; network
    # (UNC) targets cannot be junction targets and are refused up front.
    absolute = os.path.abspath(str(target))
    if absolute.upper().startswith("\\\\?\\UNC\\"):
        raise OSError(f"a junction cannot point to a network path: {target}")
    if absolute.startswith("\\\\?\\"):
        absolute = absolute[4:]
    if absolute.startswith("\\\\"):
        raise OSError(f"a junction cannot point to a network path: {target}")
    absolute = absolute.rstrip("\\")
    if len(absolute) == 2:
        # A drive root such as ``C:\`` loses its backslash above; restore it.
        absolute += "\\"
    substitute = ("\\??\\" + absolute).encode("utf-16-le")
    printed = absolute.encode("utf-16-le")
    path_buffer = substitute + b"\0\0" + printed + b"\0\0"
    header = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed))
    data = header + path_buffer
    buffer = struct.pack("<IHH", io_reparse_tag_mount_point, len(data), 0) + data

    # Touch what the steps below use for the first time (the error formatting, the path text), then drop the old junction.
    ctypes.byref(wintypes.DWORD(0))
    source_text = str(source)
    if replace:
        os.rmdir(source_text)

    # Create the empty directory that will carry the reparse point. Every
    # failure from here on removes it again, so no half-made junction is left.
    if not kernel32.CreateDirectoryW(source_text, None):
        raise ctypes.WinError(ctypes.get_last_error())

    # Open the new directory itself (not what it would point to) and attach it.
    handle = kernel32.CreateFileW(
        str(source),
        generic_write,
        file_share_all,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    if handle == wintypes.HANDLE(-1).value or handle is None:
        error = ctypes.WinError(ctypes.get_last_error())
        os.rmdir(str(source))
        raise error
    try:
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle,
            fsctl_set_reparse_point,
            buffer,
            len(buffer),
            None,
            0,
            ctypes.byref(returned),
            None,
        )
        error = None if ok else ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    if error is not None:
        os.rmdir(str(source))
        raise error


# ---------------------------------------------------------------------------
# Shortcut files through the Shell COM API
# ---------------------------------------------------------------------------
# Each call initializes COM for the calling thread, which lets worker threads
# use it, and releases every interface it acquired before returning.

def _write_shortcut_file(path: Path, link: ShortcutSpec) -> None:
    """Create or replace one ``.lnk`` file.

    Parameters
    ----------
    path : Path
        Full ``.lnk`` path to write. The parent directory must exist.
    link : ShortcutSpec
        Fields to store. ``target_path`` is stored unresolved.

    Raises
    ------
    OSError
        If any COM call fails. ``filename`` is *path*.
    """
    with _open_link(path) as (shell_link, persist):
        # Set the target first, then each optional text field.
        _check(
            _method(shell_link, _ISHELLLINK_SLOTS["SetPath"], wintypes.LPCWSTR)(
                link.target_path
            ),
            path,
        )
        setters = (
            ("SetArguments", link.arguments),
            ("SetWorkingDirectory", link.working_directory),
            ("SetDescription", link.description),
        )
        for name, value in setters:
            _check(_method(shell_link, _ISHELLLINK_SLOTS[name], wintypes.LPCWSTR)(value), path)

        # An empty icon location means "no icon", so the setter is skipped.
        if link.icon_location:
            icon_path, index = _split_icon(link.icon_location)
            set_icon = _method(
                shell_link, _ISHELLLINK_SLOTS["SetIconLocation"], wintypes.LPCWSTR, ctypes.c_int
            )
            _check(set_icon(icon_path, index), path)

        # Save through IPersistFile; this replaces any existing file.
        save = _method(persist, _IPERSISTFILE_SLOTS["Save"], wintypes.LPCWSTR, wintypes.BOOL)
        _check(save(os.fspath(path), True), path)


def _read_shortcut_file(path: Path) -> ShortcutSpec:
    """Read one ``.lnk`` file.

    Parameters
    ----------
    path : Path
        Existing ``.lnk`` file.

    Returns
    -------
    ShortcutSpec
        The stored fields. The target is the raw stored text, with ``%VAR%``
        references unexpanded. ``icon_location`` is ``"path,index"``, or empty
        when the link has no icon path.

    Raises
    ------
    OSError
        If the file cannot be loaded or a field cannot be read. ``filename``
        is *path*.
    """
    with _open_link(path) as (shell_link, persist):
        # Load the file into the link object.
        load = _method(persist, _IPERSISTFILE_SLOTS["Load"], wintypes.LPCWSTR, wintypes.DWORD)
        _check(load(os.fspath(path), _STGM_READ), path)

        def read_text(name: str, *extra: tuple[Any, Any]) -> str:
            """Call one ``Get...(buffer, size, ...)`` method and return its text."""
            buffer = ctypes.create_unicode_buffer(_BUFFER_CHARS)
            getter = _method(
                shell_link,
                _ISHELLLINK_SLOTS[name],
                ctypes.c_void_p,
                ctypes.c_int,
                *[argtype for argtype, _ in extra],
            )
            _check(getter(buffer, _BUFFER_CHARS, *[value for _, value in extra]), path)
            return buffer.value

        # Read the text fields; the raw-path flag keeps %VAR% unexpanded.
        target = read_text("GetPath", (ctypes.c_void_p, None), (wintypes.DWORD, _SLGP_RAWPATH))
        arguments = read_text("GetArguments")
        working_directory = read_text("GetWorkingDirectory")
        description = read_text("GetDescription")

        # The icon comes back as a path plus a separate index.
        icon_buffer = ctypes.create_unicode_buffer(_BUFFER_CHARS)
        icon_index = ctypes.c_int(0)
        get_icon = _method(
            shell_link,
            _ISHELLLINK_SLOTS["GetIconLocation"],
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        )
        _check(get_icon(icon_buffer, _BUFFER_CHARS, ctypes.byref(icon_index)), path)
        icon = f"{icon_buffer.value},{icon_index.value}" if icon_buffer.value else ""
    return ShortcutSpec(target, arguments, working_directory, icon, description)


# ------------------------------------------
# Section: COM plumbing
# ------------------------------------------

# IShortcutSpecW vtable slots (ShObjIdl_core.h method order, IUnknown is 0-2):
#   3 GetPath, 4 GetIDList, 5 SetIDList, 6 GetDescription, 7 SetDescription,
#   8 GetWorkingDirectory, 9 SetWorkingDirectory, 10 GetArguments,
#   11 SetArguments, 12 GetHotkey, 13 SetHotkey, 14 GetShowCmd, 15 SetShowCmd,
#   16 GetIconLocation, 17 SetIconLocation, 18 SetRelativePath, 19 Resolve,
#   20 SetPath
_ISHELLLINK_SLOTS = {
    "GetPath": 3,
    "GetDescription": 6,
    "SetDescription": 7,
    "GetWorkingDirectory": 8,
    "SetWorkingDirectory": 9,
    "GetArguments": 10,
    "SetArguments": 11,
    "GetIconLocation": 16,
    "SetIconLocation": 17,
    "SetPath": 20,
}

# IPersistFile vtable slots (IUnknown 0-2, IPersist::GetClassID 3):
#   4 IsDirty, 5 Load, 6 Save, 7 SaveCompleted, 8 GetCurFile
_IPERSISTFILE_SLOTS = {"Load": 5, "Save": 6}

_IUNKNOWN_QUERY_INTERFACE = 0
_IUNKNOWN_RELEASE = 2

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
# HRESULTs are signed 32-bit values in ctypes.
_RPC_E_CHANGED_MODE = ctypes.c_long(0x80010106).value
_STGM_READ = 0
_SLGP_RAWPATH = 0x4
_BUFFER_CHARS = 32768


class _GUID(ctypes.Structure):
    """Native ``GUID`` layout."""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """Build a native GUID from its canonical text form."""
    value = uuid.UUID(text)
    return _GUID(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (ctypes.c_ubyte * 8)(*value.bytes[8:]),
    )


def _hresult_error(hresult: int, path: Path) -> OSError:
    """Convert a failed ``HRESULT`` into an ``OSError`` naming *path*."""
    # ctypes wants signed HRESULTs; normalise whatever width the caller used.
    signed = ctypes.c_long(hresult & 0xFFFFFFFF).value
    code = signed & 0xFFFFFFFF
    # HRESULT_FROM_WIN32 wraps Win32 codes with facility 7; unwrap them so the
    # message and winerror match what the rest of Python reports.
    if (code >> 16) == 0x8007:
        signed = code & 0xFFFF
    message = ctypes.FormatError(signed).strip() or f"COM error 0x{code:08X}"
    return OSError(0, message, str(path), signed)


def _ole32() -> Any:
    """Return ``ole32`` with the prototypes used here."""
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    return ole32


@contextmanager
def _com_apartment(path: Path) -> Iterator[Any]:
    """Initialize COM on this thread for the duration of the block."""
    ole32 = _ole32()
    hresult = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if hresult == _RPC_E_CHANGED_MODE:
        # The thread already uses another apartment model; COM still works and
        # this call must not be balanced by CoUninitialize.
        initialized = False
    elif hresult < 0:
        raise _hresult_error(hresult, path)
    else:
        # S_OK and S_FALSE both add a reference that CoUninitialize releases.
        initialized = True
    try:
        yield ole32
    finally:
        if initialized:
            ole32.CoUninitialize()


def _method(pointer: ctypes.c_void_p, slot: int, *argtypes: Any) -> Callable[..., int]:
    """Return COM method number *slot* of *pointer* as a Python callable."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    function = prototype(vtable[slot])
    return lambda *args: function(pointer, *args)


def _release(pointer: ctypes.c_void_p) -> None:
    """Release one COM interface pointer."""
    if pointer.value:
        _method(pointer, _IUNKNOWN_RELEASE)()


def _check(hresult: int, path: Path) -> None:
    """Raise ``OSError`` when *hresult* reports failure."""
    if hresult < 0:
        raise _hresult_error(hresult, path)


@contextmanager
def _open_link(path: Path) -> Iterator[tuple[ctypes.c_void_p, ctypes.c_void_p]]:
    """Yield ``(IShortcutSpecW, IPersistFile)`` pointers for a new shell link."""
    with _com_apartment(path) as ole32:
        link = ctypes.c_void_p()
        persist = ctypes.c_void_p()
        try:
            clsid = _guid("00021401-0000-0000-C000-000000000046")  # CLSID_ShortcutSpec
            iid_link = _guid("000214F9-0000-0000-C000-000000000046")  # IID_IShortcutSpecW
            iid_persist = _guid("0000010B-0000-0000-C000-000000000046")  # IID_IPersistFile
            _check(
                ole32.CoCreateInstance(
                    ctypes.byref(clsid),
                    None,
                    _CLSCTX_INPROC_SERVER,
                    ctypes.byref(iid_link),
                    ctypes.byref(link),
                ),
                path,
            )
            query = _method(
                link,
                _IUNKNOWN_QUERY_INTERFACE,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )
            _check(query(ctypes.byref(iid_persist), ctypes.byref(persist)), path)
            yield link, persist
        finally:
            _release(persist)
            _release(link)


def _split_icon(icon_location: str) -> tuple[str, int]:
    """Split ``"path,index"`` on its last comma, as WScript.Shell does."""
    head, comma, tail = icon_location.rpartition(",")
    if comma:
        try:
            return head, int(tail.strip())
        except ValueError:
            pass
    return icon_location, 0
