"""Build the self-contained gupkg release ZIP and the integrity data that verifies it.

This is a build tool for the development tree; it is never shipped. A release
is one ZIP, ``gupkg-<version>-win-amd64.zip``, that already contains everything
gupkg needs to run: the embedded CPython, the locked dependencies, the native
shims and the application. Hashes are computed here, once, and published with
the bundle; installed code only checks them.

Usage and API
-------------
``python tools/build_standalone.py [--runtime ZIP] [--output ZIP]`` builds the
bundle (the pinned CPython is fetched and verified when ``--runtime`` is not
given), writes ``<zip>.sha256`` next to it and prints the ZIP's SHA-256.
``build_bundle(...)`` is the same operation for ``release.py``.

Implementation Approach
-----------------------
1. The CPython archive is verified against the pinned digest and the wheels
   are installed with ``--require-hashes`` (see ``fetch_runtime.py``); a
   mismatch stops the build.
2. The version-directory tree is assembled in a temporary folder.
3. ``gupkg/RELEASE.txt`` records the version and the commit, and
   ``gupkg/MANIFEST.sha256`` lists ``<sha256>  <relative path>`` for every
   other file in the tree, sorted by path. The manifest is written last.
4. The tree is zipped and the ZIP's SHA-256 is reported.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_runtime  # noqa: E402  (a sibling script, not an installed package)

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    """Build the release ZIP from a checked-out source tree and report its digest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runtime", type=Path, help="embeddable CPython ZIP (default: fetch the pinned one)")
    parser.add_argument("--runtime-sha256", default=fetch_runtime.PYTHON_SHA256, help="digest the runtime must have")
    parser.add_argument("--source", type=Path, default=ROOT / "src" / "gupkg")
    parser.add_argument("--manifest", type=Path, default=ROOT / "src" / "gupkg" / "pkg.toml")
    parser.add_argument("--version", help="assert the version declared in _version.py")
    parser.add_argument("--commit", help="commit recorded in RELEASE.txt (default: HEAD)")
    parser.add_argument("--output", type=Path, help="default: dist/gupkg-<version>-win-amd64.zip")
    parser.add_argument("--deps-lock", type=Path, default=fetch_runtime.DEFAULT_LOCK)
    parser.add_argument("--skip-deps", action="store_true", help="do not install locked dependencies (offline test builds)")
    args = parser.parse_args(argv)

    # The runtime module owns the release number; an optional command-line
    # value is only an assertion for release scripts, never an override.
    release_version = source_version(args.source)
    if args.version is not None and args.version != release_version:
        raise SystemExit(f"--version {args.version!r} does not match the runtime version {release_version!r}")
    output = args.output or ROOT / "dist" / f"gupkg-{release_version}-win-amd64.zip"
    runtime = args.runtime or fetch_runtime.download_runtime_archive(ROOT / "dist" / "cache")

    # Record the commit the bundle is built from, so a release can refuse a stale ZIP.
    commit = args.commit or subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    digest = build_bundle(
        source=args.source,
        manifest=args.manifest,
        runtime_zip=runtime,
        runtime_sha256=args.runtime_sha256,
        output=output,
        commit=commit,
        deps_lock=args.deps_lock,
        skip_deps=args.skip_deps,
    )
    print(f"[build] {output}")
    print(f"[build] sha256 {digest}")
    return 0


