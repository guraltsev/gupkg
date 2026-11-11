"""Define the settings schema (version 4) and merge its layered sources into one effective view (design section 11).

Settings come from the built-in defaults, the application's ``settings.toml``, the
root's own ``settings.toml`` and the command line. Every effective value keeps the
name of the layer that set it, and an invalid layer is refused with every problem
listed at once.

Usage and API
-------------
Wrap each parsed settings file in a ``Layer`` and call ``merge_layers(layers)``
with the layers in priority order, lowest first, starting with
``default_layer(mode)``. The result is a ``Settings`` that answers ``settings[key]``
(``"roots.system"``), lists every effective value with the layer it came from
(``settings.effective()``, for ``gupkg settings show``), and exposes the
registries. ``expand_env(text, env)`` expands ``%NAME%`` references in a path.

Implementation Approach
-----------------------
A layer is validated strictly against one schema table (unknown keys get a "did
you mean" hint, every problem is reported at once) and flattened to dotted keys.
Merging keeps the last layer that sets a key and remembers its name, so the
source of each effective value is never guessed afterwards. A ``[[registry]]``
array is replaced as a whole by the highest layer that sets it. The module is
pure: deciding which files exist, which layers a run may load and whether they
sit in admin-only folders is the job of ``gupkg.settingsload``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..system.port import Mode

# The settings schema version this gupkg accepts.
SCHEMA = 4

# The names of the layers, lowest priority first.
LAYER_DEFAULTS = "defaults"  # the built-in defaults
LAYER_APP = "app"  # the application's ``settings.toml``
LAYER_ROOT = "root"  # the root's own ``settings.toml``
LAYER_CLI = "cli"  # values given on the command line
# All layer names in priority order.
LAYER_NAMES = (LAYER_DEFAULTS, LAYER_APP, LAYER_ROOT, LAYER_CLI)

# The message shown for a settings file of the retired schema 3.
SCHEMA_3_REFUSAL = "settings schema 3 is no longer read; change it to schema = 4 (see _docs/rehaul_design.md §11)"

# The registry configured by the built-in defaults.
OFFICIAL_REGISTRY_SOURCE = "https://github.com/guraltsev/gupkg-registry.git"
# A valid registry name.
_REGISTRY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SettingsError(ValueError):
    """Report an invalid settings file or an unusable layer, listing every problem.

    Parameters
    ----------
    problems : str | Sequence[str]
        One message or a list of messages.
    """

    def __init__(self, problems: str | Sequence[str]) -> None:
        """Build the error from one message or a list of messages."""
        self.problems = (problems,) if isinstance(problems, str) else tuple(problems)
        super().__init__(
            self.problems[0] if len(self.problems) == 1 else "Invalid settings:\n  - " + "\n  - ".join(self.problems)
        )


def _text(value: Any) -> str:
    """Accept a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("must be a non-empty string")
    return value


def _choice(*names: str) -> Callable[[Any], str]:
    """Return a validator that accepts one of *names*."""

    def _check(value: Any) -> str:
        """Return *value* if it is one of the allowed names; otherwise raise ``ValueError``."""
        if value not in names:
            raise ValueError(f"must be one of {', '.join(names)}")
        return value

    return _check


def _boolean(value: Any) -> bool:
    """Accept true or false."""
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
    return value


def relative_folder(value: Any) -> str:
    """Accept a folder path relative to a root's Start Menu folder, possibly empty: no drive, no leading separator and no ``..`` part.

    Parameters
    ----------
    value : Any
        The configured value.

    Returns
    -------
    str
        *value* unchanged.

    Raises
    ------
    ValueError
        If the value is not text, has a drive or a leading separator, or contains a ``..`` part.

    Examples
    --------
    Basic usage:

    >>> relative_folder("Apps/Tools")
    'Apps/Tools'
    >>> relative_folder("")
    ''

    Edge cases:

    >>> relative_folder("C:/Tools")
    Traceback (most recent call last):
        ...
    ValueError: must be a relative folder without '..' (empty for none)
    >>> relative_folder("../Tools")
    Traceback (most recent call last):
        ...
    ValueError: must be a relative folder without '..' (empty for none)
    """
    if not isinstance(value, str):
        raise ValueError("must be text")
    if re.match(r"[A-Za-z]:|[\\/]", value) or ".." in re.split(r"[\\/]+", value):
        raise ValueError("must be a relative folder without '..' (empty for none)")
    return value


