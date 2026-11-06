"""Cover the commands that configure and maintain: registry, settings, link, pkg and self.

Behaviour covered: ``registry sync`` publishes a cache (and a failure is exit 3 that keeps the previous tree), ``status`` and
``list`` report it, ``add`` and ``remove`` edit ``[[registry]]`` in the settings file of the chosen layer, and the system root
is written only from an administrator shell; ``settings show|path|init|set`` load the layers and write one file after the schema
accepted the value; ``link`` follows another registry entry, none, or the same-name entry again; ``pkg new`` writes starters that
``pkg check`` accepts, ``pkg check`` reports every problem and ``--install`` installs into a throw-away root on a pretend
machine, ``pkg pin`` shows where a hash came from and asks before it writes, ``pkg export`` writes a ZIP and prints its SHA-256,
``pkg bundle`` copies a verified bundle; ``self status`` checks the manifest and the interpreter and ``self repair|check-updates|
update`` are the lifecycle commands on the package gupkg.

Real: the argument parser, the catalog, the engine, real folders and archives. Mocked: ``FakeSystem``, the environment mapping and
a hand-made bundle tree with its ``MANIFEST.sha256``.
Out of scope: the lifecycle commands themselves (``test_cli_lifecycle.py``) and the real Windows registry and UAC.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

import pytest

from gupkg.engine.receipts import read_receipt
from gupkg.launchers import launcher_text
from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.port import Mode
from gupkg.tomlio import read_toml
from tests.support.archive import registry_zip
from tests.support.cli import SOURCE, CliWorld, version_files
from tests.support.engine import pkg_text, zip_bytes
from tests.support.mode import tree_snapshot

pytestmark = pytest.mark.unit

# Table that declares the shim of the test package.
FULL = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_sync_publishes_the_cache_and_status_and_list_report_it(cli_world: CliWorld) -> None:
    """After add, sync and status: the registry is configured, synchronised, and its packages are counted."""
    assert cli_world.cli("registry", "remove", "main").code == 0
    assert cli_world.cli("registry", "add", "main", SOURCE).code == 0
    cli_world.fake.add_url(SOURCE, registry_zip(version_files("gamma", "1.0")))

    synced = cli_world.cli("registry", "sync", "--json")
    status = cli_world.cli("registry", "status", "--json").document()
    listed = cli_world.cli("registry", "list")

    assert synced.code == 0 and synced.document()["registries"][0]["status"] == "synced"
    assert status["registries"][0]["packages"] == 1 and status["registries"][0]["revision"] and status["mode"] == "user"
    assert f"main  {SOURCE}" in listed.out
    assert (cli_world.user / ".gupkg" / "registry" / "main" / "state.toml").is_file()


def test_a_failing_sync_is_exit_3_and_the_previous_tree_stays(cli_world: CliWorld) -> None:
    """A registry that cannot be downloaded fails with exit code 3 and names the registry; the cache that was there stays valid."""
    cli_world.registry({"alpha": {"versions": {"1.0.0": None}}})
    assert cli_world.cli("registry", "remove", "main").code == 0
    assert cli_world.cli("registry", "add", "main", "https://example.org/unreachable.zip").code == 0

    result = cli_world.cli("registry", "sync")

    assert result.code == 3 and "registry main" in result.err + result.out
    assert "alpha" in cli_world.cli("search", "alp", "--offline").out


def test_registry_sync_of_an_unknown_name_is_a_usage_problem_and_offline_reports_the_cache(cli_world: CliWorld) -> None:
    """Naming a registry that is not configured is exit 2; --offline syncs nothing and uses no network."""
    cli_world.registry({"alpha": {"versions": {"1.0.0": None}}})
    cli_world.fake.fetch_log.clear()

    unknown = cli_world.cli("registry", "sync", "nope")
    offline = cli_world.cli("registry", "sync", "--offline", "--json")

    assert unknown.code == 2
    assert offline.code == 0 and offline.document()["registries"][0]["status"] == "offline" and cli_world.fake.fetch_log == []


def test_registry_add_and_remove_edit_the_user_settings_and_refuse_duplicates(cli_world: CliWorld) -> None:
    """add appends to [[registry]] of the user layer (keeping the inherited ones), remove drops one, duplicates are refused."""
    assert cli_world.cli("registry", "add", "extra", "https://example.org/extra.zip").code == 0

    names = [item["name"] for item in read_toml(cli_world.user / ".gupkg" / "settings.toml")["registry"]]
    assert names == ["main", "extra"]
    assert cli_world.cli("registry", "add", "extra", "https://example.org/other.zip").code == 2
    assert cli_world.cli("registry", "remove", "extra").code == 0
    assert cli_world.cli("registry", "remove", "extra").code == 2
    assert [item["name"] for item in read_toml(cli_world.user / ".gupkg" / "settings.toml")["registry"]] == ["main"]


def test_registry_sync_writes_the_cache_of_the_scopes_root(tmp_path: Path) -> None:
    """A normal shell syncs into the user root's cache and never touches the system root; an elevated shell syncs into the system root's."""
    normal = CliWorld(tmp_path / "a")
    elevated = CliWorld(tmp_path / "b", elevated=True)
    for machine in (normal, elevated):
        machine.fake.add_url("https://github.com/guraltsev/gupkg-registry/archive/HEAD.zip", registry_zip(version_files("gamma", "1.0")))

    refused = normal.cli("registry", "sync")
    synced = elevated.cli("registry", "sync")

    assert refused.code == 0, refused.err
    assert (normal.user / ".gupkg" / "registry" / "main" / "state.toml").is_file() and not (normal.system_root / ".gupkg").exists()
    assert synced.code == 0, synced.err
    assert (elevated.system_root / ".gupkg" / "registry" / "main" / "state.toml").is_file()


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


def test_settings_show_lists_every_effective_value_with_its_layer(cli_world: CliWorld) -> None:
    """show prints each key with the layer that supplied it (here the app settings and the defaults), and json carries the rows."""
    rows = {row["key"]: row for row in cli_world.cli("settings", "show", "--json").document()["settings"]}

    assert rows["roots.user"]["layer"] == "app" and rows["logs.keep_runs"] == {"key": "logs.keep_runs", "value": 50, "layer": "defaults"}
    assert "logs.keep_runs = 50" in cli_world.cli("settings", "show").out


def test_settings_path_names_the_file_of_every_layer(cli_world: CliWorld) -> None:
    """path lists each layer's file with whether it was applied or is missing."""
    layers = {item["layer"]: item for item in cli_world.cli("settings", "path", "--json").document()["layers"]}

    assert layers["app"]["status"] == "applied" and layers["root"]["status"] == "missing"
    assert Path(layers["root"]["path"]) == cli_world.user / ".gupkg" / "settings.toml"


