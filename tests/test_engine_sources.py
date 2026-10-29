"""Cover how ``App`` is populated and kept: downloaded, script-populated and shipped (design 4, decisions D27, D28, D41).

Behaviour covered: a version directory with a ``[track]`` table or a ``track`` hook is rejected before any work; a recoverable
``App`` is disposable (deleting every ``App`` and repairing leaves the package installed and healthy with integration
unchanged); populate scripts of every kind receive the context as JSON and fill ``App``, and a failing script changes
nothing; a script-populated ``App`` is dropped by ``cleanup`` only while its inputs exist; a shipped ``App`` is never dropped
or replaced, ``--purge`` asks first and names an unrecoverable ``App``, and ``check`` reports a modified file unless the
package or the global setting opts out of the ``App`` fingerprint.

Real: the engine, the catalog, real folders and zip archives. Mocked: ``FakeSystem`` (URL fixtures, process doubles that play
the populate scripts). Out of scope: bundled installs (issue 033), the command line.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gupkg.model.manifest import APP_MISSING_ERROR
from gupkg.engine.receipts import read_receipt
from gupkg.system.port import Mode
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401
from tests.mode_support import tree_snapshot

SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n\n[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fake machine with a user root and an engine."""
    return World(tmp_path)


def machine_state(world: World) -> tuple:
    """Return what integration produced: shortcuts, environment, launchers and the install record text."""
    folder = world.fake.scope_folders(Mode.USER).bin_dir
    record = (world.user / "tool" / ".gupkg" / "package.toml").read_text(encoding="utf-8")
    return (
        dict(world.fake.shortcuts()), dict(world.fake.env_values(Mode.USER)),
        sorted(item.name for item in folder.glob("*")) if folder.is_dir() else [], record,
    )


def current(world: World) -> str:
    """Return the version `current` points at."""
    return Path(os.path.realpath(world.user / "tool" / "current")).name


# ---------------------------------------------------------------------------
# Update discovery never lives in a version directory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plant",
    [
        pytest.param(lambda folder: (folder / "pkg.toml").write_text(
            (folder / "pkg.toml").read_text() + '\n[track]\nprovider = "json"\ncheck_url = "https://example.org/x.json"\nversion = "$.v"\n',
            encoding="utf-8"), id="track-table"),
        pytest.param(lambda folder: (
            (folder / "pkg.local").mkdir(),
            (folder / "pkg.local" / "hook.py").write_text("PKG_MODULE_API = 2\ndef track(ctx):\n    return None\n", encoding="utf-8"),
        ), id="track-hook"),
    ],
)
def test_a_version_directory_with_update_discovery_is_rejected_before_any_work(world: World, plant) -> None:
    """A `[track]` table or a `track` hook in a version directory makes it invalid: nothing is fetched or written."""
    folder = world.publish("tool", "1.0.0")
    plant(folder)
    world.fake.fetch_log.clear()
    before = tree_snapshot(world.user)

    plain = world.one("install", "tool")
    named = world.one("install", "tool@1.0.0")

    assert not plain.ok and plain.exit_code == 2 and not named.ok and "_check_updates" in named.error["message"]
    assert world.fake.fetch_log == [] and tree_snapshot(world.user) == before


