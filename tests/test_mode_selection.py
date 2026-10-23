"""Cover the order and the rules of install-mode selection (design 6.2, D4, D31).

Behavior covered: an explicit mode wins over the install record, the record over the containing root, the
root over ``default_mode``; ``auto`` selects ``system`` when elevated and ``user`` otherwise and never
``portable``; the chosen mode and its reason read ``system (auto: elevated)``; an elevated run refuses a user
or portable mode however it was chosen, and a record from an untrusted place; a mode that the package's
``modes`` list lacks is refused with the right advice; contradictory inputs are refused instead of overridden.

Real: ``gupkg.model.mode.select_mode``, a pure function. Nothing is mocked. Out of scope: how the record and
the containing root are found (``test_mode_resolve.py``).
"""

from __future__ import annotations

import pytest

from gupkg.model.mode import ModeRefused, select_mode
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


@pytest.mark.parametrize(
    ("kwargs", "mode", "reason"),
    [
        pytest.param({"elevated": True}, "system", "auto: elevated", id="auto-elevated-selects-system"),
        pytest.param({"elevated": False}, "user", "auto: not elevated", id="auto-normal-selects-user"),
        pytest.param({"default_mode": "system"}, "system", "default_mode", id="default-mode-system-when-not-auto"),
        pytest.param({"default_mode": "user"}, "user", "default_mode", id="default-mode-user"),
        pytest.param({"container": "portable"}, "portable", "inside the portable root", id="root-beats-auto"),
        pytest.param({"container": "system", "default_mode": "user"}, "system", "inside the system root",
                     id="root-beats-default-mode"),
        pytest.param({"record": "portable", "container": "portable", "default_mode": "user"}, "portable",
                     "install record", id="record-beats-default-mode"),
        pytest.param({"explicit": "portable", "default_mode": "user"}, "portable", "--mode", id="flag-beats-default-mode"),
        pytest.param({"explicit": "user", "explicit_source": "selector"}, "user", "selector",
                     id="qualified-selector-is-a-flag"),
        pytest.param({"record": "user", "elevated": False}, "user", "install record", id="record-beats-auto-normal"),
    ],
)
def test_selection_follows_the_order_of_the_design(kwargs: dict, mode: str, reason: str) -> None:
    """The first of flag, install record, containing root and ``default_mode`` that decides names the mode and the reason.

    ``auto`` is used only when none of the earlier steps decides.
    """
    choice = select_mode(**kwargs)

    assert (choice.mode, choice.reason) == (mode, reason)
    assert str(choice) == f"{mode} ({reason})"


def test_auto_never_selects_portable_and_the_reason_is_printable() -> None:
    """Whatever the elevation, ``auto`` resolves to ``system`` or ``user`` and prints as ``system (auto: elevated)``."""
    chosen = {select_mode(elevated=elevated).mode for elevated in (True, False)}

    assert chosen == {"system", "user"}
    assert str(select_mode(elevated=True)) == "system (auto: elevated)"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"explicit": "user"}, id="explicit-user"),
        pytest.param({"explicit": "portable"}, id="explicit-portable"),
        pytest.param({"record": "user"}, id="user-install-record"),
        pytest.param({"container": "user"}, id="path-inside-a-user-root"),
        pytest.param({"container": "portable"}, id="path-inside-a-portable-root"),
        pytest.param({"default_mode": "user"}, id="default-mode-user"),
    ],
)
def test_an_elevated_run_never_acts_on_a_user_or_portable_root(kwargs: dict) -> None:
    """An elevated process refuses user and portable work however the mode was chosen, and says to use a normal shell (D31)."""
    with pytest.raises(ModeRefused, match="normal \\(non-elevated\\) shell"):
        select_mode(elevated=True, **kwargs)


def test_an_elevated_run_refuses_a_package_without_system_and_names_the_normal_shell() -> None:
    """With ``auto`` an elevated run selects system; a package that lacks it is refused and told to use a normal shell."""
    with pytest.raises(ModeRefused, match="does not support system mode") as raised:
        select_mode(elevated=True, supported=("user",))

    assert "normal (non-elevated) shell" in str(raised.value)


def test_an_install_record_from_an_untrusted_place_cannot_decide_an_elevated_run() -> None:
    """A record that an elevated run read from a user-writable folder is refused, not obeyed (fail closed)."""
    with pytest.raises(ModeRefused, match="does not trust it"):
        select_mode(elevated=True, record="system", record_trusted=False)


def test_a_trusted_system_record_decides_an_elevated_run() -> None:
    """A record read from an admin-only folder decides the mode of an elevated run as usual."""
    assert select_mode(elevated=True, record="system").reason == "install record"


@pytest.mark.parametrize(
    ("supported", "kwargs", "advice"),
    [
        pytest.param(("system",), {"elevated": False}, "supports system", id="normal-run-auto-user-unsupported"),
        pytest.param(("system", "user"), {"explicit": "portable"}, "supports system, user", id="portable-not-declared"),
        pytest.param(("portable",), {"explicit": "system"}, "supports portable", id="portable-only-package-in-system"),
    ],
)
def test_a_mode_the_package_does_not_support_is_refused_up_front(supported: tuple[str, ...], kwargs: dict, advice: str) -> None:
    """Selection fails before any work when the package's ``modes`` list lacks the chosen mode."""
    with pytest.raises(ModeRefused, match=advice):
        select_mode(supported=supported, **kwargs)


def test_a_supported_mode_is_accepted() -> None:
    """A mode inside the package's ``modes`` list is selected normally."""
    assert select_mode(explicit="portable", supported=("portable",)).mode == "portable"


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
    """A mode other than system, user and portable is refused with the list of modes."""
    with pytest.raises(ModeRefused, match="Unknown install mode 'machine'"):
        select_mode(explicit="machine")
