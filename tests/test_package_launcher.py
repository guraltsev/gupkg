r"""Cover the package launchers (`install.cmd`, `manage.cmd`, `check-updates.cmd`): which gupkg runs and when elevated runs refuse.

Behavior covered: a launcher runs the gupkg bundled in its folder first, then the folder named by the first line of
`<root>\.gupkg\gupkg-location` (absolute, or relative to the root; read as data), then the first `gupkg.cmd` or `gupkg.exe` on
`PATH`, and exits 9009 with the way to get gupkg when none exists; a missing or empty `gupkg-location`, or one naming a folder
without `gupkg.cmd`, is skipped with one line naming the file; exit code 6 from a gupkg moves on to the next step, and the
launcher exits 6 when no step is left; the exit code of gupkg is the launcher's own; the current directory is never searched;
`check-updates.cmd` runs `check-updates <package name>` and nothing else; an elevated launcher (simulated, see `as_elevated`)
refuses before any gupkg starts when anything it would run can be written by a standard user, or when PowerShell cannot be used
(fail closed), and, in an elevated shell, proceeds in an admin-only tree and refuses once a standard user is granted write on any
one object it checks, skips (and names) a user-writable `PATH` entry, and refuses a `gupkg.exe` shim whose config a user can write.

Real: the generated launchers run by the real `cmd.exe` in a temporary tree, with fake `gupkg.cmd` files that print their own
path and their arguments; the access check runs the real System32 PowerShell. Mocked: gupkg itself. The real `PATH`, registry
and roots are never touched: every run gets a minimal environment with a `PATH` made by the test.
Out of scope: UAC itself and the integrity-level detection of a really elevated token (manual smoke test 033-1); the tests that
need an administrator token skip themselves in a standard shell (manual smoke test 035-T11-1 covers them).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gupkg.launchers import LAUNCHERS, launcher_text, write_launchers

pytestmark = [pytest.mark.skipif(os.name != "nt", reason="the launchers are Windows cmd files"), pytest.mark.windows_integration]

# A fake gupkg: it prints its label, its own path and its arguments, and exits with the code the test set in the environment.
FAKE = '@echo off\r\necho {label} %~f0 %*\r\nexit /b %{variable}%\r\n'
SYSTEM32 = os.environ.get("SystemRoot", "C:\\Windows") + "\\System32"


def write_fake(folder: Path, label: str, variable: str = "RUN_EXIT") -> Path:
    """Create `<folder>/gupkg.cmd`, a fake gupkg that echoes *label* and exits with the code in the environment variable *variable*."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "gupkg.cmd"
    path.write_text(FAKE.format(label=label, variable=variable), encoding="ascii")
    return path


def build_tree(root: Path, *, location: str | None = None, bundled: bool = False) -> Path:
    """Create `<root>/tool/1.0` with launchers, a bundled fake gupkg when asked, and `.gupkg/gupkg-location` holding *location*.

    The file is written only when *location* is not ``None``; the folder it names is not created (the test makes it).
    """
    version = root / "tool" / "1.0"
    version.mkdir(parents=True)
    write_launchers(version)
    if location is not None:
        (root / ".gupkg").mkdir(exist_ok=True)
        (root / ".gupkg" / "gupkg-location").write_text(location, encoding="ascii")
    if bundled:
        write_fake(version / "gupkg", "BUNDLED", "BUNDLED_EXIT")
    return version


