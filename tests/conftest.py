"""Make the checkout's installable source tree importable for pytest."""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


# Git does not store empty directories, so a fresh clone lacks the empty ``App``,
# ``Icons`` and ``Shortcuts`` folders of the fixture packages. Recreate them.
FIXTURE_VERSIONS = (
    "BadPathApp/v1.0.0.l1",
    "GoodApp/v1.2.3.l1",
    "MismatchApp/v2.0.0.l3",
    "NoConfigApp/v0.9.0.l1",
    "PwshApp/v5.4.3.l2",
)


def pytest_sessionstart(session: object) -> None:
    """Create the empty fixture directories that Git cannot track."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    for version in FIXTURE_VERSIONS:
        for name in ("App", "Icons", "Shortcuts"):
            (fixtures / version / name).mkdir(parents=True, exist_ok=True)
