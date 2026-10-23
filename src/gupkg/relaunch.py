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
tells the caller to stop and wait for the elevated child. ``ElevationRefused`` (exit code 2) and
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
from .system.port import System, is_elevated_run


class ElevationRefused(ModeRefused):
    """Refuse to elevate because this gupkg is not the system gupkg."""


@dataclass(frozen=True)
class ElevationOutcome:
    """What ``ensure_elevation`` did: whether elevation is needed, and whether a child was started."""

    needed: bool
    relaunched: bool = False


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

    Returns
    -------
    ElevationOutcome
        ``needed`` is false when no context requires elevation; ``relaunched`` is true when a child
        was started and the caller should wait for it.

    Raises
    ------
    ElevationRefused
        If this gupkg runs from a place that standard users can modify.
    InputNeeded
        If the confirmation was declined or could not be asked, or the elevation prompt was refused.
    """
    # No target needs the machine, or the process already holds the token: nothing to relaunch.
    if not any(context.requires_elevation for context in contexts):
        return ElevationOutcome(False)
    if (is_elevated_run(system) if elevated is None else elevated):
        return ElevationOutcome(True)

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
    if not system.relaunch_elevated(list(argv)):
        raise InputNeeded("The elevation prompt was not accepted. Nothing was changed.")
    return ElevationOutcome(True, True)
