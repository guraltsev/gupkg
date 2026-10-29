"""Cover hook API 2: loading, the typed context, the shared HTTP client and ``gupkg.hooklib``.

Hooks are real files in a ``pkg.local`` folder, loaded and run by the service. The boundary
is ``FakeSystem`` (URLs and processes). Covers the rule that every hook, including one in a
version directory, receives gupkg's HTTP client with its User-Agent and timeout, and that
update discovery (a ``track`` hook) is accepted only in a tracker. Out of scope: the
built-in providers (``test_track_providers``) and the declarative steps (``test_prepare``).
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from gupkg import hooklib
from gupkg._version import __version__
from gupkg.model.manifest import DiscoveredChecksum, ManifestKind, TrackResult, parse_manifest
from gupkg.pkgcheck import check_directory
from gupkg.services.hooks import (
    HookContext,
    HookError,
    HookPaths,
    call_populate,
    call_track,
    call_unpack,
    load_hook,
)
from gupkg.services.http import HttpClient
from gupkg.services.populate import run_custom_populate
from gupkg.services.track import TrackError, run_tracker
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.http_fixtures import environment
from tests.scope_support import scope_of


@pytest.fixture(autouse=True)
def not_elevated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make these tests independent of an ambient GUPKG_ELEVATED marker; the elevated cases set it themselves."""
    monkeypatch.delenv("GUPKG_ELEVATED", raising=False)


def write_hook(root: Path, name: str, body: str, *, api: int | None = 2) -> str:
    """Write ``pkg.local/<name>`` below *root* (with ``PKG_MODULE_API = api``) and return its manifest reference."""
    folder = root / "pkg.local"
    folder.mkdir(parents=True, exist_ok=True)
    header = f"PKG_MODULE_API = {api}\n" if api is not None else ""
    (folder / name).write_text(header + body, encoding="utf-8")
    return f"pkg.local/{name}"


def make_context(system: FakeSystem, root: Path, **paths) -> HookContext:
    """Return a hook context over *system* with a real shared client (no waiting, no ambient environment)."""
    client = HttpClient(system, env={}, sleep=lambda seconds: None, timeout=17.0, cache_dir=root / "cache")
    return HookContext("tool", "1.0", system, scope_of(system), client, HookPaths(root.parent, root, **paths))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_a_hook_declaring_api_2_loads_and_exposes_its_entry_point(tmp_path: Path, system: FakeSystem) -> None:
    """`PKG_MODULE_API = 2` loads, and the module's functions are callable."""
    reference = write_hook(tmp_path, "x.py", "def unpack(ctx):\n    return 'done'\n")

    module = load_hook(tmp_path, reference, scope_of(system))

    assert module.unpack(None) == "done"


