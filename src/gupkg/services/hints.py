"""Recognise hook code that a built-in service now replaces, and suggest the format 2 table.

``gupkg pkg check`` reports a hook that matches a known service pattern together with the
replacement to write; the separate migration tool offers the same suggestions when it
converts a package. The scan only reads: it never edits or deletes a hook, and a hint is
advice for the author, not proof that the hook can go.

Usage and API
-------------
``scan_text(text, file_name)`` returns the ``Hint`` list for one hook's source text and
``scan_directory(directory)`` scans every Python file of a directory's ``pkg.local``.

Implementation Approach
-----------------------
The patterns are the recurring ones of design section 8. Version-discovery patterns apply
only to a file that reads from the network, so a download address in an unpack hook is not
mistaken for a tracker. Where the text names a repository or a URL the
suggestion fills it in. All patterns are anchored on short literals and run in linear time
over source files of a few kilobytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_SCAN_BYTES = 400_000


@dataclass(frozen=True)
class Hint:
    """One known service pattern found in one hook file."""

    file: str
    service: str
    suggestion: str


# A hook that reads from the network is a version-discovery candidate; one that does not is an unpack or
# prepare candidate. Entry-point names are deliberately not used: they differ between hook generations.
_NETWORK = re.compile(r"urllib\.request|urlopen|urlretrieve|requests\.|http\.client|fetch_text|fetch_json|resolve_redirects")
_GITHUB = re.compile(r"api\.github\.com/repos/(?P<repo>[\w.\-]+/[\w.\-]+)/releases")
_GITLAB = re.compile(r"gitlab\.com/api/v4/projects/(?P<project>[\w.\-%]+)/releases")
_URL = re.compile(r"https?://[^\s\"'<>)]+")

# (applies to, pattern, service, suggestion). ``applies to`` is "discovery" for a file that reads
# from the network, "unpack" for any file, or "any".
_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    ("discovery", r"Last-Modified|last_modified", "track: last_modified",
     '[track] provider = "last_modified", check_url = "<url>", format = "%Y.%m.%d"'),
    ("discovery", r"\.geturl\(\)|HTTPRedirectHandler|allow_redirects|redirect_request", "track: redirect",
     '[track] provider = "redirect", check_url = "<url>", match = "/(?P<version>\\d+(?:\\.\\d+)+)/"'),
    ("discovery", r"json\.loads?\(", "track: json",
     '[track] provider = "json", check_url = "<url>", version = "$.path.to.version"'),
    ("discovery", r"<rss|<feed|<item>|ElementTree|/rss\b", "track: rss",
     '[track] provider = "rss", check_url = "<feed url>", match = "(?P<version>\\d+(?:\\.\\d+)+)"'),
    ("discovery", r"\b_[A-Z_]*(?:DIRECTORY|FOLDER|SERIES|INDEX)\b", "track: index",
     '[track] provider = "index", check_url = "<listing url>", [[track.level]] match = "href=\\"(?P<version>\\d+(?:\\.\\d+)+)/\\""'),
    ("discovery", r"re\.(?:compile|findall|finditer|search)\(", "track: page",
     '[track] provider = "page", check_url = "<url>", match = "(?P<version>\\d+(?:\\.\\d+)+)"'),
    ("any", r"SHA256SUMS|sha256sums|sha256_sums|\.sha256\b|hashlib\.sha256|_DIGEST|_SHA256", "checksum discovery",
     "[source.checksum] sidecar = \"${download_url}.sha256\", or sums_file/entry, json/path, or page_match"),
    ("unpack", r"find_7z|seven_zip|7za?\.exe", "unpack: 7z, sfx, nsis or tar",
     '[source] kind = "7z" | "sfx" | "nsis" | "tar" (inferred from the file)'),
    ("unpack", r"zipfile\.ZipFile|extractall\(", "unpack: zip",
     '[source] kind = "zip" (inferred), strip, subdir, [[source.extract]]'),
    ("unpack", r"iterdir\(\)[^\n]*\n[^\n]*len\(\w+\)\s*==\s*1", "unpack: strip",
     '[source] strip = 1 (or strip = "auto")'),
    ("unpack", r"shutil\.rmtree|\.unlink\(|os\.remove\(", "prepare: remove",
     '[[prepare]] remove = ["<path>", ...]'),
    ("unpack", r"shutil\.copy(?:file|2)?\(", "prepare: copy",
     '[[prepare]] copy = { from = "<file>", to = ["<name>", ...] }'),
    ("unpack", r"\.rename\(|os\.replace\(|shutil\.move\(", "prepare: rename",
     '[[prepare]] rename = { from = "<glob>", to = "<name>" } or [source] strip / subdir'),
)


def scan_text(text: str, file_name: str = "") -> list[Hint]:
    """Return the service hints for one hook's source *text*.

    Parameters
    ----------
    text : str
        The Python source of a hook module (at most ``MAX_SCAN_BYTES`` characters are read).
    file_name : str
        The name to put on each hint.
    """
    text = text[:MAX_SCAN_BYTES]
    is_discovery = _NETWORK.search(text) is not None
    hints: list[Hint] = []

    # Version discovery gets exactly one hint: the most specific provider that fits. A
    # GitHub or GitLab release client wins over everything else the hook also does.
    if is_discovery:
        github, gitlab = _GITHUB.search(text), _GITLAB.search(text)
        if github:
            hints.append(Hint(file_name, "track: github",
                              f'[track] provider = "github", repo = "{github.group("repo")}", asset = "<asset name with ${{version}}>"'))
        elif gitlab:
            hints.append(Hint(file_name, "track: gitlab",
                              f'[track] provider = "gitlab", project = "{gitlab.group("project").replace("%2F", "/")}", asset_pattern = "<regex>"'))
    for scope, pattern, service, suggestion in _PATTERNS:
        if scope == "discovery" and (not is_discovery or any(h.service.startswith("track:") for h in hints)):
            continue
        if not re.search(pattern, text):
            continue
        filled = suggestion
        if "<url>" in suggestion:
            found = next((u for u in _URL.findall(text) if "api.github.com" not in u), None)
            filled = suggestion.replace("<url>", found.rstrip(",.;") if found else "<url>")
        hints.append(Hint(file_name, service, filled))
    return hints


def scan_directory(directory: Path) -> list[Hint]:
    """Return the service hints for every Python file below ``<directory>/pkg.local``.

    Unreadable files are skipped. Hints come in file order, then pattern order.
    """
    found: list[Hint] = []
    local = Path(directory) / "pkg.local"
    if not local.is_dir():
        return found
    for module in sorted(local.rglob("*.py")):
        try:
            with open(module, "rb") as handle:
                text = handle.read(MAX_SCAN_BYTES).decode("utf-8-sig", errors="replace")
        except OSError:
            continue
        found.extend(scan_text(text, module.relative_to(directory).as_posix()))
    return found
