"""Validate a package registry checkout (default: ``gupkg-registry`` beside the sources)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gupkg.registry import validate_registry_tree  # noqa: E402


def main() -> int:
    """Validate one registry checkout and print its selectors."""
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[1] / "gupkg-registry")
    args = parser.parse_args()
    packages = validate_registry_tree(args.root)
    for package in packages:
        print(f"{package.selector}\t{package.version_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
