# Design: trusted runtime for elevated gupkg runs

Status: implemented. Where the implementation differs from the original
proposal, the text below has been updated and the difference is listed in
section 9.

## 1. Problem

gupkg installs its own runtime dependencies (currently `textual`, for the TUI)
with `pip` the first time they are needed. The target is
`%LOCALAPPDATA%\gupkg\embedded\site-packages`, a per-user directory.

An elevated (administrator) run uses that same directory. This has two
consequences.

**Functional.** An elevated run creates admin-owned files inside the user's
profile. A later non-elevated run cannot read or replace them. The observed
failure was `No module named 'textual.app'`: `textual` was half-installed, the
availability check (`import textual`) passed on the damaged folder, and the
import of `textual.app` then failed. Launching from a shortcut showed the
failure; the window closed before the message could be read.

**Security.** The directory is writable by any process running as the user.
An elevated gupkg imports from it, so anything able to write there gains
administrator code execution. Other inputs an elevated Python process
trusts are also user-controlled:

| Input | Why it is attacker-reachable |
|---|---|
| `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONUSERBASE` | User-level environment variables; searched before the standard library. |
| `.pth` files and `sitecustomize` / `usercustomize` | Executed by `site`; `site.addsitedir()` is called on the user directory. |
| Current directory and script directory on `sys.path` | A planted `json.py` or `textual.py` shadows the real module. |
| `GUPKG_PYTHON`, the `gupkg.python` file, `python` / `py` from `PATH` | Select which interpreter runs; may be a user-writable install. |
| Automatic `pip install` | Downloads unpinned code and may run build scripts as admin. |
| `__pycache__` | A user-writable cache can hold a replaced `.pyc`. |

Scope note: when the elevated process belongs to the same account as an
unelevated one (a single admin user with UAC), Windows does not treat
elevation as a security boundary, so this hardening raises the cost of one
escalation path without closing all of them. It is a real boundary when an
administrator logs in separately from a standard user. The functional bug is
fixed in both cases.

## 2. Goals and non-goals

Goals:

1. An elevated run executes only code from locations writable by
   administrators alone.
2. An elevated run never installs code implicitly or from the network.
3. A damaged dependency install is detected and reported, never trusted.
4. A user can update dependencies deliberately, with review and
   hash verification.
5. A user is told when a newer version of a pinned dependency exists.
6. Non-elevated runs keep their current convenience behavior.

Non-goals:

- Defending against an attacker who already has an administrator token.
- Sandboxing package hooks (`check_update.py`, `unpack_app.py`). They run
  arbitrary package-local code by design.
- Replacing Python's import machinery or inventing a new loader.

## 3. Design

### 3.1 Elevation detection

Add `is_elevated()` to `gupkg.dependencies` using
`ctypes.windll.shell32.IsUserAnAdmin()`, returning `False` on non-Windows
platforms and on any error (fail toward the less privileged branch for
detection, but see 3.2: an undetected-elevation mistake only means the
non-elevated, user-scoped path is used, which an elevated process cannot be
tricked into by an attacker without already controlling the environment).
`gupkg.cmd` needs the same decision before choosing an interpreter, so the
launcher passes the result through an internal flag rather than re-deriving it.

### 3.2 Elevated interpreter

When elevated, the launcher:

1. Uses only the bundled embeddable CPython at
   `<package>\python\python.exe`. It does not consult `GUPKG_PYTHON`, the
   `gupkg.python` file, `python`, or `py`. If the bundled runtime is missing
   it reports that and stops; it does not download one into a directory it has
   not verified (the existing bootstrap download is kept for non-elevated
   runs, and elevated users run a non-elevated first launch or the explicit
   repair command from 3.5).
2. Starts the interpreter with `-I -s -B`: isolated mode (ignores `PYTHON*`
   variables, user site, and the script directory on `sys.path`), no user
   site, no `.pyc` writing.
