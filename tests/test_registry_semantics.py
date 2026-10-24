"""Cover what the 0.17 hooks did that the first service rewrite lost: each is now a tested behaviour of a real tracker.

Behavior covered: shutter-encoder finds the Windows 64-bit file whatever order the home page lists its operating
systems in and however many links fail, stops at the first match, fails closed when none matches or when too many
links must be tried, and never takes a regular-expression deadline for "this link does not match"; inkscape still
finds its version when the digest link is missing, dead or empty (the registry tool then computes the hash) and
still uses the digest when there is one; emacs takes the digest from GNU's sums file when it lists the archive and
otherwise goes on without one; pidgin falls back to an older folder when the newest has no offline installer; 7-Zip
reads the x64 announcement whichever order x86, ARM64 and x64 appear in and refuses a page that has none; winscp
sends its browser User-Agent and Accept header on both page fetches; the checksum ``optional`` flag and the chain
``optional`` and ``each`` keys validate and round-trip; and the registry's list of unversioned download addresses matches the
fixtures.
Real: the committed trackers (snapshot), the track services, the regex worker process, the HTTP client. Mocked:
``FakeSystem`` serves synthetic pages. Out of scope: the live vendors (manual).
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, manifest_to_toml, parse_manifest, render_pinned
from gupkg.services.track import TrackError, run_tracker
from gupkg.services.track.page import MAX_EACH_CANDIDATES
from gupkg.system import regex_worker
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.http_fixtures import HTTP_CASES, REGISTRY2, environment, load_case, tracker_manifest

REGISTRY = REGISTRY2  # the committed snapshot, or the registry under test when the registry's CI sets GUPKG_REGISTRY_TRACKERS


def discover(system: FakeSystem, tmp_path: Path, package: str):
    """Run the committed tracker of *package*; return ``(result, pinned manifest)``."""
    manifest = tracker_manifest(package)
    result = run_tracker(manifest, environment(system, tmp_path, package=package))
    return result, render_pinned(manifest, result)


def served(tmp_path: Path, *cases: str) -> FakeSystem:
    """A FakeSystem serving the fixture *cases*."""
    system = FakeSystem(tmp_path / "machine")
    for case in cases:
        system.urls.update(load_case(case))
    return system


def requests(system: FakeSystem, method: str | None = None) -> list[str]:
    """Return the addresses requested so far (optionally only those of one method)."""
    return [r.url for r in system.request_log if method is None or r.method == method]


# -- shutter-encoder: try each link --------------------------------------------------------------------------

SHUTTER = "https://www.shutterencoder.com/sdc_download/{}/?key={}"
LINKS = {
    "windows": (1234, "abcDEF123", 'attachment; filename="Shutter Encoder 18.4 Windows 64bits.zip"'),
    "mac": (9999, "zzzMAC999", 'attachment; filename="Shutter Encoder 18.4 Mac.dmg"'),
    "linux": (7777, "ccLINUX777", 'attachment; filename="Shutter Encoder 18.4 Linux 64bits.tar.gz"'),
    "old": (6666, "ddOLD666", 'attachment; filename="Shutter Encoder 17.0 Windows 32bits.zip"'),
    "gone": (5555, "bbbGONE555", None),
}


def shutter_system(tmp_path: Path, order: tuple[str, ...]) -> FakeSystem:
    """A home page that links the systems in *order*; each link answers with its own file name (or 404)."""
    system = FakeSystem(tmp_path / "machine")
    body = " ".join(f'<a href="{SHUTTER.format(LINKS[n][0], LINKS[n][1])}">{n}</a>' for n in order)
    system.urls["https://www.shutterencoder.com/"] = FakeHttp(body)
    for name in order:
        number, key, disposition = LINKS[name]
        system.urls[SHUTTER.format(number, key)] = (
            FakeHttp("", 200, {"Content-Disposition": disposition}) if disposition else FakeHttp("", 404)
        )
    return system


ORDERS = [
    pytest.param(order, id="-".join(order))
    for order in [
        *itertools.permutations(("windows", "mac", "linux")),
        ("gone", "mac", "windows", "linux"), ("mac", "gone", "old", "linux", "windows"), ("windows",),
    ]
]


@pytest.mark.parametrize("order", ORDERS)
def test_shutter_encoder_finds_the_windows_build_in_whatever_order_the_page_lists_the_systems(tmp_path: Path, order: tuple[str, ...]) -> None:
    """The Windows 64-bit link is chosen from any position, links before it that fail or name another system are skipped, and none after it is fetched."""
    system = shutter_system(tmp_path, order)

    result, pinned = discover(system, tmp_path, "shutter-encoder")

    assert result.version == "18.4"
    assert pinned.source.download_url == SHUTTER.format(1234, "abcDEF123")
    heads = requests(system, "HEAD")
    assert len(heads) == order.index("windows") + 1 and heads[-1] == SHUTTER.format(1234, "abcDEF123")


def test_shutter_encoder_also_finds_links_that_are_plain_text(tmp_path: Path) -> None:
    """Like the 0.17 hook's search, the tracker does not need an ``href=`` around the address: a bare URL in the page counts."""
    system = shutter_system(tmp_path, ("mac", "windows"))
    system.urls["https://www.shutterencoder.com/"] = FakeHttp(
        f"Mac {SHUTTER.format(9999, 'zzzMAC999')} and Windows {SHUTTER.format(1234, 'abcDEF123')} (also in an href)"
    )

    result, pinned = discover(system, tmp_path, "shutter-encoder")

    assert pinned.source.download_url == SHUTTER.format(1234, "abcDEF123") and result.version == "18.4"


