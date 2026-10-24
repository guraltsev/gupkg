"""Cover archive bombs: the size limit is enforced while data is written, not after.

Declared sizes are only hints (a bzip2 listing has none, a gzip trailer can lie), so each test
builds a small archive that expands far past a small limit and checks that extraction stops at
the limit, fails with an ``UnpackError`` and leaves nothing behind. Real 7-Zip runs through
``FakeSystem``; the watcher that kills 7-Zip is exercised with a process double that has already
written too much. Out of scope: ordinary unpacking (``test_unpack``).
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest

from gupkg.model.manifest import NestedArchive
from gupkg.services.unpack import Limits, UnpackError, UnpackPlan, unpack_artifact
from gupkg.services.unpack.extract import decompress_capped
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.archive_support import make_zip

MIB = 1024 * 1024
SMALL = Limits(max_total_bytes=MIB, max_file_bytes=MIB)


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    """An empty folder where unpack destinations are made; a failed unpack must leave it empty."""
    folder = tmp_path / "stage"
    folder.mkdir()
    return folder


def zero_tar(data_size: int) -> bytes:
    """Return an uncompressed tar holding one file of *data_size* zero bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("big.bin")
        info.size = data_size
        archive.addfile(info, io.BytesIO(bytes(data_size)))
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("compress", "name"),
    [pytest.param(bz2.compress, "bomb.tar.bz2", id="bzip2"), pytest.param(gzip.compress, "bomb.tar.gz", id="gzip")],
)
def test_a_compressed_tarball_bomb_stops_at_the_limit_and_leaves_nothing(
    system: FakeSystem, tmp_path: Path, stage: Path, compress, name: str
) -> None:
    """A 20 MiB tarball of zeros in a few kilobytes is refused at 1 MiB, though bzip2's listing declares no size."""
    archive = tmp_path / name
    archive.write_bytes(compress(zero_tar(20 * MIB)))
    assert archive.stat().st_size < 100_000

    with pytest.raises(UnpackError):
        unpack_artifact(system, archive, stage / "App", UnpackPlan(), limits=SMALL)

    assert list(stage.iterdir()) == []


def test_a_gzip_with_a_lying_length_trailer_is_stopped_while_it_expands(tmp_path: Path) -> None:
    """The gzip trailer claims 10 bytes, but the data is 10 MiB: the capped decompressor still stops at the cap."""
    data = bytearray(gzip.compress(bytes(10 * MIB)))
    data[-4:] = struct.pack("<I", 10)
    source = tmp_path / "liar.gz"
    source.write_bytes(bytes(data))
    destination = tmp_path / "out.bin"
    written: list[int | None] = []
    real = Path.unlink

    def spy(self: Path, *args, **kwargs):
        """Record the size of the partial output at the moment it is deleted."""
        written.append(self.stat().st_size if self.exists() else None)
        return real(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "unlink", spy)
        with pytest.raises(UnpackError, match="expands past"):
            decompress_capped(source, destination, MIB)

    assert written and written[0] <= MIB and not destination.exists()


def test_the_capped_decompressor_reads_xz_and_refuses_other_files(tmp_path: Path) -> None:
    """Gzip, bzip2 and xz are the supported wrappers; anything else is refused by name."""
    good = tmp_path / "ok.xz"
    good.write_bytes(lzma.compress(b"hello"))
    other = tmp_path / "other.bin"
    other.write_bytes(b"not compressed")

    assert decompress_capped(good, tmp_path / "ok.out", MIB) == 5
    with pytest.raises(UnpackError, match="not a gzip, bzip2 or xz"):
        decompress_capped(other, tmp_path / "other.out", MIB)


def test_a_seven_zip_that_writes_past_the_limit_is_killed_and_cleaned_up(tmp_path: Path, stage: Path) -> None:
    """The folder watcher sees 3 MiB in a 1 MiB budget while 7-Zip runs and stops it; the stage ends empty."""

    def seven_zip(call: ProcessCall) -> ProcessResult:
        """List a small archive; on extraction write 3 MiB as a runaway 7-Zip would."""
        if "l" in call.argv:
            return ProcessResult(0, "Path = x\nType = 7z\n\n----------\nPath = a.bin\nSize = 10\n")
        folder = Path(next(part for part in call.argv if part.startswith("-o"))[2:])
        (folder / "a.bin").write_bytes(bytes(3 * MIB))
        return ProcessResult(0)

    system = FakeSystem(tmp_path / "machine", processes={"7z": seven_zip}, real_7zip=False)
    archive = tmp_path / "x.7z"
    archive.write_bytes(b"7z\xbc\xaf\x27\x1c" + bytes(50))

    with pytest.raises(UnpackError, match="was stopped"):
        unpack_artifact(system, archive, stage / "App", UnpackPlan(), limits=SMALL)

    assert list(stage.iterdir()) == []


def test_limits_are_cumulative_across_nested_archives(system: FakeSystem, tmp_path: Path, stage: Path) -> None:
    """Two inner archives that each fit in the limit, but not together, fail: a nested archive does not start fresh."""
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("zeros.bin", bytes(800 * 1024))
    outer = make_zip(tmp_path / "outer.zip", {"one.zip": inner.read_bytes(), "two.zip": inner.read_bytes()})
    plan = UnpackPlan(nested=(NestedArchive("one.zip", "a", False), NestedArchive("two.zip", "b", False)))

    with pytest.raises(UnpackError, match="allowed size"):
        unpack_artifact(system, outer, stage / "App", plan, limits=SMALL)

    assert list(stage.iterdir()) == []
    # Either archive alone is fine.
    one = UnpackPlan(nested=(NestedArchive("one.zip", "a", False),))
    unpack_artifact(system, outer, stage / "Fine", one, limits=SMALL)


def test_the_default_entry_limit_is_two_hundred_thousand() -> None:
    """The default allows a large distribution (MSYS2) but not millions of entries."""
    assert Limits().max_entries == 200_000
