r"""Cover that the registry repository's own checks pass against this checkout of gupkg.

The registry (`gupkg-registry`) keeps the data of every package and tests it: each tracker finds its version in the
vendor's answers, each package passes the package checker, the registry file and the fingerprints are in order. Those
tests run gupkg's real code, so a change here can break the registry's data; and the registry's data is never copied into
gupkg's tests (`_docs/tests.md` section 3). This release-tier test runs `_scripts\test.cmd` of the registry with
`GUPKG_REPO` set to this checkout and requires every registry test to pass.

The registry folder is found through the environment variable `GUPKG_REGISTRY_REPO`, or else as a folder named
`gupkg-registry` in the root of this checkout or in any folder above it (a worktree of gupkg under `.claude\worktrees`
finds the registry beside the main checkout). When there is none, the test is skipped and says why. The run only reads
the registry: it fetches nothing, pushes nothing, and the test requires the registry's working tree to look the same
before and after.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.release

ROOT = Path(__file__).resolve().parents[2]


def find_registry() -> Path | None:
    """Return the registry checkout (a folder with `_scripts\\test.cmd`), or None when there is none."""
    chosen = os.environ.get("GUPKG_REGISTRY_REPO")
    candidates = [Path(chosen)] if chosen else [folder / "gupkg-registry" for folder in (ROOT, *ROOT.parents)]
    return next((path for path in candidates if (path / "_scripts" / "test.cmd").is_file()), None)


def working_tree(registry: Path) -> str:
    """Return `git status --short --ignored` of *registry* (empty when git is not there or it is not a checkout)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(registry), "status", "--short", "--ignored"], capture_output=True, text=True, check=False, timeout=60
        )
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def test_the_registrys_own_tests_pass_against_this_checkout() -> None:
    """Every test of the registry repository passes with `GUPKG_REPO` set to this checkout, and its working tree is left as it was."""
    registry = find_registry()
    if registry is None:
        pytest.skip("no gupkg-registry folder found: set GUPKG_REGISTRY_REPO, or put it in the checkout or in a folder above it")
    before = working_tree(registry)
    environment = {**os.environ, "GUPKG_REPO": str(ROOT), "GUPKG_NO_PAUSE": "1", "PYTHONDONTWRITEBYTECODE": "1"}

    done = subprocess.run(
        ["cmd", "/c", str(registry / "_scripts" / "test.cmd"), "-p", "no:cacheprovider"],
        capture_output=True, text=True, env=environment, check=False, timeout=1800,
    )

    summary = re.findall(r"\d+ (?:passed|failed|error|errors|skipped)[^\n]*", done.stdout)
    print(f"registry {registry}: {summary[-1] if summary else 'no summary line'}")
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-1000:]
    assert working_tree(registry) == before, "running the registry's tests changed its working tree"
