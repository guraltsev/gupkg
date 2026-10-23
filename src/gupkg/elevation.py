"""Detect elevated runs and verify that the code they import is admin-only.

An elevated gupkg process must execute only code that non-administrators
cannot modify. This module answers whether the process is elevated, reads the
access-control list of a directory, and refuses a runtime whose import path,
interpreter, or bundled dependency directory is writable by anyone but
``Administrators``, ``SYSTEM``, and ``TrustedInstaller``.

Usage and API
-------------
Call ``is_elevated()`` or ``elevated_mode()`` to choose between the user-scoped
and the trusted behavior. Call ``enforce_bundled_runtime(...)`` once at process
start, and ``require_trusted_directory(...)`` before importing from or writing
to a single directory. Failures raise ``UntrustedLocationError`` with a message
that names the directory, the offending principal, and a repair command.

Implementation Approach
-----------------------
The Windows security descriptor is read through ``GetNamedSecurityInfoW`` and
decoded in Python, so the policy (which principals may write, which rights count
as writing) is plain data that can be tested with synthetic descriptors. Only
allow entries that apply to the directory itself are considered; every ancestor
is checked for the smaller set of rights that allow renaming or replacing the
directory. Any error while reading a descriptor fails closed.
"""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

if os.name == "nt":
    # Load everything the checks need while sys.path holds only the interpreter's
    # own folders, so no module of the package directory can shadow it.
    import ctypes
    from ctypes import wintypes


class UntrustedLocationError(RuntimeError):
    """Report that an elevated run would trust a directory non-administrators can change."""


@dataclass(frozen=True)
class AccessEntry:
    """Describe one allow entry of a directory's access-control list."""

    sid: str
    mask: int
    inherit_only: bool = False


@dataclass(frozen=True)
class DirectorySecurity:
    """Carry the owner and allow entries of one directory's security descriptor."""

    owner: str
    entries: tuple[AccessEntry, ...]
    has_dacl: bool = True


# Principals that may change files an elevated process trusts: SYSTEM,
# BUILTIN\Administrators, and NT SERVICE\TrustedInstaller.
TRUSTED_SIDS = {
    "S-1-5-18": "SYSTEM",
    "S-1-5-32-544": "Administrators",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464": "TrustedInstaller",
}

# Well-known principals shown by name in messages instead of a raw SID.
_SID_NAMES = {
    "S-1-1-0": "Everyone",
    "S-1-5-11": "Authenticated Users",
    "S-1-5-32-545": "Users",
    "S-1-3-0": "CREATOR OWNER",
}

# Access-mask bits that let a principal modify a directory's contents or its
# security: write data, append, write EAs, delete child, write attributes,
# delete, write DACL, write owner, generic write, and generic all.
_WRITE_RIGHTS = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000

# Smaller set that suffices to rename or replace a directory from above it.
_REPLACE_RIGHTS = 0x40 | 0x10000 | 0x40000 | 0x80000 | 0x10000000

_ACE_INHERIT_ONLY = 0x08
_ACCESS_ALLOWED_TYPES = {0, 9}  # plain and callback allow entries; both put the SID at offset 8


# ---------------------------------------------------------------------------
# Elevation detection
# ---------------------------------------------------------------------------