def test_shutter_encoder_fails_closed_when_no_link_names_the_windows_build(tmp_path: Path) -> None:
    """With only other systems linked the tracker fails and says every candidate was tried."""
    system = shutter_system(tmp_path, ("mac", "linux", "gone", "old"))

    with pytest.raises(TrackError, match=r"None of the 4 candidates of step 1"):
        discover(system, tmp_path, "shutter-encoder")

    assert len(requests(system, "HEAD")) == 4


def test_shutter_encoder_stops_after_the_candidate_cap(tmp_path: Path) -> None:
    """More links than the cap are not all fetched: the Windows link in 25th place is never reached and the check fails."""
    system = FakeSystem(tmp_path / "machine")
    links = [SHUTTER.format(1000 + n, f"k{n:03d}") for n in range(MAX_EACH_CANDIDATES + 5)]
    system.urls["https://www.shutterencoder.com/"] = FakeHttp(" ".join(f'<a href="{u}">x</a>' for u in links))
    for link in links[:-1]:
        system.urls[link] = FakeHttp("", 200, {"Content-Disposition": 'attachment; filename="Shutter Encoder 18.4 Mac.dmg"'})
    system.urls[links[-1]] = FakeHttp("", 200, {"Content-Disposition": 'attachment; filename="Shutter Encoder 18.4 Windows 64bits.zip"'})

    with pytest.raises(TrackError, match="candidates"):
        discover(system, tmp_path, "shutter-encoder")

    assert len(requests(system, "HEAD")) == MAX_EACH_CANDIDATES


def test_a_regex_deadline_while_trying_candidates_stops_the_check(tmp_path: Path) -> None:
    """A pattern that runs out of time is an error, never taken for "this candidate does not match" and followed by the next one."""
    system = shutter_system(tmp_path, ("mac", "windows"))
    calls: list[int] = []

    def worker(call: ProcessCall) -> ProcessResult:
        calls.append(1)
        if len(calls) >= 2:
            raise TimeoutError("deadline")
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-s", "-B", str(Path(regex_worker.__file__))],
            input=call.input_text, capture_output=True, text=True, check=False,
        )
        return ProcessResult(done.returncode, done.stdout, done.stderr)

    system.processes["regex-worker"] = worker

    with pytest.raises(TrackError):
        discover(system, tmp_path, "shutter-encoder")

    assert len(requests(system, "HEAD")) == 1


# -- inkscape: the digest is optional --------------------------------------------------------------------------

DIGEST_PAGE = "https://inkscape.org/en/release/inkscape-1.4/windows/64-bit/compressed-7z/dl/"
DIGEST_FILE = "https://media.inkscape.org/dl/resources/file/inkscape-1.4_2024-10-11_86a8ad7-x64.7z.sha256"
ARCHIVE_LINK = '<a href="/gallery/item/53697/inkscape-1.4_2024-10-11_86a8ad7-x64.7z">Download</a>'


