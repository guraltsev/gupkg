"""Cover vendor installers (`[source.installer]`, design 8.4): when one runs, how, and what it may not do.

Behaviour covered: an installer runs only from a download that matches a pinned sha256 (the verified-only gate: every skip
decision and `origin-not-verified` is refused, in user and elevated runs, and a version that pins no sha256 is refused by the
plan before any download, whatever `--skip-checksum` says); with `hooks.allow_vendor_installers` not false a verified `.exe` runs
with `$Stage` expanded and the `App` is the stage; the plan, the run's events and the receipt carry the label "runs vendor installer"; the child gets a
scrubbed environment and no input; `require` is judged after the installer ran; a failing, timed-out or link-leaving
installer leaves no `App`, no `current` and no work folder; with the setting false nothing is downloaded or run, and
check-updates leaves the version without `App`; an argument that names a place outside the stage (`..`, another drive,
a UNC path, a relative path, a rooted one-segment path, `%VAR%`, `~`, a device name, a quoted `/D=`) is refused before
the installer starts, and a value that only starts with the stage path (`<stage>2`) is not inside it; an elevated run refuses an
installer path that is not admin-only and gives the child only admin-only `PATH` folders; a sandbox without a double reports `needs-real-run`; the installer is never executed for real.

Real: the engine, the planner, the unpack services and real folders. Mocked: `FakeSystem` (the installer is a process
double that writes files into the stage, never a real program).
Out of scope: Inno Setup itself (job 07 spike S3), the registry keys a real installer writes.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gupkg.engine.installer import InstallerError, expand_arguments, run_installer
from gupkg.engine.receipts import read_receipt
from gupkg.model.checksum import ChecksumRecord, parse_skip_flag
from gupkg.model.plan import Request
from gupkg.engine import lifecycle
from gupkg.report.events import Step, Warning, use_reporter
from gupkg.selfmgmt.elevation import UntrustedLocationError
from gupkg.services.artifact import FetchedArtifact
from gupkg.services.http import Download
from gupkg.system.port import ProcessResult
from tests.engine_support import BASE_URL, World, pin_no_ambient_elevation, pkg_text  # noqa: F401

RUN = '[source.installer]\nrun = ["/VERYSILENT", "/DIR=$Stage", "/NOICONS"]\n'
SETUP = "gimp-setup-1.0.0.exe"


class InstallerDouble:
    """Play a vendor installer: record the call, and write *files* into the folder named by `/DIR=`."""

    def __init__(self, files: dict[str, str] | None = None, returncode: int = 0, timeout: bool = False, outside: Path | None = None) -> None:
        self.files = files if files is not None else {"gimp.exe": "gimp\n", "bin/gimp-2.exe": "g2\n"}
        self.returncode, self.timeout, self.outside = returncode, timeout, outside
        self.calls: list = []

    def __call__(self, call) -> ProcessResult:
        """Record the call and write the files."""
        self.calls.append(call)
        if self.timeout:
            raise TimeoutError("too slow")
        stage = Path(next(part for part in call.argv if part.startswith("/DIR="))[len("/DIR="):])
        if self.returncode:
            return ProcessResult(self.returncode, "", "setup failed")
        for relative, content in self.files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self.outside is not None:
            self.outside.write_text("escaped", encoding="utf-8")
        return ProcessResult(0, "installing\nDone\n")


def publish_installer(
    world: World, *, source: str = RUN, mode: str = "user", double: InstallerDouble | None = None, version: str = "1.0.0",
    name: str = "gimp", pinned: bool = True,
) -> InstallerDouble:
    """Publish `name` `version` whose artifact is an .exe run through `[source.installer]`, with a process double for it."""
    data = b"MZ pretend installer " + version.encode()
    url = f"{BASE_URL}/{SETUP.replace('1.0.0', version)}"
    world.fake.add_url(url, data)
    folder = world.root(mode) / name / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pkg.toml").write_text(pkg_text(url, sha256=hashlib.sha256(data).hexdigest() if pinned else None, source=source), encoding="utf-8")
    double = double or InstallerDouble()
    world.fake.processes[SETUP.replace("1.0.0", version).lower()] = double
    return double


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root."""
    return World(tmp_path)