def _count(value: Any) -> int:
    """Accept a whole number of one or more."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("must be a whole number of 1 or more")
    return value


# The settings, as nested tables whose leaves are validators. ``schema`` and the
# ``[[registry]]`` array are handled separately.
_SCHEMA: dict[str, Any] = {
    "roots": {"system": _text, "user": _text},
    "integration": {"bin": _text, "start_menu": _text, "integrate": _choice("full", "none")},
    "manager": {"checksum_missing": _choice("ask", "refuse", "skip"), "keep_screen": _boolean},
    "logs": {"keep_runs": _count, "keep_days": _count},
    "cleanup": {"auto": _boolean},
    "ui": {"color": _choice("auto", "always", "never")},
    "hooks": {"allow_vendor_installers": _boolean},
    "install": {"record_manifest": _boolean},
    "adopt": {"shortcut_prefix": relative_folder},
    "check_updates": {"sources": _choice("registry", "upstream", "both")},
}
# The top-level keys a settings file may hold.
_TOP_LEVEL = ("schema", "registry", *_SCHEMA)

# One explanation per setting, written into the file that ``gupkg settings init`` creates. Keep it beside ``_SCHEMA``:
# a setting without an entry here (or an entry without a setting) stops gupkg from importing, so the template can
# never lag behind the schema.
_DOCS: dict[str, str] = {
    "roots.system": "The system root, where an administrator's installs go. Only $App\\settings.toml and --root may set it.",
    "roots.user": "The user root, where a normal user's installs go. Only $App\\settings.toml and --root may set it.",
    "integration.start_menu": (
        "Where the root's shortcuts go: the base Start Menu folder. The `name` of a shortcut in pkg.toml is relative to it\n"
        "(a games root: \"%APPDATA%\\\\Microsoft\\\\Windows\\\\Start Menu\\\\Games\")."
    ),
    "integration.bin": "Folder that holds the command shims of the root's programs (put it on PATH).",
    "integration.integrate": (
        "\"full\" or \"none\". \"none\": touch neither the registry (PATH, environment) nor the Start Menu,\n"
        "e.g. for a root on a stick."
    ),
    "adopt.shortcut_prefix": (
        "Folder put before the shortcut names that `gupkg pkg adopt` writes, relative to the Start Menu folder above\n"
        "(never a full path). Empty means none. `gupkg pkg adopt --prefix FOLDER` overrides it for one call."
    ),
    "check_updates.sources": (
        "Where `check-updates` looks for new versions in this root: \"registry\", \"upstream\" (each package's tracker)\n"
        "or \"both\". `--check-registry` and `--check-upstream` override it for one call."
    ),
    "install.record_manifest": (
        "Record a fingerprint of every file of App after an install, so `gupkg check` can notice changes. Turn it off for\n"
        "very large programs such as games; a package can also say `record_manifest = false` in its pkg.toml."
    ),
    "manager.checksum_missing": (
        "What to do with a download that has no checksum: \"ask\", \"refuse\" or \"skip\".\n"
        "Schedules treat \"ask\" as \"refuse\"."
    ),
    "manager.keep_screen": "Keep the manager's screen open after a command finishes.",
    "cleanup.auto": "true: `gupkg-manager update --all` removes old Apps, receipts and logs afterwards.",
    "logs.keep_runs": "How many runs are kept.",
    "logs.keep_days": "How many days of runs are kept.",
    "ui.color": "Colour in the terminal: \"auto\", \"always\" or \"never\".",
    "hooks.allow_vendor_installers": "Allow packages to run the vendor's own installer as a step.",
}
# Settings that a root's own settings file may not hold (``validate_layer`` refuses them), so its template leaves them out.
_NOT_IN_ROOT_FILE = ("roots",)


@dataclass(frozen=True)
class RegistryEntry:
    """One configured registry.

    Attributes
    ----------
    name : str
        The registry name.
    source : str
        Where the registry is read from.
    """

    name: str
    source: str


@dataclass(frozen=True)
class Layer:
    """One settings source: its name (``LAYER_*``), parsed data and where it came from.

    Attributes
    ----------
    name : str
        The layer name.
    data : Mapping[str, Any]
        The parsed settings of the layer.
    origin : str | None
        The file the data came from, or ``None``.
    """

    name: str
    data: Mapping[str, Any]
    origin: str | None = None


@dataclass(frozen=True)
class SettingValue:
    """One effective setting and the layer that supplied it.

    Attributes
    ----------
    key : str
        The setting key.
    value : Any
        The effective value.
    layer : str
        The layer that supplied it.
    """

    key: str
    value: Any
    layer: str


@dataclass(frozen=True)
class Settings:
    """The effective settings with the layer of every value.

    Methods
    -------
    source_of
        Return the name of the layer that supplied *key* (``registry`` for the registries).
    effective
        Return every effective value, in schema order, with its source layer.

    Attributes
    ----------
    values : Mapping[str, Any]
        The effective values by key.
    sources : Mapping[str, str]
        The layer name of each key.
    registries : tuple[RegistryEntry, ...]
        The configured registries, in priority order.
    """

    values: Mapping[str, Any]
    sources: Mapping[str, str]
    registries: tuple[RegistryEntry, ...] = field(default_factory=tuple)

    def __getitem__(self, key: str) -> Any:
        """Return the effective value of a dotted *key* such as ``roots.system``."""
        return self.values[key]

    def source_of(self, key: str) -> str:
        """Return the name of the layer that supplied *key* (``registry`` for the registries).

        Parameters
        ----------
        key : str
            A dotted settings key.

        Returns
        -------
        str
            The layer name, one of ``LAYER_NAMES``.
        """
        return self.sources[key]

    def effective(self) -> list[SettingValue]:
        """Return every effective value, in schema order, with its source layer.

        Returns
        -------
        list of SettingValue
            One row per schema key, followed by a row for the registries.
        """
        rows = [SettingValue(key, self.values[key], self.sources[key]) for key in _flat_keys()]
        rows.append(SettingValue("registry", [{"name": r.name, "source": r.source} for r in self.registries],
                                 self.sources["registry"]))
        return rows


def _flat_keys(tree: Mapping[str, Any] = _SCHEMA, prefix: str = "") -> list[str]:
    """List the dotted keys of the schema in declaration order."""
    keys: list[str] = []
    for name, spec in tree.items():
        if isinstance(spec, dict):
            keys.extend(_flat_keys(spec, f"{prefix}{name}."))
        else:
            keys.append(f"{prefix}{name}")
    return keys


if set(_DOCS) != set(_flat_keys()):  # pragma: no cover - guards a mistake made while editing the schema
    raise RuntimeError(f"settings without text, or text without a setting: {sorted(set(_DOCS) ^ set(_flat_keys()))}")


def documented_defaults(mode: str, *, root_file: bool = True) -> list[tuple[str, str, Any, str]]:
    """List every setting as ``(table, key, default, explanation)`` in schema order, for generating a settings file.

    Parameters
    ----------
    mode : str
        ``system`` or ``user``; the defaults of the ``[integration]`` folders differ.
    root_file : bool, default=True
        Leave out the settings that a root's own settings file may not hold (``[roots]``).

    Returns
    -------
    list of tuple
        ``(table, key, default, explanation)`` for each setting.
    """
    defaults = default_layer(mode).data
    rows = []
    for table, keys in _SCHEMA.items():
        if root_file and table in _NOT_IN_ROOT_FILE:
            continue
        rows.extend((table, key, defaults[table][key], _DOCS[f"{table}.{key}"]) for key in keys)
    return rows


def _hint(key: str, allowed: Sequence[str]) -> str:
    """Return a ``did you mean`` hint for the closest allowed key, or the allowed list."""
    close = difflib.get_close_matches(key, list(allowed), n=1, cutoff=0.6)
    if close:
        return f"; did you mean '{close[0]}'?"
    return f"; allowed keys: {', '.join(allowed)}"


def _flatten(tree: Mapping[str, Any], data: Mapping[str, Any], prefix: str, problems: list[str]
             ) -> dict[str, Any]:
    """Validate *data* against *tree* and return its values under dotted keys."""
    # Walk the data, reporting unknown keys and wrong shapes; every problem is collected, none stops the walk.
    flat: dict[str, Any] = {}
    for key, value in data.items():
        where = f"{prefix}{key}"
        if key not in tree:
            problems.append(f"unknown key '{where}'{_hint(key, list(tree))}")
            continue
        spec = tree[key]
        # A table recurses into the sub-schema.
        if isinstance(spec, dict):
            if not isinstance(value, dict):
                problems.append(f"'{where}' must be a table")
                continue
            flat.update(_flatten(spec, value, f"{where}.", problems))
            continue
        # A leaf is checked by its validator.
        try:
            flat[where] = spec(value)
        except ValueError as exc:
            problems.append(f"'{where}' {exc}")
    return flat


def _registries(value: Any, problems: list[str]) -> tuple[RegistryEntry, ...]:
    """Validate the ``[[registry]]`` array."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        problems.append("'registry' must be an array of tables ([[registry]])")
        return ()
    # Validate each table: only ``name`` and ``source`` are allowed, and both must be well formed.
    entries: list[RegistryEntry] = []
    for index, item in enumerate(value):
        where = f"registry[{index}]"
        for key in item:
            if key not in ("name", "source"):
                problems.append(f"unknown key '{where}.{key}'{_hint(key, ['name', 'source'])}")
        # A table with a bad name or source is reported and left out of the result.
        name, source = item.get("name"), item.get("source")
        if not isinstance(name, str) or not _REGISTRY_NAME.fullmatch(name):
            problems.append(f"'{where}.name' must be a name of letters, digits, '.', '_' and '-'")
            continue
        if not isinstance(source, str) or not source.strip():
            problems.append(f"'{where}.source' must be a non-empty string")
            continue
        entries.append(RegistryEntry(name, source))
    # Names must be unique ignoring case.
    names = [entry.name.casefold() for entry in entries]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"registry '{name}' is listed twice")
    return tuple(entries)