def inkscape_system(tmp_path: Path, *, page: str | None = None, digest: FakeHttp | None = None) -> FakeSystem:
    """The inkscape fixture, with the download page and the digest file optionally replaced."""
    system = served(tmp_path, "chain_inkscape")
    if page is not None:
        system.urls[DIGEST_PAGE] = FakeHttp(page)
    if digest is not None:
        system.urls[DIGEST_FILE] = digest
    return system


def test_inkscape_uses_the_published_digest_when_there_is_one(tmp_path: Path) -> None:
    """With the digest link and file, the checksum is the vendor's."""
    result, pinned = discover(inkscape_system(tmp_path), tmp_path, "inkscape")

    assert pinned.source.sha256 == "f9593190b7ed8fef57675e3a2d29f4d490612544bbc31de02ce784e6817b4c0c"
    assert pinned.source.sha256_from == "vendor-sums"


@pytest.mark.parametrize(
    ("name", "page", "digest"),
    [
        pytest.param("no-digest-link", ARCHIVE_LINK, None, id="no-digest-link"),
        pytest.param("dead-digest-file", None, FakeHttp("", 404), id="dead-digest-file"),
        pytest.param("digest-file-without-a-digest", None, FakeHttp("sorry, not available"), id="digest-file-without-a-digest"),
    ],
)
def test_inkscape_is_still_found_without_a_usable_digest(tmp_path: Path, name: str, page: str | None, digest: FakeHttp | None) -> None:
    """A missing, dead or empty digest costs only the checksum: the version and the download are found, and no hash is invented."""
    result, pinned = discover(inkscape_system(tmp_path, page=page, digest=digest), tmp_path, "inkscape")

    assert result.version == "1.4"
    assert pinned.source.download_url == "https://inkscape.org/gallery/item/53697/inkscape-1.4_2024-10-11_86a8ad7-x64.7z"
    assert pinned.source.sha256 is None and pinned.source.sha256_from is None


def test_inkscape_takes_a_digest_only_as_a_whole_64_digit_token(tmp_path: Path) -> None:
    """A run of 65 or more hexadecimal digits is no digest (no slice of it is taken); a real digest after other text still is one."""
    longer = inkscape_system(tmp_path, digest=FakeHttp("a" * 70 + "\n"))
    _result, pinned = discover(longer, tmp_path, "inkscape")
    assert pinned.source.sha256 is None

    framed = inkscape_system(tmp_path, digest=FakeHttp("sha256:" + "b" * 64 + " (inkscape)\n"))
    _result, pinned = discover(framed, tmp_path, "inkscape")
    assert pinned.source.sha256 == "b" * 64


def test_inkscape_still_needs_its_download_link(tmp_path: Path) -> None:
    """Only the digest steps are optional: a page without the archive link fails the check."""
    with pytest.raises(TrackError, match="matched nothing"):
        discover(inkscape_system(tmp_path, page="<p>nothing here</p>"), tmp_path, "inkscape")


# -- emacs: the sums file when GNU publishes one -----------------------------------------------------------------

SUMS = "https://ftp.gnu.org/gnu/emacs/windows/emacs-30/emacs-30.2-sha256sums.txt"


def test_emacs_takes_the_digest_from_gnus_sums_file(tmp_path: Path) -> None:
    """The listed archive's digest is the checksum, from the vendor's sums file."""
    _result, pinned = discover(served(tmp_path, "index_emacs"), tmp_path, "emacs")

    assert pinned.source.sha256 == "5e1b3c8d9a7f4e2b6c0d1a8f3e7b9c4d2a6f0e8b1c3d5a7f9e2b4c6d8a0f1e3b"
    assert pinned.source.sha256_from == "vendor-sums"


@pytest.mark.parametrize(
    "sums",
    [pytest.param(FakeHttp("", 404), id="no-sums-file"), pytest.param(FakeHttp(f"{'a' * 64}  emacs-30.2-no-deps.zip\n"), id="archive-not-listed")],
)
def test_emacs_goes_on_without_a_digest_when_gnu_publishes_none(tmp_path: Path, sums: FakeHttp) -> None:
    """A missing sums file or an unlisted archive leaves the version found and the checksum for the registry tool to compute."""
    system = served(tmp_path, "index_emacs")
    system.urls[SUMS] = sums

    result, pinned = discover(system, tmp_path, "emacs")

    assert result.version == "30.2" and pinned.source.sha256 is None


