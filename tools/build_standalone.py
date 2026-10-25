"""Build the self-contained gupkg release ZIP and the integrity data that verifies it.

This is a build tool for the development tree; it is never shipped. A release
is one ZIP, ``gupkg-<version>-win-amd64.zip``, that already contains everything
gupkg needs to run: the embedded CPython, the locked dependencies, the native
shims and the application. The ZIP is a package-root fragment of the gupkg
package (design 21.2): ``gupkg\\<version>\\`` (``pkg.toml``, ``pkg.local``, the launchers
``install.cmd`` and ``gupkg-tui.cmd``, and the bundled tree ``gupkg\\``) and
``gupkg\\_check_updates\\`` (the tracker, its hook and ``check-updates.cmd``).
Hashes are computed here, once, and published with the bundle; installed code only checks them.

Usage and API
-------------
``python tools/build_standalone.py [--runtime ZIP] [--output ZIP]`` builds the
bundle (the pinned CPython is fetched and verified when ``--runtime`` is not
given), writes ``<zip>.sha256`` next to it and prints the ZIP's SHA-256.
``build_bundle(...)`` is the same operation for ``release.py``. ``--find-links DIR``
installs the locked wheels from a local folder instead of PyPI (offline builds;
hashes are still required).

Implementation Approach
-----------------------
1. The CPython archive is verified against the pinned digest and the wheels
   are installed with ``--require-hashes`` (see ``fetch_runtime.py``); a
   mismatch stops the build.
2. The package folder is assembled in a temporary folder: the bundled tree
   (``gupkg.cmd``, the ``gupkg`` package with its interpreter, the separate programs),
   the version directory around it and the tracker folder beside it.
3. ``gupkg/RELEASE.txt`` and ``gupkg/VERSION`` of the tree record the version and the
   commit, and ``gupkg/MANIFEST.sha256`` lists ``<sha256>  <relative path>`` for every
   other file of the tree (paths relative to the version directory), sorted by path.
   The manifest is written last.
4. The staged ``python\\python.exe -I -s -B`` runs the build probe: it imports
   every top-level package of the lock (by import name) and the gupkg modules,
   and confirms pip and ensurepip are not importable. A bundle that contains
   pip, setuptools or ensurepip, or whose probe fails, is never zipped.
5. The tree is zipped and the ZIP's SHA-256 is reported.

The bundle has no pip: packages get into it only here, installed by the build
machine's pip (the development ``.venv``) from hash-pinned wheels.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_runtime  # noqa: E402  (a sibling script, not an installed package)

ROOT = Path(__file__).resolve().parents[1]

# Imported by the build probe when the bundle's dependencies are installed: the command line and every
# module it reaches, so a module that needs something the bundle lacks fails the build. The UI programs
# of issue 030 (``gupkg_ui``, ``gupkg_tui``, ``gupkg_manager``) and the migration tool are probed through their entry modules; any other
# package directory named in ``OPTIONAL_PROBE_PACKAGES`` is probed as soon as it exists beside ``gupkg`` in the version directory.
PROBE_MODULES = (
    "gupkg.cli", "gupkg_migrate.cli",
    "gupkg.services.track", "gupkg.services.unpack", "gupkg.services.prepare", "gupkg.hooklib",
    "gupkg.services.artifact", "gupkg.model.manager_policy", "gupkg.pinning",
    "gupkg.engine.lifecycle", "gupkg.model.plan", "gupkg_migrate.adopt", "gupkg.engine.locks", "gupkg.engine.installer",
    "gupkg.redact", "gupkg.authoring", "gupkg.settingsedit", "gupkg.viewdata", "gupkg.report.results",
    "gupkg.selfmgmt.location", "gupkg.selfmgmt.status", "gupkg.services.registrysource", "gupkg.catalog.registry",
    "gupkg_ui.client", "gupkg_ui.app", "gupkg_manager.cli", "gupkg_tui.cli",
)
OPTIONAL_PROBE_PACKAGES = ("gupkg_ui", "gupkg_tui", "gupkg_manager")
# Modules of the base probe that need no third-party package (used by --skip-deps builds).
# ``gupkg.cli`` is not among them any more: the CLI writes TOML with tomli_w (issue 023).
BARE_PROBE_MODULES = ("gupkg.core", "gupkg.model.version", "gupkg.model.settings")


def main(argv: list[str] | None = None) -> int:
    """Build the release ZIP from a checked-out source tree and report its digest."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runtime", type=Path, help="embeddable CPython ZIP (default: fetch the pinned one)")
    parser.add_argument("--runtime-sha256", default=fetch_runtime.PYTHON_SHA256, help="digest the runtime must have")
    parser.add_argument("--source", type=Path, default=ROOT / "src" / "gupkg")
    parser.add_argument("--manifest", type=Path, default=ROOT / "src" / "pkg.toml", help="the tracker pkg.toml of the gupkg package")
    parser.add_argument("--version", help="assert the version declared in _version.py")
    parser.add_argument("--commit", help="commit recorded in RELEASE.txt (default: HEAD)")
    parser.add_argument("--output", type=Path, help="default: dist/gupkg-<version>-win-amd64.zip")
    parser.add_argument("--deps-lock", type=Path, default=fetch_runtime.DEFAULT_LOCK)
    parser.add_argument("--skip-deps", action="store_true", help="do not install locked dependencies (offline test builds)")
    parser.add_argument("--python", help="interpreter whose pip installs the wheels (default: the one running this tool)")
    parser.add_argument("--find-links", type=Path, help="install the locked wheels from this folder instead of PyPI (offline builds)")
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
        find_links=args.find_links,
        python=args.python,
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
    find_links: Path | None = None,
    python: str | None = None,
    probe_modules: tuple[str, ...] | None = None,
) -> str:
    """Assemble the release ZIP at *output* and return its SHA-256.

    Parameters
    ----------
    source : Path
        The ``gupkg`` package directory (``src/gupkg``).
    manifest : Path
        The tracker ``pkg.toml`` of the gupkg package (``src/pkg.toml``); the version directory's own ``pkg.toml`` is derived from it.
    runtime_zip : Path
        Embeddable CPython archive; verified against *runtime_sha256*.
    commit : str
        Commit recorded in ``gupkg/RELEASE.txt``.
    deps_lock : Path
        Hash-pinned dependency lock, installed unless *skip_deps*.
    skip_deps : bool, default=False
        Leave the runtime without dependencies (offline test builds only); the
        probe then imports only what needs no third-party package.
    find_links : Path, optional
        Folder of wheels installed instead of those from PyPI (offline builds).
    python : str, optional
        Interpreter whose pip installs the wheels (the build machine's); defaults
        to the running one.
    probe_modules : tuple of str, optional
        The gupkg modules the build probe imports besides the lock's packages
        (default: ``PROBE_MODULES``; tests with stand-in wheels name fewer).

    Returns
    -------
    str
        Lower-case SHA-256 of the written ZIP; also saved in ``<output>.sha256``.

    Raises
    ------
    SystemExit
        If the digest does not match, the lock lists pip, the tree contains pip,
        setuptools or ensurepip, or the build probe fails. Nothing is written then.
    """
    # Step 1: the interpreter must be exactly the pinned (or stated) archive.
    actual = hashlib.sha256(runtime_zip.read_bytes()).hexdigest()
    if actual.casefold() != runtime_sha256.casefold():
        raise SystemExit("embedded runtime digest does not match --runtime-sha256")
    version = source_version(source)

    # Step 2: assemble the package folder of the ZIP in a temporary folder: ``gupkg\\<version>\\`` (the version directory)
    # with the bundled tree ``gupkg\\`` inside it, and the tracker folder ``gupkg\\_check_updates\\`` beside it.
    with tempfile.TemporaryDirectory(prefix="gupkg-standalone-") as temporary:
        top = Path(temporary) / "gupkg"
        root = top / version
        tree = root / "gupkg"
        payload = tree / "gupkg"
        payload.mkdir(parents=True)

        # Install-specific and generated files never belong in a release.
        ignored = shutil.ignore_patterns("python", "__pycache__", "*.pyc", "gupkg-config.toml", "digest.txt", "pkg.toml")
        shutil.copytree(source, payload, dirs_exist_ok=True, ignore=ignored)

        # Materialize the audited native launchers and their relocatable
        # relative configurations as package-owned support files, one for each
        # command whose ``.cmd`` launcher is in the package (the UI programs of
        # issue 030 add theirs).
        shim = source / "shim" / "shim-console.static.exe"  # static: no exe-shim-lib folder is needed beside it
        commands = [name for name in ("gupkg", "gupkg-tui", "gupkg-manager", "gupkg-migrate") if (payload / f"{name}.cmd").is_file()]
        if shim.is_file():
            for command in commands:
                shutil.copy2(shim, payload / f"{command}.exe")
        for command in commands:
            (payload / f"{command}.config.toml").write_text(
                'target = "%COMSPEC%"\nforward_arguments = true\nelevate = false\n\n'
                '[[argument]]\nvalue = "/d"\n\n[[argument]]\nvalue = "/s"\n\n[[argument]]\nvalue = "/c"\n\n'
                f'[[argument]]\nvalue = "{command}.cmd"\n',
                encoding="utf-8",
            )

        # The commands of the tree: what the root forwarder, the shims and the package launchers call (design 21.2).
        for wrapper_name in ("gupkg.cmd", "gupkg-tui.cmd", "gupkg-manager.cmd"):
            wrapper = source.parent / wrapper_name
            if wrapper.is_file():
                shutil.copy2(wrapper, tree / wrapper_name)

        # The separate programs (the migration tool, the UI programs and their shared library) sit beside the ``gupkg``
        # package in the tree (design section 12).
        for program in ("gupkg_migrate", "gupkg_ui", "gupkg_tui", "gupkg_manager"):
            program_dir = source.parent / program
            if program_dir.is_dir():
                shutil.copytree(program_dir, tree / program, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # The package files of gupkg's own package: the version is the tracker's pkg.toml without ``[track]`` (fully pinned,
        # no download_url), its populate script and the launchers; the tracker keeps its own hook and check-updates.cmd.
        tracker_text = manifest.read_text(encoding="utf-8")
        (root / "pkg.toml").write_text(version_manifest_text(tracker_text), encoding="utf-8", newline="\n")
        (top / "_check_updates").mkdir()
        (top / "_check_updates" / "pkg.toml").write_text(tracker_text, encoding="utf-8", newline="\n")
        hooks = source.parent / "pkg.local"
        if hooks.is_dir():
            ignore_pycache = shutil.ignore_patterns("__pycache__", "*.pyc")
            shutil.copytree(hooks, top / "_check_updates" / "pkg.local", ignore=ignore_pycache)
            shutil.copytree(hooks, root / "pkg.local", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "track.py"))
        launchers = load_launchers(source)
        launchers.write_launchers(root)
        launchers.write_launchers(top / "_check_updates", (launchers.TRACKER_LAUNCHER,))

        # The interpreter, configured to import from its own site-packages,
        # then the locked dependencies (every wheel is hash-checked by pip).
        with zipfile.ZipFile(runtime_zip) as archive:
            archive.extractall(payload / "python")
        fetch_runtime.write_pth(payload / "python")
        if not skip_deps:
            fetch_runtime.install_locked_dependencies(
                payload / "python", deps_lock, python=python, find_links=find_links
            )
        # Whatever the source (a stale or foreign runtime archive, a transitive
        # dependency), the bundle must not carry an installer.
        installers = fetch_runtime.installer_entries(payload / "python")
        if installers:
            raise SystemExit(
                f"The bundle would contain {', '.join(installers)}; the runtime ships without pip and setuptools"
            )

        # No bytecode ships: it is not release content, installed Python runs with -B,
        # and a stale .pyc would be trusted by its source's size and mtime alone.
        for leftover in list(payload.rglob("__pycache__")):
            shutil.rmtree(leftover, ignore_errors=True)
        for leftover in list(top.rglob("*.pyc")):
            leftover.unlink()

        # Step 3: record where the bundle came from (its version and commit), then its manifest, last.
        (tree / "RELEASE.txt").write_text(f"gupkg {version}\ncommit {commit}\n", encoding="utf-8", newline="\n")
        (tree / "VERSION").write_text(f"{version}\n", encoding="utf-8", newline="\n")
        write_manifest(root)

        # Step 4: the last check before zipping runs the staged interpreter on the staged tree.
        run_build_probe(tree, deps_lock, skip_deps=skip_deps, modules=probe_modules)

        # Step 5: zip the package folder in a fixed order and report the ZIP's digest.
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(top.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(top.parent))
    zip_digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + ".sha256").write_text(f"{zip_digest}  {output.name}\n", encoding="ascii")
    return zip_digest


