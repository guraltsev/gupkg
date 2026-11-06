"""Decide what a download's checksum means: verify, skip, or refuse (design section 9).

This module is the one place that knows the rules of the checksum policy, and it is pure:
it reads no file, no setting and no environment, and it never prints or asks. A caller
names the URL, the SHA-256 the version declares (or ``None``) and whether the call carries
``--skip-checksum``; the answer is a ``Verification`` or an exception.

Usage and API
-------------
``check_before_download(url, expected, skip)`` runs before any byte is fetched and returns a
``Verification`` (the digest to enforce, or ``None`` for an allowed skip, and the decision to
record) or raises ``ChecksumMissing``. ``verify_observed(verification, observed, ...)`` runs
after the download and raises ``ChecksumMismatch`` unless the hash that was observed equals
the expected one; it returns the ``ChecksumRecord`` for the receipt. ``normalize_sha256``
is the only reader of digests. ``parse_skip_flag`` turns the value of ``--skip-checksum``
into a decision. ``is_https_url`` is the only judge of "this URL is https".
``ChecksumPlan`` is what ``--plan --json`` reports for a target (``sha256: present|missing``).
``git_gate`` and ``git_record`` apply the same rules to a git source, whose commit is the
integrity anchor.

Implementation Approach
-----------------------
The policy is fail-closed. A missing hash is refused unless the call carries the flag
*and* the URL is ``https://``; a malformed digest, an unknown decision, an unknown flag
value or an odd URL spelling is an error, never a pass. A mismatch has no override at
all: ``verify_observed`` has no parameter that could waive it, and the flag never reaches
the comparison. Digests are normalised in one function (surrounding whitespace, a
``sha256:`` prefix and letter case are removed; the result must be exactly 64 hexadecimal
digits, and a text holding several digests is refused) and are compared in full.
A URL counts as https only when its text starts with ``https://`` (any letter case), has a
host, no user information, no backslash and no control character, so spellings such as
``https:/x``, ``https:\\x``, ``https://good@evil`` or ``file:`` never pass.
"""

from __future__ import annotations

import hmac
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..redact import redact_text, redact_url

# The command-line flag that lets an https download proceed without a declared SHA-256.
SKIP_FLAG = "--skip-checksum"

# The decisions a receipt can record: what the policy concluded for one install.
VERIFIED = "verified"  # the download matched a declared hash or a pinned commit
SKIPPED_CLI = "skipped-cli"  # the call carried the bare ``--skip-checksum`` flag
SKIPPED_SETTING = "skipped-setting"  # the manager's ``checksum_missing = skip`` setting applied
USER_APPROVED = "user-approved"  # the user approved an unverified install in the manager
REGISTRY_FINGERPRINT = "registry-fingerprint"  # the registry's own fingerprint matched (nothing downloaded)
ORIGIN_NOT_VERIFIED = "origin-not-verified"  # an install whose origin was never verified (nothing downloaded)

# Decisions that allow a download without a hash.
SKIP_DECISIONS = (SKIPPED_CLI, SKIPPED_SETTING, USER_APPROVED)
# Decisions of installs that fetched bytes and so record an observed digest or commit.
DOWNLOAD_DECISIONS = (VERIFIED, *SKIP_DECISIONS)
# Decisions of installs that fetched no bytes through this policy.
NO_DOWNLOAD_DECISIONS = (REGISTRY_FINGERPRINT, ORIGIN_NOT_VERIFIED)
# Every decision a checksum record may carry.
DECISIONS = (*DOWNLOAD_DECISIONS, *NO_DOWNLOAD_DECISIONS)

# The text after ``--skip-checksum=``: the manager's reasons. A bare flag is the CLI's own.
_SKIP_REASONS = {"cli": SKIPPED_CLI, "setting": SKIPPED_SETTING, "user-approved": USER_APPROVED}
# The reason text of each skip decision, the inverse of the mapping above.
SKIP_REASON_OF = {decision: reason for reason, decision in _SKIP_REASONS.items()}

# Values of ``ChecksumPlan.sha256``: a hash is declared, absent, or malformed.
PRESENT = "present"
# No hash is declared, so the download is refused unless it may be skipped.
MISSING = "missing"
# ``INVALID`` is fed by the manager (issue 030) from an ``invalid-checksum`` error document, not from a plan: the CLI plans only
# ``present`` and ``missing`` (a malformed declared hash makes the version invalid, so the version has no plan).
INVALID = "invalid"

