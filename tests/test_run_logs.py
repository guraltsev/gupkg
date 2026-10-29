"""Cover the log files a run leaves: placement, names, content, retention, failure and name safety.

Real: the command line, the engine, the event stream, the console renderer and the log files, written below pytest
temporary directories (junctions and hard links included). Mocked: only the machine boundary, as a ``FakeSystem`` (and its
process double for ``git``). Out of scope: real UAC and admin-only permissions (see ``tests/manual_smoke.md``) and the UI
programs. The ``[logs]`` retention that the command line reads from the settings is covered in ``test_cli_tools.py``.
"""

from __future__ import annotations

import json
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gupkg import cli
from gupkg.report.events import PackageEnd, PackageStart, PhaseEnd, PhaseStart, RunEnd, RunStart, Step, Warning
from gupkg.report.logfile import RunLog, list_run_logs, running_gupkg_logs_base, validate_log_file_name
from gupkg.report.session import RunSession
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ProcessResult
from tests.cli_support import CliResult, CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

FIXED_TIME = datetime(2026, 10, 22, 10, 15, 0, tzinfo=timezone.utc)


class _Collector:
    """Collect every event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        self.events.append(event)


def _world(tmp_path: Path, name: str = "Example", **options) -> CliWorld:
    """Return a fake machine whose user root holds the downloadable package *name* 1.0.0 (``options`` go to ``publish``)."""
    world = CliWorld(tmp_path)
    world.publish(name, "1.0.0", **options)
    return world


ENV = '[[env]]\nname = "EXAMPLE_READY"\nvalue = "yes"\n'


def _logs(directory: Path) -> Path:
    """Return the ``.gupkg\\logs`` folder of a root or package."""
    return directory / ".gupkg" / "logs"


def _run_files(directory: Path, suffix: str) -> list[Path]:
    """Return the run files with *suffix* in a log folder (not ``latest.log`` or caller-named files)."""
    return sorted(path for path in _logs(directory).glob(f"2*{suffix}"))


# ---------------------------------------------------------------------------
# The log writer
# ---------------------------------------------------------------------------


def test_log_files_are_named_by_utc_time_command_and_package(tmp_path: Path) -> None:
    """A run's text log and JSONL log are named <UTC stamp>-<command>[-<package>], the run log carrying the package of a single-package run."""
    root = tmp_path / "opt"
    session = RunSession(
        command="install", argv=["install", "x"], mode="user", elevated=False, version="1.0.0",
        reporters=[], clock=lambda: FIXED_TIME,
    )

    with session:
        from gupkg.report.events import package_scope

        with package_scope("firefox", root / "firefox", root=root, sole=True):
            pass
        session.finish("done", 0)

    assert [p.name for p in _run_files(root, ".log")] == ["20261022T101500Z-install-firefox.log"]
    assert [p.name for p in _run_files(root, ".jsonl")] == ["20261022T101500Z-install-firefox.jsonl"]
    assert [p.name for p in _run_files(root / "firefox", ".log")] == ["20261022T101500Z-install.log"]
    assert [p.name for p in _run_files(root / "firefox", ".jsonl")] == ["20261022T101500Z-install.jsonl"]


def test_two_runs_in_the_same_second_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """Runs that start in the same second get distinct file names instead of sharing one."""
    for _ in range(2):
        session = RunSession(
            command="check", argv=[], mode="user", elevated=False, version="1.0.0",
            reporters=[], fallback_base=tmp_path, clock=lambda: FIXED_TIME,
        )
        with session:
            session.finish("done", 0)

    names = [p.name for p in _run_files(tmp_path, ".log")]
    assert names == ["20261022T101500Z-check-2.log", "20261022T101500Z-check.log"]


def test_header_and_footer_record_the_run_and_its_exit_code(tmp_path: Path) -> None:
    """The log starts with version, argv, mode, elevation and settings layers and ends with the exit code."""
    session = RunSession(
        command="install", argv=["--scope", "user", "install", "x"], mode="user", elevated=True,
        version="1.2.3", reporters=[], fallback_base=tmp_path, clock=lambda: FIXED_TIME,
        settings_layers=("defaults", "user"),
    )
    with session:
        session.finish("install: changed", 3)

    text = _run_files(tmp_path, ".log")[0].read_text(encoding="utf-8").splitlines()
    assert text[:7] == [
        "# gupkg 1.2.3", "# command: install", "# argv: --scope user install x", "# mode: user",
        "# elevated: yes", "# settings layers: defaults, user", "# started: 2026-10-22T10:15:00+00:00",
    ]
    assert text[-2] == "# exit code: 3" and text[-1].startswith("# finished: ")


