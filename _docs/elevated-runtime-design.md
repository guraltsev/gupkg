# Design: trusted runtime for elevated gupkg runs

Status: implemented. Where the implementation differs from the original
proposal, the text below has been updated and the difference is listed in
section 9.

> **Superseded in part by issues 016, 019 and the 1.0 rehaul.** gupkg no longer installs, repairs or
> checks dependencies at run time, and it never probes for another Python:
> `repair-deps` / `update-deps`, the dependency advisory notice, `gupkg.dependencies`,
> `gupkg.locked_deps` and `gupkg.dependency_notices` were removed. The dependency lock lives in
> `tools/gupkg-deps.lock` and is maintained with `tools/update_deps.py` and `tools/check_deps.py`; the
> release build installs it (the build machine's pip installs the wheels; pip is not in the bundle and not in
> the lock), a build probe checks the staged tree, and `gupkg self status` checks the installed files against
> `MANIFEST.sha256`. The elevated trust rules below (bundled interpreter only, isolated mode, admin-only
> directories) still apply unchanged, and 1.0 extends them to package launchers (design 21.3). Sections 3.4 to
> 3.6, 4 to 7 and the `manager`/`pip` commands they mention are kept as the record of the original design;
> section 9 says what is true now.
>
> **Amended by issue 035 (D47–D55, owner-confirmed 2026-10-28).** gupkg never relaunches itself elevated: an
> elevated process is in system mode. It runs only an admin-only gupkg tree and acts only on the admin-only system
> root. Section 10 says what that means, and gives the launcher `PATH` rule.

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
2. Starts the interpreter with `-I -s -B -S`: isolated mode (ignores `PYTHON*`
   variables, user site, and the script directory on `sys.path`), no user
   site, no `.pyc` writing, and no `site` import (so no `sitecustomize` and no
   `.pth` file runs before the trust check; issue 018).
3. Does not export the caller's `PYTHONPATH`. The embeddable package's
   `python312._pth` file restricts the search path to the lines it lists. Since
   issue 018 these are only `python312.zip`, `.` (the python folder) and
   `Lib/site-packages`, with no `..` and no `import site` line (a `import site`
   line would override `-S`); `tools/fetch_runtime.py` writes it. The python
   folder, `python.exe`, the `._pth` file and `site-packages` are all checked
   admin-only in an elevated run.
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
| Interpreter flags | default | `-I -s -B -S` |
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

1. Launcher: elevated branch with bundled interpreter, `-I -s -B -S`, and no
   `PYTHONPATH`; `is_elevated()`.
2. Trusted-location ACL check.
3. Lockfile format, `repair-deps`, and staged install with swap.
4. `update-deps` with plan and confirmation.
5. Update notification.
6. Tests alongside each step; README and CLI help updates at the end.

## 9. Implementation notes and deviations

- Modules: `gupkg.selfmgmt.elevation` (detection and ACL policy). The dependency modules of the first
  implementation are gone (issue 016), and so is the elevated relaunch (issue 035, D49).
- **Launcher detection.** Since issue 018, `gupkg.cmd` no longer probes for
  elevation (no `fltmc.exe`, no `--elevated`, no `GUPKG_FORCE_ELEVATED`).
  `bootstrap.py` checks the process token (`GUPKG_ELEVATED=1` can only make a run
  stricter), and every run, elevated or not, must be the isolated bundled
  interpreter (`enforce_bundled_runtime`); only the admin-only directory checks
  are limited to elevated runs.
- **What is checked.** Every directory on `sys.path`, the package directories,
  the runtime directory and `python\Lib\site-packages`: no non-administrator may
  hold a write, delete, or permission-changing right or own the directory.
  Ancestors are checked for the rights that allow renaming or replacing the
  directory. The check cannot vouch for the launcher and bootstrap files that
  perform it; the root of trust is the admin-only install location. The python
  directory is trusted at interpreter start: `python.exe`, `python312.dll` and the
  `.pyd` files load from it before any Python code runs, so the install location
  must be admin-only before launch, and the elevated check is a refusal gate for
  later imports, not a defence against a hostile python directory.
