# Issue 017: Create and read shortcuts through COM, not PowerShell

Date: 2026-10-22
Priority: Medium
Change type: Windows boundary (`windows.py`, `shortcuts_to_gupkg_toml.py`); no
package-format or CLI changes

## Objective

gupkg should not start PowerShell to do its own work. Today the only runtime
use is shortcut handling. Replace it with direct calls to the Windows Shell
COM interfaces (`IShellLinkW` + `IPersistFile`) through `ctypes`.

**Out of scope:** package-authored `.ps1` origin scripts
(`src/gupkg/origin.py:209-210`). Running a script the package author chose
in its native interpreter is a feature, and it stays. The bootstrap scripts
(`gupkg-bootstrap.cmd/.ps1`) run before gupkg exists and are covered by
issue 016.

## Current state

| Where | What it does | Problem |
|---|---|---|
| `src/gupkg/windows.py:115-170` (`create_shortcut`) | Builds a PowerShell script with `WScript.Shell.CreateShortcut` and string-interpolated, single-quote-escaped values, then runs `powershell.exe -NoProfile -NonInteractive -Command`. | About 300-800 ms per shortcut for PowerShell startup. Values are passed by building source code. Failures come back as PowerShell error text. Machines where policy blocks or removes PowerShell (Constrained Language Mode, AppLocker) cannot get shortcuts. |
| `src/gupkg/shortcuts_to_gupkg_toml.py:270-297` | Reads existing `.lnk` files through PowerShell and `ConvertTo-Json`. | Same costs. It also calls bare `powershell` (found on `PATH`), not `system_tool("powershell")`. |
| `src/gupkg/windows.py:202` (`create_junction`) | `cmd /c mklink /J`. | Not PowerShell, but the same pattern: a child process for something `ctypes` can do. Optional, see section 3. |

The environment-change broadcast (`windows.py:427`), elevation checks and
reparse-tag reads already use `ctypes`. Shortcuts are the odd one out.

## Design

### 1. A small COM shell-link module

Add `src/gupkg/shell_link.py` with no new dependencies (no `pywin32` or
`comtypes`; the runtime stays as locked in `gupkg-deps.lock`):

```python
@dataclass(frozen=True)
class ShellLink:
    target_path: str
    arguments: str = ""
    working_directory: str = ""
    icon_location: str = ""      # "path,index", as today
    description: str = ""

def write_shell_link(path: Path, link: ShellLink) -> None: ...
def read_shell_link(path: Path) -> ShellLink: ...
```

Implementation notes:

- `CoInitializeEx(None, COINIT_APARTMENTTHREADED)` per call, paired with
  `CoUninitialize`. Treat `S_FALSE` and `RPC_E_CHANGED_MODE` as "already
  initialized" and skip the uninitialize call. The TUI calls in from worker
  threads.
- `CoCreateInstance(CLSID_ShellLink, None, CLSCTX_INPROC_SERVER, IID_IShellLinkW)`,
  then `QueryInterface(IID_IPersistFile)`. Call methods through the vtable with
  `ctypes.WINFUNCTYPE` at fixed slot indexes, and keep a comment with the
  interface's method order. `IShellLinkW` slots used (after `IUnknown` 0-2): `GetPath` 3,
  `GetDescription` 6, `SetDescription` 7, `GetWorkingDirectory` 8,
  `SetWorkingDirectory` 9, `GetArguments` 10, `SetArguments` 11,
  `GetIconLocation` 16, `SetIconLocation` 17, `SetPath` 20. Check these
  against `ShObjIdl_core.h` before relying on them. `IPersistFile`:
  `Load` 5, `Save` 6.
- Split `icon_location` on the **last** comma into path and index, as
  `WScript.Shell` does. An empty value means no icon.
- `SetPath` receives the target exactly as gupkg expanded it. Do not resolve
  it: unexpanded `%VAR%` stays in the link, which `WScript.Shell` also
  allowed, and `pkg.toml` for gupkg itself uses `%COMSPEC%`.