def test_settings_init_then_set_writes_the_user_file_after_validation(cli_world: CliWorld) -> None:
    """init creates the file with the schema number (and refuses to replace it); set changes one value that the schema accepts."""
    path = cli_world.user / ".gupkg" / "settings.toml"

    assert cli_world.cli("settings", "init").code == 0 and read_toml(path) == {"schema": 4}
    assert cli_world.cli("settings", "init").code == 2
    assert cli_world.cli("settings", "init", "--force").code == 0
    assert cli_world.cli("settings", "set", "logs.keep_runs", "20").code == 0 and cli_world.cli("settings", "set", "ui.color", "never").code == 0
    assert read_toml(path)["logs"] == {"keep_runs": 20} and read_toml(path)["ui"] == {"color": "never"}
    rows = {row["key"]: row for row in cli_world.cli("settings", "show", "--json").document()["settings"]}
    assert rows["logs.keep_runs"]["value"] == 20 and rows["logs.keep_runs"]["layer"] == "root"


def test_settings_init_writes_commented_entries_that_are_valid_once_switched_on(cli_world: CliWorld) -> None:
    """The new file explains the common settings in comments; switching every entry on keeps the defaults and the schema accepts it."""
    # Arrange: the root's settings file and the default values that ``settings show`` reports.
    path = cli_world.user / ".gupkg" / "settings.toml"
    defaults = {row["key"]: row["value"] for row in cli_world.cli("settings", "show", "--json").document()["settings"]}
    assert cli_world.cli("settings", "init").code == 0
    text = path.read_text(encoding="utf-8")

    # Assert: the template documents the optional entries.
    assert "shortcut_prefix" in text and "relative to the Start Menu folder" in text

    # Switch every entry on by uncommenting it, then show the settings again.
    path.write_text(re.sub(r"^# (\[\w+\]|\w+ = .*)$", r"\1", text, flags=re.MULTILINE), encoding="utf-8")
    shown = cli_world.cli("settings", "show", "--json")

    # Assert: the entries now come from the root and keep their default values.
    assert shown.code == 0, shown.err
    rows = {row["key"]: row for row in shown.document()["settings"]}
    switched = {key for key, row in rows.items() if row["layer"] == "root"}
    assert {"adopt.shortcut_prefix", "integration.start_menu", "install.record_manifest", "logs.keep_runs"} <= switched
    assert all(rows[key]["value"] == defaults[key] for key in switched)


