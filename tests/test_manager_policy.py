"""Cover the manager's missing-checksum policy: reading the setting, deciding each target, relaying the flag.

Behaviour covered: `[manager] checksum_missing` read with its default, ignored when it comes from a
user layer in an elevated run, and fail-closed for any odd value; the matrix of `ask`, `refuse` and
`skip` against interactive and non-interactive runs and against hash present, missing over https and
missing over http; one question per package and a *Cancel* that lets the batch continue; the exact
`--skip-checksum=<reason>` argument; and that the argument survives a real subprocess boundary and is
read back by the CLI's own flag parser.

Real: the pure ``gupkg.model.manager_policy``, the settings merge, and a child process (a stand-in
`gupkg` script that records its argv). Mocked: the manager program itself does not exist yet (issue 030),
so these tests drive the policy functions it will call. Out of scope: the UI and the real CLI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gupkg.model.checksum import SKIPPED_CLI, SKIPPED_SETTING, USER_APPROVED, ChecksumPlan, parse_skip_flag, plan_checksum
from gupkg.model.manager_policy import (
    REFUSED_INVALID_CHECKSUM,
    REFUSED_NO_CHECKSUM,
    command_line,
    decide_target,
    decide_targets,
    manager_checksum_policy,
    skip_argument,
)
from gupkg.model.settings import (
    LAYER_APP,
    LAYER_CLI,
    LAYER_ROOT,
    Layer,
    default_layer,
    merge_layers,
)

HTTPS = "https://example.org/tool.zip"
PRESENT = plan_checksum(HTTPS, "ab" * 32, url_from="pkg.toml")
MISSING_HTTPS = plan_checksum(HTTPS, None, url_from="tracker")
MISSING_HTTP = plan_checksum("http://example.org/tool.zip", None, url_from="tracker")
INVALID = plan_checksum(HTTPS, "not-a-digest", url_from="pkg.toml")


def settings_with(layer: str | None, value: object = None):
    """Return merged settings whose `manager.checksum_missing` is *value* from *layer* (defaults when None)."""
    layers = [default_layer("user")]
    if layer is not None:
        layers.append(Layer(layer, {"schema": 4, "manager": {"checksum_missing": value}}))
    return merge_layers(layers)


def test_the_default_policy_is_ask() -> None:
    """Without any setting the manager asks."""
    assert manager_checksum_policy(settings_with(None)) == "ask"


@pytest.mark.parametrize("layer", [LAYER_APP, LAYER_ROOT, LAYER_CLI])
@pytest.mark.parametrize("value", ["ask", "refuse", "skip"])
def test_the_setting_is_honoured_from_every_layer(layer: str, value: str) -> None:
    """The three values are read from every layer of the run's one scope."""
    assert manager_checksum_policy(settings_with(layer, value)) == value


def test_an_unknown_policy_value_is_a_refusal_for_a_target() -> None:
    """A policy the manager does not know never lets a target skip."""
    decision = decide_target(MISSING_HTTPS, "always", interactive=True, ask=lambda plan: True)

    assert decision.run is False and decision.message.startswith(REFUSED_NO_CHECKSUM)


MATRIX = [
    pytest.param("skip", True, None, PRESENT, True, (), id="skip-interactive-hash-present"),
    pytest.param("skip", True, None, MISSING_HTTPS, True, ("--skip-checksum=setting",), id="skip-interactive-missing-https"),
    pytest.param("skip", False, None, MISSING_HTTPS, True, ("--skip-checksum=setting",), id="skip-non-interactive-missing-https"),
    pytest.param("skip", True, None, MISSING_HTTP, False, (), id="skip-missing-http-is-refused"),
    pytest.param("refuse", True, True, MISSING_HTTPS, False, (), id="refuse-interactive-never-asks"),
    pytest.param("refuse", False, None, MISSING_HTTPS, False, (), id="refuse-non-interactive"),
    pytest.param("refuse", True, None, PRESENT, True, (), id="refuse-hash-present-runs"),
    pytest.param("ask", True, True, MISSING_HTTPS, True, ("--skip-checksum=user-approved",), id="ask-interactive-yes"),
    pytest.param("ask", True, False, MISSING_HTTPS, False, (), id="ask-interactive-cancel"),
    pytest.param("ask", False, True, MISSING_HTTPS, False, (), id="ask-non-interactive-behaves-as-refuse"),
    pytest.param("ask", True, True, MISSING_HTTP, False, (), id="ask-missing-http-is-not-even-asked"),
    pytest.param("ask", True, None, PRESENT, True, (), id="ask-hash-present-is-not-asked"),
]


