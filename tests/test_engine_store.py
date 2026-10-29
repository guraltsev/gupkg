"""Cover what the engine writes and reads beside the package: fingerprints, receipts, portable data, ``state.toml`` and error codes.

Behaviour covered: the ``App`` fingerprint never follows a link and compares files by relative path without regard to letter case;
persisted portable state is skipped by the comparison; the version fingerprint covers pkg.toml, scripts and shipped files
(not a downloaded ``App``, not ``.gupkg``) and names the differing files; receipts merge sections, refuse to be written through a
link and refuse a schema they do not know; portable hand-over refuses paths that leave ``App`` or ``data`` and never writes
through a link; the ad-hoc list keeps the other keys of ``state.toml`` and is never silently replaced; every exception an operation
can raise has a stable code and exit status.

Real: the engine's own modules and real folders and junctions. Mocked: ``FakeSystem`` for the junctions.
Out of scope: the install flow around them (the lifecycle tests).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from gupkg.catalog.catalog import AmbiguousSelector, NotFound, NotInstalled
from gupkg.catalog.record import write_install_record
from gupkg.engine.errors import describe
from gupkg.engine.fingerprint import (
    LINK_MARK,
    aggregate_digest,
    app_files,
    diff_app,
)
from gupkg.engine.planner import PlanError
from gupkg.engine.portable import PortableError, hand_back, hand_over
from gupkg.engine.receipts import MAX_RECEIPT_BYTES, list_receipts, read_receipt, receipt_checksum, update_receipt
from gupkg.engine.stateio import read_track_state, register_adhoc, unregister_adhoc, write_track_state
from gupkg.model.checksum import ChecksumMismatch, ChecksumMissing
from gupkg.model.scope import InputNeeded, ModeRefused
from gupkg.model.state import InstallRecord
from gupkg.model.settings import Layer, default_layer, merge_layers
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ProcessNotAllowed
from gupkg.tomlio import read_toml, write_toml


@pytest.fixture
def system(tmp_path: Path) -> FakeSystem:
    """A fake machine whose junctions are real."""
    return FakeSystem(tmp_path / "machine")


def write(path: Path, text: str = "x") -> Path:
    """Write a text file, creating folders, and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def test_the_app_fingerprint_never_follows_a_junction(tmp_path: Path, system: FakeSystem) -> None:
    """A junction inside App is recorded as a link and its target is neither entered nor hashed."""
    app = tmp_path / "App"
    write(app / "a.exe", "a")
    outside = tmp_path / "outside"
    write(outside / "secret.txt", "secret")
    system.create_junction(app / "Data", outside)

    files = app_files(app)

    assert set(files) == {"a.exe", "Data"} and files["Data"] == LINK_MARK


def test_diff_app_reports_modified_missing_and_extra_files_without_regard_to_case(tmp_path: Path) -> None:
    """Files are compared by relative path as Windows does; the three lists are sorted."""
    app = tmp_path / "App"
    write(app / "Lib" / "b.dll", "b")
    write(app / "a.exe", "a")
    recorded = app_files(app)
    write(app / "a.exe", "changed")
    (app / "Lib" / "b.dll").unlink()
    write(app / "new.txt", "n")

    modified, missing, extra = diff_app(app, {key.upper(): value for key, value in recorded.items()})

    assert (modified, missing, extra) == (["A.EXE"], ["LIB/B.DLL"], ["new.txt"])


def test_diff_app_skips_persisted_state(tmp_path: Path) -> None:
    """Portable state kept in App (a persisted folder and file) is not code: changes there are not reported."""
    app = tmp_path / "App"
    write(app / "a.exe", "a")
    write(app / "Data" / "state.txt", "1")
    write(app / "settings.ini", "1")
    recorded = app_files(app)
    write(app / "Data" / "state.txt", "2")
    write(app / "Data" / "more.txt", "3")
    write(app / "settings.ini", "2")

    assert diff_app(app, recorded, skip=["Data", "settings.ini"]) == ([], [], [])
    assert diff_app(app, recorded)[0] == ["Data/state.txt", "settings.ini"]


def test_the_aggregate_digest_does_not_depend_on_order_or_letter_case() -> None:
    """One digest over a record: the same files in any order and spelling give the same value; a changed hash changes it."""
    first = aggregate_digest({"A.txt": "1", "b.txt": "2"})

    assert first == aggregate_digest({"b.TXT": "2", "a.txt": "1"}) and first != aggregate_digest({"A.txt": "1", "b.txt": "3"})


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


