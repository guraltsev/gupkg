"""Cover redirected output of the real launcher when a package name is not representable in the ANSI code page.

Behavior covered: ``gupkg list`` and ``gupkg list --json`` run through the real ``src\\gupkg\\gupkg.cmd`` (isolated Python,
``PYTHONIOENCODING`` ignored) with standard output redirected to a file and to a pipe exit with 0 for package folders named
in Cyrillic, CJK and emoji and for a name holding a lone surrogate; the JSON document is pure ASCII with the right characters
after parsing, and the plain listing is UTF-8; standard error never raises either.

Real: the launcher, the bundled Python, the argument parser, the catalog and a real folder tree under ``tmp_path``.
Mocked: nothing. Out of scope: a real console (``tests/manual_smoke.md``).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "src" / "gupkg" / "gupkg.cmd"
NAMES = {"cyrillic": "жук", "cjk": "漢字パッケージ", "emoji": "tool-\U0001F600", "cp1252-only": "café"}

pytestmark = pytest.mark.skipif(os.name != "nt", reason="runs the Windows launcher")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A user root with one package folder per test name, and the settings file that points at it."""
    packages = tmp_path / "root"
    for name in NAMES.values():
        version = packages / name / "1.0.0"
        version.mkdir(parents=True)
        (version / "pkg.toml").write_text('format = 2\ndescription = "x"\n', encoding="utf-8")
    (tmp_path / "settings.toml").write_text(f'schema = 4\n[roots]\nuser = "{packages.as_posix()}"\n', encoding="utf-8")
    return tmp_path


def _gupkg(root: Path, *arguments: str, stdout) -> subprocess.CompletedProcess:
    """Run the real launcher on the settings of *root* with standard output sent to *stdout* (a file or a pipe)."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment.pop("NO_COLOR", None)
    return subprocess.run(
        [str(LAUNCHER), "--settings", str(root / "settings.toml"), *arguments],
        cwd=root, stdout=stdout, stderr=subprocess.PIPE, env=environment, timeout=180,
    )


@pytest.mark.parametrize("sink", ["file", "pipe"])
def test_the_json_document_survives_a_redirect_with_any_package_name(root: Path, sink: str) -> None:
    """Redirected ``--json`` is exit 0, pure ASCII, and parses back to the real names (Cyrillic, CJK, emoji, cp1252-only)."""
    target = root / "out.json"
    if sink == "file":
        with target.open("wb") as handle:
            finished = _gupkg(root, "list", "--json", stdout=handle)
        raw = target.read_bytes()
    else:
        finished = _gupkg(root, "list", "--json", stdout=subprocess.PIPE)
        raw = finished.stdout

    assert finished.returncode == 0, finished.stderr.decode("utf-8", "replace")
    assert raw.isascii()
    assert {row["name"] for row in json.loads(raw)["packages"]} == set(NAMES.values())


@pytest.mark.parametrize("sink", ["file", "pipe"])
def test_the_plain_listing_is_utf8_when_redirected(root: Path, sink: str) -> None:
    """Redirected plain output is UTF-8, so a name that the ANSI code page cannot hold is neither lost nor an exit code 4."""
    target = root / "out.txt"
    if sink == "file":
        with target.open("wb") as handle:
            finished = _gupkg(root, "list", stdout=handle)
        raw = target.read_bytes()
    else:
        finished = _gupkg(root, "list", stdout=subprocess.PIPE)
        raw = finished.stdout

    assert finished.returncode == 0, finished.stderr.decode("utf-8", "replace")
    text = raw.decode("utf-8")
    assert all(name in text for name in NAMES.values())


def test_a_lone_surrogate_in_a_folder_name_never_crashes_the_output(root: Path) -> None:
    """A file name NTFS allows but Unicode does not (a lone surrogate) is shown replaced and the exit code stays 0, JSON or plain."""
    odd = root / "root" / "odd\ud800name" / "1.0.0"
    try:
        odd.mkdir(parents=True)
    except OSError:
        pytest.skip("this file system cannot create a name with a lone surrogate")
    (odd / "pkg.toml").write_text('format = 2\ndescription = "x"\n', encoding="utf-8")

    as_json = _gupkg(root, "list", "--json", stdout=subprocess.PIPE)
    as_text = _gupkg(root, "list", stdout=subprocess.PIPE)

    assert as_json.returncode == 0 and as_text.returncode == 0, as_json.stderr.decode("utf-8", "replace") + as_text.stderr.decode("utf-8", "replace")
    assert as_json.stdout.isascii() and json.loads(as_json.stdout)["packages"]
