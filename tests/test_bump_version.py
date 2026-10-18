"""Cover the version bump that sets the one place the version is declared.

Behavior covered: one command sets the version in the runtime module, leaves
other lines and the manifests alone, and rejects anything but a dotted number.
Real: the tool and temporary files laid out like the repository.
Out of scope: committing, which the scripts do.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "bump_version.py"
MANIFEST = '[[bin]]\nversion = "keep"\n'


def _load():
    """Load the bump tool as a module."""
    spec = importlib.util.spec_from_file_location("bump_version_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> None:
    """Create the runtime version module and a manifest that must stay untouched."""
    (root / "src" / "gupkg").mkdir(parents=True)
    (root / "src" / "gupkg" / "_version.py").write_text('"""Version."""\n\n__version__ = "0.1.0"\n', encoding="utf-8")
    (root / "src" / "pkg.toml").write_text(MANIFEST, encoding="utf-8")


def test_bump_sets_the_runtime_version_and_nothing_else(tmp_path: Path) -> None:
    """The version module gets the new version; the manifest is unchanged."""
    _tree(tmp_path)

    assert _load().main(["0.2.5", "--root", str(tmp_path)]) == 0

    assert '__version__ = "0.2.5"' in (tmp_path / "src" / "gupkg" / "_version.py").read_text(encoding="utf-8")
    assert (tmp_path / "src" / "pkg.toml").read_text(encoding="utf-8") == MANIFEST


def test_bump_rejects_a_version_that_is_not_dotted_numbers(tmp_path: Path) -> None:
    """A suffix such as -rc1 is refused and no file changes."""
    _tree(tmp_path)

    assert _load().main(["1.0-rc1", "--root", str(tmp_path)]) == 2
    assert '"0.1.0"' in (tmp_path / "src" / "gupkg" / "_version.py").read_text(encoding="utf-8")
