"""Parse and order package version names and recognise reserved directory names.

Usage and API
-------------
``compare_versions(left, right)`` orders two version directory names like 0.17
did. ``sort_versions(names)`` and ``newest_version(names)`` order a listing and
skip every name that is not a version. ``is_version_name(name)`` says whether a
name can be a version directory, ``is_version_dir(name, children)`` and
``is_tracker_dir(name, children)`` add the "contains ``pkg.toml``" test for a
directory whose child names the caller has listed.

Implementation Approach
-----------------------
Ordering is the 0.17 rule, unchanged for real versions: dotted segments compare
numerically when both are numbers, a ``-prerelease`` sorts before its release,
and the text after the first ``.local.`` is a local revision that sorts after the
plain version. Reserved names are filtered before any ordering happens, so
``_check_updates`` (and 0.17's ``_bootstrap``, which merely starts with ``_``),
``current`` and ``data`` can never be the newest version. The module does no
input or output: directory listings are passed in by the caller.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from functools import cmp_to_key

LOCAL_VERSION_MARKER = ".local."

# The package-root names that are never versions. Anything starting with ``.`` or
# ``_`` is reserved as well (see ``is_reserved_name``).
TRACKER_DIR_NAME = "_check_updates"
CURRENT_DIR_NAME = "current"
DATA_DIR_NAME = "data"
RESERVED_NAMES = frozenset({CURRENT_DIR_NAME, TRACKER_DIR_NAME, DATA_DIR_NAME})
MANIFEST_NAME = "pkg.toml"

# Characters that are never part of a version name: path syntax, control characters (C0 and C1), line and
# paragraph separators, zero-width and bidirectional controls, and the byte order mark.
_UNSAFE_NAME_CHARACTERS = re.compile(
    r'[<>:"/\\|?*]|[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]'
)
# Tokens a shell or Windows could expand or shorten: ``%TEMP%``, ``$HOME``, ``PROGRA~1``.
_EXPANDING_TOKENS = re.compile(r"[%$]|~\d")
_DIGITS = "123456789\u00b9\u00b2\u00b3"
_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"} | {f"com{d}" for d in _DIGITS} | {f"lpt{d}" for d in _DIGITS}
)


def is_reserved_name(name: str) -> bool:
    """Return whether *name* is reserved in a package root.

    Parameters
    ----------
    name : str
        A child name of a package root.

    Returns
    -------
    bool
        ``True`` for ``current``, ``_check_updates``, ``data`` (any letter case),
        for any name starting with ``.`` or ``_``, and for the swap entries that
        start with ``current.``.
    """
    folded = name.casefold()
    return (
        folded in RESERVED_NAMES
        or folded.startswith((".", "_"))
        or folded.startswith(CURRENT_DIR_NAME + ".")
    )


def is_version_name(name: str) -> bool:
    """Return whether *name* can name a version directory.

    Parameters
    ----------
    name : str
        A candidate directory name.

    Returns
    -------
    bool
        ``True`` for a non-empty, path-safe name that is not reserved. ``False`` for surrounding
        spaces, a trailing dot, a leading ``-``, control and bidirectional characters,
        ``%``, ``$`` and ``~<digit>`` (expansion and short names), and Windows device names
        (``CON``, ``NUL``, ``COM1``, ``LPT1``..., with or without an extension, any letter case).
        ``bootstrap2`` is an ordinary version.
    """
    if not name or name != name.strip() or name.endswith(".") or name.startswith("-"):
        return False
    if _UNSAFE_NAME_CHARACTERS.search(name) or _EXPANDING_TOKENS.search(name):
        return False
    if name.split(".", 1)[0].rstrip(" ").casefold() in _DEVICE_NAMES:
        return False
    return not is_reserved_name(name)


def is_version_dir(name: str, children: Collection[str]) -> bool:
    """Return whether a directory is a version directory.

    Parameters
    ----------
    name : str
        The directory's name.
    children : collection of str
        The names of the directory's children, listed by the caller.

    Returns
    -------
    bool
        ``True`` when the name is a version name and ``pkg.toml`` is among the
        children.
    """
    return is_version_name(name) and MANIFEST_NAME in children


def is_tracker_dir(name: str, children: Collection[str]) -> bool:
    """Return whether a directory is the update tracker ``_check_updates``.

    Parameters
    ----------
    name : str
        The directory's name.
    children : collection of str
        The names of the directory's children, listed by the caller.

    Returns
    -------
    bool
        ``True`` for ``_check_updates`` that contains ``pkg.toml``.
    """
    return name == TRACKER_DIR_NAME and MANIFEST_NAME in children


def split_version(version: str) -> tuple[str, str]:
    """Split a version into its upstream and local-revision parts.

    Parameters
    ----------
    version : str
        A version such as ``1.2.3`` or ``1.2.3.local.000004``.

    Returns
    -------
    tuple of str
        ``(upstream, local_revision)``. The local revision is everything after
        the first ``.local.`` and ``""`` when there is none.
    """
    upstream, _, local = version.strip().partition(LOCAL_VERSION_MARKER)
    return upstream, local


def _identifier(token: str) -> int | str:
    """Return a number for an all-digit token and the lower-cased text otherwise."""
    token = token.strip()
    return int(token) if token.isdecimal() else token.lower()


def _parse_upstream(upstream: str) -> tuple[list[int | str], list[int | str] | None]:
    """Return the main identifiers and the prerelease identifiers (``None`` when stable)."""
    upstream = upstream.strip().split("+", 1)[0]
    prerelease = None
    main_part = upstream
    if "-" in upstream:
        main_part, prerelease_part = upstream.split("-", 1)
        prerelease = [_identifier(part) for part in prerelease_part.split(".") if part != ""]
    return [_identifier(part) for part in main_part.split(".") if part != ""], prerelease


def _compare_lists(left: list[int | str], right: list[int | str], *, pad_numeric: bool) -> int:
    """Compare identifier lists; numbers sort before words, a longer list sorts later."""
    for index in range(max(len(left), len(right))):
        left_value = left[index] if index < len(left) else (0 if pad_numeric else None)
        right_value = right[index] if index < len(right) else (0 if pad_numeric else None)
        if left_value == right_value:
            continue
        if left_value is None:
            return -1
        if right_value is None:
            return 1
        if isinstance(left_value, int) and isinstance(right_value, int):
            return 1 if left_value > right_value else -1
        if isinstance(left_value, int):
            return -1
        if isinstance(right_value, int):
            return 1
        return 1 if str(left_value) > str(right_value) else -1
    return 0


def compare_versions(left: str, right: str) -> int:
    """Compare two version names.

    Parameters
    ----------
    left, right : str
        Version directory names.

    Returns
    -------
    int
        ``1`` when *left* is newer, ``-1`` when *right* is newer, ``0`` when equal.
        The result for real versions is the 0.17 ordering.
    """
    left_upstream, left_local = split_version(left)
    right_upstream, right_local = split_version(right)
    left_main, left_pre = _parse_upstream(left_upstream)
    right_main, right_pre = _parse_upstream(right_upstream)
    result = _compare_lists(left_main, right_main, pad_numeric=True)
    if result:
        return result
    if left_pre is None and right_pre is not None:
        return 1
    if left_pre is not None and right_pre is None:
        return -1
    if left_pre is not None and right_pre is not None:
        result = _compare_lists(left_pre, right_pre, pad_numeric=False)
        if result:
            return result
    # A version without a local revision sorts before one that has it.
    return _compare_lists(
        [_identifier(part) for part in left_local.split(".") if part != ""],
        [_identifier(part) for part in right_local.split(".") if part != ""],
        pad_numeric=False,
    )


def sort_versions(names: Iterable[str]) -> list[str]:
    """Return the version names among *names*, oldest first.

    Parameters
    ----------
    names : iterable of str
        Directory names; reserved and unsafe names are dropped.

    Returns
    -------
    list of str
        The version names ordered by ``compare_versions``.
    """
    return sorted((name for name in names if is_version_name(name)), key=cmp_to_key(compare_versions))


def newest_version(names: Iterable[str]) -> str | None:
    """Return the newest version among *names*, or ``None`` when there is none.

    Parameters
    ----------
    names : iterable of str
        Directory names; reserved names such as ``_check_updates``, ``current``
        and ``data`` never win.

    Returns
    -------
    str or None
        The newest version name.
    """
    ordered = sort_versions(names)
    return ordered[-1] if ordered else None
