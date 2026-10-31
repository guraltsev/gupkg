"""Cover how a tracker's ``[track]`` provider finds the newest version, the download address and the checksum.

``gupkg check-updates`` runs the provider named in a tracker's ``[track]`` table against the vendor's answers and turns
what it finds into a pinned version (``_docs/rehaul_design.md`` section 8.1). Every provider kind (``github``, ``gitlab``,
``page``, ``index``, ``redirect``, ``json``, ``last_modified``, ``rss``, ``chain``) finds the newest version by the one
shared version order (``1.10`` is newer than ``1.3``) and expands the ``download`` template against the page or release it
read; a tracker's own headers replace gupkg's User-Agent on every request of its provider; a ``sha256`` capture or a
digest the host publishes becomes the discovered checksum, and a configured ``[source.checksum]`` finds the digest of the
version that was found. A GitHub release is passed over when it is a draft, a prerelease (unless asked for), lacks the
asset or does not fit the tag rule. The index walk goes back to an older folder when the newest has no file and stops at 60
requests. A redirect is followed hop by hop. A chain step marked ``optional`` is skipped when it fails; a step marked
``each`` tries the page's links in order, at most 20, and keeps the first that the later steps accept. Anything a
response gives that cannot be used is an error with a message, never a guess: an unsafe version name, a regular
expression that runs out of time, a page that does not hold what the pattern wants.

The git provider is covered with its process double in ``test_git_services``, and the validation of the ``[track]`` table in
``tests/model/test_trackspec.py``.

Tests use small trackers and vendor answers written for them (``tests/support/track.py``). ``FakeSystem`` serves the
answers; the HTTP client, the providers and the regex worker process are the real ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gupkg.model.manifest import ManifestKind, manifest_to_toml, render_pinned
from gupkg.pkgcheck import check_directory
from gupkg.services.checksum import ChecksumDiscoveryError
from gupkg.services.http import HttpClient
from gupkg.services.track import TrackEnvironment, TrackError, run_tracker
from gupkg.services.track.index import MAX_REQUESTS
from gupkg.services.track.page import MAX_EACH_CANDIDATES
from gupkg.system.fake import FakeHttp, FakeSystem, ProcessCall
from gupkg.system.port import ProcessResult
from tests.support.scope import scope_of
from tests.support.track import (
    DIGEST,
    VENDOR_AGENT,
    VENDORS,
    environment,
    json_response,
    parse_tracker,
    real_regex_worker,
)

pytestmark = pytest.mark.unit


def run(system: FakeSystem, tmp_path: Path, text: str):
    """Run the tracker *text* against *system* and return the provider's result."""
    return run_tracker(parse_tracker(text), environment(system, tmp_path))


def requested(system: FakeSystem, method: str | None = None) -> list[str]:
    """Return the addresses requested so far, optionally only those of one HTTP method."""
    return [request.url for request in system.request_log if method is None or request.method == method]


# -- every provider kind finds the version, the address and the checksum ----------------------------------


@pytest.mark.process
@pytest.mark.parametrize("kind", [pytest.param(name, id=name) for name in VENDORS])
def test_each_provider_finds_the_newest_version_and_the_download_address(system: FakeSystem, tmp_path: Path, kind: str) -> None:
    """The newest version and the expanded download address come out in the pinned version, with any digest the vendor gave.

    The vendor answers list an older version, a newer one written with a longer number, and what a real listing holds
    besides (another system's file, an unstable release), so the version order and the selection rules are on the path.
    The pinned version is an ordinary version: no `[track]`, an address with no template left, a checksum only when the
    provider found one, and it passes the package checker as it would be written to the registry.
    """
    vendor = VENDORS[kind]
    vendor.serve(system)
    manifest = parse_tracker(vendor.tracker_text())

    result = run_tracker(manifest, environment(system, tmp_path))
    pinned = render_pinned(manifest, result)

    assert result.version == vendor.version
    assert pinned.source.download_url == vendor.download_url
    assert (pinned.source.sha256, pinned.source.sha256_from) == (vendor.sha256, vendor.sha256_from)
    assert pinned.kind is ManifestKind.VERSION and pinned.track is None
    version = tmp_path / "tool" / result.version
    version.mkdir(parents=True)
    (version / "pkg.toml").write_text(manifest_to_toml(pinned), encoding="utf-8", newline="\n")
    assert check_directory(version).ok


