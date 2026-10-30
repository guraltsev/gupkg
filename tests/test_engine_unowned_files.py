"""Cover files that no installed package owns: they are not overwritten unless ``--overwrite=KINDS`` names their kind.

Behavior covered: an install that would write a shim, its configuration, a wrapper or a Start Menu shortcut over an existing file
that no install record owns is refused before any download or change (code ``file-exists``, exit code 2), with the path, the
reason and the exact ``--overwrite`` value that lifts it; each kind (``orphaned-bin-files``, ``orphaned-shortcuts``) lifts only its
own refusal and never a ``name-conflict``; a file that already holds what would be written is not a conflict; a package keeps its
own files across ``update``, ``switch``, ``repair`` and a repeated ``install``; ``uninstall``, ``cleanup`` and ``check`` are
never refused and ``check --fix`` reports the problem and changes nothing; the value of the option is checked (usage error,
exit code 2); the failure document carries the code and the typed reader reads it.

Real: the command line, the engine, the catalog, real folders. Mocked: ``FakeSystem``. Out of scope: the manager screen
(``test_ui_unowned_file.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from gupkg.catalog.catalog import Catalog
from gupkg.catalog.record import write_install_record
from gupkg.model.plan import OVERWRITE_KINDS, parse_overwrite
from gupkg.model.state import InstallRecord
from gupkg.report.documents import read_results
from gupkg.system.port import Mode, ShortcutSpec
from tests.cli_support import CliWorld
from tests.engine_support import pin_no_ambient_elevation  # noqa: F401
from tests.test_engine_name_conflicts import ENV, SHIM, SHORTCUT, WRAPPER, state

BIN, SHORTCUTS = OVERWRITE_KINDS


@pytest.fixture
def world(tmp_path: Path) -> CliWorld:
    """A fake machine with a user root, driven through the command line."""
    return CliWorld(tmp_path)


def bin_dir(world: CliWorld) -> Path:
    """Return the user mode's ``bin`` folder."""
    return world.fake.scope_folders(Mode.USER).bin_dir


def plant_file(name: str) -> Callable[[CliWorld], Path]:
    """Return a function that puts a hand-made file called *name* in ``bin`` and returns its path."""
    def plant(world: CliWorld) -> Path:
        path = bin_dir(world) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"made by hand")
        return path
    return plant


def plant_shortcut(name: str) -> Callable[[CliWorld], Path]:
    """Return a function that puts a hand-made shortcut called *name* in the Start Menu folder and returns its path."""
    def plant(world: CliWorld) -> Path:
        path = world.fake.scope_folders(Mode.USER).shortcut_root / name
        world.fake.write_shortcut(path, ShortcutSpec(target_path="C:\\hand\\made.exe"))
        return path
    return plant


# (table of the package, what is planted, the kind that lifts the refusal)
CASES = [
    pytest.param(SHIM.format(name="tool"), plant_file("tool.exe"), BIN, id="shim"),
    pytest.param(SHIM.format(name="tool"), plant_file("tool.config.toml"), BIN, id="shim-configuration"),
    pytest.param(SHIM.format(name="tool"), plant_file("Tool.exe"), BIN, id="shim-letter-case"),
    pytest.param(WRAPPER.format(name="tool.cmd"), plant_file("tool.cmd"), BIN, id="wrapper"),
    pytest.param(SHORTCUT.format(name="Tool"), plant_shortcut("Tool.lnk"), SHORTCUTS, id="shortcut"),
]


def content(path: Path, world: CliWorld) -> object:
    """Return what is at *path*: the bytes of a file, the fields of a shortcut."""
    return path.read_bytes() if path.parent == bin_dir(world) else world.fake.read_shortcut(path)


@pytest.mark.parametrize(("table", "plant", "kind"), CASES)
def test_an_unowned_file_is_refused_before_anything_is_fetched_or_changed(
    world: CliWorld, table: str, plant: Callable[[CliWorld], Path], kind: str
) -> None:
    """The message names the path, the reason and the flag; the root, ``bin``, shortcuts and the file itself are as before."""
    world.publish("first", "1.0.0", tables=table)
    path = plant(world)
    before, kept = state(world), content(path, world)
    world.fake.fetch_log.clear()

    result = world.one("install", "first")

    assert not result.ok and result.exit_code == 2 and result.error["code"] == "file-exists"
    # The path is the one the package would write, in its own letter case (``tool.exe`` against a ``Tool.exe`` on disk).
    assert result.error["message"].lower() == (
        f"first cannot be installed: '{path}' already exists and no installed package owns it. Move or delete that file, or run "
        f"the command again with --overwrite={kind} to replace it."
    ).lower()
    assert state(world) == before and content(path, world) == kept and world.fake.fetch_log == []
    assert not (world.user / "first" / "current").exists()


