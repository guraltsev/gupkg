"""Cover the install and cleanup options that the command line hands to the engine: ``--refresh``, ``--bundled``.

Behavior covered: ``install --refresh`` rebuilds an ``App`` that exists (a file changed by hand comes back) while a plain install
leaves it; ``cleanup --bundled`` removes the bundled gupkg of old versions in a root with a gupkg-location and a plain cleanup
keeps it; ``--skip-checksum`` never waives a hash that does not match (exit code 3, no ``App``).

Real: the argument parser, the engine and real folders. Mocked: ``FakeSystem`` and the served artifacts.
Out of scope: the engine's own rules for these options (``test_engine_*``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cli_support import CliWorld
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

SHIM = '\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A fake machine with a user root and a stand-in installed system gupkg."""
    return CliWorld(tmp_path)


def test_refresh_rebuilds_an_app_that_can_be_rebuilt_and_a_plain_install_does_not(world: CliWorld) -> None:
    """A file changed by hand stays after `install tool` and comes back after `install tool --refresh`."""
    world.publish("tool", "1.0.0", tables=SHIM)
    assert world.cli("install", "tool").code == 0
    target = world.user / "tool" / "1.0.0" / "App" / "app.exe"
    target.write_text("tampered", encoding="utf-8")

    plain = world.cli("install", "tool")
    assert plain.code == 0 and target.read_text(encoding="utf-8") == "tampered"
    refreshed = world.cli("install", "tool", "--refresh")

    assert refreshed.code == 0, refreshed.err
    assert target.read_text(encoding="utf-8") == "tool 1.0.0\n"


def test_bundled_removes_the_bundled_gupkg_of_old_versions_and_a_plain_cleanup_keeps_it(world: CliWorld) -> None:
    """In a root with a gupkg-location, `cleanup --bundled` drops `<old version>\\gupkg`; `cleanup` alone leaves it."""
    (world.user / ".gupkg").mkdir(parents=True, exist_ok=True)
    # A gupkg-location as gupkg writes it, naming an installed gupkg that exists.
    (world.user / ".gupkg" / "gupkg-location").write_text("gupkg\\current\\App\r\n", encoding="utf-8")
    (world.user / "gupkg" / "current" / "App").mkdir(parents=True)
    (world.user / "gupkg" / "current" / "App" / "gupkg.cmd").write_text("@echo off\r\n", encoding="utf-8")
    world.publish("tool", "1.0.0", tables=SHIM)
    assert world.cli("install", "tool").code == 0
    old =world.user / "tool" / "1.0.0"
    (old / "gupkg").mkdir()
    (old / "gupkg" / "gupkg.cmd").write_text("@echo off\r\n", encoding="utf-8")
    world.publish("tool", "2.0.0", tables=SHIM)
    assert world.cli("update", "tool").code == 0

    plain = world.cli("cleanup", "tool")
    assert plain.code == 0 and (old / "gupkg" / "gupkg.cmd").is_file()
    bundled = world.cli("cleanup", "tool", "--bundled")

    assert bundled.code == 0, bundled.err
    assert not (old / "gupkg").exists() and (world.user / "tool" / "2.0.0" / "pkg.toml").is_file()


def test_skip_checksum_never_waives_a_hash_that_does_not_match(world: CliWorld) -> None:
    """A declared hash that the download does not match is exit code 3 whatever the flag says, and no App is left."""
    folder = world.publish("tool", "1.0.0", tables=SHIM)
    manifest = folder / "pkg.toml"
    text = manifest.read_text(encoding="utf-8")
    start = text.index('sha256 = "') + len('sha256 = "')
    manifest.write_text(text[:start] + ("0" * 64 if text[start:start + 64] != "0" * 64 else "1" * 64) + text[start + 64:], encoding="utf-8")

    plain = world.cli("install", "tool", "--json")
    skipped = world.cli("install", "tool", "--skip-checksum=cli", "--json")
    bare = world.cli("install", "tool", "--skip-checksum", "--json")

    for result in (plain, skipped, bare):
        assert result.code == 3, result.out + result.err
        assert result.document()["results"][0]["error"]["code"] == "checksum-mismatch"
    assert not (folder / "App").exists() and not (world.user / "tool" / "current").exists()
