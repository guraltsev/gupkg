"""Cover the pure checksum policy: digests, URL judgement, the gate, receipt records and the plan marking.

Behaviour covered: digest normalisation in one place (case, whitespace, `sha256:` prefix) and
the refusal of every malformed digest; `is_https_url` on ordinary and hostile spellings; the
rows of the policy table of design 9.1 at the gate; the decision values of `--skip-checksum`;
the receipt record (every decision value, required fields, strict parsing, the unverified note);
the git gate; the `sha256: present|missing` plan entry; and the `ignore_checksum` removal message.

Real: the pure ``gupkg.model.checksum`` and ``gupkg.model.manifest``. Mocked: nothing. Out of scope:
downloads (``test_checksum_fetch``), the manager (``test_manager_policy``) and pinning (``test_pinning``).
"""

from __future__ import annotations

import pytest

from gupkg.model.checksum import (
    DECISIONS,
    DOWNLOAD_DECISIONS,
    ORIGIN_NOT_VERIFIED,
    REGISTRY_FINGERPRINT,
    SKIPPED_CLI,
    SKIPPED_SETTING,
    USER_APPROVED,
    VERIFIED,
    ChecksumInvalid,
    ChecksumMismatch,
    ChecksumMissing,
    ChecksumRecord,
    check_before_download,
    git_gate,
    git_record,
    hashes_equal,
    is_https_url,
    normalize_sha256,
    parse_checksum_record,
    parse_skip_flag,
    plan_checksum,
    unverified_note,
    verify_observed,
)
from gupkg.model.manifest import ManifestError, parse_manifest

DIGEST = "ab" * 32
URL = "https://example.org/tool.zip"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(DIGEST, DIGEST, id="plain"),
        pytest.param(DIGEST.upper(), DIGEST, id="upper-case"),
        pytest.param("  " + DIGEST + "\r\n", DIGEST, id="surrounding-whitespace"),
        pytest.param("sha256:" + DIGEST, DIGEST, id="prefix"),
        pytest.param("SHA256:" + DIGEST.upper(), DIGEST, id="prefix-upper-case"),
        pytest.param("sha256: " + DIGEST, DIGEST, id="prefix-then-space"),
    ],
)
def test_digests_normalise_in_one_place(text: str, expected: str) -> None:
    """Case, whitespace and a `sha256:` prefix are removed, and nothing else is."""
    assert normalize_sha256(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(DIGEST[:63], id="63-digits"),
        pytest.param(DIGEST + "a", id="65-digits"),
        pytest.param(DIGEST[:-1] + "z", id="non-hex"),
        pytest.param(DIGEST + " " + DIGEST, id="two-digests"),
        pytest.param(DIGEST + "\n" + DIGEST, id="two-lines"),
        pytest.param("sha256:sha256:" + DIGEST, id="double-prefix"),
        pytest.param("sha1:" + DIGEST, id="other-algorithm"),
        pytest.param("0x" + DIGEST, id="hex-prefix"),
        pytest.param("", id="empty"),
        pytest.param(DIGEST[:32] + " " + DIGEST[32:], id="space-inside"),
        pytest.param("\uff21" * 64, id="full-width-letters"),
    ],
)
def test_a_malformed_digest_is_refused(text: str) -> None:
    """A wrong length, a non-hex character, several digests or another algorithm raises; nothing is truncated."""
    with pytest.raises(ChecksumInvalid):
        normalize_sha256(text)
    assert hashes_equal(text, DIGEST) is False


def test_a_non_string_digest_is_refused() -> None:
    """Bytes, numbers and `None` are not digests."""
    for value in (None, 5, DIGEST.encode(), [DIGEST]):
        with pytest.raises(ChecksumInvalid):
            normalize_sha256(value)  # type: ignore[arg-type]


def test_equal_digests_compare_equal_whatever_their_spelling() -> None:
    """The comparison normalises both sides and compares all 64 digits."""
    assert hashes_equal(DIGEST, "SHA256:" + DIGEST.upper())
    assert not hashes_equal(DIGEST, DIGEST[:-1] + "c")


