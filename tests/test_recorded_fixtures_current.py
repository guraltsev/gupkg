"""Cover that the recorded CLI documents under ``tests/fixtures`` are what the current CLI prints (issue 035 T13).

Behavior covered: the two recorders (``tests/fixtures/ui/record.py`` and ``tests/fixtures/cli_docs/record.py``) are run again into a temporary
folder, with the real ``gupkg.cmd`` of this tree on a pretend machine (``--sandbox``, no network), and each ``.json`` document they write equals the committed one
after the run timestamps of log names (``20261029T142430Z``) are replaced by one word. The events files (``*.jsonl``) are not compared: they hold
elapsed times and random stop-marker names. A document that changed on purpose is recorded again with ``python tests/fixtures/<folder>/record.py``.

Real: the recorders, the launcher of this tree with its bundled Python, real folders. Mocked: nothing (the machine is the ``--sandbox`` one).
Out of scope: how the UI replays the documents (``tests/test_ui_screens.py``).
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
STAMP = re.compile(r"\d{8}T\d{6}Z")

pytestmark = [pytest.mark.skipif(os.name != "nt", reason="the launcher is a Windows cmd file"), pytest.mark.windows_integration]


def load_recorder(folder: str):
    """Import ``tests/fixtures/<folder>/record.py`` as a module (it is a script, not a package)."""
    path = FIXTURES / folder / "record.py"
    spec = importlib.util.spec_from_file_location(f"recorder_{folder}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("folder", ["ui", "cli_docs"])
def test_the_recorded_documents_equal_what_the_current_cli_prints(tmp_path: Path, folder: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Record again into *tmp_path* and compare each JSON document with the committed fixture."""
    recorder = load_recorder(folder)
    monkeypatch.setattr(recorder, "HERE", tmp_path)

    recorder.main()

    fresh = sorted(path.name for path in tmp_path.glob("*.json"))
    committed = sorted(path.name for path in (FIXTURES / folder).glob("*.json"))
    assert fresh == committed
    for name in fresh:
        now = STAMP.sub("STAMP", (tmp_path / name).read_text(encoding="utf-8"))
        then = STAMP.sub("STAMP", (FIXTURES / folder / name).read_text(encoding="utf-8"))
        assert now == then, f"{folder}/{name} differs from the current CLI output: record it again"
