"""Cover the engine under the threat model of design 9.6: an elevated run trusts only what only administrators can change.

Behaviour covered: an elevated install refuses a cache or work folder that standard users can change before anything is
fetched; an elevated ``check-updates`` refuses an interpreter or regex worker in an untrusted folder before launching any
process; package code (a populate script, hooks) from an untrusted folder is never started; an elevated run does not read a
system root that is not admin-only; a hostile install record cannot make uninstall touch anything outside the mode's folders
and does not block the uninstall; a ``.gupkg`` folder that is a junction elsewhere is never written through; ``git`` is found
only in admin-only folders of ``PATH`` when elevated; a sandbox that has no double for a program says the package needs a real
run instead of crashing.

Real: the engine, the catalog, the trust checks and real folders and junctions. Mocked: ``FakeSystem`` decides which folders
are admin-only (its ``untrusted_dirs``) and records every process, URL and registry access.
Out of scope: real ACLs and UAC (``tests/manual_smoke.md``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gupkg.catalog.record import read_install_record, write_install_record
from gupkg.model.state import Effect, InstallRecord
from gupkg.services.gitenv import GitUnavailable, git_command, git_environment, git_executable
from gupkg.system.port import Scope
from gupkg.system.fake import FakeSystem
from tests.engine_support import World, pin_no_ambient_elevation, pkg_text  # noqa: F401

ENV = '[[env]]\nname = "TOOL_HOME"\nvalue = "$App"\n'
SHIM = '[[shim]]\nname = "tool"\ntarget = "$App\\\\app.exe"\n' + ENV


def test_an_elevated_install_refuses_an_untrusted_download_cache_before_fetching(tmp_path: Path) -> None:
    """The cache folder is created and then judged: when users can change it, no request is made and nothing is installed."""
    cache = tmp_path / "sys" / "opt" / ".gupkg" / "cache"
    world = World(tmp_path, elevated=True, untrusted={cache})
    world.publish("tool", "1.0.0", mode="system", tables=ENV)
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "untrusted-location" and world.fake.fetch_log == []
    assert not (world.system_root / "tool" / "current").exists() and not (world.system_root / "tool" / "1.0.0" / "App").exists()


def test_an_elevated_install_refuses_an_untrusted_work_folder_before_fetching(tmp_path: Path) -> None:
    """The scratch folder of the package must be admin-only too."""
    work = tmp_path / "sys" / "opt" / "tool" / ".gupkg" / "work"
    world = World(tmp_path, elevated=True, untrusted={work})
    world.publish("tool", "1.0.0", mode="system", tables=ENV)
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "untrusted-location" and world.fake.fetch_log == []


def test_an_elevated_check_updates_launches_nothing_from_an_untrusted_interpreter_folder(tmp_path: Path) -> None:
    """The interpreter that would start the regex worker is judged first: nothing is started and no request is made."""
    world = World(tmp_path, elevated=True, untrusted={Path(sys.executable).parent})
    world.publish("tool", "1.0.0", mode="system", tables=ENV)
    world.tracker("tool", mode="system", tables=ENV)
    world.announce("tool", "1.1.0")
    world.fake.fetch_log.clear()

    result = world.one("check-updates", "tool")

    assert not result.ok and result.error["code"] == "untrusted-location"
    assert world.fake.process_log == [] and world.fake.regex_log == [] and world.fake.fetch_log == []


def test_an_elevated_run_never_starts_a_populate_script_from_an_untrusted_folder(tmp_path: Path) -> None:
    """Package code in a folder users can change is refused before the script is started."""
    untrusted = tmp_path / "sys" / "opt" / "tool" / "1.0.0" / "pkg.local"
    world = World(tmp_path, elevated=True, untrusted={untrusted})
    double = world.publish_script("tool", "1.0.0", mode="system", tables=ENV)

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "hook-error" and "not admin-only" in result.error["message"]
    assert double.calls == [] and world.fake.process_log == []


@pytest.mark.parametrize("url", ["file://fileserver/share/tool.zip", "file:////fileserver/share/tool.zip"], ids=["host", "unc-share"])
def test_a_download_from_a_network_share_through_a_file_url_is_refused(tmp_path: Path, url: str) -> None:
    """Windows would authenticate to that server, so the engine's client refuses `file:` URLs that name a host or share."""
    world = World(tmp_path)
    folder = world.user / "tool" / "1.0.0"
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text(pkg_text(url, sha256="ab" * 32, tables=ENV), encoding="utf-8")
    world.fake.add_url(url, b"never served")

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "network-error" and url not in world.fake.fetch_log


def test_an_elevated_run_expands_only_machine_variables_in_a_packages_declarations(tmp_path: Path) -> None:
    """`${ProgramData}` resolves, `${USERPROFILE}` (the user's) does not: the install is refused and nothing is applied."""
    world = World(tmp_path, elevated=True)
    world.engine.env = {**world.machine.env, "ProgramData": "C:\\ProgramData", "USERPROFILE": "C:\\Users\\mallory"}
    world.publish("good", "1.0.0", mode="system", tables='[[env]]\nname = "GOOD"\nvalue = "${ProgramData}\\\\good"\n')
    world.publish("bad", "1.0.0", mode="system", tables='[[env]]\nname = "BAD"\nvalue = "${USERPROFILE}\\\\bad"\n')

    good = world.one("install", "good")
    bad = world.one("install", "bad")

    assert good.ok and world.fake.env_values(Scope.MACHINE)["GOOD"].value == "C:\\ProgramData\\good"
    assert not bad.ok and "USERPROFILE" in bad.error["message"] and "BAD" not in world.fake.env_values(Scope.MACHINE)
    assert not (world.system_root / "bad" / "current").exists()


