"""Cover ``gupkg-migrate package``: format 1 to format 2, `_bootstrap` to `_check_updates`.

Behavior covered: the whole real 0.17 registry converts to format 2 packages that the
validator accepts with zero errors and each `_bootstrap` becomes `_check_updates`;
`--check` prints a diff and changes nothing; a normal run keeps the original as a backup;
a result that fails validation leaves the original in place; a three-entry
`[[origin.versions]]` becomes one `[source]` and the other two entries are listed with a
`gupkg pkg new` command; the old key spellings, portable flag and variables are mapped;
the 1.0 launchers are written; hooks that match a known service are reported.

Real: the command line (``gupkg_migrate.cli.main``), the real 0.17 registry manifests and
hooks copied into ``tests/fixtures/registry_017`` and then into a temporary folder, and
``gupkg.pkgcheck`` as the validator. Mocked: nothing. Out of scope: shortcuts, settings
(their own test modules), running the launchers, and adoption of installed 0.17 packages
(``test_migrate_adopt``).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, parse_manifest
from gupkg.pkgcheck import check_directory
from gupkg_migrate import package as package_module
from gupkg_migrate.cli import main
from gupkg_migrate.package import apply_plan, plan_package

REGISTRY = Path(__file__).parent / "fixtures" / "registry_017"


@pytest.fixture
def registry(tmp_path: Path) -> Path:
    """A copy of the real 0.17 registry (79 `pkg.toml` files and their hooks) under a temporary folder."""
    destination = tmp_path / "registry"
    shutil.copytree(REGISTRY, destination)
    return destination


def snapshot(root: Path) -> dict[str, str]:
    """Return {relative path: sha256} of every file under *root*, and an entry per folder."""
    state = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        state[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<dir>"
    return state


def write_package(root: Path, name: str, directory: str, manifest: str, files: dict[str, str] | None = None) -> Path:
    """Write `<root>/<name>/<directory>/pkg.toml` (format 1 text) with optional extra files."""
    folder = root / name / directory
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text(manifest, encoding="utf-8")
    for relative, text in (files or {}).items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(text, encoding="utf-8")
    return folder


def load_tracker(folder: Path):
    """Parse a converted `_check_updates` manifest."""
    return parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)


def test_the_whole_0_17_registry_converts_to_valid_format_2(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every package of the real registry converts, passes validation with no error, and `_bootstrap` becomes `_check_updates`."""
    old_trackers = sorted(path.parent.parent.name for path in registry.glob("*/_bootstrap/pkg.toml"))
    assert len(old_trackers) == 78

    status = main(["package", str(registry)])

    output = capsys.readouterr()
    assert status == 0, output.err
    assert "0 failed" in output.out
    assert not list(registry.glob("*/_bootstrap"))
    assert sorted(path.parent.parent.name for path in registry.glob("*/_check_updates/pkg.toml")) == old_trackers
    checked = [path.parent for path in registry.glob("*/*/pkg.toml")]
    assert len(checked) == 79
    for directory in checked:
        # A 0.17 hook is not run by 1.0: the only errors left are the "rewrite for hook API 2" ones,
        # which the converter reports as notes for the author.
        report = check_directory(directory)
        leftover = [error for error in report.errors if not error.endswith("must declare PKG_MODULE_API = 2")]
        assert leftover == [], f"{directory.relative_to(registry)}: {leftover}"
        if directory.parent.name == "firefox":
            # The filter above must not be vacuous: firefox keeps 0.17 hooks, and they are reported.
            assert any("PKG_MODULE_API" in error for error in report.errors)


