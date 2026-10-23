"""Cover the log files a run leaves: placement, names, content, retention, failure and name safety.

Real: the CLI, the event stream, the console renderer and the log files, written
below pytest temporary directories (junctions included). Mocked: only the machine
boundary, as a ``FakeSystem`` (and its process double for ``git``). Out of scope:
real UAC and admin-only permissions (see ``tests/manual_smoke.md``), the settings
file that will set retention (the defaults are used) and the UI programs.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gupkg import cli, commands
from gupkg.report.events import PackageEnd, PackageStart, PhaseEnd, PhaseStart, RunEnd, RunStart, Step, Warning
from gupkg.report.logfile import RunLog, running_gupkg_logs_base, validate_log_file_name
from gupkg.report.session import RunSession
from gupkg.system.fake import FakeSystem
from gupkg.system.port import ProcessResult

FIXED_TIME = datetime(2026, 10, 22, 10, 15, 0, tzinfo=timezone.utc)


class _Collector:
    """Collect every event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        self.events.append(event)


def _package(root: Path, name: str = "Example", extra: str = "", app: bool = True) -> Path:
    """Create a one-version package below *root* and return the version directory."""
    version = root / name / "1.0.0"
    (version / "App").mkdir(parents=True)
    if app:
        (version / "App" / "tool.exe").write_text("payload", encoding="utf-8")
    (version / "pkg.toml").write_text(
        f'name = "{name}"\nversion = "1.0.0"\nlocalVersion = 0\n{extra}', encoding="utf-8"
    )
    return version


def _zip(entries: dict[str, str]) -> bytes:
    """Return the bytes of a ZIP archive holding *entries*."""
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as handle:
        for name, content in entries.items():
            handle.writestr(name, content)
    return archive.getvalue()


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
    """Pruning at run start leaves keep_runs runs including the new one, removes runs older than keep_days, and spares other files."""
    logs = _logs(tmp_path)
    logs.mkdir(parents=True)
    for index in range(10):
        stamp = (FIXED_TIME - timedelta(hours=index + 1)).strftime("%Y%m%dT%H%M%SZ")
        for suffix in (".log", ".jsonl"):
            (logs / f"{stamp}-install{suffix}").write_text("old", encoding="utf-8")
    ancient = (FIXED_TIME - timedelta(days=400)).strftime("%Y%m%dT%H%M%SZ")
    (logs / f"{ancient}-install.log").write_text("ancient", encoding="utf-8")
    (logs / "notes.txt").write_text("keep me", encoding="utf-8")
    (logs / "events.jsonl").write_text("caller file", encoding="utf-8")

    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[],
        fallback_base=tmp_path, clock=lambda: FIXED_TIME, keep_runs=4, keep_days=90,
    )
    with session:
        session.finish("done", 0)

    runs = sorted({p.stem for p in logs.glob("2*")})
    assert len(runs) == 4
    assert runs[-1] == "20261022T101500Z-check"
    assert runs[:3] == [(FIXED_TIME - timedelta(hours=h)).strftime("%Y%m%dT%H%M%SZ") + "-install" for h in (3, 2, 1)]
    assert (logs / "notes.txt").read_text(encoding="utf-8") == "keep me"
    assert (logs / "events.jsonl").read_text(encoding="utf-8") == "caller file"
    assert not list(logs.glob(f"{ancient}*"))


