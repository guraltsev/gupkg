"""Cover that the 0.17 path and inventory code keeps its outcomes now that the catalog supplies package state.

Behavior covered: for every shape of package root (a normal install, 0.17's ``_bootstrap`` template alone, the template
beside a version, the template beside a valid ``current``, a dangling ``current``, a ``current`` that is a plain folder,
a ``current`` that leaves the package root, and several versions with no ``current``) the table asserts what the
0.17 commands got before the catalog existed: the version each path resolves to (``identity_from_path``), the manager
inventory's status and health (``discover_manager``), the summary title (``package_summary``) and the record a command
reports. Three outcomes changed on purpose and are marked ``intentional`` in the table: several versions with no
``current`` (or the template beside a version) resolve to the newest version instead of failing with "multiple version
directories" (design 5.3), and a ``current`` that leaves its package root is refused instead of followed (design 9.6).

Real: package roots and junctions on pytest's temporary folder (``FakeSystem`` links are real) and the real adapters.
Mocked: the machine boundary (``FakeSystem``). Out of scope: installing, and the catalog's own states.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gupkg.commands import _command_package_record, package_summary
from gupkg.core import Scope
from gupkg.layout import identity_from_path
from gupkg.manager import discover_manager, load_manager_config

ERR = "ERR"


def version(root: Path, name: str) -> None:
    """Create ``<root>/<name>`` with an empty ``pkg.toml`` and a populated ``App``."""
    folder = root / name
    (folder / "App").mkdir(parents=True, exist_ok=True)
    (folder / "pkg.toml").write_text("", encoding="utf-8")
    (folder / "App" / "x.exe").write_text("x", encoding="utf-8")


def build(shape: str, base: Path, system) -> Path:
    """Build the package root ``<base>/user/pkg`` of *shape* and return it."""
    root = base / "user" / "pkg"
    root.mkdir(parents=True)
    if shape == "normal":
        version(root, "1.0")
        system.create_junction(root / "current", root / "1.0")
    elif shape == "template-only":
        version(root, "_bootstrap")
    elif shape == "template-and-version":
        version(root, "_bootstrap")
        version(root, "1.0")
    elif shape == "template-and-valid-current":
        version(root, "_bootstrap")
        version(root, "1.0")
        system.create_junction(root / "current", root / "1.0")
    elif shape == "dangling-current":
        version(root, "1.0")
        version(root, "2.0")
        system.create_junction(root / "current", root / "1.0")
        shutil.rmtree(root / "1.0")
    elif shape == "plain-folder-current":
        version(root, "1.0")
        version(root, "2.0")
        (root / "current").mkdir()
    elif shape == "current-outside-root":
        version(root, "1.0")
        version(root, "2.0")
        version(base / "outside", "1.0")
        system.create_junction(root / "current", base / "outside" / "1.0")
    else:
        version(root, "1.0")
        version(root, "2.0")
    return root


def ok(name: str, from_current: bool, is_current: bool) -> list:
    """Return the expected ``[version, installing_from_current, is_current]``."""
    return [name, from_current, is_current]


# For each shape: identity per path, inventory (status, health, installed, newest), summary title, record (installed, version).
# "intentional" marks the outcomes that differ from 0.17 on purpose (see the module docstring).
SHAPES = {
    "normal": dict(
        root=ok("1.0", True, True), current=ok("1.0", True, True), v1=ok("1.0", False, True), v2=ERR, template=ok("_bootstrap", False, False),
        target=("installed", "healthy", "1.0", "1.0"), summary="pkg 1.0  Installed: 1.0", record=("1.0", "1.0"),
    ),
    "template-only": dict(
        root=ok("_bootstrap", False, False), current=ERR, v1=ERR, v2=ERR, template=ok("_bootstrap", False, False),
        target=("not-installed", "healthy", None, "_bootstrap"), summary="pkg _bootstrap  Installed: not installed",
        record=(None, "_bootstrap"),
    ),
    "template-and-version": dict(
        root=ok("1.0", False, False),  # intentional: 0.17 failed with "multiple version directories"
        current=ERR, v1=ok("1.0", False, False), v2=ERR, template=ok("_bootstrap", False, False),
        target=("not-installed", "healthy", None, "_bootstrap"),
        summary="pkg 1.0  Installed: not installed",  # intentional: 0.17 showed "No package selected"
        record=(None, "1.0"),  # intentional
    ),
    "template-and-valid-current": dict(
        root=ok("1.0", True, True), current=ok("1.0", True, True), v1=ok("1.0", False, True), v2=ERR, template=ok("_bootstrap", False, False),
        target=("installed", "healthy", "1.0", "_bootstrap"), summary="pkg 1.0  Installed: 1.0", record=("1.0", "1.0"),
    ),
    "dangling-current": dict(
        root=ERR, current=ERR, v1=ERR, v2=ok("2.0", False, False), template=ok("_bootstrap", False, False),
        target=("broken", "unhealthy", None, "2.0"), summary="No package selected", record=None,
    ),
    "plain-folder-current": dict(
        root=ERR, current=ERR, v1=ok("1.0", False, False), v2=ok("2.0", False, False), template=ok("_bootstrap", False, False),
        target=("broken", "unhealthy", None, "2.0"), summary="No package selected", record=None,
    ),
    "current-outside-root": dict(
        root=ERR,  # intentional: 0.17 followed the junction out of the package root
        current=ERR,  # intentional
        v1=ok("1.0", False, False), v2=ok("2.0", False, False), template=ok("_bootstrap", False, False),
        target=("broken", "unhealthy", None, "2.0"),
        summary="No package selected",  # intentional: 0.17 showed the outside version as installed
        record=None,
    ),
    "no-current-several-versions": dict(
        root=ok("2.0", False, False),  # intentional: 0.17 failed with "multiple version directories"
        current=ERR, v1=ok("1.0", False, False), v2=ok("2.0", False, False), template=ok("_bootstrap", False, False),
        target=("not-installed", "healthy", None, "2.0"),
        summary="pkg 2.0  Installed: not installed",  # intentional
        record=(None, "2.0"),  # intentional
    ),
}


def resolve(path: Path, system):
    """Return ``[version, from_current, is_current]`` for *path*, or ``ERR`` when the adapter refuses it."""
    try:
        identity, from_current = identity_from_path(path, system)
    except ValueError:
        return ERR
    return [identity.version_string, from_current, identity.is_current]


@pytest.mark.parametrize("shape", SHAPES, ids=list(SHAPES))
def test_paths_resolve_as_they_did_before_the_catalog(shape: str, system, tmp_path: Path) -> None:
    """A package root, ``current``, a version directory and the template resolve to the version 0.17 chose."""
    root = build(shape, tmp_path, system)
    expected = SHAPES[shape]

    actual = {
        "root": resolve(root, system), "current": resolve(root / "current", system),
        "v1": resolve(root / "1.0", system), "v2": resolve(root / "2.0", system),
        "template": resolve(root / "_bootstrap", system),
    }

    assert actual == {key: expected[key] for key in actual}


@pytest.mark.parametrize("shape", SHAPES, ids=list(SHAPES))
def test_manager_inventory_status_and_health_are_as_before(shape: str, system, tmp_path: Path) -> None:
    """The manager inventory shows the same installation status, health, active and newest version, and explains a broken ``current``."""
    build(shape, tmp_path, system)
    (tmp_path / "system").mkdir()
    config_path = tmp_path / "gupkg-config.toml"
    config_path.write_text(
        'mode = "manager"\nschema_version = 2\n\n[packages]\n'
        f"system = '{tmp_path / 'system'}'\nuser = '{tmp_path / 'user'}'\n\n"
        f"[bin]\nsystem = '{tmp_path / 'sb'}'\nuser = '{tmp_path / 'ub'}'\n\n"
        f"[registry]\ncache = '{tmp_path / 'rc'}'\n\n[shims]\nlinkage = 'dynamic'\n",
        encoding="utf-8",
    )

    [target] = discover_manager(load_manager_config(config_path), system).targets

    status, health, installed, newest = SHAPES[shape]["target"]
    assert (target.installation_status, target.installed_version, target.local_version) == (status, installed, newest)
    assert target.health_status == health
    assert bool([item for item in target.diagnostics if item.startswith('"current"')]) == (status == "broken")


@pytest.mark.parametrize("shape", SHAPES, ids=list(SHAPES))
def test_summary_and_command_record_are_as_before(shape: str, system, tmp_path: Path) -> None:
    """The package summary title and the record a command reports name the same version and active version as before."""
    root = build(shape, tmp_path, system)
    expected = SHAPES[shape]

    title = package_summary(str(root), system).title
    record = _command_package_record(root, Scope.USER, system)

    assert title == expected["summary"]
    if expected["record"] is None:
        assert "version" not in record and record["path"] == str(root)
    else:
        assert (record["installed_version"], record["version"]) == expected["record"]
