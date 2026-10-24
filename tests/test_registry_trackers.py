"""Cover the registry's three Git trackers with a ``git`` double (the other 76 run against recorded HTTP fixtures).

Behavior covered: the trackers of latex-toolkit, overleaf-sync and windebloattools discover a version of
the form ``YYYYMMDD-HHMMSS-<short sha>``, pin the full commit they found in the version's ``[source.git]``
(the commit is the integrity anchor, so no ``sha256`` is expected), and render a pinned version that passes
the package checker. The CI job of the registry runs this module against its own checkout, through the
``GUPKG_REGISTRY_TRACKERS`` variable that ``tests/http_fixtures.py`` reads.
Real: the committed (or CI-supplied) trackers, the git provider, ``render_pinned``, the package checker.
Mocked: ``git`` (``FakeGit`` of ``test_git_services``). Out of scope: real clones (manual) and the HTTP trackers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, manifest_to_toml, render_pinned
from gupkg.pkgcheck import check_directory
from gupkg.services.track import run_tracker
from tests.http_fixtures import environment, tracker_manifest
from tests.test_git_services import COMMIT, FakeGit, machine

GIT_TRACKED = ["latex-toolkit", "overleaf-sync", "windebloattools"]


@pytest.mark.parametrize("package", [pytest.param(name, id=name) for name in GIT_TRACKED])
def test_a_git_tracker_pins_the_commit_it_found(tmp_path: Path, package: str) -> None:
    """The version is date and short commit, the pinned source names the full commit, and the version validates."""
    git = FakeGit()
    manifest = tracker_manifest(package)

    result = run_tracker(manifest, environment(machine(tmp_path, git), tmp_path, package=package))
    pinned = render_pinned(manifest, result)

    assert re.fullmatch(r"\d{8}-\d{6}-0123456", result.version)
    assert pinned.kind is ManifestKind.VERSION and pinned.track is None
    assert pinned.source.git is not None and pinned.source.git.commit == COMMIT
    version = tmp_path / package / result.version
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(manifest_to_toml(pinned), encoding="utf-8", newline="\n")
    report = check_directory(version)
    assert report.errors == () and report.warnings == (), (report.errors, report.warnings)
