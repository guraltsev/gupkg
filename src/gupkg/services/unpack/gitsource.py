"""Check out one exact commit of a Git repository as the contents of ``App``.

The ``git`` kind of ``[source]`` names a repository (``download_url``) and a commit or a
full ref (``[source.git]``). The checkout is made in the folder the caller gives, with no
history kept: the resolved commit is returned so the install record can store it. A pinned
``commit`` must be exactly what Git resolves, otherwise the checkout fails, so a moved
branch can never change what a pinned version installs.

Usage and API
-------------
``checkout_commit(system, url, ref, commit, destination, work)`` fills the new folder
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

from ...system.port import ProcessFailed, System, run_checked
from ..safepath import remove_tree
from .extract import UnpackError

_SHA = re.compile(r"[0-9a-f]{40}")
_ENVIRONMENT = {"GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "https:http:ssh:git", "GIT_CONFIG_NOSYSTEM": "1"}


def checkout_commit(system: System, url: str, ref: str | None, commit: str | None, destination: Path, work: Path) -> str:
    """Check out *commit* (or the commit *ref* resolves to) into the new folder *destination*.

    Parameters
    ----------
    system : System
        The boundary that runs ``git``.
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
    environment = {**os.environ, **_ENVIRONMENT}
    checkout = Path(work) / "git-checkout"
    remove_tree(checkout)
    checkout.mkdir(parents=True)

    def git(*arguments: str, cwd: Path | None = None) -> str:
        """Run one Git command and return its stripped output."""
        try:
            return run_checked(system, ["git", *arguments], cwd=cwd, env=environment, timeout=1800)
        except (ProcessFailed, OSError, TimeoutError) as error:
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
            raise UnpackError(f"The repository served commit {fetched}, not the pinned {wanted}")
        git("checkout", "--quiet", "--detach", wanted, cwd=checkout)
        remove_tree(checkout / ".git")
        os.replace(checkout, destination)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)
    return wanted
