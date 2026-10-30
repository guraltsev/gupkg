"""Prepare a UI program to run: the root options, the sibling CLI, the run's scope and its settings.

Both programs (``gupkg-tui`` and ``gupkg-manager``) start the same way. A run has one scope, the mode of its token: an elevated process manages
the system root and any other process the user's, and every call the program makes to the CLI runs in that scope. The root options
``--settings PATH``, ``--root DIR`` and ``--sandbox DIR`` are the CLI's own (a settings file as the highest layer, the root of the run, a pretend machine kept in a folder); the programs
read the settings with them and relay them to every call, which is how tests and trial runs act on temporary roots.

Usage and API
-------------
``add_root_options(parser)`` adds the three options to a program's parser; ``build_environment(args)`` returns the ``UiEnvironment`` (client,
settings, scope) or raises ``StartupError`` with the message to print and the exit code.

Implementation Approach
-----------------------
The machine boundary is the one the CLI uses, so a sandboxed program judges elevation and admin-only folders from the sandbox's state, never
from the real machine. The scope is built once, by ``gupkg.scope.build_scope`` inside ``load_ui_settings``, exactly as the CLI of the same gupkg
builds it. The client always runs the launcher of the program's own gupkg tree.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gupkg.model.scope import ModeRefused, Scope
from gupkg.model.settings import SettingsError
from gupkg.selfmgmt.location import RunningGupkg, locate_running_gupkg
from gupkg.system.fake import FakeSystem
from gupkg.system.port import System
from gupkg.system.windows import WindowsSystem

from .client import Client, ClientError, sibling_launcher
from .reads import Reads
from .settings import UiSettings, load_ui_settings


SHORT_ROOT = 40


def shorten_path(path: str, home: str | None = None, width: int = SHORT_ROOT) -> str:
    """Return *path* in at most *width* characters: the home folder as ``~``, and a long path with the middle replaced by ``…`` (the drive and the last folders stay)."""
    text = path
    base = (home or "").rstrip("\\")
    if base and text.casefold().startswith(base.casefold()) and text[len(base):len(base) + 1] in ("", "\\"):
        text = "~" + text[len(base):]
    if len(text) <= width:
        return text
    parts = text.split("\\")
    if text.startswith("\\\\") and len(parts) > 4:
        # A UNC path keeps ``\\server\share`` as its head: which share is managed must never be hidden.
        head, folders = "\\".join(parts[:4]), parts[4:]
    else:
        head, folders = parts[0], parts[1:]
    if not folders:
        return text[:width - 1] + "…"

    # Keep the drive, then as many of the last folders as fit after the ``…``.
    room = width - len(head) - len("\\…\\")
    tail = folders[-1:]
    for folder in reversed(folders[:-1]):
        if len("\\".join([folder, *tail])) > room:
            break
        tail.insert(0, folder)
    shown = "\\".join(tail)
    return f"{head}\\…\\{shown if len(shown) <= room else '…' + shown[len(shown) - room + 1:]}"


class StartupError(Exception):
    """Report that the program cannot start, with the exit code to return (2: the caller must change something)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UiEnvironment:
    """Everything a UI program needs to start working.

    Attributes
    ----------
    client : Client
        Runs the calls of the sibling ``gupkg``.
    settings : UiSettings
        The effective settings (``[manager]``, ``[cleanup]``) of the run's scope.
    location, system : object
        Where this gupkg lives and the machine boundary used for reading.
    env : mapping
        The environment of the program.
    reads : Reads, optional
        A replacement for the in-process reads (a test double); ``source`` builds the real ones from the scope and the machine.
    """

    client: Client
    settings: UiSettings
    location: RunningGupkg
    system: System
    env: Mapping[str, str]
    reads: Reads | None = None

    @property
    def source(self) -> Reads:
        """Return the in-process reads of the run's scope (the inventory, a package, the registry state); the field ``reads`` replaces them (tests)."""
        return self.reads if self.reads is not None else Reads(self.scope, self.system)

    @property
    def scope(self) -> Scope:
        """Return the run's scope: its mode (the mode of the process token) and its root."""
        return self.settings.scope

    @property
    def scope_label(self) -> str:
        """Return ``system · <root>`` or ``user · <root>``: what the title and the status line say about the run."""
        return f"{self.scope.mode} · {self.scope.root}"

    @property
    def scope_text(self) -> str:
        """Return the label with the root shortened to ``SHORT_ROOT`` characters (``user · ~\\opt``, ``system · C:\\opt``), for the status line."""
        return f"{self.scope.mode} · {shorten_path(str(self.scope.root), self.env.get('USERPROFILE'))}"


