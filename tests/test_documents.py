"""Cover the one typed reader of the CLI's ``--json`` documents (``gupkg.report.documents``) against the documents the real CLI recorded.

Behaviour covered: every recorded document (``tests/fixtures/cli_docs``) reads without error, ``available`` rows (no installed version) and the
``system only`` mark included; a document that is not ``api`` 2, has a field of the wrong type or lacks a field raises ``DocumentError`` naming the field;
a failed call is never read as an empty list; writer and reader agree (a typed row written with ``to_data`` reads back equal, and the recorded
``list`` and ``info`` documents are rebuilt byte for byte from what the reader returned).

Real: the reader and the documents of the real CLI. Mocked: nothing. Out of scope: the screens (``test_ui_screens.py``) and the in-process reads (``test_ui_reads.py``).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from gupkg.report.documents import (
    DocumentError, ListDocument, ListRow, read_info, read_list, read_plan, read_registry_status, read_results,
)

DOCS = Path(__file__).resolve().parent / "fixtures" / "cli_docs"


def doc(name: str) -> dict:
    """Return the recorded document *name*."""
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def test_read_list_reads_every_recorded_document_including_available_rows() -> None:
    """``list``, ``list --available`` and ``list --names`` read; an available row has no installed version and the drv row is marked ``system only`` with its reason."""
    listed, available, names = read_list(doc("list.json")), read_list(doc("list_available.json")), read_list(doc("list_names.json"))

    assert [row.name for row in listed.rows] == ["alpha", "beta", "gamma"] and listed.mode == "user"
    assert {row.name: row.status for row in available.rows} == {"copyq": "available", "drv": "available", "graphviz": "available"}
    assert all(row.version is None and row.path is None for row in available.rows)
    drv = next(row for row in available.rows if row.name == "drv")
    assert drv.restriction == "system only" and drv.restriction_reason and "administrator" in drv.restriction_reason
    assert names.rows == () and names.names is not None and "alpha" in names.names


def test_the_other_recorded_documents_read() -> None:
    """``info``, ``registry status``, the plan, a success, a failure and ``check`` read into typed entries."""
    info = read_info(doc("info.json")).rows[0]
    registry = read_registry_status(doc("registry_status.json"))
    plan = read_plan(doc("install_plan.json"))
    done = read_results(doc("install_result.json"))
    failure = read_results(doc("install_failure.json"))
    check = read_results(doc("check.json"))

    assert info.row.name == "alpha" and [item.name for item in info.versions] == ["1.0.0", "2.0.0"] and info.log
    assert registry.registries[0].name == "main" and registry.registries[0].revision and registry.registries[0].packages == 3
    assert plan.items[0].target == "copyq" and (plan.items[0].plan is not None or plan.items[0].result is not None)
    assert done.ok and done.results[0].status == "installed"
    assert not failure.ok and failure.results[0].error is not None and failure.results[0].error.code
    assert check.results[0].package == "alpha"


def test_a_row_with_a_field_of_the_wrong_type_is_an_error_that_names_the_field() -> None:
    """A number where the version should be text fails with the path ``packages[1].version``, never a dropped row."""
    document = doc("list.json")
    document["packages"][1]["version"] = 3

    with pytest.raises(DocumentError, match=r"packages\[1\]\.version: expected text or null, got int"):
        read_list(document)


@pytest.mark.parametrize("mutation, message", [
    pytest.param(lambda d: d.update(api=1), "api: expected 2", id="old-api"),
    pytest.param(lambda d: d.pop("api"), "'api' is missing", id="no-api"),
    pytest.param(lambda d: d["packages"][0].pop("restriction"), r"packages\[0\]: the field 'restriction' is missing", id="missing-field"),
    pytest.param(lambda d: d.update(packages="x"), "packages: expected a list", id="packages-not-a-list"),
    pytest.param(lambda d: d["packages"].append("junk"), r"packages\[3\]: expected an object", id="row-not-an-object"),
    pytest.param(lambda d: d.update(command="info"), "command: expected 'list'", id="wrong-command"),
])
def test_a_list_document_that_cannot_be_read_is_an_error(mutation, message: str) -> None:
    """A wrong api, a missing field, a row that is not an object: the reader raises with the field named."""
    document = copy.deepcopy(doc("list.json"))
    mutation(document)

    with pytest.raises(DocumentError, match=message):
        read_list(document)


def test_a_document_without_a_mode_is_an_error_not_an_empty_inventory() -> None:
    """The silent empty list of a document that names no mode is now a visible error."""
    document = doc("list.json")
    del document["mode"]

    with pytest.raises(DocumentError, match="'mode' is missing"):
        read_list(document)


def test_a_failed_call_is_never_read_as_an_empty_list() -> None:
    """``ok: false`` with an error and no rows raises with the CLI's message."""
    document = {"api": 2, "command": "list", "ok": False, "exit_code": 2, "errors": [{"code": "bad", "message": "the root cannot be read"}]}

    with pytest.raises(DocumentError, match="the root cannot be read"):
        read_list(document)


def test_an_unreadable_checksum_marker_in_a_plan_is_an_error() -> None:
    """A marker that is not present, missing or invalid never means ``present``."""
    document = doc("install_plan.json")
    item = document["plans"][0]
    if "plan" not in item:
        pytest.skip("the recorded plan has no download")
    item["plan"].setdefault("populate", {})["checksum"] = {"sha256": "maybe", "url": "https://x", "url_from": "pkg.toml", "skippable": True}

    with pytest.raises(DocumentError, match=r"plans\[0\]\.plan\.populate\.checksum\.sha256"):
        read_plan(document)


def test_a_typed_row_written_with_to_data_reads_back_equal() -> None:
    """Writer and reader share the types: write, read, compare."""
    row = ListRow("x", "available", None, "1.0", None, "main/x", ("w",), "user only", "why", ("p",))
    written = ListDocument("user", "C:\\opt", (row,), ("careful",)).to_data()

    assert read_list(written) == ListDocument("user", "C:\\opt", (row,), ("careful",))


def test_the_recorded_list_and_info_documents_are_rebuilt_from_what_the_reader_returned() -> None:
    """``to_data`` of the read document is the recorded document: the writer's key set and order are the reader's."""
    listed, info = doc("list.json"), doc("info.json")

    assert read_list(listed).to_data() == listed
    assert read_info(info).to_data() == info
    assert read_registry_status(doc("registry_status.json")).to_data() == doc("registry_status.json")
