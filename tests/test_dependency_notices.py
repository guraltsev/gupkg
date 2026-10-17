"""Cover the advisory warning about newer releases of pinned dependencies.

Behavior covered: which locked dependencies produce a warning, that hostile or
broken PyPI data produces no warning and no error, the once-a-day cache, the
behavior when the cache cannot be written, and the manager setting that turns
the check off.

Mocked boundary: the PyPI request (a fake fetch function returns JSON
documents). Real: the lockfile, the cache file, and the version rules. Out of
scope: real network access and terminal rendering of the warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.core import ConfigValidationError
from gupkg.dependency_notices import CACHE_NAME, dependency_notices
from gupkg.manager import load_manager_config

LOCK = f"textual==8.2.8 --hash=sha256:{'a' * 64}\nrich==15.0.0 --hash=sha256:{'b' * 64}\n"
NOW = 1_000_000.0


def _document(**releases: list[dict]) -> dict:
    """Build a PyPI-style document from version -> file lists (underscores stand for dots)."""
    return {"releases": {version.replace("_", "."): files for version, files in releases.items()}}


LIVE = [{"yanked": False}]
YANKED = [{"yanked": True}]


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A package directory holding a two-distribution lockfile."""
    (tmp_path / "gupkg-deps.lock").write_text(LOCK, encoding="utf-8")
    return tmp_path


def _fetch(documents: dict[str, dict], calls: list[str] | None = None):
    """Return a fake PyPI fetcher serving *documents* by distribution name."""

    def fetch(name: str, timeout: float) -> dict:
        if calls is not None:
            calls.append(name)
        return documents[name]

    return fetch


def test_newer_stable_release_produces_the_documented_warning(package: Path) -> None:
    """A pinned dependency with a newer stable release is reported with the repair command."""
    documents = {
        "textual": _document(**{"8_2_8": LIVE, "8_3_0": LIVE}),
        "rich": _document(**{"15_0_0": LIVE}),
    }

    notices = dependency_notices(package, now=NOW, fetch=_fetch(documents))

    assert notices == ["textual 8.2.8 is pinned; 8.3.0 is available. Run: gupkg manager self update-deps"]


def test_prereleases_and_yanked_releases_never_trigger_a_warning(package: Path) -> None:
    """Only stable, non-yanked releases count as available."""
    documents = {
        "textual": _document(**{"8_2_8": LIVE, "9_0_0": YANKED, "9_1_0rc1": LIVE, "9_1_0.dev1": LIVE}),
        "rich": _document(**{"15_0_0": LIVE}),
    }

    assert dependency_notices(package, now=NOW, fetch=_fetch(documents)) == []


def test_hostile_response_text_cannot_reach_the_warning(package: Path) -> None:
    """Version strings that are not plain numbers are ignored, so control sequences never print."""
    hostile = "9.9.9\x1b]0;owned\x07"
    documents = {
        "textual": {"releases": {hostile: LIVE, "8.2.8": LIVE}},
        "rich": _document(**{"15_0_0": LIVE}),
    }

    assert dependency_notices(package, now=NOW, fetch=_fetch(documents)) == []


def test_unreachable_index_is_silent_and_not_retried_within_a_day(package: Path) -> None:
    """An offline machine sees nothing, and the failed attempt is not repeated until the next day."""
    calls: list[str] = []

    def offline(name: str, timeout: float) -> dict:
        calls.append(name)
        raise OSError("offline")

    assert dependency_notices(package, now=NOW, fetch=offline) == []
    assert dependency_notices(package, now=NOW + 3600, fetch=offline) == []
    assert len(calls) == 1
    dependency_notices(package, now=NOW + 25 * 3600, fetch=offline)
    assert len(calls) == 2


def test_result_is_cached_for_a_day(package: Path) -> None:
    """A second check inside a day reuses the cached answer instead of asking PyPI again."""
    documents = {
        "textual": _document(**{"8_2_8": LIVE, "8_3_0": LIVE}),
        "rich": _document(**{"15_0_0": LIVE}),
    }
    calls: list[str] = []

    first = dependency_notices(package, now=NOW, fetch=_fetch(documents, calls))
    second = dependency_notices(package, now=NOW + 60, fetch=_fetch(documents, calls))

    assert first == second and first
    assert sorted(calls) == ["rich", "textual"]
    assert (package / CACHE_NAME).is_file()


def test_unwritable_cache_location_is_skipped_without_error(package: Path) -> None:
    """If the cache cannot be written beside the lockfile, the check still works and writes nowhere else."""
    (package / CACHE_NAME).mkdir()
    documents = {
        "textual": _document(**{"8_2_8": LIVE, "8_3_0": LIVE}),
        "rich": _document(**{"15_0_0": LIVE}),
    }

    notices = dependency_notices(package, now=NOW, fetch=_fetch(documents))

    assert len(notices) == 1
    assert sorted(path.name for path in package.iterdir()) == [CACHE_NAME, "gupkg-deps.lock"]


def test_a_tampered_cache_cannot_inject_text(package: Path) -> None:
    """Cached values are validated like network data before they can be printed."""
    (package / CACHE_NAME).write_text(
        '{"checked": %s, "locked": {"textual": "8.2.8", "rich": "15.0.0"}, '
        '"latest": {"textual": "9.9\\u001b[31m", "rich": "15.0.0"}}' % NOW,
        encoding="utf-8",
    )

    assert dependency_notices(package, now=NOW + 1, fetch=_fetch({})) == []


def test_check_can_be_disabled(package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The enabled switch and the environment variable each turn the check off without any request."""
    calls: list[str] = []
    documents = {"textual": _document(**{"9_0_0": LIVE}), "rich": _document(**{"9_0_0": LIVE})}

    assert dependency_notices(package, enabled=False, now=NOW, fetch=_fetch(documents, calls)) == []
    monkeypatch.setenv("GUPKG_NO_DEPENDENCY_UPDATE_CHECK", "1")
    assert dependency_notices(package, now=NOW, fetch=_fetch(documents, calls)) == []
    assert calls == []


def test_manager_configuration_can_disable_the_check(tmp_path: Path) -> None:
    """`[self] dependency_update_check = false` is accepted, defaults to on, and rejects non-booleans."""
    config_path = tmp_path / "gupkg-config.toml"
    base = (
        'mode = "manager"\nschema_version = 2\n\n[packages]\nsystem = "system"\nuser = "user"\n\n'
        '[bin]\nsystem = "system-bin"\nuser = "user-bin"\n'
    )

    config_path.write_text(base, encoding="utf-8")
    assert load_manager_config(config_path).dependency_update_check is True

    config_path.write_text(base + "\n[self]\ndependency_update_check = false\n", encoding="utf-8")
    assert load_manager_config(config_path).dependency_update_check is False

    config_path.write_text(base + '\n[self]\ndependency_update_check = "no"\n', encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="dependency_update_check"):
        load_manager_config(config_path)
