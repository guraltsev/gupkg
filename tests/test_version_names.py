"""Cover which names can be version directories: real versions pass, hostile ones never reach the disk.

Behavior covered: a table of real versions (plain, dated, prerelease, local revision, Git-style) is accepted;
Windows device names (any case, with or without an extension, including superscript digits), control and
bidirectional characters, surrounding spaces or dots, a leading dash and expansion tokens (``%TEMP%``,
``$HOME``, ``PROGRA~1``) are refused; a hostile version found by a tracker page makes ``update_registry`` fail
for that package and write nothing; and the catalog ignores hostile directory names in a package root.
Real: ``is_version_name``, the registry tool with real services and files, the local scan. Mocked:
``FakeSystem`` serves the hostile page. Out of scope: creating device-named directories (Windows cannot make them
through normal calls, so the catalog test uses the other hostile names; the device rule is the table).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.local import scan_package
from gupkg.model.version import is_version_name
from gupkg.system.fake import FakeHttp, FakeSystem
from gupkg_registry.layout import read_registry_file, write_registry_file
from gupkg_registry.update import update_registry
from tests.catalog_support import write_version

REAL = ["1.2.3", "2024.10.1", "v1.2", "1.0.0-rc.1", "20261022-101500-ab12cd3", "0.17.0.local.1", "bootstrap2", "10.02", "3.31-1", "2.26"]
HOSTILE = [
    "CON", "con", "Con.txt", "con.tar.gz", "PRN", "AUX", "aux", "NUL", "nul.txt", "CONIN$", "conout$", "COM1", "com9", "COM1.txt", "LPT1", "lpt9.x",
    "COM¹", "LPT²", "LPT³.txt", "con .txt", "a‮b", "⁦x", "a\x00b", "a\x1fb", "a\x7fb", "a\x85b", "a​b", "a‏b",
    "a b", "a﻿b", "a‎b", "a‪b", "a‫b", "a‬b", "a‭b", "⁧x", "⁨x", "⁩x", "a b", " 1.0", "1.0 ", "1.0.", "-1.0", "--help", "%TEMP%", "a%b", "PROGRA~1", "v1~2", "$HOME", "a$b", "1/2", "1\\2", "a:b",
    "a*b", "a?b", 'a"b', "a<b", "a|b", "", "_check_updates", "current", "data", ".hidden",
]


@pytest.mark.parametrize("name", [pytest.param(name, id=name) for name in REAL])
def test_real_version_names_are_accepted(name: str) -> None:
    """Dotted, dated, prerelease, local-revision and Git-style versions can be directories."""
    assert is_version_name(name)


@pytest.mark.parametrize("name", [pytest.param(name, id=ascii(name)) for name in HOSTILE])
def test_hostile_names_are_refused(name: str) -> None:
    """Device names, control and bidirectional characters, spaces, dots, dashes and expansion tokens are not versions."""
    assert not is_version_name(name)


TRACKER = (
    'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "page"\n'
    'check_url = "https://example.org/dl"\nmatch = \'release-(?P<version>[\\w.%~$\\-]+)\\.zip\'\n'
    '[source]\ndownload_url = "https://example.org/release-${version}.zip"\n'
)


@pytest.mark.parametrize("version", ["CON", "con.txt", "COM1", "aux", "NUL", "lpt1", "-rf", "PROGRA~1", "%TEMP%", "a$b"])
def test_a_hostile_version_on_a_vendor_page_writes_nothing(tmp_path: Path, version: str) -> None:
    """A tracker that finds a hostile version fails for its package; no directory and no fingerprint appears."""
    root = tmp_path / "registry"
    (root / "tool" / "_check_updates").mkdir(parents=True)
    (root / "tool" / "_check_updates" / "pkg.toml").write_text(TRACKER, encoding="utf-8", newline="\n")
    write_registry_file(root, {"format": 2, "name": "t", "description": "t"})
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    system = FakeSystem(tmp_path / "machine")
    system.urls["https://example.org/dl"] = FakeHttp(f"<a>release-{version}.zip</a>")

    outcome = update_registry(root, system, sandbox=tmp_path / "sandbox", names=["tool"], environ={}).outcomes[0]

    assert outcome.status == "failed" and "cannot name a version directory" in outcome.message
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before
    assert "fingerprints" not in read_registry_file(root) or not read_registry_file(root)["fingerprints"]
    assert not any(request.url.endswith(".zip") for request in system.request_log)


def test_the_catalog_ignores_hostile_directory_names_in_a_package_root(tmp_path: Path, system: FakeSystem) -> None:
    """Directories named like expansion tokens or options are not versions; the real version is the only one found."""
    package = tmp_path / "user" / "foo"
    write_version(package, "1.0", populated=True)
    for name in ("%TEMP%", "PROGRA~1", "$HOME", "-rf"):
        write_version(package, name, populated=True)

    local = scan_package(package, system)

    assert [item.name for item in local.valid_versions] == ["1.0"]
