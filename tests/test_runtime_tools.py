"""Cover the maintainer tools that fetch the runtime and report stale dependency pins.

Behavior covered: the pinned CPython archive is kept only after it matches its
digest (a mismatch deletes it and stops), ``._pth`` is written so the runtime
imports from its own ``Lib/site-packages``, and ``tools/check_deps.py`` reports
exactly the pins that have a newer stable release and fails loudly when PyPI
cannot be asked.

Mocked boundaries: the archive download and the PyPI request (fake functions).
Real: the files, the digests and the lock format. Out of scope: real network
access and real wheels.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

import check_deps
import fetch_runtime

LOCK = f"textual==8.2.8 --hash=sha256:{'a' * 64}\nrich==15.0.0 --hash=sha256:{'b' * 64}\n"
LIVE = [{"yanked": False}]


def _runtime_zip(path: Path) -> str:
    """Write a tiny stand-in CPython archive and return its SHA-256."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("python312._pth", "python312.zip\n")
        archive.writestr("python.exe", "stub")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_is_downloaded_once_and_reused_from_the_cache(tmp_path: Path) -> None:
    """A verified archive is cached; asking again does not download it a second time."""
    source = tmp_path / "source.zip"
    digest = _runtime_zip(source)
    calls: list[str] = []

    def fetch(url: str, destination: Path) -> None:
        calls.append(url)
        destination.write_bytes(source.read_bytes())

    first = fetch_runtime.download_runtime_archive(tmp_path / "cache", url="https://example.invalid/py.zip", sha256=digest, fetch=fetch)
    second = fetch_runtime.download_runtime_archive(tmp_path / "cache", url="https://example.invalid/py.zip", sha256=digest, fetch=fetch)

    assert first == second and first.read_bytes() == source.read_bytes()
    assert calls == ["https://example.invalid/py.zip"]


def test_archive_that_fails_the_pinned_digest_is_deleted_and_stops_the_tool(tmp_path: Path) -> None:
    """A download that does not match the digest is removed and the tool exits with a message."""
    source = tmp_path / "source.zip"
    _runtime_zip(source)

    def fetch(url: str, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())

    with pytest.raises(SystemExit, match="SHA-256"):
        fetch_runtime.download_runtime_archive(tmp_path / "cache", url="https://example.invalid/py.zip", sha256="0" * 64, fetch=fetch)

    assert list((tmp_path / "cache").iterdir()) == []


def test_pth_file_points_the_runtime_at_its_own_site_packages(tmp_path: Path) -> None:
    """The runtime's search-path file names its interpreter zip, the folder, its parent and Lib/site-packages."""
    (tmp_path / "python312._pth").write_text("python312.zip\n.\n", encoding="ascii")

    fetch_runtime.write_pth(tmp_path)

    assert (tmp_path / "python312._pth").read_text(encoding="ascii") == "python312.zip\n.\n..\nLib/site-packages\n\nimport site\n"


def test_check_deps_reports_only_pins_with_a_newer_stable_release(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A newer stable release is printed with the update command; yanked and pre-releases are ignored."""
    lock = tmp_path / "lock"
    lock.write_text(LOCK, encoding="utf-8")
    documents = {
        "textual": {"releases": {"8.2.8": LIVE, "8.3.0": LIVE, "9.0.0": [{"yanked": True}], "9.1.0rc1": LIVE}},
        "rich": {"releases": {"15.0.0": LIVE}},
    }

    outdated = check_deps.outdated_dependencies(lock, fetch=lambda name, timeout: documents[name])

    assert outdated == [("textual", "8.2.8", "8.3.0")]


def test_check_deps_exit_status_tells_current_outdated_and_unreachable_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 when current, 1 with a line per outdated pin, 2 when PyPI could not be asked."""
    lock = tmp_path / "lock"
    lock.write_text(LOCK, encoding="utf-8")
    current = {"textual": {"releases": {"8.2.8": LIVE}}, "rich": {"releases": {"15.0.0": LIVE}}}
    newer = {"textual": {"releases": {"8.2.8": LIVE, "8.3.0": LIVE}}, "rich": current["rich"]}

    def serve(documents):
        monkeypatch.setattr(check_deps, "_fetch_pypi", lambda name, timeout: documents[name])

    serve(current)
    assert check_deps.main(["--lock", str(lock)]) == 0
    serve(newer)
    assert check_deps.main(["--lock", str(lock)]) == 1
    assert "textual 8.2.8 is pinned; 8.3.0 is available" in capsys.readouterr().out

    def offline(name: str, timeout: float) -> dict:
        raise OSError("offline")

    monkeypatch.setattr(check_deps, "_fetch_pypi", offline)
    assert check_deps.main(["--lock", str(lock)]) == 2
    assert "Could not ask PyPI" in capsys.readouterr().err
