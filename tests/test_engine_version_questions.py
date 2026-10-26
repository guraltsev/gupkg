"""Cover the two questions of ``install`` and ``update``: an older version than the active one, and other broken or unsupported versions.

Behavior covered: installing a version older than the active one asks "install it anyway?" and goes on only after a yes, with
``--yes`` or with ``--allow-downgrade``; other version directories that are broken or do not support the mode ask the same way
(``--yes`` or ``--allow-other-broken-versions`` answer); a missing answer (``--no-input``, or nobody to ask) is exit code 5 and
changes nothing; ``switch``, a repair of the active version and a package whose other versions are all valid ask nothing.

Real: the engine, the catalog and real folders. Mocked: ``FakeSystem`` and the answer (a function that records the questions).
Out of scope: how the command line reads the answer (``tests/test_cli_machine_interface.py``).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.engine_support import World


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root and an engine."""
    return World(tmp_path)


def answering(world: World, answer: bool, **flags) -> list[str]:
    """Give the world's engine a ``confirm`` that answers *answer* and records the questions; extra *flags* set Engine fields."""
    asked: list[str] = []

    def confirm(question: str) -> bool:
        """Record the question and answer it."""
        asked.append(question)
        return answer

    world.engine = replace(world.engine, confirm=confirm, **flags)
    return asked


def two_versions(world: World) -> None:
    """Install 2.0.0 of a package that also has 1.0.0 on disk."""
    world.publish("tool", "1.0.0")
    world.publish("tool", "2.0.0")
    assert world.one("install", "tool@2.0.0").ok


def break_version(world: World, version: str) -> None:
    """Make the version directory *version* of ``tool`` invalid."""
    (world.user / "tool" / version / "pkg.toml").write_text("this is [not valid", encoding="utf-8")


def test_installing_an_older_version_asks_and_goes_on_after_a_yes(world: World) -> None:
    """`install tool@1.0.0` while 2.0.0 is active asks first, and a yes makes 1.0.0 the active version."""
    two_versions(world)
    asked = answering(world, True)

    result = world.one("install", "tool@1.0.0")

    assert result.ok and result.version == "1.0.0"
    assert asked == ["tool 1.0.0 is older than the active version 2.0.0. Install it anyway?"]


def test_a_no_leaves_the_active_version_alone(world: World) -> None:
    """A no to the downgrade question is exit code 5 and 2.0.0 stays active."""
    two_versions(world)
    answering(world, False)

    result = world.one("install", "tool@1.0.0")

    assert not result.ok and result.exit_code == 5
    assert (world.user / "tool" / "current").resolve().name == "2.0.0"


@pytest.mark.parametrize("flags", [{"yes": True}, {"allow_downgrade": True}], ids=["yes", "allow-downgrade"])
def test_yes_and_allow_downgrade_answer_the_downgrade_question(world: World, flags: dict[str, bool]) -> None:
    """Either option installs the older version without asking."""
    two_versions(world)
    asked = answering(world, False, **flags)

    result = world.one("install", "tool@1.0.0")

    assert result.ok and result.version == "1.0.0" and asked == []


def test_no_input_turns_the_downgrade_question_into_a_refusal_that_names_the_option(world: World) -> None:
    """With nobody to ask, the refusal says which options would answer it, and nothing changes."""
    two_versions(world)
    world.engine = replace(world.engine, no_input=True)

    result = world.one("install", "tool@1.0.0")

    assert result.exit_code == 5 and "--allow-downgrade" in result.error["message"]
    assert (world.user / "tool" / "current").resolve().name == "2.0.0"


def test_switch_and_a_repair_ask_nothing(world: World) -> None:
    """`switch` is the explicit way back, and installing the active version again is a repair: neither asks."""
    two_versions(world)
    asked = answering(world, False)

    assert world.one("install", "tool").status == "repaired"
    switched = world.run("switch", "tool", version="1.0.0")

    assert switched.results[0].ok and asked == []


def test_other_broken_versions_ask_before_an_install(world: World) -> None:
    """A broken version directory beside the chosen one is named in a question; a yes goes on and installs the valid one."""
    world.publish("tool", "1.0.0")
    world.publish("tool", "2.0.0")
    break_version(world, "2.0.0")
    asked = answering(world, True)

    result = world.one("install", "tool")

    assert result.ok and result.version == "1.0.0"
    assert len(asked) == 1 and "2.0.0" in asked[0] and "broken or not supported" in asked[0] and asked[0].endswith("Install 1.0.0 anyway?")


@pytest.mark.parametrize("flags", [{"yes": True}, {"allow_other_broken_versions": True}], ids=["yes", "allow-other-broken-versions"])
def test_yes_and_the_option_answer_the_broken_versions_question(world: World, flags: dict[str, bool]) -> None:
    """Either option goes on without asking, and no-input refusal names the option when neither is given."""
    world.publish("tool", "1.0.0")
    world.publish("tool", "2.0.0")
    break_version(world, "2.0.0")
    asked = answering(world, False, **flags)

    assert world.one("install", "tool").ok and asked == []


def test_a_package_that_does_not_support_the_mode_in_another_version_counts_as_unsupported(world: World) -> None:
    """A valid version directory whose `modes` leaves out this mode is listed among the versions that are not supported."""
    world.publish("tool", "1.0.0")
    world.publish("tool", "2.0.0", top='modes = ["portable"]\n')
    asked = answering(world, True)

    result = world.one("install", "tool@1.0.0")

    assert result.ok
    assert len(asked) == 1 and "2.0.0 (does not support user mode)" in asked[0]


def test_valid_versions_ask_nothing(world: World) -> None:
    """With every other version valid and none older than the active one, install asks nothing."""
    world.publish("tool", "1.0.0")
    world.publish("tool", "2.0.0")
    asked = answering(world, False)

    assert world.one("install", "tool").ok and asked == []
