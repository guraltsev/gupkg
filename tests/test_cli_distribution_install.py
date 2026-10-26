"""Cover the install of a distribution folder: everything the run writes belongs to the destination, nothing to the source.

Behavior covered: ``gupkg install <downloads>\\<name>\\<version>`` copies the folder into the mode's root and installs from the
copy; the run's package logs, the lock, the receipt and its ``[run] log`` path are in the destination package's ``.gupkg``
(``gupkg logs <name>`` shows the install), and the source folder is byte-identical afterwards, in user mode and in system mode
from an administrator shell.

Real: the argument parser, the engine, the run session and log files, real folders. Mocked: ``FakeSystem`` and the
environment mapping built by the test. Out of scope: Mark-of-the-Web handling of the copy (``test_distribution_copy.py``).
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import pytest

from tests.cli_support import CliWorld
from tests.engine_support import zip_bytes
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401


def snapshot(folder: Path) -> dict[str, str | None]:
    """Return every file and folder below *folder* with the SHA-256 of each file (``None`` for a folder)."""
    return {
        str(path.relative_to(folder)): None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
    }


@pytest.mark.parametrize("mode", ["user", "system"])
def test_installing_a_distribution_folder_writes_nothing_into_the_source(tmp_path: Path, mode: str) -> None:
    """Logs, lock and receipt go to the destination package; the source folder is byte-identical, and `gupkg logs` shows the run."""
    world = CliWorld(tmp_path, elevated=mode == "system")
    source = world.machine.distribution("demo", "1.0.0")
    world.fake.add_url("https://example.org/tool-1.0.0.zip", zip_bytes({"app.exe": "demo 1.0.0"}))
    root = world.system_root if mode == "system" else world.user
    before = snapshot(source.parent.parent)

    result = world.cli("install", str(source), "--mode", mode, "--yes", "--skip-checksum")

    assert result.code == 0, result.out + result.err
    assert snapshot(source.parent.parent) == before, "the download folder was changed"
    assert not (source.parent / ".gupkg").exists()
    destination = root / "demo"
    [package_log] = (destination / ".gupkg" / "logs").glob("*-install.log")
    receipt_file = next(destination.glob("*/App.info"))
    assert tomllib.loads(receipt_file.read_text(encoding="utf-8"))["run"]["log"] == str(package_log)
    listed = world.cli("logs", "demo", "--path", "--mode", mode)
    assert listed.code == 0 and str(destination / ".gupkg" / "logs") in listed.out