def test_an_allowed_installer_runs_into_the_stage_and_becomes_the_app(world: World) -> None:
    """The verified installer runs once with `$Stage` expanded; its files are the App; the receipt records the arguments."""
    double = publish_installer(world)

    result = world.one("install", "gimp")

    package = world.user / "gimp"
    assert result.ok and (package / "1.0.0" / "App" / "gimp.exe").is_file() and (package / "1.0.0" / "App" / "bin" / "gimp-2.exe").is_file()
    [call] = double.calls
    stage = call.argv[2][len("/DIR="):]
    assert call.argv[0].endswith(SETUP) and call.argv[1] == "/VERYSILENT" and call.argv[3] == "/NOICONS"
    assert Path(stage).name == "App" and Path(stage).parent.parent.name == "work"
    assert read_receipt(package, "1.0.0").data["source"]["installer"] == ["/VERYSILENT", f"/DIR={stage}", "/NOICONS"]
    assert not (package / ".gupkg" / "work").exists() or not any((package / ".gupkg" / "work").iterdir())


def test_the_installer_gets_a_scrubbed_environment_and_no_input(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child sees only machine-level variables (never the user's secrets), a TEMP inside the work folder and no stdin."""
    monkeypatch.setenv("MY_SECRET_TOKEN", "hunter2")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    double = publish_installer(world)

    world.one("install", "gimp")

    [call] = double.calls
    assert "MY_SECRET_TOKEN" not in call.env and "GITHUB_TOKEN" not in call.env
    assert call.input_text is None and "work" in call.env["TEMP"] and call.env["TEMP"] == call.env["TMP"]
    assert Path(call.cwd).name == "installer"


def test_the_label_is_in_the_plan_the_events_and_the_receipt(world: World) -> None:
    """`runs vendor installer` shows in --plan data, in the run's Unpack phase and as a warning that names the risk."""
    publish_installer(world)

    plans = lifecycle.plan(world.engine, [Request("install", "gimp")])
    data = plans[0].to_data()
    events: list[object] = []

    class Collect:
        """Keep every event."""

        def handle(self, event: object) -> None:
            events.append(event)

    with use_reporter(Collect()):
        assert world.one("install", "gimp").ok

    assert data["populate"]["label"] == "runs vendor installer" and data["populate"]["installer"][0] == "/VERYSILENT"
    unpack = next(phase for phase in data["phases"] if phase["name"] == "Unpack")
    assert unpack["steps"] == ["runs vendor installer: /VERYSILENT /DIR=$Stage /NOICONS"]
    texts = [event.text for event in events if isinstance(event, (Step, Warning))]
    assert any(text.startswith("runs vendor installer:") for text in texts) and any("outside" in text for text in texts if "vendor installer" in text)


def test_require_is_judged_after_the_installer_ran(world: World) -> None:
    """A required path the installer did not create fails the install; one it created passes."""
    publish_installer(world, source='require = ["gimp-3.exe"]\n' + RUN)

    result = world.one("install", "gimp")

    package = world.user / "gimp"
    assert not result.ok and result.error["code"] == "unpack-failed" and "gimp-3.exe" in result.error["message"]
    assert not (package / "1.0.0" / "App").exists() and not (package / "current").exists()


@pytest.mark.parametrize(
    ("double", "code", "text"),
    [
        pytest.param(InstallerDouble(returncode=3), "installer-failed", "status 3", id="failing"),
        pytest.param(InstallerDouble(timeout=True), "installer-failed", "did not finish", id="timeout"),
    ],
)
def test_a_failing_or_timed_out_installer_leaves_nothing(world: World, double: InstallerDouble, code: str, text: str) -> None:
    """Exit status 3: no App, no current, no work folder, no integration."""
    publish_installer(world, double=double)

    result = world.one("install", "gimp")

    package = world.user / "gimp"
    assert not result.ok and result.exit_code == 3 and result.error["code"] == code and text in result.error["message"]
    assert not (package / "1.0.0" / "App").exists() and not (package / "current").exists()
    assert not (package / ".gupkg" / "work").exists() or not any((package / ".gupkg" / "work").iterdir())
    assert not (package / ".gupkg" / "package.toml").exists()


def test_an_installer_that_leaves_a_link_in_the_app_is_refused(world: World) -> None:
    """A junction or symlink in the staged App could point anywhere; the whole App is refused."""
    outside = world.base / "elsewhere"
    outside.mkdir()

    class Linker(InstallerDouble):
        """Create a link next to the files."""

        def __call__(self, call) -> ProcessResult:
            result = super().__call__(call)
            stage = Path(next(part for part in call.argv if part.startswith("/DIR="))[len("/DIR="):])
            world.fake.create_junction(stage / "link", outside)
            return result

    publish_installer(world, double=Linker())

    result = world.one("install", "gimp")

    assert not result.ok and "link" in result.error["message"] and not (world.user / "gimp" / "1.0.0" / "App").exists()
    assert outside.is_dir()


def test_with_the_setting_false_nothing_is_downloaded_or_run(world: World) -> None:
    """hooks.allow_vendor_installers = false: the plan refuses before a byte is requested and the installer never starts."""
    world.machine.write_app_settings(tables="[hooks]\nallow_vendor_installers = false\n")
    double = publish_installer(world)
    world.fake.fetch_log.clear()

    result = world.one("install", "gimp")

    assert not result.ok and result.exit_code == 2 and "allow_vendor_installers" in result.error["message"]
    assert double.calls == [] and world.fake.fetch_log == [] and world.fake.process_log == []


def test_an_installed_version_is_not_touched_when_the_setting_is_false(world: World) -> None:
    """An App that already exists needs no installer, so a repair works with the setting false."""
    publish_installer(world)
    assert world.one("install", "gimp").ok
    world.machine.write_app_settings(tables="[hooks]\nallow_vendor_installers = false\n")

    assert world.one("install", "gimp").ok


def tracked(world: World) -> InstallerDouble:
    """Install gimp 1.0.0, give it a tracker whose versions run the installer, and announce 1.1.0; return the installer's double."""
    publish_installer(world, version="1.0.0")
    assert world.one("install", "gimp").ok
    world.tracker("gimp", tables=RUN, suffix="exe")
    world.announce("gimp", "1.1.0", suffix="exe")
    double = InstallerDouble()
    world.fake.processes["gimp-1.1.0.exe"] = double
    return double


def test_check_updates_runs_the_installer_into_the_new_version_when_allowed(world: World) -> None:
    """The version written by check-updates is populated by the installer; nothing is activated."""
    double = tracked(world)

    result = world.one("check-updates", "gimp")

    package = world.user / "gimp"
    assert result.ok and len(double.calls) == 1 and (package / "1.1.0" / "App" / "gimp.exe").is_file()
    assert (package / "current").resolve().name == "1.0.0"


def test_check_updates_with_the_setting_false_writes_the_version_without_its_app(world: World) -> None:
    """check-updates never runs the installer when it is forbidden; it warns and leaves the version unpopulated."""
    double = tracked(world)
    world.machine.write_app_settings(tables="[hooks]\nallow_vendor_installers = false\n")
    texts: list[str] = []

    class Collect:
        """Keep the warnings."""

        def handle(self, event: object) -> None:
            if isinstance(event, Warning):
                texts.append(event.text)

    with use_reporter(Collect()):
        result = world.one("check-updates", "gimp")

    package = world.user / "gimp"
    assert result.ok and double.calls == [] and (package / "1.1.0" / "pkg.toml").is_file() and not (package / "1.1.0" / "App").exists()
    assert any("allow_vendor_installers" in text for text in texts)


@pytest.mark.parametrize(
    "argument",
    [
        "/DIR=C:\\Windows\\System32",
        "/DIR=D:\\other",
        "/DIR=\\\\server\\share\\x",
        "/DIR=$Stage\\..\\..\\evil",
        "/DIR=..\\evil",
        "/DIR=sub\\dir",
        "C:\\Users\\Public\\x",
        "/LOG=$Stage\\..\\install.log",
        "/D=C:\\elsewhere",
        "-dir:C:\\elsewhere",
        "/DIR=\\Windows",
        "/DIR=/Windows",
        "/LOG=\\x",
        "-oC:\\x",
        "/LOG=con",
        "/DIR=nul",
        "/DIR=NUL.txt",
        "/LOG=COM1",
        "/DIR=%TEMP%",
        "/DIR=%TEMP%\\x",
        "/DIR=~\\x",
        "/DIR=~",
        "/DIR=$Stage:evil",
        "/DIR=$Stage\\x:stream",
        '/DIR="C:\\Windows"',
        "/DIR=a/b",
        "/DIR=..",
        "/LOG=x..\\y",
        "/DIR=D:$Stage",
        "/LOG=$Stage\\con",
        "/LOG=$Stage\\nul.txt",
        "/DIR=$Stage\\sub\\COM1",
        '/D="$Stage"',
        '/d="$Stage"',
    ],
)
def test_an_argument_that_names_a_place_outside_the_stage_is_refused_before_the_installer_starts(world: World, argument: str) -> None:
    """Hostile `run` arguments end the install with the code installer-failed, and the installer never starts."""
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"')
    double = publish_installer(world, source=f'[source.installer]\nrun = ["/VERYSILENT", "{escaped}", "/DIR=$Stage"]\n')

    result = world.one("install", "gimp")

    assert not result.ok and result.error["code"] == "installer-failed" and double.calls == []
    assert not (world.user / "gimp" / "1.0.0" / "App").exists()


@pytest.mark.parametrize("argument", ["/DIR=${Stage}2", "/DIR=${Stage}-evil", "/LOG=${Stage}x\\y", "/DIR=${Stage}:", "/DIR=${Stage}~1"])
def test_a_value_that_only_starts_with_the_stage_path_is_not_inside_it(tmp_path: Path, argument: str) -> None:
    """`<stage>2` and `<stage>-evil` are sibling folders: the prefix must end at a separator (the braced form can append text)."""
    with pytest.raises(InstallerError):
        expand_arguments([argument], tmp_path / "work" / "App")


@pytest.mark.parametrize(
    "argument",
    ["/VERYSILENT", "/NORESTART", "/DIR=$Stage", '/DIR="$Stage"', "/DIR=$Stage\\sub", "/LOG=install.log", "/MERGETASKS=!desktopicon", "$$HOME"],
)
def test_ordinary_arguments_and_paths_inside_the_stage_are_accepted(tmp_path: Path, argument: str) -> None:
    """Flags, plain file names and absolute paths inside the stage pass; `$Stage` expands to the stage."""
    stage = tmp_path / "work" / "App"

    [expanded] = expand_arguments([argument], stage)

    assert "$Stage" not in expanded and (str(stage) in expanded) == ("Stage" in argument)


def fetched_for(path: Path, name: str = SETUP, decision: str = "verified") -> FetchedArtifact:
    """Return a download of the file *path* named *name*, with the checksum *decision* (verified by default)."""
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    download = Download(path, len(data), digest, f"{BASE_URL}/{name}", name, False, False)
    return FetchedArtifact(download, ChecksumRecord(decision, f"{BASE_URL}/{name}", f"{BASE_URL}/{name}", digest, len(data)))


def direct_world(tmp_path: Path, **kwargs) -> tuple[World, Path, Path, Path]:
    """Return a world, a cached installer file, a work folder and a stage for a direct `run_installer` call."""
    world = World(tmp_path, **kwargs)
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = cache / "download.bin"
    cached.write_bytes(b"MZ pretend installer")
    work = tmp_path / "work"
    work.mkdir()
    return world, cached, work, work / "App"


def test_an_elevated_run_refuses_an_installer_path_that_is_not_admin_only(tmp_path: Path) -> None:
    """In an elevated run the folder the installer is copied to must be admin-only, judged before it starts."""
    world, cached, work, stage = direct_world(tmp_path, elevated=True)
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double
    world.fake.untrusted_dirs = {Path(os.path.abspath(work / "installer"))}

    with pytest.raises(UntrustedLocationError):
        run_installer(world.engine, fetched_for(cached), ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    assert double.calls == []


def test_a_normal_run_does_not_judge_the_folder(tmp_path: Path) -> None:
    """The admin-only rule belongs to elevated runs: the same untrusted folder is fine for a normal one."""
    world, cached, work, stage = direct_world(tmp_path)
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double
    world.fake.untrusted_dirs = {Path(os.path.abspath(work / "installer"))}

    run_installer(world.engine, fetched_for(cached), ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    assert len(double.calls) == 1


def test_a_cached_file_that_changed_after_verification_is_not_run(tmp_path: Path) -> None:
    """The installer is hashed again right before it is copied; a replaced file never runs."""
    world, cached, work, stage = direct_world(tmp_path)
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double
    fetched = fetched_for(cached)
    cached.write_bytes(b"MZ something else entirely")

    with pytest.raises(Exception, match="changed after it was verified"):
        run_installer(world.engine, fetched, ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    assert double.calls == []


def test_an_artifact_that_is_not_an_exe_is_refused(tmp_path: Path) -> None:
    """Only a downloaded .exe is run; a zip or msi named in an installer step is refused."""
    world, cached, work, stage = direct_world(tmp_path)

    with pytest.raises(InstallerError, match="not an .exe"):
        run_installer(world.engine, fetched_for(cached, "tool.zip"), ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")


def test_a_sandbox_without_a_double_reports_needs_a_real_run(world: World) -> None:
    """Under the fake machine an installer with no process double is not started: the result says `needs-real-run`."""
    publish_installer(world)
    world.fake.processes.clear()

    result = world.one("install", "gimp")

    assert not result.ok and result.error["code"] == "needs-real-run" and not (world.user / "gimp" / "1.0.0" / "App").exists()


@pytest.mark.parametrize("decision", ["skipped-cli", "skipped-setting", "user-approved", "origin-not-verified"])
@pytest.mark.parametrize("elevated", [False, True], ids=["user", "elevated"])
def test_an_installer_never_runs_from_a_download_that_was_not_verified(tmp_path: Path, decision: str, elevated: bool) -> None:
    """Every skip decision (and an unverified origin) is refused by `run_installer`, in user and elevated runs; nothing starts."""
    world, cached, work, stage = direct_world(tmp_path, elevated=elevated)
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double

    with pytest.raises(InstallerError, match="no verified sha256") as raised:
        run_installer(world.engine, fetched_for(cached, decision=decision), ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    assert "gupkg pkg pin" in str(raised.value) and SETUP in str(raised.value) and double.calls == [] and not (work / "installer").exists()


@pytest.mark.parametrize("decision", ["verified", "registry-fingerprint"])
def test_a_verified_download_still_runs(tmp_path: Path, decision: str) -> None:
    """A checked hash (or a matching registry fingerprint) is what an installer needs."""
    world, cached, work, stage = direct_world(tmp_path)
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double
    fetched = fetched_for(cached, decision="verified")
    if decision == "registry-fingerprint":
        fetched = FetchedArtifact(fetched.download, ChecksumRecord(decision, registry="main", observed_sha256=fetched.record.observed_sha256))

    run_installer(world.engine, fetched, ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    assert len(double.calls) == 1


@pytest.mark.parametrize("elevated", [False, True], ids=["user", "elevated"])
def test_an_unpinned_installer_is_refused_before_any_download_even_with_skip_checksum(tmp_path: Path, elevated: bool) -> None:
    """The plan refuses a version that pins no sha256; --skip-checksum is for archives only. Nothing is fetched or run."""
    world = World(tmp_path, elevated=elevated)
    mode = "system" if elevated else "user"
    double = publish_installer(world, pinned=False, mode=mode)
    world.fake.fetch_log.clear()

    result = world.one("install", f"{mode}:gimp", skip=parse_skip_flag(""))

    assert not result.ok and result.exit_code == 2 and "gupkg pkg pin" in result.error["message"]
    assert double.calls == [] and world.fake.fetch_log == [] and world.fake.process_log == []


def test_check_updates_refuses_an_unpinned_installer_before_the_download(world: World) -> None:
    """A tracker that discovers no checksum for an installer version leaves it unpopulated: the installer is neither fetched nor run."""
    publish_installer(world, version="1.0.0")
    assert world.one("install", "gimp").ok
    world.tracker("gimp", tables=RUN, suffix="exe", checksum=False)
    world.announce("gimp", "1.1.0", suffix="exe", sidecar=False)
    double = InstallerDouble()
    world.fake.processes["gimp-1.1.0.exe"] = double
    world.fake.fetch_log.clear()

    result = world.one("check-updates", "gimp", skip=parse_skip_flag(""))

    assert not result.ok and result.error["code"] == "installer-failed" and double.calls == []
    assert not any(url.endswith("gimp-1.1.0.exe") for url in world.fake.fetch_log)


def test_an_elevated_installer_gets_only_admin_only_folders_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The process PATH can hold the user's own folders; the elevated child gets the admin-only ones only."""
    trusted, writable = tmp_path / "windows" / "system32", tmp_path / "users" / "me" / "bin"
    trusted.mkdir(parents=True)
    writable.mkdir(parents=True)
    monkeypatch.setenv("PATH", os.pathsep.join([str(writable), str(trusted)]))
    world, cached, work, stage = direct_world(tmp_path, elevated=True, untrusted={writable})
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double

    run_installer(world.engine, fetched_for(cached), ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    [call] = double.calls
    assert call.env["PATH"] == str(trusted)


def test_a_normal_installer_keeps_the_process_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an elevated run narrows PATH."""
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(first), str(second)]))
    world, cached, work, stage = direct_world(tmp_path, untrusted={first})
    double = InstallerDouble()
    world.fake.processes[SETUP.lower()] = double

    run_installer(world.engine, fetched_for(cached), ["/DIR=$Stage"], stage, work, (), "gimp", "1.0.0")

    assert double.calls[0].env["PATH"] == os.pathsep.join([str(first), str(second)])
