"""Parse and expand the ``pkg.toml`` template language and path variables (design sections 7.1 and 7.5).

Templates are ``${version}`` and named-capture references with optional filters;
path variables such as ``$App`` are listed here but expanded at install time.
A malformed reference is refused with ``TemplateError``.

Usage and API
-------------
``references(text)`` lists the ``${...}`` references in a string and rejects a
malformed one with ``TemplateError``. ``render(text, values)`` expands the
references whose name is in *values*. ``is_version_template(ref)`` tells a
template (``${version}``, a filtered reference) from a plain braced environment
reference such as ``${USERPROFILE}``. ``path_variables(text)`` lists the
``$App``-style path variables of a string.

Implementation Approach
-----------------------
``${version}`` and named captures (``${tag}``) are expanded by the tracker when it
writes a version directory. A reference may carry filters, separated by ``|``:
``replace:a:b``, ``major``, ``minor``, ``patch`` and ``nodots``. A braced name
that is neither ``version`` nor a capture of the track result stays untouched, so
an environment reference such as ``${USERPROFILE}`` survives. ``$App``-style path
variables are not templates: they are expanded at install time. Everything here
is a pure string function.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


class TemplateError(ValueError):
    """Report a malformed ``${...}`` reference."""


# The variables of design section 7.5 that expand to a path.
PATH_VARIABLES = frozenset({"App", "VersionRoot", "PackageRoot", "Bin"})
# ``$Stage`` is the staging directory of a vendor installer and is valid only there.
INSTALLER_VARIABLES = frozenset({"Stage"})

# The filters a reference may carry, applied left to right.
FILTER_NAMES = ("replace", "major", "minor", "patch", "nodots")

# A valid reference name: letters, digits and underscores, not starting with a digit.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# ``$$`` is a literal dollar sign and never starts a reference.
_BRACED = re.compile(r"\$\$|\$\{([^}]*)\}")
# A ``$Name`` path variable (but not ``${...}``); ``$$`` is skipped.
_PATH_VARIABLE = re.compile(r"\$\$|\$(?!\{)([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class Reference:
    """One parsed ``${name|filter|...}`` reference.

    Attributes
    ----------
    name : str
        The variable name.
    filters : tuple[tuple[str, ...], ...]
        The filters, each as a name followed by its arguments.
    text : str
        The reference as written.
    """

    name: str
    filters: tuple[tuple[str, ...], ...]
    text: str


def references(text: str) -> tuple[Reference, ...]:
    """List the ``${...}`` references in *text*, in order.

    Parameters
    ----------
    text : str
        Any string of a ``pkg.toml``.

    Returns
    -------
    tuple of Reference
        The references; ``$$`` escapes are skipped.

    Raises
    ------
    TemplateError
        If a reference has an invalid name or an unknown or malformed filter.

    Examples
    --------
    Basic usage:

    >>> [(ref.name, ref.filters) for ref in references("tool-${version}-${tag|major}")]
    [('version', ()), ('tag', (('major',),))]

    Edge cases:

    >>> references("a $${version} escape")
    ()
    >>> references("${tag|bogus}")
    Traceback (most recent call last):
        ...
    gupkg.model.template.TemplateError: ${tag|bogus} uses the unknown filter 'bogus'; use one of replace, major, minor, patch, nodots
    """
    found = []
    for match in _BRACED.finditer(text):
        if match.group(1) is not None:
            found.append(_parse_reference(match.group(1), match.group(0)))
    return tuple(found)


def is_version_template(reference: Reference) -> bool:
    """Return whether *reference* is a template rather than an environment reference.

    Parameters
    ----------
    reference : Reference
        A parsed reference.

    Returns
    -------
    bool
        ``True`` for ``${version}`` and for any reference with a filter.

    Examples
    --------
    Basic usage:

    >>> is_version_template(references("${version}")[0])
    True
    >>> is_version_template(references("${tag|major}")[0])
    True
    >>> is_version_template(references("${USERPROFILE}")[0])
    False
    """
    return reference.name == "version" or bool(reference.filters)


def path_variables(text: str) -> tuple[str, ...]:
    """List the ``$Name`` path variables used in *text*.

    Parameters
    ----------
    text : str
        A string that is expanded as a path or command line.

    Returns
    -------
    tuple of str
        The names after ``$``, in order. ``$$`` and ``${...}`` are not variables.

    Examples
    --------
    Basic usage:

    >>> path_variables("$App/bin and $Data")
    ('App', 'Data')

    Edge cases:

    >>> path_variables("$$App and ${version}")
    ()
    """
    return tuple(match.group(1) for match in _PATH_VARIABLE.finditer(text) if match.group(1))


def render(text: str, values: Mapping[str, str]) -> str:
    """Expand the references in *text* whose name is a key of *values*.

    Parameters
    ----------
    text : str
        A string that may contain ``${version}`` or capture references.
    values : mapping of str to str
        ``version`` and the track result's captures.

    Returns
    -------
    str
        The expanded text. A reference whose name is not in *values* is kept as
        written, so environment references survive.

    Raises
    ------
    TemplateError
        If a reference is malformed.

    Examples
    --------
    Basic usage:

    >>> render("tool-${version}.zip", {"version": "1.2.3"})
    'tool-1.2.3.zip'

    Filters:

    >>> render("${version|major}.${version|minor}", {"version": "1.2.3"})
    '1.2'
    >>> render("${version|nodots}", {"version": "1.2.3"})
    '123'
    >>> render("${version|replace:.:_}", {"version": "1.2.3"})
    '1_2_3'

    Edge cases:

    >>> render("${USERPROFILE}/${version}", {"version": "1.0"})
    '${USERPROFILE}/1.0'
    >>> render("$${version}", {"version": "1.0"})
    '$${version}'
    """
    # Validate every reference first so a malformed one is refused before anything is expanded.
    references(text)

    def _replace(match: re.Match[str]) -> str:
        """Return the expansion of one reference, or the reference itself when its name is unknown."""
        if match.group(1) is None:
            return match.group(0)
        # Unknown names are left alone; known ones are expanded and then filtered left to right.
        reference = _parse_reference(match.group(1), match.group(0))
        if reference.name not in values:
            return match.group(0)
        value = str(values[reference.name])
        for parts in reference.filters:
            value = _apply_filter(value, parts)
        return value

    # Expand every reference through the callback; ``$$`` escapes pass through unchanged.
    return _BRACED.sub(_replace, text)


# --- supporting helpers ---


def _parse_reference(body: str, text: str) -> Reference:
    """Parse the inside of one ``${...}``; reject empty names and unknown filters.

    Raises
    ------
    TemplateError
        If the name is invalid or a filter is unknown or used with the wrong arguments.
    """
    # The name comes before the first ``|`` and must be a plain identifier.
    name, *filter_texts = body.split("|")
    name = name.strip()
    if not _NAME.fullmatch(name):
        raise TemplateError(f"{text} is not a valid reference: the name must be letters, digits and underscores")
    filters: list[tuple[str, ...]] = []
    # Each filter must be known and carry the right number of arguments (``replace`` takes two).
    for filter_text in filter_texts:
        parts = tuple(filter_text.strip().split(":"))
        filter_name = parts[0]
        if filter_name not in FILTER_NAMES:
            raise TemplateError(f"{text} uses the unknown filter '{filter_name}'; use one of {', '.join(FILTER_NAMES)}")
        # ``replace:<from>:<to>`` takes a non-empty first argument; the other filters take none.
        expected = 3 if filter_name == "replace" else 1
        if len(parts) != expected or (filter_name == "replace" and not parts[1]):
            usage = "replace:<from>:<to>" if filter_name == "replace" else filter_name
            raise TemplateError(f"{text} uses the filter '{filter_name}' wrongly; write {usage}")
        filters.append(parts)
    return Reference(name, tuple(filters), text)


def _segment(value: str, index: int) -> str:
    """Return the leading number of dotted segment *index*, or ``0`` when it is absent."""
    parts = value.split(".")
    if index >= len(parts):
        return "0"
    leading = re.match(r"\d+", parts[index])
    return leading.group(0) if leading else parts[index]


def _apply_filter(value: str, parts: tuple[str, ...]) -> str:
    """Apply one parsed filter to *value*."""
    name = parts[0]
    if name == "replace":
        return value.replace(parts[1], parts[2])
    if name == "nodots":
        return value.replace(".", "")
    return _segment(value, ("major", "minor", "patch").index(name))
