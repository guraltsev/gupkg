"""Turn the targets of an operation into the ``gupkg`` calls that run it: one call, or one per distinct ``--skip-checksum`` argument.

A run has one scope, so the CLI never has to guess a mode and a batch is **one** call of the CLI that carries every target. The only reason to
make a second call is the checksum policy: ``--skip-checksum`` is a flag of the call, so targets that need a different one are not put together.

Usage and API
-------------
``make_steps(command, subjects, settings=..., options=..., skips=...)`` returns the ``Step`` list. ``subject_of(row)`` turns an inventory row into
the thing an operation acts on, and ``safe_subjects(command, subjects)`` splits off the targets the launcher cannot be given.

Implementation Approach
-----------------------
Targets are package names. ``--skip-checksum`` only ever covers a missing hash (a declared hash is always verified, with or without it), and a
target that needed a skip was approved while the others were not run without one. So targets that need no skip join the one call that carries it,
which keeps a batch at one call; only a second, different skip reason would need a call of its own. The events folder of a call is the
``.gupkg\\logs`` of its first target's root.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .client import ClientError, refuse_unsafe, targets_after_options
from .plans import PackageRow
from .session import EXIT_USAGE, Step, synthetic_entry
from .settings import UiSettings

# What a call is called in the output, where the CLI word reads badly.
LABELS = {"check-updates": "check for updates"}


@dataclass(frozen=True)
class Subject:
    """Something an operation acts on: a target, its package folder (``None`` before it exists) and its name."""

    target: str
    path: str | None
    name: str

    @property
    def package_dir(self) -> Path | None:
        """The package folder, or ``None`` when the package is not in a root yet."""
        return Path(self.path) if self.path else None


def subject_of(row: PackageRow) -> Subject:
    """Return the subject of an inventory row."""
    return Subject(row.target, row.path, row.name)


def safe_subjects(command: str, subjects: Sequence[Subject]) -> tuple[list[Subject], list[dict]]:
    """Split *subjects* into those whose target can be passed to the launcher and the entries of those that cannot.

    A name with a character such as ``& | < > ^ " % !`` or a control character is refused here, for every call (the launcher is a ``.cmd``
    file, and the CLI refuses the same characters): one such package never blocks the others.
    """
    safe: list[Subject] = []
    refused: list[dict] = []
    for subject in subjects:
        try:
            refuse_unsafe([subject.target])
        except ClientError as error:
            refused.append(synthetic_entry(subject.target, command, "failed", EXIT_USAGE, str(error)))
        else:
            safe.append(subject)
    return safe, refused


def events_dir_for(subject: Subject, settings: UiSettings) -> Path:
    """Return the ``.gupkg\\logs`` folder where a call on *subject* writes its events file: its root's, known from its folder or the settings."""
    root = subject.package_dir.parent if subject.package_dir is not None else settings.root
    return root / ".gupkg" / "logs"


def make_steps(
    command: str,
    subjects: Sequence[Subject],
    *,
    settings: UiSettings,
    options: Sequence[str] = (),
    skips: Mapping[str, tuple[str, ...]] | None = None,
    trailing: Sequence[str] = (),
) -> list[Step]:
    """Group *subjects* into calls of *command*: one call, or one per distinct ``--skip-checksum`` argument.

    Parameters
    ----------
    command : str
        The CLI command word (``update``, ``install``, ``uninstall``, ``check-updates``, ``check``, ``cleanup`` ...).
    subjects : sequence of Subject
        What to act on, in order.
    settings : UiSettings
        Where the root is (for the events folder).
    options : sequence of str
        Options of every call (``--yes``, ``--keep``, ``--no-download`` ...); targets are added after ``--``.
    skips : mapping, optional
        Per target, the extra ``--skip-checksum=<reason>`` arguments the policy decided.
    trailing : sequence of str
        Arguments that follow the targets after ``--`` (the version of ``switch``).

    Returns
    -------
    list of Step
        The calls to make, in order; empty for no subjects.
    """
    groups: dict[tuple[str, ...], list[Subject]] = {}
    for subject in subjects:
        groups.setdefault(tuple((skips or {}).get(subject.target, ())), []).append(subject)

    # Targets that need no skip join the one call that carries it.
    needing = [key for key in groups if key]
    if len(needing) == 1 and () in groups:
        groups[needing[0]] = [*groups.pop(()), *groups[needing[0]]]

    steps: list[Step] = []
    for extra, members in groups.items():
        targets = tuple(item.target for item in members)
        steps.append(Step(
            LABELS.get(command, command), targets_after_options(command, [*options, *extra], [*targets, *trailing]), targets,
            events_dir_for(members[0], settings),
        ))
    return steps