def _admin_signal() -> bool:
    """Return ``IsUserAnAdmin()`` as a bool (raises when the call is unavailable)."""
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _token_elevation_signal() -> bool:
    """Return the ``TokenElevation`` flag of the process token (raises on any failure)."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x8, ctypes.byref(token)):  # TOKEN_QUERY
        raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
    try:
        elevation = wintypes.DWORD()
        size = wintypes.DWORD()
        # TokenElevation = 20
        if not advapi32.GetTokenInformation(token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")
        return bool(elevation.value)
    finally:
        kernel32.CloseHandle(token)


def is_elevated() -> bool:
    """Return whether the current process may hold an elevated administrator token.

    Returns
    -------
    bool
        On Windows, ``True`` when either ``IsUserAnAdmin`` or the token's
        ``TokenElevation`` flag says elevated, and also when either signal
        cannot be read (detection fails closed, so the strict checks apply).
        ``False`` on other platforms.
    """
    if os.name != "nt":
        return False
    for signal in (_admin_signal, _token_elevation_signal):
        try:
            if signal():
                return True
        except Exception:
            return True
    return False


def elevated_mode() -> bool:
    """Return whether this run must use only trusted, non-implicit behavior.

    Returns
    -------
    bool
        ``True`` when the process is elevated or the launcher marked the run
        elevated; the marker can only make behavior stricter.
    """
    return os.environ.get("GUPKG_ELEVATED") == "1" or is_elevated()


# ---------------------------------------------------------------------------
# Trust policy
# ---------------------------------------------------------------------------


def enforce_bundled_runtime(package_directory: Path, elevated: bool = False) -> None:
    """Refuse any run that is not the isolated bundled interpreter; also check admin-only paths when elevated.

    Parameters
    ----------
    package_directory : Path
        Directory of the ``gupkg`` package that is running.
    elevated : bool, optional
        Also require that every directory the run imports from is admin-only.

    Raises
    ------
    UntrustedLocationError
        If the interpreter is not ``<package>\\python\\python.exe`` or is not
        isolated (``-I``), or, for an elevated run, if any directory on the
        import path or the bundled dependency directory is writable by a
        non-administrator.
    """
    package_directory = Path(package_directory).resolve()
    runtime_directory = package_directory / "python"

    # gupkg.cmd starts the bundled interpreter with -I -s -B; any other way of
    # reaching gupkg would honor the caller's Python, variables and site-packages.
    expected = runtime_directory / "python.exe"
    if (
        os.path.normcase(os.path.realpath(sys.executable)) != os.path.normcase(str(expected.resolve()))
        or not sys.flags.isolated
    ):
        raise UntrustedLocationError(
            "Start gupkg through gupkg.cmd or gupkg.exe; it runs only on its bundled Python "
            f"({expected}, started in isolated mode), not {sys.executable}."
        )
    if not elevated:
        return

    # Every directory the interpreter can import from, plus the bundled
    # dependency directory that is added to the path later.
    directories = {package_directory, package_directory.parent, runtime_directory}
    directories.add(runtime_directory / "Lib" / "site-packages")
    for entry in sys.path:
        if entry and os.path.isdir(entry):
            directories.add(Path(entry).resolve())
    for directory in sorted(directories, key=str):
        if directory.exists():
            require_trusted_directory(directory)


def require_trusted_directory(directory: Path, reader=None) -> None:
    """Raise unless only administrators can modify *directory* and replace its ancestors.

    Parameters
    ----------
    directory : Path
        Directory an elevated process is about to trust.
    reader : callable, optional
        Replacement for the security-descriptor reader, used by tests.

    Raises
    ------
    UntrustedLocationError
        If the directory or one of its ancestors is not admin-only, or its
        security descriptor cannot be read.
    """
    read = reader or read_directory_security
    resolved = Path(os.path.realpath(directory))
    problem = None
    try:
        # The directory itself: any non-admin write right, or a non-admin owner.
        problem = find_untrusted_principal(read(resolved), _WRITE_RIGHTS)

        # Its ancestors: only rights that allow renaming or replacing it.
        if problem is None:
            for ancestor in resolved.parents:
                problem = find_untrusted_principal(read(ancestor), _REPLACE_RIGHTS, where=str(ancestor))
                if problem is not None:
                    break
    except OSError as exc:
        problem = f"its permissions could not be read ({exc})"
    if problem is not None:
        raise UntrustedLocationError(_untrusted_message(resolved, problem))


def find_untrusted_principal(security: DirectorySecurity, rights: int, where: str | None = None) -> str | None:
    """Return a description of the first non-administrator who holds *rights*, if any.

    Parameters
    ----------
    security : DirectorySecurity
        Decoded owner and allow entries of one directory.
    rights : int
        Access-mask bits that count as modifying.
    where : str, optional
        Parent directory to name in the description.

    Returns
    -------
    str or None
        Human-readable reason, or ``None`` when only trusted principals qualify.
    """
    place = f"its parent {where}" if where else "it"
    if not security.has_dacl:
        return f"{place} has no access-control list, so everyone can modify it"
    if security.owner not in TRUSTED_SIDS:
        return f"{place} is owned by {_principal_name(security.owner)}, who can change its permissions"
    for entry in security.entries:
        if entry.inherit_only or entry.sid in TRUSTED_SIDS:
            continue
        if entry.mask & rights:
            return f"{_principal_name(entry.sid)} can modify {place}"
    return None


def _principal_name(sid: str) -> str:
    """Return a readable name for a SID."""
    return _SID_NAMES.get(sid, TRUSTED_SIDS.get(sid, sid))


def _untrusted_message(directory: Path, problem: str) -> str:
    """Build the failure message, including an icacls command that fixes the common case."""
    fix = (
        f'icacls "{directory}" /inheritance:r /setowner *S-1-5-32-544 '
        "/grant:r *S-1-5-18:(OI)(CI)F *S-1-5-32-544:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX"
    )
    return (
        f"Refusing to run elevated: {directory} is not admin-only because {problem}. "
        "Elevated runs require an install location that only Administrators and SYSTEM "
        f"can modify. From an elevated prompt, restrict it with: {fix} "
        "(children inherit it; do not add /T), or reinstall gupkg under such a location."
    )


# ---------------------------------------------------------------------------
# Security-descriptor reader (the Windows boundary)
# ---------------------------------------------------------------------------


def read_directory_security(path: Path) -> DirectorySecurity:
    """Read the owner and allow entries of one directory from Windows.

    Parameters
    ----------
    path : Path
        Existing directory.

    Returns
    -------
    DirectorySecurity
        Decoded descriptor.

    Raises
    ------
    OSError
        If the platform is not Windows or the descriptor cannot be read.
    """
    if os.name != "nt":
        raise OSError("access-control lists can only be read on Windows")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer),
        ctypes.POINTER(pointer), ctypes.POINTER(pointer),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorLength.argtypes = [pointer]
    advapi32.GetSecurityDescriptorLength.restype = wintypes.DWORD
    advapi32.GetLengthSid.argtypes = [pointer]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = [pointer]
    kernel32.LocalFree.restype = pointer

    # SE_FILE_OBJECT = 1; request the owner (1) and the DACL (4).
    owner = pointer()
    dacl = pointer()
    descriptor = pointer()
    status = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 1 | 4, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if status != 0:
        raise OSError(status, f"GetNamedSecurityInfoW failed for {path}")
    try:
        owner_sid = _sid_text(ctypes.string_at(owner, advapi32.GetLengthSid(owner)))
        if not dacl.value:
            return DirectorySecurity(owner_sid, (), has_dacl=False)

        # The ACL header stores its own total size; decode the bytes in Python.
        acl_size = struct.unpack_from("<H", ctypes.string_at(dacl, 4), 2)[0]
        return DirectorySecurity(owner_sid, parse_acl(ctypes.string_at(dacl, acl_size)))
    finally:
        kernel32.LocalFree(descriptor)


def parse_acl(data: bytes) -> tuple[AccessEntry, ...]:
    """Decode the allow entries of a binary ACL.

    Parameters
    ----------
    data : bytes
        An ACL: an 8-byte header followed by its entries.

    Returns
    -------
    tuple of AccessEntry
        Allow entries only. Deny entries are ignored, which can only make the
        result stricter.

    Raises
    ------
    OSError
        If the structure is truncated or inconsistent.
    """
    try:
        count = struct.unpack_from("<H", data, 4)[0]
        offset = 8
        entries = []
        for _ in range(count):
            ace_type, flags, size = struct.unpack_from("<BBH", data, offset)
            if size < 8:
                raise ValueError("entry too small")
            if ace_type in _ACCESS_ALLOWED_TYPES:
                mask = struct.unpack_from("<I", data, offset + 4)[0]
                sid = _sid_text(data[offset + 8 : offset + size])
                entries.append(AccessEntry(sid, mask, bool(flags & _ACE_INHERIT_ONLY)))
            elif ace_type not in (1, 6, 10, 2, 3, 4, 7, 8, 12, 13, 14, 15, 16, 17, 18, 19, 20):
                # An allow-like entry this reader cannot decode must not be trusted silently.
                raise ValueError(f"unknown entry type {ace_type}")
            offset += size
    except (struct.error, ValueError) as exc:
        raise OSError(f"malformed access-control list: {exc}") from exc
    return tuple(entries)


def _sid_text(data: bytes) -> str:
    """Render a binary SID as its ``S-1-...`` string."""
    revision, count = data[0], data[1]
    authority = int.from_bytes(data[2:8], "big")
    subauthorities = struct.unpack_from(f"<{count}I", data, 8)
    return "S-" + "-".join(str(part) for part in (revision, authority, *subauthorities))