@pytest.mark.process
@pytest.mark.parametrize("kind", [pytest.param(name, id=name) for name, vendor in VENDORS.items() if vendor.sends_track_headers])
def test_a_tracker_sends_its_own_headers_with_every_request_of_its_provider(system: FakeSystem, tmp_path: Path, kind: str) -> None:
    """A tracker's `headers` replace gupkg's User-Agent on every request its provider makes, the second page of a chain included.

    Some vendors refuse the default User-Agent, so the tracker names the one the vendor accepts.
    """
    vendor = VENDORS[kind]
    vendor.serve(system)

    run(system, tmp_path, vendor.tracker_text(headers=True))

    sent = [request.headers["User-Agent"] for request in system.request_log]
    assert sent and set(sent) == {VENDOR_AGENT}


# -- GitHub and GitLab releases ------------------------------------------------------------------------------

RELEASES = "https://api.github.com/repos/o/r/releases?per_page=100"


def release(tag: str, *assets: str, prerelease: bool = False, draft: bool = False) -> dict[str, object]:
    """Return a GitHub release of *tag* with the named assets."""
    return {
        "tag_name": tag, "prerelease": prerelease, "draft": draft,
        "assets": [{"name": name, "state": "uploaded", "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{name}"} for name in assets],
    }


def newest_matching(extra: str = "") -> str:
    """Return a tracker for the newest release of o/r that holds the asset `tool-<version>.zip`."""
    return (
        'format = 2\n[track]\nprovider = "github"\nrepo = "o/r"\nrelease = "newest-matching"\nasset = "tool-${version}.zip"\n'
        + extra + '[source]\ndownload_url = "${asset_url}"\n'
    )


def test_the_newest_release_wins_by_version_order_and_unusable_releases_are_passed_over(system: FakeSystem, tmp_path: Path) -> None:
    """Among many releases the newest by version order that has the asset is chosen, not the newest tag or the first listed.

    `v1.10.0` beats `v1.9.0` (the order is numeric); a release candidate, a draft and a newer release that lacks the
    asset are all passed over.
    """
    system.urls[RELEASES] = json_response([
        release("v1.9.0", "tool-1.9.0.zip"),
        release("v2.0.0-rc1", "tool-2.0.0-rc1.zip", prerelease=True),
        release("v1.10.0", "tool-1.10.0.zip", "tool-1.10.0.tar.gz"),
        release("v1.11.0", "tool-1.11.0.zip", draft=True),
        release("v1.12.0", "tool-1.12.0.tar.gz"),
    ])

    result = run(system, tmp_path, newest_matching())

    assert result.version == "1.10.0" and result.captures["asset_name"] == "tool-1.10.0.zip"
    assert result.captures["tag"] == "v1.10.0"


def test_a_newer_prerelease_is_ignored_unless_the_tracker_asks_for_prereleases(system: FakeSystem, tmp_path: Path) -> None:
    """`prerelease = false` (the default) passes over a newer release candidate; `prerelease = true` takes it."""
    system.urls[RELEASES] = json_response([release("v2.0.0-rc1", "tool-2.0.0-rc1.zip", prerelease=True), release("v1.9.0", "tool-1.9.0.zip")])

    assert run(system, tmp_path, newest_matching()).version == "1.9.0"
    assert run(system, tmp_path, newest_matching("prerelease = true\n")).version == "2.0.0-rc1"


def test_a_tag_prefix_is_removed_and_a_tag_without_it_is_passed_over(system: FakeSystem, tmp_path: Path) -> None:
    """With `tag_prefix` the version is the tag after the prefix; a release of another product that is newer is skipped."""
    system.urls[RELEASES] = json_response([release("demo-9.9", "tool-9.9.zip"), release("tool-1.2", "tool-1.2.zip")])

    result = run(system, tmp_path, newest_matching('tag_prefix = "tool-"\n'))

    assert result.version == "1.2"


def test_the_github_provider_sends_the_token_only_to_github_over_https(system: FakeSystem, tmp_path: Path) -> None:
    """GITHUB_TOKEN reaches api.github.com in an Authorization header; a client without it sends none at all."""
    vendor = VENDORS["github-latest"]
    vendor.serve(system)
    manifest = parse_tracker(vendor.tracker_text())
    with_token = TrackEnvironment(system, scope_of(system), HttpClient(system, env={"GITHUB_TOKEN": "t0ken"}, sleep=lambda seconds: None))

    run_tracker(manifest, with_token)
    run_tracker(manifest, environment(system, tmp_path))

    assert system.request_log[0].url.startswith("https://api.github.com/")
    assert system.request_log[0].headers["Authorization"] == "Bearer t0ken"
    assert "Authorization" not in system.request_log[1].headers


