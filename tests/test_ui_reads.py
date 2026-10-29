"""Cover the in-process reads of the UI programs (``gupkg_ui.reads.Reads``) on a real root, and that they give what the CLI's ``--json`` documents say.

Behaviour covered: the inventory, the available rows (with the ``system only`` mark and its reason), one package's details and the registry state are read by
calling the core functions in the run's scope, and each equals what the typed reader returns for the document of the same CLI call (``to_data`` of the in-process
result round-trips through ``read_list`` / ``read_info`` / ``read_registry_status``); a package that is not there is a ``DocumentError`` with the reason, never an empty
answer; reading never touches the machine.

Real: package roots, junctions and registry caches on pytest's temporary folder, the catalog, the core read functions. Mocked: the machine boundary (``FakeSystem``).
Out of scope: the screens (``test_ui_screens.py``) and the documents of the real CLI (``test_documents.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gupkg.report.documents import DocumentError, read_info, read_list, read_registry_status
from gupkg.viewdata import info_document, list_document, registry_status
from gupkg_ui.reads import Reads
from tests.catalog_support import activate, build_registry, make_catalog, write_version


@pytest.fixture
def world(system, tmp_path: Path):
    """A user root with one installed package and a registry holding two packages, one for the system mode only; returns ``(catalog, root)``."""
    catalog, roots = make_catalog(system, tmp_path, mode="user")
    package = roots["user"] / "alpha"
    write_version(package, "1.0.0", populated=True)
    activate(system, package, "1.0.0")
    cache = build_registry(roots["user"], {"copyq": {"versions": ["1.0.0"]}, "drv": {"versions": ["1.0.0"]}})
    manifest = next(cache.rglob("pkgs/drv/1.0.0")) / "pkg.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("format = 2\n", 'format = 2\nmodes = ["system"]\n', 1), encoding="utf-8")
    return catalog, roots["user"]


def test_the_inventory_is_read_in_process_and_equals_the_document_of_list(system, world) -> None:
    """``inventory`` lists the installed package; with ``available`` it adds the registry's, and ``drv`` is marked ``system only`` with its reason."""
    catalog, root = world
    reads = Reads(catalog.scope, system)

    installed = asyncio.run(reads.inventory())
    everything = asyncio.run(reads.inventory(available=True))

    assert [row.name for row in installed] == ["alpha"] and installed[0].status == "installed" and installed[0].version == "1.0.0"
    assert [(row.name, row.status) for row in everything] == [("alpha", "installed"), ("copyq", "available"), ("drv", "available")]
    drv = everything[2]
    assert drv.restriction == "system only" and drv.restriction_reason and drv.version is None and drv.path is None
    document = list_document(catalog, catalog.scope, None).to_data()
    assert read_list(document).rows == tuple(reads.list_document(False).rows) and read_list(document).root == str(root)


def test_one_package_and_the_registry_state_are_read_in_process(system, world) -> None:
    """``info`` gives the typed details (and equals the info document), ``registry`` the revision and package count of the cache."""
    catalog, _ = world
    reads = Reads(catalog.scope, system)

    info = asyncio.run(reads.info("alpha"))
    registry = asyncio.run(reads.registry())

    assert info.row.name == "alpha" and [item.name for item in info.versions] == ["1.0.0"]
    assert read_info(info_document(catalog, catalog.scope, ["alpha"]).to_data()).rows[0] == info
    assert registry.registries[0].name == "main" and registry.registries[0].revision and registry.registries[0].packages == 2
    assert read_registry_status(registry_status(catalog.scope).to_data()) == registry


def test_a_package_that_is_not_there_is_an_error_with_the_reason(system, world) -> None:
    """``info`` of an unknown name raises ``DocumentError`` carrying the CLI's message, not an empty answer."""
    catalog, _ = world

    with pytest.raises(DocumentError, match="no information about ghost: .*ghost"):
        asyncio.run(Reads(catalog.scope, system).info("ghost"))
