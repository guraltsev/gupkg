"""Copy the package folders of a registry checkout into ``tests/fixtures/registry_100``.

The registry is a separate repository (``gupkg-registry``). The tests of this repository need
its real packages, so they run against a committed snapshot of them: every folder that is a
package (no leading ``.`` or ``_``) and ``_registry.toml``. Run this after the registry
changes, review the diff, and commit it with the test changes it causes.

Usage and API
-------------
``python tools/snapshot_registry.py <path to gupkg-registry>`` replaces the snapshot.
``python tools/snapshot_registry.py <path> --check`` changes nothing and exits 1 when the
snapshot differs from the registry.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

SNAPSHOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "registry_100"
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak")


def package_folders(registry: Path) -> list[Path]:
    """Return the package folders of *registry*, sorted by name."""
    return sorted(p for p in registry.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def differs(registry: Path, snapshot: Path) -> list[str]:
    """Return what differs between *registry* and *snapshot* (names, relative to the root)."""
    problems: list[str] = []
    wanted = {p.name for p in package_folders(registry)}
    have = {p.name for p in package_folders(snapshot)} if snapshot.is_dir() else set()
    problems += [f"missing from snapshot: {name}" for name in sorted(wanted - have)]
    problems += [f"not in registry: {name}" for name in sorted(have - wanted)]

    def walk(left: Path, right: Path, label: str) -> None:
        comparison = filecmp.dircmp(left, right, ignore=["__pycache__"])
        problems.extend(f"{label}/{name} only in registry" for name in comparison.left_only)
        problems.extend(f"{label}/{name} only in snapshot" for name in comparison.right_only)
        for name in comparison.common_files:
            if (left / name).read_bytes() != (right / name).read_bytes():
                problems.append(f"{label}/{name} differs")
        for name in comparison.common_dirs:
            walk(left / name, right / name, f"{label}/{name}")

    for name in sorted(wanted & have):
        walk(registry / name, snapshot / name, name)
    for file in ("_registry.toml",):
        left, right = registry / file, snapshot / file
        if left.is_file() != right.is_file() or (left.is_file() and left.read_bytes() != right.read_bytes()):
            problems.append(f"{file} differs")
    return problems


def main(argv: list[str] | None = None) -> int:
    """Refresh or check the snapshot; return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("registry", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        problems = differs(args.registry, SNAPSHOT)
        print("\n".join(problems) or "snapshot is current")
        return 1 if problems else 0
    if SNAPSHOT.exists():
        shutil.rmtree(SNAPSHOT)
    SNAPSHOT.mkdir(parents=True)
    for folder in package_folders(args.registry):
        shutil.copytree(folder, SNAPSHOT / folder.name, ignore=IGNORED)
    if (args.registry / "_registry.toml").is_file():
        shutil.copyfile(args.registry / "_registry.toml", SNAPSHOT / "_registry.toml")
    print(f"snapshot written: {len(package_folders(SNAPSHOT))} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
