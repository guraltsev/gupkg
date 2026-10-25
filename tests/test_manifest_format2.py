"""Cover parsing, validation and rendering of ``pkg.toml`` format 2.

Behavior covered: the design's own examples parse; format 1 files and every 0.17 spelling
are refused like any other invalid file; a version has one source, and the three URLs
(`project_url`, `[source] download_url`, `[track] check_url`) are separate keys; the
`shipped` rules; `record_manifest`; path variables and their mode-dependent validity;
templates and their filters; `[track]` and templates only in a tracker; and
``render_pinned`` turning a tracker plus a discovered version into an ordinary version.

Real: the pure ``gupkg.model.manifest`` parser and writer on real TOML text. Mocked:
nothing. Out of scope: reading files (see ``test_pkgcheck.py``) and the providers' own
validation, which a later issue adds.
"""

from __future__ import annotations

import pytest

from gupkg.model.manifest import (
    DiscoveredChecksum,
    ManifestError,
    ManifestKind,
    TrackResult,
    manifest_to_toml,
    parse_manifest,
    render_pinned,
)

SHA = "ab" * 32

PINNED = """\
format = 2
description = "Fast text search"
project_url = "https://github.com/BurntSushi/ripgrep"
modes = ["system", "user"]

[source]
download_url = "https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep-14.1.0-x86_64-pc-windows-msvc.zip"
sha256 = "%s"
sha256_from = "github-digest"
strip = 1

[[shortcut]]
name = "Ripgrep"
target = "$App\\\\rg.exe"

[[env]]
name = "RIPGREP_HOME"
value = "$App"

[[path]]
value = "$App"

[[shim]]
name = "rg"
target = "$App\\\\rg.exe"
""" % SHA

FIREFOX_TRACKER = """\
format = 2
description = "Mozilla Firefox web browser"
project_url = "https://www.mozilla.org/firefox/"
modes = ["system", "user", "portable"]

[track]
provider = "json"
check_url = "https://product-details.mozilla.org/1.0/firefox_versions.json"
version = "$.LATEST_FIREFOX_VERSION"

[source]
download_url = "https://archive.mozilla.org/pub/firefox/releases/${version}/win64/en-US/Firefox%20Setup%20${version}.exe"
kind = "sfx"
subdir = "core"
require = ["firefox.exe"]

[source.checksum]
sums_file = "https://archive.mozilla.org/pub/firefox/releases/${version}/SHA256SUMS"
entry = "win64/en-US/Firefox Setup ${version}.exe"

[portable]
args = ["-profile", "$Data\\\\profile"]

[[shortcut]]
name = "Firefox"
target = "$App\\\\firefox.exe"
"""


def version_manifest(body: str = "", header: str = 'format = 2\n'):
    """Parse a small version manifest made of a header and a body."""
    return parse_manifest(header + body)


def problems(text: str, kind: ManifestKind = ManifestKind.VERSION) -> list[str]:
    """Return the problems ``parse_manifest`` reports for *text*, failing when there are none."""
    with pytest.raises(ManifestError) as caught:
        parse_manifest(text, kind)
    return list(caught.value.problems)


# ---------------------------------------------------------------------------
# The design's examples
# ---------------------------------------------------------------------------


def test_pinned_version_example_parses_into_typed_data() -> None:
    """The pinned-version example of design section 7.2 parses, with every field typed."""
    manifest = parse_manifest(PINNED)

    assert manifest.kind is ManifestKind.VERSION
    assert manifest.modes == ("system", "user")
    assert manifest.project_url == "https://github.com/BurntSushi/ripgrep"
    assert manifest.source.sha256 == SHA
    assert manifest.source.sha256_from == "github-digest"
    assert manifest.source.strip == 1
    assert manifest.source.download_url.endswith("pc-windows-msvc.zip")
    assert [(s.name, s.target) for s in manifest.shortcuts] == [("Ripgrep", "$App\\rg.exe")]
    assert [(e.name, e.value) for e in manifest.envs] == [("RIPGREP_HOME", "$App")]
    assert manifest.paths == ("$App",)
    assert [(s.name, s.type, s.forward_args) for s in manifest.shims] == [("rg", "console", True)]


