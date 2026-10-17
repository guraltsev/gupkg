"""Locate the separate ``gupkg-registry`` checkout for tests about package content.

The package definitions live in their own repository. Tests that read them look
for a checkout in ``gupkg-registry`` beside the sources, or in the folder named by
``GUPKG_REGISTRY_ROOT``, and are skipped when there is none.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REGISTRY_ROOT = Path(os.environ.get("GUPKG_REGISTRY_ROOT") or Path(__file__).resolve().parents[1] / "gupkg-registry")

requires_registry = pytest.mark.skipif(
    not REGISTRY_ROOT.is_dir(), reason="needs a gupkg-registry checkout (see tests/registry_location.py)"
)
