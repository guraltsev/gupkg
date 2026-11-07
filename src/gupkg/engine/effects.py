"""Turn a package's declarations into integration effects, apply and remove them, and verify them.

The effects of a version are its ``[[shortcut]]`` rows (Start Menu links), ``[[shim]]`` rows
(commands in the mode's ``bin`` folder), ``[[env]]`` rows (environment values), ``[[path]]`` rows
(``PATH`` entries). Each
becomes one ``Effect`` of ``gupkg.model.state`` whose fields hold everything needed to apply, remove
and verify it. Variables (``$App``, ``$Bin``...) are expanded when the effect is built, against
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
registry is a write target (design 9.5): an environment value is written and deleted without being looked up,
and ``PATH`` is consulted only as part of its read-modify-write; ``verify_effects`` inspects only values that gupkg
wrote. An environment or ``PATH`` entry is never changed for a mode that does not touch the environment.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..catalog.local import canonical_path, is_inside, is_reparse_point
from ..core import write_bytes_atomic
from ..model.manifest import Manifest, env_name_problem, name_part_problem, path_entry_shape_problem
from ..model.scope import ENV_MACHINE, Scope
from ..model.plan import OVERWRITE_BIN_FILES, OVERWRITE_SHORTCUTS, effect_key, normalize_entry
from ..model.state import Effect
from ..system.port import Mode, ShortcutSpec, System
from ..tomlio import dumps
from .planner import UnownedFile, name_key

# The root's setting that package launchers read to find the installed gupkg (a one-line data file in ``<root>\.gupkg``).
LOCATION_FILE = "gupkg-location"

# The folder that holds the bundled launcher binaries, their licenses and their runtime files.
_SHIM_DIR = Path(__file__).resolve().parents[1] / "shim"

# The folder of runtime files a dynamically linked launcher loads its libraries from (``exe-shim-lib`` beside the launcher:
# the launchers do not look in their own folder, so a stray DLL in ``bin`` is never picked up by mistake).
SHIM_LIB_NAME = "exe-shim-lib"


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
        ``system`` or ``user``.
    bin_dir : Path
        The mode's ``bin`` folder.
    start_menu : Path or None
        The Start Menu ``opt`` folder; ``None`` when the mode creates no shortcuts.
    scope : Mode or None
        The environment and ``PATH`` target; ``None`` when the mode does not touch the environment.
    env : mapping
        The environment used to expand ``${NAME}`` references.
    linkage : str
        ``dynamic`` or ``static`` launcher build.
    root_data : Path or None
        The root's data folder ``<root>\\.gupkg``, where ``gupkg-location`` is written; ``None`` when unknown.
    """

    package_path: Path
    mode: str
    bin_dir: Path
    start_menu: Path | None
    scope: Mode | None
    env: Mapping[str, str]
    linkage: str = "dynamic"
    root_data: Path | None = None


