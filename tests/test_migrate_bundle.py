"""Cover that the release bundle ships the migration tool beside gupkg and covers it in the integrity manifest.

Behavior covered: the release ZIP built from this checkout contains the `gupkg_migrate` package
next to the `gupkg` package, with no bytecode, and `MANIFEST.sha256` lists its files so a
modified tool is detected like any other file of the bundle.

Real: the ZIP built by ``tools/build_standalone.py`` for the session (shared fixture).
Mocked: nothing. Out of scope: running the tool from the bundle, and the `gupkg migrate`
forwarder, which the CLI issue adds.
"""

from __future__ import annotations

import zipfile


def test_bundle_contains_the_migration_tool_without_bytecode(release_bundle) -> None:
    """The ZIP has `gupkg_migrate` beside `gupkg`, and no `.pyc` or `__pycache__` anywhere in it."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        names = archive.namelist()
    top = names[0].split("/")[0]

    assert f"{top}/gupkg_migrate/cli.py" in names
    assert f"{top}/gupkg_migrate/__main__.py" in names
    assert f"{top}/gupkg/cli.py" in names
    assert not [name for name in names if name.endswith(".pyc") or "__pycache__" in name]


def test_manifest_lists_the_migration_tool_files(release_bundle) -> None:
    """`MANIFEST.sha256` has an entry for every file of `gupkg_migrate`, so edits to the tool are detected."""
    with zipfile.ZipFile(release_bundle.zip) as archive:
        names = archive.namelist()
        top = names[0].split("/")[0]
        manifest = archive.read(f"{top}/gupkg/MANIFEST.sha256").decode("utf-8")
        tool_files = [name for name in names if name.startswith(f"{top}/gupkg_migrate/") and not name.endswith("/")]

    assert tool_files
    for name in tool_files:
        relative = name[len(top) + 1:]
        assert f"  {relative}\n" in manifest or f" {relative}\n" in manifest
