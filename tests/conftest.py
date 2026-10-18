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
    "BadPathApp/1.0.0.local.1",
    "GoodApp/1.2.3.local.1",
    "MismatchApp/2.0.0.local.3",
    "NoConfigApp/0.9.0.local.1",
    "PwshApp/5.4.3.local.2",
)


def pytest_sessionstart(session: object) -> None:
    """Create the empty fixture directories that Git cannot track."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    for version in FIXTURE_VERSIONS:
        for name in ("App", "Icons", "Shortcuts"):
            (fixtures / version / name).mkdir(parents=True, exist_ok=True)
