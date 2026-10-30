# Troubleshooting

Find the message you saw, then apply the fix. Every `gupkg` error is printed once, prefixed with `ERROR:`. With `--json` the
same error is `{"code": ..., "message": ..., "hint": ...}` in the result document, and the **code** below is stable. The exit
status is `2` when something about your input or configuration must change (including usage errors), `3` when the operation
failed, `4` for an unexpected internal error, `5` when a question was declined or input is needed, and `6` when the package
needs a newer gupkg.

## First steps for any problem

```bat
gupkg check                     :: drift, App fingerprint, registry fingerprint, unverified installs
gupkg pkg check <path>          :: lists every problem of a package definition at once
gupkg logs <package> --path     :: the newest log of the failed run
gupkg self status               :: is gupkg's own installation healthy?
```

## Messages and fixes

### Package definitions (`invalid-package`, exit 2)

| Message | What it means and what to do |
| --- | --- |
| `Unsupported pkg.toml format ('missing'): this gupkg reads only format 2; set format = 2.` | The file has no `format = 2` (a 0.17 file has none, or `format = 1`). 1.0 reads only format 2. |
| `Unsupported pkg.toml format (3): this package needs a newer gupkg ...` (exit 6) | The package is written for a newer gupkg than the one running. A package launcher then runs its bundled gupkg; otherwise update gupkg with `gupkg self check-updates` and `gupkg self update`. The same exit code is used for an unmet `requires_gupkg`. |
| `Unknown key 'x' in ...` | A typo, or a 0.17 spelling (`[origin]`, `[update]`, `[[bin]]`, `[[environment]]`, `targetPath`, `homepage`, `only_portable`). There are no aliases; the message names the key. |
| `version is not allowed: the version is the name of the version directory and the name is the package folder` | `pkg.toml` holds `version`, `localVersion` or `name`. Delete the line; the folder name is the identity. |
| `declares more than one [source]` / `[[origin.versions]]` | A version has exactly one `[source]`. Another version is another folder with its own `pkg.toml`. |
| `needs 'portable' in modes` | `[portable]` is declared for a package whose `modes` exclude `portable`. |
| `a SHA-256 ... must be exactly 64 hexadecimal digits` (`checksum-invalid`) | A malformed `sha256`. A malformed value makes the version invalid and is never skippable. |
| `App is missing and no way to recover it.` | The version has no `App` and no `download_url`, populate script or shipped files. Restore the files, or add a `[source]` that fills them. `gupkg check --fix` adds `Reinstall with: gupkg install <name>`. |
| `... is not valid: ...` (`invalid-package`) | A version directory failed validation; the problems follow. Fix them and run `gupkg pkg check <folder>`. |
| `the tracker is invalid: ...` | `_check_updates\pkg.toml` has a problem; run `gupkg pkg check <package folder>`. A tracker must have `[track]`; a version directory must not. |
| `Error loading TOML config ...` / a syntax error with a line | `pkg.toml` or `settings.toml` is not valid TOML. |

### Finding and selecting packages

| Message | Code | What it means and what to do |
| --- | --- | --- |
| `package 'x' was not found in the root or any registry` / `Path does not exist: ...` | `not-found` | Check the name or path. A name searches the root and the synced registries (`gupkg registry sync`). |
| `... is not a version directory: its name is reserved or not a safe folder name` | `not-found` | The folder is `current`, starts with `.` or `_`, or has an unsafe name. Give the version folder. |
| `'x' is not installed; use gupkg install x` | `not-installed` | `switch`, `uninstall` and `update` only act on installed packages; `update` never installs. |
| `'x' is not a local package, so there is nothing to check; use gupkg install x` | `not-installed` | `check-updates` needs a package folder with a tracker. |
| `'x' has no tracker to run` | `cannot-plan` | The package has no `_check_updates\pkg.toml`. Add one, or use `install` for a version that is already there. |
| The name matches several registries | `ambiguous` | Use `registry/name` (`main/firefox`). |
| `no version known` / `no valid version` | `no-version-known`, `no-valid-version` | Nothing newer is known locally or in the registry: run `gupkg check-updates x`. Or every known version is invalid: see `gupkg info x`. |
| `needs repair` | `needs-repair` | The active version's integration or `current` is broken. Run `gupkg install x` or `gupkg check x --fix`. |
| `registry entry 'x' was not found` | `not-found` | `gupkg link` names an entry that is not in any synced registry. |

