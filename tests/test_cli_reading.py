"""Cover the commands that read: list, info, search, logs and shell.

Behaviour covered: ``list`` shows each package with its status and filters by status and mode, prints only names for scripts
(qualifying a name that exists in two modes), and with ``--details`` shows the versions, the vendor-installer label and how the
install was verified; ``info`` shows everything for each target, reports an unknown target without hiding the others, and shows
a registry-only package; ``search`` finds registry entries offline, and refreshes a registry that was never synchronised unless
``--offline``; ``logs`` lists a package's run logs newest first, prints the newest path and opens it; ``shell`` prints the PATH
line of a user root with ``integrate = "none"`` and says nothing is needed for the others. The ``--json`` documents carry
the same facts.

Real: the argument parser, the catalog, the engine (to install the packages), real folders. Mocked: ``FakeSystem`` and the
environment mapping built by the test.
Out of scope: the lifecycle commands (``test_cli_lifecycle.py``) and the registry cache (``test_catalog_registry.py``).
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from gupkg.tomlio import dumps
from tests.cli_support import CliWorld
from tests.engine_support import pkg_text
from tests.mode_support import pin_no_ambient_elevation  # noqa: F401

FULL = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n'


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A fake machine with tool 1.0.0 installed (and 2.0.0 published), and a cached registry holding alpha and beta."""
    machine = CliWorld(tmp_path)
    machine.publish("tool", "1.0.0", tables=FULL)
    assert machine.cli("install", "tool").code == 0
    machine.publish("tool", "2.0.0", tables=FULL)
    machine.registry({"alpha": {"versions": {"1.0.0": None}}, "beta": {"versions": {"2.0.0": None}}})
    return machine


def registry_zip(files: dict[str, str]) -> bytes:
    """Return the ZIP of a registry repository with format 2 and *files*."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("repo-main/_registry.toml", dumps({"format": 2, "name": "main"}))
        for name, text in files.items():
            archive.writestr(f"repo-main/{name}", text)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("flag", "names"),
    [
        pytest.param(None, ["tool"], id="everything-local"),
        pytest.param("--installed", ["tool"], id="installed-includes-outdated"),
        pytest.param("--outdated", ["tool"], id="outdated"),
        pytest.param("--available", ["alpha", "beta"], id="available"),
        pytest.param("--local", [], id="local"),
        pytest.param("--broken", [], id="broken"),
    ],
)
def test_list_filters_by_status(world: CliWorld, flag: str | None, names: list[str]) -> None:
    """Each flag lists only the matching packages: tool is installed and, because 2.0.0 is known, outdated."""
    result = world.cli("list", *([flag] if flag else []), "--names")

    assert result.code == 0 and result.out.split() == names


def test_list_installed_includes_a_package_with_nothing_newer(world: CliWorld) -> None:
    """A package that is installed and up to date is listed by --installed, and only the outdated one by --outdated."""
    world.publish("fresh", "1.0.0")
    assert world.cli("install", "fresh").code == 0

    assert world.cli("list", "--installed", "--names").out.split() == ["fresh", "tool"]
    assert world.cli("list", "--outdated", "--names").out.split() == ["tool"]


def test_list_shows_the_newest_known_version_of_an_outdated_package(world: CliWorld) -> None:
    """The table says `outdated -> 2.0.0` for the installed package; nothing is checked over the network."""
    world.fake.fetch_log.clear()
    world.fake.request_log.clear()

    result = world.cli("list")

    assert result.code == 0 and "outdated -> 2.0.0" in result.out and "1.0.0" in result.out
    assert world.fake.fetch_log == [] and world.fake.request_log == []


def test_list_names_shows_only_the_packages_of_the_scope(world: CliWorld) -> None:
    """A run has one root: a package of the other root is not listed, so a name is never ambiguous and never qualified."""
    world.publish("tool", "1.0.0", mode="system")
    world.publish("sys-only", "1.0.0", mode="system")

    names = world.cli("list", "--names").out.split()

    assert names == ["tool"]


def test_list_json_document_has_the_packages_of_the_scope_with_the_pinned_keys(world: CliWorld) -> None:
    """--json carries the rows of the run's root only, with the pinned keys."""
    world.publish("sys-tool", "1.0.0", mode="system")
    world.publish("user-tool", "1.0.0")

    result = world.cli("list", "--json")

    document = result.document()
    assert [row["name"] for row in document["packages"]] == ["tool", "user-tool"] and document["command"] == "list"
    assert document["mode"] == "user" and document["root"] == str(world.user)
    assert set(document["packages"][0]) == {
        "name", "status", "version", "newest_known", "path", "restriction", "restriction_reason", "following", "problems", "warnings",
    }


def test_list_details_show_the_versions_and_how_the_install_was_verified(world: CliWorld) -> None:
    """--details names the versions, and an install made without a checksum carries the unverified note."""
    world.publish("loose", "1.0.0", hashed=False)
    assert world.cli("install", "loose", "--skip-checksum").code == 0

    result = world.cli("list", "--details")

    assert "versions: 1.0.0 (populated)" in result.out
    assert "installed without a checksum (--skip-checksum)" in result.out
    assert "in user mode the receipt can be edited by the user" in result.out