def validate_layer(layer: Layer) -> tuple[dict[str, Any], tuple[RegistryEntry, ...] | None]:
    """Validate one layer and return its flattened values and registries.

    Parameters
    ----------
    layer : Layer
        A layer from a file or from the built-in defaults.

    Returns
    -------
    tuple
        ``(values, registries)``: values under dotted keys, and the registries when
        the layer has a ``[[registry]]`` array (otherwise ``None``).

    Raises
    ------
    SettingsError
        If the layer has an unknown key, a wrong value or the wrong ``schema``.

    Examples
    --------
    Basic usage:

    >>> values, registries = validate_layer(Layer("user", {"schema": SCHEMA, "ui": {"color": "never"}}, "settings.toml"))
    >>> values
    {'ui.color': 'never'}
    >>> registries is None
    True

    Edge cases:

    >>> validate_layer(Layer("user", {"schema": SCHEMA, "ui": {"colour": "never"}}, "settings.toml"))
    Traceback (most recent call last):
        ...
    gupkg.model.settings.SettingsError: user settings (settings.toml): unknown key 'ui.colour'; did you mean 'color'?
    """
    # Refuse a root layer that sets ``[roots]``, then check the schema version.
    problems: list[str] = []
    data = layer.data
    if layer.name == LAYER_ROOT and "roots" in data:
        raise SettingsError(
            f"[roots] cannot be set in a root's own settings.toml ({layer.origin}); "
            "set it in $App\\settings.toml or with --root"
        )
    # Schema 3 has its own message; any other mismatch is reported with the value found.
    if data.get("schema") == 3:
        problems.append(SCHEMA_3_REFUSAL)
    elif (layer.name != LAYER_DEFAULTS or "schema" in data) and data.get("schema") != SCHEMA:
        shown = data.get("schema", "missing")
        problems.append(f"unsupported settings schema ({shown!r}); this gupkg reads schema {SCHEMA}")
    # Validate the tables against the schema and the registry array separately.
    body = {key: value for key, value in data.items() if key not in ("schema", "registry")}
    unknown_top = [key for key in body if key not in _TOP_LEVEL]
    # Unknown top-level keys get a hint; the rest are validated.
    for key in unknown_top:
        problems.append(f"unknown key '{key}'{_hint(key, [k for k in _TOP_LEVEL if k != 'schema'])}")
    values = _flatten(_SCHEMA, {k: v for k, v in body.items() if k not in unknown_top}, "", problems)
    registries = _registries(data["registry"], problems) if "registry" in data else None
    # Report every problem found, naming the layer and its origin.
    if problems:
        where = f" ({layer.origin})" if layer.origin else ""
        raise SettingsError([f"{layer.name} settings{where}: {problem}" for problem in problems])
    return values, registries


