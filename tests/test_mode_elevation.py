"""Cover the up-front elevation step for system mode (design 9.6, D19, D25, threat model T1).

Behavior covered: a gupkg that runs from a user-writable place never relaunches elevated and says that system
installs need the system gupkg; an admin-only gupkg relaunches itself once for the whole call, after a
confirmation or immediately with ``--yes``; a declined or unanswerable confirmation (``--no-input``) and a
refused elevation prompt are exit code 5 with nothing started; user contexts, and an already
elevated process, need no relaunch.

Real: ``ensure_elevation`` and the scopes built by ``make_scope``. Fake: ``FakeSystem`` supplies the
elevation flag, the admin-only answer for gupkg's own folders and records the relaunch (UAC itself is manual:
see ``tests/manual_smoke.md``). Out of scope: what the elevated child does with its arguments
(``test_targets.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.scope import InputNeeded, make_scope
from gupkg.model.settings import default_layer, merge_layers
from gupkg.relaunch import ElevationRefused, ensure_elevation
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Mode
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

ENV = {"USERPROFILE": "C:\\Users\\pat", "APPDATA": "C:\\Users\\pat\\AppData\\Roaming", "ProgramData": "C:\\ProgramData"}
ARGV = ["install", "tool", "--relaunched"]


def context(mode: str):
    """Return the scope of *mode* built from the built-in settings."""
    return make_scope(Mode(mode), Path("C:\\opt"), "test", merge_layers([default_layer(mode)]), ENV)


def call(system: FakeSystem, contexts, tmp_path: Path, **options):
    """Run the elevation step for gupkg installed at ``tmp_path/opt/gupkg/1.0.0``."""
    return ensure_elevation(
        contexts, system=system, argv=ARGV, version_dir=tmp_path / "opt" / "gupkg" / "1.0.0", root=tmp_path / "opt", **options
    )


def test_an_admin_only_gupkg_relaunches_once_after_the_user_confirms(tmp_path: Path) -> None:
    """A system target from an admin-only gupkg asks, then starts one elevated run with the same arguments."""
    system = FakeSystem(tmp_path / "machine")
    questions: list[str] = []

    outcome = call(system, [context("system")], tmp_path, confirm=lambda question: questions.append(question) or True)

    assert (outcome.needed, outcome.relaunched) == (True, True)
    assert system.relaunches == [ARGV]
    assert len(questions) == 1


def test_yes_relaunches_without_asking(tmp_path: Path) -> None:
    """``--yes`` skips the question (D25) and still relaunches once."""
    system = FakeSystem(tmp_path / "machine")

    outcome = call(system, [context("system")], tmp_path, yes=True)

    assert outcome.relaunched and system.relaunches == [ARGV]


def test_several_system_targets_cause_one_relaunch(tmp_path: Path) -> None:
    """The whole CLI call is checked once: three system targets start one elevated run, not three."""
    system = FakeSystem(tmp_path / "machine")

    call(system, [context("system"), context("user"), context("system"), context("system")], tmp_path, yes=True)

    assert len(system.relaunches) == 1


@pytest.mark.parametrize("folder", ["version", "root"])
def test_a_gupkg_in_a_user_writable_place_never_elevates(tmp_path: Path, folder: str) -> None:
    """When gupkg's own version directory or root is user-writable, it refuses and says to use the system gupkg."""
    untrusted = tmp_path / "opt" / "gupkg" / "1.0.0" if folder == "version" else tmp_path / "opt"
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={untrusted})

    with pytest.raises(ElevationRefused, match="System installs need the system gupkg"):
        call(system, [context("system")], tmp_path, yes=True)

    assert system.relaunches == []


def test_the_untrusted_gupkg_refuses_before_it_asks_anything(tmp_path: Path) -> None:
    """A user-writable gupkg does not even offer the elevation question."""
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={tmp_path / "opt"})
    questions: list[str] = []

    with pytest.raises(ElevationRefused):
        call(system, [context("system")], tmp_path, confirm=lambda question: questions.append(question) or True)

    assert questions == []


def test_a_declined_confirmation_is_exit_5_and_starts_nothing(tmp_path: Path) -> None:
    """Answering no leaves everything unchanged with exit code 5."""
    system = FakeSystem(tmp_path / "machine")

    with pytest.raises(InputNeeded) as raised:
        call(system, [context("system")], tmp_path, confirm=lambda question: False)

    assert raised.value.exit_code == 5 and system.relaunches == []


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"no_input": True, "confirm": lambda question: True}, id="no-input-never-asks"),
        pytest.param({}, id="nobody-can-be-asked"),
    ],
)
def test_the_question_under_no_input_is_exit_5(tmp_path: Path, options: dict) -> None:
    """With ``--no-input`` the elevation question becomes exit code 5 and nothing is started."""
    system = FakeSystem(tmp_path / "machine")

    with pytest.raises(InputNeeded) as raised:
        call(system, [context("system")], tmp_path, **options)

    assert raised.value.exit_code == 5 and system.relaunches == []


def test_a_refused_elevation_prompt_is_exit_5(tmp_path: Path) -> None:
    """When UAC is not accepted the call ends with exit code 5."""
    system = FakeSystem(tmp_path / "machine", relaunch_result=False)

    with pytest.raises(InputNeeded) as raised:
        call(system, [context("system")], tmp_path, yes=True)

    assert raised.value.exit_code == 5


def test_user_targets_need_no_elevation(tmp_path: Path) -> None:
    """Only system mode elevates; user work never relaunches."""
    system = FakeSystem(tmp_path / "machine")

    outcome = call(system, [context("user")], tmp_path)

    assert (outcome.needed, outcome.relaunched) == (False, False) and system.relaunches == []


def test_an_already_elevated_process_does_not_relaunch(tmp_path: Path) -> None:
    """An elevated process proceeds with its own token and starts nothing."""
    system = FakeSystem(tmp_path / "machine", elevated=True)

    outcome = call(system, [context("system")], tmp_path)

    assert (outcome.needed, outcome.relaunched) == (True, False) and system.relaunches == []