def test_log_lines_are_flushed_while_the_run_is_still_open(tmp_path: Path) -> None:
    """Each event is on disk, in both files, before the run ends, so a crash or a follower loses nothing."""
    log = RunLog("install", warn=lambda text: None, clock=lambda: FIXED_TIME)
    log.handle(RunStart("install", ("install",), "user", False, "1.0.0"))
    log.bind(tmp_path)

    log.handle(PhaseStart("Fetch"))
    log.handle(Step("downloading"))

    text_path, json_path = _run_files(tmp_path, ".log")[0], _run_files(tmp_path, ".jsonl")[0]
    assert "==> Fetch" in text_path.read_text(encoding="utf-8") and "downloading" in text_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in json_path.read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["run_start", "phase_start", "step"]
    log.close()


def test_latest_log_is_a_copy_of_the_newest_text_log(tmp_path: Path) -> None:
    """latest.log holds the text of the most recent run, replacing the previous run's copy."""
    for stamp in (FIXED_TIME, FIXED_TIME + timedelta(minutes=5)):
        session = RunSession(
            command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
            fallback_base=tmp_path, clock=lambda stamp=stamp: stamp,
        )
        with session:
            session.finish(f"summary of {stamp:%H%M}", 0)

    newest = _run_files(tmp_path, ".log")[-1]
    assert newest.name.startswith("20261022T102000Z")
    assert (_logs(tmp_path) / "latest.log").read_text(encoding="utf-8") == newest.read_text(encoding="utf-8")


