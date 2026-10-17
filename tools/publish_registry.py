"""Publish the package registry by recording a new row in ``REGISTRY.md``.

The registry (the ``pkgs`` folder) is versioned independently of gupkg itself.
Users sync the commit named by the newest row of ``REGISTRY.md`` on ``main``,
so package changes reach them without a gupkg release or version bump. The
script refuses to publish unless the working tree is clean, ``HEAD`` is already
pushed, every package seed carries the current ``gupkg.cmd`` launcher, and the
whole tree validates. It records ``HEAD`` as the new revision and commits the
row; nothing is published until you push.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "REGISTRY.md"
SEPARATOR = "|---|---|---|---|"

sys.path.insert(0, str(ROOT / "src"))


def _git(*arguments: str) -> str:
    """Run one Git command in the repository and return its stripped output."""
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def main() -> int:
    """Check the registry and record a new revision; return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("notes", help="one-line description of what changed")
    args = parser.parse_args()

    # Only a committed, already-published tree may become the public registry.
    if _git("status", "--porcelain"):
        raise SystemExit("The working tree is not clean; commit or stash first.")
    if not _git("branch", "-r", "--contains", "HEAD"):
        raise SystemExit("HEAD is not on any remote branch; push it first.")

    # Same checks as the registry loader applies, plus the shipped launcher.
    from gupkg.registry import validate_registry_tree

    packages = validate_registry_tree(ROOT / "pkgs")
    launcher = (ROOT / "src" / "gupkg.cmd").read_bytes()
    stale = [
        package.selector
        for package in packages
        if not (package.version_path / "gupkg.cmd").is_file()
        or (package.version_path / "gupkg.cmd").read_bytes() != launcher
    ]
    if stale:
        raise SystemExit(
            f"These packages lack the current gupkg.cmd: {', '.join(stale)}. "
            "Run tools/sync_package_launchers.py and commit."
        )

    # Number the revision after the newest row, and refuse to list a commit twice.
    text = INDEX.read_text(encoding="utf-8")
    commit = _git("rev-parse", "HEAD")
    rows = re.findall(r"(?m)^\|\s*(\d+)\s*\|\s*`?([0-9a-f]{40})`?\s*\|", text)
    if rows and rows[0][1] == commit:
        raise SystemExit("HEAD is already the newest registry revision.")
    revision = max((int(number) for number, _ in rows), default=0) + 1
    row = f"| {revision} | `{commit}` | {date.today().isoformat()} | {args.notes} |"
    INDEX.write_text(text.replace(SEPARATOR, f"{SEPARATOR}\n{row}", 1), encoding="utf-8")
    _git("add", "REGISTRY.md")
    _git("commit", "-m", f"Registry revision {revision}")
    remote = (_git("remote").splitlines() or ["origin"])[0]
    print(f"Registry revision {revision} at {commit} ({len(packages)} packages).")
    print(f"Publish with:  git push {remote} main")
    return 0


if __name__ == "__main__":
    sys.exit(main())