### Scopes, modes and trust (`refused`, `untrusted-location`, `name-conflict`, exit 2 or 5)

A run has one scope: the mode of its process (an elevated process is system mode, any other is user mode) and one root. gupkg
never starts an elevated copy of itself, so none of these messages can be answered by a prompt; they say which shell to use.

| Message | What it means and what to do |
| --- | --- |
| `This package installs only system-wide. Run gupkg as administrator: use the Start Menu entry "gupkg manager (administrator)", or run in an administrator shell: gupkg <command> <name>` | The package's `modes` is `["system"]` and this run is a user run. Open an administrator shell (or the Start Menu entry) and run the command there. For an `update` of a package already installed in this root the message is `<name> <version> installs only system-wide, so it cannot be installed in this user root. The installed version <installed> keeps working.`: nothing to do, the installed version stays. |
| `This package installs only for a single user. Run gupkg without administrator rights.` / `This is a portable package: it installs only for a single user. ...` | The package is `["user"]` or portable and this run is elevated. Run the command from a normal shell. `uninstall`, `check` and `cleanup` are never refused this way. |
| `x cannot be installed: it would provide the shim 'y' (...), which the installed package 'z' already provides. Uninstall 'z' first, or use a package definition with a different name.` (`name-conflict`) | Two packages would put the same shim, shortcut or environment variable on the machine (`copyq` and `copyq-portable` do). Install only one of them, or uninstall the other first. Two targets of one call that clash are both refused. `check` reports a clash that is already on disk and changes nothing. |
| `<path> is not inside this scope's root (<root>)` | A path names a folder outside the run's root (for example the other mode's root). A run reads and writes only its own root. Run from the right shell, or give `--root`. A version directory outside the root (an unzipped distribution) is copied in; any other path is refused. |
| `the install record says user; this package was moved between scopes` (`broken`) | The package was installed by the other mode. Reinstall it with `gupkg install <name>` in this run's mode, or run gupkg in the mode that installed it. |
| `no install record (made by an older gupkg); reinstall with: gupkg install <name>` (`broken`) | A package folder without `.gupkg\package.toml`. gupkg changes nothing in it by itself; `gupkg install <name>` rebuilds it and writes the record, and `install <name> --refresh` rebuilds the App too. |
| `--root X is not admin-only, so it cannot be the system root: ...` | In an elevated run `--root` must be a folder only Administrators can change. |
| `--settings is not accepted in system mode: an elevated run reads only admin-only files` | An elevated run takes no `--settings`. Put the setting in the system root's `.gupkg\settings.toml`. |
| `settings schema 3 is no longer read; change it to schema = 4` | An old settings file. Change `schema` to 4; `[integration.system]` and `[integration.user]` become one `[integration]` table, the mode setting and the portable root setting are gone. |
| `Refusing <folder> in an elevated run: ...` (`untrusted-location`) | An elevated run never reads or runs a folder that a standard user can change. Use an admin-only folder, or run in a normal shell on a user root. |
| `an elevated gupkg must be installed in an admin-only root; run the system gupkg ...` | The gupkg tree an administrator started is user-writable (a user install, a bundled copy in Downloads, a source checkout). Run the gupkg installed in `C:\opt` instead, or install it with `gupkg-bootstrap.cmd -Scope system` from an administrator shell. |
| `This gupkg is installed in <root>, but the user root of this run is <other>, so `gupkg update gupkg` would look for it there. Run it from an administrator shell.` (`self update`, `self repair`, `self check-updates`) | These act on the root of the run. A system gupkg is updated from an administrator shell; a gupkg installed in another root, from a run on that root. |
| `<name> <version> is older than the active version <active>. Install it anyway?` (exit 5 with `--no-input`) | Answer yes, or pass `--allow-downgrade` (or `--yes`). `gupkg switch <name> <version>` goes back without asking. |
| `<name> has other version directories that are broken or not supported: ...` (exit 5 with `--no-input`) | Fix or delete those folders, or pass `--allow-other-broken-versions` (or `--yes`) to install anyway. |
| `The bundled Python is missing: ...` or an `ImportError` in gupkg itself | The install is damaged. `gupkg self status` shows which files differ from `MANIFEST.sha256`; install the release ZIP again with `gupkg-bootstrap.cmd`. gupkg never repairs or downloads its runtime. |
| A mode prefix in front of a name (`a target no longer names a mode`) | Remove the prefix. The mode of a run is the mode of its shell. |
### Checksums (exit 2 or 3)

