"""Create and edit one settings file (``gupkg settings init|set`` and ``gupkg registry add|remove``).

The file of one layer is loaded as data, changed, validated against the schema together with the built-in defaults and only
then written back, so a command can never leave a file that the next run refuses. The change is made in the file's own
text: comments, blank lines, the order of the entries and the line endings stay as they are. Nothing is changed when the new
data is not valid, or when the text cannot be edited so that it parses to exactly the validated data.

Usage and API
-------------
``init_settings(path, layer, mode, force=...)`` writes a new file; ``set_setting(path, layer, key, text)`` sets one dotted key
from the text the user typed (a TOML value such as ``true``, ``90`` or ``"auto"``; anything that is not a TOML value is
taken as a string); ``add_registry`` and ``remove_registry`` edit the ``[[registry]]`` array. Each returns the data that was
written. ``SettingsEditError`` carries the message.

Implementation Approach
-----------------------
The new file is generated from the settings schema (``documented_defaults``): every setting appears as a commented entry
with its explanation and its default, so the template cannot lag behind the schema. The text of a value is parsed as TOML
first, so ``true`` is a boolean and ``90`` a number, and a bare word falls back to a string. The merged result (defaults plus
this one layer) goes through the schema validator of ``gupkg.model.settings``. An edit replaces the entry's line (keeping its
trailing comment), or switches on the commented template entry of that setting, or adds a line to the table, or adds the
table; the edited text is parsed again and must equal the validated data before the atomic write.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from .core import write_text_atomic
from .model.settings import SCHEMA, Layer, RegistryEntry, SettingsError, documented_defaults, validate_layer
from .tomlio import dumps, dumps_tables, read_toml


class SettingsEditError(ValueError):
    """Report a settings change that was refused, and why."""


def init_settings(path: Path, layer: str, mode: str, *, force: bool = False) -> dict[str, Any]:
    """Write a new settings file holding ``schema = 4`` and every setting as a commented entry, and return its data.

    Parameters
    ----------
    path : Path
        The settings file of the layer.
    layer : str
        The layer's name, for messages.
    mode : str
        ``system`` or ``user``; the defaults shown in the file are those of this mode.
    force : bool, default=False
        Replace an existing file.

    Returns
    -------
    dict
        The data of the new file, which holds only ``schema``.

    Raises
    ------
    SettingsEditError
        If the file exists and *force* is not set.
    """
    if path.exists() and not force:
        raise SettingsEditError(f"{path} exists; use --force to replace it")
    data: dict[str, Any] = {"schema": SCHEMA}
    write_text_atomic(path, _template(mode))
    return data


def parse_value(text: str) -> Any:
    """Return the value that *text* spells: a TOML value when it is one, otherwise the text as a string.

    Parameters
    ----------
    text : str
        The value as the user typed it, such as ``true``, ``90`` or ``"auto"``.

    Returns
    -------
    Any
        The parsed TOML value, or *text* itself when it is not valid TOML.

    Examples
    --------
    Basic usage:

    >>> parse_value("true")
    True
    >>> parse_value("90")
    90
    >>> parse_value('"auto"')
    'auto'

    Edge cases:

    >>> parse_value("auto")
    'auto'
    >>> parse_value("[1, 2]")
    [1, 2]
    """
    # Let the TOML parser decide the type; a bare word is not TOML, so it stays a string.
    try:
        return tomllib.loads(f"value = {text}")["value"]
    except tomllib.TOMLDecodeError:
        return text


def set_setting(path: Path, layer: str, key: str, text_value: str) -> dict[str, Any]:
    """Set the dotted *key* of the file at *path* to the value *text_value* spells, and return the data written.

    The file's comments and layout are kept.

    Parameters
    ----------
    path : Path
        The settings file of the layer.
    layer : str
        The layer's name, for messages.
    key : str
        The dotted setting name, such as ``ui.color``.
    text_value : str
        The value as the user typed it; see ``parse_value``.

    Returns
    -------
    dict
        The data written to the file.

    Raises
    ------
    SettingsEditError
        If the key is unknown or the value is not valid for it (the schema's message), or the file cannot be read or edited.
    """
    if key in ("schema", "registry"):
        raise SettingsEditError(f"'{key}' cannot be set here; use `gupkg registry add|remove` for the registries")
    # Load the file and make sure it names its schema before any key is added.
    text = _read_text(path)
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
    # Store the value, check the schema, then change the file's own text and write it if it parses to exactly that data.
    value = parse_value(text_value)
    table[parts[-1]] = value
    _validate(layer, data, path)
    _write_edited(path, data, _set_key(_with_schema(text), ".".join(parts[:-1]), parts[-1], value))
    return data


def add_registry(path: Path, layer: str, name: str, source: str, inherited: tuple[RegistryEntry, ...]) -> dict[str, Any]:
    """Add the registry *name* with *source* to the file at *path*, and return the data written.

    The ``[[registry]]`` array of a layer replaces the one below it as a whole, so a file that has none starts from the
    registries *inherited* from the layers below.

    Parameters
    ----------
    path : Path
        The settings file of the layer.
    layer : str
        The layer's name, for messages.
    name : str
        The new registry's name; compared ignoring case.
    source : str
        The registry's source URL or path.
    inherited : tuple of RegistryEntry
        The registries of the layers below, used when the file has no ``[[registry]]`` array.

    Returns
    -------
    dict
        The data written to the file.

    Raises
    ------
    SettingsEditError
        If a registry of that name is already configured, or the result is not valid.
    """
    # Start from the file's own array, or from the inherited registries when it has none.
    text = _read_text(path)
    data = _read(path)
    data.setdefault("schema", SCHEMA)
    had_array = "registry" in data
    entries = list(data.get("registry", [{"name": item.name, "source": item.source} for item in inherited]))
    if any(str(item.get("name", "")).casefold() == name.casefold() for item in entries):
        raise SettingsEditError(f"registry '{name}' is already configured")
    # Append the entry and write only if the schema accepts the result; a file without an array gets all the entries.
    new = {"name": name, "source": source}
    entries.append(new)
    data["registry"] = entries
    _validate(layer, data, path)
    _write_edited(path, data, _append_registries(_with_schema(text), [new] if had_array else entries))
    return data


def remove_registry(path: Path, layer: str, name: str, inherited: tuple[RegistryEntry, ...]) -> dict[str, Any]:
    """Remove the registry *name* from the file at *path*, and return the data written.

    Parameters
    ----------
    path : Path
        The settings file of the layer.
    layer : str
        The layer's name, for messages.
    name : str
        The registry's name; compared ignoring case.
    inherited : tuple of RegistryEntry
        The registries of the layers below, used when the file has no ``[[registry]]`` array.

    Returns
    -------
    dict
        The data written to the file.

    Raises
    ------
    SettingsEditError
        If no registry of that name is configured.
    """
    # Start from the file's own array, or from the inherited registries when it has none.
    text = _read_text(path)
    data = _read(path)
    data.setdefault("schema", SCHEMA)
    had_array = "registry" in data
    entries = list(data.get("registry", [{"name": item.name, "source": item.source} for item in inherited]))
    # Drop every entry of that name; nothing dropped means the name was never configured.
    kept = [item for item in entries if str(item.get("name", "")).casefold() != name.casefold()]
    if len(kept) == len(entries):
        raise SettingsEditError(f"registry '{name}' is not configured")
    data["registry"] = kept
    _validate(layer, data, path)
    # Delete the blocks of the file; an array that ends up empty (or was inherited) is written out, so it still wins.
    edited = _drop_registries(_with_schema(text), name) if had_array else _with_schema(text)
    if not had_array or not kept:
        edited = _append_registries(edited, [] if had_array else kept)
    _write_edited(path, data, edited)
    return data


# --- supporting helpers ---


def _read(path: Path) -> dict[str, Any]:
    """Return the data of the settings file at *path*; a missing file is empty, an invalid one is refused."""
    if not path.is_file():
        return {}
    try:
        return read_toml(path)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise SettingsEditError(f"cannot read {path}: {error}") from error


def _read_text(path: Path) -> str:
    """Return the text of the settings file at *path* without a byte order mark; a missing file is empty."""
    if not path.is_file():
        return ""
    try:
        return path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise SettingsEditError(f"cannot read {path}: {error}") from error


def _validate(layer: str, data: dict[str, Any], path: Path) -> None:
    """Refuse *data* when the schema would not accept it as the settings file of *layer*."""
    try:
        validate_layer(Layer(layer, data, str(path)))
    except SettingsError as error:
        raise SettingsEditError(str(error)) from error


def _write_edited(path: Path, expected: dict[str, Any], text: str) -> None:
    """Write *text* to *path* atomically, but only if it parses to exactly *expected* (the data the schema accepted)."""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        parsed = None
    if parsed != expected:
        raise SettingsEditError(f"cannot edit {path} in place (unusual layout); change it by hand")
    write_text_atomic(path, text)


def _eol(text: str) -> str:
    """Return the line ending that *text* uses (``\\n`` when it has none)."""
    return "\r\n" if "\r\n" in text else "\n"


def _with_schema(text: str) -> str:
    """Return *text* with a ``schema`` line at the top when it has none."""
    try:
        if "schema" in tomllib.loads(text):
            return text
    except tomllib.TOMLDecodeError:
        return text
    return f"schema = {SCHEMA}{_eol(text)}{text}"


# A table header, a commented table header of a template, and any line that starts a table or an array of tables.
_HEADER = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_COMMENTED_HEADER = re.compile(r"^\s*#\s*\[\s*([^\[\]]+?)\s*\]\s*$")
_ANY_HEADER = re.compile(r"^\s*\[")
_REGISTRY_HEADER = re.compile(r"^\s*\[\[\s*registry\s*\]\]")
_EMPTY_ARRAY = re.compile(r"^\s*registry\s*=\s*\[\s*\]\s*(?:#.*)?$")


def _is_header(line: str) -> bool:
    """Tell whether *line* starts a table or an array of tables, or is a commented table header of a template."""
    return bool(_ANY_HEADER.match(line) or _COMMENTED_HEADER.match(line))


def _end_of_content(lines: list[str], start: int, end: int) -> int:
    """Return the index after the last line of ``lines[start:end]`` that is neither blank nor a comment (at least *start*)."""
    while end > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return end


def _trailing_comment(rest: str) -> str:
    """Return the comment (with the spaces before it) that follows the value in *rest*, or an empty string."""
    for index, char in enumerate(rest):
        if char == "#":
            try:
                tomllib.loads("v = " + rest[:index])
            except tomllib.TOMLDecodeError:
                continue
            return rest[len(rest[:index].rstrip()):]
    return ""


def _set_key(text: str, table: str, key: str, value: Any) -> str:
    """Return *text* with ``key = value`` set in *table* (dotted name), changing as little of the text as possible."""
    eol = _eol(text)
    lines = text.splitlines()
    entry = dumps({key: value}).strip()

    # Find the table: an active header first, then the commented header of a template.
    start, switch_on = None, False
    for index, line in enumerate(lines):
        found = _HEADER.match(line)
        if found and found.group(1) == table:
            start = index
            break
    if start is None:
        for index, line in enumerate(lines):
            found = _COMMENTED_HEADER.match(line)
            if found and found.group(1) == table:
                start, switch_on = index, True
                break

    # No such table: add it at the end.
    if start is None:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ([""] if lines else []) + [f"[{table}]", entry]
        return eol.join(lines) + eol

    # The table runs to the next header. An active entry is replaced, then a commented template entry is switched on.
    end = next((index for index in range(start + 1, len(lines)) if _is_header(lines[index])), len(lines))
    active = re.compile(rf"^(\s*){re.escape(key)}\s*=(.*)$")
    commented = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
    for index in range(start + 1, end):
        found = active.match(lines[index])
        if found:
            lines[index] = found.group(1) + entry + _trailing_comment(found.group(2))
            break
    else:
        for index in range(start + 1, end):
            if commented.match(lines[index]):
                lines[index] = entry
                break
        else:
            # A new entry goes after the last line of the table that is not blank or a comment.
            lines.insert(_end_of_content(lines, start + 1, end), entry)
    if switch_on:
        lines[start] = f"[{table}]"
    return eol.join(lines) + eol


def _append_registries(text: str, entries: list[dict[str, Any]]) -> str:
    """Return *text* with a ``[[registry]]`` block per entry at the end; no entries means ``registry = []`` above the tables."""
    eol = _eol(text)
    lines = text.splitlines()
    if not entries:
        first = next((index for index, line in enumerate(lines) if _ANY_HEADER.match(line)), len(lines))
        lines.insert(_end_of_content(lines, 0, first) if first == len(lines) else first, "registry = []")
        return eol.join(lines) + eol
    # An empty ``registry = []`` written by a removal gives way to the blocks.
    lines = [line for line in lines if not _EMPTY_ARRAY.match(line)]
    while lines and not lines[-1].strip():
        lines.pop()
    for entry in entries:
        lines += ([""] if lines else []) + dumps_tables({"registry": [entry]}).strip().splitlines()
    return eol.join(lines) + eol


def _drop_registries(text: str, name: str) -> str:
    """Return *text* without the ``[[registry]]`` blocks named *name* (ignoring case)."""
    eol = _eol(text)
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if _REGISTRY_HEADER.match(lines[index]):
            end = next((j for j in range(index + 1, len(lines)) if _is_header(lines[j])), len(lines))
            stop = _end_of_content(lines, index + 1, end)
            try:
                block = tomllib.loads("\n".join(lines[index:stop])).get("registry", [{}])[0]
            except tomllib.TOMLDecodeError:
                block = {}
            if str(block.get("name", "")).casefold() == name.casefold():
                del lines[index:stop]
                continue
        index += 1
    return eol.join(lines) + eol


def _template(mode: str) -> str:
    """Return the text of a new settings file: ``schema`` and every setting as a commented entry with its default.

    The text is generated from the schema (``documented_defaults``). Every ``# key = value`` line is valid once the ``# `` is
    removed; the file as written holds only ``schema``.
    """
    out = [
        "# gupkg settings for this root. Everything below is commented out and shows the built-in default:",
        '# remove the leading "# " of a line to change it. `gupkg settings show` lists the effective values.',
        "# `gupkg settings set KEY VALUE` changes a value and keeps these comments.",
        f"schema = {SCHEMA}",
    ]
    table = None
    for name, key, default, text in documented_defaults(mode):
        if name != table:
            out += ["", f"# [{name}]"]
            table = name
        out += [f"# {line}" for line in text.splitlines()]
        out.append(f"# {dumps({key: default}).strip()}")
    return "\n".join(out) + "\n"
