"""Record a gupkg release in RELEASES.md and tag it.

A release is the current ``HEAD`` commit. The script checks that the working
tree is clean and that the version in ``_version.py`` is not already listed, then adds a row to ``RELEASES.md``, commits it, and tags the
released commit ``v<version>``. Nothing is pushed; the script prints the push
commands instead.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "RELEASES.md"
VERSION_FILE = ROOT / "src" / "gupkg" / "_version.py"
SEPARATOR = "|---|---|---|---|"


def _git(*arguments: str) -> str:
    """Run one Git command in the repository and return its stripped output."""
    result = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def runtime_version() -> str:
    """Read ``__version__`` from the runtime's version module."""
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit(f"No __version__ in {VERSION_FILE}")


def main() -> int:
    """Add a release row for ``HEAD``, commit it, and tag the released commit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notes", help="one-line release notes for the table")
    args = parser.parse_args()
    notes = args.notes.replace("|", "/").strip()

    version = runtime_version()
    if _git("status", "--porcelain"):
        raise SystemExit("The working tree is not clean; commit or stash first.")
    text = RELEASES.read_text(encoding="utf-8")
    if re.search(rf"^\|\s*{re.escape(version)}\s*\|", text, re.MULTILINE):
        raise SystemExit(f"Version {version} is already listed in RELEASES.md; bump the version first.")

    commit = _git("rev-parse", "HEAD")
    row = f"| {version} | `{commit}` | {date.today().isoformat()} | {notes} |"
    RELEASES.write_text(text.replace(SEPARATOR, f"{SEPARATOR}\n{row}", 1), encoding="utf-8")
    _git("add", "RELEASES.md")
    _git("commit", "-m", f"Release {version}")
    _git("tag", f"v{version}", commit)
    print(f"Released {version} at {commit}.\nPublish with:  git push origin main v{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
