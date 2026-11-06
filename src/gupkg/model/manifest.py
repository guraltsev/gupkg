"""Parse and validate ``pkg.toml`` format 2 into typed, frozen data (design sections 4, 7 and 9.3).

A ``pkg.toml`` describes one version directory (a pinned source, preparation steps, shortcuts,
environment values, ``PATH`` entries and shims) or a tracker (``_check_updates``) that discovers
new versions. Invalid files are refused with every problem listed, and a tracker's discovery
result can be rendered into a pinned version manifest.

Usage and API
-------------
``parse_manifest(text, kind=ManifestKind.VERSION)`` turns the text of a
``pkg.toml`` into a frozen ``Manifest`` and raises ``ManifestError`` listing
every problem when it is invalid. ``kind`` is ``VERSION`` for a version
directory and ``TRACKER`` for ``_check_updates``. ``render_pinned(tracker,
track_result)`` writes the version a tracker discovered. ``manifest_to_data``
and ``manifest_to_toml`` turn a manifest back into plain data and TOML text.

Implementation Approach
-----------------------
Parsing is strict. Every table rejects unknown keys with a "did you mean" hint
and every problem is collected before the error is raised. A version directory
describes exactly one source of its own ``App`` and carries no update discovery:
``[track]`` and ``${version}`` style templates are valid only in a tracker.
``[source]`` names how ``App`` is populated (``download_url``, a custom
``script`` or neither) and ``shipped`` says whether gupkg may drop and rebuild
it. The module is pure: it reads no files, so checks that need the directory
(missing ``App``, hook files) live in ``gupkg.pkgcheck``.
"""

from __future__ import annotations

import difflib
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PureWindowsPath
from typing import Any

import tomli_w

from ..tomlio import dumps_tables

from .template import (
    INSTALLER_VARIABLES,
    PATH_VARIABLES,
    TemplateError,
    is_version_template,
    path_variables,
    references,
    render,
)
from .trackspec import TrackSpecError, check_header_map, parse_spec
from .version import TRACKER_DIR_NAME, compare_versions, is_version_name

# The only ``format`` this gupkg reads.
FORMAT = 2
# The install modes a package may list.
MODES = ("system", "user")
# A package is portable when its ``modes`` is exactly ``["portable"]``: it installs in the user root only (issue 035 4.6).
PORTABLE = "portable"
# The modes of a package that does not state ``modes``.
DEFAULT_MODES = ("system", "user")
# The archive or file kinds ``[source] kind`` accepts.
SOURCE_KINDS = ("zip", "7z", "tar", "sfx", "nsis", "msi", "exe", "file", "git")
# Where a declared ``sha256`` may say it came from.
SHA256_FROM = ("github-digest", "vendor-sums", "registry", "manual")
# The origins a tracker may discover a checksum from.
DISCOVERED_SHA256_FROM = ("github-digest", "vendor-sums")
# The file types a ``pkg.local`` script may have.
SCRIPT_SUFFIXES = (".py", ".ps1", ".cmd", ".bat", ".exe")
# The shim kinds a ``[[shim]] type`` accepts.
SHIM_TYPES = ("console", "gui")
# The folder of a version directory that holds its hook scripts.
LOCAL_DIR = "pkg.local"
# The message reported for a version whose ``App`` is missing and cannot be rebuilt.
APP_MISSING_ERROR = "App is missing and no way to recover it."
# The label that the plan, the install output and ``list --details`` give a version that runs a vendor installer (design 8.4).
INSTALLER_LABEL = "runs vendor installer"

# Environment variables a package may not set or delete through ``[[env]]``: ``Path`` would replace the whole PATH
# (``[[path]]`` adds one entry the safe way), and the others are the system's own. Compared case-insensitively.
RESERVED_ENV_NAMES = frozenset(
    name.casefold() for name in (
        "Path", "PATHEXT", "ComSpec", "SystemRoot", "SystemDrive", "windir", "TEMP", "TMP", "USERPROFILE", "APPDATA",
        "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
        "CommonProgramFiles(x86)", "CommonProgramW6432", "PSModulePath", "OS", "NUMBER_OF_PROCESSORS", "USERNAME",
        "COMPUTERNAME", "PUBLIC", "HOMEDRIVE", "HOMEPATH", "USERDOMAIN", "LOGONSERVER",
    )
)


# Windows device names, which cannot be file names whatever their extension.
_DEVICE_NAME = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])[ ]*(\..*)?$", re.IGNORECASE)


# The longest file or folder name Windows accepts.
MAX_NAME_PART = 255
# The longest environment variable name a package may use.
MAX_ENV_NAME = 128


def env_name_problem(name: str) -> str | None:
    """Return why *name* is not an environment variable a package may set, or ``None``.

    Refused: an empty or longer than ``MAX_ENV_NAME`` name; ``=``, white space, ``%`` (it would be expanded) and any
    control, format (zero-width), surrogate, private-use or unassigned character; and the system's own names
    (``is_reserved_env_name``).

    Parameters
    ----------
    name : str
        The variable name from an ``[[env]]`` row.

    Returns
    -------
    str or None
        A phrase completing "the name ...", or ``None`` when the name is acceptable.

    Examples
    --------
    Basic usage:

    >>> env_name_problem("MY_TOOL_HOME") is None
    True

    Edge cases:

    >>> env_name_problem("")
    'is empty'
    >>> env_name_problem("A=B")
    "holds '=', '%', white space or an invisible or control character"
    >>> env_name_problem("PATH")
    'is a reserved system variable; use [[path]] to add a PATH entry'
    """
    # Cheap shape checks first, then characters, then the reserved system names.
    if not name:
        return "is empty"
    if len(name) > MAX_ENV_NAME:
        return f"is longer than {MAX_ENV_NAME} characters"
    if re.search(r"[=%\s]", name) or any(unicodedata.category(char).startswith("C") for char in name):
        return "holds '=', '%', white space or an invisible or control character"
    if is_reserved_env_name(name):
        return "is a reserved system variable; use [[path]] to add a PATH entry"
    return None


def name_part_problem(part: str) -> str | None:
    """Return why *part* cannot be one file or folder name on Windows (device name, stream, trailing dot or space), or ``None``.

    ``CON``, ``NUL``, ``aux.lnk`` and ``COM1`` name devices whatever the extension; ``Tool:ads`` names an NTFS
    alternate stream; a trailing dot or space is silently dropped by Windows, so two names would collide.

    Parameters
    ----------
    part : str
        One path segment.

    Returns
    -------
    str or None
        A phrase completing "the segment ...", or ``None`` when the segment is acceptable.

    Examples
    --------
    Basic usage:

    >>> name_part_problem("Tool.lnk") is None
    True

    Edge cases:

    >>> name_part_problem("NUL")
    'is a Windows device name'
    >>> name_part_problem("report.")
    'ends with a dot or a space'
    """
    # Length, forbidden characters, device names and trailing dots or spaces, in that order.
    if len(part) > MAX_NAME_PART:
        return f"is longer than {MAX_NAME_PART} characters"
    if re.search(r'[:*?"<>|\x00-\x1f]', part):
        return "has a character Windows does not allow in a name (including ':', which names an NTFS stream)"
    if _DEVICE_NAME.match(part):
        return "is a Windows device name"
    if part.endswith((".", " ")):
        return "ends with a dot or a space"
    return None


# Every control, format (zero-width, bidirectional), surrogate, private-use and unassigned character, and the line and paragraph separators.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# A path that starts with a drive letter and a separator.
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# A path that starts with one ``%VARIABLE%`` and a separator.
_ENV_PREFIX = re.compile(r"^%[A-Za-z_][A-Za-z0-9_()]*%[\\/]")


def path_entry_shape_problem(text: str, *, template: bool) -> str | None:
    """Return why *text* cannot be one ``PATH`` entry, or ``None``, judging the text alone.

    A ``PATH`` entry is one absolute local folder: ``X:\\...``. Refused: an empty entry, a ``;`` (one declaration would
    become several entries, and uninstall would not match them), a control character or a quote, a ``..`` segment,
    a UNC, ``\\\\?\\`` or device path, a relative path (a relative entry in the machine ``PATH`` resolves against the current
    directory, so a program could plant itself there), and names Windows treats specially (devices, streams). With
    *template* true the text is the declaration before expansion, so it may start with a variable (``$App``,
    ``${ProgramFiles}``) instead of a drive. No file system is asked.

    Parameters
    ----------
    text : str
        The declared entry.
    template : bool
        Whether *text* is a declaration that may start with a variable.

    Returns
    -------
    str or None
        A phrase completing "the entry ...", or ``None`` when the entry has an acceptable shape.

    Examples
    --------
    Basic usage:

    >>> path_entry_shape_problem("C:/Tools/bin", template=False) is None
    True
    >>> path_entry_shape_problem("$App/bin", template=True) is None
    True

    Edge cases:

    >>> path_entry_shape_problem("C:/Tools/../bin", template=False)
    "has a '..' segment"
    >>> path_entry_shape_problem("bin", template=False)
    'must be an absolute local path with a drive (X:\\...)'
    >>> path_entry_shape_problem("C:/a;C:/b", template=False)
    "holds a ';' (one PATH entry only)"
    """
    # Refuse empty text, hidden characters, separators that would split the entry, and UNC or parent-relative forms.
    if not text.strip():
        return "is empty"
    if any(ord(char) < 32 or char in '"<>|*?' or unicodedata.category(char) in _INVISIBLE_CATEGORIES for char in text):
        return "holds a control, invisible or line-separator character, a quote or one of < > | * ?"
    # One declaration is one entry, and network or device paths are never local folders.
    if ";" in text:
        return "holds a ';' (one PATH entry only)"
    if text.startswith(("\\\\", "//")):
        return "is a UNC or device path"
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    if ".." in parts:
        return "has a '..' segment"
    # ``%LOCALAPPDATA%\bin`` is the Windows way to write a PATH entry that follows an environment variable; it is allowed
    # as a prefix only (the machine PATH refuses it, see the engine), and a second ``%`` is not.
    prefix = _ENV_PREFIX.match(text)
    if ("%" in text[prefix.end():]) if prefix else ("%" in text):
        return "holds a '%' that is not the one environment variable it may start with"
    # The entry must be absolute, and none of its segments may be a name Windows treats specially.
    if not (_DRIVE_PATH.match(text) or prefix or (template and text.startswith("$"))):
        return "must be an absolute local path with a drive (X:\\...)" + (" or start with a variable" if template else "")
    for part in parts[1:]:
        if (problem := name_part_problem(part)) is not None:
            return f"has a segment {part!r} that {problem}"
    return None