def test_converted_registry_keeps_the_meaning_of_representative_packages(registry: Path) -> None:
    """GitHub, module, git, rename, extract, portable and version-directory packages map to the intended format 2."""
    assert main(["package", str(registry)]) == 0

    gh = load_tracker(registry / "gh" / "_check_updates")
    assert (gh.track.provider, gh.track.repo, gh.track.options["asset"]) == ("github", "cli/cli", "gh_${version}_windows_amd64.zip")
    assert gh.source.download_url == "${asset_url}" and gh.source.shipped is False
    assert gh.project_url == "https://cli.github.com/"
    assert [(s.name, s.target) for s in gh.shims] == [("gh", "$App\\bin\\gh.exe")]

    sqlite = load_tracker(registry / "sqlitebrowser" / "_check_updates")
    assert sqlite.modes == ("portable",)
    assert sqlite.track.repo == "sqlitebrowser/sqlitebrowser"
    assert sqlite.shims[0].type == "gui"

    firefox = load_tracker(registry / "firefox" / "_check_updates")
    assert (firefox.track.provider, firefox.track.options["module"]) == ("module", "pkg.local/check_update.py")
    assert firefox.source.script == "pkg.local/unpack_app.py"
    assert firefox.shortcuts[0].working_dir == "%USERPROFILE%"

    latex = load_tracker(registry / "latex-toolkit" / "_check_updates")
    assert latex.track.provider == "git" and latex.track.repo_url == "git@github.com:guraltsev/latex-toolkit.git"
    assert latex.source.kind == "git" and latex.source.git.commit == "${commit}"

    tixati = load_tracker(registry / "tixati" / "_check_updates")
    assert [(rule.src, rule.dest) for rule in tixati.source.extract] == [
        ("Tixati_portable/tixati_Windows64bit.exe", ""),
        ("Tixati_portable/tixati_portable_mode.txt", ""),
    ]

    sumatra = load_tracker(registry / "SumatraPDF-portable" / "_check_updates")
    assert sumatra.prepare[0].source == "SumatraPDF-${version}-64.exe" and sumatra.prepare[0].target == "SumatraPDF.exe"

    version = parse_manifest((registry / "qbittorrent" / "5.2.3" / "pkg.toml").read_text(encoding="utf-8"))
    assert version.source.script == "pkg.local/populate_app.py"
    assert version.track is None
    assert version.shortcuts[0].target == "$App/qbittorrent.exe"


