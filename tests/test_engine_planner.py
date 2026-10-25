"""Cover the planner: plans are data, built without touching the machine, with the phases and effects of design 13.1.

Behaviour covered: the phases of an install plan in order and which are skipped; the effects diff (add, update, keep, remove,
and the ``added`` fact of a kept ``PATH`` entry); ``sha256: present | missing`` and the source URL in the plan document;
that planning changes nothing (no file, no registry value, no request); that an install plan never contains a tracker
step; the check-updates plan; the cleanup rules (a shipped ``App`` is never dropped, an ``App`` of a version newer than
``current`` stays, ``--keep``); the uninstall plan and what ``--purge`` would delete.

Real: the planner, the catalog views and real folders. Mocked: ``FakeSystem``. Out of scope: executing a plan (the lifecycle tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.catalog import Catalog
from gupkg.engine import lifecycle
from gupkg.engine.planner import CleanupFacts, diff_effects, plan_cleanup
from gupkg.model.plan import INSTALL_PHASES, InstallPlan, PackageResult, Request
from gupkg.model.state import Effect, Root
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401
from tests.mode_support import tree_snapshot

TABLES = (
    '[[shortcut]]\nname = "Tool"\ntarget = "$App\\\\app.exe"\n\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n\n'
    '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'
)
ENV = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root."""
    return World(tmp_path)


def planned(world: World, operation: str, selector: str, **options) -> InstallPlan:
    """Return the plan of one request (it must be a plan, not a failure)."""
    found = lifecycle.plan(world.engine, [Request(operation, selector, **options)])[0]
    assert isinstance(found, InstallPlan), found
    return found


def test_an_install_plan_has_the_phases_of_design_13_1_in_order(world: World) -> None:
    """Resolve, Fetch, Verify, Unpack, Prepare, Activate, Integrate, Finish: each with its readable steps."""
    world.publish("tool", "1.0.0", tables=TABLES)

    plan = planned(world, "install", "tool")

    assert [phase.name for phase in plan.phases] == list(INSTALL_PHASES)
    assert not any(phase.skipped for phase in plan.phases)
    assert plan.populate.needed and plan.populate.kind == "download"
    assert [item.action for item in plan.effects] == ["add", "add", "add", "add"] and sorted(item.effect.kind for item in plan.effects) == [
        "env", "path", "shim", "shortcut"]
    assert any("switch current to 1.0.0" in step.text for step in plan.phases[5].steps)


def test_planning_changes_nothing_and_makes_no_request(world: World) -> None:
    """A plan reads the disk but writes no file, sets no registry value and fetches nothing."""
    world.publish("tool", "1.0.0", tables=TABLES)
    world.tracker("tool")
    before = tree_snapshot(world.user)
    world.fake.fetch_log.clear()
    world.fake.registry_log.clear()

    for operation in ("install", "check-updates"):
        planned(world, operation, "tool")

    assert tree_snapshot(world.user) == before and world.fake.fetch_log == [] and world.fake.registry_log == []
    assert world.fake.shortcuts() == {} and world.fake.broadcasts == 0