def test_the_toml_reader_accepts_a_utf8_bom_and_settings_set_edits_such_a_file(cli_world: CliWorld, tmp_path: Path) -> None:
    """A settings file saved by an editor with a UTF-8 BOM is read, and `settings set` rewrites it (without the BOM)."""
    plain = tmp_path / "bom.toml"
    plain.write_bytes('﻿name = "tool"\n'.encode("utf-8"))
    path = cli_world.user / ".gupkg" / "settings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("﻿schema = 4\n[logs]\nkeep_days = 10\n".encode("utf-8"))

    result = cli_world.cli("settings", "set", "logs.keep_runs", "20")

    assert read_toml(plain)["name"] == "tool"
    assert result.code == 0, result.err
    assert read_toml(path)["logs"] == {"keep_days": 10, "keep_runs": 20} and not path.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    ("key", "value", "says"),
    [
        pytest.param("logs.keep_runs", "zero", "whole number", id="wrong-type"),
        pytest.param("logs.keep_runz", "5", "did you mean 'keep_runs'", id="unknown-key"),
        pytest.param("registry", "[]", "gupkg registry", id="registry-array"),
        pytest.param("integration.integrate", "maybe", "must be one of", id="not-a-choice"),
        pytest.param("roots.user", "C:\\x", "cannot be set in a root's own settings.toml", id="roots-in-a-root-layer"),
    ],
)
def test_settings_set_refuses_what_the_schema_refuses_and_leaves_the_file_alone(cli_world: CliWorld, key: str, value: str, says: str) -> None:
    """A value or key the schema does not accept is exit code 2 with the schema's own message, and nothing is written."""
    assert cli_world.cli("settings", "set", "logs.keep_days", "10").code == 0
    path = cli_world.user / ".gupkg" / "settings.toml"
    before = path.read_bytes()

    result = cli_world.cli("settings", "set", key, value)

    assert result.code == 2 and says in result.err and path.read_bytes() == before


def test_settings_set_writes_the_file_of_the_scopes_root(tmp_path: Path) -> None:
    """A normal shell writes the user root's file and never the system root's; elevated, the value goes to the system root's file."""
    normal, elevated = CliWorld(tmp_path / "a"), CliWorld(tmp_path / "b", elevated=True)

    refused = normal.cli("settings", "set", "logs.keep_runs", "9")
    written = elevated.cli("settings", "set", "logs.keep_runs", "9")

    assert refused.code == 0 and not (normal.system_root / ".gupkg" / "settings.toml").exists()
    assert read_toml(normal.user / ".gupkg" / "settings.toml")["logs"] == {"keep_runs": 9}
    assert written.code == 0 and read_toml(elevated.system_root / ".gupkg" / "settings.toml")["logs"] == {"keep_runs": 9}


