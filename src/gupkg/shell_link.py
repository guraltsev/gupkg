"""Read and write Windows shortcut (``.lnk``) files through the Shell COM API.

Shortcuts are written and read by calling ``IShellLinkW`` and ``IPersistFile``
directly through :mod:`ctypes`. No child process is started, no value is ever
embedded in source code, and no third-party package is needed.

Public API
----------
``ShellLink``
    The shortcut fields gupkg manages.
``write_shell_link(path, link)``
    Create or replace one ``.lnk`` file.
``read_shell_link(path)``
    Read one ``.lnk`` file back into a ``ShellLink``.

Notes
-----
The target is stored exactly as given: it is not resolved and ``%VAR%``
references stay unexpanded in the link. A failed COM call surfaces as
``OSError`` whose ``filename`` is the shortcut path.

Implementation Approach
-----------------------
COM methods are called through the object's vtable at fixed slot indexes
(see ``_ISHELLLINK_SLOTS`` and ``_IPERSISTFILE_SLOTS``). Each call initializes
COM for its thread, so the TUI may call in from worker threads, and releases
every interface pointer before returning.
"""

from __future__ import annotations

import ctypes
import os
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

__all__ = ["ShellLink", "read_shell_link", "write_shell_link"]


@dataclass(frozen=True)
class ShellLink:
    """Hold the fields of one Windows shortcut.

    Parameters
    ----------
    target_path : str
        Executable or document the shortcut opens, exactly as stored.
    arguments : str
        Command-line arguments.
    working_directory : str
        "Start in" directory.
    icon_location : str
        Icon reference as ``"path,index"``. Empty means no icon.
    description : str
        Comment shown by Windows.
    """

    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""
    description: str = ""


# IShellLinkW vtable slots (ShObjIdl_core.h method order, IUnknown is 0-2):
#   3 GetPath, 4 GetIDList, 5 SetIDList, 6 GetDescription, 7 SetDescription,
#   8 GetWorkingDirectory, 9 SetWorkingDirectory, 10 GetArguments,
#   11 SetArguments, 12 GetHotkey, 13 SetHotkey, 14 GetShowCmd, 15 SetShowCmd,
#   16 GetIconLocation, 17 SetIconLocation, 18 SetRelativePath, 19 Resolve,
#   20 SetPath
_ISHELLLINK_SLOTS = {
    "GetPath": 3,
    "GetDescription": 6,
    "SetDescription": 7,
    "GetWorkingDirectory": 8,
    "SetWorkingDirectory": 9,
    "GetArguments": 10,
    "SetArguments": 11,
    "GetIconLocation": 16,
    "SetIconLocation": 17,
    "SetPath": 20,
}

# IPersistFile vtable slots (IUnknown 0-2, IPersist::GetClassID 3):
#   4 IsDirty, 5 Load, 6 Save, 7 SaveCompleted, 8 GetCurFile
_IPERSISTFILE_SLOTS = {"Load": 5, "Save": 6}

_IUNKNOWN_QUERY_INTERFACE = 0
_IUNKNOWN_RELEASE = 2

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_S_FALSE = 1
_RPC_E_CHANGED_MODE = -0x7FEFFEFA  # 0x80010106 as a signed HRESULT
_STGM_READ = 0
_SLGP_RAWPATH = 0x4
_BUFFER_CHARS = 32768


class _GUID(ctypes.Structure):
    """Native ``GUID`` layout."""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> _GUID:
    """Build a native GUID from its canonical text form."""
    value = uuid.UUID(text)
    return _GUID(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (ctypes.c_ubyte * 8)(*value.bytes[8:]),
    )


def _hresult_error(hresult: int, path: Path) -> OSError:
    """Convert a failed ``HRESULT`` into an ``OSError`` naming *path*."""
    code = hresult & 0xFFFFFFFF
    # HRESULT_FROM_WIN32 wraps Win32 codes with facility 7; unwrap them so the
    # message and winerror match what the rest of Python reports.
    if (code >> 16) == 0x8007:
        code &= 0xFFFF
    message = ctypes.FormatError(code).strip() or f"COM error 0x{hresult & 0xFFFFFFFF:08X}"
    return OSError(0, message, str(path), code)


def _ole32() -> Any:
    """Return ``ole32`` with the prototypes used here."""
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ole32.CoCreateInstance.restype = ctypes.c_long
    return ole32


@contextmanager
def _com_apartment(path: Path) -> Iterator[Any]:
    """Initialize COM on this thread for the duration of the block."""
    ole32 = _ole32()
    hresult = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if hresult == _RPC_E_CHANGED_MODE:
        # The thread already uses another apartment model; COM still works and
        # this call must not be balanced by CoUninitialize.
        initialized = False
    elif hresult < 0:
        raise _hresult_error(hresult, path)
    else:
        # S_OK and S_FALSE both add a reference that CoUninitialize releases.
        initialized = True
    try:
        yield ole32
    finally:
        if initialized:
            ole32.CoUninitialize()