# ---------------------------------------------------------------------------
# A recoverable App is disposable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("repair", ["install-refresh", "check-fix"])
@pytest.mark.parametrize("shipped", [None, False, True], ids=["default", "shipped-false", "shipped-true-with-url"])
def test_a_recoverable_app_is_disposable(world: World, shipped: bool | None, repair: str) -> None:
    """Deleting the App of every version and repairing leaves the package installed and healthy, integration unchanged."""
    if shipped:
        world.publish_shipped("tool", "1.0.0", download=True, tables=SHIM)
    else:
        world.publish("tool", "1.0.0", tables=SHIM, shipped=shipped)
    assert world.one("install", "tool").ok
    world.publish("tool", "1.1.0", tables=SHIM, shipped=shipped)
    assert world.one("update", "tool").ok
    package = world.user / "tool"
    for version in ("1.0.0", "1.1.0"):
        for item in sorted((package / version / "App").rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        (package / version / "App").rmdir()
    state = machine_state(world)
    assert world.one("check", "tool").status == "unhealthy"

    result = world.one("install", "tool", refresh=True) if repair == "install-refresh" else world.one("check", "tool", fix=True)

    assert result.ok, result.error
    assert (package / "1.1.0" / "App" / "app.exe").is_file() and current(world) == "1.1.0"
    assert world.one("check", "tool").status == "healthy" and machine_state(world) == state


def test_a_refresh_rebuilds_an_app_that_can_be_rebuilt(world: World) -> None:
    """--refresh replaces a downloaded App with a fresh one (a file changed by hand comes back)."""
    world.publish("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    target = world.user / "tool" / "1.0.0" / "App" / "app.exe"
    target.write_text("tampered", encoding="utf-8")

    result = world.one("install", "tool", refresh=True)

    assert result.ok and target.read_text() == "tool 1.0.0\n"


# ---------------------------------------------------------------------------
# Custom populate scripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", ["populate.ps1", "populate.cmd", "populate.exe"])
def test_a_populate_script_of_any_kind_receives_the_context_as_json_and_fills_app(world: World, script: str) -> None:
    """A `.ps1`, a `.cmd` and an executable populate script each get the JSON context and fill App; the install is not labelled unverified."""
    double = world.publish_script("tool", "1.0.0", script=script, tables=SHIM)

    result = world.one("install", "tool")

    assert result.ok and (world.user / "tool" / "current" / "App" / "app.exe").is_file()
    context = double.calls[0]
    assert context["package"] == "tool" and context["version"] == "1.0.0" and context["api"] == 2
    assert context["paths"]["app"].endswith("App") and context["paths"]["version_root"].endswith("1.0.0")
    checked = world.one("check", "tool")
    assert not [note for note in checked.details["notes"] if note["kind"] == "unverified"]
    assert "checksum" not in read_receipt(world.user / "tool", "1.0.0").data


def test_a_failing_populate_script_leaves_current_and_integration_unchanged(world: World) -> None:
    """When the script of the new version fails, `current` stays and nothing of the integration changes."""
    world.publish("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    world.publish_script("tool", "1.1.0", returncode=1, tables=SHIM)
    state = machine_state(world)

    result = world.one("update", "tool")

    assert not result.ok and result.error["code"] == "hook-error" and result.exit_code == 3
    assert current(world) == "1.0.0" and machine_state(world) == state
    assert not (world.user / "tool" / "1.1.0" / "App").exists()


def test_a_script_populated_app_is_dropped_by_cleanup_only_while_its_inputs_exist(world: World) -> None:
    """cleanup rebuilds-safe: a non-current script App goes while its script exists, and stays when an input is gone."""
    world.publish_script("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    world.publish_script("tool", "1.1.0", tables=SHIM)
    world.one("update", "tool")
    package = world.user / "tool"
    (package / "1.0.0" / "pkg.local" / "populate.cmd").unlink()

    kept = world.one("cleanup", "tool", keep=0)

    assert kept.ok and (package / "1.0.0" / "App").is_dir()
    assert {"version": "1.0.0", "why": "the populate script's inputs are gone"} in kept.details["kept"]
    (package / "1.0.0" / "pkg.local" / "populate.cmd").write_text("rem restored\n", encoding="utf-8")
    dropped = world.one("cleanup", "tool", keep=0)
    assert dropped.details["apps"] == ["1.0.0"] and not (package / "1.0.0" / "App").exists()


def test_repair_of_a_script_version_fails_when_its_script_is_gone(world: World) -> None:
    """With the script removed, rebuilding the App fails with a clear message instead of guessing."""
    world.publish_script("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    package = world.user / "tool"
    (package / "1.0.0" / "pkg.local" / "populate.cmd").unlink()
    for item in sorted((package / "1.0.0" / "App").rglob("*"), reverse=True):
        item.unlink()
    (package / "1.0.0" / "App").rmdir()

    result = world.one("install", "tool")

    assert not result.ok and "populate.cmd" in result.error["message"]


# ---------------------------------------------------------------------------
# A shipped App is never lost
# ---------------------------------------------------------------------------


def test_cleanup_and_plain_uninstall_keep_a_shipped_app(world: World) -> None:
    """cleanup (even --keep 0) and uninstall leave a shipped App alone."""
    world.publish_shipped("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    world.publish_shipped("tool", "1.1.0", tables=SHIM)
    world.one("update", "tool")
    package = world.user / "tool"

    cleaned = world.one("cleanup", "tool", keep=0)
    world.one("uninstall", "tool")

    assert cleaned.details["apps"] == [] and {"version": "1.0.0", "why": "shipped App is never dropped"} in cleaned.details["kept"]
    assert (package / "1.0.0" / "App" / "app.exe").is_file() and (package / "1.1.0" / "App" / "app.exe").is_file()


def test_purge_of_an_unrecoverable_shipped_app_asks_and_names_it(world: World) -> None:
    """With --no-input --purge exits 5 and deletes nothing; the question names the App as unrecoverable."""
    world.publish_shipped("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    package = world.user / "tool"
    world.engine.no_input = True

    refused = world.one("uninstall", "tool", purge=True)

    assert refused.exit_code == 5 and (package / "1.0.0" / "App" / "app.exe").is_file()
    assert "1.0.0 cannot be rebuilt" in refused.error["message"] and "lost for good" in refused.error["message"]


def test_a_shipped_app_with_a_url_is_never_replaced_but_recovered_when_missing(world: World) -> None:
    """--refresh leaves a modified shipped App alone; after the App is deleted by hand, repair recovers it from the download_url."""
    world.publish_shipped("tool", "1.0.0", download=True, tables=SHIM)
    world.one("install", "tool")
    app = world.user / "tool" / "1.0.0" / "App"
    (app / "app.exe").write_text("mine", encoding="utf-8")

    kept = world.one("install", "tool", refresh=True)

    assert kept.ok and (app / "app.exe").read_text() == "mine"
    for item in sorted(app.rglob("*"), reverse=True):
        item.unlink()
    app.rmdir()
    recovered = world.one("install", "tool")
    assert recovered.ok and (app / "app.exe").read_text() == "tool 1.0.0\n"


def test_a_shipped_app_without_a_url_cannot_be_recovered(world: World) -> None:
    """Without a download_url or script, repair fails with `App is missing and no way to recover it.`."""
    world.publish_shipped("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    app = world.user / "tool" / "1.0.0" / "App"
    (app / "app.exe").unlink()
    app.rmdir()

    result = world.one("install", "tool")

    assert not result.ok and APP_MISSING_ERROR in result.error["message"]


@pytest.mark.parametrize("opt_out", ["package", "global", "none"])
def test_check_reports_a_modified_app_file_unless_the_fingerprint_is_switched_off(world: World, opt_out: str) -> None:
    """A changed file of a shipped App is reported; `record_manifest = false` (package) or the global switch silences it (D41)."""
    top = "record_manifest = false\n" if opt_out == "package" else ""
    if opt_out == "global":
        world.write_app_settings(tables="[install]\nrecord_manifest = false\n")
    world.publish_shipped("tool", "1.0.0", tables=SHIM, top=top)
    world.one("install", "tool")
    (world.user / "tool" / "1.0.0" / "App" / "app.exe").write_text("modified", encoding="utf-8")

    result = world.one("check", "tool")

    reported = [item["kind"] for item in result.details["problems"]]
    assert ("app-modified" in reported) == (opt_out == "none")
    assert result.ok == (opt_out != "none")


def test_a_repair_never_blesses_a_file_that_was_changed_since_the_install(world: World) -> None:
    """The App fingerprint is recorded once: installing again does not record the modified file as the new baseline."""
    world.publish_shipped("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    (world.user / "tool" / "1.0.0" / "App" / "app.exe").write_text("modified", encoding="utf-8")

    repaired = world.one("install", "tool")
    checked = world.one("check", "tool")

    assert repaired.ok and not checked.ok and "app-modified" in [item["kind"] for item in checked.details["problems"]]


def test_check_reports_deleted_and_added_app_files(world: World) -> None:
    """Deleted and extra files of the active App are named in the report."""
    world.publish_shipped("tool", "1.0.0", files={"app.exe": "a", "lib.dll": "b"}, tables=SHIM)
    world.one("install", "tool")
    app = world.user / "tool" / "1.0.0" / "App"
    (app / "lib.dll").unlink()
    (app / "new.txt").write_text("extra", encoding="utf-8")

    result = world.one("check", "tool")

    kinds = {item["kind"]: item["message"] for item in result.details["problems"]}
    assert "lib.dll" in kinds["app-missing"] and "new.txt" in kinds["app-extra"]


# ---------------------------------------------------------------------------
# Failures after a download leave the previous version alone
# ---------------------------------------------------------------------------


def test_a_checksum_mismatch_changes_nothing(world: World) -> None:
    """A download whose bytes do not match the declared digest fails with exit 3 and leaves the machine as it was."""
    world.publish("tool", "1.0.0", tables=SHIM)
    world.one("install", "tool")
    folder = world.publish("tool", "1.1.0", tables=SHIM)
    world.fake.add_url(f"https://example.org/files/tool-1.1.0.zip", b"not the archive that was hashed")
    state = machine_state(world)

    result = world.one("update", "tool")

    assert not result.ok and result.error["code"] == "checksum-mismatch" and result.exit_code == 3
    assert current(world) == "1.0.0" and machine_state(world) == state and not (folder / "App").exists()
