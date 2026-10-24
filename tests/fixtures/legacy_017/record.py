"""Record what the REAL 0.17 install code (commit 4172a00) leaves on a fake machine, as JSON fixtures.

Run with the repository root of a checkout of commit 4172a00 (the last commit with ``gupkg/gupkg.py``) as the argument.
The recorded files are committed under ``tests/fixtures/legacy_017``; the migration tool's tests replay them.
"""
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

checkout = Path(sys.argv[1])
out = Path(sys.argv[2])
sys.path.insert(0, str(checkout / "src"))
sys.path.insert(0, str(checkout))
if importlib.util.find_spec("tomli_w") is None:
    shipped = Path(sys.argv[3])
    spec = importlib.util.spec_from_file_location("tomli_w", shipped, submodule_search_locations=[str(shipped.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["tomli_w"] = module
    spec.loader.exec_module(module)

import os  # noqa: E402

from gupkg import gupkg as legacy  # noqa: E402
from gupkg.core import Scope  # noqa: E402
from gupkg.system.fake import FakeSystem  # noqa: E402

FIXTURE = checkout / "tests" / "fixtures" / "GoodApp"


def record(base: Path, user_root: Path, fake: FakeSystem, package: Path) -> dict:
    """Return the machine state and the package folder state, with the machine's folders replaced by placeholders."""
    folders = fake.scope_folders(Scope.USER)

    def place(text: str) -> str:
        for needle, name in ((str(folders.shortcut_root), "<MENU>"), (str(folders.bin_dir), "<BIN>"), (str(user_root), "<ROOT>"), (str(base), "<BASE>")):
            text = text.replace(needle, name).replace(needle.replace("\\", "\\\\"), name)
        return text

    shortcuts = {}
    for path, spec in fake.shortcuts().items():
        shortcuts[place(str(path))] = {key: place(str(value)) for key, value in vars(spec).items()}
    env = {name: {"value": place(value.value), "expandable": value.expandable} for name, value in fake.env_values(Scope.USER).items()}
    bin_files = {}
    for item in sorted(folders.bin_dir.glob("*")):
        if item.is_file() and item.suffix in (".cmd", ".toml"):
            bin_files[item.name] = place(item.read_text(encoding="utf-8"))
    launchers = sorted(item.name for item in folders.bin_dir.glob("*.exe"))
    current = Path(os.path.realpath(package / "current")).name if (package / "current").exists() else None
    return {"shortcuts": shortcuts, "env": env, "path": [place(entry) for entry in fake.get_path(Scope.USER)],
            "bin_files": bin_files, "launchers": launchers, "current": current}


def scenario(name: str, prepare) -> dict:
    base = Path(tempfile.mkdtemp(prefix="rec017-"))
    user_root = base / "home" / "opt"
    fake = FakeSystem(base / "machine")
    package = prepare(user_root)
    result = legacy.install_package(package, fake, scope=Scope.USER)
    assert result.ok, result.message
    state = record(base, user_root, fake, package)
    shutil.rmtree(base, ignore_errors=True)
    return state


def good_app(user_root: Path) -> Path:
    package = user_root / "GoodApp"
    shutil.copytree(FIXTURE, package)
    version = next(item for item in package.iterdir() if item.is_dir())
    (version / "App").mkdir(exist_ok=True)
    (version / "App" / "bin").mkdir(exist_ok=True)
    (version / "App" / "GoodApp.exe").write_text("exe", encoding="utf-8")
    return package


def dotted(user_root: Path) -> Path:
    package = user_root / "Dotted"
    version = package / "1.0.0"
    (version / "App").mkdir(parents=True)
    (version / "App" / "tool.exe").write_text("exe", encoding="utf-8")
    (version / "pkg.toml").write_text(
        'name = "Dotted"\nversion = "1.0.0"\nlocalVersion = 0\n\n[[shortcut]]\nname = "Tool 1.0"\ntargetPath = "$App\\\\tool.exe"\n',
        encoding="utf-8",
    )
    return package


LEGACY_RICH = """name = "Rich"
version = "2.0"
localVersion = 1

[[shortcut]]
name = "Rich App"
targetPath = "$App\\\\rich.exe"
arguments = "--flag"
workingDirectory = "$App"
iconLocation = "$App\\\\rich.ico,0"
description = "Rich"

[[environment]]
Name = "RICH_HOME"
Value = "$App"

[[environment]]
Name = "RICH_BIN"
Value = "$App\\\\bin;${USERPROFILE}"

[[path]]
value = "$App\\\\bin"

[[bin]]
name = "rich"
target = "$App\\\\rich.exe"
type = "gui"
arguments = ["--a", "$App\\\\x"]
forward_args = false
elevate = true
working_dir = "$App"

[[bin]]
name = "rich-run.cmd"
content = "@echo off\\r\\n\\"$App\\\\rich.exe\\" %*\\r\\n"
"""


def rich(user_root: Path) -> Path:
    version = user_root / "Rich" / "2.0.local.1"
    (version / "App" / "bin").mkdir(parents=True)
    (version / "App" / "rich.exe").write_text("exe", encoding="utf-8")
    (version / "pkg.toml").write_text(LEGACY_RICH, encoding="utf-8")
    return user_root / "Rich"


os.environ["USERPROFILE"] = "C:\\Users\\same"
out.mkdir(parents=True, exist_ok=True)
for name, prepare in (("goodapp", good_app), ("dotted", dotted), ("rich", rich)):
    (out / f"{name}.json").write_text(json.dumps(scenario(name, prepare), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("recorded", name)
