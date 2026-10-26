r"""Cover the package launchers (`install.cmd`, `gupkg-tui.cmd`, `check-updates.cmd`): which gupkg runs and when elevated runs refuse.

Behavior covered: a launcher runs the root forwarder `<root>\.gupkg\gupkg.cmd` first and the bundled
`gupkg\gupkg.cmd` otherwise; `--no-system-gupkg=true` skips the forwarder and is not passed on; exit
code 6 from the forwarder re-runs the command with the bundled gupkg; with neither it exits 9009 and says
how to get gupkg; the exit code of gupkg is the launcher's own; `PATH` and the current directory are never
searched; `check-updates.cmd` runs `check-updates <package name>` and nothing else; an elevated launcher
(simulated, see `as_elevated`) refuses before any gupkg starts when anything it would run can be written by
a standard user, or when PowerShell cannot be used (fail closed), and, in an elevated shell, proceeds in an
admin-only tree and refuses once a standard user is granted write on any one object it checks.

Real: the generated launchers run by the real `cmd.exe` in a temporary tree, with fake `gupkg.cmd` files
that print their arguments; the access check runs the real System32 PowerShell. Mocked: gupkg itself.
Out of scope: UAC itself and the integrity-level detection of a really elevated token (manual smoke test
033-1); the tests that need an administrator token skip themselves in a standard shell.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gupkg.launchers import LAUNCHERS, launcher_text, write_launchers

pytestmark = [pytest.mark.skipif(os.name != "nt", reason="the launchers are Windows cmd files"), pytest.mark.windows_integration]

FAKE = '@echo off\r\necho {label} %*\r\nexit /b %{variable}%\r\n'


def build_tree(root: Path, *, forwarder: bool, bundled: bool) -> Path:
    """Create `<root>/tool/1.0` with launchers, and fake forwarder and bundled gupkg as asked."""
    version = root / "tool" / "1.0"
    version.mkdir(parents=True)
    write_launchers(version)
    if forwarder:
        (root / ".gupkg").mkdir()
        (root / ".gupkg" / "gupkg.cmd").write_text(FAKE.format(label="FORWARDER", variable="FORWARDER_EXIT"), encoding="ascii")
        # The gupkg the forwarder names: without it the forwarder is broken and is not used.
        (root / "gupkg" / "current" / "App").mkdir(parents=True)
        (root / "gupkg" / "current" / "App" / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")
    if bundled:
        (version / "gupkg").mkdir()
        (version / "gupkg" / "gupkg.cmd").write_text(FAKE.format(label="BUNDLED", variable="BUNDLED_EXIT"), encoding="ascii")
    return version


def run(
    launcher: Path, *args: str, forwarder_exit: int = 0, bundled_exit: int = 0, path: str = "", cwd: Path | None = None,
    inherit: bool = False, pause: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a launcher with cmd.exe and return its output and exit code.

    The environment is minimal unless *inherit* is true (the access check needs PowerShell's usual environment).
    """
    environment = {
        "SystemRoot": os.environ["SystemRoot"], "ComSpec": os.environ["ComSpec"], "GUPKG_NO_PAUSE": "1",
        "FORWARDER_EXIT": str(forwarder_exit), "BUNDLED_EXIT": str(bundled_exit), "PATH": path or os.environ["SystemRoot"] + "\\System32",
    }
    if inherit:
        environment = {**os.environ, **environment, "PATH": os.environ["PATH"]}
    if pause:
        environment.pop("GUPKG_NO_PAUSE")
    return subprocess.run(
        [os.environ["ComSpec"], "/d", "/c", str(launcher), *args],
        capture_output=True, text=True, env=environment, check=False, cwd=str(cwd or launcher.parent),
        input="\r\n" if pause else None,
    )


def test_both_launchers_come_from_one_template_with_different_commands() -> None:
    """`install.cmd` runs `install` and `gupkg-tui.cmd` runs `tui`; everything else is identical."""
    install, tui = launcher_text("install"), launcher_text("tui")

    assert set(LAUNCHERS) == {"install.cmd", "gupkg-tui.cmd", "check-updates.cmd"}
    differing = ("if not defined GUPKG_NO_PAUSE pause", 'if not "%GUPKG_EXIT%"=="0" if not defined GUPKG_NO_PAUSE pause')
    assert install.replace(" install ", " tui ").replace(*differing, 1).splitlines()[3:] == tui.splitlines()[3:]
    assert "\r\n" in install


