"""Cover the copy of a distribution folder into a root (design 21.4, D17, D40).

Behavior covered: the folder is copied whole into ``<root>\\<name>\\<version>``; the copies carry no Mark of the Web
(``Zone.Identifier``) and the result says whether the source was marked; an existing destination is never changed
(``identical`` when it holds the same files, ``diverged`` otherwise); a source with a link or junction, a missing
``pkg.toml`` or an unsafe name is refused and nothing is written; an interrupted layout leaves no work folder.

Also: a name that Windows cannot hold (reserved device names, trailing dots) is refused up front; a directory or file
swapped for a junction between the check and the use is never copied; a refused copy leaves the root as it was; and
the package's ``modes`` are checked on the staged copy before anything is moved into the root.

Bounds (issue 028): a source with too many entries, bytes, folder levels or a huge ``pkg.toml`` is refused; a junction at
``root\\.gupkg\\work``, ``root\\<name>`` or ``root\\<name>\\<version>`` is refused; a file that vanishes while the source is read is
a ``DistributionRefused``; and the identity checks (open handle against the path, folder before and after its listing) are
exercised by lying about the identity.

Real: the copy on real folders and real NTFS alternate data streams below pytest's temporary folder (the
streams need NTFS, so the marked-source tests are ``windows_integration``), and a real junction. Out of scope:
the engine's receipt, which records ``source_marked`` (issue 028).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gupkg import distcopy
from gupkg.catalog.local import tree_hash
from gupkg.distcopy import COPIED, DIVERGED, IDENTICAL, DistributionRefused, copy_distribution
from gupkg.model.scope import ModeRefused, make_scope
from gupkg.model.settings import default_layer, merge_layers
from gupkg.system.port import Mode
from gupkg.system.fake import FakeSystem
from tests.catalog_support import version_text
from tests.mode_support import pin_no_ambient_elevation, tree_snapshot  # noqa: F401


EXTENDED = "\\\\?\\"  # the extended-length path prefix


def distribution(base: Path, name: str = "tool", version: str = "1.0.0") -> Path:
    """Write ``<base>/<name>/<version>`` with a manifest, an ``App`` folder with a file and an empty folder."""
    folder = base / name / version
    (folder / "App" / "bin").mkdir(parents=True)
    (folder / "Icons").mkdir()
    (folder / "pkg.toml").write_text("format = 2\n", encoding="utf-8")
    (folder / "App" / "bin" / "tool.exe").write_bytes(b"MZ-tool")
    return folder


def mark(path: Path) -> None:
    """Give *path* a Mark of the Web, as Explorer does for a file extracted from a downloaded ZIP."""
    with open(f"{path}:Zone.Identifier", "w", encoding="ascii") as stream:
        stream.write("[ZoneTransfer]\nZoneId=3\n")


def is_marked(path: Path) -> bool:
    """Return whether *path* has a ``Zone.Identifier`` stream."""
    try:
        with open(f"{path}:Zone.Identifier", "rb"):
            return True
    except OSError:
        return False


def test_the_folder_is_copied_whole_into_the_root(tmp_path: Path) -> None:
    """Files, nested folders and empty folders arrive at ``<root>\\<name>\\<version>`` with equal content."""
    source = distribution(tmp_path / "downloads")
    root = tmp_path / "opt"

    result = copy_distribution(source, root)

    assert (result.status, result.destination) == (COPIED, root / "tool" / "1.0.0")
    assert tree_hash(result.destination) == tree_hash(source)
    assert (result.destination / "Icons").is_dir()
    assert result.files == 2
    assert not (root / ".gupkg" / "work").exists() or not any((root / ".gupkg" / "work").iterdir())


@pytest.mark.windows_integration
def test_the_copies_carry_no_mark_of_the_web_and_the_result_says_the_source_was_marked(tmp_path: Path) -> None:
    """A source whose files Explorer marked is copied without any ``Zone.Identifier``, and ``source_marked`` is set (D40)."""
    source = distribution(tmp_path / "downloads")
    for path in source.rglob("*"):
        if path.is_file():
            mark(path)
    assert is_marked(source / "pkg.toml")

    result = copy_distribution(source, tmp_path / "opt")

    assert result.source_marked is True
    copies = [path for path in result.destination.rglob("*") if path.is_file()]
    assert copies and not any(is_marked(path) for path in copies)


@pytest.mark.windows_integration
def test_an_unmarked_source_is_reported_unmarked(tmp_path: Path) -> None:
    """A source with no Mark of the Web (for example from ``tar -xf``) is reported as not marked."""
    source = distribution(tmp_path / "downloads")

    assert copy_distribution(source, tmp_path / "opt").source_marked is False


@pytest.mark.windows_integration
def test_one_marked_file_is_enough_to_report_the_source_as_marked(tmp_path: Path) -> None:
    """The receipt must say so when any file of the source was marked, not only the manifest."""
    source = distribution(tmp_path / "downloads")
    mark(source / "App" / "bin" / "tool.exe")

    assert copy_distribution(source, tmp_path / "opt").source_marked is True


def test_an_identical_existing_copy_is_reported_and_left_alone(tmp_path: Path) -> None:
    """Copying the same distribution twice is ``identical``; a file added later (an ``App`` populated) does not matter."""
    source = distribution(tmp_path / "downloads")
    root = tmp_path / "opt"
    first = copy_distribution(source, root)
    (first.destination / "App" / "extra.txt").write_text("populated later", encoding="utf-8")

    second = copy_distribution(source, root)

    assert second.status == IDENTICAL
    assert (first.destination / "App" / "extra.txt").read_text(encoding="utf-8") == "populated later"


def test_a_differing_existing_copy_is_diverged_and_never_overwritten(tmp_path: Path) -> None:
    """When ``<root>\\<name>\\<version>`` differs from the source it is reported ``diverged`` and keeps its content."""
    source = distribution(tmp_path / "downloads")
    root = tmp_path / "opt"
    destination = root / "tool" / "1.0.0"
    destination.mkdir(parents=True)
    (destination / "pkg.toml").write_text("format = 2\n# edited by hand\n", encoding="utf-8")

    result = copy_distribution(source, root)

    assert result.status == DIVERGED
    assert (destination / "pkg.toml").read_text(encoding="utf-8").endswith("# edited by hand\n")
    assert not (destination / "App").exists()


@pytest.mark.windows_integration
def test_a_junction_inside_the_source_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    """A link in the source could point to files the copying account can read, so the copy is refused outright."""
    source = distribution(tmp_path / "downloads")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "token.txt").write_text("private", encoding="utf-8")
    FakeSystem(tmp_path / "machine").create_junction(source / "App" / "link", secret)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match="link or junction"):
        copy_distribution(source, root)

    assert not (root / "tool").exists()


@pytest.mark.windows_integration
def test_a_root_whose_data_folder_is_a_junction_is_refused(tmp_path: Path) -> None:
    """The copy never writes through a link planted in the root."""
    source = distribution(tmp_path / "downloads")
    root = tmp_path / "opt"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    FakeSystem(tmp_path / "machine").create_junction(root / ".gupkg", elsewhere)

    with pytest.raises(DistributionRefused, match="is a link"):
        copy_distribution(source, root)

    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda base: distribution(base, version="current"), id="reserved-version-name"),
        pytest.param(lambda base: distribution(base, name="_hidden"), id="reserved-package-name"),
        pytest.param(lambda base: distribution(base, version="1.0."), id="trailing-dot-version"),
    ],
)
def test_an_unsafe_name_is_refused(tmp_path: Path, build) -> None:
    """A folder name that is reserved or unsafe on Windows cannot become a package or version directory."""
    source = build(tmp_path / "downloads")

    with pytest.raises(DistributionRefused, match="not shaped"):
        copy_distribution(source, tmp_path / "opt")

    assert not (tmp_path / "opt" / "tool").exists()


def test_a_folder_without_a_manifest_is_refused(tmp_path: Path) -> None:
    """A folder with no ``pkg.toml`` is not a version directory."""
    source = distribution(tmp_path / "downloads")
    (source / "pkg.toml").unlink()

    with pytest.raises(DistributionRefused, match="pkg.toml"):
        copy_distribution(source, tmp_path / "opt")


def make_raw(path: Path, content: bytes = b"x") -> None:
    """Create a file whose name Win32 would normalise away (trailing dot, device name) through the extended-length prefix."""
    with open(EXTENDED + str(path), "wb") as stream:
        stream.write(content)


@pytest.mark.windows_integration
@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("a.txt.", id="trailing-dot"),
        pytest.param("b ", id="trailing-space"),
        pytest.param("NUL", id="device-name"),
        pytest.param("con.txt", id="device-name-with-extension"),
        pytest.param("COM¹", id="superscript-device-name"),
        pytest.param("lpt3.log", id="printer-device-name"),
    ],
)
def test_a_hostile_file_name_is_refused_up_front_and_the_root_is_unchanged(tmp_path: Path, hostile: str) -> None:
    """A name that Windows cannot hold is a refusal with exit code 2 (not a raw OSError), and nothing is left in the root."""
    source = distribution(tmp_path / "downloads")
    make_raw(source / hostile)
    root = tmp_path / "opt"
    before = tree_snapshot(tmp_path / "opt")

    try:
        with pytest.raises(DistributionRefused) as raised:
            copy_distribution(source, root)
    finally:
        shutil.rmtree(EXTENDED + str(tmp_path / "downloads"), ignore_errors=True)

    assert raised.value.exit_code == 2
    assert tree_snapshot(root) == before and not root.exists()


def test_a_refused_copy_leaves_an_existing_root_as_it_was(tmp_path: Path) -> None:
    """A refusal after the work folder was made removes it again; an existing root keeps exactly its content."""
    source = distribution(tmp_path / "downloads")
    (source / "App" / "link").write_text("x", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.mkdir()
    FakeSystem(tmp_path / "machine").create_junction(source / "Icons" / "j", secret)
    root = tmp_path / "opt"
    (root / "other").mkdir(parents=True)
    before = tree_snapshot(root)

    with pytest.raises(DistributionRefused):
        copy_distribution(source, root)

    assert tree_snapshot(root) == before


@pytest.mark.windows_integration
def test_a_directory_swapped_for_a_junction_after_the_check_is_never_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If ``App`` becomes a junction to a secret folder just before it is listed, the copy is refused and copies nothing."""
    source = distribution(tmp_path / "downloads")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "secret.txt").write_text("private", encoding="utf-8")
    real_scandir = os.scandir
    fake = FakeSystem(tmp_path / "machine")
    swapped: list[bool] = []

    def swapping_scandir(path):
        """List *path*, after replacing ``App`` by a junction the first time it is listed."""
        if Path(path).name == "App" and not swapped:
            swapped.append(True)
            Path(path).rename(Path(path).with_name("App.real"))
            fake.create_junction(Path(path), secret)
        return real_scandir(path)

    monkeypatch.setattr(distcopy.os, "scandir", swapping_scandir)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match="link or junction|changed while"):
        copy_distribution(source, root)

    assert swapped and not root.exists()


