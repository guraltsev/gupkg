"""Cover how the UI turns the targets of an operation into calls (``gupkg_ui.steps``): the label of a call that carries a ``--skip-checksum`` argument.

Behaviour covered: a call that relays ``--skip-checksum=<reason>`` is labelled ``update (--skip-checksum=<reason>)`` so the run view says which approval it carries, and a call without
one is labelled by the command alone; targets that need no skip join the one call that carries it.

Real: ``make_steps`` and the settings of a temporary root. Mocked: nothing.
Out of scope: running the calls (``test_ui_flows.py``).
"""

from __future__ import annotations

from pathlib import Path

from gupkg.model.manager_policy import USER_APPROVED, skip_argument
from gupkg_ui.steps import Subject, make_steps
from tests.ui_support import FakeGupkg, make_env


def test_a_call_with_a_skip_checksum_argument_carries_it_in_its_label(tmp_path: Path) -> None:
    """The label names the relayed argument; the plain call is just the command; the target without a skip joins the call that has one."""
    settings = make_env(tmp_path, FakeGupkg(tmp_path)).settings
    argument = skip_argument(USER_APPROVED)

    (step,) = make_steps("update", [Subject("a", None, "a"), Subject("b", None, "b")], settings=settings, options=["--yes"], skips={"a": (argument,)})
    (plain,) = make_steps("update", [Subject("b", None, "b")], settings=settings, options=["--yes"])

    assert step.label == f"update ({argument})" == "update (--skip-checksum=user-approved)"
    assert step.targets == ("b", "a")
    assert argument in step.args and plain.label == "update" and argument not in plain.args
