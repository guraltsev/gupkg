"""Cover the behavior that every ``System`` implementation must share.

One set of contract tests runs against ``FakeSystem`` on every run and against
``WindowsSystem`` on Windows (marked ``windows_integration``), so the real and
the fake machine cannot drift apart. The Windows run is real: NTFS junctions,
hard links, ``.lnk`` files through the Shell COM API, child processes, and
``file:`` URL downloads, all below pytest temporary directories; environment
values live in a throwaway ``HKCU\\Software\\gupkg-test-<uuid>`` key that is
deleted afterwards, never in ``HKCU\\Environment`` or ``HKLM``. The
``WM_SETTINGCHANGE`` broadcast, UAC prompts and the real Start Menu are out of
scope (see ``tests/manual_smoke.md``), and so is what the engine does with the
answers; the fake's own bookkeeping is covered in ``tests/test_fake_system.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from gupkg.system.fake import FakeSystem
from gupkg.system.port import EnvValue, ProcessAborted, ProcessResult, Scope, ShortcutSpec, System, WorkerDidNotStart
from gupkg.system.windows import WindowsSystem, _ProcessJob

windows_only = pytest.mark.skipif(os.name != "nt", reason="needs Windows")

AWKWARD_TEXT = "it's \"quoted\" $var `tick` %TEMP% caf\u00e9 \u4e2d\u6587"


def _delete_key_tree(hive: object, subkey: str) -> None:
    """Delete a registry key and everything below it; a missing key is fine."""
    import winreg

    try:
        with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_key_tree(hive, subkey + "\\" + child)
    except FileNotFoundError:
        return
    winreg.DeleteKey(hive, subkey)


@pytest.fixture(
    params=[
        pytest.param("fake", id="fake"),
        pytest.param("windows", id="windows", marks=[pytest.mark.windows_integration, windows_only]),
    ]
)
def machine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[System]:
    """Provide each ``System`` implementation, confined to temporary locations.

    The fake keeps everything below ``tmp_path/machine``. The Windows
    implementation stores user and machine environment values in two
    throwaway ``HKCU`` keys and deletes them afterwards.
    """
    if request.param == "fake":
        yield FakeSystem(tmp_path / "machine")
        return
    import winreg

    base = rf"Software\gupkg-test-{uuid.uuid4().hex}"
    hive = winreg.HKEY_CURRENT_USER
    try:
        yield WindowsSystem(
            user_environment=(hive, base + r"\user"),
            machine_environment=(hive, base + r"\machine"),
        )
    finally:
        _delete_key_tree(hive, base)


@pytest.fixture
def served(machine: System, tmp_path: Path):
    """Return a function that publishes bytes at a URL this implementation can fetch.

    The fake registers the bytes in its URL map; the Windows implementation
    gets a ``file:`` URL for a temporary file, so no network is involved.
    """
    def publish(body: bytes) -> str:
        """Make *body* downloadable and return its URL."""
        if isinstance(machine, FakeSystem):
            url = f"https://example.invalid/{uuid.uuid4().hex}"
            machine.add_url(url, body)
            return url
        path = tmp_path / f"served-{uuid.uuid4().hex}"
        path.write_bytes(body)
        return path.as_uri()

    return publish


# ---------------------------------------------------------------------------
# Environment and PATH
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", [Scope.USER, Scope.MACHINE], ids=["user", "machine"])
def test_environment_value_round_trips_and_names_are_case_insensitive(machine: System, scope: Scope) -> None:
    """A stored value reads back unchanged under any spelling of its name, and a re-set replaces it.

    The expandable flag survives, a second write with different capitalisation
    leaves one value rather than two, and deleting it (twice) leaves nothing.
    """
    machine.set_env(scope, "Gupkg_Test_Var", r"%USERPROFILE%\tools", True)

    assert machine.get_env(scope, "GUPKG_TEST_VAR") == EnvValue(r"%USERPROFILE%\tools", True)

    machine.set_env(scope, "gupkg_test_var", "plain", False)
    assert machine.get_env(scope, "Gupkg_Test_Var") == EnvValue("plain", False)

    machine.delete_env(scope, "GUPKG_TEST_VAR")
    machine.delete_env(scope, "GUPKG_TEST_VAR")
    assert machine.get_env(scope, "Gupkg_Test_Var") is None


def test_user_and_machine_environments_are_independent(machine: System) -> None:
    """A value set for one scope is invisible to the other."""
    machine.set_env(Scope.USER, "GUPKG_SCOPE_TEST", "user", False)

    assert machine.get_env(Scope.MACHINE, "GUPKG_SCOPE_TEST") is None
    assert machine.get_env(Scope.USER, "GUPKG_SCOPE_TEST") == EnvValue("user", False)


def test_path_round_trip_is_idempotent_and_stored_as_expandable_path(machine: System) -> None:
    """PATH entries read back in order, writing them again changes nothing, and ``PATH`` is a case-insensitive name.

    A scope with no PATH value reads as an empty list, and empty entries are dropped.
    """
    assert machine.get_path(Scope.USER) == []
    entries = [r"C:\Tools\One", r"%USERPROFILE%\bin", r"c:\tools\ONE2"]

    machine.set_path(Scope.USER, entries)
    machine.set_path(Scope.USER, machine.get_path(Scope.USER))

    assert machine.get_path(Scope.USER) == entries
    assert machine.get_env(Scope.USER, "PATH") == EnvValue(";".join(entries), True)
    assert machine.get_env(Scope.USER, "path") == EnvValue(";".join(entries), True)

    machine.set_env(Scope.USER, "Path", r"C:\a;;  ;C:\b", True)
    assert machine.get_path(Scope.USER) == [r"C:\a", r"C:\b"]


# ---------------------------------------------------------------------------
# Shortcuts
# ---------------------------------------------------------------------------


def test_shortcut_fields_survive_a_round_trip_and_the_folder_is_created(machine: System, tmp_path: Path) -> None:
    """Quotes, shell metacharacters, ``%VAR%``, non-ASCII text and an icon index come back unchanged.

    The shortcut's folder does not exist beforehand, and writing again
    replaces every field.
    """
    path = tmp_path / "Start Menu" / "nested" / "round trip.lnk"
    spec = ShortcutSpec(
        target_path="%COMSPEC%",
        arguments=f"/k echo {AWKWARD_TEXT}",
        working_directory="C:\\" + "d" * 210,
        icon_location="C:\\Windows\\System32\\cmd.exe,3",
        description=AWKWARD_TEXT,
    )

    machine.write_shortcut(path, spec)
    assert machine.read_shortcut(path) == spec

    machine.write_shortcut(path, ShortcutSpec("C:\\new.exe"))
    assert machine.read_shortcut(path) == ShortcutSpec("C:\\new.exe")


def test_shortcut_can_be_deleted_and_deleting_an_absent_one_is_fine(machine: System, tmp_path: Path) -> None:
    """A deleted shortcut can no longer be read, and deleting it again is not an error."""
    path = tmp_path / "menu" / "gone.lnk"
    machine.write_shortcut(path, ShortcutSpec("C:\\Windows\\notepad.exe"))

    machine.delete_shortcut(path)
    machine.delete_shortcut(path)

    with pytest.raises(OSError):
        machine.read_shortcut(path)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@pytest.fixture
def link_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Provide a target directory holding ``marker.txt``, a second directory, and a link path that does not exist."""
    target = tmp_path / "target dir"
    target.mkdir()
    (target / "marker.txt").write_text("marker", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    return target, other, tmp_path / "link"


def test_junction_shows_the_target_and_is_recognised_as_a_link(
    machine: System, link_dirs: tuple[Path, Path, Path]
) -> None:
    """A created junction opens the target's contents, reads back its target, and a real directory is no link."""
    target, _, link = link_dirs

    machine.create_junction(link, target)

    assert machine.is_link(link)
    assert machine.read_link(link) == target.resolve()
    assert (link / "marker.txt").read_text(encoding="utf-8") == "marker"
    assert not machine.is_link(target)
    assert machine.read_link(target) is None


def test_junction_is_replaced_atomically_and_a_failed_replacement_keeps_the_old_one(
    machine: System, link_dirs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Re-creating a junction re-points it; a replacement that cannot be made leaves the old junction intact."""
    target, other, link = link_dirs
    machine.create_junction(link, target)

    machine.create_junction(link, other)
    assert machine.read_link(link) == other.resolve()

    with pytest.raises(RuntimeError, match="does not exist"):
        machine.create_junction(link, tmp_path / "absent")
    assert machine.read_link(link) == other.resolve()


def test_junction_refuses_to_replace_a_real_directory_or_file(
    machine: System, link_dirs: tuple[Path, Path, Path]
) -> None:
    """An existing real directory (or file) is never overwritten by a junction, and keeps its contents."""
    target, _, link = link_dirs
    link.mkdir()
    (link / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a junction"):
        machine.create_junction(link, target)

    assert (link / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_removing_a_link_keeps_the_target_and_refuses_real_directories(
    machine: System, link_dirs: tuple[Path, Path, Path]
) -> None:
    """Removing a junction deletes only the link; asked to remove a real directory it refuses and changes nothing."""
    target, _, link = link_dirs
    machine.create_junction(link, target)

    machine.remove_link(link)

    assert not link.exists() and not os.path.lexists(link)
    assert (target / "marker.txt").exists()
    with pytest.raises(ValueError):
        machine.remove_link(target)
    assert (target / "marker.txt").exists()


def test_hardlink_is_a_second_name_for_the_same_file(machine: System, tmp_path: Path) -> None:
    """A hard link shows the file's content, and a change through one name is visible through the other."""
    original = tmp_path / "original.txt"
    original.write_text("one", encoding="utf-8")
    link = tmp_path / "second.txt"

    machine.hardlink(link, original)
    with open(original, "a", encoding="utf-8") as handle:
        handle.write("+two")

    assert link.read_text(encoding="utf-8") == "one+two"


# ---------------------------------------------------------------------------
# Privilege and locations
# ---------------------------------------------------------------------------


def test_elevation_is_reported_as_a_bool(machine: System) -> None:
    """``is_elevated`` answers with a bool on every implementation."""
    assert isinstance(machine.is_elevated(), bool)


def test_a_scratch_directory_is_private_empty_and_deleted_on_exit(machine: System) -> None:
    """`scratch_directory` yields an empty existing folder, two calls give two folders, and the folder and its content are gone afterwards."""
    with machine.scratch_directory("gupkg-contract-") as first, machine.scratch_directory("gupkg-contract-") as second:
        assert first != second and first.is_dir() and list(first.iterdir()) == []
        (first / "nested").mkdir()
        (first / "nested" / "file.txt").write_text("x", encoding="utf-8")

    assert not first.exists() and not second.exists()


def test_the_process_id_is_running_and_an_unused_one_is_not(machine: System) -> None:
    """``process_id`` is a running process on every implementation; a freshly finished child's id is not running."""
    import subprocess
    import sys

    assert machine.process_is_running(machine.process_id())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not machine.process_is_running(child.pid)


@pytest.mark.parametrize("flag", [True, False], ids=["elevated", "ordinary"])
def test_fake_reports_the_configured_elevation(tmp_path: Path, flag: bool) -> None:
    """The fake's elevation flag is what ``is_elevated`` reports, so elevated and ordinary runs can be staged."""
    assert FakeSystem(tmp_path / "machine", elevated=flag).is_elevated() is flag


@pytest.mark.windows_integration
@windows_only
def test_windows_elevation_matches_an_independent_reading_of_the_token() -> None:
    """The Windows implementation agrees with ``IsUserAnAdmin`` about whether this test process is elevated.

    Windows treats an unreadable signal as elevated (fail closed), which a
    working machine never triggers, so the two answers are equal here.
    """
    import ctypes

    assert WindowsSystem().is_elevated() == bool(ctypes.windll.shell32.IsUserAnAdmin())


@pytest.mark.windows_integration
@windows_only
def test_windows_distinguishes_admin_only_folders_from_user_writable_ones(tmp_path: Path) -> None:
    """``System32`` is admin-only, a pytest temporary folder is not, and the refusal names a reason."""
    system = WindowsSystem()
    system32 = Path(os.environ["SystemRoot"]) / "System32"

    assert system.directory_is_admin_only(system32)
    assert system.directory_untrusted_reason(system32) is None
    assert not system.directory_is_admin_only(tmp_path)
    assert system.directory_untrusted_reason(tmp_path)


@pytest.mark.windows_integration
@windows_only
def test_windows_judges_a_single_file_by_its_own_permissions(tmp_path: Path) -> None:
    """A user-writable file is untrusted, and a file of an admin-only folder is trusted (the per-file check of the hook rule)."""
    system = WindowsSystem()
    mine = tmp_path / "script.py"
    mine.write_text("x", encoding="utf-8")
    system_file = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"

    assert system.directory_untrusted_reason(mine)
    assert system.directory_untrusted_reason(system_file) is None


def test_machine_and_user_scopes_have_different_shortcut_and_bin_folders(machine: System) -> None:
    """Each scope has its own Start Menu ``opt`` folder and its own ``bin`` folder."""
    user, machine_scope = machine.scope_folders(Scope.USER), machine.scope_folders(Scope.MACHINE)

    assert user.shortcut_root != machine_scope.shortcut_root
    assert user.bin_dir != machine_scope.bin_dir
    assert user.shortcut_root.parts[-1] == "opt" and machine_scope.shortcut_root.parts[-1] == "opt"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


def test_fetch_writes_the_body_and_reports_progress_from_zero_to_the_end(
    machine: System, served, tmp_path: Path
) -> None:
    """A download writes every byte, announces the size first, and ends at the full count."""
    body = bytes(range(256)) * 1200
    url = served(body)
    seen: list[tuple[int, int | None]] = []

    count = machine.fetch(url, {"User-Agent": "gupkg-test"}, tmp_path / "out.bin", lambda *a: seen.append(a))

    assert count == len(body)
    assert (tmp_path / "out.bin").read_bytes() == body
    assert seen[0] == (0, len(body))
    assert seen[-1] == (len(body), len(body))
    assert [received for received, _ in seen] == sorted(received for received, _ in seen)


def test_fetch_text_returns_decoded_text_and_an_unreachable_url_is_an_oserror(
    machine: System, served, tmp_path: Path
) -> None:
    """Text comes back decoded as UTF-8, and a URL that cannot be served raises ``OSError`` instead of returning."""
    url = served("caf\u00e9 \u4e2d\u6587".encode("utf-8"))

    assert machine.fetch_text(url, {"Accept": "text/plain"}) == "caf\u00e9 \u4e2d\u6587"
    missing = (tmp_path / "missing-file").as_uri() if not isinstance(machine, FakeSystem) else "https://example.invalid/none"
    with pytest.raises(OSError):
        machine.fetch_text(missing)
    with pytest.raises(OSError):
        machine.fetch(missing, None, tmp_path / "never.bin")


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


@pytest.mark.windows_integration
@windows_only
def test_windows_run_returns_status_and_streams_each_line_of_both_streams(tmp_path: Path) -> None:
    """A child's exit status and both output streams are returned, and every line reaches ``on_line`` as it is read."""
    script = (
        "import sys; print('out one'); print('err one', file=sys.stderr); "
        "print('out two'); sys.exit(3)"
    )
    lines: list[str] = []

    result = WindowsSystem().run([sys.executable, "-c", script], tmp_path, None, lines.append)

    assert result.returncode == 3
    assert result.stdout.splitlines() == ["out one", "out two"]
    assert result.stderr.splitlines() == ["err one"]
    assert sorted(lines) == ["err one", "out one", "out two"]


@pytest.mark.windows_integration
@windows_only
def test_windows_run_feeds_standard_input_and_runs_scripts_through_their_interpreter(tmp_path: Path) -> None:
    """Input text reaches a ``.cmd`` script that is started by path, which runs in the directory it was given."""
    script = tmp_path / "echo input.cmd"
    script.write_text("@echo off\r\nset /p line=\r\necho got:%line%\r\ncd\r\n", encoding="utf-8")

    result = WindowsSystem().run([str(script)], tmp_path, None, None, input_text="hello\n")

    assert result.returncode == 0
    assert result.stdout.splitlines()[0] == "got:hello"
    assert Path(result.stdout.splitlines()[1]).resolve() == tmp_path.resolve()


@pytest.mark.windows_integration
@windows_only
def test_windows_run_kills_a_child_that_outlives_its_timeout(tmp_path: Path) -> None:
    """A child that runs past the timeout is killed and the caller gets a ``TimeoutError``."""
    with pytest.raises(TimeoutError):
        WindowsSystem().run([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, None, None, timeout=0.5)


# ---------------------------------------------------------------------------
# Processes under a watcher (System.run(watch=...))
# ---------------------------------------------------------------------------


def _watched_command(machine: System, script: str, output: str = "") -> list[str]:
    """Return a command for *machine* that behaves like the Python *script* (a double on the fake).

    The fake runs no real child: it gets a double that finishes at once with *output* and, like the
    contract says, asks the watcher once after the child has run.
    """
    if isinstance(machine, FakeSystem):
        machine.processes["probe"] = lambda call: ProcessResult(0, output)
        return ["probe"]
    return [sys.executable, "-c", script]


def test_a_watcher_that_names_a_reason_aborts_the_run_with_that_reason(machine: System, tmp_path: Path) -> None:
    """``watch`` returning text raises ``ProcessAborted`` carrying it; on the real system the child is killed first."""
    command = _watched_command(machine, "import time; time.sleep(30)")
    started = time.monotonic()

    with pytest.raises(ProcessAborted, match="too much output"):
        machine.run(command, tmp_path, None, None, watch=lambda: "too much output")

    assert time.monotonic() - started < 15


def test_a_watcher_that_returns_nothing_leaves_the_run_unchanged(machine: System, tmp_path: Path) -> None:
    """``watch`` returning ``None`` gives the normal status and output, and ``on_line`` still streams each line."""
    command = _watched_command(machine, "print('one'); print('two')", "one\ntwo\n")
    lines: list[str] = []
    calls: list[int] = []

    result = machine.run(command, tmp_path, None, lines.append, watch=lambda: calls.append(1))

    # A child shorter than one poll may never be watched, so the number of polls is not part of the contract.
    assert result.returncode == 0 and result.stdout.split() == ["one", "two"] and lines == ["one", "two"]
    assert len(calls) < 50


@pytest.mark.windows_integration
@windows_only
def test_windows_watcher_is_polled_while_the_child_runs_and_the_timeout_still_applies(tmp_path: Path) -> None:
    """A slow child sees several polls; with a watcher that never objects, ``timeout`` still raises ``TimeoutError``."""
    polls: list[float] = []

    with pytest.raises(TimeoutError):
        WindowsSystem().run(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, None, None,
            timeout=1.0, watch=lambda: polls.append(time.monotonic()),
        )

    assert len(polls) >= 3


@pytest.mark.windows_integration
@windows_only
def test_windows_aborted_child_is_gone_and_so_is_a_grandchild_it_started(tmp_path: Path) -> None:
    """After an abort neither the child nor the helper process it started is still running to write its marker file."""
    marker = tmp_path / "survivor.txt"
    grandchild = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('alive')"
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
    )
    state = {"polls": 0}

    def watch() -> str | None:
        """Let the child start its helper, then ask for the abort."""
        state["polls"] += 1
        return "stop" if state["polls"] >= 3 else None

    with pytest.raises(ProcessAborted):
        WindowsSystem().run([sys.executable, "-c", script], tmp_path, None, None, watch=watch)
    time.sleep(5)

    assert not marker.exists()


def test_a_watcher_that_raises_stops_the_run_at_once_and_its_error_reaches_the_caller(machine: System, tmp_path: Path) -> None:
    """An exception from ``watch`` propagates and the child is killed (a leaked child would keep the call for 30 s)."""
    command = _watched_command(machine, "import time; time.sleep(30)")
    calls = {"count": 0}

    def watch() -> str | None:
        """Fail on the second look, so the child is running when it is raised."""
        calls["count"] += 1
        if calls["count"] >= 2 or isinstance(machine, FakeSystem):
            raise RuntimeError("watcher broke")
        return None

    started = time.monotonic()

    with pytest.raises(RuntimeError, match="watcher broke"):
        machine.run(command, tmp_path, None, None, watch=watch)

    assert time.monotonic() - started < 15


GRANDCHILD_SCRIPT = (
    "import subprocess, sys; "
    "subprocess.Popen([sys.executable, '-c', "
    "\"import time, pathlib; time.sleep(3); pathlib.Path(r'%s').write_text('alive')\"]); "
)


@pytest.mark.windows_integration
@windows_only
@pytest.mark.parametrize("how", [pytest.param("raises", id="the-final-poll-raises"), pytest.param("aborts", id="the-final-poll-names-a-reason")])
def test_a_helper_that_outlived_a_finished_child_is_killed_when_the_last_watcher_poll_fails(tmp_path: Path, how: str) -> None:
    """The child exits at once leaving a helper; the final poll fails: the helper is killed (job object) and the call is quick."""
    marker = tmp_path / "survivor.txt"
    script = GRANDCHILD_SCRIPT % marker.as_posix()
    state = {"polls": 0}

    def watch() -> str | None:
        """Let the child start its helper and finish, then fail on the final look."""
        state["polls"] += 1
        time.sleep(0.5) if state["polls"] == 1 else None
        if how == "raises":
            raise RuntimeError("watcher broke")
        return "stop"

    started = time.monotonic()
    with pytest.raises(RuntimeError if how == "raises" else ProcessAborted):
        WindowsSystem().run([sys.executable, "-c", script], tmp_path, None, None, watch=watch)
    elapsed = time.monotonic() - started
    time.sleep(5)

    assert not marker.exists() and elapsed < 3


@pytest.mark.windows_integration
@windows_only
def test_a_helper_started_by_a_normal_run_is_left_running_when_nothing_went_wrong(tmp_path: Path) -> None:
    """The job object kills nothing on a clean finish: a helper that is meant to outlive its parent keeps running."""
    marker = tmp_path / "survivor.txt"
    script = GRANDCHILD_SCRIPT % marker.as_posix()

    result = WindowsSystem().run([sys.executable, "-c", script], tmp_path, None, None)
    time.sleep(6)

    assert result.returncode == 0 and marker.read_text() == "alive"


@pytest.mark.windows_integration
@windows_only
def test_a_child_that_never_reads_its_input_cannot_keep_the_call_past_its_timeout(tmp_path: Path) -> None:
    """Two megabytes of input to a child that sleeps: the timeout still fires (input is fed from its own thread)."""
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        WindowsSystem().run(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, None, None,
            input_text="x" * 2_000_000, timeout=1.0,
        )

    assert time.monotonic() - started < 8


REQUEST = '{"op":"search","pattern":"(?P<v>\\\\d+)","flags":32,"texts":["tool 42"],"limit":1,"cap":1000}'
HANGING = '{"op":"search","pattern":"(a+)+$","flags":32,"texts":["%s!"],"limit":1,"cap":1000}' % ("a" * 40)


def _alive(pid: int) -> bool:
    """Return whether a process with this id is running (``tasklist`` lists it)."""
    listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=30)
    return f'"{pid}"' in listing.stdout


def test_the_regex_worker_answers_a_request_in_isolation(machine: System, tmp_path: Path) -> None:
    """A benign request gets an `ok` answer with the match; the first line on standard error is the worker's pid."""
    lines: list[str] = []

    result = machine.regex_worker(REQUEST, 20, on_line=lines.append)

    # The pid line (standard error) and the answer (standard output) reach the callback from two reader threads, in either order.
    assert result.returncode == 0 and any(line.startswith("pid ") for line in lines)
    assert '"ok":true' in result.stdout and '"42"' in result.stdout


def test_the_regex_worker_is_killed_at_its_deadline_and_is_gone(machine: System) -> None:
    """A catastrophic pattern raises TimeoutError about a second after the worker is up, and its process no longer exists."""
    lines: list[str] = []
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        machine.regex_worker(HANGING, 1.0, on_line=lines.append)

    assert time.monotonic() - started < 1.0 + 0.5 + 4  # the deadline, a poll, and a slow process start
    pid = int(next(line for line in lines if line.startswith("pid ")).split()[1])
    time.sleep(0.5)
    assert not _alive(pid)


PROBE = (
    "import json, os, sys\n"
    "sys.stderr.write('pid %d\\n' % os.getpid()); sys.stderr.flush()\n"
    "print(json.dumps({'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site, 'no_bytecode': sys.flags.dont_write_bytecode,\n"
    "                  'no_user_site': sys.flags.no_user_site, 'env': sorted(os.environ), 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}))\n"
)


@pytest.mark.windows_integration
@windows_only
def test_the_regex_worker_runs_isolated_with_a_minimal_environment_in_an_empty_temporary_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker is started with -I -s -B -S, only SystemRoot in its environment, and an empty temporary folder as cwd."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    monkeypatch.setattr("gupkg.system.windows._REGEX_WORKER", probe)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    result = WindowsSystem().regex_worker("the request", 20)

    seen = json.loads(result.stdout)
    assert seen["isolated"] == seen["no_site"] == seen["no_bytecode"] == seen["no_user_site"] == 1
    assert [name.upper() for name in seen["env"]] == ["SYSTEMROOT"] and seen["stdin"] == "the request"
    assert "gupkg-regex-" in seen["cwd"] and "secret-token" not in result.stdout


@pytest.mark.windows_integration
@windows_only
def test_the_regex_deadline_counts_from_the_pid_line_not_from_the_process_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worker that is slow to say it is up (1.5 s) but quick afterwards is not timed out by a 0.5 s deadline."""
    slow = tmp_path / "slow_start.py"
    slow.write_text(
        "import os, sys, time\ntime.sleep(1.5)\nsys.stderr.write('pid %d\\n' % os.getpid()); sys.stderr.flush()\n"
        "print('{\"ok\":true,\"names\":{},\"results\":[[]]}')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("gupkg.system.windows._REGEX_WORKER", slow)

    result = WindowsSystem().regex_worker(REQUEST, 0.5)

    assert result.returncode == 0 and '"ok":true' in result.stdout


@pytest.mark.windows_integration
@windows_only
def test_a_worker_that_overruns_its_start_limit_is_reported_as_not_started_with_that_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The error names the start limit, not the (shorter) matching deadline."""
    silent = tmp_path / "silent2.py"
    silent.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr("gupkg.system.windows._REGEX_WORKER", silent)
    monkeypatch.setattr("gupkg.system.windows._WORKER_START_SECONDS", 1.0)

    with pytest.raises(WorkerDidNotStart, match="did not start within 1 seconds"):
        WindowsSystem().regex_worker(REQUEST, 0.5)


@pytest.fixture
def job_closes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the calls of ``_ProcessJob.close`` (one per job handle released)."""
    closed: list[int] = []
    original = _ProcessJob.close

    def spy(self: _ProcessJob) -> None:
        """Record the release, then do it."""
        closed.append(1)
        original(self)

    monkeypatch.setattr(_ProcessJob, "close", spy)
    return closed


@pytest.mark.windows_integration
@windows_only
@pytest.mark.parametrize("path", ["finishes", "times-out", "watcher-raises", "worker-deadline"])
def test_the_job_handle_is_closed_on_every_path(job_closes: list[int], tmp_path: Path, path: str) -> None:
    """A clean finish, a timeout, a raising watcher and a regex deadline each release their job handle exactly once."""
    system = WindowsSystem()
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]

    if path == "finishes":
        system.run([sys.executable, "-c", "pass"], tmp_path)
    elif path == "times-out":
        with pytest.raises(TimeoutError):
            system.run(sleeper, tmp_path, timeout=0.5)
    elif path == "watcher-raises":
        with pytest.raises(RuntimeError):
            system.run(sleeper, tmp_path, watch=lambda: (_ for _ in ()).throw(RuntimeError("broke")))
    else:
        with pytest.raises(TimeoutError):
            system.regex_worker(HANGING, 0.5)

    assert job_closes == [1]


@pytest.mark.windows_integration
@windows_only
def test_a_failure_to_create_the_job_kills_the_child_and_reaches_the_caller(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If the job object code raises, the child is killed (no leaked process or reader thread) and the error is raised."""
    lines: list[str] = []

    def broken(process: object, *, kill_on_close: bool = False) -> None:
        """Fail the way a ctypes problem would."""
        raise OSError("job objects are unavailable")

    monkeypatch.setattr(_ProcessJob, "around", broken)
    started = time.monotonic()

    with pytest.raises(OSError, match="job objects are unavailable"):
        WindowsSystem().run(
            [sys.executable, "-c", "import os, time; print(os.getpid(), flush=True); time.sleep(30)"], tmp_path, None, lines.append
        )

    assert time.monotonic() - started < 10
    time.sleep(0.5)
    assert not _alive(int(lines[0])) if lines else True


@pytest.mark.windows_integration
@windows_only
def test_the_regex_worker_runs_in_a_kill_on_close_job_and_an_ordinary_child_does_not(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only the regex worker's job ends its processes when the handle closes; a normal run's helpers may outlive it."""
    asked: list[bool] = []
    original = _ProcessJob.around.__func__

    def spy(cls: type, process: object, *, kill_on_close: bool = False) -> object:
        """Record the flag, then create the job as usual."""
        asked.append(kill_on_close)
        return original(cls, process, kill_on_close=kill_on_close)

    monkeypatch.setattr(_ProcessJob, "around", classmethod(spy))

    WindowsSystem().regex_worker(REQUEST, 20)
    WindowsSystem().run([sys.executable, "-c", "pass"], tmp_path)

    assert asked == [True, False]


@pytest.mark.windows_integration
@windows_only
def test_closing_a_kill_on_close_job_ends_the_process_in_it(tmp_path: Path) -> None:
    """With kill_on_close the process dies when the job handle is closed (as it would if the parent were killed); without it, it lives."""
    mortal = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    spared = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _ProcessJob.around(mortal, kill_on_close=True).close()
        _ProcessJob.around(spared).close()

        assert mortal.wait(timeout=10) is not None
        time.sleep(0.5)
        assert spared.poll() is None
    finally:
        spared.kill()
        mortal.kill()
        spared.wait()
        mortal.wait()


@pytest.mark.windows_integration
@windows_only
def test_a_regex_worker_that_never_starts_is_stopped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A worker that does not report within the start limit is killed and reported as a timeout, not waited for."""
    silent = tmp_path / "silent.py"
    silent.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")  # never prints the pid line
    monkeypatch.setattr("gupkg.system.windows._REGEX_WORKER", silent)
    monkeypatch.setattr("gupkg.system.windows._WORKER_START_SECONDS", 1.0)
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="did not start"):
        WindowsSystem().regex_worker(REQUEST, 20)

    assert time.monotonic() - started < 15


def test_a_child_that_finishes_at_once_is_still_judged_by_one_last_look_of_the_watcher(machine: System, tmp_path: Path) -> None:
    """A watcher that only objects after the child ended still aborts the run, on the fake and on Windows alike."""
    command = _watched_command(machine, "pass")

    with pytest.raises(ProcessAborted, match="late objection"):
        machine.run(command, tmp_path, None, None, watch=lambda: "late objection")