def test_check_mode_prints_a_diff_and_changes_no_file(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--check` shows what would change and leaves every file and folder exactly as it was."""
    before = snapshot(registry)

    status = main(["package", str(registry / "firefox"), "--check"])

    output = capsys.readouterr().out
    assert status == 0
    assert snapshot(registry) == before
    assert "[would convert] firefox/_check_updates (was _bootstrap)" in output
    assert "--- _bootstrap/pkg.toml (format 1)" in output
    assert "+++ _check_updates/pkg.toml (format 2)" in output
    assert "+format = 2" in output
    assert "-homepage =" in output and "+project_url =" in output


def test_a_normal_run_keeps_the_original_as_a_backup(registry: Path) -> None:
    """The original `pkg.toml` is kept byte for byte as `pkg.toml.bak` unless `--no-backup` is given."""
    original = (REGISTRY / "gh" / "_bootstrap" / "pkg.toml").read_bytes()
    plain = (REGISTRY / "tixati" / "_bootstrap" / "pkg.toml").read_bytes()

    assert main(["package", str(registry / "gh")]) == 0
    assert main(["package", str(registry / "tixati"), "--no-backup"]) == 0

    assert (registry / "gh" / "_check_updates" / "pkg.toml.bak").read_bytes() == original
    assert not (registry / "tixati" / "_check_updates" / "pkg.toml.bak").exists()
    assert plain != (registry / "tixati" / "_check_updates" / "pkg.toml").read_bytes()


def test_a_second_run_finds_everything_already_converted_and_changes_nothing(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Running the tool again over converted packages reports them as format 2 and rewrites nothing."""
    assert main(["package", str(registry)]) == 0
    capsys.readouterr()
    before = snapshot(registry)

    assert main(["package", str(registry)]) == 0

    assert snapshot(registry) == before
    assert capsys.readouterr().out.count("[already format 2]") == 79


def test_a_result_that_fails_validation_leaves_the_original_in_place(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """When the converted file would be invalid, the original stays, with no backup, launcher or rename."""
    broken = (
        'description = "x"\nhomepage = "not a web address"\n[update.check]\nmode = "module"\n[update.payload]\nmode = "module"\n'
    )
    tracker = write_package(tmp_path, "bad", "_bootstrap", broken, {"pkg.local/check_update.py": "x = 1\n", "pkg.local/unpack_app.py": "x = 1\n"})
    version = write_package(tmp_path, "bad", "1.0", 'homepage = "not a web address"\n', {"App/tool.exe": "x"})
    before = snapshot(tmp_path)

    status = main(["package", str(tmp_path / "bad")])

    assert status == 1
    assert snapshot(tmp_path) == before
    assert (tracker / "pkg.toml").read_text(encoding="utf-8") == broken
    assert not (version / "pkg.toml.bak").exists() and not (version / "install.cmd").exists()
    assert "project_url" in capsys.readouterr().err


def test_three_origin_versions_become_one_source_and_the_others_are_listed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Of three `[[origin.versions]]`, the directory's own becomes `[source]`, the other two are listed with `gupkg pkg new`."""
    manifest = (
        'description = "Tool"\nhomepage = "https://example.org/tool"\n'
        + "".join(
            f'[[origin.versions]]\nversion = "{version}"\nurl = "https://example.org/tool-{version}.zip"\n'
            f'checksum = "sha256:{digit * 64}"\n'
            for version, digit in (("1.0.0", "a"), ("1.1.0", "b"), ("1.2.0", "c"))
        )
    )
    folder = write_package(tmp_path, "tool", "1.1.0", manifest, {"App/tool.exe": "x"})

    status = main(["package", str(folder)])

    output = capsys.readouterr().out
    assert status == 0
    converted = parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8"))
    assert converted.source.download_url == "https://example.org/tool-1.1.0.zip"
    assert converted.source.sha256 == "b" * 64
    assert "origin" not in (folder / "pkg.toml").read_text(encoding="utf-8")
    assert "gupkg pkg new tool@1.0.0" in output and "gupkg pkg new tool@1.2.0" in output
    assert "gupkg pkg new tool@1.1.0" not in output
    assert check_directory(folder).ok


def test_old_spellings_flags_and_variables_are_mapped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Environment, shortcut, bin and `$Icons` spellings, `only_portable`, `ignore_checksum` and version keys all convert."""
    manifest = (
        'name = "Gadget"\nversion = "2.0"\nlocalVersion = 1\nonly_portable = true\ndescription = "G"\n'
        'homepage = "https://example.org/"\n'
        '[origin]\nurl = "https://example.org/gadget-2.0.zip"\nextractSubdir = "gadget"\n'
        '[update]\n[update.check]\nmode = "module"\n[update.payload]\nmode = "zip"\nignore_checksum = true\n'
        '[[environment]]\nName = "GADGET_HOME"\nValue = "$App"\n'
        '[[shortcut]]\nname = "Gadget"\ntargetPath = "$App/gadget.exe"\niconLocation = "$Icons/gadget.ico,0"\n'
        'workingDirectory = "$App"\n'
        '[[bin]]\nname = "gadget"\ntarget = "$App\\\\gadget.exe"\nworking_dir = "$App"\narguments = ["--x"]\n'
        '[[path]]\nvalue = "$App"\n'
    )
    folder = write_package(tmp_path, "gadget", "2.0", manifest, {"App/gadget.exe": "x", "pkg.local/check_update.py": "x = 1\n"})

    status = main(["package", str(folder)])

    output = capsys.readouterr().out
    assert status == 0, output
    converted = parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8"))
    assert converted.modes == ("portable",)
    assert converted.source.download_url == "https://example.org/gadget-2.0.zip"
    assert converted.source.subdir == "gadget" and converted.source.shipped is False
    assert [(e.name, e.value) for e in converted.envs] == [("GADGET_HOME", "$App")]
    shortcut = converted.shortcuts[0]
    assert (shortcut.target, shortcut.working_dir) == ("$App/gadget.exe", "$App")
    assert shortcut.icon_location == "$VersionRoot\\Icons/gadget.ico,0"
    assert converted.shims[0].working_dir == "$App" and converted.shims[0].arguments == ("--x",)
    assert converted.project_url == "https://example.org/"
    assert "dropped top-level version" in output
    assert "dropped [update]" in output
    assert "pkg.local/check_update.py is no longer used" in output


def test_version_directory_without_an_origin_is_shipped(tmp_path: Path) -> None:
    """A 0.17 version directory that names no origin keeps its App as a shipped one, written explicitly."""
    folder = write_package(tmp_path, "local", "1.0", 'description = "x"\n', {"App/tool.exe": "x"})

    assert main(["package", str(folder)]) == 0

    converted = parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8"))
    assert converted.source.shipped_declared is True
    assert converted.source.app_kind == "shipped"


def test_a_script_outside_pkg_local_is_moved_there(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A 0.17 origin script that lies elsewhere in the directory becomes a populate script in `pkg.local`."""
    manifest = 'description = "x"\n[origin]\nscript = "tools\\\\fetch.ps1"\n'
    folder = write_package(tmp_path, "p", "1.0", manifest, {"tools/fetch.ps1": "Write-Output 1\n"})

    assert main(["package", str(folder)]) == 0

    assert (folder / "pkg.local" / "fetch.ps1").read_text(encoding="utf-8") == "Write-Output 1\n"
    assert not (folder / "tools" / "fetch.ps1").exists()
    assert parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8")).source.script == "pkg.local/fetch.ps1"
    assert "move: tools/fetch.ps1 -> pkg.local/fetch.ps1" in capsys.readouterr().out


def test_versions_get_install_and_tui_launchers_and_a_tracker_gets_only_check_updates(registry: Path) -> None:
    """A converted version gets `install.cmd` and `gupkg-tui.cmd`; a tracker gets `check-updates.cmd` and loses the old PATH-searching `gupkg-tui.cmd`."""
    old = "@echo off\nfor %%P in (\"%PATH:;=\" \"%\") do echo %%P\ncall gupkg tui\n"
    (registry / "gh" / "_bootstrap" / "gupkg-tui.cmd").write_text(old, encoding="utf-8")

    assert main(["package", str(registry)]) == 0

    version = registry / "qbittorrent" / "5.2.3"
    for name in ("install.cmd", "gupkg-tui.cmd"):
        text = (version / name).read_text(encoding="ascii")
        assert "..\\..\\.gupkg\\gupkg.cmd" in text or "\\.gupkg\\gupkg.cmd" in text
        assert "gupkg\\gupkg.cmd" in text and "%PATH" not in text
    assert "tui" in (version / "gupkg-tui.cmd").read_text(encoding="ascii")
    tracker = registry / "gh" / "_check_updates"
    check_launcher = (tracker / "check-updates.cmd").read_text(encoding="ascii")
    assert "check-updates" in check_launcher and "%PATH" not in check_launcher and "\\.gupkg\\gupkg.cmd" in check_launcher
    assert not (tracker / "gupkg-tui.cmd").exists() and not (tracker / "install.cmd").exists()
    assert (tracker / "gupkg-tui.cmd.bak").read_text(encoding="utf-8") == old
    assert not (registry / "firefox" / "_check_updates" / "install.cmd").exists()


def test_hooks_that_match_a_known_service_are_reported_with_the_replacement(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The real firefox and portablegit hooks are reported against the services that replace them."""
    assert main(["package", str(registry / "firefox"), "--check"]) == 0
    firefox = capsys.readouterr().out
    assert main(["package", str(registry / "portablegit"), "--check"]) == 0
    portablegit = capsys.readouterr().out

    assert 'pkg.local/check_update.py matches track: json; use [track] provider = "json"' in firefox
    assert "pkg.local/check_update.py matches track: github" in portablegit


def test_package_command_accepts_a_registry_a_package_folder_or_one_directory(registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The path may be a single directory, a package folder (its versions and tracker) or a whole registry."""
    assert main(["package", str(registry / "gh" / "_bootstrap"), "--check"]) == 0
    assert "1 directories" in capsys.readouterr().out
    assert main(["package", str(registry / "qbittorrent"), "--check"]) == 0
    assert "1 directories" in capsys.readouterr().out
    assert main(["package", str(registry), "--check"]) == 0
    assert "79 directories" in capsys.readouterr().out
    assert main(["package", str(registry / "nowhere")]) == 1
    assert "no pkg.toml found" in capsys.readouterr().err


def test_current_junction_and_data_folders_are_not_converted_twice(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A package folder's `data` and `current` entries are skipped, so a version is converted once."""
    folder = write_package(tmp_path, "tool", "1.0", 'description = "x"\n', {"App/tool.exe": "x"})
    write_package(tmp_path, "tool", "data", 'description = "state, not a version"\n')
    write_package(tmp_path, "tool", "current", 'description = "alias"\n')

    assert main(["package", str(tmp_path / "tool")]) == 0

    assert "1 directories" in capsys.readouterr().out
    assert (folder / "pkg.toml.bak").exists()
    assert not (tmp_path / "tool" / "data" / "pkg.toml.bak").exists()


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://sourceforge.net/projects/tool/files/latest/download", id="sourceforge-latest"),
        pytest.param("https://downloads.example.org/get?id=3", id="query-string"),
    ],
)
def test_a_download_url_without_a_file_extension_is_kept_with_its_checksum(tmp_path: Path, url: str, capsys: pytest.CaptureFixture[str]) -> None:
    """An origin URL that is not a project page stays the download URL, with its checksum, and a note says to check it."""
    manifest = f'description = "x"\n[origin]\nurl = "{url}"\nchecksum = "sha256:{"d" * 64}"\nextractSubdir = "inner"\n'
    folder = write_package(tmp_path, "tool", "1.0", manifest, {"App/tool.exe": "x"})

    assert main(["package", str(folder)]) == 0

    converted = parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8"))
    assert converted.source.download_url == url and converted.source.shipped is False
    assert converted.source.sha256 == "d" * 64 and converted.source.subdir == "inner"
    assert "was kept as download_url" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://github.com/o/r", "https://gitlab.com/o/r", "https://example.org/o/r.git"])
def test_a_repository_page_is_not_a_download_and_the_dropped_checksum_is_reported(tmp_path: Path, url: str, capsys: pytest.CaptureFixture[str]) -> None:
    """A repository page becomes a shipped source, and the checksum and subdirectory that went with it are named in notes."""
    manifest = f'description = "x"\n[origin]\nurl = "{url}"\nchecksum = "sha256:{"d" * 64}"\nextractSubdir = "inner"\n'
    folder = write_package(tmp_path, "tool", "1.0", manifest, {"App/tool.exe": "x"})

    assert main(["package", str(folder)]) == 0

    output = capsys.readouterr().out
    assert parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8")).source.shipped is True
    assert "repository or project page" in output
    assert "checksum = 'sha256:" in output and "extractSubdir = 'inner' was dropped" in output


def test_a_malformed_checksum_is_reported_not_dropped_silently(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A checksum that is not sha256:<hex> is dropped with a note."""
    manifest = 'description = "x"\n[origin]\nurl = "https://example.org/a.zip"\nchecksum = "md5:abc"\n'
    folder = write_package(tmp_path, "tool", "1.0", manifest, {"App/tool.exe": "x"})

    assert main(["package", str(folder)]) == 0

    assert "not sha256:<hex> and was dropped" in capsys.readouterr().out


def test_a_placeholder_entry_for_the_own_version_reports_what_it_drops(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A version-only `[[origin.versions]]` entry with a checksum and subdirectory gives a shipped source and names both dropped keys."""
    manifest = (
        'description = "x"\n[[origin.versions]]\nversion = "1.0"\nchecksum = "sha256:' + "e" * 64 + '"\nextractSubdir = "inner"\n'
    )
    folder = write_package(tmp_path, "tool", "1.0", manifest, {"App/tool.exe": "x"})

    assert main(["package", str(folder)]) == 0

    output = capsys.readouterr().out
    assert parse_manifest((folder / "pkg.toml").read_text(encoding="utf-8")).source.shipped is True
    assert "checksum = 'sha256:" in output and "extractSubdir = 'inner' was dropped" in output


def test_a_version_whose_source_was_an_origin_module_gets_the_hook_api_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A version directory whose populate script comes from an old `origin.module` is told to rewrite it for hook API 2."""
    folder = write_package(tmp_path, "tool", "1.0", 'description = "x"\n[origin]\nmodule = "pkg.local/populate_app.py"\n',
                           {"pkg.local/populate_app.py": "x = 1\n"})

    assert main(["package", str(folder)]) == 0

    assert "uses hook API 1; rewrite it for hook API 2" in capsys.readouterr().out


def test_output_says_comments_are_not_preserved_and_the_backup_stays(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The run states that comments of the 0.17 file are lost in the new file and that `pkg.toml.bak` stays in the directory."""
    folder = write_package(tmp_path, "tool", "1.0", '# a comment\ndescription = "x"\n', {"App/tool.exe": "x"})

    assert main(["package", str(folder)]) == 0

    output = capsys.readouterr().out
    assert "comments of the 0.17 file are not preserved" in output
    assert "stays in the directory" in output
    assert "# a comment" in (folder / "pkg.toml.bak").read_text(encoding="utf-8")


def test_a_conflicting_pkg_local_file_fails_the_run_and_leaves_the_directory_unchanged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A script that would be moved onto an existing `pkg.local` file stops the conversion with every file untouched."""
    manifest = 'description = "x"\n[origin]\nscript = "tools/fetch.ps1"\n'
    folder = write_package(tmp_path, "p", "1.0", manifest, {"tools/fetch.ps1": "new\n", "pkg.local/fetch.ps1": "existing\n"})
    before = snapshot(tmp_path)

    assert main(["package", str(folder)]) == 1

    assert snapshot(tmp_path) == before
    assert "already exists" in capsys.readouterr().err


def test_a_conflict_that_appears_after_planning_is_refused_before_any_change(tmp_path: Path) -> None:
    """A file created between planning and applying makes `apply_plan` refuse before it writes anything."""
    manifest = 'description = "x"\n[origin]\nscript = "tools/fetch.ps1"\n'
    folder = write_package(tmp_path, "p", "1.0", manifest, {"tools/fetch.ps1": "new\n"})
    plan = plan_package(folder)
    assert plan.ok
    (folder / "pkg.local").mkdir(exist_ok=True)
    (folder / "pkg.local" / "fetch.ps1").write_text("late\n", encoding="utf-8")
    before = snapshot(tmp_path)

    with pytest.raises(ValueError, match="already exists"):
        apply_plan(plan)

    assert snapshot(tmp_path) == before


def test_a_failure_in_the_middle_of_applying_restores_the_original_directory(tmp_path: Path) -> None:
    """When a late step fails (here the tracker launcher's path is a folder), every earlier step is undone, including the rename."""
    folder = write_package(tmp_path, "tool", "_bootstrap",
                           'description = "x"\n[update.check]\nmode = "module"\n[update.payload]\nmode = "module"\n'
                           '[origin]\nurl = "https://example.org/a.zip"\n',
                           {"pkg.local/check_update.py": "x = 1\n", "pkg.local/unpack_app.py": "x = 1\n", "gupkg-tui.cmd": "old\n"})
    plan = plan_package(folder)
    assert plan.ok and plan.launchers == ["check-updates.cmd"] and plan.stale_launchers == ["gupkg-tui.cmd"]
    (folder / "check-updates.cmd").mkdir()
    before = snapshot(tmp_path)

    with pytest.raises(OSError):
        apply_plan(plan)

    assert snapshot(tmp_path) == before
    assert not (tmp_path / "tool" / "_check_updates").exists()


def test_an_existing_launcher_is_backed_up_before_it_is_replaced(tmp_path: Path) -> None:
    """A hand-edited `install.cmd` is kept as `install.cmd.bak` when the 1.0 launcher replaces it."""
    folder = write_package(tmp_path, "tool", "1.0", 'description = "x"\n', {"App/tool.exe": "x", "install.cmd": "my own\n"})

    assert main(["package", str(folder)]) == 0

    assert (folder / "install.cmd.bak").read_text(encoding="utf-8") == "my own\n"
    assert "gupkg.cmd" in (folder / "install.cmd").read_text(encoding="ascii")


def test_a_failed_apply_restores_the_modification_time_and_removes_what_it_created(tmp_path: Path) -> None:
    """After a late failure the original keeps its time, a read-only backup is removed, and an empty pkg.local that the move created is gone."""
    manifest = 'description = "x"\n[origin]\nscript = "tools/fetch.ps1"\n'
    folder = write_package(tmp_path, "p", "1.0", manifest, {"tools/fetch.ps1": "new\n", "App/a.exe": "x"})
    plan = plan_package(folder)
    assert plan.ok and plan.launchers
    manifest_path = folder / "pkg.toml"
    manifest_path.chmod(0o444)
    os.utime(manifest_path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
    (folder / "install.cmd").mkdir()
    before = snapshot(tmp_path)

    with pytest.raises(OSError):
        apply_plan(plan)

    assert snapshot(tmp_path) == before
    assert manifest_path.stat().st_mtime_ns == 1_000_000_000_000_000_000
    assert not (folder / "pkg.toml.bak").exists() and not (folder / "pkg.local").exists()
    manifest_path.chmod(0o666)


def test_an_original_script_that_cannot_be_removed_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The conversion is complete once the new files are in place; a script left behind is a line in the report."""
    manifest = 'description = "x"\n[origin]\nscript = "tools/fetch.ps1"\n'
    folder = write_package(tmp_path, "p", "1.0", manifest, {"tools/fetch.ps1": "new\n", "App/a.exe": "x"})
    plan = plan_package(folder)
    real_remove = package_module._remove_file

    def refusing(path: Path) -> None:
        """Refuse to delete the original script, as a locked file would."""
        if path.name == "fetch.ps1" and path.parent.name == "tools":
            raise PermissionError("locked")
        real_remove(path)

    monkeypatch.setattr(package_module, "_remove_file", refusing)

    lines = apply_plan(plan)

    assert any("could not remove the original tools/fetch.ps1" in line for line in lines)
    assert (folder / "pkg.local" / "fetch.ps1").is_file() and "script" in (folder / "pkg.toml").read_text(encoding="utf-8")


def test_a_directory_that_cannot_be_converted_does_not_stop_the_other_directories(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An OSError on one directory is reported and counted; the rest of the run continues."""
    write_package(tmp_path, "a", "1.0", 'description = "x"\n', {"App/a.exe": "x"})
    broken = write_package(tmp_path, "b", "1.0", 'description = "x"\n', {"App/a.exe": "x"})
    write_package(tmp_path, "c", "1.0", 'description = "x"\n', {"App/a.exe": "x"})
    (broken / "install.cmd").mkdir()

    status = main(["package", str(tmp_path), "--no-adopt"])

    captured = capsys.readouterr()
    assert status == 1 and "3 directories, 1 failed" in captured.out
    assert "[FAILED]" in captured.err and (tmp_path / "c" / "1.0" / "install.cmd").is_file()


@pytest.mark.parametrize("prefix", ["sha256:", "SHA256:", "Sha256:"])
def test_the_sha256_prefix_is_read_without_regard_to_letter_case(tmp_path: Path, prefix: str) -> None:
    """A 0.17 checksum spelled `SHA256:<hex>` is carried over like `sha256:<hex>`."""
    digest = "ab" * 32
    manifest = f'description = "x"\n[origin]\nurl = "https://example.org/a.zip"\nchecksum = "{prefix}{digest}"\n'
    folder = write_package(tmp_path, "p", "1.0", manifest)

    plan = plan_package(folder)

    assert plan.ok and f'sha256 = "{digest}"' in plan.new_text


def test_the_own_versions_entry_is_used_when_the_inline_origin_is_a_project_page(tmp_path: Path) -> None:
    """An inline [origin] url that is only a repository page does not drop the entry for this very version."""
    manifest = (
        'description = "x"\n[origin]\nurl = "https://github.com/owner/tool"\n'
        '[[origin.versions]]\nversion = "1.0"\nurl = "https://example.org/tool-1.0.zip"\nchecksum = "sha256:' + "cd" * 32 + '"\n'
    )
    folder = write_package(tmp_path, "tool", "1.0", manifest)

    plan = plan_package(folder)

    assert plan.ok and 'download_url = "https://example.org/tool-1.0.zip"' in plan.new_text
    assert any("project page, so this version's own" in note for note in plan.notes)


def test_no_backup_says_that_no_copy_of_the_comments_is_kept(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With --no-backup the note no longer claims a .bak keeps the comments."""
    folder = write_package(tmp_path, "tool", "1.0", 'description = "x"\n', {"App/tool.exe": "x"})

    main(["package", str(folder), "--no-backup"])

    output = capsys.readouterr().out
    assert "pkg.toml.bak keeps them" not in output and "--no-backup keeps no copy" in output
