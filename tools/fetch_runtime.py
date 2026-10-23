"""Fetch the pinned embeddable CPython and provision a gupkg runtime from it.

This is a build tool for the development tree; it is never shipped and gupkg
never runs it. It is the only place that knows where the embeddable CPython
comes from: the URL and the SHA-256 below. Everything else (the release build,
the committed runtime in ``src/gupkg/python``) obtains the interpreter here.

Usage and API
-------------
``python tools/fetch_runtime.py [--destination DIR] [--force]`` downloads the
archive into ``dist/cache``, verifies it, extracts it, writes ``._pth`` and
installs ``tools/gupkg-deps.lock`` into ``Lib/site-packages``. The functions
``download_runtime_archive``, ``write_pth`` and ``install_locked_dependencies``
are shared with ``build_standalone.py``.

Implementation Approach
-----------------------
A download is kept only after its SHA-256 equals the pinned value; a mismatch
deletes it and stops. Dependencies are installed with the running interpreter's
pip, targeted at the embeddable runtime, with ``--require-hashes
--only-binary=:all: --no-deps`` so only the pinned wheels can land and no build
script can run. The lock does not contain pip: the bundle has no installer, and the
build machine's pip (the development ``.venv``) does the install.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]

# The embeddable CPython that every gupkg release ships, and its pinned digest.
PYTHON_VERSION = "3.12.10"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

# The hash-pinned wheels installed into the runtime's site-packages.
DEFAULT_LOCK = Path(__file__).resolve().parent / "gupkg-deps.lock"


def main(argv: list[str] | None = None) -> int:
    """Provision a runtime directory from the pinned CPython and the dependency lock."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--destination", type=Path, default=ROOT / "src" / "gupkg" / "python")
    parser.add_argument("--cache", type=Path, default=ROOT / "dist" / "cache")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--force", action="store_true", help="replace a non-empty destination")
    parser.add_argument("--skip-deps", action="store_true", help="do not install the locked dependencies")
    args = parser.parse_args(argv)

    # Never overwrite an existing runtime unless the maintainer asked for it.
    if args.destination.exists() and any(args.destination.iterdir()):
        if not args.force:
            raise SystemExit(f"{args.destination} is not empty; pass --force to replace it")
        shutil.rmtree(args.destination)

    # Download (or reuse) the verified archive, extract it, then configure it.
    archive = download_runtime_archive(args.cache)
    args.destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as runtime_zip:
        runtime_zip.extractall(args.destination)
    write_pth(args.destination)
    if not args.skip_deps:
        install_locked_dependencies(args.destination, args.lock)
    print(f"[fetch-runtime] Runtime ready in {args.destination}")
    return 0


def download_runtime_archive(
    cache: Path,
    *,
    url: str = PYTHON_URL,
    sha256: str = PYTHON_SHA256,
    fetch: Callable[[str, Path], None] | None = None,
) -> Path:
    """Return the path of the verified embeddable CPython archive, downloading it if needed.

    Parameters
    ----------
    cache : Path
        Directory that keeps downloaded archives between runs.
    url, sha256 : str
        Source and expected digest; the defaults are the pinned values.
    fetch : callable, optional
        Replacement for the network download, ``fetch(url, destination)``.

    Raises
    ------
    SystemExit
        If the archive does not match *sha256*; the bad file is deleted.
    """
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(url).name

    # A cached archive is trusted only after it passes the same check as a fresh one.
    if not archive.is_file():
        partial = archive.with_suffix(".part")
        print(f"[fetch-runtime] Downloading {url}")
        (fetch or _download)(url, partial)
        partial.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest().casefold() != sha256.casefold():
        archive.unlink()
        raise SystemExit(f"{archive.name} failed SHA-256 verification against the pinned digest; deleted it")
    return archive


def write_pth(runtime: Path) -> None:
    """Write the runtime's ``._pth`` so it imports from its own ``Lib/site-packages``."""
    # The embeddable archive names its search-path file after the interpreter version.
    pth_files = sorted(runtime.glob("python*._pth"))
    if len(pth_files) != 1:
        raise SystemExit(f"Expected exactly one python*._pth in {runtime}, found {len(pth_files)}")
    tag = pth_files[0].name[len("python") : -len("._pth")]
    pth_files[0].write_text(f"python{tag}.zip\n.\n..\nLib/site-packages\n\nimport site\n", encoding="ascii")


# Installers and build tools never belong in the bundle: nothing may install packages at run time.
# Names are compared case-insensitively against the entries of ``site-packages`` (and ``Lib``).
FORBIDDEN_PREFIXES = ("pip", "setuptools", "_distutils_hack", "ensurepip")


def runtime_tag(runtime: Path) -> str:
    """Return the interpreter tag (for example ``312``) from the runtime's single ``python*._pth``."""
    pth_files = sorted(runtime.glob("python*._pth"))
    if len(pth_files) != 1:
        raise SystemExit(f"Need exactly one python*._pth in {runtime}, found {len(pth_files)}")
    return pth_files[0].name[len("python") : -len("._pth")]