def test_tracker_example_parses_with_templates_and_track() -> None:
    """The firefox tracker of design section 7.3 parses as a tracker with templates and `[track]`."""
    manifest = parse_manifest(FIREFOX_TRACKER, ManifestKind.TRACKER)

    assert manifest.track.provider == "json"
    assert manifest.track.check_url.endswith("firefox_versions.json")
    assert manifest.track.options["version"] == "$.LATEST_FIREFOX_VERSION"
    assert "${version}" in manifest.source.download_url
    assert manifest.source.checksum.sums_file.endswith("SHA256SUMS")
    assert manifest.source.require == ("firefox.exe",)
    assert manifest.portable.args == ("-profile", "$Data\\profile")
    assert manifest.supports("portable")


def test_manifest_written_back_to_toml_parses_to_the_same_manifest() -> None:
    """Writing a manifest with the one TOML writer and reading it again changes nothing."""
    for text, kind in ((PINNED, ManifestKind.VERSION), (FIREFOX_TRACKER, ManifestKind.TRACKER)):
        original = parse_manifest(text, kind)
        assert parse_manifest(manifest_to_toml(original), kind) == original


def test_every_section_of_a_full_manifest_round_trips() -> None:
    """Prepare steps, portable and nonportable tables, shims with content and nested archives survive a write and read."""
    text = """\
format = 2
description = "Everything"
modes = ["system", "user", "portable"]
requires = ["vcredist"]
record_manifest = false

[source]
download_url = "https://example.org/a-1.0.7z"
kind = "7z"
strip = "auto"
require = ["a.exe"]
verify_manifest = "MANIFEST.sha256"

[[source.nested]]
path = "inner.zip"
dest = "plugins"
replace = true

[[source.extract]]
src = "x-*/bin/*"
dest = ""

[[prepare]]
remove = ["$PLUGINSDIR", "uninstall.exe"]

[[prepare]]
rename = { from = "setup_*.exe", to = "app.exe" }

[[prepare]]
copy = { from = "shim.exe", to = ["node.exe", "npm.exe"] }

[[prepare]]
write = { path = "portable.ini", content = "x=1" }

[[prepare]]
module = "pkg.local/fix.py"

[portable]
persist = ["Data"]
markers = ["portable.txt", { path = "a.xml", content = "" }]
args = ["--portable"]
env = { HOME = "$Data" }

[nonportable]
remove = ["portable"]

[[shim]]
name = "tool"
target = "$App\\\\tool.exe"
type = "gui"
arguments = ["--x"]
forward_args = false
elevate = true
working_dir = "$App"

[[shim]]
name = "tool2"
content = "@echo off"
"""
    original = parse_manifest(text)

    assert original.record_manifest is False
    assert parse_manifest(manifest_to_toml(original)) == original


# ---------------------------------------------------------------------------
# One source per version, the format, and 0.17 spellings
# ---------------------------------------------------------------------------


def test_origin_versions_table_is_refused() -> None:
    """A per-version source list (`[[origin.versions]]`) is not part of format 2."""
    found = problems('format = 2\n[[origin.versions]]\nversion = "1.0"\nurl = "https://x.test/a.zip"\n')

    assert any("unknown key 'origin'" in problem for problem in found)


@pytest.mark.parametrize("key", ["version", "localVersion", "name"])
def test_identity_keys_are_refused_because_the_folder_names_the_version(key: str) -> None:
    """A top-level `version`, `localVersion` or `name` is rejected: identity lives in folder names."""
    found = problems(f'format = 2\n{key} = "1.0"\n')

    assert len(found) == 1
    assert f"{key}: is not allowed" in found[0]


def test_more_than_one_source_is_refused() -> None:
    """Two `[[source]]` tables are rejected: a version has exactly one source."""
    found = problems('format = 2\n[[source]]\ndownload_url = "https://x.test/a.zip"\n[[source]]\ndownload_url = "https://x.test/b.zip"\n')

    assert any("exactly one [source] table" in problem for problem in found)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("description = 'x'\n", id="no-format"),
        pytest.param("format = 1\ndescription = 'x'\n", id="format-1"),
        pytest.param("format = 3\n", id="future-format"),
        pytest.param('format = "2"\n', id="format-as-string"),
        pytest.param("format = true\n", id="format-as-boolean"),
    ],
)
def test_files_without_format_2_get_the_generic_unsupported_format_error(text: str) -> None:
    """Anything but `format = 2` is refused with one generic error that does not mention any other tool."""
    found = problems(text)

    assert len(found) == 1
    assert found[0].startswith("Unsupported pkg.toml format")
    assert "migrat" not in found[0].lower()