@pytest.mark.parametrize(("table", "plant", "kind"), CASES)
def test_overwrite_replaces_the_file_says_so_and_makes_the_package_its_owner(
    world: CliWorld, table: str, plant: Callable[[CliWorld], Path], kind: str
) -> None:
    """With the kind, the install succeeds, one ``replaced`` line is printed, and a second package with that name is a ``name-conflict``."""
    world.publish("first", "1.0.0", tables=table)
    path = plant(world)
    kept = content(path, world)

    done = world.cli("install", "first", f"--overwrite={kind}")

    assert done.code == 0, done.err
    lines = [line for line in (done.out + done.err).splitlines() if "replaced " in line]
    assert len(lines) == 1 and f"replaced {path} (no installed package owned it)".lower() in lines[0].lower(), lines
    assert content(path, world) != kept
    world.publish("second", "1.0.0", tables=table)
    refused = world.one("install", "second")
    assert not refused.ok and refused.error["code"] == "name-conflict"


def test_each_kind_lifts_only_its_own_refusal_and_the_message_names_the_exact_value(world: CliWorld) -> None:
    """A shim and a shortcut are both in the way: one kind alone still refuses, naming the value that lifts both."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool") + SHORTCUT.format(name="Tool"))
    exe, link = plant_file("tool.exe")(world), plant_shortcut("Tool.lnk")(world)
    before = state(world)
    both = f"{BIN},{SHORTCUTS}"

    nothing = world.one("install", "first")
    only_bin = world.one("install", "first", overwrite=(BIN,))
    only_links = world.one("install", "first", overwrite=(SHORTCUTS,))

    assert nothing.error["code"] == only_bin.error["code"] == only_links.error["code"] == "file-exists"
    assert f"'{exe}'" in nothing.error["message"] and f"'{link}'" in nothing.error["message"]
    assert nothing.error["message"].endswith(f"--overwrite={both} to replace them.")
    assert f"'{link}' already exists" in only_bin.error["message"] and f"'{exe}'" not in only_bin.error["message"]
    assert only_bin.error["message"].endswith(f"--overwrite={both} to replace it.")
    assert f"'{exe}' already exists" in only_links.error["message"] and f"'{link}'" not in only_links.error["message"]
    assert only_links.error["message"].endswith(f"--overwrite={both} to replace it.")
    assert state(world) == before

    assert world.one("install", "first", overwrite=(BIN, SHORTCUTS)).ok


def test_many_paths_are_listed_in_one_message_five_at_most(world: CliWorld) -> None:
    """Seven wrappers in the way: the message shows five paths and says how many more."""
    names = [f"w{index}.cmd" for index in range(7)]
    world.publish("first", "1.0.0", tables="".join(WRAPPER.format(name=name) for name in names))
    for name in names:
        plant_file(name)(world)

    result = world.one("install", "first")

    message = result.error["message"]
    assert result.error["code"] == "file-exists" and message.count("w") >= 5 and "and 2 more already exist" in message
    assert "no installed package owns them. Move or delete those files, or run the command again with --overwrite=orphaned-bin-files to replace them." in message


def test_overwrite_never_lifts_a_name_conflict_with_another_package(world: CliWorld) -> None:
    """Another installed package owns the name: both kinds together still give the ``name-conflict`` refusal."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool") + SHORTCUT.format(name="Tool"))
    assert world.one("install", "first").ok
    world.publish("second", "1.0.0", tables=SHIM.format(name="tool") + SHORTCUT.format(name="Tool"))
    before = state(world)

    result = world.one("install", "second", overwrite=(BIN, SHORTCUTS))

    assert not result.ok and result.error["code"] == "name-conflict" and state(world) == before


@pytest.mark.parametrize("kind", ["shim", "shortcut"])
def test_a_file_that_holds_what_would_be_written_is_not_a_conflict(world: CliWorld, kind: str) -> None:
    """A package whose record was lost installs again without the flag when the files are exactly what it would write."""
    table = SHIM.format(name="tool") if kind == "shim" else SHORTCUT.format(name="Tool")
    world.publish("first", "1.0.0", tables=table)
    assert world.one("install", "first").ok
    files = {path: path.read_bytes() for path in bin_dir(world).glob("tool*")}
    links = {path: world.fake.read_shortcut(path) for path in world.fake.shortcuts()}
    assert world.one("uninstall", "first").ok
    for path, data in files.items():
        path.write_bytes(data)
    for path, spec in links.items():
        world.fake.write_shortcut(path, spec)
    assert files or links

    result = world.one("install", "first")

    assert result.ok, result.error


