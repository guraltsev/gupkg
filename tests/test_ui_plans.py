"""Cover how the UI programs read plans and apply the ``[manager] checksum_missing`` policy to each planned target.

Behaviour covered: the policy matrix of design 9.1 (ask, refuse, skip, each interactive and not, for a missing hash, a malformed hash and a plain ``http://`` URL), asked at most
once per package and never for a package that needs no question; a malformed declared hash (the ``invalid-checksum`` error of the plan) is never asked and never relayed; a
non-https URL is never skippable; the relayed argument is ``--skip-checksum=setting`` for the setting and ``=user-approved`` for a yes; plan entries that cannot be understood are failed
targets, never ready ones; the package list reader drops rows it cannot act on.

Real: the plan reader and ``gupkg.model.manager_policy`` (which this module feeds). Mocked: the question (a function that records that it was asked).
Out of scope: calling the CLI (``test_ui_flows.py``).
"""

from __future__ import annotations

import asyncio

import pytest

from gupkg_ui.plans import PlannedTarget, decide_targets, read_packages, read_plan
from tests.ui_support import entry, head, list_doc, plan_doc, plan_entry, plan_failure


def decide(items: list[PlannedTarget], policy: str, *, interactive: bool, answer: bool = True):
    """Decide *items* under *policy*; return the decisions and the list of targets that were asked about."""
    asked: list[str] = []

    def ask(item: PlannedTarget) -> bool:
        """Record the question and answer it."""
        asked.append(item.target)
        return answer

    return asyncio.run(decide_targets(items, lambda _item: policy, interactive=interactive, ask=ask)), asked


def planned(*entries: dict) -> list[PlannedTarget]:
    """Read *entries* as a plan document for their own targets."""
    return read_plan(plan_doc(list(entries)), [item["target"] for item in entries])


@pytest.mark.parametrize(
    ("policy", "interactive", "answer", "runs", "argument", "asked"),
    [
        pytest.param("ask", True, True, True, "--skip-checksum=user-approved", True, id="ask-interactive-yes"),
        pytest.param("ask", True, False, False, None, True, id="ask-interactive-no"),
        pytest.param("ask", False, True, False, None, False, id="ask-without-anyone-is-refuse"),
        pytest.param("refuse", True, True, False, None, False, id="refuse-never-asks"),
        pytest.param("refuse", False, True, False, None, False, id="refuse-schedule"),
        pytest.param("skip", True, True, True, "--skip-checksum=setting", False, id="skip-interactive"),
        pytest.param("skip", False, True, True, "--skip-checksum=setting", False, id="skip-schedule"),
    ],
)
def test_a_missing_https_checksum_follows_the_policy_table(policy, interactive, answer, runs, argument, asked) -> None:
    """Design 9.1: ``ask`` asks once and a yes relays ``user-approved``; ``skip`` relays ``setting``; ``refuse`` and a nobody-to-ask ``ask`` do not run the target."""
    items = planned(plan_entry("p", "p", sha256="missing"))

    decisions, questions = decide(items, policy, interactive=interactive, answer=answer)

    decision = decisions["p"]
    assert decision.run is runs
    assert (decision.arguments == (argument,)) if argument else decision.arguments == ()
    assert bool(questions) is asked and len(questions) <= 1
    if not runs and not (policy == "ask" and interactive):
        assert "no checksum" in decision.message


@pytest.mark.parametrize("policy", ["ask", "refuse", "skip"])
@pytest.mark.parametrize("interactive", [True, False], ids=["interactive", "schedule"])
def test_a_plain_http_download_without_a_hash_is_never_skippable(policy, interactive) -> None:
    """An ``http://`` artifact with no hash is refused under every policy, never asked about and never relayed a skip flag."""
    items = planned(plan_entry("p", "p", sha256="missing", url="http://example.org/x.zip"))

    decisions, questions = decide(items, policy, interactive=interactive)

    assert not decisions["p"].run and decisions["p"].arguments == () and questions == []
    assert "not https" in decisions["p"].message


