"""Cover every case of the package state table (design 5.3) through the catalog's public calls.

Behavior covered: for each row of the table (registry only, local versions, local tracker,
registry plus local when linked and when opted out, broken) a root is built on a temporary
folder with a fixture registry cache, and the test asserts the status that ``Catalog.list``
shows, the version that ``install`` would choose, the version that ``update`` would choose
(or the refusal) and the follow state. Rows differ only in the disk they build, so each row
is one labelled case.

Real: package roots, real ``current`` junctions (through ``FakeSystem``, whose links are real),
registry caches in the layout a sync publishes, and the catalog code. Mocked: nothing.
Out of scope: executing installs (issue 028), running trackers and the network.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from tests.catalog_support import (
    activate,
    build_registry,
    link_record,
    make_catalog,
    version_text,
    write_record,
    write_tracker,
    write_version,
)
from gupkg.catalog.catalog import ListFilter, NeedsRepair, NoVersionKnown, NotInstalled


@dataclass
class Ctx:
    """The temporary machine a case builds on: the user root, the registry cache root and the system."""

    system: object
    user: Path
    registry_root: Path

    @property
    def foo(self) -> Path:
        """The package root ``<user root>/foo``."""
        return self.user / "foo"

    def registry(self, **packages: dict) -> None:
        """Publish a registry cache with the named packages."""
        build_registry(self.registry_root, packages)


@dataclass
class Case:
    """One row of design 5.3: how to build the disk and what the catalog must answer."""

    build: Callable[[Ctx], None]
    status: str
    install: str
    update: str
    check: str = "nothing"
    following: str | None = None
    note: str | None = None
    tracker: str = "none"
    candidates: tuple[str, ...] = ()
    diverged: tuple[str, ...] = ()


def installed(ctx: Ctx, current: str, versions: tuple[str, ...], populated: tuple[str, ...] | None = None) -> None:
    """Build ``foo`` with *versions*, the *populated* ones (default: only *current*) and ``current`` at *current*."""
    populated = (current,) if populated is None else populated
    for version in versions:
        write_version(ctx.foo, version, populated=version in populated)
    activate(ctx.system, ctx.foo, current)


def broken_dangling(ctx: Ctx) -> None:
    """Build ``foo`` whose ``current`` points at a version directory that was deleted."""
    installed(ctx, "1.0", ("1.0", "1.1"))
    shutil.rmtree(ctx.foo / "1.0")


CASES = {
    "R-registry-only": Case(
        lambda c: c.registry(foo={"versions": ["1.0", "1.1"], "tracker": "r"}),
        status="available", install="registry:1.1", update=NotInstalled.__name__, following="main/foo", check="not-local",
        tracker="copy", candidates=("1.1@registry", "1.0@registry"),
    ),
    "R-registry-only-with-tracker-and-no-version": Case(
        lambda c: c.registry(foo={"versions": [], "tracker": "r"}),
        status="available", install=NoVersionKnown.__name__, update=NotInstalled.__name__, following="main/foo", tracker="copy", check="not-local",
    ),
    "V-local-versions-not-installed": Case(
        lambda c: [write_version(c.foo, "1.0"), write_version(c.foo, "1.1")],
        status="local", install="local:1.1", update=NotInstalled.__name__, candidates=("1.1@local", "1.0@local"),
    ),
    "V-installed-up-to-date": Case(
        lambda c: installed(c, "1.1", ("1.0", "1.1")),
        status="installed", install="local:1.1", update="None",
    ),
    "V-installed-newer-local-version": Case(
        lambda c: installed(c, "1.0", ("1.0", "1.1")),
        status="outdated", install="local:1.0", update="local:1.1", candidates=("1.1@local",),
    ),
    "T-tracker-and-versions-not-installed": Case(
        lambda c: [write_version(c.foo, "1.0"), write_version(c.foo, "1.1"), write_tracker(c.foo)],
        status="local", install="local:1.1", update=NotInstalled.__name__, candidates=("1.1@local", "1.0@local"), check="run",
    ),
    "T-tracker-only-not-installed": Case(
        lambda c: write_tracker(c.foo),
        status="local", install=NoVersionKnown.__name__, update=NotInstalled.__name__, check="run",
    ),
    "T-installed-with-version-written-by-check-updates": Case(
        lambda c: [installed(c, "1.0", ("1.0", "1.1")), write_tracker(c.foo)],
        status="outdated", install="local:1.0", update="local:1.1", candidates=("1.1@local",), check="run",
    ),
    "RVT-linked-not-installed": Case(
        lambda c: [c.registry(foo={"versions": ["1.0", "1.2"], "tracker": "r"}), write_version(c.foo, "1.0"), write_tracker(c.foo)],
        status="local", install="local:1.0", update=NotInstalled.__name__, following="main/foo", check="run",
        tracker="keep", candidates=("1.2@registry", "1.0@local"),
    ),
    "RVT-linked-no-local-tracker-registry-has-one": Case(
        lambda c: [c.registry(foo={"versions": ["1.0"], "tracker": "r"}), write_version(c.foo, "1.0")],
        status="local", install="local:1.0", update=NotInstalled.__name__, following="main/foo",
        check="run", tracker="copy", candidates=("1.0@local",),
    ),
    "RVT-linked-installed-registry-newer": Case(
        lambda c: [c.registry(foo={"versions": ["1.0", "1.1", "1.2"]}), installed(c, "1.0", ("1.0",))],
        status="outdated", install="local:1.0", update="registry:1.2", following="main/foo",
        candidates=("1.2@registry", "1.1@registry"),
    ),
    "RVT-linked-installed-local-newer-than-registry": Case(
        lambda c: [c.registry(foo={"versions": ["1.0", "1.2"]}), installed(c, "1.0", ("1.0", "1.5"))],
        status="outdated", install="local:1.0", update="local:1.5", following="main/foo",
        candidates=("1.5@local", "1.2@registry"),
    ),
    "RVT-opted-out": Case(
        lambda c: [
            c.registry(foo={"versions": ["1.0", "1.2"]}), installed(c, "1.0", ("1.0",)),
            write_record(c.foo, link_record("user", unlinked=True)),
        ],
        status="installed", install="local:1.0", update="None", note="same name in registry: main (not followed)",
    ),
    "RVT-explicit-link-to-another-entry": Case(
        lambda c: [
            c.registry(foo={"versions": ["1.9"]}, bar={"versions": ["2.0"]}), installed(c, "1.0", ("1.0",)),
            write_record(c.foo, link_record("user", registry="main", name="bar")),
        ],
        status="outdated", install="local:1.0", update="registry:2.0", following="main/bar", candidates=("2.0@registry",),
    ),
    "Broken-current-dangles": Case(
        broken_dangling,
        status="broken", install="local:1.1", update=NeedsRepair.__name__, candidates=("1.1@local",),
    ),
    "Broken-active-app-missing": Case(
        lambda c: installed(c, "1.0", ("1.0",), populated=()),
        status="broken", install="local:1.0", update=NeedsRepair.__name__,
    ),
    "RVT-local-copy-differs-from-registry-is-diverged": Case(
        lambda c: [
            c.registry(foo={"versions": ["1.0", "1.1"]}),
            write_version(c.foo, "1.1", text=version_text("1.1").replace("tool 1.1", "tool 1.1 edited locally")),
        ],
        status="local", install="local:1.1", update=NotInstalled.__name__, following="main/foo",
        candidates=("1.1@local", "1.0@registry"), diverged=("1.1",),
    ),
}


def outcome(action: Callable[[], object]) -> str:
    """Return ``source:version`` for a candidate, ``None`` for no candidate, or the error's class name."""
    try:
        found = action()
    except Exception as exc:  # the table names the refusal by its class
        return type(exc).__name__
    return "None" if found is None else f"{found.source}:{found.version}"


@pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
def test_each_row_of_the_state_table_gives_its_status_candidates_and_refusals(case: Case, system, tmp_path: Path) -> None:
    """The catalog shows the row's status, follow state, install and update choice, and tracker action.

    The disk is built from nothing for every row, the catalog is opened on it, and the answers
    come from ``list``, ``install_candidate``, ``update_candidate``, ``candidates`` and ``tracker_sync``.
    """
    roots = make_catalog(system, tmp_path)[1]
    case.build(Ctx(system, roots["user"], roots["user"]))

    # The catalog is opened after the disk is built, as a command would open it.
    catalog = make_catalog(system, tmp_path)[0]
    listing = catalog.list(ListFilter(include_available=True))
    [view] = [item for item in listing.views if item.name == "foo"]

    assert view.status == case.status
    assert view.following == case.following
    assert view.note == case.note
    assert outcome(lambda: catalog.install_candidate(view)) == case.install
    assert outcome(lambda: catalog.update_candidate(view)) == case.update
    assert catalog.check_updates_plan(view).action == case.check
    assert catalog.tracker_sync(view).action == case.tracker
    assert tuple(f"{item.version}@{item.source}" for item in catalog.candidates(view)) == case.candidates
    assert view.diverged == case.diverged
