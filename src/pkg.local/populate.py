"""Populate ``App`` of a gupkg version from its bundled tree: the custom populate script of gupkg's own package.

The distribution of gupkg carries a complete gupkg in ``<version>\\gupkg\\``, the bundled tree. Installing the package copies
that tree into ``App``, and the installed gupkg runs from there (decisions D14, D15 and D27). The bundled tree stays where it
is, because it is this script's input: ``App`` can be built again while it exists.

Usage and API
-------------
The engine calls ``populate(ctx)`` for ``[source] script = "pkg.local/populate.py"``. When the version directory has no bundled
tree yet, as after ``gupkg self check-updates`` wrote it, the script first fetches that version's release ZIP (decision D28
allows the network), checks it against the committed ``RELEASES.md`` row and puts the tree in place. It raises ``HookError``
for every refusal, and then leaves no half-written tree and no ``App`` behind.

Implementation Approach
-----------------------
Before anything is unpacked, the ZIP's SHA-256 must equal the digest of the row (a row without one is refused); the download
client enforces it. The unpacked release must hold exactly one ``gupkg\\<version>\\gupkg`` tree whose files match its own
``MANIFEST.sha256`` (no modified, missing or extra file), whose ``RELEASE.txt`` names the commit of the row and whose declared
version is the version being populated. Only then is it moved into the version directory, in one rename. Whether the tree came
from the distribution or from the release, it is verified against its manifest once more and copied into ``App`` whole, so
nothing of an earlier runtime is carried over.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from gupkg.hooklib import HookError, safe_extract, fetch_text
from gupkg.integrity import ManifestError, verify_tree
from .releases import RELEASES_URL, asset_url, parse_releases

PKG_MODULE_API = 2


def populate(ctx):
    """Fill ``ctx.paths.app`` with a verified copy of the bundled tree, fetching the release first when the tree is missing.

    Parameters
    ----------
    ctx : HookContext
        The context of the version being populated; ``ctx.paths.version_root`` holds the bundled tree and ``ctx.paths.app``
        is the staged ``App`` folder to create.

    Raises
    ------
    HookError
        If the tree is missing and cannot be fetched and verified, does not match its manifest, or is another version.
    """
    version_root = Path(ctx.paths.version_root)
    tree = version_root / "gupkg"

    # A version written by check-updates has no tree yet: fetch the release it names.
    if not tree.exists():
        _fetch_release(ctx, tree)

    # The tree must be exactly what its manifest says and the version that is being populated.
    _require_verified(tree, ctx.version)

    # Copy the whole tree: App is a plain copy, so the bundled tree stays the input for the next rebuild.
    ctx.reporter.step(f"copying the bundled gupkg {ctx.version} into App", "add")
    shutil.copytree(tree, ctx.paths.app, dirs_exist_ok=True)


def _fetch_release(ctx, tree: Path) -> None:
    """Download the release ZIP of ``ctx.version``, verify it against its RELEASES.md row and put its tree at *tree*."""
    # The row names the digest and the commit this version must have been built from.
    try:
        rows = parse_releases(fetch_text(ctx, RELEASES_URL))
    except OSError as error:
        raise HookError(f"Could not read the gupkg release list: {error}") from error
    row = next((item for item in rows if item["version"] == ctx.version), None)
    if row is None:
        raise HookError(f"RELEASES.md does not list gupkg {ctx.version}")
    if row["sha256"] is None:
        raise HookError(f"RELEASES.md lists gupkg {ctx.version} without a SHA-256 digest")

    # The client compares the digest before it hands over the file, so nothing is unpacked from a wrong download.
    ctx.reporter.step(f"fetching the gupkg {ctx.version} release", "info")
    try:
        download = ctx.http.download(asset_url(ctx.version), expected_sha256=row["sha256"], require_https=True)
    except OSError as error:
        raise HookError(f"The gupkg {ctx.version} release could not be downloaded and verified: {error}") from error

    # Unpack into the work folder and find the single tree of this version in it.
    work = Path(ctx.paths.work) if ctx.paths.work is not None else Path(ctx.paths.version_root) / ".gupkg-release"
    extracted = work / "release"
    safe_extract(ctx, download.path, extracted)
    trees = [path.parent for path in extracted.glob("gupkg/*/gupkg/MANIFEST.sha256")]
    if len(trees) != 1 or trees[0].parent.name != ctx.version:
        raise HookError(f"The release ZIP does not contain exactly one gupkg {ctx.version} tree")
    candidate = trees[0]

    # The unpacked tree must match its manifest, name the commit of the row and be the version asked for.
    _require_verified(candidate, ctx.version)
    note = (candidate / "RELEASE.txt").read_text(encoding="utf-8") if (candidate / "RELEASE.txt").is_file() else ""
    commit = re.search(r"(?m)^commit ([0-9a-f]{40})$", note)
    if commit is None or commit.group(1) != row["commit"]:
        raise HookError(
            f"The release ZIP was built from commit {commit.group(1) if commit else None!r}, not the listed {row['commit']!r}"
        )

    # Put the tree in place with one rename, so a failure never leaves a half-written tree.
    staging = Path(ctx.paths.version_root) / ".gupkg-tree-new"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(candidate, staging)
    staging.replace(tree)


def _require_verified(tree: Path, version: str) -> None:
    """Raise ``HookError`` unless *tree* matches its ``MANIFEST.sha256`` exactly and declares *version*."""
    try:
        report = verify_tree(tree, tree / "MANIFEST.sha256")
    except (ManifestError, OSError) as error:
        raise HookError(f"The bundled gupkg cannot be checked: {error}") from error
    if not report.ok:
        shown = "; ".join(report.problems()[:5])
        raise HookError(f"The bundled gupkg does not match its MANIFEST.sha256 ({shown})")
    declared = re.search(r'(?m)^__version__ = "([^"]*)"', (tree / "gupkg" / "_version.py").read_text(encoding="utf-8"))
    if declared is None or declared.group(1) != version:
        raise HookError(f"The bundled gupkg is version {declared.group(1) if declared else None!r}, not {version!r}")
