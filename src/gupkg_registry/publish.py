"""Commit and push what the registry tool wrote (``--auto-push`` only; design 17, decision D36).

Without ``--auto-push`` the tool never starts ``git``: the maintainer reviews the new directories
and pushes them. With it, one commit holds the new version directories and ``_registry.toml``, and
one ``git push`` publishes it, so a scheduled job can keep the registry current.

Usage and API
-------------
``commit_message(outcomes)`` builds the message (it names every hash the tool computed itself, which
a maintainer should review). ``commit_and_push(system, root, outcomes)`` runs ``git add``, ``git
commit`` and ``git push`` through ``System.run`` and raises ``PublishError`` at the first failure.

Implementation Approach
-----------------------
Only the paths this run wrote are added (never ``git add -A``), hooks are never skipped and nothing
is forced. Git uses the maintainer's own configuration and credentials: this is the maintainer's job,
unlike the scrubbed ``git`` the services run on untrusted input.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from gupkg.system.port import System

from .layout import REGISTRY_FILE
from .update import Outcome


def _usable_ref_name(name: str, what: str) -> bool:
    """Return whether *name* passes the rules of ``git check-ref-format`` (and is no option, and for a branch no ``refs/`` name)."""
    if not name or name.startswith(("-", "/", ".")) or name.endswith(("/", ".", ".lock")):
        return False
    if ".." in name or "@{" in name or "//" in name or name == "@" or "\\" in name:
        return False
    if any(ord(ch) < 33 or ord(ch) == 127 or ch in "~^:?*[" for ch in name):
        return False
    if any(part.startswith(".") or part.endswith(".lock") for part in name.split("/")):
        return False
    return not (what == "branch" and name.startswith("refs/"))


class PublishError(RuntimeError):
    """Report a ``git`` command that failed while committing or pushing."""


def commit_message(outcomes: Sequence[Outcome]) -> str:
    """Return the commit message for the written *outcomes* (added or pinned versions)."""
    written = [o for o in outcomes if o.status in ("added", "pinned")]
    names = ", ".join(f"{o.package} {o.version}" for o in written)
    lines = [f"Registry tool: {names}", ""]
    computed = [o for o in written if o.review]
    if computed:
        lines.append("Hashes computed by the registry tool (the vendor publishes none); review before relying on them:")
        lines.extend(f"- {o.package} {o.version} (sha256_from = \"registry\")" for o in computed)
    else:
        lines.append("Every hash was verified against a checksum the vendor publishes.")
    unversioned = [o for o in written if o.unversioned]
    if unversioned:
        lines.append("")
        lines.append("The download address has no version in it (an older pinned version stops verifying when the vendor replaces the file):")
        lines.extend(f"- {o.package} {o.version}" for o in unversioned)
    return "\n".join(lines) + "\n"


def commit_and_push(
    system: System,
    root: Path,
    outcomes: Sequence[Outcome],
    *,
    allow_computed: bool = False,
    remote: str | None = None,
    branch: str | None = None,
) -> None:
    """Commit the versions in *outcomes* with ``_registry.toml`` and push the commit.

    Only the paths this run wrote are committed (``git commit -- <paths>``), so anything the maintainer
    already staged stays out. The push never forces. With no *remote* and *branch* it is a plain ``git
    push`` (the current branch's upstream); with them it is ``git push <remote> HEAD:<branch>``.

    Parameters
    ----------
    allow_computed : bool
        A hash the tool computed itself (``sha256_from = "registry"``) is trust on first use and is
        refused here unless this is true: a commit-message flag is not a review.
    remote, branch : str, optional
        Where to push. A name starting with ``-`` is refused.

    Raises
    ------
    PublishError
        If there is nothing to commit, a computed hash is not allowed, a remote or branch name is
        unsafe, or ``git add``, ``commit`` or ``push`` exits non-zero.
    """
    root = Path(root)
    written = [o for o in outcomes if o.status in ("added", "pinned") and o.path is not None]
    if not written:
        raise PublishError("nothing was written, so there is nothing to commit")
    computed = [f"{o.package} {o.version}" for o in written if o.review]
    if computed and not allow_computed:
        raise PublishError(
            "refusing to publish hashes the registry tool computed itself (trust on first use): "
            + ", ".join(computed) + ". Review them and commit by hand, or run again with --allow-computed."
        )
    for name, what in ((remote, "remote"), (branch, "branch")):
        if name is not None and not _usable_ref_name(name, what):
            raise PublishError(f"{name!r} is not a usable {what} name")
    paths = [f":(literal){o.path.relative_to(root).as_posix()}" for o in written] + [f":(literal){REGISTRY_FILE}"]
    push = ["git", "push"]
    if remote or branch:
        push += [remote or "origin", f"HEAD:{branch}"] if branch else [remote]
    steps = [
        ["git", "add", "--", *paths],
        ["git", "commit", "-m", commit_message(outcomes), "--", *paths],
        push,
    ]
    for command in steps:
        result = system.run(command, cwd=root)
        if result.returncode != 0:
            raise PublishError(f"'{' '.join(command[:2])}' failed with status {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
