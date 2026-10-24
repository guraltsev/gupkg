"""Cover that gupkg never acquires or repairs anything at run time.

Behavior covered: the commands and option that installed Python dependencies are unknown (``self repair-deps``,
``self update-deps`` and ``--allow-hook-dependency-install``), and an ordinary gupkg run creates nothing under
``%LOCALAPPDATA%\\gupkg`` and starts no ``pip`` process.

Real: the command-line parser and a real install through the engine. Mocked: ``subprocess`` (to fail on any process start),
the ``System`` boundary (``FakeSystem``) and the ``LOCALAPPDATA`` variable (pointed at an empty temporary folder). Out of
scope: the build-time tools that do install dependencies (``tools/``).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(["self", "repair-deps"], "invalid choice: 'repair-deps'", id="repair-deps"),
        pytest.param(["self", "update-deps"], "invalid choice: 'update-deps'", id="update-deps"),
        pytest.param(["--allow-hook-dependency-install", "list"], "unrecognized arguments: --allow-hook-dependency-install", id="allow-hook-dependency-install"),
        pytest.param(["self", "update", "--allow-hook-dependency-install"], "unrecognized arguments: --allow-hook-dependency-install", id="option-after-command"),
    ],
)
def test_dependency_commands_and_option_no_longer_exist(tmp_path: Path, arguments: list[str], message: str) -> None:
    """The removed commands and the removed option are rejected by the parser with a usage error."""
    result = CliWorld(tmp_path).cli(*arguments)

    assert result.code == 2
    assert message in result.err


def test_a_gupkg_run_creates_no_local_state_and_starts_no_pip(tmp_path: Path) -> None:
    """Running an install leaves %LOCALAPPDATA% empty and never starts a process."""
    local_app_data = tmp_path / "local"
    local_app_data.mkdir()
    world = CliWorld(tmp_path / "machine")
    world.env["LOCALAPPDATA"] = str(local_app_data)
    world.publish("tool", "1.0.0")
    started: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        started.append(args[0] if args else kwargs)
        raise AssertionError("no process may be started")

    with mock.patch("subprocess.run", side_effect=refuse), mock.patch("subprocess.Popen", side_effect=refuse):
        result = world.cli("install", "tool")

    assert result.code == 0
    assert started == []
    assert list(local_app_data.iterdir()) == []