# Process exit code of every checksum refusal.
EXIT_CHECKSUM = 3

# A normalised SHA-256: exactly 64 lower-case hexadecimal digits.
_SHA256 = re.compile(r"[0-9a-f]{64}")
# A git commit id: exactly 40 lower-case hexadecimal digits.
_COMMIT = re.compile(r"[0-9a-f]{40}")
# The optional ``sha256:`` label that digests may carry.
_PREFIX = re.compile(r"sha256:", re.IGNORECASE)
# The keys of a checksum record, in the order they are written.
_RECORD_KEYS = ("decision", "url", "final_url", "observed_sha256", "size", "sha256_from", "commit", "registry")


class ChecksumError(Exception):
    """Base of every checksum refusal; ``code`` and ``exit_code`` serve the machine interface.

    Parameters
    ----------
    message : str
        The readable message.
    url : str | None
        The URL the error concerns.

    Methods
    -------
    to_data
        Return the structured error of a ``--json`` result.
    """

    code = "checksum-error"
    exit_code = EXIT_CHECKSUM

    def __init__(self, message: str, url: str | None = None) -> None:
        """Build the error from a readable message and the URL it concerns."""
        super().__init__(message)
        self.url = url

    def to_data(self) -> dict[str, Any]:
        """Return the structured error of a ``--json`` result.

        Returns
        -------
        dict
            ``{"code": ..., "message": ...}`` with a redacted ``"url"`` when the error names one.
        """
        data: dict[str, Any] = {"code": self.code, "message": redact_text(str(self))}
        if self.url is not None:
            data["url"] = redact_url(self.url)
        return data


class ChecksumMissing(ChecksumError):
    """Report a download with no SHA-256 that the policy does not allow to proceed.

    ``skippable`` says whether ``--skip-checksum`` could have helped (an ``https://`` URL).

    Parameters
    ----------
    message : str
        The readable message.
    url : str | None
        The URL that has no checksum.
    skippable : bool
        Whether ``--skip-checksum`` could have helped.

    Methods
    -------
    to_data
        Return the structured error, extended with whether the flag could be used.
    """

    code = "checksum-missing"

    def __init__(self, message: str, url: str | None = None, *, skippable: bool = False) -> None:
        """Build the error; *skippable* is true when the URL is https and the flag was not given."""
        super().__init__(message, url)
        self.skippable = skippable

    def to_data(self) -> dict[str, Any]:
        """Return the structured error, extended with whether the flag could be used.

        Returns
        -------
        dict
            The base error data plus ``"skippable"``.
        """
        return {**super().to_data(), "skippable": self.skippable}


class ChecksumMismatch(ChecksumError):
    """Report a download or commit that is not what the version declares, a refusal nothing overrides.

    Parameters
    ----------
    message : str
        The readable message.
    url : str | None
        The URL that was fetched.
    expected : str | None
        The value the version declares.
    observed : str | None
        The value that was found.
    """

    code = "checksum-mismatch"

    def __init__(self, message: str, url: str | None = None, *, expected: str | None = None, observed: str | None = None) -> None:
        """Build the error with the expected and the observed value."""
        super().__init__(message, url)
        self.expected = expected
        self.observed = observed


class ChecksumInvalid(ChecksumError):
    """Report a malformed digest, flag value, decision or receipt record."""

    code = "checksum-invalid"


def normalize_sha256(text: object) -> str:
    """Return *text* as a lower-case 64-digit SHA-256.

    Surrounding whitespace, a leading ``sha256:`` (any letter case) and letter case are
    removed. Anything else that is not exactly 64 hexadecimal digits is refused: a wrong
    length, a non-hex character, or a text with several digits or words in it. Nothing is
    ever truncated or compared partially.

    Parameters
    ----------
    text : object
        The declared or observed digest, normally a string.

    Returns
    -------
    str
        The 64 lower-case hexadecimal digits.

    Raises
    ------
    ChecksumInvalid
        If *text* is not a string or is not exactly one 64-digit SHA-256.

    Examples
    --------
    Basic usage:

    >>> normalize_sha256("  SHA256:" + "AB" * 32)
    'abababababababababababababababababababababababababababababababab'

    Edge cases:

    >>> normalize_sha256("abc")
    Traceback (most recent call last):
        ...
    gupkg.model.checksum.ChecksumInvalid: 'abc' is not a SHA-256: it must be exactly 64 hexadecimal digits
    """
    # Reject non-text first, then strip the label, whitespace and case before the strict match.
    if not isinstance(text, str):
        raise ChecksumInvalid(f"a SHA-256 must be text, not {type(text).__name__}")
    cleaned = text.strip()
    cleaned = _PREFIX.sub("", cleaned, count=1) if _PREFIX.match(cleaned) else cleaned
    cleaned = cleaned.strip().lower()
    if not _SHA256.fullmatch(cleaned):
        shown = text.strip()[:80]
        raise ChecksumInvalid(f"{shown!r} is not a SHA-256: it must be exactly 64 hexadecimal digits")
    return cleaned


