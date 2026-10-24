"""Validate and expand the values that remote pages supply to a service.

Versions, addresses and templates found in responses are untrusted. This module checks
that a version can name a version directory, that an address is a bounded http or https
URL, expands ``${name}`` templates strictly (an unknown name is an error, not a silent
blank), and compiles patterns whose substituted values are escaped.

Usage and API
-------------
``valid_version(text)``, ``absolute_url(text, base)``, ``expand(template, values)`` and
``compile_runtime(pattern, values)`` return the checked value or raise ``TrackError``.

Implementation Approach
-----------------------
Checks are plain text rules with no network access. Template syntax and escaping come
from ``gupkg.model.template`` and ``gupkg.model.patterns``, so the validator of a manifest
and the service that runs it agree.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping

from ..model.patterns import PatternError, compile_pattern, escape_template
from ..model.template import TemplateError, references, render
from ..model.version import is_version_name
from .safepath import safe_file_name

MAX_VERSION_LENGTH = 64
MAX_URL_LENGTH = 4096


class TrackError(RuntimeError):
    """Report that a provider or a checksum source could not supply a value."""


def valid_version(text: str) -> str:
    """Return *text* stripped, after checking that it can name a version directory.

    Raises
    ------
    TrackError
        If it is empty, longer than ``MAX_VERSION_LENGTH``, contains whitespace or a
        template, or is a reserved or unsafe directory name.
    """
    version = text.strip()
    if not version or len(version) > MAX_VERSION_LENGTH or re.search(r"\s", version) or "${" in version:
        raise TrackError(f"The discovered version {text[:80]!r} is not usable: it must be 1-{MAX_VERSION_LENGTH} characters without spaces")
    if not is_version_name(version):
        raise TrackError(f"The discovered version {version!r} cannot name a version directory")
    return version


def absolute_url(text: str, base: str | None = None) -> str:
    """Return *text* as an absolute http or https URL, resolved against *base*.

    Raises
    ------
    TrackError
        If the result is not http or https, or is longer than ``MAX_URL_LENGTH``.
    """
    joined = urllib.parse.urljoin(base or "", text.strip())
    parts = urllib.parse.urlsplit(joined)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or len(joined) > MAX_URL_LENGTH:
        raise TrackError(f"{text[:100]!r} is not an http or https address")
    if any(ord(character) < 33 for character in joined):
        raise TrackError(f"{text[:100]!r} is not a valid address")
    return joined


def expand(template: str, values: Mapping[str, str]) -> str:
    """Expand ``${name}`` references in *template* from *values*.

    Raises
    ------
    TrackError
        If the template is malformed or names something that has no value.
    """
    try:
        rendered = render(template, values)
        left = [reference.text for reference in references(rendered) if reference.name not in values]
    except TemplateError as error:
        raise TrackError(str(error)) from error
    if left:
        raise TrackError(f"{left[0]} has no value in this check; known: {', '.join(sorted(values)) or 'nothing'}")
    return rendered


def compile_runtime(text: str, values: Mapping[str, str]) -> re.Pattern[str]:
    """Compile a pattern that may contain ``${name}`` references, escaping each substituted value."""
    try:
        return compile_pattern(escape_template(text, values))
    except PatternError as error:
        raise TrackError(str(error)) from error


def file_name_from_url(url: str) -> str:
    """Return a harmless file name for the artifact at *url*.

    The last path segment is used, except that a trailing ``download`` or ``dl`` marker (SourceForge
    serves ``.../CrystalDiskInfo9_4_2.zip/download``) is skipped in favour of the segment before it.
    """
    segments = [urllib.parse.unquote(segment) for segment in urllib.parse.urlsplit(url).path.split("/") if segment]
    while len(segments) > 1 and segments[-1].lower() in ("download", "dl"):
        segments.pop()
    return safe_file_name(segments[-1] if segments else "", "download")
