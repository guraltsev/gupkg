"""The command line of ``gupkg-manager``: the browser, and the batch commands that schedules and scripts run.

``gupkg-manager`` (or ``gupkg-manager tui``) opens the browser over every package. ``gupkg-manager check-updates --all`` writes and populates the
new versions of every package and activates nothing. ``gupkg-manager update --all [--mode M] [--yes]`` plans an update of every package that has
a newer known version, asks once (``--yes`` answers), applies the ``[manager] checksum_missing`` policy per package and runs it grouped by mode:
one non-elevated call and one call for all system targets, which the CLI runs in a single elevated child. ``gupkg-manager check [--fix]`` is the
health check. All of them take ``--json`` for one result document on standard output (human text then goes to standard error). Everything the
manager does to a package is a call to the ``gupkg`` of the same gupkg tree.

Usage and API
-------------
Call ``main(argv)`` from Python or run ``gupkg-manager``. Exit codes follow the CLI (0 ok, 2 usage or configuration, 3 an operation failed or was
refused, 4 internal error, 5 declined or input needed, 6 a newer gupkg is needed): a batch returns the highest code of its targets.
``--settings PATH`` and ``--sandbox DIR`` (before the command) are relayed to every ``gupkg`` call.

Implementation Approach
-----------------------
Each command reads the inventory, builds a batch (``gupkg_manager.batch``), and runs it through a ``BatchSession`` whose sink prints to the
console. A person at a terminal is asked once for the plan and once per package whose checksum is missing (policy ``ask``); a schedule (``--yes``, no
terminal, ``--json``) is never asked, so ``ask`` behaves as ``refuse`` and a plan that cannot be confirmed is exit code 5 with nothing changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from gupkg.report.results import dumps
from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.port import System
from gupkg_ui.client import ClientError
from gupkg_ui.interaction import Interaction
from gupkg_ui.console import ConsoleSink, closing_text, configure_streams, console_ask_checksum, console_confirm, outcome_document
from gupkg_ui.session import EXIT_FAILED, EXIT_INPUT, BatchOutcome, BatchSession
from gupkg_ui.startup import StartupError, UiEnvironment, add_root_options, build_environment

from .app.app import ManagerApp
from .batch import check_all, check_updates_all, load_inventory, pick, update_all

PROGRAM = "gupkg-manager"


def main(
    argv: Sequence[str] | None = None,
    *,
    location: RunningGupkg | None = None,
    system: System | None = None,
    env: Mapping[str, str] | None = None,
    command: Sequence[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one ``gupkg-manager`` command line and return its exit code.

    Parameters
    ----------
    argv : sequence of str, optional
        The arguments without the program name; ``sys.argv[1:]`` by default.
    location, system, env : optional
        Where this gupkg lives, the machine boundary and the environment; the real ones by default.
    command : sequence of str, optional
        The program that stands for ``gupkg``; the sibling ``gupkg.cmd`` by default. Tests pass a double.
    stdin, stdout, stderr : TextIO, optional
        The standard streams; the process's by default.

    Returns
    -------
    int
        The exit code (0 ok, 2 usage or configuration, 3 failed or refused, 4 internal, 5 declined or input needed).
    """
    out, err, source = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    configure_streams(out, err)
    parser = _parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:
        return int(stop.code or 0)

    # Start: refuse an elevated shell, find the sibling gupkg, read the settings.
    try:
        environment = build_environment(args, location=location, system=system, env=env, command=command)
    except StartupError as error:
        print(f"{PROGRAM}: error: {error}", file=err)
        return error.code
    if args.command in (None, "tui"):
        return _tui(environment, source, out, err)
    if args.command != "check" and not args.all:
        print(f"{PROGRAM}: error: {args.command} needs --all; name packages with `gupkg {args.command} NAME`", file=err)
        return 2

    # The batch commands run on the event loop of this process; every call is a child process.
    return asyncio.run(_batch_command(args, environment, source, out, err))


# ---------------------------------------------------------------------------
# The batch commands
# ---------------------------------------------------------------------------


