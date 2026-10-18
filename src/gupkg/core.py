"""Provide shared models, logging, version handling, expansion, and atomic I/O.

The module contains dependency-light values and utilities used across the gupkg
runtime. Filesystem writes use same-directory temporary files so replacements
remain atomic on the destination volume.

Implementation Approach
-----------------------
Small immutable or result-oriented models carry package identity and outcomes.
Pure parsing and expansion helpers are kept beside the narrowly scoped logging
and atomic-write infrastructure shared by higher-level domains.
"""

from __future__ import annotations


import logging
import os
import re
import shutil
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union


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

# Names that can never be version directories: the activation junction (and
# its ``current.__new__``/``current.__old__`` swap entries) and hidden folders.
_RESERVED_DIR_PREFIXES = ("current", ".")
_UNSAFE_DIR_CHARACTERS = re.compile(r'[<>:"/\\|?*]|[\x00-\x1f]')

# The one directory name that holds a package's update template. It is not a
# version: promoting it creates the first real version directory.
BOOTSTRAP_DIR_NAME = "pkg.bootstrap"
BOOTSTRAP_VERSION = "bootstrap"

class ConfigValidationError(ValueError):
    """Raised when ``pkg.toml`` content is structurally invalid.

    The exception is used for problems the caller can usually fix by editing the
    package configuration, such as unsupported keys, invalid block shapes, or
    missing required fields.
    """


class Scope(Enum):
    """Installation scope supported by ``gupkg``.

    ``AUTO`` selects machine scope for administrators when the package permits
    it and user scope otherwise. ``USER`` targets per-user configuration
    locations. ``MACHINE`` targets machine-wide configuration locations and
    therefore usually requires Administrator privileges.
    """

    AUTO = "auto"
    USER = "user"
    MACHINE = "system"


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
            name and is not the ``pkg.bootstrap`` template directory.

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


# ANSI foreground codes for the few roles that carry meaning in console output.
_ANSI_ROLES = {"error": "31", "warning": "33", "success": "32", "dim": "2"}
_vt_enabled: dict[int, bool] = {}


def supports_color(stream: Any) -> bool:
    """Return whether *stream* is a console that should receive ANSI colors.

    Colors are off for redirected output, ``NO_COLOR``, and ``TERM=dumb``. On
    Windows the console's virtual-terminal mode is switched on first, because a
    legacy console would otherwise print the escape codes as visible text.
    """
    isatty = getattr(stream, "isatty", None)
    if not (isatty and isatty()) or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    if os.name != "nt":
        return True
    try:
        key = stream.fileno()
    except (OSError, ValueError, AttributeError):
        return False
    if key not in _vt_enabled:
        _vt_enabled[key] = _enable_virtual_terminal(key)
    return _vt_enabled[key]


def _enable_virtual_terminal(fileno: int) -> bool:
    """Turn on ANSI processing for a Windows console handle; report success."""
    import ctypes
    import msvcrt

    try:
        kernel32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(fileno)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (OSError, AttributeError):
        return False


def colorize(text: str, role: str, stream: Any = None) -> str:
    """Wrap *text* in the ANSI color for *role* when *stream* (default stdout) supports it.

    Roles are ``error``, ``warning``, ``success``, and ``dim``; the text is
    returned unchanged for redirected output.
    """
    if not supports_color(stream if stream is not None else sys.stdout):
        return text
    return f"\x1b[{_ANSI_ROLES[role]}m{text}\x1b[0m"


class _DynamicStdoutHandler(logging.Handler):
    """Logging handler that writes to the current stdout stream.

    ``contextlib.redirect_stdout()`` swaps out ``sys.stdout`` at runtime. The
    handler writes through :func:`print` during ``emit()`` so test capture keeps
    working even though the logger is configured once at import time.
    """

    def emit(self, record: logging.LogRecord) -> None:
        """Render and flush one log record to stdout, colored by severity on a console."""
        try:
            role = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else None
            text = self.format(record)
            print(colorize(text, role) if role else text, flush=True)
        except Exception:
            self.handleError(record)


_LOGGER = logging.getLogger("gupkg.stdout")
for _existing_handler in list(_LOGGER.handlers):
    _LOGGER.removeHandler(_existing_handler)
    try:
        _existing_handler.close()
    except Exception:
        pass
_LOGGER.setLevel(logging.INFO)
_LOGGER.propagate = False
_stdout_handler = _DynamicStdoutHandler()
_stdout_handler.setFormatter(logging.Formatter("%(message)s"))
_LOGGER.addHandler(_stdout_handler)


def log_info(message: str) -> None:
    """Emit one informational line to stdout."""
    _LOGGER.info(message)


