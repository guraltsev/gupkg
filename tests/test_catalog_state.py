"""Cover what the catalog derives about packages beyond the row-by-row state table.

Behavior covered: a root with a tracker and several versions is ``local`` and installs its newest
valid version, never ``_check_updates``; an invalid tracker or version invalidates only itself and
leaves the other candidates; a local package follows the same-name registry entry automatically,
opts out with ``unlink`` and follows another entry when linked (decision D2); every pinned registry
version is a candidate (D36); a registry tracker is copied, refreshed or kept by the recorded hash;
a package with only a tracker reports "no version known yet" without any network request; ad-hoc
roots recorded in ``state.toml`` are listed with their mode; a name in both the system and the user
root draws a shadowing warning; ``compare`` decides whether a tracker result is newer than every
known version; install-record effects are compared with the system only on request.

Real: package roots, junctions (``FakeSystem`` links are real), registry caches and the catalog.
Mocked: the machine boundary (``FakeSystem``; its registry access log shows what was read).
Out of scope: executing installs (issue 028), running trackers, and path-safety (``test_catalog_security``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.catalog.catalog import ListFilter, NoVersionKnown
from gupkg.catalog.local import tree_hash
from gupkg.model.manifest import TrackResult
from gupkg.model.state import Effect, InstallRecord, Link
from gupkg.system.port import Mode, ShortcutSpec
from gupkg.system.windows import WindowsSystem
from gupkg.tomlio import write_toml
from tests.catalog_support import (
    activate,
    build_registry,
    link_record,
    make_catalog,
    write_record,
    write_tracker,
    write_version,
)


def test_tracker_and_two_versions_without_current_is_local_and_installs_the_newest_version(system, tmp_path: Path) -> None:
    """A package root with ``_check_updates`` and two versions is ``local``; install chooses the newer version, never the tracker."""
    catalog, roots = make_catalog(system, tmp_path)
    package = roots["user"] / "foo"
    write_version(package, "1.0")
    write_version(package, "1.4")
    write_tracker(package)

    view = catalog.describe("foo")

    assert view.status == "local"
    assert [item.name for item in view.local.versions] == ["1.0", "1.4"]
    assert catalog.install_candidate(view).version == "1.4"
    assert view.local.tracker is not None and view.local.tracker.valid


def test_invalid_tracker_marks_only_the_tracker_and_update_candidates_survive(system, tmp_path: Path) -> None:
    """An invalid ``_check_updates`` is invalid by itself; an installed package still sees newer local and registry versions."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0", "1.3"]}})
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    write_version(package, "1.2")
    tracker = write_tracker(package)
    (tracker / "pkg.toml").write_text("format = 2\n[track]\nprovider = 'nonsense'\n", encoding="utf-8")
    activate(system, package, "1.0")

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert not view.local.tracker.valid and view.local.tracker.problems
    assert all(item.valid for item in view.local.versions)
    assert [(item.version, item.source) for item in catalog.candidates(view)] == [("1.3", "registry"), ("1.2", "local")]
    assert view.status == "outdated"


def test_invalid_version_is_invalid_alone_and_never_a_candidate(system, tmp_path: Path) -> None:
    """A version whose ``pkg.toml`` fails validation is reported ``invalid`` while its siblings stay usable."""
    catalog, roots = make_catalog(system, tmp_path)
    package = roots["user"] / "foo"
    write_version(package, "1.0")
    write_version(package, "2.0", text="format = 1\n[origin]\nurl = 'x'\n")

    view = catalog.describe("foo")

    assert {item.name: item.status for item in view.local.versions} == {"1.0": "available", "2.0": "invalid"}
    assert catalog.install_candidate(view).version == "1.0"
    assert view.status == "local"


@pytest.mark.parametrize(
    ("record", "following", "note", "update"),
    [
        pytest.param(None, "main/foo", None, "1.3", id="no-link-follows-same-name"),
        pytest.param(link_record("user", unlinked=True), None, "same name in registry: main (not followed)", None, id="unlink-opts-out"),
        pytest.param(link_record("user", registry="main", name="bar"), "main/bar", None, "2.0", id="linked-to-another-entry"),
    ],
)
def test_local_package_follows_the_registry_by_name_unless_it_opted_out(
    system, tmp_path: Path, record, following, note, update
) -> None:
    """Decision D2: the same-name entry is followed by default, ``unlink`` stops it and shows a note, a link follows another entry."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0", "1.3"]}, "bar": {"versions": ["2.0"]}})
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    if record is not None:
        write_record(package, record)

    view = make_catalog(system, tmp_path)[0].describe("foo")
    chosen = catalog.update_candidate(view)

    assert view.following == following
    assert view.note == note
    assert (chosen.version if chosen else None) == update
    if update is None:
        assert [item for item in catalog.candidates(view) if item.source == "registry"] == []


def test_registry_entry_with_three_pinned_versions_offers_all_three(system, tmp_path: Path) -> None:
    """Every pinned version the registry holds is a candidate (decision D36)."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0", "1.1", "1.2"]}})

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.status == "available"
    assert [item.version for item in catalog.candidates(view)] == ["1.2", "1.1", "1.0"]
    assert {item.source for item in catalog.candidates(view)} == {"registry"}