@pytest.mark.parametrize(("policy", "interactive", "answer", "plan", "runs", "arguments"), MATRIX)
def test_the_manager_policy_matrix(
    policy: str, interactive: bool, answer: bool | None, plan: ChecksumPlan, runs: bool, arguments: tuple[str, ...]
) -> None:
    """Each policy, with and without someone to answer, runs the target with the right relayed flag or not at all."""
    asked: list[ChecksumPlan] = []

    def ask(entry: ChecksumPlan) -> bool:
        """Record the question and give the scripted answer."""
        asked.append(entry)
        return bool(answer)

    decision = decide_target(plan, policy, interactive=interactive, ask=ask)

    assert decision.run is runs and decision.arguments == arguments
    expect_question = policy == "ask" and interactive and plan is MISSING_HTTPS
    assert len(asked) == (1 if expect_question else 0)
    cancelled = not runs and expect_question
    if cancelled:
        assert decision.status == "cancelled"
    elif not runs:
        assert decision.status == "refused" and decision.message.startswith(REFUSED_NO_CHECKSUM)


@pytest.mark.parametrize("policy", ["skip", "ask", "refuse"])
@pytest.mark.parametrize("interactive", [True, False], ids=["interactive", "non-interactive"])
def test_an_invalid_checksum_is_never_asked_about_never_skipped_and_has_its_own_message(policy: str, interactive: bool) -> None:
    """A malformed declared hash is refused under every policy, with no question and no relayed flag."""
    asked: list[ChecksumPlan] = []

    decision = decide_target(INVALID, policy, interactive=interactive, ask=lambda plan: asked.append(plan) or True)

    assert INVALID.sha256 == "invalid" and INVALID.skippable is False
    assert decision.run is False and decision.arguments == () and asked == []
    assert decision.message == REFUSED_INVALID_CHECKSUM


def test_a_plan_entry_that_is_neither_present_nor_missing_is_refused() -> None:
    """An unknown `sha256` status fails closed."""
    odd = ChecksumPlan("unknown", HTTPS, "tracker", True)

    assert decide_target(odd, "skip", interactive=True).run is False


def test_a_batch_asks_once_per_package_and_cancel_lets_the_next_package_run() -> None:
    """Three targets: a hash present, one the user cancels, one the user approves; each missing package is asked once."""
    plans = {"present": PRESENT, "cancelled": MISSING_HTTPS, "approved": plan_checksum(HTTPS + "2", None, url_from="registry main")}
    questions: list[str] = []

    def ask(name: str, plan: ChecksumPlan) -> bool:
        """Answer yes only for the package named approved."""
        questions.append(name)
        return name == "approved"

    decisions = decide_targets(plans, "ask", interactive=True, ask=ask)

    assert questions == ["cancelled", "approved"]
    assert [(name, d.run, d.status) for name, d in decisions.items()] == [
        ("present", True, "run"), ("cancelled", False, "cancelled"), ("approved", True, "run"),
    ]
    assert decisions["approved"].arguments == ("--skip-checksum=user-approved",)


