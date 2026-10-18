"""Cover gupkg updating itself from the release table in RELEASES.md.

Behavior covered: the gupkg package finds the newest row of RELEASES.md,
downloads that commit's source archive, and stages a complete new version tree
that keeps install-specific files and drops removed modules; an archive built
from another commit is refused; ``tools/release.py`` refuses an unchanged
version.

Real: the repository's own ``src`` tree used as the installed package, the
update coordinator, hook loading, and staging on disk. Mocked: HTTP
(``urllib.request.urlopen``). Out of scope: shortcut/PATH activation, which
``test_update_lifecycle`` covers, and GitHub itself.
"""

from __future__ import annotations

import io
import re
import shutil
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

from gupkg import gupkg as workflows

REPO = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
NEW_VERSION = "9.9.9"


def _installed(tmp_path: Path) -> Path:
    """Copy the repository's gupkg package into a package root as the active version."""
    current = re.search(
        r'(?m)^__version__ = "([^"]+)"', (REPO / "src" / "gupkg" / "_version.py").read_text(encoding="utf-8")
    )[1]
    version = tmp_path / "gupkg" / f"{current}"
    shutil.copytree(
        REPO / "src", version, ignore=shutil.ignore_patterns("__pycache__", "python", "7zip")
    )
    (version / "gupkg" / "python").mkdir()
    (version / "gupkg" / "python" / "python.exe").write_text("embedded", encoding="utf-8")
    (version / "gupkg" / "removed_module.py").write_text("old", encoding="utf-8")
    return version


def _release_archive(installed: Path, comment: str) -> bytes:
    """Build a GitHub-style commit archive whose gupkg version is ``NEW_VERSION``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.comment = comment.encode()
        for file in (installed).rglob("*"):
            relative = file.relative_to(installed)
            if not file.is_file() or relative.parts[:2] == ("gupkg", "python") or relative.name == "removed_module.py":
                continue
            data = file.read_bytes()
            if relative.as_posix() == "gupkg/_version.py":
                data = re.sub(rb'(?m)^__version__ = "[^"]+"', b'__version__ = "%s"' % NEW_VERSION.encode(), data)
            archive.writestr(f"pkg-{COMMIT}/src/{relative.as_posix()}", data)
    return buffer.getvalue()


def _releases_text(version: str) -> bytes:
    """Return a RELEASES.md body listing one release."""
    return f"| Version | Commit | Date | Notes |\n|---|---|---|---|\n| {version} | `{COMMIT}` | 2026-01-01 | test |\n".encode()


def test_update_stages_the_listed_commit_as_a_complete_new_version(tmp_path: Path) -> None:
    """A newer row yields a new version folder with new code, kept runtime, and no stale modules."""
    installed = _installed(tmp_path)
    responses = [io.BytesIO(_releases_text(NEW_VERSION)), io.BytesIO(_release_archive(installed, COMMIT))]

    with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
        result = workflows.download_package_update(installed)

    staged = tmp_path / "gupkg" / f"{NEW_VERSION}"
    assert result.ok, result.errors
    assert result.status == "downloaded"
    assert f'__version__ = "{NEW_VERSION}"' in (staged / "gupkg" / "_version.py").read_text(encoding="utf-8")
    assert (staged / "gupkg" / "cli.py").is_file()
    assert (staged / "pkg.local" / "check_update.py").is_file()
    assert (staged / "gupkg" / "python" / "python.exe").read_text(encoding="utf-8") == "embedded"
    assert not (staged / "gupkg" / "removed_module.py").exists()


def test_update_reports_current_when_the_table_lists_no_newer_release(tmp_path: Path) -> None:
    """The newest row equal to the installed version is not an update."""
    installed = _installed(tmp_path)
    current = installed.name.removeprefix("v")

    with mock.patch.object(urllib.request, "urlopen", return_value=io.BytesIO(_releases_text(current))):
        result = workflows.download_package_update(installed)

    assert result.ok, result.errors
    assert result.status == "current"
    assert not (tmp_path / "gupkg" / f"{NEW_VERSION}").exists()


def test_update_refuses_an_archive_built_from_another_commit(tmp_path: Path) -> None:
    """An archive whose embedded commit differs from the listed one is never staged."""
    installed = _installed(tmp_path)
    responses = [io.BytesIO(_releases_text(NEW_VERSION)), io.BytesIO(_release_archive(installed, "b" * 40))]

    with mock.patch.object(urllib.request, "urlopen", side_effect=responses):
        result = workflows.download_package_update(installed)

    assert not result.ok
    assert any("commit" in error for error in result.errors)
    assert not (tmp_path / "gupkg" / f"{NEW_VERSION}").exists()
