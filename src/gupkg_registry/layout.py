"""Describe the registry layout (design 17) and load and store ``_registry.toml`` in one place.

Usage and API
-------------
``package_dirs(root)`` lists the package folders; ``read_registry_file(root)`` and
``write_registry_file(root, data)`` handle ``_registry.toml``; ``write_text_bytes`` writes a file
with LF line endings (the registry stores every file byte for byte, so fingerprints are stable).

Implementation Approach
-----------------------
``_registry.toml`` holds ``format = 2``, ``name``, ``description``, an optional ``unpinned`` list
(versions that still wait for their first hash, written as ``"name/version"``), an optional
``unversioned`` list (packages whose download address does not contain the version, so an older
pinned version stops verifying once the vendor replaces the file) and the
``[fingerprints]`` table ``"name/version" = "<sha256>"`` that gupkg reads (design 17). Keys are
written in sorted order so a re-run changes nothing.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from gupkg.model.version import TRACKER_DIR_NAME
from gupkg.tomlio import dumps, read_toml

# Name of the registry description file.
REGISTRY_FILE = "_registry.toml"
# The only ``format`` value this tool reads and writes.
REGISTRY_FORMAT = 2
# Launcher file names of version and tracker folders.
LAUNCHER_NAMES = ("install.cmd", "manage.cmd")  # the launchers of a version directory
RETIRED_LAUNCHER_NAMES = ("gupkg-tui.cmd",)  # the former name of manage.cmd (issue 037): the registry tool removes it, validate refuses it
TRACKER_LAUNCHER_NAME = "check-updates.cmd"  # the one launcher of a tracker folder
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RegistryFileError(ValueError):
    """Report a ``_registry.toml`` that is missing, unreadable or not of the expected shape."""


def package_dirs(root: Path) -> list[Path]:
    """Return the package folders of the registry at *root*, sorted by name (``_`` and ``.`` folders are not packages).

    Parameters
    ----------
    root : Path
        Registry checkout.

    Returns
    -------
    list of Path
        Package folders in name order.
    """
    return sorted(
        (p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith((".", "_"))), key=lambda p: p.name
    )


def tracker_dir(package: Path) -> Path:
    """Return the ``_check_updates`` folder of *package* (it may not exist).

    Parameters
    ----------
    package : Path
        Package folder.

    Returns
    -------
    Path
        The tracker folder inside it.
    """
    return Path(package) / TRACKER_DIR_NAME


def read_registry_file(root: Path) -> dict[str, Any]:
    """Read ``_registry.toml`` and check its shape.

    Parameters
    ----------
    root : Path
        Registry checkout.

    Returns
    -------
    dict
        The parsed file, validated.

    Raises
    ------
    RegistryFileError
        If it is missing, is not format 2, or its ``fingerprints`` / ``unpinned`` are malformed.
    """
    # Parse the file, turning every way it can fail to load into one error type.
    path = Path(root) / REGISTRY_FILE
    try:
        data = read_toml(path)
    except FileNotFoundError:
        raise RegistryFileError(f"{REGISTRY_FILE} is missing") from None
    except (OSError, ValueError) as error:
        raise RegistryFileError(f"cannot read {REGISTRY_FILE}: {error}") from error
    # Check the format and the required text keys.
    if data.get("format") != REGISTRY_FORMAT:
        raise RegistryFileError(f"{REGISTRY_FILE} must say format = {REGISTRY_FORMAT}")
    # The registry needs a name and a description to show to users.
    for key in ("name", "description"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise RegistryFileError(f"{REGISTRY_FILE} needs a text '{key}'")
    # Check the shape of each optional table or list before callers rely on it.
    fingerprints = data.get("fingerprints", {})
    if not isinstance(fingerprints, dict) or any(
        not isinstance(k, str) or "/" not in k or not isinstance(v, str) or not SHA256_PATTERN.fullmatch(v)
        for k, v in fingerprints.items()
    ):
        raise RegistryFileError("[fingerprints] must map 'name/version' to a lower-case SHA-256")
    # ``unpinned`` lists versions that still wait for their first hash.
    unpinned = data.get("unpinned", [])
    if not isinstance(unpinned, list) or any(not isinstance(item, str) or "/" not in item for item in unpinned):
        raise RegistryFileError("'unpinned' must be a list of 'name/version' texts")
    # ``unversioned`` lists packages whose download address has no version in it.
    unversioned = data.get("unversioned", [])
    if not isinstance(unversioned, list) or any(not isinstance(item, str) or not item or "/" in item for item in unversioned):
        raise RegistryFileError("'unversioned' must be a list of package names")
    return data


def write_registry_file(root: Path, data: dict[str, Any]) -> None:
    """Write ``_registry.toml`` with sorted fingerprints and ``unpinned``, using LF line endings.

    Parameters
    ----------
    root : Path
        Registry checkout.
    data : dict
        Contents to store; ``fingerprints``, ``unpinned`` and ``unversioned`` are sorted.
    """
    # Rebuild the file contents with the sorted collections last, so a re-run changes nothing.
    clean = {key: value for key, value in data.items() if key not in ("fingerprints", "unpinned", "unversioned")}
    unpinned = sorted(set(data.get("unpinned", [])))
    if unpinned:
        clean["unpinned"] = unpinned
    unversioned = sorted(set(data.get("unversioned", [])))
    if unversioned:
        clean["unversioned"] = unversioned
    clean["fingerprints"] = dict(sorted(data.get("fingerprints", {}).items()))
    write_text_bytes(Path(root) / REGISTRY_FILE, dumps(clean))


def write_text_bytes(path: Path, text: str) -> None:
    """Write *text* to *path* as UTF-8 with LF line endings, replacing the file atomically (temporary file, then replace).

    Parameters
    ----------
    path : Path
        File to write.
    text : str
        Contents; ``CRLF`` becomes ``LF``.

    Raises
    ------
    BaseException
        Whatever writing or replacing raises, after the temporary file is removed.
    """
    # Write beside the target and replace it, so a reader never sees half a file.
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        temporary.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
