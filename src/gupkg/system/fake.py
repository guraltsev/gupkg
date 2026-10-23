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
no fixture to the real ``delegate``; processes never go there (only ``git`` and 7-Zip run for real).

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .port import EnvValue, ProcessResult, Scope, ScopeFolders, ShortcutSpec, System
from .windows import WindowsLinks, WindowsSystem


class UnexpectedCall(OSError):
    """Report a URL, process or action that the fake was not set up to serve."""


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
        What ``relaunch_elevated`` reports.
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
    failures: dict[str, BaseException] = field(default_factory=dict)
    registry_log: list[RegistryAccess] = field(default_factory=list)
    broadcasts: int = 0
    relaunches: list[list[str]] = field(default_factory=list)
    fetch_log: list[str] = field(default_factory=list)
    process_log: list[list[str]] = field(default_factory=list)
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
        # Processes stay on an allow-list: git (a package may clone its origin) and,
        # through ``real_7zip``, 7-Zip. Any other program raises ``UnexpectedCall``.
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

        Each name joins the process allow-list as a pass-through double; any
        other program still raises ``UnexpectedCall``.
        """
        real = WindowsSystem()
        for program in programs:
            self.processes[program.lower()] = lambda call, real=real: real.run(
                call.argv, call.cwd, call.env, None, input_text=call.input_text
            )

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

    def relaunch_elevated(self, argv: Sequence[str]) -> bool:
        """Record the request and report the configured outcome; nothing is started."""
        self._maybe_fail("relaunch_elevated")
        self.relaunches.append([str(part) for part in argv])
        self._save()
        return self.relaunch_result

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
            if on_line is not None:
                for line in (result.stdout + result.stderr).splitlines():
                    on_line(line)
            return result

        # 7-Zip may really run when it exists, since extraction touches only files.
        if self.real_7zip and program.stem.lower() in {"7z", "7za", "7zr"} and (
            program.is_file() or shutil.which(command[0])
        ):
            return (self.delegate or WindowsSystem()).run(
                command, cwd, env, on_line, input_text=input_text, timeout=timeout
            )
        raise UnexpectedCall(f"FakeSystem has no process double for {command[0]!r}: {command}")

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

    # -- internals -----------------------------------------------------------------

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
