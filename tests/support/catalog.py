"""Build package roots, registry caches and catalogs on a temporary folder for the catalog tests.

The builders write real format 2 directories (version directories with a download URL, a
tracker with a ``[track]`` table), real ``current`` junctions through the ``System`` they
are given, and registry caches in the layout that ``gupkg.catalog.registry`` publishes, so
the tests exercise the catalog on data that has the shape of an installed machine.

Usage and API
-------------
Call ``write_version`` and ``write_tracker`` to lay out a package, ``build_registry`` to publish a registry cache,
``activate`` to switch ``current``, and ``make_catalog`` to open the catalog of a root over a ``System``.

Implementation Approach
-----------------------
Every builder writes the same files that the product writes and reads, and nothing is mocked inside the catalog, so a
test observes the catalog through the disk it would meet on a machine.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.catalog.catalog import Catalog
from gupkg.catalog.record import write_install_record
from gupkg.catalog.registry import registry_cache_dir
from gupkg.model.scope import make_scope
from gupkg.model.settings import LAYER_APP, Layer, default_layer, merge_layers
from gupkg.model.state import InstallRecord, Link
from gupkg.system.port import Mode
from gupkg.tomlio import write_toml

# Revision hash used for the registry trees these tests write.
REVISION = "a" * 40


def version_text(version: str, *, extra: str = "") -> str:
    """Return a valid pinned ``pkg.toml`` that downloads ``tool-<version>.zip``.

    Parameters
    ----------
    version : str
        Version label used in the description and the download address.
    extra : str, optional
        Additional ``pkg.toml`` text appended after the ``[source]`` table.

    Returns
    -------
    str
        Text of a ``pkg.toml`` for one pinned version.
    """
    return f'format = 2\ndescription = "tool {version}"\n[source]\ndownload_url = "https://example.org/tool-{version}.zip"\n{extra}'


def tracker_text(variant: str = "") -> str:
    """Return a valid tracker ``pkg.toml``; *variant* changes its content (and so its hash).

    Parameters
    ----------
    variant : str, optional
        Text placed in the description; a different value gives a different content and hash.

    Returns
    -------
    str
        Text of a tracker ``pkg.toml``.
    """
    return (
        f'format = 2\ndescription = "tracker {variant}"\n'
        '[track]\nprovider = "github"\nrepo = "owner/tool"\nasset = "tool-${version}.zip"\n'
        '[source]\ndownload_url = "${asset_url}"\n'
    )


def write_version(package: Path, version: str, *, populated: bool = False, text: str | None = None) -> Path:
    """Write ``<package>/<version>/pkg.toml`` (and an ``App`` when *populated*) and return the folder.

    Parameters
    ----------
    package : Path
        Package folder.
    version : str
        Version label, used as the folder name.
    populated : bool, optional
        Whether to also create an ``App`` folder holding one file.
    text : str | None, optional
        Content of ``pkg.toml``; a valid pinned one is written when ``None``.

    Returns
    -------
    Path
        The version folder written.
    """
    folder = package / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pkg.toml").write_text(text if text is not None else version_text(version), encoding="utf-8")
    if populated:
        (folder / "App").mkdir(exist_ok=True)
        (folder / "App" / "tool.exe").write_text("exe", encoding="utf-8")
    return folder


def write_tracker(package: Path, variant: str = "") -> Path:
    """Write ``<package>/_check_updates/pkg.toml`` and return the folder.

    Parameters
    ----------
    package : Path
        Package folder.
    variant : str, optional
        Variant passed to ``tracker_text``.

    Returns
    -------
    Path
        The ``_check_updates`` folder written.
    """
    folder = package / "_check_updates"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pkg.toml").write_text(tracker_text(variant), encoding="utf-8")
    return folder


def activate(system, package: Path, version: str) -> None:
    """Point ``<package>/current`` at the version directory with a real junction.

    A package with ``current`` and no install record is ``broken``, so a record with no mode is written
    unless the package has one; a test that needs another record writes it afterwards.

    Parameters
    ----------
    system : object
        Fake machine that creates the junction.
    package : Path
        Package folder.
    version : str
        Version folder name that ``current`` points at.
    """
    system.create_junction(package / "current", package / version)
    if not (package / ".gupkg" / "package.toml").exists():
        write_install_record(package, InstallRecord())


def write_record(package: Path, record: InstallRecord) -> None:
    """Write the install record of *package* through the data model's TOML data.

    Parameters
    ----------
    package : Path
        Package folder.
    record : InstallRecord
        Install record to write.
    """
    write_install_record(package, record)


def link_record(mode: str, **link) -> InstallRecord:
    """Return an install record with *mode* and a link built from the keyword arguments.

    Parameters
    ----------
    mode : str
        Install mode recorded.
    **link : object
        Keyword arguments for the record's link; no link is recorded when empty.

    Returns
    -------
    InstallRecord
        The install record.
    """
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

    Parameters
    ----------
    root : Path
        Root folder that holds the cache.
    packages : dict[str, dict]
        Maps a package name to ``{"versions": [...], "tracker": variant or None}``.
    name : str, optional
        Registry name.
    revision : str, optional
        Revision recorded in the cache.
    fingerprints : dict[str, str] | None, optional
        Optional ``{package: fingerprint}`` entries recorded with the cache.

    Returns
    -------
    Path
        The folder of the registry cache.
    """
    # Lay out the revision tree the way a sync publishes it, one folder per package.
    cache = registry_cache_dir(root, name)
    tree = cache / "trees" / revision
    for package, spec in packages.items():
        folder = tree / "pkgs" / package
        folder.mkdir(parents=True, exist_ok=True)
        for version in spec.get("versions", ()):
            write_version(folder, version)
        if spec.get("tracker") is not None:
            write_tracker(folder, spec["tracker"])
    # Write the registry file and the state file that mark the cache as synchronized.
    registry_data: dict[str, object] = {"format": 2, "name": name}
    if fingerprints:
        registry_data["fingerprints"] = fingerprints
    write_toml(tree / "registry.toml", registry_data)
    write_toml(cache / "state.toml", {"revision": revision, "source": "test", "synchronized_at": "", "last_failure": ""})
    return cache


