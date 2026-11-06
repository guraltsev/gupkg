"""Cover the three command shims that the gupkg package installs (`gupkg`, `gupkg-tui`, `gupkg-manager`).

Behavior covered: the shims declared in `src/pkg.toml` start the bundled `python.exe` with the fixed flags and hand it every
argument exactly as the caller typed it (a `.cmd` target would be run as `cmd /c "<target>" "a" "b"`, which breaks any
argument with `=`, `,`, `;`, `&`, `|`, `%`, `^` or an empty or quoted value); an update replaces the configuration of an
install that still has the 1.1.1 `.cmd` shims, also while the running shim is the one being updated.

Real: the bundled `shim-console.exe`, the bundled `python.exe`, the real `src/pkg.toml`, the effect code.
Mocked: `FakeSystem` (registry, shortcuts); the gupkg `bootstrap.py` is a stub that prints its arguments.
Out of scope: the install flow around the effects, and the elevated launcher's check of a shim (see the T18b log).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from gupkg.engine.effects import EffectContext, apply_effect, desired_effects, shim_config_text, verify_each
from gupkg.engine.planner import diff_effects
from gupkg.model.manifest import ManifestKind, parse_manifest
from gupkg.model.state import Effect
from gupkg.system.fake import FakeSystem
from gupkg.system.port import Mode

# Root of this checkout.
REPO = Path(__file__).resolve().parents[1]
# The source folder, which holds the shim launchers and the bundled runtime.
SRC = REPO / "src"
# The launchers and the arguments each one forwards to the bootstrap.
SHIMS = {"gupkg": [], "gupkg-tui": ["--gupkg-program=tui"], "gupkg-manager": ["--gupkg-program=manager"]}

pytestmark = [pytest.mark.process, pytest.mark.skipif(os.name != "nt", reason="the shims are Windows launchers")]


def _shim_effects(tmp_path: Path) -> tuple[EffectContext, dict[str, Effect]]:
    """Return an effect context below *tmp_path* and the shim effects of the real `src/pkg.toml`, by command name."""
    manifest = parse_manifest((SRC / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)
    context = EffectContext(tmp_path / "opt" / "gupkg", "user", tmp_path / "bin", None, None, {})
    effects, _notes = desired_effects(manifest, context)
    return context, {Path(str(item.fields["path"])).stem: item for item in effects if item.kind == "shim"}


def _link(link: Path, target: Path) -> None:
    """Make the junction *link* to the folder *target* (no administrator rights needed)."""
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=True)


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The gupkg package as installed: `<package>/current` -> a version folder whose `App` holds a bundled python and a stub bootstrap.

    Returns the `bin` folder with the three shims written by the real effect code, and the folder of the tree. The stub
    `bootstrap.py` prints the arguments it received as JSON and exits with 7, so the arguments and the exit code are observable.
    """
    # Build an installed tree with the real runtime and a bootstrap that prints its arguments.
    base = tmp_path_factory.mktemp("installed")
    context, effects = _shim_effects(base)
    version = context.package_path / "1.0"
    app = version / "App"
    shutil.copytree(SRC / "gupkg" / "python", app / "gupkg" / "python")
    (app / "gupkg" / "bootstrap.py").write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(7)\n", encoding="utf-8")
    # Link ``current`` and apply every shim effect.
    _link(context.package_path / "current", version)
    system = FakeSystem(base / "machine")
    for effect in effects.values():
        apply_effect(system, effect, context)
    return context.bin_dir, app