# -- the page provider: the select rule -----------------------------------------------------------------------

PAGE = "https://vendor.example/page"
PAGE_TRACKER = (
    'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://vendor.example/page"\n'
    "match = 'tool-(?P<version>\\d+(?:\\.\\d+)+)\\.zip'\n@SELECT@[source]\ndownload_url = \"https://vendor.example/${version}.zip\"\n"
)


@pytest.mark.process
@pytest.mark.parametrize(
    ("select", "page", "expected"),
    [
        pytest.param("", "tool-1.3.zip tool-3.0.21.zip tool-3.0.9.2.zip", "3.0.21", id="max-is-the-default-and-orders-by-version"),
        pytest.param('select = "first"\n', "tool-1.3.zip tool-3.0.21.zip", "1.3", id="first-takes-the-first-in-the-page"),
        pytest.param('select = "unique"\n', "tool-1.3.zip tool-1.3.zip", "1.3", id="unique-accepts-one-value-listed-twice"),
    ],
)
def test_the_select_rule_chooses_among_several_matches_on_a_page(
    system: FakeSystem, tmp_path: Path, select: str, page: str, expected: str
) -> None:
    """`max` takes the newest by version order (3.0.21 over 3.0.9.2), `first` the first, `unique` the only value."""
    system.urls[PAGE] = FakeHttp(page)

    assert run(system, tmp_path, PAGE_TRACKER.replace("@SELECT@", select)).version == expected


@pytest.mark.process
def test_select_unique_refuses_a_page_that_gives_two_different_values(system: FakeSystem, tmp_path: Path) -> None:
    """When the page must name one value and names two, the check fails and counts them instead of picking one."""
    system.urls[PAGE] = FakeHttp("tool-1.3.zip tool-2.0.zip")

    with pytest.raises(TrackError, match="found 2 different values"):
        run(system, tmp_path, PAGE_TRACKER.replace("@SELECT@", 'select = "unique"\n'))


@pytest.mark.process
@pytest.mark.parametrize(
    ("digest", "found"),
    [
        pytest.param("A" * 64, "a" * 64, id="upper-case-digest-is-lower-cased"),
        pytest.param("a" * 65, None, id="longer-than-64-is-no-digest"),
        pytest.param("a" * 63, None, id="shorter-than-64-is-no-digest"),
        pytest.param("z" * 64, None, id="not-hexadecimal-is-no-digest"),
    ],
)
def test_a_sha256_capture_is_the_checksum_only_when_it_is_a_whole_digest(
    system: FakeSystem, tmp_path: Path, digest: str, found: str | None
) -> None:
    """A page that gives the digest next to the version supplies the checksum, but only as exactly 64 hexadecimal digits."""
    system.urls[PAGE] = FakeHttp(f"tool-1.0.zip sha256={digest}")
    text = (
        'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://vendor.example/page"\n'
        "match = 'tool-(?P<version>\\d+(?:\\.\\d+)+)\\.zip sha256=(?P<sha256>\\S+)'\n"
        '[source]\ndownload_url = "https://vendor.example/${version}.zip"\n'
    )

    result = run(system, tmp_path, text)

    assert result.version == "1.0"
    assert (result.checksum.sha256 if result.checksum else None) == found


# -- the index provider -------------------------------------------------------------------------------------------


@pytest.mark.process
def test_the_index_walk_goes_back_to_an_older_folder_when_the_newest_has_no_file(system: FakeSystem, tmp_path: Path) -> None:
    """A release folder that does not hold the file yet is left for the next best one, and both folders were looked into."""
    vendor = VENDORS["index"]
    vendor.serve(system)
    system.urls["https://vendor.example/releases/1.10/"] = FakeHttp("<p>still uploading</p>")
    system.urls["https://vendor.example/releases/1.9/"] = FakeHttp('<a href="tool-1.9-setup.exe">setup</a>')

    result = run(system, tmp_path, vendor.tracker_text())

    assert result.version == "1.9" and result.captures["download_url"] == "https://vendor.example/releases/1.9/tool-1.9-setup.exe"
    asked = requested(system)
    assert "https://vendor.example/releases/1.10/" in asked and asked[-1] == "https://vendor.example/releases/1.9/"


