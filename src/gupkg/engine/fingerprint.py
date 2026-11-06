"""Fingerprint an ``App`` folder and compare it with what was recorded (decision D41).

Every install records the SHA-256 of every file of its ``App``, so ``gupkg check`` can say which files were modified,
deleted or added. The walk follows no link or junction: a link is hashed by name and never entered, so a persisted
portable folder or a planted junction cannot lead the walk out of the tree.

The fingerprint of a *version directory* (what a registry publishes and what verifies a package installed without a
download, decision D29) is not computed here: there is one implementation, ``gupkg.services.fingerprint``
(``fingerprint_directory``, ``file_digests``, ``differing_files``, ``verify_directory``), whose byte-exact contract is in
design section 17.

Usage and API
-------------
``app_files(app)`` returns ``{relative path: sha256}`` for an ``App``; ``aggregate_digest(files)`` folds such a record into
one digest; ``diff_app(app, recorded, skip=...)`` returns the modified, missing and extra paths against a record.

Implementation Approach
-----------------------
Paths are recorded with ``/`` separators and compared without regard to letter case, as Windows does.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Collection, Mapping
from pathlib import Path

from ..catalog.local import is_reparse_point

# What a link or junction is recorded as: its name only, never its target or its contents.
LINK_MARK = "<link>"

# The chunk size used to hash large files.
_CHUNK = 1024 * 1024


def app_files(app: Path) -> dict[str, str]:
    """Return the SHA-256 of every file of *app*, keyed by its relative path; links are marked, not followed.

    Parameters
    ----------
    app : Path
        The ``App`` folder to fingerprint.

    Returns
    -------
    dict[str, str]
        Relative ``/`` path to SHA-256 (or ``LINK_MARK`` for a link), sorted without regard to case.

    Raises
    ------
    OSError
        If a file cannot be read.
    """
    found: dict[str, str] = {}
    for relative, path, is_link in _walk(app):
        found[relative] = LINK_MARK if is_link else _file_digest(path)
    return dict(sorted(found.items(), key=lambda item: item[0].casefold()))


def aggregate_digest(files: Mapping[str, str]) -> str:
    """Return one SHA-256 over a ``{path: digest}`` record (paths with letter case folded, sorted).

    Parameters
    ----------
    files : Mapping[str, str]
        ``{relative path: sha256}`` as returned by ``app_files``.

    Returns
    -------
    str
        The hex digest of the whole record.

    Examples
    --------
    Basic usage:

    >>> aggregate_digest({})
    'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

    The digest ignores order and letter case of the paths:

    >>> aggregate_digest({"a": "1", "b": "2"}) == aggregate_digest({"B": "2", "A": "1"})
    True
    >>> aggregate_digest({"a": "1"}) == aggregate_digest({"a": "2"})
    False
    """
    digest = hashlib.sha256()
    for path, value in sorted((key.casefold(), val) for key, val in files.items()):
        digest.update(f"{path}\0{value}\n".encode("utf-8"))
    return digest.hexdigest()


def diff_app(app: Path, recorded: Mapping[str, str], skip: Collection[str] = ()) -> tuple[list[str], list[str], list[str]]:
    """Compare *app* with a recorded fingerprint.

    Parameters
    ----------
    app : Path
        The ``App`` folder.
    recorded : mapping
        ``{relative path: sha256}`` as recorded at install time.
    skip : collection of str
        Relative paths (folders or files) that are state, not code: a portable package's persisted items.
        Neither they nor anything below them is compared.

    Returns
    -------
    tuple of list
        ``(modified, missing, extra)`` relative paths, sorted.
    """
    # Normalise the skipped paths to case-folded ``/`` prefixes so folders and files match alike.
    prefixes = [item.replace("\\", "/").strip("/").casefold() for item in skip if item.strip()]

    def _is_skipped(path: str) -> bool:
        """Return whether *path* is a skipped item or lies below one."""
        folded = path.casefold()
        return any(folded == prefix or folded.startswith(prefix + "/") for prefix in prefixes)

    # Compare by case-folded name, as Windows does, after dropping the skipped state paths from both sides.
    now = {key: value for key, value in app_files(app).items() if not _is_skipped(key)}
    before = {key: value for key, value in recorded.items() if not _is_skipped(key)}
    now_by_name = {key.casefold(): (key, value) for key, value in now.items()}
    before_by_name = {key.casefold(): (key, value) for key, value in before.items()}

    # Modified: in both with a different digest; missing: only recorded; extra: only present now.
    modified = sorted(before_by_name[name][0] for name in before_by_name if name in now_by_name and now_by_name[name][1] != before_by_name[name][1])
    missing = sorted(before_by_name[name][0] for name in before_by_name if name not in now_by_name)
    extra = sorted(now_by_name[name][0] for name in now_by_name if name not in before_by_name)
    return modified, missing, extra


# --- supporting helpers ---


def _file_digest(path: Path) -> str:
    """Return the SHA-256 of the file *path*, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path):
    """Yield ``(relative posix path, absolute path, is_link)`` for every file and link below *root*, links not entered."""
    # Depth-first walk with an explicit stack; an unreadable folder is passed over rather than failing the whole walk.
    stack = [Path(root)]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue

        # Links are yielded by name and never entered; real folders are queued, everything else is a file.
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if is_reparse_point(path):
                yield relative, path, True
            elif entry.is_dir(follow_symlinks=False):
                stack.append(path)
            else:
                yield relative, path, False