def hashes_equal(first: str, second: str) -> bool:
    """Return whether two digests are equal once normalised.

    Parameters
    ----------
    first, second : str
        The digests to compare.

    Returns
    -------
    bool
        ``True`` only when both are well formed and equal; a malformed digest is never equal.

    Examples
    --------
    Basic usage:

    >>> hashes_equal("a" * 64, "A" * 64)
    True
    >>> hashes_equal("a" * 64, "b" * 64)
    False

    Edge cases:

    >>> hashes_equal("not a hash", "not a hash")
    False
    """
    try:
        return hmac.compare_digest(normalize_sha256(first), normalize_sha256(second))
    except ChecksumInvalid:
        return False


def is_https_url(url: object) -> bool:
    """Return whether *url* is an ``https://`` address with a host, judged on its parsed form.

    Refused: any other scheme (``http``, ``file``, ``ftp``, ``ssh``), a scheme without ``//``
    (``https:/x``), an empty host, user information (``https://good@evil``), a backslash, a
    space or any control character anywhere, and a port that is not a number.

    Parameters
    ----------
    url : object
        The address to judge, normally a string.

    Returns
    -------
    bool
        ``True`` when *url* passes every check above.

    Examples
    --------
    Basic usage:

    >>> is_https_url("https://example.org/tool.zip")
    True
    >>> is_https_url("http://example.org/tool.zip")
    False

    Edge cases:

    >>> is_https_url("https://good@evil.example/tool.zip")
    False
    >>> is_https_url("https:/example.org")
    False
    >>> is_https_url("https://example.org:port/")
    False
    """
    # Cheap textual refusals first: scheme spelling, then characters no https URL may contain.
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        return False
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" or char.isspace() for char in url):
        return False
    # Then judge the parsed form: host present, no user information, a numeric port.
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.netloc) and "@" not in parts.netloc and bool(host)


def parse_skip_flag(value: str | None) -> str | None:
    """Return the decision a ``--skip-checksum`` value stands for.

    Parameters
    ----------
    value : str or None
        ``None`` when the flag was not given; ``""`` or ``"cli"`` for the bare flag;
        ``"setting"`` or ``"user-approved"`` for the manager's ``--skip-checksum=<reason>``.

    Returns
    -------
    str or None
        ``skipped-cli``, ``skipped-setting`` or ``user-approved``.

    Raises
    ------
    ChecksumInvalid
        For any other value. An unknown reason is refused, not read as the bare flag.

    Examples
    --------
    Basic usage:

    >>> parse_skip_flag(None) is None
    True
    >>> parse_skip_flag("")
    'skipped-cli'
    >>> parse_skip_flag("user-approved")
    'user-approved'

    Edge cases:

    >>> parse_skip_flag("maybe")
    Traceback (most recent call last):
        ...
    gupkg.model.checksum.ChecksumInvalid: --skip-checksum=maybe is not understood; the reasons are cli, setting, user-approved
    """
    if value is None:
        return None
    reason = value.strip().lower() or "cli"
    if reason not in _SKIP_REASONS:
        raise ChecksumInvalid(f"{SKIP_FLAG}={value} is not understood; the reasons are {', '.join(_SKIP_REASONS)}")
    return _SKIP_REASONS[reason]


@dataclass(frozen=True)
class Verification:
    """Hold the outcome of the rule that runs before a download starts.

    Attributes
    ----------
    expected : str or None
        The normalised SHA-256 the download must have; ``None`` only for an allowed skip.
    decision : str
        ``verified`` when *expected* is set, otherwise the skip decision of the call.
    """

    expected: str | None
    decision: str

    @property
    def skipped(self) -> bool:
        """Whether the download proceeds without a checksum."""
        return self.expected is None