def test_retention_keeps_the_newest_runs_and_drops_aged_ones_when_a_run_starts(tmp_path: Path) -> None:
    """Pruning at run start leaves keep_runs runs including the new one, newest first, and spares other files (the age rule has its own test)."""
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    for index in range(10):
        stamp = (FIXED_TIME - timedelta(hours=index + 1)).strftime("%Y%m%dT%H%M%SZ")
        for suffix in (".log", ".jsonl"):
            (logs / f"{stamp}-install{suffix}").write_text("old", encoding="utf-8")
    (logs / "notes.txt").write_text("keep me", encoding="utf-8")
    (logs / "events.jsonl").write_text("caller file", encoding="utf-8")

    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME, keep_runs=4, keep_days=10000,
    )
    with session:
        session.finish("done", 0)

    runs = sorted({p.stem for p in logs.glob("2*")})
    assert len(runs) == 4
    assert runs[-1] == "20261022T101500Z-check"
    assert runs[:3] == [(FIXED_TIME - timedelta(hours=h)).strftime("%Y%m%dT%H%M%SZ") + "-install" for h in (3, 2, 1)]
    assert (logs / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (logs / "events.jsonl").read_text(encoding="utf-8") == "caller file"


def test_an_unwritable_log_location_gives_one_warning_and_the_run_still_succeeds(tmp_path: Path) -> None:
    """When the root's log directory cannot be created, the run warns exactly once, succeeds, and the package log is still written."""
    world = _world(tmp_path)
    (world.user / ".gupkg").mkdir(parents=True)
    (world.user / ".gupkg" / "logs").write_text("a file where the log directory should be", encoding="utf-8")

    result = world.cli("install", "Example")

    output = result.out + result.err
    assert result.code == 0
    assert len(re.findall(r"WARNING: Cannot write the run log", output)) == 1
    assert len(_run_files(world.user / "Example", ".log")) == 1
    assert "==> Done" in result.out and result.out.rstrip().splitlines()[-1].startswith("Log of Example:")


def test_a_log_failure_in_the_middle_of_a_run_is_one_warning_not_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log file whose disk fills up mid-run is switched off with one warning, and the other reporters still get every event."""
    import builtins

    from gupkg.report import logfile
    from gupkg.report.events import emit

    disk_full = {"armed": False}

    class Disk:
        """Wrap a real log file; writes fail once the disk is "full"."""

        def __init__(self, handle) -> None:
            self.handle = handle

        def write(self, text):
            if disk_full["armed"]:
                raise OSError(28, "No space left on device")
            return self.handle.write(text)

        def __getattr__(self, name):
            return getattr(self.handle, name)

    monkeypatch.setattr(logfile, "open", lambda *args, **kwargs: Disk(builtins.open(*args, **kwargs)), raising=False)
    collector = _Collector()
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[collector],
        clock=lambda: FIXED_TIME,
    )

    with session:
        session.bind(tmp_path)
        emit(Step("before"))
        disk_full["armed"] = True
        emit(Step("after one"))
        emit(Step("after two"))
        session.finish("done", 0)

    warnings = [e for e in collector.events if isinstance(e, Warning)]
    assert len(warnings) == 1 and "No space left on device" in warnings[0].text
    assert [e.text for e in collector.events if isinstance(e, Step)] == ["before", "after one", "after two"]
    assert isinstance(collector.events[-1], RunEnd)
    assert "before" in _run_files(tmp_path, ".log")[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Names that a caller supplies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("..\\evil.jsonl", id="parent-backslash"),
        pytest.param("../evil.jsonl", id="parent-slash"),
        pytest.param("sub\\events.jsonl", id="subfolder-backslash"),
        pytest.param("sub/events.jsonl", id="subfolder-slash"),
        pytest.param("C:evil.jsonl", id="drive-relative"),
        pytest.param("C:\\evil.jsonl", id="drive-absolute"),
        pytest.param("\\\\server\\share\\evil.jsonl", id="unc"),
        pytest.param("events.jsonl:hidden", id="alternate-data-stream"),
        pytest.param("..", id="dotdot"),
        pytest.param(".", id="dot"),
        pytest.param("", id="empty"),
        pytest.param("events.jsonl.", id="trailing-dot"),
        pytest.param("events.jsonl ", id="trailing-space"),
        pytest.param(" events.jsonl", id="leading-space"),
        pytest.param("CON", id="device-con"),
        pytest.param("nul.jsonl", id="device-with-extension"),
        pytest.param("COM1.txt", id="device-com1"),
        pytest.param("lpt9", id="device-lpt9"),
        pytest.param("run.log", id="text-log-suffix"),
        pytest.param("latest.LOG", id="latest-log"),
        pytest.param("ev\u00e9nts.jsonl", id="non-ascii"),
        pytest.param("ev\uff3cil.jsonl", id="fullwidth-backslash"),
        pytest.param("a*.jsonl", id="wildcard"),
        pytest.param("x" * 101, id="too-long"),
        pytest.param("events\x00.jsonl", id="nul-character"),
    ],
)
def test_events_file_names_that_are_not_plain_file_names_are_refused(name: str) -> None:
    """A name with a separator, ``..``, a drive, a stream, a device name, a trailing dot or space, or odd characters is refused."""
    with pytest.raises(ValueError):
        validate_log_file_name(name)


def test_plain_events_file_names_are_accepted() -> None:
    """Ordinary file names such as events.jsonl are accepted unchanged."""
    assert [validate_log_file_name(n) for n in ("events.jsonl", "run-1_events.ndjson", "e")] == [
        "events.jsonl", "run-1_events.ndjson", "e"
    ]


def test_the_cli_refuses_an_events_file_with_a_path_and_writes_nothing(tmp_path: Path) -> None:
    """A path as --events-file is a usage error (exit 2) before any work happens, and no log or events file is created anywhere."""
    world = _world(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    absolute = world.cli("install", "Example", "--events-file", str(elsewhere / "events.jsonl"))
    parent = world.cli("install", "Example", "--events-file", "..\\events.jsonl")

    assert absolute.code == parent.code == 2
    assert "plain file name" in absolute.err
    assert list(elsewhere.iterdir()) == []
    assert not (world.user / ".gupkg").exists() and not (world.user / "Example" / ".gupkg").exists()
    assert not (world.user / "Example" / "current").exists(), "nothing was installed"


def test_a_junction_planted_as_the_log_directory_cannot_redirect_the_writes(tmp_path: Path) -> None:
    """If .gupkg\\logs is a junction to a folder outside the package, nothing is written through it and the run warns once and succeeds."""
    world = _world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (world.user / "Example" / ".gupkg").mkdir(parents=True)
    world.fake.create_junction(world.user / "Example" / ".gupkg" / "logs", outside)

    result = world.cli("install", "Example", "--events-file", "events.jsonl")

    output = result.out + result.err
    assert result.code == 0
    assert list(outside.iterdir()) == []
    assert len(re.findall(r"WARNING: Cannot write the log of package Example", output)) == 1
    assert len(_run_files(world.user, ".log")) == 1, "the root's own log is unaffected"


def test_an_events_file_name_that_is_a_junction_inside_the_log_directory_is_not_followed(tmp_path: Path) -> None:
    """A junction planted under the events-file name is refused rather than written through, so the folder it points to stays empty."""
    world = _world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = _logs(world.user / "Example")
    logs.mkdir(parents=True)
    world.fake.create_junction(logs / "events.jsonl", outside)

    result = world.cli("install", "Example", "--events-file", "events.jsonl")

    assert result.code == 0
    assert list(outside.iterdir()) == []
    assert "WARNING: Cannot write the log of package Example" in result.out + result.err


# ---------------------------------------------------------------------------
# What the CLI leaves behind
# ---------------------------------------------------------------------------


def test_an_install_leaves_package_logs_a_linking_run_log_and_nothing_in_appdata(tmp_path: Path) -> None:
    """An install writes .log and .jsonl in the package's logs, a run log without package output that links them, names both in the summary, and writes nothing under AppData or ProgramData."""
    world = _world(tmp_path, tables=ENV)
    appdata, programdata = Path(world.env["APPDATA"]), Path(world.env["ProgramData"])

    result = world.cli("install", "Example")

    output = result.out + result.err
    root = world.user
    assert result.code == 0, output
    [package_log] = _run_files(root / "Example", ".log")
    [package_events] = _run_files(root / "Example", ".jsonl")
    [run_log] = _run_files(root, ".log")
    assert package_log.name.endswith("-install.log") and run_log.name.endswith("-install-Example.log")
    package_text = package_log.read_text(encoding="utf-8")
    assert "==> Resolve" in package_text and f"  {world.scope.describe()}" in package_text and "==> Integrate" in package_text

    # The run log holds global facts and a link, and none of the package's output.
    run_text = run_log.read_text(encoding="utf-8")
    assert f"Package Example: {package_log}" in run_text
    assert f"# scope: {world.scope.describe()}" in run_text and "==> Resolve" not in run_text and run_text.count("user root") == 1
    run_records = [json.loads(line) for line in _run_files(root, ".jsonl")[0].read_text(encoding="utf-8").splitlines()]
    assert not {"phase_start", "step"} & {r["event"] for r in run_records}
    [link] = [r for r in run_records if r["event"] == "log_link"]
    assert (link["log"], link["events"]) == (str(package_log), str(package_events))

    # The summary names every log and is the last thing printed.
    tail = result.out.rstrip().splitlines()[-2:]
    assert tail == [f"Run log: {run_log}", f"Log of Example: {package_log}"]
    assert not appdata.exists() and not programdata.exists()

    # The receipt describes the App only: it does not name the log of the run.
    receipt_file = next((root / "Example").glob("*/App.info"))
    assert "run" not in tomllib.loads(receipt_file.read_text(encoding="utf-8"))


def test_redirected_install_output_is_plain_with_phase_headers_and_a_final_summary(tmp_path: Path) -> None:
    """With output redirected, an install prints ==> phase headers, no ANSI codes and no carriage returns, and one final progress line per download."""
    world = _world(tmp_path, members={"tool.exe": "payload"})

    result = world.cli("install", "Example")

    output = result.out + result.err
    assert result.code == 0
    assert "\x1b" not in output and "\r" not in output
    lines = output.splitlines()
    assert "==> Resolve" in lines and "==> Fetch" in lines
    assert len([line for line in lines if "Example-1.0.0.zip" in line and "(100%)" in line]) == 1
    assert lines.index("==> Done") > lines.index("==> Fetch")


def test_events_file_appears_in_the_run_and_package_log_directories(tmp_path: Path) -> None:
    """--events-file events.jsonl writes the event stream under that name in the root's and the package's log directories."""
    world = _world(tmp_path)
    root = world.user

    assert world.cli("install", "Example", "--events-file", "events.jsonl").code == 0

    package_lines = (_logs(root / "Example") / "events.jsonl").read_text(encoding="utf-8").splitlines()
    run_lines = (_logs(root) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["event"] == "phase_start" for line in package_lines)
    assert not any(json.loads(line)["event"] == "phase_start" for line in run_lines)
    assert json.loads(run_lines[-1])["event"] == "run_end"
    assert set(tmp_path.rglob("events.jsonl")) == {_logs(root) / "events.jsonl", _logs(root / "Example") / "events.jsonl"}


def test_a_failing_git_clone_shows_gits_error_lines_in_the_unpack_phase_of_console_and_log(tmp_path: Path) -> None:
    """A git clone that fails prints git's own error lines inside the Fetch phase, on the console and in the package log, and the install fails."""
    def git(call) -> ProcessResult:
        """Answer the git command that was started."""
        return ProcessResult(128, "", "Cloning into 'App.new'...\nfatal: unable to access 'https://example.invalid/x.git/': Could not resolve host\n")

    world = CliWorld(tmp_path)
    world.fake.processes["git"] = git
    folder = world.user / "Example" / "1.0.0"
    folder.mkdir(parents=True)
    folder.joinpath("pkg.toml").write_text(
        f'format = 2\n[source]\ndownload_url = "https://example.invalid/x.git"\nkind = "git"\n[source.git]\ncommit = "{"a" * 40}"\n',
        encoding="utf-8",
    )

    result = world.cli("install", "Example")

    output = result.out + result.err
    assert result.code == 3, output
    package_text = _run_files(world.user / "Example", ".log")[0].read_text(encoding="utf-8")
    for text in (output, package_text):
        unpack = text.split("==> Unpack", 1)[1].split("Unpack: failed", 1)[0]
        assert "[git] fatal: unable to access 'https://example.invalid/x.git/': Could not resolve host" in unpack
        assert "[git] Cloning into 'App.new'..." in unpack
    assert "Unpack: failed" in output


def test_each_warning_appears_once_on_the_console_and_once_in_the_log(tmp_path: Path) -> None:
    """A warning that the engine reports live and again in the result is printed once on the console and logged once."""
    world = _world(tmp_path, tables=ENV)
    world.fake.failures["broadcast_environment_change"] = OSError("broadcast refused")

    result = world.cli("install", "Example")

    assert result.code == 0
    live_log = _run_files(world.user / "Example", ".log")[0].read_text(encoding="utf-8")
    for text in (result.out + result.err, live_log):
        assert text.count("could not be broadcast") == 1


def test_a_bundled_gupkg_run_with_no_package_logs_into_its_own_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that involves no package, started by a bundled gupkg, leaves its log in its own package's .gupkg\\logs, and an installed gupkg logs in its root."""
    # Bundled layout: <root>\<package>\<version>\gupkg\gupkg\<module>
    package = tmp_path / "opt" / "MyApp"
    bundled = package / "1.0.0" / "gupkg" / "gupkg"
    bundled.mkdir(parents=True)
    (package / "1.0.0" / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
    assert running_gupkg_logs_base(bundled) == package

    # Installed layout: <root>\gupkg\<version>\App\gupkg\<module>
    installed = tmp_path / "root" / "gupkg" / "1.0.0" / "App" / "gupkg"
    installed.mkdir(parents=True)
    (tmp_path / "root" / "gupkg" / "1.0.0" / "pkg.toml").write_text("", encoding="utf-8")
    assert running_gupkg_logs_base(installed) == tmp_path / "root"

    # A source checkout has neither layout and logs nowhere.
    assert running_gupkg_logs_base(tmp_path / "src" / "gupkg") is None

    # A real run that names no package (an install of a path that is no package) logs in the bundle's package.
    world = CliWorld(tmp_path / "machine")
    monkeypatch.setattr(cli, "running_gupkg_logs_base", lambda: package)
    result = world.cli("install", str(tmp_path / "no-such-package"))

    assert result.code == 2
    [log] = _run_files(package, ".log")
    assert log.name.endswith("-install.log") and "ERROR:" in log.read_text(encoding="utf-8")
    assert result.out.rstrip().splitlines()[-1] == f"Run log: {log}"


# ---------------------------------------------------------------------------
# Several targets in one call
# ---------------------------------------------------------------------------


def test_a_call_with_several_packages_logs_each_one_and_links_them_from_the_run_log(tmp_path: Path) -> None:
    """An update of two packages records each package's phase output in its own log, and the run log links both without their output."""
    world = CliWorld(tmp_path)
    for name in ("alpha", "beta"):
        world.publish(name, "1.0.0")
        assert world.cli("install", name).code == 0
        world.publish(name, "2.0.0")
    user = world.user

    result = world.cli("update", "alpha", "beta", "--yes")

    output = result.out + result.err
    assert result.code == 0, output
    [run_log] = _run_files(user, ".log")[-1:]
    run_text = run_log.read_text(encoding="utf-8")
    for name in ("alpha", "beta"):
        package_log = _run_files(user / name, ".log")[-1]
        package_text = package_log.read_text(encoding="utf-8")
        assert "==> Fetch" in package_text and f"{name}-2.0.0.zip" in package_text
        assert f"Package {name}: {package_log}" in run_text
        assert f"Log of {name}: {package_log}" in output
        other = "beta" if name == "alpha" else "alpha"
        assert f"{other}-2.0.0.zip" not in package_text
    assert "==> Fetch" not in run_text and "-2.0.0.zip" not in run_text


# ---------------------------------------------------------------------------
# Hostile entries in a log directory (the directory of an elevated run is
# admin-only, but a user-owned root has no such protection)
# ---------------------------------------------------------------------------


def _run_install_with_events_file(tmp_path: Path, prepare) -> tuple[Path, CliResult]:
    """Prepare the package's log directory with *prepare(logs)*, then run an install with ``--events-file events.jsonl``."""
    world = _world(tmp_path)
    logs = _logs(world.user / "Example")
    logs.mkdir(parents=True)
    prepare(logs)
    return logs, world.cli("install", "Example", "--events-file", "events.jsonl")


def _victim(tmp_path: Path) -> Path:
    """Create a file outside every log directory that an attack would want to overwrite."""
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    return victim


def test_an_events_file_name_that_is_a_hard_link_to_another_file_is_refused(
    tmp_path: Path,
) -> None:
    """A hard link planted under the events-file name is not opened, so the file it shares stays untouched, and the run warns once and succeeds."""
    import os

    victim = _victim(tmp_path)
    logs, result = _run_install_with_events_file(tmp_path, lambda logs: os.link(victim, logs / "events.jsonl"))

    assert result.code == 0
    assert victim.read_text(encoding="utf-8") == "precious"
    assert (logs / "events.jsonl").read_text(encoding="utf-8") == "precious"
    assert (result.out + result.err).count("WARNING: Cannot write the log of package Example") == 1


@pytest.mark.windows_integration
def test_an_events_file_name_that_is_a_symbolic_link_is_not_followed(
    tmp_path: Path,
) -> None:
    """A symbolic link planted under the events-file name is refused rather than written through."""
    import os

    victim = _victim(tmp_path)

    def plant(logs: Path) -> None:
        try:
            os.symlink(victim, logs / "events.jsonl")
        except OSError:
            pytest.skip("this account may not create symbolic links")

    logs, result = _run_install_with_events_file(tmp_path, plant)

    assert result.code == 0
    assert victim.read_text(encoding="utf-8") == "precious"
    assert "WARNING: Cannot write the log of package Example" in (result.out + result.err)


def test_a_link_planted_between_the_check_and_the_creation_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a hard link appears right after the name was found free, the exclusive creation fails and the victim stays untouched."""
    import os

    from gupkg.report import logfile

    victim = _victim(tmp_path)
    real_lexists = os.path.lexists

    def racing_lexists(path) -> bool:
        free = not real_lexists(path)
        if Path(path).name == "events.jsonl" and free:
            os.link(victim, path)       # the attacker wins the race here
        return not free

    monkeypatch.setattr(logfile.os.path, "lexists", racing_lexists)
    logs, result = _run_install_with_events_file(tmp_path, lambda logs: None)

    assert result.code == 0
    assert victim.read_text(encoding="utf-8") == "precious"
    assert "WARNING: Cannot write the log of package Example" in (result.out + result.err)


def test_an_events_file_that_gains_a_second_name_while_being_created_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file that was linked to another place right after its creation is detected and not used."""
    import builtins
    import os

    from gupkg.report import logfile

    created: list[Path] = []

    def linking_open(path, mode="r", *args, **kwargs):
        handle = builtins.open(path, mode, *args, **kwargs)
        if Path(path).name == "events.jsonl" and mode == "x":
            created.append(tmp_path / f"second-name-{len(created)}.txt")
            os.link(path, created[-1])
        return handle

    monkeypatch.setattr(logfile, "open", linking_open, raising=False)
    logs, result = _run_install_with_events_file(tmp_path, lambda logs: None)

    output = (result.out + result.err)
    assert result.code == 0 and len(created) == 2
    assert "WARNING: Cannot write the log of package Example" in output
    assert "WARNING: Cannot write the run log" in output


def test_a_stale_events_file_from_an_earlier_run_is_replaced(tmp_path: Path) -> None:
    """A plain events file left by an earlier run is replaced, not appended to."""
    logs, result = _run_install_with_events_file(
        tmp_path, lambda logs: (logs / "events.jsonl").write_text("stale\n", encoding="utf-8")
    )

    assert result.code == 0
    text = (logs / "events.jsonl").read_text(encoding="utf-8")
    assert "OLD-RUN-MARKER" not in text and json.loads(text.splitlines()[0])["event"] == "run_start"


def test_latest_log_is_published_without_writing_through_planted_names(tmp_path: Path) -> None:
    """latest.log is built in a fresh temporary file and swapped in, so hard links planted at the old fixed temporary name or at latest.log never receive the copy."""
    import os

    victim = _victim(tmp_path)
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    os.link(victim, logs / "latest.log.tmp")
    os.link(victim, logs / "latest.log")
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME,
    )

    with session:
        session.finish("done", 0)

    assert victim.read_text(encoding="utf-8") == "precious"
    assert "# command: check" in (logs / "latest.log").read_text(encoding="utf-8")
    assert [p.name for p in logs.glob("latest.*.tmp")] == ["latest.log.tmp"], "no temporary copy leaked"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("latest.log.tmp", id="latest-temporary"),
        pytest.param("latest.jsonl", id="latest-prefix"),
        pytest.param("20261022T101500Z-install.jsonl", id="run-jsonl-name"),
        pytest.param("20261022T101500Z-install-Example.log", id="run-log-name"),
        pytest.param("20261022T101500Z-update.JSONL", id="run-jsonl-name-upper-case"),
        pytest.param("20261022t101500z-update.jsonl", id="run-jsonl-name-lower-case-stamp"),
        pytest.param("LATEST.tmp", id="latest-upper-case"),
    ],
)
def test_events_file_names_that_collide_with_the_logs_own_files_are_refused(name: str) -> None:
    """A name that would open a second handle on a run file, or on latest.log or its temporary copy, is refused."""
    with pytest.raises(ValueError):
        validate_log_file_name(name)


