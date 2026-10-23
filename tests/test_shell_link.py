"""Cover Windows shortcut and junction creation without child processes.

Real ``.lnk`` files and real NTFS junctions are created, but only below pytest
temporary directories. The Windows Shell COM interfaces and ``DeviceIoControl``
are real. An independent ``WScript.Shell`` read (through PowerShell, used only
by the test) confirms Explorer-visible properties. The registry, the ``PATH``
and the real Start Menu are never touched. Elevated runs and the Explorer
Properties dialog are out of scope; see ``tests/manual_smoke.md``.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Iterator

import pytest

from gupkg import cli
from gupkg import gupkg as gupkg_module
from gupkg import windows
from gupkg.shell_link import ShellLink, read_shell_link, write_shell_link

pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(os.name != "nt", reason="shortcuts and junctions need Windows"),
]

AWKWARD_TEXT = "it's \"quoted\" $var `tick` %TEMP% caf\u00e9 \u4e2d\u6587"


def _write_install_package(tmp_path: Path, shortcut_count: int) -> tuple[Path, Path]:
    """Build a one-package tree with *shortcut_count* shortcuts and return it with the Start Menu root."""
    version_dir = tmp_path / "ShortApp" / "1.0.0.local.1"
    (version_dir / "App").mkdir(parents=True)
    rows = "".join(
        f'\n[[shortcut]]\nname = "Short {index}"\n'
        f'targetPath = "C:\\\\Windows\\\\System32\\\\cmd.exe"\n'
        f'arguments = "/k echo {index}"\ndescription = "Shortcut {index}"\n'
        for index in range(shortcut_count)
    )
    (version_dir / "pkg.toml").write_text(
        f'name = "ShortApp"\nversion = "1.0.0"\nlocalVersion = 1\n{rows}', encoding="utf-8"
    )
    start_menu = tmp_path / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "opt"
    return version_dir, start_menu


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version_dir: Path
) -> int:
    """Run ``gupkg install`` for a user-scope package with every machine-wide effect redirected."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "UserProfile"))
    # The junction and PATH steps are outside this suite's scope and touch the host.
    monkeypatch.setattr(
        gupkg_module, "update_current_junction_if_needed", lambda *a, **k: True
    )
    from gupkg import components

    class _Done:
        ok, changed, warnings, errors = True, False, [], []

    monkeypatch.setattr(components, "ensure_bin_in_path", lambda *a, **k: _Done())
    return cli.main(["install", str(version_dir)])


def test_shortcut_fields_round_trip_exactly(tmp_path: Path) -> None:
    """Quotes, shell metacharacters, %VAR%, non-ASCII text and long paths survive a write and read."""
    link = ShellLink(
        target_path="%COMSPEC%",
        arguments=f"/k echo {AWKWARD_TEXT}",
        working_directory="C:\\" + "d" * 210,
        icon_location="C:\\Windows\\System32\\cmd.exe,3",
        description=AWKWARD_TEXT,
    )
    path = tmp_path / "round trip.lnk"

    write_shell_link(path, link)

    assert read_shell_link(path) == link


def test_shortcut_without_optional_fields_reads_back_empty(tmp_path: Path) -> None:
    """Optional fields that were never set read back as empty text, with no icon."""
    path = tmp_path / "plain.lnk"

    write_shell_link(path, ShellLink(target_path="C:\\Windows\\notepad.exe"))

    assert read_shell_link(path) == ShellLink(target_path="C:\\Windows\\notepad.exe")


def test_icon_index_splits_on_the_last_comma(tmp_path: Path) -> None:
    """An icon path containing a comma keeps it, and only the final comma starts the index."""
    path = tmp_path / "icon.lnk"

    write_shell_link(
        path,
        ShellLink(target_path="C:\\Windows\\notepad.exe", icon_location="C:\\a,b\\x.dll,7"),
    )

    assert read_shell_link(path).icon_location == "C:\\a,b\\x.dll,7"


def test_rewriting_a_shortcut_replaces_it(tmp_path: Path) -> None:
    """Writing over an existing shortcut leaves only the new fields."""
    path = tmp_path / "again.lnk"
    write_shell_link(path, ShellLink("C:\\old.exe", arguments="--old", description="old"))

    write_shell_link(path, ShellLink("C:\\new.exe"))

    assert read_shell_link(path) == ShellLink("C:\\new.exe")