def test_list_details_label_a_version_that_runs_a_vendor_installer(world: CliWorld) -> None:
    """A version whose source runs a vendor installer is labelled `runs vendor installer` in the listing."""
    folder = world.publish("gimpish", "1.0.0")
    text = folder.joinpath("pkg.toml").read_text(encoding="utf-8")
    folder.joinpath("pkg.toml").write_text(text.replace("[source]", '[source]\nkind = "exe"', 1) + '\n[source.installer]\nrun = ["/S", "/D=$Stage"]\n', encoding="utf-8")

    result = world.cli("list", "--details", "--json")

    row = next(item for item in result.document()["packages"] if item["name"] == "gimpish")
    assert row["versions"][0]["label"] == "runs vendor installer"


def test_info_shows_everything_known_about_each_target(world: CliWorld) -> None:
    """info prints the package's versions, checksum record, receipts and log; a registry-only target shows its entry."""
    result = world.cli("info", "tool", "alpha")

    assert result.code == 0
    assert "tool  1.0.0  outdated" in result.out and "versions: 1.0.0 (populated), 2.0.0 (available)" in result.out
    assert "checksum: verified" in result.out and "receipts: 1.0.0" in result.out and "log:" in result.out
    assert "alpha  -  available" in result.out and "registry versions: 1.0.0" in result.out and "follows: main/alpha" in result.out


def test_info_reports_an_unknown_target_and_still_shows_the_others(world: CliWorld) -> None:
    """One target that is not found is exit code 2 with a structured error; the other target is still shown."""
    result = world.cli("info", "tool", "nothing-like-this", "--json")

    document = result.document()
    assert result.code == 2 and [row["name"] for row in document["packages"]] == ["tool"]
    assert document["errors"][0]["target"] == "nothing-like-this" and document["errors"][0]["code"] == "not-found"


def test_info_json_reports_the_install_notes_and_the_last_check(world: CliWorld) -> None:
    """The document says the stored `verified` is the install's own report in user mode, and the last tracker result."""
    world.tracker("tool")
    world.announce("tool", "2.0.0")
    assert world.cli("check-updates", "tool", "--no-download").code == 0

    row = world.cli("info", "tool", "--json").document()["packages"][0]

    assert row["self_reported"] is True and any(note["kind"] == "self-reported" for note in row["notes"])
    assert row["checksum"]["decision"] == "verified" and row["last_check"]["found"] == "2.0.0"


def test_search_finds_registry_entries_offline(world: CliWorld) -> None:
    """--offline reads the cache only: matches by name, with the registry and the newest version, and no request."""
    world.fake.fetch_log.clear()

    result = world.cli("search", "alp", "--offline")

    assert result.code == 0 and result.out.split()[:3] == ["alpha", "main", "1.0.0"]
    assert world.fake.fetch_log == []
    assert "beta" not in result.out and "No registry package matches" in world.cli("search", "zzz", "--offline").out


def test_search_refreshes_a_registry_that_was_never_synchronised(tmp_path: Path) -> None:
    """Without --offline a first search downloads the configured registry once, then finds its packages."""
    world = CliWorld(tmp_path)
    assert world.cli("registry", "remove", "main").code == 0
    assert world.cli("registry", "add", "main", "https://example.org/main.zip").code == 0
    world.fake.add_url("https://example.org/main.zip", registry_zip({"gamma/1.0/pkg.toml": pkg_text("https://example.org/g.zip", sha256="a" * 64)}))

    result = world.cli("search", "gam")

    assert result.code == 0 and result.out.split()[0] == "gamma"
    assert world.fake.fetch_log == ["https://example.org/main.zip"]
    world.cli("search", "gam")
    assert world.fake.fetch_log == ["https://example.org/main.zip"]


def test_logs_lists_a_package_newest_first_and_prints_or_opens_the_newest(world: CliWorld) -> None:
    """`logs tool` lists the run logs newest first, --path prints the newest one, --open opens it."""
    assert world.cli("update", "tool").code == 0

    listed = world.cli("logs", "tool").out.split()
    newest = world.cli("logs", "tool", "--path").out.strip()
    opened = world.cli("logs", "tool", "--open")

    assert len(listed) == 2 and listed == sorted(listed, reverse=True) and newest == listed[0]
    assert opened.code == 0 and world.fake.opened == [Path(listed[0])]
    document = world.cli("logs", "tool", "--json").document()
    assert document["logs"] == listed


def test_logs_without_a_target_lists_the_roots_logs_and_a_package_without_logs_is_an_error(world: CliWorld) -> None:
    """The root's run logs are listed with no target; --path on a package that has none is exit code 2."""
    world.publish("never-run", "1.0.0")
    root_logs = world.cli("logs").out.split()

    missing = world.cli("logs", "never-run", "--path")

    assert root_logs and all(".gupkg\\logs" in item and "\\tool\\" not in item for item in root_logs)
    assert missing.code == 2 and "has no run logs" in missing.err


def test_shell_prints_the_path_line_of_an_integrate_none_root(tmp_path: Path) -> None:
    """With ``[integration] integrate = "none"`` the root's bin folder is not on PATH, so `gupkg shell` prints the line that adds it."""
    world = CliWorld(tmp_path)
    world.write_app_settings(tables='[integration]\nintegrate = "none"\n')

    result = world.cli("shell")

    assert result.code == 0 and result.out.strip() == f'set "PATH={world.user / "bin"};%PATH%"'
    document = world.cli("shell", "--json").document()
    assert document["path_line"] == result.out.strip() and document["bin"] == str(world.user / "bin")


def test_shell_has_nothing_to_print_for_a_mode_that_edits_the_path_itself(world: CliWorld) -> None:
    """User mode adds its bin folder to PATH through the registry, so there is no line to print (and a note on stderr)."""
    result = world.cli("shell")

    assert result.code == 0 and result.out == "" and "nothing to print" in result.err