def test_an_unwritable_log_location_gives_one_warning_and_the_run_still_succeeds(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the root's log directory cannot be created, the run warns exactly once, succeeds, and the package log is still written."""
    root = tmp_path / "opt"
    version = _package(root)
    (root / ".gupkg").write_text("a file where the data directory should be", encoding="utf-8")

    code = cli.main(["--scope", "user", "install", str(version)], system=system)

    output = capsys.readouterr().out
    assert code == 0
    assert len(re.findall(r"WARNING: Cannot write the run log", output)) == 1
    assert len(_run_files(root / "Example", ".log")) == 1
    assert "==> Done" in output and output.rstrip().splitlines()[-1].startswith("Log of Example:")


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


def test_the_cli_refuses_an_events_file_with_a_path_and_writes_nothing(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path as --events-file is a usage error (exit 2) before any work happens, and no log or events file is created anywhere."""
    root = tmp_path / "opt"
    version = _package(root)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--events-file", str(elsewhere / "events.jsonl"), "install", str(version)], system=system)
    with pytest.raises(SystemExit):
        cli.main(["--events-file", "..\\events.jsonl", "install", str(version)], system=system)

    assert stopped.value.code == 2
    assert "plain file name" in capsys.readouterr().err
    assert list(elsewhere.iterdir()) == []
    assert not (root / ".gupkg").exists() and not (root / "Example" / ".gupkg").exists()
    assert not (root / "Example" / "current").exists(), "nothing was installed"


def test_a_junction_planted_as_the_log_directory_cannot_redirect_the_writes(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """If .gupkg\\logs is a junction to a folder outside the package, nothing is written through it and the run warns once and succeeds."""
    root = tmp_path / "opt"
    version = _package(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Example" / ".gupkg").mkdir()
    system.create_junction(root / "Example" / ".gupkg" / "logs", outside)

    code = cli.main(["--scope", "user", "--events-file", "events.jsonl", "install", str(version)], system=system)

    output = capsys.readouterr().out
    assert code == 0
    assert list(outside.iterdir()) == []
    assert len(re.findall(r"WARNING: Cannot write the log of package Example", output)) == 1
    assert len(_run_files(root, ".log")) == 1, "the root's own log is unaffected"


def test_an_events_file_name_that_is_a_junction_inside_the_log_directory_is_not_followed(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A junction planted under the events-file name is refused rather than written through, so the folder it points to stays empty."""
    root = tmp_path / "opt"
    version = _package(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = _logs(root / "Example")
    logs.mkdir(parents=True)
    system.create_junction(logs / "events.jsonl", outside)

    code = cli.main(["--scope", "user", "--events-file", "events.jsonl", "install", str(version)], system=system)

    assert code == 0
    assert list(outside.iterdir()) == []
    assert "WARNING: Cannot write the log of package Example" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# What the CLI leaves behind
# ---------------------------------------------------------------------------


def test_an_install_leaves_package_logs_a_linking_run_log_and_nothing_in_appdata(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install writes .log and .jsonl in the package's logs, a run log without package output that links them, names both in the summary, and writes nothing under AppData or ProgramData."""
    appdata, local, programdata = tmp_path / "appdata", tmp_path / "local", tmp_path / "programdata"
    for folder, variable in ((appdata, "APPDATA"), (local, "LOCALAPPDATA"), (programdata, "ProgramData")):
        folder.mkdir()
        monkeypatch.setenv(variable, str(folder))
    root = tmp_path / "opt"
    version = _package(root, extra='\n[[environment]]\nName = "EXAMPLE_READY"\nValue = "yes"\n')

    code = cli.main(["--scope", "user", "install", str(version)], system=system)

    output = capsys.readouterr().out
    assert code == 0
    [package_log] = _run_files(root / "Example", ".log")
    [package_events] = _run_files(root / "Example", ".jsonl")
    [run_log] = _run_files(root, ".log")
    assert package_log.name.endswith("-install.log") and run_log.name.endswith("-install-Example.log")
    package_text = package_log.read_text(encoding="utf-8")
    assert "==> Resolve" in package_text and "Installing Example 1.0.0" in package_text
    assert "==> Environment variables" in package_text

    # The run log holds global facts and a link, and none of the package's output.
    run_text = run_log.read_text(encoding="utf-8")
    assert f"Package Example: {package_log}" in run_text
    assert "Installing Example 1.0.0" not in run_text and "==> Resolve" not in run_text
    run_records = [json.loads(line) for line in _run_files(root, ".jsonl")[0].read_text(encoding="utf-8").splitlines()]
    assert not {"phase_start", "step"} & {r["event"] for r in run_records}
    [link] = [r for r in run_records if r["event"] == "log_link"]
    assert (link["log"], link["events"]) == (str(package_log), str(package_events))

    # The summary names every log and is the last thing printed.
    tail = output.rstrip().splitlines()[-2:]
    assert tail == [f"Run log: {run_log}", f"Log of Example: {package_log}"]
    assert list(appdata.iterdir()) == [] and list(local.iterdir()) == [] and list(programdata.iterdir()) == []


def test_redirected_install_output_is_plain_with_phase_headers_and_a_final_summary(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """With output redirected, an install prints ==> phase headers, no ANSI codes and no carriage returns, and one final progress line per download."""
    root = tmp_path / "opt"
    archive = _zip({"tool/tool.exe": "payload"})
    system.add_url("https://example.invalid/tool.zip", archive)
    version = _package(
        root, app=False, extra='\n[origin]\nurl = "https://example.invalid/tool.zip"\nextractSubdir = "tool"\n'
    )

    code = cli.main(["--scope", "user", "install", str(version)], system=system)

    output = capsys.readouterr().out
    assert code == 0
    assert "\x1b" not in output and "\r" not in output
    lines = output.splitlines()
    assert "==> Resolve" in lines and "==> Fetch" in lines
    assert len([line for line in lines if "Downloading origin:" in line and "(100%)" in line]) == 1
    assert lines.index("==> Done") > lines.index("==> Fetch")


def test_events_file_appears_in_the_run_and_package_log_directories(
    tmp_path: Path, system: FakeSystem
) -> None:
    """--events-file events.jsonl writes the event stream under that name in the root's and the package's log directories."""
    root = tmp_path / "opt"
    version = _package(root)

    assert cli.main(["--scope", "user", "--events-file", "events.jsonl", "install", str(version)], system=system) == 0

    package_lines = (_logs(root / "Example") / "events.jsonl").read_text(encoding="utf-8").splitlines()
    run_lines = (_logs(root) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["event"] == "phase_start" for line in package_lines)
    assert not any(json.loads(line)["event"] == "phase_start" for line in run_lines)
    assert json.loads(run_lines[-1])["event"] == "run_end"
    assert set(tmp_path.rglob("events.jsonl")) == {_logs(root) / "events.jsonl", _logs(root / "Example") / "events.jsonl"}


def test_a_failing_git_clone_shows_gits_error_lines_in_the_fetch_phase_of_console_and_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A git clone that fails prints git's own error lines inside the Fetch phase, on the console and in the package log, and the install fails."""
    def git(call) -> ProcessResult:
        if "ls-remote" in call.argv:
            return ProcessResult(0, "a" * 40 + "\tHEAD\n", "")
        return ProcessResult(128, "", "Cloning into 'App.new'...\nfatal: unable to access 'https://example.invalid/x.git/': Could not resolve host\n")

    system = FakeSystem(tmp_path / "machine", processes={"git": git})
    root = tmp_path / "opt"
    version = _package(
        root, app=False, extra='\n[origin]\nmode = "git"\nurl = "https://example.invalid/x.git"\nref = "refs/heads/main"\n'
    )

    code = cli.main(["--scope", "user", "install", str(version)], system=system)

    output = capsys.readouterr().out
    assert code == 3, output
    package_text = _run_files(root / "Example", ".log")[0].read_text(encoding="utf-8")
    for text in (output, package_text):
        fetch = text.split("==> Fetch", 1)[1].split("Fetch: failed", 1)[0]
        assert "[git] fatal: unable to access 'https://example.invalid/x.git/': Could not resolve host" in fetch
        assert "[git] Cloning into 'App.new'..." in fetch
    assert "Fetch: failed" in output


def test_each_warning_appears_once_on_the_console_and_once_in_the_log(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A warning reported live and again in the result is printed once and logged once; a live-only warning also appears once."""
    root = tmp_path / "opt"
    version = _package(root, extra='\n[[environment]]\nName = "EXAMPLE_READY"\nValue = "yes"\n')
    system.failures["broadcast_environment_change"] = OSError("broadcast refused")

    assert cli.main(["--scope", "user", "install", str(version)], system=system) == 0
    live_output = capsys.readouterr().out
    live_log = _run_files(root / "Example", ".log")[0].read_text(encoding="utf-8")

    for text in (live_output, live_log):
        assert text.count("WARNING: failed to broadcast environment change notification: broadcast refused") == 1

    # A workflow that reports a warning live and also returns it.
    from gupkg.core import ActionResult, log_warning

    def operation() -> ActionResult:
        log_warning("reported twice")
        return ActionResult(ok=True, warnings=["reported twice", "only in the result"])

    collector = _Collector()
    session = RunSession(
        command="check", argv=[], mode="user", elevated=False, version="1.0.0", reporters=[collector],
        fallback_base=tmp_path / "own", clock=lambda: FIXED_TIME,
    )
    with session:
        commands.run_guarded("check", operation)
        session.finish("done", 0)

    assert [e.text for e in collector.events if isinstance(e, Warning)] == ["reported twice", "only in the result"]
    own_log = _run_files(tmp_path / "own", ".log")[0].read_text(encoding="utf-8")
    assert own_log.count("WARNING: reported twice") == 1 and own_log.count("WARNING: only in the result") == 1


def test_a_bundled_gupkg_run_with_no_package_logs_into_its_own_package(
    tmp_path: Path, system: FakeSystem, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that involves no package, started by a bundled gupkg, leaves its log in its own package's .gupkg\\logs, and an installed gupkg logs in its root."""
    # Bundled layout: <root>\<package>\<version>\gupkg\gupkg\<module>
    package = tmp_path / "opt" / "MyApp"
    bundled = package / "1.0.0" / "gupkg" / "gupkg"
    bundled.mkdir(parents=True)
    (package / "1.0.0" / "pkg.toml").write_text('name = "MyApp"\nversion = "1.0.0"\nlocalVersion = 0\n', encoding="utf-8")
    assert running_gupkg_logs_base(bundled) == package

    # Installed layout: <root>\gupkg\<version>\App\gupkg\<module>
    installed = tmp_path / "root" / "gupkg" / "1.0.0" / "App" / "gupkg"
    installed.mkdir(parents=True)
    (tmp_path / "root" / "gupkg" / "1.0.0" / "pkg.toml").write_text("", encoding="utf-8")
    assert running_gupkg_logs_base(installed) == tmp_path / "root"

    # A source checkout has neither layout and logs nowhere.
    assert running_gupkg_logs_base(tmp_path / "src" / "gupkg") is None

    # A real run that names no package (an install of a path that is no package) logs in the bundle's package.
    monkeypatch.setattr(cli, "running_gupkg_logs_base", lambda: package)
    code = cli.main(["install", str(tmp_path / "no-such-package")], system=system)

    assert code == 2
    [log] = _run_files(package, ".log")
    assert log.name.endswith("-install.log") and "ERROR:" in log.read_text(encoding="utf-8")
    assert capsys.readouterr().out.rstrip().splitlines()[-1] == f"Run log: {log}"


# ---------------------------------------------------------------------------
# Several targets in one call
# ---------------------------------------------------------------------------


def _updatable(root: Path, system: FakeSystem, name: str) -> None:
    """Install-shaped package <name> 1.0.0 whose GitHub release offers 2.0.0 (served by the fake network)."""
    version = _package(
        root, name,
        extra=(
            f'\n[origin]\nurl = "https://github.com/owner/{name}"\n'
            '\n[update.check]\nmode = "github"\nassetName = "pkg.zip"\n\n[update.payload]\nmode = "zip"\n'
        ),
    )
    system.create_junction(root / name / "current", version)
    archive = _zip({f"{name}.exe": "v2"})
    system.add_url(
        f"https://api.github.com/repos/owner/{name}/releases/latest",
        json.dumps({
            "id": 1, "tag_name": "2.0.0",
            "assets": [{
                "name": "pkg.zip", "state": "uploaded",
                "browser_download_url": f"https://example.invalid/{name}.zip",
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
            }],
        }),
    )
    system.add_url(f"https://example.invalid/{name}.zip", archive)


def test_a_manager_call_with_several_packages_logs_each_one_and_links_them_from_the_run_log(
    tmp_path: Path, system: FakeSystem, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manager update of two packages records each package's phase output in its own log, and the run log links both without their output."""
    user, system_root, manager = tmp_path / "user", tmp_path / "system", tmp_path / "manager"
    for folder in (user, system_root, manager):
        folder.mkdir()
    _updatable(user, system, "alpha")
    _updatable(user, system, "beta")
    config = manager / "gupkg-config.toml"
    config.write_text(
        'mode = "manager"\nschema_version = 2\n\n'
        f"[packages]\nsystem = '{system_root}'\nuser = '{user}'\n\n"
        f"[bin]\nsystem = '{manager / 'sbin'}'\nuser = '{manager / 'ubin'}'\n\n"
        f"[registry]\ncache = '{manager / 'registry'}'\n\n[shims]\nlinkage = 'dynamic'\n",
        encoding="utf-8",
    )

    code = cli.main(["--scope", "user", "manager", "--config", str(config), "update", "--yes"], system=system)

    output = capsys.readouterr().out
    assert code == 0, output
    [run_log] = _run_files(user, ".log")
    run_text = run_log.read_text(encoding="utf-8")
    for name in ("alpha", "beta"):
        [package_log] = _run_files(user / name, ".log")
        package_text = package_log.read_text(encoding="utf-8")
        assert "==> Fetch" in package_text and f"Downloading update: " in package_text
        assert f"Package {name}: {package_log}" in run_text
        assert f"Log of {name}: {package_log}" in output
        other = "beta" if name == "alpha" else "alpha"
        assert f"{other}.zip" not in package_text
    assert "==> Fetch" not in run_text and "Downloading update" not in run_text