def is_reserved_env_name(name: str) -> bool:
    """Return whether *name* is an environment variable a package may not set (``Path``, ``TEMP``, ``PROCESSOR_*`` ...).

    Parameters
    ----------
    name : str
        A variable name.

    Returns
    -------
    bool
        ``True`` for a system-owned name, compared without regard to letter case.

    Examples
    --------
    Basic usage:

    >>> is_reserved_env_name("path")
    True
    >>> is_reserved_env_name("MY_TOOL_HOME")
    False
    """
    folded = name.strip().casefold()
    return folded in RESERVED_ENV_NAMES or folded.startswith("processor_")


# The keys each table of a ``pkg.toml`` accepts; any other key is reported with a hint.
_TOP_KEYS = (
    "format", "description", "project_url", "modes", "requires", "requires_gupkg", "record_manifest",
    "source", "track", "prepare", "portable", "shortcut", "env", "path", "shim",
)
# The keys of ``[source]``.
_SOURCE_KEYS = (
    "download_url", "script", "shipped", "sha256", "sha256_from", "kind", "strip", "subdir",
    "require", "verify_manifest", "checksum", "installer", "git", "nested", "extract", "headers",
)
# The keys of ``[source.checksum]`` (besides ``optional``).
_CHECKSUM_KEYS = ("sidecar", "sums_file", "entry", "json", "path", "page_match")
# The keys of a ``[[shortcut]]`` row.
_SHORTCUT_KEYS = ("name", "target", "arguments", "working_dir", "icon_location", "description", "run_as_admin", "modes")
# The text-valued optional keys of a ``[[shortcut]]`` row.
_SHORTCUT_TEXT_KEYS = ("arguments", "working_dir", "icon_location", "description")
# The keys of a ``[[shim]]`` row.
_SHIM_KEYS = ("name", "target", "type", "arguments", "forward_args", "elevate", "working_dir", "content")
# The step names of a ``[[prepare]]`` row; each row names exactly one.
_PREPARE_KEYS = ("remove", "rename", "copy", "write", "module")

# ``[track]``: the keys every provider takes, and the provider-specific ones of design
# section 8.1, extended by issue 026 with ``version``, ``download``, ``headers`` and the
# json and chain helpers. ``gupkg.model.trackspec`` validates the values.
_TRACK_COMMON = ("provider", "check_url", "repo", "repo_url")
# The provider-specific keys of each ``[track]`` provider.
_TRACK_PROVIDERS: dict[str, tuple[str, ...]] = {
    "github": ("asset", "asset_pattern", "version_from", "tag_pattern", "tag_prefix", "release", "prerelease", "version"),
    "gitlab": ("project", "link_name", "asset_pattern", "tag_pattern", "tag_prefix", "version"),
    "page": ("match", "select", "version", "download", "headers"),
    "index": ("level", "version", "download", "headers"),
    "redirect": ("match", "unwrap_query", "version", "download", "headers"),
    "json": ("records", "filter", "version", "match", "download", "checksum", "captures", "select", "headers"),
    "last_modified": ("format", "headers"),
    "rss": ("match", "select", "version", "download", "headers"),
    "git": ("ref",),
    "chain": ("fetch", "version", "download"),
    "module": ("module",),
}
# Which of the three location keys a provider takes (and needs one of).
_TRACK_LOCATION: dict[str, tuple[str, ...]] = {
    "github": ("repo", "repo_url"),
    "gitlab": ("repo", "repo_url", "project"),
    "git": ("repo_url", "repo"),
    "page": ("check_url",),
    "index": ("check_url",),
    "redirect": ("check_url",),
    "json": ("check_url",),
    "last_modified": ("check_url",),
    "rss": ("check_url",),
    "chain": (),
    "module": (),
}
# The tracking hook a ``module`` provider uses when it names none.
_DEFAULT_TRACK_MODULE = "pkg.local/track.py"


class ManifestKind(Enum):
    """Say whether a ``pkg.toml`` belongs to a version directory or to ``_check_updates``."""

    VERSION = "version"
    TRACKER = "tracker"


# The problem a malformed declared hash is reported as (and recognised by, so that callers can give it a stable error code).
INVALID_SHA256_MESSAGE = "must be 64 hexadecimal characters"


def invalid_checksum_problem(problems: "Sequence[str]") -> bool:
    """Return whether one of the *problems* of a version directory is a malformed ``source.sha256``.

    Parameters
    ----------
    problems : sequence of str
        The problem messages of a version directory.

    Returns
    -------
    bool
        ``True`` when one of them reports a malformed declared hash.
    """
    return any(f"source.sha256: {INVALID_SHA256_MESSAGE}" in problem for problem in problems)


class ManifestError(ValueError):
    """Report that a ``pkg.toml`` is invalid, listing every problem found.

    Parameters
    ----------
    problems : str | list[str] | tuple[str, ...]
        One message or a list of messages.

    Attributes
    ----------
    problems : tuple of str
        One readable message per problem.
    """

    def __init__(self, problems: str | list[str] | tuple[str, ...]) -> None:
        """Build the error from one message or a list of messages."""
        self.problems = (problems,) if isinstance(problems, str) else tuple(problems)
        if len(self.problems) == 1:
            super().__init__(self.problems[0])
        else:
            super().__init__("Invalid pkg.toml:\n  - " + "\n  - ".join(self.problems))


# The words that mark a problem as "this package is for a newer gupkg" (exit code 6, design 10.1 and 21.3).
NEEDS_NEWER_GUPKG = "needs a newer gupkg"

# ``requires_gupkg`` is a minimum version: ``>=1.2``.
_GUPKG_REQUIREMENT = re.compile(r">=\s*([0-9]+(?:\.[0-9]+){0,3})")


class NeedsNewerGupkg(ManifestError):
    """Report that a package is for a newer gupkg than the running one (a newer ``format`` or an unmet ``requires_gupkg``).

    The exit code is 6, which makes a package launcher run the bundled gupkg instead (design 21.3).
    """

    exit_code = 6  # EXIT_NEWER_GUPKG of ``gupkg.model.plan`` (this module cannot import it)


def gupkg_requirement_met(requirement: str, version: str) -> bool:
    """Return whether the running gupkg *version* satisfies a ``requires_gupkg`` text such as ``>=1.2``.

    A requirement that is not of the form ``>=<number>[.<number>]...`` is not met (it is refused when the manifest is parsed).

    Parameters
    ----------
    requirement : str
        The ``requires_gupkg`` text.
    version : str
        The running gupkg's version.

    Returns
    -------
    bool
        ``True`` when *version* is at least the required minimum.

    Examples
    --------
    Basic usage:

    >>> gupkg_requirement_met(">=1.2", "1.10.0")
    True
    >>> gupkg_requirement_met(">=2", "1.9")
    False

    Edge cases:

    >>> gupkg_requirement_met("~1", "1.9")
    False
    """
    match = _GUPKG_REQUIREMENT.fullmatch(requirement.strip())
    return match is not None and compare_versions(version, match.group(1)) >= 0


def require_gupkg_version(requirement: str | None, version: str, label: str) -> None:
    """Raise ``NeedsNewerGupkg`` when the running gupkg *version* does not satisfy a package's ``requires_gupkg``.

    Parameters
    ----------
    requirement : str, optional
        The package's ``requires_gupkg`` (``None`` when it states none).
    version : str
        The running gupkg's version.
    label : str
        What the requirement belongs to, for the message (``firefox 135.0``).

    Raises
    ------
    NeedsNewerGupkg
        If *requirement* is set and the running version does not satisfy it.

    Examples
    --------
    Basic usage:

    >>> require_gupkg_version(">=1.0", "1.5", "tool 2.0") is None
    True
    >>> require_gupkg_version(None, "1.5", "tool 2.0") is None
    True

    Edge cases:

    >>> require_gupkg_version(">=99", "1.5", "tool 2.0")
    Traceback (most recent call last):
        ...
    gupkg.model.manifest.NeedsNewerGupkg: tool 2.0 needs a newer gupkg: it requires gupkg >=99, and this is gupkg 1.5.
    """
    if requirement is not None and not gupkg_requirement_met(requirement, version):
        raise NeedsNewerGupkg(f"{label} {NEEDS_NEWER_GUPKG}: it requires gupkg {requirement}, and this is gupkg {version}.")


# ---------------------------------------------------------------------------
# Typed data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Checksum:
    """How to find the hash of a discovered version (``[source.checksum]``).

    Attributes
    ----------
    sidecar : str | None
        Address template of a sidecar file that lists the digest.
    sums_file : str | None
        Address template of a file that lists digests for several files.
    entry : str | None
        The path to look up in the sums file; it must equal the listed path exactly.
    json : str | None
        Address template of a JSON document that holds the digest.
    path : str | None
        JSONPath of the digest inside that document.
    page_match : str | None
        Regular expression with a ``sha256`` group, run on the page the version came from.
    optional : bool
        Whether a source that holds no digest is waived instead of failing.
    """

    sidecar: str | None = None
    sums_file: str | None = None
    entry: str | None = None
    json: str | None = None
    path: str | None = None
    page_match: str | None = None
    optional: bool = False


@dataclass(frozen=True)
class Installer:
    """A vendor installer run with these arguments (``[source.installer]``).

    Attributes
    ----------
    run : tuple[str, ...]
        The arguments the vendor installer is started with.
    """

    run: tuple[str, ...]


@dataclass(frozen=True)
class GitSource:
    """The ref and the commit that anchor a ``git`` source (``[source.git]``).

    Attributes
    ----------
    ref : str | None
        The branch or tag to check out.
    commit : str | None
        The commit the checkout must resolve to.
    """

    ref: str | None = None
    commit: str | None = None


@dataclass(frozen=True)
class NestedArchive:
    """An inner archive to extract (``[[source.nested]]``).

    Attributes
    ----------
    path : str
        The inner archive, relative to the unpacked tree.
    dest : str
        Where the inner archive is extracted.
    replace : bool
        Whether the inner archive is removed after it is extracted.
    """

    path: str
    dest: str
    replace: bool = False


@dataclass(frozen=True)
class ExtractRule:
    """One glob-mapped partial extraction (``[[source.extract]]``).

    Attributes
    ----------
    src : str
        The glob that selects archive members.
    dest : str
        Where the selected members are placed.
    """

    src: str
    dest: str


