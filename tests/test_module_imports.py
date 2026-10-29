"""Cover that every module of gupkg and the registry tool imports under the bundled interpreter.

Behavior covered: each module below ``src\\gupkg`` and ``src\\gupkg_registry`` imports on the committed
Python 3.12 started the way gupkg starts it (``-I -s -B -S``, the bundled ``Lib\\site-packages`` only), so no module still needs
code that was deleted (the 0.17 coordinators), a third-party package the bundle lacks, or syntax that Python 3.12 does not
read; importing writes no bytecode.

Real: the committed ``src\\gupkg\\python\\python.exe`` in a child process. Mocked: nothing. Out of scope: what the modules do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src"
PYTHON = SOURCE / "gupkg" / "python" / "python.exe"

SCRIPT = r"""
import importlib, pathlib, sys
source = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(source))
names = []
for package in ("gupkg", "gupkg_registry"):
    for path in sorted((source / package).rglob("*.py")):
        relative = path.relative_to(source)
        if "python" in relative.parts[:2] and relative.parts[0] == "gupkg" and relative.parts[1] == "python":
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        elif parts[-1] == "__main__":
            continue
        names.append(".".join(parts))
failed = []
for name in names:
    try:
        importlib.import_module(name)
    except BaseException as error:
        failed.append(f"{name}: {type(error).__name__}: {error}")
print(len(names))
for line in failed:
    print("FAILED", line)
sys.exit(1 if failed else 0)
"""


@pytest.mark.windows_integration
def test_every_module_imports_on_the_bundled_interpreter_without_writing_bytecode() -> None:
    """All modules import with -I -s -B -S on the committed Python; none needs deleted code, and no .pyc appears."""
    before = {path for path in SOURCE.rglob("*.pyc") if "python" not in path.relative_to(SOURCE).parts[:2]}

    result = subprocess.run(
        [str(PYTHON), "-I", "-s", "-B", "-S", "-c", SCRIPT, str(SOURCE)], capture_output=True, text=True, check=False, timeout=300
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert int(result.stdout.splitlines()[0]) > 100
    after = {path for path in SOURCE.rglob("*.pyc") if "python" not in path.relative_to(SOURCE).parts[:2]}
    assert after == before