# -- pidgin: older folders ----------------------------------------------------------------------------------------


def test_pidgin_falls_back_to_the_newest_folder_that_has_the_offline_installer(tmp_path: Path) -> None:
    """2.14.14 has only a source archive, so the walk looks into it and then settles on 2.14.13."""
    system = served(tmp_path, "index_pidgin")

    result, pinned = discover(system, tmp_path, "pidgin")

    assert result.version == "2.14.13"
    assert pinned.source.download_url.endswith("/2.14.13/pidgin-2.14.13-offline.exe/download")
    asked = requests(system)
    assert any(url.endswith("/Pidgin/2.14.14/") for url in asked) and any(url.endswith("/Pidgin/2.14.13/") for url in asked)


# -- 7-Zip: the x64 announcement ---------------------------------------------------------------------------------

ANNOUNCEMENTS = {
    "x86": "<B>Download 7-Zip 24.09 (2024-11-29) for Windows x86</B>",
    "arm64": "<B>Download 7-Zip 24.08 (2024-08-11) for Windows ARM64</B>",
    "x64": "<B>Download 7-Zip 25.01 (2025-08-03) for Windows x64</B>",
    "x64-tags": "<B>Download 7-Zip 25.01 (2025-08-03) for Windows</B> <B>x64</B>",
    "x64-nbsp": "<B>Download 7-Zip 25.01 (2025-08-03) for Windows&nbsp;x64:</B>",
}


@pytest.mark.parametrize(
    "order",
    [
        pytest.param(order, id="-".join(order))
        for order in [
            ("x86", "x64"), ("x64", "x86"), ("arm64", "x86", "x64"), ("x64", "arm64", "x86"),
            ("x86", "x64-tags", "arm64"), ("arm64", "x64-nbsp", "x86"),
        ]
    ],
)
def test_7zip_reads_the_x64_announcement_in_whatever_order_the_builds_are_listed(tmp_path: Path, order: tuple[str, ...]) -> None:
    """The version of the x64 announcement wins over the x86 and ARM64 ones, and the download is the x64 installer."""
    system = FakeSystem(tmp_path / "machine")
    system.urls["https://www.7-zip.org/"] = FakeHttp("<html><body>" + "<P>" + "</P><P>".join(ANNOUNCEMENTS[n] for n in order) + "</P></body></html>")

    result, pinned = discover(system, tmp_path, "7zip")

    assert result.version == "25.01"
    assert pinned.source.download_url == "https://www.7-zip.org/a/7z2501-x64.exe"


def test_7zip_refuses_a_page_with_no_x64_announcement(tmp_path: Path) -> None:
    """A page that announces only x86 and ARM64 builds is an error, never a version taken from them."""
    system = FakeSystem(tmp_path / "machine")
    system.urls["https://www.7-zip.org/"] = FakeHttp(f"<P>{ANNOUNCEMENTS['x86']}</P><P>{ANNOUNCEMENTS['arm64']}</P>")

    with pytest.raises(TrackError, match="matched nothing"):
        discover(system, tmp_path, "7zip")


# -- winscp: browser headers on both fetches -----------------------------------------------------------------------


def test_winscp_sends_its_browser_headers_on_both_page_fetches(tmp_path: Path) -> None:
    """The downloads page and the file's own page are both requested with the User-Agent and Accept header of 0.17."""
    system = served(tmp_path, "chain_winscp")

    discover(system, tmp_path, "winscp")

    pages = [r for r in system.request_log if r.url.startswith("https://winscp.net/")]
    assert len(pages) == 2
    for request in pages:
        headers = {k.lower(): v for k, v in request.headers.items()}
        assert "gupkg-update-check" in headers["user-agent"] and headers["accept"].startswith("text/html")


# -- the new manifest keys ---------------------------------------------------------------------------------------

CHAIN = (
    'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "chain"\ndownload = "${link}"\n'
    '[[track.fetch]]\nurl = "https://example.org/"\nselect = "@FIRST@"\nmatch = \'(?P<link>https://example.org/f/\\d+)\'\n'
    '[[track.fetch]]\nurl = "${link}"\nheader = "Content-Disposition"\nmatch = \'(?P<version>\\d+\\.\\d+)\'\n@EXTRA@'
    '[source]\ndownload_url = "${download_url}"\n'
)