def test_registry_tracker_is_refreshed_when_unedited_and_kept_when_edited(system, tmp_path: Path) -> None:
    """A tracker copied from an older revision is refreshed if its hash still matches the record, and kept after an edit."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0"], "tracker": "old"}}, revision="a" * 40)
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    copied = write_tracker(package, "old")
    activate(system, package, "1.0")
    write_record(package, link_record("user", registry="main", revision="a" * 40, tracker_sha256=tree_hash(copied)))

    # The registry moves to a revision with a changed tracker.
    build_registry(roots["user"], {"foo": {"versions": ["1.0"], "tracker": "new"}}, revision="b" * 40)
    unedited = make_catalog(system, tmp_path)[0].describe("foo")
    (copied / "pkg.toml").write_text((copied / "pkg.toml").read_text(encoding="utf-8") + "# my edit\n", encoding="utf-8")
    edited = make_catalog(system, tmp_path)[0].describe("foo")

    assert catalog.tracker_sync(unedited).action == "refresh"
    assert catalog.tracker_sync(edited).action == "keep"


def test_tracker_is_copied_when_missing_and_current_when_unchanged(system, tmp_path: Path) -> None:
    """A linked package without a tracker gets the registry's; an unedited copy of the current tracker needs nothing."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0"], "tracker": "same"}})
    package = roots["user"] / "foo"
    write_version(package, "1.0")

    missing = make_catalog(system, tmp_path)[0].describe("foo")
    copied = write_tracker(package, "same")
    write_record(package, link_record("user", tracker_sha256=tree_hash(copied)))
    current = make_catalog(system, tmp_path)[0].describe("foo")

    assert catalog.tracker_sync(missing).action == "copy"
    assert catalog.tracker_sync(current).action == "none"