def log_progress(message: str, *, final: bool = False) -> None:
    """Show one progress line that later progress replaces.

    On a live stream (an interactive console, or a stream that declares
    ``accepts_transient``), intermediate messages overwrite one line and the
    final message is committed with a newline. On any other stream (a
    redirected file, a pipe, test capture), intermediate messages are dropped
    and only the final message is written, as an ordinary line.
    """
    global _progress_width

    # The stream is looked up per call so ``redirect_stdout`` keeps working.
    stream = sys.stdout
    isatty = getattr(stream, "isatty", None)
    live = bool(getattr(stream, "accepts_transient", False)) or bool(isatty and isatty())
    if not live:
        if final:
            log_info(message)
        return

    # A wrapped line cannot be overwritten with a carriage return, so
    # intermediate text is cut to fit. Spaces (not ANSI erase codes) clear the
    # remainder of a longer previous line on legacy Windows consoles.
    if not final:
        message = message[: max(shutil.get_terminal_size().columns - 1, 1)]
    padded = message.ljust(_progress_width)
    stream.write(padded + ("\n" if final else "\r"))
    stream.flush()
    _progress_width = 0 if final else len(message)


# Width of the last transient progress line, so a shorter one can blank it.
_progress_width = 0


def log_warning(message: str) -> None:
    """Emit one warning line to stdout with a stable prefix."""
    if not message.startswith("WARNING:"):
        message = f"WARNING: {message}"
    _LOGGER.warning(message)


def log_error(message: str) -> None:
    """Emit one error line to stdout with a stable prefix."""
    if not message.startswith("ERROR:"):
        message = f"ERROR: {message}"
    _LOGGER.error(message)


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
        (``current``, ``pkg.bootstrap``, or a hidden name).

    """
    if not name or name != name.strip() or name.endswith("."):
        return False
    if name == BOOTSTRAP_DIR_NAME or _UNSAFE_DIR_CHARACTERS.search(name):
        return False
    return not name.lower().startswith(_RESERVED_DIR_PREFIXES)


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

    def parse_identifier(token: str) -> Union[int, str]:
        """Parse one dotted version token into a comparable value.

        Parameters
        ----------
        token : str
            One version token, for example ``42`` or ``beta``.

        Returns
        -------
        Union[int, str]
            An ``int`` when the token is numeric; otherwise a lower-cased
            string.

        """
        token = token.strip()
        if token.isdigit():
            return int(token)
        return token.lower()

    def parse_upstream(
        upstream: str,
    ) -> Tuple[List[Union[int, str]], Optional[List[Union[int, str]]]]:
        """Parse the upstream portion of a version string.

        Parameters
        ----------
        upstream : str
            Upstream version without the ``.local.`` revision.

        Returns
        -------
        Tuple[List[Union[int, str]], Optional[List[Union[int, str]]]]
            A tuple ``(main_identifiers, prerelease_identifiers)`` where the
            prerelease part is ``None`` for stable releases.

        """
        upstream = upstream.strip()
        if "+" in upstream:
            upstream = upstream.split("+", 1)[0]
        if "-" in upstream:
            main_part, prerelease_part = upstream.split("-", 1)
            prerelease = [
                parse_identifier(part)
                for part in prerelease_part.split(".")
                if part != ""
            ]
        else:
            main_part = upstream
            prerelease = None
        main = [parse_identifier(part) for part in main_part.split(".") if part != ""]
        return main, prerelease

    def compare_identifier_lists(
        left: List[Union[int, str]],
        right: List[Union[int, str]],
        *,
        pad_numeric: bool,
    ) -> int:
        """Compare parsed identifier lists.

        Parameters
        ----------
        left : List[Union[int, str]]
            Parsed identifiers for the left-hand version.
        right : List[Union[int, str]]
            Parsed identifiers for the right-hand version.
        pad_numeric : bool
            Whether to treat missing trailing numeric identifiers as
                zero when one side runs out of tokens.

        Returns
        -------
        int
            ``1`` if *left* is newer, ``-1`` if *right* is newer, or ``0`` if
            they compare equal.

        """
        max_len = max(len(left), len(right))
        for index in range(max_len):
            left_value = (
                left[index] if index < len(left) else (0 if pad_numeric else None)
            )
            right_value = (
                right[index] if index < len(right) else (0 if pad_numeric else None)
            )
            if left_value == right_value:
                continue
            if left_value is None:
                return -1
            if right_value is None:
                return 1
            if isinstance(left_value, int) and isinstance(right_value, int):
                return 1 if left_value > right_value else -1
            if isinstance(left_value, int) and isinstance(right_value, str):
                return -1
            if isinstance(left_value, str) and isinstance(right_value, int):
                return 1
            return 1 if str(left_value) > str(right_value) else -1
        return 0

    upstream1, local1 = split_package_version(version1)
    upstream2, local2 = split_package_version(version2)
    main1, prerelease1 = parse_upstream(upstream1)
    main2, prerelease2 = parse_upstream(upstream2)
    main_comparison = compare_identifier_lists(main1, main2, pad_numeric=True)
    if main_comparison != 0:
        return main_comparison
    if prerelease1 is None and prerelease2 is not None:
        return 1
    if prerelease1 is not None and prerelease2 is None:
        return -1
    if prerelease1 is not None and prerelease2 is not None:
        prerelease_comparison = compare_identifier_lists(
            prerelease1, prerelease2, pad_numeric=False
        )
        if prerelease_comparison != 0:
            return prerelease_comparison
    # A version without a local revision sorts before one that has it.
    return compare_identifier_lists(
        [parse_identifier(part) for part in local1.split(".") if part != ""],
        [parse_identifier(part) for part in local2.split(".") if part != ""],
        pad_numeric=False,
    )


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
