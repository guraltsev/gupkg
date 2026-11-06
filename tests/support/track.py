"""Small trackers with the vendor responses they read, written for the tests of the track providers.

``VENDORS`` holds one tracker per provider kind together with the answers a vendor would give, and the version and
download address the tracker must find in them. ``environment`` builds the ``TrackEnvironment`` a provider runs in, with a
client that never sleeps and ignores the process environment. Everything here is invented for the tests: no package of any
real registry is copied.

Usage and API
-------------
Index ``VENDORS`` by provider kind, call ``Vendor.serve`` to make a ``FakeSystem`` answer for it, and
``environment`` to build the environment a provider runs in; ``parse_tracker`` parses a tracker's text.

Implementation Approach
-----------------------
Each vendor carries its tracker text with markers for the optional header table, its invented answers, and the version
and address the provider must find, so one table drives every provider test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from gupkg.model.manifest import Manifest, ManifestKind, parse_manifest
from gupkg.services.http import HttpClient
from gupkg.services.track import TrackEnvironment
from gupkg.system import regex_worker
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.support.scope import scope_of

# The User-Agent that a tracker with headers sends in place of gupkg's own.
VENDOR_AGENT = "Vendor-Agent/1"
# Digest that the GitHub and JSON vendors publish for their artifact.
DIGEST = "9ce45e2deeb806d62a504075f992a4c5de66873173c2e403a49a3de9126fa5d3"


def environment(system: FakeSystem, tmp_path: Path, *, directory: Path | None = None, package: str = "pkg") -> TrackEnvironment:
    """Return a ``TrackEnvironment`` over *system* with a client that never sleeps and ignores the process environment.

    Parameters
    ----------
    system : FakeSystem
        Fake machine that serves the vendor's addresses.
    tmp_path : Path
        Temporary folder for the cache and the work folder.
    directory : Path | None, optional
        Package version folder the tracker runs for.
    package : str, optional
        Package name.

    Returns
    -------
    TrackEnvironment
        The tracker environment.
    """
    client = HttpClient(system, env={}, sleep=lambda seconds: None, cache_dir=tmp_path / "cache")
    return TrackEnvironment(system, scope_of(system), client, work_dir=tmp_path / "work", directory=directory, package=package)


def real_regex_worker(call: ProcessCall) -> ProcessResult:
    """Run the real regex worker for *call* (for a double that changes only some of its answers, such as a deadline).

    Parameters
    ----------
    call : ProcessCall
        The process call whose input is passed to the worker.

    Returns
    -------
    ProcessResult
        The worker's exit code and output.
    """
    done = subprocess.run(
        [sys.executable, "-I", "-S", "-s", "-B", str(Path(regex_worker.__file__))],
        input=call.input_text, capture_output=True, text=True, check=False,
    )
    return ProcessResult(done.returncode, done.stdout, done.stderr)


def parse_tracker(text: str) -> Manifest:
    """Parse the text of a tracker ``pkg.toml``.

    Parameters
    ----------
    text : str
        Text of a tracker ``pkg.toml``.

    Returns
    -------
    Manifest
        The parsed manifest.
    """
    return parse_manifest(text, ManifestKind.TRACKER)


def json_response(document: object) -> FakeHttp:
    """Return the answer of a server that sends *document* as JSON.

    Parameters
    ----------
    document : object
        Value sent as the JSON body.

    Returns
    -------
    FakeHttp
        A fake HTTP answer with status 200.
    """
    return FakeHttp(json.dumps(document), 200, {"Content-Type": "application/json"})


@dataclass(frozen=True)
class Vendor:
    """One provider kind: a tracker, the responses it reads, and what it must find.

    ``tracker`` holds the marker ``@HEADERS@`` where a tracker puts its ``[track.headers]`` table, and ``@STEP@`` after
    the keys of each fetch step of a chain; ``tracker_text(headers=True)`` fills them in. ``uses_patterns`` tells whether
    the provider matches a regular expression, which runs in the regex worker process.

    Methods
    -------
    tracker_text
        Returns the tracker with its markers filled in.
    serve
        Registers the vendor's addresses on a fake machine.

    Attributes
    ----------
    tracker : str
        Tracker text with the ``@HEADERS@`` and ``@STEP@`` markers.
    urls : dict[str, FakeHttp]
        Responses served by the vendor, keyed by address.
    version : str
        Version the tracker must find.
    download_url : str
        Download address the tracker must find.
    uses_patterns : bool
        Whether the provider matches a regular expression in the regex worker.
    sha256 : str | None
        Digest the tracker must find, if any.
    sha256_from : str | None
        Where the digest comes from, if any.
    sends_track_headers : bool
        Whether the provider sends the ``[track.headers]`` table.
    """

    tracker: str
    urls: dict[str, FakeHttp]
    version: str
    download_url: str
    uses_patterns: bool = True
    sha256: str | None = None
    sha256_from: str | None = None
    sends_track_headers: bool = True

    def tracker_text(self, *, headers: bool = False) -> str:
        """Return the tracker, with a header table (a User-Agent of the vendor) when *headers* is true.

        Parameters
        ----------
        headers : bool, optional
            Whether to fill in the header tables with the vendor's User-Agent.

        Returns
        -------
        str
            Text of the tracker.
        """
        table = f'[track.headers]\nUser-Agent = "{VENDOR_AGENT}"\n' if headers else ""
        step = f'headers = {{ User-Agent = "{VENDOR_AGENT}" }}\n' if headers else ""
        return self.tracker.replace("@HEADERS@", table).replace("@STEP@", step)

    def serve(self, system: FakeSystem) -> FakeSystem:
        """Make *system* answer the vendor's addresses; return it.

        Parameters
        ----------
        system : FakeSystem
            Fake machine that should answer the vendor's addresses.

        Returns
        -------
        FakeSystem
            *system*, for chaining.
        """
        system.urls.update(self.urls)
        return system


# Release page of the invented GitHub project.
GITHUB = "https://github.com/vendor/tool/releases"


def _asset(name: str, tag: str, *, digest: str | None = None) -> dict[str, object]:
    """Return a GitHub release asset named *name* of release *tag*."""
    asset: dict[str, object] = {"name": name, "state": "uploaded", "browser_download_url": f"{GITHUB}/download/{tag}/{name}"}
    if digest is not None:
        asset["digest"] = f"sha256:{digest}"
    return asset


def _release(tag: str, *names: str, prerelease: bool = False, draft: bool = False, digest: str | None = None) -> dict[str, object]:
    """Return a GitHub release of *tag* holding the assets *names*."""
    return {
        "tag_name": tag, "draft": draft, "prerelease": prerelease, "html_url": f"{GITHUB}/tag/{tag}",
        "assets": [_asset(name, tag, digest=digest) for name in names],
    }


def github_releases(*releases: dict[str, object]) -> dict[str, FakeHttp]:
    """Return the answer of GitHub's list of releases of ``vendor/tool``.

    Returns
    -------
    dict[str, FakeHttp]
        Map of the releases address of ``vendor/tool`` to its answer.
    """
    return {"https://api.github.com/repos/vendor/tool/releases?per_page=100": json_response(list(releases))}


# Address of the listing page used by the index vendor.
LISTING = "https://vendor.example/releases/"
# Address of the artifact that the page vendor points at.
DOWNLOAD = "https://vendor.example/files/tool_1002.zip"

# One tracker per provider kind, with the vendor answers it reads and what it must find.
VENDORS: dict[str, Vendor] = {
    "github-latest": Vendor(
        'format = 2\n[track]\nprovider = "github"\nrepo = "vendor/tool"\nasset = "tool-${version}-windows.zip"\n'
        '[source]\ndownload_url = "${asset_url}"\n',
        {"https://api.github.com/repos/vendor/tool/releases/latest": json_response(
            _release("v2.0.1", "tool-2.0.1-windows.zip", "tool-2.0.1-linux.tar.gz", digest=DIGEST))},
        "2.0.1", f"{GITHUB}/download/v2.0.1/tool-2.0.1-windows.zip", False, DIGEST, "github-digest", False,
    ),
    "github-newest-matching": Vendor(
        'format = 2\n[track]\nprovider = "github"\nrepo = "vendor/tool"\nrelease = "newest-matching"\n'
        "tag_pattern = 'release/v(?P<version>\\d+(?:\\.\\d+)+)'\nasset_pattern = 'tool-${version}-x64\\.zip'\n"
        '[source]\ndownload_url = "${asset_url}"\n',
        github_releases(
            _release("release/v1.9.0", "tool-1.9.0-x64.zip"),
            _release("release/v1.10.0", "tool-1.10.0-x64.zip", "tool-1.10.0-arm.zip"),
        ),
        "1.10.0", f"{GITHUB}/download/release/v1.10.0/tool-1.10.0-x64.zip", sends_track_headers=False,
    ),
    "github-version-from-asset": Vendor(
        'format = 2\n[track]\nprovider = "github"\nrepo = "vendor/tool"\nrelease = "newest-matching"\n'
        "version_from = \"asset\"\nasset_pattern = 'tool-(?P<version>\\d+(?:\\.\\d+)+)-win\\.zip'\n"
        '[source]\ndownload_url = "${asset_url}"\n',
        github_releases(_release("build-1", "tool-3.1-win.zip"), _release("build-2", "tool-3.2-win.zip")),
        "3.2", f"{GITHUB}/download/build-2/tool-3.2-win.zip", sends_track_headers=False,
    ),
    "gitlab": Vendor(
        'format = 2\n[track]\nprovider = "gitlab"\nrepo = "https://gitlab.example/vendor/tool"\nlink_name = "tool-${version}.zip"\n'
        '[source]\ndownload_url = "${asset_url}"\n',
        {"https://gitlab.example/api/v4/projects/vendor%2Ftool/releases?per_page=100": json_response([
            {"tag_name": f"v{number}", "assets": {"links": [{
                "name": f"tool-{number}.zip",
                "direct_asset_url": f"https://gitlab.example/vendor/tool/-/releases/v{number}/downloads/tool-{number}.zip",
            }]}}
            for number in ("1.3.0", "1.4.0")
        ])},
        "1.4.0", "https://gitlab.example/vendor/tool/-/releases/v1.4.0/downloads/tool-1.4.0.zip", False, sends_track_headers=False,
    ),
    "page": Vendor(
        'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://vendor.example/download"\n'
        "match = 'href=\"(?P<href>/files/tool_(?P<major>\\d+?)(?P<minor>\\d{2})\\.zip)\"'\n"
        'version = "${major}.${minor}"\ndownload = "${href}"\n@HEADERS@[source]\ndownload_url = "${download_url}"\n',
        {"https://vendor.example/download": FakeHttp(
            '<a href="/files/tool_854.zip">old</a> <a href="/files/tool_1002.zip">new</a> <a href="/files/tool_842.zip">older</a>')},
        "10.02", DOWNLOAD,
    ),
    "index": Vendor(
        'format = 2\n[track]\nprovider = "index"\ncheck_url = "https://vendor.example/releases/"\n'
        'download = "https://vendor.example/releases/${version}/${file}"\n@HEADERS@'
        "[[track.level]]\nmatch = '/releases/(?P<version>\\d+(?:\\.\\d+)+)/'\nselect = \"max\"\nnext = \"${version}/\"\n"
        "[[track.level]]\nmatch = '(?P<file>tool-${version}-setup\\.exe)'\n"
        '[source]\ndownload_url = "${download_url}"\n',
        {
            LISTING: FakeHttp('<a href="/releases/1.9/">1.9</a> <a href="/releases/1.10/">1.10</a> <a href="/releases/1.2/">1.2</a>'),
            f"{LISTING}1.10/": FakeHttp('<a href="tool-1.10-setup.exe">setup</a>'),
        },
        "1.10", f"{LISTING}1.10/tool-1.10-setup.exe",
    ),
    "redirect": Vendor(
        'format = 2\n[track]\nprovider = "redirect"\ncheck_url = "https://vendor.example/latest"\nunwrap_query = "url"\n'
        "match = '/files/(?P<version>\\d+(?:\\.\\d+)+)/'\n@HEADERS@[source]\ndownload_url = \"${download_url}\"\n",
        {
            "https://vendor.example/latest": FakeHttp("", 302, {"Location": "https://vendor.example/get?product=tool"}),
            "https://vendor.example/get?product=tool": FakeHttp(
                "", 302, {"Location": "https://vendor.example/dl?url=https%3A%2F%2Fcdn.example%2Ffiles%2F3.4.1%2FTool3_4_1.zip"}),
        },
        "3.4.1", "https://cdn.example/files/3.4.1/Tool3_4_1.zip",
    ),
    "chain": Vendor(
        'format = 2\n[track]\nprovider = "chain"\ndownload = "${link}"\n'
        "[[track.fetch]]\nurl = \"https://vendor.example/\"\nmatch = '(?P<link>https://vendor.example/get/\\d+)'\n@STEP@"
        "[[track.fetch]]\nurl = \"${link}\"\nheader = \"Content-Disposition\"\n"
        "match = 'tool-(?P<version>\\d+(?:\\.\\d+)+)\\.zip'\n@STEP@"
        '[source]\ndownload_url = "${download_url}"\n',
        {
            "https://vendor.example/": FakeHttp('<a href="https://vendor.example/get/77">download</a>'),
            "https://vendor.example/get/77": FakeHttp("", 200, {"Content-Disposition": 'attachment; filename="tool-2.5.zip"'}),
        },
        "2.5", "https://vendor.example/get/77",
    ),
    "json": Vendor(
        'format = 2\n[track]\nprovider = "json"\ncheck_url = "https://vendor.example/releases.json"\n'
        'records = "$.releases[*]"\nfilter = "stable==true"\nversion = "$.name"\ndownload = "$.url"\nchecksum = "$.sha"\n'
        "match = 'v(?P<version>\\d+(?:\\.\\d+)+)'\n@HEADERS@"
        '[source]\ndownload_url = "${download_url}"\n',
        {"https://vendor.example/releases.json": json_response({"releases": [
            {"name": "v1.9.0", "stable": True, "url": "https://vendor.example/d/1.9.0.zip", "sha": f"sha256:{'1' * 64}"},
            {"name": "v2.0.0", "stable": False, "url": "https://vendor.example/d/2.0.0.zip", "sha": f"sha256:{'2' * 64}"},
            {"name": "v1.10.0", "stable": True, "url": "https://vendor.example/d/1.10.0.zip", "sha": f"sha256:{DIGEST}"},
        ]})},
        "1.10.0", "https://vendor.example/d/1.10.0.zip", True, DIGEST, "vendor-sums",
    ),
    "last-modified": Vendor(
        'format = 2\n[track]\nprovider = "last_modified"\ncheck_url = "https://vendor.example/files/Tool.zip"\nformat = "%Y%m%d"\n'
        '@HEADERS@[source]\ndownload_url = "https://vendor.example/files/Tool.zip"\n',
        {"https://vendor.example/files/Tool.zip": FakeHttp("", 200, {"Last-Modified": "Wed, 16 Oct 2024 01:30:00 +0200"})},
        "20241015", "https://vendor.example/files/Tool.zip", False,
    ),
    "rss": Vendor(
        'format = 2\n[track]\nprovider = "rss"\ncheck_url = "https://vendor.example/rss"\n'
        "match = '/(?P<name>tool-v(?P<version>\\d+(?:\\.\\d+)*)\\.zip)'\n"
        'download = "https://vendor.example/files/${name}"\n@HEADERS@[source]\ndownload_url = "${download_url}"\n',
        {"https://vendor.example/rss": FakeHttp(
            "<rss><channel>"
            + "".join(f"<item><title>/tool-v{number}.zip</title><link>https://vendor.example/x/{number}</link></item>" for number in ("1.3", "1.10", "1.9"))
            + "</channel></rss>")},
        "1.10", "https://vendor.example/files/tool-v1.10.zip",
    ),
}
