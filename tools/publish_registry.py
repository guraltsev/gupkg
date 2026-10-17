"""Publish the package registry by moving the ``registry`` tag to ``HEAD``.

The registry (the ``pkgs`` folder) is versioned independently of gupkg itself:
users sync the archive of the ``registry`` tag, so package changes reach them
without a gupkg release or version bump. The script refuses to move the tag
unless the working tree is clean, ``HEAD`` is already pushed, every package seed
carries the current ``gupkg.cmd`` launcher, and the whole tree validates. It
moves the tag locally and prints the push command; nothing is published until
you push.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = "registry"

sys.path.insert(0, str(ROOT / "src"))


def _git(*arguments: str) -> str:
    """Run one Git command in the repository and return its stripped output."""
    result = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def main() -> int:
    """Check the registry and move the tag; return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--remote", help="remote to print the push command for (default: the first one)")
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

    commit = _git("rev-parse", "HEAD")
    _git("tag", "-f", TAG, commit)
    remote = args.remote or (_git("remote").splitlines() or ["origin"])[0]
    print(f"Registry tag '{TAG}' now at {commit} ({len(packages)} packages).")
    print(f"Publish with:  git push {remote} +{TAG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
