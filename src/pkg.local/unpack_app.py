"""Stage a gupkg release from its self-contained release ZIP.

The gupkg package keeps its whole tree (manifest, launchers, and the ``gupkg``
Python package with its bundled interpreter) at the version root rather than
below ``App``. This hook therefore replaces the carried-over support tree in the
staged version with the tree from the release ZIP, and leaves a small marker in
``App`` so the staged version counts as a prepared payload.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading and
verifying the candidate ZIP against the digest listed in RELEASES.md.

Implementation Approach
-----------------------
The ZIP is extracted safely into the work area. Before anything is staged, the
extracted tree must match its own ``gupkg/MANIFEST.sha256`` exactly (no
modified, missing or extra file), name the listed commit in
``gupkg/RELEASE.txt``, and declare the candidate's version. Only then is the
previous version's code, interpreter and launchers removed and replaced by the
extracted tree; nothing of the old runtime is carried over. Installation state
(``App``, ``Icons``, ``Shortcuts`` and the per-version ``gupkg-config.toml``)
is kept.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from gupkg.integrity import verify_version_directory
from gupkg.origin import safe_extract_zip


PKG_MODULE_API = 1

# Entries of a version directory that hold installation state, not release code.
_KEEP_TOP_LEVEL = {"app", "icons", "shortcuts"}


def unpack_app(context: dict[str, Any]) -> None:
    """Replace the staged version tree with the release ZIP's tree.

    Parameters
    ----------
    context : dict[str, Any]
        Update context with the candidate and the downloaded artifact and
        staging paths.

    Raises
    ------
    RuntimeError
        The ZIP has no gupkg version tree, does not match its manifest, was
        built from another commit, or declares another version.
    """
    candidate = context["candidate"]
    paths = context["paths"]
    artifact, stage, stage_app = Path(paths["artifact"]), Path(paths["stageRoot"]), Path(paths["stageApp"])

    # Extract the ZIP and find its single version tree (the folder that holds
    # pkg.toml and the gupkg package).
    extract = stage.parent / "release-extract"
    safe_extract_zip(artifact, extract)
    trees = [path.parent for path in extract.glob("*/pkg.toml") if (path.parent / "gupkg" / "cli.py").is_file()]
    if len(trees) != 1:
        raise RuntimeError("Release ZIP does not contain exactly one gupkg version tree")
    tree = trees[0]

    # The whole tree must match the manifest built with the release; any
    # difference means a damaged or tampered ZIP, and nothing is staged.
    report = verify_version_directory(tree)
    if not report.ok:
        shown = "; ".join(report.problems()[:5])
        raise RuntimeError(f"Release ZIP does not match its MANIFEST.sha256 ({shown})")

    # The ZIP must have been built from the listed commit and be the listed version.
    release_note = (tree / "gupkg" / "RELEASE.txt").read_text(encoding="utf-8")
    commit = re.search(r"(?m)^commit ([0-9a-f]{40})$", release_note)
    if commit is None or commit.group(1) != candidate["commit"]:
        raise RuntimeError(
            f"Release ZIP was built from commit {commit.group(1) if commit else None!r}, "
            f"not the listed {candidate['commit']!r}"
        )
    match = re.search(
        r'(?m)^__version__ = "([^"]*)"', (tree / "gupkg" / "_version.py").read_text(encoding="utf-8")
    )
    version = match.group(1) if match else None
    if version != candidate["version"]:
        raise RuntimeError(f"Release version {version!r} is not {candidate['version']!r}")

    # Remove the previous version's code, interpreter and launchers; only the
    # installation state survives, and the per-version manager configuration
    # is set aside so it can be put back inside the new package folder.
    saved_config = None
    for entry in list(stage.iterdir()):
        if entry.name.casefold() in _KEEP_TOP_LEVEL:
            continue
        if entry.name == "gupkg":
            config_file = entry / "gupkg-config.toml"
            if config_file.is_file():
                saved_config = config_file.read_bytes()
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()

    # Lay the verified tree into the stage and restore the saved configuration.
    shutil.copytree(tree, stage, dirs_exist_ok=True)
    if saved_config is not None:
        (stage / "gupkg" / "gupkg-config.toml").write_bytes(saved_config)
    stage_app.mkdir(parents=True, exist_ok=True)
    (stage_app / "RELEASE.txt").write_text(
        f"gupkg {candidate['version']}\ncommit {candidate['commit']}\n", encoding="utf-8"
    )