def test_forwarder_runs_first_with_the_command_and_this_folder(tmp_path: Path) -> None:
    """The installed gupkg of the root is used when it exists, called with `install` and the version folder."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", "--refresh")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("FORWARDER install ")
    assert f'"{version}\\."' in result.stdout and "--refresh" in result.stdout
    assert "BUNDLED" not in result.stdout


def test_install_and_check_updates_launchers_always_wait_for_a_key_and_the_tui_launcher_only_after_a_failure(tmp_path: Path) -> None:
    """Double-clicking install.cmd or check-updates.cmd keeps the window open on success and failure; gupkg-tui.cmd waits only after a failure."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)
    tracker = tmp_path / "tool" / "_check_updates"
    tracker.mkdir()
    write_launchers(tracker, ("check-updates.cmd",))

    for launcher, waits_on_success in ((version / "install.cmd", True), (tracker / "check-updates.cmd", True), (version / "gupkg-tui.cmd", False)):
        assert ("Press any key" in run(launcher, pause=True).stdout) is waits_on_success, launcher.name
        assert "Press any key" in run(launcher, forwarder_exit=3, pause=True).stdout, launcher.name


def test_no_pause_variable_turns_the_pause_off_even_for_install(tmp_path: Path) -> None:
    """Scripts (the bootstrap) set GUPKG_NO_PAUSE so install.cmd returns at once."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)

    assert "Press any key" not in run(version / "install.cmd").stdout


def make_root_gupkg(root: Path, version: str, *, label: str, current: bool = False) -> Path:
    """Create `<root>/gupkg/<version>` with a bundled gupkg that echoes *label* (and `<root>/gupkg/current/App` when asked)."""
    folder = root / "gupkg" / version
    (folder / "gupkg").mkdir(parents=True)
    (folder / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
    (folder / "gupkg" / "gupkg.cmd").write_text(FAKE.format(label=label, variable="BUNDLED_EXIT"), encoding="ascii")
    if current:
        (root / "gupkg" / "current" / "App").mkdir(parents=True)
        (root / "gupkg" / "current" / "App" / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")
    return folder


def test_a_forwarder_whose_gupkg_is_gone_is_not_used_and_the_bundled_gupkg_runs(tmp_path: Path) -> None:
    """A root forwarder is broken when <root>\\gupkg\\current\\App\\gupkg.cmd is missing: the launcher says so and uses the bundled copy."""
    version = build_tree(tmp_path, forwarder=False, bundled=True)
    (tmp_path / ".gupkg").mkdir()
    (tmp_path / ".gupkg" / "gupkg.cmd").write_text(FAKE.format(label="FORWARDER", variable="FORWARDER_EXIT"), encoding="ascii")

    result = run(version / "install.cmd")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("BUNDLED install ") and "FORWARDER" not in result.stdout
    assert "The installed gupkg is broken" in result.stderr


def test_a_broken_installed_gupkg_is_repaired_from_the_newest_gupkg_folder_of_the_root_and_the_command_runs(tmp_path: Path) -> None:
    """With no bundled copy, the newest <root>\\gupkg\\<version> that carries a gupkg repairs the installed one (install of that folder) and runs the command."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)
    (tmp_path / ".gupkg").mkdir()
    (tmp_path / ".gupkg" / "gupkg.cmd").write_text(FAKE.format(label="FORWARDER", variable="FORWARDER_EXIT"), encoding="ascii")
    old = make_root_gupkg(tmp_path, "1.0.2", label="OLD")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    newest = make_root_gupkg(tmp_path, "1.0.5", label="NEWEST")

    result = run(version / "install.cmd", "--yes")

    lines = result.stdout.splitlines()
    assert result.returncode == 0, result.stderr
    assert lines[0].startswith('NEWEST "install" ') and f'"{newest}\\."' in lines[0]
    assert lines[1].startswith("NEWEST install ") and "--yes" in lines[1] and f'"{version}\\."' in lines[1]
    assert "OLD" not in result.stdout and "Repairing it from" in result.stderr


def test_an_elevated_launcher_does_not_repair_from_a_gupkg_folder_a_standard_user_can_change(tmp_path: Path) -> None:
    """In a user-writable tree an elevated launcher with a broken installed gupkg refuses the root's gupkg folder: nothing of it runs."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)
    (tmp_path / ".gupkg").mkdir()
    (tmp_path / ".gupkg" / "gupkg.cmd").write_text(FAKE.format(label="FORWARDER", variable="FORWARDER_EXIT"), encoding="ascii")
    make_root_gupkg(tmp_path, "1.0.5", label="NEWEST")
    as_elevated(version)

    result = run(version / "install.cmd", inherit=True)

    assert "NEWEST" not in result.stdout and "cannot be trusted in an elevated run" in result.stderr
    assert result.returncode == 9009


def test_a_working_installed_gupkg_is_never_replaced_by_a_root_gupkg_folder(tmp_path: Path) -> None:
    """When the forwarder works, the gupkg folders of the root play no part."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)
    make_root_gupkg(tmp_path, "1.0.5", label="NEWEST")

    result = run(version / "install.cmd")

    assert result.stdout.startswith("FORWARDER install ") and "NEWEST" not in result.stdout and "broken" not in result.stderr