# ---------------------------------------------------------------------------
# Retention in isolation
# ---------------------------------------------------------------------------


def _old_run(logs: Path, moment: datetime, command: str = "install") -> str:
    """Create a run's .log and .jsonl stamped *moment* and return their shared name."""
    stem = f"{moment:%Y%m%dT%H%M%SZ}-{command}"
    for suffix in (".log", ".jsonl"):
        (logs / f"{stem}{suffix}").write_text("old", encoding="utf-8")
    return stem


def test_the_age_limit_alone_removes_old_runs_while_the_count_would_keep_them(tmp_path: Path) -> None:
    """With a generous run count, only runs older than keep_days are removed."""
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    recent = _old_run(logs, FIXED_TIME - timedelta(days=10))
    aged = _old_run(logs, FIXED_TIME - timedelta(days=200))
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME, keep_runs=50, keep_days=90,
    )

    with session:
        session.finish("done", 0)

    assert (logs / f"{recent}.log").exists() and (logs / f"{recent}.jsonl").exists()
    assert not (logs / f"{aged}.log").exists() and not (logs / f"{aged}.jsonl").exists()


@pytest.mark.windows_integration
def test_pruning_leaves_symbolic_links_named_like_old_runs_alone(tmp_path: Path) -> None:
    """A symbolic link named like an expired run is neither removed nor followed; only regular files are pruned."""
    import os

    victim = _victim(tmp_path)
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    expired = FIXED_TIME - timedelta(days=500)
    link = logs / f"{expired:%Y%m%dT%H%M%SZ}-install.log"
    try:
        os.symlink(victim, link)
    except OSError:
        pytest.skip("this account may not create symbolic links")
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME,
    )

    with session:
        session.finish("done", 0)

    assert link.is_symlink() and victim.read_text(encoding="utf-8") == "precious"


