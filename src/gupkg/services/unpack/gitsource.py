"""Check out one exact commit of a Git repository as the contents of ``App``.

The ``git`` kind of ``[source]`` names a repository (``download_url``) and a commit or a
full ref (``[source.git]``). The checkout is made in the folder the caller gives, with no
history kept: the resolved commit is returned so the install record can store it. A pinned
``commit`` must be exactly what Git resolves, otherwise the checkout fails, so a moved
branch can never change what a pinned version installs.

Usage and API
-------------
``checkout_commit(system, mode, url, ref, commit, destination, work)`` fills the new folder
*destination* and returns the 40-digit commit id.

Implementation Approach
-----------------------
Git runs through ``System.run`` with prompts disabled and only network transports allowed
(no ``ext::`` or ``file`` helpers). The repository is initialised in a scratch folder,
the one commit is fetched shallowly by id, checked out detached, and the ``.git`` folder
is removed before the tree is moved into place.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ...report.events import subprocess_lines
from ...system.port import Mode, ProcessFailed, System, run_checked
from ..gitenv import GitUnavailable, git_command, git_environment
from ..safepath import remove_tree
from .extract import UnpackError

_SHA = re.compile(r"[0-9a-f]{40}")


class CommitMismatch(UnpackError):
    """Report that the repository served a commit other than the one the version pins."""

    def __init__(self, message: str, expected: str, observed: str) -> None:
        """Build the error with the pinned and the served commit."""
        super().__init__(message)
        self.expected = expected
        self.observed = observed


def checkout_commit(system: System, mode: Mode, url: str, ref: str | None, commit: str | None, destination: Path, work: Path) -> str:
    """Check out *commit* (or the commit *ref* resolves to) into the new folder *destination*.

    Parameters
    ----------
    system : System
        The boundary that runs ``git``.
    mode : Mode
        The run's mode; system mode runs only a trusted ``git``.
    url : str
        The repository address.
    ref : str, optional
        A full ``refs/...`` name, used when *commit* is not given.
    commit : str, optional
        The 40-digit commit id to check out; it must exist in the repository.
    destination : Path
        A folder that does not exist yet; it receives the working tree without ``.git``.
    work : Path
        An existing scratch folder.

    Returns
    -------
    str
        The 40-digit commit id that was checked out.

    Raises
    ------
    UnpackError
        If ``git`` fails, the ref does not resolve, or the fetched commit differs from *commit*.
    """
    if url.startswith("-"):
        raise UnpackError("A repository address cannot start with '-'")
    environment = git_environment(system, mode, Path(work))
    checkout = Path(work) / "git-checkout"
    remove_tree(checkout)
    checkout.mkdir(parents=True)

    def git(*arguments: str, cwd: Path | None = None) -> str:
        """Run one Git command and return its stripped output."""
        try:
            command = git_command(*arguments, system=system, mode=mode)
            return run_checked(system, command, cwd=cwd, env=environment, on_line=subprocess_lines(command), timeout=1800)
        except (ProcessFailed, OSError, TimeoutError, GitUnavailable) as error:
            raise UnpackError(f"git {arguments[0]} failed: {error}") from error

    try:
        # Resolve what to fetch: the pinned commit, or the commit the ref points at now.
        wanted = commit.lower() if commit else None
        if wanted is None:
            listing = git("ls-remote", "--exit-code", "--", url, ref or "HEAD").split()
            wanted = listing[0].lower() if listing else ""
        if not _SHA.fullmatch(wanted or ""):
            raise UnpackError(f"Could not resolve a commit of {url}")

        git("init", "--quiet", str(checkout))
        git("fetch", "--quiet", "--depth", "1", "--no-tags", "--", url, wanted, cwd=checkout)
        fetched = git("rev-parse", "FETCH_HEAD", cwd=checkout).lower()
        if fetched != wanted:
            raise CommitMismatch(f"The repository served commit {fetched}, not the pinned {wanted}", wanted, fetched)
        git("checkout", "--quiet", "--detach", wanted, cwd=checkout)
        remove_tree(checkout / ".git")
        os.replace(checkout, destination)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
    return wanted