@dataclass(frozen=True)
class Source:
    """How this version's ``App`` is obtained (``[source]``).

    ``shipped_declared`` is the key as written (``None`` when absent) and
    ``shipped`` is the effective value.

    Attributes
    ----------
    download_url : str | None
        The address the archive is downloaded from.
    script : str | None
        The populate script in ``pkg.local`` (an alternative to a download).
    shipped_declared : bool | None
        The ``shipped`` key as written, or ``None`` when absent.
    sha256 : str | None
        The declared SHA-256 of the download.
    sha256_from : str | None
        Where the declared hash came from, such as ``manual`` or ``vendor-sums``.
    kind : str | None
        The declared archive kind, or ``None`` to infer it from the file.
    strip : int | str | None
        Levels of a single wrapper folder to remove, or ``auto``.
    subdir : str | None
        The subtree of the unpacked files to keep.
    require : tuple[str, ...]
        Files that must exist after unpacking.
    verify_manifest : str | None
        A manifest shipped inside the artifact that every unpacked file must match.
    checksum : Checksum | None
        How to find the hash of a discovered version.
    installer : Installer | None
        A vendor installer to run instead of unpacking.
    git : GitSource | None
        The ref and commit of a ``git`` source.
    nested : tuple[NestedArchive, ...]
        Inner archives to extract.
    extract : tuple[ExtractRule, ...]
        Glob-mapped partial extraction rules.
    headers : Mapping[str, str]
        Extra HTTP headers for the download.
    populate : str
        Return ``"download"``, ``"script"`` or ``"none"``: what populates ``App``.
    shipped : bool
        Return the effective ``shipped``: declared, else true without a populate script.
    app_kind : str
        Return the label of the ``App`` kind, as ``list --details`` shows it.
    recoverable : bool
        Return whether a missing ``App`` can be rebuilt from this source.
    """

    download_url: str | None = None
    script: str | None = None
    shipped_declared: bool | None = None
    sha256: str | None = None
    sha256_from: str | None = None
    kind: str | None = None
    strip: int | str | None = None
    subdir: str | None = None
    require: tuple[str, ...] = ()
    verify_manifest: str | None = None
    checksum: Checksum | None = None
    installer: Installer | None = None
    git: GitSource | None = None
    nested: tuple[NestedArchive, ...] = ()
    extract: tuple[ExtractRule, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def populate(self) -> str:
        """Return ``"download"``, ``"script"`` or ``"none"``: what populates ``App``."""
        if self.download_url is not None:
            return "download"
        return "script" if self.script is not None else "none"

    @property
    def shipped(self) -> bool:
        """Return the effective ``shipped``: declared, else true without a populate script."""
        if self.shipped_declared is not None:
            return self.shipped_declared
        return self.populate == "none"

    @property
    def app_kind(self) -> str:
        """Return the label of the ``App`` kind, as ``list --details`` shows it."""
        if self.shipped:
            return "shipped (source available)" if self.populate != "none" else "shipped"
        return "script-populated" if self.populate == "script" else "downloaded"

    @property
    def recoverable(self) -> bool:
        """Return whether a missing ``App`` can be rebuilt from this source."""
        return self.populate != "none"


@dataclass(frozen=True)
class Track:
    """How a tracker finds newer versions (``[track]``, ``_check_updates`` only).

    ``options`` holds the provider-specific keys; ``gupkg.model.trackspec`` validates their values.

    Attributes
    ----------
    provider : str
        The tracker provider name.
    check_url : str | None
        The address the tracker checks.
    repo : str | None
        The repository of a hosted provider.
    repo_url : str | None
        The repository URL of a git provider.
    options : Mapping[str, Any]
        The provider-specific keys.
    """

    provider: str
    check_url: str | None = None
    repo: str | None = None
    repo_url: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PrepareRemove:
    """``[[prepare]] remove``: delete these paths from ``App``.

    Attributes
    ----------
    paths : tuple[str, ...]
        The paths removed from ``App``.
    """

    paths: tuple[str, ...]


@dataclass(frozen=True)
class PrepareRename:
    """``[[prepare]] rename``: move the one match of a glob to a new name.

    Attributes
    ----------
    source : str
        The glob whose one match is renamed.
    target : str
        The new name.
    """

    source: str
    target: str


@dataclass(frozen=True)
class PrepareCopy:
    """``[[prepare]] copy``: copy one file to each target.

    Attributes
    ----------
    source : str
        The file to copy.
    targets : tuple[str, ...]
        The destinations.
    """

    source: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class PrepareWrite:
    """``[[prepare]] write``: write a file into ``App``.

    Attributes
    ----------
    path : str
        The file to write.
    content : str
        The text to write.
    """

    path: str
    content: str


@dataclass(frozen=True)
class PrepareModule:
    """``[[prepare]] module``: run a hook module from ``pkg.local``.

    Attributes
    ----------
    module : str
        The hook module in ``pkg.local``.
    """

    module: str


PrepareStep = PrepareRemove | PrepareRename | PrepareCopy | PrepareWrite | PrepareModule


@dataclass(frozen=True)
class Marker:
    """A file the app needs in ``App`` to enter portable mode.

    Attributes
    ----------
    path : str
        The marker file, relative to ``App``.
    content : str | None
        The text to write into it, or ``None`` for an empty file.
    """

    path: str
    content: str | None = None


@dataclass(frozen=True)
class Portable:
    """What a portable package keeps and creates (``[portable]``): the persisted items and the marker files.

    Attributes
    ----------
    persist : tuple[str, ...]
        The items that are kept between versions.
    markers : tuple[Marker, ...]
        The marker files that put the app into portable mode.
    """

    persist: tuple[str, ...] = ()
    markers: tuple[Marker, ...] = ()


@dataclass(frozen=True)
class Shortcut:
    """A Start Menu shortcut (``[[shortcut]]``).

    Attributes
    ----------
    name : str
        The shortcut name.
    target : str
        The file the shortcut starts.
    arguments : str
        The command-line arguments.
    working_dir : str
        The working folder.
    icon_location : str
        The icon file and index.
    description : str
        The shortcut's description.
    run_as_admin : bool
        Whether the shortcut asks for elevation.
    modes : tuple[str, ...] | None
        The modes that create it, or ``None`` for every mode of the package.
    """

    name: str
    target: str
    arguments: str = ""
    working_dir: str = ""
    icon_location: str = ""
    description: str = ""
    run_as_admin: bool = False
    modes: tuple[str, ...] | None = None  # None: every mode of the package


@dataclass(frozen=True)
class EnvVar:
    """An environment variable (``[[env]]``).

    Attributes
    ----------
    name : str
        The variable name.
    value : str
        The variable value.
    """

    name: str
    value: str


@dataclass(frozen=True)
class Shim:
    """A command in the mode's ``bin`` directory (``[[shim]]``).

    Attributes
    ----------
    name : str
        The command name.
    target : str
        The file the command starts.
    type : str
        The kind of command the shim is.
    arguments : tuple[str, ...]
        Fixed arguments placed before the caller's.
    forward_args : bool
        Whether the caller's arguments are passed on.
    elevate : bool
        Whether the command asks for elevation.
    working_dir : str | None
        The working folder, or ``None``.
    content : str | None
        The text of the command file, for a shim that writes one.
    """

    name: str
    target: str = ""
    type: str = "console"
    arguments: tuple[str, ...] = ()
    forward_args: bool = True
    elevate: bool = False
    working_dir: str | None = None
    content: str | None = None


@dataclass(frozen=True)
class Manifest:
    """A validated ``pkg.toml`` of format 2.

    Methods
    -------
    supports
        Return whether the package may be installed in *mode*: it is in ``modes``, or the package is
        portable and the mode is ``user``.
    referenced_hooks
        Return the ``pkg.local`` files that ``[source]`` and ``[[prepare]]`` reference.

    Attributes
    ----------
    kind : ManifestKind
        Whether this is a version manifest or a tracker.
    description : str | None
        A one-line description of the package.
    project_url : str | None
        The project's home page.
    modes : tuple[str, ...]
        The modes the package supports.
    requires : tuple[str, ...]
        The ``requires`` list of other packages.
    requires_gupkg : str | None
        The gupkg version range the package needs.
    record_manifest : bool
        Whether a file manifest of ``App`` is recorded.
    source : Source
        How ``App`` is obtained.
    track : Track | None
        How newer versions are found, for a tracker.
    prepare : tuple[PrepareStep, ...]
        The steps that prepare ``App``.
    portable : Portable | None
        The portable-mode settings, or ``None``.
    shortcuts : tuple[Shortcut, ...]
        The Start Menu shortcuts.
    envs : tuple[EnvVar, ...]
        The environment variables.
    paths : tuple[str, ...]
        The directories added to ``PATH``.
    shims : tuple[Shim, ...]
        The commands placed in the ``bin`` directory.
    is_portable : bool
        Return whether the package is portable (``modes = ["portable"]``): it installs in the user
        root only.
    """

    kind: ManifestKind
    description: str | None = None
    project_url: str | None = None
    modes: tuple[str, ...] = DEFAULT_MODES
    requires: tuple[str, ...] = ()
    requires_gupkg: str | None = None
    record_manifest: bool = True
    source: Source = field(default_factory=Source)
    track: Track | None = None
    prepare: tuple[PrepareStep, ...] = ()
    portable: Portable | None = None
    shortcuts: tuple[Shortcut, ...] = ()
    envs: tuple[EnvVar, ...] = ()
    paths: tuple[str, ...] = ()
    shims: tuple[Shim, ...] = ()

    @property
    def is_portable(self) -> bool:
        """Return whether the package is portable (``modes = ["portable"]``): it installs in the user root only."""
        return self.modes == (PORTABLE,)

    def supports(self, mode: str) -> bool:
        """Return whether the package may be installed in *mode*: it is in ``modes``, or the package is portable and the mode is ``user``.

        Parameters
        ----------
        mode : str
            ``system`` or ``user``.

        Returns
        -------
        bool
            ``True`` when the package supports *mode*.
        """
        return mode == "user" if self.is_portable else mode in self.modes

    def referenced_hooks(self) -> tuple[str, ...]:
        """Return the ``pkg.local`` files that ``[source]`` and ``[[prepare]]`` reference.

        The tracker's own ``[track] module`` is not included: ``render_pinned`` never
        copies a tracking hook into a version directory.

        Returns
        -------
        tuple of str
            The referenced files, each once, in the order they are used.
        """
        names: list[str] = []
        if self.source.script is not None:
            names.append(self.source.script)
        names.extend(step.module for step in self.prepare if isinstance(step, PrepareModule))
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class DiscoveredChecksum:
    """A SHA-256 that the tracker discovered, with where it came from.

    Attributes
    ----------
    sha256 : str
        The discovered SHA-256.
    sha256_from : str
        Where it was found.
    """

    sha256: str
    sha256_from: str


@dataclass(frozen=True)
class TrackResult:
    """What a tracker found: the version, its captures and a discovered checksum.

    Attributes
    ----------
    version : str
        The version that was found.
    captures : Mapping[str, str]
        Named values captured while finding it.
    checksum : DiscoveredChecksum | None
        A checksum discovered with the version, or ``None``.
    notes : tuple[str, ...]
        Remarks to show the user.
    """

    version: str
    captures: Mapping[str, str] = field(default_factory=dict)
    checksum: DiscoveredChecksum | None = None
    notes: tuple[str, ...] = ()
    """What the tracker waived and a maintainer should know (an optional checksum source that held nothing)."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _Collector:
    """Collect the problems of one parse together with the manifest kind."""

    def __init__(self, kind: ManifestKind) -> None:
        """Start with no problems for a manifest of *kind*."""
        self.kind = kind
        self.problems: list[str] = []

    def error(self, where: str, message: str) -> None:
        """Record one problem found at *where*.

        Parameters
        ----------
        where : str
            The key path of the problem; empty for the top level.
        message : str
            What is wrong.
        """
        self.problems.append(f"{where}: {message}" if where else message)

    def table(self, value: Any, where: str) -> dict[str, Any] | None:
        """Return *value* when it is a table, otherwise record a problem.

        Parameters
        ----------
        value : Any
            The parsed value.
        where : str
            The key path, for the message.

        Returns
        -------
        dict or None
            *value* when it is a table, else ``None``.
        """
        if isinstance(value, dict):
            return value
        self.error(where, f"must be a table, not {_type_name(value)}")
        return None

    def rows(self, value: Any, where: str) -> list[tuple[int, dict[str, Any]]]:
        """Return the rows of an array of tables.

        Parameters
        ----------
        value : Any
            The parsed value; ``None`` means the array is absent.
        where : str
            The key path, for messages.

        Returns
        -------
        list of tuple of (int, dict)
            Each row with its index; rows that are not tables are reported and skipped.
        """
        # Absence is fine; a non-array is a problem; non-table items are reported one by one.
        if value is None:
            return []
        if not isinstance(value, list):
            self.error(where, f"must be an array of tables ([[{where}]]), not {_type_name(value)}")
            return []
        rows = []
        # Keep the table rows with their positions, so later messages can say ``name[index]``.
        for index, item in enumerate(value):
            if isinstance(item, dict):
                rows.append((index, item))
            else:
                self.error(f"{where}[{index}]", f"must be a table, not {_type_name(item)}")
        return rows

    def keys(self, table: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
        """Reject keys that are not in *allowed*, with a hint for near misses.

        Parameters
        ----------
        table : mapping
            The table to check.
        allowed : tuple of str
            The keys the table may hold.
        where : str
            The key path of the table, for messages.
        """
        for key in table:
            if key in allowed:
                continue
            hint = _hint(str(key), allowed)
            prefix = f"{where}: " if where else ""
            self.problems.append(f"{prefix}unknown key '{key}'{hint}")

    def string(self, table: Mapping[str, Any], key: str, where: str, *, required: bool = False,
               empty: bool = False) -> str | None:
        """Return the string at *key*, recording a problem when it has the wrong type.

        Parameters
        ----------
        table : mapping
            The table that holds the key.
        key : str
            The key to read.
        where : str
            The key path of the table, for messages.
        required : bool
            Whether a missing key is a problem.
        empty : bool
            Whether an empty (or blank) string is accepted.

        Returns
        -------
        str or None
            The string, or ``None`` when absent or invalid.
        """
        # A missing key is a problem only when required; a present one must be a string, non-blank unless allowed.
        label = f"{where}.{key}" if where else key
        if key not in table:
            if required:
                self.error(label, "is required")
            return None
        # A present value must be a string, and non-blank unless blanks are allowed.
        value = table[key]
        if not isinstance(value, str):
            self.error(label, f"must be a string, not {_type_name(value)}")
            return None
        if not empty and not value.strip():
            self.error(label, "must not be empty")
            return None
        return value

    def boolean(self, table: Mapping[str, Any], key: str, where: str) -> bool | None:
        """Return the boolean at *key*, recording a problem for any other type.

        Parameters
        ----------
        table : mapping
            The table that holds the key.
        key : str
            The key to read.
        where : str
            The key path of the table, for messages.

        Returns
        -------
        bool or None
            The value, or ``None`` when absent or not a boolean.
        """
        if key not in table:
            return None
        value = table[key]
        if not isinstance(value, bool):
            self.error(f"{where}.{key}" if where else key, f"must be true or false, not {_type_name(value)}")
            return None
        return value

    def strings(self, table: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
        """Return the array of strings at *key*.

        Parameters
        ----------
        table : mapping
            The table that holds the key.
        key : str
            The key to read.
        where : str
            The key path of the table, for messages.

        Returns
        -------
        tuple of str
            The strings; empty when absent or invalid (the problem is recorded).
        """
        label = f"{where}.{key}" if where else key
        value = table.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
            self.error(label, "must be an array of non-empty strings")
            return ()
        return tuple(value)


def _type_name(value: Any) -> str:
    """Return a reader-friendly name for the TOML type of *value*."""
    return {
        dict: "a table", list: "an array", str: "a string", bool: "a boolean",
        int: "an integer", float: "a number",
    }.get(type(value), type(value).__name__)


def _hint(key: str, allowed: tuple[str, ...]) -> str:
    """Return ``"; did you mean 'x'?"`` for the closest allowed key, or ``""``."""
    if key == "ignore_checksum":
        return ("; ignore_checksum was removed in format 2: add a sha256 to [source], or run the command with "
                "--skip-checksum for a download over https")
    folded = {name.casefold(): name for name in allowed}
    if key.casefold() in folded:
        return f"; did you mean '{folded[key.casefold()]}'?"
    close = difflib.get_close_matches(key, allowed, n=1, cutoff=0.6)
    if close:
        return f"; did you mean '{close[0]}'?"
    return f"; allowed keys: {', '.join(allowed)}" if allowed else ""


def _walk_strings(value: Any):
    """Yield every string inside a parsed TOML value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _check_templates(collector: _Collector, value: Any, where: str, *, source_table: bool) -> None:
    """Reject templates in a version directory and malformed references anywhere.

    Inside ``[source]`` of a version directory every ``${...}`` is a template and is
    rejected (the update-discovery rule of design section 9.3). Elsewhere only
    ``${version}`` and filtered references are templates: a plain ``${NAME}`` is an
    environment reference.
    """
    # Every string must hold well-formed references; a tracker may use templates freely.
    for text in _walk_strings(value):
        try:
            found = references(text)
        except TemplateError as exc:
            collector.error(where, str(exc))
            continue
        if collector.kind is ManifestKind.TRACKER:
            continue
        # In a version directory a template is an error; report it once per string.
        for reference in found:
            if source_table or is_version_template(reference):
                collector.error(
                    where,
                    f"{reference.text} is a template, and a version directory has none: "
                    f"templates and update discovery live only in {TRACKER_DIR_NAME}",
                )
                break


def _relative_path(collector: _Collector, value: str, where: str, *, empty: bool = False) -> str | None:
    """Return *value* when it is a safe path relative to ``App``, else record a problem."""
    # An empty path is allowed only where the caller says so; anything absolute or climbing out of App is refused.
    if value == "" and empty:
        return value
    windows = PureWindowsPath(value)
    # Blank, absolute (drive, root or UNC), parent-climbing and NUL-bearing paths all escape App.
    if (
        not value.strip()
        or windows.anchor
        or windows.drive
        or value.startswith(("/", "\\"))
        or ".." in re.split(r"[\\/]", value)
        or "\x00" in value
    ):
        collector.error(where, f"{value!r} must be a relative path that stays inside App")
        return None
    # The path is safe to use below App.
    return value


def _local_script(collector: _Collector, value: str, where: str, *, suffixes: tuple[str, ...]) -> str | None:
    """Return *value* normalised when it names a file below ``pkg.local``."""
    # The path must be relative, stay below ``pkg.local`` and name a file; then the suffix must be allowed.
    normal = value.replace("\\", "/")
    parts = normal.split("/")
    windows = PureWindowsPath(value)
    # Absolute paths, parent or empty segments, and anything outside ``pkg.local`` are refused.
    if (
        windows.anchor
        or windows.drive
        or ".." in parts
        or "." in parts[:-1]
        or "" in parts
        # The first segment must be ``pkg.local`` itself, with a file name after it.
        or parts[0] != LOCAL_DIR
        or len(parts) < 2
    ):
        collector.error(where, f"{value!r} must be a relative path below {LOCAL_DIR}/")
        return None
    # The file type decides what the engine may run, so it must be one of the allowed suffixes.
    if not normal.lower().endswith(suffixes):
        collector.error(where, f"{value!r} must end in {', '.join(suffixes)}")
        return None
    return normal


def _check_variables(collector: _Collector, text: str, where: str, *, installer: bool = False) -> None:
    """Check the ``$Name`` path variables of one expanded string."""
    for name in path_variables(text):
        if name in PATH_VARIABLES:
            continue
        if installer and name in INSTALLER_VARIABLES:
            continue
        known = tuple(sorted(PATH_VARIABLES | (INSTALLER_VARIABLES if installer else frozenset())))
        collector.error(where, f"unknown variable ${name}{_hint(name, known)}")


def parse_manifest(text: str, kind: ManifestKind = ManifestKind.VERSION) -> Manifest:
    """Parse and validate the text of a ``pkg.toml``.

    Parameters
    ----------
    text : str
        The file's text.
    kind : ManifestKind, optional
        ``VERSION`` for a version directory (default) or ``TRACKER`` for
        ``_check_updates``.

    Returns
    -------
    Manifest
        The validated, frozen manifest.

    Raises
    ------
    ManifestError
        If the text is not TOML, is not format 2, or breaks any rule; the error
        lists every problem.

    Examples
    --------
    Basic usage:

    >>> manifest = parse_manifest('''
    ... format = 2
    ... description = "A tool"
    ...
    ... [source]
    ... download_url = "https://example.org/tool-1.0.zip"
    ...
    ... [[shim]]
    ... name = "tool"
    ... target = "$App/tool.exe"
    ... ''')
    >>> manifest.description, manifest.modes
    ('A tool', ('system', 'user'))
    >>> manifest.source.download_url
    'https://example.org/tool-1.0.zip'
    >>> [shim.name for shim in manifest.shims]
    ['tool']

    Edge cases:

    >>> parse_manifest("format = 1")
    Traceback (most recent call last):
        ...
    gupkg.model.manifest.ManifestError: Unsupported pkg.toml format (1): this gupkg reads only format 2; set format = 2.
    >>> parse_manifest("format = 3")
    Traceback (most recent call last):
        ...
    gupkg.model.manifest.NeedsNewerGupkg: Unsupported pkg.toml format (3): this package needs a newer gupkg (this gupkg reads format 2).
    """
    # Strip a leading byte order mark, then parse the TOML; the rules are applied to the parsed tree.
    try:
        data = tomllib.loads(text.lstrip("﻿"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"Not valid TOML: {exc}") from exc
    return parse_manifest_data(data, kind)


def parse_manifest_data(data: Mapping[str, Any], kind: ManifestKind = ManifestKind.VERSION) -> Manifest:
    """Validate an already parsed ``pkg.toml`` document.

    Parameters
    ----------
    data : mapping
        The parsed TOML tree.
    kind : ManifestKind, optional
        ``VERSION`` (default) or ``TRACKER``.

    Returns
    -------
    Manifest
        The validated, frozen manifest.

    Raises
    ------
    ManifestError
        If the document is not format 2 or breaks any rule.

    Examples
    --------
    Basic usage:

    >>> parse_manifest_data({"format": 2, "description": "A tool"}).description
    'A tool'
    >>> parse_manifest_data({"format": 2}).kind
    <ManifestKind.VERSION: 'version'>

    Edge cases:

    >>> parse_manifest_data({"description": "no format"})
    Traceback (most recent call last):
        ...
    gupkg.model.manifest.ManifestError: Unsupported pkg.toml format (missing): this gupkg reads only format 2; set format = 2.
    """
    collector = _Collector(kind)

    # An unknown format is refused before anything else is read: the rest of the
    # file means nothing to this reader.
    declared = data.get("format")
    if declared != FORMAT or isinstance(declared, bool):
        shown = "missing" if "format" not in data else repr(declared)
        if isinstance(declared, int) and not isinstance(declared, bool) and declared > FORMAT:
            raise NeedsNewerGupkg(
                f"Unsupported pkg.toml format ({shown}): this package {NEEDS_NEWER_GUPKG} (this gupkg reads format {FORMAT})."
            )
        raise ManifestError(
            f"Unsupported pkg.toml format ({shown}): this gupkg reads only format {FORMAT}; set format = {FORMAT}."
        )

    # Keys that the directory names already supply are refused, then the remaining top-level keys are checked.
    for key in ("version", "localVersion", "name"):
        if key in data:
            collector.error(
                key, "is not allowed: the version is the name of the version directory and the name is the package folder"
            )
    collector.keys({key: value for key, value in data.items() if key not in ("version", "localVersion", "name")},
                   _TOP_KEYS, "")

    # The plain top-level keys, each checked for type and shape.
    description = collector.string(data, "description", "", empty=True)
    project_url = collector.string(data, "project_url", "")
    if project_url is not None and not re.match(r"https?://[^\s/]+", project_url):
        collector.error("project_url", "must be an http:// or https:// URL")
        project_url = None
    # Modes, requirements and the gupkg minimum decide who may install the package.
    modes = _parse_modes(collector, data)
    requires = collector.strings(data, "requires", "")
    requires_gupkg = collector.string(data, "requires_gupkg", "")
    if requires_gupkg is not None and not _GUPKG_REQUIREMENT.fullmatch(requires_gupkg.strip()):
        collector.error("requires_gupkg", 'must be a minimum version such as ">=1.2"')
        requires_gupkg = None
    record_manifest = collector.boolean(data, "record_manifest", "")

    # Each table is parsed by its own function, which reports into the same collector.
    source = _parse_source(collector, data.get("source"))
    track = _parse_track(collector, data.get("track"))
    prepare = _parse_prepare(collector, data.get("prepare"))
    portable = _parse_portable(collector, data.get("portable"), modes)
    shortcuts = _parse_shortcuts(collector, data.get("shortcut"), modes)
    envs = _parse_envs(collector, data.get("env"))
    paths = _parse_paths(collector, data.get("path"), modes)
    shims = _parse_shims(collector, data.get("shim"))

    # Templates: nothing in a version directory, anything well-formed in a tracker.
    for key in ("description", "project_url", "prepare", "portable", "shortcut", "env", "path", "shim"):
        if key in data:
            _check_templates(collector, data[key], key, source_table=False)
    if "source" in data:
        _check_templates(collector, data["source"], "source", source_table=True)
    # Rules that depend on the kind: a tracker needs ``[track]`` and sets no hash of its own; a version has no discovery.
    if kind is ManifestKind.TRACKER:
        if track is None and "track" not in data:
            collector.error("track", f"a tracker ({TRACKER_DIR_NAME}) needs a [track] table")
        if source.sha256 is not None or source.sha256_from is not None:
            collector.error("source", "a tracker has no sha256 or sha256_from: check-updates sets them from a discovered checksum")
    elif source.checksum is not None:
        collector.error(
            "source.checksum",
            "finds the hash of a discovered version and belongs to a tracker; a version directory carries the sha256 itself",
        )

    # Raise every problem together, or return the frozen manifest.
    if collector.problems:
        raise ManifestError(collector.problems)
    # An absent ``record_manifest`` means true.
    return Manifest(
        kind=kind,
        description=description,
        project_url=project_url,
        modes=modes,
        requires=requires,
        requires_gupkg=requires_gupkg,
        record_manifest=True if record_manifest is None else record_manifest,
        # The parsed tables.
        source=source,
        track=track,
        prepare=prepare,
        portable=portable,
        shortcuts=shortcuts,
        envs=envs,
        paths=paths,
        shims=shims,
    )


def _parse_modes(collector: _Collector, data: Mapping[str, Any]) -> tuple[str, ...]:
    """Parse ``modes``: ``system`` and/or ``user`` (default both), or ``["portable"]`` alone (a portable package, user only)."""
    if "modes" not in data:
        return DEFAULT_MODES
    # The value must be a non-empty array of known, distinct modes; ``portable`` may not be mixed with the others.
    value = data["modes"]
    known = (*MODES, PORTABLE)
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        collector.error("modes", f"must be a non-empty array of {', '.join(known)}")
        return DEFAULT_MODES
    bad = [item for item in value if item not in known]
    for item in bad:
        collector.error("modes", f"unknown mode '{item}'{_hint(item, known)}")
    # Duplicates and mixing ``portable`` with other modes are reported; unknown modes fall back to the default.
    if len(set(value)) != len(value):
        collector.error("modes", "lists a mode twice")
    if PORTABLE in value and len(set(value)) > 1 and not bad:
        collector.error("modes", 'a package is portable or not; publish a separate "<name>-portable" package')
    return tuple(value) if not bad else DEFAULT_MODES


def _parse_source(collector: _Collector, raw: Any) -> Source:
    """Parse ``[source]``: one table that says how ``App`` is populated."""
    # A missing, repeated or malformed table yields an empty source (the problem is recorded).
    if raw is None:
        return Source()
    if isinstance(raw, list):
        collector.error("source", "a version has exactly one [source] table, not an array ([[source]])")
        return Source()
    table = collector.table(raw, "source")
    if table is None:
        return Source()
    collector.keys(table, _SOURCE_KEYS, "source")

    # Read each key with its own type check first; the blocks after that check how
    # the keys fit together (one populate method, shipped, unpack options).
    download_url = collector.string(table, "download_url", "source")
    script = collector.string(table, "script", "source")
    if script is not None:
        script = _local_script(collector, script, "source.script", suffixes=SCRIPT_SUFFIXES)
    shipped = collector.boolean(table, "shipped", "source")
    # The declared hash is normalised to lower case; a malformed one is reported and dropped.
    sha256 = collector.string(table, "sha256", "source")
    if sha256 is not None:
        if re.fullmatch(r"[0-9A-Fa-f]{64}", sha256):
            sha256 = sha256.lower()
        else:
            collector.error("source.sha256", INVALID_SHA256_MESSAGE)
            sha256 = None
    # The origin of the hash and the archive kind must come from their fixed vocabularies.
    sha256_from = collector.string(table, "sha256_from", "source")
    if sha256_from is not None and sha256_from not in SHA256_FROM:
        collector.error("source.sha256_from", f"must be one of {', '.join(SHA256_FROM)}")
        sha256_from = None
    kind = collector.string(table, "kind", "source")
    if kind is not None and kind not in SOURCE_KINDS:
        collector.error("source.kind", f"must be one of {', '.join(SOURCE_KINDS)}")
        kind = None
    # A pinned version's URL must be a concrete address; a tracker's holds templates
    # such as ${asset_url}, so it is checked only after rendering.
    if download_url is not None and collector.kind is ManifestKind.VERSION:
        web = re.match(r"(https?|file)://\S", download_url, re.IGNORECASE)
        # A git source may also be an ssh or scp-style address.
        remote = kind == "git" and re.match(r"(ssh|git)://\S|[\w.-]+@[\w.-]+:\S", download_url)
        if not (web or remote):
            collector.error("source.download_url", "must be an http://, https:// or file:// URL")
    # Unpack options: how many wrapper levels to strip, which subtree to keep, and
    # which files must exist afterwards. Every path must stay inside App.
    strip = table.get("strip")
    if strip is not None and not (
        (isinstance(strip, int) and not isinstance(strip, bool) and strip >= 0) or strip == "auto"
    ):
        collector.error("source.strip", 'must be a number of levels (0 or more) or "auto"')
        strip = None
    subdir = collector.string(table, "subdir", "source")
    if subdir is not None:
        subdir = _relative_path(collector, subdir, "source.subdir")
    # Files that must exist after unpacking, and the manifest to verify; every path stays inside App.
    require = tuple(
        item for item in (
            _relative_path(collector, value, "source.require") for value in collector.strings(table, "require", "source")
        ) if item is not None
    )
    verify_manifest = collector.string(table, "verify_manifest", "source")
    if verify_manifest is not None:
        verify_manifest = _relative_path(collector, verify_manifest, "source.verify_manifest")
    # Request headers for the download go through the shared header checks.
    headers: dict[str, str] = {}
    if table.get("headers") is not None:
        if not isinstance(table["headers"], dict):
            collector.error("source.headers", "must be a table of header names and values")
        else:
            try:
                headers = check_header_map(table["headers"], "source.headers")
            except ValueError as exc:
                collector.error("source.headers", str(exc))

    # Sub-tables and arrays of tables are parsed by their own functions.
    checksum = _parse_checksum(collector, table.get("checksum"))
    installer = _parse_installer(collector, table.get("installer"))
    git = _parse_git(collector, table.get("git"), kind)
    nested = _parse_nested(collector, table.get("nested"))
    extract = _parse_extract(collector, table.get("extract"))

    # A vendor installer replaces the unpack step, so the unpack options and every kind but ``exe`` would be ignored: refuse them.
    if installer is not None:
        if kind not in (None, "exe"):
            collector.error("source.installer", f"runs the downloaded .exe itself, so kind must be \"exe\" or absent, not \"{kind}\"")
        for key, value in (("strip", strip), ("subdir", subdir), ("nested", nested or None), ("extract", extract or None),
                           ("verify_manifest", verify_manifest)):
            if value is not None:
                collector.error(f"source.{key}", "is an unpack option, and a version that runs a vendor installer does not unpack")

    # One populate method per version; shipped needs no populate method.
    if download_url is not None and script is not None:
        collector.error("source", "has both download_url and script: a version has one populate method")
    populated = download_url is not None or script is not None
    if shipped is False and not populated:
        collector.error("source", "shipped = false needs a download_url or a script: nothing could ever populate App")

    # Only the built-in download script unpacks an artifact, so unpack options and a
    # checksum make no sense without a download_url. Refuse them rather than ignore them.
    if not populated or script is not None:
        unpack_only = {
            "kind": kind, "strip": strip, "subdir": subdir, "checksum": checksum, "installer": installer,
            "git": git, "sha256": sha256, "sha256_from": sha256_from,
            "nested": nested or None, "extract": extract or None, "headers": headers or None,
        }
        for key, value in unpack_only.items():
            if value is not None:
                collector.error(f"source.{key}", "needs download_url: only the built-in download script unpacks an artifact")
    # The hash origin needs a hash, and ``git`` options belong to git sources only.
    if sha256_from is not None and sha256 is None:
        collector.error("source.sha256_from", "needs a sha256")
    if kind == "git":
        if download_url is None:
            collector.error("source.kind", "'git' needs download_url (the repository URL)")
        if git is None or (git.ref is None and git.commit is None):
            collector.error("source.git", "a git source needs a ref or a commit")
    elif git is not None:
        collector.error("source.git", "is valid only with kind = \"git\"")
    # Assemble the typed source from the validated pieces.
    return Source(
        download_url=download_url, script=script, shipped_declared=shipped, sha256=sha256,
        sha256_from=sha256_from, kind=kind, strip=strip, subdir=subdir, require=require,
        verify_manifest=verify_manifest, checksum=checksum, installer=installer, git=git,
        nested=nested, extract=extract, headers=headers,
    )


def _parse_checksum(collector: _Collector, raw: Any) -> Checksum | None:
    """Parse ``[source.checksum]``: exactly one discovery method."""
    # Read every key, then work out which discovery methods were named.
    if raw is None:
        return None
    table = collector.table(raw, "source.checksum")
    if table is None:
        return None
    collector.keys(table, (*_CHECKSUM_KEYS, "optional"), "source.checksum")
    values = {key: collector.string(table, key, "source.checksum") for key in _CHECKSUM_KEYS}
    optional = bool(collector.boolean(table, "optional", "source.checksum"))
    # The discovery method is whichever of these four keys is set.
    methods = [
        name for name, present in (
            ("sidecar", values["sidecar"]), ("sums_file", values["sums_file"]),
            ("json", values["json"]), ("page_match", values["page_match"]),
        ) if present is not None
    ]
    # Exactly one method, and its companion key (``entry`` or ``path``) only with it.
    if len(methods) != 1:
        collector.error("source.checksum", "needs exactly one of sidecar, sums_file, json or page_match")
    elif methods[0] == "sums_file" and values["entry"] is None:
        collector.error("source.checksum", "sums_file needs an entry")
    elif methods[0] == "json" and values["path"] is None:
        collector.error("source.checksum", "json needs a path")
    # ``entry`` belongs to ``sums_file`` and ``path`` to ``json``; anywhere else they would be ignored.
    if values["entry"] is not None and methods != ["sums_file"]:
        collector.error("source.checksum.entry", "belongs to sums_file")
    if values["path"] is not None and methods != ["json"]:
        collector.error("source.checksum.path", "belongs to json")
    return Checksum(**values, optional=optional)


def _parse_installer(collector: _Collector, raw: Any) -> Installer | None:
    """Parse ``[source.installer]``: the arguments of a vendor installer."""
    # The arguments are required, and may use the installer path variables.
    if raw is None:
        return None
    table = collector.table(raw, "source.installer")
    if table is None:
        return None
    collector.keys(table, ("run",), "source.installer")
    run = collector.strings(table, "run", "source.installer")
    if "run" not in table:
        collector.error("source.installer.run", "is required")
    # Each argument may use the installer path variables (``$Stage`` among them).
    for text in run:
        _check_variables(collector, text, "source.installer.run", installer=True)
    return Installer(run)


def _parse_git(collector: _Collector, raw: Any, kind: str | None) -> GitSource | None:
    """Parse ``[source.git]``: a full ref and/or a 40-digit commit."""
    if raw is None:
        return None
    table = collector.table(raw, "source.git")
    if table is None:
        return None
    collector.keys(table, ("ref", "commit"), "source.git")
    ref = collector.string(table, "ref", "source.git")
    commit = collector.string(table, "commit", "source.git")
    if ref is not None and (not ref.startswith("refs/") or ref.startswith("-") or any(ord(c) < 32 for c in ref)):
        collector.error("source.git.ref", "must be a full refs/... name")
    # A template may stand in for the commit in a tracker; a pinned commit is 40 hex digits.
    if commit is not None and not re.fullmatch(r"[0-9A-Fa-f]{40}|.*\$\{[^}]*\}.*", commit):
        collector.error("source.git.commit", "must be a 40-digit commit id")
    return GitSource(ref, commit.lower() if commit and re.fullmatch(r"[0-9A-Fa-f]{40}", commit) else commit)


def _parse_nested(collector: _Collector, raw: Any) -> tuple[NestedArchive, ...]:
    """Parse ``[[source.nested]]``."""
    rows = []
    for index, row in collector.rows(raw, "source.nested"):
        where = f"source.nested[{index}]"
        collector.keys(row, ("path", "dest", "replace"), where)
        path = collector.string(row, "path", where, required=True)
        dest = collector.string(row, "dest", where, required=True, empty=True)
        replace = collector.boolean(row, "replace", where)
        # Both paths must stay inside App before the row is kept.
        if path is not None and dest is not None:
            path = _relative_path(collector, path, f"{where}.path")
            dest = _relative_path(collector, dest, f"{where}.dest", empty=True)
            if path is not None and dest is not None:
                rows.append(NestedArchive(path, dest, bool(replace)))
    return tuple(rows)


def _parse_extract(collector: _Collector, raw: Any) -> tuple[ExtractRule, ...]:
    """Parse ``[[source.extract]]``: glob-mapped partial extraction."""
    rows = []
    for index, row in collector.rows(raw, "source.extract"):
        where = f"source.extract[{index}]"
        collector.keys(row, ("src", "dest"), where)
        src = collector.string(row, "src", where, required=True)
        dest = collector.string(row, "dest", where, required=True, empty=True)
        # Both patterns must stay inside App before the rule is kept.
        if src is not None and dest is not None:
            src = _relative_path(collector, src, f"{where}.src")
            dest = _relative_path(collector, dest, f"{where}.dest", empty=True)
            if src is not None and dest is not None:
                rows.append(ExtractRule(src, dest))
    return tuple(rows)


def _parse_track(collector: _Collector, raw: Any) -> Track | None:
    """Parse ``[track]``; valid only in a tracker."""
    # Only a tracker has ``[track]``; a version directory that has one is refused.
    if raw is None:
        return None
    if collector.kind is ManifestKind.VERSION:
        collector.error(
            "track",
            f"a version directory has no [track] table: update discovery lives only in {TRACKER_DIR_NAME}",
        )
        return None
    # The value must be a table with a known provider.
    table = collector.table(raw, "track")
    if table is None:
        return None
    # The provider decides which keys are allowed, including which location key it takes.
    provider = collector.string(table, "provider", "track", required=True)
    if provider is None:
        return None
    if provider not in _TRACK_PROVIDERS:
        collector.error("track.provider", f"unknown provider '{provider}'{_hint(provider, tuple(_TRACK_PROVIDERS))}")
        return None
    # A location key the provider does not take is reported; the rest of the keys are checked against its list.
    location = _TRACK_LOCATION[provider]
    allowed = ("provider", *location, *_TRACK_PROVIDERS[provider])
    for key in ("check_url", "repo", "repo_url"):
        if key in table and key not in location:
            collector.error(f"track.{key}", f"is not a key of the '{provider}' provider")
    collector.keys({k: v for k, v in table.items() if k not in ("check_url", "repo", "repo_url") or k in location},
                   allowed, "track")
    # Read the location values, require one of them, and check the shape of ``check_url``.
    values = {key: collector.string(table, key, "track") for key in ("check_url", "repo", "repo_url") if key in location}
    if location and not any(table.get(key) for key in location):
        collector.error("track", f"the '{provider}' provider needs {' or '.join(location)}")
    check_url = values.get("check_url")
    if check_url is not None and not re.match(r"https?://\S", check_url, re.IGNORECASE):
        collector.error("track.check_url", "must be an http:// or https:// URL")
    # The provider-specific keys become the options; a ``module`` provider's hook path is normalised.
    options = {
        key: value for key, value in table.items()
        if key in _TRACK_PROVIDERS[provider] and key not in ("check_url", "repo", "repo_url")
    }
    if provider == "module":
        # The hook must be a ``.py`` file below ``pkg.local``.
        module = options.get("module", _DEFAULT_TRACK_MODULE)
        if not isinstance(module, str):
            collector.error("track.module", "must be a string")
        else:
            normal = _local_script(collector, module, "track.module", suffixes=(".py",))
            if normal is not None:
                options["module"] = normal
    # The shape is right; now check the values (patterns, paths, templates, choices).
    try:
        parse_spec(provider, {key: values.get(key) for key in ("check_url", "repo", "repo_url")}, options)
    except TrackSpecError as exc:
        for key, message in exc.problems:
            collector.error(key, message)
    return Track(provider, values.get("check_url"), values.get("repo"), values.get("repo_url"), options)


def _parse_prepare(collector: _Collector, raw: Any) -> tuple[PrepareStep, ...]:
    """Parse ``[[prepare]]``: each row names exactly one step."""
    # Each row must name exactly one step; its value is parsed by the step's own rules.
    steps: list[PrepareStep] = []
    for index, row in collector.rows(raw, "prepare"):
        where = f"prepare[{index}]"
        collector.keys(row, _PREPARE_KEYS, where)
        present = [key for key in _PREPARE_KEYS if key in row]
        # A row with no step, or with several, cannot be run in a defined order.
        if len(present) != 1:
            collector.error(where, f"needs exactly one of {', '.join(_PREPARE_KEYS)}")
            continue
        step = _parse_prepare_step(collector, present[0], row[present[0]], f"{where}.{present[0]}")
        if step is not None:
            steps.append(step)
    return tuple(steps)


def _parse_prepare_step(collector: _Collector, name: str, value: Any, where: str) -> PrepareStep | None:
    """Parse the value of one ``[[prepare]]`` step."""
    # ``remove`` takes a list of paths and ``module`` a hook script; neither is a table.
    if name == "remove":
        if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
            collector.error(where, "must be a non-empty array of paths")
            return None
        paths = [_relative_path(collector, item, where) for item in value]
        return PrepareRemove(tuple(paths)) if None not in paths else None
    # A hook module is a string path below ``pkg.local``.
    if name == "module":
        if not isinstance(value, str):
            collector.error(where, "must be a string")
            return None
        module = _local_script(collector, value, where, suffixes=(".py",))
        return PrepareModule(module) if module is not None else None
    # The remaining steps are tables: ``rename`` and ``copy`` move files, ``write`` creates one.
    table = collector.table(value, where)
    if table is None:
        return None
    # ``rename`` needs both paths inside App.
    if name == "rename":
        collector.keys(table, ("from", "to"), where)
        source, target = collector.string(table, "from", where, required=True), collector.string(table, "to", where, required=True)
        if source is None or target is None:
            return None
        source, target = _relative_path(collector, source, f"{where}.from"), _relative_path(collector, target, f"{where}.to")
        return PrepareRename(source, target) if source is not None and target is not None else None
    if name == "copy":
        collector.keys(table, ("from", "to"), where)
        # ``to`` may be one path or an array of paths; all must stay inside App.
        source = collector.string(table, "from", where, required=True)
        targets = table.get("to")
        if isinstance(targets, str):
            targets = [targets]
        # A non-empty array of strings is required once a single path has been wrapped.
        if not isinstance(targets, list) or not targets or any(not isinstance(item, str) for item in targets):
            collector.error(f"{where}.to", "must be a path or a non-empty array of paths")
            return None
        if source is None:
            return None
        # Check the source and every target together, so each bad path is reported.
        source = _relative_path(collector, source, f"{where}.from")
        checked = [_relative_path(collector, item, f"{where}.to") for item in targets]
        return PrepareCopy(source, tuple(checked)) if source is not None and None not in checked else None
    # Whatever remains is ``write``: a path inside App and the (possibly empty) content to put there.
    collector.keys(table, ("path", "content"), where)
    path = collector.string(table, "path", where, required=True)
    content = collector.string(table, "content", where, required=True, empty=True)
    if path is None or content is None:
        return None
    path = _relative_path(collector, path, f"{where}.path")
    return PrepareWrite(path, content) if path is not None else None


def _parse_portable(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> Portable | None:
    """Parse ``[portable]`` (``persist`` and ``markers``), which is required exactly when ``modes = ["portable"]``."""
    # The table is required for a portable package and refused for any other.
    is_portable = modes == (PORTABLE,)
    if raw is None:
        if is_portable:
            collector.error("portable", 'is required when modes is ["portable"]')
        return None
    # The table must be a table, and only a portable package may have one.
    table = collector.table(raw, "portable")
    if table is None:
        return None
    # The table is valid only for a portable package.
    collector.keys(table, ("persist", "markers"), "portable")
    if not is_portable:
        collector.error("portable", 'needs modes = ["portable"]')
    # The persisted paths must stay inside App.
    persist = tuple(
        item for item in (_relative_path(collector, value, "portable.persist")
                          for value in collector.strings(table, "persist", "portable")) if item is not None
    )
    # Each marker is a path or a ``{ path, content }`` table.
    markers: list[Marker] = []
    raw_markers = table.get("markers", [])
    if not isinstance(raw_markers, list):
        collector.error("portable.markers", "must be an array")
        raw_markers = []
    for index, item in enumerate(raw_markers):
        where = f"portable.markers[{index}]"
        # A bare string is a path with no content; a table gives both.
        if isinstance(item, str):
            path, content = item, None
        elif isinstance(item, dict):
            collector.keys(item, ("path", "content"), where)
            path = collector.string(item, "path", where, required=True)
            content = collector.string(item, "content", where, empty=True)
        else:
            collector.error(where, "must be a path or a { path, content } table")
            continue
        # A marker path must stay inside App.
        if path is not None and _relative_path(collector, path, where) is not None:
            markers.append(Marker(path, content))
    return Portable(persist, tuple(markers))


def _parse_shortcuts(collector: _Collector, raw: Any, package_modes: tuple[str, ...]) -> tuple[Shortcut, ...]:
    """Parse ``[[shortcut]]``, whose optional ``modes`` must be a non-empty subset of the package's (never ``portable``)."""
    # Read the required name and target, then the optional text keys, admin flag and modes.
    rows = []
    for index, row in collector.rows(raw, "shortcut"):
        where = f"shortcut[{index}]"
        collector.keys(row, _SHORTCUT_KEYS, where)
        name = collector.string(row, "name", where, required=True)
        target = collector.string(row, "target", where, required=True)
        optional: dict[str, Any] = {key: collector.string(row, key, where, empty=True) or "" for key in _SHORTCUT_TEXT_KEYS}
        optional["run_as_admin"] = bool(collector.boolean(row, "run_as_admin", where))
        optional["modes"] = _parse_shortcut_modes(collector, row, where, package_modes)
        # Every path variable in the target, arguments, folder and icon must be a known one.
        for key in ("target", "arguments", "working_dir", "icon_location"):
            text = target if key == "target" else optional[key]
            if text:
                _check_variables(collector, text, f"{where}.{key}")
        # Each part of the shortcut name becomes a file or folder name, so it must fit Windows' limit.
        if name is not None and any(len(part) > MAX_NAME_PART for part in re.split(r"[\\/]", name)):
            collector.error(f"{where}.name", f"has a part longer than {MAX_NAME_PART} characters")
            name = None
        if name is not None and target is not None:
            rows.append(Shortcut(name, target, **optional))
    return tuple(rows)


def _parse_shortcut_modes(
    collector: _Collector, row: Mapping[str, Any], where: str, package_modes: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Parse a shortcut's ``modes``; ``None`` when it is absent (the package's modes apply)."""
    # Absent means the package's modes; a present value must be a non-empty array of distinct package modes.
    if "modes" not in row:
        return None
    value = row["modes"]
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        collector.error(f"{where}.modes", f"must be a non-empty array of {', '.join(MODES)}")
        return None
    # Every listed mode must be one of the package's own (and never ``portable``), each only once.
    ok = True
    for item in value:
        if item not in package_modes or item == PORTABLE:
            collector.error(f"{where}.modes", f"'{item}' is not one of the package's modes ({', '.join(package_modes)})")
            ok = False
    if len(set(value)) != len(value):
        collector.error(f"{where}.modes", "lists a mode twice")
        ok = False
    return tuple(value) if ok else None


def _parse_envs(collector: _Collector, raw: Any) -> tuple[EnvVar, ...]:
    """Parse ``[[env]]``."""
    rows = []
    for index, row in collector.rows(raw, "env"):
        where = f"env[{index}]"
        collector.keys(row, ("name", "value"), where)
        name = collector.string(row, "name", where, required=True)
        value = collector.string(row, "value", where, required=True)
        # The value may use path variables; the name must be one a package may set.
        if value is not None:
            _check_variables(collector, value, f"{where}.value")
        if name is not None and (problem := env_name_problem(name)) is not None:
            collector.error(f"{where}.name", f"{name!r} is not a valid environment variable name: it {problem}")
            name = None
        if name is not None and value is not None:
            rows.append(EnvVar(name, value))
    # Names are unique ignoring case, as Windows treats them.
    folded = [row.name.casefold() for row in rows]
    for name in sorted({item for item in folded if folded.count(item) > 1}):
        collector.error("env", f"declares '{name}' twice")
    return tuple(rows)


def _parse_paths(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> tuple[str, ...]:
    """Parse ``[[path]]``."""
    values = []
    for index, row in collector.rows(raw, "path"):
        where = f"path[{index}]"
        collector.keys(row, ("value",), where)
        value = collector.string(row, "value", where, required=True)
        # Check the variables and the entry's shape; an entry that fails either is left out.
        if value is not None:
            _check_variables(collector, value, f"{where}.value")
            if (problem := path_entry_shape_problem(value, template=True)) is not None:
                collector.error(f"{where}.value", f"{value!r} {problem}")
                continue
            if value.startswith("%") and "system" in modes:
                # The machine PATH never follows an environment variable (a user can override it), so a package that can be
                # installed in system mode must not declare such an entry; the engine refuses it too, but the file fails inspection first.
                collector.error(
                    f"{where}.value",
                    f"{value!r} follows an environment variable, which a user can override: not allowed on the machine PATH; "
                    "a package that declares it must not list 'system' in modes",
                )
                continue
            values.append(value)
    return tuple(values)


def _parse_shims(collector: _Collector, raw: Any) -> tuple[Shim, ...]:
    """Parse ``[[shim]]``: a launcher with a target, or a raw script."""
    rows = []
    for index, row in collector.rows(raw, "shim"):
        where = f"shim[{index}]"
        collector.keys(row, _SHIM_KEYS, where)
        # The name must be one plain file name that Windows accepts.
        name = collector.string(row, "name", where, required=True)
        if name is not None and re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
            collector.error(f"{where}.name", f"{name!r} must be a plain file name")
            name = None
        elif name is not None and (problem := name_part_problem(name)) is not None:
            collector.error(f"{where}.name", f"{name!r} {problem}")
            name = None
        # A content shim is a raw script and takes no launcher options.
        content = collector.string(row, "content", where, empty=True)
        target = collector.string(row, "target", where)
        if content is not None:
            if any(key in row for key in ("target", "type", "arguments", "forward_args", "elevate", "working_dir")):
                collector.error(where, "content cannot be combined with shim options")
            if name is not None:
                rows.append(Shim(name=name, content=content))
            continue
        # A launcher shim needs a target and may set its type, arguments, forwarding, elevation and folder.
        if target is None and "target" not in row:
            collector.error(where, "needs a target or content")
        shim_type = collector.string(row, "type", where)
        if shim_type is not None and shim_type not in SHIM_TYPES:
            collector.error(f"{where}.type", f"must be one of {', '.join(SHIM_TYPES)}")
            shim_type = None
        # The optional launcher settings, then the path variables in the target, folder and arguments.
        arguments = collector.strings(row, "arguments", where)
        forward_args = collector.boolean(row, "forward_args", where)
        elevate = collector.boolean(row, "elevate", where)
        working_dir = collector.string(row, "working_dir", where)
        for key, text in (("target", target), ("working_dir", working_dir), *(("arguments", a) for a in arguments)):
            if text:
                _check_variables(collector, text, f"{where}.{key}")
        # Keep the shim only when it has both a valid name and a target; ``forward_args`` defaults to true.
        if name is not None and target is not None:
            rows.append(Shim(
                name=name, target=target, type=shim_type or "console", arguments=arguments,
                forward_args=True if forward_args is None else forward_args,
                elevate=bool(elevate), working_dir=working_dir,
            ))
    # Shim names are unique ignoring case, as Windows treats file names.
    folded = [row.name.casefold() for row in rows]
    for name in sorted({item for item in folded if folded.count(item) > 1}):
        collector.error("shim", f"declares '{name}' twice")
    return tuple(rows)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _drop_empty(data: dict[str, Any]) -> dict[str, Any]:
    """Return *data* without ``None`` values and empty lists (TOML has no null)."""
    return {key: value for key, value in data.items() if value is not None and value != [] and value != {}}


def _source_to_data(source: Source) -> dict[str, Any]:
    """Return the ``[source]`` table of *source* in the documented key order."""
    # The plain keys first, in documentation order; unset ones are dropped at the end.
    data: dict[str, Any] = {
        "download_url": source.download_url, "script": source.script, "sha256": source.sha256,
        "sha256_from": source.sha256_from, "kind": source.kind, "strip": source.strip, "subdir": source.subdir,
        "shipped": source.shipped_declared, "require": list(source.require),
        "verify_manifest": source.verify_manifest,
    }
    # The checksum discovery table keeps only its set keys.
    if source.checksum is not None:
        data["checksum"] = _drop_empty(
            {**{key: getattr(source.checksum, key) for key in _CHECKSUM_KEYS}, **({"optional": True} if source.checksum.optional else {})}
        )
    # Sub-tables and arrays of tables follow the plain keys.
    if source.installer is not None:
        data["installer"] = {"run": list(source.installer.run)}
    if source.git is not None:
        data["git"] = _drop_empty({"ref": source.git.ref, "commit": source.git.commit})
    # Arrays of tables and the request headers close the table.
    if source.nested:
        data["nested"] = [{"path": n.path, "dest": n.dest, "replace": n.replace} for n in source.nested]
    if source.extract:
        data["extract"] = [{"src": e.src, "dest": e.dest} for e in source.extract]
    if source.headers:
        data["headers"] = dict(source.headers)
    return _drop_empty(data)


def _prepare_to_data(step: PrepareStep) -> dict[str, Any]:
    """Return the ``[[prepare]]`` row for *step*."""
    if isinstance(step, PrepareRemove):
        return {"remove": list(step.paths)}
    if isinstance(step, PrepareRename):
        return {"rename": {"from": step.source, "to": step.target}}
    if isinstance(step, PrepareCopy):
        return {"copy": {"from": step.source, "to": list(step.targets)}}
    if isinstance(step, PrepareWrite):
        return {"write": {"path": step.path, "content": step.content}}
    return {"module": step.module}


def manifest_to_data(manifest: Manifest, *, explicit_modes: bool = False) -> dict[str, Any]:
    """Return *manifest* as plain TOML-ready data in the documented key order.

    Parameters
    ----------
    manifest : Manifest
        A validated manifest.
    explicit_modes : bool, optional
        Write ``modes`` even when it equals the default (a registry states the modes of every package).

    Returns
    -------
    dict
        Data that ``parse_manifest_data`` accepts and ``tomli_w`` can write.
    """
    # Top-level keys in documentation order; ``None`` marks a key to leave out.
    data: dict[str, Any] = {"format": FORMAT, "description": manifest.description, "project_url": manifest.project_url}
    data["modes"] = list(manifest.modes) if explicit_modes or manifest.modes != DEFAULT_MODES else None
    data["requires"] = list(manifest.requires)
    data["requires_gupkg"] = manifest.requires_gupkg
    data["record_manifest"] = None if manifest.record_manifest else False
    # The tracker's discovery table, then the source and the preparation and portable tables.
    if manifest.track is not None:
        track = {"provider": manifest.track.provider, "check_url": manifest.track.check_url,
                 "repo": manifest.track.repo, "repo_url": manifest.track.repo_url}
        track.update(manifest.track.options)
        data["track"] = _drop_empty(track)
    # A declared ``shipped`` keeps an otherwise empty ``[source]`` table.
    source = _source_to_data(manifest.source)
    if source or manifest.source.shipped_declared is not None:
        data["source"] = source
    if manifest.prepare:
        data["prepare"] = [_prepare_to_data(step) for step in manifest.prepare]
    # A marker without content is written as a bare path.
    if manifest.portable is not None:
        portable = manifest.portable
        data["portable"] = _drop_empty({
            "persist": list(portable.persist),
            "markers": [m.path if m.content is None else {"path": m.path, "content": m.content} for m in portable.markers],
        })
    # Shortcuts write only their set fields.
    data["shortcut"] = [
        _drop_empty({"name": s.name, "target": s.target, "arguments": s.arguments or None,
                     "working_dir": s.working_dir or None, "icon_location": s.icon_location or None,
                     "description": s.description or None, "run_as_admin": s.run_as_admin or None,
                     "modes": list(s.modes) if s.modes is not None else None})
        for s in manifest.shortcuts
    ]
    # Environment values, PATH entries and shims are written as arrays of tables.
    data["env"] = [{"name": e.name, "value": e.value} for e in manifest.envs]
    data["path"] = [{"value": value} for value in manifest.paths]
    data["shim"] = [_shim_to_data(shim) for shim in manifest.shims]
    return _drop_empty_keep_tables(data)


def _shim_to_data(shim: Shim) -> dict[str, Any]:
    """Return the ``[[shim]]`` row for *shim*, writing only non-default options."""
    if shim.content is not None:
        return {"name": shim.name, "content": shim.content}
    return _drop_empty({
        "name": shim.name, "target": shim.target, "type": shim.type if shim.type != "console" else None,
        "arguments": list(shim.arguments), "forward_args": None if shim.forward_args else False,
        "elevate": True if shim.elevate else None, "working_dir": shim.working_dir,
    })


def _drop_empty_keep_tables(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` and empty lists at the top level, keeping a declared empty ``[source]``."""
    return {key: value for key, value in data.items() if value is not None and value != []
            and not (value == {} and key != "source")}


def manifest_to_toml(manifest: Manifest, *, explicit_modes: bool = False) -> str:
    """Return the TOML text of *manifest* (written with ``tomli_w``).

    Parameters
    ----------
    manifest : Manifest
        A validated manifest.
    explicit_modes : bool, optional
        Write ``modes`` even when it equals the default.

    Returns
    -------
    str
        TOML text that ``parse_manifest`` reads back to an equal manifest.

    Examples
    --------
    Basic usage:

    >>> manifest = parse_manifest_data({"format": 2, "description": "A tool", "shim": [{"name": "tool", "target": "$App/tool.exe"}]})
    >>> print(manifest_to_toml(manifest))
    format = 2
    description = "A tool"
    <BLANKLINE>
    [[shim]]
    name = "tool"
    target = "$App/tool.exe"
    <BLANKLINE>

    Round trip:

    >>> parse_manifest(manifest_to_toml(manifest)) == manifest
    True
    """
    return dumps_tables(manifest_to_data(manifest, explicit_modes=explicit_modes))


# ---------------------------------------------------------------------------
# From tracker to version
# ---------------------------------------------------------------------------


def _render_data(value: Any, values: Mapping[str, str]) -> Any:
    """Expand the templates of every string in a parsed TOML value."""
    if isinstance(value, str):
        return render(value, values)
    if isinstance(value, dict):
        return {key: _render_data(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_data(item, values) for item in value]
    return value


def render_pinned(tracker: Manifest, result: TrackResult) -> Manifest:
    """Write the pinned version manifest that a tracker's discovery produced.

    Every template is expanded with the discovered version and captures, ``[track]``
    is removed, ``shipped`` is written only when it is true (false is the default for a source that can populate), and ``sha256`` and ``sha256_from``
    are set only from a checksum the tracker *discovered*. The result is validated as
    an ordinary version-directory manifest.

    Parameters
    ----------
    tracker : Manifest
        The manifest of ``_check_updates``.
    result : TrackResult
        What the tracker found.

    Returns
    -------
    Manifest
        A ``VERSION`` manifest with no ``[track]`` and no templates.

    Raises
    ------
    ManifestError
        If *tracker* is not a tracker, the version cannot name a directory, the
        checksum is not a discovered one, or the result still has a template.
    """
    # Only a tracker can be rendered, and only for a discovered version that can name a directory.
    if tracker.kind is not ManifestKind.TRACKER or tracker.track is None:
        raise ManifestError("render_pinned needs the manifest of a tracker with a [track] table")
    if not is_version_name(result.version):
        raise ManifestError(f"The discovered version {result.version!r} cannot name a version directory")
    values = {**result.captures, "version": result.version}

    # ``${download_url}`` in ``[source.checksum]`` means the address that this version downloads from.
    if tracker.source.download_url is not None:
        values["download_url"] = render(tracker.source.download_url, values)
    # Expand every template in the tracker's data, without its ``[track]`` table.
    data = manifest_to_data(tracker)
    data.pop("track", None)
    data = _render_data(data, values)

    # Adjust ``[source]`` for a version directory: record ``shipped``, drop tracker-only keys, add the discovered hash.
    source = data.setdefault("source", {})
    if tracker.source.shipped:  # false is the default of a source that can populate App: it is not written
        source["shipped"] = True
    source.pop("checksum", None)  # tracker-only: a version carries the sha256 it was given (D10)
    checksum = result.checksum
    if checksum is not None:
        if checksum.sha256_from not in DISCOVERED_SHA256_FROM:
            raise ManifestError(
                f"A discovered checksum comes from {' or '.join(DISCOVERED_SHA256_FROM)}, not '{checksum.sha256_from}'"
            )
        if "download_url" not in source:
            raise ManifestError("A checksum needs a download_url to belong to")
        source["sha256"] = checksum.sha256
        source["sha256_from"] = checksum.sha256_from
    # The result is validated like any version-directory manifest, so a leftover template is refused.
    return parse_manifest_data(data, ManifestKind.VERSION)
