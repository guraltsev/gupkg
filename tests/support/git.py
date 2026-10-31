"""A scripted ``git`` for the tests of the Git provider, the Git unpack kind and the Git checksum source.

``FakeGit`` is a process double that answers ``ls-remote``, ``fetch``, ``log``, ``rev-parse`` and ``checkout`` like Git and
records every command line; ``git_machine`` returns a ``FakeSystem`` whose only process is that double.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult

COMMIT = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40
EPOCH = 1728720930  # 2024-10-12 08:15:30 UTC


def subcommand(argv: tuple[str, ...]) -> list[str]:
    """Return the arguments after `git` and its `-c name=value` settings."""
    words = list(argv[1:])
    while words[:1] == ["-c"]:
        words = words[2:]
    return words


class FakeGit:
    """A scripted ``git``: records calls and answers `ls-remote`, `fetch`, `log`, `rev-parse` and `checkout`."""

    def __init__(self, *, served: str = COMMIT, listed: str = COMMIT, files: dict[str, str] | None = None) -> None:
        """Configure the commit that `fetch` serves, the one `ls-remote` lists, and the working tree `checkout` creates."""
        self.served, self.listed = served, listed
        self.files = files if files is not None else {"src/main.py": "print()", "README": "r"}
        self.calls: list[ProcessCall] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Answer one git command."""
        self.calls.append(call)
        words = subcommand(call.argv)
        if words[0] == "ls-remote":
            return ProcessResult(0, f"{self.listed}\t{words[-1]}\n") if self.listed else ProcessResult(2)
        if words[0] == "log":
            return ProcessResult(0, f"{EPOCH}\n")
        if words[0] == "rev-parse":
            return ProcessResult(0, f"{self.served}\n")
        if words[0] == "checkout":
            for name, text in self.files.items():
                path = call.cwd / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (call.cwd / ".git").mkdir()
            (call.cwd / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        return ProcessResult(0)

    def words(self) -> list[str]:
        """Return the first word after `git` of each recorded call."""
        return [subcommand(call.argv)[0] for call in self.calls]


def git_machine(tmp_path: Path, git: FakeGit) -> FakeSystem:
    """Return a FakeSystem whose only process is the scripted git."""
    return FakeSystem(tmp_path / "machine", processes={"git": git}, real_7zip=False)
