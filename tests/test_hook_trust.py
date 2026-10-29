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
from gupkg.selfmgmt.elevation import UntrustedLocationError
from gupkg.services.hooks import (
    HookContext,
    HookError,
    HookPaths,
    call_populate,
    call_prepare,
    call_track,
    call_unpack,
    load_hook,
)
from gupkg.services.http import HttpClient
from gupkg.services.populate import run_custom_populate
from gupkg.services.prepare import PrepareError, run_prepare
from gupkg.system.fake import FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.scope_support import scope_of


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
    return HookContext("tool", "1.0", system, scope_of(system), HttpClient(system, env={}), HookPaths(version.parent, version, app=version / "App"))


def elevated(tmp_path: Path, version: Path, *, untrusted: bool) -> FakeSystem:
    """Return an elevated FakeSystem that calls *version* untrusted (user-writable) or admin-only."""
    return FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={version} if untrusted else set())


def test_an_elevated_run_refuses_to_import_a_hook_from_a_user_writable_directory(tmp_path: Path, version: Path) -> None:
    """Nothing is imported: the planted module's top-level code never runs."""
    system = elevated(tmp_path, version, untrusted=True)

    with pytest.raises(HookError, match="Refusing to run code .* not admin-only"):
        load_hook(version, "pkg.local/planted.py", scope_of(system))

    assert not (version / "pkg.local" / "executed.txt").exists()


def test_an_elevated_run_loads_a_hook_from_an_admin_only_directory(tmp_path: Path, version: Path) -> None:
    """The same hook loads when only administrators can change its folder."""
    module = load_hook(version, "pkg.local/planted.py", scope_of(elevated(tmp_path, version, untrusted=False)))

    assert module.PKG_MODULE_API == 2 and (version / "pkg.local" / "executed.txt").exists()


def test_a_normal_run_may_run_the_users_own_hook(tmp_path: Path, version: Path) -> None:
    """Without elevation the code is the user's own and the folder is not judged."""
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={version})

    load_hook(version, "pkg.local/planted.py", scope_of(system))

    assert (version / "pkg.local" / "executed.txt").exists()