@pytest.mark.process
def test_the_index_walk_stops_after_its_request_budget(system: FakeSystem, tmp_path: Path) -> None:
    """A listing of a hundred entries that all fail their probe cannot keep the check running: it stops at 60 requests."""
    entries = " ".join(f'<a href="/releases/1.{number}/">x</a>' for number in range(100))
    system.urls["https://vendor.example/releases/"] = FakeHttp(entries)
    text = (
        'format = 2\n[track]\nprovider = "index"\ncheck_url = "https://vendor.example/releases/"\n'
        "[[track.level]]\nmatch = '/releases/(?P<version>\\d+(?:\\.\\d+)+)/'\nnext = \"${version}/\"\nprobe = \"${version}/tool.zip\"\n"
        '[source]\ndownload_url = "https://vendor.example/${version}.zip"\n'
    )
    for number in range(100):
        system.urls[f"https://vendor.example/releases/1.{number}/tool.zip"] = FakeHttp("", 404)

    with pytest.raises(TrackError, match=f"more than {MAX_REQUESTS} requests"):
        run(system, tmp_path, text)

    assert len(system.request_log) == MAX_REQUESTS == 60


# -- the redirect provider ----------------------------------------------------------------------------------------

REDIRECT = (
    'format = 2\n[track]\nprovider = "redirect"\ncheck_url = "https://vendor.example/latest"\n'
    "match = '/files/(?P<version>\\d+(?:\\.\\d+)+)/'\n[source]\ndownload_url = \"${download_url}\"\n"
)


@pytest.mark.process
def test_a_redirect_is_followed_to_the_address_that_answers_and_that_address_is_the_download(system: FakeSystem, tmp_path: Path) -> None:
    """Without `unwrap_query` the hops are followed until an address answers; its version and file name are the result's."""
    system.urls["https://vendor.example/latest"] = FakeHttp("", 302, {"Location": "https://cdn.example/files/3.4.1/Tool3_4_1.zip"})
    system.urls["https://cdn.example/files/3.4.1/Tool3_4_1.zip"] = FakeHttp("")

    result = run(system, tmp_path, REDIRECT)

    assert result.version == "3.4.1"
    assert result.captures["download_url"] == "https://cdn.example/files/3.4.1/Tool3_4_1.zip"
    assert result.captures["file_name"] == "Tool3_4_1.zip"
    assert requested(system) == ["https://vendor.example/latest", "https://cdn.example/files/3.4.1/Tool3_4_1.zip"]


@pytest.mark.process
def test_a_server_that_refuses_head_is_asked_again_with_get(system: FakeSystem, tmp_path: Path) -> None:
    """Some servers answer HEAD with 405; the provider asks again with GET so the redirect is still read."""
    answers = iter([FakeHttp("", 405), FakeHttp("", 302, {"Location": "https://cdn.example/files/2.0/T.zip"})])
    system.urls["https://vendor.example/latest"] = lambda url, headers: next(answers)
    system.urls["https://cdn.example/files/2.0/T.zip"] = FakeHttp("")

    result = run(system, tmp_path, REDIRECT)

    assert result.version == "2.0"
    assert [request.method for request in system.request_log[:2]] == ["HEAD", "GET"]


def test_a_redirect_that_never_ends_is_an_error(system: FakeSystem, tmp_path: Path) -> None:
    """Two addresses that send the client to each other fail with a message instead of looping."""
    system.urls["https://vendor.example/latest"] = FakeHttp("", 302, {"Location": "https://vendor.example/again"})
    system.urls["https://vendor.example/again"] = FakeHttp("", 302, {"Location": "https://vendor.example/latest"})

    with pytest.raises(TrackError, match="redirects more than"):
        run(system, tmp_path, REDIRECT)


# -- the chain provider: optional steps and the each step ---------------------------------------------------------

OPTIONAL_CHAIN = (
    'format = 2\n[track]\nprovider = "chain"\ndownload = "https://vendor.example/tool-${version}.zip"\n'
    "[[track.fetch]]\nurl = \"https://vendor.example/\"\nmatch = 'tool-(?P<version>\\d+\\.\\d+)\\.zip'\n"
    "[[track.fetch]]\nurl = \"https://vendor.example/${version}.sha256\"\noptional = true\nmatch = '(?P<sha256>[0-9a-f]{64})'\n"
    '[source]\ndownload_url = "${download_url}"\n'
)