@pytest.mark.parametrize(
    ("url", "https"),
    [
        pytest.param("https://example.org/a", True, id="plain"),
        pytest.param("HTTPS://EXAMPLE.org/a", True, id="upper-case"),
        pytest.param("https://example.org:8443/a?x=1#y", True, id="port-query-fragment"),
        pytest.param("https://[2001:db8::1]/a", True, id="ipv6-host"),
        pytest.param("http://example.org/a", False, id="http"),
        pytest.param("file:///C:/a", False, id="file"),
        pytest.param("file://host/share/a", False, id="file-with-host"),
        pytest.param("\\\\?\\C:\\a", False, id="extended-length-path"),
        pytest.param("\\\\server\\share\\a", False, id="unc"),
        pytest.param("https:/example.org/a", False, id="one-slash"),
        pytest.param("https:example.org/a", False, id="no-slashes"),
        pytest.param("https:\\\\example.org\\a", False, id="backslashes"),
        pytest.param("https://good@evil/a", False, id="user-information"),
        pytest.param("https://user:pw@example.org/a", False, id="user-and-password"),
        pytest.param("https://", False, id="no-host"),
        pytest.param("https:///a", False, id="empty-host"),
        pytest.param("https://example.org:port/a", False, id="bad-port"),
        pytest.param("https://example.org/a b", False, id="space"),
        pytest.param("https://example.org/a\tb", False, id="tab"),
        pytest.param("https://example.org/a\nb", False, id="line-break"),
        pytest.param("https://example.org/a\u2028b", False, id="unicode-line-separator"),
        pytest.param(" https://example.org/a", False, id="leading-space"),
        pytest.param("xhttps://example.org/a", False, id="prefix-text"),
        pytest.param("", False, id="empty"),
    ],
)
def test_is_https_url_judges_the_parsed_address(url: str, https: bool) -> None:
    """Only a plain `https://host...` address counts; every odd spelling does not."""
    assert is_https_url(url) is https


def test_is_https_url_refuses_non_text() -> None:
    """A non-string is never https."""
    assert is_https_url(None) is False and is_https_url(b"https://x/") is False


@pytest.mark.parametrize(
    ("value", "decision"),
    [
        pytest.param(None, None, id="absent"),
        pytest.param("", SKIPPED_CLI, id="bare-flag"),
        pytest.param("cli", SKIPPED_CLI, id="cli"),
        pytest.param("setting", SKIPPED_SETTING, id="setting"),
        pytest.param("user-approved", USER_APPROVED, id="user-approved"),
        pytest.param(" Setting ", SKIPPED_SETTING, id="spaces-and-case"),
    ],
)
def test_skip_flag_values_name_their_decisions(value: str | None, decision: str | None) -> None:
    """The bare flag is `skipped-cli`; the manager relays `setting` or `user-approved`."""
    assert parse_skip_flag(value) == decision


@pytest.mark.parametrize("value", ["verified", "yes", "true", "origin-not-verified", "skipped-cli"])
def test_an_unknown_skip_reason_is_refused_not_read_as_the_bare_flag(value: str) -> None:
    """Fail closed: a typo never silently becomes a skip."""
    with pytest.raises(ChecksumInvalid):
        parse_skip_flag(value)


GATE = [
    pytest.param(DIGEST, None, URL, VERIFIED, id="present-https-no-flag"),
    pytest.param(DIGEST, SKIPPED_CLI, URL, VERIFIED, id="present-https-flag-is-ignored"),
    pytest.param(DIGEST, None, "http://example.org/a", VERIFIED, id="present-http"),
    pytest.param(None, SKIPPED_CLI, URL, SKIPPED_CLI, id="missing-https-flag"),
    pytest.param(None, SKIPPED_SETTING, URL, SKIPPED_SETTING, id="missing-https-manager-skip"),
    pytest.param(None, USER_APPROVED, URL, USER_APPROVED, id="missing-https-manager-ask-yes"),
    pytest.param(None, None, URL, "refuse-skippable", id="missing-https-no-flag"),
    pytest.param(None, None, "http://example.org/a", "refuse", id="missing-http-no-flag"),
    pytest.param(None, SKIPPED_CLI, "http://example.org/a", "refuse", id="missing-http-flag"),
    pytest.param(None, SKIPPED_CLI, "file:///C:/a.zip", "refuse", id="missing-file-url-flag"),
    pytest.param(None, SKIPPED_CLI, "https://good@evil/a", "refuse", id="missing-userinfo-flag"),
]


@pytest.mark.parametrize(("expected", "skip", "url", "outcome"), GATE)
def test_the_gate_applies_the_rows_of_the_policy_table(expected: str | None, skip: str | None, url: str, outcome: str) -> None:
    """Design 9.1: a hash is verified; with none, only an https address with the flag may go on."""
    if outcome.startswith("refuse"):
        with pytest.raises(ChecksumMissing) as raised:
            check_before_download(url, expected, skip)
        assert raised.value.skippable is (outcome == "refuse-skippable")
        assert ("run this command again with --skip-checksum" in str(raised.value)) is (outcome == "refuse-skippable")
        assert raised.value.to_data()["code"] == "checksum-missing" and raised.value.to_data()["url"] == url
        return
    verification = check_before_download(url, expected, skip)
    assert verification.decision == outcome
    assert (verification.expected is None) is (outcome != VERIFIED)