| Code and message | What it means and what to do |
| --- | --- |
| `checksum-missing`: `<url> has no SHA-256. Add one to the version's pkg.toml, or run this command again with --skip-checksum` | The version declares no hash and the tracker found none. Pin one (`sha256` in `[source]`, or `gupkg pkg pin` after a skipped install) or add `--skip-checksum` to that one call. `gupkg-manager` asks per package, or follows `[manager] checksum_missing`. |
| `checksum-missing` with `skippable: false` (`--skip-checksum works only for https:// addresses`) | The address is plain `http://`. It is never skippable: add a `sha256`. |
| `checksum-mismatch` | The download is not what the version declares. **Nothing overrides this.** Retry once (a corrupt or interrupted download); if it fails again the file changed upstream or was tampered with: re-run `gupkg check-updates` for a newer version, or fix the declared hash after you verified the file yourself. |
| `checksum-invalid` | A malformed hash, flag value or receipt record. The `version` and `field` are in the error. |
| `registry-fingerprint-mismatch` | A distribution folder (or a registry package) is not the version the registry fingerprints. Do not install it; get the distribution again from its source. |
| `checksum-discovery` | The tracker's `[source.checksum]` found no digest for the version (a missing sums file or entry, a fetch that failed). The version is written without `App` (D46); report it to the package author, or pin a hash yourself. |
| `<name> <version> runs vendor installer, and hooks.allow_vendor_installers is false` / a vendor installer without a pinned hash | A vendor installer runs only from a download that matches a pinned `sha256`, and only when `[hooks] allow_vendor_installers` is not `false`. It is never skippable. |

### Populating, installing and updating (exit 3)

