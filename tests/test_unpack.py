"""Cover the unpack service: every kind, the shared options, and hostile archives.

Archives are built at test time: zip with ``zipfile``, 7z with the bundled 7-Zip, tarballs
with ``tarfile``, and a self-extractor by putting the bundled ``7z.exe`` in front of a 7z
archive (the layout of a real SFX). 7-Zip runs for real through ``FakeSystem``. The NSIS and
MSI kinds have no committed sample (no NSIS compiler is available offline): they are covered
with a process double that answers like 7-Zip, which checks the command line and the safety
rules; real vendor installers are in ``tests/manual_smoke.md``. Out of scope: downloading the
artifact and the manifest keys that choose the options (``test_manifest_format2``).
"""

from __future__ import annotations

import gzip
import os
import struct
import zipfile
from pathlib import Path

import pytest

from gupkg.extractors import find_7z
from gupkg.model.manifest import ExtractRule, ManifestError, NestedArchive, parse_manifest
from gupkg.services.unpack import Limits, UnpackError, UnpackPlan, unpack_artifact
from gupkg.services.unpack.extract import check_tree
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.archive_support import make_7z, make_tar, make_zip, sums_manifest, tree


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    """An empty folder where unpack destinations are made; a failed unpack must leave it empty."""
    folder = tmp_path / "stage"
    folder.mkdir()
    return folder


def unpack(system: FakeSystem, archive: Path, stage: Path, plan: UnpackPlan | None = None, **options):
    """Unpack *archive* into ``stage/App`` and return the report."""
    return unpack_artifact(system, archive, stage / "App", plan or UnpackPlan(), **options)


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