def run(
    launcher: Path, *args: str, bundled_exit: int = 0, location_exit: int = 0, path_exit: int = 0, path: str = "",
    cwd: Path | None = None, inherit: bool = False, pause: bool = False, extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a launcher with cmd.exe and return its output and exit code.

    The environment is minimal (the ``PATH`` is *path*, or System32 alone) unless *inherit* is true (the access check needs
    PowerShell's usual environment, but the ``PATH`` is still *path*).
    """
    environment = {
        "SystemRoot": os.environ["SystemRoot"], "ComSpec": os.environ["ComSpec"], "GUPKG_NO_PAUSE": "1",
        "BUNDLED_EXIT": str(bundled_exit), "LOCATION_EXIT": str(location_exit), "PATH_EXIT": str(path_exit),
        "PATH": path or SYSTEM32,
    }
    if inherit:
        environment = {**os.environ, **environment}
    if pause:
        environment.pop("GUPKG_NO_PAUSE")
    environment.update(extra_env or {})
    return subprocess.run(
        [os.environ["ComSpec"], "/d", "/c", str(launcher), *args],
        capture_output=True, text=True, env=environment, check=False, cwd=str(cwd or launcher.parent),
        input="\r\n" if pause else None,
    )


def on_path(folder: Path) -> str:
    """Return a ``PATH`` value with *folder* first and System32 (for `where`) after it."""
    return f"{folder};{SYSTEM32}"


def test_all_launchers_come_from_one_template_with_different_commands() -> None:
    """`install.cmd` runs `install` and `manage.cmd` runs `tui`; everything else is identical."""
    install, tui = launcher_text("install"), launcher_text("tui")

    assert set(LAUNCHERS) == {"install.cmd", "manage.cmd", "check-updates.cmd"}
    differing = ("if not defined GUPKG_NO_PAUSE pause", 'if not "%GUPKG_EXIT%"=="0" if not defined GUPKG_NO_PAUSE pause')
    # Line 4 names the file itself (issue 037); the rest differs only in the command and the pause rule.
    assert install.replace(" install ", " tui ").replace(*differing, 1).splitlines()[4:] == tui.splitlines()[4:]
    assert "(install.cmd)" in install.splitlines()[3] and "(manage.cmd)" in tui.splitlines()[3]
    assert "\r\n" in install


def test_the_launcher_template_has_no_root_forwarder_and_no_system_gupkg_option() -> None:
    """The root forwarder files and the option that skipped them are gone from the launcher (issue 035, D52)."""
    text = launcher_text("install").lower()

    assert "forwarder" not in text and "system-gupkg" not in text
    assert "gupkg-manager.cmd" not in text and "\\.gupkg\\gupkg.cmd" not in text


def test_the_bundled_gupkg_runs_first_even_when_the_root_names_another(tmp_path: Path) -> None:
    """With a bundled copy and a `gupkg-location`, the bundled gupkg runs, called with `install` and the version folder."""
    version = build_tree(tmp_path, location=r"gupkg\current\App", bundled=True)
    write_fake(tmp_path / "gupkg" / "current" / "App", "LOCATION", "LOCATION_EXIT")

    result = run(version / "install.cmd", "--refresh")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("BUNDLED ") and str(version / "gupkg" / "gupkg.cmd") in result.stdout
    assert f' install "{version}\\."' in result.stdout and "--refresh" in result.stdout
    assert "LOCATION" not in result.stdout


def test_without_a_bundled_copy_the_gupkg_named_in_gupkg_location_runs(tmp_path: Path) -> None:
    """The first line of `<root>\\.gupkg\\gupkg-location` is a folder holding gupkg.cmd; that gupkg runs."""
    elsewhere = tmp_path / "somewhere else" / "App"
    version = build_tree(tmp_path / "root", location=str(elsewhere))
    write_fake(elsewhere, "LOCATION", "LOCATION_EXIT")

    result = run(version / "install.cmd")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("LOCATION ") and str(elsewhere / "gupkg.cmd") in result.stdout
    assert "Skipped" not in result.stderr


def test_a_relative_gupkg_location_is_resolved_against_the_root(tmp_path: Path) -> None:
    """`gupkg\\current\\App` names `<root>\\gupkg\\current\\App`, whatever the current directory is; only the first line counts."""
    version = build_tree(tmp_path, location="gupkg\\current\\App\r\nthis second line is ignored\r\n")
    write_fake(tmp_path / "gupkg" / "current" / "App", "LOCATION", "LOCATION_EXIT")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = run(version / "install.cmd", cwd=elsewhere)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("LOCATION ") and str(tmp_path / "gupkg" / "current" / "App" / "gupkg.cmd") in result.stdout


def test_gupkg_location_is_read_as_data_and_never_executed(tmp_path: Path) -> None:
    """A `gupkg-location` that holds a command is not run: it names no folder, so it is skipped."""
    marker = tmp_path / "executed.txt"
    version = build_tree(tmp_path / "root", location=f'& echo x> "{marker}" & rem')

    result = run(version / "install.cmd")

    assert not marker.exists()
    assert result.returncode == 9009 and "Skipped" in result.stderr


@pytest.mark.parametrize("content", ["", "\r\n", "missing\\folder"], ids=["empty-file", "blank-line", "folder-without-gupkg-cmd"])
def test_an_empty_or_wrong_gupkg_location_is_skipped_with_one_line_and_path_is_used(tmp_path: Path, content: str) -> None:
    """An empty file, and one naming a folder without gupkg.cmd, print which file was skipped and why; the gupkg on PATH runs."""
    version = build_tree(tmp_path / "root", location=content)
    on_the_path = tmp_path / "bin"
    write_fake(on_the_path, "PATHGUPKG", "PATH_EXIT")

    result = run(version / "install.cmd", path=on_path(on_the_path))

    skipped = [line for line in result.stderr.splitlines() if "Skipped" in line]
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PATHGUPKG ") and str(on_the_path / "gupkg.cmd") in result.stdout
    assert len(skipped) == 1 and str(tmp_path / "root" / ".gupkg" / "gupkg-location") in skipped[0]
    assert ("is empty" in skipped[0]) == (content != "missing\\folder") and ("has no gupkg.cmd" in skipped[0]) == (content == "missing\\folder")


def test_a_missing_gupkg_location_is_skipped_and_named(tmp_path: Path) -> None:
    """With no bundled copy and no `gupkg-location`, the launcher names the file it did not find and goes on to PATH."""
    version = build_tree(tmp_path / "root")
    on_the_path = tmp_path / "bin"
    write_fake(on_the_path, "PATHGUPKG", "PATH_EXIT")

    result = run(version / "install.cmd", path=on_path(on_the_path))

    assert result.stdout.startswith("PATHGUPKG ")
    assert "Skipped" in result.stderr and "does not exist" in result.stderr


def test_install_and_check_updates_launchers_always_wait_for_a_key_and_the_tui_launcher_only_after_a_failure(tmp_path: Path) -> None:
    """Double-clicking install.cmd or check-updates.cmd keeps the window open on success and failure; manage.cmd waits only after a failure."""
    version = build_tree(tmp_path, bundled=True)
    tracker = tmp_path / "tool" / "_check_updates"
    tracker.mkdir()
    write_launchers(tracker, ("check-updates.cmd",))
    (tmp_path / ".gupkg").mkdir()
    (tmp_path / ".gupkg" / "gupkg-location").write_text("gupkg\\current\\App", encoding="ascii")
    write_fake(tmp_path / "gupkg" / "current" / "App", "LOCATION", "LOCATION_EXIT")

    for launcher, waits_on_success in ((version / "install.cmd", True), (tracker / "check-updates.cmd", True), (version / "manage.cmd", False)):
        assert ("Press any key" in run(launcher, pause=True).stdout) is waits_on_success, launcher.name
        assert "Press any key" in run(launcher, bundled_exit=3, location_exit=3, pause=True).stdout, launcher.name


def test_no_pause_variable_turns_the_pause_off_even_for_install(tmp_path: Path) -> None:
    """Scripts (the bootstrap) set GUPKG_NO_PAUSE so install.cmd returns at once."""
    version = build_tree(tmp_path, bundled=True)

    assert "Press any key" not in run(version / "install.cmd").stdout


def test_tui_launcher_runs_the_tui_command(tmp_path: Path) -> None:
    """`manage.cmd` asks gupkg for its text interface on this folder."""
    version = build_tree(tmp_path, bundled=True)

    assert " tui " in run(version / "manage.cmd").stdout


def test_exit_code_6_moves_on_to_the_next_step(tmp_path: Path) -> None:
    """A bundled gupkg that cannot handle the package exits 6; the launcher says so and runs the gupkg of `gupkg-location`."""
    version = build_tree(tmp_path, location="gupkg\\current\\App", bundled=True)
    write_fake(tmp_path / "gupkg" / "current" / "App", "LOCATION", "LOCATION_EXIT")

    result = run(version / "install.cmd", bundled_exit=6, location_exit=0)

    assert result.returncode == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("BUNDLED ") and lines[1].startswith("LOCATION ")
    assert "cannot handle this package" in result.stderr


def test_exit_code_6_goes_on_from_the_location_to_path_and_the_last_step_returns_it(tmp_path: Path) -> None:
    """Bundled, location and PATH are tried in order on exit code 6; when the last one also exits 6, so does the launcher."""
    version = build_tree(tmp_path / "root", location="..\\app", bundled=True)
    write_fake(tmp_path / "app", "LOCATION", "LOCATION_EXIT")
    on_the_path = tmp_path / "bin"
    write_fake(on_the_path, "PATHGUPKG", "PATH_EXIT")

    moved_on = run(version / "install.cmd", bundled_exit=6, location_exit=6, path_exit=0, path=on_path(on_the_path))
    exhausted = run(version / "install.cmd", bundled_exit=6, location_exit=6, path_exit=6, path=on_path(on_the_path))

    assert [line.split()[0] for line in moved_on.stdout.splitlines()] == ["BUNDLED", "LOCATION", "PATHGUPKG"] and moved_on.returncode == 0
    assert [line.split()[0] for line in exhausted.stdout.splitlines()] == ["BUNDLED", "LOCATION", "PATHGUPKG"] and exhausted.returncode == 6


def test_exit_code_6_without_another_gupkg_is_returned_unchanged(tmp_path: Path) -> None:
    """With no later step to try, exit code 6 is the launcher's exit code."""
    version = build_tree(tmp_path, bundled=True)

    assert run(version / "install.cmd", bundled_exit=6).returncode == 6


def test_the_exit_code_of_gupkg_is_the_launchers_exit_code(tmp_path: Path) -> None:
    """A failing gupkg makes the launcher fail with the same code."""
    version = build_tree(tmp_path, bundled=True)

    assert run(version / "install.cmd", bundled_exit=3).returncode == 3


def test_without_any_gupkg_the_launcher_exits_9009_and_says_how_to_get_one(tmp_path: Path) -> None:
    """With no bundled gupkg, no `gupkg-location` and none on PATH the launcher exits 9009 and names the bootstrap."""
    version = build_tree(tmp_path)

    result = run(version / "install.cmd")

    assert result.returncode == 9009
    assert "gupkg-bootstrap.cmd" in result.stderr


def test_a_gupkg_on_path_is_used_when_nothing_else_is_found(tmp_path: Path) -> None:
    """A `gupkg.cmd` on PATH is the third step: it runs with the command and this folder."""
    version = build_tree(tmp_path)
    decoy = tmp_path / "decoy"
    write_fake(decoy, "PATHGUPKG", "PATH_EXIT")

    result = run(version / "install.cmd", path=on_path(decoy))

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PATHGUPKG ") and f' install "{version}\\."' in result.stdout


def test_a_gupkg_exe_on_path_is_found_too(tmp_path: Path) -> None:
    """The first `gupkg.exe` on PATH runs when no `gupkg.cmd` is there (a copy of a tiny real program stands in for the shim)."""
    version = build_tree(tmp_path)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "gupkg.exe").write_bytes(Path(SYSTEM32, "whoami.exe").read_bytes())

    result = run(version / "install.cmd", path=on_path(decoy))

    # whoami.exe does not know the arguments and fails with its own code; what matters is that it, and no 9009, was the result.
    assert result.returncode != 9009 and "gupkg-bootstrap.cmd" not in result.stderr


def test_a_gupkg_cmd_in_the_current_directory_is_never_used(tmp_path: Path) -> None:
    """A `gupkg.cmd` sitting in the directory the launcher is started from is ignored: PATH is searched, the current directory is not."""
    version = build_tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    write_fake(elsewhere, "CWDGUPKG", "PATH_EXIT")

    result = run(version / "install.cmd", cwd=elsewhere)

    assert result.returncode == 9009
    assert "CWDGUPKG" not in result.stdout


def test_check_updates_launcher_checks_the_package_named_by_its_folder_and_installs_nothing(tmp_path: Path) -> None:
    """`check-updates.cmd` in `<root>/<name>/_check_updates` runs `check-updates <name>` through the gupkg of `gupkg-location`."""
    tracker = tmp_path / "my tool" / "_check_updates"
    tracker.mkdir(parents=True)
    write_launchers(tracker, ("check-updates.cmd",))
    (tmp_path / ".gupkg").mkdir()
    (tmp_path / ".gupkg" / "gupkg-location").write_text("gupkg\\current\\App", encoding="ascii")
    write_fake(tmp_path / "gupkg" / "current" / "App", "LOCATION", "LOCATION_EXIT")

    result = run(tracker / "check-updates.cmd")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().startswith("LOCATION ") and result.stdout.strip().endswith(' check-updates "my tool"')


def test_check_updates_launcher_without_a_gupkg_exits_9009(tmp_path: Path) -> None:
    """A tracker launcher in a root with no gupkg says how to get one (a tracker carries no bundled copy)."""
    tracker = tmp_path / "tool" / "_check_updates"
    tracker.mkdir(parents=True)
    write_launchers(tracker, ("check-updates.cmd",))

    result = run(tracker / "check-updates.cmd")

    assert result.returncode == 9009 and "gupkg-bootstrap.cmd" in result.stderr


# ---------------------------------------------------------------------------
# Elevated runs
# ---------------------------------------------------------------------------

# A fake gupkg that proves it started by creating a file next to the test's root.
MARKER = '@echo off\r\necho started> "{marker}"\r\nexit /b 0\r\n'


def as_elevated(version: Path, *, powershell: str | None = None) -> None:
    """Make every launcher in *version* believe the run is elevated, so the real access check runs in a standard shell.

    The detection of a really elevated token is a manual check (033-1); what is tested here is what an elevated
    launcher does once it knows. *powershell* replaces the path of the PowerShell the check starts (to block it).
    """
    for launcher in version.glob("*.cmd"):
        text = launcher.read_bytes().decode("ascii")
        assert "call :integrity_level" in text
        text = text.replace("call :integrity_level", 'set "GUPKG_ELEVATED=yes"')
        if powershell is not None:
            text = text.replace("%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", powershell)
        launcher.write_bytes(text.encode("ascii"))


def marker_tree(root: Path, *, location: bool) -> tuple[Path, Path]:
    """Create `<root>/tool/1.0` whose `gupkg-location` (or bundled) gupkg writes a marker file; return the version folder and the marker.

    With *location* the gupkg is `<root>/gupkg/1.0/App/gupkg.cmd`, reached through the junction `<root>/gupkg/current` that
    `.gupkg/gupkg-location` names (`gupkg\\current\\App`), like an installed gupkg.
    """
    marker = root.parent / f"{root.name}-started.txt"
    version = root / "tool" / "1.0"
    version.mkdir(parents=True)
    write_launchers(version)
    target = root / "gupkg" / "1.0" / "App" if location else version / "gupkg"
    target.mkdir(parents=True)
    (target / "gupkg.cmd").write_text(MARKER.format(marker=marker), encoding="ascii")
    if location:
        (root / ".gupkg").mkdir()
        (root / ".gupkg" / "gupkg-location").write_text("gupkg\\current\\App", encoding="ascii")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "gupkg" / "current"), str(root / "gupkg" / "1.0")], capture_output=True, check=True)
    return version, marker


