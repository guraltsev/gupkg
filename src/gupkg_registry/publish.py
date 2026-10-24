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
    return "\n".join(lines) + "\n"


def commit_and_push(system: System, root: Path, outcomes: Sequence[Outcome]) -> None:
    """Commit the versions in *outcomes* with ``_registry.toml`` and push the commit.

    Raises
    ------
    PublishError
        If there is nothing to commit, or ``git add``, ``commit`` or ``push`` exits non-zero.
    """
    root = Path(root)
    written = [o for o in outcomes if o.status in ("added", "pinned") and o.path is not None]
    if not written:
        raise PublishError("nothing was written, so there is nothing to commit")
    paths = [o.path.relative_to(root).as_posix() for o in written] + [REGISTRY_FILE]
    steps = [
        ["git", "add", "--", *paths],
        ["git", "commit", "-m", commit_message(outcomes)],
        ["git", "push"],
    ]
    for command in steps:
        result = system.run(command, cwd=root)
        if result.returncode != 0:
            raise PublishError(f"'{' '.join(command[:2])}' failed with status {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