def test_pruning_never_touches_what_a_junction_or_hard_link_named_like_an_old_run_points_to(
    tmp_path: Path, system: FakeSystem
) -> None:
    """Junctions and hard links named like expired runs (no privilege needed): the junction and everything behind it survive, a hard link loses only its name."""
    import os

    victim = _victim(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    expired = FIXED_TIME - timedelta(days=500)
    junction = logs / f"{expired:%Y%m%dT%H%M%SZ}-install.log"
    linked = logs / f"{expired - timedelta(days=1):%Y%m%dT%H%M%SZ}-install.jsonl"
    system.create_junction(junction, outside)
    os.link(victim, linked)
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME,
    )

    with session:
        session.finish("done", 0)

    assert junction.exists() and (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert victim.read_text(encoding="utf-8") == "precious"
    assert not linked.exists(), "an expired plain file (even one with a second name) is pruned by name only"


@pytest.mark.parametrize("form", ["plain-file", "hard-link"])
@pytest.mark.parametrize("name", ["latest.tmp", "latest.log.tmp"])
def test_old_fixed_temporary_names_for_latest_log_are_never_written(
    tmp_path: Path, name: str, form: str
) -> None:
    """Files (or hard links to a victim) already sitting under the fixed temporary names are left exactly as they were, and the run succeeds."""
    import os

    victim = _victim(tmp_path)
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    if form == "hard-link":
        os.link(victim, logs / name)
    else:
        (logs / name).write_text("precious", encoding="utf-8")
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME,
    )

    with session:
        session.finish("done", 0)

    assert victim.read_text(encoding="utf-8") == "precious"
    assert (logs / name).read_text(encoding="utf-8") == "precious"
    assert "# command: check" in (logs / "latest.log").read_text(encoding="utf-8")