@pytest.mark.process
@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        pytest.param(FakeHttp("", 404), "dead", id="the-address-answers-404"),
        pytest.param(FakeHttp("sorry, not available"), "empty", id="the-page-has-no-digest"),
    ],
)
def test_an_optional_chain_step_that_fails_costs_only_what_it_would_have_found(
    system: FakeSystem, tmp_path: Path, answer: FakeHttp, kind: str
) -> None:
    """The version and the address are found without the optional step's digest, and no checksum is invented."""
    system.urls["https://vendor.example/"] = FakeHttp("tool-2.5.zip")
    system.urls["https://vendor.example/2.5.sha256"] = answer

    result = run(system, tmp_path, OPTIONAL_CHAIN)

    assert result.version == "2.5" and result.captures["download_url"] == "https://vendor.example/tool-2.5.zip"
    assert result.checksum is None


@pytest.mark.process
def test_an_optional_chain_step_that_succeeds_supplies_the_checksum(system: FakeSystem, tmp_path: Path) -> None:
    """When the optional step finds a digest, it becomes the checksum of the version."""
    system.urls["https://vendor.example/"] = FakeHttp("tool-2.5.zip")
    system.urls["https://vendor.example/2.5.sha256"] = FakeHttp(f"{DIGEST}  tool-2.5.zip")

    result = run(system, tmp_path, OPTIONAL_CHAIN)

    assert (result.checksum.sha256, result.checksum.sha256_from) == (DIGEST, "vendor-sums")


@pytest.mark.process
def test_a_required_chain_step_that_matches_nothing_fails_the_check(system: FakeSystem, tmp_path: Path) -> None:
    """Only steps marked optional may be skipped: a page without the version fails with a message naming the step."""
    system.urls["https://vendor.example/"] = FakeHttp("nothing here")

    with pytest.raises(TrackError, match="Step 1 of the chain .* matched nothing"):
        run(system, tmp_path, OPTIONAL_CHAIN)


EACH_CHAIN = (
    'format = 2\n[track]\nprovider = "chain"\ndownload = "${link}"\n'
    "[[track.fetch]]\nurl = \"https://vendor.example/\"\nselect = \"each\"\nmatch = '(?P<link>https://vendor.example/get/\\d+)'\n"
    "[[track.fetch]]\nurl = \"${link}\"\nheader = \"Content-Disposition\"\nmatch = 'tool-(?P<version>\\d+\\.\\d+)-windows\\.zip'\n"
    '[source]\ndownload_url = "${download_url}"\n'
)
# What each link of a page answers to a HEAD request: the file name of the system it is for (None: the link is dead).
SYSTEMS = {
    "windows": (1, 'attachment; filename="tool-18.4-windows.zip"'),
    "mac": (2, 'attachment; filename="tool-18.4-mac.dmg"'),
    "linux": (3, 'attachment; filename="tool-18.4-linux.tar.gz"'),
    "gone": (4, None),
}


def page_of_systems(system: FakeSystem, order: tuple[str, ...]) -> str:
    """Serve a home page that links the systems in *order*, each link answering with its own file name; return the Windows link."""
    system.urls["https://vendor.example/"] = FakeHttp(" ".join(f'<a href="https://vendor.example/get/{SYSTEMS[name][0]}">{name}</a>' for name in order))
    for number, disposition in SYSTEMS.values():
        system.urls[f"https://vendor.example/get/{number}"] = (
            FakeHttp("", 200, {"Content-Disposition": disposition}) if disposition else FakeHttp("", 404)
        )
    return "https://vendor.example/get/1"


@pytest.mark.process
@pytest.mark.parametrize(
    "order",
    [
        pytest.param(("windows", "mac", "linux"), id="the-first-link-is-the-one"),
        pytest.param(("mac", "linux", "windows"), id="after-links-for-other-systems"),
        pytest.param(("gone", "mac", "windows", "linux"), id="after-a-dead-link"),
    ],
)
def test_an_each_step_keeps_the_first_link_that_the_later_steps_accept(system: FakeSystem, tmp_path: Path, order: tuple[str, ...]) -> None:
    """The Windows link is chosen from any position; links before it that fail or name another system are skipped, none after it is fetched."""
    windows = page_of_systems(system, order)

    result = run(system, tmp_path, EACH_CHAIN)

    assert result.version == "18.4" and result.captures["download_url"] == windows
    heads = requested(system, "HEAD")
    assert len(heads) == order.index("windows") + 1 and heads[-1] == windows


