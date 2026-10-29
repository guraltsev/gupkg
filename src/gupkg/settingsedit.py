"""Create and edit one settings file (``gupkg settings init|set`` and ``gupkg registry add|remove``).

The file of one layer is read as data, changed, validated against the schema together with the built-in defaults and only
then written back, so a command can never leave a file that the next run refuses. Comments of an existing file are not
kept (the file is rewritten from its data): ``init`` writes a commented starting point, and ``set`` tells the user that
comments are gone. Nothing is changed when the new data is not valid.

Usage and API
-------------
``init_settings(path, layer, force=...)`` writes a new file; ``set_setting(path, layer, key, text)`` sets one dotted key
from the text the user typed (a TOML value such as ``true``, ``90`` or ``"auto"``; anything that is not a TOML value is
taken as a string); ``add_registry`` and ``remove_registry`` edit the ``[[registry]]`` array. Each returns the data that was
written. ``SettingsEditError`` carries the message.

Implementation Approach
-----------------------
The text of a value is parsed as TOML first, so ``true`` is a boolean and ``90`` a number, and a bare word falls back to a
string. The merged result (defaults plus this one layer) goes through the schema validator of ``gupkg.model.settings``; a
problem is reported with the schema's own message, and the write is atomic.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .model.settings import SCHEMA, Layer, RegistryEntry, SettingsError, validate_layer
from .tomlio import read_toml, write_toml


class SettingsEditError(ValueError):
    """Report a settings change that was refused, and why."""


def _read(path: Path) -> dict[str, Any]:
    """Return the data of the settings file at *path*; a missing file is empty, an invalid one is refused."""
    if not path.is_file():
        return {}
    try:
        return read_toml(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SettingsEditError(f"cannot read {path}: {error}") from error


def _validate(layer: str, data: dict[str, Any], path: Path) -> None:
    """Refuse *data* when the schema would not accept it as the settings file of *layer*."""
    try:
        validate_layer(Layer(layer, data, str(path)))
    except SettingsError as error:
        raise SettingsEditError(str(error)) from error


def init_settings(path: Path, layer: str, *, force: bool = False) -> dict[str, Any]:
    """Write a new settings file holding only ``schema = 4``, and return its data.

    Parameters
    ----------
    path : Path
        The settings file of the layer.
    layer : str
        The layer's name, for messages.
    force : bool, default=False
        Replace an existing file.

    Raises
    ------
    SettingsEditError
        If the file exists and *force* is not set.
    """
    if path.exists() and not force:
        raise SettingsEditError(f"{path} exists; use --force to replace it")
    data: dict[str, Any] = {"schema": SCHEMA}
    write_toml(path, data)
    return data


def parse_value(text: str) -> Any:
    """Return the value that *text* spells: a TOML value when it is one, otherwise the text as a string."""
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        return text


def set_setting(path: Path, layer: str, key: str, text: str) -> dict[str, Any]:
    """Set the dotted *key* of the file at *path* to the value *text* spells, and return the data written.

    Raises
    ------
    SettingsEditError
        If the key is unknown or the value is not valid for it (the schema's message), or the file cannot be read.
    """
    if key in ("schema", "registry"):
        raise SettingsEditError(f"'{key}' cannot be set here; use `gupkg registry add|remove` for the registries")
    data = _read(path)
    data.setdefault("schema", SCHEMA)

    # Walk to the table that holds the key, creating tables on the way; a path through a non-table is refused.
    parts = key.split(".")
    table = data
    for part in parts[:-1]:
        child = table.setdefault(part, {})
        if not isinstance(child, dict):
            raise SettingsEditError(f"'{key}': '{part}' is not a table in {path}")
        table = child
    table[parts[-1]] = parse_value(text)
    _validate(layer, data, path)
    write_toml(path, data)
    return data


def add_registry(path: Path, layer: str, name: str, source: str, inherited: tuple[RegistryEntry, ...]) -> dict[str, Any]:
    """Add the registry *name* with *source* to the file at *path*, and return the data written.

    The ``[[registry]]`` array of a layer replaces the one below it as a whole, so a file that has none starts from the
    registries *inherited* from the layers below.
    """
    data = _read(path)
    data.setdefault("schema", SCHEMA)
    entries = list(data.get("registry", [{"name": item.name, "source": item.source} for item in inherited]))
    if any(str(item.get("name", "")).casefold() == name.casefold() for item in entries):
        raise SettingsEditError(f"registry '{name}' is already configured")
    entries.append({"name": name, "source": source})
    data["registry"] = entries
    _validate(layer, data, path)
    write_toml(path, data)
    return data


def remove_registry(path: Path, layer: str, name: str, inherited: tuple[RegistryEntry, ...]) -> dict[str, Any]:
    """Remove the registry *name* from the file at *path*, and return the data written.

    Raises
    ------
    SettingsEditError
        If no registry of that name is configured.
    """
    data = _read(path)
    data.setdefault("schema", SCHEMA)
    entries = list(data.get("registry", [{"name": item.name, "source": item.source} for item in inherited]))
    kept = [item for item in entries if str(item.get("name", "")).casefold() != name.casefold()]
    if len(kept) == len(entries):
        raise SettingsEditError(f"registry '{name}' is not configured")
    data["registry"] = kept
    _validate(layer, data, path)
    write_toml(path, data)
    return data