def effect_context(mode_context: Scope, package_path: Path, env: Mapping[str, str]) -> EffectContext:
    """Build the effect context of a package from its resolved install mode.

    Parameters
    ----------
    mode_context : Scope
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
        scope = Mode.SYSTEM if mode_context.env_target == ENV_MACHINE else Mode.USER
    return EffectContext(
        Path(package_path), mode_context.mode, mode_context.bin_dir, mode_context.start_menu, scope, env,
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
        ``(effects, notes)``. A mode that creates no shortcuts or touches no environment (a user
        root with ``integrate = "none"``) leaves those rows out and says so in the notes.

    Raises
    ------
    EffectError
        If a row cannot be expanded (an unknown ``${NAME}`` or ``$Name``) or would
        write outside the Start Menu or ``bin`` folder.
    """
    package = context.package_path
    values = {
        "App": str(package / "current" / "App"),
        "VersionRoot": str(package / "current"),
        "PackageRoot": str(package),
        "Bin": str(context.bin_dir),
    }
    effects: list[Effect] = []
    notes: list[str] = []

    # Shortcuts live below the Start Menu folder; a mode without one declares none.
    if manifest.shortcuts and context.start_menu is None:
        notes.append("this root does not create shortcuts (integrate = \"none\"): shortcuts were left out")
    for row in manifest.shortcuts if context.start_menu is not None else ():
        if row.modes is not None and context.mode not in row.modes:
            continue
        label = f"shortcut '{row.name}'"
        name = _expand(row.name, values, context.env, f"{label} name").strip()
        link = _confined(context.start_menu, name if name.lower().endswith(".lnk") else name + ".lnk", label)
        arguments = _expand(row.arguments, values, context.env, f"{label} arguments")
        # run_as_admin is recorded only when true, so records written before it existed still match.
        effects.append(Effect("shortcut", {
            "path": str(link),
            "target": _expand(row.target, values, context.env, f"{label} target"),
            "arguments": arguments,
            "working_dir": _expand(row.working_dir, values, context.env, f"{label} working_dir"),
            "icon_location": _expand(row.icon_location, values, context.env, f"{label} icon_location"),
            "description": _expand(row.description, values, context.env, f"{label} description"),
            **({"run_as_admin": True} if row.run_as_admin else {}),
        }))

    # Shims live in the mode's bin folder; a launcher is an .exe with a config file, a content shim is one file.
    for shim in manifest.shims:
        label = f"shim '{shim.name}'"

        # A content shim is one wrapper file, identified by the hash of the bytes that will be written.
        if shim.content is not None:
            path = _confined(context.bin_dir, shim.name, label)
            content = _expand(shim.content, values, context.env, f"{label} content", literal_unknown=True)
            effects.append(Effect("file", {
                "path": str(path), "content": content, "sha256": hashlib.sha256(_wrapper_bytes(path, content)).hexdigest(),
            }))
            continue

        # A launcher shim is an ``.exe`` plus a ``.config.toml``; the config's hash is recorded to detect drift.
        stem = shim.name[:-4] if shim.name.lower().endswith(".exe") else shim.name
        path = _confined(context.bin_dir, stem + ".exe", label)

        # Every field needed to write, remove and verify the launcher is recorded now, variables already expanded.
        fields: dict[str, Any] = {
            "path": str(path),
            "config": str(path.with_name(f"{stem}.config.toml")),
            "type": shim.type,
            "linkage": context.linkage,
            # What the launcher starts and how it passes its own arguments on.
            "target": _expand(shim.target, values, context.env, f"{label} target"),
            "arguments": [_expand(item, values, context.env, f"{label} argument") for item in shim.arguments],
            "forward_args": shim.forward_args,
            "elevate": shim.elevate,
            "working_dir": _expand(shim.working_dir or "", values, context.env, f"{label} working_dir"),
            # The preferred targets and the PATH search are recorded only when declared, so records written before they
            # existed still match.
            **({"prefer_targets": [_expand(item, values, context.env, f"{label} prefer_targets") for item in shim.prefer_targets]}
               if shim.prefer_targets else {}),
            **({"search_path": True} if shim.search_path else {}),
            **({"skip_app_execution_aliases": True} if shim.skip_app_execution_aliases else {}),
        }

        # The hash of the config text lets verification detect an edited configuration.
        fields["config_sha256"] = hashlib.sha256(shim_config_text(fields).encode("utf-8")).hexdigest()
        effects.append(Effect("shim", fields))

    # Only installing the gupkg package writes the root's gupkg-location: it lets the package launchers of this root find the
    # installed gupkg (design 21.3, decision D52).
    if package.name.casefold() == "gupkg" and context.root_data is not None:
        effects.append(_location_effect(context.root_data, package))

    # Environment values and PATH entries need an environment target.
    wants_environment = bool(manifest.envs or manifest.paths or manifest.shims)
    if context.scope is None:
        if manifest.envs or manifest.paths:
            notes.append("this root does not touch the environment (integrate = \"none\"): environment values and PATH entries were left out")
        if manifest.shims:
            notes.append(f"shims are in {context.bin_dir}; run `gupkg shell` for the PATH line to use")
    elif wants_environment:
        scope = "system" if context.scope is Mode.SYSTEM else "user"

        # Environment values: each name must be free, and the value is expanded now.
        for row in manifest.envs:
            _require_free_name(row.name)
            effects.append(Effect("env", {
                "scope": scope, "name": row.name, "value": _expand(row.value, values, context.env, f"environment variable '{row.name}'"),
            }))

        # Shims need the shared ``bin`` folder on PATH; it is marked shared so removal never takes it off.
        if manifest.shims:
            effects.append(Effect("path", {"scope": scope, "entry": str(context.bin_dir), "added": False, "shared": True}))

        # PATH entries declared by the package: expanded, shape-checked and, in system scope, trust-checked.
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


def _location_effect(root_data: Path, package: Path) -> Effect:
    """Return the ``location_file`` effect of the gupkg package: ``<root>\\.gupkg\\gupkg-location`` naming ``gupkg\\current\\App``.

    The value is relative to the root, so the file stays right when the root is moved, and it goes through ``current``, so a new
    version needs no change. Launchers read the first line as data and never execute it (design 21.3).
    """
    value = os.path.relpath(package / "current" / "App", root_data.parent)
    return Effect("location_file", {"path": str(root_data / LOCATION_FILE), "content": value})


