"""Provide shared models, log adapters, version handling, expansion, and atomic I/O.

The module contains dependency-light values and utilities used across the gupkg
runtime. Filesystem writes use same-directory temporary files so replacements
remain atomic on the destination volume.

Implementation Approach
-----------------------
Small immutable or result-oriented models carry package identity and outcomes.
Pure parsing and expansion helpers are kept beside the narrowly scoped logging
and atomic-write infrastructure shared by higher-level domains; the ``log_*`` adapters turn legacy messages into events.
"""

from __future__ import annotations


import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from .report.events import Error, Step, Warning, emit
from .model.version import compare_versions, is_version_name
from .system.port import Scope


__copyright__ = "Copyright (C) 2025 Gennady Uraltsev. All rights reserved."
__license__ = "MIT"

# Public process exit statuses shared by every command and workflow result.
EXIT_SUCCESS = 0
EXIT_USER_ERROR = 2
EXIT_MUTATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4

# A version directory is named by its version string, any text that is a safe
# folder name. Text after the first ``.local.`` is the local revision, itself
# any string, so ``1.2.3.local.000002`` is upstream ``1.2.3`` with local ``000002``.
LOCAL_VERSION_MARKER = ".local."

# The one directory name that holds a package's update template. It is not a
# version: promoting it creates the first real version directory.
BOOTSTRAP_DIR_NAME = "_bootstrap"
BOOTSTRAP_VERSION = "bootstrap"

class ConfigValidationError(ValueError):
    """Raised when ``pkg.toml`` content is structurally invalid.

    The exception is used for problems the caller can usually fix by editing the
    package configuration, such as unsupported keys, invalid block shapes, or
    missing required fields.
    """


@dataclass
class StepResult:
    """Outcome of a single mutation step.

    Attributes
    ----------
    ok : bool
        ``True`` when the step succeeded.
    changed : bool
        ``True`` when the step made a filesystem or registry change.
    warnings : list[str]
        Non-fatal issues emitted while performing the step.
    errors : list[str]
        Fatal issues encountered by the step.

    """

    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class ActionResult:
    """Outcome of a high-level CLI action.

    Attributes
    ----------
    ok : bool
        ``True`` when the action completed successfully.
    changed : bool
        ``True`` when the action made one or more changes.
    warnings : list[str]
        Non-fatal warnings accumulated during the action.
    errors : list[str]
        Fatal errors accumulated during the action.
    exit_code : int
        Process exit code that should be returned to the caller.
    status : str or None
        Optional action-specific outcome for callers that need more detail than
        whether a filesystem or registry change occurred.

    """

    ok: bool
    changed: bool = False
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    exit_code: int = EXIT_SUCCESS
    status: str | None = None


@dataclass(frozen=True)
class PackageIdentity:
    """Directory-derived identity for a package version.

    Attributes
    ----------
    name : str
        Package name derived from the package-root directory name.
    version : str
        Upstream version string derived from the version directory.
    local_version : str
        Local revision text after the first ``.local.`` in the directory name,
        or ``""`` when the marker is absent.
    version_string : str
        Original version-directory name, for example ``1.2.3.local.000004``.
    package_root : Path
        Parent directory that contains all versions and ``current``.
    version_path : Path
        Concrete version directory represented by the identity.
    is_current : bool
        ``True`` when the version already matches ``current``.
    only_portable_by_name : bool
        ``True`` when the package naming convention marks it as portable.

    """

    name: str
    version: str
    local_version: str
    version_string: str
    package_root: Path
    version_path: Path
    is_current: bool
    only_portable_by_name: bool

    @classmethod
    def from_version_path(
        cls,
        package_root: Path,
        version_path: Path,
        *,
        is_current: bool,
    ) -> "PackageIdentity":
        """Construct an identity object from one package root and version path.

        Parameters
        ----------
        package_root : Path
            Directory that owns ``current`` and all version directories.
        version_path : Path
            Concrete version directory represented by the identity.
        is_current : bool
            Whether ``current`` already resolves to ``version_path``.

        Returns
        -------
        PackageIdentity
            Identity derived directly from the filesystem layout.

        Raises
        ------
        ValueError
            Raised when ``version_path`` is not a safe, non-reserved folder
            name and is not the ``_bootstrap`` template directory.

        """
        if version_path.name == BOOTSTRAP_DIR_NAME:
            upstream, local_version = BOOTSTRAP_VERSION, ""
        else:
            if not is_version_directory_name(version_path.name):
                raise ValueError(
                    f"Invalid version directory name: {version_path.name}. Use a plain folder "
                    f"name such as 1.2.3 or 1.2.3{LOCAL_VERSION_MARKER}000001; "
                    f"'current' and names starting with '.' are reserved. "
                    f"The template folder is {BOOTSTRAP_DIR_NAME}."
                )
            upstream, local_version = split_package_version(version_path.name)
        return cls(
            name=package_root.name,
            version=upstream,
            local_version=local_version,
            version_string=version_path.name,
            package_root=package_root,
            version_path=version_path,
            is_current=is_current,
            only_portable_by_name=package_root.name.lower().endswith("-portable"),
        )


