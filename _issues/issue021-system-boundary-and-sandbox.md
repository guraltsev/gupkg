# Issue 021: System boundary, FakeSystem, and sandbox mode

Date: 2026-10-22
Priority: Critical (foundation)
Epic: 020. Design: `_docs/rehaul_design.md` §12 (`system/`), §16

## Problem

Every Windows side effect is called directly, with no injection seam:

- junctions: `windows.create_junction` and `is_junction`, called from
  `layout.py`;
- `winreg` reads and writes;
- shortcuts, created through a `powershell.exe` COM subprocess;
- `WM_SETTINGCHANGE` broadcasts;
- `IsUserAnAdmin`, implemented twice (`windows.is_current_user_admin` and
  `elevation.is_elevated`);
- ACL reads;
- `ShellExecuteW runas`;
- `urllib.request.urlopen`, called from `origin.py`, `updates.py`,
  `registry.py` and `github_releases.py`;
- `git` subprocesses.

Tests work around this with `mock.patch.object` on module attributes. That
includes patching `gupkg.install_components` 12 times and
`manager.__file__` 12 times. End-to-end installation from a registry seed has
no test. The owner notes that "a lot of things are not easy to test" because
system installation is involved.

## Scope

1. **`system/port.py`.** Define a `System` `Protocol` with these groups:
   - **links:** `create_junction`, `read_link`, `is_link`, `remove_link`,
     `hardlink`;
   - **environment:** `get_env(scope, name)`, `set_env(scope, name, value,
     expandable)`, `delete_env`, `get_path(scope)`, `set_path(scope, entries)`,
     `broadcast_environment_change()`;
   - **shortcuts:** `write_shortcut(path, ShortcutSpec)`,
     `read_shortcut(path)`, `delete_shortcut(path)`;
   - **privilege:** `is_elevated()`, `relaunch_elevated(argv)`,
     `directory_is_admin_only(path)`;
   - **processes:** `run(argv, cwd, env, on_line)`, which passes each output
     line to the `on_line` callback (issue 022 connects it to the event
     stream);
   - **network:** `fetch(url, headers, dest, on_progress)` and
     `fetch_text(url, headers)`.
2. **`system/windows.py`.** Move the existing implementations behind the
   protocol unchanged in behaviour, and delete the duplicate admin check.
   The shortcut implementation is the COM `IShellLinkW` module from issue
   017, if that issue has landed. Otherwise this issue moves the PowerShell
   version and issue 017 replaces it behind the boundary.
   - Junction creation is `DeviceIoControl(FSCTL_SET_REPARSE_POINT)`, from
     issue 017 section 3, if 017 did it. Otherwise this issue implements it,
     so that no `cmd /c mklink` remains.
   - Elevated relaunch starts the bundled launcher, as issue 018 requires.
     It never starts `sys.executable` with arbitrary flags.
3. **`system/fake.py`.** A `FakeSystem` that:
   - uses real temporary directories for files;
   - keeps an in-memory registry for user and system scope;
   - keeps shortcuts as records serialised to `.lnk.json` files, so they are
     visible in the temporary tree;
   - has a configurable elevation flag;
   - serves fetches from a URL→bytes or URL→callable fixture map, and errors
     on unknown URLs;
   - runs processes only from an allow-list of test doubles, plus a real 7-Zip
     where it is available.
4. **Sandbox mode.** `--sandbox DIR` on the CLI runs everything against a
   `FakeSystem` whose state persists in `DIR/system.json`. Network access is
   real in sandbox mode. Only machine integration is faked.
5. **Contract tests.** One test module checks the behaviour of both
   implementations. The `WindowsSystem` run is marked `windows_integration`
   and uses:
   - a throwaway HKCU key (`HKCU\Software\gupkg-test-<uuid>`) instead of
     `HKCU\Environment`, which works because the environment target is a
     parameter of the implementation;
   - a temporary shortcut folder;
   - a temporary junction.
6. **Callers move to the protocol.** Every current direct caller uses a
   `System` passed in explicitly. Module-level imports of `winreg` and
   `ctypes` outside `system/windows.py` are not allowed.

## Out of scope

The engine redesign (issue 028). This issue only moves the existing call
sites behind the boundary.

## Acceptance criteria

- [ ] `rg "winreg|ctypes|urlopen|powershell|subprocess\.run" src/gupkg --type py`
      matches only `system/windows.py`, plus `selfmgmt/` where that is
      justified in a comment.
- [ ] The contract suite passes against `FakeSystem` on every run and
      against `WindowsSystem` on Windows with `-m windows_integration`.
- [ ] `gupkg --sandbox %TEMP%\s install <fixture package>` creates no HKCU
      values, Start Menu entries or PATH changes on the real machine. The
      shortcut and environment state it would have created is visible in
      `system.json`.
- [ ] Existing tests that patch `create_shortcut`, `install_components`,
      `is_elevated` or `urlopen` are rewritten to use `FakeSystem`, or deleted
      where they only pinned implementation.

## Tests

Contract tests (§16). The behaviours protected:

- environment and PATH round-trips are idempotent and case-insensitive;
- a junction replacement is atomic, with rollback;
- shortcut fields survive a round-trip;
- elevation is reported correctly.
