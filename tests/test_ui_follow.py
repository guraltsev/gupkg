"""Cover how the UI programs follow the events files of a ``gupkg --events-file`` call: ordering, bounds and distrust of the file.

Behaviour covered: the global file and the package files it links to merge into one stream in reading order with each event once; a half-written last line
waits until it is complete; a line that is too long, is not JSON or is not an event becomes one visible placeholder instead of an exception; a file that is a
folder is never read; a package file whose name or place is not exactly ``<package>\\.gupkg\\logs\\NAME`` is not followed; one poll returns a bounded number of
records; the announced stop marker and ``run_end`` are exposed.

Real: the follower on real files in a temporary folder, including the files the real CLI wrote (recorded fixtures). Mocked: nothing.
Out of scope: how the records are drawn (``test_ui_render.py``) and the subprocess (``test_ui_client.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

from gupkg_ui import follow
from gupkg_ui.follow import EventStream, RecordReader, read_run_end
from tests.ui_support import load_fixture


def write(path: Path, *records: object, raw: str = "") -> None:
    """Append *records* (and raw text) to *path* as JSON lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
        handle.write(raw)


def test_a_half_written_line_waits_until_it_is_complete(tmp_path: Path) -> None:
    """A last line without its newline is not parsed (or lost): it appears once the rest arrives."""
    path = tmp_path / "events.jsonl"
    write(path, {"event": "step", "text": "one"}, raw='{"event": "step", "te')
    reader = RecordReader(path)

    assert [item["text"] for item in reader.read()] == ["one"]
    write(path, raw='xt": "two"}\n')
    assert [item["text"] for item in reader.read()] == ["two"]


def test_bad_lines_become_one_visible_placeholder_each(tmp_path: Path) -> None:
    """A line that is not JSON and one that is not an event record are reported, never raised and never silently dropped."""
    path = tmp_path / "events.jsonl"
    write(path, raw="not json\n[1, 2]\n" + json.dumps({"no_event": 1}) + "\n")
    write(path, {"event": "step", "text": "after"})

    records = RecordReader(path).read()

    assert [item["event"] for item in records] == ["unreadable", "unreadable", "unreadable", "step"]


def test_a_record_over_the_limit_is_skipped_and_the_next_one_is_read(tmp_path: Path, monkeypatch) -> None:
    """A huge line costs bounded memory: it is reported once and reading continues after it."""
    monkeypatch.setattr(follow, "MAX_RECORD_BYTES", 200)
    path = tmp_path / "events.jsonl"
    write(path, {"event": "step", "text": "x" * 5000}, {"event": "step", "text": "small"})

    records = RecordReader(path).read()

    assert [item["event"] for item in records] == ["unreadable", "step"] and records[1]["text"] == "small"


def test_a_folder_instead_of_a_file_is_never_read(tmp_path: Path) -> None:
    """Only a plain file is followed; a folder (or a link) under the name gives one placeholder and nothing more."""
    path = tmp_path / "events.jsonl"
    path.mkdir()
    reader = RecordReader(path)

    assert [item["event"] for item in reader.read()] == ["unreadable"] and reader.read() == []


def test_a_missing_file_gives_nothing_and_no_error(tmp_path: Path) -> None:
    """The call may not have created its file yet: following it is quiet until it appears."""
    assert RecordReader(tmp_path / "later.jsonl").read() == []


def test_one_poll_returns_at_most_the_limit_and_the_rest_next_time(tmp_path: Path) -> None:
    """A file with many records is delivered in bounded batches, so a runaway stream never freezes the caller."""
    path = tmp_path / "events.jsonl"
    write(path, *({"event": "step", "text": str(number)} for number in range(30)))
    reader = RecordReader(path)

    first = reader.read(limit=10)
    rest = reader.read(limit=100)

    assert [item["text"] for item in first] == [str(number) for number in range(10)]
    assert [item["text"] for item in rest] == [str(number) for number in range(10, 30)]


def test_the_recorded_run_of_the_real_cli_merges_in_reading_order(tmp_path: Path) -> None:
    """The global file and the two package files that the real CLI wrote read as one run: each package's events sit between its start and end, once.

    The fixture is a real ``update`` of two packages: the global file has no phases (they are in the package files), so the merged stream shows both.
    """
    logs = tmp_path / "home" / "opt" / ".gupkg" / "logs"
    logs.mkdir(parents=True)
    (logs / "rec.jsonl").write_text(load_fixture("events_global.jsonl", tmp_path), encoding="utf-8")
    for name in ("alpha", "beta"):
        folder = tmp_path / "home" / "opt" / name / ".gupkg" / "logs"
        folder.mkdir(parents=True)
        (folder / "rec.jsonl").write_text(load_fixture(f"events_{name}.jsonl", tmp_path), encoding="utf-8")

    stream = EventStream(logs / "rec.jsonl")
    kinds = [(item["event"], item.get("phase") or (item.get("view") or {}).get("name") or (item.get("result") or {}).get("name")) for item in stream.poll()]

    assert kinds[0][0] == "run_start" and kinds[-1][0] == "run_end"
    assert [name for kind, name in kinds if kind == "package_start"] == ["alpha", "beta"]
    alpha_start, alpha_end = kinds.index(("package_start", "alpha")), kinds.index(("package_end", "alpha"))
    beta_start = kinds.index(("package_start", "beta"))
    assert alpha_start < kinds.index(("phase_start", "Resolve")) < alpha_end < beta_start
    assert [kind for kind, _ in kinds].count("run_start") == 1 and [kind for kind, _ in kinds].count("package_start") == 2
    assert stream.run_end is not None and stream.stop_file and stream.stop_file.endswith(tuple("0123456789abcdef"))


def test_a_package_file_with_the_wrong_shape_is_not_followed(tmp_path: Path) -> None:
    """A link record that names another file name or a place that is not ``<package>\\.gupkg\\logs`` is ignored: the stream does not read it."""
    logs = tmp_path / "root" / ".gupkg" / "logs"
    outside = tmp_path / "elsewhere" / "secret.jsonl"
    write(outside, {"event": "step", "text": "SECRET"})
    write(logs / "run.jsonl",
          {"event": "log_link", "package": "p", "events_file": str(outside)},
          {"event": "package_start", "view": {"name": "p", "path": str(tmp_path / "p")}},
          {"event": "package_end", "result": {"name": "p"}},
          {"event": "run_end", "summary": "", "exit_code": 0, "result": {}})

    texts = [item.get("text") for item in EventStream(logs / "run.jsonl").poll()]

    assert "SECRET" not in texts


def test_read_run_end_needs_the_last_record_to_be_run_end(tmp_path: Path) -> None:
    """A finished file gives its ``run_end``; an unfinished one gives nothing, which callers treat as a failed run."""
    done, unfinished = tmp_path / "done.jsonl", tmp_path / "unfinished.jsonl"
    write(done, {"event": "step", "text": "x"}, {"event": "run_end", "summary": "s", "exit_code": 0, "result": {}})
    write(unfinished, {"event": "step", "text": "x"})

    assert read_run_end(done)["summary"] == "s"
    assert read_run_end(unfinished) is None and read_run_end(tmp_path / "missing.jsonl") is None
