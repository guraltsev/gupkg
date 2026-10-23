"""Cover validation of a format 2 version or tracker directory on disk.

Behavior covered: an `App` that is missing or empty is fine when the version can recover
it and otherwise fails with the exact text `App is missing and no way to recover it.`;
`shipped`, download and script-populated versions are reported with their kind; a
version directory with `[track]`, with a `pkg.local` module that defines `track`, or
with a templated source URL fails with an error that names `_check_updates`; a version
hook that imports `urllib` validates without a warning (decision D28); a tracker is
checked without needing an `App`.

Real: ``gupkg.pkgcheck.check_directory`` on directories built under pytest's temporary
folder. Mocked: nothing. Out of scope: running hooks and downloading anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import APP_MISSING_ERROR, ManifestKind
from gupkg.pkgcheck import check_directory, defines_track

URL = 'download_url = "https://example.org/tool-1.0.zip"'


def make_dir(root: Path, name: str, manifest: str, *, app: bool | str = False, files: dict[str, str] | None = None) -> Path:
    """Build a package folder `<root>/<name>` with a pkg.toml, an optional App and extra files."""
    folder = root / "pkg" / name
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text("format = 2\n" + manifest, encoding="utf-8")
    if app:
        (folder / "App").mkdir()
        (folder / "App" / "tool.exe").write_text("x", encoding="utf-8")
    for relative, text in (files or {}).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def test_unpopulated_version_with_a_download_url_is_valid_and_recoverable(tmp_path: Path) -> None:
    """A pinned version that has no App yet but a download URL is valid and reported as downloaded."""
    report = check_directory(make_dir(tmp_path, "1.0", f"[source]\n{URL}\n"))

    assert report.ok
    assert report.app_kind == "downloaded"
    assert report.manifest.source.download_url.endswith("tool-1.0.zip")


@pytest.mark.parametrize("empty_app", [False, True], ids=["missing-app", "empty-app"])
def test_missing_or_empty_app_without_a_populate_source_fails_with_the_exact_message(tmp_path: Path, empty_app: bool) -> None:
    """A version with neither a download nor a script and no App fails with exactly the designed error."""
    folder = make_dir(tmp_path, "1.0", "")
    if empty_app:
        (folder / "App").mkdir()

    report = check_directory(folder)

    assert report.errors == (APP_MISSING_ERROR,)
    assert APP_MISSING_ERROR == "App is missing and no way to recover it."


def test_shipped_version_with_an_app_is_valid(tmp_path: Path) -> None:
    """A shipped version with its App present validates, and is reported as shipped."""
    report = check_directory(make_dir(tmp_path, "1.0", "[source]\nshipped = true\n", app=True))

    assert report.ok and report.warnings == ()
    assert report.app_kind == "shipped"


def test_shipped_version_with_a_source_recovers_a_missing_app_with_a_warning(tmp_path: Path) -> None:
    """A shipped version that also has a download URL is reported as shipped with a source, and a missing App only warns."""
    report = check_directory(make_dir(tmp_path, "1.0", f"[source]\nshipped = true\n{URL}\n"))

    assert report.ok
    assert report.app_kind == "shipped (source available)"
    assert any("recovered from its source" in warning for warning in report.warnings)


@pytest.mark.parametrize("suffix", ["py", "ps1", "cmd", "bat", "exe"])
def test_custom_populate_script_in_pkg_local_validates(tmp_path: Path, suffix: str) -> None:
    """A version whose source names a Python, PowerShell, cmd, bat or executable script that exists validates."""
    script = f"pkg.local/populate.{suffix}"
    report = check_directory(make_dir(tmp_path, "1.0", f'[source]\nscript = "{script}"\n', files={script: "PKG_MODULE_API = 2\n"}))

    assert report.ok, report.errors
    assert report.app_kind == "script-populated"


def test_populate_script_that_does_not_exist_is_an_error(tmp_path: Path) -> None:
    """A source script that is not in the directory fails, and so does the App it was meant to build."""
    report = check_directory(make_dir(tmp_path, "1.0", '[source]\nscript = "pkg.local/populate.py"\n'))

    assert any("pkg.local/populate.py is named in pkg.toml but does not exist" in e for e in report.errors)
    assert APP_MISSING_ERROR in report.errors


def test_version_hook_that_imports_urllib_validates_without_a_warning(tmp_path: Path) -> None:
    """Network use by a version-directory script is allowed: no error and no warning for `urllib` (decision D28)."""
    hook = "PKG_MODULE_API = 2\nimport urllib.request\n\ndef populate(context):\n    return urllib.request.urlopen('https://example.org').read()\n"
    report = check_directory(make_dir(
        tmp_path, "1.0", '[source]\nscript = "pkg.local/populate.py"\n', files={"pkg.local/populate.py": hook},
    ))

    assert report.ok, report.errors
    assert report.warnings == ()


def test_track_table_in_a_version_directory_fails_naming_check_updates(tmp_path: Path) -> None:
    """A version directory with `[track]` fails validation and the error names `_check_updates`."""
    report = check_directory(make_dir(
        tmp_path, "1.0", f'[track]\nprovider = "json"\ncheck_url = "https://example.org/v.json"\n[source]\n{URL}\n'
    ))

    assert not report.ok
    assert any("_check_updates" in error for error in report.errors)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("def track(context):\n    return None\n", id="function"),
        pytest.param("track = lambda context: None\n", id="assignment"),
        pytest.param("from helpers import check as track\n", id="import-alias"),
        pytest.param("import sys\nif sys.platform:\n    def track(context):\n        pass\n", id="defined-under-if"),
        pytest.param("class track:\n    pass\n", id="class"),
    ],
)
def test_pkg_local_module_that_defines_track_fails_naming_check_updates(tmp_path: Path, code: str) -> None:
    """A version's `pkg.local` module that defines `track`, however it is spelled, fails and names `_check_updates`."""
    report = check_directory(make_dir(tmp_path, "1.0", f"[source]\n{URL}\n", files={"pkg.local/hook.py": code}))

    assert any("pkg.local/hook.py defines track" in e and "_check_updates" in e for e in report.errors)


