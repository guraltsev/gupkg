"""Doubles and builders for the tests of the registry tool (``gupkg_registry``).

``registry_of`` builds a registry holding small invented trackers (``gadget``, ``widget``, ``stamped``, ``editor``: a page, a
GitHub, a ``Last-Modified`` and an index provider), ``machine`` a ``FakeSystem`` that serves the vendor answers of those
packages and their artifacts, ``valid_registry`` a registry with one tracker and one pinned version that passes every rule,
``Git`` is a ``git`` double and ``run_cli`` runs the tool's command line. Nothing here is copied from a real registry.

Usage and API
-------------
Call ``registry_of`` or ``valid_registry`` for a registry folder, ``machine`` for the pretend machine that serves the
vendors, and ``run_cli`` to run the tool against them.

Implementation Approach
-----------------------
Trackers and vendor answers are small invented documents kept in two tables keyed by package name, so a test chooses
its world by naming packages, and the tool runs on real folders with a fake network.
"""

from __future__ import annotations

from pathlib import Path

from gupkg.launchers import write_launchers
from gupkg.services.fingerprint import fingerprint_directory
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from gupkg_registry.cli import main
from gupkg_registry.layout import TRACKER_LAUNCHER_NAME, write_registry_file
from tests.support.archive import make_zip
from tests.support.track import DIGEST, json_response

# The trackers of the invented packages, by package name.
TRACKERS = {
    # A page provider with its own User-Agent, and a download that needs a Referer.
    "gadget": (
        'format = 2\ndescription = "A gadget."\nmodes = ["system", "user"]\n\n'
        '[track]\nprovider = "page"\ncheck_url = "https://vendor.example/download/"\n'
        "match = 'href=\"(?P<href>https://vendor\\.example/files/gadget_(?P<major>\\d+?)(?P<minor>\\d{2})\\.zip)\"'\n"
        'version = "${major}.${minor}"\ndownload = "${href}"\n\n'
        '[track.headers]\nUser-Agent = "Vendor-Agent/1"\n\n'
        '[source]\ndownload_url = "${download_url}"\nrequire = ["Gadget64.exe"]\n\n'
        '[source.headers]\nReferer = "https://vendor.example/download/"\n\n'
        '[[shortcut]]\nname = "Gadget"\ntarget = "$App\\\\Gadget64.exe"\n'
    ),
    # A GitHub provider whose release carries a digest for the asset.
    "widget": (
        'format = 2\ndescription = "A widget."\nmodes = ["system", "user"]\n\n'
        '[track]\nprovider = "github"\nrepo = "o/widget"\nasset = "widget-${version}-windows.zip"\n\n'
        '[source]\ndownload_url = "${asset_url}"\nrequire = ["widget.exe", "widget-shim.exe"]\n\n'
        '[[prepare]]\ncopy = { from = "widget-shim.exe", to = ["node.exe", "npm.exe"] }\n\n'
        '[[path]]\nvalue = "$App"\n\n[[shim]]\nname = "widget"\ntarget = "$App\\\\widget.exe"\n'
    ),
    # A file republished under one name: the date of its last change is the version.
    "stamped": (
        'format = 2\ndescription = "A stamped tool."\nmodes = ["system", "user"]\n\n'
        '[track]\nprovider = "last_modified"\ncheck_url = "https://download.example/files/Stamped.zip"\nformat = "%Y%m%d"\n\n'
        '[source]\ndownload_url = "https://download.example/files/Stamped.zip"\n\n'
        '[[shortcut]]\nname = "Tools\\\\Stamped"\ntarget = "$App\\\\Stamped64.exe"\n'
    ),
    # An index two levels deep, with a sums file that is optional.
    "editor": (
        'format = 2\ndescription = "An editor."\nmodes = ["system", "user"]\n\n'
        '[track]\nprovider = "index"\ncheck_url = "https://ftp.example/editor/"\n'
        'download = "https://ftp.example/editor/editor-${major}/editor-${version}-win.zip"\n\n'
        "[[track.level]]\nmatch = 'href=\"editor-(?P<major>\\d+)/\"'\nnext = \"editor-${major}/\"\n\n"
        "[[track.level]]\nmatch = 'editor-(?P<version>\\d+(?:\\.\\d+)*)-win\\.zip'\n\n"
        '[source]\ndownload_url = "${download_url}"\nrequire = ["bin\\\\editor.exe", "bin\\\\editorw.exe"]\n\n'
        '[source.checksum]\nsums_file = "https://ftp.example/editor/editor-${major}/editor-${version}-sha256sums.txt"\n'
        'entry = "editor-${version}-win.zip"\noptional = true\n\n'
        '[[shim]]\nname = "editor"\ntarget = "$App\\\\bin\\\\editor.exe"\n'
    ),
}