def check_before_download(url: str, expected_sha256: str | None, skip: str | None = None) -> Verification:
    """Apply the verification rule (design 9.1) before anything is downloaded.

    A declared hash is enforced; a missing one is refused unless the call may skip it.

    Parameters
    ----------
    url : str
        The address that will be downloaded.
    expected_sha256 : str or None
        The ``sha256`` of the version's ``[source]``; ``None`` when it has none.
    skip : str or None
        The decision of ``--skip-checksum`` (``parse_skip_flag``), or ``None`` without the flag.

    Returns
    -------
    Verification
        What to enforce and what to record.

    Raises
    ------
    ChecksumInvalid
        If the hash is malformed or *skip* is not one of the skip decisions.
    ChecksumMissing
        If there is no hash and the call may not skip: no flag, or a URL that is not https.

    Examples
    --------
    Basic usage:

    >>> check_before_download("https://example.org/tool.zip", "AB" * 32).decision
    'verified'
    >>> check_before_download("https://example.org/tool.zip", None, "skipped-cli").skipped
    True

    Edge cases:

    >>> try:
    ...     check_before_download("https://example.org/tool.zip", None)
    ... except ChecksumMissing as error:
    ...     print(error.skippable)
    True
    >>> try:
    ...     check_before_download("http://example.org/tool.zip", None, "skipped-cli")
    ... except ChecksumMissing as error:
    ...     print(error.skippable)
    False
    """
    # Refuse an unknown skip decision so a typo can never act as permission.
    if skip is not None and skip not in SKIP_DECISIONS:
        raise ChecksumInvalid(f"{skip!r} is not a skip decision")
    # A declared hash always wins and is enforced after the download.
    if expected_sha256 is not None:
        return Verification(normalize_sha256(expected_sha256), VERIFIED)
    # No hash: only an https download with an explicit skip may proceed unverified.
    if skip is None or not is_https_url(url):
        raise _missing(url, skip)
    return Verification(None, skip)


@dataclass(frozen=True)
class ChecksumRecord:
    """Hold the checksum part of a receipt (design 9.2).

    Methods
    -------
    to_data
        Return the record as plain TOML-ready data, without unset fields.

    Attributes
    ----------
    decision : str
        One of ``DECISIONS``.
    url, final_url : str or None
        The address that was asked for and the one that served the file after redirects.
    observed_sha256 : str or None
        The SHA-256 of the bytes that were verified and used.
    size : int or None
        Their size in bytes.
    sha256_from : str or None
        Where the expected hash came from (``github-digest``, ``vendor-sums``, ``registry``, ``manual``).
    commit : str or None
        The 40-digit commit a git source resolved to.
    registry : str or None
        The registry whose fingerprint matched (``registry-fingerprint``).
    """

    decision: str
    url: str | None = None
    final_url: str | None = None
    observed_sha256: str | None = None
    size: int | None = None
    sha256_from: str | None = None
    commit: str | None = None
    registry: str | None = None

    def __post_init__(self) -> None:
        """Refuse an unknown decision and a record that lacks what its decision promises.

        Raises
        ------
        ChecksumInvalid
            If the decision is unknown, a digest, commit or size is malformed, or the
            record lacks the digest, commit or registry its decision promises.
        """
        # Check the decision first, then normalise the digest so equal digests compare equal.
        if self.decision not in DECISIONS:
            raise ChecksumInvalid(f"{self.decision!r} is not a checksum decision; expected one of {', '.join(DECISIONS)}")
        if self.observed_sha256 is not None:
            object.__setattr__(self, "observed_sha256", normalize_sha256(self.observed_sha256))
        # Validate the shapes of the remaining fields.
        if self.commit is not None and not _COMMIT.fullmatch(self.commit):
            raise ChecksumInvalid(f"{self.commit!r} is not a 40-digit lower-case commit id")
        if self.size is not None and (isinstance(self.size, bool) or not isinstance(self.size, int) or self.size < 0):
            raise ChecksumInvalid("size must be a non-negative integer")
        # Finally require what each decision promises: a digest or commit, or a registry name.
        if self.decision in DOWNLOAD_DECISIONS and self.observed_sha256 is None and self.commit is None:
            raise ChecksumInvalid(f"a '{self.decision}' record needs the observed SHA-256 (or the commit of a git source)")
        if self.decision == REGISTRY_FINGERPRINT and not self.registry:
            raise ChecksumInvalid("a 'registry-fingerprint' record names the registry")

    @property
    def verified(self) -> bool:
        """Whether the install is verified: a checked hash or commit, or a matching registry fingerprint."""
        return self.decision in (VERIFIED, REGISTRY_FINGERPRINT)

    @property
    def note(self) -> str | None:
        """The text ``list --details``, ``info`` and ``check`` show for an unverified install, else ``None``."""
        return unverified_note(self.decision)

    def to_data(self) -> dict[str, Any]:
        """Return the record as plain TOML-ready data, without unset fields.

        Returns
        -------
        dict
            The set fields keyed by name, with URLs redacted.
        """
        values = {key: getattr(self, key) for key in _RECORD_KEYS}
        for key in ("url", "final_url"):
            if values[key] is not None:
                values[key] = redact_url(values[key])
        return {key: value for key, value in values.items() if value is not None}