@pytest.mark.parametrize("api", [1, 3, None], ids=["api-1", "api-3", "no-declaration"])
def test_a_hook_with_another_api_version_is_rejected_like_any_invalid_hook(tmp_path: Path, system: FakeSystem, api: int | None) -> None:
    """Version 1 (and anything but 2) is refused with the same plain message; core says nothing about migration (D33)."""
    reference = write_hook(tmp_path, "old.py", "def check_update(context):\n    return None\n", api=api)

    with pytest.raises(HookError) as raised:
        load_hook(tmp_path, reference, scope_of(system))

    assert str(raised.value) == "pkg.local/old.py must declare PKG_MODULE_API = 2"


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("pkg.local/../escape.py", id="parent-step"),
        pytest.param("elsewhere/x.py", id="outside-pkg-local"),
        pytest.param("pkg.local/x.txt", id="not-python"),
        pytest.param("pkg.local/missing.py", id="missing"),
    ],
)
def test_a_hook_path_that_is_not_a_python_file_inside_pkg_local_is_refused(tmp_path: Path, system: FakeSystem, reference: str) -> None:
    """Only existing `.py` files below `pkg.local` can be loaded as hooks."""
    (tmp_path / "escape.py").write_text("PKG_MODULE_API = 2\n", encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.py").write_text("PKG_MODULE_API = 2\n", encoding="utf-8")
    write_hook(tmp_path, "present.py", "")

    with pytest.raises(HookError):
        load_hook(tmp_path, reference, scope_of(system))


def test_a_hook_cannot_be_a_junction_to_a_file_outside(tmp_path: Path, system: FakeSystem) -> None:
    """A `pkg.local` that is a junction to another folder is refused, so a hook cannot be swapped in from elsewhere."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("PKG_MODULE_API = 2\n", encoding="utf-8")
    version = tmp_path / "version"
    version.mkdir()
    system.create_junction(version / "pkg.local", outside)

    with pytest.raises(HookError, match="link or junction"):
        load_hook(version, "pkg.local/x.py", scope_of(system))


def test_a_hook_may_import_a_sibling_module_and_leaves_no_bytecode(tmp_path: Path, system: FakeSystem) -> None:
    """Relative imports of the hook's own folder work; nothing is written beside the files and nothing leaks into `sys.modules`."""
    write_hook(tmp_path, "helper.py", "VALUE = 41\n", api=None)
    reference = write_hook(tmp_path, "main.py", "from . import helper\n\ndef unpack(ctx):\n    return helper.VALUE + 1\n")

    module = load_hook(tmp_path, reference, scope_of(system))

    assert module.unpack(None) == 42
    assert not (tmp_path / "pkg.local" / "__pycache__").exists()


def test_a_hook_that_imports_something_unavailable_fails_with_an_explanation(tmp_path: Path, system: FakeSystem) -> None:
    """An import that cannot be satisfied is reported with the allowed sources, and nothing is installed."""
    reference = write_hook(tmp_path, "x.py", "import module_that_does_not_exist_anywhere\n")

    with pytest.raises(HookError, match="module_that_does_not_exist_anywhere.*gupkg installs nothing|standard library"):
        load_hook(tmp_path, reference, scope_of(system))


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def test_a_track_hook_must_return_a_track_result(tmp_path: Path, system: FakeSystem) -> None:
    """`track(ctx)` returning a plain dictionary (the old shape) is an error, not a guess."""
    good = write_hook(tmp_path, "good.py", (
        "from gupkg.hooklib import TrackResult, DiscoveredChecksum\n"
        "def track(ctx):\n    return TrackResult('2.0', {'download_url': 'https://example.org/t.zip'}, DiscoveredChecksum('a' * 64, 'vendor-sums'))\n"
    ))
    bad = write_hook(tmp_path, "bad.py", "def track(ctx):\n    return {'version': '2.0'}\n")
    context = make_context(system, tmp_path)

    assert call_track(load_hook(tmp_path, good, scope_of(system)), context) == TrackResult(
        "2.0", {"download_url": "https://example.org/t.zip"}, DiscoveredChecksum("a" * 64, "vendor-sums")
    )
    with pytest.raises(HookError, match="must return a TrackResult"):
        call_track(load_hook(tmp_path, bad, scope_of(system)), context)


def test_a_hook_without_the_requested_entry_point_is_an_error(tmp_path: Path, system: FakeSystem) -> None:
    """Calling `unpack` on a module that defines only `track` names the missing function."""
    reference = write_hook(tmp_path, "x.py", "def track(ctx):\n    pass\n")

    with pytest.raises(HookError, match=r"does not define unpack\(ctx\)"):
        call_unpack(load_hook(tmp_path, reference, scope_of(system)), make_context(system, tmp_path))


def test_the_module_provider_runs_a_tracker_hook_through_run_tracker(tmp_path: Path, system: FakeSystem) -> None:
    """A tracker with `provider = "module"` calls the hook with a context whose options are the `[track]` keys."""
    tracker = tmp_path / "pkg" / "_check_updates"
    tracker.mkdir(parents=True)
    write_hook(tracker, "track.py", (
        "from gupkg.hooklib import TrackResult, fetch_text\n"
        "def track(ctx):\n    version = fetch_text(ctx, 'https://example.org/version.txt').strip()\n"
        "    return TrackResult(version, {'download_url': 'https://example.org/t-' + version + '.zip'})\n"
    ))
    (tracker / "pkg.toml").write_text(
        'format = 2\n[track]\nprovider = "module"\n[source]\ndownload_url = "${download_url}"\n', encoding="utf-8")
    system.urls["https://example.org/version.txt"] = "7.1\n"
    manifest = parse_manifest((tracker / "pkg.toml").read_text(encoding="utf-8"), ManifestKind.TRACKER)

    result = run_tracker(manifest, environment(system, tmp_path, directory=tracker, package="pkg"))

    assert result.version == "7.1" and result.captures["download_url"] == "https://example.org/t-7.1.zip"


def test_a_failing_track_hook_is_reported_as_a_track_error_with_its_name(tmp_path: Path, system: FakeSystem) -> None:
    """An exception in a tracker hook becomes a TrackError that names the hook file."""
    tracker = tmp_path / "pkg" / "_check_updates"
    tracker.mkdir(parents=True)
    write_hook(tracker, "track.py", "def track(ctx):\n    raise RuntimeError('boom')\n")
    manifest = parse_manifest('format = 2\n[track]\nprovider = "module"\n[source]\ndownload_url = "x"\n', ManifestKind.TRACKER)

    with pytest.raises(TrackError, match="pkg.local/track.py failed: boom"):
        run_tracker(manifest, environment(system, tmp_path, directory=tracker))


# ---------------------------------------------------------------------------
# The shared HTTP client in every hook (D28)
# ---------------------------------------------------------------------------


def test_a_version_directory_hook_gets_the_shared_client_with_the_user_agent_and_timeout(tmp_path: Path, system: FakeSystem) -> None:
    """A populate hook in a version directory fetches through `ctx.http`: its request carries gupkg's User-Agent and timeout."""
    version = tmp_path / "pkg" / "1.0"
    (version / "App").mkdir(parents=True)
    reference = write_hook(version, "populate.py", (
        "def populate(ctx):\n"
        "    text = ctx.http.text('https://example.org/payload.txt')\n"
        "    (ctx.paths.app / 'payload.txt').write_text(text, encoding='utf-8')\n"
    ))
    system.urls["https://example.org/payload.txt"] = "fetched"
    context = make_context(system, version, app=version / "App")

    call_populate(load_hook(version, reference, scope_of(system)), context)

    request = system.request_log[0]
    assert (version / "App" / "payload.txt").read_text() == "fetched"
    assert request.headers["User-Agent"] == f"gupkg/{__version__}" and request.timeout == 17.0


def test_a_version_directory_hook_that_uses_the_network_directly_validates_without_a_warning(tmp_path: Path) -> None:
    """Importing urllib, http, socket, requests or subprocess is allowed and silent in a version directory (D28)."""
    folder = tmp_path / "pkg" / "1.0"
    (folder / "pkg.local").mkdir(parents=True)
    (folder / "pkg.toml").write_text('format = 2\n[source]\nscript = "pkg.local/populate.py"\n', encoding="utf-8")
    (folder / "pkg.local" / "populate.py").write_text(
        "PKG_MODULE_API = 2\nimport urllib.request, http.client, socket, subprocess\n\n"
        "try:\n    import requests\nexcept ImportError:\n    requests = None\n\n"
        "def populate(ctx):\n    return urllib.request.urlopen('https://example.org').read()\n", encoding="utf-8")

    report = check_directory(folder)

    assert report.ok, report.errors
    assert report.warnings == ()


@pytest.mark.parametrize(
    ("toml", "hook", "message"),
    [
        pytest.param('[track]\nprovider = "json"\ncheck_url = "https://example.org/v.json"\nversion = "$.v"\n', None,
                     "_check_updates", id="track-table"),
        pytest.param('', "def track(ctx):\n    pass\n", "defines track", id="track-hook"),
    ],
)
def test_update_discovery_is_rejected_in_a_version_directory(tmp_path: Path, toml: str, hook: str | None, message: str) -> None:
    """A `[track]` table or a `track` hook in a version directory fails validation and the error names `_check_updates`."""
    folder = tmp_path / "pkg" / "1.0"
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text(f'format = 2\n{toml}[source]\ndownload_url = "https://example.org/t.zip"\n', encoding="utf-8")
    if hook:
        write_hook(folder, "x.py", hook)

    report = check_directory(folder)

    assert not report.ok and any(message in error and "_check_updates" in error for error in report.errors)


def test_the_context_is_given_to_a_non_python_script_as_json(tmp_path: Path, system: FakeSystem) -> None:
    """`to_json()` is a plain JSON document with the API version, names and string paths."""
    context = make_context(system, tmp_path / "pkg" / "1.0", app=tmp_path / "App")

    document = json.loads(context.to_json())

    assert document["api"] == 2 and document["package"] == "tool" and document["version"] == "1.0"
    assert document["paths"]["app"] == str(tmp_path / "App") and document["paths"]["artifact"] is None


# ---------------------------------------------------------------------------
# gupkg.hooklib
# ---------------------------------------------------------------------------


def test_hooklib_fetches_through_the_context_client(tmp_path: Path, system: FakeSystem) -> None:
    """`fetch_text`, `fetch_json`, `resolve_redirects` and `github_release` all use the shared client and its rules."""
    system.urls["https://example.org/t.txt"] = "text"
    system.urls["https://example.org/j.json"] = '{"a": [1, 2]}'
    system.urls["https://example.org/go"] = FakeHttp("", 302, {"Location": "https://example.org/final.zip"})
    system.urls["https://example.org/final.zip"] = FakeHttp("")
    system.urls["https://api.github.com/repos/o/r/releases/tags/v1"] = '{"tag_name": "v1", "assets": []}'
    context = make_context(system, tmp_path)

    assert hooklib.fetch_text(context, "https://example.org/t.txt") == "text"
    assert hooklib.fetch_json(context, "https://example.org/j.json") == {"a": [1, 2]}
    assert hooklib.resolve_redirects(context, "https://example.org/go") == "https://example.org/final.zip"
    assert hooklib.github_release(context, "o/r", "v1")["tag_name"] == "v1"
    assert all(request.headers["User-Agent"] == f"gupkg/{__version__}" for request in system.request_log)


def test_hooklib_extracts_flattens_and_runs_seven_zip_safely(tmp_path: Path, system: FakeSystem) -> None:
    """`extract_archive` applies the shared options, `safe_extract` refuses a hostile archive, `flatten` strips wrappers, `seven_zip` runs 7-Zip."""
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("wrap/bin/tool.exe", "x")
    hostile = tmp_path / "bad.zip"
    with zipfile.ZipFile(hostile, "w") as bundle:
        bundle.writestr("../evil.txt", "x")
    context = make_context(system, tmp_path)

    hooklib.extract_archive(context, archive, tmp_path / "out", strip=1, require=("bin/tool.exe",))
    with pytest.raises(Exception, match="unsafe member name"):
        hooklib.safe_extract(context, hostile, tmp_path / "bad-out")
    nested = tmp_path / "nest" / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "f.txt").write_text("f", encoding="utf-8")
    hooklib.flatten(tmp_path / "nest", "auto")
    output = hooklib.seven_zip(context, "i")

    assert (tmp_path / "out" / "bin" / "tool.exe").read_text() == "x" and not (tmp_path / "bad-out").exists()
    assert (tmp_path / "nest" / "f.txt").exists()
    assert "7-Zip" in output


