"""Cover that every TOML file gupkg writes survives hostile text and reads back unchanged.

Behavior covered: values with quotes, backslashes, newlines, tabs and non-ASCII text round-trip
through the machine-readable command output, the update state and receipt, and the manager
configuration, all of which are now written by the one TOML writer (`tomli_w`) instead of
hand-built strings.

Real: the public writers and readers on files under a temporary folder. Mocked: nothing. Out
of scope: the content rules of each file (covered by the command tests).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from gupkg.core import ActionResult, PackageIdentity
from gupkg.manager import ManagerConfig, load_manager_config, manager_config_text
from gupkg.outcome import Outcome, format_toml
from gupkg.updates import load_update_state, write_receipt, write_update_state

HOSTILE = 'C:\\opt\\"quoted" name\twith\ttabs\nand a line break — and ünïcode ☃'


def test_machine_output_round_trips_hostile_text_in_every_section() -> None:
    """`format_toml` output parses back to the same message text, section values and record lists."""
    outcome = Outcome(
        "info",
        ActionResult(ok=False, changed=True, errors=[HOSTILE], warnings=['a "b"'], exit_code=2),
        {
            "package": {"name": HOSTILE, "count": 3, "ready": True, "missing": None},
            "registry": {"source": "https://example.org/x.git"},
            "registry_packages": [{"name": HOSTILE}, {"name": "second"}],
            "targets": [{"id": "user:a", "aliases": ["x", HOSTILE]}],
            "self": {"version": "1.0"},
            "self_shims": [{"name": "gupkg"}],
        },
    )

    parsed = tomllib.loads(format_toml(outcome))

    assert parsed["output_schema"] == 1 and parsed["ok"] is False and parsed["exit_code"] == 2
    assert parsed["errors"] == [HOSTILE] and parsed["warnings"] == ['a "b"']
    assert parsed["package"] == {"name": HOSTILE, "count": 3, "ready": True}
    assert [row["name"] for row in parsed["registry"]["package"]] == [HOSTILE, "second"]
    assert parsed["registry"]["source"] == "https://example.org/x.git"
    assert parsed["target"][0]["aliases"] == ["x", HOSTILE]
    assert parsed["self"]["shim"] == [{"name": "gupkg"}]


def test_update_state_and_receipt_round_trip_hostile_text(tmp_path: Path) -> None:
    """The update state and a receipt keep text with quotes and backslashes intact."""
    state_file = tmp_path / "state" / "update.toml"
    write_update_state(state_file, {
        "lastStatus": "failed", "lastError": HOSTILE, "lastCandidateId": "abc",
        "assignedVersion": [{"candidateId": HOSTILE, "version": "1.0"}],
    })

    state = load_update_state(state_file)

    assert state["lastError"] == HOSTILE
    assert state["assignedVersion"] == [{"candidateId": HOSTILE, "version": "1.0"}]
    identity = PackageIdentity.from_version_path(tmp_path / "tool", tmp_path / "tool" / "1.2.3", is_current=False)
    receipt = tmp_path / "receipts" / "1.2.3.toml"
    write_receipt(receipt, {"candidateId": HOSTILE}, identity)
    parsed = tomllib.loads(receipt.read_text(encoding="utf-8"))
    assert parsed["candidateId"] == HOSTILE and parsed["version"] == "1.2.3"


def test_manager_configuration_round_trips_unusual_paths(tmp_path: Path) -> None:
    """A manager configuration written for roots and a registry source with a quote and non-ASCII text loads back equal."""
    config = ManagerConfig(
        tmp_path / "cfg" / "gupkg-config.toml", tmp_path / "sys ünï", tmp_path / "user'quote",
        system_bin=tmp_path / "sbin", user_bin=tmp_path / "ubin", registry_cache=tmp_path / "cache",
        registry_source="https://example.org/team-\"registry\".git", shim_linkage="static",
    )
    path = config.path
    path.parent.mkdir()
    path.write_text(manager_config_text(config), encoding="utf-8")

    loaded = load_manager_config(path)

    assert loaded.system_root == (tmp_path / "sys ünï").resolve()
    assert loaded.user_root == (tmp_path / "user'quote").resolve()
    assert loaded.registry_source == config.registry_source
    assert loaded.shim_linkage == "static"