- Turn a failed `HRESULT` into `OSError(winerror, FormatMessage text, path)`,
  so callers keep reporting one clear line.
- Release every interface pointer in `finally`.

### 2. Switch the callers

- `windows.create_shortcut` keeps its signature and calls
  `write_shell_link`. Remove `_escape_powershell_single_quoted` and the
  PowerShell script.
- `shortcuts_to_gupkg_toml.py` calls `read_shell_link` and drops the
  PowerShell/JSON round trip.
- `system_tool` keeps `"powershell"` only for `origin.py`.

### 3. Optional: junctions without `cmd`

Replace `mklink /J` with `CreateDirectoryW` + `DeviceIoControl(FSCTL_SET_REPARSE_POINT)`
with a `REPARSE_DATA_BUFFER` (mount-point tag `IO_REPARSE_TAG_MOUNT_POINT`,
substitute name `\??\<absolute target>`). `_win_get_reparse_tag` already
opens reparse points this way. Do this in the same change only if it stays
small. Otherwise open a follow-up issue.

## Tests

Behavior to protect (Windows-only, `pytest.mark.skipif(os.name != "nt")`):

- A shortcut written by `gupkg install` opens the declared target with the
  declared arguments, working directory, icon and description. Check by
  reading it back with `read_shell_link`, and once in the test with
  `WScript.Shell` through PowerShell as an independent oracle (test-only use).
- Values containing `'`, `"`, `$`, `` ` ``, `%VAR%`, non-ASCII text and paths
  longer than 200 characters round-trip unchanged.
- Re-installing replaces an existing `.lnk`.
- Writing into a directory the user cannot write fails with a one-line error
  that names the shortcut path. The install reports it as today.
- `shortcuts_to_gupkg_toml.py` produces the same TOML for a fixture shortcut as
  before the change.
- With `PATH` empty and `powershell.exe` unreachable (monkeypatch
  `system_tool` to raise for `"powershell"`), installing a package with
  shortcuts and no `.ps1` origin succeeds.

## Acceptance checklist

- [ ] No `powershell` call in `src/gupkg` except `origin.py`'s `.ps1` branch.
- [ ] `gupkg install` of a package with 10 shortcuts is noticeably faster
      (record before/after timings in this issue).
- [ ] Shortcuts created by the new code are identical in Explorer's
      Properties dialog to those created by the old code (target, arguments,
      start in, icon, comment).
- [ ] Works from the TUI (worker thread) and from an elevated system-scope run.
- [ ] `docs/` and the `windows.py` module docstring no longer mention
      PowerShell for shortcuts. The README sentence "PowerShell is used to
      create Windows shortcuts" is removed.

## Integration with the 1.0 rehaul

Added 2026-10-22. See `_docs/rehaul_design.md` §20 and epic issue 020.

This issue is **stage 0** of the rehaul. Its decisions stand.

- **It becomes the shortcut implementation behind the `System` boundary**
  (issue 021). `shell_link.write_shell_link` and `read_shell_link` become
  `WindowsSystem.write_shortcut` and `read_shortcut`. `FakeSystem` stores the
  same `ShellLink` fields, so the shortcut contract tests check both
  implementations.
- **Order.** This issue lands strictly before 021, which depends on all of
  stage 0 (decision D35). Issue 021 moves this issue's COM code behind the
  boundary; it never moves PowerShell shortcut code.
- **Junctions (section 3).** Do this part. Issue 021 needs junctions,
  replacement and rollback as a contract-tested `System` operation, and a
  `DeviceIoControl` implementation removes the last `cmd /c` child process.
- **Read-back** is used by `gupkg check`, the health check that replaces
  `doctor` (issue 028, decision D21), to detect shortcut drift (missing, or
  changed target) without starting a process.
- **`shortcuts_to_gupkg_toml.py`** was to move under a core `migrate/`
  package in 1.0. Core carries no migration code (decision D33), so it
  moves out of core with the separate migration tool (issue 023). It keeps
  using `read_shell_link`.
