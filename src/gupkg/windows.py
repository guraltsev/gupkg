"""Provide the Windows operating-system boundary for package installation.

Shortcut, junction, registry, environment-notification, elevation, and console
operations are isolated here so package workflows can express intent without
mixing platform mechanics into configuration or orchestration.

Implementation Approach
-----------------------
The module calls Windows APIs through ``ctypes`` and the standard library.
Shortcuts go through the Shell COM API (see ``shell_link``) and junctions
through ``DeviceIoControl``; neither starts a child process.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .core import Scope, normalize_path

if os.name == "nt":
    import winreg
else:
    winreg = None  # type: ignore[assignment]


def system_tool(name: str) -> str:
    """Return the absolute path of a Windows system tool.

    Parameters
    ----------
    name : {"powershell", "cmd"}
        Tool to locate under ``%SystemRoot%``. ``"powershell"`` is used only to
        run package-authored ``.ps1`` origin scripts.

    Returns
    -------
    str
        Absolute path when the tool exists under ``%SystemRoot%``. Otherwise the
        bare name is returned for an ordinary run, and ``OSError`` is raised in
        an elevated run, which never searches ``PATH``.
    """
    relative = {
        "powershell": Path("System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "cmd": Path("System32", "cmd.exe"),
    }[name]
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if root and (Path(root) / relative).is_file():
        return str(Path(root) / relative)
    from .elevation import elevated_mode

    if elevated_mode():
        raise OSError(f"Cannot find {name} under %SystemRoot%; elevated runs do not search PATH")
    return name


def require_winreg() -> Any:
    """Return the :mod:`winreg` module or raise a platform error.

    Returns
    -------
    Any
        The imported :mod:`winreg` module.

    Raises
    ------
    OSError
        If the current interpreter does not provide :mod:`winreg`.

    """
    if winreg is None:
        raise OSError("winreg is only available on Windows.")
    return winreg


def create_shortcut(
    shortcut_path: Path,
    target_path: str,
    *,
    arguments: str = "",
    working_directory: str = "",
    icon_location: str = "",
    description: str = "",
) -> None:
    """Create or replace one ``.lnk`` file through the Windows Shell COM API.

    Parameters
    ----------
    shortcut_path : Path
        Full ``.lnk`` path to create.
    target_path : str
        Executable path the shortcut should launch, stored unresolved.
    arguments : str
        Optional command-line arguments.
    working_directory : str
        Optional working directory.
    icon_location : str
        Optional ``path,index`` icon reference.
    description : str
        Optional description shown by Windows.

    Raises
    ------
    OSError
        If the shortcut cannot be written. The error names ``shortcut_path``.

    """
    from .shell_link import ShellLink, write_shell_link

    write_shell_link(
        shortcut_path,
        ShellLink(
            target_path=target_path,
            arguments=arguments,
            working_directory=working_directory,
            icon_location=icon_location,
            description=description,
        ),
    )


def create_junction(source: Path, target: Path) -> None:
    r"""Create or replace one NTFS junction.

    Parameters
    ----------
    source : Path
        Path where the junction should be created.
    target : Path
        Existing directory the junction should reference.

    Raises
    ------
    RuntimeError
        If the junction cannot be created safely.

    """
    if not target.exists() or not target.is_dir():
        raise RuntimeError(
            f"Junction target does not exist or is not a directory: {target}"
        )

    if os.path.lexists(str(source)):
        if is_junction(source):
            os.rmdir(str(source))
        else:
            raise RuntimeError(
                f"{source} already exists and is not a junction; refusing to overwrite."
            )

    # Create the junction; callers see one RuntimeError for any failure.
    try:
        _win_make_junction(source, target)
    except OSError as exc:
        raise RuntimeError(f"Cannot create junction {source} -> {target}: {exc}") from exc


def _win_make_junction(source: Path, target: Path) -> None:
    r"""Create *source* as an NTFS junction to *target* without a child process.

    Parameters
    ----------
    source : Path
        Path that must not exist yet.
    target : Path
        Existing directory the junction references.

    Raises
    ------
    OSError
        If the directory or the reparse point cannot be created.

    """
    import ctypes
    import struct
    from ctypes import wintypes

    generic_write = 0x40000000
    open_existing = 3
    file_share_all = 0x00000007
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    fsctl_set_reparse_point = 0x000900A4
    io_reparse_tag_mount_point = 0xA0000003

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    kernel32.CreateDirectoryW.restype = wintypes.BOOL
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    # Build the MOUNT_POINT_REPARSE_BUFFER: the substitute name is the NT path
    # ``\??\<absolute target>``, the print name is the plain absolute path.
    # A ``\\?\`` prefix is dropped so the NT name is not doubled; network
    # (UNC) targets cannot be junction targets and are refused up front.
    absolute = os.path.abspath(str(target))
    if absolute.upper().startswith("\\\\?\\UNC\\"):
        raise OSError(f"a junction cannot point to a network path: {target}")
    if absolute.startswith("\\\\?\\"):
        absolute = absolute[4:]
    if absolute.startswith("\\\\"):
        raise OSError(f"a junction cannot point to a network path: {target}")
    absolute = absolute.rstrip("\\")
    if len(absolute) == 2:
        # A drive root such as ``C:\`` loses its backslash above; restore it.
        absolute += "\\"
    substitute = ("\\??\\" + absolute).encode("utf-16-le")
    printed = absolute.encode("utf-16-le")
    path_buffer = substitute + b"\0\0" + printed + b"\0\0"
    header = struct.pack("<HHHH", 0, len(substitute), len(substitute) + 2, len(printed))
    data = header + path_buffer
    buffer = struct.pack("<IHH", io_reparse_tag_mount_point, len(data), 0) + data

    # Create the empty directory that will carry the reparse point. Every
    # failure from here on removes it again, so no half-made junction is left.
    if not kernel32.CreateDirectoryW(str(source), None):
        raise ctypes.WinError(ctypes.get_last_error())

    # Open the new directory itself (not what it would point to) and attach it.
    handle = kernel32.CreateFileW(
        str(source),
        generic_write,
        file_share_all,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    if handle == wintypes.HANDLE(-1).value or handle is None:
        error = ctypes.WinError(ctypes.get_last_error())
        os.rmdir(str(source))
        raise error
    try:
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle,
            fsctl_set_reparse_point,
            buffer,
            len(buffer),
            None,
            0,
            ctypes.byref(returned),
            None,
        )
        error = None if ok else ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    if error is not None:
        os.rmdir(str(source))
        raise error


def _win_get_reparse_tag(path: Path) -> Optional[int]:
    """Read the reparse tag for one filesystem entry.

    Parameters
    ----------
    path : Path
        Filesystem entry to inspect.

    Returns
    -------
    Optional[int]
        The integer reparse tag, or ``None`` when the tag cannot be read.

    """
    try:
        import ctypes
        from ctypes import wintypes

        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        OPEN_EXISTING = 3
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        FSCTL_GET_REPARSE_POINT = 0x000900A8

        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        device_io_control = ctypes.windll.kernel32.DeviceIoControl
        device_io_control.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        device_io_control.restype = wintypes.BOOL

        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle_value = wintypes.HANDLE(-1).value
        if handle == invalid_handle_value:
            return None

        try:
            buffer = ctypes.create_string_buffer(16 * 1024)
            returned = wintypes.DWORD(0)
            ok = device_io_control(
                handle,
                FSCTL_GET_REPARSE_POINT,
                None,
                0,
                buffer,
                len(buffer),
                ctypes.byref(returned),
                None,
            )
            if not ok:
                return None
            return int.from_bytes(buffer.raw[0:4], "little", signed=False)
        finally:
            close_handle(handle)
    except Exception:
        return None


def is_junction(path: Path) -> bool:
    """Return whether one path is an NTFS junction.

    Parameters
    ----------
    path : Path
        Filesystem entry to inspect.

    Returns
    -------
    bool
        ``True`` when *path* exists and is a junction; otherwise ``False``.

    """
    try:
        if hasattr(os.path, "isjunction"):
            return os.path.isjunction(str(path))  # type: ignore[attr-defined]

        if not os.path.isdir(str(path)):
            return False

        io_reparse_tag_mount_point = 0xA0000003
        return _win_get_reparse_tag(path) == io_reparse_tag_mount_point
    except Exception:
        return False


def get_junction_target(path: Path) -> Optional[Path]:
    """Resolve the target of one junction path.

    Parameters
    ----------
    path : Path
        Junction path to inspect.

    Returns
    -------
    Optional[Path]
        The normalized target path, or ``None`` when the target cannot be read.

    """
    try:
        return normalize_path(os.readlink(str(path)))
    except (OSError, AttributeError):
        return None


def environment_registry_location(scope: Scope) -> Tuple[Any, str]:
    """Return the registry location used for one environment scope.

    Parameters
    ----------
    scope : Scope
        Installation scope whose environment location is needed.

    Returns
    -------
    Tuple[Any, str]
        A tuple ``(root_hkey_or_none, subkey)``.

    """
    if scope == Scope.USER:
        return (
            winreg.HKEY_CURRENT_USER if winreg is not None else None,
            r"Environment",
        )
    return (
        winreg.HKEY_LOCAL_MACHINE if winreg is not None else None,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )


def read_registry_value(root: Any, subkey: str, name: str) -> Tuple[Any, int]:
    """Read one registry value.

    Parameters
    ----------
    root : Any
        Registry hive constant.
    subkey : str
        Registry key path below *root*.
    name : str
        Value name to read.

    Returns
    -------
    Tuple[Any, int]
        Tuple ``(value, registry_type)`` from ``QueryValueEx``.

    Raises
    ------
    OSError
        If the value cannot be read.

    """
    reg = require_winreg()
    with reg.OpenKey(root, subkey, 0, reg.KEY_READ) as key:
        return reg.QueryValueEx(key, name)


def write_registry_value(
    root: Any, subkey: str, name: str, value: str, reg_type: int
) -> None:
    """Write one registry value.

    Parameters
    ----------
    root : Any
        Registry hive constant.
    subkey : str
        Registry key path below *root*.
    name : str
        Value name to write.
    value : str
        Value data to store.
    reg_type : int
        ``winreg`` registry type constant.

    Raises
    ------
    OSError
        If the value cannot be written.

    """
    reg = require_winreg()
    with reg.OpenKey(root, subkey, 0, reg.KEY_SET_VALUE) as key:
        reg.SetValueEx(key, name, 0, reg_type, value)


def broadcast_environment_change() -> None:
    """Notify Windows that environment values changed.

    Raises
    ------
    OSError
        If Windows does not accept the broadcast notification.

    """
    import ctypes
    from ctypes import wintypes

    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002

    send_message_timeout = ctypes.windll.user32.SendMessageTimeoutW
    send_message_timeout.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    send_message_timeout.restype = wintypes.LPARAM

    # SendMessageTimeoutW writes a DWORD_PTR result. ctypes.c_size_t is the
    # portable pointer-sized unsigned type, unlike wintypes.ULONG_PTR, which
    # is absent from some supported Python builds.
    result = ctypes.c_size_t(0)

    # LPARAM is an integer type, so obtain the Environment string address via
    # c_void_p rather than casting directly to a non-pointer ctypes type.
    environment_name = ctypes.c_wchar_p("Environment")
    environment_lparam = ctypes.cast(environment_name, ctypes.c_void_p).value
    ok = send_message_timeout(
        hwnd_broadcast,
        wm_settingchange,
        0,
        environment_lparam,
        smto_abortifhung,
        5000,
        ctypes.byref(result),
    )
    if not ok:
        raise OSError("SendMessageTimeoutW failed")


def is_current_user_admin() -> bool:
    """Return whether the current process has Administrator privileges.

    Returns
    -------
    bool
        ``True`` when the current process is elevated; otherwise ``False``.

    """
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(arguments: list[str]) -> bool:
    """Request one elevated process using an argument vector."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        # Go through the package launcher, never "python -m gupkg": the launcher
        # starts the bundled interpreter in isolated mode, so the elevated
        # process cannot import from the current directory or user variables.
        launcher = Path(__file__).resolve().parent / "gupkg.cmd"
        if not launcher.is_file():
            return False
        params = f'/d /s /c ""{launcher}" {subprocess.list2cmdline(arguments)}"'
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", system_tool("cmd"), params, str(launcher.parent), 1
        )
        return result > 32
    except Exception:
        return False


def wait_for_keypress() -> None:
    """Pause for a keypress using the Windows console when possible.

    Returns
    -------
    None
        ``None``.

    """
    try:
        import msvcrt

        msvcrt.getch()
    except ImportError:
        input("Press Enter to continue...")
