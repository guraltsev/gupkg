"""Cover calls with several targets: failures do not stop the run, the log names them, cancel stops after the current package.

Behaviour covered: a 3-package install where package 2 fails still installs package 3 and both the summary and the log name the
failure; cancelling while package 1 runs finishes package 1 cleanly and reports packages 2 and 3 as skipped and unchanged; a stop
marker given to an elevated batch does the same; the receipt stores the package's log path; the registry rule (design
9.5): install, update and uninstall read nothing from the Windows registry except ``PATH``.

Real: the engine, the run session and log files, real folders. Mocked: ``FakeSystem`` (elevation flag, process
doubles that play populate scripts).
Out of scope: the command-line spelling of cancel (issue 029).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.engine import lifecycle
from gupkg.engine.context import stop_marker
from gupkg.engine.receipts import read_receipt
from gupkg.report.session import RunSession
from tests.engine_support import ScriptDouble, World, pin_no_ambient_elevation  # noqa: F401

ENV = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'


class MarkerDouble(ScriptDouble):
    """A populate script that, like a user pressing Esc, asks the run to stop while it works."""

    def __init__(self, files: dict[str, str], marker: Path) -> None:
        super().__init__(files)
        self.marker = marker

    def __call__(self, call):
        """Write the stop marker, then fill App as usual."""
        self.marker.write_text("stop", encoding="utf-8")
        return super().__call__(call)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root."""
    return World(tmp_path)


def test_a_failing_package_does_not_stop_the_batch_and_the_summary_and_log_name_it(world: World) -> None:
    """Package 2 fails (a missing file after unpacking); package 3 is still installed; the summary and the log both name the failure."""
    world.publish("one", "1.0.0", tables=ENV)
    world.publish("two", "1.0.0", tables=ENV, source='require = ["missing.exe"]\n')
    world.publish("three", "1.0.0", tables=ENV)
    root = world.user

    with RunSession(command="install", argv=["install"], mode="user", elevated=False, version="test", reporters=[]) as run:
        run.bind(root)
        world.engine.log_path = lambda path: str(run.log.package_log(path)) if run.log.package_log(path) else None
        batch = lifecycle.install(world.engine, ["one", "two", "three"])
        run.finish(batch.summary(), batch.exit_code)

    assert [item.ok for item in batch.results] == [True, False, True] and batch.exit_code == 3
    assert (root / "three" / "current").is_dir() and not (root / "two" / "current").exists()
    assert "two" in batch.summary() and "1 of 3 failed" in batch.summary()
    run_log = next((root / ".gupkg" / "logs").glob("2*.log")).read_text(encoding="utf-8")
    assert "two" in run_log and "missing.exe" in run_log
    assert "missing.exe" in next((root / "two" / ".gupkg" / "logs").glob("2*.log")).read_text(encoding="utf-8")


def test_the_receipt_stores_the_log_path_of_the_package(world: World) -> None:
    """The receipt of an install made inside a run session names that package's log file."""
    world.publish("one", "1.0.0", tables=ENV)
    with RunSession(command="install", argv=["install"], mode="user", elevated=False, version="test", reporters=[]) as run:
        run.bind(world.user)
        world.engine.log_path = lambda path: str(run.log.package_log(path)) if run.log.package_log(path) else None
        batch = lifecycle.install(world.engine, ["one"])
        run.finish(batch.summary(), batch.exit_code)

    logged = batch.results[0].log
    assert logged and Path(logged).is_file() and Path(logged).parent == world.user / "one" / ".gupkg" / "logs"


def test_cancel_while_package_one_runs_finishes_it_and_skips_the_rest(world: World) -> None:
    """Cancelling during package 1: it finishes cleanly, packages 2 and 3 are skipped and unchanged."""
    marker = world.base / "stop.marker"
    world.fake.processes["populate.cmd"] = MarkerDouble({"app.exe": "one"}, marker)
    world.publish_script("one", "1.0.0", tables=ENV)
    world.fake.processes["populate.cmd"] = MarkerDouble({"app.exe": "one"}, marker)
    world.publish("two", "1.0.0", tables=ENV)
    world.publish("three", "1.0.0", tables=ENV)
    world.engine.cancel = stop_marker(marker)

    batch = lifecycle.install(world.engine, ["one", "two", "three"])

    assert [item.status for item in batch.results] == ["installed", "skipped", "skipped"]
    assert (world.user / "one" / "current" / "App" / "app.exe").is_file()
    assert not (world.user / "two" / "current").exists() and not (world.user / "three" / "current").exists()
    assert not batch.ok and batch.exit_code == 5 and "Skipped after cancel: two, three" in batch.summary()
    assert not (world.user / "two" / ".gupkg").exists()


def test_a_stop_marker_given_to_an_elevated_batch_has_the_same_effect(tmp_path: Path) -> None:
    """In an elevated run the same user-writable marker stops the batch after the current package, and can do nothing else."""
    world = World(tmp_path, elevated=True)
    marker = tmp_path / "stop.marker"
    world.publish_script("one", "1.0.0", mode="system", tables=ENV)
    world.fake.processes["populate.cmd"] = MarkerDouble({"app.exe": "one"}, marker)
    world.publish("two", "1.0.0", mode="system", tables=ENV)
    world.engine.cancel = stop_marker(marker)

    batch = lifecycle.install(world.engine, ["one", "two"])

    assert [item.status for item in batch.results] == ["installed", "skipped"]
    assert not (world.system_root / "two" / "current").exists()


def test_install_update_and_uninstall_read_nothing_from_the_registry_but_path(world: World) -> None:
    """The registry is a write target (design 9.5): the access log shows no read other than the PATH read-modify-write."""
    tables = '[[shortcut]]\nname = "Tool"\ntarget = "$App\\\\app.exe"\n\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n\n' + ENV + '\n[[path]]\nvalue = "$App"\n'
    world.publish("tool", "1.0.0", tables=tables)
    world.fake.registry_log.clear()

    world.one("install", "tool")
    world.publish("tool", "1.1.0", tables=ENV)
    world.one("update", "tool")
    world.one("switch", "tool", version="1.0.0")
    world.one("uninstall", "tool")

    reads = {(item.scope, item.name) for item in world.fake.registry_log if item.operation == "read"}
    assert reads == {("user", "Path")}
    assert any(item.operation == "write" and item.name == "TOOL_HOME" for item in world.fake.registry_log)