@pytest.mark.parametrize("location", [True, False], ids=["gupkg-location", "bundled-copy"])
def test_an_elevated_launcher_in_a_user_writable_tree_refuses_before_any_gupkg_starts(tmp_path: Path, location: bool) -> None:
    """In a folder a standard user owns (Downloads, a user root) an elevated launcher exits 1 and starts nothing."""
    version, marker = marker_tree(tmp_path / "root", location=location)
    as_elevated(version)

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1
    assert "Refusing to run elevated" in result.stderr and "GUPKG-ACL-FAIL" in result.stderr
    assert not marker.exists()


def test_an_elevated_launcher_refuses_a_gupkg_on_path_in_a_user_writable_tree_and_starts_nothing(tmp_path: Path) -> None:
    """With the launcher's own folders user-writable, an elevated run refuses before it looks at PATH entries, and nothing starts."""
    version, marker = marker_tree(tmp_path / "root", location=False)
    (version / "gupkg" / "gupkg.cmd").unlink()
    on_the_path = tmp_path / "bin"
    on_the_path.mkdir()
    (on_the_path / "gupkg.cmd").write_text(MARKER.format(marker=marker), encoding="ascii")
    as_elevated(version)

    result = run(version / "install.cmd", inherit=True, path=on_path(on_the_path))

    assert result.returncode == 1 and "Refusing to run elevated" in result.stderr
    assert not marker.exists()


