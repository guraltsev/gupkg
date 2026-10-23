"""Make the checkout's source tree and maintainer tools importable, and share a real release ZIP."""

from __future__ import annotations

import hashlib
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
    runtime = Path(__file__).resolve().parents[1] / "src" / "gupkg" / "python"
    runtime_zip = work / "runtime.zip"
    with zipfile.ZipFile(runtime_zip, "w", zipfile.ZIP_STORED) as archive:
        for path in sorted(runtime.rglob("*")):
            relative = path.relative_to(runtime)
            if path.is_file() and "__pycache__" not in path.parts and relative.parts[:2] != ("Lib", "site-packages"):
                archive.write(path, relative)
    output = work / "bundle.zip"
    tool = Path(__file__).resolve().parents[1] / "tools" / "build_standalone.py"
    version = re.search(
        r'(?m)^__version__ = "([^"]+)"', (SOURCE_ROOT / "gupkg" / "_version.py").read_text(encoding="utf-8")
    )[1]
    subprocess.run(
        [
            sys.executable, str(tool), "--skip-deps", "--runtime", str(runtime_zip),
            "--runtime-sha256", hashlib.sha256(runtime_zip.read_bytes()).hexdigest(),
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