- **Dependencies ship in the bundle.** `tools/build_standalone.py` installs the locked wheels into
  `python\Lib\site-packages` (with `--require-hashes`, cross-targeted at the bundled CPython by the build
  machine's pip) and writes the `._pth` file, so an elevated first run needs no installation. The bundle has
  no pip, setuptools or ensurepip (issue 019), and nothing is installed at run time.
- **No relaunch.** gupkg contains no code that starts an elevated copy of itself (issue 035, D49): no
  `gupkg.relaunch`, no elevated child, no `--relaunched`, `--batch-confirmed` or `--stop-file`. An elevated run is a
  process that was started elevated (a shell run as administrator, or the Start Menu shortcut "gupkg manager
  (administrator)"). System tools (`powershell`, `cmd`) are resolved under `%SystemRoot%`; an elevated
  run never searches `PATH` for them.
- **Package hooks** run as trusted package-local code. Since 1.0 (design 8.6, D19) an elevated run refuses a
  version directory or `pkg.local` that is not admin-only before importing or starting anything; install
  system packages under the admin-only system root.
- **Self-update.** gupkg is an ordinary package of itself: `gupkg self check-updates` and `gupkg self update`
  prepare and activate a new release, and the populate script copies the verified bundled tree into `App`
  (nothing of the old runtime is carried over).
- **Lockfile provenance.** The shipped `gupkg-deps.lock` was resolved from PyPI
  for CPython 3.12 on Windows (amd64). Review it as you would any dependency
  change.
- **Settings.** The layered `settings.toml` (design section 11, schema 4) has the layers defaults, `$App`, the
  run's root and `--settings`. An elevated run is in system mode: it has no user layer to ignore, it refuses
  `--settings`, and it refuses a root layer whose folder is not admin-only.
- **Registry source.** The package registry is its own repository (`gupkg-registry`). It is cloned only with a
  trusted `git` from an admin-only folder (user-writable copies on `PATH` are skipped, never run), with HTTPS only,
  no hooks and user Git settings ignored; otherwise the repository archive is downloaded. Each registry is
  cached in `<root>\.gupkg\registry\<registry name>`, and a system run uses only the system root's cache.

## 10. One scope per run (issue 035)

This section is the elevated side of design sections 6.1, 9.4, 9.6 and 21.3 (decisions D47 to D52, confirmed by the
owner on 2026-10-28).

- **There is no relaunch.** An elevated process is in system mode, and the mode comes from the process token
  alone. gupkg never starts an elevated copy of itself. To act in system mode, start gupkg or the manager
  elevated: in an administrator shell, or through the Start Menu shortcut "gupkg manager (administrator)", whose
  link carries the "run as administrator" flag. Windows asks for UAC, and gupkg contains no elevation code.
- **An elevated process runs only an admin-only gupkg tree** (`enforce_bundled_runtime`, unchanged). A
  user-installed, bundled-in-Downloads or source gupkg started elevated refuses to start, with exit code 2.
- **An elevated process acts only on the admin-only system root.** The root comes from the system column of the
  fallback chain in design section 6.1: `--root DIR` (which must be admin-only, else exit code 2), the root of the
  installed system gupkg that runs, `[roots].system` of `$App\settings.toml`, then the built-in `C:\opt`. The
  system mode names no user location: `Scope` has no field that could hold a user root, user settings or a user
  cache, and the system mode refuses `--settings`.
- **Checks inside the system root stay** (D30, spike S1): the admin-only checks on hooks, locks, the work folder,
  the HTTP cache and trusted `git`. The engine reaches them through `scope.require_trusted(path)`, which does
  nothing in user mode.
- **The launcher `PATH` rule** (issue 035 §4.9, owner's answer Q7 (b)). A package launcher tries the bundled gupkg,
  then the gupkg named by `<root>\.gupkg\gupkg-location`, then `PATH`. When elevated, step 3 is limited:
  - the process `PATH` is scanned in order, and an entry is used only if its folder passes the admin-only check; a
    user-writable entry is skipped and named in the output;
  - the launcher never reads the machine `PATH` from the registry, so the registry rule (D18) stays whole;
  - what must be admin-only is the whole chain of what will run: the shim, its `.config.toml`, the target it names,
    and that target's gupkg tree, recursively, with the existing ACL check (D30). It fails closed;
  - accepted consequence: an entry from an administrator's own user `PATH` is used when its folder happens to be
    admin-only. Only an administrator can change such a folder, so threat model T1 is unaffected.
- **The elevated launcher's ACL check** covers the root, the package and version directories, `.gupkg\` with the
  `gupkg-location` file, and the tree of whichever gupkg the order selects (design section 21.3).