def test_the_message_for_a_userinfo_url_does_not_print_the_credentials() -> None:
    """A refusal never echoes the user information of an address."""
    with pytest.raises(ChecksumMissing) as raised:
        check_before_download("https://admin:hunter2@example.org/a.zip", None, None)

    assert "hunter2" not in str(raised.value)


def test_the_gate_refuses_a_skip_decision_that_is_not_one() -> None:
    """The gate itself validates the decision it is handed."""
    with pytest.raises(ChecksumInvalid):
        check_before_download(URL, None, VERIFIED)


def test_the_comparison_has_no_way_to_waive_a_mismatch() -> None:
    """`verify_observed` raises for a different digest, also when the call carried a skip (which a declared hash ignores)."""
    verification = check_before_download(URL, DIGEST, SKIPPED_CLI)

    with pytest.raises(ChecksumMismatch) as raised:
        verify_observed(verification, "cd" * 32, url=URL, final_url=URL, size=1)

    assert verification.decision == VERIFIED
    assert raised.value.expected == DIGEST and raised.value.observed == "cd" * 32
    assert raised.value.exit_code == 3


def test_a_skipped_download_served_from_a_non_https_final_address_is_refused() -> None:
    """The final address is judged again after the download, on its parsed form."""
    verification = check_before_download(URL, None, SKIPPED_CLI)

    with pytest.raises(ChecksumMismatch):
        verify_observed(verification, DIGEST, url=URL, final_url="http://example.org/tool.zip", size=1)


def test_a_malformed_observed_digest_is_an_error_not_a_pass() -> None:
    """A client that reported something that is not a digest cannot satisfy any expectation."""
    verification = check_before_download(URL, DIGEST, None)

    with pytest.raises(ChecksumInvalid):
        verify_observed(verification, DIGEST[:-1], url=URL, final_url=URL, size=1)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", DECISIONS)
def test_every_decision_value_makes_a_record_and_round_trips(decision: str) -> None:
    """Each documented value builds a record, serialises to plain data and parses back equal."""
    extra = {"registry": "main"} if decision == REGISTRY_FINGERPRINT else {}
    observed = DIGEST if decision in DOWNLOAD_DECISIONS else None
    record = ChecksumRecord(decision, url=URL, final_url=URL, observed_sha256=observed, size=3 if observed else None, **extra)

    assert parse_checksum_record(record.to_data()) == record


def test_the_decision_values_are_exactly_the_documented_ones() -> None:
    """Design 9.2 lists six values; there is no `verified-by-bootstrap` (D29)."""
    assert DECISIONS == (
        "verified", "skipped-cli", "skipped-setting", "user-approved", "registry-fingerprint", "origin-not-verified",
    )


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        pytest.param({"decision": "verified-by-bootstrap", "observed_sha256": DIGEST}, "not a checksum decision", id="removed-decision"),
        pytest.param({"decision": "ok", "observed_sha256": DIGEST}, "not a checksum decision", id="unknown-decision"),
        pytest.param({}, "no decision", id="no-decision"),
        pytest.param({"decision": "verified"}, "needs the observed", id="verified-without-a-hash"),
        pytest.param({"decision": "skipped-cli"}, "needs the observed", id="skipped-without-a-hash"),
        pytest.param({"decision": "verified", "observed_sha256": DIGEST[:10]}, "not a SHA-256", id="short-hash"),
        pytest.param({"decision": "registry-fingerprint"}, "names the registry", id="fingerprint-without-registry"),
        pytest.param({"decision": "verified", "observed_sha256": DIGEST, "extra": "x"}, "unknown key", id="unknown-key"),
        pytest.param({"decision": "verified", "observed_sha256": DIGEST, "size": -1}, "non-negative", id="negative-size"),
        pytest.param({"decision": "verified", "observed_sha256": DIGEST, "size": True}, "non-negative", id="boolean-size"),
        pytest.param({"decision": "verified", "observed_sha256": DIGEST, "url": 5}, "must be a string", id="url-not-text"),
        pytest.param({"decision": "verified", "commit": "abc"}, "40-digit", id="short-commit"),
    ],
)
def test_a_malformed_receipt_record_is_refused(data: dict, problem: str) -> None:
    """The receipt's checksum table is read strictly: an unknown decision or a missing hash fails."""
    with pytest.raises(ChecksumInvalid, match=problem):
        parse_checksum_record(data)


def test_no_download_decisions_need_no_hash() -> None:
    """`origin-not-verified` and `registry-fingerprint` (with a registry) carry no download hash."""
    assert ChecksumRecord(ORIGIN_NOT_VERIFIED).verified is False
    assert ChecksumRecord(REGISTRY_FINGERPRINT, registry="main").verified is True