def import_names(site_packages: Path, lock: Path) -> list[str]:
    """Return the top-level import names of every distribution in *lock*, read from its ``RECORD``.

    Raises
    ------
    SystemExit
        If a locked distribution is not installed or its ``RECORD`` names no module.
    """
    names: list[str] = []
    for line in lock.read_text(encoding="utf-8").replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        distribution = re.sub(r"[-_.]+", "_", line.split("==", 1)[0]).casefold()
        records = [
            folder / "RECORD"
            for folder in site_packages.glob("*.dist-info")
            if re.sub(r"[-_.]+", "_", folder.name[: -len(".dist-info")].rsplit("-", 1)[0]).casefold() == distribution
        ]
        if not records:
            raise SystemExit(f"Locked distribution {line.split('==', 1)[0]} is not installed in the bundle")
        found = set()
        for entry in records[0].read_text(encoding="utf-8").splitlines():
            first = entry.replace("\\", "/").split(",", 1)[0].split("/", 1)[0]
            if first.endswith((".dist-info", ".data")) or first in {"", "..", "bin", "__pycache__"}:
                continue
            # Only code counts: a package directory with __init__.py, or a .py/.pyd module file.
            # A stray LICENSE or README at the top of the RECORD is not a module.
            path = site_packages / first
            if path.is_dir() and (path / "__init__.py").is_file():
                module = first
            elif path.is_file() and first.endswith((".py", ".pyd")):
                module = first.split(".", 1)[0]
            else:
                continue
            if module.isidentifier():
                found.add(module)
        if not found:
            raise SystemExit(f"The RECORD of {line.split('==', 1)[0]} names no importable module")
        names.extend(sorted(found))
    return names


