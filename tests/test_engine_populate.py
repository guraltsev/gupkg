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

import os
from pathlib import Path

import pytest

from gupkg.engine.appbuild import AppBuildError, populate_version
from gupkg.model.checksum import ChecksumMismatch
from gupkg.model.manifest import parse_manifest
from gupkg.report.events import PhaseStart, use_reporter
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401


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


def test_a_cached_file_replaced_after_its_verification_stops_the_population(tmp_path: Path) -> None:
    """The file is hashed again right before it is unpacked: a replacement between the check and the use is a checksum mismatch."""
    world = World(tmp_path)
    folder = world.publish("tool", "1.0.0")

    with use_reporter(Tamper(world.user / ".gupkg" / "cache")):
        with pytest.raises(ChecksumMismatch, match="changed after it was verified"):
            build(world, folder)

    assert not (folder / "App").exists()
    work = folder.parent / ".gupkg" / "work"
    assert not work.exists() or list(work.iterdir()) == []


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


def test_an_app_that_is_a_link_is_never_built_below_or_replaced(tmp_path: Path) -> None:
    """An App junction in the version directory is refused by the population itself, whatever the scan above it did."""
    world = World(tmp_path)
    folder = world.publish("tool", "1.0.0")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    world.fake.create_junction(folder / "App", elsewhere)

    with pytest.raises(AppBuildError, match="link"):
        build(world, folder, replace=True)

    assert list(elsewhere.iterdir()) == []


def test_an_existing_app_is_replaced_only_on_request(tmp_path: Path) -> None:
    """Without `replace` a populated App stays and the call says so; with it the new App takes its place."""
    world = World(tmp_path)
    folder = world.publish("tool", "1.0.0")
    build(world, folder)
    (folder / "App" / "app.exe").write_text("edited", encoding="utf-8")

    with pytest.raises(AppBuildError, match="already holds an App"):
        build(world, folder)
    assert (folder / "App" / "app.exe").read_text() == "edited"

    build(world, folder, replace=True)
    assert (folder / "App" / "app.exe").read_text() == "tool 1.0.0\n"


def test_the_old_app_is_put_back_when_the_new_one_cannot_be_moved_into_place(tmp_path: Path) -> None:
    """If the final rename fails the previous App is restored, the failure is reported and no scratch folder is left."""
    world = World(tmp_path)
    folder = world.publish("tool", "1.0.0")
    build(world, folder)
    (folder / "App" / "keep.txt").write_text("previous", encoding="utf-8")
    real_replace = os.replace
    failed: list[bool] = []

    def failing_replace(source, destination, *arguments, **options):
        """Let the old App be moved aside, then fail (once) the move of the new one to its final place."""
        if Path(destination) == folder / "App" and not failed:
            failed.append(True)
            raise PermissionError("locked")
        return real_replace(source, destination, *arguments, **options)

    world.engine.replace = failing_replace

    with pytest.raises(AppBuildError, match="cannot be moved into place"):
        build(world, folder, replace=True)

    assert (folder / "App" / "keep.txt").read_text() == "previous"
    work = folder.parent / ".gupkg" / "work"
    assert not work.exists() or list(work.iterdir()) == []


def test_when_the_old_app_cannot_be_put_back_it_is_kept_and_named(tmp_path: Path) -> None:
    """If even the restore fails, the previous App is not deleted: it stays in the work folder and the error says where."""
    world = World(tmp_path)
    folder = world.publish("tool", "1.0.0")
    build(world, folder)
    (folder / "App" / "keep.txt").write_text("previous", encoding="utf-8")
    real_replace = os.replace

    def failing_replace(source, destination, *arguments, **options):
        """Fail every move that targets the final App folder."""
        if Path(destination) == folder / "App":
            raise PermissionError("locked")
        return real_replace(source, destination, *arguments, **options)

    world.engine.replace = failing_replace

    with pytest.raises(AppBuildError, match="previous App is kept in"):
        build(world, folder, replace=True)

    kept = list((folder.parent / ".gupkg" / "work").glob("old-App-*"))
    assert len(kept) == 1 and (kept[0] / "keep.txt").read_text() == "previous"
