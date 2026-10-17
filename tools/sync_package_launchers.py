"""Give every package seed of the registry checkout the lightweight ``gupkg.cmd`` launcher.

The launcher is ``src/gupkg.cmd``: it runs a gupkg installed beside it (in a
``gupkg`` subfolder) and otherwise the ``gupkg.exe`` found on ``PATH``, so a
package folder can be managed with ``gupkg.cmd update`` and similar commands.
Run without options to copy it into each seed of ``gupkg-registry``; ``--check`` only
reports seeds whose copy is missing or differs, and exits with status 1.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "src" / "gupkg.cmd"


def seed_directories(registry: Path) -> list[Path]:
    """Return each package's version-seed directory (``pkg.bootstrap`` or ``v<version>``)."""
    seeds = []
    for selector in sorted(path for path in registry.iterdir() if path.is_dir()):
        seeds.extend(
            sorted(child for child in selector.iterdir() if child.is_dir() and (child / "pkg.toml").is_file())
        )
    return seeds


def main() -> int:
    """Copy or verify the launcher in every seed; return the process status."""
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", nargs="?", type=Path, default=ROOT / "gupkg-registry")
    parser.add_argument("--check", action="store_true", help="report differences instead of copying")
    args = parser.parse_args()

    # Compare bytes so line endings count: the launcher must be identical everywhere.
    expected = LAUNCHER.read_bytes().replace(b"\r\n", b"\n")
    stale = []
    for seed in seed_directories(args.registry):
        target = seed / "gupkg.cmd"
        if target.is_file() and target.read_bytes().replace(b"\r\n", b"\n") == expected:
            continue
        stale.append(target)
        if not args.check:
            shutil.copyfile(LAUNCHER, target)
    for target in stale:
        print(f"{'missing or different' if args.check else 'updated'}: {target.relative_to(ROOT)}")
    return 1 if (stale and args.check) else 0


if __name__ == "__main__":
    sys.exit(main())
