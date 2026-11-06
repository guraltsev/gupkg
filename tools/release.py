"""Build, publish and record a gupkg release.

A release is the current ``HEAD`` commit. The script checks that nothing is staged
and ``RELEASES.md`` has no edits, and that the version in ``_version.py`` of ``HEAD`` is
not already listed. It builds the self-contained ZIP ``gupkg-<version>-win-amd64.zip``
from a temporary Git worktree of ``HEAD`` (so other edits or untracked files in the
working tree cannot reach the ZIP), or takes one given with ``--zip``, and refuses a ZIP that was not built from ``HEAD`` (the
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
import contextlib
import hashlib
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_standalone  # noqa: E402  (a sibling script, not an installed package)

# Repository root, where ``RELEASES.md`` and the Git checkout live.
ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    """Build, publish and record the release of ``HEAD``.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments; ``sys.argv[1:]`` when omitted.
    root : Path, default=ROOT
        Repository root whose ``HEAD`` is released.

    Returns
    -------
    int
        ``0`` once the release is recorded.

    Raises
    ------
    SystemExit
        If changes are staged, ``RELEASES.md`` is edited, the version is
        already listed, or the ZIP is not the release of ``HEAD``.
    """
    # Describe the command line from the module summary.
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("notes", help="one-line release notes for the table")
    parser.add_argument("--zip", type=Path, help="use this release ZIP instead of building one")
    parser.add_argument("--no-publish", action="store_true", help="keep the tag and row local; upload nothing")
    parser.add_argument("--remote", help="Git remote that receives the tag (default: the first remote)")
    parser.add_argument("--gh", default="gh", help="the GitHub CLI executable")
    args = parser.parse_args(argv)
    notes = args.notes.replace("|", "/").strip()

    # The release is the commit HEAD. Only the row commit is made from the working tree, so refuse a staged change
    # (it would ride along in that commit) and an edited RELEASES.md; other edits and untracked files are harmless.
    if _git(root, "diff", "--cached", "--name-only"):
        raise SystemExit("Changes are staged; commit or unstage them first.")
    if _git(root, "status", "--porcelain", "--untracked-files=no", "--", "RELEASES.md"):
        raise SystemExit("RELEASES.md has uncommitted edits; commit or discard them first.")

    # Read the released commit and its version, and refuse a version already listed.
    commit = _git(root, "rev-parse", "HEAD")
    version = committed_version(root, commit)
    releases = root / "RELEASES.md"
    text = releases.read_text(encoding="utf-8")
    if re.search(rf"^\|\s*{re.escape(version)}\s*\|", text, re.MULTILINE):
        raise SystemExit(f"Version {version} is already listed in RELEASES.md; bump the version first.")

    # Build the ZIP from a worktree of HEAD (or accept one), then prove it is that release.
    archive = args.zip or root / "dist" / f"gupkg-{version}-win-amd64.zip"
    if args.zip is None:
        # Only the committed tree is built, so working-tree edits cannot reach the ZIP.
        with _worktree(root, commit) as tree:
            build_standalone.main([
                "--source", str(tree / "src" / "gupkg"),
                "--manifest", str(tree / "src" / "pkg.toml"),
                "--deps-lock", str(tree / "tools" / "gupkg-deps.lock"),
                "--output", str(archive),
                "--commit", commit,
            ])
    check_release_zip(archive, version, commit)
    # The digest is taken from the exact file that will be uploaded.
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    # Tag the released commit, then publish the tag and the ZIP.
    _git(root, "tag", f"v{version}", commit)
    if not args.no_publish:
        # Push the tag first, then attach the ZIP and its digest to the GitHub release.
        remote = args.remote or _git(root, "remote").splitlines()[0]
        _git(root, "push", remote, f"refs/tags/v{version}")
        sidecar = archive.with_name(archive.name + ".sha256")
        sidecar.write_bytes(f"{digest}  {archive.name}\n".encode("ascii"))
        subprocess.run(
            [args.gh, "release", "create", f"v{version}", str(archive), str(sidecar), "--title", f"gupkg {version}", "--notes", notes],
            cwd=root,
            check=True,
        )
        # Read the published asset back so a corrupted upload is caught before it is recorded.
        _verify_published(args.gh, root, f"v{version}", archive.name, digest)

    # Record the release, naming the released commit and the published bytes.
    separator = "|---|---|---|---|---|"
    row = f"| {version} | `{commit}` | {date.today().isoformat()} | {digest} | {notes} |"
    releases.write_text(text.replace(separator, f"{separator}\n{row}", 1), encoding="utf-8")
    # Commit the new row and tell the maintainer how to publish it.
    _git(root, "add", "RELEASES.md")
    _git(root, "commit", "-m", f"Record release {version} hash in RELEASES.md")
    print(f"Released {version} at {commit}.\nsha256 {digest}\nPublish the row with:  git push origin main")
    return 0


def check_release_zip(archive: Path, version: str, commit: str) -> None:
    """Refuse a ZIP that is not the release of *version* built from *commit*.

    Parameters
    ----------
    archive : Path
        Release ZIP to inspect.
    version : str
        Version the ZIP must declare.
    commit : str
        Full commit hash the ZIP must record.

    Raises
    ------
    SystemExit
        If ``gupkg/RELEASE.txt`` is missing or names another version or commit.
    """
    with zipfile.ZipFile(archive) as zipped:
        check_release_zip_object(zipped, archive.name, version, commit)


def check_release_zip_object(zipped: zipfile.ZipFile, name: str, version: str, commit: str) -> None:
    """Refuse the open ZIP *zipped* unless ``RELEASE.txt`` names *version* and *commit*.

    Parameters
    ----------
    zipped : zipfile.ZipFile
        Open release ZIP.
    name : str
        Name used for the ZIP in error messages.
    version : str
        Version the ZIP must declare.
    commit : str
        Full commit hash the ZIP must record.

    Raises
    ------
    SystemExit
        If ``gupkg/RELEASE.txt`` is missing or names another version or commit.
    """
    # Locate the single RELEASE.txt and read the version and commit it records.
    notes = [entry for entry in zipped.namelist() if re.fullmatch(r"(?:[^/]+/){1,2}gupkg/RELEASE\.txt", entry)]
    if len(notes) != 1:
        raise SystemExit(f"{name} has no gupkg/RELEASE.txt; it is not a gupkg release ZIP")
    recorded = zipped.read(notes[0]).decode("utf-8")
    recorded_version = re.search(r"(?m)^gupkg (\S+)$", recorded)
    recorded_commit = re.search(r"(?m)^commit ([0-9a-f]{40})$", recorded)
    # Both recorded values must equal what the caller expects.
    if recorded_version is None or recorded_version.group(1) != version:
        raise SystemExit(f"{name} is for gupkg {recorded_version and recorded_version.group(1)}, not {version}")
    if recorded_commit is None or recorded_commit.group(1) != commit:
        raise SystemExit(
            f"{name} was built from commit {recorded_commit and recorded_commit.group(1)}, "
            f"not HEAD {commit}; rebuild it."
        )


def committed_version(root: Path, commit: str) -> str:
    """Return ``__version__`` from ``_version.py`` as it is in *commit*, not in the working tree.

    Parameters
    ----------
    root : Path
        Repository root.
    commit : str
        Commit whose ``src/gupkg/_version.py`` is read.

    Returns
    -------
    str
        The declared version.
    """
    text = _git(root, "show", f"{commit}:src/gupkg/_version.py")
    return _version_from(ast.parse(text), f"{commit}:src/gupkg/_version.py")


def runtime_version(root: Path = ROOT) -> str:
    """Return ``__version__`` from the runtime's version module in the working tree.

    Parameters
    ----------
    root : Path, default=ROOT
        Repository root.

    Returns
    -------
    str
        The declared version.
    """
    version_file = root / "src" / "gupkg" / "_version.py"
    return _version_from(ast.parse(version_file.read_text(encoding="utf-8")), str(version_file))


# --- supporting helpers ---


def _verify_published(gh: str, root: Path, tag: str, name: str, digest: str) -> None:
    """Download the release asset *name* of *tag* and require its SHA-256 to equal *digest*.

    Raises
    ------
    SystemExit
        If the published bytes differ from the local ZIP; the release row is then not recorded.
    """
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run([gh, "release", "download", tag, "--pattern", name, "--dir", folder], cwd=root, check=True)
        fetched = hashlib.sha256((Path(folder) / name).read_bytes()).hexdigest()
    if fetched != digest:
        raise SystemExit(f"The published {name} has SHA-256 {fetched}, not {digest}; the row was not recorded.")
    print(f"Verified the published {name}: sha256 {digest}")


@contextlib.contextmanager
def _worktree(root: Path, commit: str):
    """Check *commit* out into a temporary detached worktree of the repository, yield its path, and remove it again."""
    with tempfile.TemporaryDirectory(prefix="gupkg-release-") as folder:
        tree = Path(folder) / "tree"
        _git(root, "worktree", "add", "--detach", str(tree), commit)
        try:
            yield tree
        finally:
            _git(root, "worktree", "remove", "--force", str(tree))


def _version_from(tree: ast.Module, origin: str) -> str:
    """Return the ``__version__`` assigned in the parsed module *tree* (read from *origin*)."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise SystemExit(f"No __version__ in {origin}")


def _git(root: Path, *arguments: str) -> str:
    """Run one Git command in the repository and return its stripped output."""
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


if __name__ == "__main__":
    sys.exit(main())