@pytest.mark.parametrize(
    ("decision", "flagged"),
    [
        pytest.param(VERIFIED, False, id="verified"),
        pytest.param(REGISTRY_FINGERPRINT, False, id="registry-fingerprint"),
        pytest.param(SKIPPED_CLI, True, id="skipped-cli"),
        pytest.param(SKIPPED_SETTING, True, id="skipped-setting"),
        pytest.param(USER_APPROVED, True, id="user-approved"),
        pytest.param(ORIGIN_NOT_VERIFIED, True, id="origin-not-verified"),
        pytest.param("something-new", True, id="unknown-decision-is-flagged"),
    ],
)
def test_unverified_installs_are_flagged_and_unknown_decisions_fail_closed(decision: str, flagged: bool) -> None:
    """`list --details` and `check` show a note for every unverified decision, and for any unknown one."""
    assert (unverified_note(decision) is not None) is flagged


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def test_the_git_gate_follows_the_table() -> None:
    """A pinned commit verifies; unpinned needs the flag and https; the commit must be 40 hex digits."""
    assert git_gate("ssh://h/r.git", COMMIT).decision == VERIFIED
    assert git_gate("https://h/r.git", None, SKIPPED_CLI).decision == SKIPPED_CLI
    with pytest.raises(ChecksumMissing):
        git_gate("https://h/r.git", None)
    with pytest.raises(ChecksumMissing):
        git_gate("ssh://h/r.git", None, SKIPPED_CLI)
    with pytest.raises(ChecksumInvalid):
        git_gate("https://h/r.git", "abc123")


@pytest.mark.parametrize("commit", [123, ["a" * 40], b"a" * 40, True], ids=["int", "list", "bytes", "bool"])
def test_a_commit_that_is_not_text_is_an_invalid_checksum_not_an_attribute_error(commit: object) -> None:
    """A non-text commit fails closed with ChecksumInvalid (the plan reports it as `invalid`, never as a crash)."""
    with pytest.raises(ChecksumInvalid, match="text"):
        git_gate("https://h/r.git", commit)


def test_a_git_record_requires_the_pinned_commit() -> None:
    """The resolved commit is recorded; a different one than the pin is a mismatch."""
    gate = git_gate("https://h/r.git", COMMIT)

    assert git_record(gate, COMMIT.upper(), url="https://h/r.git").commit == COMMIT
    with pytest.raises(ChecksumMismatch):
        git_record(gate, "f" * 40, url="https://h/r.git")


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "sha256", "git", "commit", "status", "skippable"),
    [
        pytest.param(URL, DIGEST, False, None, "present", False, id="hash-present"),
        pytest.param(URL, None, False, None, "missing", True, id="hash-missing-https"),
        pytest.param("http://example.org/a", None, False, None, "missing", False, id="hash-missing-http"),
        pytest.param(URL, DIGEST[:-1], False, None, "invalid", False, id="malformed-hash-is-invalid-and-not-skippable"),
        pytest.param(URL, DIGEST + " " + DIGEST, False, None, "invalid", False, id="two-digests-is-invalid"),
        pytest.param(URL, "", False, None, "invalid", False, id="empty-hash-is-invalid"),
        pytest.param(URL, 5, False, None, "invalid", False, id="non-text-hash-is-invalid"),
        pytest.param("https://h/r.git", None, True, "abc", "invalid", False, id="git-short-commit-is-invalid"),
        pytest.param("https://h/r.git", None, True, 5, "invalid", False, id="git-non-text-commit-is-invalid"),
        pytest.param("https://h/r.git", None, True, COMMIT, "present", False, id="git-with-commit"),
        pytest.param("https://h/r.git", None, True, None, "missing", True, id="git-ref-only"),
        pytest.param("ssh://h/r.git", None, True, None, "missing", False, id="git-ssh-ref-only"),
    ],
)
def test_the_plan_marks_present_or_missing_with_the_url_and_its_source(
    url: str, sha256: str | None, git: bool, commit: str | None, status: str, skippable: bool
) -> None:
    """`--plan --json` can say `sha256: present|missing` and whether the flag could help, before any download."""
    entry = plan_checksum(url, sha256, url_from="tracker", commit=commit, git=git)

    assert entry.to_data() == {"sha256": status, "url": url, "url_from": "tracker", "skippable": skippable}


# ---------------------------------------------------------------------------
# ignore_checksum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["[source]", "[source.checksum]"])
def test_ignore_checksum_is_not_part_of_the_format_and_the_message_says_what_to_do(table: str) -> None:
    """The removed key is an error that points at `sha256` and `--skip-checksum`."""
    text = f'format = 2\n[source]\ndownload_url = "{URL}"\n' + ("" if table == "[source]" else "[source.checksum]\nsidecar = \"x\"\n") + "ignore_checksum = true\n"

    with pytest.raises(ManifestError) as raised:
        parse_manifest(text)

    assert "ignore_checksum was removed" in str(raised.value) and "--skip-checksum" in str(raised.value)
