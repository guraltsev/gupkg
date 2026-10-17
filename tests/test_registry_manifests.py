"""Cover text mistakes in the checked-in registry manifests that break paths.

Behavior covered: no value in any package manifest contains a control
character, which is what a Windows path like ``"$App\npbackup.exe"`` turns into
when its backslash is not doubled in a TOML basic string.
Real: the repository's ``pkgs`` manifests. Out of scope: whether the named files exist.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests.registry_location import REGISTRY_ROOT, requires_registry

pytestmark = requires_registry

ROOT = Path(__file__).resolve().parents[1]


def _control_characters(value: object, where: str = ""):
    """Yield (location, text) for every string below *value* that holds a control character."""
    if isinstance(value, str):
        if any(ord(char) < 32 for char in value):
            yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _control_characters(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _control_characters(item, f"{where}[{index}]")


def test_no_registry_manifest_value_contains_a_control_character() -> None:
    """Escaped-backslash mistakes such as \"$App\npbackup.exe\" are caught before release."""
    problems = []
    for manifest in sorted(REGISTRY_ROOT.glob("*/*/pkg.toml")):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        problems += [
            f"{manifest.parent.parent.name}{where}: {text!r}" for where, text in _control_characters(data)
        ]
    assert problems == []
