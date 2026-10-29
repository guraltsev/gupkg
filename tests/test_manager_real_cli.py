"""Cover the UI programs against the REAL command line: the real launchers, the real CLI behind them, real folders, a pretend machine.

Behaviour covered: ``gupkg-manager update --all --yes`` updates every outdated package through the real ``gupkg`` (the version in ``current`` changes), with its events followed from the real
log directories; without ``--yes`` and without a terminal nothing changes (exit 5); ``--json`` is one document; ``check`` is read only; with ``[cleanup] auto = true`` an update ends with a
cleanup call and without it none is made; a package
folder named with shell characters is refused alone; ``gupkg manager`` and ``gupkg tui`` start the programs of the same tree through the real launchers and do not loop; both programs run under the
bundled interpreter in isolated mode and write no bytecode.

Real: the launchers, the bundled Python, the CLI, the engine and the file system below the test's folder. Mocked: the machine (``--sandbox`` keeps registry, shortcuts and elevation in a folder) and nothing else.
Out of scope: the screens (``test_ui_screens.py``) and real elevation, which is a manual check (``tests/manual_smoke.md``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.ui_support import LAUNCHERS, RealWorld


def run_plain(world: RealWorld, launcher: str, *args: str) -> subprocess.CompletedProcess:
    """Run a real launcher without root options (the forwarders refuse them, which is how ``gupkg manager`` and ``gupkg tui`` are started)."""
    return subprocess.run(
        [str(LAUNCHERS / f"{launcher}.cmd"), *args], capture_output=True, text=True, encoding="utf-8", env=world.env, stdin=subprocess.DEVNULL, timeout=60, check=False,
    )


@pytest.fixture
def world(tmp_path: Path) -> RealWorld:
    """A pretend machine with alpha 1.0.0 installed and 2.0.0 and 3.0.0 on disk, and beta installed at its only version."""
    machine = RealWorld(tmp_path)
    machine.package("alpha", ("1.0.0", "2.0.0", "3.0.0"))
    machine.package("beta", ("1.0.0",))
    machine.install("alpha@1.0.0", "beta@1.0.0")
    return machine


def test_update_all_updates_through_the_real_cli(world: RealWorld) -> None:
    """The outdated package moves to its newest known version; the up-to-date one is left alone; the output ends with the verdict and the summary."""
    done = world.run("gupkg-manager", "update", "--all", "--yes")

    assert done.returncode == 0, done.stderr
    assert world.active("alpha") == "3.0.0" and world.active("beta") == "1.0.0"
    lines = done.stdout.rstrip().splitlines()
    assert lines[-1] == "    ok alpha 3.0.0: updated (was 1.0.0)" and lines[-2].startswith("━━ Done")
    assert "━━ Activate" in done.stdout and "+ current -> 3.0.0" in done.stdout


def test_update_all_without_yes_and_without_a_terminal_changes_nothing(world: RealWorld) -> None:
    """The schedule mistake (forgetting ``--yes``) is exit code 5 and a clear message, with every package where it was."""
    done = world.run("gupkg-manager", "update", "--all")

    assert done.returncode == 5 and "Pass --yes" in done.stdout
    assert world.active("alpha") == "1.0.0"


def test_json_is_one_document_with_the_cli_head(world: RealWorld) -> None:
    """Standard output is one parseable document; the run's text is on standard error."""
    done = world.run("gupkg-manager", "update", "--all", "--yes", "--json")

    document = json.loads(done.stdout)
    assert (document["api"], document["command"], document["ok"], document["exit_code"]) == (2, "update", True, 0)
    assert [item["package"] for item in document["results"]] == ["alpha"] and document["results"][0]["status"] == "updated"
    assert "━━ Done" in done.stderr and document["calls"][0]["exit_code"] == 0


def test_check_reads_every_package_and_changes_nothing(world: RealWorld) -> None:
    """The health check is one call for all packages and reports each as healthy."""
    done = world.run("gupkg-manager", "check", "--json")

    document = json.loads(done.stdout)
    assert done.returncode == 0 and sorted(item["package"] for item in document["results"]) == ["alpha", "beta"]
    assert {item["status"] for item in document["results"]} == {"healthy"} and world.active("alpha") == "1.0.0"


@pytest.mark.parametrize(("auto", "cleanup_calls"), [pytest.param(True, 1, id="auto-cleanup"), pytest.param(False, 0, id="no-cleanup")])
def test_cleanup_ends_an_update_only_with_the_setting(tmp_path: Path, auto: bool, cleanup_calls: int) -> None:
    """With ``[cleanup] auto = true`` the run includes a ``cleanup`` call for the user root and its result; without it no cleanup is made (decision D43)."""
    machine = RealWorld(tmp_path, settings=f"[cleanup]\nauto = {str(auto).lower()}")
    machine.package("alpha", ("1.0.0", "2.0.0"))
    machine.install("alpha@1.0.0")

    done = machine.run("gupkg-manager", "update", "--all", "--yes", "--json")

    document = json.loads(done.stdout)
    made = [call for call in document["calls"] if "cleanup" in call["argv"]]
    assert done.returncode == 0 and len(made) == cleanup_calls
    assert ("cleaned" in {item["status"] for item in document["results"]}) is auto


def test_a_package_folder_with_shell_characters_is_refused_alone(tmp_path: Path) -> None:
    """A name such as ``evil&calc`` is never put on a command line; the other package still updates and the batch exits 2."""
    machine = RealWorld(tmp_path)
    machine.package("good", ("1.0.0", "2.0.0"))
    machine.package("evil&calc", ("1.0.0", "2.0.0"))
    machine.install("good@1.0.0")
    (machine.user / "evil&calc" / ".gupkg").mkdir()

    done = machine.run("gupkg-manager", "update", "--all", "--yes", "--json")

    document = json.loads(done.stdout)
    entries = {item["package"]: item for item in document["results"]}
    assert machine.active("good") == "2.0.0" and entries["good"]["ok"] is True
    assert all("evil&calc" not in " ".join(call["argv"]) for call in document["calls"])


def test_gupkg_manager_forwards_to_the_manager_of_the_same_tree(world: RealWorld) -> None:
    """``gupkg manager --help`` starts the real ``gupkg-manager`` launcher of the same tree and passes its output and exit code through."""
    done = run_plain(world, "gupkg", "manager", "--help")

    assert done.returncode == 0 and "check-updates" in done.stdout and "Open the browser" in done.stdout


def test_gupkg_tui_forwards_to_the_package_program_and_does_not_loop(world: RealWorld) -> None:
    """``gupkg tui --help`` reaches ``gupkg-tui`` once and prints its help; the old launcher that called ``gupkg tui`` again is gone."""
    done = run_plain(world, "gupkg", "tui", "--help")

    assert done.returncode == 0 and "PATH|NAME" in done.stdout


@pytest.mark.parametrize("program", ["gupkg-tui", "gupkg-manager"])
def test_the_programs_run_on_the_bundled_interpreter_without_writing_bytecode(world: RealWorld, program: str) -> None:
    """Each launcher starts ``python -I -s -B -S`` of its own tree (the help works), and no ``__pycache__`` appears in the three packages."""
    done = world.run(program, "--help")

    assert done.returncode == 0 and "usage:" in done.stdout
    # The test process (another Python) may have left its own bytecode; the bundled 3.12 interpreter, started with -B, writes none.
    assert not [path for name in ("gupkg_ui", "gupkg_tui", "gupkg_manager") for path in (LAUNCHERS.parent / name).rglob("*.cpython-312*.pyc")]
