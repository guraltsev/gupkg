"""Typed, validated ``pkg.toml`` format 2 (design sections 4, 7 and 9.3).

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
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PureWindowsPath
from typing import Any

import tomli_w

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
from .version import TRACKER_DIR_NAME, is_version_name

FORMAT = 2
MODES = ("system", "user", "portable")
DEFAULT_MODES = ("system", "user")
SOURCE_KINDS = ("zip", "7z", "tar", "sfx", "nsis", "msi", "exe", "file", "git")
SHA256_FROM = ("github-digest", "vendor-sums", "registry", "manual")
DISCOVERED_SHA256_FROM = ("github-digest", "vendor-sums")
SCRIPT_SUFFIXES = (".py", ".ps1", ".cmd", ".bat", ".exe")
SHIM_TYPES = ("console", "gui")
LOCAL_DIR = "pkg.local"
APP_MISSING_ERROR = "App is missing and no way to recover it."

_TOP_KEYS = (
    "format", "description", "project_url", "modes", "requires", "record_manifest",
    "source", "track", "prepare", "portable", "nonportable", "shortcut", "env", "path", "shim",
)
_SOURCE_KEYS = (
    "download_url", "script", "shipped", "sha256", "sha256_from", "kind", "strip", "subdir",
    "require", "verify_manifest", "checksum", "installer", "git", "nested", "extract", "headers",
)
_CHECKSUM_KEYS = ("sidecar", "sums_file", "entry", "json", "path", "page_match")
_SHORTCUT_KEYS = ("name", "target", "arguments", "working_dir", "icon_location", "description")
_SHIM_KEYS = ("name", "target", "type", "arguments", "forward_args", "elevate", "working_dir", "content")
_PREPARE_KEYS = ("remove", "rename", "copy", "write", "module")

# ``[track]``: the keys every provider takes, and the provider-specific ones of design
# section 8.1, extended by issue 026 with ``version``, ``download``, ``headers`` and the
# json and chain helpers. ``gupkg.model.trackspec`` validates the values.
_TRACK_COMMON = ("provider", "check_url", "repo", "repo_url")
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
_DEFAULT_TRACK_MODULE = "pkg.local/track.py"


class ManifestKind(Enum):
    """Say whether a ``pkg.toml`` belongs to a version directory or to ``_check_updates``."""

    VERSION = "version"
    TRACKER = "tracker"


class ManifestError(ValueError):
    """Report that a ``pkg.toml`` is invalid, listing every problem found.

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


# ---------------------------------------------------------------------------
# Typed data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Checksum:
    """How to find the hash of a discovered version (``[source.checksum]``)."""

    sidecar: str | None = None
    sums_file: str | None = None
    entry: str | None = None
    json: str | None = None
    path: str | None = None
    page_match: str | None = None


@dataclass(frozen=True)
class Installer:
    """A vendor installer run with these arguments (``[source.installer]``)."""

    run: tuple[str, ...]


@dataclass(frozen=True)
class GitSource:
    """The ref and the commit that anchor a ``git`` source (``[source.git]``)."""

    ref: str | None = None
    commit: str | None = None


@dataclass(frozen=True)
class NestedArchive:
    """An inner archive to extract (``[[source.nested]]``)."""

    path: str
    dest: str
    replace: bool = False


@dataclass(frozen=True)
class ExtractRule:
    """One glob-mapped partial extraction (``[[source.extract]]``)."""

    src: str
    dest: str


@dataclass(frozen=True)
class Source:
    """How this version's ``App`` is obtained (``[source]``).

    ``shipped_declared`` is the key as written (``None`` when absent) and
    ``shipped`` is the effective value.
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
    """

    provider: str
    check_url: str | None = None
    repo: str | None = None
    repo_url: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PrepareRemove:
    """``[[prepare]] remove``: delete these paths from ``App``."""

    paths: tuple[str, ...]


@dataclass(frozen=True)
class PrepareRename:
    """``[[prepare]] rename``: move the one match of a glob to a new name."""

    source: str
    target: str