def test_receipt_sections_are_merged_and_whole_sections_replaced(tmp_path: Path) -> None:
    """A later write replaces the sections it names and keeps the others; there is one receipt per version."""
    package = tmp_path / "tool"
    update_receipt(package, "1.0.0", {"source": {"kind": "download"}, "checksum": {"decision": "verified", "observed_sha256": "a" * 64}})

    update_receipt(package, "1.0.0", {"run": {"operation": "update"}, "checksum": {"decision": "origin-not-verified"}})

    data = read_receipt(package, "1.0.0").data
    assert data["source"] == {"kind": "download"} and data["run"] == {"operation": "update"}
    assert receipt_checksum(data).decision == "origin-not-verified" and "observed_sha256" not in data["checksum"]
    assert list_receipts(package) == ["1.0.0"]


def test_a_receipt_is_never_written_through_a_link(tmp_path: Path, system: FakeSystem) -> None:
    """A version folder that is a junction elsewhere is refused for writing and reading."""
    package = tmp_path / "tool"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    package.mkdir()
    system.create_junction(package / "1.0.0", elsewhere)

    with pytest.raises(ValueError, match="link"):
        update_receipt(package, "1.0.0", {"source": {}})
    assert read_receipt(package, "1.0.0").problem and list(elsewhere.iterdir()) == []


def test_a_receipt_over_the_size_limit_is_refused_unread(tmp_path: Path) -> None:
    """A user-writable receipt cannot make `check` read an enormous file: one byte over 1 MiB is a problem, 1 MiB itself is read."""
    package = tmp_path / "tool"
    path = update_receipt(package, "1.0.0", {"source": {"kind": "download"}})
    valid = path.read_bytes()
    padding = b"# " + b"x" * (MAX_RECEIPT_BYTES - len(valid) - 3) + b"\n"
    path.write_bytes(valid + padding)
    assert path.stat().st_size == MAX_RECEIPT_BYTES and read_receipt(package, "1.0.0").data["source"] == {"kind": "download"}

    path.write_bytes(valid + padding + b"#")
    read = read_receipt(package, "1.0.0")

    assert MAX_RECEIPT_BYTES == 1024 * 1024 and read.data is None and "is not read" in read.problem


def test_a_receipt_with_an_unknown_schema_or_broken_text_is_a_problem_not_data(tmp_path: Path) -> None:
    """An unreadable or foreign receipt is reported, and a checksum table that is not valid yields no record."""
    package = tmp_path / "tool"
    write(package / "1.0.0" / "App.info", "schema = 9\n")
    write(package / "2.0.0" / "App.info", "not = [toml")

    assert "unsupported schema" in read_receipt(package, "1.0.0").problem
    assert "Cannot read" in read_receipt(package, "2.0.0").problem
    assert read_receipt(package, "3.0.0") == read_receipt(package, "4.0.0") and read_receipt(package, "3.0.0").problem is None
    assert receipt_checksum({"checksum": {"decision": "bogus"}}) is None


# ---------------------------------------------------------------------------
# Portable data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("item", ["..\\outside.txt", "a\\..\\..\\outside.txt", "C:\\outside.txt", "", "a:b"])
def test_portable_persistence_refuses_paths_that_leave_app_or_data(tmp_path: Path, system: FakeSystem, item: str) -> None:
    """A persisted path with `..`, a drive or a stream is refused before anything is moved."""
    app, data = tmp_path / "App", tmp_path / "data"
    app.mkdir()
    write(tmp_path / "outside.txt", "keep")

    with pytest.raises(PortableError):
        hand_over(system, app, data, [item])
    with pytest.raises(PortableError):
        hand_back(system, app, data, [item])

    assert (tmp_path / "outside.txt").read_text() == "keep"


def test_portable_hand_over_never_writes_through_a_link_in_the_path(tmp_path: Path, system: FakeSystem) -> None:
    """A persisted path below a junction of App would write outside App: it is refused."""
    app, data = tmp_path / "App", tmp_path / "data"
    outside = tmp_path / "outside"
    app.mkdir()
    outside.mkdir()
    system.create_junction(app / "sub", outside)
    write(data / "sub" / "x.txt", "saved")

    with pytest.raises(PortableError, match="link"):
        hand_over(system, app, data, ["sub/x.txt"])

    assert list(outside.iterdir()) == []


