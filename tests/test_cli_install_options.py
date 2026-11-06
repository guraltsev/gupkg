"""Cover the install and cleanup options that the command line hands to the engine: ``--refresh``, ``--bundled``.

Behavior covered: ``install --refresh`` rebuilds an ``App`` that exists (a file changed by hand comes back) while a plain install
leaves it; ``cleanup --bundled`` removes the bundled gupkg of old versions in a root with a gupkg-location and a plain cleanup
keeps it; ``--skip-checksum`` never waives a hash that does not match (exit code 3, no ``App``).

Real: the argument parser, the engine and real folders. Mocked: ``FakeSystem`` and the served artifacts.
Out of scope: the engine's own rules for these options (``test_engine_*``).
"""

from __future__ import annotations

import pytest

from tests.support.cli import CliWorld
from tests.support.engine import replace_declared_sha256

pytestmark = pytest.mark.unit

# Table that declares the shim of the test package.
SHIM = '\n[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


def test_refresh_rebuilds_an_app_that_can_be_rebuilt_and_a_plain_install_does_not(cli_world: CliWorld) -> None:
    """A file changed by hand stays after `install tool` and comes back after `install tool --refresh`."""
    cli_world.publish("tool", "1.0.0", tables=SHIM)
    assert cli_world.cli("install", "tool").code == 0
    target = cli_world.user / "tool" / "1.0.0" / "App" / "app.exe"
    target.write_text("tampered", encoding="utf-8")

    plain = cli_world.cli("install", "tool")
    assert plain.code == 0 and target.read_text(encoding="utf-8") == "tampered"
    refreshed = cli_world.cli("install", "tool", "--refresh")

    assert refreshed.code == 0, refreshed.err
    assert target.read_text(encoding="utf-8") == "tool 1.0.0\n"


def test_bundled_removes_the_bundled_gupkg_of_old_versions_and_a_plain_cleanup_keeps_it(cli_world: CliWorld) -> None:
    """In a root with a gupkg-location, `cleanup --bundled` drops `<old version>\\gupkg`; `cleanup` alone leaves it."""
    (cli_world.user / ".gupkg").mkdir(parents=True, exist_ok=True)
    # A gupkg-location as gupkg writes it, naming an installed gupkg that exists.
    (cli_world.user / ".gupkg" / "gupkg-location").write_text("gupkg\\current\\App\r\n", encoding="utf-8")
    (cli_world.user / "gupkg" / "current" / "App").mkdir(parents=True)
    (cli_world.user / "gupkg" / "current" / "App" / "gupkg.cmd").write_text("@echo off\r\n", encoding="utf-8")
    # Install 1.0.0 and give it a bundled gupkg.
    cli_world.publish("tool", "1.0.0", tables=SHIM)
    assert cli_world.cli("install", "tool").code == 0
    old =cli_world.user / "tool" / "1.0.0"
    (old / "gupkg").mkdir()
    (old / "gupkg" / "gupkg.cmd").write_text("@echo off\r\n", encoding="utf-8")
    # Publish and install 2.0.0, which makes 1.0.0 an old version.
    cli_world.publish("tool", "2.0.0", tables=SHIM)
    assert cli_world.cli("update", "tool").code == 0

    # Act and assert: a plain cleanup keeps the bundled gupkg.
    plain = cli_world.cli("cleanup", "tool")
    assert plain.code == 0 and (old / "gupkg" / "gupkg.cmd").is_file()
    # Act and assert: ``cleanup --bundled`` removes it.
    bundled = cli_world.cli("cleanup", "tool", "--bundled")

    assert bundled.code == 0, bundled.err
    assert not (old / "gupkg").exists() and (cli_world.user / "tool" / "2.0.0" / "pkg.toml").is_file()


def test_skip_checksum_never_waives_a_hash_that_does_not_match(cli_world: CliWorld) -> None:
    """A declared hash that the download does not match is exit code 3 whatever the flag says, and no App is left."""
    folder = cli_world.publish("tool", "1.0.0", tables=SHIM)
    replace_declared_sha256(folder)

    plain = cli_world.cli("install", "tool", "--json")
    skipped = cli_world.cli("install", "tool", "--skip-checksum=cli", "--json")
    bare = cli_world.cli("install", "tool", "--skip-checksum", "--json")

    for result in (plain, skipped, bare):
        assert result.code == 3, result.out + result.err
        assert result.document()["results"][0]["error"]["code"] == "checksum-mismatch"
    assert not (folder / "App").exists() and not (cli_world.user / "tool" / "current").exists()
