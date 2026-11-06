"""A scripted ``git`` for the tests of the Git provider, the Git unpack kind and the Git checksum source.

``FakeGit`` is a process double that answers ``ls-remote``, ``fetch``, ``log``, ``rev-parse`` and ``checkout`` like Git and
records every command line; ``git_machine`` returns a ``FakeSystem`` whose only process is that double.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult

# Commit hash that the scripted git serves and lists by default.
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# A second, different commit hash for tests that need the served and listed commits to disagree.
OTHER = "f" * 40
# Commit time of every commit, in epoch seconds.
EPOCH = 1728720930  # 2024-10-12 08:15:30 UTC


def subcommand(argv: tuple[str, ...]) -> list[str]:
    """Return the arguments after `git` and its `-c name=value` settings.

    Parameters
    ----------
    argv : tuple[str, ...]
        Full command line, beginning with ``git``.

    Returns
    -------
    list[str]
        The arguments after ``git`` and its ``-c name=value`` settings.
    """
    words = list(argv[1:])
    while words[:1] == ["-c"]:
        words = words[2:]
    return words


class FakeGit:
    """A scripted ``git``: records calls and answers `ls-remote`, `fetch`, `log`, `rev-parse` and `checkout`.

    Parameters
    ----------
    served : str, optional
        Commit that ``fetch`` and ``rev-parse`` serve.
    listed : str, optional
        Commit that ``ls-remote`` lists; an empty value makes the listing fail.
    files : dict[str, str] | None, optional
        Working tree that ``checkout`` creates, as ``{path: text}``.

    Methods
    -------
    words
        Returns the subcommand of each recorded call.
    """

    def __init__(self, *, served: str = COMMIT, listed: str = COMMIT, files: dict[str, str] | None = None) -> None:
        """Configure the commit that `fetch` serves, the one `ls-remote` lists, and the working tree `checkout` creates."""
        self.served, self.listed = served, listed
        self.files = files if files is not None else {"src/main.py": "print()", "README": "r"}
        self.calls: list[ProcessCall] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Answer one git command."""
        # Record the call, then answer according to the git subcommand.
        self.calls.append(call)
        words = subcommand(call.argv)
        if words[0] == "ls-remote":
            return ProcessResult(0, f"{self.listed}\t{words[-1]}\n") if self.listed else ProcessResult(2)
        if words[0] == "log":
            return ProcessResult(0, f"{EPOCH}\n")
        if words[0] == "rev-parse":
            return ProcessResult(0, f"{self.served}\n")
        # Materialise the checkout as real files plus a minimal ``.git`` marker.
        if words[0] == "checkout":
            for name, text in self.files.items():
                path = call.cwd / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            (call.cwd / ".git").mkdir()
            (call.cwd / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        return ProcessResult(0)

    def words(self) -> list[str]:
        """Return the first word after `git` of each recorded call.

        Returns
        -------
        list[str]
            The first word after ``git`` of each call, in order.
        """
        return [subcommand(call.argv)[0] for call in self.calls]


def git_machine(tmp_path: Path, git: FakeGit) -> FakeSystem:
    """Return a FakeSystem whose only process is the scripted git.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder that holds the machine.
    git : FakeGit
        Scripted git that answers the ``git`` process.

    Returns
    -------
    FakeSystem
        A fake machine whose only process is *git*.
    """
    return FakeSystem(tmp_path / "machine", processes={"git": git}, real_7zip=False)
