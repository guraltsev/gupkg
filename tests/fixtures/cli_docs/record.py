"""Record the ``--json`` documents of the real CLI (run by hand: ``python tests/fixtures/cli_docs/record.py``).

It builds a small world in a temporary folder (a user root with two installed packages and one local package, a registry cache with three
packages, one of them for the system mode only), runs the real ``gupkg.cmd`` launcher of this tree with ``--root``, ``--settings`` and ``--sandbox``
(a pretend machine; nothing of the real machine changes, nothing uses the network) and saves what each call printed. The temporary folder is replaced by
``@ROOT@``. Nothing is edited by hand: run the script again after a change of the documents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
LAUNCHER = REPO / "src" / "gupkg" / "gupkg.cmd"
sys.path.insert(0, str(REPO))

from tests.catalog_support import build_registry  # noqa: E402


def package(root: Path, name: str, versions: tuple[str, ...]) -> None:
    """Write *versions* of the package *name* with a shipped App, so nothing needs the network."""
    for version in versions:
        folder = root / name / version
        (folder / "App").mkdir(parents=True)
        (folder / "pkg.toml").write_text(f'format = 2\ndescription = "{name}"\n[source]\nshipped = true\n', encoding="utf-8")
        (folder / "App" / "app.exe").write_text("x", encoding="utf-8")


def main() -> None:
    """Build the world, run the real CLI and save the documents."""
    work = Path(tempfile.mkdtemp(prefix="gupkg-doc-record-"))
    try:
        root = work / "home" / "opt"
        for name in ("alpha", "beta"):
            package(root, name, ("1.0.0", "2.0.0"))
        package(root, "gamma", ("1.0.0",))
        cache = build_registry(root, {"copyq": {"versions": ["1.0.0"]}, "drv": {"versions": ["1.0.0"]}, "graphviz": {"versions": ["1.0.0"]}})
        manifest = next(cache.rglob("pkgs/drv/1.0.0")) / "pkg.toml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("format = 2\n", 'format = 2\nmodes = ["system"]\n', 1), encoding="utf-8")
        (work / "temp").mkdir()
        settings = work / "settings.toml"
        settings.write_text(
            f"schema = 4\n[integration]\nbin = '{work}\\home\\bin'\nstart_menu = '{work}\\home\\sm'\n"
            '[[registry]]\nname = "main"\nsource = "https://example.org/main.zip"\n',
            encoding="utf-8",
        )
        env = {**os.environ, "USERPROFILE": str(work / "home"), "APPDATA": str(work / "appdata"), "TEMP": str(work / "temp")}
        options = ["--root", str(root), "--settings", str(settings), "--sandbox", str(work / "sb")]

        def run(*args: str) -> str:
            """Run the real CLI and return standard output (the call may fail, with a document)."""
            done = subprocess.run([str(LAUNCHER), *options, *args], capture_output=True, text=True, env=env, encoding="utf-8", stdin=subprocess.DEVNULL, check=False)
            return done.stdout

        def keep(name: str, text: str) -> None:
            """Save *text* with the temporary folder replaced by ``@ROOT@``."""
            for variant in {str(work), str(work).replace("\\", "\\\\")}:
                text = text.replace(variant, "@ROOT@")
            (HERE / name).write_text(text, encoding="utf-8", newline="\n")

        run("install", "alpha@1.0.0", "beta@1.0.0", "--yes", "--no-input")
        keep("list.json", run("list", "--json"))
        keep("list_names.json", run("list", "--names", "--json"))
        keep("list_available.json", run("list", "--available", "--json"))
        keep("info.json", run("info", "alpha", "--json"))
        keep("registry_status.json", run("registry", "status", "--json"))
        keep("check.json", run("check", "--json", "--no-input", "--", "alpha"))
        keep("install_plan.json", run("install", "--plan", "--json", "--no-input", "--", "copyq"))
        keep("install_result.json", run("install", "--yes", "--json", "--no-input", "--", "gamma@1.0.0"))
        keep("install_failure.json", run("install", "--yes", "--json", "--no-input", "--", "nothere"))
        print("recorded in", HERE)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