@dataclass
class ExpansionResult:
    """Result of variable expansion.

    Attributes
    ----------
    value : str
        Expanded text.
    unresolved : list[str]
        Ordered list of unresolved variable tokens left in the output, for
        example ``${MISSING}``.

    """

    value: str
    unresolved: List[str] = field(default_factory=list)


class ExpansionMode(Enum):
    """Ruleset to use when expanding ``$`` variables.

    ``GENERAL`` expands package variables plus braced environment references and
    reports unsupported plain ``$NAME`` tokens as unresolved. ``SCRIPT``
    expands package variables plus braced environment references, but preserves
    plain ``$NAME`` tokens so script languages such as PowerShell keep their
    native variable syntax.
    """

    GENERAL = "general"
    SCRIPT = "script"


# ---------------------------------------------------------------------------
# Transitional log adapters
# ---------------------------------------------------------------------------
# Workflows that predate the event stream still call these; each call becomes
# an event for whichever reporter the front end installed. Nothing is printed
# here: with no reporter installed, the call does nothing.


def log_info(message: str) -> None:
    """Report one informational line as a ``Step`` event (blank lines are layout and are dropped)."""
    if message.strip():
        emit(Step(message))


def log_warning(message: str) -> None:
    """Report one warning as a ``Warning`` event; a leading ``WARNING:`` is not repeated."""
    emit(Warning(message.removeprefix("WARNING:").lstrip()))


def log_error(message: str) -> None:
    """Report one error as an ``Error`` event; a leading ``ERROR:`` is not repeated."""
    emit(Error(message.removeprefix("ERROR:").lstrip()))



def is_version_directory_name(name: str) -> bool:
    """Return whether *name* can name a package version directory.

    Parameters
    ----------
    name : str
        Directory name to validate.

    Returns
    -------
    bool
        ``True`` for a non-empty, path-safe name that is not reserved
        (``current``, ``_check_updates``, ``data``, or a name starting with
        ``.`` or ``_``, which includes ``_bootstrap``).

    """
    return is_version_name(name)


def is_version_directory(path: Path) -> bool:
    """Return whether *path* is a discoverable version folder.

    A version folder has a valid name and contains ``pkg.toml``.
    """
    return is_version_directory_name(path.name) and path.is_dir() and (path / "pkg.toml").is_file()


def is_package_directory(path: Path) -> bool:
    """Return whether *path* is a discoverable version folder or the template."""
    return path.name == BOOTSTRAP_DIR_NAME or is_version_directory(path)


def is_package_directory_name(name: str) -> bool:
    """Return whether *name* is acceptable as a version directory or the template."""
    return name == BOOTSTRAP_DIR_NAME or is_version_directory_name(name)


def split_package_version(version: str) -> Tuple[str, str]:
    """Split a version directory name into upstream and local components.

    Parameters
    ----------
    version : str
        Version text such as ``1.2.3`` or ``1.2.3.local.000004``.

    Returns
    -------
    Tuple[str, str]
        A tuple ``(upstream_version, local_revision)``; the local revision is
        everything after the first ``.local.`` and is ``""`` when absent.

    """
    upstream, _, local = version.strip().partition(LOCAL_VERSION_MARKER)
    return upstream, local