def _location_first_line(path: Path) -> str | None:
    """Return the first line of *path* without its line ending, or ``None`` when it cannot be read."""
    try:
        text = path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines()
    return lines[0].strip() if lines else ""


def launchers_find_gupkg(root: Path, path_value: str | None, untrusted_reason: Callable[[Path], str | None] | None = None) -> bool:
    """Return whether a package launcher of *root* would find a gupkg at step 2 or step 3 of its order (design 21.3, 21.8).

    Step 2 is the folder named by the first line of ``<root>\\.gupkg\\gupkg-location`` (relative to *root*) when it holds a
    ``gupkg.cmd``. Step 3 is a ``gupkg.cmd`` or ``gupkg.exe`` in a folder of *path_value* (a ``PATH`` string). The bundled copy
    (step 1) is deliberately not asked: ``cleanup --bundled`` removes it. The launcher refuses a location line with ``& | < > ^``
    and removes quotes from it, and its ``where`` ignores quoted ``PATH`` entries; this function does the same.

    *untrusted_reason* is the machine's judge of a folder (``System.directory_untrusted_reason``), given in system mode: an
    elevated launcher refuses a gupkg that standard users can change, so such a folder does not count as found.

    Parameters
    ----------
    root : Path
        The install root whose ``.gupkg`` folder holds ``gupkg-location``.
    path_value : str or None
        A ``PATH`` string to search for step 3, or ``None`` for none.
    untrusted_reason : callable, optional
        Judges a folder; a folder with a reason does not count as found.

    Returns
    -------
    bool
        ``True`` when a launcher would find a gupkg at step 2 or step 3.
    """
    def _usable(folder: Path) -> bool:
        """Return whether *folder* passes the optional trust judge."""
        return untrusted_reason is None or untrusted_reason(folder) is None

    # Step 2: the location file names a folder relative to the root; lines the launcher would refuse do not count.
    line = _location_first_line(Path(root) / ".gupkg" / LOCATION_FILE)
    if line and not any(char in line for char in "&|<>^"):
        folder = Path(os.path.normpath(Path(root) / line.replace('"', "")))
        if (folder / "gupkg.cmd").is_file() and _usable(folder):
            return True

    # Step 3: any unquoted PATH folder that holds a gupkg launcher.
    for entry in (path_value or "").split(os.pathsep):
        folder = Path(entry.strip())
        if entry.strip() and '"' not in entry and any((folder / name).is_file() for name in ("gupkg.cmd", "gupkg.exe")) and _usable(folder):
            return True
    return False


def shim_config_text(fields: Mapping[str, Any]) -> str:
    """Return the ``.config.toml`` text of a shim effect.

    Parameters
    ----------
    fields : Mapping[str, Any]
        The fields of a ``shim`` effect (``target``, ``arguments``, ``forward_args``, ``elevate``, ``working_dir``,
        ``prefer_targets``, ``search_path``, ``skip_app_execution_aliases``).

    Returns
    -------
    str
        The TOML text the launcher reads next to its ``.exe``.
    """
    # Required keys first, with defaults matching a plain forwarding launcher.
    config: dict[str, Any] = {
        "target": fields["target"],
        "forward_arguments": bool(fields.get("forward_args", True)),
        "elevate": bool(fields.get("elevate", False)),
    }

    # Optional keys appear only when the effect sets them, so the text stays minimal and stable.
    if fields.get("prefer_targets"):
        config["prefer_targets"] = list(fields["prefer_targets"])
    for key in ("search_path", "skip_app_execution_aliases"):
        if fields.get(key):
            config[key] = True
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


def _under(path: str | Path, root: Path) -> bool:
    """Return whether *path* is *root* or lies below it (letter case and ``..`` judged on the canonical form)."""
    return canonical_path(path) == canonical_path(root) or is_inside(path, root)