def test_an_elevated_launcher_refuses_when_powershell_cannot_be_used(tmp_path: Path) -> None:
    """The check fails closed: with PowerShell missing the launcher refuses instead of trusting the folders."""
    version, marker = marker_tree(tmp_path / "root", location=False)
    as_elevated(version, powershell=str(tmp_path / "no-such-dir" / "powershell.exe"))

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1 and "Refusing to run elevated" in result.stderr
    assert not marker.exists()


def test_a_launcher_that_cannot_tell_whether_it_is_elevated_refuses(tmp_path: Path) -> None:
    """With no usable `whoami` the launcher refuses rather than assuming a standard token (and so skipping the check)."""
    version, marker = marker_tree(tmp_path / "root", location=False)
    text = (version / "install.cmd").read_bytes().decode("ascii").replace("whoami.exe", "no-such-whoami.exe")
    (version / "install.cmd").write_bytes(text.encode("ascii"))

    result = run(version / "install.cmd")

    assert result.returncode == 1 and "Cannot tell whether this run is elevated" in result.stderr
    assert not marker.exists()


def test_a_standard_run_never_starts_powershell(tmp_path: Path) -> None:
    """A standard (not elevated) launcher runs gupkg without the access check, so the check costs nothing there."""
    version, marker = marker_tree(tmp_path / "root", location=False)
    text = (version / "install.cmd").read_bytes().decode("ascii")
    (version / "install.cmd").write_bytes(text.replace("WindowsPowerShell\\v1.0\\powershell.exe", "poisoned\\powershell.exe").encode("ascii"))

    result = run(version / "install.cmd")

    assert result.returncode == 0 and marker.exists()


