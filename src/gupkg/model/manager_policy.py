"""Apply ``[manager] checksum_missing`` for ``gupkg-manager`` (design 9.1 and 15).

The policy for a missing hash lives entirely in the manager; the CLI only knows "refuse" and
the explicit ``--skip-checksum``. This module is the manager's side: it reads the setting,
decides per target whether to run it, and produces the ``--skip-checksum=<reason>`` argument
that relays the decision to the CLI. It is pure, never prompts (the caller supplies the answer
function) and the CLI never imports it.

Usage and API
-------------
``manager_checksum_policy(settings)`` returns ``ask``, ``refuse`` or ``skip``.
``decide_target(plan, policy, interactive=..., ask=...)`` returns a ``TargetDecision``:
``run`` and the extra ``arguments`` for the call, or a refusal with the words to show.
``decide_targets`` does that for a batch, asking at most once per package.

Implementation Approach
-----------------------
A plan entry marked ``invalid`` (a malformed declared hash) is never asked about and never skipped: the
target is refused with its own message. (The CLI's plan carries only ``present`` or ``missing``: a malformed hash makes the version
invalid, so the manager (issue 030) builds this ``invalid`` entry from the error document, whose ``code`` is ``invalid-checksum``.) Fail-closed everywhere: an unknown policy value, a plan entry that is not clearly ``missing``
and skippable, no way to ask, a failed answer: all of them mean the target is not run with a
skip. The settings are those of the run's one scope, so every layer in them is one the run may trust. A target whose URL is not ``https://`` is never offered a skip, because the CLI
would refuse it anyway. The answer to a question is never stored.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .checksum import INVALID, MISSING, SKIP_FLAG, SKIP_REASON_OF, SKIPPED_SETTING, USER_APPROVED, ChecksumPlan
from .settings import Settings

# The accepted values of the ``[manager] checksum_missing`` setting.
POLICIES = ("ask", "refuse", "skip")
# The settings key that holds the policy.
POLICY_KEY = "manager.checksum_missing"
# The message shown for a target refused because it has no hash.
REFUSED_NO_CHECKSUM = "refused: no checksum"
# The message shown for a target refused because its declared hash is malformed.
REFUSED_INVALID_CHECKSUM = "refused: the checksum in pkg.toml is malformed (fix it; a malformed checksum cannot be skipped)"


def manager_checksum_policy(settings: Settings) -> str:
    """Return the effective ``[manager] checksum_missing`` policy, treating an unknown value as ``refuse``.

    Parameters
    ----------
    settings : Settings
        The effective settings of the run.

    Returns
    -------
    str
        ``ask``, ``refuse`` or ``skip``.
    """
    # Fail closed: a missing key or an unknown value means refuse.
    try:
        value = settings[POLICY_KEY]
    except KeyError:
        return "refuse"
    if value not in POLICIES:
        return "refuse"
    return value


def skip_argument(decision: str) -> str:
    """Return the CLI argument that relays *decision* (``skipped-setting`` or ``user-approved``).

    Parameters
    ----------
    decision : str
        ``skipped-setting`` or ``user-approved``.

    Returns
    -------
    str
        The ``--skip-checksum=<reason>`` argument.

    Raises
    ------
    ValueError
        If *decision* is not one the manager may relay.

    Examples
    --------
    Basic usage:

    >>> skip_argument("skipped-setting")
    '--skip-checksum=setting'
    >>> skip_argument("user-approved")
    '--skip-checksum=user-approved'

    Edge cases:

    >>> skip_argument("skipped-cli")
    Traceback (most recent call last):
        ...
    ValueError: the manager relays only skipped-setting and user-approved, not 'skipped-cli'
    """
    if decision not in (SKIPPED_SETTING, USER_APPROVED):
        raise ValueError(f"the manager relays only {SKIPPED_SETTING} and {USER_APPROVED}, not {decision!r}")
    return f"{SKIP_FLAG}={SKIP_REASON_OF[decision]}"


@dataclass(frozen=True)
class TargetDecision:
    """Hold what the manager does with one target.

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

    Examples
    --------
    Basic usage:

    >>> present = ChecksumPlan("present", "https://example.org/tool.zip", "pkg.toml", False)
    >>> decide_target(present, "refuse", interactive=False).run
    True

    A missing hash follows the policy:

    >>> missing = ChecksumPlan("missing", "https://example.org/tool.zip", "pkg.toml", True)
    >>> decide_target(missing, "skip", interactive=False).arguments
    ('--skip-checksum=setting',)
    >>> decide_target(missing, "ask", interactive=True, ask=lambda plan: True).arguments
    ('--skip-checksum=user-approved',)
    >>> decide_target(missing, "ask", interactive=True, ask=lambda plan: False).status
    'cancelled'
    >>> decide_target(missing, "refuse", interactive=True).status
    'refused'

    Edge cases:

    >>> insecure = ChecksumPlan("missing", "http://example.org/tool.zip", "pkg.toml", False)
    >>> decide_target(insecure, "skip", interactive=False).status
    'refused'
    """
    # A malformed hash is never asked about or skipped; a present hash needs no decision.
    if plan.sha256 == INVALID:
        return TargetDecision(False, (), "refused", REFUSED_INVALID_CHECKSUM)
    if plan.sha256 != MISSING:
        return TargetDecision(True) if plan.sha256 == "present" else _refused(plan)
    # A missing hash can be skipped only for an https URL.
    if not plan.skippable:
        return _refused(plan)
    # The setting skips without asking; ``ask`` needs someone who can answer.
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
    policy : str
        ``manager_checksum_policy(...)``.
    interactive : bool
        Whether someone can answer (a TTY, and not ``--yes``).
    ask : callable, optional
        ``ask(name, plan)`` returns ``True`` to install once without a checksum.

    Returns
    -------
    dict of str to TargetDecision
        The decision for each target, in the order of *plans*.

    Examples
    --------
    Basic usage:

    >>> plans = {
    ...     "good": ChecksumPlan("present", "https://example.org/a.zip", "pkg.toml", False),
    ...     "bare": ChecksumPlan("missing", "https://example.org/b.zip", "pkg.toml", True),
    ... }
    >>> decisions = decide_targets(plans, "refuse", interactive=False)
    >>> {name: decision.status for name, decision in decisions.items()}
    {'good': 'run', 'bare': 'refused'}
    """
    # Decide targets independently, binding each target's name into its own ask callback.
    decisions: dict[str, TargetDecision] = {}
    for name, plan in plans.items():
        answer = None if ask is None else (lambda entry, name=name: ask(name, entry))
        decisions[name] = decide_target(plan, policy, interactive=interactive, ask=answer)
    return decisions


