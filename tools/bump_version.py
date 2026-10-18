"""Set the gupkg version, which lives only in the runtime module.

``tools/bump_version.py 0.14.0`` rewrites ``src/gupkg/_version.py``; the
manifests carry no version. It does not commit; ``scripts/bump.cmd`` does.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    """Rewrite the three version declarations; return the process status."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="new version such as 0.14.0")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: this one)")
    args = parser.parse_args(argv)

    # Release rows and folder names assume plain dotted numbers.
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", args.version):
        print(f"Not a plain dotted version: {args.version!r}", file=sys.stderr)
        return 2

    # The version is declared on exactly one line; replace exactly that line.
    targets = (
        (args.root / "src" / "gupkg" / "_version.py", r'(?m)^__version__ = "[^"]*"', f'__version__ = "{args.version}"'),
    )
    for path, pattern, replacement in targets:
        text = path.read_text(encoding="utf-8")
        updated, count = re.subn(pattern, replacement, text, count=1)
        if count != 1:
            print(f"No version line found in {path}", file=sys.stderr)
            return 1
        path.write_text(updated, encoding="utf-8", newline="")
        print(f"{path.relative_to(args.root)}: {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