# The warning shown for each unverified decision.
_NOTES = {
    SKIPPED_CLI: "installed without a checksum (--skip-checksum)",
    SKIPPED_SETTING: "installed without a checksum (the manager's checksum_missing = skip)",
    USER_APPROVED: "installed without a checksum (approved by the user)",
    ORIGIN_NOT_VERIFIED: "origin not verified",
}


def unverified_note(decision: str) -> str | None:
    """Return the warning for a decision that is not verified, ``None`` for a verified one.

    An unknown decision is reported as unverified (fail closed).

    Parameters
    ----------
    decision : str
        A checksum decision.

    Returns
    -------
    str or None
        The warning text, or ``None`` when the decision is verified.

    Examples
    --------
    Basic usage:

    >>> unverified_note("verified") is None
    True
    >>> unverified_note("skipped-cli")
    'installed without a checksum (--skip-checksum)'

    Edge cases:

    >>> unverified_note("bogus")
    "unknown checksum decision 'bogus': treated as unverified"
    """
    if decision in (VERIFIED, REGISTRY_FINGERPRINT):
        return None
    return _NOTES.get(decision, f"unknown checksum decision {decision!r}: treated as unverified")


def parse_checksum_record(data: Mapping[str, Any]) -> ChecksumRecord:
    """Read the checksum table of a receipt strictly.

    Parameters
    ----------
    data : Mapping
        The ``[checksum]`` table of a receipt.

    Returns
    -------
    ChecksumRecord
        The validated record.

    Raises
    ------
    ChecksumInvalid
        For an unknown key, a wrong type, an unknown decision or a malformed digest.

    Examples
    --------
    Basic usage:

    >>> record = parse_checksum_record({"decision": "verified", "observed_sha256": "AB" * 32, "size": 3})
    >>> record.verified, record.size
    (True, 3)

    Edge cases:

    >>> parse_checksum_record({"decision": "verified", "extra": 1})
    Traceback (most recent call last):
        ...
    gupkg.model.checksum.ChecksumInvalid: unknown key in the checksum record: extra
    """
    # Refuse anything that is not a table, has unknown keys or lacks the decision.
    if not isinstance(data, Mapping):
        raise ChecksumInvalid("the checksum record must be a table")
    unknown = [key for key in data if key not in _RECORD_KEYS]
    if unknown:
        raise ChecksumInvalid(f"unknown key in the checksum record: {', '.join(map(str, unknown))}")
    if "decision" not in data:
        raise ChecksumInvalid("the checksum record has no decision")
    # Every key except ``size`` is text; the record's own validation checks the rest.
    for key in _RECORD_KEYS:
        if key not in ("size",) and key in data and not isinstance(data[key], str):
            raise ChecksumInvalid(f"checksum record key '{key}' must be a string")
    return ChecksumRecord(**{key: data[key] for key in _RECORD_KEYS if key in data})