def test_a_package_keeps_its_own_files_across_update_switch_repair_and_reinstall(world: CliWorld) -> None:
    """A file the package owns is its own even after a person changed it: none of these operations is refused."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool") + SHORTCUT.format(name="Tool"))
    assert world.one("install", "first").ok
    (bin_dir(world) / "tool.exe").write_bytes(b"changed by hand")
    world.publish("first", "2.0.0", tables=SHIM.format(name="tool") + SHORTCUT.format(name="Tool"))

    for operation, options in (("update", {}), ("switch", {"version": "1.0.0"}), ("install", {}), ("repair", {})):
        result = world.one(operation, "first", **options)
        assert result.ok, (operation, result.error)



def appear_during(monkeypatch: pytest.MonkeyPatch, module: object, world: CliWorld, call: int, *, after: bool = False) -> None:
    """Make a hand-made ``bin\\tool.exe`` appear around the *call*-th look at the disk made through ``module.find_unowned``.

    With *after* the file appears once that look has returned (so it saw nothing); otherwise just before it.
    """
    original, calls = module.find_unowned, []

    def looking(*args, **kwargs):
        calls.append(1)
        if len(calls) != call:
            return original(*args, **kwargs)
        if after:
            found = original(*args, **kwargs)
            plant_file("tool.exe")(world)
            return found
        plant_file("tool.exe")(world)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "find_unowned", looking)


def test_a_file_that_appears_after_the_plan_is_refused_before_anything_is_activated(world: CliWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan saw no file; one appears while the package is fetched and unpacked: exit code 2, the file is kept, ``current`` is not moved."""
    from gupkg.engine import lifecycle

    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    appear_during(monkeypatch, lifecycle, world, call=1, after=True)

    done = world.one("install", "first")

    assert not done.ok and done.exit_code == 2 and done.error["code"] == "file-exists", done.error
    path = bin_dir(world) / "tool.exe"
    assert f"'{path}' already exists and no installed package owns it." in done.error["message"]
    assert f"--overwrite={BIN} to replace it." in done.error["message"] and "was unpacked but not activated" in done.error["message"]
    assert path.read_bytes() == b"made by hand" and not (world.user / "first" / "current").exists()


