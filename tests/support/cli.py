"""Run the real command line against a pretend machine and capture what it prints.

``CliWorld`` is the engine tests' ``World`` (three roots in a temporary folder, a ``FakeSystem``, published packages and
registries) plus ``cli(...)``, which calls ``gupkg.cli.main`` with the machine, an explicit environment and a stand-in
``RunningGupkg`` and returns the exit code with everything written to standard output and standard error. No ambient
environment variable is read: the environment mapping is built here, and the stop marker's temporary folder is inside
the test's own folder.

Usage and API
-------------
Use the ``cli_world`` and ``cli_elevated_world`` fixtures, or construct ``CliWorld``, and call ``cli(...)`` with the
words of a command line; ``CliResult.document()`` parses a ``--json`` document.

Implementation Approach
-----------------------
The command line runs in-process with streams, standard input, the environment and the running-gupkg description
replaced, so a call is deterministic and reaches only the pretend machine.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from gupkg import cli
from gupkg.selfmgmt.location import RunningGupkg
from tests.support.engine import World, pkg_text


class Terminal(io.StringIO):
    """A stream that claims to be a console (``isatty``) and holds what a person types, as when a person runs the command in a window.

    Methods
    -------
    isatty
        Reports a console.
    """

    def isatty(self) -> bool:
        """Report a console.

        Returns
        -------
        bool
            Always ``True``.
        """
        return True


class ForbiddenInput(io.StringIO):
    """A standard input that fails the test the moment anything reads it (a ``--yes`` or ``--no-input`` run must not ask).

    Methods
    -------
    read
        Fails the test.
    readline
        Fails the test.
    """

    def read(self, *args: Any) -> str:
        """Fail: nothing may read standard input.

        Parameters
        ----------
        *args : Any
            Ignored.

        Raises
        ------
        AssertionError
            Always; nothing may read standard input.
        """
        raise AssertionError("standard input was read")

    def readline(self, *args: Any) -> str:
        """Fail: nothing may read standard input.

        Parameters
        ----------
        *args : Any
            Ignored.

        Raises
        ------
        AssertionError
            Always; nothing may read standard input.
        """
        raise AssertionError("standard input was read")


@dataclass
class CliResult:
    """What one command-line call did: its exit code and the text it wrote to standard output and standard error.

    Methods
    -------
    document
        Parses the standard output as one JSON document.

    Attributes
    ----------
    code : int
        Exit code of the call.
    out : str
        Text written to standard output.
    err : str
        Text written to standard error.
    """

    code: int
    out: str
    err: str

    def document(self) -> dict[str, Any]:
        """Return the JSON document the call printed on standard output (it must be exactly one document).

        Returns
        -------
        dict[str, Any]
            The parsed document.
        """
        return json.loads(self.out)


class CliWorld(World):
    """A ``World`` that is driven through ``gupkg.cli.main``.

    Parameters
    ----------
    base : Path
        The temporary folder everything lives under.
    elevated : bool
        Whether the fake machine reports an elevated process.
    installed : bool
        Whether the stand-in gupkg is installed in the system root (the system gupkg).

    Methods
    -------
    cli
        Runs ``gupkg`` with the given arguments in process.
    """

    def __init__(self, base: Path, *, elevated: bool = False, installed: bool = True, **options: Any) -> None:
        super().__init__(base, elevated=elevated, **options)
        # Give the world a private TEMP folder and an explicit environment, so no ambient variable is read.
        self.temp = base / "temp"
        self.temp.mkdir()
        self.env = {**self.machine.env, "TEMP": str(self.temp)}
        # Build a stand-in gupkg tree, installed in the system root or a plain source tree.
        tree = base / "gupkg-tree" / "gupkg" / "1.0.0" / "App"
        (tree / "gupkg").mkdir(parents=True, exist_ok=True)
        (tree.parent / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
        self.location = (
            RunningGupkg("installed", tree / "gupkg", (tree,), tree.parent, self.system_root, self.machine.app)
            if installed else RunningGupkg("source", tree / "gupkg", (tree,))
        )

    def cli(self, *argv: str, stdin: str | None = "", location: RunningGupkg | None = None, tty: bool = False) -> CliResult:
        """Run ``gupkg <argv>`` and return its exit code and output; *stdin* is what a question would read (``None``: reading it fails the test); *tty* makes it look like a console.

        Parameters
        ----------
        *argv : str
            Words of the command line after ``gupkg``.
        stdin : str | None, optional
            Text that a question reads; ``None`` makes any read fail the test.
        location : RunningGupkg | None, optional
            Running-gupkg description to use instead of the world's own.
        tty : bool, optional
            Whether standard input looks like a console.

        Returns
        -------
        CliResult
            The exit code and the captured output.
        """
        # Replace standard input for the duration of the call; a question reads from *stdin*.
        out, err = io.StringIO(), io.StringIO()
        original = sys.stdin
        sys.stdin = ForbiddenInput() if stdin is None else (Terminal if tty else io.StringIO)(stdin)

        # The process PATH is pinned to a folder with nothing in it: a git or program on the developer's PATH must not decide a test.
        try:
            with mock.patch.dict(os.environ, {"PATH": str(self.temp / "no-programs")}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv), system=self.fake, env=self.env, location=location or self.location)
        finally:
            sys.stdin = original
        return CliResult(code, out.getvalue(), err.getvalue())


# Download address of the registry used by the command-line tests.
SOURCE = "https://example.org/main.zip"


def version_files(name: str, version: str) -> dict[str, str]:
    """Return the files of one registry version (a hashed download).

    Parameters
    ----------
    name : str
        Package name.
    version : str
        Version label.

    Returns
    -------
    dict[str, str]
        Registry files of one pinned version, keyed by path.
    """
    return {f"{name}/{version}/pkg.toml": pkg_text(f"https://example.org/{name}-{version}.zip", sha256="a" * 64)}
