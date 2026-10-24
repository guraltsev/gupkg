"""Run the real command line against a pretend machine and read what it prints.

``CliWorld`` is the engine tests' ``World`` (three roots in a temporary folder, a ``FakeSystem``, published packages and
registries) plus ``cli(...)``, which calls ``gupkg.cli.main`` with the machine, an explicit environment and a stand-in
``RunningGupkg`` and returns the exit code with everything written to standard output and standard error. No ambient
environment variable is read: the environment mapping is built here, and the stop marker's temporary folder is inside
the test's own folder.
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
from tests.engine_support import World


class TerminalInput(io.StringIO):
    """A standard input that claims to be a console (``isatty``), as when a person runs the command in a window."""

    def isatty(self) -> bool:
        """Report a console."""
        return True


class ForbiddenInput(io.StringIO):
    """A standard input that fails the test the moment anything reads it (a ``--yes`` or ``--no-input`` run must not ask)."""

    def read(self, *args: Any) -> str:
        """Fail: nothing may read standard input."""
        raise AssertionError("standard input was read")

    def readline(self, *args: Any) -> str:
        """Fail: nothing may read standard input."""
        raise AssertionError("standard input was read")


@dataclass
class CliResult:
    """What one command-line call did: its exit code and the text it wrote to standard output and standard error."""

    code: int
    out: str
    err: str

    def document(self) -> dict[str, Any]:
        """Return the JSON document the call printed on standard output (it must be exactly one document)."""
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
        Whether the stand-in gupkg is installed in the system root (a system gupkg that may relaunch itself elevated).
    """

    def __init__(self, base: Path, *, elevated: bool = False, installed: bool = True, **options: Any) -> None:
        super().__init__(base, elevated=elevated, **options)
        self.temp = base / "temp"
        self.temp.mkdir()
        self.env = {**self.machine.env, "TEMP": str(self.temp)}
        tree = base / "gupkg-tree" / "gupkg" / "1.0.0" / "App"
        (tree / "gupkg").mkdir(parents=True, exist_ok=True)
        (tree.parent / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
        self.location = (
            RunningGupkg("installed", tree / "gupkg", (tree,), tree.parent, self.system_root, self.machine.app)
            if installed else RunningGupkg("source", tree / "gupkg", (tree,))
        )

    def cli(self, *argv: str, stdin: str | None = "", location: RunningGupkg | None = None, tty: bool = False) -> CliResult:
        """Run ``gupkg <argv>`` and return its exit code and output; *stdin* is what a question would read (``None``: reading it fails the test); *tty* makes it look like a console."""
        out, err = io.StringIO(), io.StringIO()
        original = sys.stdin
        sys.stdin = ForbiddenInput() if stdin is None else (TerminalInput if tty else io.StringIO)(stdin)

        # The process PATH is pinned to a folder with nothing in it: a git or program on the developer's PATH must not decide a test.
        try:
            with mock.patch.dict(os.environ, {"PATH": str(self.temp / "no-programs")}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv), system=self.fake, env=self.env, location=location or self.location)
        finally:
            sys.stdin = original
        return CliResult(code, out.getvalue(), err.getvalue())