def fake_powershell(folder: Path) -> tuple[Path, Path]:
    """Create a stand-in for PowerShell that logs the list of paths the elevated check was asked about and says all is well.

    The ACL judge itself is the real one only in an elevated shell; this stand-in lets a normal shell see exactly which paths an
    elevated launcher hands to the check (the spec string it builds). Returns the stand-in and the log file it appends to.
    """
    log = folder / "spec-log.txt"
    script = folder / "fakeps.cmd"
    script.write_text('@echo off\r\nsetlocal EnableDelayedExpansion\r\n>> "%FAKEPS_LOG%" echo !GUPKG_ACL_PATHS!\r\necho GUPKG-ACL-OK\r\n', encoding="ascii")
    return script, log


def spec_lines(log: Path) -> list[list[str]]:
    """Return each logged spec as its list of ``kind:path`` entries."""
    return [line.strip().split("|") for line in log.read_text(encoding="ascii").splitlines() if line.strip()]


def test_an_elevated_check_of_a_gupkg_cmd_on_path_covers_its_whole_chain(tmp_path: Path) -> None:
    """For a gupkg.cmd on PATH the check is given the tree (recursive), its version folder, and the package and root folders above."""
    version, _marker = marker_tree(tmp_path / "root", location=False)
    (version / "gupkg" / "gupkg.cmd").unlink()
    app = tmp_path / "tools" / "root2" / "gupkg" / "current" / "App"
    write_fake(app, "PATHGUPKG", "PATH_EXIT")
    fake, log = fake_powershell(tmp_path)
    as_elevated(version, powershell=str(fake))

    result = run(version / "install.cmd", inherit=True, path=on_path(app), extra_env={"FAKEPS_LOG": str(log)})

    assert result.returncode == 0, result.stderr
    candidate = spec_lines(log)[-1]
    assert candidate == [f"v:{app.parent}", f"n:{app.parent.parent}", f"n:{app.parent.parent.parent}", f"r:{app}"]