3. Does not export the caller's `PYTHONPATH`. The embeddable package's
   `python312._pth` file already restricts the search path to the lines it
   lists (`.`, `..`, `Lib/site-packages`); `bootstrap.py` already writes it.
   The extra `PYTHONPATH` handling in `gupkg.cmd` is skipped when elevated.
4. Resolves `powershell.exe` and similar helpers by absolute path under
   `%SystemRoot%` (already the case for the download step).

### 3.3 Trusted-location check

Before an elevated run imports anything, `gupkg.dependencies` verifies that the
directories on the interpreter's path are not writable by non-administrators:

- the package directory (the `..` entry),
- the bundled runtime directory,
- `python\Lib\site-packages`.

The check reads each directory's ACL and fails if any principal other than
`Administrators`, `SYSTEM`, and `TrustedInstaller` holds a write, modify,
delete, or ownership right (including through `Users` or
`Authenticated Users`). On failure the process prints which directory failed
and why, and exits without importing from it. A copy under a user profile
fails, which is intended. A default `C:\opt` does **not** pass: folders created
under the root of `C:` inherit `Authenticated Users: Modify`. The failure
message prints the `icacls` command that restricts the folder, and
`gupkg-bootstrap.ps1 -Scope system` applies it to the `gupkg` package folder so
that every later version folder inherits it.

### 3.4 Dependency availability probes

Replace the bare top-level import check with a probe import that proves the
installation is usable:

- `_IMPORT_PROBES = {"textual": "textual.app"}`; other dependencies probe their
  own name.
- `pip install` uses `--upgrade` so a damaged existing folder is replaced
  (`--target` otherwise skips an existing directory).

This part is already implemented in `dependencies.py` and covered by
`test_damaged_textual_install_is_repaired_even_when_top_level_imports`. It
applies to non-elevated runs, which keep installing into
`%LOCALAPPDATA%\gupkg\embedded\site-packages`.

### 3.5 Elevated dependency management

Elevated runs never run `pip` implicitly. If a dependency probe fails, the
run stops with: which dependency failed, the directory inspected, and the
command that repairs it.

Dependencies for elevated runs live in `<package>\python\Lib\site-packages`,
governed by a lockfile `<package>\gupkg-deps.lock` listing, for every
distribution (including transitive ones):

```
textual==8.2.8 --hash=sha256:...
rich==15.0.0 --hash=sha256:...
```

Two commands, both under `gupkg manager self`, both refusing to run unless
elevated and unless 3.3 passes:

- `repair-deps` — reinstall exactly what the lockfile pins.
- `update-deps` — resolve newer versions, show a plan, and update after
  confirmation.

Installation procedure shared by both:

1. Create a staging directory next to the target (same volume, admin-only
   ACL inherited from the package directory).
2. `python -I -m pip install --target <staging> --require-hashes
   --only-binary=:all: --no-deps -r <lock>`. Wheels only, so no build scripts
   run; `--no-deps` because the lockfile already lists the full closure.
3. Probe imports run against the staging directory in a child process started
   with `-I`.
4. On success, swap: rename the live directory to a backup name, rename the
   staging directory into place, delete the backup. On any failure the live
   directory is untouched.

`update-deps` additionally:

1. Resolves candidate versions into a temporary lock in the staging area
   (stable, non-yanked releases only).
2. Prints a plan: each distribution's current version, new version, and
   whether it is added or removed. The user confirms before anything is
   installed.
3. After a successful swap, writes the new lockfile. A failed install never
   changes the lockfile.

Limit of this scheme: hashes record what PyPI served when the user approved the
update. They protect against later tampering, not against a release that was
already malicious at update time. The confirmation step, the wheels-only rule,
and keeping the dependency set small are the controls for that risk.

### 3.6 Update notification

On TUI start and in `manager self` status output, gupkg compares each locked
distribution with the latest on PyPI and prints a one-line warning when a newer
release exists:

```
textual 8.2.8 is pinned; 8.3.0 is available. Run: gupkg manager self update-deps
```

Rules:

- Advisory only. It never installs and never edits the lockfile.
- HTTPS to the PyPI JSON API, a few seconds' timeout, silent on any failure so
  offline use and PyPI outages never block startup.
- At most one check per day. The result is cached beside the lockfile; if the
  process cannot write there it skips the cache (it never falls back to a
  user-writable location).
- Only versions matching a strict pattern (digits, dots, and recognised
  pre-release tags) are accepted, so response text cannot inject terminal
  control sequences into the warning.
- Only stable, non-yanked releases trigger the warning.
- A setting disables the check entirely.

### 3.7 Shortcut diagnostics

The shortcut already ends with `|| pause` so a failing run leaves the window
open. That is implemented in `src\pkg.toml` and `src\gupkg\pkg.toml`.

## 4. Behavior summary

| | Non-elevated | Elevated |
|---|---|---|
| Interpreter | bundled, else `GUPKG_PYTHON`, `python`, `py`; bootstrap may download | bundled only |
| Interpreter flags | default | `-I -s -B` |
| `PYTHONPATH` | honored | ignored |
| Dependency directory | `%LOCALAPPDATA%\gupkg\embedded\site-packages` | `<package>\python\Lib\site-packages` |
| Implicit `pip install` | yes (probe-based, `--upgrade`) | never |
| Install source | latest from PyPI | lockfile, hashes required, wheels only |
| Trust check on directories | none | ACL check, fails closed |
| Update command | not needed | `gupkg manager self update-deps` |
| Update notification | optional | optional (cache only if writable and trusted) |

## 5. Failure modes and messages

- Package directory fails the ACL check: name the directory and the offending
  principal; say that elevated runs require an admin-only install location.
- Bundled runtime missing when elevated: say to run `repair-deps` or reinstall
  gupkg from an elevated prompt.
- Probe import fails when elevated: name the distribution and the command
  `gupkg manager self repair-deps`.
- Hash mismatch, or no wheel available for a pinned version: abort before the
  swap; leave the live directory untouched; report the distribution.
- PyPI unreachable during `update-deps`: abort with a clear message; the
  existing install is unaffected.

## 6. Testing

Following `_docs/tests.md`:

- Unit tests with the external boundaries mocked: `is_elevated`, the ACL reader,
  `subprocess.run` for pip, and the PyPI client.
- Probe behavior: top-level import succeeds but the probe fails; fresh install;
  already-good install.
- Staging and swap: failure at each step leaves the live directory and the
  lockfile unchanged; success replaces both.
- Launcher: argument construction for elevated and non-elevated runs, including
  that elevated runs never read `GUPKG_PYTHON`, the `gupkg.python` file, or the
  inherited `PYTHONPATH`.
- Notification: version comparison, malformed and hostile response bodies,
  cache read and write rules, timeout behavior.
- ACL check: directories with `Users` write access, `Authenticated Users`
  modify access, and admin-only access, using a stub that returns descriptors.

## 7. Open questions

1. Where the lockfile is authored: committed in the repository and shipped, or
   generated by `update-deps` at first use? This design assumes it ships with
   the package and is rewritten by `update-deps`.
2. Which administrator-equivalent principals the ACL check should accept
   beyond `Administrators`, `SYSTEM`, and `TrustedInstaller`.
3. Whether to also offer `OSV` or PyPI advisory data in the update warning so
   security releases are distinguished from ordinary ones.
4. Whether package hooks should be refused under elevation unless the package
   directory also passes the trusted-location check (this design assumes the
   same check covers them only when the package lives in the gupkg tree).

## 8. Implementation order

1. Launcher: elevated branch with bundled interpreter, `-I -s -B`, and no
   `PYTHONPATH`; `is_elevated()`.