def test_pkg_local_module_that_cannot_be_parsed_fails_closed(tmp_path: Path) -> None:
    """A module that cannot be read as Python cannot be shown free of `track`, so the version is invalid."""
    report = check_directory(make_dir(tmp_path, "1.0", f"[source]\n{URL}\n", files={"pkg.local/broken.py": "def (:\n"}))

    assert any("pkg.local/broken.py cannot be checked" in e for e in report.errors)


@pytest.mark.parametrize(
    "code",
    [
        pytest.param("try:\n    import fast\nexcept ImportError:\n    def track(context):\n        return None\n", id="except-handler"),
        pytest.param("import sys\nmatch sys.platform:\n    case 'win32':\n        def track(context):\n            pass\n", id="match-case"),
        pytest.param("try:\n    pass\nfinally:\n    track = 1\n", id="finally"),
        pytest.param("for _ in []:\n    pass\nelse:\n    def track(c):\n        pass\n", id="loop-else"),
    ],
)
def test_track_defined_in_handlers_cases_and_else_branches_is_found(code: str) -> None:
    """A `track` defined inside an except handler, a match case, a finally block or an else branch counts as defined."""
    assert defines_track(code)


def test_modules_that_merely_mention_track_are_fine(tmp_path: Path) -> None:
    """A helper that calls or names `track` inside a function, or a method called `track`, does not count as defining it."""
    code = "def run(context):\n    track = 1\n    return track\n\nclass A:\n    def track(self):\n        pass\n"
    report = check_directory(make_dir(tmp_path, "1.0", f"[source]\n{URL}\n", files={"pkg.local/h.py": code}))

    assert report.ok, report.errors
    assert not defines_track(code)


def test_templated_source_url_in_a_version_directory_fails_naming_check_updates(tmp_path: Path) -> None:
    """A version whose download URL still has `${version}` fails and names `_check_updates`."""
    report = check_directory(make_dir(tmp_path, "1.0", '[source]\ndownload_url = "https://example.org/${version}.zip"\n'))

    assert any("_check_updates" in error for error in report.errors)


def test_tracker_directory_is_valid_without_an_app_and_with_templates(tmp_path: Path) -> None:
    """`_check_updates` validates with templates and `[track]` and needs no App."""
    folder = make_dir(
        tmp_path, "_check_updates",
        '[track]\nprovider = "page"\ncheck_url = "https://example.org/dl"\nmatch = "(?P<version>[0-9.]+)"\n'
        '[source]\ndownload_url = "https://example.org/${version}.zip"\n',
    )

    report = check_directory(folder)

    assert report.ok, report.errors
    assert report.kind is ManifestKind.TRACKER
    assert report.app_kind is None


def test_tracker_with_a_module_provider_needs_its_module_file(tmp_path: Path) -> None:
    """A module tracker whose module file is absent fails, and passes once the file exists."""
    manifest = '[track]\nprovider = "module"\nmodule = "pkg.local/check_update.py"\n'

    missing = check_directory(make_dir(tmp_path / "a", "_check_updates", manifest))
    present = check_directory(make_dir(
        tmp_path / "b", "_check_updates", manifest, files={"pkg.local/check_update.py": "PKG_MODULE_API = 2\ndef track(ctx):\n    pass\n"},
    ))

    assert any("pkg.local/check_update.py is named in pkg.toml but does not exist" in e for e in missing.errors)
    assert present.ok, present.errors


def test_reserved_and_old_bootstrap_names_are_not_version_directories(tmp_path: Path) -> None:
    """`_bootstrap` is a reserved `_` name like any other and is not accepted as a version directory."""
    report = check_directory(make_dir(tmp_path, "_bootstrap", f"[source]\n{URL}\n"))

    assert any("cannot be a version directory" in error for error in report.errors)


def test_format_1_manifest_gets_only_the_generic_error(tmp_path: Path) -> None:
    """A manifest without `format = 2` is refused with the generic unsupported-format error and no other advice."""
    folder = tmp_path / "pkg" / "1.0"
    folder.mkdir(parents=True)
    (folder / "pkg.toml").write_text('description = "x"\n[origin]\nurl = "https://example.org/a.zip"\n', encoding="utf-8")

    report = check_directory(folder)

    assert len(report.errors) == 1
    assert report.errors[0].startswith("Unsupported pkg.toml format")
    assert "migrat" not in report.errors[0].lower()


def test_missing_directory_and_missing_manifest_are_errors(tmp_path: Path) -> None:
    """A path that is not a directory, or a directory without pkg.toml, is reported rather than crashing."""
    (tmp_path / "1.0").mkdir()

    assert not check_directory(tmp_path / "nope").ok
    assert any("Cannot read" in e for e in check_directory(tmp_path / "1.0").errors)