# Address of the checksum list that the ``index_editor`` case serves.
SUMS = "https://ftp.example/editor/editor-30/editor-30.2-sha256sums.txt"
# What the vendors of those packages answer, by case name: {address: answer}.
ANSWERS: dict[str, dict[str, FakeHttp]] = {
    "page_gadget": {"https://vendor.example/download/": FakeHttp(
        '<a href="https://vendor.example/files/gadget_854.zip">Local</a> <a href="https://vendor.example/files/gadget_1002.zip">Local</a>')},
    "github_widget": {"https://api.github.com/repos/o/widget/releases/latest": json_response({
        "tag_name": "v2.0.1", "draft": False, "prerelease": False, "html_url": "https://github.com/o/widget/releases/tag/v2.0.1",
        "assets": [{
            "name": "widget-2.0.1-windows.zip", "state": "uploaded", "digest": f"sha256:{DIGEST}",
            "browser_download_url": "https://github.com/o/widget/releases/download/v2.0.1/widget-2.0.1-windows.zip",
        }],
    })},
    "last_modified_stamped": {"https://download.example/files/Stamped.zip": FakeHttp(
        "", 200, {"Last-Modified": "Wed, 11 Sep 2024 17:31:09 GMT", "Content-Type": "application/x-zip-compressed"})},
    "index_editor": {
        "https://ftp.example/editor/": FakeHttp('<a href="../">../</a> <a href="editor-29/">editor-29/</a> <a href="editor-30/">editor-30/</a>'),
        "https://ftp.example/editor/editor-30/": FakeHttp(
            '<a href="editor-30.1-win.zip">editor-30.1-win.zip</a> <a href="editor-30.2-win.zip">editor-30.2-win.zip</a>'),
        SUMS: FakeHttp(f"{'a' * 64}  editor-30.2-other.zip\n{'5' * 64}  editor-30.2-win.zip\n"),
    },
}


class Git:
    """A ``git`` double that records every command and can fail one subcommand.

    Parameters
    ----------
    fail : str | None, optional
        Subcommand (``add``, ``commit`` or ``push``) that exits non-zero; none fails when ``None``.
    """

    def __init__(self, fail: str | None = None) -> None:
        """Remember which subcommand (``add``, ``commit`` or ``push``) should exit non-zero."""
        self.fail = fail
        self.calls: list[ProcessCall] = []

    def __call__(self, call: ProcessCall) -> ProcessResult:
        """Record the call and answer with success, or with status 1 for the failing subcommand."""
        self.calls.append(call)
        return ProcessResult(1, "", "refused") if call.argv[1] == self.fail else ProcessResult(0)