def test_hooklib_find_digest_reads_a_sums_file() -> None:
    """`find_digest` returns the lower-case digest listed for a name, and `None` when it is not listed."""
    text = f"{'A' * 64}  win64/Setup 1.exe\n{'b' * 64} *other.zip\n"

    assert hooklib.find_digest(text, "win64/Setup 1.exe") == "a" * 64
    assert hooklib.find_digest(text, "other.zip") == "b" * 64
    assert hooklib.find_digest(text, "absent.zip") is None


def test_hooklib_run_prepare_applies_the_declarative_steps_to_the_staged_app(tmp_path: Path, system: FakeSystem) -> None:
    """A custom populate script can use the same `[[prepare]]` steps through its context (D27)."""
    app = tmp_path / "App"
    app.mkdir()
    (app / "a.exe").write_text("a", encoding="utf-8")
    context = make_context(system, tmp_path, app=app)

    hooklib.run_prepare(context, [hooklib.PrepareRename("a.exe", "b.exe"), hooklib.PrepareRemove(("missing",))])

    assert os.listdir(app) == ["b.exe"]


# ---------------------------------------------------------------------------
# Custom populate scripts (D27)
# ---------------------------------------------------------------------------


def test_a_non_python_populate_script_receives_the_context_as_json_on_standard_input(tmp_path: Path) -> None:
    """A `.cmd` script is started in the version directory with the context JSON on stdin, and may fill `App` itself."""
    version = tmp_path / "pkg" / "1.0"
    (version / "pkg.local").mkdir(parents=True)
    (version / "pkg.local" / "populate.cmd").write_text("@echo off\n", encoding="utf-8")
    received: list[ProcessCall] = []

    def script(call: ProcessCall) -> ProcessResult:
        """Record the call and pretend the script succeeded."""
        received.append(call)
        return ProcessResult(0, "downloading\n")

    system = FakeSystem(tmp_path / "machine", processes={"populate.cmd": script}, real_7zip=False)
    context = make_context(system, version, app=version / "App")

    run_custom_populate(context, "pkg.local/populate.cmd")

    call = received[0]
    assert Path(call.argv[0]) == version / "pkg.local" / "populate.cmd" and call.cwd == version
    assert json.loads(call.input_text)["paths"]["app"] == str(version / "App")


