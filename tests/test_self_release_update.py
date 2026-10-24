"""Cover gupkg's own package: installing it from its distribution and updating it from a release (design 21.5, 21.6; D14, D27, D28).

Behavior covered: installing the `gupkg` package runs its populate script, which copies the bundled tree into `App` after
checking it against its `MANIFEST.sha256`, and writes the root forwarder, the shims and the shortcut; `gupkg self check-updates`
reads RELEASES.md, writes the new version directory and populates it from the downloaded release; `self update` activates it;
the forwarder (through `current`) needs no change; and every refusal leaves the active version unchanged: a release ZIP whose
digest differs from its RELEASES.md row is refused before anything is unpacked, a row without a digest is refused, an unpacked
tree must match its own manifest exactly (a modified, missing or extra file), `RELEASE.txt` must name the commit of the row, the
declared version must be the one asked for, a release of a higher major version installs nothing and names the bootstrap, the
tracker asks for nothing when RELEASES.md cannot be read, and a hook that imports an unavailable module fails the update naming
the module. Nothing of the old runtime is carried into the new `App`.

Real: the engine, the real `src/pkg.toml` and `src/pkg.local` hooks, real folders and a real ZIP built here. Mocked:
`FakeSystem` (HTTP, registry, shortcuts). Out of scope: building the real release (test_release_bundle) and the bootstrap
script (test_bootstrap_script).
"""

from __future__ import annotations

import hashlib
import io
import shutil
import zipfile
from pathlib import Path

import pytest

from gupkg._version import __version__
from gupkg.launchers import write_launchers
from gupkg.model.manifest import ManifestKind, TrackResult, parse_manifest, render_pinned, manifest_to_toml
from tests.engine_support import World, pin_no_ambient_elevation  # noqa: F401

SOURCE = Path(__file__).resolve().parents[1] / "src"
COMMIT = "c" * 40
RELEASES_URL = "https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md"
RUNNING_MAJOR = int(__version__.split(".")[0])
OLD, NEW = f"{RUNNING_MAJOR}.50.0", f"{RUNNING_MAJOR}.51.0"


def tree_files(version: str, *, marker: str = "") -> dict[str, bytes]:
    """The files of a small bundled tree of *version*, by path relative to the tree."""
    files = {
        "gupkg.cmd": b"@echo off\r\n", "gupkg-tui.cmd": b"@echo off\r\n", "gupkg-manager.cmd": b"@echo off\r\n",
        "gupkg/__init__.py": b"", "gupkg/_version.py": f'__version__ = "{version}"\n'.encode(),
        "gupkg/python/python.exe": b"MZ", "RELEASE.txt": f"gupkg {version}\ncommit {COMMIT}\n".encode(), "VERSION": f"{version}\n".encode(),
    }
    if marker:
        files[marker] = b"only in this runtime\n"
    return files


def manifest_text(files: dict[str, bytes]) -> bytes:
    """Return ``MANIFEST.sha256`` for *files* (paths relative to the version directory, under ``gupkg/``)."""
    lines = sorted(f"{hashlib.sha256(data).hexdigest()}  gupkg/{path}" for path, data in files.items())
    return ("\n".join(lines) + "\n").encode()


def write_tree(folder: Path, files: dict[str, bytes]) -> None:
    """Write *files* and their manifest into the bundled tree folder *folder*."""
    for path, data in {**files, "MANIFEST.sha256": manifest_text(files)}.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def release_zip(version: str, files: dict[str, bytes] | None = None, *, manifest: dict[str, bytes] | None = None) -> bytes:
    """Return a release ZIP of *version*: ``gupkg/<version>/gupkg/...`` with its manifest (of *manifest* when it differs from *files*)."""
    files = tree_files(version) if files is None else files
    entries = {**files, "MANIFEST.sha256": manifest_text(files if manifest is None else manifest)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"gupkg/{version}/pkg.toml", "format = 2\n")
        for path, data in entries.items():
            archive.writestr(f"gupkg/{version}/gupkg/{path}", data)
    return buffer.getvalue()


def releases_md(*rows: tuple[str, str, str | None]) -> str:
    """Return a RELEASES.md table from ``(version, commit, sha256 or None)`` rows."""
    lines = ["| Version | Commit | Date | SHA-256 | Notes |", "|---|---|---|---|---|"]
    for version, commit, digest in rows:
        lines.append(f"| {version} | `{commit}` | 2026-10-24 | {digest or '-'} | notes |")
    return "\n".join(lines) + "\n"