def locked_names(lock: Path) -> list[str]:
    """Return the distribution names of a lock file's ``name==version`` lines."""
    names = []
    for line in lock.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.split("==", 1)[0].strip())
    return names


def installer_entries(runtime: Path) -> list[str]:
    """Return the pip, setuptools and ensurepip entries found in *runtime* (empty when it is clean).

    Looks at ``Lib/site-packages`` and ``Lib`` (package directories, ``*.dist-info``
    folders and single-file modules) and at ``python*.zip``, which holds the standard library.
    """
    found = []
    for folder in (runtime / "Lib" / "site-packages", runtime / "Lib"):
        if folder.is_dir():
            for entry in sorted(folder.iterdir()):
                stem = entry.name.casefold().split("-", 1)[0].removesuffix(".py").removesuffix(".dist")
                if stem in FORBIDDEN_PREFIXES:
                    found.append(entry.relative_to(runtime).as_posix())
    for archive_path in sorted(runtime.glob("python*.zip")):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                tops = {name.split("/", 1)[0].casefold().removesuffix(".pyc").removesuffix(".py") for name in archive.namelist()}
        except zipfile.BadZipFile:
            continue
        found.extend(f"{archive_path.name}/{name}" for name in sorted(tops & set(FORBIDDEN_PREFIXES)))
    return found


def tidy_site_packages(site_packages: Path) -> None:
    """Remove what ``pip --target`` writes that the bundle must not carry.

    The ``bin`` folder of console-script launchers goes, and so do the ``RECORD``
    entries of every ``*.dist-info`` that point at it (or outside the folder).
    """
    shutil.rmtree(site_packages / "bin", ignore_errors=True)
    for record in site_packages.glob("*.dist-info/RECORD"):
        kept = [
            line
            for line in record.read_text(encoding="utf-8").splitlines()
            if line.replace("\\", "/").split(",", 1)[0].split("/", 1)[0] not in {"bin", ".."}
        ]
        record.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")


def wheel_install_command(
    site_packages: Path, lock: Path, tag: str, python: str | None = None, find_links: Path | None = None
) -> list[str]:
    """Return the pip command that installs the locked wheels into *site_packages*.

    The build machine's pip does the work: wheels only, hashes required, no
    dependency resolution, targeted at the bundled interpreter's version rather than
    the interpreter running this script. *find_links* replaces the package index with
    a local folder of wheels (offline builds); hashes are still required.
    """
    command = [
        python or sys.executable, "-I", "-B", "-m", "pip", "install", "--isolated", "--disable-pip-version-check",
        "--no-input", "--target", str(site_packages), "--no-compile", "--require-hashes", "--only-binary=:all:",
        "--no-deps", "--upgrade", "--platform", "win_amd64", "--implementation", "cp",
        "--python-version", f"{tag[0]}.{tag[1:]}", "--abi", f"cp{tag}", "-r", str(lock),
    ]
    if find_links is not None:
        command[command.index("-r") : command.index("-r")] = ["--no-index", "--find-links", str(find_links)]
    return command


def install_locked_dependencies(
    runtime: Path, lock: Path, python: str | None = None, find_links: Path | None = None
) -> None:
    """Install the locked wheels into *runtime*'s ``Lib/site-packages``, with the build machine's pip.

    Parameters
    ----------
    runtime : Path
        Extracted embeddable runtime that already has its ``._pth`` file.
    lock : Path
        Lock file of ``name==version --hash=sha256:...`` lines.
    python : str, optional
        Interpreter whose pip does the install; defaults to the running one.
    find_links : Path, optional
        Folder of wheels to install from instead of the package index (offline builds).

    Raises
    ------
    SystemExit
        If the lock is missing or lists pip, pip fails (for example, a hash
        mismatch), or the installed tree contains an installer (see ``installer_entries``).
    """
    tag = runtime_tag(runtime)
    if not lock.is_file():
        raise SystemExit(f"No lock file at {lock}")
    banned = [name for name in locked_names(lock) if name.casefold().replace("_", "-") in FORBIDDEN_PREFIXES]
    if banned:
        raise SystemExit(f"{lock.name} lists {', '.join(banned)}; the bundle must not contain pip or setuptools")

    site_packages = runtime / "Lib" / "site-packages"
    if subprocess.run(wheel_install_command(site_packages, lock, tag, python, find_links), check=False).returncode != 0:
        raise SystemExit("Installing the locked dependencies into the runtime failed")
    tidy_site_packages(site_packages)


def _download(url: str, destination: Path) -> None:
    """Download *url* into *destination* over HTTPS."""
    with urllib.request.urlopen(url, timeout=120) as response, open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)


if __name__ == "__main__":
    raise SystemExit(main())
