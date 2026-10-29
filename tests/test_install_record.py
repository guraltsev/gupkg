"""Cover the install record ``<package>\\.gupkg\\package.toml`` and the ``adhoc`` list of ``state.toml``.

Behavior covered: a record written and read back keeps its mode, its link (registry, name, revision, tracker hash),
the ``unlinked`` opt-out and its effects (including "adopted" ones); a package with no record reads as no record;
every kind of invalid record is reported with its problems and never raises out of the reader; the record is neither
read nor written through a linked ``.gupkg`` folder; ``state.toml`` ad-hoc entries need an absolute path and a known mode.

Real: files under pytest's temporary folder and junctions through ``FakeSystem`` (real links); the TOML writer of
``gupkg.tomlio``. Mocked: nothing. Out of scope: which effects exist and applying them (issue 028).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.record import read_install_record, record_path, write_install_record
from gupkg.model.state import Effect, InstallRecord, Link, RecordError, parse_install_record, parse_state

SHA = "0123456789abcdef" * 4


def test_record_round_trips_mode_link_and_effects(tmp_path: Path) -> None:
    """Writing a full record and reading it again returns the same record."""
    record = InstallRecord(
        mode="user",
        link=Link(registry="main", name="bar", revision="a" * 40, tracker_sha256=SHA),
        effects=(
            Effect("shortcut", {"path": "C:\\menu\\Foo.lnk"}),
            Effect("path", {"scope": "user", "entry": "C:\\bin"}, adopted=True),
            Effect("env", {"scope": "user", "name": "FOO", "value": "1", "export": ["a", "b"]}),
        ),
    )

    written = write_install_record(tmp_path / "foo", record)
    read = read_install_record(tmp_path / "foo")

    assert written == record_path(tmp_path / "foo") == tmp_path / "foo" / ".gupkg" / "package.toml"
    assert read.problem is None and read.record == record


def test_opt_out_is_recorded_as_unlinked(tmp_path: Path) -> None:
    """The ``gupkg link --unlink`` opt-out is a ``[link]`` table with ``unlinked = true`` and reads back as such."""
    write_install_record(tmp_path, InstallRecord(mode="user", link=Link(unlinked=True)))

    text = record_path(tmp_path).read_text(encoding="utf-8")
    read = read_install_record(tmp_path)

    assert "unlinked = true" in text
    assert read.record.link == Link(unlinked=True)


def test_package_without_a_record_has_none_and_no_problem(tmp_path: Path) -> None:
    """A missing record is normal: no record and no problem."""
    (tmp_path / "foo").mkdir()

    read = read_install_record(tmp_path / "foo")

    assert read.record is None and read.problem is None


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        pytest.param("schema = 1\nmode = 'user\n", "Cannot use the install record", id="not-toml"),
        pytest.param("mode = 'user'\n", "unsupported install record schema", id="no-schema"),
        pytest.param("schema = 2\n", "unsupported install record schema", id="future-schema"),
        pytest.param("schema = 1\nmode = 'everywhere'\n", "mode must be one of", id="unknown-mode"),
        pytest.param("schema = 1\nbogus = 1\n", "unknown key 'bogus'", id="unknown-key"),
        pytest.param("schema = 1\n[link]\nregistry = 'main'\nunlinked = true\n", "cannot be combined", id="link-and-unlink"),
        pytest.param("schema = 1\n[link]\ntracker_sha256 = 'abc'\n", "64 lower-case", id="bad-hash"),
        pytest.param("schema = 1\n[link]\nsurprise = 1\n", "unknown key 'link.surprise'", id="unknown-link-key"),
        pytest.param("schema = 1\n[[effect]]\nname = 'x'\n", "effect[0].kind", id="effect-without-kind"),
    ],
)
def test_invalid_records_are_reported_with_the_problem(tmp_path: Path, text: str, fragment: str) -> None:
    """An invalid record yields no record and a problem naming what is wrong; the reader never raises."""
    (tmp_path / ".gupkg").mkdir()
    record_path(tmp_path).write_text(text, encoding="utf-8")

    read = read_install_record(tmp_path)

    assert read.record is None
    assert fragment in read.problem


def test_parse_reports_every_problem_at_once() -> None:
    """Two mistakes in one record are both listed."""
    with pytest.raises(RecordError) as raised:
        parse_install_record({"schema": 1, "mode": "x", "nope": 1})

    assert len(raised.value.problems) == 2


def test_record_is_not_read_or_written_through_a_linked_data_folder(system, tmp_path: Path) -> None:
    """A ``.gupkg`` junction is neither followed to read a record nor written through."""
    package = tmp_path / "foo"
    package.mkdir()
    elsewhere = tmp_path / "elsewhere"
    write_install_record(elsewhere, InstallRecord(mode="user"))
    system.create_junction(package / ".gupkg", elsewhere / ".gupkg")

    assert "link" in read_install_record(package).problem
    with pytest.raises(ValueError, match="link"):
        write_install_record(package, InstallRecord(mode="system"))
    assert read_install_record(elsewhere).record.mode == "user"


def test_write_replaces_an_existing_record_atomically(tmp_path: Path) -> None:
    """Writing again replaces the record and leaves no temporary file behind."""
    write_install_record(tmp_path, InstallRecord(mode="user"))
    write_install_record(tmp_path, InstallRecord(mode="system"))

    assert read_install_record(tmp_path).record.mode == "system"
    assert sorted(path.name for path in (tmp_path / ".gupkg").iterdir()) == ["package.toml"]


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        pytest.param({"adhoc": [{"path": "C:\\x", "mode": "user"}]}, None, id="valid"),
        pytest.param({"adhoc": [{"path": "x", "mode": "user"}]}, "absolute path", id="relative-path"),
        pytest.param({"adhoc": [{"path": "C:\\x", "mode": "auto"}]}, "mode must be one of", id="unknown-mode"),
        pytest.param({"adhoc": "C:\\x"}, "array of tables", id="not-a-table-array"),
        pytest.param({"registry_sync": {"at": "now"}}, None, id="other-keys-are-ignored"),
    ],
)
def test_adhoc_entries_of_state_toml_are_validated(data: dict, problem: str | None) -> None:
    """``[[adhoc]]`` entries need an absolute path with a drive and a known mode; other keys belong to other readers."""
    if problem is None:
        parse_state(data)
        return
    with pytest.raises(RecordError, match=problem):
        parse_state(data)