def test_a_yes_applies_to_one_package_only_and_is_not_remembered() -> None:
    """The next package is asked again; nothing carries over."""
    plans = {"a": MISSING_HTTPS, "b": plan_checksum(HTTPS + "b", None, url_from="tracker")}
    answers = iter([True, False])

    decisions = decide_targets(plans, "ask", interactive=True, ask=lambda name, plan: next(answers))

    assert decisions["a"].run is True and decisions["b"].run is False


def test_a_yes_answer_must_be_exactly_true() -> None:
    """A truthy non-boolean from a faulty prompt is not a yes."""
    assert decide_target(MISSING_HTTPS, "ask", interactive=True, ask=lambda plan: "yes").run is False  # type: ignore[arg-type,return-value]


def test_without_an_ask_function_ask_is_refuse() -> None:
    """Interactive but with nobody wired to answer is still a refusal."""
    assert decide_target(MISSING_HTTPS, "ask", interactive=True).run is False


@pytest.mark.parametrize(
    ("decision", "argument"),
    [(SKIPPED_SETTING, "--skip-checksum=setting"), (USER_APPROVED, "--skip-checksum=user-approved")],
)
def test_the_relayed_argument_names_the_reason(decision: str, argument: str) -> None:
    """The manager passes its decision as `--skip-checksum=<reason>`."""
    assert skip_argument(decision) == argument


def test_the_manager_cannot_relay_a_verified_decision() -> None:
    """Only `skipped-setting` and `user-approved` can be relayed (the bare flag is the user's own)."""
    for decision in ("verified", SKIPPED_CLI, "origin-not-verified"):
        with pytest.raises(ValueError):
            skip_argument(decision)


def test_a_target_that_is_not_run_has_no_command_line() -> None:
    """Asking for the command line of a refused target is a programming error."""
    with pytest.raises(ValueError):
        command_line(["gupkg", "update", "x"], decide_target(MISSING_HTTP, "skip", interactive=False))


FAKE_GUPKG = """\
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as record:
    record.write(json.dumps(sys.argv[2:]) + "\\n")
"""


def run_fake_gupkg(tmp_path: Path, argv: list[str]) -> list[str]:
    """Run the stand-in gupkg as a real child process and return the argv it recorded."""
    script = tmp_path / "fake_gupkg.py"
    script.write_text(FAKE_GUPKG, encoding="utf-8")
    log = tmp_path / "argv.jsonl"
    subprocess.run([sys.executable, "-I", str(script), str(log), *argv], check=True, timeout=60)
    return json.loads(log.read_text(encoding="utf-8").splitlines()[-1])


@pytest.mark.parametrize(
    ("policy", "interactive", "answer", "reason"),
    [
        pytest.param("skip", False, None, SKIPPED_SETTING, id="skip"),
        pytest.param("ask", True, True, USER_APPROVED, id="ask-yes"),
    ],
)
def test_the_relayed_flag_crosses_a_real_process_boundary_and_the_cli_parser_reads_it_back(
    tmp_path: Path, policy: str, interactive: bool, answer: bool | None, reason: str
) -> None:
    """The recorded argv holds one `--skip-checksum=<reason>` argument and the CLI's parser maps it to the same decision."""
    decision = decide_target(MISSING_HTTPS, policy, interactive=interactive, ask=lambda plan: bool(answer))
    argv = command_line(["update", "tool", "--yes"], decision)

    received = run_fake_gupkg(tmp_path, argv)

    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-checksum", nargs="?", const="cli", default=None)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("rest", nargs="*")
    parsed = parser.parse_args(received)
    assert received[-1].count("--skip-checksum") == 1 and parse_skip_flag(parsed.skip_checksum) == reason


def test_a_refused_target_starts_no_process(tmp_path: Path) -> None:
    """With `refuse`, nothing is run, so the stand-in is never asked to record an argv."""
    log = tmp_path / "argv.jsonl"
    decision = decide_target(MISSING_HTTPS, "refuse", interactive=True, ask=lambda plan: True)

    if decision.run:
        run_fake_gupkg(tmp_path, command_line(["update", "tool"], decision))

    assert decision.run is False and not log.exists()