def test_the_plan_document_marks_the_checksum_and_names_the_url(world: World) -> None:
    """`--plan --json` data: sha256 present or missing, the source URL, the mode, the elevation need and the effects."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.publish("other", "1.0.0", tables=ENV, hashed=False)

    present = planned(world, "install", "tool").to_data()
    missing = planned(world, "install", "other").to_data()

    assert present["sha256"] == "present" and present["url"].endswith("tool-1.0.0.zip") and present["mode"] == "user"
    assert present["requires_elevation"] is False and present["effects"] == [
        {"action": "add", "kind": "env", "scope": "user", "name": "TOOL_HOME",
         "value": str(world.user / "tool" / "current" / "App")}]
    assert missing["sha256"] == "missing" and missing["populate"]["checksum"]["skippable"] is True


def test_a_plan_for_a_system_package_requires_elevation(world: World) -> None:
    """The plan of a system-mode target says that elevation is required."""
    world.publish("tool", "1.0.0", mode="system", tables=ENV)

    assert planned(world, "install", "system:tool").requires_elevation is True


def test_installing_an_installed_populated_package_skips_fetch_and_activate(world: World) -> None:
    """A repair of a healthy package skips the download phases and the switch, and changes no effect."""
    world.publish("tool", "1.0.0", tables=TABLES)
    world.one("install", "tool")

    plan = planned(world, "install", "tool")

    skipped = {phase.name for phase in plan.phases if phase.skipped}
    assert skipped == {"Fetch", "Verify", "Unpack", "Prepare", "Activate", "Integrate"}
    assert plan.changes == () and not plan.populate.needed


def test_an_install_plan_never_contains_a_tracker_step(world: World) -> None:
    """No phase and no step of an install or update plan runs a tracker (decision D11)."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.tracker("tool")
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)

    for plan in (planned(world, "install", "tool"), planned(world, "update", "tool")):
        text = " ".join(step.text.casefold() for phase in plan.phases for step in phase.steps)
        assert "Track" not in [phase.name for phase in plan.phases] and "tracker" not in text and "check-updates" not in text


def test_the_update_plan_removes_what_the_new_version_drops(world: World) -> None:
    """Updating to a version without the shortcut and the shim plans their removal as stale."""
    world.publish("tool", "1.0.0", tables=TABLES)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)

    plan = planned(world, "update", "tool")

    # The shared bin entry of PATH is forgotten by the record, but the machine keeps it (it belongs to the mode).
    assert sorted(item.effect.kind for item in plan.effects if item.action == "remove") == ["path", "shim", "shortcut"]
    assert any("stale, removed" in step.text for step in plan.phases[6].steps)


def test_the_check_updates_plan_activates_and_integrates_nothing(world: World) -> None:
    """The check-updates plan has Track and Write phases, populates, and has no Activate or Integrate phase."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.tracker("tool")
    world.one("install", "tool")

    plan = planned(world, "check-updates", "tool")
    skipped = planned(world, "check-updates", "tool", no_download=True)

    names = [phase.name for phase in plan.phases]
    assert names == ["Resolve", "Track", "Write", "Fetch", "Verify", "Unpack", "Prepare", "Finish"]
    assert not any(phase.skipped for phase in plan.phases)
    assert {phase.name for phase in skipped.phases if phase.skipped} == {"Fetch", "Verify", "Unpack", "Prepare"}


def test_the_uninstall_plan_lists_every_recorded_effect_and_what_purge_destroys(world: World) -> None:
    """Uninstall removes each recorded effect; --purge names the package root, data and the Apps that cannot be rebuilt."""
    world.publish_shipped("tool", "1.0.0", tables=TABLES)
    world.one("install", "tool")

    plain = planned(world, "uninstall", "tool")
    purge = planned(world, "uninstall", "tool", purge=True)

    assert {item.action for item in plain.effects} == {"remove"} and len(plain.effects) == 4 and "purge" not in plain.extra
    assert purge.extra["purge"]["unrecoverable_apps"] == ["1.0.0"] and purge.purge


def test_the_cleanup_plan_keeps_shipped_apps_updates_that_are_ready_and_the_newest_previous(world: World) -> None:
    """Shipped, newer-than-current and the newest `--keep` older Apps stay; the others are dropped."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    for version in ("1.1.0", "1.2.0", "1.3.0"):
        world.publish("tool", version, tables=ENV)
        world.one("update", "tool")
    world.publish("tool", "2.0.0", tables=ENV)
    world.one("update", "tool", download_only=True)
    world.publish_shipped("tool", "0.9.0", tables=ENV)

    plan = planned(world, "cleanup", "tool").extra["cleanup"]

    kept = {item["version"]: item["why"] for item in plan["kept_apps"]}
    assert sorted(plan["drop_apps"]) == ["1.0.0", "1.1.0"]
    assert kept["1.3.0"] == "current" and kept["2.0.0"].startswith("newer than current") and "shipped" in kept["0.9.0"]
    assert "newest older" in kept["1.2.0"]


