"""Cover the maintainer tool that updates or refreshes the locked runtime dependencies.

Behavior covered: lock file reading and writing, that ``--repair`` replaces the
runtime's ``Lib/site-packages`` only after a verified staged install, that an
update shows a plan and changes nothing unless it is confirmed and succeeds, and
the command-line behavior without a terminal, and that the runtime has no pip: the
installing interpreter (the build machine's) does the work, pip is never locked,
resolved or staged.

Mocked boundaries: the subprocess that runs pip and the probe (a fake pip creates
files in the requested target and writes a resolution report). Real: the
filesystem, the lock format, and the staging and swap logic. Out of scope: real
network access and real wheels, and the shipped gupkg (which never runs this tool).
"""

from __future__ import annotations

import io
import json
import types
from pathlib import Path
from unittest import mock

import pytest

import update_deps
from update_deps import LockedRequirement

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
OLD_LOCK = f"textual==1.0.0 --hash=sha256:{HASH_A}\ntomli_w==1.2.0 --hash=sha256:{HASH_B}\n"


class FakePip:
    """Stand in for the runtime interpreter: install into --target, resolve, and probe."""

    def __init__(
        self, *, fail_install: bool = False, fail_probe: bool = False, report=None, fail_resolve=False, plant=None
    ) -> None:
        self.plant = plant
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
            if self.plant:
                (target / self.plant).mkdir()
                (target / self.plant / "__init__.py").write_text("", encoding="utf-8")
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
def checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A runtime folder with a live site-packages and a lock file, as ``(runtime, lock)``."""
    runtime = tmp_path / "python"
    live = runtime / "Lib" / "site-packages"
    live.mkdir(parents=True)
    (runtime / "python.exe").touch()
    (runtime / "python312._pth").write_text("python312.zip\n", encoding="ascii")
    (live / "old.txt").write_text("old", encoding="utf-8")
    lock = tmp_path / "gupkg-deps.lock"
    lock.write_text(OLD_LOCK, encoding="utf-8")
    return runtime, lock


def test_lock_file_round_trips_and_rejects_unhashed_requirements(tmp_path: Path) -> None:
    """A lock file keeps its pins and hashes, and a line without a hash is rejected."""
    entries = [LockedRequirement("Pygments", "2.21.0", (HASH_A, HASH_B)), LockedRequirement("textual", "8.2.8", (HASH_C,))]
    path = tmp_path / "lock"
    path.write_text(update_deps.format_lock(entries), encoding="utf-8")

    assert update_deps.read_lock(path) == entries

    path.write_text("textual==8.2.8\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Malformed or unhashed"):
        update_deps.read_lock(path)


def test_repair_installs_only_the_pinned_hashed_wheels_and_replaces_the_live_directory(checkout) -> None:
    """Repair installs the lock with hashes required and wheels only, then swaps it in."""
    runtime, lock = checkout
    pip = FakePip()
    with mock.patch.object(update_deps.subprocess, "run", pip):
        update_deps.repair_deps(runtime, lock, progress=lambda line: None)

    install = next(command for command in pip.commands if "--target" in command)
    for required in ("--require-hashes", "--only-binary=:all:", "--no-deps", "-I"):
        assert required in install
    live = runtime / "Lib" / "site-packages"
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
def test_failed_repair_leaves_the_live_directory_untouched(checkout, failure: dict) -> None:
    """A failed install or import check aborts before the swap and leaves the live install as it was."""
    runtime, lock = checkout
    with (
        mock.patch.object(update_deps.subprocess, "run", FakePip(**failure)),
        pytest.raises(RuntimeError, match="unchanged"),
    ):
        update_deps.repair_deps(runtime, lock, progress=lambda line: None)

    live = runtime / "Lib" / "site-packages"
    assert (live / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (live / "installed.lock").exists()
    assert not live.with_name("site-packages.staging").exists()


def test_update_shows_the_plan_and_changes_nothing_when_declined(checkout) -> None:
    """Declining the plan leaves both the live directory and the lock as they were."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("tomli_w", "1.2.0", HASH_B)])
    shown: list[list[str]] = []

    def decline(lines: list[str]) -> bool:
        shown.append(lines)
        return False

    with mock.patch.object(update_deps.subprocess, "run", pip):
        applied = update_deps.update_deps(runtime, lock, confirm=decline, progress=lambda line: None)

    assert applied == []
    assert any("textual 1.0.0 -> 2.0.0" in line for line in shown[0])
    assert lock.read_text(encoding="utf-8") == OLD_LOCK
    assert (runtime / "Lib" / "site-packages" / "old.txt").exists()
    assert not any("--target" in command and "--report" not in command for command in pip.commands)


