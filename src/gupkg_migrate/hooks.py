"""Recognise 0.17 hook code that a built-in service now replaces.

``scan_hooks(directory)`` reads the Python files of a package's ``pkg.local`` and
returns a ``HookFinding`` for every known service pattern with the declarative
replacement to use. The scan only reports: it never edits or deletes a hook.

The patterns are the recurring ones of design section 8 (version discovery, unpacking,
preparation and checksum discovery). A finding is a hint for the author, not a proof
that the hook can be deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HookFinding:
    """One known service pattern found in one hook file."""

    file: str
    service: str
    suggestion: str


# (regular expression over the hook's text, service, suggested replacement)
_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"api\.github\.com|/releases/latest", "track: github", '[track] provider = "github", repo = "<owner>/<name>", asset = "..."'),
    (r"gitlab", "track: gitlab", '[track] provider = "gitlab"'),
    (r"Last-Modified|last_modified", "track: last_modified", '[track] provider = "last_modified", check_url = "...", format = "%Y.%m.%d"'),
    (r"<rss|<feed|<item>|rss", "track: rss", '[track] provider = "rss", check_url = "...", match = "..."'),
    (r"\.geturl\(\)|HTTPRedirectHandler|allow_redirects", "track: redirect", '[track] provider = "redirect", check_url = "...", match = "..."'),
    (r"json\.loads?\(", "track: json", '[track] provider = "json", check_url = "...", version = "$.path"'),
    (r"re\.(findall|finditer|search)\(", "track: page", '[track] provider = "page", check_url = "...", match = "(?P<version>...)"'),
    (r"SHA256SUMS|sha256sums|sha256_sums|\.sha256\b|hashlib\.sha256", "checksum discovery", "[source.checksum] sidecar, sums_file/entry, json/path or page_match"),
    (r"zipfile\.ZipFile|extractall\(", "unpack: zip", '[source] kind = "zip" (inferred), strip, subdir, [[source.extract]]'),
    (r"shutil\.move\(|os\.replace\(|\.rename\(", "prepare: rename or strip", "[[prepare]] rename = { from, to } or [source] strip = N"),
    (r"shutil\.copy2?\(", "prepare: copy", "[[prepare]] copy = { from, to }"),
    (r"os\.remove\(|\.unlink\(|shutil\.rmtree\(", "prepare: remove", "[[prepare]] remove = [...]"),
    (r"7z\b|seven_zip|7za?\.exe", "unpack: 7z/sfx/nsis/tar", '[source] kind = "7z" | "sfx" | "nsis" | "tar"'),
)


def scan_hooks(directory: Path) -> list[HookFinding]:
    """Report the known service patterns in a package directory's hooks.

    Parameters
    ----------
    directory : Path
        The directory that holds ``pkg.local``.

    Returns
    -------
    list of HookFinding
        One finding per matching pattern and file, in file and pattern order. An
        unreadable file is skipped.
    """
    findings: list[HookFinding] = []
    local = Path(directory) / "pkg.local"
    if not local.is_dir():
        return findings
    for module in sorted(local.rglob("*.py")):
        try:
            text = module.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        relative = module.relative_to(directory).as_posix()
        for pattern, service, suggestion in _PATTERNS:
            if re.search(pattern, text):
                findings.append(HookFinding(relative, service, suggestion))
    return findings