@pytest.mark.windows_integration
def test_a_folder_swapped_for_a_junction_just_before_a_file_is_opened_is_never_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a parent folder becomes a junction between the listing and the open, the outside file is not copied."""
    source = distribution(tmp_path / "downloads")
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "tool.exe").write_bytes(b"SECRET")
    real_open = os.open
    fake = FakeSystem(tmp_path / "machine")
    swapped: list[bool] = []

    def swapping_open(path, *args, **kwargs):
        """Open *path*, after swapping its parent for a junction to the secret folder the first time."""
        if Path(path).name == "tool.exe" and not swapped:
            swapped.append(True)
            parent = Path(path).parent
            parent.rename(parent.with_name("bin.real"))
            fake.create_junction(parent, secret)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(distcopy.os, "open", swapping_open)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match="leads outside"):
        copy_distribution(source, root)

    assert swapped and not root.exists()


def _scope(mode: Mode, root: Path):
    """Return a scope of *mode* on *root* built from the built-in settings."""
    env = {"USERPROFILE": "C:\\Users\\pat", "APPDATA": "C:\\Users\\pat\\AppData\\Roaming", "ProgramData": "C:\\ProgramData"}
    return make_scope(mode, root, "test", merge_layers([default_layer(mode)]), env)


def test_the_packages_modes_are_checked_on_the_staged_copy_before_anything_reaches_the_root(tmp_path: Path) -> None:
    """A package that does not list the required mode is refused with the mode message, and the root stays empty."""
    source = tmp_path / "downloads" / "tool" / "1.0.0"
    source.mkdir(parents=True)
    manifest = version_text("1.0.0").replace("format = 2\n", 'format = 2\nmodes = ["user"]\n', 1)
    (source / "pkg.toml").write_text(manifest, encoding="utf-8")
    root = tmp_path / "opt"

    with pytest.raises(ModeRefused, match="installs only for a single user"):
        copy_distribution(source, root, required=_scope(Mode.SYSTEM, root))
    assert not root.exists()

    assert copy_distribution(source, root, required=_scope(Mode.USER, root)).status == COPIED


# ---------------------------------------------------------------------------
# Resource bounds, links in the root, and a source that changes while it is read (issue 028 handoff)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "build", "word"),
    [
        pytest.param(distcopy.CopyLimits(entries=3), lambda folder: [(folder / f"f{n}.txt").write_text("x") for n in range(4)], "files and folders", id="entries"),
        pytest.param(distcopy.CopyLimits(bytes=10), lambda folder: (folder / "big.bin").write_bytes(b"x" * 11), "bytes", id="bytes"),
        pytest.param(distcopy.CopyLimits(depth=2), lambda folder: (folder / "a" / "b" / "c" / "d").mkdir(parents=True), "nested", id="depth"),
        pytest.param(distcopy.CopyLimits(manifest_bytes=8), lambda folder: None, "pkg.toml", id="manifest-size"),
    ],
)
def test_a_source_beyond_the_limits_is_refused_and_the_root_is_unchanged(tmp_path: Path, limits, build, word: str) -> None:
    """A source with too many entries, bytes, folder levels or a huge pkg.toml is refused and nothing is written."""
    source = distribution(tmp_path / "downloads")
    build(source)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match=word):
        copy_distribution(source, root, limits=limits)

    assert not root.exists()


def test_the_default_limits_are_the_unpack_limits() -> None:
    """The copy bounds a source like an archive: 200,000 entries and 8 GiB, plus a depth and a manifest size."""
    limits = distcopy.CopyLimits()

    assert (limits.entries, limits.bytes) == (200_000, 8 * 1024**3) and limits.depth == 64 and limits.manifest_bytes == 1024 * 1024


@pytest.mark.windows_integration
@pytest.mark.parametrize("which", ["work", "package", "version"])
def test_a_root_with_a_junction_where_the_copy_writes_is_refused(tmp_path: Path, which: str) -> None:
    """A junction at root\\.gupkg\\work, root\\<name> or root\\<name>\\<version> would send the copy elsewhere: it is refused."""
    source = distribution(tmp_path / "downloads")
    root = tmp_path / "opt"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = FakeSystem(tmp_path / "machine")
    place = {"work": root / ".gupkg" / "work", "package": root / "tool", "version": root / "tool" / "1.0.0"}[which]
    place.parent.mkdir(parents=True, exist_ok=True)
    fake.create_junction(place, elsewhere)

    with pytest.raises(DistributionRefused, match="link"):
        copy_distribution(source, root)

    assert list(elsewhere.iterdir()) == []


def test_a_source_that_loses_a_file_while_it_is_read_is_refused_not_a_raw_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that disappears between the listing and the open is a DistributionRefused (the source changed), not FileNotFoundError."""
    source = distribution(tmp_path / "downloads")
    real_open = os.open

    def vanishing_open(path, *args, **kwargs):
        """Delete the file just before it is opened."""
        if Path(path).name == "tool.exe":
            Path(path).unlink()
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(distcopy.os, "open", vanishing_open)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match="changed while it was being read"):
        copy_distribution(source, root)

    assert not root.exists()


