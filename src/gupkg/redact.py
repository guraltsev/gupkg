"""Remove credentials from URLs before they are written to a receipt, a log, an event or an error message.

A download address can carry a secret: ``https://user:password@host/file`` holds user information, and a query such as
``?token=...`` or an ``X-Amz-Signature=...`` holds a bearer secret. The address is data the user wrote (or a tracker
discovered), but receipts and logs are kept, shared in bug reports and consumed by other tools, so they must not hold the secret.

Usage and API
-------------
``redact_url(url)`` returns *url* with the user information replaced by ``***`` and the value of every secret-looking query
parameter replaced by ``***``; a URL without either is returned unchanged. ``redact_text(text)`` does the same for every
``http://`` or ``https://`` address inside a sentence.

Limits: a secret that is part of the *path* (``https://host/<token>/file.zip``, a signed folder name) cannot be told from
an ordinary name, so it is not masked; keep secrets in the query, in a header or in the user information, where they are.

Implementation Approach
-----------------------
A query or fragment parameter (separated by ``&`` or ``;``) is secret when its name contains one of ``SECRET_WORDS`` (case-insensitive): ``token``, ``secret``,
``pass`` (``password``, ``passwd``), ``pwd``, ``signature``, ``credential``, ``auth``, ``key`` (``AWSAccessKeyId``,
``Key-Pair-Id``), ``sig``, ``policy``, ``session``, ``jwt``, ``bearer`` and ``sas``. The name is percent-decoded (twice) before it
is judged, and a percent-encoded ``=`` (``token%3DS``) counts as a separator. The match is deliberately broad: hiding a harmless
value costs nothing, showing a secret does. The URL is split and joined with the standard library; its path is never
changed. Text that is not a URL is returned as it was.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

# Substrings (compared case-insensitively) that mark a query or fragment parameter name as secret.
SECRET_WORDS = (
    "token", "secret", "pass", "pwd", "signature", "credential", "auth", "key", "sig", "policy", "session", "jwt", "bearer", "sas",
)

# Replacement shown in place of a redacted value.
MASK = "***"


def redact_url(url: str) -> str:
    """Return *url* without user information and without the values of secret-looking query parameters.

    Parameters
    ----------
    url : str
        The address to redact; text that does not parse as a URL is returned as it was.

    Returns
    -------
    str
        *url* with user information and secret parameter values replaced by ``***``, or *url* itself when
        there was nothing to mask.

    Examples
    --------
    Basic usage:

    >>> redact_url("https://user:pw@host/file.zip?token=abc&x=1")
    'https://***@host/file.zip?token=***&x=1'
    """
    # Unparseable text is not a URL; leave it alone.
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    # Mask user information, then the secret parameters of the query and the fragment.
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{MASK}@{netloc.rsplit('@', 1)[1]}"
    query, fragment = _mask_parameters(parts.query), _mask_parameters(parts.fragment)

    # Return the original string untouched when nothing changed, so the path and spelling are preserved.
    if netloc == parts.netloc and query == parts.query and fragment == parts.fragment:
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def redact_text(text: str) -> str:
    """Return *text* with every ``http`` or ``https`` address redacted by ``redact_url``.

    Parameters
    ----------
    text : str
        A sentence or message that may contain addresses.

    Returns
    -------
    str
        *text* with each address redacted; trailing punctuation after an address is kept outside it.

    Examples
    --------
    Basic usage:

    >>> redact_text("fetch https://host/f?token=abc, then stop")
    'fetch https://host/f?token=***, then stop'
    """
    # Find addresses with a simple pattern; trailing punctuation belongs to the sentence, not to the address.
    address_pattern = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
    trailing = ".,;:)]}"

    # Redact one address at a time, keeping the sentence punctuation that followed it.
    def _replace(match: re.Match[str]) -> str:
        """Redact one matched address and re-attach the punctuation that followed it."""
        address = match.group(0)
        stripped = address.rstrip(trailing)
        return redact_url(stripped) + address[len(stripped):]

    # Apply the redaction to every address in the text.
    return address_pattern.sub(_replace, text)


# ---------------------------------------------------------------------------
# Parameter masking
# ---------------------------------------------------------------------------


def _is_secret(name: str) -> bool:
    """Return whether a query parameter named *name* is a secret."""
    folded = _decode(name).casefold()
    return any(word in folded for word in SECRET_WORDS)


def _decode(text: str) -> str:
    """Percent-decode *text* twice, so ``%74oken`` and ``%2574oken`` are read as ``token``."""
    return unquote(unquote(text))


def _mask_parameters(text: str) -> str:
    """Mask the value of every secret-looking ``name=value`` pair of a query or a fragment (``&`` or ``;`` separated)."""
    # Split on the separators but keep them, so the original layout is rebuilt exactly.
    pieces = re.split(r"([&;])", text)
    for index in range(0, len(pieces), 2):
        piece = pieces[index]
        name, separator, _value = _decode(piece).partition("=")
        if separator and _is_secret(name):
            # Keep the name as it was written when the ``=`` was written too; a percent-encoded ``=`` (``token%3DS``) has no raw name.
            raw_name = piece.partition("=")[0] if "=" in piece else name
            pieces[index] = f"{raw_name}={MASK}"
    return "".join(pieces)