def test_the_elevation_marker_counts_even_when_the_token_says_otherwise(tmp_path: Path, version: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`GUPKG_ELEVATED=1` (set by the launcher) makes the run strict, and the check fails closed."""
    monkeypatch.setenv("GUPKG_ELEVATED", "1")
    system = FakeSystem(tmp_path / "machine", untrusted_dirs={version})

    with pytest.raises(HookError, match="not admin-only"):
        load_hook(version, "pkg.local/planted.py", scope_of(system))

    assert not (version / "pkg.local" / "executed.txt").exists()


def test_a_planted_pkg_local_folder_is_judged_too(tmp_path: Path, version: Path) -> None:
    """A user-writable `pkg.local` inside an admin-only version directory is still refused."""
    system = FakeSystem(tmp_path / "machine", elevated=True, untrusted_dirs={version / "pkg.local"})

    with pytest.raises(HookError, match="pkg.local"):
        load_hook(version, "pkg.local/planted.py", scope_of(system))

    assert not (version / "pkg.local" / "executed.txt").exists()


def test_the_script_file_and_the_folders_between_it_and_the_version_directory_are_judged_too(tmp_path: Path, version: Path) -> None:
    """A user-writable script file, or a user-writable subfolder that holds it, is refused in an elevated run."""
    nested = version / "pkg.local" / "sub"
    nested.mkdir()
    (nested / "deep.py").write_text("PKG_MODULE_API = 2\nfrom pathlib import Path\nPath(__file__).with_name('ran.txt').write_text('x')\n", encoding="utf-8")
    file_untrusted = FakeSystem(tmp_path / "m1", elevated=True, untrusted_dirs={nested / "deep.py"})
    folder_untrusted = FakeSystem(tmp_path / "m2", elevated=True, untrusted_dirs={nested})
    trusted = FakeSystem(tmp_path / "m3", elevated=True)

    with pytest.raises(HookError, match=r"deep\.py"):
        load_hook(version, "pkg.local/sub/deep.py", scope_of(file_untrusted))
    with pytest.raises(HookError, match="sub"):
        load_hook(version, "pkg.local/sub/deep.py", scope_of(folder_untrusted))
    assert not (nested / "ran.txt").exists()
    load_hook(version, "pkg.local/sub/deep.py", scope_of(trusted))
    assert (nested / "ran.txt").exists()


def test_a_script_file_that_is_untrusted_blocks_a_non_python_populate_script_too(tmp_path: Path, version: Path) -> None:
    """The `.cmd` script itself is judged, not only its folder."""
    started: list[ProcessCall] = []
    system = FakeSystem(
        tmp_path / "machine", elevated=True, untrusted_dirs={version / "pkg.local" / "populate.cmd"},
        processes={"populate.cmd": lambda call: started.append(call) or ProcessResult(0)}, real_7zip=False,
    )

    with pytest.raises(HookError, match=r"populate\.cmd"):
        run_custom_populate(context(system, version), "pkg.local/populate.cmd")

    assert started == []


@pytest.mark.parametrize("body", ["import sys\nsys.exit(3)\n", "raise SystemExit('bye')\n"], ids=["sys-exit", "system-exit"])
def test_a_hook_that_exits_while_loading_is_a_hook_error(tmp_path: Path, system: FakeSystem, body: str) -> None:
    """`sys.exit()` in a hook's top-level code does not end gupkg: it is a HookError naming the hook."""
    (tmp_path / "pkg.local").mkdir()
    (tmp_path / "pkg.local" / "quit.py").write_text("PKG_MODULE_API = 2\n" + body, encoding="utf-8")

    with pytest.raises(HookError, match=r"pkg\.local/quit\.py failed while loading: SystemExit"):
        load_hook(tmp_path, "pkg.local/quit.py", scope_of(system))


@pytest.mark.parametrize("entry", ["track", "unpack", "prepare", "populate"])
def test_a_hook_that_exits_inside_an_entry_point_is_a_hook_error(tmp_path: Path, system: FakeSystem, entry: str) -> None:
    """`sys.exit()` inside `track`, `unpack`, `prepare` or `populate` is a HookError, like at import time."""
    (tmp_path / "pkg.local").mkdir()
    body = f"import sys\nPKG_MODULE_API = 2\n\ndef {entry}(ctx):\n    sys.exit(4)\n"
    (tmp_path / "pkg.local" / "quit.py").write_text(body, encoding="utf-8")
    module = load_hook(tmp_path, "pkg.local/quit.py", scope_of(system))
    call = {"track": call_track, "unpack": call_unpack, "prepare": call_prepare, "populate": call_populate}[entry]

    with pytest.raises(HookError, match=rf"{entry}\(ctx\) called sys.exit\(4\)"):
        call(module, context(system, tmp_path))


def test_a_keyboard_interrupt_in_a_hook_still_stops_the_run(tmp_path: Path, system: FakeSystem) -> None:
    """An interrupt from the user is not swallowed as a hook failure."""
    (tmp_path / "pkg.local").mkdir()
    (tmp_path / "pkg.local" / "stop.py").write_text("PKG_MODULE_API = 2\nraise KeyboardInterrupt\n", encoding="utf-8")

    with pytest.raises(KeyboardInterrupt):
        load_hook(tmp_path, "pkg.local/stop.py", scope_of(system))


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
        load_hook(tmp_path, "pkg.local/bad.py", scope_of(system))


def test_a_prepare_module_that_fails_to_load_is_a_prepare_error(tmp_path: Path, system: FakeSystem) -> None:
    """`run_prepare` turns a hook's load failure into a PrepareError that names the step."""
    version = tmp_path / "pkg" / "1.0"
    (version / "pkg.local").mkdir(parents=True)
    (version / "pkg.local" / "bad.py").write_text("PKG_MODULE_API = 2\nraise RuntimeError('boom')\n", encoding="utf-8")
    app = version / "App"
    app.mkdir()

    with pytest.raises(PrepareError, match=r"\[\[prepare\]\] module 'pkg.local/bad.py'.*boom"):
        run_prepare([PrepareModule("pkg.local/bad.py")], app, context(system, version))


def test_a_hook_folder_that_is_user_writable_is_refused_through_require_trusted_in_system_mode_and_runs_in_user_mode(
    tmp_path: Path, version: Path
) -> None:
    """The trust check is `Scope.require_trusted`: the same folder is refused by the system-mode scope and accepted by the user-mode one."""
    system_scope = scope_of(FakeSystem(tmp_path / "m1", elevated=True, untrusted_dirs={version}))
    user_scope = scope_of(FakeSystem(tmp_path / "m2", untrusted_dirs={version}))

    with pytest.raises(UntrustedLocationError, match="the hook folder"):
        system_scope.require_trusted(version, "the hook folder")
    user_scope.require_trusted(version, "the hook folder")
    with pytest.raises(HookError, match="not admin-only"):
        load_hook(version, "pkg.local/planted.py", system_scope)
    assert not (version / "pkg.local" / "executed.txt").exists()

    load_hook(version, "pkg.local/planted.py", user_scope)

    assert (version / "pkg.local" / "executed.txt").exists()