@pytest.mark.parametrize(
    "fragment",
    [
        pytest.param('homepage = "https://x.test"\n', id="homepage"),
        pytest.param('[origin]\nurl = "https://x.test/a.zip"\n', id="origin"),
        pytest.param('[update]\n[update.check]\nmode = "module"\n', id="update"),
        pytest.param("only_portable = true\n", id="only_portable"),
        pytest.param('[[bin]]\nname = "x"\ntarget = "$App\\\\x.exe"\n', id="bin"),
        pytest.param('[[environment]]\nName = "A"\nValue = "b"\n', id="environment"),
        pytest.param('[[shortcut]]\nname = "S"\ntargetPath = "$App\\\\s.exe"\n', id="targetPath"),
        pytest.param('[[env]]\nName = "A"\nValue = "b"\n', id="Name-Value"),
        pytest.param('[source]\ndownload_url = "https://x.test/a.zip"\nignore_checksum = true\n', id="ignore_checksum"),
        pytest.param('[[shim]]\nname = "s"\ntarget = "$App\\\\s.exe"\nworkingDirectory = "$App"\n', id="workingDirectory"),
        pytest.param('[source]\nlocal = "pkg.local/App"\n', id="source-local"),
    ],
)
def test_every_0_17_spelling_is_an_unknown_key(fragment: str) -> None:
    """Each 0.17 key or table is rejected as an unknown key, with no alias and no format 1 hint."""
    found = problems("format = 2\n" + fragment)

    assert any("unknown key" in problem for problem in found)
    assert not any("0.17" in problem or "legacy" in problem.lower() for problem in found)


def test_unknown_key_gets_a_did_you_mean_hint() -> None:
    """A near miss names the key that was probably meant."""
    found = problems('format = 2\n[[shortcut]]\nname = "S"\ntargetPath = "$App\\\\s.exe"\n')

    assert any("targetPath" in problem and "did you mean 'target'" in problem for problem in found)


def test_every_problem_is_reported_at_once() -> None:
    """A file with several faults lists all of them in one error."""
    found = problems(
        'format = 2\nmodes = ["mars"]\nrecord_manifest = "no"\n[[shortcut]]\nname = "x"\n[source]\nsha256 = "zz"\n'
    )

    assert len(found) >= 4


def test_invalid_toml_is_reported_as_an_error() -> None:
    """Text that is not TOML raises the same error type as any other invalid file."""
    assert "Not valid TOML" in problems("format = [")[0]


# ---------------------------------------------------------------------------
# `[source] shipped` and how App is populated
# ---------------------------------------------------------------------------


def test_shipped_defaults_to_true_without_a_populate_script_and_false_with_one() -> None:
    """With no `shipped` key the effective value is true for a bare source and false for a download or a script."""
    bare = version_manifest()
    downloaded = version_manifest('[source]\ndownload_url = "https://x.test/a.zip"\n')
    scripted = version_manifest('[source]\nscript = "pkg.local/populate.py"\n')

    assert (bare.source.shipped, bare.source.app_kind) == (True, "shipped")
    assert (downloaded.source.shipped, downloaded.source.app_kind) == (False, "downloaded")
    assert (scripted.source.shipped, scripted.source.app_kind) == (False, "script-populated")
    assert bare.source.shipped_declared is None


def test_shipped_false_without_a_populate_script_is_refused() -> None:
    """Nothing could ever populate such an App, so `shipped = false` alone is invalid."""
    found = problems("format = 2\n[source]\nshipped = false\n")

    assert any("shipped = false needs a download_url or a script" in problem for problem in found)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('download_url = "https://x.test/a.zip"', id="download-url"),
        pytest.param('script = "pkg.local/populate.py"', id="script"),
    ],
)
def test_shipped_true_with_a_recovery_source_is_accepted_and_reported(source: str) -> None:
    """`shipped = true` next to a download or a script is valid and reported as shipped with a source."""
    manifest = version_manifest(f"[source]\nshipped = true\n{source}\n")

    assert manifest.source.shipped is True
    assert manifest.source.recoverable
    assert manifest.source.app_kind == "shipped (source available)"