def test_independent_wscript_reader_sees_the_declared_properties(tmp_path: Path) -> None:
    """Windows Script Host, the old Explorer-facing reader, sees the target, arguments, start-in, icon and comment."""
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        pytest.skip("powershell.exe is not available as the test oracle")
    path = tmp_path / "oracle.lnk"
    write_shell_link(
        path,
        ShellLink(
            target_path="C:\\Windows\\System32\\cmd.exe",
            arguments="/k echo 'hi'",
            working_directory="C:\\Windows",
            icon_location="C:\\Windows\\System32\\cmd.exe,0",
            description="caf\u00e9 comment",
        ),
    )
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
        "$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK_PATH);"
        "@{T=$l.TargetPath;A=$l.Arguments;W=$l.WorkingDirectory;"
        "I=$l.IconLocation;D=$l.Description} | ConvertTo-Json -Compress"
    )

    completed = subprocess.run(
        [str(powershell), "-NoProfile", "-Command", script],
        env={**os.environ, "LNK_PATH": str(path)},
        capture_output=True,
        encoding="utf-8",
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "T": "C:\\Windows\\System32\\cmd.exe",
        "A": "/k echo 'hi'",
        "W": "C:\\Windows",
        "I": "C:\\Windows\\System32\\cmd.exe,0",
        "D": "caf\u00e9 comment",
    }


def test_writing_into_a_missing_directory_fails_with_a_one_line_error_naming_the_path(
    tmp_path: Path,
) -> None:
    """A shortcut that cannot be written raises OSError whose one-line message names the shortcut."""
    path = tmp_path / "missing folder" / "x.lnk"

    with pytest.raises(OSError) as caught:
        windows.create_shortcut(path, "C:\\Windows\\notepad.exe")

    assert caught.value.filename == str(path)
    assert "\n" not in str(caught.value)


def test_unwritable_directory_is_reported_by_install_and_names_the_shortcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install reports a denied shortcut write as a failed install that names the shortcut."""
    version_dir, start_menu = _write_install_package(tmp_path, 1)
    start_menu.mkdir(parents=True)
    user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
    subprocess.run(
        ["icacls", str(start_menu), "/deny", f"{user}:(OI)(CI)(WD,AD)"],
        check=True,
        capture_output=True,
    )
    try:
        code = _install(monkeypatch, tmp_path, version_dir)
    finally:
        subprocess.run(
            ["icacls", str(start_menu), "/remove:d", user], check=True, capture_output=True
        )

    output = capsys.readouterr()
    assert code != 0
    assert "Short 0" in output.out + output.err


def test_install_creates_every_shortcut_without_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing a package with shortcuts succeeds when PowerShell cannot be found at all."""
    version_dir, start_menu = _write_install_package(tmp_path, 10)

    def no_powershell(name: str) -> str:
        raise OSError(f"{name} unavailable")

    monkeypatch.setattr(windows, "system_tool", no_powershell)
    monkeypatch.setenv("PATH", "")
    code = _install(monkeypatch, tmp_path, version_dir)

    assert code == 0
    links = sorted(start_menu.rglob("*.lnk"))
    assert len(links) == 10
    first = read_shell_link(links[0])
    assert first.target_path == "C:\\Windows\\System32\\cmd.exe"
    assert first.arguments.startswith("/k echo ")


def test_shortcuts_can_be_written_from_worker_threads(tmp_path: Path) -> None:
    """Shortcut writes work from threads other than the one that started the program."""
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            path = tmp_path / f"thread{index}.lnk"
            write_shell_link(path, ShellLink("C:\\Windows\\notepad.exe", description=str(index)))
            assert read_shell_link(path).description == str(index)
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


@pytest.fixture
def junction_dirs(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    """Provide an existing target directory with one file, and a not-yet-created junction path."""
    target = tmp_path / "target dir"
    target.mkdir()
    (target / "marker.txt").write_text("marker", encoding="utf-8")
    yield target, tmp_path / "link"


def test_junction_opens_the_target_directory(junction_dirs: tuple[Path, Path]) -> None:
    """A created junction is recognised as one and shows the target's contents."""
    target, link = junction_dirs

    windows.create_junction(link, target)

    assert windows.is_junction(link)
    assert windows.get_junction_target(link) == windows.normalize_path(str(target))
    assert (link / "marker.txt").read_text(encoding="utf-8") == "marker"


def test_junction_is_replaced_and_deleting_it_keeps_the_target(
    junction_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    """Re-creating a junction re-points it, and removing it does not delete the target files."""
    target, link = junction_dirs
    other = tmp_path / "other"
    other.mkdir()
    windows.create_junction(link, target)

    windows.create_junction(link, other)

    assert windows.get_junction_target(link) == windows.normalize_path(str(other))
    os.rmdir(link)
    assert (target / "marker.txt").exists()


def test_junction_refuses_to_replace_a_real_directory_and_a_missing_target(
    junction_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    """A real directory is never overwritten, and a missing target is refused with no leftovers."""
    target, link = junction_dirs
    link.mkdir()
    with pytest.raises(RuntimeError, match="not a junction"):
        windows.create_junction(link, target)
    link.rmdir()

    with pytest.raises(RuntimeError, match="does not exist"):
        windows.create_junction(link, tmp_path / "absent")
    assert not link.exists()


def test_junction_failure_leaves_no_directory_behind(
    junction_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    """When the junction cannot be created, the error names the paths and no empty directory remains."""
    target, _ = junction_dirs
    link = tmp_path / "no parent" / "link"

    with pytest.raises(RuntimeError, match="Cannot create junction"):
        windows.create_junction(link, target)

    assert not link.exists()
