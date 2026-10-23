"""Cover each guard of the unpack service on its own, so removing any single one fails a test.

The folder watcher (entry count, one file too large, total too large, links not followed), the final tree
walk (one file, the total), the listing check (one entry, the total, the entry count), the zip header checks
(one file, the total), the budget handed to nested archives, and the xz memory limit. Real folders and
archives; 7-Zip is a process double where the listing must say something specific. Out of scope: bombs end to
end (``test_unpack_bombs``) and ordinary unpacking (``test_unpack``).
"""

from __future__ import annotations

import lzma
import struct
import zlib
from pathlib import Path

import pytest

from gupkg.model.manifest import NestedArchive
from gupkg.services import unpack as unpack_module
from gupkg.services.unpack import Limits, UnpackError, UnpackPlan, unpack_artifact
from gupkg.services.unpack.extract import XZ_MEMORY_LIMIT, _FolderWatch, check_tree, decompress_capped
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.archive_support import make_zip

KIB = 1024


def files(folder: Path, sizes: list[int]) -> None:
    """Create files of the given sizes in *folder*."""
    folder.mkdir(parents=True, exist_ok=True)
    for index, size in enumerate(sizes):
        (folder / f"f{index}.bin").write_bytes(bytes(size))


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    """An empty folder where unpack destinations are made; a failed unpack must leave it empty."""
    folder = tmp_path / "stage"
    folder.mkdir()
    return folder


# -- the folder watcher -------------------------------------------------------------------------


def test_the_watcher_stops_a_folder_with_too_many_entries(tmp_path: Path) -> None:
    """Five tiny files exceed an entry limit of three, though their size is nothing."""
    files(tmp_path / "w", [1] * 5)

    reason = _FolderWatch(tmp_path / "w", max_bytes=10**9, max_entries=3, max_file=10**9)()

    assert reason == "the archive has more entries than allowed"


def test_the_watcher_stops_one_file_that_is_too_large_even_when_the_total_would_be_fine(tmp_path: Path) -> None:
    """A 3 KiB file passes a 1 MiB total but not a 2 KiB per-file limit."""
    files(tmp_path / "w", [3 * KIB])

    reason = _FolderWatch(tmp_path / "w", max_bytes=MIB_TEN, max_entries=100, max_file=2 * KIB)()

    assert reason == "a file is larger than the allowed size"


def test_the_watcher_stops_a_total_that_is_too_large_though_every_file_is_small(tmp_path: Path) -> None:
    """Four 1 KiB files pass a 2 KiB per-file limit but not a 3 KiB total."""
    files(tmp_path / "w", [KIB] * 4)

    reason = _FolderWatch(tmp_path / "w", max_bytes=3 * KIB, max_entries=100, max_file=2 * KIB)()

    assert reason == "the archive expands past the allowed size"


def test_the_watcher_is_quiet_within_the_limits(tmp_path: Path) -> None:
    """A folder inside every limit gives no reason."""
    files(tmp_path / "w", [KIB] * 2)

    assert _FolderWatch(tmp_path / "w", max_bytes=10 * KIB, max_entries=10, max_file=2 * KIB)() is None


MIB_TEN = 10 * 1024 * 1024


@pytest.mark.windows_integration
def test_the_watcher_does_not_follow_junctions_and_cannot_loop_on_a_cycle(tmp_path: Path, system: FakeSystem) -> None:
    """A junction back to the folder (a cycle) and a junction to a big folder outside are neither entered nor counted."""
    root = tmp_path / "w"
    files(root, [10])
    outside = tmp_path / "outside"
    files(outside, [10 * KIB, 10 * KIB])
    system.create_junction(root / "loop", root)
    system.create_junction(root / "elsewhere", outside)

    reason = _FolderWatch(root, max_bytes=100, max_entries=50, max_file=1000)()

    assert reason is None


# -- the final walk -------------------------------------------------------------------------------