def test_one_populate_method_per_version() -> None:
    """A download URL and a script together are refused."""
    found = problems(
        'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\nscript = "pkg.local/p.py"\n'
    )

    assert any("one populate method" in problem for problem in found)


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("pkg.local/populate.py", id="python"),
        pytest.param("pkg.local/populate.ps1", id="powershell"),
        pytest.param("pkg.local/populate.cmd", id="cmd"),
        pytest.param("pkg.local/populate.bat", id="bat"),
        pytest.param("pkg.local\\\\bin\\\\populate.exe", id="executable-with-backslashes"),
    ],
)
def test_custom_populate_scripts_of_every_kind_are_accepted(script: str) -> None:
    """A Python, PowerShell, cmd/bat or executable script below `pkg.local` is a valid populate script."""
    manifest = version_manifest(f'[source]\nscript = "{script}"\n')

    assert manifest.source.populate == "script"
    assert manifest.source.script.startswith("pkg.local/")


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("populate.py", id="outside-pkg-local"),
        pytest.param("pkg.local/../evil.py", id="parent-segment"),
        pytest.param("C:/evil/populate.py", id="absolute"),
        pytest.param("pkg.local/populate.txt", id="unknown-suffix"),
    ],
)
def test_populate_script_must_be_a_file_below_pkg_local(script: str) -> None:
    """A script outside `pkg.local`, with `..`, absolute, or of an unknown type is refused."""
    assert problems(f'format = 2\n[source]\nscript = "{script}"\n')


def test_unpack_options_need_a_download_url() -> None:
    """`strip`, `kind` and the other unpack options are refused when nothing is downloaded."""
    found = problems("format = 2\n[source]\nstrip = 1\nkind = \"zip\"\n")

    assert sum("needs download_url" in problem for problem in found) == 2


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('subdir = "../x"', id="subdir-parent"),
        pytest.param('subdir = "C:\\\\x"', id="subdir-absolute"),
        pytest.param('require = ["../x.exe"]', id="require-parent"),
        pytest.param('verify_manifest = "/etc/x"', id="verify-absolute"),
        pytest.param('kind = "inno"', id="no-inno-kind"),
        pytest.param('strip = -1', id="negative-strip"),
        pytest.param('strip = "all"', id="unknown-strip"),
        pytest.param('sha256 = "1234"', id="short-sha256"),
    ],
)
def test_source_values_are_validated(line: str) -> None:
    """Paths that escape App, unknown kinds, bad strip and bad hashes are refused."""
    assert problems(f'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n{line}\n')


def test_git_source_needs_a_ref_or_a_commit() -> None:
    """A `git` source without an anchor is refused, and `[source.git]` needs `kind = "git"`."""
    assert any("needs a ref or a commit" in p for p in problems(
        'format = 2\n[source]\ndownload_url = "https://x.test/r.git"\nkind = "git"\n'))
    assert any("only with kind" in p for p in problems(
        'format = 2\n[source]\ndownload_url = "https://x.test/r.zip"\n[source.git]\nref = "refs/heads/main"\n'))
    manifest = version_manifest(
        '[source]\ndownload_url = "https://x.test/r.git"\nkind = "git"\n[source.git]\ncommit = "%s"\n' % ("a" * 40)
    )
    assert manifest.source.git.commit == "a" * 40


def test_checksum_discovery_needs_exactly_one_method() -> None:
    """`[source.checksum]` names one way to find a hash, with the fields that way needs."""
    base = (
        'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://x.test/"\nmatch = "v(?P<version>[0-9]+)"\n'
        '[source]\ndownload_url = "https://x.test/a.zip"\n[source.checksum]\n'
    )

    assert problems(base, ManifestKind.TRACKER)
    assert problems(base + 'sidecar = "https://x.test/a.sha256"\npage_match = "x"\n', ManifestKind.TRACKER)
    assert problems(base + 'sums_file = "https://x.test/SUMS"\n', ManifestKind.TRACKER)
    assert parse_manifest(base + 'sidecar = "https://x.test/a.sha256"\n', ManifestKind.TRACKER).source.checksum.sidecar


def test_a_version_directory_has_no_checksum_table() -> None:
    """`[source.checksum]` finds the hash of a discovered version, so it belongs to a tracker, not to a pinned version."""
    text = 'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n[source.checksum]\nsidecar = "https://x.test/a.sha256"\n'

    assert any("belongs to a tracker" in problem for problem in problems(text))


# ---------------------------------------------------------------------------
# The three URLs, record_manifest, modes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["check_url", "repo", "repo_url"])
def test_track_urls_outside_track_are_refused(key: str) -> None:
    """`check_url`, `repo` and `repo_url` are valid only inside `[track]`."""
    top = problems(f'format = 2\n{key} = "https://x.test"\n')
    in_source = problems(f'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n{key} = "x"\n')

    assert any(f"unknown key '{key}'" in problem for problem in top)
    assert any(f"unknown key '{key}'" in problem for problem in in_source)


