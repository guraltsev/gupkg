"""Assemble a relocatable standalone source package for release automation.

The script deliberately does not download Python or execute package-local
hooks. CI supplies a verified embedded-Python archive and its SHA-256 digest,
then this script copies the application and release metadata into a versioned
payload for the native bootstrap artifact.

The bundled runtime is made self-sufficient for elevated runs, which never
install anything: the script installs the hash-pinned wheels from
``gupkg-deps.lock`` into ``python/Lib/site-packages`` and writes the runtime's
``._pth`` file. Pass ``--skip-deps`` only for offline test builds.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path


def _source_version(source: Path) -> str:
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


def _install_locked_dependencies(runtime: Path, lock: Path) -> None:
    """Install the locked wheels into the extracted runtime and enable its site-packages."""
    # The embeddable archive names its search-path file after the interpreter
    # version (python312._pth); the same name identifies the wheel tags.
    pth_files = sorted(runtime.glob("python*._pth"))
    if len(pth_files) != 1 or not lock.is_file():
        raise SystemExit(f"Need exactly one python*._pth in the runtime and a lockfile at {lock}")
    tag = pth_files[0].name[len("python") : -len("._pth")]
    pth_files[0].write_text(f"python{tag}.zip\n.\n..\nLib/site-packages\n\nimport site\n", encoding="ascii")

    # Wheels only, hashes required, targeted at the bundled interpreter rather
    # than the interpreter running this script.
    site_packages = runtime / "Lib" / "site-packages"
    command = [
        sys.executable, "-m", "pip", "install", "--isolated", "--disable-pip-version-check", "--no-input",
        "--target", str(site_packages), "--require-hashes", "--only-binary=:all:", "--no-deps",
        "--platform", "win_amd64", "--implementation", "cp", "--python-version", f"{tag[0]}.{tag[1:]}",
        "--abi", f"cp{tag}", "-r", str(lock),
    ]
    if subprocess.run(command, check=False).returncode != 0:
        raise SystemExit("Installing the locked dependencies into the runtime failed")
    # pip --target also writes console-script launchers; the bundle needs none.
    shutil.rmtree(site_packages / "bin", ignore_errors=True)


def main() -> int:
    """Build a standalone ZIP from a checked-out source tree and runtime ZIP."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--source", type=Path, default=Path("src/gupkg"))
    parser.add_argument("--manifest", type=Path, default=Path("src/gupkg/pkg.toml"))
    parser.add_argument("--version")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--deps-lock", type=Path, help="default: gupkg-deps.lock inside --source")
    parser.add_argument("--skip-deps", action="store_true", help="do not install locked dependencies")
    args = parser.parse_args()
    # The runtime module owns the release number; an optional command-line
    # value is only an assertion for release scripts, never an override.
    release_version = _source_version(args.source)
    if args.version is not None and args.version != release_version:
        raise SystemExit(
            f"--version {args.version!r} does not match the runtime version {release_version!r}"
        )
    manifest_data = tomllib.loads(args.manifest.read_text(encoding="utf-8"))
    if manifest_data.get("version") != release_version:
        raise SystemExit(
            f"standalone manifest version {manifest_data.get('version')!r} "
            f"does not match runtime version {release_version!r}"
        )
    digest = hashlib.sha256(args.runtime.read_bytes()).hexdigest()
    if digest.casefold() != args.runtime_sha256.casefold():
        raise SystemExit("embedded runtime digest does not match --runtime-sha256")
    with tempfile.TemporaryDirectory(prefix="gupkg-standalone-") as temporary:
        root = Path(temporary) / f"v{release_version}"
        payload = root / "gupkg"
        payload.mkdir(parents=True)
        shutil.copytree(args.source, payload, dirs_exist_ok=True, ignore=shutil.ignore_patterns("python", "__pycache__"))
        # Materialize the audited native launchers and their relocatable
        # relative configurations as package-owned support files.
        shim = args.source / "shim" / "shim-console.exe"
        if shim.is_file():
            shutil.copy2(shim, payload / "gupkg.exe")
            shutil.copy2(shim, payload / "gupkg-tui.exe")
        (payload / "gupkg.config.toml").write_text(
            'target = "%COMSPEC%"\nforward_arguments = true\nelevate = false\n\n[[argument]]\nvalue = "/d"\n\n[[argument]]\nvalue = "/s"\n\n[[argument]]\nvalue = "/c"\n\n[[argument]]\nvalue = "gupkg.cmd"\n',
            encoding="utf-8",
        )
        # No gupkg-config.toml is shipped: manager settings belong to the
        # administrator ('gupkg manager init'), never inside the immutable version.
        (payload / "gupkg-tui.config.toml").write_text(
            'target = "%COMSPEC%"\nforward_arguments = true\nelevate = false\n\n[[argument]]\nvalue = "/d"\n\n[[argument]]\nvalue = "/s"\n\n[[argument]]\nvalue = "/c"\n\n[[argument]]\nvalue = "gupkg-tui.cmd"\n',
            encoding="utf-8",
        )
        for wrapper_name in ("gupkg.cmd", "gupkg-tui.cmd"):
            wrapper = args.source.parent / wrapper_name
            if wrapper.is_file():
                shutil.copy2(wrapper, root / wrapper_name)
        shutil.copy2(args.manifest, root / "pkg.toml")
        # The update hooks sit beside the manifest, not inside the payload.
        hooks = args.source.parent / "pkg.local"
        if hooks.is_dir():
            shutil.copytree(hooks, root / "pkg.local", ignore=shutil.ignore_patterns("__pycache__"))
        with zipfile.ZipFile(args.runtime) as runtime_zip:
            runtime_zip.extractall(payload / "python")
        if not args.skip_deps:
            _install_locked_dependencies(payload / "python", args.deps_lock or args.source / "gupkg-deps.lock")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in root.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(root.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
