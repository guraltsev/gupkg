"""Download a source artifact under the checksum policy (design 9.1 and 9.2).

``fetch_artifact`` is what the built-in download script calls: it applies the rule *before*
a byte is requested, downloads through the shared client, compares the hash that was observed
with the one the version declares, and returns the file with the ``ChecksumRecord`` the receipt
stores. ``fetch_git`` does the same for a git source, whose commit is the anchor.

Usage and API
-------------
``fetch_artifact(client, url, expected_sha256=..., skip=...)`` returns a ``FetchedArtifact``
(``download`` and ``record``) or raises ``ChecksumMissing``, ``ChecksumMismatch`` or
``ChecksumInvalid`` (all ``ChecksumError``: exit code 3, a stable ``code``) or ``HttpError``.
``skip`` is the decision of ``--skip-checksum`` (``gupkg.model.checksum.parse_skip_flag``), never a
setting: this module takes no settings and reads no environment, so a ``[manager]`` value
cannot reach it. ``reverify(fetched)`` hashes the cached file again and must be called right
before the file is unpacked or copied.

Implementation Approach
-----------------------
A missing hash with no usable skip stops the run before the client is called, so the download
is never started. When the skip applies, the client is told to require ``https://`` for the
address and every redirect hop, and the final address is judged again on its parsed form. The
client computes the SHA-256 of the bytes it wrote to the cache; this module compares that digest
(and not a digest it read from anywhere else) with the expected one, and records it as
``observed_sha256``. The cache entry is renamed into place only after the client verified it;
``reverify`` closes the gap between that moment and the use of the file. The skip is shown as a
warning event, which the engine reports in the Verify phase, and an elevated run shows it too.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from ..model.checksum import (
    ChecksumMismatch,
    ChecksumRecord,
    check_before_download,
    git_gate,
    git_record,
    hashes_equal,
    verify_observed,
)
from ..report.events import Step, Warning, emit
from ..system.port import Mode, System
from .http import Download, DownloadMismatch, HttpClient, file_digest
from .unpack.gitsource import CommitMismatch, checkout_commit


@dataclass(frozen=True)
class FetchedArtifact:
    """A downloaded artifact and the checksum facts for its receipt."""

    download: Download
    record: ChecksumRecord


def _warn_skipped(url: str, decision: str, what: str) -> None:
    """Show that this download proceeds without verification."""
    emit(Warning(f"{what} has no checksum and is not verified: continuing because the call allowed it ({decision})."))


def fetch_artifact(
    client: HttpClient,
    url: str,
    *,
    expected_sha256: str | None,
    skip: str | None = None,
    sha256_from: str | None = None,
    headers: Mapping[str, str] | None = None,
    label: str | None = None,
) -> FetchedArtifact:
    """Download *url* under the missing-checksum policy and return it with its receipt record.

    Parameters
    ----------
    client : HttpClient
        The shared client; its cache directory receives the file.
    url : str
        The address written in the version's ``pkg.toml``.
    expected_sha256 : str or None
        The version's ``sha256``; ``None`` when it has none.
    skip : str or None
        The decision of ``--skip-checksum`` for this call, or ``None``.
    sha256_from : str, optional
        The provenance of the expected hash, recorded in the receipt.
    headers, label : optional
        Passed to the client.

    Raises
    ------
    ChecksumMissing
        No hash, and no allowed skip. Nothing was requested.
    ChecksumMismatch
        The bytes do not have the declared SHA-256. The file is deleted. No flag changes this.
    ChecksumInvalid
        The declared hash or the skip decision is malformed.
    HttpError
        The download itself failed, or a hop was not https during a skipped download.
    """
    verification = check_before_download(url, expected_sha256, skip)
    name = label or url.rsplit("/", 1)[-1].split("?", 1)[0] or "the download"
    if verification.skipped:
        _warn_skipped(url, verification.decision, name)
    try:
        download = client.download(
            url, expected_sha256=verification.expected, headers=headers, label=label, require_https=verification.skipped
        )
    except DownloadMismatch as error:
        raise ChecksumMismatch(
            f"{name} does not have the SHA-256 the version declares ({verification.expected}). "
            f"A mismatch is never skipped. {error}",
            url, expected=verification.expected,
        ) from error
    record = verify_observed(
        verification, download.sha256, url=url, final_url=download.final_url, size=download.size, sha256_from=sha256_from
    )
    if verification.expected is not None:
        emit(Step(f"SHA-256 of {name} verified", "ok"))
    return FetchedArtifact(download, record)


def reverify(fetched: FetchedArtifact) -> None:
    """Hash the cached file again and require it to be the file that was verified.

    Call it immediately before the artifact is unpacked or copied: it closes the gap between
    the download's verification and the use of the file.

    Raises
    ------
    ChecksumMismatch
        If the file changed, or vanished, after it was verified.
    """
    observed = fetched.record.observed_sha256 or ""
    try:
        digest, size = file_digest(fetched.download.path)
    except OSError as error:
        raise ChecksumMismatch(f"The verified file {fetched.download.path} cannot be read again: {error}",
                               fetched.record.url) from error
    if not hashes_equal(digest, observed) or size != fetched.download.size:
        raise ChecksumMismatch(
            f"{fetched.download.path.name} changed after it was verified (SHA-256 {digest}, verified {observed}).",
            fetched.record.url, expected=observed, observed=digest,
        )


def fetch_git(
    system: System, mode: Mode, url: str, ref: str | None, commit: str | None, destination: Path, work: Path, *, skip: str | None = None
) -> ChecksumRecord:
    """Check out a git source under the policy and return its receipt record.

    The rule runs before ``git`` is started: a pinned *commit* is the anchor, a source with only
    a *ref* is refused unless *skip* applies (https only). The commit that was checked out is
    recorded, and must equal a pinned one.

    Raises
    ------
    ChecksumMissing
        Nothing is pinned and no skip applies. Nothing was fetched.
    ChecksumMismatch
        The repository served another commit than the pinned one.
    UnpackError
        ``git`` failed.
    """
    verification = git_gate(url, commit, skip)
    if verification.skipped:
        _warn_skipped(url, verification.decision, url)
    try:
        resolved = checkout_commit(system, mode, url, ref, verification.expected, destination, work)
    except CommitMismatch as error:
        raise ChecksumMismatch(str(error), url, expected=error.expected, observed=error.observed) from error
    return git_record(verification, resolved, url=url)