def test_the_three_urls_are_separate_keys() -> None:
    """`project_url`, `download_url` and `check_url` each live in their own place and never overlap."""
    manifest = parse_manifest(
        'format = 2\nproject_url = "https://project.test/"\n'
        '[track]\nprovider = "page"\ncheck_url = "https://check.test/releases"\nmatch = "(?P<version>[0-9.]+)"\n'
        '[source]\ndownload_url = "https://download.test/${version}.zip"\n',
        ManifestKind.TRACKER,
    )

    assert manifest.project_url == "https://project.test/"
    assert manifest.track.check_url == "https://check.test/releases"
    assert manifest.source.download_url == "https://download.test/${version}.zip"


def test_project_url_must_be_a_web_address() -> None:
    """`project_url` is information only, but it must still look like an http(s) URL."""
    assert problems('format = 2\nproject_url = "not a url"\n')


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        pytest.param("false", True, id="false"),
        pytest.param("true", True, id="true"),
        pytest.param('"false"', False, id="string"),
        pytest.param("0", False, id="integer"),
    ],
)
def test_record_manifest_is_a_boolean(value: str, valid: bool) -> None:
    """`record_manifest = false` opts out of the App fingerprint; a non-boolean value is rejected."""
    if valid:
        assert version_manifest(f"record_manifest = {value}\n").record_manifest is (value == "true")
    else:
        assert any("record_manifest" in problem for problem in problems(f"format = 2\nrecord_manifest = {value}\n"))


def test_record_manifest_defaults_to_true() -> None:
    """Without the key the package keeps recording its App fingerprint."""
    assert version_manifest().record_manifest is True


def test_modes_default_and_validation() -> None:
    """`modes` defaults to system and user, and rejects unknown, empty and repeated modes."""
    assert version_manifest().modes == ("system", "user")
    assert version_manifest('modes = ["portable"]\n').modes == ("portable",)
    for bad in ('modes = []', 'modes = ["mars"]', 'modes = ["user", "user"]', 'modes = "user"'):
        assert problems(f"format = 2\n{bad}\n")


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


def test_data_variable_is_valid_only_when_portable_is_a_mode() -> None:
    """`$Data` is accepted when `portable` is in `modes` and rejected otherwise."""
    shortcut = '[[shortcut]]\nname = "S"\ntarget = "$App\\\\s.exe"\narguments = "--data $Data"\n'

    assert version_manifest(shortcut, 'format = 2\nmodes = ["portable"]\n').shortcuts
    assert any("$Data is valid only when 'portable'" in p for p in problems("format = 2\n" + shortcut))


def test_package_root_bin_and_version_root_variables_are_accepted() -> None:
    """`$PackageRoot`, `$Bin` and `$VersionRoot` are valid variables in every mode."""
    manifest = version_manifest(
        '[[env]]\nname = "A"\nvalue = "$PackageRoot"\n[[path]]\nvalue = "$Bin"\n[[path]]\nvalue = "$VersionRoot\\\\x"\n'
    )

    assert manifest.paths == ("$Bin", "$VersionRoot\\x")


def test_unknown_variable_is_rejected_with_a_hint() -> None:
    """A mistyped `$Name` is an error that names the likely variable, and `$$` stays a literal dollar."""
    found = problems('format = 2\n[[path]]\nvalue = "$Aap\\\\bin"\n')

    assert any("unknown variable $Aap" in problem and "did you mean 'App'" in problem for problem in found)
    assert version_manifest('[[env]]\nname = "PRICE"\nvalue = "$$5"\n').envs[0].value == "$$5"


def test_stage_variable_is_valid_only_in_installer_arguments() -> None:
    """`$Stage` belongs to a vendor installer's `run` and is unknown anywhere else."""
    ok = version_manifest(
        '[source]\ndownload_url = "https://x.test/setup.exe"\nkind = "exe"\n[source.installer]\nrun = ["/DIR=$Stage"]\n'
    )

    assert ok.source.installer.run == ("/DIR=$Stage",)
    assert problems('format = 2\n[[path]]\nvalue = "$Stage"\n')


@pytest.mark.parametrize("kind", ["git", "zip", "7z", "nsis", "msi", "tar", "file", "sfx"])
def test_a_vendor_installer_refuses_a_kind_other_than_exe(kind: str) -> None:
    """The installer runs the downloaded .exe itself: any other kind would be silently ignored, so it is an error."""
    extra = '[source.git]\nref = "main"\n' if kind == "git" else ""
    text = f'format = 2\n[source]\ndownload_url = "https://x.test/setup.exe"\nkind = "{kind}"\n[source.installer]\nrun = ["/DIR=$Stage"]\n{extra}'

    assert any("kind must be" in item and kind in item for item in problems(text))