def registry_of(tmp_path: Path, *packages: str, extra: dict[str, str] | None = None) -> Path:
    """Build a registry in ``tmp_path/registry`` holding the trackers of *packages* (names of ``TRACKERS``) and a fresh ``_registry.toml``.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder that receives ``registry``.
    *packages : str
        Names from ``TRACKERS`` whose trackers are written.
    extra : dict[str, str] | None, optional
        Additional files written as given, as ``{path: text}``.

    Returns
    -------
    Path
        The registry folder.
    """
    # Start from an empty registry folder and write each requested tracker with its launcher.
    root = registry_without_file(tmp_path)
    for package in packages:
        tracker = root / package / "_check_updates"
        tracker.mkdir(parents=True)
        (tracker / "pkg.toml").write_text(TRACKERS[package], encoding="utf-8", newline="\n")
        write_launchers(tracker, (TRACKER_LAUNCHER_NAME,))
    # Add the extra files verbatim, then the registry file that fingerprints nothing.
    for name, text in (extra or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    write_registry_file(root, {"format": 2, "name": "test-registry", "description": "A registry for tests."})
    return root


def registry_without_file(tmp_path: Path) -> Path:
    """Create and return the empty folder ``tmp_path/registry``.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder that receives ``registry``.

    Returns
    -------
    Path
        The empty registry folder.
    """
    root = tmp_path / "registry"
    root.mkdir()
    return root


def machine(tmp_path: Path, *cases: str, artifacts: dict[str, bytes] | None = None, git: Git | None = None) -> FakeSystem:
    """A FakeSystem serving the vendor answers of *cases* (names of ``ANSWERS``) and the *artifacts* ``{url: bytes}``, with *git* as the only ``git``.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder that holds the machine.
    *cases : str
        Names from ``ANSWERS`` whose vendor answers are served.
    artifacts : dict[str, bytes] | None, optional
        Additional ``{url: bytes}`` downloads to serve.
    git : Git | None, optional
        Scripted git that is the only ``git`` process.

    Returns
    -------
    FakeSystem
        The fake machine.
    """
    system = FakeSystem(tmp_path / "machine", processes={"git": git} if git else {})
    for case in cases:
        system.urls.update(ANSWERS[case])
    for url, body in (artifacts or {}).items():
        system.urls[url] = FakeHttp(body)
    return system


def zip_bytes(tmp_path: Path, name: str, members: dict[str, str]) -> bytes:
    """Return the bytes of a zip archive of *members*.

    Parameters
    ----------
    tmp_path : Path
        Temporary folder where the archive is staged.
    name : str
        File name of the archive.
    members : dict[str, str]
        Archive members as ``{name: text}``.

    Returns
    -------
    bytes
        Bytes of the archive.
    """
    return make_zip(tmp_path / name, members).read_bytes()


def run_cli(root: Path, system: FakeSystem, tmp_path: Path, *arguments: str) -> int:
    """Run the command line against *root* with the given system and an empty environment.

    Parameters
    ----------
    root : Path
        Registry folder to check.
    system : FakeSystem
        Fake machine to run against.
    tmp_path : Path
        Temporary folder that holds the sandbox.
    *arguments : str
        Command words before the registry and sandbox options.

    Returns
    -------
    int
        The exit code of the command.
    """
    return main([*arguments, "--registry", str(root), "--sandbox", str(tmp_path / "sandbox")], system=system, environ={})


# A tracker that passes the package checker with no warning, and a pinned version that validates.
VALID_TRACKER = (
    'format = 2\ndescription = "A command-line tool."\nmodes = ["system", "user"]\n\n'
    '[track]\nprovider = "github"\nrepo = "owner/tool"\nasset = "tool_${version}_windows_amd64.zip"\n\n'
    '[source]\ndownload_url = "${asset_url}"\nrequire = ["bin/tool.exe"]\n\n'
    '[[path]]\nvalue = "$App\\\\bin"\n\n'
    '[[shim]]\nname = "tool"\ntarget = "$App\\\\bin\\\\tool.exe"\n'
).encode()
# A pinned version that validates: its digest comes from the registry's fingerprints.
VALID_VERSION = (
    'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n\n[source]\n'
    'download_url = "https://vendor.example/files/gizmo_1002.zip"\n'
    f'sha256 = "{"c" * 64}"\nsha256_from = "registry"\nshipped = false\n'
).encode()


def valid_registry(tmp_path: Path) -> Path:
    """A registry of two packages that passes every rule: ``gh`` (a tracker) and ``gizmo`` (the fully pinned version ``10.02``).

    Parameters
    ----------
    tmp_path : Path
        Temporary folder that receives ``registry``.

    Returns
    -------
    Path
        The registry folder.
    """
    # Write the tracker and the pinned version, each with its launchers.
    root = registry_without_file(tmp_path)
    tracker = root / "gh" / "_check_updates"
    tracker.mkdir(parents=True)
    (tracker / "pkg.toml").write_bytes(VALID_TRACKER)
    write_launchers(tracker, (TRACKER_LAUNCHER_NAME,))
    version = root / "gizmo" / "10.02"
    version.mkdir(parents=True)
    (version / "pkg.toml").write_bytes(VALID_VERSION)
    write_launchers(version)
    # Publish the version's fingerprint so the registry file agrees with the files on disk.
    write_registry_file(root, {
        "format": 2, "name": "test-registry", "description": "A registry for tests.",
        "fingerprints": {"gizmo/10.02": fingerprint_directory(version)},
    })
    return root
