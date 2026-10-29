"""Cover registry 2 synchronisation, validation and reading by the catalog.

Behavior covered: a sync publishes a validated tree and index in the root data directory
(``<root>\\.gupkg\\registry\\<name>``, not in the gupkg version's ``App``); one invalid package, version or
tracker is reported and skipped without failing the registry; a registry that is not format 2 is refused and the previous tree stays active; the validated index is used for a revision and
a stale one is ignored; published fingerprints reach the candidates; several registries are joined with
the first match winning and ``registry/name`` choosing explicitly.

Real: ZIP archives built at test time and served by ``FakeSystem`` (``urls``), the real sync and the catalog. Mocked: the network
(``FakeSystem.add_url``). Out of scope: Git transport, downloading package payloads, and executing installs.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from gupkg.catalog.catalog import NoVersionKnown
from gupkg.catalog.registry import load_registry, registry_cache_dir, search_snapshot, sync_registry
from gupkg.model.manifest import ManifestKind, TrackResult, manifest_to_toml, parse_manifest, render_pinned
from gupkg.tomlio import dumps
from tests.catalog_support import build_registry, make_catalog, tracker_text, version_text
from tests.scope_support import scope_of

SOURCE = "https://example.org/main.zip"


def registry_zip(files: dict[str, str], *, registry: dict | None = None, top: str = "gupkg-registry-main") -> bytes:
    """Return the ZIP of a registry repository: ``_registry.toml`` (format 2 unless overridden) and *files*."""
    data = {"format": 2, "name": "main"} if registry is None else registry
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if data:
            archive.writestr(f"{top}/_registry.toml", dumps(data))
        for name, text in files.items():
            archive.writestr(f"{top}/{name}", text)
    return buffer.getvalue()


def sync(system, root: Path, archive: bytes, **options):
    """Serve *archive* at ``SOURCE`` and sync registry ``main`` into the data directory of *root*."""
    system.add_url(SOURCE, archive)
    return sync_registry(registry_cache_dir(root, "main"), scope_of(system), name="main", source=SOURCE, **options)


GOOD = {
    "foo/1.0/pkg.toml": version_text("1.0"),
    "foo/1.1/pkg.toml": version_text("1.1"),
    "foo/_check_updates/pkg.toml": tracker_text(),
    "bar/_check_updates/pkg.toml": tracker_text("bar"),
}


def test_sync_publishes_a_validated_tree_in_the_root_data_directory(system, tmp_path: Path) -> None:
    """A sync writes the cache to ``<root>\\.gupkg\\registry\\main`` and the catalog reads its entries, versions and trackers."""
    result = sync(system, tmp_path, registry_zip(GOOD))
    snapshot = load_registry("main", registry_cache_dir(tmp_path, "main"), scope_of(system))

    assert result.ok and result.status == "synced" and result.problems == ()
    assert (tmp_path / ".gupkg" / "registry" / "main" / "state.toml").is_file()
    assert sorted(snapshot.entries) == ["bar", "foo"]
    assert [version.name for version in snapshot.entry("foo").versions] == ["1.0", "1.1"]
    assert snapshot.entry("foo").tracker is not None and snapshot.entry("bar").versions == ()
    assert snapshot.revision == result.revision
    assert [entry.name for entry in search_snapshot(snapshot, "FO")] == ["foo"]


def test_syncing_the_same_archive_again_reports_current(system, tmp_path: Path) -> None:
    """A second sync of the same revision changes nothing and says ``current``."""
    archive = registry_zip(GOOD)
    first = sync(system, tmp_path, archive)

    second = sync(system, tmp_path, archive)

    assert second.ok and second.status == "current" and second.revision == first.revision


def test_one_invalid_package_version_or_tracker_is_reported_and_skipped(system, tmp_path: Path) -> None:
    """Invalid entries are dropped one by one and listed as problems; the rest of the registry is published."""
    files = {
        **GOOD,
        "broken/1.0/pkg.toml": "format = 1\n[origin]\nurl = 'x'\n",
        "halfbad/1.0/pkg.toml": version_text("1.0"),
        "halfbad/2.0/pkg.toml": "format = 2\n[source]\ndownload_url = 'https://x.test/${version}.zip'\n",
        "halfbad/_check_updates/pkg.toml": "format = 2\n",
        "linked-seed/_bootstrap/pkg.toml": tracker_text(),
    }

    result = sync(system, tmp_path, registry_zip(files))
    snapshot = load_registry("main", registry_cache_dir(tmp_path, "main"), scope_of(system))

    assert result.ok
    assert sorted(snapshot.entries) == ["bar", "foo", "halfbad"]
    assert [version.name for version in snapshot.entry("halfbad").versions] == ["1.0"]
    assert snapshot.entry("halfbad").tracker is None
    problems = "\n".join(result.problems)
    assert "broken: no valid version or tracker; skipped" in problems
    assert "halfbad/2.0" in problems and "version dropped" in problems
    assert "halfbad/_check_updates" in problems and "tracker dropped" in problems
    assert "linked-seed" not in snapshot.entries


@pytest.mark.parametrize(
    ("registry", "reason"),
    [
        pytest.param({}, "unsupported registry format", id="no-registry-file"),
        pytest.param({"format": 1}, "unsupported registry format", id="format-1"),
        pytest.param({"format": 2, "fingerprints": {"foo/1.0": "not-a-hash"}}, "fingerprints", id="malformed-fingerprint"),
    ],
)
def test_registry_that_is_not_format_2_is_refused_and_the_previous_tree_stays(
    system, tmp_path: Path, registry: dict, reason: str
) -> None:
    """A tree with no usable ``_registry.toml`` fails the sync; the earlier validated revision remains active."""
    assert sync(system, tmp_path, registry_zip(GOOD)).ok
    before = load_registry("main", registry_cache_dir(tmp_path, "main"), scope_of(system))

    result = sync(system, tmp_path, registry_zip({**GOOD, "foo/1.2/pkg.toml": version_text("1.2")}, registry=registry))
    after = load_registry("main", registry_cache_dir(tmp_path, "main"), scope_of(system))

    assert not result.ok and reason in result.error
    assert after.revision == before.revision and [v.name for v in after.entry("foo").versions] == ["1.0", "1.1"]


def test_offline_sync_reports_the_cache_without_any_request(system, tmp_path: Path) -> None:
    """``offline`` validates nothing new and fetches nothing; with no cache it fails."""
    assert not sync_registry(registry_cache_dir(tmp_path, "main"), scope_of(system), name="main", source=SOURCE, offline=True).ok
    sync(system, tmp_path, registry_zip(GOOD))
    system.fetch_log.clear()

    result = sync_registry(registry_cache_dir(tmp_path, "main"), scope_of(system), name="main", source=SOURCE, offline=True)

    assert result.ok and result.status == "offline" and system.fetch_log == []


def test_validated_index_is_used_for_its_revision_and_a_stale_one_is_rebuilt(system, tmp_path: Path) -> None:
    """Reading a revision uses the index written by the sync; an index of another revision is ignored and rebuilt in memory."""
    sync(system, tmp_path, registry_zip(GOOD))
    cache = registry_cache_dir(tmp_path, "main")
    revision = load_registry("main", cache, scope_of(system)).revision
    tree = cache / "trees" / revision
    victim = tree / "pkgs" / "foo" / "1.1" / "pkg.toml"

    # The tree changes behind the index: the index still describes the validated revision.
    victim.write_text("format = 2\n", encoding="utf-8")
    indexed = load_registry("main", cache, scope_of(system))
    assert [version.name for version in indexed.entry("foo").versions] == ["1.0", "1.1"]

    # Without a usable index the tree is validated again, and the reader writes nothing.
    index = tree / "index.toml"
    index.write_text(index.read_text(encoding="utf-8").replace(revision, "f" * 40), encoding="utf-8")
    rebuilt = load_registry("main", cache, scope_of(system))
    assert [version.name for version in rebuilt.entry("foo").versions] == ["1.0"]
    assert "f" * 40 in index.read_text(encoding="utf-8")


def test_published_fingerprints_reach_the_candidates(system, tmp_path: Path) -> None:
    """A fingerprint published in ``_registry.toml`` is attached to its version's candidate; versions without one carry none."""
    digest = "ab" * 32
    archive = registry_zip(GOOD, registry={"format": 2, "fingerprints": {"foo/1.1": digest}})
    roots = make_catalog(system, tmp_path)[1]
    sync(system, roots["user"], archive)
    catalog = make_catalog(system, tmp_path)[0]

    view = catalog.describe("foo")

    assert {item.version: item.fingerprint for item in catalog.candidates(view)} == {"1.1": digest, "1.0": None}


def test_first_registry_with_the_name_wins_and_registry_name_selects_explicitly(system, tmp_path: Path) -> None:
    """With two registries that both have ``foo`` the first configured one is followed; ``team/foo`` picks the other."""
    roots = make_catalog(system, tmp_path)[1]
    build_registry(roots["user"], {"foo": {"versions": ["1.0"]}}, name="main", revision="a" * 40)
    build_registry(roots["user"], {"foo": {"versions": ["7.0"]}, "only-team": {"versions": ["1.0"]}}, name="team", revision="b" * 40)
    catalog = make_catalog(system, tmp_path, registries=("main", "team"))[0]

    by_default = catalog.resolve("foo").view
    explicit = catalog.resolve("team/foo")
    only_team = catalog.resolve("only-team").view

    assert by_default.following == "main/foo" and catalog.install_candidate(by_default).version == "1.0"
    assert explicit.view.following == "team/foo" and catalog.install_candidate(explicit.view).version == "7.0"
    assert only_team.following == "team/only-team"