@pytest.mark.parametrize("option", ['strip = 1', 'subdir = "x"', 'verify_manifest = "M.sha256"'])
def test_a_vendor_installer_refuses_unpack_options(option: str) -> None:
    """strip, subdir, verify_manifest (and nested, extract) belong to an unpack that does not happen."""
    text = f'format = 2\n[source]\ndownload_url = "https://x.test/setup.exe"\nkind = "exe"\n{option}\n[source.installer]\nrun = ["/DIR=$Stage"]\n'

    assert any("unpack option" in item for item in problems(text))


def test_a_vendor_installer_with_kind_exe_or_none_is_valid() -> None:
    """The two accepted spellings."""
    for kind_line in ('kind = "exe"\n', ""):
        text = f'format = 2\n[source]\ndownload_url = "https://x.test/setup.exe"\n{kind_line}[source.installer]\nrun = ["/DIR=$Stage"]\n'
        assert parse_manifest(text).source.installer is not None


# ---------------------------------------------------------------------------
# Templates, tracker, and update discovery
# ---------------------------------------------------------------------------


def test_track_table_is_refused_in_a_version_and_the_error_names_check_updates() -> None:
    """A version directory with `[track]` is invalid, and the error points at `_check_updates`."""
    found = problems('format = 2\n[track]\nprovider = "json"\ncheck_url = "https://x.test/v.json"\n')

    assert any("_check_updates" in problem and "[track]" in problem for problem in found)


def test_tracker_needs_track() -> None:
    """A manifest read as a tracker without `[track]` is invalid."""
    assert any("needs a [track] table" in p for p in problems("format = 2\n", ManifestKind.TRACKER))


@pytest.mark.parametrize(
    "line",
    [
        pytest.param('download_url = "https://x.test/${version}/a.zip"', id="download-url"),
        pytest.param('download_url = "https://x.test/${tag}/a.zip"', id="named-capture"),
        pytest.param('download_url = "https://x.test/a.zip"\n[source.checksum]\nsums_file = "https://x.test/${version}/SUMS"\nentry = "a.zip"', id="checksum-url"),
        pytest.param('download_url = "https://x.test/a.zip"\n[[source.nested]]\npath = "x-${version}.zip"\ndest = ""', id="nested"),
    ],
)
def test_templates_in_a_version_directory_source_are_refused_naming_check_updates(line: str) -> None:
    """Source, nested and checksum URLs of a version cannot carry templates; the error names `_check_updates`."""
    found = problems(f"format = 2\n[source]\n{line}\n")

    assert any("_check_updates" in problem and "template" in problem for problem in found)


def test_version_templates_are_refused_outside_source_too_but_environment_references_are_not() -> None:
    """`${version}` and filtered references are templates everywhere; a plain `${USERPROFILE}` is an environment reference."""
    assert problems('format = 2\n[[env]]\nname = "V"\nvalue = "${version}"\n')
    assert problems('format = 2\n[[env]]\nname = "V"\nvalue = "${USERPROFILE|nodots}"\n')
    assert version_manifest('[[env]]\nname = "H"\nvalue = "${USERPROFILE}\\\\x"\n').envs[0].value == "${USERPROFILE}\\x"


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("${version|upper}", id="unknown-filter"),
        pytest.param("${version|replace:.}", id="replace-missing-argument"),
        pytest.param("${version|major:1}", id="major-with-argument"),
        pytest.param("${}", id="empty-reference"),
        pytest.param("${1bad}", id="bad-name"),
    ],
)
def test_malformed_templates_are_rejected_in_a_tracker(template: str) -> None:
    """A reference with an unknown or malformed filter, or a bad name, is an error even in a tracker."""
    text = (
        'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://x.test/"\nmatch = "x"\n'
        f'[source]\ndownload_url = "https://x.test/{template}.zip"\n'
    )

    assert problems(text, ManifestKind.TRACKER)


def test_track_providers_and_their_location_keys_are_checked() -> None:
    """`[track]` needs a known provider and the location key that provider takes; other keys are refused."""
    def track(body: str) -> str:
        return f"format = 2\n[track]\n{body}\n"

    assert parse_manifest(track('provider = "github"\nrepo = "cli/cli"\nasset = "gh.zip"'), ManifestKind.TRACKER)
    assert parse_manifest(track('provider = "git"\nrepo_url = "https://x.test/r.git"\nref = "refs/heads/main"'), ManifestKind.TRACKER)
    assert parse_manifest(track('provider = "module"'), ManifestKind.TRACKER).track.options["module"] == "pkg.local/track.py"
    assert problems(track('provider = "ftp"'), ManifestKind.TRACKER)
    assert problems(track('provider = "github"'), ManifestKind.TRACKER)
    assert problems(track('provider = "github"\ncheck_url = "https://x.test/"'), ManifestKind.TRACKER)
    assert problems(track('provider = "json"\ncheck_url = "https://x.test/v.json"\nbogus = 1'), ManifestKind.TRACKER)