def test_the_final_walk_refuses_one_file_over_the_per_file_limit(tmp_path: Path) -> None:
    """A single 3 KiB file fails a 2 KiB per-file limit although the total limit is far away."""
    files(tmp_path / "t", [3 * KIB])

    with pytest.raises(UnpackError, match="larger than the allowed size"):
        check_tree(tmp_path / "t", Limits(max_total_bytes=MIB_TEN, max_file_bytes=2 * KIB))


def test_the_final_walk_refuses_a_total_over_the_limit_when_every_file_is_small(tmp_path: Path) -> None:
    """Four 1 KiB files fail a 3 KiB total limit although each is under the per-file limit."""
    files(tmp_path / "t", [KIB] * 4)

    with pytest.raises(UnpackError, match="larger than the allowed size"):
        check_tree(tmp_path / "t", Limits(max_total_bytes=3 * KIB, max_file_bytes=2 * KIB))


def test_the_final_walk_refuses_too_many_entries(tmp_path: Path) -> None:
    """Five files fail an entry limit of three."""
    files(tmp_path / "t", [1] * 5)

    with pytest.raises(UnpackError, match="more than 3 entries"):
        check_tree(tmp_path / "t", Limits(max_entries=3))


# -- the table-of-contents check ------------------------------------------------------------------


def seven_zip_listing(tmp_path: Path, entries: list[int]) -> FakeSystem:
    """Return a FakeSystem whose 7-Zip lists one file per size in *entries* and extracts nothing big."""

    def seven_zip(call: ProcessCall) -> ProcessResult:
        """List the archive, or extract nothing."""
        if "l" in call.argv:
            body = "".join(f"Path = f{index}.bin\nSize = {size}\n\n" for index, size in enumerate(entries))
            return ProcessResult(0, f"Path = x\nType = 7z\n\n----------\n{body}")
        return ProcessResult(0)

    return FakeSystem(tmp_path / "machine", processes={"7z": seven_zip}, real_7zip=False)


@pytest.mark.parametrize(
    ("sizes", "limits", "message"),
    [
        pytest.param([3 * KIB], Limits(max_total_bytes=MIB_TEN, max_file_bytes=2 * KIB), "larger than the allowed size", id="one-entry-too-large"),
        pytest.param([KIB] * 4, Limits(max_total_bytes=3 * KIB, max_file_bytes=2 * KIB), "more uncompressed data", id="total-too-large"),
        pytest.param([1] * 5, Limits(max_entries=3), "at most 3 are allowed", id="too-many-entries"),
    ],
)
def test_the_listing_is_judged_before_anything_is_extracted(tmp_path: Path, stage: Path, sizes: list[int], limits: Limits, message: str) -> None:
    """Each declared limit refuses on its own, before 7-Zip extracts a byte."""
    system = seven_zip_listing(tmp_path, sizes)
    archive = tmp_path / "x.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + bytes(50))

    with pytest.raises(UnpackError, match=message):
        unpack_artifact(system, archive, stage / "App", UnpackPlan(), limits=limits)

    assert not any("x" in call and "-o" in " ".join(call) for call in system.process_log if "l" not in call)
    assert list(stage.iterdir()) == []


# -- zip header checks ----------------------------------------------------------------------------


def test_a_zip_member_larger_than_the_per_file_limit_is_refused_though_the_total_fits(tmp_path: Path, stage: Path, system: FakeSystem) -> None:
    """One 3 KiB member fails a 2 KiB per-file limit with a roomy total."""
    archive = make_zip(tmp_path / "a.zip", {"big.bin": bytes(3 * KIB)})

    with pytest.raises(UnpackError, match="more uncompressed data"):
        unpack_artifact(system, archive, stage / "App", UnpackPlan(), limits=Limits(max_total_bytes=MIB_TEN, max_file_bytes=2 * KIB))

    assert list(stage.iterdir()) == []


