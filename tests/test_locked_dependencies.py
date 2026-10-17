"""Cover hash-pinned dependency repair and update for elevated gupkg installs.

Behavior covered: lockfile reading and writing, the refusal to run unless
elevated, that ``repair-deps`` replaces the live dependency directory only after
a verified staged install, that ``update-deps`` shows a plan and changes nothing
unless it is confirmed and succeeds, and the command-line exit codes.

Mocked boundaries: the subprocess that runs the bundled pip (a fake pip creates
files in the requested target and writes a resolution report), the process's
elevation state, and the access-control check. Real: the filesystem, the lock
format, and the staging and swap logic. Out of scope: real network access and
real wheels.
"""

from __future__ import annotations

import io
import json
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from gupkg import cli, locked_deps
from gupkg.locked_deps import LockedRequirement

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
OLD_LOCK = f"textual==1.0.0 --hash=sha256:{HASH_A}\npip==24.0 --hash=sha256:{HASH_B}\n"


class FakePip:
    """Stand in for the bundled interpreter: install into --target, resolve, and probe."""

    def __init__(self, *, fail_install: bool = False, fail_probe: bool = False, report=None, fail_resolve=False) -> None:
        self.fail_install = fail_install
        self.fail_probe = fail_probe
        self.fail_resolve = fail_resolve
        self.report = report or []
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "--report" in command:
            if self.fail_resolve:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="no network")
            Path(command[command.index("--report") + 1]).write_text(
                json.dumps({"install": self.report}), encoding="utf-8"
            )
            return ok
        if "--target" in command:
            if self.fail_install:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="hash mismatch")
            target = Path(command[command.index("--target") + 1])
            lock_text = Path(command[command.index("-r") + 1]).read_text(encoding="utf-8")
            (target / "installed.lock").write_text(lock_text, encoding="utf-8")
            return ok
        if "-c" in command:
            if self.fail_probe:
                return types.SimpleNamespace(returncode=1, stdout="textual.app: boom", stderr="")
            return ok
        raise AssertionError(f"unexpected command {command}")


def _release(name: str, version: str, digest: str) -> dict:
    """Build one entry of a pip installation report."""
    return {
        "metadata": {"name": name, "version": version},
        "download_info": {"archive_info": {"hashes": {"sha256": digest}}},
    }


@pytest.fixture
def package(tmp_path: Path):
    """A package directory with a bundled python, a lockfile, and a live dependency directory."""
    root = tmp_path / "gupkg"
    live = root / "python" / "Lib" / "site-packages"
    live.mkdir(parents=True)
    (root / "python" / "python.exe").touch()
    (live / "old.txt").write_text("old", encoding="utf-8")
    (root / locked_deps.LOCK_NAME).write_text(OLD_LOCK, encoding="utf-8")
    return root


@pytest.fixture
def elevated():
    """Pretend to be an elevated process on admin-only directories."""
    with (
        mock.patch.object(locked_deps, "elevated_mode", return_value=True),
        mock.patch.object(locked_deps, "require_trusted_directory"),
    ):
        yield


def test_lockfile_round_trips_and_rejects_unhashed_requirements(tmp_path: Path) -> None:
    """A lockfile keeps its pins and hashes, and a line without a hash is rejected."""
    entries = [LockedRequirement("Pygments", "2.21.0", (HASH_A, HASH_B)), LockedRequirement("textual", "8.2.8", (HASH_C,))]
    path = tmp_path / "lock"
    path.write_text(locked_deps.format_lock(entries), encoding="utf-8")

    assert locked_deps.read_lock(path) == entries

    path.write_text("textual==8.2.8\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Malformed or unhashed"):
        locked_deps.read_lock(path)


def test_repair_requires_an_elevated_process(package: Path) -> None:
    """Dependency repair refuses to run, and runs no subprocess, without elevation."""
    pip = FakePip()
    with (
        mock.patch.object(locked_deps, "elevated_mode", return_value=False),
        mock.patch.object(locked_deps.subprocess, "run", pip),
        pytest.raises(locked_deps.NotElevatedError),
    ):
        locked_deps.repair_deps(package, progress=lambda line: None)

    assert pip.commands == []
    assert (package / "python" / "Lib" / "site-packages" / "old.txt").exists()


