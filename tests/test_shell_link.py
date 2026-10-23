"""Cover the Windows-specific limits of shortcut and junction creation.

Real ``.lnk`` files and real NTFS junctions are created through the real
``WindowsSystem``, but only below pytest temporary directories. The Windows
Shell COM interfaces and ``DeviceIoControl`` are real. An independent
``WScript.Shell`` read (through PowerShell, used only by the test) confirms
Explorer-visible properties. The registry, the ``PATH`` and the real Start Menu
are never touched: install runs use packages without environment or ``PATH``
rows, with ``APPDATA`` and ``USERPROFILE`` pointed at a temporary home. What
every ``System`` shares (round trips, replacement, removal) is covered by
``tests/test_system_contract.py``. Elevated runs and the Explorer Properties
dialog are out of scope; see ``tests/manual_smoke.md``.
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
from gupkg.system import windows
from gupkg.system.port import ShortcutSpec
from gupkg.system.windows import WindowsSystem

pytestmark = [
    pytest.mark.windows_integration,
    pytest.mark.skipif(os.name != "nt", reason="shortcuts and junctions need Windows"),
]

SYSTEM = WindowsSystem()

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
    """Run ``gupkg install`` for a user-scope package with the Start Menu and profile redirected below *tmp_path*."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "UserProfile"))
    return cli.main(["--scope", "user", "install", str(version_dir)])


def test_shortcut_fields_round_trip_exactly(tmp_path: Path) -> None:
    """Quotes, shell metacharacters, %VAR%, non-ASCII text and long paths survive a write and read."""
    link = ShortcutSpec(
        target_path="%COMSPEC%",
        arguments=f"/k echo {AWKWARD_TEXT}",
        working_directory="C:\\" + "d" * 210,
        icon_location="C:\\Windows\\System32\\cmd.exe,3",
        description=AWKWARD_TEXT,
    )
    path = tmp_path / "round trip.lnk"

    SYSTEM.write_shortcut(path, link)

    assert SYSTEM.read_shortcut(path) == link


def test_target_and_icon_paths_over_two_hundred_characters_round_trip(
    tmp_path: Path,
) -> None:
    """Target and icon paths of 259 and 258 characters, near the Windows limit, come back unchanged."""
    link = ShortcutSpec(
        target_path="C:\\" + "t" * 256,
        icon_location="C:\\" + "i" * 255 + ",4",
        working_directory="C:\\" + "w" * 257,
    )
    path = tmp_path / "long.lnk"

    SYSTEM.write_shortcut(path, link)

    assert SYSTEM.read_shortcut(path) == link


def test_target_longer_than_windows_allows_raises_an_error_naming_the_path(
    tmp_path: Path,
) -> None:
    """A target over 259 characters is refused with an OSError that names the shortcut path."""
    path = tmp_path / "toolong.lnk"

    with pytest.raises(OSError) as caught:
        SYSTEM.write_shortcut(path, ShortcutSpec(target_path="C:\\" + "t" * 300))

    assert caught.value.filename == str(path)
    assert "\n" not in str(caught.value)
    assert not path.exists()


def test_reading_a_corrupt_lnk_raises_an_error_naming_the_path(tmp_path: Path) -> None:
    """A file that is not a shortcut raises OSError naming the file, so callers can report it."""
    path = tmp_path / "corrupt.lnk"
    path.write_text("garbage", encoding="utf-8")

    with pytest.raises(OSError) as caught:
        SYSTEM.read_shortcut(path)

    assert caught.value.filename == str(path)
    assert "\n" not in str(caught.value)


def test_shortcuts_work_on_a_thread_already_in_the_multithreaded_apartment(
    tmp_path: Path,
) -> None:
    """A thread that already initialised COM as multithreaded can still write and read links."""
    import ctypes

    results: list[ShortcutSpec] = []
    errors: list[BaseException] = []

    def work() -> None:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        try:
            path = tmp_path / "mta.lnk"
            SYSTEM.write_shortcut(path, ShortcutSpec("C:\\Windows\\notepad.exe"))
            results.append(SYSTEM.read_shortcut(path))
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            errors.append(exc)
        finally:
            ole32.CoUninitialize()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    assert errors == []
    assert results == [ShortcutSpec("C:\\Windows\\notepad.exe")]