def test_a_failing_or_missing_non_python_script_is_a_hook_error(tmp_path: Path) -> None:
    """A non-zero exit status and a missing file are both HookError."""
    version = tmp_path / "pkg" / "1.0"
    (version / "pkg.local").mkdir(parents=True)
    (version / "pkg.local" / "bad.cmd").write_text("x", encoding="utf-8")
    system = FakeSystem(tmp_path / "machine", processes={"bad.cmd": lambda call: ProcessResult(3)}, real_7zip=False)
    context = make_context(system, version)

    with pytest.raises(HookError, match="exited with status 3"):
        run_custom_populate(context, "pkg.local/bad.cmd")
    with pytest.raises(HookError, match="does not exist"):
        run_custom_populate(context, "pkg.local/absent.cmd")


def test_a_python_populate_script_runs_as_a_hook_module(tmp_path: Path, system: FakeSystem) -> None:
    """A `.py` populate script is loaded as a hook (API 2) and its `populate(ctx)` fills `App`."""
    version = tmp_path / "pkg" / "1.0"
    app = version / "App"
    app.mkdir(parents=True)
    reference = write_hook(version, "populate.py", "def populate(ctx):\n    (ctx.paths.app / 'made.txt').write_text('ok', encoding='utf-8')\n")

    run_custom_populate(make_context(system, version, app=app), reference)

    assert (app / "made.txt").read_text() == "ok"
