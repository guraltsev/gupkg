"""Cover what happens when a package-local update hook needs a module that is not available.

Behavior covered: a hook that imports an unavailable module fails the update with a
message naming the module, the active version stays as it was, nothing is
installed and no ``pip`` process is started, whether the import happens when the
hook module loads, when the check runs, or while the payload is unpacked.

Real: the update coordinator, hook loading from ``pkg.local``, and the package
tree on disk. Stood in by ``FakeSystem``: HTTP, and the child-process boundary
(whose log proves that no installer starts, since every process gupkg starts goes
through it). Out of scope: the content of real packages' hooks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg import gupkg as workflows
from gupkg.system.fake import FakeSystem

MISSING = "gupkg_absent_module_for_tests"

CHECK_WITH_CANDIDATE = (
    "PKG_MODULE_API = 1\n"
    "def check_update(context):\n"
    '    return {"candidateId": "tool:2.0.0", "version": "2.0.0", "url": "https://example.invalid/tool.zip"}\n'
)
HOOKS = {
    "import-at-load": {
        "check.py": f"import {MISSING}\nPKG_MODULE_API = 1\ndef check_update(context):\n    return None\n",
        "unpack.py": "PKG_MODULE_API = 1\ndef unpack_app(context):\n    pass\n",
    },
    "import-in-check": {
        "check.py": f"PKG_MODULE_API = 1\ndef check_update(context):\n    import {MISSING}\n    return None\n",
        "unpack.py": "PKG_MODULE_API = 1\ndef unpack_app(context):\n    pass\n",
    },
    "import-in-unpack": {
        "check.py": CHECK_WITH_CANDIDATE,
        "unpack.py": f"PKG_MODULE_API = 1\ndef unpack_app(context):\n    import {MISSING}\n",
    },
}


def _package(root: Path, hooks: dict[str, str]) -> Path:
    """Create an installed ``Tool 1.0.0`` whose update hooks are the given sources."""
    version = root / "Tool" / "1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("old", encoding="utf-8")
    (version / "pkg.local").mkdir()
    for name, source in hooks.items():
        (version / "pkg.local" / name).write_text(source, encoding="utf-8")
    (version / "pkg.toml").write_text(
        '[update.check]\nmode = "module"\nmodule = "pkg.local/check.py"\n\n'
        '[update.payload]\nmode = "module"\nmodule = "pkg.local/unpack.py"\nignore_checksum = true\n',
        encoding="utf-8",
    )
    return version


@pytest.mark.parametrize("case", list(HOOKS), ids=list(HOOKS))
def test_hook_importing_an_unavailable_module_fails_the_update_naming_it(
    tmp_path: Path, system: FakeSystem, case: str
) -> None:
    """The update fails with the module's name, changes nothing, and starts no installer."""
    version = _package(tmp_path, HOOKS[case])
    system.add_url("https://example.invalid/tool.zip", b"zip")

    result = workflows.download_package_update(version, system)

    assert not result.ok
    assert any(MISSING in error for error in result.errors), result.errors
    assert any("installs nothing" in error for error in result.errors)
    assert system.process_log == []
    assert sorted(path.name for path in version.parent.iterdir() if not path.name.startswith(".")) == ["1.0.0"]
    assert (version / "App" / "tool.exe").read_text(encoding="utf-8") == "old"
