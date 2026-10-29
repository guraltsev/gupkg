"""Prepare a UI program to run: the root options, the sibling CLI, the settings and whether this gupkg may ask for administrator rights.

Both programs (``gupkg-tui`` and ``gupkg-manager``) start the same way. They are standard-user programs that call the elevated CLI,
so a process that already holds the administrator token refuses to start. The root options ``--settings PATH`` and ``--sandbox DIR`` are
the CLI's own (a settings file as the highest layer, a pretend machine kept in a folder); the programs read the settings with them and relay
them to every call, which is how tests and trial runs act on temporary roots.

Usage and API
-------------
``add_root_options(parser)`` adds the two options to a program's parser; ``build_environment(args)`` returns the ``UiEnvironment`` (client,
settings, elevation ability) or raises ``StartupError`` with the message to print and the exit code.

Implementation Approach
-----------------------
The machine boundary is the one the CLI uses, so a sandboxed program judges elevation and admin-only folders from the sandbox's state, never
from the real machine. The client always runs the launcher of the program's own gupkg tree.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gupkg.model.settings import SettingsError
from gupkg.selfmgmt.location import RunningGupkg, locate_running_gupkg
from gupkg.system.fake import FakeSystem
from gupkg.scope import process_mode
from gupkg.system.port import Mode, System
from gupkg.system.windows import WindowsSystem

from .client import Client, ClientError, sibling_launcher
from .settings import UiSettings, load_ui_settings
from .steps import can_elevate


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
        The effective settings (``[manager]``, ``[cleanup]``, roots).
    can_elevate : bool
        Whether this gupkg is the system gupkg and may ask for administrator rights (one prompt per batch).
    location, system : object
        Where this gupkg lives and the machine boundary used for reading.
    env : mapping
        The environment of the program.
    """

    client: Client
    settings: UiSettings
    can_elevate: bool
    location: RunningGupkg
    system: System
    env: Mapping[str, str]


def add_root_options(parser: argparse.ArgumentParser) -> None:
    """Add ``--settings PATH`` and ``--sandbox DIR`` to *parser*; they are relayed to every ``gupkg`` call."""
    parser.add_argument("--settings", type=Path, metavar="PATH", help="read this settings file as the highest layer (relayed to gupkg)")
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
) -> UiEnvironment:
    """Build the environment of a UI program from its parsed root options.

    Parameters
    ----------
    args : Namespace
        The parsed options; ``settings`` and ``sandbox`` are read when present.
    location : RunningGupkg, optional
        Where this gupkg lives; found from the loaded packages by default.
    system : System, optional
        The machine boundary; the real one (or the sandbox over it) by default.
    env : mapping, optional
        The environment; ``os.environ`` by default.
    command : sequence of str, optional
        The program that stands for ``gupkg``; the sibling ``gupkg.cmd`` by default. Tests pass a double.

    Raises
    ------
    StartupError
        If the process is elevated, the sibling ``gupkg`` is missing, or the settings cannot be read.
    """
    where = location or locate_running_gupkg()
    environment = dict(os.environ if env is None else env)
    machine = system or WindowsSystem()
    sandbox, settings_path = getattr(args, "sandbox", None), getattr(args, "settings", None)
    if sandbox is not None:
        sandbox.mkdir(parents=True, exist_ok=True)
        machine = FakeSystem.sandbox(sandbox, machine)

    # The programs call the elevated CLI themselves and ask for administrator rights once; running them elevated is refused.
    if process_mode(machine) is Mode.SYSTEM:
        raise StartupError("this program is not started from an elevated shell: it calls gupkg and asks for administrator rights itself. Run it from a normal shell.")

    # The command is always the launcher of this gupkg tree; the root options travel to every call.
    try:
        program = tuple(command) if command is not None else (str(sibling_launcher(where)),)
    except ClientError as error:
        raise StartupError(str(error)) from error
    options = (*(("--settings", str(settings_path)) if settings_path is not None else ()), *(("--sandbox", str(sandbox)) if sandbox is not None else ()))
    try:
        settings = load_ui_settings(settings_path=settings_path, env=environment, location=where, system=machine)
    except (SettingsError, OSError, ValueError) as error:
        raise StartupError(f"cannot read the settings: {error}") from error
    return UiEnvironment(Client(program, options, environment), settings, can_elevate(where, machine), where, machine, environment)
