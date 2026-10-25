"""Read and write gupkg's own TOML files with the one writer, ``tomli_w``.

Usage and API
-------------
``dumps(data)`` returns the TOML text of plain data; ``dumps_tables(data)`` does the same but writes every
list of tables as ``[[name]]`` blocks (package manifests are written that way, as people write them).
``write_toml(path, data)``
replaces a file atomically with that text, and ``read_toml(path)`` parses a file
with ``tomllib``.

Implementation Approach
-----------------------
Settings, state, install records, receipts, shim configurations and machine output
are all written from plain dictionaries through ``tomli_w``, so quoting and escaping
are never hand-written. Files are replaced atomically beside the destination.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import tomli_w
from tomli_w import _writer as _tomli_w_writer

from .core import read_toml_file, write_text_atomic


def dumps(data: dict[str, Any]) -> str:
    """Return the TOML text of *data*.

    Parameters
    ----------
    data : dict
        Plain TOML-ready data (strings, numbers, booleans, lists, dictionaries).

    Returns
    -------
    str
        TOML text; scalars come before tables, as TOML requires.
    """
    return tomli_w.dumps(data)


_TABLES_LOCK = threading.Lock()
_COMPACT_ARRAY_WIDTH = 80


def dumps_tables(data: dict[str, Any]) -> str:
    """Return the TOML text of *data* with every list of tables written as ``[[name]]`` blocks.

    ``tomli_w`` writes a short table in a list as an inline table (``shim = [ { name = "x", ... }, ]``) and a long one as
    ``[[shim]]``, so one manifest mixes both. Package manifests are written by hand as ``[[shim]]``, so this writer makes
    ``tomli_w`` treat no table as short enough to inline and a short list of plain values is written on one line (`modes = ["system", "user"]`).

    Parameters
    ----------
    data : dict
        Plain TOML-ready data.

    Returns
    -------
    str
        TOML text; every list of tables is a sequence of ``[[name]]`` blocks.
    """
    with _TABLES_LOCK:
        original_table = _tomli_w_writer.is_suitable_inline_table
        original_array = _tomli_w_writer.format_inline_array

        def compact_array(array: Any, context: Any, nest_level: int) -> str:
            """Write a short list of plain values on one line; anything else as ``tomli_w`` does."""
            if array and not any(isinstance(item, (dict, list, tuple)) for item in array):
                line = "[" + ", ".join(_tomli_w_writer.format_literal(item, context, nest_level=nest_level + 1) for item in array) + "]"
                if len(line) <= _COMPACT_ARRAY_WIDTH:
                    return line
            return original_array(array, context, nest_level)

        _tomli_w_writer.is_suitable_inline_table = lambda table, context: False
        _tomli_w_writer.format_inline_array = compact_array
        try:
            return tomli_w.dumps(data)
        finally:
            _tomli_w_writer.is_suitable_inline_table = original_table
            _tomli_w_writer.format_inline_array = original_array


def write_toml(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* to *path* as TOML.

    Parameters
    ----------
    path : Path
        Destination file; its folder is created when needed.
    data : dict
        Plain TOML-ready data.
    """
    write_text_atomic(Path(path), dumps(data))


def read_toml(path: Path) -> dict[str, Any]:
    """Parse the TOML file at *path*.

    Parameters
    ----------
    path : Path
        File to read; a UTF-8 byte order mark is accepted.

    Returns
    -------
    dict
        The parsed tree.

    Raises
    ------
    OSError
        If the file cannot be read.
    UnicodeDecodeError
        If the file is not valid UTF-8.
    tomllib.TOMLDecodeError
        If the file is not valid TOML.
    """
    return read_toml_file(Path(path))
