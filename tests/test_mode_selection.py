"""Cover the order and the rules of install-mode selection (design 6.2, D4, D31).

Behavior covered: an explicit mode wins over the install record, the record over the containing root, the
root over the process token; ``auto`` selects ``system`` when elevated and ``user`` otherwise; the chosen
mode and its reason read ``system (auto: elevated)``; an elevated run refuses a user mode however it was chosen, and a
record from an untrusted place; a mode that the package's ``modes`` list lacks (a portable package: system) is refused
with the message of the case (D55); contradictory inputs are refused instead of overridden.

Real: ``gupkg.model.mode.select_mode``, a pure function. Nothing is mocked. Out of scope: how the record and
the containing root are found (``test_mode_resolve.py``).
"""

from __future__ import annotations

import pytest

from gupkg.model.mode import PORTABLE_REFUSAL, SYSTEM_ONLY_REFUSAL, USER_ONLY_REFUSAL, ModeRefused, select_mode
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


@pytest.mark.parametrize(
    ("kwargs", "mode", "reason"),
    [
        pytest.param({"elevated": True}, "system", "auto: elevated", id="auto-elevated-selects-system"),
        pytest.param({"elevated": False}, "user", "auto: not elevated", id="auto-normal-selects-user"),
        pytest.param({"container": "user"}, "user", "inside the user root", id="root-beats-auto"),
        pytest.param({"container": "system"}, "system", "inside the system root", id="root-beats-auto-system"),
        pytest.param({"record": "system", "container": "system"}, "system", "install record", id="record-beats-root"),
        pytest.param({"explicit": "system"}, "system", "--mode", id="flag-beats-auto"),
        pytest.param({"explicit": "user", "explicit_source": "selector"}, "user", "selector",
                     id="qualified-selector-is-a-flag"),
        pytest.param({"record": "user", "elevated": False}, "user", "install record", id="record-beats-auto-normal"),
    ],
)
def test_selection_follows_the_order_of_the_design(kwargs: dict, mode: str, reason: str) -> None:
    """The first of flag, install record, containing root and the process token that decides names the mode and the reason.

    ``auto`` is used only when none of the earlier steps decides.
    """
    choice = select_mode(**kwargs)

    assert (choice.mode, choice.reason) == (mode, reason)
    assert str(choice) == f"{mode} ({reason})"


def test_auto_resolves_to_system_or_user_and_the_reason_is_printable() -> None:
    """Whatever the elevation, ``auto`` resolves to ``system`` or ``user`` and prints as ``system (auto: elevated)``."""
    chosen = {select_mode(elevated=elevated).mode for elevated in (True, False)}

    assert chosen == {"system", "user"}
    assert str(select_mode(elevated=True)) == "system (auto: elevated)"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"explicit": "user"}, id="explicit-user"),
        pytest.param({"record": "user"}, id="user-install-record"),
        pytest.param({"container": "user"}, id="path-inside-a-user-root"),
    ],
)
def test_an_elevated_run_never_acts_on_a_user_root(kwargs: dict) -> None:
    """An elevated process refuses user work however the mode was chosen, and says to use a normal shell (D31)."""
    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        select_mode(elevated=True, **kwargs)


def test_an_elevated_run_refuses_a_user_only_package_and_says_to_run_without_administrator_rights() -> None:
    """With ``auto`` an elevated run selects system; a package that lacks it is refused with the single-user message."""
    with pytest.raises(ModeRefused) as raised:
        select_mode(elevated=True, supported=("user",))

    assert str(raised.value) == USER_ONLY_REFUSAL


def test_an_install_record_from_an_untrusted_place_cannot_decide_an_elevated_run() -> None:
    """A record that an elevated run read from a user-writable folder is refused, not obeyed (fail closed)."""
    with pytest.raises(ModeRefused, match="does not trust it"):
        select_mode(elevated=True, record="system", record_trusted=False)


def test_a_trusted_system_record_decides_an_elevated_run() -> None:
    """A record read from an admin-only folder decides the mode of an elevated run as usual."""
    assert select_mode(elevated=True, record="system").reason == "install record"


@pytest.mark.parametrize(
    ("supported", "kwargs", "message"),
    [
        pytest.param(("system",), {"elevated": False}, SYSTEM_ONLY_REFUSAL, id="normal-run-auto-user-system-only"),
        pytest.param(("user",), {"explicit": "system"}, USER_ONLY_REFUSAL, id="system-flag-user-only"),
        pytest.param(("portable",), {"explicit": "system"}, PORTABLE_REFUSAL, id="portable-package-in-system"),
        pytest.param(("portable",), {"elevated": True}, PORTABLE_REFUSAL, id="portable-package-in-an-elevated-run"),
    ],
)
def test_a_mode_the_package_does_not_support_is_refused_up_front(supported: tuple[str, ...], kwargs: dict, message: str) -> None:
    """Selection fails before any work when the package's ``modes`` list lacks the chosen mode, with the message of the case."""
    with pytest.raises(ModeRefused) as raised:
        select_mode(supported=supported, **kwargs)

    assert str(raised.value) == message


def test_a_supported_mode_is_accepted() -> None:
    """A mode inside the package's ``modes`` list is selected normally; a portable package is user only."""
    assert select_mode(explicit="system", supported=("system", "user")).mode == "system"
    assert select_mode(explicit="user", supported=("user",)).mode == "user"
    assert select_mode(explicit="user", supported=("portable",)).mode == "user"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"explicit": "system", "record": "user"}, id="flag-contradicts-record"),
        pytest.param({"explicit": "system", "container": "user"}, id="flag-contradicts-root"),
        pytest.param({"record": "system", "container": "user"}, id="record-contradicts-root"),
    ],
)
def test_contradicting_inputs_are_refused_instead_of_overridden(kwargs: dict) -> None:
    """A package belongs to one scope, so ``--mode system`` on a user package is an error, not a second install."""
    with pytest.raises(ModeRefused, match="belongs to one mode"):
        select_mode(**kwargs)


def test_an_unknown_mode_is_refused() -> None:
    """A mode other than system and user is refused with the list of modes."""
    with pytest.raises(ModeRefused, match="Unknown install mode 'machine'"):
        select_mode(explicit="machine")