def test_portable_first_population_moves_the_vendor_default_into_data_and_state_wins_later(tmp_path: Path, system: FakeSystem) -> None:
    """The first hand-over migrates what the vendor shipped; a later App gets the saved state in place of its own defaults."""
    data = tmp_path / "data"
    first = tmp_path / "1" / "App"
    write(first / "settings.ini", "vendor")
    write(first / "Data" / "state.txt", "vendor state")

    hand_over(system, first, data, ["settings.ini", "Data"])
    write(first / "settings.ini", "user")
    hand_back(system, first, data, ["settings.ini", "Data"])
    second = tmp_path / "2" / "App"
    write(second / "settings.ini", "vendor 2")
    write(second / "Data" / "state.txt", "vendor 2 state")
    hand_over(system, second, data, ["settings.ini", "Data"])

    assert (data / "settings.ini").read_text() == "user" and (second / "settings.ini").read_text() == "user"
    assert (second / "Data" / "state.txt").read_text() == "vendor state" and system.is_link(second / "Data")


@pytest.mark.parametrize("which", ["receipt", "install-record", "toml", "track-state"])
def test_a_write_that_fails_at_the_replace_leaves_the_old_file_intact_and_no_temporary_file(tmp_path: Path, which: str) -> None:
    """Writes are atomic: while another handle holds the old file the replace fails (Windows refuses it), the old bytes stay and no temp file is left."""
    package = tmp_path / "tool"
    writers = {
        "receipt": (package / "1.0.0" / "App.info", lambda: update_receipt(package, "1.0.0", {"source": {"kind": "download"}, "new": 1})),
        "install-record": (package / ".gupkg" / "package.toml", lambda: write_install_record(package, InstallRecord("user", None, ()))),
        "toml": (package / "x.toml", lambda: write_toml(package / "x.toml", {"a": 2})),
        "track-state": (package / ".gupkg" / "state" / "track.toml", lambda: write_track_state(package, {"found": "9.9"})),
    }
    target, write = writers[which]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('schema = 1\nold = "yes"\n', encoding="utf-8")
    before = target.read_bytes()

    with open(target, "rb"):
        with pytest.raises(OSError):
            write()

    assert target.read_bytes() == before
    assert [item.name for item in target.parent.iterdir()] == [target.name]


def test_a_persisted_folder_the_application_replaced_is_merged_into_data_and_nothing_is_lost(tmp_path: Path, system: FakeSystem) -> None:
    """The app swapped its junction for a real folder: newer files win, files only data has stay, and the next hand-over loses nothing."""
    data, app = tmp_path / "data", tmp_path / "1" / "App"
    write(data / "Data" / "old.txt", "saved old")
    write(data / "Data" / "only-in-data.txt", "keep me")
    write(data / "Data" / "shared.txt", "saved shared")
    write(app / "Data" / "new.txt", "new file")
    write(app / "Data" / "sub" / "deep.txt", "deep")
    write(app / "Data" / "shared.txt", "app shared")
    write(app / "Data" / "old.txt", "app edit")
    stamp = time.time()
    os.utime(data / "Data" / "old.txt", (stamp - 1000, stamp - 1000))
    os.utime(data / "Data" / "shared.txt", (stamp, stamp))
    os.utime(app / "Data" / "shared.txt", (stamp - 500, stamp - 500))  # older than data's copy: data keeps its own
    os.utime(app / "Data" / "old.txt", (stamp - 10, stamp - 10))  # newer than data's copy: the app's wins

    saved = hand_back(system, app, data, ["Data"])
    second = tmp_path / "2" / "App"
    write(second / "Data" / "vendor.txt", "vendor default")
    hand_over(system, second, data, ["Data"])

    assert saved == ["Data (3 newer files merged)"]
    assert (data / "Data" / "only-in-data.txt").read_text() == "keep me" and (data / "Data" / "shared.txt").read_text() == "saved shared"
    assert (data / "Data" / "old.txt").read_text() == "app edit" and (data / "Data" / "sub" / "deep.txt").read_text() == "deep"
    assert (data / "Data" / "new.txt").read_text() == "new file" and system.is_link(second / "Data")
    assert (second / "Data" / "new.txt").read_text() == "new file"


def test_a_file_that_is_a_folder_in_data_is_a_portable_error_not_a_crash(tmp_path: Path, system: FakeSystem) -> None:
    """A persisted file whose `data` counterpart is a folder raises PortableError up front and writes nothing."""
    data, app = tmp_path / "data", tmp_path / "1" / "App"
    write(data / "settings.ini" / "inner.txt", "a folder in data")
    write(app / "settings.ini", "a file in the app")

    with pytest.raises(PortableError, match="nothing was changed"):
        hand_back(system, app, data, ["settings.ini"])

    assert (data / "settings.ini" / "inner.txt").read_text() == "a folder in data" and (data / "settings.ini").is_dir()