@pytest.mark.process
def test_an_each_step_fails_closed_when_no_link_gets_through(system: FakeSystem, tmp_path: Path) -> None:
    """With only other systems linked the check fails and says how many candidates were tried; each was tried."""
    page_of_systems(system, ("mac", "linux", "gone"))

    with pytest.raises(TrackError, match="None of the 3 candidates of step 1"):
        run(system, tmp_path, EACH_CHAIN)

    assert len(requested(system, "HEAD")) == 3


@pytest.mark.process
def test_an_each_step_tries_a_link_that_the_page_repeats_only_once(system: FakeSystem, tmp_path: Path) -> None:
    """A link that appears twice on the page (in an href and in its text) is one candidate, not two requests."""
    page_of_systems(system, ("mac",))
    system.urls["https://vendor.example/"] = FakeHttp('<a href="https://vendor.example/get/2">https://vendor.example/get/2</a>')

    with pytest.raises(TrackError, match="None of the 1 candidates"):
        run(system, tmp_path, EACH_CHAIN)

    assert len(requested(system, "HEAD")) == 1


@pytest.mark.process
def test_an_each_step_stops_after_the_candidate_cap(system: FakeSystem, tmp_path: Path) -> None:
    """More links than the cap are not all fetched: a Windows link placed after the cap is never reached and the check fails."""
    links = [f"https://vendor.example/get/{1000 + number}" for number in range(MAX_EACH_CANDIDATES + 5)]
    system.urls["https://vendor.example/"] = FakeHttp(" ".join(f'<a href="{link}">x</a>' for link in links))
    for link in links[:-1]:
        system.urls[link] = FakeHttp("", 200, {"Content-Disposition": 'attachment; filename="tool-18.4-mac.dmg"'})
    system.urls[links[-1]] = FakeHttp("", 200, {"Content-Disposition": 'attachment; filename="tool-18.4-windows.zip"'})

    with pytest.raises(TrackError, match="candidates"):
        run(system, tmp_path, EACH_CHAIN)

    assert len(requested(system, "HEAD")) == MAX_EACH_CANDIDATES


@pytest.mark.process
def test_a_regex_deadline_while_trying_candidates_stops_the_check(system: FakeSystem, tmp_path: Path) -> None:
    """A pattern that runs out of time is an error, never taken for "this candidate does not match" and followed by the next one."""
    page_of_systems(system, ("mac", "windows"))
    calls: list[int] = []

    def worker(call: ProcessCall) -> ProcessResult:
        """Run the first pattern for real; the second one runs out of time."""
        calls.append(1)
        if len(calls) >= 2:
            raise TimeoutError("deadline")
        return real_regex_worker(call)

    system.processes["regex-worker"] = worker

    with pytest.raises(TrackError):
        run(system, tmp_path, EACH_CHAIN)

    assert len(requested(system, "HEAD")) == 1


# -- the checksum a tracker names ------------------------------------------------------------------------------------

SUMS_TRACKER = (
    'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://vendor.example/page"\n'
    "match = 'tool-(?P<version>\\d+(?:\\.\\d+)+)\\.zip'\n"
    '[source]\ndownload_url = "https://vendor.example/tool-${version}.zip"\n'
    '[source.checksum]\nsums_file = "https://vendor.example/${version}/SHA256SUMS"\nentry = "tool-${version}.zip"\n@OPTIONAL@'
)


@pytest.mark.process
def test_a_configured_checksum_source_gives_the_version_that_was_found_its_digest(system: FakeSystem, tmp_path: Path) -> None:
    """`[source.checksum]` is read for the version the provider found: the entry of that version's file is the checksum."""
    system.urls[PAGE] = FakeHttp("tool-1.9.zip tool-2.0.zip")
    system.urls["https://vendor.example/2.0/SHA256SUMS"] = FakeHttp(f"{'0' * 64}  tool-1.9.zip\n{DIGEST}  tool-2.0.zip\n")

    result = run(system, tmp_path, SUMS_TRACKER.replace("@OPTIONAL@", ""))

    assert result.version == "2.0" and (result.checksum.sha256, result.checksum.sha256_from) == (DIGEST, "vendor-sums")


