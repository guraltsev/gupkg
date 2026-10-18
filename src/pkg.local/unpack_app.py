"""Stage a gupkg release from its commit source archive.

The gupkg package keeps its whole tree (manifest, launchers, and the ``gupkg``
Python package) at the version root rather than below ``App``. This hook
therefore replaces the carried-over support tree in the staged version with the
archive's ``src`` folder, and leaves a small marker in ``App`` so the staged
version counts as a prepared payload.

Usage and API
-------------
The package manager calls ``unpack_app(context)`` after downloading the
candidate archive.

Implementation Approach
-----------------------
GitHub stores the archived commit hash in the ZIP comment. The hook requires it
to equal the commit listed in RELEASES.md, extracts safely into the work area,
checks that the manifest version matches the candidate, and only then replaces
the staged tree. Stale ``gupkg`` Python files from the previous version are
removed first so no deleted module survives an update; the embedded Python
and native launchers are kept.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from gupkg.origin import safe_extract_zip


PKG_MODULE_API = 1


def unpack_app(context: dict[str, Any]) -> None:
    """Replace the staged version tree with the archive's ``src`` contents.

    Parameters
    ----------
    context : dict[str, Any]
        Update context with the candidate and the downloaded artifact and
        staging paths.

    Raises
    ------
    RuntimeError
        The archive was not built from the listed commit, has no gupkg
        ``src`` folder, or its runtime version differs from the candidate.
    """
    candidate = context["candidate"]
    paths = context["paths"]
    artifact, stage, stage_app = Path(paths["artifact"]), Path(paths["stageRoot"]), Path(paths["stageApp"])

    with zipfile.ZipFile(artifact) as archive:
        embedded = archive.comment.decode("ascii", errors="replace").strip().lower()
    if embedded != candidate["commit"]:
        raise RuntimeError(
            f"Release archive is for commit {embedded!r}, not the listed {candidate['commit']!r}"
        )

    extract = stage.parent / "release-extract"
    safe_extract_zip(artifact, extract)
    sources = [path.parent for path in extract.glob("*/src/pkg.toml")]
    if len(sources) != 1 or not (sources[0] / "gupkg" / "cli.py").is_file():
        raise RuntimeError("Release archive does not contain a gupkg src folder")
    source = sources[0]

    # The runtime module is the one place the release number is declared.
    match = re.search(
        r'(?m)^__version__ = "([^"]*)"', (source / "gupkg" / "_version.py").read_text(encoding="utf-8")
    )
    version = match.group(1) if match else None
    if version != candidate["version"]:
        raise RuntimeError(f"Release version {version!r} is not {candidate['version']!r}")

    # Drop the previous code, then lay the new tree over the carried-over
    # support files (pkg.local hooks, config.default, and similar).
    # Anything else under gupkg (the embedded Python, native launchers) is
    # install-specific and carries over.
    old = stage / "gupkg"
    if old.is_dir():
        replaced = {entry.name for entry in (source / "gupkg").iterdir()}
        for entry in old.iterdir():
            if entry.name in replaced or entry.suffix == ".py" or entry.name == "__pycache__":
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    shutil.copytree(source, stage, dirs_exist_ok=True)
    stage_app.mkdir(parents=True, exist_ok=True)
    (stage_app / "RELEASE.txt").write_text(
        f"gupkg {candidate['version']}\ncommit {candidate['commit']}\n", encoding="utf-8"
    )
