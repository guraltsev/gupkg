"""Cover that gupkg never acquires or repairs anything at run time.

Behavior covered: the commands and option that installed Python dependencies are
unknown (``manager self repair-deps``, ``manager self update-deps`` and
``--allow-hook-dependency-install``), and an ordinary gupkg run creates nothing
under ``%LOCALAPPDATA%\\gupkg`` and starts no ``pip`` process.

Real: the command-line parser and a real command (``config-check``) on a fixture
package. Mocked: ``subprocess`` (to fail on any pip invocation) and the
``LOCALAPPDATA`` variable (pointed at an empty temporary folder). Out of scope:
the build-time tools that do install dependencies (``tools/``).
"""

from __future__ import annotations

import io
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from gupkg import cli

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(["manager", "self", "repair-deps"], "invalid choice: 'repair-deps'", id="repair-deps"),
        pytest.param(["manager", "self", "update-deps"], "invalid choice: 'update-deps'", id="update-deps"),
        pytest.param(
            ["--allow-hook-dependency-install", "manager", "list"],
            "unrecognized arguments: --allow-hook-dependency-install",
            id="allow-hook-dependency-install",
        ),
        pytest.param(
            ["manager", "self", "update", "--allow-hook-dependency-install"],
            "unrecognized arguments: --allow-hook-dependency-install",
            id="option-after-command",
        ),
    ],
)
def test_dependency_commands_and_option_no_longer_exist(
    arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The removed commands and the removed option are rejected by the parser with a usage error."""
    with pytest.raises(SystemExit) as stopped:
        cli.main(arguments)

    assert stopped.value.code == 2
    assert message in capsys.readouterr().err


def test_a_gupkg_run_creates_no_local_state_and_starts_no_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Running a command leaves %LOCALAPPDATA% empty and never starts a pip process."""
    local_app_data = tmp_path / "local"
    local_app_data.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    started: list[object] = []

    # A run writes its logs next to the package it acts on, so use a copy.
    package = tmp_path / "GoodApp"
    shutil.copytree(FIXTURES / "GoodApp", package)

    def refuse(*args: object, **kwargs: object) -> None:
        started.append(args[0] if args else kwargs)
        raise AssertionError("no process may be started")

    with (
        mock.patch("subprocess.run", side_effect=refuse),
        mock.patch("subprocess.Popen", side_effect=refuse),
        redirect_stdout(io.StringIO()),
    ):
        code = cli.main(["config-check", str(package / "1.2.3.local.1")])

    assert code == 0
    assert started == []
    assert list(local_app_data.iterdir()) == []