def test_zip_members_that_are_small_but_together_too_large_are_refused(tmp_path: Path, stage: Path, system: FakeSystem) -> None:
    """Four 1 KiB members fail a 3 KiB total although each fits the per-file limit."""
    archive = make_zip(tmp_path / "a.zip", {f"f{index}.bin": bytes(KIB) for index in range(4)})

    with pytest.raises(UnpackError, match="more uncompressed data"):
        unpack_artifact(system, archive, stage / "App", UnpackPlan(), limits=Limits(max_total_bytes=3 * KIB, max_file_bytes=2 * KIB))

    assert list(stage.iterdir()) == []


# -- the budget handed to nested archives ---------------------------------------------------------


def test_each_nested_archive_receives_the_budget_with_what_earlier_ones_used(
    system: FakeSystem, tmp_path: Path, stage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second nested extraction starts with the first one's bytes and entries already charged."""
    inner = make_zip(tmp_path / "inner.zip", {"zeros.bin": bytes(100 * KIB)})
    outer = make_zip(tmp_path / "outer.zip", {"one.zip": inner.read_bytes(), "two.zip": inner.read_bytes()})
    seen: list[tuple[int, int]] = []
    real = unpack_module.extract_archive

    def spy(system_, archive, kind, destination, budget, seven_zip=None):
        """Record what the budget already holds, then extract."""
        seen.append((budget.used_bytes, budget.used_entries))
        return real(system_, archive, kind, destination, budget, seven_zip)

    monkeypatch.setattr(unpack_module, "extract_archive", spy)
    plan = UnpackPlan(nested=(NestedArchive("one.zip", "a", False), NestedArchive("two.zip", "b", False)))

    unpack_artifact(system, outer, stage / "App", plan)

    outer_bytes = seen[0][0]
    assert outer_bytes == 0 and seen[1][0] > 0
    assert seen[2][0] >= seen[1][0] + 100 * KIB and seen[2][1] > seen[1][1]


# -- the xz memory limit --------------------------------------------------------------------------


def xz_asking_for_a_huge_dictionary() -> bytes:
    """Return a tiny valid xz stream whose LZMA2 header asks for a 4 GiB dictionary (the block header CRC is fixed)."""
    data = bytearray(lzma.compress(b"hello", format=lzma.FORMAT_XZ))
    header_size = (data[12] + 1) * 4
    header = bytearray(data[12 : 12 + header_size - 4])
    position = header.index(b"\x21\x01") + 2
    header[position] = 40  # dictionary size code 40: 4 GiB - 1
    data[12 : 12 + header_size - 4] = header
    data[12 + header_size - 4 : 12 + header_size] = struct.pack("<I", zlib.crc32(bytes(header)))
    return bytes(data)


def test_an_xz_file_that_needs_more_memory_than_the_limit_fails_cleanly(tmp_path: Path) -> None:
    """A 4 GiB dictionary request is refused by the decoder's memory limit before anything is allocated or written."""
    source = tmp_path / "huge.xz"
    source.write_bytes(xz_asking_for_a_huge_dictionary())

    with pytest.raises(UnpackError, match="cannot be decompressed"):
        decompress_capped(source, tmp_path / "out.bin", 10 * KIB)

    assert not (tmp_path / "out.bin").exists() and XZ_MEMORY_LIMIT == 256 * 1024 * 1024


def test_an_ordinary_xz_still_decompresses_and_a_cut_short_one_fails(tmp_path: Path) -> None:
    """The memory limit does not disturb a normal file; a truncated stream is an UnpackError."""
    good = tmp_path / "ok.xz"
    good.write_bytes(lzma.compress(b"hello world" * 1000))
    cut = tmp_path / "cut.xz"
    cut.write_bytes(good.read_bytes()[:-20])

    assert decompress_capped(good, tmp_path / "ok.out", MIB_TEN) == 11_000
    with pytest.raises(UnpackError, match="cannot be decompressed"):
        decompress_capped(cut, tmp_path / "cut.out", MIB_TEN)
    assert not (tmp_path / "cut.out").exists()
