"""Hold what every lifecycle operation needs: the machine, the environment and the run's callbacks.

An ``Engine`` is built once per call by the front end. It carries the ``System`` boundary, the
environment used to expand variables, the loader of the effective settings, and the optional hooks a front
end plugs in: how to ask a question (``confirm``), how to learn that the user asked to stop (``cancel``), where
a package's log file is (``log_path``), and the facts about the running gupkg that elevation needs. Nothing
here prints or reads input: a question is a callable that the front end provides, and an engine without one
treats every question as unanswerable (exit code 5).

Usage and API
-------------
Create ``Engine(system=..., env=..., load=...)`` and pass it to the functions of ``gupkg.engine.lifecycle``.
``Engine.http_client(...)`` builds the shared HTTP client for a mode; ``Engine.now()`` is the clock.

Implementation Approach
-----------------------
The engine is a plain mutable dataclass so tests can adjust one field. It decides nothing itself: the
admin-only checks of an elevated run live in ``require_work_folder`` and ``Engine.http_client``, which create
the cache and work folders first and then refuse them unless only administrators can change them.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .._version import __version__
from ..extractors import find_7z
from ..model.mode import ModeContext
from ..modes import SettingsLoader
from ..services.http import HttpClient, github_environment
from ..services.unpack import Limits
from ..system.port import System, is_elevated_run, require_admin_only


# The largest App whose files are listed one by one in a receipt (about 150 bytes each, under the receipt size limit).
MAX_RECORDED_FILES = 4000

# The variables an elevated run lets a package's ``${NAME}`` see: the machine's own, never the user's (D19).
MACHINE_VARIABLES = frozenset(name.casefold() for name in (
    "SystemRoot", "SystemDrive", "WINDIR", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
    "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432", "ALLUSERSPROFILE", "PUBLIC", "COMSPEC",
    "PATHEXT", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
))


@dataclass
class Engine:
    """Everything a lifecycle call needs besides its requests.

    Parameters
    ----------
    system : System
        The machine boundary.
    env : mapping
        The environment for ``${NAME}`` expansion and the HTTP client's token; never read from ``os.environ`` here.
    load : callable
        ``load(system_mode=False, portable_root=None)`` returns the loaded settings (``gupkg.modes.settings_loader``).
    confirm : callable, optional
        Asks a yes-or-no question and returns the answer; ``None`` when nobody can be asked.
    yes, no_input : bool
        ``--yes`` answers every question with yes; ``--no-input`` turns every question into exit code 5.
    cancel : callable
        Returns true once the user asked to stop; checked between packages.
    log_path : callable
        ``log_path(package_path)`` returns the package's log file of this run (or ``None``); stored in receipts.
    relaunched : bool
        This process is the elevated child of a non-elevated call.
    argv, gupkg_dir, gupkg_root : optional
        The command line for an elevated child and the running gupkg's version directory and root.
    relaunch_note : str
        Added to the message when the wait for the elevated child is given up (its stop marker and events file).
    request_stop : callable, optional
        Asks the elevated child to stop (creates the child's stop marker); called when the user interrupts the wait for it.
    seven_zip : Path, optional
        The 7-Zip executable (the bundled one by default).
    limits : Limits
        Bounds on unpacked archives.
    sleep : callable
        Replaces ``time.sleep`` in the HTTP client (tests do not wait).
    retries : int, optional
        Overrides the HTTP client's retry count.
    clock : callable, optional
        Returns the current UTC time.
    installer_timeout : float
        Seconds a vendor installer may run before it is killed (design 8.4).
    replace : callable
        ``replace(source, destination)`` is the atomic rename that puts a finished ``App`` in place (``os.replace``); a test
        injects a failing one to exercise the restore of the previous ``App``.
    max_recorded_files : int
        The largest ``App`` whose files are listed one by one in a receipt (larger ones record the count and one digest).
    """

    system: System
    env: Mapping[str, str]
    load: SettingsLoader
    confirm: Callable[[str], bool] | None = None
    yes: bool = False
    no_input: bool = False
    cancel: Callable[[], bool] = lambda: False
    log_path: Callable[[Path], str | None] = lambda path: None
    relaunched: bool = False
    argv: tuple[str, ...] = ()
    gupkg_dir: Path | None = None
    gupkg_root: Path | None = None
    request_stop: Callable[[], None] | None = None
    relaunch_note: str = ""
    seven_zip: Path | None = None
    limits: Limits = field(default_factory=Limits)
    sleep: Callable[[float], None] = time.sleep
    retries: int | None = None
    clock: Callable[[], datetime] | None = None
    installer_timeout: float = 1800.0
    replace: Callable[[Path, Path], None] = os.replace
    max_recorded_files: int = MAX_RECORDED_FILES

    def now(self) -> datetime:
        """Return the current UTC time."""
        return self.clock() if self.clock is not None else datetime.now(timezone.utc)

    @property
    def elevated(self) -> bool:
        """Return whether this run is elevated (unknown counts as elevated)."""
        return is_elevated_run(self.system)

    @property
    def version(self) -> str:
        """Return the running gupkg's version."""
        return __version__

    @property
    def effect_env(self) -> Mapping[str, str]:
        """Return the environment that ``${NAME}`` references in a package's declarations resolve against.

        An elevated run ignores the user's environment (decision D19): only the machine-level variables of
        ``MACHINE_VARIABLES`` are visible to a package then. A normal run sees everything it was given.
        """
        if not self.elevated:
            return self.env
        return {name: value for name, value in self.env.items() if name.casefold() in MACHINE_VARIABLES}

    def seven_zip_path(self) -> Path | None:
        """Return the 7-Zip executable to use, or ``None`` when none is found (an archive then fails with its own message)."""
        if self.seven_zip is not None:
            return self.seven_zip
        try:
            return find_7z()
        except FileNotFoundError:
            return None

    def http_client(self, context: ModeContext, headers: Mapping[str, str] | None = None) -> HttpClient:
        """Build the shared HTTP client whose download cache is the mode's ``<root>\\.gupkg\\cache``.

        In an elevated run the cache folder must be admin-only (it is created first, then judged), so a
        standard user cannot plant a file that is later unpacked.

        Raises
        ------
        gupkg.selfmgmt.elevation.UntrustedLocationError
            If the run is elevated and the cache folder can be changed by standard users.
        """
        cache = context.data_dir / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        if self.elevated:
            require_admin_only(self.system, cache)
        options = {} if self.retries is None else {"retries": self.retries}
        return HttpClient(
            self.system, headers=dict(headers or {}), cache_dir=cache, env=github_environment(self.system, self.env),
            sleep=self.sleep, **options,
        )


def require_trusted_tools(engine: Engine, *tools: Path | None) -> None:
    """Refuse, in an elevated run, to start a program whose folder standard users can change; check *before* launching it.

    An elevated gupkg runs no code that a standard user can modify (decision D19): the interpreter that starts the regex
    worker, the worker script and 7-Zip must all lie in admin-only folders. A normal run is not restricted.

    Raises
    ------
    gupkg.selfmgmt.elevation.UntrustedLocationError
        If the run is elevated and the folder of a tool is not admin-only (or cannot be judged).
    """
    if not engine.elevated:
        return
    for tool in tools:
        if tool is not None:
            require_admin_only(engine.system, Path(tool).parent)


def tracker_tools() -> tuple[Path, ...]:
    """Return the files an elevated ``check-updates`` starts: the running interpreter and the regex worker script."""
    return Path(sys.executable), Path(__file__).resolve().parents[1] / "system" / "regex_worker.py"


def stop_marker(path: Path) -> Callable[[], bool]:
    """Return a ``cancel`` callable that is true once the file *path* exists (decision D37).

    The marker is data a user-writable program may create: it is only ever read for its existence, and the
    only thing it can do is make the run stop after the package that is running, so it is safe in an elevated run.

    Notes
    -----
    The caller (issue 029) must give each run its own marker path (a name with a fresh random part, in a folder only the
    run and its front end use) and must never reuse one: a marker file that already exists when the run starts makes
    every package of the run ``skipped``. A front end that cancels creates the file; it never leaves one behind.
    """
    marker = Path(path)
    return lambda: os.path.lexists(marker)


def require_work_folder(engine: Engine, folder: Path) -> Path:
    """Create *folder* and, in an elevated run, require it to be admin-only; return it.

    Raises
    ------
    gupkg.selfmgmt.elevation.UntrustedLocationError
        If the run is elevated and standard users can change *folder*.
    """
    folder.mkdir(parents=True, exist_ok=True)
    if engine.elevated:
        require_admin_only(engine.system, folder)
    return folder
