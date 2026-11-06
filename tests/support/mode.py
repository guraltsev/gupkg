"""Build a pretend machine for the scope tests: two roots, an app settings file and the scope of a run on it.

Everything lives below one temporary folder. The builders write real format 2 version directories, real
``current`` junctions (through the ``FakeSystem`` they are given) and real install records, so the mode code is
exercised on data that has the shape of an installed machine. No ambient environment variable is read: the
environment mapping is built here.

Usage and API
-------------
Construct ``Machine`` with a temporary folder; ``Machine.scope()`` returns the scope of a run, ``package`` and
``distribution`` write packages, and ``tree_snapshot`` records a folder tree to compare before and after a run.

Implementation Approach
-----------------------
All folders, roots and environment variables are derived from the one base folder, and the scope is built by the
product's own ``build_scope`` from the settings file written here.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.catalog.record import write_install_record
from gupkg.model.state import InstallRecord
from gupkg.model.scope import Scope
from gupkg.scope import build_scope
from gupkg.selfmgmt.location import RunningGupkg
from gupkg.settingsload import read_text_file
from gupkg.system.fake import FakeSystem

from tests.support.catalog import activate, version_text, write_version


class Machine:
    """A temporary machine: ``system`` and ``user`` roots, an environment and an app settings file.

    Parameters
    ----------
    base : Path
        The temporary folder everything lives under.
    elevated : bool
        Whether the fake machine reports an elevated process.
    untrusted : set[Path] | None
        Folders that the fake machine reports as changeable by standard users.
    installed : bool
        Whether the running gupkg is the one installed in the system root (its ``$App`` folder is
        ``<system root>\\gupkg\\1.0.0\\App``). By default it is a source tree and ``$App`` is ``<base>\\app``.

    Methods
    -------
    write_app_settings
        Writes the app folder's ``settings.toml``.
    read_text
        Reads a settings file and records the read.
    location
        Describes the running gupkg.
    scope
        Builds the scope of a run.
    package
        Writes a package into a root.
    distribution
        Writes a distribution folder outside every root.
    """

    def __init__(
        self, base: Path, *, elevated: bool = False, untrusted: set[Path] | None = None, installed: bool = False
    ) -> None:
        # Keep every folder and environment variable inside the test's own base folder.
        self.base = base
        self.installed = installed
        self.system_root = base / "sys" / "opt"
        self.user_root = base / "home" / "opt"
        self.app = self.system_root / "gupkg" / "1.0.0" / "App" if installed else base / "app"
        self.env = {
            "USERPROFILE": str(base / "home"),
            "APPDATA": str(base / "appdata"),
            "ProgramData": str(base / "programdata"),
        }
        # Build the fake machine and write the settings that place both roots inside the base folder.
        self.fake = FakeSystem(base / "machine", elevated=elevated, untrusted_dirs=untrusted or set())
        self.reads: list[Path] = []
        self.write_app_settings()

    def write_app_settings(self, top: str = "", tables: str = "") -> None:
        """Write ``settings.toml`` of the app folder: it places the two roots inside the temporary folder.

        *top* holds extra top-level keys (such as ``schema``), *tables* extra tables after ``[roots]``.

        Parameters
        ----------
        top : str, optional
            Extra top-level keys, such as ``schema``.
        tables : str, optional
            Extra tables written after ``[roots]``.
        """
        self.app.mkdir(parents=True, exist_ok=True)
        roots = f"[roots]\nsystem = '{self.system_root}'\nuser = '{self.user_root}'\n"
        text = "schema = 4\n" + top + roots + tables
        (self.app / "settings.toml").write_text(text, encoding="utf-8")

    def read_text(self, path: Path) -> str | None:
        """Read a settings file and remember that it was read.

        Parameters
        ----------
        path : Path
            Settings file to read.

        Returns
        -------
        str | None
            Text of the file, or ``None`` when it does not exist.
        """
        self.reads.append(Path(path))
        return read_text_file(path)

    @property
    def location(self) -> RunningGupkg:
        """A stand-in for the running gupkg: the one installed in the system root, or a source tree; its ``$App`` folder is this machine's app folder."""
        if self.installed:
            return RunningGupkg("installed", self.app / "gupkg", (self.app,), self.app.parent, self.system_root, self.app)
        return RunningGupkg("source", self.base / "gupkg-src" / "gupkg", (), app_dir=self.app)

    def scope(self, *, settings_path: Path | None = None, root_option: Path | None = None) -> Scope:
        """Return the scope of a run on this machine: system mode when the fake machine is elevated, else user mode.

        Parameters
        ----------
        settings_path : Path | None, optional
            Settings file named on the command line.
        root_option : Path | None, optional
            Root folder named on the command line.

        Returns
        -------
        Scope
            The scope of the run.
        """
        return build_scope(
            system=self.fake, env=self.env, location=self.location, root_option=root_option, settings_path=settings_path,
            read_text=self.read_text,
        )

    def package(
        self, root: Path, name: str, *, version: str = "1.0.0", modes: str | None = None,
        record: str | None = None, installed: bool = False,
    ) -> Path:
        """Write ``<root>/<name>/<version>`` (optionally with ``modes``, an install record and ``current``).

        Parameters
        ----------
        root : Path
            Root folder that receives the package.
        name : str
            Package name.
        version : str, optional
            Version label.
        modes : str | None, optional
            Value of the ``modes`` key in ``pkg.toml``, as TOML text; omitted when ``None``.
        record : str | None, optional
            Mode recorded in an install record; no record is written when ``None``.
        installed : bool, optional
            Whether to populate ``App`` and point ``current`` at the version.

        Returns
        -------
        Path
            The package folder.
        """
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
        """Write a distribution folder ``<downloads>/<name>/<version>`` outside every root and return the version folder.

        Parameters
        ----------
        name : str, optional
            Package name.
        version : str, optional
            Version label.
        modes : str | None, optional
            Value of the ``modes`` key in ``pkg.toml``, as TOML text; omitted when ``None``.

        Returns
        -------
        Path
            The version folder.
        """
        extra = f"modes = {modes}\n" if modes else ""
        text = version_text(version).replace("format = 2\n", f"format = 2\n{extra}", 1)
        folder = self.base / "downloads" / name / version
        folder.mkdir(parents=True)
        (folder / "pkg.toml").write_text(text, encoding="utf-8")
        return folder


def tree_snapshot(root: Path) -> dict[str, tuple[int, int] | None]:
    """Return ``{relative path: (size, mtime_ns)}`` for every file and folder below *root* (``None`` for a folder).

    Parameters
    ----------
    root : Path
        Folder to inspect.

    Returns
    -------
    dict[str, tuple[int, int] | None]
        Size and modification time of each file below *root*, ``None`` for a folder; empty when *root* does not exist.
    """
    snapshot: dict[str, tuple[int, int] | None] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        snapshot[str(path.relative_to(root))] = None if path.is_dir() else (info.st_size, info.st_mtime_ns)
    return snapshot