def test_repair_installs_only_the_pinned_hashed_wheels_and_replaces_the_live_directory(package: Path, elevated) -> None:
    """Repair installs the lockfile with hashes required and wheels only, then swaps it in."""
    pip = FakePip()
    with mock.patch.object(locked_deps.subprocess, "run", pip):
        locked_deps.repair_deps(package, progress=lambda line: None)

    install = next(command for command in pip.commands if "--target" in command)
    for required in ("--require-hashes", "--only-binary=:all:", "--no-deps", "-I"):
        assert required in install
    live = package / "python" / "Lib" / "site-packages"
    assert (live / "installed.lock").read_text(encoding="utf-8") == OLD_LOCK
    assert not (live / "old.txt").exists()
    assert not live.with_name("site-packages.staging").exists()
    assert not live.with_name("site-packages.old").exists()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"fail_install": True}, id="hash-mismatch-or-missing-wheel"),
        pytest.param({"fail_probe": True}, id="staged-copy-does-not-import"),
    ],
)
def test_failed_repair_leaves_the_live_directory_untouched(package: Path, elevated, failure: dict) -> None:
    """A failed install or import check aborts before the swap and leaves the live install as it was."""
    with (
        mock.patch.object(locked_deps.subprocess, "run", FakePip(**failure)),
        pytest.raises(RuntimeError, match="unchanged"),
    ):
        locked_deps.repair_deps(package, progress=lambda line: None)

    live = package / "python" / "Lib" / "site-packages"
    assert (live / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (live / "installed.lock").exists()
    assert not live.with_name("site-packages.staging").exists()


def test_update_shows_the_plan_and_changes_nothing_when_declined(package: Path, elevated) -> None:
    """Declining the plan leaves both the live directory and the lockfile as they were."""
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("pip", "24.0", HASH_B)])
    shown: list[list[str]] = []

    def decline(lines: list[str]) -> bool:
        shown.append(lines)
        return False

    with mock.patch.object(locked_deps.subprocess, "run", pip):
        applied = locked_deps.update_deps(package, confirm=decline, progress=lambda line: None)

    assert applied == []
    assert any("textual 1.0.0 -> 2.0.0" in line for line in shown[0])
    assert (package / locked_deps.LOCK_NAME).read_text(encoding="utf-8") == OLD_LOCK
    assert (package / "python" / "Lib" / "site-packages" / "old.txt").exists()
    assert not any("--target" in command for command in pip.commands)


def test_confirmed_update_installs_the_new_pins_and_rewrites_the_lockfile(package: Path, elevated) -> None:
    """After confirmation the new versions are installed from a temporary lock and then recorded."""
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("pip", "24.0", HASH_B), _release("rich", "1.0", HASH_A)])

    with mock.patch.object(locked_deps.subprocess, "run", pip):
        applied = locked_deps.update_deps(package, confirm=lambda lines: True, progress=lambda line: None)

    assert sorted(entry.name for entry in applied) == ["rich", "textual"]
    new_lock = (package / locked_deps.LOCK_NAME).read_text(encoding="utf-8")
    assert f"textual==2.0.0 --hash=sha256:{HASH_C}" in new_lock
    live = package / "python" / "Lib" / "site-packages"
    assert (live / "installed.lock").read_text(encoding="utf-8") == new_lock
    assert not (package / (locked_deps.LOCK_NAME + ".new")).exists()


def test_failed_update_install_does_not_change_the_lockfile(package: Path, elevated) -> None:
    """If the new set cannot be installed, neither the live directory nor the lockfile changes."""
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("pip", "24.0", HASH_B)], fail_install=True)

    with mock.patch.object(locked_deps.subprocess, "run", pip), pytest.raises(RuntimeError):
        locked_deps.update_deps(package, confirm=lambda lines: True, progress=lambda line: None)

    assert (package / locked_deps.LOCK_NAME).read_text(encoding="utf-8") == OLD_LOCK
    assert (package / "python" / "Lib" / "site-packages" / "old.txt").exists()
    assert not (package / (locked_deps.LOCK_NAME + ".new")).exists()


def test_update_aborts_with_a_clear_message_when_pypi_is_unreachable(package: Path, elevated) -> None:
    """An unreachable index aborts the update and leaves the existing install alone."""
    with (
        mock.patch.object(locked_deps.subprocess, "run", FakePip(fail_resolve=True)),
        pytest.raises(RuntimeError, match="PyPI reachable"),
    ):
        locked_deps.update_deps(package, confirm=lambda lines: True, progress=lambda line: None)

    assert (package / locked_deps.LOCK_NAME).read_text(encoding="utf-8") == OLD_LOCK


def test_update_refuses_releases_that_are_not_plain_stable_versions(package: Path, elevated) -> None:
    """A pre-release or oddly named release in the resolution is rejected before anything is installed."""
    pip = FakePip(report=[_release("textual", "2.0.0rc1", HASH_C)])

    with mock.patch.object(locked_deps.subprocess, "run", pip), pytest.raises(RuntimeError, match="stable"):
        locked_deps.update_deps(package, confirm=lambda lines: True, progress=lambda line: None)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def test_repair_deps_command_fails_with_a_user_error_when_not_elevated() -> None:
    """`gupkg manager self repair-deps` exits with the user-error status outside an elevated prompt."""
    output = io.StringIO()
    with (
        mock.patch.object(locked_deps, "elevated_mode", return_value=False),
        redirect_stdout(output),
        mock.patch("sys.stderr", io.StringIO()) as errors,
    ):
        status = cli.main(["manager", "self", "repair-deps"])

    assert status == 2
    assert "elevated" in errors.getvalue()


def test_update_deps_command_needs_yes_when_it_cannot_ask(package: Path, elevated) -> None:
    """Without --yes and without a terminal, an update plan is refused rather than applied."""
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("pip", "24.0", HASH_B)])
    errors = io.StringIO()

    with (
        mock.patch.object(locked_deps, "__file__", str(package / "locked_deps.py")),
        mock.patch.object(locked_deps, "enforce_elevated_runtime"),
        mock.patch.object(locked_deps.subprocess, "run", pip),
        mock.patch("sys.stdin", io.StringIO()),
        mock.patch("sys.stderr", errors),
        redirect_stdout(io.StringIO()),
    ):
        status = cli.main(["manager", "self", "update-deps"])

    assert status == 2
    assert "--yes" in errors.getvalue()
    assert (package / locked_deps.LOCK_NAME).read_text(encoding="utf-8") == OLD_LOCK
    assert not any("--target" in command for command in pip.commands)
