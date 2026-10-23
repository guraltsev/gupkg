"""Build package roots, registry caches and catalogs on a temporary folder for the catalog tests.

The builders write real format 2 directories (version directories with a download URL, a
tracker with a ``[track]`` table), real ``current`` junctions through the ``System`` they
are given, and registry caches in the layout that ``gupkg.catalog.registry`` publishes, so
the tests exercise the catalog on data that has the shape of an installed machine.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.catalog.catalog import Catalog
from gupkg.catalog.record import write_install_record
from gupkg.catalog.registry import registry_cache_dir
from gupkg.model.settings import LAYER_SYSTEM, Layer, default_layer, merge_layers
from gupkg.model.state import InstallRecord, Link, Root
from gupkg.tomlio import write_toml

REVISION = "a" * 40


def version_text(version: str, *, extra: str = "") -> str:
    """Return a valid pinned ``pkg.toml`` that downloads ``tool-<version>.zip``."""
    return f'format = 2\ndescription = "tool {version}"\n[source]\ndownload_url = "https://example.org/tool-{version}.zip"\n{extra}'


def tracker_text(variant: str = "") -> str:
    """Return a valid tracker ``pkg.toml``; *variant* changes its content (and so its hash)."""
    return (
        f'format = 2\ndescription = "tracker {variant}"\n'
        '[track]\nprovider = "github"\nrepo = "owner/tool"\nasset = "tool-${version}.zip"\n'
        '[source]\ndownload_url = "${asset_url}"\n'
    )


def write_version(package: Path, version: str, *, populated: bool = False, text: str | None = None) -> Path:
    """Write ``<package>/<version>/pkg.toml`` (and an ``App`` when *populated*) and return the folder."""
    folder = package / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pkg.toml").write_text(text if text is not None else version_text(version), encoding="utf-8")
    if populated:
        (folder / "App").mkdir(exist_ok=True)
        (folder / "App" / "tool.exe").write_text("exe", encoding="utf-8")
    return folder


def write_tracker(package: Path, variant: str = "") -> Path:
    """Write ``<package>/_check_updates/pkg.toml`` and return the folder."""
    folder = package / "_check_updates"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pkg.toml").write_text(tracker_text(variant), encoding="utf-8")
    return folder


def activate(system, package: Path, version: str) -> None:
    """Point ``<package>/current`` at the version directory with a real junction."""
    system.create_junction(package / "current", package / version)


def write_record(package: Path, record: InstallRecord) -> None:
    """Write the install record of *package* through the data model's TOML data."""
    write_install_record(package, record)


def link_record(mode: str, **link) -> InstallRecord:
    """Return an install record with *mode* and a link built from the keyword arguments."""
    return InstallRecord(mode=mode, link=Link(**link) if link else None)


def build_registry(
    root: Path,
    packages: dict[str, dict],
    *,
    name: str = "main",
    revision: str = REVISION,
    fingerprints: dict[str, str] | None = None,
) -> Path:
    """Write a registry cache below ``<root>/.gupkg/registry/<name>`` and return its folder.

    *packages* maps a package name to ``{"versions": [...], "tracker": variant or None}``;
    the cache has the layout that a sync publishes (state file, revision tree, registry file).
    """
    cache = registry_cache_dir(root, name)
    tree = cache / "trees" / revision
    for package, spec in packages.items():
        folder = tree / "pkgs" / package
        folder.mkdir(parents=True, exist_ok=True)
        for version in spec.get("versions", ()):
            write_version(folder, version)
        if spec.get("tracker") is not None:
            write_tracker(folder, spec["tracker"])
    registry_data: dict[str, object] = {"format": 2, "name": name}
    if fingerprints:
        registry_data["fingerprints"] = fingerprints
    write_toml(tree / "registry.toml", registry_data)
    write_toml(cache / "state.toml", {"revision": revision, "source": "test", "synchronized_at": "", "last_failure": ""})
    return cache


def make_settings(system_root: Path, user_root: Path, portable_root: Path, registries: tuple[str, ...] = ("main",)):
    """Return merged settings whose three roots are the given folders and whose registries are *registries*."""
    layer = Layer(
        LAYER_SYSTEM,
        {
            "schema": 3,
            "roots": {"system": str(system_root), "user": str(user_root), "portable": str(portable_root)},
            "registry": [{"name": item, "source": f"https://example.org/{item}.zip"} for item in registries],
        },
    )
    return merge_layers([default_layer(), layer])


def make_catalog(system, tmp_path: Path, *, registries: tuple[str, ...] = ("main",), elevated: bool | None = None, verify: bool = False):
    """Return ``(catalog, roots)`` for roots below ``tmp_path`` (``system``, ``user``, ``portable``)."""
    roots = {mode: tmp_path / mode for mode in ("system", "user", "portable")}
    for path in roots.values():
        path.mkdir(exist_ok=True)
    settings = make_settings(roots["system"], roots["user"], roots["portable"], registries)
    catalog = Catalog.from_settings(settings, system, {}, elevated=elevated, verify_effects=verify)
    return catalog, roots


def root_of(path: Path, mode: str) -> Root:
    """Return a ``Root`` for tests that describe a package of a custom root."""
    return Root(path, mode)