def build_bundle(
    *,
    source: Path,
    manifest: Path,
    runtime_zip: Path,
    runtime_sha256: str,
    output: Path,
    commit: str,
    deps_lock: Path,
    skip_deps: bool = False,
) -> str:
    """Assemble the release ZIP at *output* and return its SHA-256.

    Parameters
    ----------
    source : Path
        The ``gupkg`` package directory (``src/gupkg``).
    manifest : Path
        The ``pkg.toml`` that becomes the version directory's manifest.
    runtime_zip : Path
        Embeddable CPython archive; verified against *runtime_sha256*.
    commit : str
        Commit recorded in ``gupkg/RELEASE.txt``.
    deps_lock : Path
        Hash-pinned dependency lock, installed unless *skip_deps*.
    skip_deps : bool, default=False
        Leave the runtime without dependencies (offline test builds only).

    Returns
    -------
    str
        Lower-case SHA-256 of the written ZIP; also saved in ``<output>.sha256``.
    """
    # Step 1: the interpreter must be exactly the pinned (or stated) archive.
    actual = hashlib.sha256(runtime_zip.read_bytes()).hexdigest()
    if actual.casefold() != runtime_sha256.casefold():
        raise SystemExit("embedded runtime digest does not match --runtime-sha256")
    version = source_version(source)

    # Step 2: assemble the version-directory tree in a temporary folder.
    with tempfile.TemporaryDirectory(prefix="gupkg-standalone-") as temporary:
        root = Path(temporary) / f"gupkg-{version}"
        payload = root / "gupkg"
        payload.mkdir(parents=True)

        # Install-specific and generated files never belong in a release.
        ignored = shutil.ignore_patterns("python", "__pycache__", "*.pyc", "gupkg-config.toml", "digest.txt")
        shutil.copytree(source, payload, dirs_exist_ok=True, ignore=ignored)

        # Materialize the audited native launchers and their relocatable
        # relative configurations as package-owned support files.
        shim = source / "shim" / "shim-console.exe"
        if shim.is_file():
            shutil.copy2(shim, payload / "gupkg.exe")
            shutil.copy2(shim, payload / "gupkg-tui.exe")
        for command in ("gupkg", "gupkg-tui"):
            (payload / f"{command}.config.toml").write_text(
                'target = "%COMSPEC%"\nforward_arguments = true\nelevate = false\n\n'
                '[[argument]]\nvalue = "/d"\n\n[[argument]]\nvalue = "/s"\n\n[[argument]]\nvalue = "/c"\n\n'
                f'[[argument]]\nvalue = "{command}.cmd"\n',
                encoding="utf-8",
            )
        for wrapper_name in ("gupkg.cmd", "gupkg-tui.cmd"):
            wrapper = source.parent / wrapper_name
            if wrapper.is_file():
                shutil.copy2(wrapper, root / wrapper_name)
        shutil.copy2(manifest, root / "pkg.toml")

        # The update hooks sit beside the manifest, not inside the payload.
        hooks = source.parent / "pkg.local"
        if hooks.is_dir():
            shutil.copytree(hooks, root / "pkg.local", ignore=shutil.ignore_patterns("__pycache__"))

        # The interpreter, configured to import from its own site-packages,
        # then the locked dependencies (every wheel is hash-checked by pip).
        with zipfile.ZipFile(runtime_zip) as archive:
            archive.extractall(payload / "python")
        fetch_runtime.write_pth(payload / "python")
        if not skip_deps:
            fetch_runtime.install_locked_dependencies(payload / "python", deps_lock)

        # Step 3: record where the bundle came from, then its manifest, last.
        (payload / "RELEASE.txt").write_text(f"gupkg {version}\ncommit {commit}\n", encoding="utf-8", newline="\n")
        write_manifest(root)

        # Step 4: zip the tree in a fixed order and report the ZIP's digest.
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(root.parent))
    zip_digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + ".sha256").write_text(f"{zip_digest}  {output.name}\n", encoding="ascii")
    return zip_digest


def write_manifest(root: Path) -> None:
    """Write ``gupkg/MANIFEST.sha256`` for every other file below *root*, sorted by path."""
    manifest = root / "gupkg" / "MANIFEST.sha256"
    entries = []
    for path in root.rglob("*"):
        if path.is_file() and path != manifest:
            entries.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = [f"{digest}  {relative}" for relative, digest in sorted(entries)]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def source_version(source: Path) -> str:
    """Read the release value from the runtime's single version module."""
    version_source = source / "_version.py"
    tree = ast.parse(version_source.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise ValueError(f"No string __version__ found in {version_source}")


if __name__ == "__main__":
    raise SystemExit(main())
