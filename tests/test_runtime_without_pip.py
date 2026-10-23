"""Cover the pip-free bundled runtime: the build that keeps pip out, and the probe that proves it.

Behavior covered: the committed runtime (``src/gupkg/python``) holds the locked
dependencies including ``tomli_w`` and no pip, setuptools or ensurepip, and
``python.exe -I -m pip`` in it fails with ``No module named pip``; the build
installs stand-in wheels with the build machine's pip, removes ``bin`` and the
``RECORD`` entries that point at it, and writes a ZIP without pip; the build stops
(and writes no ZIP) when the lock lists pip, when a wheel or the runtime archive
brings pip, setuptools or ensurepip, or when a locked package does not import; the
build probe imports every locked package by import name and the gupkg modules on
the committed runtime and the real lock; and no shipped file starts pip.

Real: the build tool, the pip of the build machine (offline: ``--no-index`` with
hash-pinned stand-in wheels written at test time), the committed ``python.exe`` and
site-packages, and the files. Mocked: nothing. Out of scope: PyPI and real wheel
downloads (a manual check in ``tests/manual_smoke.md``), and the runtime updater
tool (``test_update_deps_tool.py``).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import build_standalone
import fetch_runtime
import update_deps
from gupkg.integrity import verify_version_directory

REPO = Path(__file__).resolve().parents[1]
RUNTIME = REPO / "src" / "gupkg" / "python"
SITE_PACKAGES = RUNTIME / "Lib" / "site-packages"
SOURCE = REPO / "src" / "gupkg"
LOCK = REPO / "tools" / "gupkg-deps.lock"
COMMIT = "b" * 40


@pytest.fixture(scope="session")
def build_python() -> str:
    """The interpreter whose pip installs wheels: this one, or its base interpreter when it has pip."""
    for candidate in (Path(sys.executable), Path(sys.base_prefix) / "python.exe"):
        if candidate.is_file():
            found = subprocess.run([str(candidate), "-m", "pip", "--version"], capture_output=True, check=False)
            if found.returncode == 0:
                return str(candidate)
    pytest.fail("These tests need the build machine's pip: create the development .venv with pip")


def make_wheel(folder: Path, name: str, files: dict[str, str], *, script: str | None = None) -> str:
    """Write ``<name>-1.0-py3-none-any.whl`` into *folder* and return its lock line."""
    folder.mkdir(parents=True, exist_ok=True)
    dist_info = f"{name}-1.0.dist-info"
    entries = dict(files)
    entries[f"{dist_info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n"
    entries[f"{dist_info}/WHEEL"] = "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    if script:
        entries[f"{dist_info}/entry_points.txt"] = f"[console_scripts]\n{script} = {name}:main\n"
    entries[f"{dist_info}/RECORD"] = "".join(f"{path},,\n" for path in [*entries, f"{dist_info}/RECORD"])
    wheel = folder / f"{name}-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in entries.items():
            archive.writestr(path, text)
    return f"{name}==1.0 --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n"


def build(tmp_path: Path, build_python: str, runtime_zip_factory, lock_text: str, *, runtime_extra=None, find_links=None, skip_deps=False) -> Path:
    """Run ``build_bundle`` offline on the committed CPython and return the ZIP path (it may not exist)."""
    runtime = tmp_path / "runtime.zip"
    digest = runtime_zip_factory(runtime, runtime_extra)
    lock = tmp_path / "gupkg-deps.lock"
    lock.write_text(lock_text, encoding="utf-8")
    output = tmp_path / "out.zip"
    build_standalone.build_bundle(
        source=SOURCE,
        manifest=SOURCE / "pkg.toml",
        runtime_zip=runtime,
        runtime_sha256=digest,
        output=output,
        commit=COMMIT,
        deps_lock=lock,
        skip_deps=skip_deps,
        find_links=find_links,
        python=build_python,
        probe_modules=("gupkg.cli",),
    )
    return output


def test_committed_runtime_has_the_locked_packages_and_no_installer() -> None:
    """The committed site-packages holds exactly the locked distributions (tomli_w included) and no pip."""
    locked = {re.sub(r"[-_.]+", "_", item.name).casefold() for item in update_deps.read_lock(LOCK)}
    installed = {
        re.sub(r"[-_.]+", "_", folder.name[: -len(".dist-info")].rsplit("-", 1)[0]).casefold()
        for folder in SITE_PACKAGES.glob("*.dist-info")
    }

    assert installed == locked
    assert "tomli_w" in locked
    assert fetch_runtime.installer_entries(RUNTIME) == []


def test_git_tracks_no_pip_file_in_the_committed_runtime() -> None:
    """No tracked path under the committed runtime's site-packages belongs to pip."""
    listing = subprocess.run(
        ["git", "ls-files", "src/gupkg/python"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    assert listing
    assert [path for path in listing if re.search(r"site-packages/(pip|setuptools|_distutils_hack)", path)] == []


def test_the_bundled_python_cannot_import_pip() -> None:
    """``python.exe -I -m pip`` in the committed runtime fails with 'No module named pip'."""
    result = subprocess.run(
        [str(RUNTIME / "python.exe"), "-I", "-B", "-m", "pip", "--version"],
        capture_output=True, text=True, check=False, cwd=RUNTIME,
    )

    assert result.returncode != 0
    assert "No module named pip" in result.stderr


def test_import_names_come_from_the_installed_records() -> None:
    """Every locked distribution of the committed runtime yields its import names (modules and packages)."""
    names = build_standalone.import_names(SITE_PACKAGES, LOCK)

    assert {"textual", "tomli_w", "rich", "markdown_it", "linkify_it", "mdit_py_plugins", "mdurl",
            "platformdirs", "pygments", "typing_extensions"} <= set(names)
    assert not {"pip", "__pycache__", "bin"} & set(names)


def test_the_probe_passes_on_the_committed_runtime_with_the_real_lock_and_default_modules(tmp_path: Path) -> None:
    """On the committed runtime and the real lock, the default probe imports every package and gupkg UI module."""
    root = tmp_path / "gupkg-test"
    shutil.copytree(SOURCE, root / "gupkg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    build_standalone.run_build_probe(root, LOCK)

    assert list(root.rglob("*.pyc")) == []


@pytest.mark.parametrize("planted", ["pip/__init__.py", "ensurepip/__init__.py"])
def test_the_probe_fails_when_pip_or_ensurepip_is_importable(tmp_path: Path, planted: str) -> None:
    """A pip (or ensurepip) package importable from the staged runtime fails the probe, naming it."""
    root = tmp_path / "gupkg-test"
    shutil.copytree(SOURCE, root / "gupkg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = root / "gupkg" / "python" / "Lib" / "site-packages" / planted
    target.parent.mkdir()
    target.write_text("", encoding="utf-8")

    with pytest.raises(SystemExit, match=planted.split("/")[0] + " is importable"):
        build_standalone.run_build_probe(root, LOCK)


def test_the_probe_fails_when_a_locked_package_does_not_import(tmp_path: Path) -> None:
    """A locked package whose import raises fails the probe and the message names the module."""
    root = tmp_path / "gupkg-test"
    shutil.copytree(SOURCE, root / "gupkg", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (root / "gupkg" / "python" / "Lib" / "site-packages" / "tomli_w" / "__init__.py").write_text(
        "raise RuntimeError('damaged')\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit, match="import tomli_w: RuntimeError: damaged"):
        build_standalone.run_build_probe(root, LOCK)


def test_the_build_installs_stand_in_wheels_without_pip_and_the_probe_imports_them(
    tmp_path: Path, build_python: str, runtime_zip_factory
) -> None:
    """Wheels are installed with the build machine's pip; the ZIP has the packages, no pip, no bin, and verifies."""
    wheels = tmp_path / "wheels"
    lock = make_wheel(wheels, "fakedep", {"fakedep/__init__.py": "VALUE = 1\n"}, script="fakecmd")
    lock += make_wheel(wheels, "fakesingle", {"fakesingle.py": "VALUE = 2\n"})

    output = build(tmp_path, build_python, runtime_zip_factory, lock, find_links=wheels)

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        archive.extractall(tmp_path / "unpacked")
    site_packages = "gupkg/python/Lib/site-packages/"
    assert any(name.endswith(site_packages + "fakedep/__init__.py") for name in names)
    assert any(name.endswith(site_packages + "fakesingle.py") for name in names)
    assert not any(re.search(r"site-packages/(pip|setuptools|_distutils_hack|bin/)", name) for name in names)
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    unpacked = next((tmp_path / "unpacked").iterdir())
    record = (unpacked / "gupkg" / "python" / "Lib" / "site-packages" / "fakedep-1.0.dist-info" / "RECORD").read_text(
        encoding="utf-8"
    )
    assert "fakecmd" not in record and "fakedep/__init__.py" in record
    assert verify_version_directory(unpacked).ok


def test_the_build_fails_when_a_locked_package_does_not_import(tmp_path: Path, build_python: str, runtime_zip_factory) -> None:
    """The build probe imports each locked package; a broken one stops the build before any ZIP is written."""
    wheels = tmp_path / "wheels"
    lock = make_wheel(wheels, "brokendep", {"brokendep/__init__.py": "raise ImportError('no good')\n"})

    with pytest.raises(SystemExit, match="import brokendep: ImportError: no good"):
        build(tmp_path, build_python, runtime_zip_factory, lock, find_links=wheels)

    assert not (tmp_path / "out.zip").exists()


def test_the_build_fails_naming_pip_when_the_lock_lists_it(tmp_path: Path, build_python: str, runtime_zip_factory) -> None:
    """A wheel set that includes pip fails the build with a message naming pip, and no ZIP is written."""
    wheels = tmp_path / "wheels"
    lock = make_wheel(wheels, "fakedep", {"fakedep/__init__.py": ""})
    lock += make_wheel(wheels, "pip", {"pip/__init__.py": ""})

    with pytest.raises(SystemExit, match="pip"):
        build(tmp_path, build_python, runtime_zip_factory, lock, find_links=wheels)

    assert not (tmp_path / "out.zip").exists()


@pytest.mark.parametrize(
    "files",
    [
        pytest.param({"pip/__init__.py": ""}, id="pip"),
        pytest.param({"setuptools/__init__.py": ""}, id="setuptools"),
        pytest.param({"_distutils_hack/__init__.py": ""}, id="distutils-hack"),
    ],
)
def test_the_build_fails_when_a_wheel_brings_an_installer_along(tmp_path: Path, build_python: str, runtime_zip_factory, files) -> None:
    """A lock that does not name pip is not enough: an installer inside any wheel's files fails the build."""
    wheels = tmp_path / "wheels"
    lock = make_wheel(wheels, "carrier", {"carrier/__init__.py": "", **files})

    with pytest.raises(SystemExit, match=re.escape(next(iter(files)).split("/")[0].split("-")[0])):
        build(tmp_path, build_python, runtime_zip_factory, lock, find_links=wheels)

    assert not (tmp_path / "out.zip").exists()


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"Lib/site-packages/pip/__init__.py": ""}, id="stale-site-packages-pip"),
        pytest.param({"Lib/site-packages/pip-26.2.1.dist-info/METADATA": ""}, id="stale-pip-dist-info"),
        pytest.param({"Lib/site-packages/setuptools/__init__.py": ""}, id="stale-setuptools"),
        pytest.param({"Lib/ensurepip/__init__.py": ""}, id="ensurepip-in-lib"),
    ],
)
def test_the_build_fails_when_the_runtime_archive_already_contains_an_installer(
    tmp_path: Path, build_python: str, runtime_zip_factory, extra: dict[str, str]
) -> None:
    """A stale or foreign runtime archive with pip, setuptools or ensurepip fails the build, even with --skip-deps."""
    with pytest.raises(SystemExit, match="pip|setuptools|ensurepip"):
        build(tmp_path, build_python, runtime_zip_factory, "", runtime_extra=extra, skip_deps=True)

    assert not (tmp_path / "out.zip").exists()


def test_tidy_removes_script_launchers_and_their_record_entries(tmp_path: Path) -> None:
    """The ``bin`` folder and every RECORD line that points at it (or outside the folder) are removed."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "tool.exe").write_bytes(b"x")
    info = tmp_path / "dep-1.0.dist-info"
    info.mkdir()
    (info / "RECORD").write_text(
        "dep/__init__.py,sha256=abc,1\n../bin/tool.exe,sha256=def,2\nbin/tool.exe,,\n"
        "dep-1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )

    fetch_runtime.tidy_site_packages(tmp_path)

    assert not (tmp_path / "bin").exists()
    assert (info / "RECORD").read_text(encoding="utf-8") == "dep/__init__.py,sha256=abc,1\ndep-1.0.dist-info/RECORD,,\n"


def test_install_command_uses_the_build_machines_pip_and_requires_hashes(tmp_path: Path) -> None:
    """The install is cross-targeted at the bundled CPython, wheels only, hash-checked, and may read a local folder."""
    command = fetch_runtime.wheel_install_command(
        tmp_path / "site", tmp_path / "lock", "312", python="C:/dev/python.exe", find_links=tmp_path / "wheels"
    )

    assert command[:5] == ["C:/dev/python.exe", "-I", "-B", "-m", "pip"]
    for required in ("--require-hashes", "--only-binary=:all:", "--no-deps", "--no-compile", "--no-index"):
        assert required in command
    assert command[command.index("--python-version") + 1] == "3.12"
    assert command[command.index("--platform") + 1] == "win_amd64"


SHIPPED = (
    *sorted((REPO / "src" / "gupkg").glob("*.py")),
    *sorted((REPO / "src" / "pkg.local").glob("*.py")),
    *sorted((REPO / "src").glob("*.cmd")),
    *sorted((REPO / "src" / "gupkg").glob("*.cmd")),
    REPO / "gupkg-bootstrap.ps1",
    REPO / "gupkg-bootstrap.cmd",
)
PIP_USE = re.compile(r"-m\s+pip\b|\bimport\s+pip\b|\bfrom\s+pip\b|get-pip|pip\s+install", re.IGNORECASE)


def test_no_shipped_file_starts_or_imports_pip() -> None:
    """The shipped sources, hooks and launchers contain no ``-m pip``, ``import pip``, ``get-pip`` or ``pip install``."""
    assert len(SHIPPED) > 20
    offenders = [
        f"{path.relative_to(REPO)}: {match.group(0)}"
        for path in SHIPPED
        for match in PIP_USE.finditer(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


@pytest.mark.parametrize("text", ["python -m pip install x", "import pip", "from pip import main", "Invoke get-pip.py", "pip  install"])
def test_the_shipped_tree_scan_recognises_pip_use(text: str) -> None:
    """The pattern behind the shipped-tree scan matches each way of starting or importing pip."""
    assert PIP_USE.search(text)