def default_layer(mode: str) -> Layer:
    """Return the built-in defaults of *mode* (``system`` or ``user``) as the lowest layer.

    The two modes differ only in the ``[integration]`` folders.

    Parameters
    ----------
    mode : str
        ``system`` or ``user``.

    Returns
    -------
    Layer
        The defaults layer, with origin ``built-in``.

    Raises
    ------
    ValueError
        If *mode* is neither ``system`` nor ``user``.
    """
    # Choose the mode-specific integration folders.
    if mode == Mode.SYSTEM:
        integration = {
            "bin": "C:\\bin",
            "start_menu": "%ProgramData%\\Microsoft\\Windows\\Start Menu\\opt",
            "integrate": "full",
        }
    # A user run keeps its shims and shortcuts under the user's profile.
    elif mode == Mode.USER:
        integration = {
            "bin": "%USERPROFILE%\\bin",
            "start_menu": "%APPDATA%\\Microsoft\\Windows\\Start Menu\\opt",
            "integrate": "full",
        }
    else:
        raise ValueError(f"unknown mode {mode!r}; use 'system' or 'user'")
    # Everything else is the same for both modes.
    return Layer(
        LAYER_DEFAULTS,
        {
            "schema": SCHEMA,
            "roots": {
                "system": "C:\\opt",
                "user": "%USERPROFILE%\\opt",
            },
            "integration": integration,
            # The values below are the same for both modes.
            "manager": {"checksum_missing": "ask", "keep_screen": True},
            "registry": [{"name": "main", "source": OFFICIAL_REGISTRY_SOURCE}],
            "logs": {"keep_runs": 50, "keep_days": 90},
            # Housekeeping, display and hook policy defaults.
            "cleanup": {"auto": False},
            "ui": {"color": "auto"},
            "hooks": {"allow_vendor_installers": True},
            "install": {"record_manifest": True},
            "adopt": {"shortcut_prefix": ""},
            "check_updates": {"sources": "registry"},
        },
        origin="built-in",
    )