def test_confirmed_update_installs_the_new_pins_and_rewrites_the_lock(checkout) -> None:
    """After confirmation the new versions are installed from a temporary lock and then recorded."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("tomli_w", "1.2.0", HASH_B), _release("rich", "1.0", HASH_A)])

    with mock.patch.object(update_deps.subprocess, "run", pip):
        applied = update_deps.update_deps(runtime, lock, confirm=lambda lines: True, progress=lambda line: None)

    assert sorted(entry.name for entry in applied) == ["rich", "textual"]
    new_lock = lock.read_text(encoding="utf-8")
    assert f"textual==2.0.0 --hash=sha256:{HASH_C}" in new_lock
    assert (runtime / "Lib" / "site-packages" / "installed.lock").read_text(encoding="utf-8") == new_lock
    assert not lock.with_name(lock.name + ".new").exists()


def test_failed_update_install_does_not_change_the_lock(checkout) -> None:
    """If the new set cannot be installed, neither the live directory nor the lock changes."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("tomli_w", "1.2.0", HASH_B)], fail_install=True)

    with mock.patch.object(update_deps.subprocess, "run", pip), pytest.raises(RuntimeError):
        update_deps.update_deps(runtime, lock, confirm=lambda lines: True, progress=lambda line: None)

    assert lock.read_text(encoding="utf-8") == OLD_LOCK
    assert (runtime / "Lib" / "site-packages" / "old.txt").exists()
    assert not lock.with_name(lock.name + ".new").exists()


def test_update_aborts_with_a_clear_message_when_pypi_is_unreachable(checkout) -> None:
    """An unreachable index aborts the update and leaves the existing install alone."""
    runtime, lock = checkout
    with (
        mock.patch.object(update_deps.subprocess, "run", FakePip(fail_resolve=True)),
        pytest.raises(RuntimeError, match="PyPI reachable"),
    ):
        update_deps.update_deps(runtime, lock, confirm=lambda lines: True, progress=lambda line: None)

    assert lock.read_text(encoding="utf-8") == OLD_LOCK


def test_update_refuses_releases_that_are_not_plain_stable_versions(checkout) -> None:
    """A pre-release or oddly named release in the resolution is rejected before anything is installed."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0rc1", HASH_C)])

    with mock.patch.object(update_deps.subprocess, "run", pip), pytest.raises(RuntimeError, match="stable"):
        update_deps.update_deps(runtime, lock, confirm=lambda lines: True, progress=lambda line: None)


def test_command_line_needs_yes_when_it_cannot_ask(checkout) -> None:
    """Without --yes and without a terminal, an update plan is refused rather than applied."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("tomli_w", "1.2.0", HASH_B)])

    with (
        mock.patch.object(update_deps.subprocess, "run", pip),
        mock.patch("sys.stdin", io.StringIO()),
        pytest.raises(SystemExit, match="--yes"),
    ):
        update_deps.main(["--runtime", str(runtime), "--lock", str(lock)])

    assert lock.read_text(encoding="utf-8") == OLD_LOCK
    assert not any("--target" in command and "--report" not in command for command in pip.commands)