def test_an_elevated_check_of_a_gupkg_exe_shim_covers_the_folder_that_holds_it_and_its_parent(tmp_path: Path) -> None:
    """A shim in a folder (and parent) that a user can change can be swapped whatever its own ACL says: both folders are checked."""
    version, _marker = marker_tree(tmp_path / "root", location=False)
    (version / "gupkg" / "gupkg.cmd").unlink()
    folder = tmp_path / "tools" / "bin"
    folder.mkdir(parents=True)
    shim = folder / "gupkg.exe"
    shim.write_bytes(Path(SYSTEM32, "whoami.exe").read_bytes())
    fake, log = fake_powershell(tmp_path)
    as_elevated(version, powershell=str(fake))

    run(version / "install.cmd", inherit=True, path=on_path(folder), extra_env={"FAKEPS_LOG": str(log)})

    assert spec_lines(log)[-1] == [f"n:{folder}", f"n:{folder.parent}", f"s:{shim}"]


def test_an_elevated_check_of_the_location_gupkg_covers_its_version_package_and_root_folders(tmp_path: Path) -> None:
    """A gupkg-location that names a tree under a user-writable parent is checked up the chain, as a bundled gupkg is."""
    version, _marker = marker_tree(tmp_path / "root", location=True)
    fake, log = fake_powershell(tmp_path)
    as_elevated(version, powershell=str(fake))

    result = run(version / "install.cmd", inherit=True, extra_env={"FAKEPS_LOG": str(log)})

    app = tmp_path / "root" / "gupkg" / "current" / "App"
    assert result.returncode == 0, result.stderr
    spec = spec_lines(log)[-1]
    assert spec[-4:] == [f"v:{app.parent}", f"n:{app.parent.parent}", f"n:{app.parent.parent.parent}", f"r:{app}"]
    assert f"n:{tmp_path / 'root' / '.gupkg' / 'gupkg-location'}" in spec


