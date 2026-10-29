"""Provide the shared configuration error and the atomic file readers and writers.

The module holds the three small utilities that every other domain of gupkg shares: ``ConfigValidationError`` (a problem in
a configuration or registry that the user can fix), ``read_toml_file`` (a TOML file as plain data, a UTF-8 byte order mark
accepted) and the atomic writers ``write_text_atomic`` and ``write_bytes_atomic``. Filesystem writes use same-directory
temporary files so replacements remain atomic on the destination volume.

Usage and API
-------------
Import the function or exception that is needed; ``gupkg.tomlio`` wraps the readers and writers for TOML files, and the
engine and ``gupkg pkg pin`` call the atomic writers directly.

Implementation Approach
-----------------------
A write is staged in a temporary file beside the destination, flushed to disk and renamed over it, so a failed write never
leaves a truncated file behind and a reader never sees a half-written one.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any

__copyright__ = "Copyright (C) 2025 Gennady Uraltsev. All rights reserved."
__license__ = "MIT"


class ConfigValidationError(ValueError):
    """Raise when a configuration, registry or ``pkg.toml`` is structurally invalid.

    The exception is used for problems the caller can usually fix by editing the configuration, such as unsupported keys,
    invalid block shapes, or missing required fields.
    """


def read_toml_file(path: Path) -> dict[str, Any]:
    """Read a TOML file into plain Python data.

    Parameters
    ----------
    path : Path
        TOML file to load.

    Returns
    -------
    dict
        A dictionary-like tree of plain Python values.

    Raises
    ------
    OSError
        If the file cannot be read.
    tomllib.TOMLDecodeError
        If the file is not valid TOML.
    ConfigValidationError
        If the parsed document is not a top-level table.
    """
    # Decode TOML as text so a UTF-8 BOM from common Windows editors is
    # accepted while ordinary UTF-8 remains unchanged.
    text = path.read_text(encoding="utf-8-sig")
    data = tomllib.loads(text)
    if not isinstance(data, dict):
        raise ConfigValidationError(f"Configuration must be a TOML table, got: {type(data).__name__}")
    return data


def write_text_atomic(path: Path, text: str) -> None:
    """Write UTF-8 text atomically to a file.

    Parameters
    ----------
    path : Path
        Destination file path.
    text : str
        Text content to write without newline translation.

    Raises
    ------
    OSError
        If the temporary file or final replacement cannot be written.
    """
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, content: bytes) -> None:
    """Write bytes atomically to a file.

    Parameters
    ----------
    path : Path
        Destination file path.
    content : bytes
        Raw bytes to write.

    Raises
    ------
    OSError
        If the temporary file or final replacement cannot be written.
    """
    # Stage beside the destination so the final rename stays on one volume and
    # a failed write never leaves a truncated destination behind.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as file_handle:
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