@pytest.mark.parametrize("policy", ["ask", "refuse", "skip"])
@pytest.mark.parametrize("interactive", [True, False], ids=["interactive", "schedule"])
def test_a_malformed_declared_hash_is_never_asked_or_skipped(policy, interactive) -> None:
    """The plan's ``invalid-checksum`` failure becomes an invalid checksum plan: it is refused under every policy, with no question and no skip flag."""
    items = planned(plan_failure("p", "invalid-checksum", "sha256 is malformed", package="p"))

    decisions, questions = decide(items, policy, interactive=interactive)

    assert not decisions["p"].run and decisions["p"].arguments == () and questions == []
    assert "malformed" in decisions["p"].message


def test_a_present_checksum_and_a_download_free_plan_run_without_a_question() -> None:
    """A verified download and a plan that downloads nothing need no decision; an up-to-date target gets none at all."""
    items = planned(
        plan_entry("a", "a", sha256="present"), plan_entry("b", "b"),
        {"target": "c", "ok": True, "exit_code": 0, "result": entry("c", status="up-to-date")},
    )

    decisions, questions = decide(items, "ask", interactive=True)

    assert decisions["a"].run and decisions["b"].run and "c" not in decisions and questions == []


def test_each_package_is_asked_once_and_an_answer_does_not_leak_to_the_next() -> None:
    """Two packages without a hash are two questions; a yes for one and a no for the other are applied to the right package."""
    items = planned(plan_entry("a", "a", sha256="missing"), plan_entry("b", "b", sha256="missing"))
    answers = iter([True, False])
    asked: list[str] = []

    def ask(item: PlannedTarget) -> bool:
        """Answer yes for the first package and no for the second."""
        asked.append(item.target)
        return next(answers)

    decisions = asyncio.run(decide_targets(items, lambda _item: "ask", interactive=True, ask=ask))

    assert asked == ["a", "b"] and decisions["a"].run and not decisions["b"].run
    assert decisions["b"].status == "cancelled"


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"sha256": "maybe", "url": "https://x", "url_from": "pkg.toml", "skippable": True}, id="unknown-marker"),
        pytest.param({"sha256": "missing", "url": "https://x", "url_from": "pkg.toml", "skippable": "yes"}, id="skippable-not-boolean"),
        pytest.param("missing", id="not-an-object"),
    ],
)
def test_a_plan_entry_that_cannot_be_understood_is_a_failed_target(bad) -> None:
    """An unreadable checksum marker never means 'present': the target is failed and never decided to run."""
    good = plan_entry("p", "p", sha256="missing")
    good["plan"]["populate"]["checksum"] = bad

    (item,) = read_plan(plan_doc([good]), ["p"])

    assert item.state == "failed" and item.code == "unreadable-plan"
    assert asyncio.run(decide_targets([item], lambda _x: "skip", interactive=False, ask=None)) == {}


def test_a_target_the_plan_does_not_answer_for_has_failed() -> None:
    """A document without an entry for a target must not be read as 'nothing to do': the target is failed with exit code 4."""
    (item,) = read_plan(plan_doc([]), ["ghost"])

    assert item.state == "failed" and item.exit_code == 4 and item.code == "no-plan"


def test_other_plan_failures_keep_their_code_and_message() -> None:
    """A failed plan (not installed, say) is shown with the CLI's code and words and is never run."""
    (item,) = read_plan(plan_doc([plan_failure("p", "not-installed", "'p' is not installed", exit_code=2)]), ["p"])

    assert (item.state, item.code, item.message, item.exit_code) == ("failed", "not-installed", "'p' is not installed", 2)


def test_the_package_list_reader_drops_rows_it_cannot_act_on() -> None:
    """A row without a text name, mode and status is left out; the others keep their facts and qualified target."""
    document = list_doc([("alpha", "user", "outdated", "1.0", "2.0", "C:\\opt\\alpha")])
    document["packages"] += [{"name": 3, "mode": "user", "status": "x"}, "junk", {"name": "m", "mode": "", "status": "x"}]

    (row,) = read_packages(document)

    assert (row.target, row.status, row.newest_known) == ("alpha", "outdated", "2.0")
    assert read_packages(head("list")) == []