def test_an_elevated_run_does_not_read_a_system_root_that_users_can_change(tmp_path: Path) -> None:
    """When the system root itself is not admin-only an elevated run ignores it: the package is not found, nothing is written."""
    world = World(tmp_path, elevated=True, untrusted={tmp_path / "sys" / "opt"})
    world.publish("tool", "1.0.0", mode="system", tables=ENV)

    result = world.one("install", "tool")

    assert not result.ok and not (world.system_root / "tool" / "current").exists() and not (world.system_root / ".gupkg").exists()


def test_a_hostile_install_record_cannot_steer_uninstall_outside_the_modes_folders(tmp_path: Path) -> None:
    """A record that names a shim in System32 and a real one: the real one is removed, the hostile one is ignored and dropped."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", tables=SHIM)
    assert world.one("install", "tool").ok
    package = world.user / "tool"
    victim = tmp_path / "victim" / "important.exe"
    victim.parent.mkdir()
    victim.write_text("keep me", encoding="utf-8")
    record = read_install_record(package).record
    hostile = Effect("shim", {"path": str(victim), "config": str(victim) + ".toml"})
    write_install_record(package, InstallRecord(record.mode, record.link, (*record.effects, hostile)))

    result = world.one("uninstall", "tool")

    assert result.ok and victim.read_text() == "keep me"
    assert any("Ignored a recorded effect" in warning for warning in result.warnings)
    assert read_install_record(package).record.effects == ()
    assert not (world.fake.scope_folders(Scope.USER).bin_dir / "tool.exe").exists()


def test_a_gupkg_folder_that_is_a_junction_elsewhere_is_never_written_through(tmp_path: Path) -> None:
    """If `<package>\\.gupkg` leads somewhere else, the install refuses and the other place gets nothing."""
    world = World(tmp_path)
    world.publish("tool", "1.0.0", tables=ENV)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.fake.create_junction(world.user / "tool" / ".gupkg", elsewhere)

    result = world.one("install", "tool")

    assert not result.ok and list(elsewhere.iterdir()) == []
    assert not (world.user / "tool" / "current").exists()


def test_a_hook_that_builds_the_name_track_at_run_time_is_refused_when_it_loads(tmp_path: Path) -> None:
    """The static scan cannot see `globals()["tr" + "ack"]`; the loader refuses a version-directory hook that exposes `track`."""
    world = World(tmp_path)
    folder = world.user / "tool" / "1.0.0"
    (folder / "pkg.local").mkdir(parents=True)
    (folder / "pkg.local" / "populate.py").write_text(
        'PKG_MODULE_API = 2\nglobals()["tr" + "ack"] = lambda ctx: None\n\ndef populate(ctx):\n    pass\n', encoding="utf-8"
    )
    (folder / "pkg.toml").write_text(pkg_text(None, script="pkg.local/populate.py", tables=ENV), encoding="utf-8")

    result = world.one("install", "tool")

    assert not result.ok and "exposes track" in result.error["message"]
    assert not (folder / "App").exists()


def test_a_sandbox_without_a_double_says_the_package_needs_a_real_run(tmp_path: Path) -> None:
    """A populate script that the sandbox has no double for is a clear result with code needs-real-run, not a crash."""
    world = World(tmp_path)
    world.publish_script("tool", "1.0.0", tables=ENV)
    del world.fake.processes["populate.cmd"]

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "needs-real-run" and result.exit_code == 3
    assert not (world.user / "tool" / "current").exists() and not (world.user / "tool" / "1.0.0" / "App").exists()


# ---------------------------------------------------------------------------
# git in an elevated run
# ---------------------------------------------------------------------------


def path_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *folders: tuple[str, bool]) -> list[Path]:
    """Put a git.exe in each named folder of PATH (the flag says whether the folder holds it) and return the folders."""
    made = []
    for name, has_git in folders:
        folder = tmp_path / name
        folder.mkdir()
        if has_git:
            (folder / "git.exe").write_text("git", encoding="utf-8")
        made.append(folder)
    monkeypatch.setenv("PATH", ";".join(str(item) for item in made))
    return made


def test_an_elevated_run_resolves_git_only_from_admin_only_folders_of_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first folder of PATH is user-writable and holds a git.exe: it is skipped; the admin-only one is used by its full path."""
    folders = path_with(tmp_path, monkeypatch, ("user-bin", True), ("tools", True))
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={folders[0]})

    assert git_executable(system) == str(folders[1] / "git.exe")
    assert git_command("status", system=system)[0] == str(folders[1] / "git.exe")
    assert git_environment(system)["PATH"] == str(folders[1])


def test_an_elevated_run_with_no_trusted_git_refuses_instead_of_using_the_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When every git.exe on PATH is in a user-writable folder, an elevated run has no git and says why."""
    folders = path_with(tmp_path, monkeypatch, ("user-bin", True))
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={folders[0]})

    with pytest.raises(GitUnavailable, match="administrators"):
        git_executable(system)


def test_a_normal_run_uses_the_bare_git_name_and_keeps_its_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside an elevated run nothing changes: `git` is looked up as before and PATH is passed on."""
    folders = path_with(tmp_path, monkeypatch, ("user-bin", True))
    system = FakeSystem(tmp_path / "machine", elevated=False, untrusted_dirs={folders[0]})

    assert git_executable(system) == "git" and git_environment(system)["PATH"] == str(folders[0])
