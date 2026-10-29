"""A stand-in ``gupkg`` executable for the UI program tests, plus builders for the documents it replays.

``FakeGupkg`` writes a small Python script that behaves like the machine interface of the CLI: it records the argv it receives, prints the ``--json``
document that the test scheduled for that command (``update``, ``update:plan``, ``list`` ...), and writes an events file in the real layout (a global
file with ``run_start``, ``log_link``, ``package_start``, ``package_end`` and ``run_end``, and a file per package) into the folders the test names. A run can wait
for the stop marker it announced (to test cancelling) and a response can print anything at all (to test untrusted output). The recorded documents of the real
CLI live in ``tests/fixtures/ui`` and are loaded with ``load_fixture``.

The script is a Python file run with the test interpreter, so no ``.cmd`` quoting is involved; the real launcher is covered by the tests that run it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from gupkg.selfmgmt.location import RunningGupkg
from gupkg.system.fake import FakeSystem
from gupkg.report.documents import InfoRow, ListDocument, RegistryStatusDocument, read_info, read_list, read_registry_status
from gupkg_ui.client import Client
from gupkg_ui.reads import Reads, one_info
from gupkg_ui.settings import load_ui_settings
from gupkg_ui.startup import UiEnvironment

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ui"

SCRIPT = r'''
import json, os, sys, time, uuid
from pathlib import Path

base = Path(os.environ["FAKE_GUPKG_DIR"])
args = sys.argv[1:]
index = 0
while index < len(args) and args[index] in ("--settings", "--sandbox"):
    index += 2
command, rest = args[index], args[index + 1:]
options = rest[: rest.index("--")] if "--" in rest else rest
targets = rest[rest.index("--") + 1:] if "--" in rest else []
name = options[options.index("--events-file") + 1] if "--events-file" in options else None
key = command + (":plan" if "--plan" in options else "")
# The full argument list (the command and its options, without the machine-interface flags) is the most exact key: ``list`` and ``list --available`` differ.
skipped = {"--json", "--no-input"}
full, held = [command], False
for option in options:
    if held:
        held = False
    elif option == "--events-file":
        held = True
    elif option not in skipped:
        full.append(option)
full_key = " ".join(full)

def retry(function):
    """Try a file operation again a few times: a virus scanner or the test's own reader may hold the file for a moment on Windows."""
    for attempt in range(40):
        try:
            return function()
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def bump():
    counts_path = base / "counts.json"
    counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
    counts[matched] = counts.get(matched, 0) + 1
    counts_path.write_text(json.dumps(counts))
    return counts[matched] - 1


def log_call():
    with open(base / "calls.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": args, "key": key, "full_key": full_key, "targets": targets, "options": options, "elevated_env": os.environ.get("GUPKG_ELEVATED")}) + "\n")


scenario = retry(lambda: json.loads((base / "scenario.json").read_text()))
matched = next((name for name in (full_key, key, command) if scenario.get(name)), full_key)
turn = retry(bump)
retry(log_call)

responses = scenario.get(matched) or []
if not responses:
    print("no scheduled response for " + full_key, file=sys.stderr)
    sys.exit(97)
response = responses[min(turn, len(responses) - 1)]

if response.get("sleep"):
    time.sleep(response["sleep"])