| Code and message | What it means and what to do |
| --- | --- |
| `app-build-failed`: `... already holds an App; use --refresh to rebuild it` | Install with `--refresh` to rebuild an `App` that can be rebuilt. A shipped `App` is never replaced. |
| `app-build-failed`: `the populate step of x 1.0 left an empty App` | A populate script or `[[prepare]]` step produced nothing. Run it by hand; read the log of the run. |
| `app-build-failed`: `... ships its App and has nothing to populate it from` | A shipped version with no `App` and no way to recover it. Restore the files. |
| `unpack-failed` | The archive was refused or damaged: an entry that leaves `App`, a link, a device or stream name, encryption, or more than the entry, size or nesting limits (the message names it); or `require` files are missing. Do not extract it by hand into `App`. |
| `prepare-failed` | A `[[prepare]]` step failed (`rename` and `copy` need exactly one match and never overwrite). The staged `App` was deleted; nothing is half-installed. |
| `track-failed` / `network-error` | The tracker could not read its source (offline, a changed page, a rate limit; set `GITHUB_TOKEN` for GitHub). Nothing was written. A regular-expression that exceeds its deadline is reported with the pattern and address. |
| `hook-error` | A hook raised, imported something it may not, or its folder is not admin-only in an elevated run. Hooks may import only the standard library, what the bundle ships, `gupkg.hooklib` and their own `pkg.local` folder. |
| `installer-failed` | The vendor installer failed or timed out; the stage is discarded. |
| `integrate-failed`: `These effects were not applied: ...` | The files are in place but a shortcut, shim, variable or `PATH` entry failed (listed). Fix the cause and run `gupkg install <name>` again. |
| `effect-error` / `portable-error` | An effect or portable data hand-over was refused (for example a link in the way). The message names the path. |
| `package-busy`: `x is being changed by ... Wait for it to finish and run the command again.` | Another gupkg run holds the package. If it is gone, the next run removes the stale lock (`<package>\.gupkg\locks\package.lock`) once its process has ended. |
| `io-error` | A file operation failed (permissions, a file in use, a path that is too long). The message names it; enable long paths and keep roots short for deep packages. |
| `io-error`: `<file> is in use by another program. The package is uninstalled: ... its folder ... is still there.` (`uninstall --purge`) | The integration and `current` are already removed; only the folder is left. Close the program, then run `gupkg uninstall <name> --purge` again. |
| `io-error`: `the Finish step failed: ... The package is now on <new>: `current` points to <new>; version <old> is kept.` (`update`, `install`, `switch`) | A step after `current` moved failed. `current` is not rolled back. `gupkg check <name>` shows the state; `gupkg switch <name> <old>` returns to the old version. |
| `check`: `<name> has versions ... but no active version (`current` is missing) and the shim ... is still in `bin`.` | The `current` junction was deleted. `gupkg install <name>` activates a version again (`gupkg switch` and `check --fix` refuse a package with no active version). `gupkg self status` says the same of gupkg itself: run `gupkg install gupkg`, or the bootstrap again if gupkg does not start. |
| `needs-real-run` | A `--sandbox` run met a step that needs a real process (not git or 7-Zip). Run without `--sandbox`. |
| A new command or variable is "not found" | Windows gives new environment values only to new processes. Open a new terminal. |
| `fatal: ... Filename too long` while cloning (git packages) | `git config --global core.longpaths true`, and keep the package root path short. |

### Distribution folders and launchers

| Message | What it means and what to do |
| --- | --- |
| `... exists and differs from <distribution>; it was left alone.` (`DistributionRefused`) | The root already holds a different copy of that version. Compare it; delete it deliberately and install again. |
| `... is a link or junction; a distribution folder must contain only real files` / `holds more than N files` / `is nested deeper than N folders` | A distribution folder is copied only if it is plain and bounded. Export a clean one with `gupkg pkg export`. |
| `... changed while it was being copied; nothing was installed` | The source changed during the copy; run again. |
| `[gupkg] gupkg is not installed, and none is bundled with this package.` (exit 9009) | The folder carries no `gupkg\`, the root has no usable `.gupkg\gupkg-location` (the launcher prints `Skipped ...` and why) and no `gupkg.cmd` or `gupkg.exe` is on `PATH`. Run `gupkg-bootstrap.cmd` once. |
| `[gupkg] Cannot tell whether this run is elevated, so it is refused.` | The launcher could not read the integrity level (`whoami`); run it from a normal or an elevated shell with `%SystemRoot%\System32` intact. |
| `[gupkg] Refusing to run elevated: something this launcher would run can be changed by a standard user ...` | An elevated launcher found a folder a standard user can change. Install from the installed system gupkg instead: `gupkg install <folder>` in an administrator shell. An elevated launcher skips a `PATH` entry that a standard user can change and names it (`Skipped PATH entry ...`); but when the bundled gupkg or the gupkg named by `gupkg-location` cannot be trusted, it refuses to run (exit 1) and does not go on to `PATH`. |
| `[gupkg] The gupkg ... cannot handle this package; trying the next one.` | Exit code 6 from the gupkg the launcher tried first: it went on to the next one (bundled copy, `gupkg-location`, `PATH`). Update gupkg (`self check-updates`, `self update`). |
| `gupkg-bootstrap`: `No version directory ... was found` / `refusing an existing C:\opt that is not admin-only` | The ZIP or folder is not a gupkg distribution, or `C:\opt` was created by someone else. Fix its permissions or use the right ZIP. |
| A package launcher misbehaves with `%`, `^` or `"` in an argument | The launchers forward arguments through `cmd.exe`, which expands `%` and treats `^` and quotes specially. Run `gupkg ...` directly for such arguments, and avoid these characters in launcher arguments. |

