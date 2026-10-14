"""Cover resolving a user-supplied path to a package version.

Protects package roots whose names start with ``v`` (``volta``, ``vlc``): they
must resolve to their version directory, not be mistaken for one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from gupkg.layout import resolve_input_path


@pytest.mark.parametrize("root_name", ["volta", "vlc"])
def test_v_prefixed_package_root_resolves_to_its_bootstrap_version(
    tmp_path: Path, root_name: str
) -> None:
    """A package root named like ``volta`` resolves to its only version directory."""
    root = tmp_path / root_name
    (root / "pkg.bootstrap").mkdir(parents=True)

    identity, from_current = resolve_input_path(root)

    assert identity.name == root_name
    assert identity.version_path == root / "pkg.bootstrap"
    assert not from_current