def _machine_roots(context: EffectContext) -> list[Path]:
    """Return the folders a machine ``PATH`` entry may lie in: the package, its bin folder, the machine-wide variable folders."""
    # The variables whose folders are machine-wide, so a system ``PATH`` entry below one of them is as trusted as the package root.
    machine_roots = (
        "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles", "CommonProgramFiles(x86)", "CommonProgramW6432",
        "SystemRoot", "windir",
    )
    folded = {name.casefold(): value for name, value in context.env.items()}
    return [context.package_path, context.bin_dir] + [
        Path(folded[name.casefold()]) for name in machine_roots if folded.get(name.casefold())
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
    # The shape rules apply in every scope; the trust rules below apply only to the machine PATH.
    problem = path_entry_shape_problem(entry, template=False)
    if problem is not None:
        raise EffectError(f"{label} {entry!r} {problem}")
    if scope != "system":
        return

    # A machine PATH entry must be a fixed location: not an environment reference, and inside a trusted root.
    if entry.startswith("%"):
        raise EffectError(f"{label} {entry!r} follows an environment variable, which a user can override: not allowed on the machine PATH")
    normalized = os.path.normpath(entry)
    if not any(_under(normalized, root) for root in _machine_roots(context)):
        raise EffectError(
            f"{label} {entry!r} is not inside the package, the bin folder or a machine-wide folder, so it cannot go on the machine PATH"
        )

    # A missing folder outside the package could be created later by a standard user, so it is refused (not on removal).
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
    # Find the nearest existing ancestor: that is the folder whose permissions can be judged.
    folder = Path(os.path.normpath(entry))
    probe = folder
    while not os.path.lexists(probe) and probe.parent != probe:
        probe = probe.parent

    # Walk from the entry up to its governing root and refuse any link or junction on the way.
    current = context.package_path / "current"
    governing = next((root for root in (current, *_machine_roots(context)) if _under(folder, root)), None)
    walker = folder
    while governing is not None and not canonical_path(walker) == canonical_path(governing) and walker.parent != walker:
        if os.path.lexists(walker) and is_reparse_point(walker):
            raise EffectError(f"PATH entry {entry!r} is, or sits below, a link or junction ({walker}) and cannot go on the machine PATH")
        walker = walker.parent

    # Finally ask the machine whether standard users can change the judged folder.
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

    # ``$$``, ``${NAME}`` and ``$Name`` are the three forms of a variable reference.
    token = re.compile(r"\$(?:(\$)|\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
    folded = {name.casefold(): value for name, value in env.items()}

    def _replace(match: re.Match[str]) -> str:
        """Return the replacement text of one variable reference, or raise ``EffectError`` for an unknown one."""
        # An escaped dollar and a package variable are resolved first; the environment is consulted only for ``${NAME}``.
        escaped, braced, plain = match.groups()
        if escaped:
            return "$"
        name = braced if braced is not None else plain
        if name in values:
            return values[name]

        # Environment names are matched without regard to letter case, as Windows does.
        if braced is not None:
            if name.casefold() in folded:
                return folded[name.casefold()]
            raise EffectError(f"{label} refers to ${{{name}}}, which is not defined")
        if literal_unknown:
            return match.group(0)
        raise EffectError(f"{label} uses the unknown variable ${name}")

    return token.sub(_replace, text)


def _confined(folder: Path, name: str, label: str) -> Path:
    """Return ``folder\\name`` after requiring it to stay inside *folder* (a relative name, no drive, no ``..`` escape)."""
    # The name must be relative and made of valid file-name parts.
    if not name or re.match(r"^[A-Za-z]:", name) or name.startswith(("\\", "/")):
        raise EffectError(f"{label}: '{name}' must be a relative name inside {folder}")
    for part in re.split(r"[\\/]", name):
        problem = None if part in ("", ".", "..") else name_part_problem(part)
        if problem is not None:
            raise EffectError(f"{label}: '{name}' {problem}")

    # After normalising ``..`` the result must still lie strictly inside the folder.
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

    # Shortcuts are written through the system boundary, only below the Start Menu folder.
    if effect.kind == "shortcut":
        path = _inside(Path(str(fields["path"])), context.start_menu, "shortcut")
        system.write_shortcut(path, ShortcutSpec(
            target_path=str(fields["target"]), arguments=str(fields.get("arguments", "")),
            working_directory=str(fields.get("working_dir", "")), icon_location=str(fields.get("icon_location", "")),
            description=str(fields.get("description", "")), run_as_admin=bool(fields.get("run_as_admin", False)),
        ))
        return effect

    # A launcher shim writes its ``.exe``, its configuration and the shared runtime files.
    if effect.kind == "shim":
        _write_shim(fields, context)
        return effect

    # Content wrappers and the gupkg-location file are plain files written atomically inside their owning folder.
    if effect.kind == "file":
        path = _inside(Path(str(fields["path"])), context.bin_dir, "wrapper")
        write_bytes_atomic(path, _wrapper_bytes(path, str(fields["content"])))
        return effect
    if effect.kind == "location_file":
        path = _inside(Path(str(fields["path"])), context.root_data, "gupkg-location")
        write_bytes_atomic(path, (str(fields["content"]) + "\r\n").encode("ascii"))
        return effect

    # An environment value is set by name, unless the name is one the system owns.
    if effect.kind == "env":
        _require_free_name(str(fields["name"]))
        system.set_env(_scope(fields), str(fields["name"]), str(fields["value"]), True)
        return effect

    # A PATH entry is appended only when absent; ``added`` records whether this install put it there.
    if effect.kind == "path":
        scope = _scope(fields)
        entry = str(fields["entry"])

        # The shared bin folder is created on demand; any other entry must pass the shape and trust rules first.
        if fields.get("shared"):
            context.bin_dir.mkdir(parents=True, exist_ok=True)
        else:
            _require_path_entry(entry, context, str(fields.get("scope")), "PATH entry")
            _require_trusted_folder(system, entry, str(fields.get("scope")), context)

        # Append the entry unless it is already there, keeping an earlier install's claim to have added it.
        current = system.get_path(scope)
        if normalize_entry(entry) in {normalize_entry(item) for item in current}:
            added = bool(previous.fields.get("added")) if previous is not None else False
        else:
            system.set_path(scope, [*current, entry])
            added = True
        return Effect(effect.kind, {**fields, "added": added}, effect.adopted)
    raise EffectError(f"unknown effect kind {effect.kind!r}")


def find_unowned(
    system: System, desired: Sequence[Effect], owners: Mapping[tuple[str, str], str]
) -> tuple[UnownedFile, ...]:
    """Return the shims, wrappers and shortcuts of *desired* that would be written over a file no installed package owns.

    *owners* maps each name key to the package that provides it, every package of the root included (this one too), so a name
    the installing package already owns is never unowned. A path that does not exist, or that already holds exactly what would
    be written (a shim by its bytes, a configuration or wrapper by its content, a shortcut by every field), is not a conflict.
    ``env`` and ``path`` effects write no file and are not looked at.

    Parameters
    ----------
    system : System
        The boundary used to read existing shortcuts.
    desired : Sequence[Effect]
        The effects the installing package wants.
    owners : Mapping[tuple[str, str], str]
        Name key to the package that provides it, for every installed package.

    Returns
    -------
    tuple[UnownedFile, ...]
        One entry per file or shortcut that would overwrite something nobody owns.
    """
    found: list[UnownedFile] = []
    for effect in desired:
        fields = effect.fields

        # A shortcut is a conflict only when an unowned, different shortcut already sits at its path.
        if effect.kind == "shortcut":
            path = str(fields["path"])
            if name_key("shortcut", path) not in owners and (reason := _shortcut_conflict(system, Path(path), fields)):
                found.append(UnownedFile(path, OVERWRITE_SHORTCUTS, reason))

        # A shim writes its launcher and config, a wrapper its content; each unowned file with other bytes is a conflict.
        elif effect.kind in ("shim", "file"):
            path = str(fields["path"])
            if effect.kind == "file":
                writes = [(path, _wrapper_bytes(Path(path), str(fields["content"])))]
            else:
                writes = [(path, _launcher(fields).read_bytes())]
                if fields.get("config"):
                    writes.append((str(fields["config"]), shim_config_text(fields).encode("utf-8")))

            # Report each target that exists, is unowned and holds other bytes than would be written.
            for path, data in writes:
                if name_key("file", path) not in owners and os.path.lexists(path) and not _file_is(Path(path), data):
                    found.append(UnownedFile(path, OVERWRITE_BIN_FILES, "it holds different content than the file this package would write"))
    return tuple(found)


def _file_is(path: Path, data: bytes) -> bool:
    """Return whether *path* is a file holding exactly *data*."""
    try:
        return path.is_file() and path.read_bytes() == data
    except OSError:
        return False


def _shortcut_conflict(system: System, path: Path, fields: Mapping[str, Any]) -> str:
    """Return why a shortcut at *path* differs from the one *fields* describe, or ``""`` when none exists or it is the same."""
    try:
        stored = system.read_shortcut(path)
    except (OSError, ValueError):
        return "it cannot be read as a shortcut" if os.path.lexists(path) else ""
    differences = shortcut_differences(stored, fields)
    return "it differs from the shortcut this package would write: " + "; ".join(differences) if differences else ""


def shortcut_differences(stored: ShortcutSpec, fields: Mapping[str, Any]) -> list[str]:
    """Return what differs between the shortcut *stored* and the one the effect *fields* describe, one phrase per field.

    This is the one comparison of a shortcut, used to tell an unowned shortcut from one that is already right and to find
    drift. Paths (target, working directory, icon) ignore letter case; arguments and description are compared exactly. The
    wanted icon and description are compared as Windows reads them back: an icon has an index, a description at most 260 characters.

    Parameters
    ----------
    stored : ShortcutSpec
        The shortcut as read from the machine.
    fields : Mapping[str, Any]
        The fields of a ``shortcut`` effect.

    Returns
    -------
    list[str]
        One phrase per differing field; empty when the shortcut matches.
    """
    # The wanted icon and description are normalised to what Windows would hand back after saving.
    found: list[str] = []
    icon = str(fields.get("icon_location", ""))
    description = str(fields.get("description", ""))

    # Compare each field in turn; paths ignore letter case and the others are exact.
    for label, have, want, compared, is_path in (
        ("target", stored.target_path, str(fields["target"]), None, True),
        ("arguments", stored.arguments, str(fields.get("arguments", "")), None, False),
        ("working directory", stored.working_directory, str(fields.get("working_dir", "")), None, True),
        ("icon", stored.icon_location, icon, _with_icon_index(icon), True),
        ("description", stored.description, description, description[:260], False),
    ):
        wanted = want if compared is None else compared
        if not (same_path(have, wanted) if is_path else have == wanted):
            found.append(f"{label} is '{have}' but would be '{want}'")

    # The administrator flag is a separate boolean, not text.
    if stored.run_as_admin != bool(fields.get("run_as_admin", False)):
        found.append(f"run as administrator is {'on' if stored.run_as_admin else 'off'} but would be {'off' if stored.run_as_admin else 'on'}")
    return found


def _with_icon_index(location: str) -> str:
    """Return an icon location with its index, which is ``0`` when *location* names none; an empty text stays empty."""
    index = location.rpartition(",")[2].strip()
    return location if not location or ("," in location and index.lstrip("-").isdigit()) else f"{location},0"


def same_path(left: str, right: str) -> bool:
    """Return whether two path texts name the same place on Windows: letter case, ``/`` against ``\\`` and ``..`` are ignored.

    This is the one comparison of a shortcut's paths, the same normalisation as :func:`name_key`; an empty text equals only an empty text.

    Parameters
    ----------
    left, right : str
        The two path texts.

    Returns
    -------
    bool
        ``True`` when both texts name the same place.
    """
    if not left or not right:
        return left == right
    return name_key("file", left)[1] == name_key("file", right)[1]


def changes_environment(effect: Effect) -> bool:
    """Return whether applying or removing *effect* changes the environment (so a broadcast is due).

    Parameters
    ----------
    effect : Effect
        The effect to classify.

    Returns
    -------
    bool
        ``True`` for ``env`` and ``path`` effects.
    """
    return effect.kind in ("env", "path")


def remove_effect(system: System, effect: Effect, context: EffectContext) -> bool:
    """Remove one recorded effect from the machine; return whether anything was removed.

    A shortcut, shim or wrapper is deleted only when its recorded path lies inside the folder the mode
    owns. An environment value is deleted by name. A ``PATH`` entry is removed only when this install added
    it and it is not the shared ``bin`` folder.

    Parameters
    ----------
    system : System
        The boundary that deletes shortcuts, environment values and ``PATH`` entries.
    effect : Effect
        The recorded effect to remove.
    context : EffectContext
        Where the mode's folders are.

    Returns
    -------
    bool
        ``True`` when something was removed.

    Raises
    ------
    EffectError
        If a recorded path lies outside the mode's folder.
    OSError
        If the machine refuses the change.
    """
    fields = effect.fields

    # A shortcut is deleted, then the empty Start Menu folders it leaves behind are pruned.
    if effect.kind == "shortcut":
        path = _inside(Path(str(fields["path"])), context.start_menu, "shortcut")
        existed = _shortcut_exists(system, path)
        system.delete_shortcut(path)
        _prune_empty(path.parent, context.start_menu)
        return existed

    # A shim's launcher and config, or a wrapper file, are deleted (made writable first) and empty folders pruned.
    if effect.kind in ("shim", "file"):
        path = _inside(Path(str(fields["path"])), context.bin_dir, "shim")
        removed = False

        # Each file is confined to ``bin`` again before it is deleted, because the config path comes from the record.
        names = [path] + ([Path(str(fields["config"]))] if effect.kind == "shim" and fields.get("config") else [])
        for name in names:
            name = _inside(name, context.bin_dir, "shim")
            if name.is_file():
                os.chmod(name, 0o666)
                name.unlink()
                removed = True

        # Folders the removal emptied are cleaned up, never ``bin`` itself.
        _prune_empty(path.parent, context.bin_dir)
        return removed

    if effect.kind == "location_file":
        # Only a file that still holds the value gupkg wrote is removed: a person's edit is theirs.
        path = _inside(Path(str(fields["path"])), context.root_data, "gupkg-location")
        if not path.is_file() or _location_first_line(path) != str(fields["content"]):
            return False
        os.chmod(path, 0o666)
        path.unlink()
        return True

    # An environment value is deleted by name.
    if effect.kind == "env":
        _require_free_name(str(fields["name"]))
        system.delete_env(_scope(fields), str(fields["name"]))
        return True

    # A PATH entry is taken off only when this install added it and it is not the shared bin folder.
    if effect.kind == "path":
        if not fields.get("added") or fields.get("shared"):
            return False

        # The recorded entry is re-validated (a damaged record must not edit an arbitrary PATH), then filtered out.
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


def verify_each(system: System, effects: Sequence[Effect], *, mode: Mode) -> list[tuple[Effect, str | None, str | None]]:
    """Compare each recorded effect with the machine; return ``(effect, drift, unverified)`` per effect.

    ``drift`` says what is missing or differs (a deleted shim, a changed shortcut, an environment value or
    ``PATH`` entry that is gone); ``unverified`` says why an effect could not be judged. A system-mode run never reads the
    user's environment: a user-scope ``env`` or ``path`` effect is unverified there. Exactly one of the two is set, or neither
    when the effect is in place.

    Parameters
    ----------
    system : System
        The boundary used to read shortcuts, environment values and ``PATH``.
    effects : Sequence[Effect]
        The recorded effects to check.
    mode : Mode
        The mode of the run; a system-mode run does not read the user's environment.

    Returns
    -------
    list[tuple[Effect, str or None, str or None]]
        One ``(effect, drift, unverified)`` triple per effect, in order.
    """
    # Judge each effect by kind; a malformed record is reported as drift rather than aborting the check.
    found: list[tuple[Effect, str | None, str | None]] = []
    for effect in effects:
        fields = effect.fields
        drift: str | None = None
        unverified: str | None = None
        try:
            # A shortcut must exist and match every field of the record.
            if effect.kind == "shortcut":
                try:
                    stored = system.read_shortcut(Path(str(fields["path"])))
                except OSError:
                    drift = f"shortcut missing: {fields['path']}"
                else:
                    if differences := shortcut_differences(stored, fields):
                        drift = f"shortcut differs: {fields['path']} ({'; '.join(differences)})"

            # A shim needs its launcher, its exact configuration and, if dynamically linked, its runtime files.
            elif effect.kind == "shim":
                if not Path(str(fields["path"])).is_file():
                    drift = f"shim missing: {fields['path']}"
                elif not _file_has(Path(str(fields["config"])), str(fields["config_sha256"])):
                    drift = f"shim configuration differs: {fields['config']}"
                elif str(fields.get("linkage", "dynamic")) == "dynamic" and (
                    missing := shim_runtime_missing(Path(str(fields["path"])).parent)
                ):
                    drift = f"shim runtime files missing in {Path(str(fields['path'])).parent / SHIM_LIB_NAME}: {', '.join(missing)}"

            # A wrapper or the location file must still hold exactly what was written.
            elif effect.kind == "file":
                if not _file_has(Path(str(fields["path"])), str(fields["sha256"])):
                    drift = f"file missing or changed: {fields['path']}"
            elif effect.kind == "location_file":
                if _location_first_line(Path(str(fields["path"]))) != str(fields["content"]):
                    drift = f"gupkg-location missing or changed: {fields['path']}"

            # Environment values and PATH entries are read back from the registry, except the user's in a system run.
            elif effect.kind in ("env", "path"):
                scope = _scope(fields)
                if mode is Mode.SYSTEM and scope is Mode.USER:
                    unverified = f"{effect.kind} in the user environment (not read in a system-mode run)"

                # An environment value must exist with the recorded text.
                elif effect.kind == "env":
                    stored_value = system.get_env(scope, str(fields["name"]))
                    if stored_value is None or stored_value.value != str(fields["value"]):
                        drift = f"environment variable {fields['name']} differs or is missing"

                # A PATH entry must still be on the PATH.
                elif normalize_entry(str(fields["entry"])) not in {normalize_entry(item) for item in system.get_path(scope)}:
                    drift = f"PATH entry missing: {fields['entry']}"
            else:
                unverified = f"effect kind {effect.kind}"
        except (KeyError, EffectError) as exc:
            drift = f"malformed {effect.kind} effect: {exc}"
        found.append((effect, drift, unverified))
    return found


def verify_effects(system: System, effects: Sequence[Effect], *, mode: Mode) -> tuple[list[str], list[str]]:
    """Compare recorded effects with the machine; return ``(drift, unverified)`` as lists of messages.

    Parameters
    ----------
    system : System
        The boundary used to read the machine.
    effects : Sequence[Effect]
        The recorded effects to check.
    mode : Mode
        The mode of the run.

    Returns
    -------
    tuple[list[str], list[str]]
        Messages for effects that drifted and for effects that could not be judged.
    """
    rows = verify_each(system, effects, mode=mode)
    return [drift for _, drift, _ in rows if drift], [text for _, _, text in rows if text]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(fields: Mapping[str, Any]) -> Mode:
    """Return the environment scope named by an effect."""
    scope = {"system": Mode.SYSTEM, "user": Mode.USER}.get(str(fields.get("scope", "")))
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
    """Return the files of the shim runtime folder (``exe-shim-lib``: the DLLs, their assembly manifest and the licenses).

    Returns
    -------
    list[Path]
        The bundled runtime files, sorted; empty when the folder is absent.
    """
    folder = _SHIM_DIR / SHIM_LIB_NAME
    return sorted(item for item in folder.iterdir() if item.is_file()) if folder.is_dir() else []


def shim_runtime_missing(bin_dir: Path) -> list[str]:
    """Return the names of the ``exe-shim-lib`` files missing from *bin_dir* (what a dynamic launcher cannot start without).

    Parameters
    ----------
    bin_dir : Path
        The mode's ``bin`` folder.

    Returns
    -------
    list[str]
        File names that the bundled runtime has but *bin_dir* lacks.
    """
    folder = bin_dir / SHIM_LIB_NAME
    return [source.name for source in shim_library_files() if not (folder / source.name).is_file()]


def _launcher(fields: Mapping[str, Any]) -> Path:
    """Return the bundled launcher binary that a shim effect copies to its path."""
    linkage = str(fields.get("linkage", "dynamic"))
    if linkage not in ("dynamic", "static"):
        raise EffectError("shim linkage must be either 'dynamic' or 'static'")
    kind = str(fields.get("type", "console"))
    return _SHIM_DIR / (f"shim-{kind}.exe" if linkage == "dynamic" else f"shim-{kind}.static.exe")


def _write_shim(fields: Mapping[str, Any], context: EffectContext) -> None:
    """Write a launcher, its config and, for a dynamic launcher, the runtime files that are not there yet."""
    # Both targets must lie inside ``bin`` before anything is written.
    path = _inside(Path(str(fields["path"])), context.bin_dir, "shim")
    config = _inside(Path(str(fields["config"])), context.bin_dir, "shim")
    linkage = str(fields.get("linkage", "dynamic"))
    launcher = _launcher(fields)
    context.bin_dir.mkdir(parents=True, exist_ok=True)

    # An identical launcher is left alone: `gupkg update gupkg` runs through this very file, and Windows refuses to replace a running .exe.
    # A different one that is running can still be renamed: it is moved aside, and a later write deletes it once it has exited.
    # Leftovers of earlier renamed-aside launchers are deleted when they are no longer running.
    for stale in path.parent.glob(f".{path.name}.*.old"):
        try:
            stale.unlink()
        except OSError:
            pass

    # Write the launcher only when it differs; a running one is renamed aside and restored if the write fails.
    launcher_bytes = launcher.read_bytes()
    if not path.is_file() or path.read_bytes() != launcher_bytes:
        try:
            write_bytes_atomic(path, launcher_bytes)
        except PermissionError:
            # The running launcher cannot be replaced but can be renamed: move it aside, then write the new one.
            aside = path.with_name(f".{path.name}.{os.getpid()}-{os.urandom(4).hex()}.old")
            os.replace(path, aside)
            try:
                write_bytes_atomic(path, launcher_bytes)
            except BaseException:
                os.replace(aside, path)
                raise

    # The configuration is small and always rewritten.
    write_bytes_atomic(config, shim_config_text(fields).encode("utf-8"))

    # Shared runtime files are never overwritten: another command may be using them.
    if linkage == "dynamic":
        # The project licenses that sit beside the launcher binaries.
        shim_licenses = ("LICENSE-exe-shim-MIT.txt", "LICENSE-exe-shim-UNLICENSE.txt")
        for name in shim_licenses:
            target = context.bin_dir / name
            if not target.exists():
                write_bytes_atomic(target, (_SHIM_DIR / name).read_bytes())

        # The runtime library folder is filled with the bundled files that are not there yet.
        library = context.bin_dir / SHIM_LIB_NAME
        library.mkdir(exist_ok=True)
        for source in shim_library_files():
            target = library / source.name
            if not target.exists():
                write_bytes_atomic(target, source.read_bytes())
