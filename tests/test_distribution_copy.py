"""Cover the copy of a distribution folder into a root (design 21.4, D17, D40).

Behavior covered: the folder is copied whole into ``<root>\\<name>\\<version>``; the copies carry no Mark of the Web
(``Zone.Identifier``) and the result says whether the source was marked; an existing destination is never changed
(``identical`` when it holds the same files, ``diverged`` otherwise); a source with a link or junction, a missing
``pkg.toml`` or an unsafe name is refused and nothing is written; an interrupted layout leaves no work folder.

Real: the copy on real folders and real NTFS alternate data streams below pytest's temporary folder (the
streams need NTFS, so the marked-source tests are ``windows_integration``), and a real junction. Out of scope:
the engine's receipt, which records ``source_marked`` (issue 028).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.local import tree_hash
from gupkg.distcopy import COPIED, DIVERGED, IDENTICAL, DistributionRefused, copy_distribution
from gupkg.system.fake import FakeSystem


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
