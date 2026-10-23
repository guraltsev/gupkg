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
script can run. The lock pins pip itself, so pip arrives from its pinned wheel.
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


def install_locked_dependencies(runtime: Path, lock: Path, python: str | None = None) -> None:
    """Install the locked wheels into *runtime*'s ``Lib/site-packages``.

    Parameters
    ----------
    runtime : Path
        Extracted embeddable runtime that already has its ``._pth`` file.
    lock : Path
        Lock file of ``name==version --hash=sha256:...`` lines.
    python : str, optional
        Interpreter whose pip does the install; defaults to the running one.

    Raises
    ------
    SystemExit
        If the lock is missing or pip fails (for example, a hash mismatch).
    """
    pth_files = sorted(runtime.glob("python*._pth"))
    if len(pth_files) != 1 or not lock.is_file():
        raise SystemExit(f"Need exactly one python*._pth in {runtime} and a lock file at {lock}")
    tag = pth_files[0].name[len("python") : -len("._pth")]

    # Wheels only, hashes required, targeted at the bundled interpreter's
    # version rather than at the interpreter running this script.
    site_packages = runtime / "Lib" / "site-packages"
    command = [
        python or sys.executable, "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input",
        "--target", str(site_packages), "--require-hashes", "--only-binary=:all:", "--no-deps",
        "--platform", "win_amd64", "--implementation", "cp", "--python-version", f"{tag[0]}.{tag[1:]}",
        "--abi", f"cp{tag}", "-r", str(lock),
    ]
    if subprocess.run(command, check=False).returncode != 0:
        raise SystemExit("Installing the locked dependencies into the runtime failed")

    # pip --target also writes console-script launchers; the bundle needs none.
    shutil.rmtree(site_packages / "bin", ignore_errors=True)


def _download(url: str, destination: Path) -> None:
    """Download *url* into *destination* over HTTPS."""
    with urllib.request.urlopen(url, timeout=120) as response, open(destination, "wb") as handle:
        shutil.copyfileobj(response, handle)


if __name__ == "__main__":
    raise SystemExit(main())