@dataclass(frozen=True)
class PrepareCopy:
    """``[[prepare]] copy``: copy one file to each target."""

    source: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class PrepareWrite:
    """``[[prepare]] write``: write a file into ``App``."""

    path: str
    content: str


@dataclass(frozen=True)
class PrepareModule:
    """``[[prepare]] module``: run a hook module from ``pkg.local``."""

    module: str


PrepareStep = PrepareRemove | PrepareRename | PrepareCopy | PrepareWrite | PrepareModule


@dataclass(frozen=True)
class Marker:
    """A file the app needs in ``App`` to enter portable mode."""

    path: str
    content: str | None = None


@dataclass(frozen=True)
class Portable:
    """What a portable install keeps and passes (``[portable]``)."""

    persist: tuple[str, ...] = ()
    markers: tuple[Marker, ...] = ()
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NonPortable:
    """What a non-portable install removes from ``App`` (``[nonportable]``)."""

    remove: tuple[str, ...] = ()


@dataclass(frozen=True)
class Shortcut:
    """A Start Menu shortcut (``[[shortcut]]``)."""

    name: str
    target: str
    arguments: str = ""
    working_dir: str = ""
    icon_location: str = ""
    description: str = ""


@dataclass(frozen=True)
class EnvVar:
    """An environment variable (``[[env]]``)."""

    name: str
    value: str


@dataclass(frozen=True)
class Shim:
    """A command in the mode's ``bin`` directory (``[[shim]]``)."""

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
    """A validated ``pkg.toml`` of format 2."""

    kind: ManifestKind
    description: str | None = None
    project_url: str | None = None
    modes: tuple[str, ...] = DEFAULT_MODES
    requires: tuple[str, ...] = ()
    record_manifest: bool = True
    source: Source = field(default_factory=Source)
    track: Track | None = None
    prepare: tuple[PrepareStep, ...] = ()
    portable: Portable | None = None
    nonportable: NonPortable | None = None
    shortcuts: tuple[Shortcut, ...] = ()
    envs: tuple[EnvVar, ...] = ()
    paths: tuple[str, ...] = ()
    shims: tuple[Shim, ...] = ()

    def supports(self, mode: str) -> bool:
        """Return whether the package declares *mode* in ``modes``."""
        return mode in self.modes

    def referenced_hooks(self) -> tuple[str, ...]:
        """Return the ``pkg.local`` files that ``[source]`` and ``[[prepare]]`` reference.

        The tracker's own ``[track] module`` is not included: ``render_pinned`` never
        copies a tracking hook into a version directory.
        """
        names: list[str] = []
        if self.source.script is not None:
            names.append(self.source.script)
        names.extend(step.module for step in self.prepare if isinstance(step, PrepareModule))
        return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class DiscoveredChecksum:
    """A SHA-256 that the tracker discovered, with where it came from."""

    sha256: str
    sha256_from: str


