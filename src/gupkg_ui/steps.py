"""Group the targets of an operation into the ``gupkg`` calls that run it: one non-elevated call and at most one elevated call.

The grouping rule of design section 13.2: the targets of the user and portable modes run in one non-elevated call, and every system target
runs in **one** call that the CLI carries out in a single elevated child, so a batch shows one administrator prompt however many
system packages it holds. A system target is only run when this gupkg can elevate (it is the system gupkg, installed in an admin-only
root); from any other gupkg the call is never made and the target is reported as needing the system gupkg, so no prompt is requested
and no administrator right is ever sought from a gupkg that a standard user can change.

Usage and API
-------------
``make_steps(command, subjects, settings=..., can_elevate=..., options=..., skips=...)`` returns the ``Step`` list and the entries of the
targets that could not be run. ``can_elevate(location, system)`` says whether this gupkg may ask for administrator rights.
``subject_of(row)`` turns an inventory row into the thing an operation acts on.

Implementation Approach
-----------------------
Targets are qualified (``system:name``), so the CLI never has to guess a mode. Targets that need a different ``--skip-checksum`` argument
are never put in one call (the flag is per call), but the manager's policy gives every target of a mode the same one, so a batch still
makes one call per group. The events folder of a call is the ``.gupkg\\logs`` of its first target's root.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.port import System

from .client import ClientError, refuse_unsafe, targets_after_options
from .plans import PackageRow
from .session import EXIT_USAGE, Step, synthetic_entry
from .settings import UiSettings

# Commands whose system targets need the administrator token (``check`` only with ``--fix``; ``check-updates`` never elevates).
ELEVATING = frozenset({"install", "update", "uninstall", "switch", "cleanup", "link"})
NEEDS_SYSTEM_GUPKG = (
    "this is a system package: only the system gupkg can change it (it asks for administrator rights once). "
    "Start gupkg-manager from the system gupkg."
)


@dataclass(frozen=True)
class Subject:
    """Something an operation acts on: a qualified target, its mode and its package folder (``None`` before it exists)."""

    target: str
    mode: str | None
    path: str | None
    name: str

    @property
    def package_dir(self) -> Path | None:
        """The package folder, or ``None`` when the package is not in a root yet."""
        return Path(self.path) if self.path else None


def subject_of(row: PackageRow) -> Subject:
    """Return the subject of an inventory row."""
    return Subject(row.target, row.mode, row.path, row.name)


def safe_subjects(command: str, subjects: Sequence[Subject]) -> tuple[list[Subject], list[dict]]:
    """Split *subjects* into those whose target can be passed to the launcher and the entries of those that cannot.

    A name with a character such as ``& | < > ^ " % !`` or a control character is refused here, for every call (the launcher is a ``.cmd``
    file, and the CLI refuses the same characters before it asks for administrator rights): one such package never blocks the others.
    """
    safe: list[Subject] = []
    refused: list[dict] = []
    for subject in subjects:
        try:
            refuse_unsafe([subject.target])
        except ClientError as error:
            refused.append(synthetic_entry(subject.target, command, "failed", EXIT_USAGE, str(error), mode=subject.mode))
        else:
            safe.append(subject)
    return safe, refused


def can_elevate(location: RunningGupkg, system: System) -> bool:
    """Return whether this gupkg is the system gupkg: installed in an admin-only root, the only gupkg that may ask for administrator rights."""
    return location.kind == "installed" and location.root is not None and system.directory_is_admin_only(location.root)


def events_dir_for(subject: Subject, settings: UiSettings) -> Path:
    """Return the ``.gupkg\\logs`` folder where a call on *subject* writes its events file: its root's, known from its folder or the settings."""
    root = subject.package_dir.parent if subject.package_dir is not None else settings.root_of(subject.mode or settings.default_mode)  # Removed in issue 035 T05.
    return root / ".gupkg" / "logs"


def make_steps(
    command: str,
    subjects: Sequence[Subject],
    *,
    settings: UiSettings,
    can_elevate: bool,
    options: Sequence[str] = (),
    skips: Mapping[str, tuple[str, ...]] | None = None,
    fix: bool = False,
    trailing: Sequence[str] = (),
    labels: tuple[str, str] = ("user targets", "system targets"),
) -> tuple[list[Step], list[dict]]:
    """Group *subjects* into calls of *command* and report the subjects that cannot run.

    Parameters
    ----------
    command : str
        The CLI command word (``update``, ``install``, ``uninstall``, ``check-updates``, ``check``, ``cleanup`` ...).
    subjects : sequence of Subject
        What to act on, in order.
    settings : UiSettings
        Where the roots are (for the events folders).
    can_elevate : bool
        Whether this gupkg may ask for administrator rights.
    options : sequence of str
        Options of every call (``--yes``, ``--keep``, ``--no-download`` ...); targets are added after ``--``.
    skips : mapping, optional
        Per target, the extra ``--skip-checksum=<reason>`` arguments the policy decided.
    fix : bool
        For ``check``: whether ``--fix`` is among the options, which makes its system targets need the token.
    trailing : sequence of str
        Arguments that follow the targets after ``--`` (the version of ``switch``).
    labels : tuple of str
        The names of the user group and the system group in the output.

    Returns
    -------
    tuple
        The steps (user group first, then the one elevated call) and the entries of the subjects that were not run.
    """
    elevating = command in ELEVATING or (command == "check" and fix)
    steps: list[Step] = []
    refused: list[dict] = []
    groups: dict[tuple[bool, tuple[str, ...]], list[Subject]] = {}

    # Every system target of an elevating command goes to the elevated group, or is reported when this gupkg cannot elevate.
    for subject in subjects:
        wants_token = elevating and subject.mode == "system"
        if wants_token and not can_elevate:
            refused.append(synthetic_entry(subject.target, command, "needs-system-gupkg", EXIT_USAGE, NEEDS_SYSTEM_GUPKG, mode="system"))
            continue
        extra = tuple((skips or {}).get(subject.target, ()))
        groups.setdefault((wants_token, extra), []).append(subject)

    # ``--skip-checksum`` is a per-call flag, but it only ever covers a missing hash (a declared hash is always verified, with or without it), and a
    # target that needed a skip was approved while the others were not run without one. So targets that need no skip join the one call that carries
    # it, which keeps a batch at one call per group; only a second, different skip reason would need a call of its own.
    for elevated in (False, True):
        needing = [key for key in groups if key[0] is elevated and key[1]]
        plain = (elevated, ())
        if len(needing) == 1 and plain in groups:
            groups[needing[0]] = [*groups.pop(plain), *groups[needing[0]]]

    # One call per group; the non-elevated ones come first, so the administrator prompt appears last.
    for (elevated, extra), members in sorted(groups.items(), key=lambda item: item[0][0]):
        targets = tuple(item.target for item in members)
        steps.append(Step(
            labels[1] if elevated else labels[0], targets_after_options(command, [*options, *extra], [*targets, *trailing]), targets,
            events_dir_for(members[0], settings), elevated,
        ))
    return steps, refused
