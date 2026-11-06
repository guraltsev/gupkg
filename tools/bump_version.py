"""Set the gupkg version, which lives only in the runtime module.

The version is declared on exactly one line of ``src/gupkg/_version.py``; the
manifests carry no version. The tool never commits; ``_scripts/bump.cmd`` does.

Usage and API
-------------
``python tools/bump_version.py 0.14.0`` rewrites ``src/gupkg/_version.py``.
Instead of a version, ``patch`` (the default), ``minor`` or ``major`` raises
that part of the current version, and ``--print`` shows the current version
without changing anything. The command line delegates to ``main(...)``.

Implementation Approach
-----------------------
A requested version must be plain dotted numbers, because release rows and
folder names assume them. The single ``__version__`` line is located with an
anchored pattern and replaced in place, so the rest of the file is preserved
byte for byte.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Repository root used when ``--root`` is not given.
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    """Set or raise the gupkg version and return the process status.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments; ``sys.argv[1:]`` when omitted.

    Returns
    -------
    int
        ``0`` on success, ``1`` when no version line is found, ``2`` when the
        requested version is not plain dotted numbers.
    """
    # Describe the command line from the module summary.
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", nargs="?", default="patch", help="new version such as 0.14.0, or patch, minor, major")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="repository root (default: this one)")
    parser.add_argument("--print", action="store_true", dest="show", help="print the current version and change nothing")
    args = parser.parse_args(argv)

    # ``--print`` only reports the current version and leaves the file alone.
    version_file = args.root / "src" / "gupkg" / "_version.py"
    if args.show:
        found = re.search(r'(?m)^__version__ = "([^"]*)"', version_file.read_text(encoding="utf-8"))
        if found is None:
            print(f"No version line found in {version_file}", file=sys.stderr)
            return 1
        print(found.group(1))
        return 0

    # A bump keyword is turned into a concrete version before validation.
    if args.version in ("patch", "minor", "major"):
        args.version = _raised(version_file.read_text(encoding="utf-8"), args.version)
        if args.version is None:
            print(f"No plain dotted version found in {version_file}", file=sys.stderr)
            return 1

    # Release rows and folder names assume plain dotted numbers.
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", args.version):
        print(f"Not a plain dotted version: {args.version!r}", file=sys.stderr)
        return 2

    # The version is declared on exactly one line; replace exactly that line.
    targets = (
        (args.root / "src" / "gupkg" / "_version.py", r'(?m)^__version__ = "[^"]*"', f'__version__ = "{args.version}"'),
    )
    # Rewrite the line in place, keeping line endings, and report each change.
    for path, pattern, replacement in targets:
        text = path.read_text(encoding="utf-8")
        updated, count = re.subn(pattern, replacement, text, count=1)
        if count != 1:
            print(f"No version line found in {path}", file=sys.stderr)
            return 1
        path.write_text(updated, encoding="utf-8", newline="")
        print(f"{path.relative_to(args.root)}: {args.version}")
    return 0


def _raised(text: str, part: str) -> str | None:
    """Return the version in *text* with *part* (patch, minor or major) raised by one, or ``None``."""
    found = re.search(r'(?m)^__version__ = "([0-9]+)\.([0-9]+)\.([0-9]+)"', text)
    if found is None:
        return None
    major, minor, patch = (int(number) for number in found.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


if __name__ == "__main__":
    sys.exit(main())