def run_build_probe(
    root: Path, lock: Path, *, skip_deps: bool = False, modules: tuple[str, ...] | None = None
) -> None:
    """Run the staged interpreter on the staged tree and fail the build if it cannot import it.

    The probe runs ``python\\python.exe -I -s -B``. It imports every top-level
    package of the lock (by import name) and the gupkg modules, asserts that
    ``pip`` and ``ensurepip`` are not importable, and prints the failures. With
    *skip_deps* (no dependencies installed) only the modules that need no
    third-party package are imported. *modules* replaces the default gupkg module
    list. The probe must leave no bytecode behind.

    Raises
    ------
    SystemExit
        If anything fails to import, pip or ensurepip is importable, or the probe
        wrote bytecode into the tree.
    """
    payload = root / "gupkg"
    interpreter = payload / "python" / "python.exe"
    if skip_deps:
        names = list(BARE_PROBE_MODULES)
    else:
        names = [
            *import_names(payload / "python" / "Lib" / "site-packages", lock),
            *(PROBE_MODULES if modules is None else modules),
        ]
        names += [name for name in OPTIONAL_PROBE_PACKAGES if (root / name).is_dir()]
    code = (
        "import importlib, importlib.util, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "failed = []\n"
        "for name in sys.argv[2:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except BaseException as exc:\n"
        "        failed.append(f'import {name}: {type(exc).__name__}: {exc}')\n"
        "for banned in ('pip', 'ensurepip'):\n"
        "    if importlib.util.find_spec(banned) is not None:\n"
        "        failed.append(f'{banned} is importable')\n"
        "print('\\n'.join(failed))\n"
        "raise SystemExit(1 if failed else 0)\n"
    )
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith(("PYTHON", "PIP_"))}
    result = subprocess.run(
        [str(interpreter), "-I", "-s", "-B", "-c", code, str(root), *names],
        cwd=str(payload / "python"), env=environment, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip() or f"exit status {result.returncode}"
        raise SystemExit(f"The build probe failed on the staged bundle:\n{detail}")
    stray = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.pyc"))
    if stray:
        raise SystemExit(f"The build probe wrote bytecode into the bundle: {', '.join(stray[:5])}")


def write_manifest(root: Path) -> None:
    """Write ``gupkg/MANIFEST.sha256`` for every file of the bundled tree ``<root>/gupkg``, sorted by path.

    *root* is the version directory; the entries are relative to it (``gupkg/gupkg/cli.py``), so the manifest names only the
    tree. ``pkg.toml``, ``pkg.local`` and the launchers of the version directory are not listed: ``check-updates`` writes
    them itself and the registry fingerprint covers them.
    """
    tree = root / "gupkg"
    manifest = tree / "MANIFEST.sha256"
    entries = []
    for path in tree.rglob("*"):
        if path.is_file() and path != manifest:
            entries.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    lines = [f"{digest}  {relative}" for relative, digest in sorted(entries)]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def version_manifest_text(tracker_text: str) -> str:
    """Return the ``pkg.toml`` of a pinned version from the text of the package's tracker: the same file without ``[track]``.

    The tracker of gupkg's own package carries no template and no checksum, so dropping its ``[track]`` table (and the comment
    lines right above it) leaves the pinned version that ``check-updates`` would render.
    """
    kept: list[str] = []
    skipping = False
    for line in tracker_text.splitlines():
        if re.match(r"\[track\]\s*(#.*)?$", line):
            skipping = True
            while kept and kept[-1].startswith("#"):
                kept.pop()
            continue
        if skipping and re.match(r"\[", line):
            skipping = False
        if not skipping:
            kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


def load_launchers(source: Path):
    """Load ``gupkg/launchers`` of the source tree by path (it needs nothing but the standard library)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("gupkg_launchers_build", source / "launchers" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