def test_a_file_that_appears_after_the_plan_is_replaced_and_reported_when_the_kind_is_given(world: CliWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same sequence with ``--overwrite=orphaned-bin-files``: the file is replaced and one ``replaced`` line is printed."""
    from gupkg.engine import lifecycle

    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    appear_during(monkeypatch, lifecycle, world, call=1, after=True)

    done = world.cli("install", "first", f"--overwrite={BIN}")

    assert done.code == 0, done.err
    lines = [line for line in (done.out + done.err).splitlines() if "replaced " in line]
    assert len(lines) == 1 and "tool.exe (no installed package owned it)" in lines[0]
    assert (bin_dir(world) / "tool.exe").read_bytes() != b"made by hand"


def test_a_file_that_appears_just_before_the_write_is_not_replaced_silently(world: CliWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    """It appears after the look before activation: the effect is not applied, the result says so (exit code 3), the file is kept."""
    from gupkg.engine import executor

    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    appear_during(monkeypatch, executor, world, call=2)

    done = world.one("install", "first")

    assert not done.ok and done.error["code"] == "integrate-failed" and done.exit_code == 3, done.error
    assert "already exists and no installed package owns it" in done.error["message"] and "this effect was not applied" in done.error["message"]
    assert (bin_dir(world) / "tool.exe").read_bytes() == b"made by hand"


def test_uninstall_cleanup_and_check_are_never_refused_by_an_unowned_file(world: CliWorld) -> None:
    """An update that is refused because of an unowned file leaves the package alone: it can be checked, cleaned and removed."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    assert world.one("install", "first").ok
    world.publish("first", "2.0.0", tables=SHIM.format(name="other"))
    plant_file("other.exe")(world)
    assert world.one("update", "first").error["code"] == "file-exists"

    assert world.one("check", "first").error is None
    assert world.one("cleanup", "first").ok
    assert world.one("uninstall", "first").ok
    assert (bin_dir(world) / "other.exe").read_bytes() == b"made by hand"


def test_check_fix_reports_the_unowned_file_and_the_flag_and_changes_nothing(world: CliWorld) -> None:
    """The install record lost the shim; the file is the package's but no record says so: ``--fix`` names the command and stops."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    assert world.one("install", "first").ok
    view = Catalog(world.scope, world.fake).describe("first")
    write_install_record(view.path, InstallRecord(view.record.mode, view.record.link, ()))
    exe = bin_dir(world) / "tool.exe"
    exe.write_bytes(b"made by hand")
    for item in (world.user / "first" / "current" / "App").iterdir():
        item.unlink()
    before = state(world)

    result = world.one("check", "first", fix=True)

    assert not result.ok and result.error["code"] == "file-exists"
    assert result.error["message"].startswith("first cannot be repaired: ")
    assert f"run `gupkg install first --overwrite={BIN}` to replace it." in result.error["message"]
    assert exe.read_bytes() == b"made by hand" and state(world) == before


@pytest.mark.parametrize(
    ("value", "words"),
    [("", "needs the kinds"), ("nonsense", "does not know the kind 'nonsense'"), (f"{BIN},", "empty item"),
     (f"{BIN},,{SHORTCUTS}", "empty item"), (f"{BIN},{BIN}", f"names '{BIN}' twice"), ("all", "does not know the kind 'all'")],
)
def test_a_bad_value_is_a_usage_error_that_lists_the_valid_kinds(world: CliWorld, value: str, words: str) -> None:
    """``--overwrite=`` with no kind, an unknown one, an empty item or a duplicate: exit code 2, the kinds are named, nothing runs."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    world.fake.fetch_log.clear()

    for argv in (["install", "first", f"--overwrite={value}"], ["update", "first", f"--overwrite={value}"]):
        result = world.cli(*argv)
        assert result.code == 2 and words in result.err and f"{BIN}, {SHORTCUTS}" in result.err, result.err
    assert world.fake.fetch_log == [] and not (world.user / "first" / "current").exists()


def test_a_bare_overwrite_is_a_usage_error_even_before_another_option(world: CliWorld) -> None:
    """``--overwrite`` alone never takes the next word as its value."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))

    for argv in (["install", "first", "--overwrite"], ["install", "--overwrite", "first"], ["install", "first", "--overwrite", "--yes"]):
        result = world.cli(*argv)
        assert result.code == 2 and f"{BIN}, {SHORTCUTS}" in result.err, (argv, result.err)
    assert not (world.user / "first" / "current").exists()


def test_the_value_split_at_equals_and_comma_by_gupkg_cmd_is_one_value_again(world: CliWorld) -> None:
    """``gupkg.cmd`` forwards ``--overwrite=a,b`` as ``--overwrite a b``: the kinds after a bare flag are its value."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool") + SHORTCUT.format(name="Tool"))
    plant_file("tool.exe")(world), plant_shortcut("Tool.lnk")(world)

    refused = world.cli("install", "first", "--overwrite", BIN)
    done = world.cli("install", "first", "--overwrite", BIN, SHORTCUTS)

    assert refused.code == 2 and f"--overwrite={BIN},{SHORTCUTS}" in refused.err
    assert done.code == 0, done.err


def test_the_option_exists_on_install_update_switch_and_self_repair_and_update_but_not_on_check(world: CliWorld) -> None:
    """The help of each lists the kinds; ``check`` and ``uninstall`` do not take it."""
    for argv in (["install"], ["update"], ["switch"], ["self", "repair"], ["self", "update"]):
        result = world.cli(*argv, "--help")
        squeezed = "".join(result.out.split())  # argparse wraps the help, maybe inside a hyphenated word
        assert result.code == 0 and BIN in squeezed and SHORTCUTS in squeezed, argv
    for command in ("check", "uninstall", "cleanup"):
        assert world.cli(command, "first", f"--overwrite={BIN}").code == 2


def test_the_failure_document_carries_file_exists_and_the_typed_reader_reads_it(world: CliWorld) -> None:
    """``install --json`` of a refused package: exit code 2, ``file-exists`` in the entry, and ``read_results`` accepts it."""
    world.publish("first", "1.0.0", tables=SHIM.format(name="tool"))
    path = plant_file("tool.exe")(world)

    refused = world.cli("install", "first", "--json")

    assert refused.code == 2
    document = refused.document()
    entry = document["results"][0]
    assert entry["error"]["code"] == "file-exists" and str(path) in entry["error"]["message"]
    parsed = read_results(document)
    assert parsed.exit_code == 2 and parsed.results[0].error.code == "file-exists"


def test_the_environment_variable_and_path_effects_are_not_files(world: CliWorld) -> None:
    """An environment variable and the shared ``PATH`` entry are never refused by this rule."""
    world.publish("first", "1.0.0", tables=ENV.format(name="TOOL_HOME") + SHIM.format(name="tool"))

    assert world.one("install", "first").ok


def test_parse_overwrite_returns_the_kinds_in_the_order_written() -> None:
    """Unit view: a list of valid kinds is returned as written, spaces around an item are ignored."""
    assert parse_overwrite(f"{SHORTCUTS},{BIN}") == (SHORTCUTS, BIN)
    assert parse_overwrite(f" {BIN} ") == (BIN,)