def verify_observed(
    verification: Verification, observed_sha256: str, *, url: str, final_url: str, size: int, sha256_from: str | None = None
) -> ChecksumRecord:
    """Compare the hash that was observed with the expected one and build the receipt record.

    There is deliberately no way to waive the comparison: *verification* decides only whether
    there is an expected digest, and the flag of the call is not an input here.

    Parameters
    ----------
    verification : Verification
        The result of ``check_before_download``.
    observed_sha256 : str
        The SHA-256 of the downloaded bytes.
    url : str
        The address that was requested.
    final_url : str
        The address that served the file after redirects.
    size : int
        The size of the download in bytes.
    sha256_from : str or None
        Where the expected hash came from, recorded only for a verified download.

    Returns
    -------
    ChecksumRecord
        The record for the receipt.

    Raises
    ------
    ChecksumMismatch
        If the verification expects a digest and the observed one differs, or if a skipped download
        was served from a URL that is not https (or not text).
    ChecksumInvalid
        If the observed digest is malformed.
    """
    observed = normalize_sha256(observed_sha256)
    # With an expected digest the comparison is exact and has no override.
    if verification.expected is not None:
        if not hmac.compare_digest(verification.expected, observed):
            raise ChecksumMismatch(
                f"{_shown(url)} has SHA-256 {observed}, but the version declares {verification.expected}. "
                "A mismatch is never skipped.",
                url, expected=verification.expected, observed=observed,
            )
    # Without one (an allowed skip) the file must at least have been served over https.
    elif not is_https_url(final_url):
        served = _shown(final_url) if isinstance(final_url, str) else "an unknown address"
        raise ChecksumMismatch(
            f"{_shown(url)} was served from {served}, which is not https; an unverified download is refused.",
            url, observed=observed,
        )
    # Build the receipt record from what was actually observed.
    return ChecksumRecord(
        verification.decision, url=url, final_url=final_url, observed_sha256=observed, size=size,
        sha256_from=sha256_from if verification.expected is not None else None,
    )


# ---------------------------------------------------------------------------
# Git sources
# ---------------------------------------------------------------------------


def git_gate(url: str, commit: str | None, skip: str | None = None) -> Verification:
    """Apply the rule to a git source, where a pinned commit is the integrity anchor (design 9.1 item 4).

    A source with a ``commit`` is verified by it. A source with only a ``ref`` has no anchor
    and follows the missing-checksum rule: refused, unless the call carries ``--skip-checksum``
    and the repository address is ``https://``.

    Parameters
    ----------
    url : str
        The repository address.
    commit : str or None
        The pinned 40-digit commit, or ``None`` when the source has only a ref.
    skip : str or None
        The decision of ``--skip-checksum``, or ``None`` without the flag.

    Returns
    -------
    Verification
        ``expected`` holds the pinned commit (40 hex digits) or ``None`` for an allowed skip.

    Raises
    ------
    ChecksumInvalid
        If *skip* is unknown or *commit* is not a 40-digit commit id.
    ChecksumMissing
        If there is no commit and the call may not skip.
    """
    # Refuse an unknown skip decision so a typo can never act as permission.
    if skip is not None and skip not in SKIP_DECISIONS:
        raise ChecksumInvalid(f"{skip!r} is not a skip decision")
    # A pinned commit is verified by itself, after strict normalisation.
    if commit is not None:
        if not isinstance(commit, str):
            raise ChecksumInvalid(f"a commit id must be text, not {type(commit).__name__}")
        cleaned = commit.strip().lower()
        if not _COMMIT.fullmatch(cleaned):
            raise ChecksumInvalid(f"{commit!r} is not a 40-digit commit id")
        return Verification(cleaned, VERIFIED)
    # A ref alone has no anchor: the missing-checksum rule decides.
    if skip is None or not is_https_url(url):
        raise _missing(url, skip)
    return Verification(None, skip)


