"""Build a pretend machine for the install-mode tests: three roots, an app settings file and a settings loader.

Everything lives below one temporary folder. The builders write real format 2 version directories, real
``current`` junctions (through the ``FakeSystem`` they are given) and real install records, so the mode code is
exercised on data that has the shape of an installed machine. No ambient environment variable is read: the
environment mapping is built here.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.catalog.record import write_install_record
from gupkg.model.state import InstallRecord
from gupkg.modes import settings_loader
from gupkg.settingsload import read_text_file
from gupkg.system.fake import FakeSystem

from tests.catalog_support import activate, version_text, write_version


class Machine:
    """A temporary machine: ``system``, ``user`` and ``portable`` roots, an environment and an app settings file."""

    def __init__(
        self, base: Path, *, elevated: bool = False, untrusted: set[Path] | None = None, portable_configured: bool = True
    ) -> None:
        self.base = base
        self.portable_configured = portable_configured
        self.system_root = base / "sys" / "opt"
        self.user_root = base / "home" / "opt"
        self.portable_root = base / "home" / "opt-portable"
        self.app = base / "app"
        self.env = {
            "USERPROFILE": str(base / "home"),
            "APPDATA": str(base / "appdata"),
            "ProgramData": str(base / "programdata"),
        }
        self.fake = FakeSystem(base / "machine", elevated=elevated, untrusted_dirs=untrusted or set())
        self.reads: list[Path] = []
        self.write_app_settings()

    def write_app_settings(self, top: str = "", tables: str = "") -> None:
        """Write ``settings.toml`` of the app folder: it places the three roots inside the temporary folder.

        *top* holds extra top-level keys (such as ``default_mode``), *tables* extra tables after ``[roots]``.
        """
        self.app.mkdir(parents=True, exist_ok=True)
        roots = f"[roots]\nsystem = '{self.system_root}'\nuser = '{self.user_root}'\n"
        if self.portable_configured:
            roots += f"portable = '{self.portable_root}'\n"
        text = "schema = 3\n" + top + roots + tables
        (self.app / "settings.toml").write_text(text, encoding="utf-8")

    def read_text(self, path: Path) -> str | None:
        """Read a settings file and remember that it was read."""
        self.reads.append(Path(path))
        return read_text_file(path)

    def loader(self, *, settings_path: Path | None = None, elevated: bool | None = None):
        """Return the settings loader of a run on this machine."""
        return settings_loader(
            system=self.fake, env=self.env, elevated=self.fake.elevated if elevated is None else elevated,
            app_dir=self.app, settings_path=settings_path, read_text=self.read_text,
        )

    def package(
        self, root: Path, name: str, *, version: str = "1.0.0", modes: str | None = None,
        record: str | None = None, installed: bool = False,
    ) -> Path:
        """Write ``<root>/<name>/<version>`` (optionally with ``modes``, an install record and ``current``)."""
        extra = f"modes = {modes}\n" if modes else ""
        text = version_text(version).replace("format = 2\n", f"format = 2\n{extra}", 1)
        package = root / name
        write_version(package, version, populated=installed, text=text)
        if installed:
            activate(self.fake, package, version)
        if record is not None:
            write_install_record(package, InstallRecord(mode=record))
        return package

    def distribution(self, name: str = "tool", version: str = "1.0.0", *, modes: str | None = None) -> Path:
        """Write a distribution folder ``<downloads>/<name>/<version>`` outside every root and return the version folder."""
        extra = f"modes = {modes}\n" if modes else ""
        text = version_text(version).replace("format = 2\n", f"format = 2\n{extra}", 1)
        folder = self.base / "downloads" / name / version
        folder.mkdir(parents=True)
        (folder / "pkg.toml").write_text(text, encoding="utf-8")
        return folder


def tree_snapshot(root: Path) -> dict[str, tuple[int, int] | None]:
    """Return ``{relative path: (size, mtime_ns)}`` for every file and folder below *root* (``None`` for a folder)."""
    snapshot: dict[str, tuple[int, int] | None] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        snapshot[str(path.relative_to(root))] = None if path.is_dir() else (info.st_size, info.st_mtime_ns)
    return snapshot