def make_settings(registries: tuple[str, ...] = ("main",), mode: str = "user"):
    """Return merged settings whose registries are *registries*.

    Parameters
    ----------
    registries : tuple[str, ...], optional
        Names of the registries to configure.
    mode : str, optional
        Mode whose defaults are merged.

    Returns
    -------
    object
        The merged settings.
    """
    layer = Layer(
        LAYER_APP,
        {
            "schema": 4,
            "integration": {"integrate": "none"},
            "registry": [{"name": item, "source": f"https://example.org/{item}.zip"} for item in registries],
        },
    )
    return merge_layers([default_layer(mode), layer])


def catalog_over(system, root: Path, *, mode: str = "user", registries: tuple[str, ...] = ("main",), verify: bool = False) -> Catalog:
    """Return the catalog of a scope whose root is the folder *root*.

    Parameters
    ----------
    system : object
        Fake machine.
    root : Path
        Folder used as the scope's root.
    mode : str, optional
        Mode of the scope, ``user`` or ``system``.
    registries : tuple[str, ...], optional
        Names of the configured registries.
    verify : bool, optional
        Whether the catalog verifies the packages it reads.

    Returns
    -------
    Catalog
        The catalog of the scope.
    """
    scope = make_scope(Mode(mode), root, "test", make_settings(registries, mode), {}, system=system)
    return Catalog(scope, system, verify)


def make_catalog(system, tmp_path: Path, *, mode: str = "user", registries: tuple[str, ...] = ("main",), verify: bool = False):
    """Return ``(catalog, roots)``: the catalog of the *mode* root below ``tmp_path``, and both root folders (``system``, ``user``).

    A second call with the other *mode* gives the catalog of the other root of the same machine.

    Parameters
    ----------
    system : object
        Fake machine.
    tmp_path : Path
        Temporary folder under which both roots are made.
    mode : str, optional
        Which root's catalog to return, ``user`` or ``system``.
    registries : tuple[str, ...], optional
        Names of the configured registries.
    verify : bool, optional
        Whether the catalog verifies the packages it reads.

    Returns
    -------
    tuple[Catalog, dict[str, Path]]
        The catalog and the ``{"system": ..., "user": ...}`` root folders.
    """
    roots = {item: tmp_path / item for item in ("system", "user")}
    for path in roots.values():
        path.mkdir(exist_ok=True)
    return catalog_over(system, roots[mode], mode=mode, registries=registries, verify=verify), roots
