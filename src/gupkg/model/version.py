"""Parse and order package version names and recognise reserved directory names.

A version name is the name of a directory in a package root. Names that are reserved
or unsafe as folder names are never versions, and valid names are ordered numerically
by dotted segment, with prereleases before their release and local revisions after.

Usage and API
-------------
``compare_versions(left, right)`` orders two version directory names.
``sort_versions(names)`` and ``newest_version(names)`` order a listing and
skip every name that is not a version. ``is_version_name(name)`` says whether a
name can be a version directory, ``is_version_dir(name, children)`` and
``is_tracker_dir(name, children)`` add the "contains ``pkg.toml``" test for a
directory whose child names the caller has listed.

Implementation Approach
-----------------------
Dotted segments compare
numerically when both are numbers, a ``-prerelease`` sorts before its release,
and the text after the first ``.local.`` is a local revision that sorts after the
plain version. Reserved names are filtered before any ordering happens, so
``_check_updates`` and every other name that starts with ``_`` or ``.``,
``current`` and ``data`` can never be the newest version. The module does no
input or output: directory listings are passed in by the caller.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable
from functools import cmp_to_key

# The text that separates a version's upstream part from its local revision.
LOCAL_VERSION_MARKER = ".local."

# The package-root names that are never versions. Anything starting with ``.`` or
# ``_`` is reserved as well (see ``is_reserved_name``).
TRACKER_DIR_NAME = "_check_updates"  # the update tracker directory
CURRENT_DIR_NAME = "current"  # the junction to the active version
DATA_DIR_NAME = "data"  # a portable package's data folder
# Every fixed reserved name.
RESERVED_NAMES = frozenset({CURRENT_DIR_NAME, TRACKER_DIR_NAME, DATA_DIR_NAME})
# The manifest file that makes a directory a version or tracker directory.
MANIFEST_NAME = "pkg.toml"

# Characters that are never part of a version name: path syntax, control characters (C0 and C1), line and
# paragraph separators, zero-width and bidirectional controls, and the byte order mark.
_UNSAFE_NAME_CHARACTERS = re.compile(
    r'[<>:"/\\|?*]|[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]'
)
# Unicode categories that make a name invisible or ambiguous: controls, format characters (soft hyphen, word joiner,
# tag characters, bidirectional marks), line and paragraph separators, private use, surrogates and unassigned code points;
# and the variation selectors, which change nothing a reader can see.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn", "Cs"})
# Variation selectors, which change nothing a reader can see.
_VARIATION_SELECTORS = re.compile("[\ufe00-\ufe0f\U000e0100-\U000e01ef]")
# The longest version name that is accepted.
MAX_NAME_LENGTH = 128
# Tokens a shell or Windows could expand or shorten: ``%TEMP%``, ``$HOME``, ``PROGRA~1``.
_EXPANDING_TOKENS = re.compile(r"[%$]|~\d")
# The digits that Windows accepts after ``COM`` and ``LPT`` (including superscripts).
_DIGITS = "123456789\u00b9\u00b2\u00b3"
# Windows device names, which cannot be folder names with or without an extension.
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

    Examples
    --------
    Basic usage:

    >>> is_reserved_name("current")
    True
    >>> is_reserved_name("Data")
    True
    >>> is_reserved_name("1.2.3")
    False

    Edge cases:

    >>> is_reserved_name("current.old")
    True
    >>> is_reserved_name("_check_updates")
    True
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
        (``CON``, ``NUL``, ``COM1``, ``LPT1``..., with or without an extension, any letter case),
        Unicode control, format, separator, private-use, surrogate and unassigned characters, variation
        selectors, and names longer than ``MAX_NAME_LENGTH`` (128; a version that a tracker discovers
        is capped at 64 by ``valid_version``). ``bootstrap2`` is an ordinary version.

    Examples
    --------
    Basic usage:

    >>> is_version_name("1.2.3")
    True
    >>> is_version_name("bootstrap2")
    True

    Edge cases:

    >>> is_version_name("current")
    False
    >>> is_version_name("CON")
    False
    >>> is_version_name("%TEMP%")
    False
    >>> is_version_name("-1.0")
    False
    """
    # Shape of the name first, then forbidden characters, then device names and reserved names.
    if not name or name != name.strip() or name.endswith(".") or name.startswith("-"):
        return False
    if len(name) > MAX_NAME_LENGTH or _UNSAFE_NAME_CHARACTERS.search(name) or _EXPANDING_TOKENS.search(name):
        return False
    if _VARIATION_SELECTORS.search(name) or any(unicodedata.category(char) in _UNSAFE_CATEGORIES for char in name):
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

    Examples
    --------
    Basic usage:

    >>> is_version_dir("1.2.3", ["pkg.toml", "App"])
    True
    >>> is_version_dir("1.2.3", ["App"])
    False
    >>> is_version_dir("current", ["pkg.toml"])
    False
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

    Examples
    --------
    Basic usage:

    >>> is_tracker_dir("_check_updates", ["pkg.toml"])
    True
    >>> is_tracker_dir("_check_updates", [])
    False
    >>> is_tracker_dir("1.2.3", ["pkg.toml"])
    False
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

    Examples
    --------
    Basic usage:

    >>> split_version("1.2.3")
    ('1.2.3', '')

    Local revisions:

    >>> split_version("1.2.3.local.000004")
    ('1.2.3', '000004')
    """
    upstream, _, local = version.strip().partition(LOCAL_VERSION_MARKER)
    return upstream, local


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

    Examples
    --------
    Basic usage:

    >>> compare_versions("1.10", "1.9")
    1
    >>> compare_versions("1.2", "1.10")
    -1
    >>> compare_versions("1.0", "1.0.0")
    0

    Edge cases:

    >>> compare_versions("1.0-rc1", "1.0")
    -1
    >>> compare_versions("1.0", "1.0.local.1")
    -1
    """
    # Split off the local revisions and compare the main identifiers first.
    left_upstream, left_local = split_version(left)
    right_upstream, right_local = split_version(right)
    left_main, left_pre = _parse_upstream(left_upstream)
    right_main, right_pre = _parse_upstream(right_upstream)
    result = _compare_lists(left_main, right_main, pad_numeric=True)
    if result:
        return result
    # Equal main parts: a prerelease sorts before its release, and two prereleases compare by identifier.
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

    Examples
    --------
    Basic usage:

    >>> sort_versions(["1.10", "1.2", "1.2-beta"])
    ['1.2-beta', '1.2', '1.10']

    Edge cases:

    >>> sort_versions(["1.0", "current", "_check_updates", "data"])
    ['1.0']
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

    Examples
    --------
    Basic usage:

    >>> newest_version(["1.2", "1.10", "1.9"])
    '1.10'

    Edge cases:

    >>> newest_version(["current", "_check_updates"]) is None
    True
    """
    ordered = sort_versions(names)
    return ordered[-1] if ordered else None


# --- supporting helpers ---


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
    # Compare position by position; a missing position pads with zero (numeric) or sorts earlier (prerelease).
    for index in range(max(len(left), len(right))):
        left_value = left[index] if index < len(left) else (0 if pad_numeric else None)
        right_value = right[index] if index < len(right) else (0 if pad_numeric else None)
        if left_value == right_value:
            continue
        if left_value is None:
            return -1
        if right_value is None:
            return 1
        # Numbers compare numerically and sort before words; two words compare as text.
        if isinstance(left_value, int) and isinstance(right_value, int):
            return 1 if left_value > right_value else -1
        if isinstance(left_value, int):
            return -1
        if isinstance(right_value, int):
            return 1
        return 1 if str(left_value) > str(right_value) else -1
    return 0
