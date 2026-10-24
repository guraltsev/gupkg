"""Apply ``[manager] checksum_missing`` for ``gupkg-manager`` (design 9.1 and 15).

The policy for a missing hash lives entirely in the manager; the CLI only knows "refuse" and
the explicit ``--skip-checksum``. This module is the manager's side: it reads the setting,
decides per target whether to run it, and produces the ``--skip-checksum=<reason>`` argument
that relays the decision to the CLI. It is pure, never prompts (the caller supplies the answer
function) and the CLI never imports it.

Usage and API
-------------
``manager_checksum_policy(settings, elevated=...)`` returns ``ask``, ``refuse`` or ``skip``.
``decide_target(plan, policy, interactive=..., ask=...)`` returns a ``TargetDecision``:
``run`` and the extra ``arguments`` for the call, or a refusal with the words to show.
``decide_targets`` does that for a batch, asking at most once per package.

Implementation Approach
-----------------------
A plan entry marked ``invalid`` (a malformed declared hash) is never asked about and never skipped: the
target is refused with its own message. (The CLI's plan carries only ``present`` or ``missing``: a malformed hash makes the version
invalid, so the manager (issue 030) builds this ``invalid`` entry from the error document, whose ``code`` is ``invalid-checksum``.) Fail-closed everywhere: an unknown policy value, a plan entry that is not clearly ``missing``
and skippable, no way to ask, a failed answer: all of them mean the target is not run with a
skip. In an elevated run only admin-only layers count (the settings loader already drops the
user layer; the source layer is checked again here, and a value from the user or portable layer
means ``refuse``). A target whose URL is not ``https://`` is never offered a skip, because the CLI
would refuse it anyway. The answer to a question is never stored.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .checksum import INVALID, MISSING, SKIP_FLAG, SKIP_REASON_OF, SKIPPED_SETTING, USER_APPROVED, ChecksumPlan
from .settings import LAYER_PORTABLE, LAYER_USER, Settings

POLICIES = ("ask", "refuse", "skip")
POLICY_KEY = "manager.checksum_missing"
REFUSED_NO_CHECKSUM = "refused: no checksum"
REFUSED_INVALID_CHECKSUM = "refused: the checksum in pkg.toml is malformed (fix it; a malformed checksum cannot be skipped)"
_USER_LAYERS = (LAYER_USER, LAYER_PORTABLE)


def manager_checksum_policy(settings: Settings, *, elevated: bool = False) -> str:
    """Return the effective ``[manager] checksum_missing``: ``ask``, ``refuse`` or ``skip``.

    In an elevated run a value that came from the user or portable layer is not honoured
    and the answer is ``refuse``; an unknown value is ``refuse`` as well.
    """
    try:
        value = settings[POLICY_KEY]
        layer = settings.source_of(POLICY_KEY)
    except KeyError:
        return "refuse"
    if value not in POLICIES or (elevated and layer in _USER_LAYERS):
        return "refuse"
    return value


def skip_argument(decision: str) -> str:
    """Return the CLI argument that relays *decision* (``skipped-setting`` or ``user-approved``)."""
    if decision not in (SKIPPED_SETTING, USER_APPROVED):
        raise ValueError(f"the manager relays only {SKIPPED_SETTING} and {USER_APPROVED}, not {decision!r}")
    return f"{SKIP_FLAG}={SKIP_REASON_OF[decision]}"


@dataclass(frozen=True)
class TargetDecision:
    """What the manager does with one target.

    Attributes
    ----------
    run : bool
        Whether to run the target.
    arguments : tuple of str
        Extra CLI arguments for the call: empty, or the ``--skip-checksum=<reason>`` pair.
    status : str
        ``run``, ``refused`` or ``cancelled``.
    message : str
        The line to show for a target that is not run (``refused: no checksum`` ...).
    """

    run: bool
    arguments: tuple[str, ...] = ()
    status: str = "run"
    message: str = ""


def _refused(plan: ChecksumPlan) -> TargetDecision:
    """Return the refusal of a target, saying why a skip was not possible when the URL is not https."""
    suffix = "" if plan.skippable else " (the URL is not https, so the checksum cannot be skipped)"
    return TargetDecision(False, (), "refused", REFUSED_NO_CHECKSUM + suffix)


def decide_target(
    plan: ChecksumPlan,
    policy: str,
    *,
    interactive: bool,
    ask: Callable[[ChecksumPlan], bool] | None = None,
) -> TargetDecision:
    """Decide one target from its plan entry and the policy.

    Parameters
    ----------
    plan : ChecksumPlan
        The target's ``sha256`` status from ``--plan --json``.
    policy : str
        ``manager_checksum_policy(...)``.
    interactive : bool
        Whether someone can answer (a TTY, and not ``--yes``).
    ask : callable, optional
        Shows the package, version, URL and its source and returns ``True`` for "install once".
        Called at most once for this target.

    Returns
    -------
    TargetDecision
        Run as planned (hash present), run with a relayed skip, refuse, or cancel.
    """
    if plan.sha256 == INVALID:
        return TargetDecision(False, (), "refused", REFUSED_INVALID_CHECKSUM)
    if plan.sha256 != MISSING:
        return TargetDecision(True) if plan.sha256 == "present" else _refused(plan)
    if not plan.skippable:
        return _refused(plan)
    if policy == "skip":
        return TargetDecision(True, (skip_argument(SKIPPED_SETTING),))
    if policy == "ask" and interactive and ask is not None:
        if ask(plan) is True:
            return TargetDecision(True, (skip_argument(USER_APPROVED),))
        return TargetDecision(False, (), "cancelled", "cancelled: left unchanged")
    return _refused(plan)


def decide_targets(
    plans: Mapping[str, ChecksumPlan],
    policy: str,
    *,
    interactive: bool,
    ask: Callable[[str, ChecksumPlan], bool] | None = None,
) -> dict[str, TargetDecision]:
    """Decide a batch, in order, asking once per package; one refusal never stops the others.

    Parameters
    ----------
    plans : mapping of str to ChecksumPlan
        Target name to plan entry.
    ask : callable, optional
        ``ask(name, plan)`` returns ``True`` to install once without a checksum.
    """
    decisions: dict[str, TargetDecision] = {}
    for name, plan in plans.items():
        answer = None if ask is None else (lambda entry, name=name: ask(name, entry))
        decisions[name] = decide_target(plan, policy, interactive=interactive, ask=answer)
    return decisions


def command_line(base: Sequence[str], decision: TargetDecision) -> list[str]:
    """Return *base* with the target's extra arguments, for a target that runs."""
    if not decision.run:
        raise ValueError("a target that is not run has no command line")
    return [*base, *decision.arguments]