def test_a_cleanup_with_keep_zero_drops_every_recoverable_old_app(world: World) -> None:
    """--keep 0 drops all non-current recoverable Apps (and still keeps the current one)."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)
    world.one("update", "tool")

    plan = planned(world, "cleanup", "tool", keep=0).extra["cleanup"]

    assert plan["drop_apps"] == ["1.0.0"]


def test_the_cleanup_plan_is_made_from_the_facts_it_is_given_and_never_looks_at_the_disk(world: World) -> None:
    """Which versions hold a bundled gupkg is data (CleanupFacts.bundled): the planner names exactly those, whatever the disk says."""
    world.publish("tool", "1.0.0", tables=ENV)
    world.one("install", "tool")
    view = Catalog.from_settings(
        world.engine.load().settings, world.fake, world.machine.env, elevated=False
    ).describe("tool", Root(world.user, "user"))

    named = plan_cleanup(Request("cleanup", "tool", bundled=True), view, CleanupFacts(bundled=("1.0.0",)))
    none = plan_cleanup(Request("cleanup", "tool", bundled=True), view, CleanupFacts())

    assert not (world.user / "tool" / "1.0.0" / "gupkg").exists()
    assert named.drop_bundled == ("1.0.0",) and none.drop_bundled == ()


def test_a_plan_that_cannot_be_made_is_a_failure_result(world: World) -> None:
    """A target that is not installed cannot be planned for update: the answer is a result with the error, not an exception."""
    world.publish("tool", "1.0.0", tables=ENV)

    found = lifecycle.plan(world.engine, [Request("update", "tool")])[0]

    assert isinstance(found, PackageResult) and not found.ok and found.error["code"] == "not-installed"


# ---------------------------------------------------------------------------
# The effect diff
# ---------------------------------------------------------------------------


def shim(name: str, target: str) -> Effect:
    """Return a shim effect."""
    return Effect("shim", {"path": f"C:\\bin\\{name}.exe", "target": target})


def test_diff_effects_adds_updates_keeps_and_removes_with_removals_last() -> None:
    """Same identity and same fields is kept, other fields is an update, new is an add, recorded but unwanted is a remove."""
    recorded = [shim("a", "x"), shim("b", "x"), shim("gone", "x")]
    desired = [shim("a", "x"), shim("b", "y"), shim("new", "x")]

    result = diff_effects(recorded, desired)

    assert [(item.action, item.effect.fields["path"].rsplit("\\", 1)[-1]) for item in result] == [
        ("keep", "a.exe"), ("update", "b.exe"), ("add", "new.exe"), ("remove", "gone.exe")]
    assert result[1].previous.fields["target"] == "x"


def test_diff_effects_keeps_the_recorded_added_fact_of_a_path_entry() -> None:
    """A PATH entry that an earlier install added stays marked as added when nothing else changed."""
    recorded = [Effect("path", {"scope": "user", "entry": "C:\\Tool", "added": True})]
    desired = [Effect("path", {"scope": "user", "entry": "c:\\tool\\", "added": False})]

    [item] = diff_effects(recorded, desired)

    assert item.action == "keep" and item.effect.fields["added"] is True


@pytest.mark.parametrize("code", [True, False, 2.0, "3", None])
def test_an_exit_code_that_is_not_an_integer_never_passes_through_as_one(code: object) -> None:
    """A bool, float, text or missing child exit code is exit code 4, not 0 to 6 (`True == 1` would pass as a usage error)."""
    from gupkg.model.plan import EXIT_INTERNAL, clamp_child_exit_code

    assert clamp_child_exit_code(code) == EXIT_INTERNAL  # type: ignore[arg-type]
