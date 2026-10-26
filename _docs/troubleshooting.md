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
| `Unsupported pkg.toml format ('missing'): this gupkg reads only format 2; set format = 2.` | The file has no `format = 2` (a 0.17 file has none, or `format = 1`). 1.0 reads only format 2; convert 0.17 files once with `gupkg migrate package <path>` (see [Migrating](operations.md#migrating-from-017-and-018)). |
| `Unsupported pkg.toml format (3): this package needs a newer gupkg ...` (exit 6) | The package is written for a newer gupkg than the one running. A package launcher then runs its bundled gupkg; otherwise update gupkg with `gupkg self check-updates` and `gupkg self update`. The same exit code is used for an unmet `requires_gupkg`. |
| `Unknown key 'x' in ...` | A typo, or a 0.17 spelling (`[origin]`, `[update]`, `[[bin]]`, `[[environment]]`, `targetPath`, `homepage`, `only_portable`). There are no aliases; the message names the key. |
| `version is not allowed: the version is the name of the version directory and the name is the package folder` | `pkg.toml` holds `version`, `localVersion` or `name`. Delete the line; the folder name is the identity. |
| `declares more than one [source]` / `[[origin.versions]]` | A version has exactly one `[source]`. Another version is another folder with its own `pkg.toml`. |
| `needs 'portable' in modes` | `[portable]` is declared for a package whose `modes` exclude `portable`. |
| `a SHA-256 ... must be exactly 64 hexadecimal digits` (`checksum-invalid`) | A malformed `sha256`. A malformed value makes the version invalid and is never skippable. |
| `App is missing and no way to recover it.` | The version has no `App` and no `download_url`, populate script or shipped files. Restore the files, or add a `[source]` that fills them. |
| `... is not valid: ...` (`invalid-package`) | A version directory failed validation; the problems follow. Fix them and run `gupkg pkg check <folder>`. |
| `the tracker is invalid: ...` | `_check_updates\pkg.toml` has a problem; run `gupkg pkg check <package folder>`. A tracker must have `[track]`; a version directory must not. |
| `Error loading TOML config ...` / a syntax error with a line | `pkg.toml` or `settings.toml` is not valid TOML. |

### Finding and selecting packages

| Message | Code | What it means and what to do |
| --- | --- | --- |
| `package 'x' was not found in any root or registry` / `Path does not exist: ...` | `not-found` | Check the name or path. A name searches the roots and the synced registries (`gupkg registry sync`). |
| `... is not a version directory: its name is reserved or not a safe folder name` | `not-found` | The folder is `current`, starts with `.` or `_`, or has an unsafe name. Give the version folder. |
| `'x' is not installed; use gupkg install x` | `not-installed` | `switch`, `uninstall` and `update` only act on installed packages; `update` never installs. |
| `'x' is not a local package, so there is nothing to check; use gupkg install x` | `not-installed` | `check-updates` needs a package folder with a tracker. |
| `'x' has no tracker to run` | `cannot-plan` | The package has no `_check_updates\pkg.toml`. Add one, or use `install` for a version that is already there. |
| The name matches several registries | `ambiguous` | Use `registry/name` (`main/firefox`). |
| `no version known` / `no valid version` | `no-version-known`, `no-valid-version` | Nothing newer is known locally or in the registry: run `gupkg check-updates x`. Or every known version is invalid: see `gupkg info x`. |
| `needs repair` | `needs-repair` | The active version's integration or `current` is broken. Run `gupkg install x` or `gupkg check x --fix`. |
| `registry entry 'x' was not found` | `not-found` | `gupkg link` names an entry that is not in any synced registry. |

### Modes, elevation and trust (`refused`, `untrusted-location`, exit 2 or 5)

| Message | What it means and what to do |
| --- | --- |
| `This call needs administrator rights, and this run does not know where its own gupkg is ...` | gupkg cannot place itself (a source checkout). Install it with the bootstrap. |
| `System installs need the system gupkg. This gupkg runs from ..., and ... can be changed by standard users` | A user-writable or bundled gupkg never starts an elevated copy of itself. Install gupkg system-wide (`gupkg-bootstrap.cmd -Scope system` from an administrator shell) and run that one. |
| `System mode needs administrator rights. Relaunch gupkg elevated for this call? ... Pass --yes` (exit 5) | A question under `--no-input`, or declined. `--yes` answers it. `Elevation was declined` and `The elevation prompt was not accepted` mean the UAC prompt was refused; nothing changed. |
| `The elevated gupkg is not started: ... is not allowed in a command that runs with administrator rights` | A target or option value holds `& \| < > ^ " % !`, a backtick, a control character or starts with `-`. Rename the folder, or run gupkg from an administrator shell. |
| `Stopped waiting for the elevated gupkg after an interrupt; it may still be running` | Ctrl-C reached the waiting parent. The elevated child stops after its current package (stop marker); read its events file in the system root's `.gupkg\logs`. |
| `The elevated gupkg was started, but waiting for it failed` (`process-failed`) | Read the system root's logs: the child may still be running or may have finished. |
| `Refusing mode user in an elevated run: an elevated gupkg acts only on system packages` / `Refusing <folder> in an elevated run: ...` (`untrusted-location`) | An elevated gupkg never acts on a user or portable root and never reads a folder that a standard user can change. Use a normal shell, or an admin-only folder. |
| `--mode x contradicts the qualifier of 'user:name'` / `Unknown install mode` | Do not combine a `user:` or `system:` prefix with a different `--mode`. Modes are `system`, `user`, `portable`. |
| `The portable root ... is the system root / lies inside the system root / is the user root` | A portable root is never machine-wide and never the user root. Choose another folder. |
| `No portable root is configured and none was given ...` (exit 5) | Set `[roots].portable` in `settings.toml`, or give `--mode portable` a folder shaped `<name>\<version>\pkg.toml`. |
| `The roots differ between the system settings and the settings of a normal run` | A user layer changes a root. Make the system layer alone name the roots. |
| `... is outside every root and is not a version directory` | A folder you named is not a version folder (no `pkg.toml`). Name the version folder. |
| `<name> <version> is older than the active version <active>. Install it anyway?` (exit 5 with `--no-input`) | Answer yes, or pass `--allow-downgrade` (or `--yes`). `gupkg switch <name> <version>` goes back without asking. |
| `<name> has other version directories that are broken or not supported: ...` (exit 5 with `--no-input`) | Fix or delete those folders, or pass `--allow-other-broken-versions` (or `--yes`) to install anyway. |
| `--in-place with system mode needs an admin-only folder` | Copy the distribution into a root (the default) or place it in an admin-only folder. |
| `Refusing to run elevated: ... is not admin-only` | gupkg's own folders can be changed by standard users, so an elevated run refuses. Run the `icacls` command in the message from an elevated prompt, or install gupkg under an admin-only folder. |
| `The bundled Python is missing: ...` or an `ImportError` in gupkg itself | The install is damaged. `gupkg self status` shows which files differ from `MANIFEST.sha256`; install the release ZIP again with `gupkg-bootstrap.cmd`. gupkg never repairs or downloads its runtime. |
| `mixed-modes`: `x was not run: this call needs an elevated gupkg for its system targets.` | A call with system and user targets relaunched elevated for the system ones and left the rest out (exit 2 for them). Run the user and portable targets in a separate call, or use `gupkg-manager`, which groups them. |

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
| `needs-real-run` | A `--sandbox` run met a step that needs a real process (not git or 7-Zip). Run without `--sandbox`. |
| A new command or variable is "not found" | Windows gives new environment values only to new processes. Open a new terminal. |
| `fatal: ... Filename too long` while cloning (git packages) | `git config --global core.longpaths true`, and keep the package root path short. |

### Distribution folders and launchers

| Message | What it means and what to do |
| --- | --- |
| `... exists and differs from <distribution>; it was left alone.` (`DistributionRefused`) | The root already holds a different copy of that version. Compare it; delete it deliberately and install again. |
| `... is a link or junction; a distribution folder must contain only real files` / `holds more than N files` / `is nested deeper than N folders` | A distribution folder is copied only if it is plain and bounded. Export a clean one with `gupkg pkg export`. |
| `... changed while it was being copied; nothing was installed` | The source changed during the copy; run again. |
| `[gupkg] gupkg is not installed, and none is bundled with this package.` (exit 9009) | The root has no `.gupkg\gupkg.cmd` and the folder carries no `gupkg\`. Run `gupkg-bootstrap.cmd` once. |
| `[gupkg] Cannot tell whether this run is elevated, so it is refused.` | The launcher could not read the integrity level (`whoami`); run it from a normal or an elevated shell with `%SystemRoot%\System32` intact. |
| `[gupkg] Refusing to run elevated: something this launcher would run can be changed by a standard user ...` | An elevated launcher found a folder a standard user can change. Install from the installed system gupkg instead: `gupkg install <folder> --mode system` in an administrator shell. |
| `The installed gupkg cannot handle this package; using the gupkg bundled with it.` | Exit code 6 from the installed gupkg: the launcher finished with the bundled copy. Update gupkg (`self check-updates`, `self update`). |
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
| A UI program exits with `refuse an elevated shell` | `gupkg-tui` and `gupkg-manager` are standard-user programs. Start them from a normal shell; they ask for administrator rights themselves, once per batch. A scheduled task with "highest privileges" is refused too. |
| `the system gupkg is needed` for system targets | This gupkg is a user or bundled copy and never asks for administrator rights. Use the installed system gupkg. |
| `update --all` exits 5 with nothing changed | There is no terminal and no `--yes`, or `--json` was given. Add `--yes`. |
| A package is refused with `checksum-missing` in a schedule | `ask` behaves as `refuse` without a terminal. Pin a hash, or set `[manager] checksum_missing = "skip"` knowingly. |
| `child-no-result` / a run ends without `run_end` | The elevated or child call died without a result; check the system root's `.gupkg\logs`. |
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
- `pkg.toml.bak` files that `gupkg migrate` kept.

Never delete the version folder `current` points to, or `<package>\.gupkg\package.toml` (the install record that `uninstall`
uses to remove exactly what was installed).

## Still stuck

Re-run with `--json` and keep the output: it contains the exact status, exit code, warnings and errors. Add `gupkg logs <name>
--path` and the log it names, the `pkg.toml` (with secrets removed), and the output of `gupkg --version`.
