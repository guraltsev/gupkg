"""Make the checkout's source tree and maintainer tools importable, and share a real release ZIP.

The module puts ``src`` and ``tools`` on the import path, enables the hygiene checks, and provides the fixtures that
every area shares: a release bundle built once per session, a pretend machine, the engine worlds and the scripted
``gupkg`` of the UI tests. Every test starts without the variables that change colour, width or encoding of output.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import NamedTuple

import pytest


# The automatic hygiene checks (_docs/tests_hygiene.md section 6) run with every session.
pytest_plugins = ["tests.support.hygiene"]

# Directory that holds the importable gupkg source packages.
# The checkout's ``src`` folder, put first on ``sys.path`` so tests import this tree.
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# tomli_w is the one TOML writer. A dev .venv made earlier
# may lack it, so load the very copy the release bundle ships, and only that package.
if importlib.util.find_spec("tomli_w") is None:
    _shipped = SOURCE_ROOT / "gupkg" / "python" / "Lib" / "site-packages" / "tomli_w" / "__init__.py"
    _spec = importlib.util.spec_from_file_location(
        "tomli_w", _shipped, submodule_search_locations=[str(_shipped.parent)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["tomli_w"] = _module
    _spec.loader.exec_module(_module)

# The maintainer tools are plain scripts in ``tools/``; their tests import them by name.
TOOLS_ROOT = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))


# Git does not store empty directories, so a fresh clone lacks the empty ``App``,
# ``Icons`` and ``Shortcuts`` folders of the fixture packages. Recreate them.
FIXTURE_VERSIONS = (
    "BadPathApp/1.0.0.local.1",
    "GoodApp/1.2.3.local.1",
    "MismatchApp/2.0.0.local.3",
    "NoConfigApp/0.9.0.local.1",
    "PwshApp/5.4.3.local.2",
)


def pytest_sessionstart(session: object) -> None:
    """Create the empty fixture directories that Git cannot track.

    Parameters
    ----------
    session : object
        The pytest session that is starting; unused.
    """
    fixtures = Path(__file__).resolve().parent / "fixtures"
    for version in FIXTURE_VERSIONS:
        for name in ("App", "Icons", "Shortcuts"):
            (fixtures / version / name).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# A real release ZIP, shared by the bootstrap, self-update and integrity tests
# ---------------------------------------------------------------------------
# Placeholder commit identifier recorded in the fake release bundles.

# Fixed commit hash recorded in the session's release bundle.
RELEASE_COMMIT = "a" * 40


class ReleaseBundle(NamedTuple):
    """The release ZIP built for the session and the facts the tests compare it with.

    Attributes
    ----------
    zip : Path
        Path of the release ZIP.
    sha256 : str
        SHA-256 of the ZIP, as lower-case hexadecimal.
    version : str
        Version directory name inside the release.
    commit : str
        Commit recorded in ``gupkg/RELEASE.txt``.
    """

    zip: Path
    sha256: str
    version: str
    commit: str


def write_runtime_zip(
    destination: Path, extra: dict[str, bytes | str] | None = None, packages: tuple[str, ...] = ()
) -> str:
    """Zip this checkout's committed CPython (without ``Lib/site-packages``) and return the ZIP's SHA-256.

    The archive stands in for the downloaded embeddable CPython, so a build can run
    the real ``python.exe`` without network access. *extra* adds ``{path: content}``
    entries, for example a planted ``Lib/site-packages/pip/__init__.py``. *packages* names
    committed ``site-packages`` folders to keep, such as ``tomli_w``, which the CLI needs
    to start (a ``--skip-deps`` build installs no dependency itself).

    Parameters
    ----------
    destination : Path
        Path of the ZIP file to write.
    extra : dict[str, bytes | str] | None, optional
        Additional ``{path: content}`` entries to add to the archive.
    packages : tuple[str, ...], optional
        Names of committed ``site-packages`` folders to keep in the archive.

    Returns
    -------
    str
        SHA-256 of the written archive, as lower-case hexadecimal.
    """
    # Zip every committed runtime file except the site-packages the caller did not ask for.
    runtime = Path(__file__).resolve().parents[1] / "src" / "gupkg" / "python"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(runtime.rglob("*")):
            relative = path.relative_to(runtime)
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if relative.parts[:2] != ("Lib", "site-packages") or relative.parts[2] in packages:
                archive.write(path, relative)
        # Add the caller's extra entries, then report the archive's digest.
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


@pytest.fixture(scope="session")
def release_bundle(tmp_path_factory: pytest.TempPathFactory) -> ReleaseBundle:
    """A release ZIP built by ``tools/build_standalone.py`` from this checkout.

    The embeddable runtime is this checkout's committed ``src/gupkg/python``
    without its dependencies (zipped so the tool treats it like a downloaded
    CPython archive) and ``--skip-deps`` is used, so the build needs no network. The commit recorded
    in ``gupkg/RELEASE.txt`` is the fixed ``RELEASE_COMMIT``. Tests that need
    a damaged or foreign release derive it from this one.

    Parameters
    ----------
    tmp_path_factory : pytest.TempPathFactory
        Pytest factory for session-wide temporary folders.

    Returns
    -------
    ReleaseBundle
        The built release ZIP with its location, digest and the commit recorded in it; built once per session.
    """
    # Build the bundle from a zipped runtime so the tool treats it like a downloaded CPython.
    work = tmp_path_factory.mktemp("release-bundle")
    runtime_zip = work / "runtime.zip"
    runtime_sha256 = write_runtime_zip(runtime_zip, packages=("tomli_w",))
    output = work / "bundle.zip"
    tool = Path(__file__).resolve().parents[1] / "tools" / "build_standalone.py"
    version = re.search(
        r'(?m)^__version__ = "([^"]+)"', (SOURCE_ROOT / "gupkg" / "_version.py").read_text(encoding="utf-8")
    )[1]
    # Run the real build tool offline, with a fixed commit so the bundle is reproducible.
    subprocess.run(
        [
            sys.executable, str(tool), "--skip-deps", "--runtime", str(runtime_zip),
            "--runtime-sha256", runtime_sha256,
            "--commit", RELEASE_COMMIT, "--output", str(output),
        ],
        check=True,
        capture_output=True,
    )
    return ReleaseBundle(output, hashlib.sha256(output.read_bytes()).hexdigest(), version, RELEASE_COMMIT)


@pytest.fixture
def derive_zip():
    """Return ``derive(source, target, drop=(), replace=None, add=None)``, which copies a release ZIP.

    Entries whose names end with a ``drop`` suffix are left out, the entry ending
    with ``replace[0]`` gets the bytes ``replace[1]``, and every ``add`` item
    (``{path below the version directory: bytes}``) is appended. The function returns
    the copy's SHA-256 (lower case), so a test can present a damaged release
    together with a matching download digest.

    Returns
    -------
    Callable[..., str]
        The ``derive`` function that copies a release ZIP with the requested changes.
    """

    def derive(source: Path, target: Path, *, drop=(), replace=None, add=None) -> str:
        """Copy the release ZIP *source* to *target* with the requested changes and return the copy's SHA-256.

        Parameters
        ----------
        source : Path
            Release ZIP to copy.
        target : Path
            Path of the copy to write.
        drop : object, optional
            Entry-name suffixes that are left out of the copy.
        replace : object, optional
            ``(suffix, bytes)`` pair that replaces the content of the entry ending with that suffix.
        add : object, optional
            ``{path below the version directory: bytes}`` entries appended to the copy.

        Returns
        -------
        str
            SHA-256 of the copy, as lower-case hexadecimal.
        """
        # Copy the source ZIP entry by entry, applying the requested damage.
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as copy:
            version_dir = next(
                name.rsplit("/", 1)[0] for name in original.namelist() if re.fullmatch(r"gupkg/[^/]+/pkg\.toml", name)
            )
            for entry in original.infolist():
                # Skip the dropped entries and substitute the replaced one while copying.
                if any(entry.filename.endswith(suffix) for suffix in drop):
                    continue
                data = original.read(entry)
                if replace and entry.filename.endswith(replace[0]):
                    data = replace[1]
                copy.writestr(entry.filename, data)
            # Append the requested extra entries below the version directory.
            for name, data in (add or {}).items():
                copy.writestr(f"{version_dir}/{name}", data)
        return hashlib.sha256(target.read_bytes()).hexdigest()

    return derive


@pytest.fixture
def runtime_zip_factory():
    """Return ``write_runtime_zip``: ``factory(destination, extra=None)`` writes a stand-in CPython ZIP, returning its SHA-256.

    Returns
    -------
    Callable[..., str]
        The ``write_runtime_zip`` function, which writes a stand-in CPython ZIP and returns its SHA-256.
    """
    return write_runtime_zip


# ---------------------------------------------------------------------------
# The machine boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def system(tmp_path: Path):
    """A ``FakeSystem`` whose pretend machine (Start Menu, bin folders, shortcut records) is ``tmp_path/machine``.

    Registry values live in memory, links and files are real but confined to the
    temporary directory, elevation is off, and any URL or process that a test
    does not register raises, so nothing reaches the real machine.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test, under which the machine is built.

    Returns
    -------
    FakeSystem
        A fresh fake machine confined to a temporary folder.
    """
    from gupkg.system.fake import FakeSystem

    return FakeSystem(tmp_path / "machine")


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    """An empty folder where unpack destinations are made; a failed unpack must leave it empty.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test.

    Returns
    -------
    Path
        An existing, empty folder under ``tmp_path``.
    """
    folder = tmp_path / "stage"
    folder.mkdir()
    return folder


# ---------------------------------------------------------------------------
# Pretend machines with an engine (one fixture for each kind of world)
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path):
    """A ``World``: a fake machine with a user root, a system root and an engine in user mode, under ``tmp_path``.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test, under which the machine is built.

    Returns
    -------
    World
        A fake machine with a user root, a system root and an engine in user mode.
    """
    from tests.support.engine import World

    return World(tmp_path)


@pytest.fixture
def elevated_world(tmp_path: Path):
    """A ``World`` whose fake machine reports an elevated process, so the engine runs in system mode.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test, under which the machine is built.

    Returns
    -------
    World
        A fake machine that reports an elevated process, with an engine in system mode.
    """
    from tests.support.engine import World

    return World(tmp_path, elevated=True)


@pytest.fixture
def cli_world(tmp_path: Path):
    """A ``CliWorld``: a ``World`` driven through ``gupkg.cli.main``, with a stand-in installed system gupkg.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test, under which the machine is built.

    Returns
    -------
    CliWorld
        A world driven through ``gupkg.cli.main``, with a stand-in installed system gupkg.
    """
    from tests.support.cli import CliWorld

    return CliWorld(tmp_path)


@pytest.fixture
def cli_elevated_world(tmp_path: Path):
    """A ``CliWorld`` whose fake machine reports an elevated process.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test, under which the machine is built.

    Returns
    -------
    CliWorld
        A CLI world whose fake machine reports an elevated process.
    """
    from tests.support.cli import CliWorld

    return CliWorld(tmp_path, elevated=True)


# ---------------------------------------------------------------------------
# The scripted ``gupkg`` of the UI program tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fake(tmp_path: Path):
    """A scripted ``gupkg`` double (``FakeGupkg``) with its own folders under ``tmp_path``; the test schedules its responses.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder of the test, under which the machine is built.

    Returns
    -------
    FakeGupkg
        A scripted gupkg double whose responses the test schedules.
    """
    from tests.support.ui import FakeGupkg

    return FakeGupkg(tmp_path)


@pytest.fixture
def recorded_fake(fake, tmp_path: Path):
    """``fake`` that answers ``list``, ``info`` and ``registry status`` with the recorded documents of the real CLI (two outdated packages).

    Parameters
    ----------
    fake : object
        The scripted gupkg double to extend.
    tmp_path : Path
        Temporary folder of the test.

    Returns
    -------
    FakeGupkg
        The same double, answering ``list``, ``info`` and ``registry status`` with recorded documents.
    """
    from tests.support.ui import fixture_json, registry_doc

    fake.respond("list", doc=fixture_json("list.json", tmp_path))
    fake.respond("info", doc=fixture_json("info_alpha.json", tmp_path))
    fake.respond("registry status", doc=registry_doc("4f2a9c1d00"))
    return fake


# ---------------------------------------------------------------------------
# The environment that decides how output looks
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def neutral_console_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the variables that change colour, width and encoding of output before every test.

    A developer's shell may set NO_COLOR, TERM, COLUMNS, WT_SESSION or Python's
    encoding variables; none of them may decide whether a test passes. A test
    that depends on one sets it itself.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture that undoes the environment changes after the test.
    """
    for name in (
        "NO_COLOR", "FORCE_COLOR", "TERM", "COLORTERM", "COLUMNS", "LINES", "WT_SESSION",
        "PYTHONIOENCODING", "PYTHONUTF8", "JUPYTER_COLUMNS", "TTY_COMPATIBLE", "TTY_INTERACTIVE",
    ):
        monkeypatch.delenv(name, raising=False)
    # The scripts a person starts wait for a key at the end; a test never waits.
    monkeypatch.setenv("GUPKG_NO_PAUSE", "1")


@pytest.fixture(autouse=True)
def no_ambient_elevation_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the launcher's ``GUPKG_ELEVATED`` marker before every test, so elevation comes only from the test's own system.

    ``is_elevated_run`` counts the marker as elevated; a developer's elevated shell may carry it. A test that needs an
    elevated environment sets it itself.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture that undoes the environment change after the test.
    """
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)