async def _batch_command(args: argparse.Namespace, environment: UiEnvironment, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Run ``check-updates``, ``update`` or ``check`` on every package and print the result; return the exit code."""
    # Human text goes to standard error when standard output carries the document.
    stream = stderr if args.json else stdout
    sink = ConsoleSink(stream)
    command = args.command
    tty = bool(getattr(stdin, "isatty", lambda: False)()) and bool(getattr(stream, "isatty", lambda: False)())
    can_ask = tty and not args.yes and not args.json
    if command == "update":
        # ``--yes`` already answers the plan; a person at a terminal is asked; anyone else cannot confirm, so nothing runs.
        confirm = None if args.yes else (console_confirm(stdin, stream) if can_ask else _no_answer(stream, command))
        interaction = Interaction(confirm=confirm, ask_checksum=console_ask_checksum(stdin, stream) if can_ask else None, interactive=can_ask)
    else:
        interaction = Interaction(ask_checksum=console_ask_checksum(stdin, stream) if can_ask else None, interactive=can_ask)

    # Inventory: one read-only call; without it nothing can be planned.
    try:
        rows = pick(await load_inventory(environment), command, mode=args.mode)
    except ClientError as error:
        print(f"{PROGRAM}: error: the package list could not be read: {error}", file=stderr)
        return EXIT_FAILED
    if not rows:
        print("No packages." if command == "check" else f"No packages to {'update' if command == 'update' else 'check for updates'}.", file=stream)
        if args.json:
            stdout.write(dumps(outcome_document(command, BatchOutcome())))
        return 0

    session = BatchSession(environment.client, sink)
    if command == "update":
        outcome = await update_all(environment, rows, session=session, sink=sink, interaction=interaction)
    elif command == "check-updates":
        outcome = await check_updates_all(environment, rows, session=session, interaction=interaction, options=["--no-download"] if args.no_download else [])
    else:
        outcome = await _check(args, environment, rows, session, stdin, stream)

    # The verdict is already the end of the printed log; the document (if asked for) goes to standard output.
    if args.json:
        stdout.write(dumps(outcome_document(command, outcome)))
    return outcome.exit_code


def _no_answer(stream: TextIO, command: str):
    """Return the confirmation of a run nobody can be asked about: it declines and says how to run it without a question."""
    def decline(lines, question: str) -> bool:
        """Say that nothing was confirmed and decline."""
        print(f"{question} not confirmed: there is nobody to ask. Pass --yes to run it without a question; nothing was changed.", file=stream)
        return False
    return decline


async def _check(args: argparse.Namespace, environment: UiEnvironment, rows, session: BatchSession, stdin: TextIO, stream: TextIO) -> BatchOutcome:
    """Run the health check (``--fix`` repairs: it asks once, or ``--yes`` answers)."""
    if args.fix:
        tty = bool(getattr(stdin, "isatty", lambda: False)())
        if not args.yes and not (tty and not args.json and console_confirm(stdin, stream)([], f"Repair {len(rows)} package{'s' if len(rows) != 1 else ''}?")):
            print("not confirmed: pass --yes to repair without a question; nothing was changed", file=stream)
            return BatchOutcome(exit_code=EXIT_INPUT)
    return await check_all(environment, rows, session=session, fix=args.fix)


# ---------------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------------


def _tui(environment: UiEnvironment, stdin: TextIO, stdout: TextIO, stderr: TextIO) -> int:
    """Open the browser; print the last run's summary and log path when it closes (``[manager] keep_screen``)."""
    if not (getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)()):
        print(f"{PROGRAM}: error: the browser needs a terminal; use `{PROGRAM} update --all --yes` or `check-updates --all` in scripts", file=stderr)
        return 2
    app = ManagerApp(environment)
    app.run()
    if environment.settings.keep_screen and app.last_outcome is not None:
        for line in closing_text(app.last_outcome):
            print(line, file=stdout)
    return int(app.return_code or 0)


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """Build the grammar: root options, then ``tui``, ``check-updates --all``, ``update --all`` or ``check``."""
    parser = argparse.ArgumentParser(prog=PROGRAM, description="The general gupkg manager: a browser over every package, and batch commands for schedules.")
    add_root_options(parser)
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    commands.add_parser("tui", help="Open the browser (the default).", description="Open the browser (the default).")

    for name, text in (
        ("check-updates", "Write and populate the new version of every package; activate nothing."),
        ("update", "Update every package that has a newer known version."),
        ("check", "Health check of every package (drift, App fingerprint, unverified installs)."),
    ):
        leaf = commands.add_parser(name, help=text, description=text)
        if name != "check":
            leaf.add_argument("--all", action="store_true", help="act on every package (required)")
        leaf.add_argument("--mode", choices=("system", "user", "portable"), help="only the packages of this install mode")
        leaf.add_argument("--json", action="store_true", help="print one JSON result document on standard output; human text goes to standard error")
        leaf.add_argument("--yes", action="store_true", help="answer the confirmation (and run without any question)")
        if name == "check-updates":
            leaf.add_argument("--no-download", action="store_true", help="write the version directories only; download nothing")
        if name == "check":
            leaf.add_argument("--fix", action="store_true", help="re-apply the integration and rebuild a missing App")
    parser.set_defaults(all=False, json=False, yes=False, mode=None, no_download=False, fix=False)
    return parser
