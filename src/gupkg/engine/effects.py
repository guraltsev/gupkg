"""Turn a package's declarations into integration effects, apply and remove them, and verify them.

The effects of a version are its ``[[shortcut]]`` rows (Start Menu links), ``[[shim]]`` rows
(commands in the mode's ``bin`` folder), ``[[env]]`` rows (environment values), ``[[path]]`` rows
(``PATH`` entries) and, in portable mode, the arguments and environment of ``[portable]``. Each
becomes one ``Effect`` of ``gupkg.model.state`` whose fields hold everything needed to apply, remove
and verify it. Variables (``$App``, ``$Data``, ``$Bin``...) are expanded when the effect is built, against
the package's ``current`` folder, so a link or shim keeps working when ``current`` moves to another version.

Usage and API
-------------
``desired_effects(manifest, context)`` returns the effects a version wants and notes about rows that
were skipped. ``apply_effect``, ``remove_effect`` and ``verify_effects`` act on one effect or a list
through the ``System`` boundary. ``effect_context(mode_context, package_path, env)`` builds the context
from the resolved install mode. ``EffectError`` reports a declaration that cannot become an effect.

Implementation Approach
-----------------------
Declaration to effect is a pure function; only ``apply_effect``, ``remove_effect`` and ``verify_effects``
touch the machine, and only through ``System`` (shortcuts, environment, ``PATH``) or the folders of the
mode (shims). A removal first checks that the recorded path still lies inside the folder that the mode
owns, so a damaged or hostile install record cannot make gupkg delete anything elsewhere. The Windows
registry is a write target (design 9.5): an environment value is written and deleted without being read,
and ``PATH`` is read only as part of its read-modify-write; ``verify_effects`` reads only values that gupkg
wrote. An environment or ``PATH`` entry is never changed for a mode that does not touch the environment.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog.local import canonical_path, is_inside, is_reparse_point
from ..core import write_bytes_atomic
from ..model.manifest import Manifest, env_name_problem, name_part_problem, path_entry_shape_problem
from ..model.mode import ENV_MACHINE, ModeContext
from ..model.plan import effect_key, normalize_entry
from ..model.state import Effect
from ..system.port import Scope, ShortcutSpec, System
from ..tomlio import dumps

# The launcher binaries, the project licenses that sit beside them, and the folder of runtime files a dynamically linked launcher
# loads its libraries from (``exe-shim-lib`` beside the launcher: the launchers do not look in their own folder, so a stray DLL
# in ``bin`` is never picked up by mistake).
_SHIM_DIR = Path(__file__).resolve().parents[1] / "shim"
SHIM_LIB_NAME = "exe-shim-lib"
_SHIM_LICENSES = ("LICENSE-exe-shim-MIT.txt", "LICENSE-exe-shim-UNLICENSE.txt")
# What older gupkg versions copied loose into ``bin``; an identical copy there is removed when a launcher is written.
_LEGACY_LOOSE_FILES = (
    "libgcc_s_seh-1.dll",
    "libstdc++-6.dll",
    "libwinpthread-1.dll",
    "LICENSE-GCC-3.0.txt",
    "LICENSE-GCC-RUNTIME-EXCEPTION-3.1.txt",
    "LICENSE-libwinpthread-MIT.txt",
)
_TOKEN = re.compile(r"\$(?:(\$)|\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


class EffectError(ValueError):
    """Report a declaration that cannot become an effect, or an effect that cannot be applied safely."""


@dataclass(frozen=True)
class EffectContext:
    """Where the effects of one package go.

    Attributes
    ----------
    package_path : Path
        The package root.
    mode : str
        ``system``, ``user`` or ``portable``.
    bin_dir : Path
        The mode's ``bin`` folder.
    start_menu : Path or None
        The Start Menu ``opt`` folder; ``None`` when the mode creates no shortcuts.
    scope : Scope or None
        The environment and ``PATH`` target; ``None`` when the mode does not touch the environment.
    data_dir : Path or None
        ``<package>\\data`` in portable mode, else ``None``.
    env : mapping
        The environment used to expand ``${NAME}`` references.
    linkage : str
        ``dynamic`` or ``static`` launcher build.
    root_data : Path or None
        The root's data folder ``<root>\\.gupkg``, where the root forwarder is written; ``None`` when unknown.
    """

    package_path: Path
    mode: str
    bin_dir: Path
    start_menu: Path | None
    scope: Scope | None
    data_dir: Path | None
    env: Mapping[str, str]
    linkage: str = "dynamic"
    root_data: Path | None = None


def effect_context(mode_context: ModeContext, package_path: Path, env: Mapping[str, str]) -> EffectContext:
    """Build the effect context of a package from its resolved install mode.

    Parameters
    ----------
    mode_context : ModeContext
        The resolved mode (root, ``bin``, Start Menu and environment target).
    package_path : Path
        The package root.
    env : mapping
        The environment for ``${NAME}`` references.

    Returns
    -------
    EffectContext
        The context; ``scope`` follows ``mode_context.env_target``.
    """
    scope = None
    if mode_context.env_target is not None:
        scope = Scope.MACHINE if mode_context.env_target == ENV_MACHINE else Scope.USER
    data_dir = Path(package_path) / "data" if mode_context.mode == "portable" else None
    return EffectContext(
        Path(package_path), mode_context.mode, mode_context.bin_dir, mode_context.start_menu, scope, data_dir, env,
        root_data=mode_context.data_dir,
    )


# ---------------------------------------------------------------------------
# From declarations to effects
# ---------------------------------------------------------------------------


def desired_effects(manifest: Manifest, context: EffectContext) -> tuple[tuple[Effect, ...], tuple[str, ...]]:
    """Return the effects that a version declares for this mode, and notes about what was left out.

    Parameters
    ----------
    manifest : Manifest
        The version's manifest.
    context : EffectContext
        The package's locations and mode.

    Returns
    -------
    tuple
        ``(effects, notes)``. A mode that creates no shortcuts or touches no environment (a portable
        root with ``integrate = "none"``) leaves those rows out and says so in the notes.

    Raises
    ------
    EffectError
        If a row cannot be expanded (an unknown ``${NAME}``, ``$Data`` outside portable mode) or would
        write outside the Start Menu or ``bin`` folder.
    """
    package = context.package_path
    values = {
        "App": str(package / "current" / "App"),
        "VersionRoot": str(package / "current"),
        "PackageRoot": str(package),
        "Bin": str(context.bin_dir),
    }
    if context.data_dir is not None:
        values["Data"] = str(context.data_dir)
    portable = manifest.portable if context.mode == "portable" else None
    extra_arguments = [_expand(item, values, context.env, "[portable] args") for item in (portable.args if portable else ())]
    effects: list[Effect] = []
    notes: list[str] = []

    # Shortcuts live below the Start Menu folder; a mode without one declares none.
    if manifest.shortcuts and context.start_menu is None:
        notes.append("this root does not create shortcuts (integrate = \"none\"): shortcuts were left out")
    for row in manifest.shortcuts if context.start_menu is not None else ():
        label = f"shortcut '{row.name}'"
        name = _expand(row.name, values, context.env, f"{label} name").strip()
        link = _confined(context.start_menu, name if name.lower().endswith(".lnk") else name + ".lnk", label)
        arguments = _expand(row.arguments, values, context.env, f"{label} arguments")
        if extra_arguments:
            arguments = " ".join(part for part in (arguments, subprocess.list2cmdline(extra_arguments)) if part)
        effects.append(Effect("shortcut", {
            "path": str(link),
            "target": _expand(row.target, values, context.env, f"{label} target"),
            "arguments": arguments,
            "working_dir": _expand(row.working_dir, values, context.env, f"{label} working_dir"),
            "icon_location": _expand(row.icon_location, values, context.env, f"{label} icon_location"),
            "description": _expand(row.description, values, context.env, f"{label} description"),
        }))

    # Shims live in the mode's bin folder; a launcher is an .exe with a config file, a content shim is one file.
    for shim in manifest.shims:
        label = f"shim '{shim.name}'"
        if shim.content is not None:
            path = _confined(context.bin_dir, shim.name, label)
            content = _expand(shim.content, values, context.env, f"{label} content", literal_unknown=True)
            effects.append(Effect("file", {
                "path": str(path), "content": content, "sha256": hashlib.sha256(_wrapper_bytes(path, content)).hexdigest(),
            }))
            continue
        stem = shim.name[:-4] if shim.name.lower().endswith(".exe") else shim.name
        path = _confined(context.bin_dir, stem + ".exe", label)
        fields: dict[str, Any] = {
            "path": str(path),
            "config": str(path.with_name(f"{stem}.config.toml")),
            "type": shim.type,
            "linkage": context.linkage,
            "target": _expand(shim.target, values, context.env, f"{label} target"),
            "arguments": [
                *(_expand(item, values, context.env, f"{label} argument") for item in shim.arguments), *extra_arguments
            ],
            "forward_args": shim.forward_args,
            "elevate": shim.elevate,
            "working_dir": _expand(shim.working_dir or "", values, context.env, f"{label} working_dir"),
        }
        fields["config_sha256"] = hashlib.sha256(shim_config_text(fields).encode("utf-8")).hexdigest()
        effects.append(Effect("shim", fields))

    # The root forwarder belongs to the gupkg package alone: it lets the package launchers of this root find the installed gupkg.
    if manifest.root_forwarder is not None:
        effects.extend(_forwarder_effects(manifest.root_forwarder.target, values, context))

    # Environment values and PATH entries need an environment target.
    wants_environment = bool(manifest.envs or manifest.paths or (portable.env if portable else None) or manifest.shims)
    if context.scope is None:
        if manifest.envs or manifest.paths or (portable.env if portable else None):
            notes.append("this root does not touch the environment (integrate = \"none\"): environment values and PATH entries were left out")
        if manifest.shims:
            notes.append(f"shims are in {context.bin_dir}; run `gupkg shell` for the PATH line to use")
    elif wants_environment:
        scope = "system" if context.scope is Scope.MACHINE else "user"
        for row in manifest.envs:
            _require_free_name(row.name)
            effects.append(Effect("env", {
                "scope": scope, "name": row.name, "value": _expand(row.value, values, context.env, f"environment variable '{row.name}'"),
            }))
        for name, value in (portable.env if portable else {}).items():
            _require_free_name(name)
            effects.append(Effect("env", {
                "scope": scope, "name": name, "value": _expand(value, values, context.env, f"[portable] env '{name}'"),
            }))
        if manifest.shims:
            effects.append(Effect("path", {"scope": scope, "entry": str(context.bin_dir), "added": False, "shared": True}))
        for value in manifest.paths:
            entry = _expand(value, values, context.env, f"PATH entry '{value}'").strip()
            _require_path_entry(entry, context, scope, f"PATH entry '{value}'")
            effects.append(Effect("path", {"scope": scope, "entry": os.path.normpath(entry), "added": False}))

    # One effect per identity: a second declaration of the same thing is an error in the package.
    seen: dict[tuple[str, ...], Effect] = {}
    for item in effects:
        key = effect_key(item)
        if key in seen and seen[key].fields != item.fields:
            raise EffectError(f"two declarations write the same {item.kind}: {key[1:]}")
        seen.setdefault(key, item)
    return tuple(seen.values()), tuple(notes)


def _forwarder_effects(target: str, values: Mapping[str, str], context: EffectContext) -> list[Effect]:
    """Return the two forwarder files (``gupkg.cmd`` and ``gupkg-manager.cmd``) of ``[root_forwarder]``.

    Only a package called ``gupkg`` may declare it, and its target must lie inside the package folder, so a forwarder always
    points to a gupkg in the root that it serves (design 21.3). The files call the target by a path relative to themselves,
    which keeps them free of non-ASCII path text that ``cmd`` could not read.

    Raises
    ------
    EffectError
        If the package is not ``gupkg``, the root has no data folder, or the target leaves the package folder.
    """
    package = context.package_path
    if package.name.casefold() != "gupkg":
        raise EffectError("[root_forwarder] is valid only in the gupkg package")
    if context.root_data is None:
        raise EffectError("[root_forwarder] needs the root's data folder, which this context does not have")
    folder = Path(_expand(target, values, context.env, "[root_forwarder] target"))
    if not is_inside(folder, package) or canonical_path(folder) == canonical_path(package):
        raise EffectError(f"[root_forwarder] target {folder} is not inside the package folder {package}")

    # The forwarder sits in <root>\.gupkg, so the target is reached by climbing one level to the root.
    relative = os.path.relpath(folder, context.root_data.parent)
    effects = []
    for name in ("gupkg.cmd", "gupkg-manager.cmd"):
        content = (
            "@echo off\r\n"
            "rem Written by gupkg: the gupkg installed in this root. Package launchers call this file.\r\n"
            f'call "%~dp0..\\{relative}\\{name}" %*\r\n'
            "exit /b %ERRORLEVEL%\r\n"
        )
        path = context.root_data / name
        effects.append(Effect("forwarder", {
            "path": str(path), "content": content, "sha256": hashlib.sha256(content.encode("ascii")).hexdigest(),
        }))
    return effects


def forwarder_target(data_dir: Path) -> Path | None:
    """Return the installed gupkg's ``gupkg.cmd`` that the root forwarder in *data_dir* calls, or ``None``.

    ``None`` means the root has no forwarder, or the forwarder does not call a ``gupkg.cmd`` that exists (the installed gupkg
    is gone): then no installed gupkg can take over from the bundled copies of this root (design 21.8). Only the call line that
    gupkg itself writes is understood; the path is read relative to *data_dir*.
    """
    forwarder = Path(data_dir) / "gupkg.cmd"
    try:
        text = forwarder.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        match = re.fullmatch(r'call "%~dp0([^"%]+)" %\*', line.strip())
        if match is not None:
            target = Path(os.path.normpath(Path(data_dir) / match.group(1)))
            return target if target.is_file() else None
    return None


def shim_config_text(fields: Mapping[str, Any]) -> str:
    """Return the ``.config.toml`` text of a shim effect."""
    config: dict[str, Any] = {
        "target": fields["target"],
        "forward_arguments": bool(fields.get("forward_args", True)),
        "elevate": bool(fields.get("elevate", False)),
    }
    if fields.get("working_dir"):
        config["working_dir"] = fields["working_dir"]
    arguments = list(fields.get("arguments") or ())
    if arguments:
        config["argument"] = [{"value": argument} for argument in arguments]
    return dumps(config)


def _wrapper_bytes(path: Path, content: str) -> bytes:
    """Return the bytes of a content wrapper: ASCII for batch files (``cmd`` ignores a UTF-8 mark), UTF-8 otherwise."""
    if path.suffix.lower() not in (".cmd", ".bat"):
        return content.encode("utf-8")
    try:
        return content.encode("ascii")
    except UnicodeEncodeError:
        return content.encode("utf-8-sig")


# The variables whose folders are machine-wide, so a system ``PATH`` entry below one of them is as trusted as the package root.
_MACHINE_ROOTS = (
    "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432",
    "SystemRoot", "windir",
)


def _under(path: str | Path, root: Path) -> bool:
    """Return whether *path* is *root* or lies below it (letter case and ``..`` judged on the canonical form)."""
    return canonical_path(path) == canonical_path(root) or is_inside(path, root)


def _machine_roots(context: EffectContext) -> list[Path]:
    """Return the folders a machine ``PATH`` entry may lie in: the package, its bin folder, the machine-wide variable folders."""
    folded = {name.casefold(): value for name, value in context.env.items()}
    return [context.package_path, context.bin_dir] + [
        Path(folded[name.casefold()]) for name in _MACHINE_ROOTS if folded.get(name.casefold())
    ]


def _require_path_entry(entry: str, context: EffectContext, scope: str, label: str, *, removal: bool = False) -> None:
    """Refuse a ``PATH`` entry that is not one absolute local folder, and, in the system scope, one outside trusted places.

    After expansion the entry must pass ``path_entry_shape_problem`` (no ``;``, ``..``, relative or UNC path...). A
    machine ``PATH`` entry must also lie inside the package root, the mode's ``bin`` folder or a machine-wide folder
    (``ProgramFiles``, ``SystemRoot``...), and, unless it is inside the package (whose ``App`` and ``bin`` may not exist
    yet), it must already exist: a folder that is missing can be created later by a standard user inside a writable
    subfolder of ``SystemRoot`` (``Temp``, ``Tasks``...). *removal* skips the existence rule, so a recorded entry whose
    folder is gone can still be taken off the ``PATH``.
    """
    problem = path_entry_shape_problem(entry, template=False)
    if problem is not None:
        raise EffectError(f"{label} {entry!r} {problem}")
    if scope != "system":
        return
    if entry.startswith("%"):
        raise EffectError(f"{label} {entry!r} follows an environment variable, which a user can override: not allowed on the machine PATH")
    normalized = os.path.normpath(entry)
    if not any(_under(normalized, root) for root in _machine_roots(context)):
        raise EffectError(
            f"{label} {entry!r} is not inside the package, the bin folder or a machine-wide folder, so it cannot go on the machine PATH"
        )
    if not removal and not _under(normalized, context.package_path) and not os.path.lexists(normalized):
        raise EffectError(
            f"{label} {entry!r} does not exist and is outside the package: a standard user could create it later, "
            "so it cannot go on the machine PATH"
        )


def _require_trusted_folder(system: System, entry: str, scope: str, context: EffectContext) -> None:
    """Refuse a machine ``PATH`` entry that is, or sits below, a link or a folder that standard users can change.

    The entry may not exist yet (the package's own ``App`` and ``bin``): its nearest existing ancestor is judged instead, and
    every existing folder between the entry and its governing root (the package's ``current``, the package, the bin folder or
    a machine-wide folder) must not be a link or junction.
    """
    if scope != "system":
        return
    folder = Path(os.path.normpath(entry))
    probe = folder
    while not os.path.lexists(probe) and probe.parent != probe:
        probe = probe.parent
    current = context.package_path / "current"
    governing = next((root for root in (current, *_machine_roots(context)) if _under(folder, root)), None)
    walker = folder
    while governing is not None and not canonical_path(walker) == canonical_path(governing) and walker.parent != walker:
        if os.path.lexists(walker) and is_reparse_point(walker):
            raise EffectError(f"PATH entry {entry!r} is, or sits below, a link or junction ({walker}) and cannot go on the machine PATH")
        walker = walker.parent
    reason = system.directory_untrusted_reason(probe)
    if reason is not None:
        raise EffectError(f"PATH entry {entry!r} can be changed by standard users ({probe}: {reason}), so it cannot go on the machine PATH")


def _require_free_name(name: str) -> None:
    """Refuse an environment variable that is the system's own (``Path`` would clobber the whole PATH)."""
    problem = env_name_problem(name)
    if problem is not None:
        raise EffectError(f"environment variable {name!r} {problem}; a package never sets or deletes it")


def _expand(text: str, values: Mapping[str, str], env: Mapping[str, str], label: str, *, literal_unknown: bool = False) -> str:
    """Expand ``$Name`` path variables and ``${NAME}`` environment references in one declared string.

    ``$$`` is a literal dollar sign. An unknown ``${NAME}`` is an error; a plain ``$Name`` that is not a
    package variable stays literal only for script content (``literal_unknown``), and otherwise is an error.
    """
    if not text:
        return ""
    folded = {name.casefold(): value for name, value in env.items()}

    def replace(match: re.Match[str]) -> str:
        escaped, braced, plain = match.groups()
        if escaped:
            return "$"
        name = braced if braced is not None else plain
        if name in values:
            return values[name]
        if name == "Data":
            raise EffectError(f"{label} uses $Data, which exists only in portable mode")
        if braced is not None:
            if name.casefold() in folded:
                return folded[name.casefold()]
            raise EffectError(f"{label} refers to ${{{name}}}, which is not defined")
        if literal_unknown:
            return match.group(0)
        raise EffectError(f"{label} uses the unknown variable ${name}")

    return _TOKEN.sub(replace, text)


def _confined(folder: Path, name: str, label: str) -> Path:
    """Return ``folder\\name`` after requiring it to stay inside *folder* (a relative name, no drive, no ``..`` escape)."""
    if not name or re.match(r"^[A-Za-z]:", name) or name.startswith(("\\", "/")):
        raise EffectError(f"{label}: '{name}' must be a relative name inside {folder}")
    for part in re.split(r"[\\/]", name):
        problem = None if part in ("", ".", "..") else name_part_problem(part)
        if problem is not None:
            raise EffectError(f"{label}: '{name}' {problem}")
    path = Path(os.path.normpath(folder / name))
    if not is_inside(path, folder) or canonical_path(path) == canonical_path(folder):
        raise EffectError(f"{label}: '{name}' would write outside {folder}")
    return path


# ---------------------------------------------------------------------------
# Applying, removing and verifying
# ---------------------------------------------------------------------------


def apply_effect(system: System, effect: Effect, context: EffectContext, previous: Effect | None = None) -> Effect:
    """Create or replace one effect on the machine and return the effect as it must be recorded.

    Parameters
    ----------
    system : System
        The boundary that writes shortcuts, the environment and ``PATH``.
    effect : Effect
        The desired effect.
    context : EffectContext
        Where the mode's folders are.
    previous : Effect, optional
        The recorded effect with the same identity; a ``PATH`` entry that an earlier install added stays
        marked as added.

    Returns
    -------
    Effect
        The effect with run-time facts filled in (a ``PATH`` entry's ``added``).

    Raises
    ------
    EffectError
        If a path would leave the mode's folder.
    OSError
        If the machine refuses the change.
    """
    fields = effect.fields
    if effect.kind == "shortcut":
        path = _inside(Path(str(fields["path"])), context.start_menu, "shortcut")
        system.write_shortcut(path, ShortcutSpec(
            target_path=str(fields["target"]), arguments=str(fields.get("arguments", "")),
            working_directory=str(fields.get("working_dir", "")), icon_location=str(fields.get("icon_location", "")),
            description=str(fields.get("description", "")),
        ))
        return effect
    if effect.kind == "shim":
        _write_shim(fields, context)
        return effect
    if effect.kind == "file":
        path = _inside(Path(str(fields["path"])), context.bin_dir, "wrapper")
        write_bytes_atomic(path, _wrapper_bytes(path, str(fields["content"])))
        return effect
    if effect.kind == "forwarder":
        path = _inside(Path(str(fields["path"])), context.root_data, "root forwarder")
        write_bytes_atomic(path, str(fields["content"]).encode("ascii"))
        return effect
    if effect.kind == "env":
        _require_free_name(str(fields["name"]))
        system.set_env(_scope(fields), str(fields["name"]), str(fields["value"]), True)
        return effect
    if effect.kind == "path":
        scope = _scope(fields)
        entry = str(fields["entry"])
        if fields.get("shared"):
            context.bin_dir.mkdir(parents=True, exist_ok=True)
        else:
            _require_path_entry(entry, context, str(fields.get("scope")), "PATH entry")
            _require_trusted_folder(system, entry, str(fields.get("scope")), context)
        current = system.get_path(scope)
        if normalize_entry(entry) in {normalize_entry(item) for item in current}:
            added = bool(previous.fields.get("added")) if previous is not None else False
        else:
            system.set_path(scope, [*current, entry])
            added = True
        return Effect(effect.kind, {**fields, "added": added}, effect.adopted)
    raise EffectError(f"unknown effect kind {effect.kind!r}")


def changes_environment(effect: Effect) -> bool:
    """Return whether applying or removing *effect* changes the environment (so a broadcast is due)."""
    return effect.kind in ("env", "path")


def remove_effect(system: System, effect: Effect, context: EffectContext) -> bool:
    """Remove one recorded effect from the machine; return whether anything was removed.

    A shortcut, shim or wrapper is deleted only when its recorded path lies inside the folder the mode
    owns. An environment value is deleted by name. A ``PATH`` entry is removed only when this install added
    it and it is not the shared ``bin`` folder.

    Raises
    ------
    EffectError
        If a recorded path lies outside the mode's folder.
    OSError
        If the machine refuses the change.
    """
    fields = effect.fields
    if effect.kind == "shortcut":
        path = _inside(Path(str(fields["path"])), context.start_menu, "shortcut")
        existed = _shortcut_exists(system, path)
        system.delete_shortcut(path)
        _prune_empty(path.parent, context.start_menu)
        return existed
    if effect.kind in ("shim", "file"):
        path = _inside(Path(str(fields["path"])), context.bin_dir, "shim")
        removed = False
        names = [path] + ([Path(str(fields["config"]))] if effect.kind == "shim" and fields.get("config") else [])
        for name in names:
            name = _inside(name, context.bin_dir, "shim")
            if name.is_file():
                os.chmod(name, 0o666)
                name.unlink()
                removed = True
        _prune_empty(path.parent, context.bin_dir)
        return removed
    if effect.kind == "forwarder":
        path = _inside(Path(str(fields["path"])), context.root_data, "root forwarder")
        if not path.is_file():
            return False
        os.chmod(path, 0o666)
        path.unlink()
        return True
    if effect.kind == "env":
        _require_free_name(str(fields["name"]))
        system.delete_env(_scope(fields), str(fields["name"]))
        return True
    if effect.kind == "path":
        if not fields.get("added") or fields.get("shared"):
            return False
        scope = _scope(fields)
        _require_path_entry(str(fields["entry"]), context, str(fields.get("scope")), "recorded PATH entry", removal=True)
        current = system.get_path(scope)
        wanted = normalize_entry(str(fields["entry"]))
        kept = [item for item in current if normalize_entry(item) != wanted]
        if len(kept) == len(current):
            return False
        system.set_path(scope, kept)
        return True
    raise EffectError(f"unknown effect kind {effect.kind!r}")


def verify_each(system: System, effects: Sequence[Effect], *, elevated: bool) -> list[tuple[Effect, str | None, str | None]]:
    """Compare each recorded effect with the machine; return ``(effect, drift, unverified)`` per effect.

    ``drift`` says what is missing or differs (a deleted shim, a changed shortcut, an environment value or
    ``PATH`` entry that is gone); ``unverified`` says why an effect could not be judged. An elevated run does not
    read the user's environment, so a user-scope effect is unverified instead. Exactly one of the two is set, or neither
    when the effect is in place.
    """
    found: list[tuple[Effect, str | None, str | None]] = []
    for effect in effects:
        fields = effect.fields
        drift: str | None = None
        unverified: str | None = None
        try:
            if effect.kind == "shortcut":
                try:
                    stored = system.read_shortcut(Path(str(fields["path"])))
                except OSError:
                    drift = f"shortcut missing: {fields['path']}"
                else:
                    wanted = (str(fields["target"]), str(fields.get("arguments", "")), str(fields.get("working_dir", "")))
                    if (stored.target_path, stored.arguments, stored.working_directory) != wanted:
                        drift = f"shortcut differs: {fields['path']}"
            elif effect.kind == "shim":
                if not Path(str(fields["path"])).is_file():
                    drift = f"shim missing: {fields['path']}"
                elif not _file_has(Path(str(fields["config"])), str(fields["config_sha256"])):
                    drift = f"shim configuration differs: {fields['config']}"
                elif str(fields.get("linkage", "dynamic")) == "dynamic" and (
                    missing := shim_runtime_missing(Path(str(fields["path"])).parent)
                ):
                    drift = f"shim runtime files missing in {Path(str(fields['path'])).parent / SHIM_LIB_NAME}: {', '.join(missing)}"
            elif effect.kind in ("file", "forwarder"):
                if not _file_has(Path(str(fields["path"])), str(fields["sha256"])):
                    drift = f"{'root forwarder' if effect.kind == 'forwarder' else 'file'} missing or changed: {fields['path']}"
            elif effect.kind in ("env", "path"):
                scope = _scope(fields)
                if elevated and scope is Scope.USER:
                    unverified = f"{effect.kind} in the user environment (not read in an elevated run)"
                elif effect.kind == "env":
                    stored_value = system.get_env(scope, str(fields["name"]))
                    if stored_value is None or stored_value.value != str(fields["value"]):
                        drift = f"environment variable {fields['name']} differs or is missing"
                elif normalize_entry(str(fields["entry"])) not in {normalize_entry(item) for item in system.get_path(scope)}:
                    drift = f"PATH entry missing: {fields['entry']}"
            else:
                unverified = f"effect kind {effect.kind}"
        except (KeyError, EffectError) as exc:
            drift = f"malformed {effect.kind} effect: {exc}"
        found.append((effect, drift, unverified))
    return found


def verify_effects(system: System, effects: Sequence[Effect], *, elevated: bool) -> tuple[list[str], list[str]]:
    """Compare recorded effects with the machine; return ``(drift, unverified)`` as lists of messages."""
    rows = verify_each(system, effects, elevated=elevated)
    return [drift for _, drift, _ in rows if drift], [text for _, _, text in rows if text]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(fields: Mapping[str, Any]) -> Scope:
    """Return the environment scope named by an effect."""
    scope = {"system": Scope.MACHINE, "user": Scope.USER}.get(str(fields.get("scope", "")))
    if scope is None:
        raise EffectError(f"an effect names the unknown environment scope {fields.get('scope')!r}")
    return scope


def _inside(path: Path, folder: Path | None, what: str) -> Path:
    """Require *path* to lie inside the mode's *folder* (never the folder itself) before it is written or deleted."""
    if folder is None:
        raise EffectError(f"this mode has no folder for a {what}, yet the install record names {path}")
    if not is_inside(path, folder) or canonical_path(path) == canonical_path(folder):
        raise EffectError(f"refusing to touch {path}: it is outside {folder}")
    return path


def _file_has(path: Path, digest: str) -> bool:
    """Return whether *path* is a file whose SHA-256 is *digest*."""
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        pass
    return False


def _shortcut_exists(system: System, path: Path) -> bool:
    """Return whether the system can read a shortcut at *path*."""
    try:
        system.read_shortcut(path)
    except OSError:
        return False
    return True


def _prune_empty(folder: Path, stop: Path | None) -> None:
    """Remove empty folders from *folder* upwards, never *stop* itself and never leaving it."""
    while stop is not None and is_inside(folder, stop) and canonical_path(folder) != canonical_path(stop):
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def shim_library_files() -> list[Path]:
    """Return the files of the shim runtime folder (``exe-shim-lib``: the DLLs, their assembly manifest and the licenses)."""
    folder = _SHIM_DIR / SHIM_LIB_NAME
    return sorted(item for item in folder.iterdir() if item.is_file()) if folder.is_dir() else []


def _remove_legacy_loose_files(bin_dir: Path) -> None:
    """Delete the runtime files an older gupkg left loose in *bin_dir*, but only a file that is byte-for-byte ours.

    A launcher now loads its libraries from ``exe-shim-lib`` alone, so a loose copy is dead weight that could be picked up by
    another program in ``bin``; a file that differs is somebody else's and stays.
    """
    for name in _LEGACY_LOOSE_FILES:
        loose, ours = bin_dir / name, _SHIM_DIR / SHIM_LIB_NAME / name
        try:
            if loose.is_file() and ours.is_file() and loose.read_bytes() == ours.read_bytes():
                loose.unlink()
        except OSError:
            pass  # in use or not ours to delete: leave it


def shim_runtime_missing(bin_dir: Path) -> list[str]:
    """Return the names of the ``exe-shim-lib`` files missing from *bin_dir* (what a dynamic launcher cannot start without)."""
    folder = bin_dir / SHIM_LIB_NAME
    return [source.name for source in shim_library_files() if not (folder / source.name).is_file()]


def _write_shim(fields: Mapping[str, Any], context: EffectContext) -> None:
    """Write a launcher, its config and, for a dynamic launcher, the runtime files that are not there yet."""
    path = _inside(Path(str(fields["path"])), context.bin_dir, "shim")
    config = _inside(Path(str(fields["config"])), context.bin_dir, "shim")
    linkage = str(fields.get("linkage", "dynamic"))
    if linkage not in ("dynamic", "static"):
        raise EffectError("shim linkage must be either 'dynamic' or 'static'")
    kind = str(fields.get("type", "console"))
    launcher = _SHIM_DIR / (f"shim-{kind}.exe" if linkage == "dynamic" else f"shim-{kind}.static.exe")
    context.bin_dir.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(path, launcher.read_bytes())
    write_bytes_atomic(config, shim_config_text(fields).encode("utf-8"))

    # Shared runtime files are never overwritten: another command may be using them.
    if linkage == "dynamic":
        for name in _SHIM_LICENSES:
            target = context.bin_dir / name
            if not target.exists():
                write_bytes_atomic(target, (_SHIM_DIR / name).read_bytes())
        library = context.bin_dir / SHIM_LIB_NAME
        library.mkdir(exist_ok=True)
        for source in shim_library_files():
            target = library / source.name
            if not target.exists():
                write_bytes_atomic(target, source.read_bytes())
        _remove_legacy_loose_files(context.bin_dir)