def add_root_options(parser: argparse.ArgumentParser) -> None:
    """Add ``--settings PATH``, ``--root DIR`` and ``--sandbox DIR`` to *parser*; they are relayed to every ``gupkg`` call."""
    parser.add_argument("--settings", type=Path, metavar="PATH", help="read this settings file as the highest layer (relayed to gupkg)")
    parser.add_argument("--root", type=Path, metavar="DIR", help="the root of this run (the first source of the root chain; system mode needs an admin-only root; relayed to gupkg)")
    parser.add_argument(
        "--sandbox", type=Path, metavar="DIR",
        help="act on a pretend machine kept in DIR instead of the real one (relayed to gupkg; files and downloads stay real)",
    )


def build_environment(
    args: argparse.Namespace,
    *,
    location: RunningGupkg | None = None,
    system: System | None = None,
    env: Mapping[str, str] | None = None,
    command: Sequence[str] | None = None,
    reads: Reads | None = None,
) -> UiEnvironment:
    """Build the environment of a UI program from its parsed root options.

    Parameters
    ----------
    args : Namespace
        The parsed options; ``settings``, ``root`` and ``sandbox`` are read when present.
    location : RunningGupkg, optional
        Where this gupkg lives; found from the loaded packages by default.
    system : System, optional
        The machine boundary; the real one (or the sandbox over it) by default.
    env : mapping, optional
        The environment; ``os.environ`` by default.
    command : sequence of str, optional
        The program that stands for ``gupkg``; the sibling ``gupkg.cmd`` by default. Tests pass a double.
    reads : Reads, optional
        A replacement for the in-process reads; tests pass a double that serves recorded documents.

    Raises
    ------
    StartupError
        If ``--root`` names a folder that does not exist or is not a folder, the sibling ``gupkg`` is missing, the settings cannot be read, or an elevated run's gupkg tree is not admin-only.
    """
    where = location or locate_running_gupkg()
    environment = dict(os.environ if env is None else env)
    machine = system or WindowsSystem()
    sandbox, settings_path, root_option = getattr(args, "sandbox", None), getattr(args, "settings", None), getattr(args, "root", None)
    if root_option is not None and not root_option.is_dir():
        # A given ``--root`` must already be a folder (nothing is created); the default and configured roots may be missing on a first run.
        raise StartupError(f"--root {root_option} is not a folder" if root_option.exists() else f"--root {root_option} does not exist (create the folder first)")
    if sandbox is not None:
        sandbox.mkdir(parents=True, exist_ok=True)
        machine = FakeSystem.sandbox(sandbox, machine)

    # The command is always the launcher of this gupkg tree; the root options travel to every call.
    try:
        program = tuple(command) if command is not None else (str(sibling_launcher(where)),)
    except ClientError as error:
        raise StartupError(str(error)) from error
    options = (*(("--settings", str(settings_path)) if settings_path is not None else ()), *(("--root", str(root_option)) if root_option is not None else ()), *(("--sandbox", str(sandbox)) if sandbox is not None else ()))
    try:
        settings = load_ui_settings(settings_path=settings_path, root_option=root_option, env=environment, location=where, system=machine)
    except ModeRefused as error:
        raise StartupError(str(error)) from error
    except (SettingsError, OSError, ValueError) as error:
        raise StartupError(f"cannot read the settings: {error}") from error
    return UiEnvironment(Client(program, options, environment), settings, where, machine, environment, reads)