def test_tracker_cannot_carry_a_checksum_of_its_own() -> None:
    """`sha256` in a tracker is refused: only a discovered checksum is ever written, by `render_pinned`."""
    text = (
        f'format = 2\n[track]\nprovider = "module"\n[source]\ndownload_url = "https://x.test/${{version}}.zip"\nsha256 = "{SHA}"\n'
    )

    assert any("sha256" in problem for problem in problems(text, ManifestKind.TRACKER))


# ---------------------------------------------------------------------------
# render_pinned
# ---------------------------------------------------------------------------


def test_render_pinned_gives_a_concrete_url_and_no_track() -> None:
    """The firefox tracker and a fixed result give a version with a concrete URL, no `[track]` and no templates."""
    tracker = parse_manifest(FIREFOX_TRACKER, ManifestKind.TRACKER)

    pinned = render_pinned(tracker, TrackResult("128.0.3"))

    assert pinned.kind is ManifestKind.VERSION
    assert pinned.track is None
    assert pinned.source.download_url == (
        "https://archive.mozilla.org/pub/firefox/releases/128.0.3/win64/en-US/Firefox%20Setup%20128.0.3.exe"
    )
    assert pinned.source.checksum is None and "checksum" not in manifest_to_toml(pinned)  # tracker-only: dropped (D10)
    assert "[track]" not in manifest_to_toml(pinned) and "${" not in manifest_to_toml(pinned)
    # The output is an ordinary version manifest.
    assert parse_manifest(manifest_to_toml(pinned)) == pinned


def test_render_pinned_has_a_sha256_only_when_the_result_carried_one() -> None:
    """`sha256` and `sha256_from` come only from a discovered checksum, never from anywhere else."""
    tracker = parse_manifest(FIREFOX_TRACKER, ManifestKind.TRACKER)

    without = render_pinned(tracker, TrackResult("128.0.3"))
    with_hash = render_pinned(tracker, TrackResult("128.0.3", checksum=DiscoveredChecksum(SHA, "vendor-sums")))

    assert (without.source.sha256, without.source.sha256_from) == (None, None)
    assert (with_hash.source.sha256, with_hash.source.sha256_from) == (SHA, "vendor-sums")


def test_render_pinned_does_not_write_the_default_shipped_false() -> None:
    """A downloaded version does not carry `shipped = false` (the default of a source that can populate App)."""
    pinned = render_pinned(parse_manifest(FIREFOX_TRACKER, ManifestKind.TRACKER), TrackResult("1.0"))

    assert pinned.source.shipped is False and pinned.source.shipped_declared is None
    assert "shipped" not in manifest_to_toml(pinned)


def test_render_pinned_expands_filters_and_captures() -> None:
    """`replace`, `major`, `minor`, `patch`, `nodots` and named captures all expand."""
    tracker = parse_manifest(
        'format = 2\n[track]\nprovider = "module"\n'
        '[source]\ndownload_url = "https://x.test/${tag}/v${version|major}/${version|minor}/${version|patch}/'
        '${version|nodots}/${version|replace:.:-}/${version|replace:.:-|nodots}.zip"\n'
        '[[shim]]\nname = "x"\ntarget = "$App\\\\x-${version}.exe"\n',
        ManifestKind.TRACKER,
    )

    pinned = render_pinned(tracker, TrackResult("14.1.7", {"tag": "rel-9"}))

    assert pinned.source.download_url == "https://x.test/rel-9/v14/1/7/1417/14-1-7/14-1-7.zip"
    assert pinned.shims[0].target == "$App\\x-14.1.7.exe"


def test_render_pinned_refuses_unusable_input() -> None:
    """A version manifest, a version that cannot name a folder, a leftover template and a non-discovered checksum are refused."""
    tracker = parse_manifest(FIREFOX_TRACKER, ManifestKind.TRACKER)
    leftover = parse_manifest(
        'format = 2\n[track]\nprovider = "module"\n[source]\ndownload_url = "https://x.test/${missing}.zip"\n',
        ManifestKind.TRACKER,
    )

    with pytest.raises(ManifestError):
        render_pinned(parse_manifest(PINNED), TrackResult("1.0"))
    with pytest.raises(ManifestError):
        render_pinned(tracker, TrackResult("1:0"))
    with pytest.raises(ManifestError, match="template"):
        render_pinned(leftover, TrackResult("1.0"))
    with pytest.raises(ManifestError):
        render_pinned(tracker, TrackResult("1.0", checksum=DiscoveredChecksum(SHA, "manual")))