def test_local_directory_that_differs_from_the_registry_copy_is_kept_and_reported_diverged(system, tmp_path: Path) -> None:
    """A local version whose ``pkg.toml`` differs from the registry's is diverged, stays a local candidate and is never replaced."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0", "1.1"]}})
    package = roots["user"] / "foo"
    folder = write_version(package, "1.1")
    (folder / "pkg.toml").write_text((folder / "pkg.toml").read_text(encoding="utf-8").replace("tool 1.1", "my fork"), encoding="utf-8")

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.diverged == ("1.1",)
    assert [(item.version, item.source) for item in catalog.candidates(view)] == [("1.1", "local"), ("1.0", "registry")]


def test_package_with_only_a_tracker_reports_no_version_known_and_makes_no_request(system, tmp_path: Path) -> None:
    """Install of a package that has only ``_check_updates`` says to run ``check-updates`` and touches no network."""
    catalog, roots = make_catalog(system, tmp_path)
    write_tracker(roots["user"] / "foo")
    view = catalog.describe("foo")

    with pytest.raises(NoVersionKnown) as raised:
        catalog.install_candidate(view)

    assert str(raised.value) == "no version known yet; run `gupkg check-updates foo`"
    assert system.fetch_log == [] and system.process_log == []


def test_ad_hoc_roots_from_state_toml_are_listed_with_their_mode(system, tmp_path: Path) -> None:
    """A package root recorded in ``state.toml`` (and a root of packages recorded there) appears in ``list`` with its mode."""
    elsewhere = tmp_path / "elsewhere"
    write_version(elsewhere / "inplace", "3.0", populated=True)
    write_version(elsewhere / "pool" / "tool", "1.0")
    _, roots = make_catalog(system, tmp_path)
    write_toml(
        roots["user"] / ".gupkg" / "state.toml",
        {"adhoc": [
            {"path": str(elsewhere / "inplace"), "mode": "system"},
            {"path": str(elsewhere / "pool"), "mode": "user"},
        ]},
    )

    listing = make_catalog(system, tmp_path)[0].list()

    assert {(view.name, view.mode, view.root.adhoc) for view in listing.views} == {
        ("inplace", "system", True), ("tool", "user", True),
    }


def test_invalid_state_toml_is_a_warning_not_a_failure(system, tmp_path: Path) -> None:
    """A malformed ``adhoc`` list is reported as a warning and the configured roots still list."""
    _, roots = make_catalog(system, tmp_path)
    write_version(roots["user"] / "foo", "1.0")
    write_toml(roots["user"] / ".gupkg" / "state.toml", {"adhoc": [{"path": "relative", "mode": "user"}]})

    listing = make_catalog(system, tmp_path)[0].list()

    assert [view.name for view in listing.views] == ["foo"]
    assert any("state.toml" in warning for warning in listing.warnings)


def test_same_name_in_system_and_user_roots_warns_about_shadowing(system, tmp_path: Path) -> None:
    """A package in both the system and the user root is listed twice with one shadowing warning (decision D21)."""
    roots = make_catalog(system, tmp_path)[1]
    write_version(roots["system"] / "foo", "1.0")
    write_version(roots["user"] / "foo", "1.0")
    write_version(roots["user"] / "solo", "1.0")

    listing = make_catalog(system, tmp_path)[0].list()

    assert [(view.name, view.mode) for view in listing.views] == [("foo", "system"), ("foo", "user"), ("solo", "user")]
    assert [warning for warning in listing.warnings if "shadowing" in warning] == [
        "shadowing: 'foo' exists in both the system and the user root; their shims on PATH shadow each other"
    ]


def test_list_filters_by_status_mode_name_and_registry(system, tmp_path: Path) -> None:
    """``list`` filters by status, mode, name fragment and followed registry, and adds available entries on request."""
    catalog, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"alpha": {"versions": ["1.0"]}, "zeta": {"versions": ["2.0"]}})
    write_version(roots["user"] / "alpha", "1.0", populated=True)
    activate(system, roots["user"] / "alpha", "1.0")
    write_version(roots["system"] / "beta", "1.0")
    catalog = make_catalog(system, tmp_path)[0]

    everything = catalog.list(ListFilter(include_available=True)).views

    assert [(view.name, view.status) for view in everything] == [("alpha", "installed"), ("beta", "local"), ("zeta", "available")]
    assert [view.name for view in catalog.list(ListFilter(statuses=frozenset({"local"}))).views] == ["beta"]
    assert [view.name for view in catalog.list(ListFilter(modes=frozenset({"system"}))).views] == ["beta"]
    assert [view.name for view in catalog.list(ListFilter(query="LPH")).views] == ["alpha"]
    assert [view.name for view in catalog.list(ListFilter(registry="main", include_available=True)).views] == ["alpha", "zeta"]


@pytest.mark.parametrize(
    ("found", "expected", "newest"),
    [
        pytest.param("1.6", "newer", "1.5", id="newer-than-every-known"),
        pytest.param("1.2", "known", "1.5", id="already-a-valid-local-directory"),
        pytest.param("1.4", "known", "1.5", id="already-an-invalid-local-directory"),
        pytest.param("1.5", "known", "1.5", id="already-in-the-registry"),
        pytest.param("1.3", "older", "1.5", id="older-than-the-newest"),
        pytest.param("2.0.", "older", None, id="not-a-version-name"),
    ],
)
def test_compare_decides_whether_a_tracker_result_is_newer_than_every_known_version(
    system, tmp_path: Path, found: str, expected: str, newest: str | None
) -> None:
    """A result is ``newer`` only above every local directory (even an invalid one) and every registry version."""
    build_registry(make_catalog(system, tmp_path)[1]["user"], {"foo": {"versions": ["1.5"]}})
    package = tmp_path / "user" / "foo"
    write_version(package, "1.2")
    write_version(package, "1.4", text="format = 1\n")
    catalog = make_catalog(system, tmp_path)[0]
    view = catalog.describe("foo")

    verdict = catalog.compare(view, TrackResult(found))

    assert verdict.status == expected
    assert verdict.newer is (expected == "newer")
    assert verdict.newest_known == newest


def test_record_mode_that_disagrees_with_the_root_breaks_the_package(system, tmp_path: Path) -> None:
    """An install record that says another mode than the root it lies in makes the package ``broken`` with the reason."""
    catalog, roots = make_catalog(system, tmp_path)
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    write_record(package, InstallRecord(mode="system"))

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.status == "broken"
    assert any("install record says mode system" in reason for reason in view.broken_reasons)


def test_unreadable_install_record_breaks_the_package(system, tmp_path: Path) -> None:
    """An install record that is not valid TOML is reported and the package is ``broken``, never silently ignored."""
    catalog, roots = make_catalog(system, tmp_path)
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    (package / ".gupkg").mkdir()
    (package / ".gupkg" / "package.toml").write_text("this is [not toml", encoding="utf-8")

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.status == "broken"
    assert any("install record" in reason for reason in view.broken_reasons)


def test_effects_are_compared_with_the_system_only_when_asked(system, tmp_path: Path) -> None:
    """Recorded effects that the system lost make the package broken when verified; ``list`` reads nothing from the environment."""
    catalog, roots = make_catalog(system, tmp_path)
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    shortcut = tmp_path / "menu" / "Foo.lnk"
    system.write_shortcut(shortcut, ShortcutSpec(target_path=str(package / "current" / "App" / "foo.exe")))
    system.set_env(Mode.USER, "FOO_HOME", "C:\\foo", False)
    system.set_path(Mode.USER, ["C:\\bin"])
    (tmp_path / "bin").mkdir()
    effects = (
        Effect("shortcut", {"path": str(shortcut)}),
        Effect("env", {"scope": "user", "name": "FOO_HOME", "value": "C:\\foo"}),
        Effect("path", {"scope": "user", "entry": "C:\\bin"}),
        Effect("shim", {"path": str(tmp_path / "bin" / "foo.exe")}),
        Effect("something-new", {"x": 1}),
    )
    write_record(package, InstallRecord(mode="user", effects=effects))
    system.registry_log.clear()

    plain = make_catalog(system, tmp_path)[0].list().views[0]
    assert system.registry_log == [] and plain.status == "installed"

    verified = make_catalog(system, tmp_path, verify=True)[0].describe("foo")
    assert verified.status == "broken"
    assert verified.installed.drift == (f"shim missing: {tmp_path / 'bin' / 'foo.exe'}",)
    assert verified.installed.unverified == ("something-new",)

    (tmp_path / "bin" / "foo.exe").write_text("x", encoding="utf-8")
    system.set_env(Mode.USER, "FOO_HOME", "C:\\elsewhere", False)
    again = make_catalog(system, tmp_path, verify=True)[0].describe("foo")
    assert again.installed.drift == ("environment variable FOO_HOME differs",)


def test_link_to_an_entry_the_registry_does_not_have_is_a_warning(system, tmp_path: Path) -> None:
    """An explicit link whose entry is missing from the cache warns and leaves the package unfollowed, not broken."""
    _, roots = make_catalog(system, tmp_path)
    build_registry(roots["user"], {"foo": {"versions": ["1.0"]}})
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(system, package, "1.0")
    write_record(package, InstallRecord(mode="user", link=Link(registry="main", name="gone")))

    view = make_catalog(system, tmp_path)[0].describe("foo")

    assert view.status == "installed" and view.following is None
    assert any("main/gone" in warning for warning in view.warnings)


@pytest.mark.windows_integration
def test_catalog_on_real_windows_junctions_derives_installed_and_outdated(tmp_path: Path) -> None:
    """With ``WindowsSystem`` junctions an installed package is ``installed``, ``outdated`` with a newer directory, and ``broken`` when its version vanishes."""
    real = WindowsSystem()
    roots = make_catalog(real, tmp_path, elevated=False)[1]
    package = roots["user"] / "foo"
    write_version(package, "1.0", populated=True)
    activate(real, package, "1.0")
    statuses = [make_catalog(real, tmp_path, elevated=False)[0].describe("foo").status]

    write_version(package, "1.1")
    statuses.append(make_catalog(real, tmp_path, elevated=False)[0].describe("foo").status)
    (package / "1.0" / "pkg.toml").unlink()
    for child in (package / "1.0" / "App").iterdir():
        child.unlink()
    (package / "1.0" / "App").rmdir()
    (package / "1.0").rmdir()
    statuses.append(make_catalog(real, tmp_path, elevated=False)[0].describe("foo").status)

    assert statuses == ["installed", "outdated", "broken"]


def test_roots_that_do_not_exist_hold_no_packages_and_cause_no_warning(system, tmp_path: Path) -> None:
    """A configured root that has never been used (no folder yet) lists as empty without a warning."""
    roots = make_catalog(system, tmp_path)[1]
    write_version(roots["user"] / "foo", "1.0")
    roots["system"].rmdir()

    listing = make_catalog(system, tmp_path)[0].list()

    assert [view.name for view in listing.views] == ["foo"]
    assert listing.warnings == ()
