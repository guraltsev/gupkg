"""Fingerprint an ``App`` folder and a version directory, and compare either with what was recorded.

Every install records the SHA-256 of every file of its ``App`` (decision D41), so ``gupkg check`` can say
which files were modified, deleted or added. A version directory has one more fingerprint: a single SHA-256
over its files, which a registry may publish and which verifies a package that was installed without a
download (decision D29). Neither walk follows a link or junction: a link is hashed by name and never entered,
so a persisted portable folder or a planted junction cannot lead the walk out of the tree.

Usage and API
-------------
``app_files(app)`` returns ``{relative path: sha256}`` for an ``App``; ``diff_app(app, recorded, skip=...)``
returns the modified, missing and extra paths against a record. ``version_fingerprint(directory,
include_app=...)`` returns the single digest of a version directory and ``tree_differences(first, second)``
names the files that differ between two such directories.

Implementation Approach
-----------------------
Paths are recorded with ``/`` separators and compared without regard to letter case, as Windows does.
The version fingerprint covers the file names (letter case folded) and the SHA-256 of each file's content,
in sorted order, so it does not depend on directory order, and covers every file below the version
directory except ``.gupkg`` and, unless the version ships its ``App``, the ``App`` folder (a downloaded ``App``
is a product of the version, not part of its definition). A shipped ``App`` is part of what the package
distributes, so it is included.
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


def _file_digest(path: Path) -> str:
    """Return the SHA-256 of the file *path*, read in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path):
    """Yield ``(relative posix path, absolute path, is_link)`` for every file and link below *root*, links not entered."""
    stack = [Path(root)]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            if is_reparse_point(path):
                yield relative, path, True
            elif entry.is_dir(follow_symlinks=False):
                stack.append(path)
            else:
                yield relative, path, False


def app_files(app: Path) -> dict[str, str]:
    """Return the SHA-256 of every file of *app*, keyed by its relative path; links are marked, not followed.

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
    """Return one SHA-256 over a ``{path: digest}`` record (paths with letter case folded, sorted)."""
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
    prefixes = [item.replace("\\", "/").strip("/").casefold() for item in skip if item.strip()]

    def skipped(path: str) -> bool:
        folded = path.casefold()
        return any(folded == prefix or folded.startswith(prefix + "/") for prefix in prefixes)

    now = {key: value for key, value in app_files(app).items() if not skipped(key)}
    before = {key: value for key, value in recorded.items() if not skipped(key)}
    now_by_name = {key.casefold(): (key, value) for key, value in now.items()}
    before_by_name = {key.casefold(): (key, value) for key, value in before.items()}
    modified = sorted(before_by_name[name][0] for name in before_by_name if name in now_by_name and now_by_name[name][1] != before_by_name[name][1])
    missing = sorted(before_by_name[name][0] for name in before_by_name if name not in now_by_name)
    extra = sorted(now_by_name[name][0] for name in now_by_name if name not in before_by_name)
    return modified, missing, extra


def _version_entries(directory: Path, include_app: bool) -> dict[str, Path | None]:
    """Return ``{folded relative path: file}`` (``None`` for a link) of the files a version fingerprint covers."""
    entries: dict[str, Path | None] = {}
    for relative, path, is_link in _walk(directory):
        top = relative.split("/", 1)[0].casefold()
        if top == ".gupkg" or (top == "app" and not include_app):
            continue
        entries[relative.casefold() + ("\0link" if is_link else "")] = None if is_link else path
    return entries


def version_fingerprint(directory: Path, *, include_app: bool = False) -> str:
    """Return the single SHA-256 that fingerprints the version directory *directory*.

    Parameters
    ----------
    directory : Path
        A version directory.
    include_app : bool
        Whether ``App`` is part of the fingerprint: true for a version that ships its ``App``.

    Returns
    -------
    str
        Lower-case hexadecimal digest over the sorted file names and contents.

    Raises
    ------
    OSError
        If a file cannot be read.
    """
    digest = hashlib.sha256()
    for label, path in sorted(_version_entries(directory, include_app).items()):
        digest.update(label.encode("utf-8") + b"\0")
        if path is not None:
            digest.update(bytes.fromhex(_file_digest(path)))
    return digest.hexdigest()


def tree_differences(first: Path, second: Path, *, include_app: bool = False) -> list[str]:
    """Name the files that differ between two version directories (present in one, absent in the other, or changed)."""
    left, right = _version_entries(first, include_app), _version_entries(second, include_app)
    differing = []
    for label in sorted(set(left) | set(right)):
        if label not in left or label not in right:
            differing.append(label.split("\0")[0])
        elif left[label] is not None and _file_digest(left[label]) != _file_digest(right[label]):
            differing.append(label.split("\0")[0])
    return differing
