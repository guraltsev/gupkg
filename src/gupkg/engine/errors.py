"""Turn any failure of an operation into an exit code and a structured error (design 10.1 and 10.4).

Every lifecycle entry point catches what an operation can raise and returns a ``PackageResult`` instead of
raising, so a batch continues past a failure and a front end renders data. ``describe(exception)`` maps an
exception to ``(exit_code, error)``: the error is a plain mapping with a stable ``code`` and a ``message``
(and the ``hint``, ``url`` and ``skippable`` facts the exception carries), which ``--json`` prints and the
manager reads. Failures that are the caller's to fix (a refused mode, an unknown package, a package that does not
support a mode) are exit code 2, a declined or unanswerable question is 5, and a failed operation (a checksum, a
download, an unpack, a hook) is 3. Anything unexpected is 4 and keeps its type name.

Usage and API
-------------
Call ``describe(error)`` in an ``except`` block; ``failure(...)`` builds the ``PackageResult`` of a failed target.

Implementation Approach
-----------------------
One ordered table maps exception types to a code and an exit status; the first match wins, so a subclass is listed
before its base class.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..catalog.catalog import (
    AmbiguousSelector,
    CatalogError,
    NeedsRepair,
    NoValidVersion,
    NoVersionKnown,
    NotFound,
    NotInstalled,
    UntrustedLocation,
)
from ..model.checksum import ChecksumError
from ..model.manifest import ManifestError
from ..model.mode import InputNeeded, ModeRefused
from ..model.plan import EXIT_FAILED, EXIT_INPUT, EXIT_INTERNAL, EXIT_USER, PackageResult
from ..model.settings import SettingsError
from ..selfmgmt.elevation import UntrustedLocationError
from ..services.hooks import HookError
from ..services.http import HttpError
from ..services.prepare import PrepareError
from ..services.track import ChecksumDiscoveryError, TrackError
from ..services.unpack import UnpackError
from ..system.port import ProcessFailed, ProcessNotAllowed
from .effects import EffectError
from .planner import PlanError
from .portable import PortableError

# (exception type, stable code, exit code): the first entry that matches wins.
_TABLE: tuple[tuple[type[BaseException], str, int], ...] = (
    (InputNeeded, "input-needed", EXIT_INPUT),
    (ChecksumError, "checksum-error", EXIT_FAILED),
    (ProcessNotAllowed, "needs-real-run", EXIT_FAILED),
    (ModeRefused, "refused", EXIT_USER),
    (NoVersionKnown, "no-version-known", EXIT_USER),
    (NoValidVersion, "no-valid-version", EXIT_USER),
    (NotInstalled, "not-installed", EXIT_USER),
    (NeedsRepair, "needs-repair", EXIT_USER),
    (AmbiguousSelector, "ambiguous", EXIT_USER),
    (UntrustedLocation, "untrusted-location", EXIT_USER),
    (NotFound, "not-found", EXIT_USER),
    (CatalogError, "catalog", EXIT_USER),
    (UntrustedLocationError, "untrusted-location", EXIT_USER),
    (PlanError, "cannot-plan", EXIT_USER),
    (SettingsError, "settings", EXIT_USER),
    (ManifestError, "invalid-package", EXIT_USER),
    (HookError, "hook-error", EXIT_FAILED),
    (UnpackError, "unpack-failed", EXIT_FAILED),
    (PrepareError, "prepare-failed", EXIT_FAILED),
    (ChecksumDiscoveryError, "checksum-discovery", EXIT_FAILED),
    (TrackError, "track-failed", EXIT_FAILED),
    (HttpError, "network-error", EXIT_FAILED),
    (ProcessFailed, "process-failed", EXIT_FAILED),
    (EffectError, "effect-error", EXIT_FAILED),
    (PortableError, "portable-error", EXIT_FAILED),
    (OSError, "io-error", EXIT_FAILED),
)


def describe(error: BaseException) -> tuple[int, dict[str, Any]]:
    """Return ``(exit_code, error)`` for an exception raised by an operation.

    Parameters
    ----------
    error : BaseException
        What the operation raised.

    Returns
    -------
    tuple
        The exit code of design 10.1 and a mapping with ``code`` and ``message`` plus whatever the exception
        carries (``url`` and ``skippable`` of a checksum refusal, ``hint``).
    """
    for kind, code, exit_code in _TABLE:
        if isinstance(error, kind):
            data: dict[str, Any] = {"code": code, "message": str(error)}
            if isinstance(error, ChecksumError):
                data.update(error.to_data())
            hint = getattr(error, "hint", None)
            if hint:
                data["hint"] = hint
            return getattr(error, "exit_code", exit_code), data
    return EXIT_INTERNAL, {"code": "internal-error", "message": f"{type(error).__name__}: {error}"}


def failure(package: str, operation: str, error: BaseException, **fields: Any) -> PackageResult:
    """Return the ``PackageResult`` of a target whose operation raised *error*.

    Extra keyword arguments (``version``, ``mode``, ``warnings``, ``log``...) are copied into the result.
    """
    exit_code, data = describe(error)
    return PackageResult(package, operation, False, "failed", exit_code=exit_code, error=data, **fields)


def with_details(result: PackageResult, details: Mapping[str, Any]) -> PackageResult:
    """Return *result* with *details* merged into its details."""
    return PackageResult(
        result.package, result.operation, result.ok, result.status, result.version, result.previous, result.mode,
        result.exit_code, result.error, result.receipt, result.log, result.warnings, {**result.details, **details},
    )