def test_shortcut_without_optional_fields_reads_back_empty(tmp_path: Path) -> None:
    """Optional fields that were never set read back as empty text, with no icon."""
    path = tmp_path / "plain.lnk"

    SYSTEM.write_shortcut(path, ShortcutSpec(target_path="C:\\Windows\\notepad.exe"))

    assert SYSTEM.read_shortcut(path) == ShortcutSpec(target_path="C:\\Windows\\notepad.exe")


def test_icon_index_splits_on_the_last_comma(tmp_path: Path) -> None:
    """An icon path containing a comma keeps it, and only the final comma starts the index."""
    path = tmp_path / "icon.lnk"

    SYSTEM.write_shortcut(
        path,
        ShortcutSpec(target_path="C:\\Windows\\notepad.exe", icon_location="C:\\a,b\\x.dll,7"),
    )

    assert SYSTEM.read_shortcut(path).icon_location == "C:\\a,b\\x.dll,7"


def test_rewriting_a_shortcut_replaces_it(tmp_path: Path) -> None:
    """Writing over an existing shortcut leaves only the new fields."""
    path = tmp_path / "again.lnk"
    SYSTEM.write_shortcut(path, ShortcutSpec("C:\\old.exe", arguments="--old", description="old"))

    SYSTEM.write_shortcut(path, ShortcutSpec("C:\\new.exe"))

    assert SYSTEM.read_shortcut(path) == ShortcutSpec("C:\\new.exe")


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
    SYSTEM.write_shortcut(
        path,
        ShortcutSpec(
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

    captured = capsys.readouterr()
    lines = [
        line for line in (captured.out + captured.err).splitlines() if "Short 0" in line
    ]
    assert code != 0
    assert lines, "the failure must name the shortcut"
    assert any("Short 0.lnk" in line and "denied" in line.lower() for line in lines)


def test_install_creates_every_shortcut_without_powershell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing a package with shortcuts succeeds when PowerShell cannot be found at all."""
    version_dir, start_menu = _write_install_package(tmp_path, 10)

    def no_powershell(name: str) -> str:
        raise OSError(f"{name} unavailable")

    monkeypatch.setattr(windows, "_system_tool", no_powershell)
    monkeypatch.setenv("PATH", "")
    code = _install(monkeypatch, tmp_path, version_dir)

    assert code == 0
    links = sorted(start_menu.rglob("*.lnk"))
    assert len(links) == 10
    first = SYSTEM.read_shortcut(links[0])
    assert first.target_path == "C:\\Windows\\System32\\cmd.exe"
    assert first.arguments.startswith("/k echo ")


def test_shortcuts_can_be_written_from_worker_threads(tmp_path: Path) -> None:
    """Shortcut writes work from threads other than the one that started the program."""
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            path = tmp_path / f"thread{index}.lnk"
            SYSTEM.write_shortcut(path, ShortcutSpec("C:\\Windows\\notepad.exe", description=str(index)))
            assert SYSTEM.read_shortcut(path).description == str(index)
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


def test_junction_to_a_missing_target_leaves_no_directory_behind(
    junction_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    """A junction to a missing target is refused and no empty directory remains at the link path."""
    _, link = junction_dirs

    with pytest.raises(RuntimeError, match="does not exist"):
        SYSTEM.create_junction(link, tmp_path / "absent")

    assert not link.exists()


def test_junction_accepts_a_long_path_prefixed_target_and_refuses_network_targets(
    junction_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    r"""A ``\\?\``-prefixed target works like a plain one; a UNC target is refused with no leftovers."""
    target, link = junction_dirs
    prefixed = Path("\\\\?\\" + str(target))

    SYSTEM.create_junction(link, prefixed)

    assert (link / "marker.txt").read_text(encoding="utf-8") == "marker"
    SYSTEM.remove_link(link)

    with pytest.raises(RuntimeError, match="network"):
        SYSTEM.create_junction(link, Path("\\\\localhost\\C$\\Windows"))
    assert not link.exists()


def test_junction_failure_leaves_no_directory_behind(
    junction_dirs: tuple[Path, Path], tmp_path: Path
) -> None:
    """When the junction cannot be created, the error names the paths and no empty directory remains."""
    target, _ = junction_dirs
    link = tmp_path / "no parent" / "link"

    with pytest.raises(RuntimeError, match="Cannot create junction"):
        SYSTEM.create_junction(link, target)

    assert not link.exists()