def test_command_line_reports_a_missing_runtime(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A checkout without its runtime gets a message that names the tool to run, and exit status 1."""
    lock = tmp_path / "lock"
    lock.write_text(OLD_LOCK, encoding="utf-8")

    status = update_deps.main(["--runtime", str(tmp_path / "missing"), "--lock", str(lock), "--yes"])

    assert status == 1
    assert "fetch_runtime.py" in capsys.readouterr().err


def test_the_installing_interpreter_runs_pip_and_the_runtime_only_probes(checkout) -> None:
    """Pip runs in the interpreter the maintainer names, cross-targeted at the bundled CPython; the runtime only imports."""
    runtime, lock = checkout
    pip = FakePip()
    with mock.patch.object(update_deps.subprocess, "run", pip):
        update_deps.repair_deps(runtime, lock, progress=lambda line: None, python="C:/dev/.venv/python.exe")

    runtime_python = str(runtime / "python.exe")
    install = next(command for command in pip.commands if "--target" in command)
    assert install[0] == "C:/dev/.venv/python.exe"
    assert install[install.index("--platform") + 1] == "win_amd64"
    assert install[install.index("--python-version") + 1] == "3.12"
    assert install[install.index("--abi") + 1] == "cp312"
    for command in pip.commands:
        if command[0] == runtime_python:
            assert "pip" not in command and "-c" in command


def test_update_resolves_only_the_roots_without_pip_and_never_locks_it(checkout) -> None:
    """The resolution asks for textual and tomli_w only, and the rewritten lock has no pip line."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("tomli_w", "1.2.0", HASH_B)])
    with mock.patch.object(update_deps.subprocess, "run", pip):
        update_deps.update_deps(runtime, lock, confirm=lambda lines: True, progress=lambda line: None)

    resolve = next(command for command in pip.commands if "--report" in command)
    assert resolve[-2:] == ["textual", "tomli_w"]
    assert resolve[resolve.index("--python-version") + 1] == "3.12"
    assert update_deps.LOCK_ROOTS == ("textual", "tomli_w")
    assert "pip" not in [item.name.casefold() for item in update_deps.read_lock(lock)]


def test_update_refuses_a_resolution_that_brings_pip_back(checkout) -> None:
    """A transitive pip or setuptools in the resolution aborts before anything is installed or relocked."""
    runtime, lock = checkout
    pip = FakePip(report=[_release("textual", "2.0.0", HASH_C), _release("pip", "26.2.1", HASH_B)])

    with mock.patch.object(update_deps.subprocess, "run", pip), pytest.raises(RuntimeError, match="pip"):
        update_deps.update_deps(runtime, lock, confirm=lambda lines: True, progress=lambda line: None)

    assert lock.read_text(encoding="utf-8") == OLD_LOCK
    assert not any("--target" in command and "--report" not in command for command in pip.commands)


@pytest.mark.parametrize("installer", ["pip", "setuptools", "_distutils_hack", "ensurepip"])
def test_a_staged_install_that_contains_an_installer_is_refused(checkout, installer: str) -> None:
    """If the installed wheels bring pip or setuptools along, the swap does not happen and the live tree stays."""
    runtime, lock = checkout
    with (
        mock.patch.object(update_deps.subprocess, "run", FakePip(plant=installer)),
        pytest.raises(RuntimeError, match=installer),
    ):
        update_deps.repair_deps(runtime, lock, progress=lambda line: None)

    live = runtime / "Lib" / "site-packages"
    assert (live / "old.txt").exists() and not (live / installer).exists()


def test_the_real_lock_lists_tomli_w_and_no_pip() -> None:
    """tools/gupkg-deps.lock pins tomli_w as a runtime dependency and has no pip (or setuptools) line."""
    names = [item.name.casefold().replace("_", "-") for item in update_deps.read_lock(update_deps.DEFAULT_LOCK)]

    assert "tomli-w" in names
    assert not {"pip", "setuptools"} & set(names)
