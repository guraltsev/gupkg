"""Cover the lightweight launcher shipped inside every registry package.

Behavior covered: each package in the checked-in registry carries the same
``gupkg.cmd`` launcher as the source tree, so a package folder can run its
local gupkg and otherwise a system one; and the checked-in registry validates.
Real: the repository's ``pkgs`` tree and the registry validator. Out of scope:
what the launcher does when run (covered by the wrapper-script tests).
"""

from __future__ import annotations

from pathlib import Path

from gupkg.registry import validate_registry_tree

ROOT = Path(__file__).resolve().parents[1]


def test_every_registry_package_ships_the_lightweight_launcher() -> None:
    """Each package seed has a gupkg.cmd identical to src/gupkg.cmd."""
    expected = (ROOT / "src" / "gupkg.cmd").read_bytes()
    seeds = sorted(path.parent for path in (ROOT / "pkgs").glob("*/*/pkg.toml"))

    assert seeds
    wrong = [
        f"{seed.parent.name}/{seed.name}"
        for seed in seeds
        if not (seed / "gupkg.cmd").is_file() or (seed / "gupkg.cmd").read_bytes() != expected
    ]
    assert wrong == []


def test_checked_in_registry_validates() -> None:
    """The official package tree passes registry validation, launchers included."""
    assert validate_registry_tree(ROOT / "pkgs")