def chain(first: str = "max", extra: str = "") -> str:
    """Return the test chain with the first step's ``select`` and extra keys on the last step filled in."""
    return CHAIN.replace("@FIRST@", first).replace("@EXTRA@", extra)


def test_each_is_refused_on_the_last_step_and_unknown_values_are_refused() -> None:
    """``select = "each"`` needs a later step to try its candidates against; the manifest says so."""
    last = chain(first="max", extra='select = "each"\n')
    with pytest.raises(Exception, match="last step"):
        parse_manifest(last, ManifestKind.TRACKER)
    with pytest.raises(Exception, match="select"):
        parse_manifest(chain(first="sometimes"), ManifestKind.TRACKER)
    assert parse_manifest(chain(first="each"), ManifestKind.TRACKER).track.provider == "chain"


def test_checksum_optional_round_trips_and_must_be_a_boolean() -> None:
    """``optional`` on ``[source.checksum]`` is a boolean, survives writing the manifest and is absent when false."""
    base = (
        'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "page"\ncheck_url = "https://e.org/"\n'
        'match = \'v(?P<version>\\d+)\'\n[source]\ndownload_url = "https://e.org/${version}.zip"\n[source.checksum]\n'
        'sums_file = "https://e.org/${version}.sums"\nentry = "x.zip"\n'
    )
    manifest = parse_manifest(base + "optional = true\n", ManifestKind.TRACKER)

    assert manifest.source.checksum.optional is True
    assert tomllib.loads(manifest_to_toml(manifest))["source"]["checksum"]["optional"] is True
    assert "optional" not in tomllib.loads(manifest_to_toml(parse_manifest(base, ManifestKind.TRACKER)))["source"]["checksum"]
    with pytest.raises(Exception, match="true or false"):
        parse_manifest(base + 'optional = "yes"\n', ManifestKind.TRACKER)


def test_a_regex_failure_in_an_optional_checksum_is_not_swallowed(tmp_path: Path) -> None:
    """An optional checksum source that runs a pattern out of time stops the check instead of silently dropping the digest."""
    system = served(tmp_path, "index_emacs")
    calls: list[int] = []

    def worker(call: ProcessCall) -> ProcessResult:
        calls.append(1)
        if len(calls) >= 2:  # the version pattern ran; the checksum pattern runs out of time
            raise TimeoutError("deadline")
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-s", "-B", str(Path(regex_worker.__file__))], input=call.input_text,
            capture_output=True, text=True, check=False,
        )
        return ProcessResult(done.returncode, done.stdout, done.stderr)

    system.processes["regex-worker"] = worker
    tracker = (
        'format = 2\ndescription = "d"\nmodes = ["system", "user"]\n[track]\nprovider = "page"\n'
        'check_url = "https://ftp.gnu.org/gnu/emacs/windows/emacs-30/"\n'
        "match = 'emacs-(?P<version>30\\.\\d+)-UCRT64\\.zip'\n"
        '[source]\ndownload_url = "https://example.org/e-${version}.zip"\n'
        "[source.checksum]\npage_match = '(?P<sha256>[0-9a-f]{64})'\noptional = true\n"
    )

    with pytest.raises(Exception, match="was stopped"):
        run_tracker(parse_manifest(tracker, ManifestKind.TRACKER), environment(system, tmp_path, package="emacs"))


# -- unversioned download addresses ---------------------------------------------------------------------------------


def test_the_registry_lists_exactly_the_packages_whose_download_address_has_no_version() -> None:
    """``unversioned`` in ``_registry.toml`` equals the packages whose fixture download address holds no form of the version."""
    listed = set(tomllib.loads((REGISTRY / "_registry.toml").read_text(encoding="utf-8"))["unversioned"])
    computed = set()
    for case in HTTP_CASES.glob("*.json"):
        document = json.loads(case.read_text(encoding="utf-8"))
        version, url = document["expected"]["version"], document["expected"]["download_url"]
        forms = {version, version.replace(".", ""), version.replace(".", "-"), version.replace(".", "_"), version.replace("-", "").replace(".", "")}
        if not any(form in url for form in forms):
            computed.add(document["tracker"])

    assert listed == computed == {"autoruns", "dontsleep", "ffmpeg", "processexplorer", "shutter-encoder", "xnviewmp"}