def git_record(verification: Verification, resolved_commit: str, *, url: str) -> ChecksumRecord:
    """Build the receipt record of a git checkout and require it to match a pinned commit.

    Parameters
    ----------
    verification : Verification
        The result of ``git_gate``.
    resolved_commit : str
        The commit that was actually checked out.
    url : str
        The repository address.

    Returns
    -------
    ChecksumRecord
        The record for the receipt.

    Raises
    ------
    ChecksumMismatch
        If the commit that was checked out is not the pinned one.
    ChecksumInvalid
        If *resolved_commit* is not a 40-digit commit id.
    """
    # The checked-out commit must be well formed and, when one is pinned, identical to it.
    resolved = resolved_commit.strip().lower()
    if not _COMMIT.fullmatch(resolved):
        raise ChecksumInvalid(f"{resolved_commit!r} is not a 40-digit commit id")
    if verification.expected is not None and not hmac.compare_digest(verification.expected, resolved):
        raise ChecksumMismatch(
            f"{_shown(url)} served commit {resolved}, but the version pins {verification.expected}.",
            url, expected=verification.expected, observed=resolved,
        )
    return ChecksumRecord(verification.decision, url=url, final_url=url, commit=resolved)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChecksumPlan:
    """What ``--plan --json`` reports about one target's download (design 9.1 and 10.4).

    Methods
    -------
    to_data
        Return the plan entry as JSON-ready data.

    Attributes
    ----------
    sha256 : str
        ``present``, ``missing``, or ``invalid`` (a hash is declared but malformed, or a git commit is not a
        40-digit id; the install would fail with an error, so no skip is ever offered).
    url : str
        The source URL.
    url_from : str
        Where the URL came from (``pkg.toml``, ``tracker``, ``registry <name>``...), for the manager's prompt.
    skippable : bool
        Whether ``--skip-checksum`` could allow this download: only for ``https://``. Always
        ``False`` when the hash is present (nothing to skip) and always ``False`` when it is invalid.
    """

    sha256: str
    url: str
    url_from: str
    skippable: bool

    def to_data(self) -> dict[str, Any]:
        """Return the plan entry as JSON-ready data.

        Returns
        -------
        dict
            The four fields, with the URL redacted.
        """
        return {"sha256": self.sha256, "url": redact_url(self.url), "url_from": self.url_from, "skippable": self.skippable}


def plan_checksum(url: str, sha256: str | None, *, url_from: str, commit: str | None = None, git: bool = False) -> ChecksumPlan:
    """Mark a download ``present``, ``missing`` or ``invalid`` before anything is fetched.

    A git source is ``present`` when it pins a well-formed commit. A declared value that is
    malformed (or not text) is ``invalid`` and never skippable: the install would refuse it with
    an error, and a skip must not be offered for it.

    Parameters
    ----------
    url : str
        The source address.
    sha256 : str or None
        The declared hash of a file source.
    url_from : str
        Where the URL came from, for the manager's prompt.
    commit : str or None
        The pinned commit of a git source.
    git : bool
        Whether the source is a git source, so *commit* rather than *sha256* is the anchor.

    Returns
    -------
    ChecksumPlan
        The plan entry for the target.
    """
    # Pick the anchor that applies to the source kind, then classify it.
    anchor = commit if git else sha256
    if anchor is None:
        status = MISSING
    else:
        # Reuse the strict validators and translate their refusal into the ``invalid`` status.
        try:
            if not isinstance(anchor, str):
                raise ChecksumInvalid("not text")
            if git:
                if not _COMMIT.fullmatch(anchor.strip().lower()):
                    raise ChecksumInvalid("not a commit")
            else:
                normalize_sha256(anchor)
            status = PRESENT
        except ChecksumInvalid:
            # Reachable through ``plan_checksum`` alone (manifest parsing refuses a malformed hash first); kept for the manager.
            status = INVALID
    return ChecksumPlan(status, url, url_from, status == MISSING and is_https_url(url))


# --- supporting helpers ---


def _missing(url: str, skip: str | None) -> ChecksumMissing:
    """Build the refusal for a download with no hash, saying whether the flag could have helped."""
    # An https address may be skipped, so the message offers the flag.
    if is_https_url(url):
        return ChecksumMissing(
            f"{_shown(url)} has no SHA-256. Add one to the version's pkg.toml, or run this command again with "
            f"{SKIP_FLAG} to install it unverified.",
            url, skippable=True,
        )
    # Any other address can never be skipped, so the message says so (and notes a flag that was given).
    reason = "the flag was given, but " if skip is not None else ""
    return ChecksumMissing(
        f"{_shown(url)} has no SHA-256, and {reason}{SKIP_FLAG} works only for https:// addresses, so this download "
        "is refused. Add a sha256 to the version's pkg.toml.",
        url, skippable=False,
    )


def _shown(url: str) -> str:
    """Return *url* shortened and without user information, for a message."""
    text = url if len(url) <= 120 else url[:117] + "..."
    return re.sub(r"(?i)^(\w+://)[^/@]*@", r"\1", text)
