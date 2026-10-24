"""Decide, once and up front, whether a CLI call must relaunch itself elevated for system mode (design 9.6, D19, D25).

System mode needs an administrator token. A non-elevated gupkg may start one elevated run of *itself*, and
only when its own version directory and root are admin-only: a gupkg in a user-writable place (a user-mode or
bundled copy) refuses and says that system installs need the system gupkg, because an elevated process
started from code that a standard user can change would hand that user administrator rights. The relaunch
is confirmed first (or ``--yes`` skips the question), and a declined confirmation, an unanswerable question
under ``--no-input`` or a refused UAC prompt is exit code 5 with nothing changed.

Usage and API
-------------
After resolving the mode of every target of a CLI call, pass all the contexts to
``ensure_elevation(contexts, system=..., argv=..., version_dir=..., root=...)``. The call checks once for the
whole call, however many system targets there are, and relaunches at most once. ``ElevationOutcome.relaunched``
says an elevated child ran to its end; the call waits for it and ``exit_code`` is the child's. ``ElevationRefused`` (exit code 2) and
``InputNeeded`` (exit code 5, from ``gupkg.model.mode``) carry the message to print.

Implementation Approach
-----------------------
The decision reads only the ``ModeContext`` objects, the elevation reported by the ``System`` boundary
(unknown counts as elevated) and the access-control answer of ``System.directory_untrusted_reason`` for
gupkg's own folders. The relaunch itself is ``System.relaunch_elevated``, so tests observe it on a ``FakeSystem``.

See Also
--------
gupkg.modes : resolves the mode and the admission rules of the elevated child.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .model.mode import InputNeeded, ModeContext, ModeRefused
from .system.port import ProcessFailed, System, is_elevated_run, unsafe_relaunch_argument


class ElevationRefused(ModeRefused):
    """Refuse to elevate because this gupkg is not the system gupkg."""


@dataclass(frozen=True)
class ElevationOutcome:
    """What ``ensure_elevation`` did: whether elevation is needed, and whether a child was started."""

    needed: bool
    relaunched: bool = False
    exit_code: int | None = None


def ensure_elevation(
    contexts: Sequence[ModeContext],
    *,
    system: System,
    argv: Sequence[str],
    version_dir: Path,
    root: Path,
    yes: bool = False,
    no_input: bool = False,
    confirm: Callable[[str], bool] | None = None,
    elevated: bool | None = None,
    on_interrupt: Callable[[], None] | None = None,
    note: str = "",
) -> ElevationOutcome:
    """Relaunch this gupkg elevated once when any target needs system mode and the process is not elevated.

    Parameters
    ----------
    contexts : sequence of ModeContext
        The resolved context of every target of the CLI call.
    system : System
        The machine boundary (elevation, access check, relaunch).
    argv : sequence of str
        The command line for the elevated child.
    version_dir : Path
        The running gupkg's version directory; it and *root* must be admin-only.
    root : Path
        The root that holds that version directory.
    yes : bool, default=False
        ``--yes``: relaunch without asking (D25).
    no_input : bool, default=False
        ``--no-input``: the question becomes exit code 5.
    confirm : callable, optional
        Asks the user and returns whether to relaunch; ``None`` means nobody can be asked.
    elevated : bool, optional
        Overrides the elevation reported by *system*.
    on_interrupt : callable, optional
        Called once when the user interrupts the wait for the elevated child; it asks the child to stop.
    note : str, optional
        Added to the message of an abandoned wait (where the child's stop marker and events file are).

    Returns
    -------
    ElevationOutcome
        ``needed`` is false when no context requires elevation; ``relaunched`` is true when a child
        was started and has ended, and ``exit_code`` is its exit code.

    Raises
    ------
    ElevationRefused
        If this gupkg runs from a place that standard users can modify, or an argument holds a character that is
        refused in an elevated child's command line (checked before anything is asked).
    InputNeeded
        If the confirmation was declined or could not be asked, the elevation prompt was refused, or the wait for the
        elevated child was given up after an interrupt.
    """
    # No target needs the machine, or the process already holds the token: nothing to relaunch.
    if not any(context.requires_elevation for context in contexts):
        return ElevationOutcome(False)
    if (is_elevated_run(system) if elevated is None else elevated):
        return ElevationOutcome(True)

    # The child's command line is built from user-chosen targets: refuse every shell-significant character first (T1).
    for part in argv:
        reason = unsafe_relaunch_argument(part)
        if reason is not None:
            raise ElevationRefused(
                f"The elevated gupkg is not started: {reason} is not allowed in a command that runs with administrator rights. "
                "Rename the package folder or file, or run gupkg from an administrator shell. Nothing was changed."
            )

    # A target that starts with "-" could be read as an option by the child (the targets follow "--", but never rely on one rule).
    if "--" in argv:
        for target in list(argv)[list(argv).index("--") + 1:]:
            if str(target).startswith("-"):
                raise ElevationRefused(
                    f"The elevated gupkg is not started: the target {target!r} starts with '-'. "
                    "Rename the package folder (a name starting with '-' would be read as an option). Nothing was changed."
                )

    # Only an admin-only gupkg may start an elevated copy of itself: judge its own folders first, so a
    # user-writable gupkg refuses before it asks anything.
    for folder in (version_dir, root):
        reason = system.directory_untrusted_reason(folder)
        if reason is not None:
            raise ElevationRefused(
                f"System installs need the system gupkg. This gupkg runs from {version_dir}, and {folder} can be "
                f"changed by standard users ({reason}), so it will not start an elevated copy of itself. "
                "Install gupkg system-wide (an administrator shell) and run that one."
            )

    # Confirm once for the whole call; --no-input turns the question into exit code 5.
    if not yes:
        question = "System mode needs administrator rights. Relaunch gupkg elevated for this call?"
        if no_input or confirm is None:
            raise InputNeeded(f"{question} Pass --yes to relaunch without asking. Nothing was changed.")
        if not confirm(question):
            raise InputNeeded("Elevation was declined. Nothing was changed.")

    # One relaunch for every system target of the call.
    run = system.relaunch_elevated(list(argv), on_interrupt=on_interrupt)
    if run.refused is not None:
        raise ElevationRefused(
            f"The elevated gupkg is not started: {run.refused}. Nothing was asked and nothing was changed."
        )
    if not run.started:
        raise InputNeeded("The elevation prompt was not accepted. Nothing was changed.")
    if run.error is not None:
        raise ProcessFailed(f"The elevated gupkg was started, but waiting for it failed: {run.error}. It may still be running. {note}".strip())
    if run.abandoned or run.exit_code is None:
        raise InputNeeded(
            f"Stopped waiting for the elevated gupkg after an interrupt; it may still be running. {note}".strip()
        )
    return ElevationOutcome(True, True, run.exit_code)
