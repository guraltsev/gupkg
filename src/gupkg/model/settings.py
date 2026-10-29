"""The settings schema (version 4) and the layering of its sources (design section 11).

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
pure: deciding which files exist, which layers a run may read and whether they
sit in admin-only folders is the job of ``gupkg.settingsload``.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..system.port import Mode

SCHEMA = 4

LAYER_DEFAULTS = "defaults"
LAYER_APP = "app"
LAYER_ROOT = "root"
LAYER_CLI = "cli"
LAYER_NAMES = (LAYER_DEFAULTS, LAYER_APP, LAYER_ROOT, LAYER_CLI)

SCHEMA_3_REFUSAL = "settings schema 3 is no longer read; change it to schema = 4 (see _docs/rehaul_design.md §11)"

OFFICIAL_REGISTRY_SOURCE = "https://github.com/guraltsev/gupkg-registry.git"
_REGISTRY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SettingsError(ValueError):
    """Report an invalid settings file or an unusable layer, listing every problem."""

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

    def check(value: Any) -> str:
        if value not in names:
            raise ValueError(f"must be one of {', '.join(names)}")
        return value

    return check


def _boolean(value: Any) -> bool:
    """Accept true or false."""
    if not isinstance(value, bool):
        raise ValueError("must be true or false")
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
}
_TOP_LEVEL = ("schema", "registry", *_SCHEMA)


@dataclass(frozen=True)
class RegistryEntry:
    """One configured registry."""

    name: str
    source: str


@dataclass(frozen=True)
class Layer:
    """One settings source: its name (``LAYER_*``), parsed data and where it came from."""

    name: str
    data: Mapping[str, Any]
    origin: str | None = None


@dataclass(frozen=True)
class SettingValue:
    """One effective setting and the layer that supplied it."""

    key: str
    value: Any
    layer: str


@dataclass(frozen=True)
class Settings:
    """The effective settings with the layer of every value."""

    values: Mapping[str, Any]
    sources: Mapping[str, str]
    registries: tuple[RegistryEntry, ...] = field(default_factory=tuple)

    def __getitem__(self, key: str) -> Any:
        """Return the effective value of a dotted *key* such as ``roots.system``."""
        return self.values[key]

    def source_of(self, key: str) -> str:
        """Return the name of the layer that supplied *key* (``registry`` for the registries)."""
        return self.sources[key]

    def effective(self) -> list[SettingValue]:
        """Return every effective value, in schema order, with its source layer."""
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


def _hint(key: str, allowed: Sequence[str]) -> str:
    """Return a ``did you mean`` hint for the closest allowed key, or the allowed list."""
    close = difflib.get_close_matches(key, list(allowed), n=1, cutoff=0.6)
    if close:
        return f"; did you mean '{close[0]}'?"
    return f"; allowed keys: {', '.join(allowed)}"


def _flatten(tree: Mapping[str, Any], data: Mapping[str, Any], prefix: str, problems: list[str]
             ) -> dict[str, Any]:
    """Validate *data* against *tree* and return its values under dotted keys."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        where = f"{prefix}{key}"
        if key not in tree:
            problems.append(f"unknown key '{where}'{_hint(key, list(tree))}")
            continue
        spec = tree[key]
        if isinstance(spec, dict):
            if not isinstance(value, dict):
                problems.append(f"'{where}' must be a table")
                continue
            flat.update(_flatten(spec, value, f"{where}.", problems))
            continue
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
    entries: list[RegistryEntry] = []
    for index, item in enumerate(value):
        where = f"registry[{index}]"
        for key in item:
            if key not in ("name", "source"):
                problems.append(f"unknown key '{where}.{key}'{_hint(key, ['name', 'source'])}")
        name, source = item.get("name"), item.get("source")
        if not isinstance(name, str) or not _REGISTRY_NAME.fullmatch(name):
            problems.append(f"'{where}.name' must be a name of letters, digits, '.', '_' and '-'")
            continue
        if not isinstance(source, str) or not source.strip():
            problems.append(f"'{where}.source' must be a non-empty string")
            continue
        entries.append(RegistryEntry(name, source))
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
    """
    problems: list[str] = []
    data = layer.data
    if layer.name == LAYER_ROOT and "roots" in data:
        raise SettingsError(
            f"[roots] cannot be set in a root's own settings.toml ({layer.origin}); "
            "set it in $App\\settings.toml or with --root"
        )
    if data.get("schema") == 3:
        problems.append(SCHEMA_3_REFUSAL)
    elif (layer.name != LAYER_DEFAULTS or "schema" in data) and data.get("schema") != SCHEMA:
        shown = data.get("schema", "missing")
        problems.append(f"unsupported settings schema ({shown!r}); this gupkg reads schema {SCHEMA}")
    body = {key: value for key, value in data.items() if key not in ("schema", "registry")}
    unknown_top = [key for key in body if key not in _TOP_LEVEL]
    for key in unknown_top:
        problems.append(f"unknown key '{key}'{_hint(key, [k for k in _TOP_LEVEL if k != 'schema'])}")
    values = _flatten(_SCHEMA, {k: v for k, v in body.items() if k not in unknown_top}, "", problems)
    registries = _registries(data["registry"], problems) if "registry" in data else None
    if problems:
        where = f" ({layer.origin})" if layer.origin else ""
        raise SettingsError([f"{layer.name} settings{where}: {problem}" for problem in problems])
    return values, registries


def default_layer(mode: str) -> Layer:
    """Return the built-in defaults of *mode* (``system`` or ``user``) as the lowest layer.

    The two modes differ only in the ``[integration]`` folders.
    """
    if mode == Mode.SYSTEM:
        integration = {
            "bin": "C:\\bin",
            "start_menu": "%ProgramData%\\Microsoft\\Windows\\Start Menu\\opt",
            "integrate": "full",
        }
    elif mode == Mode.USER:
        integration = {
            "bin": "%USERPROFILE%\\bin",
            "start_menu": "%APPDATA%\\Microsoft\\Windows\\Start Menu\\opt",
            "integrate": "full",
        }
    else:
        raise ValueError(f"unknown mode {mode!r}; use 'system' or 'user'")
    return Layer(
        LAYER_DEFAULTS,
        {
            "schema": SCHEMA,
            "roots": {
                "system": "C:\\opt",
                "user": "%USERPROFILE%\\opt",
            },
            "integration": integration,
            "manager": {"checksum_missing": "ask", "keep_screen": True},
            "registry": [{"name": "main", "source": OFFICIAL_REGISTRY_SOURCE}],
            "logs": {"keep_runs": 50, "keep_days": 90},
            "cleanup": {"auto": False},
            "ui": {"color": "auto"},
            "hooks": {"allow_vendor_installers": True},
            "install": {"record_manifest": True},
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
    """
    if not layers or layers[0].name != LAYER_DEFAULTS:
        raise SettingsError("the built-in defaults must be the first settings layer")
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    registries: tuple[RegistryEntry, ...] = ()
    problems: list[str] = []
    for layer in layers:
        try:
            layer_values, layer_registries = validate_layer(layer)
        except SettingsError as exc:
            problems.extend(exc.problems)
            continue
        for key, value in layer_values.items():
            values[key] = value
            sources[key] = layer.name
        if layer_registries is not None:
            registries = layer_registries
            sources["registry"] = layer.name
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
    """
    folded = {name.casefold(): value for name, value in env.items()}

    def replace(match: re.Match[str]) -> str:
        value = folded.get(match.group(1).casefold())
        if value is None:
            raise SettingsError(f"the variable %{match.group(1)}% in '{text}' is not defined")
        return value

    return re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", replace, text)