def test_tui_launcher_runs_the_tui_command(tmp_path: Path) -> None:
    """`gupkg-tui.cmd` asks gupkg for its text interface on this folder."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)

    assert run(version / "gupkg-tui.cmd").stdout.startswith("FORWARDER tui ")


def test_bundled_gupkg_is_used_when_the_root_has_no_forwarder(tmp_path: Path) -> None:
    """Without a root forwarder the gupkg bundled in the version folder runs."""
    version = build_tree(tmp_path, forwarder=False, bundled=True)

    result = run(version / "install.cmd")

    assert result.stdout.startswith("BUNDLED install ")


def test_no_system_gupkg_option_skips_the_forwarder_and_is_not_passed_on(tmp_path: Path) -> None:
    """`--no-system-gupkg=true` forces the bundled gupkg and is consumed by the launcher."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", "--no-system-gupkg=true", "--yes")

    assert result.stdout.startswith("BUNDLED install ")
    assert result.stdout.rstrip().endswith('1.0\\." "--yes"')


def test_no_system_gupkg_false_keeps_the_forwarder(tmp_path: Path) -> None:
    """`--no-system-gupkg=false` is consumed too and leaves the forwarder in use."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", "--no-system-gupkg=false", "--yes")

    assert result.stdout.startswith("FORWARDER install ")
    assert result.stdout.rstrip().endswith('1.0\\." "--yes"')


def test_exit_code_6_from_the_forwarder_falls_back_to_the_bundled_gupkg(tmp_path: Path) -> None:
    """An installed gupkg that cannot handle the package exits 6; the launcher says so and re-runs with the bundled one."""
    version = build_tree(tmp_path, forwarder=True, bundled=True)

    result = run(version / "install.cmd", forwarder_exit=6, bundled_exit=0)

    assert result.returncode == 0
    assert "FORWARDER install" in result.stdout and "BUNDLED install" in result.stdout
    assert "cannot handle this package" in result.stderr


def test_exit_code_6_without_a_bundled_gupkg_is_returned_unchanged(tmp_path: Path) -> None:
    """With no bundled gupkg to fall back to, exit code 6 is the launcher's exit code."""
    version = build_tree(tmp_path, forwarder=True, bundled=False)

    assert run(version / "install.cmd", forwarder_exit=6).returncode == 6


def test_the_exit_code_of_gupkg_is_the_launchers_exit_code(tmp_path: Path) -> None:
    """A failing gupkg makes the launcher fail with the same code."""
    version = build_tree(tmp_path, forwarder=False, bundled=True)

    assert run(version / "install.cmd", bundled_exit=3).returncode == 3


def test_without_any_gupkg_the_launcher_exits_9009_and_says_how_to_get_one(tmp_path: Path) -> None:
    """With neither a forwarder nor a bundled gupkg the launcher exits 9009 and names the bootstrap."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)

    result = run(version / "install.cmd")

    assert result.returncode == 9009
    assert "gupkg-bootstrap.cmd" in result.stderr


def test_a_gupkg_on_path_is_never_used(tmp_path: Path) -> None:
    """A `gupkg.cmd` and `gupkg.exe` earlier on PATH are ignored: the launcher does not search PATH."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    (decoy / "gupkg.cmd").write_text(FAKE.format(label="PATHGUPKG", variable="BUNDLED_EXIT"), encoding="ascii")
    (decoy / "gupkg.exe").write_text("not a program", encoding="ascii")

    result = run(version / "install.cmd", path=f"{decoy};{os.environ['SystemRoot']}\\System32")

    assert result.returncode == 9009
    assert "PATHGUPKG" not in result.stdout


def test_a_gupkg_exe_in_the_current_directory_is_never_used(tmp_path: Path) -> None:
    """A `gupkg.cmd` sitting in the directory the launcher is started from is ignored."""
    version = build_tree(tmp_path, forwarder=False, bundled=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "gupkg.cmd").write_text(FAKE.format(label="CWDGUPKG", variable="BUNDLED_EXIT"), encoding="ascii")

    result = run(version / "install.cmd", cwd=elsewhere)

    assert result.returncode == 9009
    assert "CWDGUPKG" not in result.stdout