def test_render_pinned_keeps_only_hooks_that_source_and_prepare_reference() -> None:
    """The pinned manifest lists the source script and prepare modules, never the tracking module."""
    tracker = parse_manifest(
        'format = 2\n[track]\nprovider = "module"\nmodule = "pkg.local/check.py"\n'
        '[source]\nscript = "pkg.local/populate.py"\n[[prepare]]\nmodule = "pkg.local/fix.py"\n',
        ManifestKind.TRACKER,
    )

    pinned = render_pinned(tracker, TrackResult("2.0"))

    assert pinned.referenced_hooks() == ("pkg.local/populate.py", "pkg.local/fix.py")
    assert "check.py" not in manifest_to_toml(pinned)


@pytest.mark.parametrize("name", ["Path", "PATH", "pAtH", "PATHEXT", "ComSpec", "SystemRoot", "windir", "TEMP", "tmp", "USERPROFILE",
                                  "LocalAppData", "ProgramFiles(x86)", "PSModulePath", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
                                  "UserName", "PUBLIC"])
def test_an_env_row_may_not_name_a_reserved_system_variable(name: str) -> None:
    """`[[env]]` named Path (any case) would replace the whole PATH; the system's own variables are refused the same way."""
    text = f'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n\n[[env]]\nname = "{name}"\nvalue = "x"\n'

    with pytest.raises(ManifestError, match="reserved system variable"):
        parse_manifest(text)


@pytest.mark.parametrize("name", ["NUL", "con", "aux.exe", "COM1", "lpt9", "tool:ads", "tool.", "tool ", "nul .txt", "con  "])
def test_a_shim_may_not_be_named_like_a_device_or_a_stream(name: str) -> None:
    """Device names (with any extension), NTFS streams and names Windows would rewrite are refused for a shim."""
    text = f'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n\n[[shim]]\nname = "{name}"\ntarget = "$App\\\\a.exe"\n'

    with pytest.raises(ManifestError, match="shim"):
        parse_manifest(text)


def test_a_portable_env_table_may_not_name_a_reserved_variable() -> None:
    """The same rule holds for the `[portable] env` table."""
    text = ('format = 2\nmodes = ["portable"]\n[source]\ndownload_url = "https://x.test/a.zip"\n\n[portable]\n'
            'env = { Path = "x" }\n')

    with pytest.raises(ManifestError, match="not a usable variable name"):
        parse_manifest(text)


@pytest.mark.parametrize("name", ["A" * 129, "A%B", "A\u200bB", "A\u0007B", "A B", "A=B", "A\u2060B", "A\ufeffB"])
def test_an_env_name_is_capped_and_refuses_percent_and_invisible_characters(name: str) -> None:
    """128 characters at most, no `%` (it would be expanded), no zero-width or control character, no space or `=`."""
    shown = name.replace("\x07", "\\u0007")
    text = f'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n\n[[env]]\nname = "{shown}"\nvalue = "x"\n'

    with pytest.raises(ManifestError, match="env\\[0\\].name"):
        parse_manifest(text)


def test_an_env_name_of_128_characters_is_allowed() -> None:
    """The cap is inclusive."""
    name = "A" * 128
    text = f'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n\n[[env]]\nname = "{name}"\nvalue = "x"\n'

    assert parse_manifest(text).envs[0].name == name


@pytest.mark.parametrize("table", ["shim", "shortcut"])
def test_shim_and_shortcut_names_are_capped_at_255(table: str) -> None:
    """255 characters is Windows' limit for one name; 256 is refused, 255 accepted."""
    target = 'target = "C:\\\\a.exe"\n'
    head = 'format = 2\n[source]\ndownload_url = "https://x.test/a.zip"\n\n'
    ok = parse_manifest(head + f'[[{table}]]\nname = "{"a" * 255}"\n{target}')
    assert getattr(ok, "shims" if table == "shim" else "shortcuts")[0].name == "a" * 255

    with pytest.raises(ManifestError, match=f"{table}\\[0\\].name"):
        parse_manifest(head + f'[[{table}]]\nname = "{"a" * 256}"\n{target}')
