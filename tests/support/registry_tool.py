"""Doubles and builders for the tests of the registry tool (``gupkg_registry``).

``registry_of`` builds a registry holding the real trackers of some packages, ``machine`` a ``FakeSystem`` that serves the
fixture cases and artifacts, ``Git`` is a ``git`` double and ``run_cli`` runs the tool's command line.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg_registry.cli import main
from gupkg_registry.layout import write_registry_file
from tests.support.archive import make_zip
from tests.support.http_fixtures import FIXTURES, load_case

SNAPSHOT = FIXTURES / "registry_100"


class Git:
    """A ``git`` double that records every command and can fail one subcommand."""

    def __init__(self, fail: str | None = None) -> None:
        """Remember which subcommand (``add``, ``commit`` or ``push``) should exit non-zero."""
        self.fail = fail
        self.calls: list[ProcessCall] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Record the call and answer with success, or with status 1 for the failing subcommand."""
        self.calls.append(call)
        return ProcessResult(1, "", "refused") if call.argv[1] == self.fail else ProcessResult(0)


def registry_of(tmp_path: Path, *packages: str, extra: dict[str, str] | None = None) -> Path:
    """Build a registry in ``tmp_path/registry`` holding the real trackers of *packages* and a fresh ``_registry.toml``."""
    root = tmp_path / "registry"
    root.mkdir()
    for package in packages:
        shutil.copytree(SNAPSHOT / package / "_check_updates", root / package / "_check_updates")
    for name, text in (extra or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    write_registry_file(root, {"format": 2, "name": "test-registry", "description": "A registry for tests."})
    return root


def machine(tmp_path: Path, *cases: str, artifacts: dict[str, bytes] | None = None, git: Git | None = None) -> FakeSystem:
    """A FakeSystem serving the fixture *cases* and the *artifacts* ``{url: bytes}``, with *git* as the only ``git``."""
    system = FakeSystem(tmp_path / "machine", processes={"git": git} if git else {})
    for case in cases:
        system.urls.update(load_case(case))
    for url, body in (artifacts or {}).items():
        system.urls[url] = FakeHttp(body)
    return system


def zip_bytes(tmp_path: Path, name: str, members: dict[str, str]) -> bytes:
    """Return the bytes of a zip archive of *members*."""
    return make_zip(tmp_path / name, members).read_bytes()


def run_cli(root: Path, system: FakeSystem, tmp_path: Path, *arguments: str) -> int:
    """Run the command line against *root* with the given system and an empty environment."""
    return main([*arguments, "--registry", str(root), "--sandbox", str(tmp_path / "sandbox")], system=system, environ={})