def test_check_updates_launcher_checks_the_package_named_by_its_folder_and_installs_nothing(tmp_path: Path) -> None:
    """`check-updates.cmd` in `<root>/<name>/_check_updates` runs `check-updates <name>` through the root forwarder."""
    tracker = tmp_path / "my tool" / "_check_updates"
    tracker.mkdir(parents=True)
    write_launchers(tracker, ("check-updates.cmd",))
    (tmp_path / ".gupkg").mkdir()
    (tmp_path / ".gupkg" / "gupkg.cmd").write_text(FAKE.format(label="FORWARDER", variable="FORWARDER_EXIT"), encoding="ascii")
    (tmp_path / "gupkg" / "current" / "App").mkdir(parents=True)
    (tmp_path / "gupkg" / "current" / "App" / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")

    result = run(tracker / "check-updates.cmd")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'FORWARDER check-updates "my tool"'


def test_check_updates_launcher_without_a_gupkg_exits_9009(tmp_path: Path) -> None:
    """A tracker launcher in a root with no installed gupkg says how to get one (a tracker carries no bundled copy)."""
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
        text = launcher.read_text(encoding="ascii")
        assert "call :integrity_level" in text
        text = text.replace("call :integrity_level", 'set "GUPKG_ELEVATED=yes"')
        if powershell is not None:
            text = text.replace("%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", powershell)
        launcher.write_bytes(text.encode("ascii"))


def marker_tree(root: Path, *, forwarder: bool) -> tuple[Path, Path]:
    """Create `<root>/tool/1.0` whose forwarder (or bundled) gupkg writes a marker file; return the version folder and the marker."""
    marker = root.parent / f"{root.name}-started.txt"
    version = root / "tool" / "1.0"
    version.mkdir(parents=True)
    write_launchers(version)
    target = root / ".gupkg" if forwarder else version / "gupkg"
    target.mkdir(parents=True)
    (target / "gupkg.cmd").write_text(MARKER.format(marker=marker), encoding="ascii")
    if forwarder:
        (root / "gupkg" / "current" / "App").mkdir(parents=True)
        (root / "gupkg" / "current" / "App" / "gupkg.cmd").write_text("@echo off\r\n", encoding="ascii")
    return version, marker


@pytest.mark.parametrize("forwarder", [True, False], ids=["root-forwarder", "bundled-copy"])
def test_an_elevated_launcher_in_a_user_writable_tree_refuses_before_any_gupkg_starts(tmp_path: Path, forwarder: bool) -> None:
    """In a folder a standard user owns (Downloads, a user root) an elevated launcher exits 1 and starts nothing."""
    version, marker = marker_tree(tmp_path / "root", forwarder=forwarder)
    as_elevated(version)

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1
    assert "Refusing to run elevated" in result.stderr and "GUPKG-ACL-FAIL" in result.stderr
    assert not marker.exists()


def test_an_elevated_launcher_refuses_when_powershell_cannot_be_used(tmp_path: Path) -> None:
    """The check fails closed: with PowerShell missing the launcher refuses instead of trusting the folders."""
    version, marker = marker_tree(tmp_path / "root", forwarder=False)
    as_elevated(version, powershell=str(tmp_path / "no-such-dir" / "powershell.exe"))

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1 and "Refusing to run elevated" in result.stderr
    assert not marker.exists()


def test_a_launcher_that_cannot_tell_whether_it_is_elevated_refuses(tmp_path: Path) -> None:
    """With no usable `whoami` the launcher refuses rather than assuming a standard token (and so skipping the check)."""
    version, marker = marker_tree(tmp_path / "root", forwarder=False)
    text = (version / "install.cmd").read_text(encoding="ascii").replace("whoami.exe", "no-such-whoami.exe")
    (version / "install.cmd").write_bytes(text.encode("ascii"))

    result = run(version / "install.cmd")

    assert result.returncode == 1 and "Cannot tell whether this run is elevated" in result.stderr
    assert not marker.exists()


def test_a_standard_run_never_starts_powershell(tmp_path: Path) -> None:
    """A standard (not elevated) launcher runs gupkg without the access check, so the check costs nothing there."""
    version, marker = marker_tree(tmp_path / "root", forwarder=False)
    text = (version / "install.cmd").read_text(encoding="ascii")
    (version / "install.cmd").write_bytes(text.replace("WindowsPowerShell\\v1.0\\powershell.exe", "poisoned\\powershell.exe").encode("ascii"))

    result = run(version / "install.cmd")

    assert result.returncode == 0 and marker.exists()


def _elevated() -> bool:
    """Return whether this process holds an elevated administrator token (`fltmc` needs one)."""
    return subprocess.run([os.environ["SystemRoot"] + "\\System32\\fltmc.exe"], capture_output=True, check=False).returncode == 0


needs_admin = pytest.mark.skipif(os.name != "nt" or not _elevated(), reason="needs an elevated shell: manual smoke test 033-1 covers it")


def lock_down(folder: Path) -> None:
    """Make *folder* admin-only: owned by Administrators, with full control for Administrators and SYSTEM only (inherited by children)."""
    icacls = os.environ["SystemRoot"] + "\\System32\\icacls.exe"
    folder.mkdir(parents=True, exist_ok=True)
    for arguments in (
        ["/inheritance:r"], ["/grant:r", "*S-1-5-32-544:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], ["/setowner", "*S-1-5-32-544"],
    ):
        subprocess.run([icacls, str(folder), *arguments], capture_output=True, check=True)


def grant_users_write(path: Path) -> None:
    """Give the Users group modify rights on *path* only (not inherited by anything else)."""
    subprocess.run([os.environ["SystemRoot"] + "\\System32\\icacls.exe", str(path), "/grant", "*S-1-5-32-545:(M)"], capture_output=True, check=True)


def admin_tree(base: Path, *, forwarder: bool) -> tuple[Path, Path, Path]:
    """Build an admin-only root with a package, a version folder, a root forwarder or bundled gupkg and a Python tree; return root, version and marker."""
    root = base / "root"
    lock_down(root)
    version, marker = marker_tree(root, forwarder=forwarder)
    if forwarder:
        # The installed gupkg of this root: gupkg\current -> gupkg\1.0 with App\... below it.
        app = root / "gupkg" / "1.0" / "App" / "gupkg" / "python"
        app.mkdir(parents=True)
        (app / "python.exe").write_text("x", encoding="ascii")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "gupkg" / "current"), str(root / "gupkg" / "1.0")], capture_output=True, check=True)
    else:
        python = version / "gupkg" / "gupkg" / "python"
        python.mkdir(parents=True)
        (python / "python.exe").write_text("x", encoding="ascii")
    as_elevated(version)
    return root, version, marker


