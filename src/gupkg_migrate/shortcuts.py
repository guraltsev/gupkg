"""Turn the ``.lnk`` shortcuts of a package version into ``[[shortcut]]`` tables.

``plan_shortcuts(directory)`` reads every ``.lnk`` file below the version directory's
``_shortcuts`` folder and returns a ``ShortcutPlan``: the updated format 2 ``pkg.toml``
text and the shortcuts found. ``apply_shortcuts(plan)`` writes it, keeps a backup and
renames the imported files to ``.lnk.imported``. This is 0.17's
``shortcuts_to_gupkg_toml.py`` for format 2 (decision D45).

A shortcut's Start Menu name comes from its place under ``_shortcuts``, not from fields
inside the file. Paths below the version's ``App``, ``Icons`` and ``Shortcuts`` folders (or
the package's ``current`` junction) are turned back into ``$App`` and ``$VersionRoot``
paths; other paths are kept as they are. Reading a ``.lnk`` file goes through the ``System`` boundary.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from gupkg.core import write_text_atomic
from gupkg.model.manifest import ManifestError, ManifestKind, manifest_to_toml, parse_manifest_data
from gupkg.system.port import System

SHORTCUTS_DIR_NAME = "_shortcuts"


@dataclass
class ShortcutPlan:
    """The outcome of importing the shortcuts of one directory, before anything is written."""

    directory: Path
    new_text: str = ""
    shortcuts: list[dict[str, str]] = field(default_factory=list)
    imported_files: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the plan can be applied."""
        return not self.errors


def _prefixes(directory: Path) -> list[tuple[str, str]]:
    """Return (absolute prefix, variable) pairs, longest prefix first."""
    current = directory.parent / "current"
    pairs = []
    for base in (directory, current):
        pairs.extend([
            (str(base / "App"), "$App"),
            (str(base / "Icons"), "$VersionRoot\\Icons"),
            (str(base / "Shortcuts"), "$VersionRoot\\Shortcuts"),
        ])
    return sorted(pairs, key=lambda item: len(item[0]), reverse=True)


def _normalize(value: str, prefixes: list[tuple[str, str]]) -> str:
    """Replace a package-owned absolute prefix of *value* by its variable."""
    text = value.replace("/", "\\")
    for prefix, variable in prefixes:
        folded = prefix.replace("/", "\\").rstrip("\\")
        if text.casefold() == folded.casefold():
            return variable
        if text.casefold().startswith(folded.casefold() + "\\"):
            return variable + text[len(folded):]
    return value


def _normalize_icon(value: str, prefixes: list[tuple[str, str]]) -> str:
    """Normalize the path of an icon location and keep its ``,index`` suffix."""
    match = re.match(r"^(?P<path>[A-Za-z]:[\\/][^,]*)(?P<suffix>,.*)?$", value)
    if match is None:
        return _normalize(value, prefixes)
    return _normalize(match.group("path"), prefixes) + (match.group("suffix") or "")


def plan_shortcuts(directory: Path, system: System, *, shortcuts_dir_name: str = SHORTCUTS_DIR_NAME) -> ShortcutPlan:
    """Plan the import of a version directory's shortcut files.

    Parameters
    ----------
    directory : Path
        A version directory with a format 2 ``pkg.toml``.
    system : System
        The boundary that reads a ``.lnk`` file (``WindowsSystem``, or ``FakeSystem`` in tests).
    shortcuts_dir_name : str, optional
        The folder of ``.lnk`` files, relative to *directory*.

    Returns
    -------
    ShortcutPlan
        The updated ``pkg.toml`` text and the shortcuts found. Existing ``[[shortcut]]`` tables
        with the same name are replaced; all other content is kept.
    """
    directory = Path(directory)
    plan = ShortcutPlan(directory)
    folder = directory / shortcuts_dir_name
    try:
        data = tomllib.loads((directory / "pkg.toml").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        plan.errors.append(f"cannot read {directory / 'pkg.toml'}: {exc}")
        return plan
    if data.get("format") != 2:
        plan.errors.append("pkg.toml is not format 2: convert the package first")
        return plan
    files = sorted(path for path in folder.rglob("*.lnk") if path.is_file()) if folder.is_dir() else []
    if not files:
        plan.errors.append(f"no .lnk files below {folder}")
        return plan

    prefixes = _prefixes(directory)
    for path in files:
        try:
            link = system.read_shortcut(path)
        except OSError as exc:
            plan.errors.append(f"cannot read {path}: {exc}")
            continue
        row = {
            "name": str(path.relative_to(folder).with_suffix("")).replace("/", "\\"),
            "target": _normalize(link.target_path, prefixes),
            "arguments": link.arguments,
            "working_dir": _normalize(link.working_directory, prefixes),
            "icon_location": _normalize_icon(link.icon_location, prefixes) if link.icon_location else "",
            "description": link.description,
        }
        plan.shortcuts.append({key: value for key, value in row.items() if value != "" or key in ("name", "target")})
        plan.imported_files.append(path)
    if plan.errors:
        return plan

    names = {row["name"] for row in plan.shortcuts}
    kept = [row for row in data.get("shortcut", []) if isinstance(row, dict) and row.get("name") not in names]
    data["shortcut"] = [*kept, *plan.shortcuts]
    kind = ManifestKind.TRACKER if directory.name == "_check_updates" else ManifestKind.VERSION
    try:
        plan.new_text = manifest_to_toml(parse_manifest_data(data, kind))
    except ManifestError as exc:
        plan.errors.extend(f"the updated pkg.toml is invalid: {problem}" for problem in exc.problems)
    return plan


def apply_shortcuts(plan: ShortcutPlan, *, backup: bool = True) -> list[str]:
    """Write an import plan: backup, new ``pkg.toml``, and the ``.lnk.imported`` renames.

    Parameters
    ----------
    plan : ShortcutPlan
        A plan from ``plan_shortcuts``.
    backup : bool, optional
        Keep the old ``pkg.toml`` as ``pkg.toml.bak`` (``.bak.1``, ... when taken).

    Returns
    -------
    list of str
        What was written.

    Raises
    ------
    ValueError
        If the plan has errors.
    FileExistsError
        If a ``.lnk.imported`` file already exists; nothing is changed then.
    """
    if not plan.ok:
        raise ValueError("refusing to apply a shortcut plan with errors")
    archived = [path.with_suffix(".lnk.imported") for path in plan.imported_files]
    collision = next((path for path in archived if path.exists()), None)
    if collision is not None:
        raise FileExistsError(f"imported shortcut already exists: {collision}")
    done: list[str] = []
    manifest = plan.directory / "pkg.toml"
    if backup:
        destination = manifest.with_name("pkg.toml.bak")
        counter = 0
        while destination.exists():
            counter += 1
            destination = manifest.with_name(f"pkg.toml.bak.{counter}")
        shutil.copy2(manifest, destination)
        done.append(f"backup {destination}")
    write_text_atomic(manifest, plan.new_text)
    done.append(f"wrote {manifest}")
    for source, target in zip(plan.imported_files, archived):
        source.rename(target)
        done.append(f"renamed {source.name} to {target.name}")
    return done
