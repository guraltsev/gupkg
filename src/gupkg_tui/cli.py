"""The command line of ``gupkg-tui``: open the package view of one package.

``gupkg-tui PATH|NAME`` shows one package (a folder such as the version directory a package launcher passes, or a name) and acts on it. The
program needs a terminal; every operation is a call to the ``gupkg`` of the same gupkg tree. ``gupkg tui PATH`` forwards here. When the program closes, the
summary of the last run and the log path are printed to the console (``[manager] keep_screen``).

Usage and API
-------------
Call ``main(argv)`` from Python or run ``gupkg-tui``. ``--settings PATH``, ``--root DIR`` and ``--sandbox DIR`` are relayed to every ``gupkg`` call. Exit code: 0 after a normal
close, 2 for a usage or configuration problem.

Implementation Approach
-----------------------
A folder given as the target is made absolute (a relative name would depend on the folder the program was started in); a hostile name is refused before
anything starts.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.port import System
from gupkg_ui.client import ClientError, refuse_unsafe
from gupkg_ui.console import closing_text, configure_streams
from gupkg_ui.startup import StartupError, add_root_options, build_environment

from .app import TuiApp

PROGRAM = "gupkg-tui"


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
    """Open the package view of the named package and return the exit code.

    Parameters
    ----------
    argv : sequence of str, optional
        The arguments without the program name; ``sys.argv[1:]`` by default.
    location, system, env, command : optional
        Where this gupkg lives, the machine boundary, the environment and the program standing for ``gupkg`` (tests pass doubles).
    stdin, stdout, stderr : TextIO, optional
        The standard streams; the process's by default.
    """
    out, err, source = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    configure_streams(out, err)
    parser = argparse.ArgumentParser(prog=PROGRAM, description="Show one gupkg package and act on it.")
    add_root_options(parser)
    parser.add_argument("target", metavar="PATH|NAME", help="a package folder or version directory, or a package name")
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as stop:
        return int(stop.code or 0)

    # A folder is made absolute; anything the launcher could misread is refused before the program starts.
    target = os.path.abspath(args.target) if os.path.exists(args.target) else args.target
    try:
        refuse_unsafe([target])
    except ClientError as error:
        print(f"{PROGRAM}: error: {error}", file=err)
        return 2
    try:
        environment = build_environment(args, location=location, system=system, env=env, command=command)
    except StartupError as error:
        print(f"{PROGRAM}: error: {error}", file=err)
        return error.code
    if not (getattr(source, "isatty", lambda: False)() and getattr(out, "isatty", lambda: False)()):
        print(f"{PROGRAM}: error: the package view needs a terminal; use `gupkg info {args.target}` in scripts", file=err)
        return 2

    app = TuiApp(environment, target)
    app.run()
    if environment.settings.keep_screen and app.last_outcome is not None:
        for line in closing_text(app.last_outcome):
            print(line, file=out)
    return int(app.return_code or 0)
