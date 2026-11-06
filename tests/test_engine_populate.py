"""Cover the atomic population of ``App`` (``gupkg.engine.appbuild``) beyond what the lifecycle tests reach.

Behaviour covered: the cached file is hashed again right before it is unpacked, so a file replaced after its verification stops the
population; an elevated run refuses a 7-Zip in a folder users can change before any download; ``App`` that is a link is
never built below or replaced; an existing ``App`` is replaced only on request, the old one is moved aside first and put back
when the new one cannot be moved into place; a failed build leaves nothing in the work folder.

Real: the population code, real folders, zip archives and the HTTP client policy. Mocked: ``FakeSystem`` (URL fixtures,
admin-only folders); the replacement of a cached file and a failing rename are injected where the boundary is the file system.
Out of scope: the lifecycle around it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gupkg.engine.appbuild import AppBuildError, populate_version
from gupkg.model.checksum import ChecksumMismatch
from gupkg.model.manifest import parse_manifest
from gupkg.report.events import PhaseStart, use_reporter
from gupkg.system.fake import FakeHttp
from tests.support.engine import World, pkg_text

pytestmark = pytest.mark.unit


def build(world: World, version_dir: Path, **options):
    """Call ``populate_version`` for the version directory of ``tool`` in the user root."""
    manifest = parse_manifest((version_dir / "pkg.toml").read_text(encoding="utf-8"))
    return populate_version(
        world.engine, world.mode_context(), version_dir.parent, version_dir, manifest, name="tool", version=version_dir.name,
        skip=None, record_manifest=True, **options,
    )


class Tamper:
    """A reporter that, as the Unpack phase starts, replaces every cached download with other bytes of the same size."""

    def __init__(self, cache: Path) -> None:
        self.cache = cache

    def handle(self, event: object) -> None:
        """On the start of Unpack, reverse the bytes of each cached file."""
        if isinstance(event, PhaseStart) and event.phase == "Unpack":
            for item in self.cache.rglob("*"):
                if item.is_file():
                    item.write_bytes(bytes(reversed(item.read_bytes())))


def test_a_cached_file_replaced_after_its_verification_stops_the_population(world: World) -> None:
    """The file is hashed again right before it is unpacked: a replacement between the check and the use is a checksum mismatch."""
    folder = world.publish("tool", "1.0.0")

    with use_reporter(Tamper(world.user / ".gupkg" / "cache")):
        with pytest.raises(ChecksumMismatch, match="changed after it was verified"):
            build(world, folder)

    assert not (folder / "App").exists()
    work = folder.parent / ".gupkg" / "work"
    assert not work.exists() or list(work.iterdir()) == []


@pytest.mark.parametrize(
    ("final_url", "headers"),
    [
        pytest.param("https://cdn.example.net/asset/9853cec6", {"Content-Disposition": "attachment; filename=tool-1.0p.exe"}, id="named-by-the-server"),
        pytest.param("https://cdn.example.net/files/tool-1.0p.exe", {}, id="named-by-the-final-address"),
    ],
)
def test_a_plain_file_gets_the_name_it_is_served_under(world: World, final_url: str, headers: dict[str, str]) -> None:
    """A redirected download keeps the name the server gives it, so a `[[prepare]]` rename of that name finds the file."""
    # Arrange: a vendor that redirects a download to a file name that differs from the served one.
    body = b"MZ a program"
    url = "https://vendor.example/releases/download/tool-1.0p.exe"
    world.fake.add_url(url, FakeHttp("", 302, {"Location": final_url}))
    world.fake.add_url(final_url, FakeHttp(body, headers=headers))
    # A manifest that pins the digest and renames the file after the download.
    version_dir = world.user / "tool" / "1.0"
    version_dir.mkdir(parents=True)
    (version_dir / "pkg.toml").write_text(
        pkg_text(
            url, sha256=hashlib.sha256(body).hexdigest(), source='kind = "exe"',
            tables='[[prepare]]\nrename = { from = "tool-1.0p.exe", to = "tool.exe" }\n',
        ),
        encoding="utf-8",
    )

    # Act: build the app.
    build(world, version_dir)

    # Assert: the file has the name the manifest gives it.
    assert [item.name for item in (version_dir / "App").iterdir()] == ["tool.exe"]


def test_an_elevated_run_refuses_a_seven_zip_in_an_untrusted_folder_before_any_download(tmp_path: Path) -> None:
    """The extractor is judged before the work folder or the client exist: nothing is requested."""
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "7z.exe").write_bytes(b"MZ")
    world = World(tmp_path, elevated=True, untrusted={tools})
    world.engine.seven_zip = tools / "7z.exe"
    world.publish("tool", "1.0.0", mode="system")
    world.fake.fetch_log.clear()

    result = world.one("install", "tool")

    assert not result.ok and result.error["code"] == "untrusted-location" and world.fake.fetch_log == []


def test_an_app_that_is_a_link_is_never_built_below_or_replaced(tmp_path: Path, world: World) -> None:
    """An App junction in the version directory is refused by the population itself, whatever the scan above it did."""
    folder = world.publish("tool", "1.0.0")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.fake.create_junction(folder / "App", elsewhere)

    with pytest.raises(AppBuildError, match="link"):
        build(world, folder, replace=True)

    assert list(elsewhere.iterdir()) == []


def test_an_existing_app_is_replaced_only_on_request(world: World) -> None:
    """Without `replace` a populated App stays and the call says so; with it the new App takes its place."""
    folder = world.publish("tool", "1.0.0")
    build(world, folder)
    (folder / "App" / "app.exe").write_text("edited", encoding="utf-8")

    with pytest.raises(AppBuildError, match="already holds an App"):
        build(world, folder)
    assert (folder / "App" / "app.exe").read_text() == "edited"

    build(world, folder, replace=True)
    assert (folder / "App" / "app.exe").read_text() == "tool 1.0.0\n"


def test_the_old_app_is_put_back_when_the_new_one_cannot_be_moved_into_place(world: World) -> None:
    """If the final rename fails the previous App is restored, the failure is reported and no scratch folder is left."""
    # Arrange: a built app with a file of the previous version.
    folder = world.publish("tool", "1.0.0")
    build(world, folder)
    (folder / "App" / "keep.txt").write_text("previous", encoding="utf-8")
    real_replace = os.replace
    failed: list[bool] = []

    # Let the old app be moved aside, then fail the move of the new one.
    def failing_replace(source, destination, *arguments, **options):
        """Let the old App be moved aside, then fail (once) the move of the new one to its final place."""
        if Path(destination) == folder / "App" and not failed:
            failed.append(True)
            raise PermissionError("locked")
        return real_replace(source, destination, *arguments, **options)

    # Install the failing move.
    world.engine.replace = failing_replace

    # Act: rebuild the app and replace the previous one.
    with pytest.raises(AppBuildError, match="cannot be moved into place"):
        build(world, folder, replace=True)

    # Assert: the previous app is back and no work folder is left.
    assert (folder / "App" / "keep.txt").read_text() == "previous"
    work = folder.parent / ".gupkg" / "work"
    assert not work.exists() or list(work.iterdir()) == []


def test_when_the_old_app_cannot_be_put_back_it_is_kept_and_named(world: World) -> None:
    """If even the restore fails, the previous App is not deleted: it stays in the work folder and the error says where."""
    # Arrange: a built app with a file of the previous version.
    folder = world.publish("tool", "1.0.0")
    build(world, folder)
    (folder / "App" / "keep.txt").write_text("previous", encoding="utf-8")
    real_replace = os.replace

    # Fail every move to the final app folder.
    def failing_replace(source, destination, *arguments, **options):
        """Fail every move that targets the final App folder."""
        if Path(destination) == folder / "App":
            raise PermissionError("locked")
        return real_replace(source, destination, *arguments, **options)

    # Install the failing move.
    world.engine.replace = failing_replace

    # Act: rebuild the app and replace the previous one.
    with pytest.raises(AppBuildError, match="previous App is kept in"):
        build(world, folder, replace=True)

    # Assert: the previous app is kept in the work folder and named.
    kept = list((folder.parent / ".gupkg" / "work").glob("old-App-*"))
    assert len(kept) == 1 and (kept[0] / "keep.txt").read_text() == "previous"
