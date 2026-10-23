"""Build, publish and record a gupkg release.

A release is the current ``HEAD`` commit. The script checks that the working
tree is clean and that the version in ``_version.py`` is not already listed,
builds the self-contained ZIP ``gupkg-<version>-win-amd64.zip`` (or takes one
given with ``--zip``), and refuses a ZIP that was not built from ``HEAD`` (the
commit is recorded in the ZIP as ``gupkg/RELEASE.txt``) or that declares another
version. It then tags the released commit ``v<version>``, pushes the tag,
attaches the ZIP to the GitHub release (``gh release create``), and adds the
row ``| version | commit | date | sha256 | notes |`` to ``RELEASES.md``, which
it commits. The branch itself is never pushed; the script prints the command.

Usage and API
-------------
``python tools/release.py "notes" [--zip ZIP] [--no-publish]``. With
``--no-publish`` the tag and the row stay local and nothing is uploaded.

Implementation Approach
-----------------------
The ZIP's digest is computed from the file that is uploaded, so the row always
describes the published bytes. Publishing happens before the row is committed:
if a step fails, ``RELEASES.md`` does not yet advertise the release.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import sys
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_standalone  # noqa: E402  (a sibling script, not an installed package)

ROOT = Path(__file__).resolve().parents[1]
SEPARATOR = "|---|---|---|---|---|"


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    """Build, publish and record the release of ``HEAD``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("notes", help="one-line release notes for the table")
    parser.add_argument("--zip", type=Path, help="use this release ZIP instead of building one")
    parser.add_argument("--no-publish", action="store_true", help="keep the tag and row local; upload nothing")
    parser.add_argument("--remote", help="Git remote that receives the tag (default: the first remote)")
    parser.add_argument("--gh", default="gh", help="the GitHub CLI executable")
    args = parser.parse_args(argv)
    notes = args.notes.replace("|", "/").strip()

    # Refuse to release from a dirty tree or a version that is already listed.
    version = runtime_version(root)
    if _git(root, "status", "--porcelain"):
        raise SystemExit("The working tree is not clean; commit or stash first.")
    releases = root / "RELEASES.md"
    text = releases.read_text(encoding="utf-8")
    if re.search(rf"^\|\s*{re.escape(version)}\s*\|", text, re.MULTILINE):
        raise SystemExit(f"Version {version} is already listed in RELEASES.md; bump the version first.")
    commit = _git(root, "rev-parse", "HEAD")

    # Build the ZIP from HEAD (or accept one), then prove it is that release.
    archive = args.zip or root / "dist" / f"gupkg-{version}-win-amd64.zip"
    if args.zip is None:
        build_standalone.main(["--output", str(archive), "--commit", commit])
    check_release_zip(archive, version, commit)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    # Tag the released commit, then publish the tag and the ZIP.
    _git(root, "tag", f"v{version}", commit)
    if not args.no_publish:
        remote = args.remote or _git(root, "remote").splitlines()[0]
        _git(root, "push", remote, f"refs/tags/v{version}")
        subprocess.run(
            [args.gh, "release", "create", f"v{version}", str(archive), "--title", f"gupkg {version}", "--notes", notes],
            cwd=root,
            check=True,
        )

    # Record the release, naming the released commit and the published bytes.
    row = f"| {version} | `{commit}` | {date.today().isoformat()} | {digest} | {notes} |"
    releases.write_text(text.replace(SEPARATOR, f"{SEPARATOR}\n{row}", 1), encoding="utf-8")
    _git(root, "add", "RELEASES.md")
    _git(root, "commit", "-m", f"Release {version}")
    print(f"Released {version} at {commit}.\nsha256 {digest}\nPublish the row with:  git push origin main")
    return 0


def check_release_zip(archive: Path, version: str, commit: str) -> None:
    """Refuse a ZIP that is not the release of *version* built from *commit*.

    Raises
    ------
    SystemExit
        If ``gupkg/RELEASE.txt`` is missing or names another version or commit.
    """
    with zipfile.ZipFile(archive) as zipped:
        notes = [name for name in zipped.namelist() if re.fullmatch(r"[^/]+/gupkg/RELEASE\.txt", name)]
        if len(notes) != 1:
            raise SystemExit(f"{archive.name} has no gupkg/RELEASE.txt; it is not a gupkg release ZIP")
        recorded = zipped.read(notes[0]).decode("utf-8")
    recorded_version = re.search(r"(?m)^gupkg (\S+)$", recorded)
    recorded_commit = re.search(r"(?m)^commit ([0-9a-f]{40})$", recorded)
    if recorded_version is None or recorded_version.group(1) != version:
        raise SystemExit(f"{archive.name} is for gupkg {recorded_version and recorded_version.group(1)}, not {version}")
    if recorded_commit is None or recorded_commit.group(1) != commit:
        raise SystemExit(
            f"{archive.name} was built from commit {recorded_commit and recorded_commit.group(1)}, "
            f"not HEAD {commit}; rebuild it."
        )


def runtime_version(root: Path = ROOT) -> str:
    """Read ``__version__`` from the runtime's version module."""
    version_file = root / "src" / "gupkg" / "_version.py"
    tree = ast.parse(version_file.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit(f"No __version__ in {version_file}")


def _git(root: Path, *arguments: str) -> str:
    """Run one Git command in the repository and return its stripped output."""
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


if __name__ == "__main__":
    sys.exit(main())