def test_a_settings_value_changes_what_the_next_run_does(cli_world: CliWorld) -> None:
    """logs.keep_runs from the settings is the retention of the run logs: with 1 only the newest run is kept."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("settings", "set", "logs.keep_runs", "1").code == 0
    for _ in range(3):
        assert cli_world.cli("install", "tool").code == 0

    kept = [item for item in (cli_world.user / "tool" / ".gupkg" / "logs").glob("2*.log")]

    assert len(kept) == 1


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


def test_link_follows_a_registry_entry_none_or_the_same_name_again(cli_world: CliWorld) -> None:
    """link records which entry the package follows in its install record; info and list show it."""
    cli_world.publish("tool", "1.0.0")
    cli_world.registry({"tool": {"versions": {"1.0.0": None}}, "tool-esr": {"versions": {"1.0.0": None}}})
    assert cli_world.cli("install", "tool").code == 0

    assert cli_world.cli("link", "tool", "main/tool-esr").code == 0
    assert cli_world.cli("info", "tool", "--json").document()["packages"][0]["following"] == "main/tool-esr"
    assert cli_world.cli("link", "tool", "--unlink").code == 0
    assert cli_world.cli("info", "tool", "--json").document()["packages"][0]["following"] is None
    assert cli_world.cli("link", "tool").code == 0
    assert cli_world.cli("info", "tool", "--json").document()["packages"][0]["following"] == "main/tool"


def test_link_to_an_entry_that_does_not_exist_is_refused(cli_world: CliWorld) -> None:
    """A registry entry that is not in any registry cache is exit code 2 and the record is not changed."""
    cli_world.publish("tool", "1.0.0")
    assert cli_world.cli("install", "tool").code == 0
    before = (cli_world.user / "tool" / ".gupkg" / "package.toml").read_bytes()

    result = cli_world.cli("link", "tool", "main/nothing")

    assert result.code == 2 and (cli_world.user / "tool" / ".gupkg" / "package.toml").read_bytes() == before


# ---------------------------------------------------------------------------
# pkg
# ---------------------------------------------------------------------------


def test_pkg_new_writes_starters_that_pkg_check_accepts_and_never_overwrites(cli_world: CliWorld, tmp_path: Path) -> None:
    """A tracker and a pinned version are written, pass `pkg check`, and a second `pkg new` refuses to replace them."""
    # Arrange: a folder for the authoring files.
    base = tmp_path / "authoring"

    # Act: write the starters and check them.
    version = cli_world.cli("pkg", "new", "mine@1.0", "--dir", str(base), "--json")
    tracker = cli_world.cli("pkg", "new", "mine", "--track", "github:owner/mine", "--dir", str(base))
    checked = cli_world.cli("pkg", "check", str(base / "mine"), "--json")

    # Assert: the starters pass the check, an existing folder is never overwritten and bad requests are refused.
    assert version.code == 0 and tracker.code == 0, (version.err, tracker.err)
    assert version.document()["files"] == [str(base / "mine" / "1.0" / name) for name in ("pkg.toml", "install.cmd", "manage.cmd")]
    assert (base / "mine" / "_check_updates" / "check-updates.cmd").is_file()
    assert checked.code == 0 and [item["ok"] for item in checked.document()["checks"]] == [True, True]
    assert cli_world.cli("pkg", "new", "mine@1.0", "--dir", str(base)).code == 2
    assert cli_world.cli("pkg", "new", "bad name!", "--dir", str(base)).code == 2
    assert cli_world.cli("pkg", "new", "mine2", "--track", "gitlab:o/r", "--dir", str(base)).code == 2


def test_pkg_check_reports_every_problem_and_exits_2(cli_world: CliWorld, tmp_path: Path) -> None:
    """An invalid pkg.toml is exit code 2 with its errors, also as a document; a missing folder is reported too."""
    folder = tmp_path / "bad" / "1.0"
    folder.mkdir(parents=True)
    folder.joinpath("pkg.toml").write_text('format = 2\n[source]\ndownload_url = "ftp://x/y.zip"\nsha256 = "zz"\n', encoding="utf-8")

    result = cli_world.cli("pkg", "check", str(folder), "--json")

    document = result.document()
    assert result.code == 2 and document["ok"] is False and len(document["checks"][0]["errors"]) >= 1
    assert cli_world.cli("pkg", "check", str(tmp_path / "nowhere")).code == 2


def test_pkg_check_install_installs_into_a_throwaway_root_and_changes_nothing_real(cli_world: CliWorld, tmp_path: Path) -> None:
    """--install runs the real install on a pretend machine: it succeeds, and the real roots and registry are untouched."""
    url, digest = cli_world.serve("mine", "1.0")
    folder = tmp_path / "authoring" / "mine" / "1.0"
    folder.mkdir(parents=True)
    folder.joinpath("pkg.toml").write_text(pkg_text(url, sha256=digest, tables=FULL), encoding="utf-8")
    before = (tree_snapshot(cli_world.user), tree_snapshot(cli_world.system_root), len(cli_world.fake.registry_log), sorted(cli_world.fake.shortcuts()))

    result = cli_world.cli("pkg", "check", str(folder), "--install", "--json")

    document = result.document()
    assert result.code == 0, result.err
    assert document["install"]["ok"] is True and document["install"]["results"][0]["status"] == "installed"
    assert (tree_snapshot(cli_world.user), tree_snapshot(cli_world.system_root), len(cli_world.fake.registry_log), sorted(cli_world.fake.shortcuts())) == before


def test_pkg_check_install_reports_a_missing_checksum_and_refuses_elevation(cli_world: CliWorld, tmp_path: Path) -> None:
    """A version with no hash fails its sandboxed install (exit 3, checksum-missing); an elevated run does not run package code."""
    # Arrange: a version folder whose artifact has no checksum.
    url, _ = cli_world.serve("mine", "1.0")
    folder = tmp_path / "authoring" / "mine" / "1.0"
    folder.mkdir(parents=True)
    folder.joinpath("pkg.toml").write_text(pkg_text(url), encoding="utf-8")

    # Act: check it with installation, then without the checksum rule, then as an elevated user.
    failed = cli_world.cli("pkg", "check", str(folder), "--install", "--json")
    skipped = cli_world.cli("pkg", "check", str(folder), "--install", "--skip-checksum")
    cli_world.fake.elevated = True
    elevated = cli_world.cli("pkg", "check", str(folder), "--install")

    # Assert: the missing checksum is reported, can be skipped, and an elevated run is refused.
    assert failed.code == 3 and failed.document()["install"]["results"][0]["error"]["code"] == "checksum-missing"
    assert skipped.code == 0
    assert elevated.code == 2 and "normal shell" in elevated.err


def test_pkg_pin_shows_where_the_hash_came_from_and_asks_before_writing(cli_world: CliWorld) -> None:
    """The facts (URL, final URL, size, date, hash) and the warning are shown; no answer, --no-input or "n" write nothing."""
    # Arrange: an installed package without a checksum.
    cli_world.publish("loose", "1.0.0", hashed=False)
    assert cli_world.cli("install", "loose", "--skip-checksum").code == 0
    manifest = cli_world.user / "loose" / "1.0.0" / "pkg.toml"
    before = manifest.read_text(encoding="utf-8")

    # Act: decline to pin, once without input and once by answering no.
    refused = cli_world.cli("pkg", "pin", "loose", "--no-input")
    declined = cli_world.cli("pkg", "pin", "loose", stdin="n\n")

    # Assert: both are refused with the facts about the download and nothing is written.
    for result in (refused, declined):
        assert result.code == 5 and manifest.read_text(encoding="utf-8") == before
        text = result.out + result.err
        assert "https://example.org/files/loose-1.0.0.zip" in text and "final url:" in text and "size:" in text
        assert "date:" in text and "NOT verified" in text
    # Accepting writes the pinned hash and a receipt that records it.
    accepted = cli_world.cli("pkg", "pin", "loose", stdin="y\n")
    assert accepted.code == 0, accepted.err
    pinned = manifest.read_text(encoding="utf-8")
    assert 'sha256_from = "manual"' in pinned and read_receipt(cli_world.user / "loose", "1.0.0").data["checksum"]["observed_sha256"] in pinned


def test_pkg_pin_with_yes_pins_and_a_verified_install_has_nothing_to_pin(cli_world: CliWorld) -> None:
    """--yes writes without asking; a version installed against a hash that pkg.toml declares has nothing to pin (exit 0, unchanged)."""
    cli_world.publish("loose", "1.0.0", hashed=False)
    cli_world.publish("good", "1.0.0")
    assert cli_world.cli("install", "loose", "--skip-checksum").code == 0 and cli_world.cli("install", "good").code == 0

    assert cli_world.cli("pkg", "pin", "loose", "--yes", "--json").document()["changed"] is True
    assert cli_world.cli("pkg", "pin", "loose", "--yes").code == 0
    before = (cli_world.user / "good" / "1.0.0" / "pkg.toml").read_bytes()
    nothing = cli_world.cli("pkg", "pin", "good", "--yes", "--json")

    assert nothing.code == 0 and nothing.document()["changed"] is False and nothing.document()["verified"] is True
    assert "NOT verified" not in nothing.out + nothing.err
    assert (cli_world.user / "good" / "1.0.0" / "pkg.toml").read_bytes() == before


def test_pkg_export_writes_a_distribution_zip_and_prints_its_sha256(cli_world: CliWorld, tmp_path: Path) -> None:
    """The ZIP holds the version folder and the tracker, the printed digest is the file's, and the same input gives the same bytes."""
    # Arrange: a starter package with a tracker.
    base = tmp_path / "authoring"
    assert cli_world.cli("pkg", "new", "mine@1.0", "--track", "github:owner/mine", "--dir", str(base)).code == 0
    output = tmp_path / "mine-1.0.zip"

    # Act: export the version.
    result = cli_world.cli("pkg", "export", str(base / "mine" / "1.0"), "-o", str(output), "--json")

    # Assert: the digest is the file's, and the ZIP holds the version and the tracker.
    document = result.document()
    assert result.code == 0 and document["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result.err.strip() == document["sha256"]
    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == [
            "mine/1.0/install.cmd", "mine/1.0/manage.cmd", "mine/1.0/pkg.toml",
            "mine/_check_updates/check-updates.cmd", "mine/_check_updates/pkg.toml",
        ]
        assert archive.read("mine/1.0/install.cmd") == launcher_text("install").encode("ascii")
    # Exporting twice gives the same bytes, and an existing output file is refused.
    second = tmp_path / "again.zip"
    assert cli_world.cli("pkg", "export", str(base / "mine" / "1.0"), "-o", str(second)).code == 0
    assert second.read_bytes() == output.read_bytes()
    assert cli_world.cli("pkg", "export", str(base / "mine" / "1.0"), "-o", str(output)).code == 2


def test_pkg_export_adds_the_launchers_that_a_hand_made_folder_lacks(cli_world: CliWorld, tmp_path: Path) -> None:
    """A version folder with no launchers still exports with install.cmd and manage.cmd, so every distribution carries them."""
    folder = tmp_path / "hand" / "mine" / "1.0"
    folder.mkdir(parents=True)
    manifest = f"format = 2\n[source]\ndownload_url = 'https://example.org/a.zip'\nsha256 = '{'0' * 64}'\n"
    (folder / "pkg.toml").write_text(manifest, encoding="utf-8")
    output = tmp_path / "hand.zip"

    assert cli_world.cli("pkg", "export", str(folder), "-o", str(output)).code == 0

    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == ["mine/1.0/install.cmd", "mine/1.0/manage.cmd", "mine/1.0/pkg.toml"]
    assert not (folder / "install.cmd").exists()


def make_bundle(root: Path, *, tamper: bool = False) -> RunningGupkg:
    """Write a version directory with a ``gupkg`` bundle and its ``MANIFEST.sha256`` and describe the gupkg that runs from it."""
    # Lay out a version directory that holds a gupkg bundle.
    version = root / "bundle" / "1.0.0"
    package = version / "gupkg"
    package.mkdir(parents=True)
    (version / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
    (package / "cli.py").write_text("print('hi')\n", encoding="utf-8")
    (package / "python").mkdir()
    (package / "python" / "python.exe").write_bytes(b"MZ")
    # List every file with its digest, as the manifest does.
    lines = []
    for path in sorted(version.rglob("*")):
        if path.is_file():
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(version).as_posix()}")
    (package / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # A damaged bundle has a file changed after the manifest was written.
    if tamper:
        (package / "cli.py").write_text("print('evil')\n", encoding="utf-8")
    return RunningGupkg("source", package, (package,))


def test_pkg_bundle_copies_the_verified_bundle_and_refuses_a_damaged_one(cli_world: CliWorld, tmp_path: Path) -> None:
    """The bundle of a gupkg whose files match their manifest is copied to <version-dir>\\gupkg; a tampered one is refused."""
    # Arrange: a starter package.
    base = tmp_path / "authoring"
    assert cli_world.cli("pkg", "new", "mine@1.0", "--dir", str(base)).code == 0
    target = base / "mine" / "1.0"

    # Act: bundle a verified gupkg into the version.
    good = cli_world.cli("pkg", "bundle", str(target), location=make_bundle(tmp_path / "good"))

    # Assert: the bundle and the launchers are copied, and a second bundle is refused.
    assert good.code == 0, good.err
    assert (target / "gupkg" / "cli.py").is_file() and (target / "gupkg" / "python" / "python.exe").is_file()
    assert (target / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (target / "manage.cmd").read_bytes() == launcher_text("tui").encode("ascii")
    assert cli_world.cli("pkg", "bundle", str(target), location=make_bundle(tmp_path / "again")).code == 2

    # A bundle with a damaged file is refused and copies nothing.
    other = base / "mine" / "2.0"
    other.mkdir()
    other.joinpath("pkg.toml").write_text((target / "pkg.toml").read_text(encoding="utf-8"), encoding="utf-8")
    bad = cli_world.cli("pkg", "bundle", str(other), location=make_bundle(tmp_path / "bad", tamper=True))
    assert bad.code == 2 and "modified: gupkg/cli.py" in bad.err and not (other / "gupkg").exists()


def test_pkg_launchers_copies_the_templates_into_a_version_directory_and_replaces_old_ones(cli_world: CliWorld, tmp_path: Path) -> None:
    """`pkg launchers` writes install.cmd and manage.cmd (byte for byte the templates), replacing a stale one; a non-version folder is refused."""
    # Arrange: a starter package with a missing and a stale launcher.
    base = tmp_path / "authoring"
    assert cli_world.cli("pkg", "new", "mine@1.0", "--dir", str(base)).code == 0
    target = base / "mine" / "1.0"
    (target / "install.cmd").unlink()
    (target / "manage.cmd").write_bytes(b"@echo stale\r\n")

    # Act: replace the launchers.
    result = cli_world.cli("pkg", "launchers", str(target), "--json")

    # Assert: both launchers are the current templates, and a folder that is not a version is refused.
    assert result.code == 0, result.err
    assert result.document()["launchers"] == ["install.cmd", "manage.cmd"]
    assert (target / "install.cmd").read_bytes() == launcher_text("install").encode("ascii")
    assert (target / "manage.cmd").read_bytes() == launcher_text("tui").encode("ascii")
    assert cli_world.cli("pkg", "launchers", str(tmp_path)).code == 2


# ---------------------------------------------------------------------------
# self
# ---------------------------------------------------------------------------


def test_self_status_checks_the_manifest(cli_world: CliWorld, tmp_path: Path) -> None:
    """A bundle that matches its manifest is ok (exit 0); a modified file is a failure (exit 3) that names it."""
    good = cli_world.cli("self", "status", "--json", location=make_bundle(tmp_path / "good"))
    bad = cli_world.cli("self", "status", location=make_bundle(tmp_path / "bad", tamper=True))

    document = good.document()
    assert good.code == 0 and document["ok"] is True and document["integrity"]["ok"] is True and document["kind"] == "source"
    assert bad.code == 3 and "modified: gupkg/cli.py" in bad.out


def test_self_status_reports_an_interpreter_that_is_not_the_bundled_one(cli_world: CliWorld) -> None:
    """An installed gupkg that is not running on its own python.exe is reported (the tests run on the developer's Python)."""
    result = cli_world.cli("self", "status", "--json")

    document = result.document()
    assert result.code == 3 and document["interpreter_bundled"] is False and document["interpreter"]
    assert any("isolated bundled Python" in problem for problem in document["problems"])


def test_self_repair_check_updates_and_update_are_the_lifecycle_on_the_package_gupkg(cli_elevated_world: CliWorld) -> None:
    """In the root where this gupkg is installed, `self repair` restores a deleted shim and the other two check and activate."""
    # Arrange: an installed gupkg with a deleted shim and a newer version at the vendor.
    cli_elevated_world.tracker("gupkg", mode="system")
    cli_elevated_world.announce("gupkg", "1.0.0")
    cli_elevated_world.publish("gupkg", "1.0.0", mode="system", tables=FULL)
    assert cli_elevated_world.cli("install", "gupkg").code == 0
    shim = cli_elevated_world.fake.scope_folders(Mode.SYSTEM).bin_dir / "tool.exe"
    shim.unlink()
    cli_elevated_world.announce("gupkg", "1.1.0")

    # Act: repair, check for updates and update gupkg through ``self``.
    repaired = cli_elevated_world.cli("self", "repair")
    shim_after_repair = shim.is_file()
    checked = cli_elevated_world.cli("self", "check-updates", "--json")
    updated = cli_elevated_world.cli("self", "update", "--json")

    # Assert: the shim is back and the update is the current version.
    assert repaired.code == 0 and shim_after_repair, repaired.err
    assert checked.document()["results"][0]["status"] == "new-version"
    assert updated.document()["results"][0]["status"] == "updated"
    assert Path(os.path.realpath(cli_elevated_world.system_root / "gupkg" / "current")).name == "1.1.0"
    assert json.loads(updated.out)["command"] == "update"
