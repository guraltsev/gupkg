"""Validate the values of a ``[track]`` table and turn them into typed provider specifications.

``gupkg.model.manifest`` checks the shape of ``[track]`` (which keys each provider may
have). This module checks the values: regular expressions compile, are safe and define
the named groups the provider needs; JSONPath expressions parse; templates refer only
to names the provider produces; choices are among the allowed words; locations are
well-formed. The same function that validates builds the specification that
``gupkg.services.track`` executes, so a manifest that validates can be run.

Usage and API
-------------
``parse_spec(provider, location, options)`` returns the provider's frozen
specification dataclass or raises ``TrackSpecError``, whose ``problems`` list holds
``(key, message)`` pairs. ``provider_captures(spec)`` lists the capture names a
provider produces (``None`` for the ``module`` provider, which can produce any), so a
tracker's ``[source]`` templates can be checked against them. ``check_header_map``
validates a ``headers`` table.

Implementation Approach
-----------------------
Each provider has a small parser that collects every problem instead of stopping at
the first, so the author sees them together. Regular expressions go through
``gupkg.model.patterns`` (shape and group checks) and paths through
``gupkg.model.jsonpath``. Templates are checked with ``gupkg.model.template``. The
module is pure: it reads no file and opens no connection.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .jsonpath import JsonPath, JsonPathError, parse_condition, parse_path
from .patterns import PatternError, compile_pattern, escape_template
from .template import TemplateError, references

SELECTS = ("max", "first", "unique")
MAX_LEVELS = 6
MAX_STEPS = 8
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Headers the transport owns or that change how a message is framed or routed: a manifest may not set them.
FORBIDDEN_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "upgrade", "te", "trailer",
    "expect", "accept-encoding", "content-encoding", "range", "if-range", "via", "forwarded",
})
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_STRFTIME_DIRECTIVES = frozenset("YmdyHMSj%")
_SHA256 = re.compile(r"[0-9A-Fa-f]{64}")

# Captures that a provider adds on its own, next to the named groups of its patterns.
BUILTIN_CAPTURES: dict[str, tuple[str, ...]] = {
    "github": ("tag", "asset_name", "asset_url", "release_url"),
    "gitlab": ("tag", "asset_name", "asset_url", "release_url"),
    "page": ("page_url", "download_url"),
    "index": ("page_url", "download_url"),
    "redirect": ("page_url", "download_url", "final_url", "file_name"),
    "json": ("download_url",),
    "last_modified": ("download_url", "last_modified"),
    "rss": ("page_url", "download_url"),
    "git": ("commit", "short_commit", "ref", "commit_date"),
    "chain": ("page_url", "download_url"),
}


class TrackSpecError(ValueError):
    """Report every invalid value of one ``[track]`` table as ``(key, message)`` pairs."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        """Build the error from the collected problems."""
        self.problems = list(problems)
        super().__init__("; ".join(f"{key}: {message}" for key, message in problems))


@dataclass(frozen=True)
class Level:
    """One ``[[track.level]]`` of an ``index`` provider."""

    match: re.Pattern[str]
    match_text: str = ""
    select: str = "max"
    key: str | None = None
    next: str | None = None
    probe: str | None = None


@dataclass(frozen=True)
class Step:
    """One ``[[track.fetch]]`` step of a ``chain`` provider."""

    url: str
    match: re.Pattern[str] | None = None
    match_text: str | None = None
    select: str = "max"
    header: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    optional: bool = False


@dataclass(frozen=True)
class GithubSpec:
    """``provider = "github"``: releases of one repository."""

    repo: str
    asset: str | None = None
    asset_pattern: str | None = None
    version_from: str = "tag"
    tag_pattern: re.Pattern[str] | None = None
    tag_prefix: str | None = None
    release: str = "latest"
    prerelease: bool = False
    version: str = "${version}"


@dataclass(frozen=True)
class GitlabSpec:
    """``provider = "gitlab"``: releases of one project."""

    base: str
    project: str
    link_name: str | None = None
    asset_pattern: str | None = None
    tag_pattern: re.Pattern[str] | None = None
    tag_prefix: str | None = None
    version: str = "${version}"


@dataclass(frozen=True)
class PageSpec:
    """``provider = "page"``: a regular expression over one page."""

    check_url: str
    match: re.Pattern[str]
    select: str = "max"
    version: str = "${version}"
    download: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexSpec:
    """``provider = "index"``: a walk down directory listings."""

    check_url: str
    levels: tuple[Level, ...]
    version: str = "${version}"
    download: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RedirectSpec:
    """``provider = "redirect"``: the final address of a stable download link."""

    check_url: str
    match: re.Pattern[str] | None = None
    unwrap_query: str | None = None
    version: str = "${version}"
    download: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JsonSpec:
    """``provider = "json"``: values read from a JSON document with JSONPath."""

    check_url: str
    version: JsonPath
    records: JsonPath
    filters: tuple[str, ...] = ()
    match: re.Pattern[str] | None = None
    download: JsonPath | None = None
    checksum: JsonPath | None = None
    captures: Mapping[str, JsonPath] = field(default_factory=dict)
    select: str = "max"
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LastModifiedSpec:
    """``provider = "last_modified"``: the date of a download as its version."""

    check_url: str
    format: str = "%Y.%m.%d"
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RssSpec:
    """``provider = "rss"``: a regular expression over the entries of a feed."""

    check_url: str
    match: re.Pattern[str]
    select: str = "max"
    version: str = "${version}"
    download: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class GitSpec:
    """``provider = "git"``: the newest commit of a ref."""

    repo_url: str
    ref: str = "HEAD"


@dataclass(frozen=True)
class ChainSpec:
    """``provider = "chain"``: fetch steps whose captures feed the next step."""

    steps: tuple[Step, ...]
    version: str = "${version}"
    download: str | None = None


@dataclass(frozen=True)
class ModuleSpec:
    """``provider = "module"``: a ``track(ctx)`` function in ``pkg.local``."""

    module: str


TrackProviderSpec = (
    GithubSpec | GitlabSpec | PageSpec | IndexSpec | RedirectSpec | JsonSpec | LastModifiedSpec
    | RssSpec | GitSpec | ChainSpec | ModuleSpec
)


def check_header_map(headers: Mapping[str, str] | None, where: str = "headers") -> dict[str, str]:
    """Return *headers* as a plain dict after rejecting names and values that could split a request.

    Raises
    ------
    ValueError
        If a name is not an HTTP token, is a framing or hop-by-hop header (``Host``,
        ``Content-Length``, ``Connection``, ``Proxy-*``...), or a name or value has a control character.
    """
    checked: dict[str, str] = {}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError(f"{where}: names and values must be strings")
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError(f"{where}: {name!r} is not a valid header name")
        if name.lower() in FORBIDDEN_HEADERS or name.lower().startswith("proxy-"):
            raise ValueError(f"{where}: {name!r} is set by gupkg's transport and cannot be given here")
        if any((ord(character) < 32 and character != "\t") or ord(character) == 127 for character in value):
            raise ValueError(f"{where}: the value of {name!r} has a control character")
        checked[name] = value
    return checked


def parse_spec(provider: str, location: Mapping[str, str | None], options: Mapping[str, Any]) -> TrackProviderSpec:
    """Validate the values of one ``[track]`` table and return its specification.

    Parameters
    ----------
    provider : str
        The provider name (already known to be one of the supported names).
    location : mapping
        The values of ``check_url``, ``repo`` and ``repo_url`` (``None`` when absent).
    options : mapping
        Every other key of the table.

    Returns
    -------
    TrackProviderSpec
        The frozen specification of that provider.

    Raises
    ------
    TrackSpecError
        With every problem found.
    """
    problems = _Problems()
    builder = _BUILDERS[provider]
    spec = builder(_View(location, options, problems))
    if problems.items:
        raise TrackSpecError(problems.items)
    return spec


def provider_captures(spec: TrackProviderSpec) -> frozenset[str] | None:
    """Return the capture names *spec* produces, ``version`` included; ``None`` for a ``module`` provider."""
    if isinstance(spec, ModuleSpec):
        return None
    names = {"version"}
    for provider, cls in _SPEC_PROVIDER.items():
        if isinstance(spec, cls):
            names.update(BUILTIN_CAPTURES.get(provider, ()))
    for pattern in _patterns_of(spec):
        names.update(pattern.groupindex)
    if isinstance(spec, JsonSpec):
        names.update(spec.captures)
    if isinstance(spec, GitSpec):
        names.add("commit")
    return frozenset(names)


# ---------------------------------------------------------------------------
# Collecting problems and reading values
# ---------------------------------------------------------------------------


class _Problems:
    """Collect ``(key, message)`` pairs."""

    def __init__(self) -> None:
        """Start empty."""
        self.items: list[tuple[str, str]] = []

    def add(self, key: str, message: str) -> None:
        """Record one problem."""
        self.items.append((key, message))


class _View:
    """Read typed values out of one ``[track]`` table, recording a problem for each wrong one."""

    def __init__(self, location: Mapping[str, str | None], options: Mapping[str, Any], problems: _Problems) -> None:
        """Wrap the location values and the provider options."""
        self.location = location
        self.options = options
        self.problems = problems

    def error(self, key: str, message: str) -> None:
        """Record a problem for *key*."""
        self.problems.add(f"track.{key}", message)

    def string(self, key: str, *, required: bool = False, default: str | None = None) -> str | None:
        """Return a non-empty string option, or *default*."""
        value = self.options.get(key)
        if value is None:
            if required:
                self.error(key, "is required")
            return default
        if not isinstance(value, str) or not value.strip():
            self.error(key, "must be a non-empty string")
            return default
        return value

    def choice(self, key: str, allowed: tuple[str, ...], default: str) -> str:
        """Return an option that must be one of *allowed*."""
        value = self.string(key, default=default)
        if value not in allowed:
            self.error(key, f"must be one of {', '.join(allowed)}")
            return default
        return value or default

    def boolean(self, key: str, default: bool) -> bool:
        """Return a boolean option."""
        value = self.options.get(key, default)
        if not isinstance(value, bool):
            self.error(key, "must be true or false")
            return default
        return value

    def headers(self) -> dict[str, str]:
        """Return the ``headers`` table."""
        raw = self.options.get("headers")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            self.error("headers", "must be a table of header names and values")
            return {}
        try:
            return check_header_map(raw)
        except ValueError as error:
            self.error("headers", str(error))
            return {}

    def pattern(self, key: str, *, groups: tuple[str, ...] = (), required: bool = True, text: str | None = None,
                substitute: Mapping[str, str] | None = None) -> re.Pattern[str] | None:
        """Compile a regular expression option, checking its safety and groups."""
        source = text if text is not None else self.string(key, required=required)
        if source is None:
            return None
        try:
            return compile_pattern(escape_template(source, substitute) if substitute else source, groups)
        except PatternError as error:
            self.error(key, str(error))
            return None

    def path(self, key: str, *, required: bool = False, default: str | None = None) -> JsonPath | None:
        """Parse a JSONPath option."""
        text = self.string(key, required=required, default=default)
        if text is None:
            return None
        try:
            return parse_path(text)
        except JsonPathError as error:
            self.error(key, str(error))
            return None

    def template(self, key: str, allowed: set[str], *, default: str | None = None) -> str | None:
        """Return a template option after checking that it only names *allowed* captures."""
        value = self.string(key, default=default)
        if value is not None:
            self.check_template(key, value, allowed)
        return value

    def check_template(self, key: str, value: str, allowed: set[str]) -> None:
        """Record a problem for a malformed template or a reference to an unknown capture."""
        try:
            found = references(value)
        except TemplateError as error:
            self.error(key, str(error))
            return
        unknown = sorted({ref.name for ref in found} - allowed - {"version"})
        if unknown:
            self.error(key, f"refers to ${{{unknown[0]}}}, which this provider does not produce; known: {', '.join(sorted(allowed))}")

    def check_url(self) -> str:
        """Return the required ``check_url`` (shape was validated by the manifest parser)."""
        return str(self.location.get("check_url") or "")


def _needs_version(view: _View, key: str, template: str, groups: set[str]) -> None:
    """Record a problem when the version template is the default but no pattern defines ``version``."""
    if template == "${version}" and "version" not in groups and key not in view.options:
        view.error(key, "the pattern must define (?P<version>...), or give a version template")


# ---------------------------------------------------------------------------
# Provider parsers
# ---------------------------------------------------------------------------


def _github(view: _View) -> GithubSpec:
    """Validate ``github``."""
    source = view.location.get("repo") or view.location.get("repo_url") or ""
    repo = _github_repo(source)
    if repo is None:
        view.error("repo", "must be 'owner/name' or a https://github.com/owner/name URL")
    asset, asset_pattern = view.string("asset"), view.string("asset_pattern")
    if asset is not None and asset_pattern is not None:
        view.error("asset", "give asset or asset_pattern, not both")
    version_from = view.choice("version_from", ("tag", "asset"), "tag")
    release = view.choice("release", ("latest", "newest-matching"), "latest")
    prerelease = view.boolean("prerelease", False)
    if prerelease and release == "latest":
        view.error("prerelease", 'needs release = "newest-matching": the latest release is never a prerelease')
    tag_prefix = view.string("tag_prefix")
    tag_pattern = view.pattern("tag_pattern", required=False)
    names = {"version"} | set(tag_pattern.groupindex if tag_pattern else ()) | _group_names(view, "asset_pattern", asset_pattern)
    version = view.template("version", names | set(BUILTIN_CAPTURES["github"]), default="${version}") or "${version}"
    if version_from == "asset":
        if asset_pattern is None:
            view.error("version_from", 'asset needs an asset_pattern with a (?P<version>...) group')
        elif "version" not in _group_names(view, "asset_pattern", asset_pattern):
            view.error("asset_pattern", "must define (?P<version>...) when version_from = \"asset\"")
    if asset is not None:
        view.check_template("asset", asset, names | set(BUILTIN_CAPTURES["github"]))
    return GithubSpec(repo or "", asset, asset_pattern, version_from, tag_pattern, tag_prefix, release, prerelease, version)


def _gitlab(view: _View) -> GitlabSpec:
    """Validate ``gitlab``."""
    project = view.string("project")
    source = view.location.get("repo_url") or view.location.get("repo") or ""
    base = "https://gitlab.com"
    if project is None:
        match = re.fullmatch(r"(https://[A-Za-z0-9.-]+(?::\d+)?)/([A-Za-z0-9_.\-/]+?)(?:\.git)?/?", source)
        if match:
            base, project = match.group(1), match.group(2)
        elif re.fullmatch(r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+", source):
            project = source
        else:
            view.error("project", "give project, or repo as 'group/name' or a https://host/group/name URL")
    elif not re.fullmatch(r"\d+|[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+", project):
        view.error("project", "must be a numeric id or 'group/name'")
    link_name, asset_pattern = view.string("link_name"), view.string("asset_pattern")
    if link_name is not None and asset_pattern is not None:
        view.error("link_name", "give link_name or asset_pattern, not both")
    tag_pattern = view.pattern("tag_pattern", required=False)
    names = {"version"} | set(tag_pattern.groupindex if tag_pattern else ()) | _group_names(view, "asset_pattern", asset_pattern)
    version = view.template("version", names | set(BUILTIN_CAPTURES["gitlab"]), default="${version}") or "${version}"
    return GitlabSpec(base, project or "", link_name, asset_pattern, tag_pattern, view.string("tag_prefix"), version)


def _page(view: _View) -> PageSpec:
    """Validate ``page``."""
    match = view.pattern("match")
    groups = set(match.groupindex) if match else set()
    allowed = groups | set(BUILTIN_CAPTURES["page"])
    version = view.template("version", allowed, default="${version}") or "${version}"
    _needs_version(view, "version", version, groups)
    download = view.template("download", allowed)
    select = view.choice("select", SELECTS, "max")
    return PageSpec(view.check_url(), match or re.compile(""), select, version, download, view.headers())


def _index(view: _View) -> IndexSpec:
    """Validate ``index`` and its ``[[track.level]]`` rows."""
    raw = view.options.get("level")
    levels: list[Level] = []
    groups: set[str] = set()
    if not isinstance(raw, list) or not raw or any(not isinstance(row, dict) for row in raw):
        view.error("level", "needs at least one [[track.level]] table")
        raw = []
    if len(raw) > MAX_LEVELS:
        view.error("level", f"has more than {MAX_LEVELS} levels")
        raw = raw[:MAX_LEVELS]
    for position, row in enumerate(raw):
        sub = _View({}, row, view.problems)
        sub.error = lambda key, message, _n=position: view.error(f"level[{_n}].{key}", message)  # type: ignore[method-assign]
        unknown = sorted(set(row) - {"match", "select", "key", "next", "probe"})
        for key in unknown:
            sub.error(key, "is not a key of a level; use match, select, key, next or probe")
        # A level's pattern may refer to what earlier levels captured: ``${version}``.
        match_text = sub.string("match", required=True)
        match = sub.pattern("match", required=False, text=match_text, substitute={name: "x" for name in groups | {"version"}})
        if match:
            groups.update(match.groupindex)
        levels.append(Level(
            match or re.compile(""), match_text or "", sub.choice("select", ("max", "first"), "max"),
            sub.string("key"), sub.string("next"), sub.string("probe"),
        ))
        if position < len(raw) - 1 and "next" not in row:
            sub.error("next", "is required: it names the page of the next level")
    allowed = groups | set(BUILTIN_CAPTURES["index"])
    for position, level in enumerate(levels):
        for key in ("key", "next", "probe"):
            text = getattr(level, key)
            if text is not None:
                view.check_template(f"level[{position}].{key}", text, allowed)
    version = view.template("version", allowed, default="${version}") or "${version}"
    _needs_version(view, "version", version, groups)
    return IndexSpec(view.check_url(), tuple(levels), version, view.template("download", allowed), view.headers())


def _redirect(view: _View) -> RedirectSpec:
    """Validate ``redirect``."""
    match = view.pattern("match", required=False)
    groups = set(match.groupindex) if match else set()
    allowed = groups | set(BUILTIN_CAPTURES["redirect"])
    version = view.template("version", allowed, default="${version}") or "${version}"
    _needs_version(view, "version", version, groups)
    unwrap = view.string("unwrap_query")
    if unwrap is not None and not re.fullmatch(r"[A-Za-z0-9_.\-]+", unwrap):
        view.error("unwrap_query", "must be a query parameter name")
    return RedirectSpec(view.check_url(), match, unwrap, version, view.template("download", allowed), view.headers())


def _json(view: _View) -> JsonSpec:
    """Validate ``json``."""
    version = view.path("version", required=True)
    records = view.path("records", default="$")
    raw_filter = view.options.get("filter")
    filters: list[str] = []
    if raw_filter is not None:
        items = [raw_filter] if isinstance(raw_filter, str) else raw_filter
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            view.error("filter", "must be a condition or an array of conditions such as 'stable==true'")
            items = []
        for item in items:
            try:
                parse_condition(item)
                filters.append(item)
            except JsonPathError as error:
                view.error("filter", str(error))
    match = view.pattern("match", required=False)
    captures: dict[str, JsonPath] = {}
    raw_captures = view.options.get("captures")
    if raw_captures is not None:
        if not isinstance(raw_captures, dict):
            view.error("captures", "must be a table of capture names and paths")
        else:
            for name, text in raw_captures.items():
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name)) or not isinstance(text, str):
                    view.error("captures", f"{name!r} must map a capture name to a path")
                    continue
                try:
                    captures[name] = parse_path(text)
                except JsonPathError as error:
                    view.error("captures", str(error))
    return JsonSpec(
        view.check_url(), version or parse_path("$"), records or parse_path("$"), tuple(filters), match,
        view.path("download"), view.path("checksum"), captures, view.choice("select", ("max", "first"), "max"),
        view.headers(),
    )


def _last_modified(view: _View) -> LastModifiedSpec:
    """Validate ``last_modified``."""
    text = view.string("format", default="%Y.%m.%d") or "%Y.%m.%d"
    position = 0
    while position < len(text):
        character = text[position]
        if character == "%":
            if text[position + 1 : position + 2] not in _STRFTIME_DIRECTIVES or position + 1 >= len(text):
                view.error("format", "allows only %Y %m %d %y %H %M %S %j and %%")
                break
            position += 2
            continue
        if not re.fullmatch(r"[0-9A-Za-z._\-]", character):
            view.error("format", f"{character!r} cannot appear in a version name")
            break
        position += 1
    return LastModifiedSpec(view.check_url(), text, view.headers())


def _rss(view: _View) -> RssSpec:
    """Validate ``rss``."""
    match = view.pattern("match")
    groups = set(match.groupindex) if match else set()
    allowed = groups | set(BUILTIN_CAPTURES["rss"])
    version = view.template("version", allowed, default="${version}") or "${version}"
    _needs_version(view, "version", version, groups)
    return RssSpec(view.check_url(), match or re.compile(""), view.choice("select", SELECTS, "max"), version,
                   view.template("download", allowed), view.headers())


def _git(view: _View) -> GitSpec:
    """Validate ``git``."""
    url = view.location.get("repo_url") or view.location.get("repo") or ""
    if not re.fullmatch(r"(https?|ssh|git)://[^\s]+|[\w.\-]+@[\w.\-]+:[^\s]+", url) or url.startswith("-"):
        view.error("repo_url", "must be an https://, ssh:// or git:// URL, or user@host:path")
    ref = view.string("ref", default="HEAD") or "HEAD"
    if ref != "HEAD" and (not ref.startswith("refs/") or any(ord(c) < 32 or c == " " for c in ref) or ".." in ref):
        view.error("ref", 'must be "HEAD" or a full refs/... name')
    return GitSpec(url, ref)


def _chain(view: _View) -> ChainSpec:
    """Validate ``chain`` and its ``[[track.fetch]]`` rows."""
    raw = view.options.get("fetch")
    steps: list[Step] = []
    groups: set[str] = set()
    if not isinstance(raw, list) or not raw or any(not isinstance(row, dict) for row in raw):
        view.error("fetch", "needs at least one [[track.fetch]] table")
        raw = []
    if len(raw) > MAX_STEPS:
        view.error("fetch", f"has more than {MAX_STEPS} steps")
        raw = raw[:MAX_STEPS]
    seen: set[str] = set()
    for position, row in enumerate(raw):
        sub = _View({}, row, view.problems)
        sub.error = lambda key, message, _n=position: view.error(f"fetch[{_n}].{key}", message)  # type: ignore[method-assign]
        for key in sorted(set(row) - {"url", "match", "select", "header", "headers", "optional"}):
            sub.error(key, "is not a key of a fetch step; use url, match, select, header, headers or optional")
        url = sub.string("url", required=True)
        if url is not None:
            allowed_now = seen | {"version"}
            sub.check_template("url", url, allowed_now | set(BUILTIN_CAPTURES["chain"]))
        match_text = sub.string("match")
        match = sub.pattern("match", required=False, text=match_text, substitute={name: "x" for name in seen | {"version"}})
        if match is None and match_text is None and "header" not in row:
            sub.error("match", "is required")
        if match:
            groups.update(match.groupindex)
            seen.update(match.groupindex)
        select = sub.choice("select", (*SELECTS, "each"), "max")
        if select == "each" and position == len(raw) - 1:
            sub.error("select", 'is "each" on the last step; "each" tries the candidates of a step against the steps after it')
        if select == "each" and any(step.select == "each" for step in steps):
            sub.error("select", 'is "each" on a second step; a chain may try the candidates of one step only')
        steps.append(Step(url or "", match, match_text, select, sub.string("header"),
                          _View({}, row, view.problems).headers(), sub.boolean("optional", False)))
    allowed = groups | set(BUILTIN_CAPTURES["chain"])
    version = view.template("version", allowed, default="${version}") or "${version}"
    _needs_version(view, "version", version, groups)
    return ChainSpec(tuple(steps), version, view.template("download", allowed))


def _module(view: _View) -> ModuleSpec:
    """Validate ``module`` (the path itself was checked by the manifest parser)."""
    return ModuleSpec(str(view.options.get("module", "pkg.local/track.py")))


_BUILDERS = {
    "github": _github, "gitlab": _gitlab, "page": _page, "index": _index, "redirect": _redirect,
    "json": _json, "last_modified": _last_modified, "rss": _rss, "git": _git, "chain": _chain,
    "module": _module,
}
_SPEC_PROVIDER: dict[str, type] = {
    "github": GithubSpec, "gitlab": GitlabSpec, "page": PageSpec, "index": IndexSpec,
    "redirect": RedirectSpec, "json": JsonSpec, "last_modified": LastModifiedSpec, "rss": RssSpec,
    "git": GitSpec, "chain": ChainSpec,
}


def _github_repo(text: str) -> str | None:
    """Return ``owner/name`` from ``owner/name`` or a GitHub URL, or ``None``."""
    if _GITHUB_REPO.fullmatch(text):
        return text
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", text)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _group_names(view: _View, key: str, text: str | None) -> set[str]:
    """Return the named groups of a pattern that may contain ``${...}`` references (checked, not compiled twice)."""
    if text is None:
        return set()
    probe = escape_template(text, {name: "x" for name in ("version", *BUILTIN_CAPTURES["github"])})
    try:
        return set(compile_pattern(probe).groupindex)
    except PatternError as error:
        view.error(key, str(error))
        return set()


def _patterns_of(spec: TrackProviderSpec) -> list[re.Pattern[str]]:
    """Return every compiled pattern in *spec*."""
    patterns: list[re.Pattern[str]] = []
    for name in ("match", "tag_pattern"):
        value = getattr(spec, name, None)
        if isinstance(value, re.Pattern):
            patterns.append(value)
    patterns.extend(level.match for level in getattr(spec, "levels", ()))
    patterns.extend(step.match for step in getattr(spec, "steps", ()) if step.match is not None)
    for name in ("asset_pattern",):
        text = getattr(spec, name, None)
        if isinstance(text, str):
            try:
                patterns.append(compile_pattern(escape_template(text, {"version": "x"})))
            except PatternError:
                pass
    return patterns