class SelfWorld:
    """A machine with gupkg installed from a distribution folder at OLD, and the machinery to publish a release NEW."""

    def __init__(self, tmp_path: Path, *, hook_suffix: str = "") -> None:
        self.world = World(tmp_path)
        self.fake = self.world.fake
        self.package = self.world.user / "gupkg"
        self.root_data = self.world.user / ".gupkg"
        self.distribution = tmp_path / "downloads" / "gupkg" / OLD
        self._write_version_directory(self.distribution, OLD, hook_suffix)
        shutil.copytree(SOURCE / "pkg.local", tmp_path / "tracker" / "pkg.local")
        shutil.copy2(SOURCE / "pkg.toml", tmp_path / "tracker" / "pkg.toml")
        self.tracker_source = tmp_path / "tracker"
        self.fake.add_url(RELEASES_URL, releases_md())

    def _write_version_directory(self, folder: Path, version: str, hook_suffix: str = "") -> None:
        """Write a distribution version directory: the real pinned pkg.toml, the version's hooks, launchers and a bundled tree."""
        tracker = parse_manifest((SOURCE / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)
        folder.mkdir(parents=True)
        (folder / "pkg.toml").write_text(manifest_to_toml(render_pinned(tracker, TrackResult(version))), encoding="utf-8")
        shutil.copytree(SOURCE / "pkg.local", folder / "pkg.local", ignore=shutil.ignore_patterns("track.py", "__pycache__"))
        if hook_suffix:
            with open(folder / "pkg.local" / "populate.py", "a", encoding="utf-8") as handle:
                handle.write(hook_suffix)
        write_launchers(folder)
        write_tree(folder / "gupkg", tree_files(version, marker="old_only.txt"))

    def install(self):
        """Install gupkg from its distribution folder (a bundled install of gupkg itself)."""
        return self.world.one("install", str(self.distribution))

    def install_tracker(self) -> None:
        """Put the real tracker (pkg.toml and hooks) into the installed package."""
        shutil.copytree(self.tracker_source, self.package / "_check_updates")

    def publish(self, version: str = NEW, *, zip_bytes: bytes | None = None, digest: str | None = "from-zip", commit: str = COMMIT,
                extra_rows: tuple[tuple[str, str, str | None], ...] = ()) -> bytes:
        """Serve RELEASES.md with a row for *version* and the release ZIP it names; return the ZIP bytes."""
        data = release_zip(version) if zip_bytes is None else zip_bytes
        shown = hashlib.sha256(data).hexdigest() if digest == "from-zip" else digest
        self.fake.add_url(RELEASES_URL, releases_md((version, commit, shown), *extra_rows))
        self.fake.add_url(f"https://github.com/guraltsev/gupkg/releases/download/v{version}/gupkg-{version}-win-amd64.zip", data)
        return data

    def check_updates(self):
        """Run `gupkg self check-updates` (check-updates gupkg)."""
        return self.world.one("check-updates", "gupkg")


@pytest.fixture
def selfworld(tmp_path: Path) -> SelfWorld:
    """gupkg installed at OLD from its distribution, with its real tracker in place."""
    selfworld = SelfWorld(tmp_path)
    assert selfworld.install().ok
    selfworld.install_tracker()
    return selfworld


def test_installing_gupkg_copies_the_verified_tree_into_app_and_writes_forwarder_and_shims(tmp_path: Path) -> None:
    """The populate script fills App from the bundled tree; the forwarder, the three shims and the Start Menu entry exist; the tree stays."""
    selfworld = SelfWorld(tmp_path)

    result = selfworld.install()

    version = selfworld.package / OLD
    assert result.ok, result.error
    assert (version / "App" / "gupkg" / "_version.py").read_text(encoding="utf-8") == f'__version__ = "{OLD}"\n'
    assert (version / "gupkg" / "MANIFEST.sha256").is_file() and (version / "App" / "gupkg.cmd").is_file()
    assert (selfworld.root_data / "gupkg.cmd").is_file() and (selfworld.root_data / "gupkg-manager.cmd").is_file()
    bin_dir = selfworld.world.mode_context("user").bin_dir
    assert {"gupkg.exe", "gupkg-tui.exe", "gupkg-manager.exe"} <= {item.name for item in bin_dir.iterdir()}
    assert [path.name for path in selfworld.fake.shortcuts()] == ["gupkg manager.lnk"]


def test_a_tree_that_does_not_match_its_manifest_is_never_installed(tmp_path: Path) -> None:
    """An edited bundled file refuses the install: no App, no forwarder."""
    selfworld = SelfWorld(tmp_path)
    (selfworld.distribution / "gupkg" / "gupkg" / "__init__.py").write_bytes(b"# planted\n")

    result = selfworld.install()

    assert not result.ok and "MANIFEST.sha256" in str(result.error)
    assert not (selfworld.package / OLD / "App").exists() and not (selfworld.root_data / "gupkg.cmd").exists()


def test_self_check_updates_fetches_verifies_and_populates_the_new_version_and_update_switches(selfworld: SelfWorld) -> None:
    """check-updates writes and populates the new version (no activation); update switches `current`; the forwarder is untouched and nothing of the old runtime is carried over."""
    selfworld.publish(NEW)
    forwarder = (selfworld.root_data / "gupkg.cmd").read_bytes()

    checked = selfworld.check_updates()

    new = selfworld.package / NEW
    assert checked.ok and checked.version == NEW, checked.error
    assert (new / "gupkg" / "MANIFEST.sha256").is_file() and (new / "App" / "gupkg" / "_version.py").is_file()
    assert (selfworld.package / "current").resolve() == (selfworld.package / OLD).resolve()
    assert not (new / "pkg.local" / "track.py").exists() and (new / "install.cmd").is_file()

    updated = selfworld.world.one("update", "gupkg")

    assert updated.ok, updated.error
    assert (selfworld.package / "current").resolve() == new.resolve()
    assert (selfworld.root_data / "gupkg.cmd").read_bytes() == forwarder
    assert (selfworld.package / OLD / "App" / "old_only.txt").is_file() and not (new / "App" / "old_only.txt").exists()


def test_the_digest_of_the_zip_is_checked_against_the_row_before_anything_is_unpacked(selfworld: SelfWorld) -> None:
    """A ZIP that is not the one the row names is refused: no tree, no App, the active version unchanged."""
    selfworld.publish(NEW, digest="0" * 64)

    result = selfworld.check_updates()

    new = selfworld.package / NEW
    assert not result.ok and not (new / "gupkg").exists() and not (new / "App").exists()
    assert (selfworld.package / "current").resolve() == (selfworld.package / OLD).resolve()


def test_a_row_without_a_digest_is_refused(selfworld: SelfWorld) -> None:
    """RELEASES.md listing the newest release with no SHA-256 stops the check and names the version."""
    selfworld.publish(NEW, digest=None)

    result = selfworld.check_updates()

    assert not result.ok and "without a SHA-256" in str(result.error) and not (selfworld.package / NEW).exists()


@pytest.mark.parametrize("damage", ["modified", "missing", "extra"])
def test_the_unpacked_tree_must_match_its_own_manifest_exactly(selfworld: SelfWorld, damage: str) -> None:
    """A release whose files differ from its MANIFEST.sha256 by one modified, missing or extra file is refused before the tree is put in place."""
    files = tree_files(NEW)
    manifest = dict(files)
    if damage == "modified":
        files["gupkg/__init__.py"] = b"# not what the manifest says\n"
    elif damage == "missing":
        manifest["gupkg/extra_listed.py"] = b"listed but absent"
    else:
        files["gupkg/planted.py"] = b"planted"
    selfworld.publish(NEW, zip_bytes=release_zip(NEW, files, manifest=manifest))

    result = selfworld.check_updates()

    assert not result.ok and "MANIFEST.sha256" in str(result.error)
    assert not (selfworld.package / NEW / "gupkg").exists() and not (selfworld.package / NEW / "App").exists()


def test_release_txt_must_name_the_commit_of_the_row(selfworld: SelfWorld) -> None:
    """A tree built from another commit than the row lists is refused."""
    selfworld.publish(NEW, commit="d" * 40)

    result = selfworld.check_updates()

    assert not result.ok and "was built from commit" in str(result.error)


def test_the_declared_version_must_be_the_one_that_is_populated(selfworld: SelfWorld) -> None:
    """A ZIP whose tree declares another version than its row is refused."""
    files = tree_files(NEW)
    files["gupkg/_version.py"] = b'__version__ = "9.9.9"\n'
    selfworld.publish(NEW, zip_bytes=release_zip(NEW, files))

    result = selfworld.check_updates()

    assert not result.ok and "9.9.9" in str(result.error) and not (selfworld.package / NEW / "App").exists()


def test_a_release_of_a_higher_major_version_installs_nothing_and_names_the_bootstrap(selfworld: SelfWorld) -> None:
    """The tracker refuses a newer major version: the active version stays and the bootstrap is named."""
    higher = f"{RUNNING_MAJOR + 1}.0.0"
    selfworld.publish(higher)

    result = selfworld.check_updates()

    assert not result.ok and "gupkg-bootstrap.cmd" in str(result.error) and "Nothing was changed" in str(result.error)
    assert not (selfworld.package / higher).exists()
    assert (selfworld.package / "current").resolve() == (selfworld.package / OLD).resolve()


def test_the_newest_row_is_the_one_that_is_checked(selfworld: SelfWorld) -> None:
    """With several rows the newest version's row decides the version, its asset and its digest."""
    older = f"{RUNNING_MAJOR}.49.0"
    selfworld.publish(NEW, extra_rows=((older, COMMIT, "a" * 64),))

    result = selfworld.check_updates()

    assert result.ok and result.version == NEW and not (selfworld.package / older).exists()


def test_an_unreadable_release_list_is_an_error_not_no_new_version(selfworld: SelfWorld) -> None:
    """When RELEASES.md cannot be fetched the check fails and says so."""
    del selfworld.fake.urls[RELEASES_URL]

    result = selfworld.check_updates()

    assert not result.ok and "release list" in str(result.error)


def test_a_hook_that_imports_an_unavailable_module_fails_the_install_naming_it(tmp_path: Path) -> None:
    """A populate script whose import fails stops the install and names the missing module."""
    selfworld = SelfWorld(tmp_path, hook_suffix="\nimport module_that_the_bundle_does_not_ship\n")

    result = selfworld.install()

    assert not result.ok and "module_that_the_bundle_does_not_ship" in str(result.error)