def test_a_zip_is_unpacked_with_its_kind_inferred_from_the_signature(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A zip with a misleading name is recognised by its first bytes and unpacked completely."""
    archive = make_zip(tmp_path / "download.bin", {"a.txt": "1", "dir/b.txt": "2", "empty/": ""})

    report = unpack(system, archive, stage)

    assert report.kind == "zip" and report.files == 2
    assert tree(stage / "App") == {"a.txt": "1", "dir/b.txt": "2"} and (stage / "App" / "empty").is_dir()


def test_a_7z_archive_is_unpacked(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A 7z archive made by the bundled 7-Zip unpacks to the same files, and its kind is `7z`."""
    archive = make_7z(system, tmp_path / "tool.7z", {"bin/tool.exe": "exe", "readme.txt": "hi"})

    report = unpack(system, archive, stage)

    assert report.kind == "7z" and tree(stage / "App") == {"bin/tool.exe": "exe", "readme.txt": "hi"}


@pytest.mark.parametrize(
    ("name", "mode"),
    [
        pytest.param("msys2.tar.xz", "w:xz", id="tar-xz"),
        pytest.param("tool.tar.gz", "w:gz", id="tar-gz"),
        pytest.param("tool.tgz", "w:gz", id="tgz"),
        pytest.param("tool.tar.bz2", "w:bz2", id="tar-bz2"),
        pytest.param("tool.tar", "w", id="plain-tar"),
    ],
)
def test_tarballs_are_decompressed_with_a_cap_then_unpacked_by_seven_zip(system: FakeSystem, tmp_path: Path, stage: Path, name: str, mode: str) -> None:
    """`.tar.xz`, `.tar.gz` and the other tarballs are decompressed (capped, in process) and then unpacked by 7-Zip (D39)."""
    archive = make_tar(tmp_path / name, [("msys64/usr/bin/bash.exe", b"bash", "file"), ("msys64/etc/profile", b"p", "file")], mode)

    report = unpack(system, archive, stage, UnpackPlan(strip=1))

    assert report.kind == "tar"
    assert tree(stage / "App") == {"usr/bin/bash.exe": "bash", "etc/profile": "p"}
    runs = [call[1] for call in system.process_log if call[1] == "x"]
    assert len(runs) == 1  # pass one (decompression) runs in process under a byte cap; pass two is 7-Zip
    assert not any(path.name.startswith(".") for path in stage.iterdir())


def test_a_compressed_file_that_is_not_a_tarball_is_kept_as_one_file(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A lone `.gz` of a text file unpacks to that file instead of failing."""
    source = tmp_path / "notes.txt.gz"
    source.write_bytes(gzip.compress(b"plain text"))

    report = unpack(system, source, stage)

    assert report.kind == "tar" and list((stage / "App").iterdir())[0].read_bytes() == b"plain text"


def test_a_self_extractor_is_detected_as_sfx_and_unpacked(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """An executable with a 7z archive appended (the layout of a real SFX) is recognised by 7-Zip's reading and unpacked."""
    archive = make_7z(system, tmp_path / "payload.7z", {"core/app.exe": "app", "setup.exe": "setup"})
    sfx = tmp_path / "Firefox Setup.exe"
    sfx.write_bytes(find_7z().read_bytes() + archive.read_bytes())

    report = unpack(system, sfx, stage, UnpackPlan(subdir="core", require=("app.exe",)))

    assert report.kind == "sfx" and tree(stage / "App") == {"app.exe": "app"}


def test_a_plain_executable_is_copied_as_it_is_under_its_published_name(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """An executable that contains no archive is the whole payload, copied under its safe published name."""
    program = tmp_path / "abc123.file"
    program.write_bytes(find_7z().read_bytes())

    report = unpack(system, program, stage, filename="Tool Setup.exe")

    assert report.kind == "exe" and (stage / "App" / "Tool Setup.exe").read_bytes() == program.read_bytes()


def test_a_data_file_is_copied_as_one_file(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """Content that is no archive and no executable is copied under its name (kind `file`)."""
    data = tmp_path / "cert.pem"
    data.write_text("-----BEGIN-----", encoding="utf-8")

    report = unpack(system, data, stage)

    assert report.kind == "file" and tree(stage / "App") == {"cert.pem": "-----BEGIN-----"}


def test_a_declared_kind_that_does_not_fit_the_file_is_an_error(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`kind = "zip"` on a 7z file fails rather than being guessed, and leaves nothing behind."""
    archive = make_7z(system, tmp_path / "tool.7z", {"a.txt": "1"})

    with pytest.raises(UnpackError, match="not a zip"):
        unpack(system, archive, stage, UnpackPlan(kind="zip"))

    assert list(stage.iterdir()) == []


@pytest.mark.parametrize(("kind", "listing_type", "flag"), [
    pytest.param("nsis", "Nsis", "-tNsis", id="nsis"),
    pytest.param("msi", "Msi", None, id="msi"),
])
def test_nsis_and_msi_run_the_bundled_seven_zip_and_never_the_installer(
    tmp_path: Path, stage: Path, kind: str, listing_type: str, flag: str | None
) -> None:
    """The installer is listed and unpacked by 7-Zip only; the NSIS format is forced, the files land in `App`.

    7-Zip is a process double here (no NSIS or MSI sample can be built offline), so this checks
    the command lines and the handling of the answer, not the decoding itself.
    """
    calls: list[tuple[str, ...]] = []

    def seven_zip(call: ProcessCall) -> ProcessResult:
        """Answer a listing, or unpack two files into the requested folder."""
        calls.append(call.argv)
        if "l" in call.argv:
            return ProcessResult(0, f"Path = x\nType = {listing_type}\n\n----------\nPath = app.exe\nSize = 3\n\nPath = lib/a.dll\nSize = 2\n")
        folder = Path(next(part for part in call.argv if part.startswith("-o"))[2:])
        (folder / "lib").mkdir(parents=True)
        (folder / "app.exe").write_text("exe", encoding="utf-8")
        (folder / "lib" / "a.dll").write_text("dl", encoding="utf-8")
        return ProcessResult(0)

    system = FakeSystem(tmp_path / "machine", processes={"7z": seven_zip}, real_7zip=False)
    installer = tmp_path / "Setup.bin"
    installer.write_bytes(b"MZ" + b"\0" * 100)

    report = unpack_artifact(system, installer, stage / "App", UnpackPlan(kind=kind))

    assert report.kind == kind and tree(stage / "App") == {"app.exe": "exe", "lib/a.dll": "dl"}
    assert all(Path(argv[0]).name == "7z.exe" for argv in calls)
    assert (flag in calls[0]) is (flag is not None)


def test_an_nsis_installer_that_7zip_reads_as_something_else_is_refused(tmp_path: Path, stage: Path) -> None:
    """`kind = "nsis"` on a file that 7-Zip reads as a plain executable fails before anything is extracted."""
    def seven_zip(call: ProcessCall) -> ProcessResult:
        """Say that the file is a plain PE."""
        return ProcessResult(0, "Path = x\nType = PE\n\n----------\nPath = .rsrc\nSize = 3\n")

    system = FakeSystem(tmp_path / "machine", processes={"7z": seven_zip}, real_7zip=False)
    installer = tmp_path / "Setup.exe"
    installer.write_bytes(b"MZ" + b"\0" * 100)

    with pytest.raises(UnpackError, match="not an NSIS installer"):
        unpack_artifact(system, installer, stage / "App", UnpackPlan(kind="nsis"))

    assert list(stage.iterdir()) == []


def test_a_manifest_that_asks_for_an_inno_kind_fails_validation() -> None:
    """There is no `inno` kind in 1.0 (decision D39): the manifest parser lists the kinds that exist."""
    with pytest.raises(ManifestError, match="kind.*must be one of zip, 7z, tar, sfx, nsis, msi, exe, file, git"):
        parse_manifest('format = 2\n[source]\ndownload_url = "https://example.org/gimp-setup.exe"\nkind = "inno"\n')


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------


def test_strip_removes_exactly_the_requested_wrapper_levels(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`strip = 2` removes two wrapper folders; `strip = 3` on a two-level archive fails and leaves nothing."""
    archive = make_zip(tmp_path / "a.zip", {"one/two/file.txt": "x", "one/two/dir/more.txt": "y"})

    unpack(system, archive, stage, UnpackPlan(strip=2))
    assert tree(stage / "App") == {"file.txt": "x", "dir/more.txt": "y"}

    with pytest.raises(UnpackError, match="strip = 3 needs 3 wrapper folder"):
        unpack_artifact(system, archive, stage / "Other", UnpackPlan(strip=3))
    assert sorted(path.name for path in stage.iterdir()) == ["App"]


def test_strip_auto_stops_when_the_root_has_more_than_one_entry(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`strip = "auto"` peels single wrapper folders and stops at the first level with two entries."""
    archive = make_zip(tmp_path / "a.zip", {"outer/inner/a.txt": "1", "outer/inner/b.txt": "2"})

    unpack(system, archive, stage, UnpackPlan(strip="auto"))

    assert tree(stage / "App") == {"a.txt": "1", "b.txt": "2"}


def test_strip_works_when_a_wrapper_folder_has_the_name_of_its_child(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`a/a/file.txt` with `strip = 1` gives `a/file.txt`: the move survives a name clash."""
    archive = make_zip(tmp_path / "a.zip", {"a/a/file.txt": "x"})

    unpack(system, archive, stage, UnpackPlan(strip=1))

    assert tree(stage / "App") == {"a/file.txt": "x"}


def test_subdir_keeps_only_that_subtree(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`subdir = "core"` keeps the contents of `core` and drops the installer files around it."""
    archive = make_zip(tmp_path / "a.zip", {"core/firefox.exe": "ff", "core/lib/x.dll": "d", "setup.exe": "s"})

    unpack(system, archive, stage, UnpackPlan(subdir="core"))

    assert tree(stage / "App") == {"firefox.exe": "ff", "lib/x.dll": "d"}


def test_a_missing_subdir_fails_and_leaves_nothing(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A `subdir` that the artifact does not have is an error."""
    archive = make_zip(tmp_path / "a.zip", {"a.txt": "1"})

    with pytest.raises(UnpackError, match="not a folder"):
        unpack(system, archive, stage, UnpackPlan(subdir="core"))

    assert list(stage.iterdir()) == []


def test_extract_rules_map_globs_and_work_for_a_7z_archive_too(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`[[source.extract]]` copies glob matches (a trailing `/` means the folder's contents); it is not limited to zip."""
    archive = make_7z(system, tmp_path / "t.7z", {"tixati-1.2/tixati.exe": "t", "tixati-1.2/lang/en.txt": "e", "junk/x.txt": "j"})
    rules = (ExtractRule("tixati-*/", ""), ExtractRule("junk/x.txt", "extra"))

    unpack(system, archive, stage, UnpackPlan(extract=rules))

    assert tree(stage / "App") == {"tixati.exe": "t", "lang/en.txt": "e", "extra/x.txt": "j"}


def test_an_extract_rule_that_matches_nothing_fails(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A glob with no match is reported by name instead of producing an empty `App`."""
    archive = make_zip(tmp_path / "a.zip", {"a.txt": "1"})

    with pytest.raises(UnpackError, match=r"matched nothing"):
        unpack(system, archive, stage, UnpackPlan(extract=(ExtractRule("nothing-*/", ""),)))

    assert list(stage.iterdir()) == []


def test_require_lists_every_missing_file_and_leaves_nothing(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`require` names all absent paths at once; the failed unpack leaves no `App` and no scratch folder."""
    archive = make_zip(tmp_path / "a.zip", {"bin/tool.exe": "x"})

    unpack(system, archive, stage, UnpackPlan(require=("bin/tool.exe", "bin/*.exe")))
    with pytest.raises(UnpackError, match=r"volta\.exe, shim\.exe"):
        unpack_artifact(system, archive, stage / "Second", UnpackPlan(require=("volta.exe", "shim.exe")))

    assert sorted(path.name for path in stage.iterdir()) == ["App"]


def test_a_nested_archive_replaces_the_installer_contents(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """With `replace = true` the inner archive's contents become the whole tree (obsidian: only `app-64.7z` matters)."""
    inner = make_7z(system, tmp_path / "app-64.7z", {"Obsidian.exe": "app", "resources/a.asar": "a"})
    outer = make_zip(tmp_path / "setup.zip", {"$PLUGINSDIR/app-64.7z": inner.read_bytes(), "uninstall.exe": "u", "$R0/junk": "j"})

    unpack(system, outer, stage, UnpackPlan(nested=(NestedArchive("$PLUGINSDIR/app-64.7z", "", True),)))

    assert tree(stage / "App") == {"Obsidian.exe": "app", "resources/a.asar": "a"}


def test_a_nested_archive_is_merged_without_overwriting_and_then_deleted(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """Without `replace` the inner files join the outer ones (pidgin: `gtk.zip`); the inner archive file is consumed."""
    gtk = make_zip(tmp_path / "gtk.zip", {"Gtk/bin/libgtk.dll": "gtk"})
    outer = make_zip(tmp_path / "pidgin.zip", {"pidgin.exe": "p", "$PLUGINSDIR/gtk.zip": gtk.read_bytes()})

    unpack(system, outer, stage, UnpackPlan(nested=(NestedArchive("$PLUGINSDIR/gtk.zip", "", False),)))

    assert tree(stage / "App") == {"pidgin.exe": "p", "Gtk/bin/libgtk.dll": "gtk"}


def test_a_nested_archive_never_overwrites_an_existing_file(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """An inner archive that would overwrite what the outer one unpacked fails the whole unpack."""
    inner = make_zip(tmp_path / "inner.zip", {"pidgin.exe": "evil"})
    outer = make_zip(tmp_path / "outer.zip", {"pidgin.exe": "p", "in.zip": inner.read_bytes()})

    with pytest.raises(UnpackError, match="would overwrite pidgin.exe"):
        unpack(system, outer, stage, UnpackPlan(nested=(NestedArchive("in.zip", "", False),)))

    assert list(stage.iterdir()) == []


def test_a_nested_path_that_is_not_in_the_artifact_fails(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A nested archive that is absent is an error naming its path."""
    outer = make_zip(tmp_path / "outer.zip", {"a.txt": "1"})

    with pytest.raises(UnpackError, match="'missing.7z' is not in the unpacked artifact"):
        unpack(system, outer, stage, UnpackPlan(nested=(NestedArchive("missing.7z", "", False),)))


# ---------------------------------------------------------------------------
# verify_manifest
# ---------------------------------------------------------------------------

RELEASE = {"gupkg/gupkg.py": "code", "gupkg/core.py": "core", "pkg.toml": "format = 2"}


def release_zip(path: Path, files: dict[str, str], manifest: dict[str, str] | None = None) -> Path:
    """Build a release-like zip whose `gupkg/MANIFEST.sha256` lists *manifest* (default: *files*)."""
    members = {name: text for name, text in files.items()}
    members["gupkg/MANIFEST.sha256"] = sums_manifest(manifest if manifest is not None else files)
    return make_zip(path, members)


def test_an_artifact_that_matches_its_manifest_unpacks(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """With `verify_manifest` set, a tree that matches its own manifest exactly is accepted."""
    archive = release_zip(tmp_path / "gupkg.zip", RELEASE)

    report = unpack(system, archive, stage, UnpackPlan(verify_manifest="gupkg/MANIFEST.sha256"))

    assert report.files == 4 and (stage / "App" / "gupkg" / "core.py").read_text() == "core"


@pytest.mark.parametrize(
    ("files", "message"),
    [
        pytest.param({**RELEASE, "extra.py": "x"}, "extra: extra.py", id="extra-file"),
        pytest.param({k: v for k, v in RELEASE.items() if k != "pkg.toml"}, "missing: pkg.toml", id="missing-file"),
        pytest.param({**RELEASE, "gupkg/core.py": "tampered"}, "modified: gupkg/core.py", id="modified-file"),
    ],
)
def test_an_extra_missing_or_modified_file_fails_the_unpack_and_leaves_nothing_staged(
    system: FakeSystem, tmp_path: Path, stage: Path, files: dict[str, str], message: str
) -> None:
    """Any difference from the manifest (written for `RELEASE`) fails the unpack, names the file, and leaves nothing staged."""
    archive = release_zip(tmp_path / "bad.zip", files, RELEASE)

    with pytest.raises(UnpackError, match=message):
        unpack(system, archive, stage, UnpackPlan(verify_manifest="gupkg/MANIFEST.sha256"))

    assert list(stage.iterdir()) == []


def test_a_missing_manifest_fails_the_unpack(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`verify_manifest` on an artifact without that file is an error, not a pass."""
    archive = make_zip(tmp_path / "a.zip", {"a.txt": "1"})

    with pytest.raises(UnpackError, match="no manifest at gupkg/MANIFEST.sha256"):
        unpack(system, archive, stage, UnpackPlan(verify_manifest="gupkg/MANIFEST.sha256"))

    assert list(stage.iterdir()) == []


# ---------------------------------------------------------------------------
# Hostile archives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("../evil.txt", id="parent-step"),
        pytest.param("a/../../evil.txt", id="parent-step-deep"),
        pytest.param("..\\evil.txt", id="parent-step-backslash"),
        pytest.param("/abs/evil.txt", id="absolute-posix"),
        pytest.param("\\abs\\evil.txt", id="absolute-backslash"),
        pytest.param("C:/Windows/evil.txt", id="drive-letter"),
        pytest.param("C:evil.txt", id="drive-relative"),
        pytest.param("//server/share/evil.txt", id="unc-path"),
        pytest.param("file.txt:hidden", id="alternate-data-stream"),
        pytest.param("con.txt", id="device-name-with-extension"),
        pytest.param("dir/NUL", id="device-name-in-folder"),
        pytest.param("aux", id="device-name"),
        pytest.param("COM1.log", id="com-port"),
        pytest.param("name.", id="trailing-dot"),
        pytest.param("name ", id="trailing-space"),
        pytest.param("bad<name>.txt", id="forbidden-character"),
    ],
)
def test_a_zip_with_an_unsafe_member_name_is_rejected_whole(system: FakeSystem, tmp_path: Path, stage: Path, name: str) -> None:
    """One hostile name rejects the archive before any file is written, and nothing is left, even outside `App`."""
    archive = make_zip(tmp_path / "hostile.zip", {"fine.txt": "ok", name: "evil"})

    with pytest.raises(UnpackError, match="unsafe member name"):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []
    assert not (tmp_path / "evil.txt").exists() and not (stage.parent / "evil.txt").exists()


def test_a_zip_symlink_entry_is_rejected(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A member with the symlink file mode is refused, however innocent its name."""
    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        info = zipfile.ZipInfo("link")
        info.external_attr = (0o120777 << 16)
        bundle.writestr(info, "../../outside")

    with pytest.raises(UnpackError, match="symbolic link"):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_zip_with_names_that_collide_on_a_case_insensitive_disk_is_rejected(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`Tool.exe` and `tool.EXE` are one file on Windows, so an archive that holds both is refused."""
    archive = make_zip(tmp_path / "case.zip", {"Tool.exe": "a", "tool.EXE": "b"})

    with pytest.raises(UnpackError, match="same name on Windows"):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_zip_with_the_same_file_name_twice_is_rejected(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A duplicate member cannot overwrite the first one: files are created exclusively."""
    archive = tmp_path / "dup.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("a.txt", "first")
        with pytest.warns(UserWarning, match="Duplicate name"):
            bundle.writestr("a.txt", "second")

    with pytest.raises(UnpackError):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_file_and_a_folder_with_the_same_name_are_rejected(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """`a` as a file and `a/b` below it cannot both exist."""
    archive = make_zip(tmp_path / "clash.zip", {"a": "file", "a/b.txt": "below"})

    with pytest.raises(UnpackError):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_zip_with_too_many_entries_is_rejected_before_extraction(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """An archive of many tiny files exceeds `max_entries` and is refused without writing a file."""
    archive = make_zip(tmp_path / "many.zip", {f"f{index}.txt": "x" for index in range(200)})

    with pytest.raises(UnpackError, match="at most 100 are allowed"):
        unpack(system, archive, stage, limits=Limits(max_entries=100))

    assert list(stage.iterdir()) == []


def test_a_zip_bomb_is_rejected_by_its_declared_size(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A tiny archive that declares gigabytes of zeros is refused by the size limit, with nothing extracted."""
    archive = make_zip(tmp_path / "bomb.zip", {"zeros.bin": b"\0" * (6 * 1024 * 1024)})
    assert archive.stat().st_size < 20_000

    with pytest.raises(UnpackError, match="more uncompressed data than the allowed size"):
        unpack(system, archive, stage, limits=Limits(max_total_bytes=1024 * 1024))

    assert list(stage.iterdir()) == []


def test_a_zip_whose_header_lies_about_the_size_is_stopped_by_the_bytes_actually_produced(
    system: FakeSystem, tmp_path: Path, stage: Path
) -> None:
    """A central directory that claims 10 bytes for a 3 MiB file cannot make the extractor write more than it declared."""
    archive = make_zip(tmp_path / "liar.zip", {"big.bin": b"\0" * (3 * 1024 * 1024)})
    data = bytearray(archive.read_bytes())
    central = data.index(b"PK\x01\x02")
    data[central + 24 : central + 28] = struct.pack("<I", 10)
    archive.write_bytes(bytes(data))

    with pytest.raises(UnpackError):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_zip_member_with_an_unsupported_compression_is_an_unpack_error(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A member using a compression method that Python cannot read fails as an UnpackError, not a stray NotImplementedError."""
    archive = make_zip(tmp_path / "odd.zip", {"a.txt": "x" * 100})
    data = bytearray(archive.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        data[data.index(signature) + offset : data.index(signature) + offset + 2] = struct.pack("<H", 99)
    archive.write_bytes(bytes(data))

    with pytest.raises(UnpackError, match="compression"):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_corrupt_zip_is_an_unpack_error(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A zip cut in half is reported as an UnpackError, not as a stray library exception."""
    good = make_zip(tmp_path / "good.zip", {"a.txt": "x" * 1000})
    broken = tmp_path / "broken.zip"
    broken.write_bytes(good.read_bytes()[:60])

    with pytest.raises(UnpackError):
        unpack(system, broken, stage, UnpackPlan(kind="zip"))

    assert list(stage.iterdir()) == []


@pytest.mark.parametrize(
    ("members", "message"),
    [
        pytest.param([("a/file.txt", b"x", "file"), ("a/link", b"", "symlink")], "contains a link", id="symlink"),
        pytest.param([("a/file.txt", b"x", "file"), ("a/hard", b"", "hardlink")], "contains a link", id="hardlink"),
        pytest.param([("a/file.txt", b"x", "file"), ("../evil.txt", b"x", "file")], "unsafe member name", id="parent-step"),
        pytest.param([("a/file.txt", b"x", "file"), ("/etc/evil", b"x", "file")], "unsafe member name", id="absolute"),
    ],
)
def test_a_hostile_tarball_is_rejected_by_its_table_of_contents(
    system: FakeSystem, tmp_path: Path, stage: Path, members: list[tuple[str, bytes, str]], message: str
) -> None:
    """Link entries and escaping names in a `.tar.xz` are refused after pass one and before anything is unpacked."""
    archive = make_tar(tmp_path / "hostile.tar.xz", members, "w:xz")

    with pytest.raises(UnpackError, match=message):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []
    assert not (tmp_path / "evil.txt").exists()


def test_an_encrypted_7z_is_rejected_instead_of_prompting(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A password-protected archive fails with a clear error; 7-Zip is never given a chance to wait for input."""
    archive = make_7z(system, tmp_path / "secret.7z", {"a.txt": "1"}, "-psecret", "-mhe=on")

    with pytest.raises(UnpackError):
        unpack(system, archive, stage)

    assert list(stage.iterdir()) == []


def test_a_truncated_7z_is_an_unpack_error(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """A damaged 7z archive fails with 7-Zip's reason and leaves nothing behind."""
    good = make_7z(system, tmp_path / "good.7z", {"a.txt": "x" * 5000})
    broken = tmp_path / "broken.7z"
    broken.write_bytes(good.read_bytes()[:40])

    with pytest.raises(UnpackError):
        unpack(system, broken, stage)

    assert list(stage.iterdir()) == []


def test_an_unpacked_tree_with_a_junction_is_rejected(system: FakeSystem, tmp_path: Path) -> None:
    """The final walk refuses a link or junction in a tree, whatever created it (defence in depth after 7-Zip)."""
    root = tmp_path / "tree"
    (root / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    system.create_junction(root / "link", outside)

    with pytest.raises(UnpackError, match="link or junction at link"):
        check_tree(root, Limits())


def test_the_destination_must_not_exist(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """Unpacking never merges into an existing `App`: the caller names a new folder."""
    archive = make_zip(tmp_path / "a.zip", {"a.txt": "1"})
    (stage / "App").mkdir()

    with pytest.raises(UnpackError, match="already exists"):
        unpack(system, archive, stage)


def test_a_failure_in_the_final_step_removes_the_scratch_folder_too(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """Even when every option passes and only the last check fails, the stage holds neither `App` nor a scratch folder."""
    archive = release_zip(tmp_path / "a.zip", RELEASE, {**RELEASE, "gupkg/core.py": "other"})

    with pytest.raises(UnpackError):
        unpack(system, archive, stage, UnpackPlan(strip=0, verify_manifest="gupkg/MANIFEST.sha256"))

    assert os.listdir(stage) == []