def compare_package_versions(version1: str, version2: str) -> int:
    """Compare two package version directory strings.

    The comparison understands upstream semantic-ish version segments,
    prerelease suffixes, and the package-local revision after ``.local.``.

    Parameters
    ----------
    version1 : str
        Left-hand version string.
    version2 : str
        Right-hand version string.

    Returns
    -------
    int
        ``1`` if *version1* is newer, ``-1`` if *version2* is newer, or ``0``
        if they represent the same version.

    """

    return compare_versions(version1, version2)


def normalize_path(path: Union[str, Path]) -> Path:
    r"""Normalize a filesystem path for reliable comparisons.

    Parameters
    ----------
    path : Union[str, Path]
        Input path as a string or :class:`~pathlib.Path` instance.

    Returns
    -------
    Path
        A resolved path with any Windows extended-length prefix (``\\?\``)
        removed.

    """
    path_str = str(path)
    if path_str.startswith("\\\\?\\"):
        path_str = path_str[4:]
    return Path(path_str).resolve()


def read_toml_file(path: Path) -> Dict[str, Any]:
    """Read a TOML file into plain Python data.

    Parameters
    ----------
    path : Path
        TOML file to load.

    Returns
    -------
    Dict[str, Any]
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
        raise ConfigValidationError(
            f"Configuration must be a TOML table, got: {type(data).__name__}"
        )
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
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(tmp_fd, "wb") as file_handle:
            file_handle.write(content)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


# One ``$`` token: an escaped dollar, a braced name, or a plain identifier.
_EXPANSION_TOKEN_RE = re.compile(r"\$(?:(\$)|\{([^}]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def expand_text(
    text: str,
    identity: PackageIdentity,
    mode: ExpansionMode,
    *,
    install_context: Mapping[str, Any] | None = None,
) -> ExpansionResult:
    """Expand package and environment variables in text.

    Expansion rules:

    - ``$App``, ``$Icons``, ``$Shortcuts``, ``$VersionRoot``, and
      ``${version}`` expand in every mode.
    - ``$ScopeRoot`` and ``$Bin`` expand when an installation context supplies
      ``collection_root`` and ``bin_dir``.
    - ``${VAR}`` expands in every mode and is tracked as unresolved when the
      environment variable does not exist.
    - Plain ``$NAME`` only expands when it names a package variable.
    - Plain non-package ``$NAME`` tokens are reported as unresolved in
      :class:`ExpansionMode.GENERAL` and stay literal in
      :class:`ExpansionMode.SCRIPT`.
    - ``$$`` becomes a literal ``$``; any other ``$`` stays literal.

    Parameters
    ----------
    text : str
        Source text that may contain variables.
    identity : PackageIdentity
        Package identity used to resolve package-variable paths.
    mode : ExpansionMode
        Expansion ruleset to apply.
    install_context : Mapping[str, Any], optional
        Scope paths exposing ``collection_root`` and ``bin_dir`` for
        manager-owned install values.

    Returns
    -------
    ExpansionResult
        The expanded text and any unresolved variable tokens, in first-seen
        order without duplicates.

    """
    if not text:
        return ExpansionResult("")

    # Package variables intentionally resolve through ``<package>/current`` so
    # repair installs keep targeting the active package view.
    base = identity.package_root / "current"
    package_vars = {
        "App": str(base / "App"),
        "Icons": str(base / "Icons"),
        "Shortcuts": str(base / "Shortcuts"),
        "VersionRoot": str(base),
        "version": identity.version,
    }
    if install_context is not None:
        if install_context.get("collection_root") is not None:
            package_vars["ScopeRoot"] = str(install_context["collection_root"])
        if install_context.get("bin_dir") is not None:
            package_vars["Bin"] = str(install_context["bin_dir"])

    unresolved: List[str] = []

    def replace(match: re.Match[str]) -> str:
        """Return the replacement for one token and record unresolved names."""
        escaped, braced, plain = match.groups()
        token = match.group(0)
        if escaped:
            return "$"
        if braced is not None:
            if braced in package_vars:
                return package_vars[braced]
            if braced in os.environ:
                return os.environ[braced]
            unresolved.append(token)
            return token
        if plain in package_vars:
            return package_vars[plain]
        if mode == ExpansionMode.GENERAL:
            unresolved.append(token)
        return token

    value = _EXPANSION_TOKEN_RE.sub(replace, str(text))
    return ExpansionResult(value, list(dict.fromkeys(unresolved)))
