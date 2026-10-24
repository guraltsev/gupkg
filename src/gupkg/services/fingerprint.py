"""Compute and verify the fingerprint of a pinned version directory (design 9.2, 17; decision D29).

A fingerprint is one SHA-256 over the files of a version directory. The registry tool writes it
into ``_registry.toml`` under ``[fingerprints]``; gupkg computes it again for a distribution folder
that is installed without a download, and again in ``check``. The same function serves all three,
so the algorithm below is the contract.

Usage and API
-------------
``fingerprint_directory(path)`` returns the 64-digit lower-case hexadecimal fingerprint.
``file_digests(path)`` returns ``{relative path: sha256}`` for the files it covers, which is what
``differing_files`` compares to name the files that differ when a reference copy is at hand.
``verify_directory(path, expected, reference=None)`` returns the problems (an empty list means the
directory matches). Every function raises ``FingerprintError`` for a tree that cannot be hashed
safely: a link, a junction, a special file or an unusable name.

The algorithm (version 1)
-------------------------
1. The files covered are every regular file below the directory, except the top-level entries
   ``App`` and ``gupkg`` (the unpacked payload and a bundled gupkg are not part of a pinned version;
   letter case is ignored for those two names). Nothing else is skipped: ``pkg.toml``, ``pkg.local``,
   the launchers and any support file all count.
2. Each file is described by one text line ``<size> <sha256 of its bytes> <path>`` followed by
   ``\\n``. The size is decimal with no padding; the hash is lower-case hexadecimal; the path is
   relative to the directory, uses ``/`` as separator and is written as UTF-8.
3. The lines are sorted by the UTF-8 bytes of the path, so the result does not depend on directory
   order or on the platform.
4. The fingerprint is the SHA-256 of the text ``gupkg-fingerprint-1\\n`` followed by the lines.

What is deliberately not part of it: timestamps, permissions and attributes; directories as such
(an empty directory has no effect, because Git cannot store one). File bytes are hashed as they are,
with **no line-ending normalisation**: a change of one byte, including ``\\r\\n`` against ``\\n``, changes
the fingerprint. The registry therefore stores every file byte for byte (``* -text`` in its
``.gitattributes``). A file with no content is covered (its hash is that of the empty input).
Links, junctions and other reparse points are refused rather than skipped or followed: a tree that
contains one is not a tree this fingerprint can vouch for.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .safepath import UnsafePath, check_member_name, is_reparse_point

FINGERPRINT_ALGORITHM = 1
HEADER = f"gupkg-fingerprint-{FINGERPRINT_ALGORITHM}\n"
EXCLUDED_TOP_LEVEL = frozenset({"app", "gupkg"})
_CHUNK = 1024 * 1024


class FingerprintError(ValueError):
    """Report a tree that cannot be fingerprinted: a link, a special file or an unusable name."""


def _file_sha256(path: Path) -> tuple[str, int]:
    """Return the SHA-256 and the size of the file at *path*, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _entries(directory: Path) -> dict[str, tuple[str, int]]:
    """Return ``{relative path: (sha256, size)}`` for every covered file below *directory*."""
    root = Path(directory)
    if not root.is_dir() or is_reparse_point(root):
        raise FingerprintError(f"{root} is not a plain directory")
    found: dict[str, tuple[str, int]] = {}
    seen: dict[str, str] = {}
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            children = sorted(os.scandir(folder), key=lambda entry: entry.name)
        except OSError as error:
            raise FingerprintError(f"cannot read {folder}: {error}") from error
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if folder == root and child.name.casefold() in EXCLUDED_TOP_LEVEL:
                continue
            if is_reparse_point(path):
                raise FingerprintError(f"{relative} is a link or junction; a fingerprint is never computed over one")
            try:
                check_member_name(relative)
                relative.encode("utf-8")
            except (UnsafePath, UnicodeEncodeError) as error:
                raise FingerprintError(f"{relative}: {error}") from error
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                stack.append(path)
            elif stat.S_ISREG(mode):
                if seen.setdefault(relative.casefold(), relative) != relative:
                    raise FingerprintError(f"{relative} and {seen[relative.casefold()]} differ only in letter case")
                try:
                    found[relative] = _file_sha256(path)
                except OSError as error:
                    raise FingerprintError(f"cannot read {relative}: {error}") from error
            else:
                raise FingerprintError(f"{relative} is not a regular file")
    return found


def file_digests(directory: Path) -> dict[str, str]:
    """Return ``{relative path: sha256}`` for the files the fingerprint of *directory* covers."""
    return {name: digest for name, (digest, _size) in _entries(directory).items()}


def fingerprint_directory(directory: Path) -> str:
    """Return the fingerprint of *directory* (algorithm version 1, described in the module docstring).

    Raises
    ------
    FingerprintError
        If the tree holds a link, junction, special file, unusable name or unreadable file.
    """
    entries = _entries(directory)
    lines = [f"{size} {digest} {name}\n" for name, (digest, size) in entries.items()]
    lines.sort(key=lambda line: line.split(" ", 2)[2].encode("utf-8"))
    return hashlib.sha256((HEADER + "".join(lines)).encode("utf-8")).hexdigest()


def differing_files(expected: Mapping[str, str], actual: Mapping[str, str]) -> list[str]:
    """Name the files that differ between two ``file_digests`` results (changed, missing or extra)."""
    names = sorted(set(expected) | set(actual))
    result: list[str] = []
    for name in names:
        if name not in actual:
            result.append(f"{name} (missing)")
        elif name not in expected:
            result.append(f"{name} (extra)")
        elif expected[name] != actual[name]:
            result.append(f"{name} (changed)")
    return result


def verify_directory(directory: Path, expected: str, reference: Path | None = None) -> list[str]:
    """Check *directory* against the published fingerprint *expected*.

    Parameters
    ----------
    directory : Path
        The copy to check, for example a distribution folder.
    expected : str
        The fingerprint the registry publishes.
    reference : Path, optional
        The registry's own copy of the version directory. When given and the fingerprints differ,
        the files that differ are named.

    Returns
    -------
    list of str
        Empty when the fingerprint matches; otherwise the problems, never an exception for a
        mismatch. A tree that cannot be hashed is reported as a problem too (fail closed).
    """
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        return [f"the published fingerprint {expected!r} is not a SHA-256 in lower-case hexadecimal"]
    try:
        actual = fingerprint_directory(directory)
    except FingerprintError as error:
        return [f"cannot compute the fingerprint: {error}"]
    if actual == expected:
        return []
    problems = [f"the fingerprint is {actual}, the registry publishes {expected}"]
    if reference is not None:
        try:
            problems.extend(f"differs: {line}" for line in differing_files(file_digests(reference), file_digests(directory)))
        except FingerprintError as error:
            problems.append(f"cannot compare with the registry copy: {error}")
    return problems