def _method(pointer: ctypes.c_void_p, slot: int, *argtypes: Any) -> Callable[..., int]:
    """Return COM method number *slot* of *pointer* as a Python callable."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    function = prototype(vtable[slot])
    return lambda *args: function(pointer, *args)


def _release(pointer: ctypes.c_void_p) -> None:
    """Release one COM interface pointer."""
    if pointer.value:
        _method(pointer, _IUNKNOWN_RELEASE)()


def _check(hresult: int, path: Path) -> None:
    """Raise ``OSError`` when *hresult* reports failure."""
    if hresult < 0:
        raise _hresult_error(hresult, path)


@contextmanager
def _open_link(path: Path) -> Iterator[tuple[ctypes.c_void_p, ctypes.c_void_p]]:
    """Yield ``(IShellLinkW, IPersistFile)`` pointers for a new shell link."""
    with _com_apartment(path) as ole32:
        link = ctypes.c_void_p()
        persist = ctypes.c_void_p()
        try:
            clsid = _guid("00021401-0000-0000-C000-000000000046")  # CLSID_ShellLink
            iid_link = _guid("000214F9-0000-0000-C000-000000000046")  # IID_IShellLinkW
            iid_persist = _guid("0000010B-0000-0000-C000-000000000046")  # IID_IPersistFile
            _check(
                ole32.CoCreateInstance(
                    ctypes.byref(clsid),
                    None,
                    _CLSCTX_INPROC_SERVER,
                    ctypes.byref(iid_link),
                    ctypes.byref(link),
                ),
                path,
            )
            query = _method(
                link,
                _IUNKNOWN_QUERY_INTERFACE,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )
            _check(query(ctypes.byref(iid_persist), ctypes.byref(persist)), path)
            yield link, persist
        finally:
            _release(persist)
            _release(link)


def _split_icon(icon_location: str) -> tuple[str, int]:
    """Split ``"path,index"`` on its last comma, as WScript.Shell does."""
    head, comma, tail = icon_location.rpartition(",")
    if comma:
        try:
            return head, int(tail.strip())
        except ValueError:
            pass
    return icon_location, 0


def write_shell_link(path: Path, link: ShellLink) -> None:
    """Create or replace one ``.lnk`` file.

    Parameters
    ----------
    path : Path
        Full ``.lnk`` path to write. The parent directory must exist.
    link : ShellLink
        Fields to store. ``target_path`` is stored unresolved.

    Raises
    ------
    OSError
        If any COM call fails. ``filename`` is *path*.
    """
    with _open_link(path) as (shell_link, persist):
        _check(_method(shell_link, _ISHELLLINK_SLOTS["SetPath"], wintypes.LPCWSTR)(link.target_path), path)
        setters = (
            ("SetArguments", link.arguments),
            ("SetWorkingDirectory", link.working_directory),
            ("SetDescription", link.description),
        )
        for name, value in setters:
            _check(_method(shell_link, _ISHELLLINK_SLOTS[name], wintypes.LPCWSTR)(value), path)
        if link.icon_location:
            icon_path, index = _split_icon(link.icon_location)
            _check(
                _method(
                    shell_link, _ISHELLLINK_SLOTS["SetIconLocation"], wintypes.LPCWSTR, ctypes.c_int
                )(icon_path, index),
                path,
            )
        _check(
            _method(persist, _IPERSISTFILE_SLOTS["Save"], wintypes.LPCWSTR, wintypes.BOOL)(
                os.fspath(path), True
            ),
            path,
        )


def read_shell_link(path: Path) -> ShellLink:
    """Read one ``.lnk`` file.

    Parameters
    ----------
    path : Path
        Existing ``.lnk`` file.

    Returns
    -------
    ShellLink
        The stored fields. The target is the raw stored text, with ``%VAR%``
        references unexpanded. ``icon_location`` is ``"path,index"``, or empty
        when the link has no icon path.

    Raises
    ------
    OSError
        If the file cannot be loaded or a field cannot be read.
    """
    with _open_link(path) as (shell_link, persist):
        _check(
            _method(persist, _IPERSISTFILE_SLOTS["Load"], wintypes.LPCWSTR, wintypes.DWORD)(
                os.fspath(path), _STGM_READ
            ),
            path,
        )

        def read_text(name: str, *extra: Any) -> str:
            buffer = ctypes.create_unicode_buffer(_BUFFER_CHARS)
            _check(
                _method(
                    shell_link,
                    _ISHELLLINK_SLOTS[name],
                    ctypes.c_void_p,
                    ctypes.c_int,
                    *[argtype for argtype, _ in extra],
                )(buffer, _BUFFER_CHARS, *[value for _, value in extra]),
                path,
            )
            return buffer.value

        target = read_text(
            "GetPath", (ctypes.c_void_p, None), (wintypes.DWORD, _SLGP_RAWPATH)
        )
        arguments = read_text("GetArguments")
        working_directory = read_text("GetWorkingDirectory")
        description = read_text("GetDescription")

        icon_buffer = ctypes.create_unicode_buffer(_BUFFER_CHARS)
        icon_index = ctypes.c_int(0)
        _check(
            _method(
                shell_link,
                _ISHELLLINK_SLOTS["GetIconLocation"],
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_int),
            )(icon_buffer, _BUFFER_CHARS, ctypes.byref(icon_index)),
            path,
        )
        icon = f"{icon_buffer.value},{icon_index.value}" if icon_buffer.value else ""
    return ShellLink(target, arguments, working_directory, icon, description)
