"""Cover the trust contract of package code: an elevated run never loads or starts code a user can modify.

Hooks and populate scripts are Python modules and programs read from a version directory. In an
elevated run (decision D19) they are refused unless the directory is admin-only, before anything is
imported or started. Covers ``load_hook``, ``run_custom_populate`` and the ``[[prepare]] module``
step, the fail-closed marker ``GUPKG_ELEVATED``, and hooks whose import-time code raises. The
admin-only answer is ``FakeSystem(untrusted_dirs=...)``. Out of scope: how the engine decides to
elevate (issue 028).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import PrepareModule
from gupkg.services.hooks import HookContext, HookError, HookPaths, load_hook
from gupkg.services.http import HttpClient
from gupkg.services.populate import run_custom_populate
from gupkg.services.prepare import PrepareError, run_prepare
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult


@pytest.fixture(autouse=True)
def not_marked_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make these tests independent of an ambient GUPKG_ELEVATED marker; the marker cases set it themselves."""
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)


@pytest.fixture
def version(tmp_path: Path) -> Path:
    """A version directory whose hook writes `executed.txt` beside itself when it is imported or called."""
    folder = tmp_path / "pkg" / "1.0"
    (folder / "pkg.local").mkdir(parents=True)
    (folder / "pkg.local" / "planted.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('executed.txt').write_text('ran')\n"
        "PKG_MODULE_API = 2\n\ndef populate(ctx):\n    Path(__file__).with_name('called.txt').write_text('ran')\n"
        "\ndef prepare(ctx):\n    Path(__file__).with_name('prepared.txt').write_text('ran')\n",
        encoding="utf-8",
    )
    (folder / "pkg.local" / "populate.cmd").write_text("@echo off\n", encoding="utf-8")
    return folder


def context(system: FakeSystem, version: Path) -> HookContext:
    """Return a hook context for *version* over *system*."""
    return HookContext("tool", "1.0", system, HttpClient(system, env={}), HookPaths(version.parent, version, app=version / "App"))


def elevated(tmp_path: Path, version: Path, *, untrusted: bool) -> FakeSystem:
    """Return an elevated FakeSystem that calls *version* untrusted (user-writable) or admin-only."""
    return FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={version} if untrusted else set())


def test_an_elevated_run_refuses_to_import_a_hook_from_a_user_writable_directory(tmp_path: Path, version: Path) -> None:
    """Nothing is imported: the planted module's top-level code never runs."""
    system = elevated(tmp_path, version, untrusted=True)

    with pytest.raises(HookError, match="Refusing to run code .* not admin-only"):
        load_hook(version, "pkg.local/planted.py", system)

    assert not (version / "pkg.local" / "executed.txt").exists()


def test_an_elevated_run_loads_a_hook_from_an_admin_only_directory(tmp_path: Path, version: Path) -> None:
    """The same hook loads when only administrators can change its folder."""
    module = load_hook(version, "pkg.local/planted.py", elevated(tmp_path, version, untrusted=False))

    assert module.PKG_MODULE_API == 2 and (version / "pkg.local" / "executed.txt").exists()


def test_a_normal_run_may_run_the_users_own_hook(tmp_path: Path, version: Path) -> None:
    """Without elevation the code is the user's own and the folder is not judged."""
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={version})

    load_hook(version, "pkg.local/planted.py", system)

    assert (version / "pkg.local" / "executed.txt").exists()


def test_the_elevation_marker_counts_even_when_the_token_says_otherwise(tmp_path: Path, version: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GUPKG_ELEVATED=1` (set by the launcher) makes the run strict, and the check fails closed."""
    monkeypatch.setenv("GUPKG_ELEVATED", "1")
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={version})

    with pytest.raises(HookError, match="not admin-only"):
        load_hook(version, "pkg.local/planted.py", system)

    assert not (version / "pkg.local" / "executed.txt").exists()


def test_a_planted_pkg_local_folder_is_judged_too(tmp_path: Path, version: Path) -> None:
    """A user-writable `pkg.local` inside an admin-only version directory is still refused."""
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={version / "pkg.local"})

    with pytest.raises(HookError, match="pkg.local"):
        load_hook(version, "pkg.local/planted.py", system)

    assert not (version / "pkg.local" / "executed.txt").exists()


def test_an_elevated_run_refuses_a_populate_script_and_a_prepare_module_from_an_untrusted_directory(tmp_path: Path, version: Path) -> None:
    """The Python populate hook, a `.cmd` populate script and a `[[prepare]] module` step are all refused before they start."""
    started: list[ProcessCall] = []
    system = FakeSystem(
        tmp_path / "machine", elevated=True, untrusted_dirs={version},
        processes={"populate.cmd": lambda call: started.append(call) or ProcessResult(0)}, real_7zip=False,
    )
    app = version / "App"
    app.mkdir()

    with pytest.raises(HookError, match="not admin-only"):
        run_custom_populate(context(system, version), "pkg.local/planted.py")
    with pytest.raises(HookError, match="not admin-only"):
        run_custom_populate(context(system, version), "pkg.local/populate.cmd")
    with pytest.raises(PrepareError, match="not admin-only"):
        run_prepare([PrepareModule("pkg.local/planted.py")], app, context(system, version))

    assert started == []
    assert not [name for name in ("executed.txt", "called.txt", "prepared.txt") if (version / "pkg.local" / name).exists()]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("def broken(:\n", id="syntax-error"),
        pytest.param("raise ValueError('boom')\n", id="value-error"),
        pytest.param("1 / 0\n", id="zero-division"),
        pytest.param("import json\nraise KeyError('k')\n", id="key-error"),
    ],
)
def test_any_exception_in_a_hooks_import_time_code_is_a_hook_error(tmp_path: Path, system: FakeSystem, body: str) -> None:
    """A hook that fails while loading raises HookError naming the hook, never a raw exception from its code."""
    (tmp_path / "pkg.local").mkdir()
    (tmp_path / "pkg.local" / "bad.py").write_text("PKG_MODULE_API = 2\n" + body, encoding="utf-8")

    with pytest.raises(HookError, match="pkg.local/bad.py"):
        load_hook(tmp_path, "pkg.local/bad.py", system)


def test_a_prepare_module_that_fails_to_load_is_a_prepare_error(tmp_path: Path, system: FakeSystem) -> None:
    """`run_prepare` turns a hook's load failure into a PrepareError that names the step."""
    version = tmp_path / "pkg" / "1.0"
    (version / "pkg.local").mkdir(parents=True)
    (version / "pkg.local" / "bad.py").write_text("PKG_MODULE_API = 2\nraise RuntimeError('boom')\n", encoding="utf-8")
    app = version / "App"
    app.mkdir()

    with pytest.raises(PrepareError, match=r"\[\[prepare\]\] module 'pkg.local/bad.py'.*boom"):
        run_prepare([PrepareModule("pkg.local/bad.py")], app, context(system, version))