def test_the_cli_keeps_warnings_and_errors_on_standard_error_and_json_mode_moves_everything_there(tmp_path: Path) -> None:
    """Plain output puts warnings and errors on stderr and the rest on stdout; --json leaves stdout as only the document."""
    world = _world(tmp_path, tables=ENV)
    world.fake.failures["broadcast_environment_change"] = OSError("broadcast refused")

    plain = world.cli("install", "Example")
    assert plain.code == 0
    assert "WARNING: " in plain.err and "could not be broadcast" in plain.err and "WARNING" not in plain.out
    assert "==> Done" in plain.out and "ok Example 1.0.0" in plain.out

    failed = world.cli("install", "no-such-package")
    assert failed.code == 2
    assert "ERROR:" in failed.err and "ERROR:" not in failed.out

    world.fake.failures["broadcast_environment_change"] = OSError("broadcast refused")
    world.publish("Second", "1.0.0", tables=ENV)
    machine = world.cli("install", "Second", "--json")
    assert machine.code == 0 and json.loads(machine.out)["api"] == 2 and "==>" not in machine.out
    assert "==> Done" in machine.err and "WARNING: " in machine.err


def test_the_run_logs_of_a_package_are_listed_newest_first_without_latest_or_strangers(tmp_path: Path) -> None:
    """`gupkg logs <target>` lists the run text logs, newest first; latest.log, event files and other files are not runs."""
    logs = _logs(tmp_path / "tool")
    logs.mkdir(parents=True)
    for name in ("20261020T080000Z-install.log", "20261022T101500Z-update.log", "20261021T090000Z-check.log",
                 "20261022T101500Z-update.jsonl", "latest.log", "notes.txt", "events.jsonl"):
        (logs / name).write_text("x", encoding="utf-8")

    assert [item.name for item in list_run_logs(tmp_path / "tool")] == [
        "20261022T101500Z-update.log", "20261021T090000Z-check.log", "20261020T080000Z-install.log"]
    assert list_run_logs(tmp_path / "none") == []


def test_a_log_folder_that_is_a_junction_is_not_listed(tmp_path: Path, system: FakeSystem) -> None:
    """The listing never follows a `.gupkg\\logs` junction to another folder."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "20261022T101500Z-update.log").write_text("x", encoding="utf-8")
    (tmp_path / "tool" / ".gupkg").mkdir(parents=True)
    system.create_junction(tmp_path / "tool" / ".gupkg" / "logs", elsewhere)

    assert list_run_logs(tmp_path / "tool") == []
