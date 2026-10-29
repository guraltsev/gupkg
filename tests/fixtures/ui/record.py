"""Record the real documents and events files that the UI program tests replay (run by hand: ``python tests/fixtures/ui/record.py``).

It builds a small world in a temporary folder (two user packages with a newer version each, shipped Apps so nothing needs the network), runs the real
``gupkg`` launcher with ``--settings`` and ``--sandbox`` and saves what it printed: the ``list``, ``info`` and ``update --plan`` documents, the result
document of an ``update`` and the global and package events files of that run. The temporary folder is replaced by ``@ROOT@`` so the fixtures are
portable; the tests put their own folder back.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE.parents[2] / "src" / "gupkg" / "gupkg.cmd"


def main() -> None:
    """Build the world, run the real CLI and save the documents."""
    work = Path(tempfile.mkdtemp(prefix="gupkg-ui-record-"))
    try:
        for name in ("alpha", "beta"):
            for version in ("1.0.0", "2.0.0"):
                folder = work / "home" / "opt" / name / version
                (folder / "App").mkdir(parents=True)
                (folder / "pkg.toml").write_text(f'format = 2\ndescription = "{name}"\n[source]\nshipped = true\n', encoding="utf-8")
                (folder / "App" / "app.exe").write_text("x", encoding="utf-8")
        (work / "sys" / "opt").mkdir(parents=True)
        (work / "temp").mkdir()
        settings = work / "settings.toml"
        settings.write_text(
            f"schema = 3\n[roots]\nsystem = '{work}\\sys\\opt'\nuser = '{work}\\home\\opt'\n"
            f"[integration.user]\nbin = '{work}\\home\\bin'\nstart_menu = '{work}\\home\\sm'\n"
            f"[integration.system]\nbin = '{work}\\sys\\bin'\nstart_menu = '{work}\\sys\\sm'\n",
            encoding="utf-8",
        )
        env = {**os.environ, "USERPROFILE": str(work / "home"), "APPDATA": str(work / "appdata"), "TEMP": str(work / "temp")}
        root = ["--settings", str(settings), "--sandbox", str(work / "sb")]

        def run(*args: str) -> str:
            """Run the real CLI and return standard output (the call must succeed or fail with a document)."""
            result = subprocess.run([str(LAUNCHER), *root, *args], capture_output=True, text=True, env=env, encoding="utf-8", stdin=subprocess.DEVNULL, check=False)
            return result.stdout

        def keep(name: str, text: str) -> None:
            """Save *text* with the temporary folder replaced by ``@ROOT@``."""
            variants = {str(work), str(work).replace("\\", "\\\\")}
            for variant in variants:
                text = text.replace(variant, "@ROOT@")
            (HERE / name).write_text(text, encoding="utf-8", newline="\n")

        run("install", "alpha@1.0.0", "beta@1.0.0", "--yes", "--no-input")
        keep("list.json", run("list", "--json"))
        keep("info_alpha.json", run("info", "alpha", "--json"))
        keep("plan_update.json", run("update", "--plan", "--json", "--", "user:alpha", "user:beta"))
        keep("update_result.json", run("update", "--yes", "--json", "--events-file", "rec.jsonl", "--", "user:alpha", "user:beta"))
        logs = work / "home" / "opt" / ".gupkg" / "logs"
        keep("events_global.jsonl", (logs / "rec.jsonl").read_text(encoding="utf-8"))
        for name in ("alpha", "beta"):
            keep(f"events_{name}.jsonl", (work / "home" / "opt" / name / ".gupkg" / "logs" / "rec.jsonl").read_text(encoding="utf-8"))
        print("recorded in", HERE)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
