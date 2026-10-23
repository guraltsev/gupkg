"""Cover small guarantees of the services: name derivation, overlapping rules, repeated names, case renames, the boundary.

Real services over real folders and ``FakeSystem``. Covers the file name of an artifact whose URL ends in
``/download``, ``[[source.extract]]`` rules that map to one destination, a tarball that repeats a file
name, renaming ``Foo.exe`` to ``foo.exe``, and the repository rule that only the Windows system class and
the elevation module touch the registry, ``ctypes``, the URL opener or a shell. Out of scope: the details
tested in ``test_unpack`` and ``test_prepare``.
"""

from __future__ import annotations

import io
import re
import tarfile
from pathlib import Path

import pytest

from gupkg.model.manifest import ExtractRule, PrepareRename, render_pinned
from gupkg.services.http import HttpClient
from gupkg.services.prepare import PrepareError, run_prepare
from gupkg.services.track import run_tracker
from gupkg.services.unpack import UnpackError, UnpackPlan, unpack_artifact
from gupkg.services.values import file_name_from_url
from gupkg.system.fake import FakeHttp, FakeSystem
from tests.archive_support import make_zip, tree
from tests.http_fixtures import environment, serve, tracker_manifest

SOURCE = Path(__file__).resolve().parents[1] / "src" / "gupkg"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param("https://sourceforge.net/projects/crystaldiskinfo/files/9.4.2/CrystalDiskInfo9_4_2.zip/download", "CrystalDiskInfo9_4_2.zip", id="sourceforge-download"),
        pytest.param("https://example.org/files/tool%20v1.zip", "tool v1.zip", id="percent-encoded"),
        pytest.param("https://example.org/a/b/dl", "b", id="dl-marker"),
        pytest.param("https://example.org/", "download", id="no-path"),
        pytest.param("https://example.org/x/..%5Cevil.exe", "evil.exe", id="backslash-in-the-name"),
    ],
)
def test_the_file_name_of_an_artifact_skips_a_trailing_download_marker(url: str, expected: str) -> None:
    """The name comes from the last real path segment, decoded and made harmless."""
    assert file_name_from_url(url) == expected


def test_a_download_whose_url_ends_in_download_gets_the_real_file_name_and_the_exe_kind_keeps_it(system: FakeSystem, tmp_path: Path) -> None:
    """The crystaldiskinfo address ends in `/download`; the cached download is still named `CrystalDiskInfo9_4_2.zip`."""
    url = "https://sourceforge.net/projects/crystaldiskinfo/files/9.4.2/CrystalDiskInfo9_4_2.zip/download"
    system.urls[url] = FakeHttp(b"MZ" + bytes(200))
    client = HttpClient(system, env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache")

    downloaded = client.download(url)
    stage = tmp_path / "stage"
    stage.mkdir()
    unpack_artifact(system, downloaded.path, stage / "App", UnpackPlan(kind="exe"), filename=downloaded.filename)

    assert downloaded.filename == "CrystalDiskInfo9_4_2.zip"
    assert [path.name for path in (stage / "App").iterdir()] == ["CrystalDiskInfo9_4_2.zip"]


def test_the_redirect_provider_names_the_file_from_the_final_address(system: FakeSystem, tmp_path: Path) -> None:
    """The `file_name` capture of the crystaldiskinfo tracker is the zip, not the word `download`."""
    serve(system, "redirect_crystaldiskinfo")

    result = run_tracker(tracker_manifest("crystaldiskinfo"), environment(system, tmp_path, package="crystaldiskinfo"))

    assert result.captures["file_name"] == "CrystalDiskInfo9_4_2.zip"


def test_two_extract_rules_that_map_to_one_destination_are_an_error(system: FakeSystem, tmp_path: Path) -> None:
    """Silently overwriting a file an earlier rule wrote would make the result depend on the rule order: it fails."""
    archive = make_zip(tmp_path / "a.zip", {"one/x.txt": "1", "two/x.txt": "2"})
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(UnpackError, match="already exists"):
        unpack_artifact(system, archive, stage / "App", UnpackPlan(extract=(ExtractRule("one/", ""), ExtractRule("two/", ""))))

    assert list(stage.iterdir()) == []


def test_extract_rules_with_different_destinations_and_empty_folders_still_work(system: FakeSystem, tmp_path: Path) -> None:
    """Distinct destinations are fine, and an empty folder under a matched folder is kept."""
    archive = make_zip(tmp_path / "a.zip", {"one/x.txt": "1", "two/x.txt": "2", "two/empty/": ""})
    stage = tmp_path / "stage"
    stage.mkdir()

    unpack_artifact(system, archive, stage / "App", UnpackPlan(extract=(ExtractRule("one/", "a"), ExtractRule("two/", "b"))))

    assert tree(stage / "App") == {"a/x.txt": "1", "b/x.txt": "2"} and (stage / "App" / "b" / "empty").is_dir()


def test_a_tarball_that_repeats_a_file_name_is_refused_by_its_table_of_contents(system: FakeSystem, tmp_path: Path) -> None:
    """Two entries for one file in a tar are refused before extraction (7-Zip would let the second overwrite the first)."""
    archive = tmp_path / "dup.tar"
    with tarfile.open(archive, "w") as bundle:
        for text in (b"first", b"second"):
            info = tarfile.TarInfo("a.txt")
            info.size = len(text)
            bundle.addfile(info, io.BytesIO(text))
    stage = tmp_path / "stage"
    stage.mkdir()

    with pytest.raises(UnpackError, match="appears more than once"):
        unpack_artifact(system, archive, stage / "App", UnpackPlan())

    assert list(stage.iterdir()) == []


def test_a_case_only_rename_is_allowed_and_a_real_collision_is_not(tmp_path: Path) -> None:
    """`Foo.exe` to `foo.exe` renames the same file; renaming onto a different existing file still fails."""
    app = tmp_path / "App"
    app.mkdir()
    (app / "Foo.exe").write_text("foo", encoding="utf-8")
    (app / "other.exe").write_text("other", encoding="utf-8")

    run_prepare([PrepareRename("Foo.exe", "foo.exe")], app)
    with pytest.raises(PrepareError, match="never overwrites"):
        run_prepare([PrepareRename("foo.exe", "other.exe")], app)

    assert sorted(path.name for path in app.iterdir()) == ["foo.exe", "other.exe"]
    assert (app / "foo.exe").read_text() == "foo"


def test_only_the_transport_and_the_elevation_module_touch_the_registry_ctypes_the_url_opener_or_a_shell() -> None:
    """The boundary rule of issue 021 still holds: no service or other module of gupkg uses these directly."""
    pattern = re.compile(r"winreg|ctypes|" + "url" + r"open|powershell|subprocess\.run")
    allowed = {Path("system/windows.py"), Path("selfmgmt/elevation.py")}
    offenders = [
        path.relative_to(SOURCE)
        for path in SOURCE.rglob("*.py")
        if "python" not in path.relative_to(SOURCE).parts[:1] and "__pycache__" not in path.parts
        and pattern.search(path.read_text(encoding="utf-8")) and path.relative_to(SOURCE) not in allowed
    ]

    assert offenders == []
