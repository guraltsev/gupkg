"""Convert a 0.17 package directory (format 1) to format 2.

``plan_package(directory)`` reads a version directory or a 0.17 ``_bootstrap`` and
returns a ``PackagePlan``: the new ``pkg.toml`` text, what changed and why, hooks that
match a known service, the other versions of a dissolved ``[[origin.versions]]``, and
the validation errors of the result. Nothing is written. ``apply_plan(plan)`` writes the
backup, the new file, any moved script, the launchers and the ``_check_updates`` rename,
and only when the plan has no error.

The 0.17 file is read as plain TOML by this module's own reader: gupkg core has no format
1 reader (decision D33). The result is checked by ``gupkg.pkgcheck.check_directory``
(the library behind ``gupkg pkg check``) on a staged copy before the original file is
touched, so a result that fails validation leaves the original in place.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gupkg.tomlio import dumps_tables

from gupkg.core import write_text_atomic
from gupkg.model.manifest import (
    FORMAT,
    ManifestError,
    ManifestKind,
    manifest_to_toml,
    parse_manifest_data,
)
from gupkg.model.version import TRACKER_DIR_NAME, split_version
from gupkg.pkgcheck import check_directory

from .hooks import HookFinding, scan_hooks
from gupkg.launchers import TRACKER_LAUNCHER, VERSION_LAUNCHERS, write_launchers

LEGACY_TRACKER_DIR_NAME = "_bootstrap"

# File types that make an ``[origin] url`` a real artifact rather than a web page.
_ARTIFACT = re.compile(r"\.(zip|7z|tar|tgz|txz|tbz2|gz|xz|bz2|exe|msi)(\?.*)?$|/releases/download/|/archive/", re.IGNORECASE)
_GITHUB_REPO = re.compile(r"https?://(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?$", re.IGNORECASE)
_GITLAB_REPO = re.compile(r"https?://(?:www\.)?gitlab\.com/[^/\s]+/[^/\s#?]+(?:/[^/\s#?]+)*?/?$", re.IGNORECASE)
_SHA256 = re.compile(r"sha256:([0-9A-Fa-f]{64})", re.IGNORECASE)


@dataclass(frozen=True)
class OtherVersion:
    """A dropped ``[[origin.versions]]`` entry, with the command that recreates it."""

    version: str
    suggestion: str


@dataclass
class PackagePlan:
    """The outcome of converting one directory, before anything is written."""

    directory: Path
    target: Path
    kind: ManifestKind | None = None
    already_converted: bool = False
    old_text: str = ""
    new_text: str = ""
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    hooks: list[HookFinding] = field(default_factory=list)
    other_versions: list[OtherVersion] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)
    launchers: list[str] = field(default_factory=list)
    stale_launchers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the plan can be applied: no validation error."""
        return not self.errors

    @property
    def changes_anything(self) -> bool:
        """Return whether applying the plan would change files."""
        return not self.already_converted and self.ok

    def diff(self) -> str:
        """Return a unified diff of ``pkg.toml`` (empty when nothing changes)."""
        return "".join(difflib.unified_diff(
            self.old_text.splitlines(keepends=True), self.new_text.splitlines(keepends=True),
            fromfile=f"{self.directory.name}/pkg.toml (format 1)", tofile=f"{self.target.name}/pkg.toml (format 2)",
        ))


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_package(directory: Path) -> PackagePlan:
    """Plan the conversion of a version directory or a 0.17 ``_bootstrap``.

    Parameters
    ----------
    directory : Path
        A directory that holds a format 1 ``pkg.toml``.

    Returns
    -------
    PackagePlan
        The converted text, notes, findings and validation errors. A directory that is
        already format 2 is returned as ``already_converted`` with its own validation
        result.
    """
    directory = Path(directory)
    is_tracker = directory.name in (LEGACY_TRACKER_DIR_NAME, TRACKER_DIR_NAME)
    target = directory.with_name(TRACKER_DIR_NAME) if is_tracker else directory
    plan = PackagePlan(directory, target, ManifestKind.TRACKER if is_tracker else ManifestKind.VERSION)

    manifest_path = directory / "pkg.toml"
    try:
        plan.old_text = manifest_path.read_text(encoding="utf-8-sig")
        old = tomllib.loads(plan.old_text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        plan.errors.append(f"cannot read {manifest_path}: {exc}")
        return plan
    if old.get("format") == FORMAT:
        plan.already_converted = True
        plan.new_text = plan.old_text
        plan.errors.extend(_without_hook_api_errors(check_directory(directory).errors, plan))
        return plan
    if "format" in old:
        plan.errors.append(f"pkg.toml has format = {old['format']!r}; only format 1 (no format key) is converted")
        return plan

    package_name = directory.parent.name
    try:
        data = _convert(old, plan, directory, package_name)
    except _Unconvertible as exc:
        plan.errors.append(str(exc))
        plan.hooks = scan_hooks(directory)
        return plan
    plan.hooks = scan_hooks(directory)

    try:
        manifest = parse_manifest_data(data, plan.kind)
    except ManifestError as exc:
        plan.errors.extend(f"the converted pkg.toml is invalid: {problem}" for problem in exc.problems)
        plan.new_text = _dump(data)
        return plan
    plan.new_text = manifest_to_toml(manifest)
    plan.errors.extend(_without_hook_api_errors(_validate_staged(plan, directory), plan))
    if plan.ok and plan.kind is ManifestKind.VERSION:
        plan.launchers = list(VERSION_LAUNCHERS)
    elif plan.ok:
        # A tracker only checks: it gets check-updates.cmd, and the 0.17 gupkg-tui.cmd (which searched PATH) is taken out.
        plan.launchers = [TRACKER_LAUNCHER]
        if (directory / "gupkg-tui.cmd").is_file():
            plan.stale_launchers = ["gupkg-tui.cmd"]
    return plan


class _Unconvertible(Exception):
    """Stop a conversion whose input cannot be mapped to format 2."""


def _dump(data: dict[str, Any]) -> str:
    """Return TOML text for *data* even when it is not valid format 2 (for the report)."""
    return dumps_tables(data)


def _without_hook_api_errors(errors: list[str], plan: PackagePlan) -> list[str]:
    """Move "must declare PKG_MODULE_API = 2" errors to the plan's notes and return the other errors.

    A 0.17 hook cannot run in 1.0, but the converted ``pkg.toml`` is still the right file: the
    author rewrites the hook (or replaces it with a built-in service), so this is a note for
    the author and not a reason to refuse the conversion.
    """
    kept: list[str] = []
    for error in errors:
        if error.endswith("must declare PKG_MODULE_API = 2"):
            plan.notes.append(
                f"{error.removesuffix(' must declare PKG_MODULE_API = 2')} is a 0.17 hook that 1.0 does not run; "
                "rewrite it for hook API 2 (gupkg.hooklib) or replace it with a built-in service"
            )
        else:
            kept.append(error)
    return kept


def _validate_staged(plan: PackagePlan, directory: Path) -> list[str]:
    """Check the result on a staged copy and return the errors, leaving *directory* untouched."""
    with tempfile.TemporaryDirectory(prefix="gupkg-migrate-") as temporary:
        staged = Path(temporary) / plan.target.name
        staged.mkdir()
        (staged / "pkg.toml").write_text(plan.new_text, encoding="utf-8")
        if (directory / "pkg.local").is_dir():
            shutil.copytree(directory / "pkg.local", staged / "pkg.local")
        for source, destination in plan.moves:
            (staged / destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(directory / source, staged / destination)
        # App can be large; a marker file stands in for its content.
        app = directory / "App"
        if app.is_dir() and any(app.iterdir()):
            (staged / "App").mkdir()
            (staged / "App" / ".present").write_text("", encoding="utf-8")
        return list(check_directory(staged).errors)


# ---------------------------------------------------------------------------
# Conversion of the data
# ---------------------------------------------------------------------------


def _convert(old: dict[str, Any], plan: PackagePlan, directory: Path, package_name: str) -> dict[str, Any]:
    """Return the format 2 data for the format 1 document *old*, filling the plan's notes."""
    notes = plan.notes
    new: dict[str, Any] = {"format": FORMAT}
    if old.get("description") is not None:
        new["description"] = old["description"]
    if old.get("homepage") is not None:
        new["project_url"] = old["homepage"]
    for key in ("name", "version", "localVersion"):
        if key in old:
            notes.append(f"dropped top-level {key}: the version is the directory name and the name is the package folder")

    modes = _modes(old, package_name, notes)
    if modes is not None:
        new["modes"] = modes

    origin = old.get("origin") if isinstance(old.get("origin"), dict) else {}
    update = old.get("update") if isinstance(old.get("update"), dict) else None
    if plan.kind is ManifestKind.TRACKER:
        _convert_tracker(new, origin, update, plan, directory)
    else:
        _convert_version(new, origin, update, plan, directory, package_name)

    _convert_components(new, old, notes)
    return new


def _modes(old: dict[str, Any], package_name: str, notes: list[str]) -> list[str] | None:
    """Return ``["portable"]`` for a portable-only package and ``None`` (the default modes) otherwise."""
    by_name = package_name.casefold().endswith("-portable")
    declared = old.get("only_portable")
    if declared is None:
        portable = by_name
    else:
        portable = bool(declared)
        if portable != by_name and (declared or by_name):
            notes.append(
                f"only_portable = {str(portable).lower()} disagrees with the package name '{package_name}'; "
                f"the flag was used. Check modes."
            )
    return ["portable"] if portable else None


def _github_repo(url: str | None) -> str | None:
    """Return ``owner/name`` for a GitHub repository URL, or ``None``."""
    match = _GITHUB_REPO.match(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _local_script(value: str, plan: PackagePlan, directory: Path, context: str) -> str:
    """Return a ``pkg.local/...`` path for a 0.17 script, planning a move when it lies elsewhere."""
    normal = value.replace("\\", "/")
    if normal.startswith("pkg.local/"):
        return normal
    if normal.startswith("/") or ".." in normal.split("/") or re.match(r"[A-Za-z]:", normal):
        raise _Unconvertible(f"{context} script {value!r} is not a path inside the version directory")
    destination = f"pkg.local/{Path(normal).name}"
    if (directory / destination).exists():
        raise _Unconvertible(f"{context} script {normal} would be moved to {destination}, which already exists")
    plan.moves.append((normal, destination))
    plan.notes.append(f"moved {context} script {normal} to {destination}: populate scripts live in pkg.local")
    if not (directory / normal).is_file():
        raise _Unconvertible(f"{context} script {normal} was not found")
    return destination


def _origin_source(origin: dict[str, Any], plan: PackagePlan, directory: Path, context: str) -> dict[str, Any] | None:
    """Map one 0.17 origin (or history entry) to a ``[source]`` table, or ``None`` when it names no artifact.

    Every key that is not carried over is reported in the plan's notes; nothing is dropped silently.
    """
    notes = plan.notes
    mode = origin.get("mode")
    url = origin.get("url")
    source: dict[str, Any] | None = None
    consumed: set[str] = set()
    if mode == "git":
        source = {"download_url": url, "kind": "git", "shipped": False, "git": {"ref": origin.get("ref") or "refs/heads/main"}}
        consumed.add("ref")
    elif origin.get("module") or origin.get("script"):
        script = origin.get("module") or origin.get("script")
        source = {"script": _local_script(script, plan, directory, context), "shipped": False}
        if origin.get("module"):
            notes.append(f"{context} module {source['script']} uses hook API 1; rewrite it for hook API 2 (gupkg.hooklib)")
    elif url and _is_project_page(url):
        # A repository or project page is information, not an artifact.
        notes.append(f"{context} url {url} is a repository or project page, not an artifact: not used as a download_url")
    elif url:
        # Any other address is the artifact itself, whatever its shape (redirects, query strings).
        source = {"download_url": url, "shipped": False}
        consumed.update({"checksum", "extractSubdir"})
        checksum = origin.get("checksum")
        if checksum:
            match = _SHA256.fullmatch(checksum)
            if match:
                source["sha256"] = match.group(1).lower()
                source["sha256_from"] = "manual"
            else:
                notes.append(f"{context} checksum {checksum!r} is not sha256:<hex> and was dropped")
        if origin.get("extractSubdir"):
            source["subdir"] = origin["extractSubdir"]
        if not _ARTIFACT.search(url):
            notes.append(f"{context} url {url} was kept as download_url; check that it names the artifact itself")

    # Report every key that did not make it into [source].
    for key in ("checksum", "extractSubdir", "ref"):
        if origin.get(key) and key not in consumed:
            notes.append(f"{context} {key} = {origin[key]!r} was dropped: no [source] was written that it belongs to")
    return source


def _is_project_page(url: str) -> bool:
    """Return whether *url* is a repository or project page rather than a downloadable artifact."""
    if _ARTIFACT.search(url):
        return False
    return bool(_GITHUB_REPO.match(url) or _GITLAB_REPO.match(url) or re.search(r"\.git/?$", url))


def _convert_version(new: dict[str, Any], origin: dict[str, Any], update: dict[str, Any] | None,
                     plan: PackagePlan, directory: Path, package_name: str) -> None:
    """Fill ``[source]`` for a version directory; dissolve ``[[origin.versions]]``; drop ``[update]``."""
    notes = plan.notes
    entries = [item for item in origin.get("versions", []) if isinstance(item, dict)]
    inline = origin if any(origin.get(key) for key in ("url", "script", "module")) or origin.get("mode") == "git" else None
    own_names = {directory.name, split_version(directory.name)[0]}
    # An inline source is authoritative; otherwise the entry for this directory's version is.
    own_entry = next((item for item in entries if item.get("version") in own_names), None)
    chosen = inline if inline is not None else own_entry
    for entry in entries:
        version = entry.get("version")
        if version is not None and version not in own_names:
            plan.other_versions.append(OtherVersion(str(version), f"gupkg pkg new {package_name}@{version}"))
    if entries:
        notes.append(f"dissolved [[origin.versions]] ({len(entries)} entries): only this directory's own source is kept")
    source = _origin_source(chosen, plan, directory, "origin") if chosen is not None else None

    # An inline origin that is only a project page names no artifact: the version's own entry still may.
    if source is None and inline is not None and own_entry is not None:
        source = _origin_source(own_entry, plan, directory, f"origin.versions[{own_entry.get('version')}]")
        if source is not None:
            notes.append(f"the inline [origin] url is a project page, so this version's own [[origin.versions]] entry was used")
    # No artifact and no script: this version's App is shipped in the directory.
    new["source"] = source if source is not None else {"shipped": True}
    if update is not None:
        notes.append(
            "dropped [update]: a version directory has no update discovery, which lives only in _check_updates"
        )
        used = {new["source"].get("script")}
        for name in ("pkg.local/check_update.py", "pkg.local/unpack_app.py"):
            if (directory / name).is_file() and name not in used:
                notes.append(f"{name} is no longer used by this version; remove it or keep it for reference")


def _convert_tracker(new: dict[str, Any], origin: dict[str, Any], update: dict[str, Any] | None,
                     plan: PackagePlan, directory: Path) -> None:
    """Fill ``[track]`` and ``[source]`` for a ``_bootstrap`` that becomes ``_check_updates``."""
    notes = plan.notes
    if origin.get("versions"):
        notes.append("dissolved [[origin.versions]] of the tracker: a tracker has no per-version sources")
    check = (update or {}).get("check")
    payload = (update or {}).get("payload") if isinstance((update or {}).get("payload"), dict) else {}
    if not isinstance(check, dict):
        check = {"mode": "git"} if origin.get("mode") == "git" else None
    if update is None or check is None:
        raise _Unconvertible("the 0.17 tracker has no [update.check]: nothing to convert to [track]")

    mode = check.get("mode")
    track: dict[str, Any]
    captured = "${download_url}"
    if mode == "github":
        repo = _github_repo(origin.get("url"))
        if repo is None:
            raise _Unconvertible(f"the github check needs a GitHub repository URL in [origin].url, not {origin.get('url')!r}")
        track = {"provider": "github", "repo": repo, "asset": check.get("assetName")}
        if check.get("tagPrefix"):
            track["tag_prefix"] = check["tagPrefix"]
        captured = "${asset_url}"
    elif mode == "git":
        ref = check.get("ref") or origin.get("ref") or "refs/heads/main"
        track = {"provider": "git", "repo_url": origin.get("url"), "ref": ref}
        for key, default in (("remote", "origin"), ("appPath", "App")):
            if check.get(key, default) != default:
                notes.append(f"update.check.{key} = {check[key]!r} was dropped: a git tracker has no such option")
    elif mode == "module":
        module = _local_script(check.get("module", "pkg.local/check_update.py"), plan, directory, "update.check")
        track = {"provider": "module", "module": module}
        if check.get("channel", "stable") != "stable":
            notes.append(f"update.check.channel = {check['channel']!r} was dropped: pass it through the hook")
    else:
        raise _Unconvertible(f"unknown update.check mode {mode!r}")
    new["track"] = {key: value for key, value in track.items() if value is not None}
    notes.append("check hook and any other 0.17 hook use hook API 1; rewrite them for hook API 2 (gupkg.hooklib) or replace them with a built-in service")

    source: dict[str, Any]
    payload_mode = payload.get("mode")
    prepare: list[dict[str, Any]] = []
    if payload_mode == "git":
        source = {
            "download_url": origin.get("url"), "kind": "git", "shipped": False,
            "git": {"ref": track.get("ref", "refs/heads/main"), "commit": "${commit}"},
        }
    elif payload_mode == "module":
        module = _local_script(payload.get("module", "pkg.local/unpack_app.py"), plan, directory, "update.payload")
        source = {"script": module, "shipped": False}
    elif payload_mode == "zip":
        source = {"download_url": captured, "shipped": False}
        if captured == "${asset_url}":
            notes.append("source.download_url uses the github provider's asset_url capture")
        else:
            notes.append("source.download_url uses a download_url capture: the track hook must return it in TrackResult.captures, or use a built-in provider (gupkg pkg check suggests one)")
        if payload.get("extractSubdir"):
            source["subdir"] = payload["extractSubdir"]
        if payload.get("extract"):
            source["extract"] = [{"src": row["src"], "dest": row["dest"]} for row in payload["extract"]]
        for row in payload.get("rename", []):
            prepare.append({"rename": {"from": row["src"], "to": row["dest"]}})
    else:
        raise _Unconvertible(f"unknown update.payload mode {payload_mode!r}")
    if payload.get("ignore_checksum"):
        notes.append("update.payload.ignore_checksum was dropped: the checksum policy of issue 027 decides what a missing hash does")
    if payload.get("maxSizeMB") is not None:
        notes.append("update.payload.maxSizeMB was dropped: format 2 has no size limit key")
    for step in (update.get("steps") or [])[1:]:
        if isinstance(step, dict) and step.get("module"):
            prepare.append({"module": _local_script(step["module"], plan, directory, "update.steps")})
    new["source"] = source
    if prepare:
        new["prepare"] = prepare


_VARIABLE_FIXES = (
    (re.compile(r"\$Icons(?![A-Za-z0-9_])"), "$VersionRoot\\Icons"),
    (re.compile(r"\$Shortcuts(?![A-Za-z0-9_])"), "$VersionRoot\\Shortcuts"),
)


def _fix_variables(value: Any) -> Any:
    """Rewrite the retired ``$Icons`` and ``$Shortcuts`` variables to paths below ``$VersionRoot``."""
    if isinstance(value, str):
        for pattern, replacement in _VARIABLE_FIXES:
            value = pattern.sub(lambda _match, text=replacement: text, value)
        return value
    if isinstance(value, list):
        return [_fix_variables(item) for item in value]
    return value


def _convert_components(new: dict[str, Any], old: dict[str, Any], notes: list[str]) -> None:
    """Map shortcuts, environment variables, PATH entries and shims to their snake_case tables."""
    shortcut_keys = {
        "name": "name", "targetPath": "target", "arguments": "arguments", "workingDirectory": "working_dir",
        "iconLocation": "icon_location", "description": "description",
    }
    shortcuts = [
        {shortcut_keys.get(key, key): _fix_variables(value) for key, value in row.items()}
        for row in old.get("shortcut", []) if isinstance(row, dict)
    ]
    if shortcuts:
        new["shortcut"] = shortcuts
    env = [
        {{"Name": "name", "Value": "value"}.get(key, key): _fix_variables(value) for key, value in row.items()}
        for row in old.get("environment", []) if isinstance(row, dict)
    ]
    if env:
        new["env"] = env
    paths = [{key: _fix_variables(value) for key, value in row.items()} for row in old.get("path", []) if isinstance(row, dict)]
    if paths:
        new["path"] = paths
    shims = [{key: _fix_variables(value) for key, value in row.items()} for row in old.get("bin", []) if isinstance(row, dict)]
    if shims:
        new["shim"] = shims
    if any("$Icons" in str(row) or "$Shortcuts" in str(row) for row in old.get("shortcut", [])):
        notes.append("rewrote $Icons and $Shortcuts as $VersionRoot\\Icons and $VersionRoot\\Shortcuts")


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _numbered_backup(path: Path) -> Path:
    """Return the first free ``<name>.bak``, ``<name>.bak.1``, ... beside *path*."""
    destination = path.with_name(path.name + ".bak")
    counter = 0
    while destination.exists():
        counter += 1
        destination = path.with_name(f"{path.name}.bak.{counter}")
    return destination


def apply_plan(plan: PackagePlan, *, backup: bool = True) -> list[str]:
    """Write a plan's changes to disk, or leave the directory as it was.

    Everything that can fail is checked first. Then each step records how to undo itself:
    a backup of ``pkg.toml`` (and of any launcher about to be replaced), copies of moved
    scripts, the new ``pkg.toml`` written under a temporary name and swapped in atomically,
    the launchers, and last the ``_bootstrap`` rename. If any step fails, the steps already
    done are undone in reverse order and the error is raised. Only after everything succeeded
    are the originals of moved scripts removed.

    Parameters
    ----------
    plan : PackagePlan
        A plan from ``plan_package``.
    backup : bool, optional
        Keep the original ``pkg.toml`` as ``pkg.toml.bak`` (``.bak.1``, ... when taken).
        Launchers that exist are always backed up the same way before being replaced.

    Returns
    -------
    list of str
        Human-readable lines of what was written.

    Raises
    ------
    ValueError
        If the plan has errors, ``_check_updates`` already exists beside ``_bootstrap``, or a
        moved script would overwrite a file.
    OSError
        If a step failed; the directory has been restored.
    """
    if not plan.ok:
        raise ValueError("refusing to apply a plan with validation errors")
    if plan.already_converted:
        return []

    # Refuse before touching anything when the result would collide with existing files.
    if plan.target != plan.directory and plan.target.exists():
        raise ValueError(f"{plan.target} already exists; cannot rename {plan.directory.name}")
    for _source, destination in plan.moves:
        if (plan.directory / destination).exists():
            raise ValueError(f"{destination} already exists; refusing to overwrite it with a moved script")

    done: list[str] = []
    undo: list[Callable[[], None]] = []
    manifest_path = plan.directory / "pkg.toml"
    original = manifest_path.read_bytes()
    original_stat = manifest_path.stat()
    try:
        # The backup of the old pkg.toml stays in the directory; comments are only kept there.
        if backup:
            destination = _numbered_backup(manifest_path)
            shutil.copy2(manifest_path, destination)
            undo.append(lambda path=destination: _remove_file(path))
            done.append(f"backup {destination.name} (it keeps the comments of the old file and stays in the directory)")

        # Scripts are copied, not moved, until everything else has worked. A pkg.local folder that this
        # step creates is removed again, when it is empty, if a later step fails.
        for source, destination in plan.moves:
            target = plan.directory / destination
            if not target.parent.exists():
                undo.append(lambda folder=target.parent: _remove_empty_folder(folder))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(plan.directory / source, target)
            undo.append(lambda path=target: _remove_file(path))
            done.append(f"moved {source} to {destination}")

        # The new pkg.toml is written under a temporary name and swapped in atomically.
        write_text_atomic(manifest_path, plan.new_text)
        undo.append(lambda: _restore_file(manifest_path, original, original_stat))
        done.append(f"wrote {manifest_path}")

        # Launchers that exist are backed up before they are replaced.
        for name in plan.launchers:
            path = plan.directory / name
            previous = path.read_bytes() if path.is_file() else None
            if previous is not None:
                saved = _numbered_backup(path)
                saved.write_bytes(previous)
                undo.append(lambda p=saved: p.unlink(missing_ok=True))
                done.append(f"backup {saved.name}")
            write_launchers(plan.directory, (name,))
            undo.append(lambda p=path, old=previous: p.write_bytes(old) if old is not None else p.unlink(missing_ok=True))
            done.append(f"wrote {path}")

        # A tracker's 0.17 gupkg-tui.cmd searched PATH; it is moved aside (kept as a backup), never left in place.
        for name in plan.stale_launchers:
            path = plan.directory / name
            saved = _numbered_backup(path)
            path.rename(saved)
            undo.append(lambda p=path, s=saved: s.rename(p))
            done.append(f"removed {name} (kept as {saved.name}); a tracker carries only {TRACKER_LAUNCHER}")

        # Renaming the folder is last: it is the one step that changes the directory's own name.
        if plan.target != plan.directory:
            plan.directory.rename(plan.target)
            undo.append(lambda: plan.target.rename(plan.directory))
            done.append(f"renamed {plan.directory.name} to {plan.target.name}")
    except BaseException:
        # Undo in reverse order; the original directory is recoverable.
        for step in reversed(undo):
            try:
                step()
            except OSError:
                pass
        raise

    # Everything worked: now the originals of moved scripts can go. A script that cannot be removed is
    # reported, not an error: the conversion is complete and the copy in pkg.local is what is used.
    base = plan.target
    for source, _destination in plan.moves:
        try:
            _remove_file(base / source)
        except OSError as error:
            done.append(f"could not remove the original {source}: {error}")
    return done


def _remove_file(path: Path) -> None:
    """Delete a file, making a read-only one writable first; a missing file is not an error."""
    if path.exists():
        os.chmod(path, 0o666)
    path.unlink(missing_ok=True)


def _remove_empty_folder(folder: Path) -> None:
    """Delete *folder* when it is empty; anything else is left alone."""
    try:
        folder.rmdir()
    except OSError:
        pass


def _restore_file(path: Path, content: bytes, stat: os.stat_result) -> None:
    """Put the original bytes and the original modification time back."""
    path.write_bytes(content)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