def merge_layers(layers: Sequence[Layer]) -> Settings:
    """Merge layers, lowest priority first, into the effective settings.

    Parameters
    ----------
    layers : sequence of Layer
        The layers to apply in order; the first must be ``default_layer()``.

    Returns
    -------
    Settings
        The effective values, each with the name of the layer that set it.

    Raises
    ------
    SettingsError
        If any layer is invalid, or the defaults are not the first layer.

    Examples
    --------
    Basic usage:

    >>> user = Layer("user", {"schema": SCHEMA, "ui": {"color": "never"}}, "settings.toml")
    >>> settings = merge_layers([default_layer("user"), user])
    >>> settings["ui.color"], settings.source_of("ui.color")
    ('never', 'user')
    >>> settings.source_of("logs.keep_runs")
    'defaults'

    Edge cases:

    >>> merge_layers([user])
    Traceback (most recent call last):
        ...
    gupkg.model.settings.SettingsError: the built-in defaults must be the first settings layer
    """
    # The defaults must come first so every key has a value.
    if not layers or layers[0].name != LAYER_DEFAULTS:
        raise SettingsError("the built-in defaults must be the first settings layer")
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    registries: tuple[RegistryEntry, ...] = ()
    problems: list[str] = []
    # Later layers overwrite earlier ones, and each value remembers the layer that set it.
    for layer in layers:
        try:
            layer_values, layer_registries = validate_layer(layer)
        except SettingsError as exc:
            problems.extend(exc.problems)
            continue
        for key, value in layer_values.items():
            values[key] = value
            sources[key] = layer.name
        # The registry array is replaced as a whole by the highest layer that sets it.
        if layer_registries is not None:
            registries = layer_registries
            sources["registry"] = layer.name
    # Every invalid layer is reported together.
    if problems:
        raise SettingsError(problems)
    return Settings(values, sources, registries)


def expand_env(text: str, env: Mapping[str, str]) -> str:
    """Expand ``%NAME%`` references in a configured path.

    Parameters
    ----------
    text : str
        A path such as ``%USERPROFILE%\\opt``.
    env : mapping of str to str
        The environment to read; names compare without regard to letter case.

    Returns
    -------
    str
        The expanded text.

    Raises
    ------
    SettingsError
        If a referenced variable is not defined.

    Examples
    --------
    Basic usage:

    >>> expand_env("%USERPROFILE%/opt", {"UserProfile": "C:/Users/me"})
    'C:/Users/me/opt'

    Edge cases:

    >>> expand_env("%NOPE%/opt", {})
    Traceback (most recent call last):
        ...
    gupkg.model.settings.SettingsError: the variable %NOPE% in '%NOPE%/opt' is not defined
    """
    folded = {name.casefold(): value for name, value in env.items()}

    def _replace(match: re.Match[str]) -> str:
        """Return the value of one ``%NAME%`` reference; an undefined variable raises ``SettingsError``."""
        value = folded.get(match.group(1).casefold())
        if value is None:
            raise SettingsError(f"the variable %{match.group(1)}% in '{text}' is not defined")
        return value

    # Replace every ``%NAME%`` reference, ignoring letter case in the name.
    return re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", _replace, text)
