"""Cover the ``[[prepare]]`` steps: remove, rename, copy, write and module, and their confinement.

The steps run on real folders below ``tmp_path``; links are real junctions made through
``FakeSystem`` (the same junction code as ``WindowsSystem``). A ``module`` step loads a real
hook file. Out of scope: parsing the steps from a manifest (``test_manifest_format2``) and
unpacking (``test_unpack``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import (
    ManifestKind,
    PrepareCopy,
    PrepareModule,
    PrepareRemove,
    PrepareRename,
    PrepareWrite,
    parse_manifest,
)
from gupkg.services.hooks import HookContext, HookPaths
from gupkg.services.http import HttpClient
from gupkg.services.populate import prepare_app
from gupkg.services.prepare import PrepareError, run_prepare
from gupkg.system.fake import FakeSystem
from tests.scope_support import scope_of


@pytest.fixture(autouse=True)
def not_marked_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make these tests independent of an ambient GUPKG_ELEVATED marker (it makes hook loading strict)."""
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """A staged `App` with a few files and an `outside` folder next to it that no step may touch."""
    root = tmp_path / "App"
    (root / "$PLUGINSDIR").mkdir(parents=True)
    (root / "$PLUGINSDIR" / "x.dll").write_text("x", encoding="utf-8")
    (root / "bin").mkdir()
    (root / "bin" / "tool.exe").write_text("tool", encoding="utf-8")
    (root / "uninstall.exe").write_text("u", encoding="utf-8")
    (root / "lockhuntersetup_portable_3-4-3.exe").write_text("lh", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    return root


def files(root: Path) -> set[str]:
    """Return the relative posix paths of every file below *root*."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def test_remove_deletes_files_folders_and_globs_and_ignores_what_is_absent(app: Path) -> None:
    """A literal folder name with a dollar sign, a file and a glob are removed; a path that is not there is not an error."""
    run_prepare([PrepareRemove(("$PLUGINSDIR", "uninstall.exe", "bin/*.exe", "never-existed.txt"))], app)

    assert files(app) == {"lockhuntersetup_portable_3-4-3.exe"}
    assert not (app / "$PLUGINSDIR").exists()


def test_remove_deletes_a_link_but_never_its_target(app: Path, system: FakeSystem, tmp_path: Path) -> None:
    """A junction inside `App` is removed itself; the folder it points to, outside `App`, keeps its files."""
    system.create_junction(app / "linked", tmp_path / "outside")

    run_prepare([PrepareRemove(("linked",))], app)

    assert not (app / "linked").exists() and (tmp_path / "outside" / "secret.txt").read_text() == "secret"


def test_remove_does_not_reach_through_a_link(app: Path, system: FakeSystem, tmp_path: Path) -> None:
    """A path that passes through a junction is refused, so the outside file survives."""
    system.create_junction(app / "linked", tmp_path / "outside")

    with pytest.raises(PrepareError, match="passes through a link"):
        run_prepare([PrepareRemove(("linked/secret.txt",))], app)

    assert (tmp_path / "outside" / "secret.txt").exists()


def test_rename_gives_the_one_match_a_stable_name(app: Path) -> None:
    """The glob `lockhuntersetup_portable_*.exe` matches one file and it becomes `LockHunter.exe`."""
    run_prepare([PrepareRename("lockhuntersetup_portable_*.exe", "LockHunter.exe")], app)

    assert "LockHunter.exe" in files(app) and not list(app.glob("lockhuntersetup_*"))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param("nothing_*.exe", "matches 0 items", id="no-match"),
        pytest.param("*.exe", "matches 2 items", id="two-matches"),
    ],
)
def test_rename_needs_exactly_one_match(app: Path, source: str, message: str) -> None:
    """A glob with no match, or with several, fails instead of picking one."""
    with pytest.raises(PrepareError, match=message):
        run_prepare([PrepareRename(source, "New.exe")], app)


def test_rename_never_overwrites_an_existing_destination(app: Path) -> None:
    """A destination that exists makes the rename fail and both files stay as they were."""
    with pytest.raises(PrepareError, match="never overwrites"):
        run_prepare([PrepareRename("uninstall.exe", "bin/tool.exe")], app)

    assert (app / "uninstall.exe").read_text() == "u" and (app / "bin" / "tool.exe").read_text() == "tool"


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("../escaped.exe", id="parent-step"),
        pytest.param("C:\\escaped.exe", id="drive"),
        pytest.param("/escaped.exe", id="absolute"),
        pytest.param("bin/../../escaped.exe", id="parent-step-inside"),
    ],
)
def test_steps_refuse_destinations_outside_the_app(app: Path, tmp_path: Path, target: str) -> None:
    """Rename, copy and write refuse a path that is absolute, has a drive or leaves `App`."""
    for step in (PrepareRename("uninstall.exe", target), PrepareCopy("uninstall.exe", (target,)), PrepareWrite(target, "x")):
        with pytest.raises(PrepareError):
            run_prepare([step], app)

    assert not (tmp_path / "escaped.exe").exists()


def test_copy_makes_one_copy_per_target(app: Path) -> None:
    """`volta-shim.exe` is copied byte for byte to each tool name (the registry's volta case)."""
    (app / "volta-shim.exe").write_bytes(b"\x00shim\xff")

    run_prepare([PrepareCopy("volta-shim.exe", ("node.exe", "npm.exe", "sub/npx.exe"))], app)

    for name in ("node.exe", "npm.exe", "sub/npx.exe"):
        assert (app / name).read_bytes() == b"\x00shim\xff"


def test_copy_checks_every_target_before_writing_any(app: Path) -> None:
    """If one target already exists nothing is copied, so a collision never leaves a half-made set."""
    (app / "volta-shim.exe").write_text("shim", encoding="utf-8")

    with pytest.raises(PrepareError, match="never overwrites"):
        run_prepare([PrepareCopy("volta-shim.exe", ("node.exe", "uninstall.exe"))], app)

    assert not (app / "node.exe").exists()


def test_copy_of_a_folder_is_refused(app: Path) -> None:
    """Only a regular file can be copied."""
    with pytest.raises(PrepareError, match="not a regular file"):
        run_prepare([PrepareCopy("bin", ("bin2",))], app)


def test_write_creates_a_file_with_exact_utf8_content_and_replaces_a_regular_file(app: Path) -> None:
    """The content is written as UTF-8 without newline conversion, and an existing regular file is replaced."""
    run_prepare([PrepareWrite("config/portable.ini", "[core]\nname=Ünï\n"), PrepareWrite("uninstall.exe", "new")], app)

    assert (app / "config" / "portable.ini").read_bytes() == "[core]\nname=Ünï\n".encode("utf-8")
    assert (app / "uninstall.exe").read_text() == "new"


def test_write_refuses_a_link_and_a_folder(app: Path, system: FakeSystem, tmp_path: Path) -> None:
    """A step never writes through a junction and never replaces a folder."""
    system.create_junction(app / "linked", tmp_path / "outside")

    with pytest.raises(PrepareError):
        run_prepare([PrepareWrite("linked/secret.txt", "overwritten")], app)
    with pytest.raises(PrepareError, match="is a folder"):
        run_prepare([PrepareWrite("bin", "x")], app)

    assert (tmp_path / "outside" / "secret.txt").read_text() == "secret"


def test_a_module_step_runs_the_package_hook_with_the_staged_app(app: Path, system: FakeSystem, tmp_path: Path) -> None:
    """The escape hatch calls `prepare(ctx)` of a `pkg.local` module, whose context names the staged `App`."""
    version = tmp_path / "version"
    (version / "pkg.local").mkdir(parents=True)
    (version / "pkg.local" / "fix.py").write_text(
        "PKG_MODULE_API = 2\n\ndef prepare(ctx):\n    (ctx.paths.app / 'fixed.txt').write_text(ctx.package, encoding='utf-8')\n",
        encoding="utf-8",
    )
    context = HookContext("tool", "1.0", system, scope_of(system), HttpClient(system, env={}), HookPaths(tmp_path, version, app=app))

    run_prepare([PrepareModule("pkg.local/fix.py")], app, context)

    assert (app / "fixed.txt").read_text() == "tool"


def test_a_module_step_without_a_context_is_an_error(app: Path) -> None:
    """A module step cannot run without the hook context."""
    with pytest.raises(PrepareError, match="needs a hook context"):
        run_prepare([PrepareModule("pkg.local/fix.py")], app)


def test_steps_run_in_order_and_an_error_names_the_failing_step(app: Path) -> None:
    """Later steps see earlier results, and a failure says which `[[prepare]]` row it was."""
    run_prepare([PrepareRename("uninstall.exe", "old.exe"), PrepareCopy("old.exe", ("copy.exe",))], app)
    assert {"old.exe", "copy.exe"} <= files(app)

    with pytest.raises(PrepareError, match=r"\[\[prepare\]\] rename 'zzz\*'"):
        run_prepare([PrepareRename("zzz*", "x")], app)


def test_a_failed_prepare_deletes_the_staged_app(app: Path, system: FakeSystem, tmp_path: Path) -> None:
    """`prepare_app` leaves no half-prepared tree: when a step fails the whole `App` is removed."""
    manifest = parse_manifest(
        'format = 2\n[source]\ndownload_url = "https://example.org/t.zip"\n[[prepare]]\nrename = { from = "nothing", to = "x" }\n',
        ManifestKind.VERSION,
    )

    with pytest.raises(PrepareError):
        prepare_app(manifest, app)

    assert not app.exists()
