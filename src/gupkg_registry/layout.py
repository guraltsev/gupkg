"""The registry layout (design 17) and ``_registry.toml``, read and written in one place.

Usage and API
-------------
``package_dirs(root)`` lists the package folders; ``read_registry_file(root)`` and
``write_registry_file(root, data)`` handle ``_registry.toml``; ``write_text_bytes`` writes a file
with LF line endings (the registry stores every file byte for byte, so fingerprints are stable).

Implementation Approach
-----------------------
``_registry.toml`` holds ``format = 2``, ``name``, ``description``, an optional ``unpinned`` list
(versions that still wait for their first hash, written as ``"name/version"``) and the
``[fingerprints]`` table ``"name/version" = "<sha256>"`` that gupkg reads (design 17). Keys are
written in sorted order so a re-run changes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from gupkg.model.version import TRACKER_DIR_NAME
from gupkg.tomlio import dumps, read_toml

REGISTRY_FILE = "_registry.toml"
REGISTRY_FORMAT = 2
LAUNCHER_NAMES = ("install.cmd", "gupkg-tui.cmd")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class RegistryFileError(ValueError):
    """Report a ``_registry.toml`` that is missing, unreadable or not of the expected shape."""


def package_dirs(root: Path) -> list[Path]:
    """Return the package folders of the registry at *root*, sorted by name (``_`` and ``.`` folders are not packages)."""
    return sorted(
        (p for p in Path(root).iterdir() if p.is_dir() and not p.name.startswith((".", "_"))), key=lambda p: p.name
    )


def tracker_dir(package: Path) -> Path:
    """Return the ``_check_updates`` folder of *package* (it may not exist)."""
    return Path(package) / TRACKER_DIR_NAME


def read_registry_file(root: Path) -> dict[str, Any]:
    """Read ``_registry.toml`` and check its shape.

    Raises
    ------
    RegistryFileError
        If it is missing, is not format 2, or its ``fingerprints`` / ``unpinned`` are malformed.
    """
    path = Path(root) / REGISTRY_FILE
    try:
        data = read_toml(path)
    except FileNotFoundError:
        raise RegistryFileError(f"{REGISTRY_FILE} is missing") from None
    except (OSError, ValueError) as error:
        raise RegistryFileError(f"cannot read {REGISTRY_FILE}: {error}") from error
    if data.get("format") != REGISTRY_FORMAT:
        raise RegistryFileError(f"{REGISTRY_FILE} must say format = {REGISTRY_FORMAT}")
    for key in ("name", "description"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise RegistryFileError(f"{REGISTRY_FILE} needs a text '{key}'")
    fingerprints = data.get("fingerprints", {})
    if not isinstance(fingerprints, dict) or any(
        not isinstance(k, str) or "/" not in k or not isinstance(v, str) or not SHA256_PATTERN.fullmatch(v)
        for k, v in fingerprints.items()
    ):
        raise RegistryFileError("[fingerprints] must map 'name/version' to a lower-case SHA-256")
    unpinned = data.get("unpinned", [])
    if not isinstance(unpinned, list) or any(not isinstance(item, str) or "/" not in item for item in unpinned):
        raise RegistryFileError("'unpinned' must be a list of 'name/version' texts")
    return data


def write_registry_file(root: Path, data: dict[str, Any]) -> None:
    """Write ``_registry.toml`` with sorted fingerprints and ``unpinned``, using LF line endings."""
    clean = {key: value for key, value in data.items() if key not in ("fingerprints", "unpinned")}
    unpinned = sorted(set(data.get("unpinned", [])))
    if unpinned:
        clean["unpinned"] = unpinned
    clean["fingerprints"] = dict(sorted(data.get("fingerprints", {}).items()))
    write_text_bytes(Path(root) / REGISTRY_FILE, dumps(clean))


def write_text_bytes(path: Path, text: str) -> None:
    """Write *text* to *path* as UTF-8 with LF line endings, replacing the file."""
    Path(path).write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