### Settings and registries

| Message | What it means and what to do |
| --- | --- |
| `settings` errors (`unknown key`, `must be one of ...`) | `settings.toml` failed the schema; `gupkg settings show` names the layer, `settings path` the file. `settings set` validates before it writes. |
| `Registry synchronization failed: ...` | The download or validation failed; the previous catalogue stays active. Check network access and the `[[registry]]` source; `--offline` uses the cache. |
| `No validated registry cache is available` | Run `gupkg registry sync`, or drop `--offline`. |
| `Registry selectors cannot be paths` | Give the registry name, such as `vscode`, not a folder. |
| A registry package is skipped | Validation is per package; the sync prints which one and why. The others are usable. |

### The manager and UI programs

| Message | What it means and what to do |
| --- | --- |
| The manager shows no system packages, or the title says `user` | `gupkg-manager` manages the scope of its own process. Start it from an administrator shell, or from the Start Menu entry "gupkg manager (administrator)", for the system root; a normal shell shows the user root. There is no key that switches. |
| `gupkg: error: --settings, --root and --sandbox are not passed on to gupkg-manager` | `gupkg --root DIR manager` is refused. The programs take no root option: start them from the gupkg installed in that root, or give `gupkg-manager --settings PATH` (or `gupkg-tui --settings PATH`) a file with `[roots].user`. |
| `update --all` exits 5 with nothing changed | There is no terminal and no `--yes`, or `--json` was given. Add `--yes`. |
| A package is refused with `checksum-missing` in a schedule | `ask` behaves as `refuse` without a terminal. Pin a hash, or set `[manager] checksum_missing = "skip"` knowingly. |
| A run ends without `run_end` | The call died without a result; read the log of the run (`gupkg logs <name> --path`, or `<root>\.gupkg\logs`). |
| A call that does not exit after its result | It is stopped after 15 seconds and its result is used with a warning; a cancelled call that does not stop is killed after 30 seconds. |
## A security warning when running install.cmd, or SmartScreen and Smart App Control

A distribution that you downloaded with a browser has the Mark of the Web on the ZIP. Extracting it with Explorer
(or "Extract All") puts that mark on **every** extracted file; extracting with `tar -xf` or with the bootstrap script puts it
on none, and `Unblock-File` on the ZIP before extracting (or ticking Unblock in the ZIP's Properties) clears it.

| What you see | What it means and what to do |
| --- | --- |
| "Open File - Security Warning: The publisher could not be verified" when you double-click `install.cmd` | The first script of a freshly extracted download is held until you click **Run**. Nothing runs before that. Click Run once; what gupkg writes afterwards carries no mark (the copy into a root drops it, and so does `check-updates`), so later launchers do not ask. To avoid the question, unblock the ZIP before extracting it. |
| Windows protects your PC (SmartScreen) or blocks an `.exe` of the bundled gupkg (Smart App Control) | The bundled `python.exe` and the shims are marked like everything else in the extracted folder. Unblock the ZIP **before** extracting it, or run `gupkg-bootstrap.cmd` (it unpacks without marks). On a machine with Smart App Control turned on, an unsigned program can still be refused; this has not been verified on such a machine (manual check 033-7 in tests/manual_smoke.md), so report what you see. |

## Cleaning up

`gupkg cleanup` removes old `App` folders, receipts, work folders, registry trees and logs, and never runs on its own. By hand,
when no `gupkg` is running:

- `<package>\.gupkg\work\` &mdash; disposable download and staging space.
- `<package>\.gupkg\locks\` &mdash; a stale lock from a killed run (the next run removes it too).

Never delete the version folder `current` points to, or `<package>\.gupkg\package.toml` (the install record that `uninstall`
uses to remove exactly what was installed).

## Still stuck

Re-run with `--json` and keep the output: it contains the exact status, exit code, warnings and errors. Add `gupkg logs <name>
--path` and the log it names, the `pkg.toml` (with secrets removed), and the output of `gupkg --version`.