2. Trusted-location ACL check.
3. Lockfile format, `repair-deps`, and staged install with swap.
4. `update-deps` with plan and confirmation.
5. Update notification.
6. Tests alongside each step; README and CLI help updates at the end.

## 9. Implementation notes and deviations

- Modules: `gupkg.elevation` (detection and ACL policy), `gupkg.locked_deps`
  (lockfile, staged install, `repair-deps`, `update-deps`),
  `gupkg.dependency_notices` (advisory warning).
- **Launcher detection.** `gupkg.cmd` decides elevation with `fltmc.exe` and
  passes `--elevated` to `bootstrap.py`. Python independently checks the process
  token, so an elevated process started any other way (`GUPKG_PYTHON`,
  `python -m gupkg`) is refused unless it is the isolated bundled interpreter.
  `GUPKG_FORCE_ELEVATED=1` selects the elevated branch for testing; it can only
  make a launch stricter.
- **What is checked.** Every directory on `sys.path`, the package directories,
  the runtime directory and `python\Lib\site-packages`: no non-administrator may
  hold a write, delete, or permission-changing right or own the directory.
  Ancestors are checked for the rights that allow renaming or replacing the
  directory. The check cannot vouch for the launcher and bootstrap files that
  perform it; the root of trust is the admin-only install location.
- **Dependencies ship in the bundle.** `tools/build_standalone.py` installs the
  locked wheels into `python\Lib\site-packages` and writes the `._pth` file, so
  an elevated first run needs no installation. The lock also pins `pip`,
  because the directory swap replaces everything in `site-packages` and
  `repair-deps` runs `pip` from it. Update and repair run the bundled
  interpreter with `-I` and `--isolated`, ignoring `PIP_*` variables and user
  pip configuration.
- **Elevated relaunch.** `windows.relaunch_elevated` now starts `cmd.exe` with
  the package `gupkg.cmd` instead of `python -m gupkg`, which put the caller's
  current directory on the elevated import path. System tools (`powershell`,
  `cmd`) are resolved under `%SystemRoot%`; an elevated run never searches
  `PATH` for them.
- **Notification scope.** The warning is shown only for elevated runs, the only
  runs governed by the pins. The cache records the pins it was computed for.
  The setting is `[self] dependency_update_check = false` in the manager
  configuration, or `GUPKG_NO_DEPENDENCY_UPDATE_CHECK=1`.
- **Package hooks** are not refused under elevation (open question 4 resolved as
  proposed): they run as arbitrary package-local code, and a package folder
  that is not admin-only can therefore still run code as administrator. Install
  system-scope packages under an admin-only root.
- **Self-update.** `gupkg update` carries the bundled runtime and its
  dependencies into the new version. When the new version pins different
  dependencies, run `gupkg manager self repair-deps` once.
- **Lockfile provenance.** The shipped `gupkg-deps.lock` was resolved from PyPI
  for CPython 3.12 on Windows (amd64). Review it as you would any dependency
  change.
- **Manager configuration.** `gupkg-config.toml` decides package roots,
  executable folders and the registry source, so it is read only from the gupkg
  package folder (or an explicit `--config`); `%APPDATA%`, `GUPKG_HOME` and the
  working directory are no longer searched. An elevated run additionally
  requires the configuration's folder to pass the trusted-location check.
  The registry cache defaults to `App\registry` in the gupkg version folder, so an
  administrator's install keeps it in the same admin-only tree. An elevated
  `manager install` still refuses a cache in a folder non-administrators can
  modify, so a configured `[registry] cache` must also be admin-only.
- **Registry source.** The package registry is its own repository
  (`gupkg-registry`). It is cloned only with a `git.exe` from an admin-only
  folder (user-writable copies on `PATH` are skipped, never run), with HTTPS only,
  no hooks and user Git settings ignored; otherwise the GitHub archive is
  downloaded. Each registry is cached in `<cache>\<registry name>`.
