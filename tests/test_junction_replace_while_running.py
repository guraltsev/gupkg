"""Replacing a junction that the running program's own library lives behind (``gupkg`` updating itself through ``current``).

Behavior covered: ``WindowsSystem.create_junction`` replaces an existing junction even when the Python that runs it loads
its modules through that junction, so the first import after the old junction is gone must not be needed.
Boundaries: real NTFS junctions, real child Python process; nothing is mocked.
Out of scope: the rest of the update (fetch, unpack, integration).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="NTFS junctions")

_SRC = Path(__file__).resolve().parents[1] / "src"


def _version(root: Path, name: str) -> Path:
    """Make a version directory whose ``enc.zip`` holds the codec module the child will import lazily."""
    folder = root / name
    folder.mkdir()
    import encodings.utf_16_le as codec

    with zipfile.ZipFile(folder / "enc.zip", "w") as archive:
        archive.write(codec.__file__, "encodings/utf_16_le.py")
        archive.write(Path(codec.__file__).with_name("utf_8_sig.py"), "encodings/utf_8_sig.py")
    return folder


def test_replacing_the_junction_the_program_runs_from_keeps_the_old_target_reachable_until_the_new_one_is_made(tmp_path: Path) -> None:
    """The junction ends up pointing at the new version when the library is read through the junction being replaced.

    The child puts a zip behind the junction on the codec search path, loads one module from it (so the zip is open)
    and replaces the junction while it has not used the UTF-16 codec yet. Removing the junction before the codec is needed makes the
    replacement fail and leaves no junction at all, which is how ``current`` of an installed gupkg was lost.
    """
    old, new = _version(tmp_path, "1.0"), _version(tmp_path, "2.0")
    current = tmp_path / "current"
    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_SRC)!r})
        from pathlib import Path
        import encodings
        from gupkg.system.windows import WindowsSystem

        current = Path({str(current)!r})
        system = WindowsSystem()
        encodings.__path__.insert(0, str(current / "enc.zip" / "encodings"))
        import encodings.utf_8_sig
        assert "encodings.utf_16_le" not in sys.modules
        system.create_junction(current, Path({str(new)!r}))
        """
    )
    subprocess.run(["cmd", "/c", "mklink", "/J", str(current), str(old)], check=True, capture_output=True)
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stderr
    assert os.path.realpath(current) == os.path.realpath(new)