@needs_admin
@pytest.mark.parametrize("forwarder", [True, False], ids=["root-forwarder", "bundled-copy"])
def test_an_elevated_launcher_in_an_admin_only_root_proceeds(tmp_path: Path, forwarder: bool) -> None:
    """Under an admin-only root the check passes and the launcher runs the forwarder or the bundled gupkg."""
    _root, version, marker = admin_tree(tmp_path, forwarder=forwarder)

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 0, result.stderr
    assert marker.exists()


@needs_admin
@pytest.mark.parametrize(
    "target", ["package", "version", "root-data", "forwarder", "bundled-python-file", "root"], ids=lambda value: value,
)
def test_an_elevated_launcher_refuses_when_a_user_can_write_any_one_object_it_checks(tmp_path: Path, target: str) -> None:
    """Granting a standard user write on any one checked object (root, package, version, data folder, forwarder, a Python file) makes it refuse."""
    forwarder = target in ("root-data", "forwarder")
    root, version, marker = admin_tree(tmp_path, forwarder=forwarder)
    chosen = {
        "package": version.parent, "version": version, "root-data": root / ".gupkg", "forwarder": root / ".gupkg" / "gupkg.cmd",
        "bundled-python-file": version / "gupkg" / "gupkg" / "python" / "python.exe", "root": root,
    }[target]
    grant_users_write(chosen)

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1 and "Refusing to run elevated" in result.stderr
    assert not marker.exists()


@needs_admin
def test_an_elevated_launcher_refuses_when_a_user_can_write_a_file_deep_in_the_installed_gupkg(tmp_path: Path) -> None:
    """The check is recursive: a writable file deep inside the installed gupkg's App makes the forwarder run refuse."""
    root, version, marker = admin_tree(tmp_path, forwarder=True)
    grant_users_write(root / "gupkg" / "1.0" / "App" / "gupkg" / "python" / "python.exe")

    result = run(version / "install.cmd", inherit=True)

    assert result.returncode == 1 and not marker.exists()


@needs_admin
def test_a_launcher_run_through_current_proceeds_only_when_current_names_a_version_of_the_same_package(tmp_path: Path) -> None:
    """Through `current` the check follows the junction: it proceeds for a sibling version folder and refuses for any other target."""
    root, version, marker = admin_tree(tmp_path, forwarder=False)
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