@dataclass(frozen=True)
class TrackResult:
    """What a tracker found: the version, its captures and a discovered checksum."""

    version: str
    captures: Mapping[str, str] = field(default_factory=dict)
    checksum: DiscoveredChecksum | None = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class _Collector:
    """Collect the problems of one parse together with the manifest kind."""

    def __init__(self, kind: ManifestKind) -> None:
        self.kind = kind
        self.problems: list[str] = []

    def error(self, where: str, message: str) -> None:
        """Record one problem found at *where*."""
        self.problems.append(f"{where}: {message}" if where else message)

    def table(self, value: Any, where: str) -> dict[str, Any] | None:
        """Return *value* when it is a table, otherwise record a problem."""
        if isinstance(value, dict):
            return value
        self.error(where, f"must be a table, not {_type_name(value)}")
        return None

    def rows(self, value: Any, where: str) -> list[tuple[int, dict[str, Any]]]:
        """Return the rows of an array of tables."""
        if value is None:
            return []
        if not isinstance(value, list):
            self.error(where, f"must be an array of tables ([[{where}]]), not {_type_name(value)}")
            return []
        rows = []
        for index, item in enumerate(value):
            if isinstance(item, dict):
                rows.append((index, item))
            else:
                self.error(f"{where}[{index}]", f"must be a table, not {_type_name(item)}")
        return rows

    def keys(self, table: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
        """Reject keys that are not in *allowed*, with a hint for near misses."""
        for key in table:
            if key in allowed:
                continue
            hint = _hint(str(key), allowed)
            prefix = f"{where}: " if where else ""
            self.problems.append(f"{prefix}unknown key '{key}'{hint}")

    def string(self, table: Mapping[str, Any], key: str, where: str, *, required: bool = False,
               empty: bool = False) -> str | None:
        """Return the string at *key*, recording a problem when it has the wrong type."""
        label = f"{where}.{key}" if where else key
        if key not in table:
            if required:
                self.error(label, "is required")
            return None
        value = table[key]
        if not isinstance(value, str):
            self.error(label, f"must be a string, not {_type_name(value)}")
            return None
        if not empty and not value.strip():
            self.error(label, "must not be empty")
            return None
        return value

    def boolean(self, table: Mapping[str, Any], key: str, where: str) -> bool | None:
        """Return the boolean at *key*, recording a problem for any other type."""
        if key not in table:
            return None
        value = table[key]
        if not isinstance(value, bool):
            self.error(f"{where}.{key}" if where else key, f"must be true or false, not {_type_name(value)}")
            return None
        return value

    def strings(self, table: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
        """Return the array of strings at *key*."""
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
    for text in _walk_strings(value):
        try:
            found = references(text)
        except TemplateError as exc:
            collector.error(where, str(exc))
            continue
        if collector.kind is ManifestKind.TRACKER:
            continue
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
    if value == "" and empty:
        return value
    windows = PureWindowsPath(value)
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
    return value


def _local_script(collector: _Collector, value: str, where: str, *, suffixes: tuple[str, ...]) -> str | None:
    """Return *value* normalised when it names a file below ``pkg.local``."""
    normal = value.replace("\\", "/")
    parts = normal.split("/")
    windows = PureWindowsPath(value)
    if (
        windows.anchor
        or windows.drive
        or ".." in parts
        or "." in parts[:-1]
        or "" in parts
        or parts[0] != LOCAL_DIR
        or len(parts) < 2
    ):
        collector.error(where, f"{value!r} must be a relative path below {LOCAL_DIR}/")
        return None
    if not normal.lower().endswith(suffixes):
        collector.error(where, f"{value!r} must end in {', '.join(suffixes)}")
        return None
    return normal


def _check_variables(collector: _Collector, text: str, where: str, modes: tuple[str, ...], *,
                     installer: bool = False) -> None:
    """Check the ``$Name`` path variables of one expanded string."""
    for name in path_variables(text):
        if name in PATH_VARIABLES:
            if name == "Data" and "portable" not in modes:
                collector.error(where, "$Data is valid only when 'portable' is in modes")
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
    """
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
    """
    collector = _Collector(kind)

    # An unknown format is refused before anything else is read: the rest of the
    # file means nothing to this reader.
    declared = data.get("format")
    if declared != FORMAT or isinstance(declared, bool):
        shown = "missing" if "format" not in data else repr(declared)
        raise ManifestError(
            f"Unsupported pkg.toml format ({shown}): this gupkg reads only format {FORMAT}; set format = {FORMAT}."
        )

    for key in ("version", "localVersion", "name"):
        if key in data:
            collector.error(
                key, "is not allowed: the version is the name of the version directory and the name is the package folder"
            )
    collector.keys({key: value for key, value in data.items() if key not in ("version", "localVersion", "name")},
                   _TOP_KEYS, "")

    description = collector.string(data, "description", "", empty=True)
    project_url = collector.string(data, "project_url", "")
    if project_url is not None and not re.match(r"https?://[^\s/]+", project_url):
        collector.error("project_url", "must be an http:// or https:// URL")
        project_url = None
    modes = _parse_modes(collector, data)
    requires = collector.strings(data, "requires", "")
    record_manifest = collector.boolean(data, "record_manifest", "")

    source = _parse_source(collector, data.get("source"), modes)
    track = _parse_track(collector, data.get("track"))
    prepare = _parse_prepare(collector, data.get("prepare"))
    portable = _parse_portable(collector, data.get("portable"), modes)
    nonportable = _parse_nonportable(collector, data.get("nonportable"))
    shortcuts = _parse_shortcuts(collector, data.get("shortcut"), modes)
    envs = _parse_envs(collector, data.get("env"), modes)
    paths = _parse_paths(collector, data.get("path"), modes)
    shims = _parse_shims(collector, data.get("shim"), modes)

    # Templates: nothing in a version directory, anything well-formed in a tracker.
    for key in ("description", "project_url", "prepare", "portable", "nonportable", "shortcut", "env", "path", "shim"):
        if key in data:
            _check_templates(collector, data[key], key, source_table=False)
    if "source" in data:
        _check_templates(collector, data["source"], "source", source_table=True)
    if kind is ManifestKind.TRACKER:
        if track is None and "track" not in data:
            collector.error("track", f"a tracker ({TRACKER_DIR_NAME}) needs a [track] table")
        if source.sha256 is not None or source.sha256_from is not None:
            collector.error("source", "a tracker has no sha256 or sha256_from: check-updates sets them from a discovered checksum")

    if collector.problems:
        raise ManifestError(collector.problems)
    return Manifest(
        kind=kind,
        description=description,
        project_url=project_url,
        modes=modes,
        requires=requires,
        record_manifest=True if record_manifest is None else record_manifest,
        source=source,
        track=track,
        prepare=prepare,
        portable=portable,
        nonportable=nonportable,
        shortcuts=shortcuts,
        envs=envs,
        paths=paths,
        shims=shims,
    )


def _parse_modes(collector: _Collector, data: Mapping[str, Any]) -> tuple[str, ...]:
    """Parse ``modes``: a non-empty list of distinct known modes, default system and user."""
    if "modes" not in data:
        return DEFAULT_MODES
    value = data["modes"]
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        collector.error("modes", f"must be a non-empty array of {', '.join(MODES)}")
        return DEFAULT_MODES
    bad = [item for item in value if item not in MODES]
    for item in bad:
        collector.error("modes", f"unknown mode '{item}'{_hint(item, MODES)}")
    if len(set(value)) != len(value):
        collector.error("modes", "lists a mode twice")
    return tuple(value) if not bad else DEFAULT_MODES


def _parse_source(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> Source:
    """Parse ``[source]``: one table that says how ``App`` is populated."""
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
    sha256 = collector.string(table, "sha256", "source")
    if sha256 is not None:
        if re.fullmatch(r"[0-9A-Fa-f]{64}", sha256):
            sha256 = sha256.lower()
        else:
            collector.error("source.sha256", "must be 64 hexadecimal characters")
            sha256 = None
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
    require = tuple(
        item for item in (
            _relative_path(collector, value, "source.require") for value in collector.strings(table, "require", "source")
        ) if item is not None
    )
    verify_manifest = collector.string(table, "verify_manifest", "source")
    if verify_manifest is not None:
        verify_manifest = _relative_path(collector, verify_manifest, "source.verify_manifest")
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
    installer = _parse_installer(collector, table.get("installer"), modes)
    git = _parse_git(collector, table.get("git"), kind)
    nested = _parse_nested(collector, table.get("nested"))
    extract = _parse_extract(collector, table.get("extract"))

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
    if sha256_from is not None and sha256 is None:
        collector.error("source.sha256_from", "needs a sha256")
    if kind == "git":
        if download_url is None:
            collector.error("source.kind", "'git' needs download_url (the repository URL)")
        if git is None or (git.ref is None and git.commit is None):
            collector.error("source.git", "a git source needs a ref or a commit")
    elif git is not None:
        collector.error("source.git", "is valid only with kind = \"git\"")
    return Source(
        download_url=download_url, script=script, shipped_declared=shipped, sha256=sha256,
        sha256_from=sha256_from, kind=kind, strip=strip, subdir=subdir, require=require,
        verify_manifest=verify_manifest, checksum=checksum, installer=installer, git=git,
        nested=nested, extract=extract, headers=headers,
    )


def _parse_checksum(collector: _Collector, raw: Any) -> Checksum | None:
    """Parse ``[source.checksum]``: exactly one discovery method."""
    if raw is None:
        return None
    table = collector.table(raw, "source.checksum")
    if table is None:
        return None
    collector.keys(table, _CHECKSUM_KEYS, "source.checksum")
    values = {key: collector.string(table, key, "source.checksum") for key in _CHECKSUM_KEYS}
    methods = [
        name for name, present in (
            ("sidecar", values["sidecar"]), ("sums_file", values["sums_file"]),
            ("json", values["json"]), ("page_match", values["page_match"]),
        ) if present is not None
    ]
    if len(methods) != 1:
        collector.error("source.checksum", "needs exactly one of sidecar, sums_file, json or page_match")
    elif methods[0] == "sums_file" and values["entry"] is None:
        collector.error("source.checksum", "sums_file needs an entry")
    elif methods[0] == "json" and values["path"] is None:
        collector.error("source.checksum", "json needs a path")
    if values["entry"] is not None and methods != ["sums_file"]:
        collector.error("source.checksum.entry", "belongs to sums_file")
    if values["path"] is not None and methods != ["json"]:
        collector.error("source.checksum.path", "belongs to json")
    return Checksum(**values)


def _parse_installer(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> Installer | None:
    """Parse ``[source.installer]``: the arguments of a vendor installer."""
    if raw is None:
        return None
    table = collector.table(raw, "source.installer")
    if table is None:
        return None
    collector.keys(table, ("run",), "source.installer")
    run = collector.strings(table, "run", "source.installer")
    if "run" not in table:
        collector.error("source.installer.run", "is required")
    for text in run:
        _check_variables(collector, text, "source.installer.run", modes, installer=True)
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
        if src is not None and dest is not None:
            src = _relative_path(collector, src, f"{where}.src")
            dest = _relative_path(collector, dest, f"{where}.dest", empty=True)
            if src is not None and dest is not None:
                rows.append(ExtractRule(src, dest))
    return tuple(rows)


def _parse_track(collector: _Collector, raw: Any) -> Track | None:
    """Parse ``[track]``; valid only in a tracker."""
    if raw is None:
        return None
    if collector.kind is ManifestKind.VERSION:
        collector.error(
            "track",
            f"a version directory has no [track] table: update discovery lives only in {TRACKER_DIR_NAME}",
        )
        return None
    table = collector.table(raw, "track")
    if table is None:
        return None
    provider = collector.string(table, "provider", "track", required=True)
    if provider is None:
        return None
    if provider not in _TRACK_PROVIDERS:
        collector.error("track.provider", f"unknown provider '{provider}'{_hint(provider, tuple(_TRACK_PROVIDERS))}")
        return None
    location = _TRACK_LOCATION[provider]
    allowed = ("provider", *location, *_TRACK_PROVIDERS[provider])
    for key in ("check_url", "repo", "repo_url"):
        if key in table and key not in location:
            collector.error(f"track.{key}", f"is not a key of the '{provider}' provider")
    collector.keys({k: v for k, v in table.items() if k not in ("check_url", "repo", "repo_url") or k in location},
                   allowed, "track")
    values = {key: collector.string(table, key, "track") for key in ("check_url", "repo", "repo_url") if key in location}
    if location and not any(table.get(key) for key in location):
        collector.error("track", f"the '{provider}' provider needs {' or '.join(location)}")
    check_url = values.get("check_url")
    if check_url is not None and not re.match(r"https?://\S", check_url, re.IGNORECASE):
        collector.error("track.check_url", "must be an http:// or https:// URL")
    options = {
        key: value for key, value in table.items()
        if key in _TRACK_PROVIDERS[provider] and key not in ("check_url", "repo", "repo_url")
    }
    if provider == "module":
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
    steps: list[PrepareStep] = []
    for index, row in collector.rows(raw, "prepare"):
        where = f"prepare[{index}]"
        collector.keys(row, _PREPARE_KEYS, where)
        present = [key for key in _PREPARE_KEYS if key in row]
        if len(present) != 1:
            collector.error(where, f"needs exactly one of {', '.join(_PREPARE_KEYS)}")
            continue
        step = _parse_prepare_step(collector, present[0], row[present[0]], f"{where}.{present[0]}")
        if step is not None:
            steps.append(step)
    return tuple(steps)


def _parse_prepare_step(collector: _Collector, name: str, value: Any, where: str) -> PrepareStep | None:
    """Parse the value of one ``[[prepare]]`` step."""
    if name == "remove":
        if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
            collector.error(where, "must be a non-empty array of paths")
            return None
        paths = [_relative_path(collector, item, where) for item in value]
        return PrepareRemove(tuple(paths)) if None not in paths else None
    if name == "module":
        if not isinstance(value, str):
            collector.error(where, "must be a string")
            return None
        module = _local_script(collector, value, where, suffixes=(".py",))
        return PrepareModule(module) if module is not None else None
    table = collector.table(value, where)
    if table is None:
        return None
    if name == "rename":
        collector.keys(table, ("from", "to"), where)
        source, target = collector.string(table, "from", where, required=True), collector.string(table, "to", where, required=True)
        if source is None or target is None:
            return None
        source, target = _relative_path(collector, source, f"{where}.from"), _relative_path(collector, target, f"{where}.to")
        return PrepareRename(source, target) if source is not None and target is not None else None
    if name == "copy":
        collector.keys(table, ("from", "to"), where)
        source = collector.string(table, "from", where, required=True)
        targets = table.get("to")
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or not targets or any(not isinstance(item, str) for item in targets):
            collector.error(f"{where}.to", "must be a path or a non-empty array of paths")
            return None
        if source is None:
            return None
        source = _relative_path(collector, source, f"{where}.from")
        checked = [_relative_path(collector, item, f"{where}.to") for item in targets]
        return PrepareCopy(source, tuple(checked)) if source is not None and None not in checked else None
    collector.keys(table, ("path", "content"), where)
    path = collector.string(table, "path", where, required=True)
    content = collector.string(table, "content", where, required=True, empty=True)
    if path is None or content is None:
        return None
    path = _relative_path(collector, path, f"{where}.path")
    return PrepareWrite(path, content) if path is not None else None


def _parse_portable(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> Portable | None:
    """Parse ``[portable]``; it needs ``portable`` in ``modes``."""
    if raw is None:
        return None
    table = collector.table(raw, "portable")
    if table is None:
        return None
    collector.keys(table, ("persist", "markers", "args", "env"), "portable")
    if "portable" not in modes:
        collector.error("portable", "needs 'portable' in modes")
    persist = tuple(
        item for item in (_relative_path(collector, value, "portable.persist")
                          for value in collector.strings(table, "persist", "portable")) if item is not None
    )
    markers: list[Marker] = []
    raw_markers = table.get("markers", [])
    if not isinstance(raw_markers, list):
        collector.error("portable.markers", "must be an array")
        raw_markers = []
    for index, item in enumerate(raw_markers):
        where = f"portable.markers[{index}]"
        if isinstance(item, str):
            path, content = item, None
        elif isinstance(item, dict):
            collector.keys(item, ("path", "content"), where)
            path = collector.string(item, "path", where, required=True)
            content = collector.string(item, "content", where, empty=True)
        else:
            collector.error(where, "must be a path or a { path, content } table")
            continue
        if path is not None and _relative_path(collector, path, where) is not None:
            markers.append(Marker(path, content))
    args = collector.strings(table, "args", "portable")
    env: dict[str, str] = {}
    raw_env = table.get("env", {})
    if not isinstance(raw_env, dict) or any(not isinstance(v, str) for v in raw_env.values()):
        collector.error("portable.env", "must be a table of strings")
    else:
        env = dict(raw_env)
    for text in (*args, *env.values()):
        _check_variables(collector, text, "portable", modes)
    return Portable(persist, tuple(markers), args, env)


def _parse_nonportable(collector: _Collector, raw: Any) -> NonPortable | None:
    """Parse ``[nonportable]``: what to remove when the package is not installed portable."""
    if raw is None:
        return None
    table = collector.table(raw, "nonportable")
    if table is None:
        return None
    collector.keys(table, ("remove",), "nonportable")
    remove = tuple(
        item for item in (_relative_path(collector, value, "nonportable.remove")
                          for value in collector.strings(table, "remove", "nonportable")) if item is not None
    )
    return NonPortable(remove)


def _parse_shortcuts(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> tuple[Shortcut, ...]:
    """Parse ``[[shortcut]]``."""
    rows = []
    for index, row in collector.rows(raw, "shortcut"):
        where = f"shortcut[{index}]"
        collector.keys(row, _SHORTCUT_KEYS, where)
        name = collector.string(row, "name", where, required=True)
        target = collector.string(row, "target", where, required=True)
        optional = {key: collector.string(row, key, where, empty=True) or "" for key in _SHORTCUT_KEYS[2:]}
        for key in ("target", "arguments", "working_dir", "icon_location"):
            text = target if key == "target" else optional[key]
            if text:
                _check_variables(collector, text, f"{where}.{key}", modes)
        if name is not None and target is not None:
            rows.append(Shortcut(name, target, **optional))
    return tuple(rows)


def _parse_envs(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> tuple[EnvVar, ...]:
    """Parse ``[[env]]``."""
    rows = []
    for index, row in collector.rows(raw, "env"):
        where = f"env[{index}]"
        collector.keys(row, ("name", "value"), where)
        name = collector.string(row, "name", where, required=True)
        value = collector.string(row, "value", where, required=True)
        if value is not None:
            _check_variables(collector, value, f"{where}.value", modes)
        if name is not None and re.search(r"[=\x00\s]", name):
            collector.error(f"{where}.name", f"{name!r} is not a valid environment variable name")
            name = None
        if name is not None and value is not None:
            rows.append(EnvVar(name, value))
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
        if value is not None:
            _check_variables(collector, value, f"{where}.value", modes)
            values.append(value)
    return tuple(values)


def _parse_shims(collector: _Collector, raw: Any, modes: tuple[str, ...]) -> tuple[Shim, ...]:
    """Parse ``[[shim]]``: a launcher with a target, or a raw script."""
    rows = []
    for index, row in collector.rows(raw, "shim"):
        where = f"shim[{index}]"
        collector.keys(row, _SHIM_KEYS, where)
        name = collector.string(row, "name", where, required=True)
        if name is not None and re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
            collector.error(f"{where}.name", f"{name!r} must be a plain file name")
            name = None
        content = collector.string(row, "content", where, empty=True)
        target = collector.string(row, "target", where)
        if content is not None:
            if any(key in row for key in ("target", "type", "arguments", "forward_args", "elevate", "working_dir")):
                collector.error(where, "content cannot be combined with shim options")
            if name is not None:
                rows.append(Shim(name=name, content=content))
            continue
        if target is None and "target" not in row:
            collector.error(where, "needs a target or content")
        shim_type = collector.string(row, "type", where)
        if shim_type is not None and shim_type not in SHIM_TYPES:
            collector.error(f"{where}.type", f"must be one of {', '.join(SHIM_TYPES)}")
            shim_type = None
        arguments = collector.strings(row, "arguments", where)
        forward_args = collector.boolean(row, "forward_args", where)
        elevate = collector.boolean(row, "elevate", where)
        working_dir = collector.string(row, "working_dir", where)
        for key, text in (("target", target), ("working_dir", working_dir), *(("arguments", a) for a in arguments)):
            if text:
                _check_variables(collector, text, f"{where}.{key}", modes)
        if name is not None and target is not None:
            rows.append(Shim(
                name=name, target=target, type=shim_type or "console", arguments=arguments,
                forward_args=True if forward_args is None else forward_args,
                elevate=bool(elevate), working_dir=working_dir,
            ))
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
    data: dict[str, Any] = {
        "download_url": source.download_url, "script": source.script, "sha256": source.sha256,
        "sha256_from": source.sha256_from, "kind": source.kind, "strip": source.strip, "subdir": source.subdir,
        "shipped": source.shipped_declared, "require": list(source.require),
        "verify_manifest": source.verify_manifest,
    }
    if source.checksum is not None:
        data["checksum"] = _drop_empty(
            {key: getattr(source.checksum, key) for key in _CHECKSUM_KEYS}
        )
    if source.installer is not None:
        data["installer"] = {"run": list(source.installer.run)}
    if source.git is not None:
        data["git"] = _drop_empty({"ref": source.git.ref, "commit": source.git.commit})
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


def manifest_to_data(manifest: Manifest) -> dict[str, Any]:
    """Return *manifest* as plain TOML-ready data in the documented key order.

    Parameters
    ----------
    manifest : Manifest
        A validated manifest.

    Returns
    -------
    dict
        Data that ``parse_manifest_data`` accepts and ``tomli_w`` can write.
    """
    data: dict[str, Any] = {"format": FORMAT, "description": manifest.description, "project_url": manifest.project_url}
    data["modes"] = list(manifest.modes) if manifest.modes != DEFAULT_MODES else None
    data["requires"] = list(manifest.requires)
    data["record_manifest"] = None if manifest.record_manifest else False
    if manifest.track is not None:
        track = {"provider": manifest.track.provider, "check_url": manifest.track.check_url,
                 "repo": manifest.track.repo, "repo_url": manifest.track.repo_url}
        track.update(manifest.track.options)
        data["track"] = _drop_empty(track)
    source = _source_to_data(manifest.source)
    if source or manifest.source.shipped_declared is not None:
        data["source"] = source
    if manifest.prepare:
        data["prepare"] = [_prepare_to_data(step) for step in manifest.prepare]
    if manifest.portable is not None:
        portable = manifest.portable
        data["portable"] = _drop_empty({
            "persist": list(portable.persist),
            "markers": [m.path if m.content is None else {"path": m.path, "content": m.content} for m in portable.markers],
            "args": list(portable.args), "env": dict(portable.env),
        })
    if manifest.nonportable is not None:
        data["nonportable"] = _drop_empty({"remove": list(manifest.nonportable.remove)})
    data["shortcut"] = [
        _drop_empty({"name": s.name, "target": s.target, "arguments": s.arguments or None,
                     "working_dir": s.working_dir or None, "icon_location": s.icon_location or None,
                     "description": s.description or None})
        for s in manifest.shortcuts
    ]
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


def manifest_to_toml(manifest: Manifest) -> str:
    """Return the TOML text of *manifest* (written with ``tomli_w``).

    Parameters
    ----------
    manifest : Manifest
        A validated manifest.

    Returns
    -------
    str
        TOML text that ``parse_manifest`` reads back to an equal manifest.
    """
    return tomli_w.dumps(manifest_to_data(manifest))


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
    is removed, ``shipped`` is written explicitly, and ``sha256`` and ``sha256_from``
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
    if tracker.kind is not ManifestKind.TRACKER or tracker.track is None:
        raise ManifestError("render_pinned needs the manifest of a tracker with a [track] table")
    if not is_version_name(result.version):
        raise ManifestError(f"The discovered version {result.version!r} cannot name a version directory")
    values = {**result.captures, "version": result.version}
    data = manifest_to_data(tracker)
    data.pop("track", None)
    data = _render_data(data, values)

    source = data.setdefault("source", {})
    source["shipped"] = tracker.source.shipped
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
    return parse_manifest_data(data, ManifestKind.VERSION)
