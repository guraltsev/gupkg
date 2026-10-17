"""Check the package registry and move and push the ``registry`` tag.

The registry (the ``pkgs`` folder) is versioned independently of gupkg itself:
users sync the archive of the ``registry`` tag, so package changes reach them
without a gupkg release or version bump. The script checks the committed
revision it is about to publish (``HEAD``, or ``--target``): the revision must
already be on a remote branch, every package seed must carry the current
``gupkg.cmd`` launcher, and the whole ``pkgs`` tree must validate. If every
check passes it moves the tag to that revision and pushes the tag. ``--force``
moves and pushes the tag even when a check fails.
"""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAG = "registry"

sys.path.insert(0, str(ROOT / "src"))


def main(argv: list[str] | None = None) -> int:
    """Run the checks, move the tag, and push it; return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", default="HEAD", help="revision to publish (default: HEAD)")
    parser.add_argument("--force", action="store_true", help="move and push the tag even if a check fails")
    parser.add_argument("--remote", help="remote to push to (default: the first remote)")
    parser.add_argument("--repo", type=Path, default=ROOT, help="repository to publish from (default: this one)")
    args = parser.parse_args(argv)

    def git(*arguments: str, binary: bool = False):
        """Run one Git command in the repository and return its output."""
        result = subprocess.run(["git", *arguments], cwd=args.repo, capture_output=True, check=True)
        return result.stdout if binary else result.stdout.decode("utf-8", "replace").strip()

    # The revision must exist; everything else is a check that --force may override.
    try:
        commit = git("rev-parse", "--verify", f"{args.target}^{{commit}}")
    except subprocess.CalledProcessError:
        print(f"Unknown revision: {args.target}", file=sys.stderr)
        return 2
    problems = check_revision(git, commit)

    # A failed check stops the publication unless the caller insists.
    for problem in problems:
        print(f"{'warning' if args.force else 'error'}: {problem}", file=sys.stderr)
    if problems and not args.force:
        print("Nothing was changed. Fix the problems, or use --force to publish anyway.", file=sys.stderr)
        return 1

    # Move the tag locally, then replace it on the remote.
    remote = args.remote or (git("remote").splitlines() or ["origin"])[0]
    git("tag", "-f", TAG, commit)
    try:
        git("push", remote, f"+refs/tags/{TAG}")
    except subprocess.CalledProcessError as error:
        print(f"Pushing the tag failed: {error.stderr.decode('utf-8', 'replace').strip()}", file=sys.stderr)
        return 3
    print(f"Registry tag '{TAG}' is now at {commit} on {remote}.")
    return 0


def check_revision(git, commit: str) -> list[str]:
    """Return the problems that make *commit* unfit to be the published registry."""
    problems = []

    # Publishing an unpushed commit would leave users pointed at nothing.
    if not git("branch", "-r", "--contains", commit):
        problems.append(f"{commit[:12]} is not on any remote branch; push it first")

    # Validate the committed tree itself, not whatever is in the working tree.
    from gupkg.core import ConfigValidationError
    from gupkg.registry import validate_registry_tree

    with tempfile.TemporaryDirectory(prefix="registry-check-") as scratch:
        try:
            archive = git("archive", "--format=tar", commit, "pkgs", "src/gupkg.cmd", binary=True)
        except subprocess.CalledProcessError:
            problems.append("the revision has no pkgs folder or no src/gupkg.cmd")
            return problems
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(scratch, filter="data")
        root = Path(scratch)
        try:
            packages = validate_registry_tree(root / "pkgs")
        except ConfigValidationError as error:
            problems.append(f"the registry does not validate: {error}")
            return problems

        # Every seed must ship the launcher exactly as the source tree has it.
        launcher = (root / "src" / "gupkg.cmd").read_bytes()
        stale = [
            package.selector
            for package in packages
            if not (package.version_path / "gupkg.cmd").is_file()
            or (package.version_path / "gupkg.cmd").read_bytes() != launcher
        ]
        if stale:
            problems.append(
                f"these packages lack the current gupkg.cmd: {', '.join(stale)} "
                "(run tools/sync_package_launchers.py and commit)"
            )
    return problems


if __name__ == "__main__":
    sys.exit(main())