@pytest.mark.process
def test_a_configured_checksum_source_without_a_digest_is_an_error_not_a_silent_skip(system: FakeSystem, tmp_path: Path) -> None:
    """When `[source.checksum]` names a sums file that lacks the entry, the check fails and says where it looked."""
    system.urls[PAGE] = FakeHttp("tool-2.0.zip")
    system.urls["https://vendor.example/2.0/SHA256SUMS"] = FakeHttp("0" * 64 + "  other.exe\n")

    with pytest.raises(ChecksumDiscoveryError, match="SHA256SUMS"):
        run(system, tmp_path, SUMS_TRACKER.replace("@OPTIONAL@", ""))


@pytest.mark.process
def test_a_regex_failure_in_an_optional_checksum_is_not_swallowed(system: FakeSystem, tmp_path: Path) -> None:
    """An optional checksum source that runs a pattern out of time stops the check instead of silently dropping the digest."""
    system.urls[PAGE] = FakeHttp("tool-2.0.zip")
    calls: list[int] = []

    def worker(call: ProcessCall) -> ProcessResult:
        """Run the version pattern for real; the checksum pattern runs out of time."""
        calls.append(1)
        if len(calls) >= 2:
            raise TimeoutError("deadline")
        return real_regex_worker(call)

    system.processes["regex-worker"] = worker
    text = (
        'format = 2\n[track]\nprovider = "page"\ncheck_url = "https://vendor.example/page"\n'
        "match = 'tool-(?P<version>\\d+(?:\\.\\d+)+)\\.zip'\n"
        '[source]\ndownload_url = "https://vendor.example/tool-${version}.zip"\n'
        "[source.checksum]\npage_match = '(?P<sha256>[0-9a-f]{64})'\noptional = true\n"
    )

    with pytest.raises(Exception, match="was stopped"):
        run(system, tmp_path, text)


# -- responses a provider cannot use ------------------------------------------------------------------------------


@pytest.mark.process
@pytest.mark.parametrize(
    ("kind", "address", "answer", "message"),
    [
        pytest.param("json", "https://vendor.example/releases.json", FakeHttp("{}"), "No record", id="json-without-a-version"),
        pytest.param("page", "https://vendor.example/download", FakeHttp("<html>nothing</html>"), "matched nothing", id="page-without-a-match"),
        pytest.param("github-latest", "https://api.github.com/repos/vendor/tool/releases/latest",
                     FakeHttp('{"tag_name": "v1", "assets": []}'), "no release with a usable version", id="github-release-without-the-asset"),
        pytest.param("github-latest", "https://api.github.com/repos/vendor/tool/releases/latest", FakeHttp("", 404),
                     "Could not read the releases", id="github-answers-404"),
        pytest.param("rss", "https://vendor.example/rss", FakeHttp("<rss><channel>"), "not well-formed", id="rss-that-is-not-xml"),
        pytest.param("rss", "https://vendor.example/rss", FakeHttp('<!DOCTYPE rss [<!ENTITY x "y">]><rss/>'),
                     "document type declaration", id="rss-with-a-doctype"),
        pytest.param("last-modified", "https://vendor.example/files/Tool.zip", FakeHttp("", 200), "reports no Last-Modified", id="file-without-a-date"),
    ],
)
def test_a_provider_reports_a_clear_error_for_a_response_it_cannot_use(
    system: FakeSystem, tmp_path: Path, kind: str, address: str, answer: FakeHttp, message: str
) -> None:
    """A response that lacks what the pattern, path, asset or header needs fails with a message, never with a guess."""
    vendor = VENDORS[kind]
    vendor.serve(system)
    system.urls[address] = answer

    with pytest.raises(TrackError, match=message):
        run(system, tmp_path, vendor.tracker_text())


def test_a_version_the_page_supplies_must_be_a_safe_directory_name(system: FakeSystem, tmp_path: Path) -> None:
    """A vendor document that "finds" a version with path characters is refused instead of becoming a directory name."""
    text = (
        'format = 2\n[track]\nprovider = "json"\ncheck_url = "https://vendor.example/latest.json"\nversion = "$.latest"\n'
        '[source]\ndownload_url = "https://vendor.example/${version}.zip"\n'
    )
    system.urls["https://vendor.example/latest.json"] = json_response({"latest": "..\\evil"})

    with pytest.raises(TrackError, match="version"):
        run(system, tmp_path, text)

    system.urls["https://vendor.example/latest.json"] = json_response({"latest": "1.2.3"})
    assert run(system, tmp_path, text).version == "1.2.3"