def _run(bin_dir: Path, command: str, raw: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run `<bin>/<command>.exe` with the argument text *raw* exactly as typed, without any shell."""
    return subprocess.run(f'"{bin_dir / (command + ".exe")}" {raw}', capture_output=True, check=False, env={**os.environ, **(env or {})})


# (the argument text as typed after the command name, the arguments the program must receive)
ARGUMENTS = [
    ("--overwrite=a,b", ["--overwrite=a,b"]),
    ("--x=1;2", ["--x=1;2"]),
    ('""', [""]),
    ('"a b  c"', ["a b  c"]),
    ('"C:\\x y\\\\"', ["C:\\x y\\"]),
    ('"caf\u00e9 \u0436\u0443\u043a"', ["caf\u00e9 \u0436\u0443\u043a"]),
    ('"a&b"', ["a&b"]),
    ('"a|b"', ["a|b"]),
    ("a^b", ["a^b"]),
    ('"%PATH%"', ["%PATH%"]),
    ("!VAR!", ["!VAR!"]),
    ('"a""b"', ['a"b']),
    ("x" * 100, ["x" * 100]),
    ("-- target", ["--", "target"]),
    ("install tool --yes", ["install", "tool", "--yes"]),
]


def test_the_shim_configuration_names_the_bundled_python_with_the_launcher_flags(tmp_path: Path) -> None:
    """The three shims start `python.exe` of the bundled gupkg with `-I -s -B -S bootstrap.py` (and the program name for tui and manager); none targets a batch file."""
    context, effects = _shim_effects(tmp_path)
    app = context.package_path / "current" / "App"

    assert set(effects) == set(SHIMS)
    for name, program in SHIMS.items():
        config = tomllib.loads(shim_config_text(effects[name].fields))
        assert config["target"] == str(app / "gupkg" / "python" / "python.exe")
        assert [item["value"] for item in config["argument"]] == ["-I", "-s", "-B", "-S", str(app / "gupkg" / "bootstrap.py"), *program]
        assert config["forward_arguments"] is True and config["elevate"] is False
        assert not config["target"].lower().endswith((".cmd", ".bat"))


@pytest.mark.parametrize(("raw", "expected"), ARGUMENTS, ids=[ascii(case[0])[:30] for case in ARGUMENTS])
def test_an_installed_shim_hands_the_program_exactly_the_arguments_it_was_given(installed: tuple[Path, Path], raw: str, expected: list[str]) -> None:
    """`=`, `,`, `;`, `&`, `|`, `%`, `^`, `!`, quotes, an empty argument, a trailing backslash, non-ASCII and 100 characters arrive unchanged, and the exit code comes back."""
    bin_dir, app = installed

    result = _run(bin_dir, "gupkg", raw, {"VAR": "EXPANDED"})

    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("command", ["gupkg-tui", "gupkg-manager"])
def test_the_tui_and_manager_shims_pass_the_program_name_first_then_every_argument(installed: tuple[Path, Path], command: str) -> None:
    """The program flag comes first, then all the caller's arguments exactly as typed."""
    bin_dir, _app = installed
    # A doubled quote inside quotes keeps the quoted state open for the rest of the line (the C runtime rule), so it is tried alone above.
    cases = [case for case in ARGUMENTS if case[0] != '"a""b"']
    raw = " ".join(case[0] for case in cases)

    result = _run(bin_dir, command, raw)

    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == [*SHIMS[command], *[item for case in cases for item in case[1]]]


def test_a_shim_without_its_bundled_python_fails_with_the_missing_path_and_a_non_zero_exit_code(installed: tuple[Path, Path], tmp_path: Path) -> None:
    """With `python.exe` gone the shim exits 1 and names the file it could not start (it does not hang or run something else)."""
    bin_dir, app = installed
    folder = tmp_path / "bin"
    shutil.copytree(bin_dir, folder)
    config = folder / "gupkg.config.toml"
    config.write_text(config.read_text(encoding="utf-8").replace("python.exe", "no-python.exe", 1), encoding="utf-8")

    result = _run(folder, "gupkg", "--version")

    assert result.returncode == 1 and b"no-python.exe" in result.stdout + result.stderr


def test_an_update_replaces_the_cmd_shims_of_a_1_1_1_install_and_leaves_no_drift(tmp_path: Path) -> None:
    """A record that still holds the 1.1.1 shims (target `$App\\gupkg.cmd`) is planned for update, and applying it writes the python configuration."""
    # Arrange: the effects of a 1.1.1 install, which used ``.cmd`` shims.
    context, desired = _shim_effects(tmp_path)
    app = context.package_path / "current" / "App"
    system = FakeSystem(tmp_path / "machine")
    old = []
    for name, effect in desired.items():
        fields = {**effect.fields, "target": str(app / f"{name}.cmd"), "arguments": []}
        fields["config_sha256"] = hashlib.sha256(shim_config_text(fields).encode("utf-8")).hexdigest()
        old.append(apply_effect(system, Effect("shim", fields), context))
    assert [row for row in verify_each(system, old, mode=Mode.USER) if row[1] or row[2]] == []

    # Act: diff the old effects against the desired ones and apply the updates.
    plan = diff_effects(old, list(desired.values()))
    assert [item.action for item in plan] == ["update"] * 3
    applied = [apply_effect(system, item.effect, context) for item in plan]

    # Assert: the new shims use the interpreter and nothing drifts.
    for effect in applied:
        config = tomllib.loads(Path(str(effect.fields["config"])).read_text(encoding="utf-8"))
        assert config["target"].endswith("python.exe")
    assert [row for row in verify_each(system, applied, mode=Mode.USER) if row[1] or row[2]] == []


def test_applying_a_shim_effect_while_that_shim_is_running_still_rewrites_its_configuration(installed: tuple[Path, Path], tmp_path: Path) -> None:
    """`gupkg update gupkg` runs through `bin\\gupkg.exe` and rewrites it: an identical launcher is not replaced (Windows refuses), and the configuration is.

    A copy of the installed shims keeps `gupkg.exe` busy with a real child that sleeps, then the effect is applied again.
    """
    # Arrange: a copy of the installed shims, with a configuration that targets this interpreter.
    context, effects = _shim_effects(tmp_path)
    shutil.copytree(installed[0], context.bin_dir)
    config = context.bin_dir / "gupkg.config.toml"
    config.write_text(f'target = "{sys.executable.replace(chr(92), chr(92) * 2)}"\nforward_arguments = true\n', encoding="utf-8")
    # Start a program through the launcher, so that it is running.
    busy = subprocess.Popen([str(context.bin_dir / "gupkg.exe"), "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(1)
        assert busy.poll() is None

        # Act: apply the effect while the launcher runs.
        apply_effect(FakeSystem(tmp_path / "machine"), effects["gupkg"], context)
    finally:
        busy.kill()
        busy.wait()

    # Assert: the configuration was rewritten.
    assert tomllib.loads(config.read_text(encoding="utf-8"))["target"].endswith("python.exe")


def test_a_running_shim_whose_launcher_differs_is_replaced_and_the_old_launcher_removed_later(installed: tuple[Path, Path], tmp_path: Path) -> None:
    """`gupkg self repair` runs through an older `bin\\gupkg.exe`: Windows refuses to overwrite a running .exe, so it is moved aside.

    The running launcher is a different build (the static launcher stands in for an older one). Applying the effect puts this
    gupkg's launcher in place while the old one still runs; the next apply, after it exited, deletes the moved-aside file.
    """
    # Arrange: a copy of the installed shims where the launcher differs from the bundled one.
    context, effects = _shim_effects(tmp_path)
    shutil.copytree(installed[0], context.bin_dir)
    launcher = context.bin_dir / "gupkg.exe"
    bundled = launcher.read_bytes()
    shutil.copyfile(SRC / "gupkg" / "shim" / "shim-console.static.exe", launcher)
    config = context.bin_dir / "gupkg.config.toml"
    config.write_text(f'target = "{sys.executable.replace(chr(92), chr(92) * 2)}"\nforward_arguments = true\n', encoding="utf-8")
    system = FakeSystem(tmp_path / "machine")
    # Keep a program running through the launcher.
    busy = subprocess.Popen([str(launcher), "-c", "import time; time.sleep(30)"])
    try:
        time.sleep(1)
        assert busy.poll() is None

        # Applying the effect leaves the running launcher in place and sets the old one aside.
        apply_effect(system, effects["gupkg"], context)

        assert launcher.read_bytes() == bundled and busy.poll() is None
    finally:
        busy.kill()
        busy.wait()
    assert [path.name for path in context.bin_dir.glob("*.old")] != []

    # Apply again once the program has ended.
    apply_effect(system, effects["gupkg"], context)

    assert [path.name for path in context.bin_dir.glob("*.old")] == []
    assert tomllib.loads(config.read_text(encoding="utf-8"))["target"].endswith("python.exe")


def test_a_launcher_moved_aside_is_put_back_when_the_new_one_cannot_be_written(installed: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the write after the move fails too, the old launcher is back under its name: the command never disappears from `bin`."""
    # Arrange: an installed shim whose launcher is an older one.
    context, effects = _shim_effects(tmp_path)
    shutil.copytree(installed[0], context.bin_dir)
    launcher = context.bin_dir / "gupkg.exe"
    old = b"an older launcher"
    launcher.write_bytes(old)

    # Fail the write of the launcher like a running program, then like a full disk.
    def refuse(path: Path, content: bytes) -> None:
        """Fail like a running .exe, then like a full disk."""
        raise PermissionError(13, "Access is denied") if path.suffix == ".exe" else OSError(28, "No space left on device")

    # Install the failing write.
    monkeypatch.setattr("gupkg.engine.effects.write_bytes_atomic", refuse)
    # Act and assert: the apply fails and the launcher is put back.
    with pytest.raises(PermissionError):
        apply_effect(FakeSystem(tmp_path / "machine"), effects["gupkg"], context)

    assert launcher.read_bytes() == old and list(context.bin_dir.glob("*.old")) == []