def test_a_path_that_names_another_file_than_the_one_opened_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The open handle must be the file the path names: when the path's identity differs (a swap), the copy stops."""
    source = distribution(tmp_path / "downloads")
    decoy = tmp_path / "decoy.bin"
    decoy.write_bytes(b"decoy")
    real_lstat = os.lstat

    def lying_lstat(path, *args, **kwargs):
        """Report the decoy's identity for the file that was opened, as if the path had been swapped."""
        if Path(path).name == "tool.exe":
            return real_lstat(decoy, *args, **kwargs)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(distcopy.os, "lstat", lying_lstat)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match="not the file that was checked"):
        copy_distribution(source, root)

    assert not root.exists()


def test_a_listing_taken_while_the_folder_changed_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder whose identity differs before and after it is listed is refused (it was replaced while read)."""
    source = distribution(tmp_path / "downloads")
    other = tmp_path / "other"
    other.mkdir()
    real_lstat = os.lstat
    calls: list[int] = []

    def changing_lstat(path, *args, **kwargs):
        """Give the folder a different identity on every other look, so the look before and after the listing disagree."""
        if Path(path).name == "bin":
            calls.append(1)
            return real_lstat(other if len(calls) % 2 == 0 else path, *args, **kwargs)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(distcopy.os, "lstat", changing_lstat)
    root = tmp_path / "opt"

    with pytest.raises(DistributionRefused, match="changed while it was being read"):
        copy_distribution(source, root)

    assert len(calls) >= 2 and not root.exists()