events = response.get("events")
doc = response.get("doc")
if events is not None and name is not None:
    stop_file = str(Path(os.environ["TEMP"]) / ("gupkg-stop-" + uuid.uuid4().hex))
    logs = Path(events["dir"])
    logs.mkdir(parents=True, exist_ok=True)
    glob = open(logs / name, "w", encoding="utf-8", newline="\n")

    def put(handle, record):
        handle.write(json.dumps(record) + "\n")
        handle.flush()

    put(glob, {"event": "run_start", "command": command, "argv": [command], "mode": "auto", "elevated": bool(events.get("elevated")), "version": "test", "api": 2, "stop_file": stop_file})
    stopped = False
    for position, package in enumerate(events.get("packages", [])):
        if stopped:
            break
        folder = Path(package["path"]) / ".gupkg" / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        events_file = folder / name
        put(glob, {"event": "log_link", "package": package["name"], "log": str(folder / "run.log"), "events": str(folder / "run.jsonl"), "events_file": package.get("events_file", str(events_file))})
        view = {"name": package["name"], "path": package["path"], "root": str(Path(package["path"]).parent), "sole": False}
        put(glob, {"event": "package_start", "view": view})
        with open(events_file, "w", encoding="utf-8", newline="\n") as own:
            put(own, {"event": "run_start", "command": command, "argv": [command], "mode": "auto", "elevated": False, "version": "test", "api": 2})
            put(own, {"event": "package_start", "view": view})
            for record in package.get("records", []):
                put(own, record)
                if package.get("delay"):
                    time.sleep(package["delay"])
            put(own, {"event": "package_end", "result": {"name": package["name"], "ok": True, "status": "updated"}})
        put(glob, {"event": "package_end", "result": {"name": package["name"], "ok": True, "status": "updated"}})
        if events.get("stop_after") == position:
            deadline = time.time() + 20
            while not os.path.lexists(stop_file) and time.time() < deadline:
                time.sleep(0.02)
            with open(base / "calls.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"saw_stop": os.path.lexists(stop_file), "stop_file": stop_file}) + "\n")
            stopped = True
    if events.get("run_end", True):
        put(glob, {"event": "run_end", "summary": "", "exit_code": (doc or {}).get("exit_code", 0), "result": events.get("result", doc)})
    glob.close()

if "raw" in response:
    sys.stdout.write(response["raw"])
elif doc is not None:
    sys.stdout.write(json.dumps(doc))
sys.stdout.flush()
sys.exit(response.get("exit", (doc or {}).get("exit_code", 0)))
'''


class FakeGupkg:
    """A scripted stand-in for the ``gupkg`` command line; see the module docstring.

    Parameters
    ----------
    base : Path
        A temporary folder the double keeps its script, scenario and call log in (and its own ``temp`` folder for stop markers).
    """

    def __init__(self, base: Path) -> None:
        self.dir = base / "fake-gupkg"
        self.dir.mkdir()
        self.temp = base / "fake-temp"
        self.temp.mkdir()
        (self.dir / "fake_gupkg.py").write_text(SCRIPT, encoding="utf-8")
        self.scenario: dict[str, list[dict[str, Any]]] = {}
        # What the manager reads in-process (``list``, ``list --available``, ``info``, ``registry status``) is served by ``reads()``, not by the script.
        self.read_scenario: dict[str, list[dict[str, Any]]] = {}
        self.read_log: list[str] = []
        self._write()

    def _write(self) -> None:
        """Save the scenario where the script reads it."""
        (self.dir / "scenario.json").write_text(json.dumps(self.scenario), encoding="utf-8")

    def respond(self, key: str, **response: Any) -> None:
        """Schedule the next response for *key*; the last one repeats.

        *key* is the full argument list of the call (``install --plan``), or the command word (``update``) or the command word with ``:plan`` (``update:plan``). The
        reads of the manager (``READ_KEYS``: ``list``, ``list --available``, ``info``, ``registry status``) are served in-process by ``reads()``, not by the script;
        their response holds ``doc``, or ``error`` (an exception to raise).
        """
        if key in READ_KEYS:
            self.read_scenario.setdefault(key, []).append(response)
            return
        self.scenario.setdefault(key, []).append(response)
        self._write()

    def reads(self) -> FakeReads:
        """Return the in-process reads double that serves the scheduled read responses."""
        return FakeReads(self)

    def read_calls(self) -> list[str]:
        """Return the keys of the reads that were made, in order."""
        return list(self.read_log)

    def client(self, *root_options: str) -> Client:
        """Return a ``Client`` that runs the double instead of the CLI, with an environment of its own (no ambient variable leaks in)."""
        env = {"FAKE_GUPKG_DIR": str(self.dir), "TEMP": str(self.temp), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        return Client((sys.executable, str(self.dir / "fake_gupkg.py")), tuple(root_options), env)

    def calls(self) -> list[dict[str, Any]]:
        """Return the calls the double received, in order (each has ``argv``, ``key``, ``targets`` and ``options``)."""
        path = self.dir / "calls.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        return [item for item in map(json.loads, lines) if "argv" in item]

    def stop_events(self) -> list[dict[str, Any]]:
        """Return what the double saw of its stop marker (``saw_stop``)."""
        path = self.dir / "calls.jsonl"
        return [item for item in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if "saw_stop" in item] if path.exists() else []


READ_KEYS = ("list", "list --available", "info", "registry status")


class FakeReads(Reads):
    """The in-process reads of the manager, served from the documents a test scheduled (read through the same typed reader as the real ones).

    The real ``Reads`` calls the core; this double returns what ``FakeGupkg.respond`` was given for ``list``, ``list --available``, ``info`` and ``registry status``
    (a ``doc``, run through ``read_list``, ``read_info`` or ``read_registry_status``, or an ``error`` to raise). Every read is logged in ``FakeGupkg.read_log``.
    """

    def __init__(self, fake: FakeGupkg) -> None:
        self._fake = fake
        self._turns: dict[str, int] = {}

    def _serve(self, key: str) -> Any:
        """Return the scheduled document for *key* (the last one repeats), or raise the scheduled error."""
        self._fake.read_log.append(key)
        responses = self._fake.read_scenario.get(key)
        assert responses, f"no scheduled read for {key}"
        turn = self._turns.get(key, 0)
        self._turns[key] = turn + 1
        response = responses[min(turn, len(responses) - 1)]
        if "error" in response:
            raise response["error"]
        return response["doc"]

    def list_document(self, available: bool) -> ListDocument:
        """Serve ``list`` or ``list --available``."""
        return read_list(self._serve("list --available" if available else "list"))

    def info_document(self, target: str) -> InfoRow:
        """Serve ``info``."""
        return one_info(read_info(self._serve("info")), target)

    def registry_document(self) -> RegistryStatusDocument:
        """Serve ``registry status``."""
        return read_registry_status(self._serve("registry status"))


def load_fixture(name: str, root: Path, folder: str = "ui") -> str:
    """Return the recorded fixture *name* of *folder* (``ui``: the world of the UI tests with its events files; ``cli_docs``: one document of every kind) with ``@ROOT@`` replaced by *root* (JSON-escaped, which is also right inside a JSON line)."""
    escaped = json.dumps(str(root))[1:-1]
    return (FIXTURES.parent / folder / name).read_text(encoding="utf-8").replace("@ROOT@", escaped)


def fixture_json(name: str, root: Path, folder: str = "ui") -> Any:
    """Return the recorded JSON fixture *name* parsed, with *root* put in."""
    return json.loads(load_fixture(name, root, folder))


def head(command: str, *, ok: bool = True, code: int = 0, **fields: Any) -> dict[str, Any]:
    """Return the common head of a result document of this interface version."""
    return {"api": 2, "command": command, "ok": ok, "exit_code": code, **fields}


def entry(package: str, operation: str = "update", *, ok: bool = True, status: str = "updated", version: str | None = "2.0.0", previous: str | None = "1.0.0",
          mode: str = "user", code: int = 0, error: dict[str, Any] | None = None, log: str | None = None) -> dict[str, Any]:
    """Return one per-target result entry in the pinned shape."""
    data: dict[str, Any] = {
        "package": package, "operation": operation, "ok": ok, "status": status, "version": version, "previous": previous, "mode": mode,
        "exit_code": code, "receipt": None, "log": log, "warnings": [],
    }
    if error is not None:
        data["error"] = error
    return data


def result(command: str, entries: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    """Return a lifecycle result document for *entries*; its ``ok`` and exit code follow the entries."""
    failed = [item["exit_code"] or 3 for item in entries if not item["ok"]]
    return head(command, ok=not failed, code=max(failed, default=0), results=entries, warnings=[], **fields)


def plan_entry(target: str, package: str, *, mode: str = "user", version: str = "2.0.0", previous: str = "1.0.0", sha256: str | None = None,
               url: str = "https://example.org/x.zip", skippable: bool = True, elevation: bool | None = None) -> dict[str, Any]:
    """Return one ready entry of a ``--plan --json`` document; *sha256* ``present``/``missing`` adds the checksum plan of a download."""
    plan: dict[str, Any] = {
        "operation": "update", "package": package, "path": f"C:\\opt\\{package}", "mode": mode, "version": version, "previous": previous, "effects": [], "phases": [], "notes": [], "sha256": sha256, "url": url if sha256 else None,
    }
    if sha256 is not None:
        plan["populate"] = {"kind": "download", "needed": True, "reason": "download", "recoverable": True, "url": url,
                            "checksum": {"sha256": sha256, "url": url, "url_from": "pkg.toml", "skippable": skippable and sha256 == "missing" and url.startswith("https://")}}
    return {"target": target, "ok": True, "exit_code": 0, "plan": plan}


def plan_failure(target: str, code: str, message: str, *, exit_code: int = 2, package: str | None = None) -> dict[str, Any]:
    """Return one failed entry of a ``--plan --json`` document."""
    return {"target": target, "ok": False, "exit_code": exit_code, "result": entry(package or target, ok=False, status="failed", version=None, previous=None,
                                                                                 mode="user", code=exit_code, error={"code": code, "message": message})}


def plan_doc(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a ``--plan --json`` document of *entries*."""
    failed = [item["exit_code"] or 3 for item in entries if not item["ok"]]
    return head("update", ok=not failed, code=max(failed, default=0), plan=True, plans=entries)


def list_doc(packages: list[tuple[str, str, str, str | None, str | None, str | None]]) -> dict[str, Any]:
    """Return a ``list --json`` document from ``(name, mode, status, version, newest_known, path)`` rows.

    A run has one scope, so the document names its ``mode`` once (the first row's, ``user`` for no rows) and the rows carry none.
    """
    rows = [
        {"name": name, "status": status, "version": version, "newest_known": newest, "path": path, "restriction": None, "restriction_reason": None,
         "following": None, "problems": [], "warnings": []}
        for name, _, status, version, newest, path in packages
    ]
    mode = packages[0][1] if packages else "user"
    return head("list", mode=mode, root=f"C:\\opt-{mode}", packages=rows, warnings=[])


def registry_doc(revision: str | None, *, name: str = "main", synchronized_at: str | None = None) -> dict[str, Any]:
    """Return a ``registry status --json`` document with one registry at *revision* (``None``: never downloaded)."""
    return head("registry status", mode="user", registries=[{
        "name": name, "source": "https://example.org/main.zip", "revision": revision, "synchronized_at": synchronized_at, "last_failure": None,
        "cache": "C:\\opt\\.gupkg\\registry\\main", "packages": 0, "problems": [],
    }])


async def until(condition, what: str, timeout: float = 20.0) -> None:
    """Wait on the event loop until *condition()* holds; fail with *what* after *timeout* seconds (a bounded poll, never a fixed sleep)."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


def unique(prefix: str = "run") -> str:
    """Return a fresh name for an events file."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}.jsonl"


# ---------------------------------------------------------------------------
# An environment for the program logic
# ---------------------------------------------------------------------------


class ListSink:
    """A ``Sink`` that keeps everything it is given, for assertions on what a run showed."""

    def __init__(self) -> None:
        self.shown: list[str] = []
        self.states: list[str] = []
        self.live: list[str | None] = []

    def lines(self, lines) -> None:
        """Remember the text of each line."""
        self.shown.extend(line.plain for line in lines)

    def progress(self, text: str | None) -> None:
        """Remember the live progress text."""
        self.live.append(text)

    def status(self, text: str) -> None:
        """Remember the run state."""
        self.states.append(text)


def make_env(base: Path, fake: FakeGupkg, *, settings: str = "", user_layer: str = "", root_options: tuple[str, ...] = ()) -> UiEnvironment:
    """Return a ``UiEnvironment`` whose client runs *fake* and whose settings come from a temporary file with roots under *base* (and *user_layer* as the settings file of the default user root)."""
    roots = base / "roots"
    file = base / "ui-settings.toml"
    file.write_text(
        f"schema = 4\n[roots]\nsystem = '{roots / 'sys'}'\nuser = '{roots / 'user'}'\n"
        f"[integration]\nbin = '{roots / 'ubin'}'\nstart_menu = '{roots / 'usm'}'\n{settings}\n",
        encoding="utf-8",
    )
    if user_layer:
        # The root layer is read from the user root that ``[roots].user`` of --settings names.
        (roots / "user" / ".gupkg").mkdir(parents=True, exist_ok=True)
        (roots / "user" / ".gupkg" / "settings.toml").write_text("schema = 4\n" + user_layer, encoding="utf-8")
    package = base / "tree" / "gupkg"
    package.mkdir(parents=True, exist_ok=True)
    where = RunningGupkg("source", package, (package,))
    env = {"USERPROFILE": str(base / "home"), "APPDATA": str(base / "appdata")}
    system = FakeSystem(base / "machine")
    loaded = load_ui_settings(settings_path=file, env=env, location=where, system=system)
    return UiEnvironment(fake.client(*root_options), loaded, where, system, env, fake.reads())


# ---------------------------------------------------------------------------
# The real command line, on a pretend machine
# ---------------------------------------------------------------------------

LAUNCHERS = Path(__file__).resolve().parents[1] / "src" / "gupkg"


class RealWorld:
    """Temporary roots and a settings file, and the real launchers run against them with ``--settings`` and ``--sandbox``.

    ``--sandbox`` fakes only the machine (registry, shortcuts, elevation); ``--settings`` puts every root inside the test's folder, so nothing outside it is read
    or written, and no prompt can happen: the sandbox's own state decides elevation.
    """

    def __init__(self, base: Path, *, settings: str = "") -> None:
        self.base = base
        self.user = base / "home" / "opt"
        self.system = base / "sys" / "opt"
        self.system.mkdir(parents=True)
        (base / "temp").mkdir()
        self.settings = base / "settings.toml"
        self.settings.write_text(
            f"schema = 4\n[roots]\nsystem = '{self.system}'\nuser = '{self.user}'\n"
            f"[integration]\nbin = '{base / 'home' / 'bin'}'\nstart_menu = '{base / 'home' / 'sm'}'\n{settings}\n",
            encoding="utf-8",
        )
        # The system mode reads no --settings file: its folders come from its own root's settings.
        (self.system / ".gupkg").mkdir(parents=True)
        (self.system / ".gupkg" / "settings.toml").write_text(
            f"schema = 4\n[integration]\nbin = '{base / 'sys' / 'bin'}'\nstart_menu = '{base / 'sys' / 'sm'}'\n", encoding="utf-8"
        )
        self.env = {
            "USERPROFILE": str(base / "home"), "APPDATA": str(base / "appdata"), "TEMP": str(base / "temp"), "TMP": str(base / "temp"),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        }
        self.root_options = ["--settings", str(self.settings), "--sandbox", str(base / "sandbox")]

    def package(self, name: str, versions: tuple[str, ...] = ("1.0.0", "2.0.0"), *, root: Path | None = None) -> None:
        """Write *versions* of package *name* with shipped Apps (nothing needs the network)."""
        for version in versions:
            folder = (root or self.user) / name / version
            (folder / "App").mkdir(parents=True)
            (folder / "pkg.toml").write_text(f'format = 2\ndescription = "{name}"\n[source]\nshipped = true\n', encoding="utf-8")
            (folder / "App" / "app.exe").write_text(version, encoding="utf-8")

    def run(self, launcher: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        """Run the real launcher *launcher* (``gupkg``, ``gupkg-manager``, ``gupkg-tui``) with the root options and return the completed process."""
        return subprocess.run(
            [str(LAUNCHERS / f"{launcher}.cmd"), *self.root_options, *args], capture_output=True, text=True, encoding="utf-8", env=self.env,
            stdin=subprocess.DEVNULL, timeout=timeout, check=False,
        )

    def install(self, *targets: str) -> None:
        """Install *targets* with the real CLI (it must succeed)."""
        done = self.run("gupkg", "install", *targets, "--yes", "--no-input")
        assert done.returncode == 0, done.stderr

    def active(self, name: str, root: Path | None = None) -> str:
        """Return the version a package's ``current`` junction points at."""
        return os.path.basename(os.path.realpath((root or self.user) / name / "current"))


# ---------------------------------------------------------------------------
# A launcher that is a .cmd with a Python grandchild, like the real one
# ---------------------------------------------------------------------------

STUB = r'''
import json, os, sys, time, uuid
from pathlib import Path

base = Path(os.environ["STUB_DIR"])
mode = (base / "mode.txt").read_text().strip()
args = sys.argv[1:]
name = args[args.index("--events-file") + 1] if "--events-file" in args else None
doc = {"api": 2, "command": "update", "ok": True, "exit_code": 0, "results": [], "warnings": []}


def put(handle, record):
    handle.write(json.dumps(record) + "\n")
    handle.flush()


stop = Path(os.environ["TEMP"]) / ("gupkg-stop-" + uuid.uuid4().hex)
events = None
if name:
    logs = Path(os.environ["STUB_LOGS"])
    logs.mkdir(parents=True, exist_ok=True)
    events = open(logs / name, "w", encoding="utf-8", newline="\n")
    put(events, {"event": "run_start", "command": "update", "argv": [], "mode": "auto", "elevated": False, "version": "t", "api": 2, "stop_file": str(stop)})
(base / "child.pid").write_text(str(os.getpid()))

if mode in ("hang", "ignore-stop"):
    time.sleep(300)
elif mode == "stop-aware":
    deadline = time.time() + 60
    while not stop.exists() and time.time() < deadline:
        time.sleep(0.02)
    (base / "saw_stop.txt").write_text(str(stop.exists()))
    put(events, {"event": "run_end", "summary": "", "exit_code": 0, "result": doc})
    print(json.dumps(doc))
elif mode == "finish-then-hang":
    put(events, {"event": "run_end", "summary": "", "exit_code": 0, "result": doc})
    time.sleep(300)
elif mode == "slow":
    time.sleep(1.5)
    put(events, {"event": "run_end", "summary": "", "exit_code": 0, "result": doc})
    print(json.dumps(doc))
'''


class GrandchildStub:
    """A launcher that is a ``.cmd`` file starting a Python process, so the process the client holds is ``cmd.exe`` and the real work is its child.

    That is the shape of the real ``gupkg.cmd``; killing only the process the client started would leave the Python child running. The child's behaviour
    (``hang``, ``ignore-stop``, ``stop-aware``, ``finish-then-hang``, ``slow``) is chosen with ``set_mode``; it records its process id in ``child.pid``.
    """

    def __init__(self, base: Path) -> None:
        self.dir = base / "stub"
        self.dir.mkdir()
        self.temp = base / "stub-temp"
        self.temp.mkdir()
        self.logs = base / "stub-root" / ".gupkg" / "logs"
        (self.dir / "stub.py").write_text(STUB, encoding="utf-8")
        (self.dir / "launcher.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{self.dir / "stub.py"}" %*\r\n', encoding="ascii")

    def set_mode(self, mode: str) -> None:
        """Choose how the child behaves."""
        (self.dir / "mode.txt").write_text(mode, encoding="utf-8")

    def client(self) -> Client:
        """Return a ``Client`` that runs the ``.cmd`` launcher, with an environment of its own."""
        env = {"STUB_DIR": str(self.dir), "STUB_LOGS": str(self.logs), "TEMP": str(self.temp), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        return Client((str(self.dir / "launcher.cmd"),), (), env)

    def child_pid(self) -> int | None:
        """Return the process id the child wrote, or ``None`` before it started."""
        path = self.dir / "child.pid"
        try:
            return int(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def process_alive(pid: int) -> bool:
    """Return whether process *pid* exists (asked of ``tasklist``; signal 0 would be a kill on Windows)."""
    done = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, check=False)
    return f'"{pid}"' in done.stdout