def run_checker(specs: str, *, constrained: bool = False) -> subprocess.CompletedProcess[str]:
    """Run the launcher's fixed PowerShell check directly, on *specs*, optionally in Constrained Language Mode."""
    text = launcher_text("install")
    script = text[text.rindex("#GUPKG-PS-BEGIN"):]
    prefix = "$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage'\n" if constrained else ""
    command = prefix + "Invoke-Expression $env:GUPKG_TEST_SCRIPT"
    return subprocess.run(
        [SYSTEM32 + "\\WindowsPowerShell\\v1.0\\powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, check=False, env={**os.environ, "GUPKG_ACL_PATHS": specs, "GUPKG_TEST_SCRIPT": script},
    )


def make_shim(folder: Path, target_line: str) -> Path:
    """Create `<folder>/gupkg.exe` with a config whose `target` line is *target_line*; return the shim."""
    folder.mkdir(parents=True)
    shim = folder / "gupkg.exe"
    shim.write_bytes(b"MZ")
    (folder / "gupkg.config.toml").write_text(target_line + "\nforward_arguments = true\n", encoding="utf-8")
    return shim


def test_the_checker_refuses_a_shim_whose_target_is_not_an_absolute_path(tmp_path: Path) -> None:
    """A relative target would be resolved against the checker's own current directory: it is refused with a clear line."""
    shim = make_shim(tmp_path / "bin", 'target = "gupkg.cmd"')
    (tmp_path / "bin" / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")

    result = run_checker(f"s:{shim}")

    assert result.returncode == 1
    assert "its target gupkg.cmd is not an absolute path" in result.stdout


@pytest.mark.parametrize("constrained", [False, True], ids=["full-language", "constrained-language"])
def test_the_checker_reads_a_shim_config_and_follows_its_target_even_in_constrained_language_mode(tmp_path: Path, constrained: bool) -> None:
    """The `s:` kind makes no .NET method call, so it works under Constrained Language Mode (design 21.3): the target is reached and judged."""
    target = tmp_path / "tree" / "App" / "gupkg.cmd"
    target.parent.mkdir(parents=True)
    target.write_text("@echo off\r\n", encoding="ascii")
    shim = make_shim(tmp_path / "bin", 'target = "' + str(target).replace("\\", "\\\\") + '"')

    result = run_checker(f"s:{shim}", constrained=constrained)

    # A temporary folder is user-writable, so the verdict is a refusal, but it is a verdict on the ACLs, not a crash on the config.
    assert result.returncode == 1 and "GUPKG-ACL-FAIL" in result.stdout
    assert "not an absolute path" not in result.stdout and "cannot be read" not in result.stdout and "Cannot invoke" not in result.stdout + result.stderr
    assert "is not a file" not in result.stdout


def _elevated() -> bool:
    """Return whether this process holds an elevated administrator token (`fltmc` needs one)."""
    return subprocess.run([SYSTEM32 + "\\fltmc.exe"], capture_output=True, check=False).returncode == 0


needs_admin = pytest.mark.skipif(os.name != "nt" or not _elevated(), reason="needs an elevated shell: manual smoke tests 033-1 and 035-T11-1 cover it")


def lock_down(folder: Path) -> None:
    """Make *folder* admin-only: owned by Administrators, with full control for Administrators and SYSTEM only (inherited by children)."""
    icacls = SYSTEM32 + "\\icacls.exe"
    folder.mkdir(parents=True, exist_ok=True)
    for arguments in (
        ["/inheritance:r"], ["/grant:r", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], ["/setowner", "*S-1-5-32-544"],
    ):
        subprocess.run([icacls, str(folder), *arguments], capture_output=True, check=True)


def grant_users_write(path: Path) -> None:
    """Give the Users group modify rights on *path* only (not inherited by anything else)."""
    subprocess.run([SYSTEM32 + "\\icacls.exe", str(path), "/grant", "*S-1-5-32-545:(M)"], capture_output=True, check=True)


def admin_tree(base: Path, *, location: bool) -> tuple[Path, Path, Path]:
    """Build an admin-only root with a package, a version folder, a `gupkg-location` or bundled gupkg and a Python tree; return root, version and marker."""
    root = base / "root"
    lock_down(root)
    version, marker = marker_tree(root, location=location)
    python = (root / "gupkg" / "1.0" / "App" if location else version / "gupkg" / "gupkg") / "python"
    python.mkdir(parents=True)
    (python / "python.exe").write_text("x", encoding="ascii")
    as_elevated(version)
    return root, version, marker


@needs_admin
@pytest.mark.parametrize("location", [True, False], ids=["gupkg-location", "bundled-copy"])
def test_an_elevated_launcher_in_an_admin_only_root_proceeds(tmp_path: Path, location: bool) -> None:
    """Under an admin-only root the check passes and the launcher runs the gupkg of `gupkg-location` or the bundled gupkg."""
    _root, version, marker = admin_tree(tmp_path, location=location)

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 0, result.stderr
    assert marker.exists()


@needs_admin
@pytest.mark.parametrize(
    "target", ["package", "version", "root-data", "location-file", "bundled-python-file", "root"], ids=lambda value: value,
)
def test_an_elevated_launcher_refuses_when_a_user_can_write_any_one_object_it_checks(tmp_path: Path, target: str) -> None:
    """Granting a standard user write on any one checked object (root, package, version, data folder, location file, a Python file) makes it refuse."""
    location = target in ("root-data", "location-file")
    root, version, marker = admin_tree(tmp_path, location=location)
    chosen = {
        "package": version.parent, "version": version, "root-data": root / ".gupkg", "location-file": root / ".gupkg" / "gupkg-location",
        "bundled-python-file": version / "gupkg" / "gupkg" / "python" / "python.exe", "root": root,
    }[target]
    grant_users_write(chosen)

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1 and "Refusing to run elevated" in result.stderr
    assert not marker.exists()


@needs_admin
def test_an_elevated_launcher_refuses_when_a_user_can_write_a_file_deep_in_the_gupkg_of_the_location(tmp_path: Path) -> None:
    """The check is recursive: a writable file deep inside the gupkg that `gupkg-location` names makes the run refuse."""
    root, version, marker = admin_tree(tmp_path, location=True)
    grant_users_write(root / "gupkg" / "1.0" / "App" / "python" / "python.exe")

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1 and not marker.exists()


@needs_admin
def test_an_elevated_launcher_skips_a_user_writable_path_entry_and_names_it(tmp_path: Path) -> None:
    """A gupkg.cmd in a user-writable folder on PATH is skipped (and named); the next admin-only entry's gupkg runs."""
    root, version, marker = admin_tree(tmp_path, location=False)
    (version / "gupkg" / "gupkg.cmd").unlink()
    user_folder = tmp_path / "user-bin"
    user_folder.mkdir()
    (user_folder / "gupkg.cmd").write_text(MARKER.format(marker=tmp_path / "user-ran.txt"), encoding="ascii")
    admin_folder = root / "admin-bin"
    lock_down(admin_folder)
    (admin_folder / "gupkg.cmd").write_text(MARKER.format(marker=marker), encoding="ascii")

    result = run(version / "install.cmd", inherit=True, path=f"{user_folder};{admin_folder};{SYSTEM32}")

    assert result.returncode == 0, result.stderr
    assert marker.exists() and not (tmp_path / "user-ran.txt").exists()
    assert f"Skipped PATH entry {user_folder}" in result.stderr


@needs_admin
def test_an_elevated_launcher_refuses_a_gupkg_exe_shim_whose_config_a_user_can_write(tmp_path: Path) -> None:
    """For a gupkg.exe shim the check covers the shim, its config and the target; a user-writable config skips the entry."""
    root, version, marker = admin_tree(tmp_path, location=False)
    (version / "gupkg" / "gupkg.cmd").unlink()
    folder = root / "shims"
    lock_down(folder)
    shim = folder / "gupkg.exe"
    shim.write_bytes(Path(SYSTEM32, "whoami.exe").read_bytes())
    target = root / "gupkg" / "1.0" / "App" / "gupkg.cmd"
    target.parent.mkdir(parents=True)
    target.write_text(MARKER.format(marker=marker), encoding="ascii")
    config = folder / "gupkg.config.toml"
    config.write_text('target = "' + str(target).replace("\\", "\\\\") + '"\n', encoding="utf-8")
    grant_users_write(config)

    result = run(version / "install.cmd", inherit=True, path=f"{folder};{SYSTEM32}")

    assert result.returncode == 9009 and not marker.exists()
    assert f"Skipped PATH entry {folder}" in result.stderr


@needs_admin
def test_a_launcher_run_through_current_proceeds_only_when_current_names_a_version_of_the_same_package(tmp_path: Path) -> None:
    """Through `current` the check follows the junction: it proceeds for a sibling version folder and refuses for any other target."""
    root, version, marker = admin_tree(tmp_path, location=False)
    package = version.parent
    current = package / "current"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(current), str(version)], capture_output=True, check=True)

    proceeded = run(current / "install.cmd", inherit=True)

    assert proceeded.returncode == 0, proceeded.stderr
    assert marker.exists()
    marker.unlink()

    # Point current at an admin-only folder that is not a version folder of this package.
    subprocess.run(["cmd", "/c", "rmdir", str(current)], capture_output=True, check=True)
    elsewhere = root / "other"
    elsewhere.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(current), str(elsewhere)], capture_output=True, check=True)
    (elsewhere / "install.cmd").write_bytes((version / "install.cmd").read_bytes())
    (elsewhere / "gupkg").mkdir()
    (elsewhere / "gupkg" / "gupkg.cmd").write_text(MARKER.format(marker=marker), encoding="ascii")

    refused = run(current / "install.cmd", inherit=True)

    assert refused.returncode == 1 and not marker.exists()
