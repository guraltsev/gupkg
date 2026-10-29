"""Cover that every file gupkg writes survives hostile text and reads back unchanged.

Behavior covered: values with quotes, backslashes, newlines, tabs and non-ASCII text round-trip through the receipt, the install
record, the last-check state, the settings file written by ``settings set`` and ``registry add``, and the JSON documents of the
machine interface. The TOML files are all written by the one TOML writer (`tomli_w`) instead of hand-built strings.

Real: the public writers and readers on files under a temporary folder. Mocked: nothing. Out of scope: the content rules of each
file (covered by the command tests).
"""

from __future__ import annotations

import json
from pathlib import Path

from gupkg import settingsedit
from gupkg.catalog.record import read_install_record, write_install_record
from gupkg.engine.receipts import read_receipt, update_receipt
from gupkg.engine.stateio import read_track_state, write_track_state
from gupkg.model.settings import RegistryEntry
from gupkg.model.state import Effect, InstallRecord, Link
from gupkg.report import results
from gupkg.tomlio import read_toml

HOSTILE = 'C:\\opt\\"quoted" name\twith\ttabs\nand a line break — and ünïcode ☃'


def test_a_receipt_round_trips_hostile_text(tmp_path: Path) -> None:
    """The receipt keeps text with quotes, backslashes and line breaks intact."""
    update_receipt(tmp_path / "tool", "1.0", {"run": {"log": HOSTILE, "operation": "install"}, "origin": {"copied_from": HOSTILE}})

    data = read_receipt(tmp_path / "tool", "1.0").data

    assert data["run"] == {"log": HOSTILE, "operation": "install"} and data["origin"]["copied_from"] == HOSTILE


def test_the_install_record_round_trips_hostile_text(tmp_path: Path) -> None:
    """A link name and an effect field with hostile text read back equal."""
    record = InstallRecord("user", Link(registry="main", name=HOSTILE), (Effect("env", {"name": "X", "value": HOSTILE}),))

    write_install_record(tmp_path / "tool", record)
    read = read_install_record(tmp_path / "tool")

    assert read.problem is None and read.record.link.name == HOSTILE and read.record.effects[0].fields["value"] == HOSTILE


def test_the_last_check_state_round_trips_hostile_text(tmp_path: Path) -> None:
    """The result of the last check-updates keeps a message with quotes and line breaks."""
    write_track_state(tmp_path / "tool", {"found": "1.0", "outcome": HOSTILE})

    assert read_track_state(tmp_path / "tool")["outcome"] == HOSTILE


def test_the_settings_file_round_trips_unusual_paths_and_registry_sources(tmp_path: Path) -> None:
    """`settings set` and `registry add` write a path with a quote and a source with non-ASCII text, and the file reads back equal."""
    path = tmp_path / "settings.toml"

    settingsedit.set_setting(path, "root", "integration.bin", HOSTILE)
    settingsedit.add_registry(path, "root", "team", 'https://example.org/team-"registry"-ü.git', (RegistryEntry("main", "https://example.org/m.git"),))

    data = read_toml(path)
    assert data["integration"]["bin"] == HOSTILE
    assert data["registry"] == [
        {"name": "main", "source": "https://example.org/m.git"},
        {"name": "team", "source": 'https://example.org/team-"registry"-ü.git'},
    ]


def test_machine_documents_round_trip_hostile_text() -> None:
    """The error document keeps its message through JSON, whatever characters it holds, and carries the contract head."""
    document = results.error_document("install", "refused", HOSTILE, 2, hint=HOSTILE)

    parsed = json.loads(results.dumps(document))

    assert parsed["api"] == 2 and parsed["errors"][0]["message"] == HOSTILE and parsed["errors"][0]["hint"] == HOSTILE