def test_a_folder_that_is_a_file_in_data_stops_the_merge_before_it_writes(tmp_path: Path, system: FakeSystem) -> None:
    """A name that is a folder on one side and a file on the other is an error, with nothing copied."""
    data, app = tmp_path / "data", tmp_path / "1" / "App"
    write(data / "Data" / "x", "a file in data")
    write(app / "Data" / "x" / "inner.txt", "a folder in the app")
    write(app / "Data" / "other.txt", "would be copied")

    with pytest.raises(PortableError, match="nothing was changed"):
        hand_back(system, app, data, ["Data"])

    assert not (data / "Data" / "other.txt").exists() and (data / "Data" / "x").read_text() == "a file in data"


# ---------------------------------------------------------------------------
# state.toml and track.toml
# ---------------------------------------------------------------------------


def settings_for(tmp_path: Path):
    """Return settings whose roots are below *tmp_path*."""
    layer = Layer("app", {"schema": 4, "roots": {
        "system": str(tmp_path / "sys"), "user": str(tmp_path / "user")}})
    return merge_layers([default_layer("user"), layer])


def test_adhoc_registration_is_idempotent_and_keeps_the_other_keys(tmp_path: Path) -> None:
    """Registering a package twice writes one entry; keys of other readers in state.toml stay."""
    settings = settings_for(tmp_path)
    state = tmp_path / "user" / ".gupkg" / "state.toml"
    write_toml(state, {"last_sync": "yesterday"})
    package = tmp_path / "elsewhere" / "tool"

    register_adhoc(settings, {}, package, "user")
    register_adhoc(settings, {}, package, "user")

    data = read_toml(state)
    assert data == {"last_sync": "yesterday", "adhoc": [{"path": str(package), "mode": "user"}]}
    unregister_adhoc(settings, {}, package, "user")
    assert read_toml(state) == {"last_sync": "yesterday"}


def test_an_unreadable_state_file_is_never_silently_replaced(tmp_path: Path) -> None:
    """A state.toml that is not valid TOML stops the registration instead of being overwritten."""
    settings = settings_for(tmp_path)
    state = write(tmp_path / "user" / ".gupkg" / "state.toml", "adhoc = [oops")

    with pytest.raises(ValueError, match="Cannot read"):
        register_adhoc(settings, {}, tmp_path / "elsewhere" / "tool", "user")

    assert state.read_text() == "adhoc = [oops"


def test_the_track_state_round_trips(tmp_path: Path) -> None:
    """The result of the last check-updates is written and read back; a missing file is None."""
    package = tmp_path / "tool"
    assert read_track_state(package) is None

    write_track_state(package, {"found": "1.1.0", "populated": True})

    assert read_track_state(package) == {"schema": 1, "found": "1.1.0", "populated": True}


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        pytest.param(ModeRefused("no"), "refused", 2, id="mode-refused"),
        pytest.param(InputNeeded("ask"), "input-needed", 5, id="input-needed"),
        pytest.param(NotFound("x"), "not-found", 2, id="not-found"),
        pytest.param(NotInstalled("x"), "not-installed", 2, id="not-installed"),
        pytest.param(AmbiguousSelector("x", ["user:x", "system:x"]), "ambiguous", 2, id="ambiguous"),
        pytest.param(PlanError("x"), "cannot-plan", 2, id="plan-error"),
        pytest.param(ChecksumMismatch("m", "https://x"), "checksum-mismatch", 3, id="checksum-mismatch"),
        pytest.param(ProcessNotAllowed("x"), "needs-real-run", 3, id="needs-a-real-run"),
        pytest.param(OSError("disk"), "io-error", 3, id="os-error"),
        pytest.param(KeyError("odd"), "internal-error", 4, id="unexpected"),
    ],
)
def test_every_failure_has_a_stable_code_and_exit_status(error: Exception, code: str, exit_code: int) -> None:
    """The codes are the machine contract: 2 caller error, 3 operation failed, 4 internal, 5 declined."""
    status, data = describe(error)

    assert (status, data["code"]) == (exit_code, code) and data["message"]


def test_a_checksum_refusal_carries_its_url_and_whether_the_flag_could_help() -> None:
    """The structured error of a missing checksum names the URL and says it is skippable (the manager reads this)."""
    status, data = describe(ChecksumMissing("no hash", "https://example.org/a.zip", skippable=True))

    assert status == 3 and data == {"code": "checksum-missing", "message": "no hash", "url": "https://example.org/a.zip", "skippable": True}