def command_line(base: Sequence[str], decision: TargetDecision) -> list[str]:
    """Return *base* with the target's extra arguments, for a target that runs.

    Parameters
    ----------
    base : sequence of str
        The CLI command line without the checksum relay.
    decision : TargetDecision
        The decision for the target; it must be one that runs.

    Returns
    -------
    list of str
        *base* followed by ``decision.arguments``.

    Raises
    ------
    ValueError
        If the decision is not to run the target.

    Examples
    --------
    Basic usage:

    >>> missing = ChecksumPlan("missing", "https://example.org/tool.zip", "pkg.toml", True)
    >>> decision = decide_target(missing, "skip", interactive=False)
    >>> command_line(["gupkg", "install", "tool"], decision)
    ['gupkg', 'install', 'tool', '--skip-checksum=setting']

    Edge cases:

    >>> command_line(["gupkg", "install", "tool"], decide_target(missing, "refuse", interactive=False))
    Traceback (most recent call last):
        ...
    ValueError: a target that is not run has no command line
    """
    if not decision.run:
        raise ValueError("a target that is not run has no command line")
    return [*base, *decision.arguments]


# --- supporting helpers ---


def _refused(plan: ChecksumPlan) -> TargetDecision:
    """Return the refusal of a target, saying why a skip was not possible when the URL is not https."""
    suffix = "" if plan.skippable else " (the URL is not https, so the checksum cannot be skipped)"
    return TargetDecision(False, (), "refused", REFUSED_NO_CHECKSUM + suffix)
