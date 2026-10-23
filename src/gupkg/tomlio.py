"""Read and write gupkg's own TOML files with the one writer, ``tomli_w``.

Usage and API
-------------
``dumps(data)`` returns the TOML text of plain data. ``write_toml(path, data)``
replaces a file atomically with that text, and ``read_toml(path)`` parses a file
with ``tomllib``.

Implementation Approach
-----------------------
Settings, state, install records, receipts, shim configurations and machine output
are all written from plain dictionaries through ``tomli_w``, so quoting and escaping
are never hand-written. Files are replaced atomically beside the destination.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomli_w

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
