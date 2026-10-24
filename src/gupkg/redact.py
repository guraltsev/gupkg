"""Remove credentials from URLs before they are written to a receipt, a log, an event or an error message.

A download address can carry a secret: ``https://user:password@host/file`` holds user information, and a query such as
``?token=...`` or an ``X-Amz-Signature=...`` holds a bearer secret. The address is data the user wrote (or a tracker
discovered), but receipts and logs are kept, shared in bug reports and read by other tools, so they must not hold the secret.

Usage and API
-------------
``redact_url(url)`` returns *url* with the user information replaced by ``***`` and the value of every secret-looking query
parameter replaced by ``***``; a URL without either is returned unchanged. ``redact_text(text)`` does the same for every
``http://`` or ``https://`` address inside a sentence.

Implementation Approach
-----------------------
A query parameter is secret when its name contains one of ``SECRET_WORDS`` (case-insensitive): ``token``, ``secret``,
``passw``, ``signature``, ``credential``, ``auth``, ``key`` and ``sig``. The match is deliberately broad: hiding a harmless
value costs nothing, showing a secret does. The URL is split and joined with the standard library; its path is never
changed. Text that is not a URL is returned as it was.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

SECRET_WORDS = ("token", "secret", "passw", "signature", "credential", "auth", "key", "sig")
MASK = "***"

_ADDRESS = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_TRAILING = ".,;:)]}"


def _is_secret(name: str) -> bool:
    """Return whether a query parameter named *name* is a secret."""
    folded = name.casefold()
    return any(word in folded for word in SECRET_WORDS)


def redact_url(url: str) -> str:
    """Return *url* without user information and without the values of secret-looking query parameters."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.rsplit('@', 1)[1]}"
    query = parts.query
    if query:
        pieces = []
        for piece in query.split("&"):
            name, separator, _value = piece.partition("=")
            pieces.append(f"{name}={MASK}" if separator and _is_secret(name) else piece)
        query = "&".join(pieces)
    if netloc == parts.netloc and query == parts.query:
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def redact_text(text: str) -> str:
    """Return *text* with every ``http`` or ``https`` address redacted by ``redact_url``."""

    def replace(match: re.Match[str]) -> str:
        address = match.group(0)
        stripped = address.rstrip(_TRAILING)
        return redact_url(stripped) + address[len(stripped):]

    return _ADDRESS.sub(replace, text)
