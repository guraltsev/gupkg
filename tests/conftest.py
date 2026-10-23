"""Make the checkout's source tree and maintainer tools importable, and share a real release ZIP."""

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


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

# tomli_w is the one TOML writer (issue 019 locks it). A dev .venv made before that lock
# lacks it, so load the very copy the release bundle ships, and only that package.
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
    """Create the empty fixture directories that Git cannot track."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    for version in FIXTURE_VERSIONS:
        for name in ("App", "Icons", "Shortcuts"):
            (fixtures / version / name).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# A real release ZIP, shared by the bootstrap, self-update and integrity tests
# ---------------------------------------------------------------------------

RELEASE_COMMIT = "a" * 40


class ReleaseBundle(NamedTuple):
    """The release ZIP built for the session and the facts the tests compare it with."""

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
    """
    runtime = Path(__file__).resolve().parents[1] / "src" / "gupkg" / "python"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(runtime.rglob("*")):
            relative = path.relative_to(runtime)
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if relative.parts[:2] != ("Lib", "site-packages") or relative.parts[2] in packages:
                archive.write(path, relative)
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
    """
    work = tmp_path_factory.mktemp("release-bundle")
    runtime_zip = work / "runtime.zip"
    runtime_sha256 = write_runtime_zip(runtime_zip, packages=("tomli_w",))
    output = work / "bundle.zip"
    tool = Path(__file__).resolve().parents[1] / "tools" / "build_standalone.py"
    version = re.search(
        r'(?m)^__version__ = "([^"]+)"', (SOURCE_ROOT / "gupkg" / "_version.py").read_text(encoding="utf-8")
    )[1]
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
    (``{path below the version tree: bytes}``) is appended. The function returns
    the copy's SHA-256 (lower case), so a test can present a damaged release
    together with a matching download digest.
    """

    def derive(source: Path, target: Path, *, drop=(), replace=None, add=None) -> str:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as copy:
            top = original.namelist()[0].split("/")[0]
            for entry in original.infolist():
                if any(entry.filename.endswith(suffix) for suffix in drop):
                    continue
                data = original.read(entry)
                if replace and entry.filename.endswith(replace[0]):
                    data = replace[1]
                copy.writestr(entry.filename, data)
            for name, data in (add or {}).items():
                copy.writestr(f"{top}/{name}", data)
        return hashlib.sha256(target.read_bytes()).hexdigest()

    return derive


@pytest.fixture
def runtime_zip_factory():
    """Return ``write_runtime_zip``: ``factory(destination, extra=None)`` writes a stand-in CPython ZIP, returning its SHA-256."""
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
    """
    from gupkg.system.fake import FakeSystem

    return FakeSystem(tmp_path / "machine")


# ---------------------------------------------------------------------------
# The environment that decides how output looks
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def neutral_console_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the variables that change colour, width and encoding of output before every test.

    A developer's shell may set NO_COLOR, TERM, COLUMNS, WT_SESSION or Python's
    encoding variables; none of them may decide whether a test passes. A test
    that depends on one sets it itself.
    """
    for name in (
        "NO_COLOR", "FORCE_COLOR", "TERM", "COLORTERM", "COLUMNS", "LINES", "WT_SESSION",
        "PYTHONIOENCODING", "PYTHONUTF8", "JUPYTER_COLUMNS", "TTY_COMPATIBLE", "TTY_INTERACTIVE",
    ):
        monkeypatch.delenv(name, raising=False)
