# gupkg 1.0 rehaul: target design

Date: 2026-10-22
Status: proposed (tracking issue: `_issues/issue020-rehaul-epic.md`)
Supersedes: the manager-mode and `_bootstrap` parts of issues 012 and 013.

Issues 016–019 were filed separately on the same day. They cover how gupkg
itself is built, shipped and started:

- **016:** one self-contained release ZIP;
- **017:** shortcuts through COM;
- **018:** no system-Python autodetection;
- **019:** no pip in the bundle.

Their decisions stand. They form **stage 0** of the rehaul and land before
issue 021. Section 20 describes how they fit this design and what this
design adds on top of them.

This document is the single description of where gupkg is going. The open
issues 020–033 implement it in order. Where this document and older issues or
user documentation disagree, this document wins. The user documentation
(`README.md`, `_docs/*.md`) describes the *current* behaviour until each issue
lands and updates it.

Contents

1. Why a rehaul
2. Goals and non-goals
3. Vocabulary
4. On-disk layout
5. Package state model (local, registry, both)
6. Install modes: system, user, portable
7. `pkg.toml` format 2
8. Built-in services (replacing hook scripts)
9. Checksums and trust
10. Command-line interface
11. Settings file
12. Architecture and module boundaries
13. Execution engine, phases, receipts, uninstall
14. Events, console output, and logs
15. The manager program (`gupkg-manager`)
16. Testability
17. Registry 2
18. Migration
19. Decisions taken and open questions
20. Runtime and distribution (issues 016–019)
21. Bundled mode, package launchers, and bootstrap

---

## 1. Why a rehaul

gupkg works, but it grew by accretion. The analysis behind this document
(2026-10-22) found the following structural problems. Each one is
reproducible in the 0.17.0 code.

**There is no package state model.** Five functions work out "installed",
"local", "bootstrap" or "registry" for themselves, each with its own fallbacks
and errors:

- `layout.resolve_input_path`
- `layout.inspect_current`
- `manager._managed_target`
- `commands.describe_package`
- `commands._package_record`

As a result:

- `_bootstrap` sorts as the newest version (`core.compare_package_versions`).
- A root holding `_bootstrap` plus one version is rejected for having
  "multiple version directories".
- A `_bootstrap` that is not an update bootstrap can become `current` and is
  then reported as broken.

**Two modes install to different places.** `gupkg install <path>` uses the
hard-coded scope defaults. `gupkg manager install` and `manager update` use the
configured `[bin]`. The manager TUI's "open package" action drops back into
package mode. So the same package gets different wrapper and PATH destinations
depending on how it was started. The word `auto` means four different things
across commands.

**The registry only copies a seed.**

- `manager install` copies a seed only if no directory of that name exists, so
  an old local seed silently wins.
- The registry is never consulted for updates.
- An installed package evolves forever from its own copied `pkg.toml`.
- Registry and inventory are never joined, so "in the registry and also
  local" has no defined meaning.

**`_bootstrap` is overloaded.** It is a template, a version-discovery
mechanism and the only distribution form. A version created from it records
neither its source URL nor its hash. That version therefore cannot rebuild its
own `App` reproducibly.

**Checksums are inconsistent.**

- A static `[origin]` without `checksum` is downloaded unverified, with no
  warning.
- 35 of 79 registry manifests set `ignore_checksum = true`. Several of them
  discard digests that GitHub does publish.
- No observed hash is ever recorded.

**About 5,200 lines of hook code are mostly copy-paste.** The 62 hook files
repeat these patterns again and again:

- 9 near-identical page scrapers
- 5 byte-identical redirect resolvers
- 6 hand-rolled GitHub API clients
- 6 copies of "7z x, then flatten one wrapper folder"
- 13 different ways of finding a checksum
- 5 different version comparators

The copies are inconsistent with each other, and some are wrong. For example,
several ignore `appReady`, so a missing `App` is never repaired.

**Portable apps lose their settings on every update.** Every update builds a
fresh `App`. Tixati, SumatraPDF, Notepad++, HWiNFO, CrystalDisk*, Rufus and
others keep their settings inside `App`, so each update wipes those settings.
Three packages delete their portable markers to avoid this. `only_portable`
and the `-portable` name suffix contradict each other in two packages
(tixati and sqlitebrowser).

**Output is scraped, never modelled.**

- Workflows print to a process-global redirected `sys.stdout`.
- Front ends split those lines on `\n` and `\r`.
- Phases are plain text, so no front end can tell a phase header from a detail
  line.
- Bulk updates throw per-package output away.
- Git and pip write straight over the TUI.
- gupkg writes no log file anywhere.
- The package TUI shows the verdict at the top and the result at the bottom.

**Nothing on the system boundary can be replaced in tests.** Junctions,
`winreg`, PowerShell shortcuts, `WM_SETTINGCHANGE`, elevation, `urlopen` and
`git` are all called directly. The tests patch module attributes, and
registry-seed installation has no end-to-end test.

**Lifecycle gaps.**

- There is no uninstall.
- Nothing prunes old versions, receipts, registry trees or logs.
- Shortcuts and wrappers removed from a package are never deleted.
- `self update` is a repair.

## 2. Goals and non-goals

Goals:

1. **A Scoop-like, Python-based package manager for Windows** with one command
   grammar, in which a package name and a package path are equally good
   targets.
2. **First-class `system`, `user` and `portable` install modes.**
3. **First-class local packages.** A local package root is a full package, not
   a degraded registry entry.
4. **A defined state for every combination** of registry, local and installed
   (section 5).
5. **Versioned directories as the unit of distribution.**
   - A version directory carries a pinned source.
   - `_check_updates` is a separate tracker. It only creates new version
     directories. Installing one of them is a separate operation.
6. **Strict checksums by default**, with an explicit, scoped way to skip them.
7. **Declarative services for recurring hook work.** Hooks remain an escape
   hatch, not the norm.
8. **A structured event stream** driving a readable console, an interactive UI,
   and standard log files.
9. **A system boundary behind one interface**, so that nearly all behaviour can
   be tested on a temporary directory without touching the real machine.
10. **Lifecycle completeness:** uninstall, switch/rollback, cleanup, doctor.

Non-goals:

- **Sandboxing package hooks.** Hooks stay trusted code. The goal is to need
  far fewer of them.
- **Running vendor installers by default.** An explicit, marked service may
  run one (section 8.4).
- **Dependency resolution between packages** beyond an informative
  `requires = [...]` list. This can be revisited later.
- **Non-Windows platforms.**
- **Background or automatic updates.** Every change remains an explicit
  command or schedule.

## 3. Vocabulary

| Term | Meaning |
| --- | --- |
| **Package** | One application in one install root: `<root>\<name>\`. Its identity is *(root, name)*. |
| **Root** | A directory that holds packages, with one install mode (`system`, `user`, `portable`). Configured in the settings; a path given on the command line may also name an *ad-hoc* root. |
| **Version directory** | `<root>\<name>\<version>\` holding `pkg.toml`, an optional `App\`, and support files. Immutable once its `App` is populated. |
| **Pinned version** | A version directory whose `[source]` is concrete: no templates, and either a fixed `download_url` (normally with a SHA-256) or a shipped `App` (section 4). The default unit of distribution. |
| **Tracker** (`_check_updates`) | `<name>\_check_updates\`: a meta-package that discovers the newest upstream version and writes a *pinned, unpopulated version directory* for it. That is all it does: it never installs anything and is never installed itself. Installing the new version is a separate `update` or `install`. Replaces `_bootstrap`. |
| **Check** | The `gupkg check` operation: run trackers, write new pinned version directories, report. The only operation that discovers anything over the network (section 9.3). |
| **Populated / unpopulated** | Whether a version directory's `App\` exists and is non-empty. |
| **Current** | The `current` junction naming the active version. A package is *installed* exactly when `current` is valid and its target is populated. |
| **Registry** | A read-only catalogue of package definitions (pinned versions and optional trackers), downloaded into a cache. Several registries may be configured; the official one is named `main`. |
| **Link** | The record in `<package>\.gupkg\package.toml` saying that a local package follows registry entry `<registry>/<name>`. |
| **Install record** | `<package>\.gupkg\package.toml`: mode, link, and the integration effects last applied (used by uninstall and stale-effect cleanup). |
| **Receipt** | `<package>\.gupkg\receipts\<version>.toml`: what one install of one version did (source URL, observed SHA-256, checksum decision, log file, timestamps). |
| **Data directory** | `<package>\data\`: version-independent application state for portable mode (section 6.3). |
| **Service** | A built-in, declarative operation named in `pkg.toml` (fetch, extract, flatten, rename, discover versions, find checksums). |
| **Hook** | A trusted package-local Python module under `pkg.local\`, used only when no service fits. |

## 4. On-disk layout

```text
<root>\                                  # one install mode per root
  <name>\                                # package root (identity = root + name)
    current\                             # junction -> active version
    _check_updates\                             # optional tracker (never installed)
      pkg.toml                           #   [track] + [source] templates
      pkg.local\                         #   optional hooks
      icons\ ...                         #   support files copied into new versions
    1.2.3\                               # pinned version
      pkg.toml                           #   [source] download_url + sha256
      App\                               #   payload; absent/empty until populated
      icons\ ...
    1.2.4\                               # pinned, not yet populated ("available")
      pkg.toml
    data\                                # portable-mode persistent state (section 6.3)
    .gupkg\
      package.toml                       # install record: mode, link, applied effects
      receipts\<version>.toml
      state\track.toml                   # last tracker check result
      locks\
      work\                              # disposable staging
```

The reserved names in a package root are `current`, `_check_updates`, `data`, and
anything starting with `.` or `_`. Inside a version directory, `pkg.local\`,
`gupkg\` (a bundled gupkg, section 21), `install.cmd` and `gupkg-manager.cmd`
(the package launchers) are reserved. `_bootstrap` is accepted only by the
migration tool.

Rules:

- **A version directory is any non-reserved child containing `pkg.toml`.**
  Version ordering is unchanged from 0.17: dotted segments, and `.local.` for a
  local revision. Reserved names never take part in ordering. This fixes
  `_bootstrap` sorting as newest.
- **Population is atomic.** `App` is built under `.gupkg\work` and renamed into
  place, so a version directory is never half-populated.
- **`App` is disposable unless it is shipped** (owner's rule, 2026-10-22).
  Each version records the kind of its `App` in **`[source] shipped`**:

  ```toml
  [source]
  shipped = true        # App is included in this version directory; gupkg never drops or replaces it
  download_url = "…"    # optional: where the same payload is available, used only to recover a missing App
  ```

  | `shipped` | `download_url` | Meaning |
  | --- | --- | --- |
  | `false` | present | **Recoverable.** `App` is built from the download and may be deleted at any time, because it can be rebuilt. |
  | `true` | present | **Shipped, with a known source.** `App` is included in the version directory, and `shipped` takes precedence: gupkg treats `App` as shipped, never drops it and never replaces it. The `download_url` says the source is available, and it is used **only** to recover an `App` that is missing (with the normal checksum rules, section 9). |
  | `true` | absent | **Shipped, irreplaceable.** Examples are proprietary software, or a licensed or internally built payload distributed on a share. A missing `App` cannot be recovered. |
  | `false` | absent | **Invalid.** Validation fails, because nothing could ever populate `App`. |

  - **The default** is `false` when `[source]` has a `download_url`, and
    `true` when it does not. An `App` with no source is therefore treated as
    irreplaceable.
  - **gupkg's own tools always write the key explicitly:** `pkg new`,
    `pkg migrate`, `pkg bundle`, and `check`'s `render_pinned`.
  - **A `local` folder** (section 21.2) only speeds up the first
    population. It does not by itself make an `App` recoverable, because the
    folder can be cleaned up.

  **Shipped is a first-class kind, not an exception.** For every
  `shipped = true` version:
  - `gupkg pkg check` requires a non-empty `App`. It accepts an
    `App\MANIFEST.sha256` (or `[source] verify_manifest`), which `doctor`
    uses to detect modified files;
  - `cleanup` never drops the `App`, and `install --refresh` never replaces
    it;
  - `uninstall` keeps the version directory with its `App`. Only
    `uninstall --purge` deletes it, after an explicit confirmation. That
    confirmation names the `App` as unrecoverable when there is no
    `download_url`;
  - `list --details` and `info` show `App: shipped`, or
    `App: shipped (source available)`.

  **When `App` is missing.** A version whose `App` is missing or empty is
  repopulated from its `download_url` if it has one, whatever the value of
  `shipped`. Without a `download_url`, `gupkg pkg check` and `install` fail
  with the error `App is missing and no way to recover it.` rather than
  trying to populate it.

  Several design choices follow from the rule:
  - **No state lives in `App`.** Portable data lives in `<package>\data`
    (section 6.3), and receipts and install records live in `.gupkg\`.
  - **Management lives outside `App`.** The bundled gupkg that manages a
    bundled install sits in `<version>\gupkg\` (section 21), so dropping a
    recoverable `App` never makes a package unmanageable.
  - **gupkg's own payload is a recoverable `App`.** It is copied in from the
    bundled tree (`local`), with the release ZIP's `download_url` as the fallback.
  - **The lifecycle commands use it.** `cleanup` frees space by dropping
    recoverable, non-current `App`s. Repair (`install --refresh`,
    `doctor --fix`) rebuilds them.
  - **The active `App` is not dropped.** Removing it is valid on disk but
    leaves the package `broken` until repaired, so `cleanup` never removes
    the active `App`, nor gupkg's own running one.
- **`current` is switched only after a successful population.** Today it is
  linked first (`gupkg.py:208-240`). That order is reversed.
- **Logs are not kept in the package root.** They go to the per-mode log
  directory (section 14), and the receipt points at the log file.

## 5. Package state model

### 5.1 Facts

A package is described by one immutable `PackageView`, built by one function,
`catalog.describe(name, root)`:

```text
PackageView
  name, root, mode
  local:     LocalState | None      # None = no package root on disk
  registry:  RegistryEntry | None   # entry with the same name in a linked/selected registry
  link:      Link | None            # from .gupkg/package.toml
  installed: InstalledState | None
```

- **`LocalState`** holds the version directories (each marked pinned or
  unpinned, and populated or not), the tracker (if `_check_updates` exists), the
  `current` target, and a health check per version.
- **`InstalledState`** holds the active version, `App` health, the
  integration effects from the install record, and drift (effects that are
  missing on the system).
- **`RegistryEntry`** holds the pinned versions and the tracker from the
  registry cache, plus the registry revision.

Every front end (the CLI, the interactive UI, doctor, list, update planning)
consumes `PackageView`. No other code works out package state.

### 5.2 Derived status

One status is derived for display and filtering:

| Status | Definition |
| --- | --- |
| `available` | No local package root; the registry has the entry. |
| `local` | A local package root exists and holds at least one version directory or tracker, but there is no valid `current`. Nothing is installed; the definition is ready. |
| `installed` | `current` is valid and its target is populated. |
| `outdated` | Installed, and a newer version is *known*. Known means a newer local version directory (including one written by an earlier `check`) or a newer pinned version in the linked registry entry. |
| `broken` | `current` is dangling or escapes the package root; the active `App` is missing or empty; or the install record and the system disagree (drift). |
| `invalid` | A version directory or tracker fails validation. Shown for that version only. It does not block other valid versions. Today one bad `_bootstrap` marks the whole package unhealthy. |

### 5.3 The cases the manager must handle

The owner listed four cases (registry-only, local versions only, local
tracker, registry plus local), each installed or not. Every combination has
one defined behaviour:

| Case | `list` shows | `check <name>` | `install <name>` | `update <name>` |
| --- | --- | --- | --- | --- |
| **R** registry only | `available` (with `--available`) | Error: not local (hint: `install`). | Create `<root>\<name>`. Copy the newest registry pinned version, plus the registry tracker if there is one. Write a link. Install. If the registry has only a tracker: copy it, write the link, and stop with "no version known yet; run `gupkg check <name>`". | Error: not installed (hint: `install`). |
| **V** local versions, no tracker, not installed | `local` | Nothing to check (no tracker). | Install the newest valid local version. `name@ver` picks another. | Error: not installed. |
| **V** installed | `installed` / `outdated` | Nothing to check (no tracker). | Repair (re-apply integration; populate `App` if empty). | Install the newest local version newer than `current`. Otherwise "up to date". |
| **T** local tracker (± versions), not installed | `local` | Run the tracker. Write a pinned version directory if the result is newer than every local version. Install nothing. | Install the newest valid local version. If there is none yet: "no version known yet; run `gupkg check <name>`". The tracker is never run. | Error: not installed. |
| **T** installed | `installed` / `outdated` | As above. The package becomes `outdated` if a newer directory was written. | Repair. | Install the newest local version newer than `current`. The tracker is never run. |
| **R+V/T, linked** | as V/T, with a `registry` column | Runs the local `_check_updates` (copied from the registry if absent, rule 2). | as V/T | Install the newest of: the local version directories, and the linked registry's pinned versions (copied in when chosen). |
| **R+V/T, not linked** (same name, no link) | as V/T, marked `same name in registry: main` | as V/T | as V/T. Never silently merged. | Local sources only. `gupkg link <name>` adopts the registry entry. |
| **Broken** | `broken` | as V/T | Repair: re-populate `App` from the active version's `[source]`; re-apply integration. | Refuses until repaired (hint: `doctor` / `install`). |

**Checking and installing are separate operations** (owner's decision,
2026-10-22). `check` is the only operation that runs a tracker. It ends once
it has written the new version directories and reported them. `install` and
`update` never run a tracker. They install only versions that are already
known: local version directories, or the linked registry's pinned versions.
There is no flag that combines the two. A scheduled job runs
`gupkg-manager check --all`, followed by `gupkg-manager update --all --yes`. Between the two
steps the new version directories can be reviewed, edited or deleted.

Rules that apply to every case:

1. **Copying a version in.** A registry pinned version is copied into the
   local package root only when it is selected for install. If a local
   directory of the same name exists and its `pkg.toml` differs from the
   registry copy, the local one is kept and the package is reported as
   `diverged`. Local files are never overwritten. In a root with no root
   forwarder, the running bundled gupkg is carried into the new directory
   (section 21.4).
2. **The registry tracker is copied in too.** When a linked package has no
   local `_check_updates` and the registry provides one, the registry's tracker is
   copied to `_check_updates` on install, check or update, and refreshed when the registry
   revision changes, unless the local copy was edited. Edits are detected by
   recording the hash of what was copied.
3. **Tracker output is a normal version directory.** Its `pkg.toml` is fully
   pinned (section 7.4). After creation it is indistinguishable from a
   registry pinned version, and the tracker plays no further part in it.
4. **Downgrades are explicit.** `gupkg switch <name> <version>` replaces
   `--allow-downgrade`.
5. **Each scope is a separate package.** `system:firefox` and `user:firefox`
   are independent. `list` warns about the pair because their shims shadow
   each other on PATH. A bare name that exists in several roots is ambiguous
   for mutating commands and must be qualified.

### 5.4 Local packages are first class

A local package root needs no registry and no settings entry:

- **Any path works as a target.** A path to a package root, a version
  directory or `current` can be given to any command.
- **The install mode of a path target is resolved as in section 6.2:**
  `--mode` first, then its install record, then the configured root that
  contains it, and `auto` (D4) only when none of them decides.
- **Ad-hoc roots are tracked.** A package installed in place (`--in-place`,
  section 21.4) from a path outside every configured root is added to the
  `adhoc` list in `state.toml` (section 11), so
  `list`, `doctor` and the manager's `update --all` still see it.
- **`gupkg pkg new <name>` scaffolds a local package.** It creates a version
  directory, or a tracker with `--track github:owner/repo`.

## 6. Install modes: system, user, portable

### 6.1 One context object

`InstallMode` is resolved once, into an immutable `ModeContext`. Every
subsystem reads only that object:

| | `system` | `user` | `portable` |
| --- | --- | --- | --- |
| Default root | `C:\opt` | `%USERPROFILE%\opt` | `%USERPROFILE%\opt-portable` (or any path) |
| Requires elevation | yes | no | no |
| Start Menu | `%ProgramData%\Microsoft\Windows\Start Menu\opt` | `%APPDATA%\Microsoft\Windows\Start Menu\opt` | user Start Menu, or none (`integrate = "none"`) |
| Environment / PATH | `HKLM\…\Session Manager\Environment` | `HKCU\Environment` | `HKCU`, or none |
| Bin (shims) | `C:\bin` (configurable) | `%USERPROFILE%\bin` (configurable) | `<root>\bin`; added to user PATH unless `integrate = "none"` |
| App settings live | where the app puts them (normally per-user profile) | same | inside the package: `<package>\data` (section 6.3) |
| gupkg's data (settings, logs, registry cache, state) | `C:\opt\.gupkg\` (admin-only) | `%USERPROFILE%\opt\.gupkg\` | `<portable root>\.gupkg\` |
| Settings layers read | the system root's only | system root's + user root's | system root's + portable root's |

- **The defaults are configurable.** Bin and Start Menu defaults live in the
  settings. The Start Menu becomes configurable, which it is not today.
- **One duplicated helper goes away.** The second admin-detection
  implementation (`windows.is_current_user_admin` beside
  `elevation.is_elevated`) is removed.
- **`ModeContext` replaces four scattered sources:**
  - `layout.compute_scope_paths`
  - `manager.InstallationContext`
  - `windows.environment_registry_location`
  - `distribution._user_bin` / `_system_bin`

### 6.2 Selecting a mode

The mode of an operation is resolved in this order:

1. `--mode system|user|portable`, or a scope-qualified target (`user:firefox`);
2. the install record of an existing package;
3. the root that contains a path target;
4. `default_mode` from the settings. The default is **`auto`** (owner's
   decision, 2026-10-22).

`auto` has exactly one meaning, and every command uses it:

- **An elevated process selects `system`.** If the package does not support
  `system`, the command refuses and says to run it from a non-elevated
  shell. An elevated gupkg never acts on a user root (section 9.6, rule 2).
- **A non-elevated process selects `user`.**
- **`auto` never selects `portable`.** Portable is always chosen explicitly,
  either with `--mode portable` or by a target inside the portable root.

`auto` only applies when steps 1–3 give no answer. An existing package keeps
the mode in its install record, and a path inside a configured root keeps
that root's mode, whatever the elevation. The chosen mode and the reason for
it (for example `system (auto: elevated)`) are always printed in the Resolve
phase and recorded in the log.

The 0.17 commands give `auto` four different meanings (single-package
install, manager list, `manager install` and `self repair`). That variation
goes away.

Further rules:

- **`pkg.toml` declares which modes a package supports:**
  `modes = ["system", "user", "portable"]`. The default is
  `["system", "user"]`.
- **Unsupported modes are rejected before any work.**
- **`modes` replaces `only_portable` and the `-portable` name suffix.** The
  suffix becomes an ordinary part of the name.

### 6.3 Portable mode

Portable means that the application's settings and state live inside the
package folder, so the whole package can be copied, synced or carried on
removable media.

**State survives updates.** Every version has its own fresh `App`, so
portable state must sit outside it. The package declares what to keep:

```toml
[portable]
# Files/folders the app writes inside App, kept in <package>\data and linked
# into every version's App (directories: junction; files: copied in on
# activation and copied back on switch-away / before update).
persist = ["Data", "settings.ini"]
# Marker files the app needs to enter portable mode (created in App).
markers = ["tixati_portable_mode.txt", { path = "doLocalConf.xml", content = "" }]
# Optional: arguments and environment that select portable mode for
# shortcuts and shims.
args = ["--portable"]
env = { VSCODE_PORTABLE = "$Data" }

[nonportable]
# What to remove from App when the package is NOT installed portable
# (today's keypirinha / mogan / systeminformer hooks).
remove = ["portable", ".portable", "*/SystemInformer.exe.settings.xml"]
```

**Rules:**

- **`$Data` expands to `<package>\data`.** It is valid only in portable mode.
- **First population migrates bundled state.** On the first population into
  portable mode, a persisted path that exists in the freshly unpacked `App`
  and not yet in `data\` is moved into `data\`. It is the vendor's default
  config.
- **Persisted files survive a switch.** On switch or update, persisted files
  are copied back from the outgoing `App` into `data\` before `current` moves.
- **Uninstall keeps `data\` unless `--purge` is given.** The same rule applies
  in every mode to anything gupkg did not create.
- **Integration in portable mode is `user` by default.** Shortcuts, shims and
  PATH go to the user profile. Setting `integrate = "none"` in the portable
  root's settings (a "USB stick" root) makes gupkg create shims in `<root>\bin`
  and nothing else. `gupkg shell` then prints the `PATH` line to use.
- **Portable is never machine-wide.** Shared writable state in `C:\opt` is a
  privilege-escalation risk.

## 7. `pkg.toml` format 2

### 7.1 Principles

- **One source per version, and the version is the directory name** (owner's
  decision, 2026-10-22).
  - **One table.** A version directory has exactly one `[source]` table,
    describing how to obtain *this* version's `App`.
  - **No per-version source lists.** There are no tables such as 0.17's
    `[[origin.versions]]`, and no `version` key anywhere in a version's
    `pkg.toml`.
  - **The version lives only in the folder name.** It is the name of the
    directory the version lives in (`<package>\<version>\`), and the
    template variable `${version}` expands to that name.
  - **Other versions get their own directories.** Another version is another
    directory with its own `pkg.toml`. Historical sources are kept by keeping
    the old version directories, not by listing them in one file.
  - **Validation** rejects `[[origin.versions]]`, a top-level `version` /
    `localVersion` / `name`, and more than one `[source]`.
- **One file format.** The same format is used for pinned version
  directories and for trackers. A tracker additionally has `[track]` and
  `${version}`-style templates.
- **One naming convention:** `snake_case` everywhere. Today the format mixes
  `targetPath`, `working_dir`, `Name`/`Value` and `ignore_checksum`.
- **Three URLs, three names, three purposes.** The 0.17 format had one
  overloaded `[origin] url` (owner's request, 2026-10-22). Format 2 separates
  them:

  | Key | Where | What it is | Fetched by gupkg? |
  | --- | --- | --- | --- |
  | `project_url` | top level (was `homepage`) | The project's web page, for people. | **Never.** Information only. |
  | `download_url` | `[source]` | The payload artifact for this version. Its presence makes `App` recoverable (section 4). | Yes, when populating `App`. |
  | `check_url` (`repo` / `repo_url` for the `github`, `gitlab` and `git` providers) | `[track]`, in `_check_updates` only | The page or API that is read to discover newer versions. | Yes, by `gupkg check` only (section 9.3). |

  The engine keeps them apart by construction. `project_url` is never passed
  to the HTTP client, and a version directory has no `check_url` (D10).
- **`format = 2` is required.** Version 1 (0.17) files are read only by
  `gupkg pkg migrate`.
- **`[origin]` and `[update.*]` are replaced** by `[source]` (how to obtain
  *this* version's `App`) and `[track]` (how to find newer versions; trackers
  only). An `[origin] url` that is really a GitHub page used as a "zip"
  disappears.

### 7.2 Pinned version

```toml
format = 2
description = "Fast text search"
project_url = "https://github.com/BurntSushi/ripgrep"
modes = ["system", "user"]

[source]
download_url = "https://github.com/BurntSushi/ripgrep/releases/download/14.1.0/ripgrep-14.1.0-x86_64-pc-windows-msvc.zip"
sha256 = "…64 hex…"
sha256_from = "github-digest"     # provenance, informational: github-digest | vendor-sums | registry-ci | manual
# kind is inferred from the file (zip, 7z, sfx, nsis, msi, inno, exe, file); override with kind = "…"
strip = 1                         # flatten one wrapper folder (service: section 8.2)

[[shortcut]]
name = "Ripgrep"
target = "$App\\rg.exe"

[[env]]
name = "RIPGREP_HOME"
value = "$App"

[[path]]
value = "$App"

[[shim]]                          # was [[bin]]
name = "rg"
target = "$App\\rg.exe"
```

### 7.3 Tracker (`_check_updates\pkg.toml`)

```toml
format = 2
description = "Mozilla Firefox web browser"
project_url = "https://www.mozilla.org/firefox/"
modes = ["system", "user", "portable"]

[track]
provider = "json"
check_url = "https://product-details.mozilla.org/1.0/firefox_versions.json"
version = "$.LATEST_FIREFOX_VERSION"

[source]
download_url = "https://archive.mozilla.org/pub/firefox/releases/${version}/win64/en-US/Firefox%20Setup%20${version}.exe"
kind = "sfx"
subdir = "core"
require = ["firefox.exe"]

[source.checksum]                 # how to find the hash for a discovered version
sums_file = "https://archive.mozilla.org/pub/firefox/releases/${version}/SHA256SUMS"
entry = "win64/en-US/Firefox Setup ${version}.exe"

[portable]
args = ["-profile", "$Data\\profile"]

[[shortcut]]
name = "Firefox"
target = "$App\\firefox.exe"
```

### 7.4 From tracker to version: `gupkg check`

`gupkg check firefox` runs these steps, and only these:

1. **Check.** Run `[track]` and get `version` plus provider captures (for
   example a GitHub asset URL or digest).
2. **Compare.** The *manager* compares the result against `current` and the
   local versions. Hooks never decide "is it current?". That ends five
   inconsistent comparators and the `appReady` bugs.
3. **Write the version directory.** Write `<package>\<version>\` from the
   tracker:
   - copy the support files, but **strip all update-check code**. Only the
     hook modules named by `[source]` or `[[prepare]]` are copied, never a
     `track` hook (section 9.3);
   - render `pkg.toml` with every template expanded;
   - remove `[track]`;
   - fill `[source].sha256` **only** from a checksum the tracker *discovered*
     (GitHub digest, sums file, sidecar), with `sha256_from` set
     accordingly;
   - in a root with no root forwarder (a bundled install), copy the running
     bundled gupkg and the package launchers into the new directory
     (section 21.4).
4. **Report and stop.** The check reports the new directory, and the package
   shows as `outdated`. Nothing is downloaded beyond what the provider needed
   for discovery, nothing is populated, and nothing is installed.

The new directory is then an ordinary pinned version, which a later and
separate `gupkg update firefox` (or `install firefox@<version>`) installs. That
install validates it again, like any other version directory.

**gupkg never computes a hash after download and writes it into `pkg.toml`.**
The hash it observes goes into the receipt only (section 9).

### 7.5 Variables

| Variable | Meaning |
| --- | --- |
| `$App` | The active version's `App`. |
| `$VersionRoot` | The version directory. |
| `$PackageRoot` | The package root. |
| `$Data` | `<package>\data` (portable mode only). |
| `$Bin` | The mode's shim directory. |
| `${version}` | The version, with filters: `${version\|replace:.:-}`, `${version\|major}`. |
| `${name}` | A named capture from `[track]`. |
| `${ENV}` | A braced environment reference, as today. |

## 8. Built-in services

These replace the recurring hook code found in the registry. The packages
counted are from the 2026-10-22 registry.

### 8.1 Version discovery (`[track].provider`)

| Provider | Replaces | Key fields |
| --- | --- | --- |
| `github` | built-in github mode + 6 hand-rolled clients (bcu, perl, portablegit, wumgr, systeminformer, msys2) | `repo`, `asset` (exact or `asset_pattern` regex with `(?P<version>)`), `version_from = "tag"\|"asset"`, `tag_pattern`, `tag_prefix`, `release = "latest"\|"newest-matching"`, `prerelease = false` |
| `gitlab` | restic-backup-gx | `project`, `link_name`/`asset_pattern` |
| `page` | 9 template scrapers + 7zip, SumatraPDF, cwrsync, tixati, dontsleep | `check_url`, `match` (regex with named groups), `select = "max"\|"first"\|"unique"`, `version` template, `download` template, `headers` |
| `index` | emacs, gimp, python, vlc, pidgin | `check_url`, ordered `[[track.level]]` with `match`, `select`, optional `probe` (descend until a file exists) |
| `redirect` | crystaldiskinfo, crystaldiskmark, geogebra, geogebra5, zotero | `check_url`, `match`, `unwrap_query` |
| `json` | firefox, julia, vscode, qbittorrent | `check_url`, `version` (JSONPath), `download`, `filter` |
| `last_modified` | autoruns, processexplorer | `check_url`, `format = "%Y.%m.%d"` |
| `rss` | netio-gui | `check_url`, `match` |
| `git` | latex-toolkit, overleaf-sync, windebloattools | `repo_url`, `ref`; version = `YYYYMMDD-HHMMSS-<short sha>` (the commit is the integrity anchor) |
| `chain` | winscp, inkscape, shutter-encoder | ordered `[[track.fetch]]` steps; each step's captures feed the next URL |
| `module` | anything else | `pkg.local/track.py` → `track(ctx) -> TrackResult` |

Every provider:

- shares one HTTP client, with one User-Agent policy, one timeout, retries
  with backoff on 5xx and 429, TLS via `tls.py`, and the `GITHUB_TOKEN` header
  when it is set;
- returns `TrackResult(version, captures, checksum?)`;
- leaves version comparison to the manager.

### 8.2 Unpacking (`[source]`)

| Field | Replaces | Meaning |
| --- | --- | --- |
| `kind` (inferred) | zip payload; 7zip/portablegit/qbittorrent unpack hooks | `zip`, `7z`, `sfx`, `nsis`, `msi` (via `msiexec /a` into stage, or 7z), `inno` (innoextract-compatible; innoextract 1.9 reads Inno Setup only up to 6.1, job 07 spike S3), `exe`/`file` (copy as-is), `git` |
| `strip = N` | 6 flatten hooks and 15 `extract = [{src="x-*/", dest=""}]` rows | Remove N levels of a single wrapper folder. `strip = "auto"` strips while the root has exactly one directory. |
| `subdir` | `extractSubdir`, firefox `core` | Keep only this subtree. |
| `[[source.nested]]` | obsidian, mogan, pidgin | Extract an inner archive (`path`, `dest`, `replace`). |
| `[[source.extract]]` | tixati | Glob-mapped partial extraction (today's `extract`). Works for every kind. |
| `require = [...]` | 5 existence checks | Files that must exist after unpacking. |
| `verify_manifest = "<path>"` | gupkg's own `unpack_app` check (issue 016) | A `<sha256>  <relative path>` manifest shipped inside the artifact. Every unpacked file must match it, with no extra or missing files. Any package can use it; gupkg's own package is the first user. |

Every unpack service is safe-extracting: no paths escape, there are no reparse
points, and partial failures leave nothing behind. Every one works for every
archive kind; today `extract`/`rename` are limited to zip.

`sfx` covers only self-extractors whose codecs the bundled 7-Zip supports.
Firefox and Zotero extract; MSYS2's `.sfx.exe` uses Zstandard and does not
(job 07, spike S3).

### 8.3 Post-unpack preparation (`[[prepare]]`)

These steps run in order, against the staged `App` only:

```toml
[[prepare]]
remove = ["$PLUGINSDIR", "uninstall.exe"]          # pidgin, keypirinha, mogan, systeminformer
[[prepare]]
rename = { from = "lockhuntersetup_portable_*.exe", to = "lockhunter.exe" }  # glob, exactly one match
[[prepare]]
copy = { from = "volta-shim.exe", to = ["node.exe", "npm.exe", "npx.exe"] }
[[prepare]]
write = { path = "portable.ini", content = "…" }
[[prepare]]
module = "pkg.local/fix_nsswitch.py"               # escape hatch
```

`rename` keeps today's rule: it never overwrites an existing destination.
Paths are always confined to `App`.

### 8.4 Vendor installers

`kind = "inno"` and `kind = "nsis"` extract the installer without running it.
Running vendor code is a separate, explicitly marked step:

```toml
[source.installer]
run = ["/VERYSILENT", "/CURRENTUSER", "/NOICONS", "/DIR=$Stage"]
```

`list` and the install output label such packages "runs vendor installer", and
the setting `allow_vendor_installers` must not be `false`. GIMP is the only
current user.

### 8.5 Checksum discovery (`[source.checksum]`)

The fields are `sidecar = "${download_url}.sha256"`, `sums_file`/`entry`,
`json`/`path`, and `page_match`. GitHub and GitLab digests are picked up
implicitly. This replaces discovery code in 13 hooks.

### 8.6 Hook library

The hook library covers what remains after the services above. `gupkg.hooklib`
is the supported API for `pkg.local` modules:

- `fetch_text` and `fetch_json` (the shared client)
- `resolve_redirects`
- `github_release`
- `extract_archive`
- `flatten`
- `safe_extract`
- `find_digest`
- `seven_zip`

Hooks receive a typed `HookContext`, never raw dictionaries. The API version
becomes `PKG_MODULE_API = 2`. Hooks may no longer download artifacts
themselves. They return URLs, and the engine downloads and verifies.

The HTTP helpers (`fetch_*`, `resolve_redirects`, `github_release`) are
available only to `track` hooks in `_check_updates`. Unpack and prepare hooks
in a version directory get a context with no HTTP client (section 9.3).

## 9. Checksums and trust

### 9.1 Verification rule

When a source artifact is downloaded:

1. **A SHA-256 in `[source]` is verified.** A **mismatch always fails.** No
   flag or setting turns a mismatch into a pass.
2. **When `[source]` has no SHA-256, the *missing-checksum policy* decides:**

   | Context | Policy |
   | --- | --- |
   | `gupkg` CLI (`install`/`update`) | **refuse**, unless the command carries `--skip-checksum`. That flag applies to this invocation only and to missing hashes only. The CLI never prompts about checksums. |
   | `gupkg-manager` (section 15) | `[manager] checksum_missing` in the settings: `ask` (default) \| `refuse` \| `skip`. The manager reads the plan (`gupkg update … --plan --json`), which marks each target whose version has no SHA-256. With `ask`, it shows the package, version and URL and asks once per package. With `skip`, or after a yes, it calls the CLI for that target with `--skip-checksum`. With `refuse`, or after a no, it does not run that target. |
   | Non-interactive manager run (`gupkg-manager update --all --yes`, no TTY) | `ask` behaves as `refuse`. |

   The policy therefore lives entirely in the manager. The CLI only knows
   "refuse" and the explicit flag.

3. **Git sources** pin a commit; the commit hash is the integrity anchor.
4. **Local files** (`file:` URLs on a share) follow the same rules.

`ignore_checksum` is removed from the format. Registry packages publish pinned
versions with hashes (section 17), so they need no bypass at all.

### 9.2 What gets recorded

The receipt records:

- `observed_sha256`
- `checksum_decision`: `verified`, `skipped-cli`, `skipped-setting`,
  `user-approved`, or `verified-by-bootstrap` (section 21.6)
- the URL, the final URL after redirects, and the size

`list --details` and `doctor` show unverified installs.

gupkg never writes an observed hash into `pkg.toml` automatically.
`gupkg pkg pin <target>` copies a receipt's observed hash into the version's
`pkg.toml` with `sha256_from = "manual"`. It is a deliberate, reviewable act.

### 9.3 Network boundary: only the update checker discovers anything

The owner decided on 2026-10-22 that **a version directory never contains
update-check functionality**. A pinned version is a security boundary: what
it fetches is fixed and reviewable in its `pkg.toml`.

The rules below are enforced by validation, not by convention.

1. **A version directory has no `[track]` table.** Its `pkg.local\` holds no
   module that defines `track`. Validation (`gupkg pkg check`, the catalog,
   and the install planner) rejects a version directory that has either,
   and the error names `_check_updates`.
2. **Version directories have a fixed set of downloads.** Installing one
   fetches exactly the URLs written in its `pkg.toml`. That is
   `[source].download_url` plus any fixed `[source.nested]` or `[source.checksum]`
   URLs, and none of them may contain templates. The engine performs these
   downloads; nothing else does.
3. **Version-directory hooks get no network access.** Hooks that unpack or
   prepare a version receive a `HookContext` with no HTTP client. They work
   only on the downloaded artifact and the staged `App`. Python cannot
   sandbox trusted code, so this is a contract, not a sandbox. Each hook is
   still checked at load time: `gupkg pkg check` warns about an import of
   `urllib`, `http`, `socket`, `requests` or `subprocess` in a
   version-directory hook.
4. **`_check_updates` is the only component that discovers anything.** It
   decides what "newer" means, which URLs to probe and what to write.
   - Its output is a new pinned version directory. That directory goes
     through the same validation as any other version directory before
     anything is installed from it.
   - The output can always be reviewed. `check` ends after writing it, and
     installing it is a separate command. The registry autoupdate job turns
     it into a pull request.
5. **Updates never come from the installed version.** Today, `[update]` is
   copied forward from the *active* version (`updates.prepare_update`). That
   path is removed. Updates come only from `_check_updates` (local, or copied
   in from a linked registry) and from registry pinned versions.
6. **Checking is its own, unprivileged operation.** `gupkg check` is the only
   operation whose network targets are not known in advance, and it is never
   part of an install. It always runs with the user's privileges, never
   elevated, even for a system-mode package.
   - **Where the check writes.** For a user or portable package, the check
     writes the new version directory straight into the package root. A
     system package root is admin-only, so for a system package the check
     writes only the rendered `pkg.toml` as a *proposal* in the user
     root's data directory:
     `%USERPROFILE%\opt\.gupkg\proposals\<name>\<version>\pkg.toml`. That
     location is user-writable. A proposal is therefore treated as
     untrusted *data*, and admission takes no code from it (below).
   - **Admission.** A later, elevated `gupkg update` *admits* a proposal
     into the system root as follows:
     1. it shows the proposal's URL and SHA-256 and asks for confirmation;
     2. it validates the proposal, which must have no templates, no `[track]`
        and no hooks of its own;
     3. it builds the version directory from that `pkg.toml`, plus support
        files and hook modules taken from the admin-only `_check_updates`.
        Code is never taken from the user-writable proposal area.
   - Only after admission does it install the version.

### 9.4 Elevated runs

The trust rules in `docs/elevated-runtime-design.md` stay (issue 032 moves
the file to `_docs/`).

- **System-mode runs read only admin-only locations.** That covers the system
  root and its data directory `C:\opt\.gupkg\` (settings, registry cache,
  state, logs). The threat model behind this rule is section 9.6.
- **The user settings layer is ignored when elevated.** This means a user
  cannot set `[manager] checksum_missing = "skip"` to affect a system install.

### 9.5 The Windows registry is a write target, never a source of information

This is the owner's decision of 2026-10-22 (D18). **gupkg never reads the
Windows registry to find anything out.** That covers where gupkg is
installed, settings, package state, installed software, tool locations and
versions. All information comes from two places:

- **the filesystem:** settings files, package roots, install records and
  receipts, root data directories and root forwarders (sections 9.7, 21.3);
- **the process:** the elevation token and the environment.

The registry is only the *target* of changes that make software run. In 1.0
those changes are the environment variables and `PATH` entries that packages
declare, written to `HKCU\Environment` or to
`HKLM\…\Session Manager\Environment`. Reads are allowed only as part of such
a change, never to learn anything:

- **Changing a value.** `PATH` has to be read before an entry can be
  appended to it or removed from it.
- **Verifying a value gupkg wrote.** `doctor` checks that a value listed in
  the install record is still present.

Rules:

- **No generic registry table.** Later releases may add *specific*
  declarative effects, such as file-type associations, `App Paths` or URL
  protocols. Each would be a named table with its own validation, recorded in
  the install record and removed on uninstall. None of them would be read
  back as information.
- **0.17 already complies.** Its only registry read is the read-modify-write
  of `PATH` in `components.py`. A test fails the build if any registry read
  outside `system/windows.py`'s environment and `PATH` functions appears.

### 9.6 Threat model (T1)

This is the owner's decision of 2026-10-22 (D19). **gupkg runs as
administrator**, because system mode needs it: the machine environment and
`PATH`, the all-users Start Menu, and an admin-only system root. Its security
goal is:

> **A standard user must not be able to gain administrator rights through
> gupkg.** An elevated gupkg never runs code, and never reads inputs, that a
> standard user can modify.

This matters when standard users share a machine with an administrator, and
when an administrator signs in separately or approves UAC prompts with their
own credentials. On a single-person machine where the user is a UAC-prompted
administrator, Windows itself does not treat elevation as a security
boundary. The same rules then cost nothing, and they still guard against
accidents and damaged files.

Rules:

1. **Only an admin-only gupkg elevates.** gupkg elevates only by relaunching
   *itself*, and only when its own location (its version directory and
   root) is admin-only. A user-mode or bundled gupkg in a user-writable place
   never elevates, and cannot perform system installs.
2. **The elevated side resolves everything itself.** From the non-elevated
   side it accepts only a package name, or a path *inside* the admin-only
   system root. Everything else comes from its own location: its root, that
   root's `.gupkg\settings.toml`, and package definitions. It ignores user
   settings, user roots and the user's environment.
3. **Launchers in admin-only roots reach only gupkg copies inside the same
   root** (section 21.3). Elevated launchers verify, with a trusted system
   tool and before running anything, that their root and version directory
   are not user-writable. They refuse otherwise.
4. **Data from user-writable places is only ever data.** Examples are
   proposals (section 9.3), and distribution folders an administrator
   installs system-wide (section 21.4). It is copied and validated into
   admin-only locations before use. Code is never executed from there.
5. **Explicitly out of scope:**
   - malware already running as the user that requests UAC on its own;
   - an administrator who chooses to install an untrusted package;
   - an administrator account that is itself compromised.

A safety check placed in a *user-writable* launcher is a guard rail against
mistakes, not a security control: whoever can write the folder can remove
the check. The rules above never rely on such a check.

### 9.7 gupkg owns no files outside its roots

This is the owner's decision of 2026-10-22 (D20). gupkg's own files live in
the **root data directory** `<root>\.gupkg\` of the root they belong to:
`settings.toml`, `state.toml`, `logs\`, the registry cache, proposals, and
the root forwarder (section 21.1). gupkg **never** uses `%LOCALAPPDATA%`,
`%APPDATA%` or `%ProgramData%` for its own data, and never the registry
(section 9.5).

- The system root's data directory inherits the root's admin-only
  permissions.
- Windows-defined *integration targets* are still written, because they are
  how software is made to run: the Start Menu folders
  (`%ProgramData%\Microsoft\Windows\Start Menu\opt`,
  `%APPDATA%\…\Start Menu\opt`) and the environment. gupkg writes them and
  never reads them as information (except to verify its own install
  record).

## 10. Command-line interface

### 10.1 Principles

- **One grammar, with no manager mode inside the CLI.** The settings are
  always loaded, and built-in defaults apply when no file exists. A name
  resolves through the configured roots and registries in every command.
- **The manager is a separate program** (owner's decision, 2026-10-22). It is
  `gupkg-manager` (section 15), and it drives `gupkg` only through the
  machine interface (section 10.4). Inventory browsing, `--all` batches,
  planning and confirmation, the missing-checksum policy, grouping operations
  under one elevation, and the interactive UI all live in the manager. The
  CLI acts on explicit targets: one or several names or paths.
- **Targets are names or paths.**
  - Selector grammar: `name`, `name@version`, `mode:name`, or a path. A path is
    anything containing `\` or `/`, or starting with `.`.
  - `registry/name` (for example `main/firefox`) selects a registry entry
    explicitly.
- **Options go after the command.** Root-only options are limited to
  `--version`, `--help`, `--settings PATH` and `--sandbox DIR` (section 16).
- **Exit codes are unchanged:** 0 ok, 2 user/config error, 3 operation
  failed, 4 internal error, 5 declined by the user (new), 6 this package needs a newer gupkg (new; drives the launcher fallback, section 21.3).
- **Every mutating command takes `--yes`.**
- **Every command takes `--json` for machine output.** It replaces `--format
  toml`, and the output is one document on stdout. Human output goes to
  stderr.

### 10.2 Commands (`gupkg`)

```text
gupkg check    <target>... [--mode M]                     # run trackers; write new pinned version dirs; install nothing
gupkg install  <target>... [--mode M] [--skip-checksum] [--refresh] [--in-place] [--yes]
gupkg update   <target>... [--download-only] [--mode M] [--skip-checksum] [--yes]   # install the newest KNOWN version; never runs a tracker
gupkg switch   <target> <version>                         # activate another local version (rollback)
gupkg uninstall <target>... [--purge] [--yes]             # remove integration; --purge also deletes the package root and data
gupkg list     [--mode M] [--installed|--available|--outdated|--broken|--local] [--details]
gupkg info     <target>...                                # PackageView: versions, link, tracker, last check, receipts, log
gupkg search   <query> [--registry R] [--offline]
gupkg doctor   [<target>...] [--fix]                      # health incl. drift (missing shortcuts/shims/PATH)
gupkg cleanup  [<target>...] [--keep N] [--bundled]       # old Apps, receipts, work, registry trees, logs (section 13.3); --bundled: section 21.8
gupkg link     <target> [<registry>/<name>]               # adopt a registry entry for a local package; --unlink
gupkg logs     [<target>] [--open | --path]
gupkg shell    [--mode M]                                 # print the PATH line for an integrate = "none" root (section 6.3)

gupkg registry sync|status|list|add|remove
gupkg pkg new <name> [--track github:owner/repo]          # authoring
gupkg pkg check <path> [--install]                        # was config-check; --install runs a sandboxed install (section 16)
gupkg pkg migrate <path>... [--no-backup]                 # 0.17 format -> format 2; _bootstrap -> _check_updates
gupkg pkg pin <target>                                    # receipt hash -> pkg.toml (explicit)
gupkg pkg bundle <version-dir> [--lite]                   # embed the running gupkg + launchers (section 21); --lite: open question 6
gupkg pkg export <version-dir> -o X.zip                   # write a distribution ZIP; print its SHA-256
gupkg settings show|path|init|set KEY VALUE
gupkg self status|repair|check|update               # check/update = `gupkg check gupkg` / `gupkg update gupkg` (section 20)
```

The CLI has no `--all` and no interactive UI. Several targets in one call
run one after another, and the run continues past a failure. For scripts,
`gupkg update $(gupkg list --outdated --names)` is the batch form; with
planning and confirmation, the batch form is `gupkg-manager`.

### 10.3 What changes for users

| 0.17 | 1.0 |
| --- | --- |
| `gupkg install C:\opt\pandoc` | unchanged |
| `gupkg manager install vscode` | `gupkg install vscode` |
| `gupkg --scope user manager update --yes` | `gupkg-manager update --all --mode user --yes` |
| `gupkg update PATH --check-only` | `gupkg check PATH`. This now also writes the new version directory, but installs nothing. |
| `gupkg update PATH` (check + download + install) | `gupkg check PATH`, then `gupkg update PATH`: two separate operations. |
| `gupkg install PATH --allow-downgrade` | `gupkg switch PATH 1.2.3` |
| `--no-checksum` | `--skip-checksum` (missing hashes only) |
| `gupkg config-check/config-fix` | `gupkg pkg check` / `gupkg pkg migrate` |
| `gupkg manager list/doctor/search/registry` | `gupkg list/doctor/search/registry` |
| `gupkg manager self status/repair` | `gupkg self status/repair` |
| `gupkg manager self update` (which only repaired) | `gupkg self check`, then `gupkg self update`: a real, verified update to a new release |
| `gupkg manager self repair-deps/update-deps` | removed (issue 016); maintainers use `tools/update_deps.py` |
| `gupkg tui PATH` / `gupkg manager tui` | `gupkg-manager PATH` / `gupkg-manager` |

During 1.0.x, `gupkg manager …`, `gupkg tui` and `config-check` /
`config-fix` remain as deprecated aliases that print the new spelling. They
are removed after that.

### 10.4 Machine interface (the contract `gupkg-manager` uses)

The manager never imports gupkg's engine. It only runs `gupkg` as a
subprocess, so this interface is a public, versioned contract (`"api": 1`).
Any other tool can use it the same way.

| Option | Meaning |
| --- | --- |
| `--json` | One result document on stdout: per target, the outcome, version, mode, receipt path and log path, plus structured errors such as `{"code": "checksum-missing", "url": …}`. Human output goes to stderr. |
| `--plan` | Accepted by `install`, `update`, `uninstall`, `switch` and `cleanup`. Resolves and plans without changing anything. Per target it reports the chosen version, mode, whether elevation is required, `sha256: present \| missing`, the source URL, and the effects to add or remove. |
| `--events-file PATH` | Writes the event stream (section 14.1) as JSON lines to `PATH`, flushed per event, ending with the result document. A file is used rather than a pipe because an elevated child started through `runas` cannot share pipes with a non-elevated parent. The manager follows the file. |
| `--no-input` | Never prompts. Anything that would need an answer becomes a structured error with exit code 5. |

**Which `gupkg` the manager runs.** The manager always runs the `gupkg`
launcher next to it in the same gupkg tree. For an installed gupkg that is
`$App\gupkg.cmd`; for a bundled one, `<version>\gupkg\gupkg.cmd`. It never
uses a `gupkg` found on `PATH`, for the same reason that issue 018 forbids
interpreter probing. The manager and the CLI come from one bundle, so their
API versions always match. The manager still checks the `"api"` field and
refuses a mismatch.

Exit codes follow section 10.1. One `install`, `update` or `uninstall` call
may carry several targets. The manager uses that to group every system-mode
target into **one** elevated call, so the user sees a single UAC prompt.

Contract tests pin the JSON shapes: the plan document, the result document
and every event type.

## 11. Settings file

There is one file format, `settings.toml`, with layered locations. The
manager config `gupkg-config.toml` (schema 2) is migrated by
`gupkg settings init --from <old>`.

Layers, lowest to highest priority. All of them are files in roots
(section 9.7):

1. **Built-in defaults.** These include the default roots `C:\opt`,
   `%USERPROFILE%\opt` and `%USERPROFILE%\opt-portable`.
2. **`$App\settings.toml`** of the running gupkg, shipped with a release
   (optional).
3. **The system layer, `C:\opt\.gupkg\settings.toml`.** It is the data
   directory of the root in which the system gupkg is installed, or of the
   default system root. It may change `[roots]`. The root it names becomes
   the system root for the other settings, which is how roots stay
   configurable without any lookup outside the roots.
4. **The user layer, `<user root>\.gupkg\settings.toml`.** It is ignored in
   elevated and system-mode runs.
5. **`--settings PATH`.** It is ignored in elevated runs unless `PATH` is
   inside the admin-only system root.

A bundled gupkg reads the same files when they exist. On a machine without
an installed gupkg they usually don't, and the built-in defaults apply.

```toml
schema = 3
default_mode = "auto"         # auto (elevated -> system, otherwise user) | system | user

[roots]
system = 'C:\opt'
user = '%USERPROFILE%\opt'
portable = '%USERPROFILE%\opt-portable'

[integration.system]
bin = 'C:\bin'
start_menu = '%ProgramData%\Microsoft\Windows\Start Menu\opt'
[integration.user]
bin = '%USERPROFILE%\bin'
[integration.portable]
integrate = "user"            # user | none

[manager]                     # read only by gupkg-manager; the CLI ignores this table
checksum_missing = "ask"      # ask | refuse | skip
keep_screen = true            # print the last run's summary to the console after the UI closes

[[registry]]
name = "main"
source = "https://github.com/guraltsev/gupkg-registry.git"

[logs]
keep_runs = 50
keep_days = 90

[ui]
color = "auto"                # auto | always | never (NO_COLOR is honoured)

[hooks]
allow_vendor_installers = true
# There is no dependency-install switch. Per issue 016, hooks may import only
# the standard library, what the release bundle ships, and their own pkg.local\.
```

Machine state that is not configuration lives in `state.toml` in the same root
data directory: the ad-hoc roots and the last registry sync. Users never need
to edit it.

The current module-directory-only lookup (`manager.manager_config_candidates`)
and its disagreement with issue 013's documented search order both go away.

## 12. Architecture and module boundaries

```text
gupkg/
  model/        pure data, no I/O
    version.py      version parsing/ordering, reserved names
    manifest.py     pkg.toml format 2: parse, validate, typed dataclasses, templates
    settings.py     settings schema + layering
    state.py        PackageView, LocalState, InstalledState, status derivation
    plan.py         InstallPlan, Phase, Effect (data only)
  catalog/      discovery and joins (reads disk, no mutation)
    local.py        scan roots, build LocalState
    registry.py     registry cache, sync, validation
    catalog.py      describe(), list(), resolve_selector(); the only place state is derived
  services/     built-in declarative operations
    http.py         shared client (UA, retry, TLS, token)
    track/          github, gitlab, page, index, redirect, json, last_modified, rss, git, chain, module
    unpack/         zip, 7z, sfx, nsis, msi, inno, file, git; strip/subdir/nested/extract/require
    prepare.py      remove, rename, copy, write, module
    checksum.py     sidecar, sums_file, json, page; verification
    hooks.py        loader + HookContext + hooklib re-exports
  system/       THE side-effect boundary
    port.py         System protocol (fs links, env/PATH, shortcuts, broadcast, elevation, processes, http fetch)
    windows.py      real implementation
    fake.py         in-memory/tempdir implementation for tests and --sandbox
  engine/       planning and execution
    planner.py      PackageView + request -> InstallPlan (pure)
    executor.py     runs phases, emits events, writes receipts/install record, rollback
    lifecycle.py    install/update/switch/uninstall/cleanup/doctor entry points
  report/       events and rendering
    events.py       event dataclasses + Reporter protocol (contextvar)
    console.py      Rich renderer
    logfile.py      text + JSONL log writer, retention
  cli.py        argparse grammar -> lifecycle calls -> exit code; --json/--plan/--events-file/--no-input
  selfmgmt/     self status/repair: MANIFEST.sha256 check, bundled-runtime enforcement (issues 016, 018); self check/update are aliases (section 20)
  migrate/      0.17 pkg.toml -> format 2, _bootstrap -> _check_updates, gupkg-config -> settings
```

```text
gupkg_manager/  separate program (gupkg-manager.exe shim); ships in the same bundle
  client.py       runs `gupkg` subprocesses; parses --json / --plan; follows --events-file
  batch.py        inventory -> plan -> confirm -> grouped execution (one elevated call per batch)
  policy.py       [manager] checksum_missing ask/refuse/skip
  app/            Textual UI: browser, package view, run view (renders the event stream)
    cli.py          gupkg-manager [PATH] | check --all | update --all [--mode M] [--yes] | doctor
```

Dependency rules:

- **Inside `gupkg`:** `model` ← `catalog`, `services` ← `engine` ← `cli`.
- **`system`** is used only through the `System` protocol that `engine` and
  `services` receive.
- **No input or output below `cli`.** No module below it prints or reads
  input.
- **No hidden cycles.** There are no function-local imports to hide them.
- **`gupkg_manager` imports nothing from `gupkg`.** A test enforces this. The
  manager talks to gupkg only through the machine interface of section 10.4,
  so it could be rewritten, or replaced by another front end, without
  touching the engine.

This retires several of today's modules:

- **Split up:** `commands.py` (1,190 lines), `manager.py`, `gupkg.py`,
  `updates.py`, `origin.py` and `configuration.py`.
- **Merged:** the four hand-written TOML emitters become one writer,
  `tomli_w`. It is added to the runtime lock as a dependency in its own
  right, through the dev-tree lock tooling of issue 016
  (`tools/update_deps.py`). Its only copy today is vendored inside pip, which
  issue 019 removes from the bundle.
- **Replaced:** `LineStream`, global `redirect_stdout`, `_quiet`, and the
  manager callback types.

## 13. Execution engine

### 13.1 Phases

Every mutating command is an `InstallPlan`: an ordered list of phases, each
with steps and declared `Effect`s. The planner is pure; the executor applies
it through `System`.

| Phase | Work |
| --- | --- |
| **Resolve** | Selector → `PackageView`; choose version; mode checks; elevation check (relaunch once, up front, for the whole call; the manager groups system targets into one call, section 10.4). |
| **Fetch** | Download to `.gupkg\work` (shared cache by URL+SHA-256 under the mode's cache dir); resume; progress events. |
| **Verify** | Checksum rule (section 9). |
| **Unpack** | `[source]` services into staged `App`. |
| **Prepare** | `[[prepare]]`, mode adjustments (`[portable]` markers / `[nonportable]` remove), `require` check; atomic rename into the version directory. |
| **Activate** | Portable data hand-over; switch `current`. |
| **Integrate** | Diff desired effects (shortcuts, env, PATH, shims) against the install record: create/update new ones, **remove stale ones**; broadcast environment change once. |
| **Finish** | Write receipt + install record; summary. |

An install plan never contains a tracker step. `check` is a separate plan:

| Check phase | Work |
| --- | --- |
| **Resolve** | Selector → `PackageView`; the package must have a tracker (local, or copied in from the linked registry). Never elevated. |
| **Track** | Run the provider; the catalog compares the result with the known versions. |
| **Write** | Render and validate the pinned `pkg.toml`. Write the version directory (user and portable roots) or a proposal (system roots, section 9.3). |
| **Finish** | Record the result in `.gupkg\state\track.toml`; report the new versions. |

A failure before **Activate** leaves the system unchanged. A failure in
**Integrate** reports exactly which effects were applied, and `doctor --fix`
or a re-run converges on the declared state.

### 13.2 Batches

**In the engine.** A CLI call with several targets is a sequence of
per-package plans:

- the targets are resolved once;
- elevation is checked once for the call;
- the run continues past failures;
- per-package output is never discarded: it goes to the same event stream
  and log as any other run.

**In the manager.** Cross-package orchestration is `gupkg-manager`'s job
(section 15):

1. **Inventory:** `gupkg list --json`.
2. **Plan:** `gupkg update <targets> --plan --json`.
3. **Confirm.** The plan is confirmed once, and the checksum policy may ask
   per package.
4. **Run, grouped by mode.** One non-elevated `gupkg update <user targets>`
   call, then one elevated `gupkg update <system targets>` call.
5. **Report.** The manager shows the combined result.

The CLI holds no confirmation logic. Today the CLI's `input()` and the TUI's
confirmation are two separate implementations.

### 13.3 Uninstall and cleanup

- **`uninstall`** removes every effect in the install record, then `current`.
  The version directories stay, so the package becomes `local` again.
- **`uninstall --purge`** also deletes the package root, including `data\`.
- **`cleanup`** removes:
  - non-current *recoverable* `App`s beyond `--keep N` (default 1 previous).
    This is safe because a recoverable `App` can be rebuilt (section 4). Each
    version directory and its `pkg.toml` stays, so switching back rebuilds
    `App` from `[source]`. A shipped `App` is never dropped;
  - receipts of versions that no longer exist;
  - `.gupkg\work`;
  - registry trees other than the active one;
  - logs beyond retention.

## 14. Events, console output, and logs

### 14.1 Events

`report.events` defines frozen dataclasses:

- `RunStart(command, argv, mode, elevated, version)`
- `PackageStart(view)`
- `PhaseStart(phase)`
- `Step(text, kind)`
- `Progress(task, done, total, unit)`
- `SubprocessLine(cmd, line, stream)`
- `Prompt(...)`
- `Warning(text)`
- `Error(text, hint)`
- `PhaseEnd(phase, status, elapsed)`
- `PackageEnd(result)`
- `RunEnd(summary, exit_code)`

How they flow:

- **Producers.** The engine and services call `report.emit(event)`. The
  `Reporter` is held in a `contextvars.ContextVar`, so worker threads inherit
  it.
- **Reporters.** Several reporters run at once: console + log file, or UI +
  log file.
- **Subprocesses.** Output from git, pip, 7-Zip, installers and hooks is
  captured line by line as `SubprocessLine`. Nothing writes to the terminal
  behind the UI's back.
- **Warnings appear once.** Today they are printed live and again in the
  summary.

### 14.2 Console renderer (Rich, already bundled)

```text
gupkg update firefox  ·  user  ·  log: %USERPROFILE%\opt\.gupkg\logs\20261022T101500Z-update-firefox.log

━━ Resolve ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    firefox  134.0  →  135.0.1 (local version directory, written by an earlier check)
━━ Fetch ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Firefox Setup 135.0.1.exe  ████████████████████░░░░  61.2/74.0 MiB  9.8 MiB/s
━━ Verify ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓ sha256 matches (vendor SHA256SUMS)
━━ Unpack ━━━━ … ━━ Prepare ━━━━ … ━━ Activate ━━━━ … ━━ Integrate ━━━━━━━━━━━━
  + shortcut  Firefox
  + shim      firefox.exe
  − shortcut  Firefox (old name)        stale, removed
━━ Done ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ✓ firefox 135.0.1 installed (user, was 134.0) in 14.2s
```

Rendering rules:

- **Phase headers** are bold and coloured: cyan while running, green when
  done, red when failed, dim when skipped.
- **Detail lines** are indented; warnings are yellow and errors red.
- **The summary is always last.**
- **Redirected output.** When output is not a terminal, or `NO_COLOR` is set,
  the renderer prints plain `==> Phase` headers, no carriage returns, and a
  final progress line only.

### 14.3 Logs

| | Location |
| --- | --- |
| a run that acts on a root | `<root>\.gupkg\logs\` of that root: `C:\opt\.gupkg\logs\` (admin-only) for system mode, `%USERPROFILE%\opt\.gupkg\logs\` for user mode, the portable or ad-hoc root's own for those |
| a run that acts on no root (`search`, `registry status`) | the data directory of the running gupkg's own root, or nothing for a bundled gupkg with no root |

- **Two files per run.** Each run writes `<UTC yyyymmddThhmmssZ>-<command>[-<package>].log`
  (plain text, flushed per line, no ANSI) and a `.jsonl` file with the same
  events. `latest.log` is a copy of the last run's text log.
- **Receipts point at their log.** Each receipt stores the log path, and
  `gupkg logs <target>` lists or opens a package's logs.
- **Retention** follows `[logs]`, and pruning happens when a run starts.
- **A logging failure never fails an operation.** It produces one warning.

## 15. The manager program (`gupkg-manager`)

`gupkg-manager` is a separate program, decided by the owner on 2026-10-22.
It ships in the same bundle as the CLI.

- **Commands.** `gupkg-manager` with no arguments opens the browser;
  `gupkg-manager PATH|NAME` opens the package view. The non-interactive
  commands for schedules are `gupkg-manager check --all`,
  `gupkg-manager update --all [--mode M] [--yes]` and `gupkg-manager doctor`.
- **It only calls the CLI.** Everything it does goes through the CLI's
  machine interface (section 10.4).
- **It owns orchestration:**
  - the inventory views (built from `gupkg list --json`);
  - `--all` batches;
  - planning and confirmation;
  - the `[manager] checksum_missing` policy (section 9.1);
  - grouping system targets into one elevated CLI call;
  - the interactive UI.
- **Rendering.** It renders the CLI's event stream, read from
  `--events-file`, with the same phase styling as the console renderer.
- **Scheduled runs.** A schedule typically runs `gupkg-manager check --all`,
  then `gupkg-manager update --all --yes` (D11 and section 5.3). These are two
  separate operations, so the new version folders can be reviewed in
  between.

Textual stays for the interactive parts: browsing, filtering and choosing.
It is pinned and already bundled. The layout changes completely:

```text
 ↑↓ move  Enter open  / filter  i install  u update  x uninstall  l logs  ? help  q quit   ← key-hint bar (1 line, always)
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ firefox      135.0.1   user    outdated → 136.0   main                              │ ← body: package table, or the
│ ripgrep      14.1.0    system  installed           main                              │   run log (RichLog, append-only,
│ my-tool      0.3       user    local               —                                 │   follows the tail, scrollable)
│ …                                                                                    │
└─────────────────────────────────────────────────────────────────────────────────────┘
 3 outdated · 1 broken · registry main @ 4f2a9c1 (synced 2h ago) · log: …\latest.log      ← status footer (1 line)
```

- **One `App`, many screens.** Opening a package, running an operation and
  returning to the browser all happen inside one application. It never exits
  and relaunches (today's `while True` loop between `GupkgApp` and
  `ManagerApp`).
- **The run view uses the console renderer's events.**
  - It shows the same coloured phase headers in an append-only `RichLog`,
    which ends the O(n²) re-render.
  - It keeps a progress bar under the active phase.
  - The verdict is written **at the end of the log**, and repeated in the
    footer.
  - While a run is active, Esc asks "cancel after the current step?". It is
    never a silent leave.
- **Package view.** It shows versions (local, registry, tracker), mode, link,
  data folder, the last receipt and the log, with actions on keys.
- **`gupkg-manager.cmd`** (the package launcher shipped in each version
  folder, which replaces `gupkg-tui.cmd`) finds a gupkg as described in
  section 21.3. It then runs `gupkg-manager <its folder>`, which opens
  straight into the package view.
- **The manager runs operations as CLI subprocesses.** It follows their
  `--events-file`, so it is never blocked and never touched by child-process
  output. It also does not need the old process-global `redirect_stdout`.
- **The style guide changes.** `_docs/tui_style_guide.md` is revised: one
  key-hint line and one status line are now *required*. Today the guide bans
  both.

## 16. Testability

- **One boundary.** `system.port.System` is the only way to:
  - create junctions, symlinks or hard links;
  - read or write environment and PATH values;
  - create or delete shortcuts;
  - broadcast environment changes;
  - query or request elevation;
  - run processes;
  - fetch URLs.

  `WindowsSystem` implements it for real. `FakeSystem` keeps the registry
  values, shortcut records and elevation state in memory, uses real temporary
  directories for files, and serves URLs from a fixture map.
- **Plans are data.** Planner tests assert on `InstallPlan`, which is pure.
  Executor tests run a plan against `FakeSystem` and assert on the resulting
  state: files, the env map, shortcut records and receipts. Neither patches
  module attributes.
- **Sandbox mode.** `gupkg --sandbox DIR …` runs the real CLI with
  `FakeSystem` persisted under `DIR`. That lets a whole install, update or
  uninstall be exercised on a real machine with no registry, Start Menu or
  PATH changes. Manual smoke tests and package authors (`gupkg pkg check
  --install`) use it.
- **Fixtures for services.** Archive fixtures are built at test time: zip, 7z,
  and nested archives. HTTP fixtures are recorded pages for the providers.
  Each provider is tested against recorded real-world pages from the registry
  cases in section 8.1.
- **Real-Windows tests are marked `windows_integration`.** These are the
  `WindowsSystem` contract tests: junction, HKCU in a throwaway key, a
  shortcut in a temporary folder. The same contract tests also run against
  `FakeSystem`, so the two cannot drift.
- **Registry CI tests stay offline.** In the registry repository, `gupkg pkg
  check` validates every manifest and runs every tracker against recorded
  responses.

## 17. Registry 2

Layout:

```text
gupkg-registry/
  <name>/
    _check_updates/        pkg.toml (+ pkg.local, support files)     # tracker (optional)
    <version>/      pkg.toml, fully pinned, no App             # newest N pinned versions (N=2)
  _registry.toml    format = 2, name, description
```

- **CI keeps the pinned versions current.** A scheduled GitHub Action
  ("autoupdate") runs every `_check_updates` tracker. For each new upstream version
  it:
  1. downloads the artifact;
  2. verifies it against the discovered checksum, or records its SHA-256 with
     `sha256_from = "registry-ci"` when the vendor publishes none;
  3. runs the unpack and prepare phases in a sandbox to prove the package
     installs;
  4. commits `<name>/<version>/` and prunes versions beyond N.

  The registry is therefore the place where a hash is computed and reviewed.
  Local installs never auto-populate hashes.
- **Every registry package installs under the strict CLI default** (no
  `--skip-checksum` needed). That is the acceptance bar for the migration.
- **Several registries.** `[[registry]]` entries can name several registries,
  for example the official one plus a private share. The selector
  `registry/name` disambiguates; otherwise the first registry that has the
  name wins, and `search` shows all of them.
- **Cache location.** The cache moves out of the gupkg version's `App` into
  the root data directory, `<root>\.gupkg\registry\`, so it survives
  self-updates. System runs use only the system root's cache. Old trees are
  pruned by `cleanup`.
- **Validation is per package.** One invalid package is reported and skipped.
  It no longer fails the whole sync.

## 18. Migration

- **`gupkg pkg migrate <path>`** converts a version directory or `_bootstrap`
  to format 2:
  - `[origin]` and `[update.*]` become `[source]` and `[track]`. The
    overloaded `[origin] url` is split by meaning: a GitHub or project page
    becomes `project_url`, or `[track] repo`; a real artifact becomes
    `[source] download_url`. `homepage` becomes `project_url`;
  - `[[origin.versions]]` is dissolved. The entry whose `version` matches the
    directory name becomes the single `[source]`. Every other entry is
    reported, with a suggested `gupkg pkg new <name>@<version>`, and dropped.
    Leftover `name`, `version` and `localVersion` keys are deleted;
  - camelCase keys become snake_case;
  - `only_portable` becomes `modes`;
  - `ignore_checksum` is dropped with a note;
  - `_bootstrap` is renamed to `_check_updates`;
  - hooks that match a known service pattern are reported with the suggested
    declarative replacement.

  It writes a backup by default and validates the result before replacing the
  file.
- **`gupkg settings init --from gupkg-config.toml`** converts manager config
  schema 2.
- **Existing installs need no reinstall.**
  - The first 1.0 run over a root writes `.gupkg\package.toml` install records
    from the existing `current` and the declared components, and marks those
    components "adopted".
  - Old receipts and `.gupkg\state\update.toml` are read once, then removed.
- **gupkg's own package** (`src\pkg.toml`, `src\pkg.local\check_update.py`,
  `unpack_app.py`) is converted to format 2 as described in section 20, and
  both hooks are deleted.
- **The registry is converted with a script in the registry repository.** It
  uses `gupkg pkg migrate`. The 62 hooks are then replaced package by package
  (issue 031). Hooks left with no remaining purpose are deleted.
- **Compatibility window.** For one minor release (1.0.x), format 1 manifests
  are refused with an error that names `gupkg pkg migrate`. They are never
  silently read. `gupkg manager …` prints the new command (section 10.3).

## 19. Decisions taken and open questions

The decisions below were taken when this design was written. Each one can be
overruled in issue 020 before the issue that depends on it starts.

| # | Decision | Alternatives considered |
| --- | --- | --- |
| D1 | Rename `_bootstrap` to **`_check_updates`** (owner's choice, 2026-10-22: the folder's only job is to check for a newer upstream version and write a new version directory). | `_latest`, `_track`, `_upstream`, `_channel` |
| D2 | A local package follows a registry entry only through an explicit **link**, written by `install <name>` from the registry or by `gupkg link`. No implicit matching by name. | Name match implies link (Scoop-like, but would silently merge a custom local package with an unrelated registry entry) |
| D3 | A checksum **mismatch is always fatal**; `--skip-checksum` covers only missing hashes. | Allow `--skip-checksum` to also bypass mismatches (Scoop's `--skip-hash`) |
| D4 | `default_mode = "auto"` (owner's decision, 2026-10-22). Elevated selects `system` (a package that doesn't support system is refused: an elevated gupkg never acts on a user root, section 9.6); non-elevated selects `user`; never `portable`. It applies only when no flag, install record or containing root decides, and it means the same in every command. | `default_mode = "user"`, with elevation never selecting system (the earlier draft) |
| D5 | Format 2 is strict, with an explicit migration tool; no dual-format reader. | Read both formats for one release |
| D6 | Keep Textual for the interactive UI (redesigned, in `gupkg-manager`); Rich for console runs. | Rich-only with inline prompts; prompt_toolkit |
| D7 | Portable mode persists declared paths in `<package>\data` and links/copies them into each `App`. | Keep everything in `App` and copy the whole old `App` forward on update |
| D8 | Registry CI computes hashes for vendors that publish none (`sha256_from = "registry-ci"`). | Leave such versions unhashed and rely on the missing-checksum policy |
| D9 | `update` never installs a package that is not installed. | `update` installs missing packages |
| D10 | **Version directories contain no update-check code** (owner's decision, 2026-10-22). `_check_updates` is the only component that discovers anything over the network. Version-directory hooks get no network access, and checking never runs elevated (section 9.3). | Allow `[track]` in pinned versions so that a lone version directory can update itself |
| D11 | **Checking and installing are separate operations** (owner's decision, 2026-10-22). `gupkg check` runs trackers and writes pinned version directories (or system-root proposals), and stops. `install` and `update` install only already-known versions and never run a tracker. No flag combines the two. | The tracker hands the install off to the new version (the earlier draft); an `update --check` convenience flag |
| D12 | **The manager is a separate program, `gupkg-manager`** (owner's decision, 2026-10-22). It imports nothing from `gupkg` and drives it only through the versioned machine interface (`--json`, `--plan`, `--events-file`, `--no-input`). The CLI acts on explicit targets, with no `--all`, no UI and no checksum prompts. | A `manager` subcommand or mode inside the CLI (0.17); the manager importing the engine directly |
| D13 | **Stage 0 is issues 016–019, and they land first.** gupkg runs only on its bundled interpreter (`-I -s -B`). There is no runtime pip, dependency install or dependency check, and the bundle integrity is `MANIFEST.sha256`. The rehaul builds on these rather than redesigning them (section 20). | Fold them into the rehaul issues |
| D14 | **gupkg is an ordinary format 2 package of itself.** It has a `_check_updates` tracker for GitHub releases, a checksum from `RELEASES.md` and `verify_manifest`. `self check` and `self update` are aliases for `check gupkg` and `update gupkg`, so D11 applies to gupkg too. | Keep self-update as special-cased hooks (`src/pkg.local`) |
| D15 | **Bundled mode** (owner's direction, 2026-10-22). Any version directory may carry a complete gupkg in `<version>\gupkg\`, so a program can be installed **without installing gupkg**. A bundled install keeps (and carries forward) its bundled copy to manage itself later, and never writes a root forwarder. gupkg's own package always carries a bundled gupkg, which installing copies into `App` (`[source] local = "gupkg"`). The engine never executes a bundled tree (section 21). | A bundled install also installs gupkg (rejected by the owner: no extra infrastructure); gupkg's payload outside `App` (`into = "gupkg"`, an earlier draft) |
| D16 | **Package launchers find gupkg relative to themselves.** They try the root forwarder `%~dp0..\..\.gupkg\gupkg.cmd` first, then the bundled `%~dp0gupkg\gupkg.cmd`. They never use `PATH`, the registry, AppData or ProgramData. Elevated launchers first verify, with PowerShell `Get-Acl` from System32 and failing closed, that their root and version directory are not user-writable, and refuse otherwise. Exit code 6 triggers the fallback to the bundled copy (section 21.3). | Search `PATH` (rejected: a fake `gupkg.exe` cannot be trusted to refuse itself); registry registration; locator files in AppData or ProgramData |
| D17 | **A distribution folder outside every root is copied into the mode's root, then installed** (`--in-place` opts out). System-wide installs of a distribution are done only by the installed system gupkg from an admin shell, which copies the data and runs nothing from the source. A system bootstrap extracts straight into an admin-only `C:\opt` (section 21.4, 21.5). | Elevating a launcher in Downloads and copying with `robocopy` (an earlier draft) |
| D18 | **The Windows registry is a write target, never a source of information** (owner's decision, 2026-10-22). gupkg never reads it to learn anything: not its own location, not settings, not package state, not installed software. Reads happen only as part of a change (read-modify-write of `PATH`) or to verify a value gupkg wrote. There is no generic registry table; specific effects such as file types may come later (section 9.5). | A `Software\gupkg` registration for discovery (an earlier draft); a generic `[[winreg]]` table |
| D19 | **Threat model T1** (owner's decision, 2026-10-22). gupkg runs as administrator for system mode. A standard user must not gain administrator rights through gupkg: an elevated gupkg never runs code, and never reads inputs, that a standard user can modify. Only an admin-only gupkg elevates, and only by relaunching itself (section 9.6). | T0: treat elevation as a convenience only |
| D20 | **gupkg owns no files outside its roots** (owner's decision, 2026-10-22). Its settings, logs, registry cache, state, proposals and the root forwarder live in `<root>\.gupkg\`. It never uses `%LOCALAPPDATA%`, `%APPDATA%` or `%ProgramData%` for its own data (section 9.7). | Per-user and machine-wide AppData/ProgramData folders (an earlier draft, taken from 0.17's embedded site-packages) |

Open questions:

1. **The default portable root.** `%USERPROFILE%\opt-portable` or a per-user
   prompt on first use.
2. **How many pinned versions the registry keeps.** `N = 2` is proposed.
3. **Whether `cleanup` runs automatically** after `gupkg-manager update --all`. It is
   proposed to be off by default, with a `[cleanup] auto = true` setting.
4. **Whether every install records an `App` manifest.** This would generalise
   issue 016's bundle check to all packages: the receipt stores
   `<version>.manifest.sha256`, and `doctor --verify` reports modified,
   missing or extra files. The cost is hashing large trees such as msys2 at
   install time. The proposal is on by default, with `[doctor]
   record_manifests = false` to switch it off.
5. **Whether `cleanup --bundled` runs by default** for distributions
   installed while the root has a forwarder to a newer installed gupkg. A bundled copy
   costs about 40 MB per version directory (section 21.8).
6. **Whether to offer a lite bundle** (`gupkg pkg bundle --lite`). It would
   contain the CLI and the embedded Python without `gupkg_manager` and
   Textual, for small programs. Its `gupkg-manager.cmd` would then offer only
   CLI actions.

## 20. Runtime and distribution (issues 016–019)

Issues 016–019 were written separately on 2026-10-22. They cover a different
part of the system from sections 1–19: how gupkg itself is built, shipped and
started, rather than how it manages other packages. Neither side contradicts
the other. This section records how they fit together. Each issue also has
an "Integration with the 1.0 rehaul" section with the same points.

### 20.1 What stage 0 provides, and what the rehaul relies on

| Issue | Decision | The rehaul relies on it for |
| --- | --- | --- |
| 016 | One self-contained release ZIP. Integrity is computed at build time (`MANIFEST.sha256`, ZIP SHA-256 in `RELEASES.md`) and only *checked* at run time. Runtime acquisition and dependency tooling move to `tools/`. `dependencies.py`, `locked_deps.py`, `dependency_notices.py`, `--allow-hook-dependency-install` and `self repair-deps/update-deps` are deleted. | The rule that hooks import only the standard library, the bundle and their own `pkg.local` (sections 8.6, 9.3). The absence of runtime pip from the trust model (section 9.4). `gupkg-manager` importing Textual directly from the bundle (section 15). |
| 017 | Shortcuts through COM `IShellLinkW` (`shell_link.py`), with no PowerShell. Optionally, junctions through `DeviceIoControl` instead of `mklink`. | The shortcut and junction implementations behind `System` (section 16, issue 021). Shortcut read-back also lets `doctor` detect drift without starting a process. |
| 018 | Exactly one interpreter, `<version>\gupkg\python\python.exe -I -s -B`, enforced on every start (`enforce_bundled_runtime`). Development uses `.venv`. | The same launch rule for `gupkg-manager` and for the elevated relaunch (sections 9.6, 13.1). Subprocess tests start `gupkg.cmd`, never `sys.executable -m gupkg`. |
| 019 | No pip in the bundle or the lock; the build fails if pip is present; a build probe imports every locked module. | Adding `tomli_w` as a locked runtime dependency (section 12). The build probe must also import `gupkg_manager`. |

### 20.2 What the rehaul adds on top

1. **gupkg's own package always carries a bundled gupkg, which becomes its
   `App`** (D14, D15, section 21). Its distribution is a version directory
   holding `pkg.toml`, the two package launchers and `gupkg\` (the bundled
   runtime).
   - Installing the package copies the bundled tree into `App`
     (`[source] local = "gupkg"`), and the installed gupkg runs from `App`.
     gupkg is thus an ordinary "`App` is the payload" package.
   - This removes the 0.17 workaround in `src\pkg.local\unpack_app.py`,
     which keeps the tree at the version root and writes an
     `App\RELEASE.txt` marker only so that the engine counts the version as
     populated.
   - Issue 016's ZIP layout is the distribution layout of section 21.2.
2. **Self-updates follow D11.** `gupkg self check` and `gupkg self update`
   are aliases for `gupkg check gupkg` and `gupkg update gupkg`. In 1.0 the
   0.17 hooks of issue 016 become a declarative tracker (section 21.6).
   - The registry may also publish pinned `gupkg\<version>\` directories,
     so `gupkg update gupkg` works for linked installs with no local check.
3. **`verify_manifest` is a general unpack service** (section 8.2). It checks
   the unpacked payload against a `<sha256>  <path>` manifest shipped in the
   artifact. gupkg's own bundle (`MANIFEST.sha256`) is the first user. Open
   question 4 proposes recording an `App` manifest for every install.
4. **`self status` and `doctor`.** Issue 016's "check the active version
   against `MANIFEST.sha256`" becomes `gupkg self status`. gupkg's own
   package also shows up in `gupkg doctor` like any other package.
5. **The manager runs its sibling CLI** (section 10.4). It always uses the
   `gupkg.cmd` next to it in the same bundle, never one found on `PATH`. This
   follows issue 018's rule against interpreter probing.
6. **The bootstrap script becomes thin** (section 21.5). `gupkg-bootstrap.ps1`
   downloads and verifies a release ZIP, extracts it, and runs its
   `install.cmd`. For system mode it first creates an admin-only `C:\opt`
   and extracts into it (section 21.5). Installing the bundled gupkg itself
   does the rest, including the root data directory with settings schema 3.
   That replaces `manager init`.
7. **One documentation pass.** Issues 016 and 018 update the README and
   `_docs` for the new distribution. Issue 032 rewrites those same pages for
   1.0 and must build on their text, not replace it.

### 20.3 Order

```text
stage 0:  017 ──────────────┐                     (shortcut/junction implementation)
          016 ─► 018, 019 ──┼─► 0.18 release
stage 1+: 021 (System boundary; moves 017's code behind the port) ─► … ─► 032 (1.0)
```

Stage 0 needs no format change. It can ship as 0.18 while the rehaul issues
are worked on. If 017 lands after 021 has started, 021 moves the PowerShell
shortcut code behind the boundary, and 017 replaces it there.

## 21. Bundled mode, package launchers, and bootstrap

Bundled mode, as the owner defined it on 2026-10-22, is **a way to install a
package without installing extra infrastructure**. A program's distribution
can carry gupkg inside its own version directory, as `<version>\gupkg\`.
Someone unzips the folder anywhere and runs `install.cmd`. The program is
then installed with its shortcuts, shims, environment and PATH, and **gupkg
itself is not installed**: no gupkg package, no root forwarder, no gupkg
shims, and no gupkg `PATH` entry appear on the machine.

gupkg's own package uses the same mechanism. Its distribution *always*
carries a bundled gupkg. When the package is installed, that bundled tree is
copied into `App`, and the installed gupkg runs from `App`. gupkg is
therefore an ordinary package in which `App` is the payload, and the bundled
copy is only the means of getting it there.

Everything in this section follows the threat model of section 9.6 (T1), the
registry rule of section 9.5, and the rule that gupkg owns no files outside
its roots (section 9.7).

### 21.1 Terms

| Term | Meaning |
| --- | --- |
| **Root** | A folder that holds packages, one subfolder each, with one install mode (section 3). Default roots: `C:\opt` (system), `%USERPROFILE%\opt` (user). |
| **Root data directory** | `<root>\.gupkg\`: gupkg's own files for that root (settings, logs, registry cache, state) and the **root forwarder**. gupkg owns no files outside its roots (section 9.7). |
| **Root forwarder** | `<root>\.gupkg\gupkg.cmd` and `gupkg-manager.cmd`: two-line scripts that call the installed gupkg (`…\gupkg\current\App\gupkg.cmd`). Package launchers find an installed gupkg through it, at a fixed position relative to themselves (section 21.3). |
| **Bundled gupkg** | A complete gupkg runtime tree in `<version>\gupkg\`: launchers, the `python\` of issue 018, the `gupkg` and `gupkg_manager` Python packages, `VERSION` and `MANIFEST.sha256` (issue 016). It needs nothing installed. |
| **Installed gupkg** | The `current` version of the `gupkg` package in a root (`<root>\gupkg\current\App\`). Only installing the `gupkg` package creates one, and that install writes its root's forwarder. |
| **Bundled install** | A package installed by a bundled gupkg, with no installed gupkg involved. It writes no forwarder. The package keeps its bundled copy, so it can later be updated, repaired and uninstalled the same way (section 21.4). |
| **Distribution** | A ZIP of a package-root fragment (`<name>\<version>\…`, optionally with `<name>\_check_updates\`) whose version directory carries a bundled gupkg and the package launchers. |
| **Package launchers** | `install.cmd` and `gupkg-manager.cmd` in a version directory. They find a gupkg to run (section 21.3). `gupkg-manager.cmd` replaces today's `gupkg-tui.cmd`. |

### 21.2 Layouts

```text
C:\opt\                               a root (system mode, admin-only)
  .gupkg\                             root data directory (section 9.7)
    gupkg.cmd  gupkg-manager.cmd      root forwarder -> C:\opt\gupkg\current\App\...
    settings.toml  state.toml  logs\  registry\
  gupkg\                              gupkg's own package
    current\                          -> 1.0.0
    1.0.0\
      pkg.toml                        [source] local = "gupkg" (+ download_url for repair)
      App\                            the installed gupkg runs from here
        gupkg.cmd  gupkg-manager.cmd  python\  gupkg\  gupkg_manager\  MANIFEST.sha256  VERSION
      gupkg\                          bundled copy: redundant once App is populated; removed by cleanup
      install.cmd  gupkg-manager.cmd
    _check_updates\pkg.toml
  firefox\
    135.0.1\
      install.cmd  gupkg-manager.cmd  launchers find C:\opt\.gupkg\gupkg.cmd (two levels up)

MyApp-1.2.0.zip                       a distribution of another program
  myapp\
    1.2.0\
      pkg.toml                        ordinary format 2 pinned version
      App\                            empty (filled from [source]) or shipped (section 4)
      gupkg\                          bundled gupkg (reserved name)
      install.cmd  gupkg-manager.cmd
    _check_updates\                   optional

gupkg-1.0.0-win-amd64.zip             gupkg's own distribution (issue 016)
  gupkg\
    1.0.0\  pkg.toml  gupkg\  install.cmd  gupkg-manager.cmd
    _check_updates\pkg.toml
```

Rules:

- **Reserved names inside a version directory:** `gupkg\`, `install.cmd` and
  `gupkg-manager.cmd`, like `pkg.local`. The engine reads, verifies and
  copies a bundled gupkg, and never executes it. Only package launchers run
  a bundled gupkg.
- **`[source] local = "<dir>"`** populates `App` by *copying* a folder that
  ships inside the version directory, after checking it with
  `verify_manifest`.
  - It falls back to `download_url` when the folder is missing or does not
    match. Without a `download_url`, `App` counts as shipped (section 4).
  - It never moves the folder, because during a first install that tree may
    be the running gupkg.
  - gupkg's own package is the main user.

### 21.3 Package launchers: which gupkg runs

`install.cmd` and `gupkg-manager.cmd` are generated from one template, by
`gupkg pkg bundle` and by the registry's launcher sync. They are plain `cmd`.
A launcher lives at `<root>\<name>\<version>\`, so its root is `%~dp0..\..`.

**Non-elevated run:**

1. `%~dp0..\..\.gupkg\gupkg.cmd`: the root forwarder, meaning the installed
   gupkg that manages this root.
2. `%~dp0gupkg\gupkg.cmd`: the bundled gupkg.
3. If neither exists, print how to get gupkg (the bootstrap URL) and exit
   9009.

**Elevated run.** The same lookup applies, but first the launcher must
establish that the location is not user-writable. It **refuses to proceed**
otherwise.

- **What is checked:** the root (`%~dp0..\..`) and the launcher's own version
  directory (`%~dp0`). Both must be owned by Administrators, SYSTEM or
  TrustedInstaller. No other security identifier may hold any write-type
  right on either: write, append, create files or folders, delete,
  delete-child, change permissions, or take ownership.
- **Why those two are enough.** Everything below them inherits their
  permissions. Once a gupkg has been reached through a verified path, it is
  trusted code and checks the rest of the tree itself: `current`, `App` and
  `MANIFEST.sha256`.
- **How it is checked.** With a tool that is not inside the location being
  checked, because a checker shipped there could have been replaced. The
  launcher calls
  `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile
  -NonInteractive` with a fixed `Get-Acl` script that compares security
  identifiers, so it does not depend on localized account names.
  - The script parses the SDDL text of `Get-Acl`'s `.Sddl` property and
    invokes no .NET methods, so it also runs under Constrained Language
    Mode, which blocks method calls such as `GetAccessRules(...)` (job 07,
    spike S1).
  - The script travels inside the launcher, after a marker line that `cmd`
    never reaches, and PowerShell reads it from `%~f0`. Passed as
    `-EncodedCommand` it would exceed `cmd`'s 8,191-character line limit
    (S1).
  - A null DACL (no `D:` part in the SDDL, so everyone has full access), an
    ACL that cannot be read, and an unknown ACE type or right all fail the
    check.
  - The check **fails closed**: if PowerShell is blocked or the result is
    unclear, the launcher refuses.
  - This is the only PowerShell use left in gupkg's own operation. It is a
    narrow exception to issue 017.
- **Then** it uses the root forwarder, or the bundled gupkg. Both lie inside
  the verified root.

Further rules:

- **Never the registry, `PATH`, AppData, ProgramData or the current
  directory** (sections 9.5 and 9.7). Today's `gupkg-tui.cmd` searches
  `PATH`, and that search is removed.
- **The refusal in a user-writable launcher is only a safety rail**, not a
  security control. Anyone who can write that folder can edit the `.cmd`
  and remove the check. Security comes from the other direction: launchers
  in admin-only roots only ever reach gupkg copies inside those roots
  (section 9.6).
- **A root's forwarder points only to a gupkg at least as trusted as the
  root.**
  - A system root's forwarder points only to the gupkg installed in that
    same root.
  - A user root's forwarder may point to a user-mode gupkg, or to the system
    gupkg (user → system is safe).
  - The forwarder is written when the gupkg package is installed into a
    root. A non-elevated installed gupkg managing a user root that has no
    forwarder writes one pointing at itself.
- **Bundled installs never write a forwarder.** Their launchers keep finding
  the bundled copy for as long as the root has no installed gupkg. That is
  how a bundled install stays free of infrastructure.
- **Version fallback.** An installed gupkg that cannot handle the package
  exits with code **6**: an unknown `pkg.toml` `format`, or an unmet
  `requires_gupkg`. The launcher then prints a notice and re-runs the same
  command with the bundled gupkg, if there is one. When elevated, it does so
  only if that bundled gupkg is in the verified location.
- **The two launchers:**
  - `install.cmd` runs `gupkg install "%~dp0."` and pauses on failure.
  - `gupkg-manager.cmd` runs `gupkg-manager "%~dp0."`, which opens the
    package view: update, repair, uninstall, logs.

  Both pass through `GUPKG_NO_PAUSE` as today.

### 21.4 Installing from a distribution folder

A version directory that lies outside every configured root, such as a
distribution unzipped into Downloads, is installed by **copying it into the
mode's root first** (`<root>\<name>\<version>\`, copied whole, including the
bundled gupkg and a shipped `_check_updates`). The install then proceeds
from that copy, so the result is an ordinary managed package. The source
folder can be deleted afterwards.

- **The copy never overwrites.** An existing `<root>\<name>\<version>\`
  that differs from the source is reported as `diverged` and left alone
  (section 5.3, rule 1).
- **`--in-place` keeps the folder where it is.** It becomes an ad-hoc root
  (section 5.4). This is the choice for a portable package carried on a USB
  stick (`--mode portable --in-place`).
- **User and portable modes.** The unzipped launcher runs non-elevated. Its
  root is the extraction folder, which has no forwarder, so the bundled
  gupkg does a **bundled install** into the user or portable root.
- **System mode** is never started from a user-writable launcher: an
  elevated `install.cmd` in Downloads refuses (section 21.3). An
  administrator installs a distribution system-wide with the installed
  system gupkg, from an admin shell:
  `gupkg install <folder> --mode system`. That gupkg only *reads and copies*
  the folder into `C:\opt`, and runs nothing from the source location. It
  then installs from the admin-only copy. The admin has chosen to trust that
  package's contents, as with any installer.

**A bundled install keeps working without an installed gupkg.**

- **The bundled copy stays** in the installed version directory. The
  version's `gupkg-manager.cmd` uses it for update, repair and uninstall.
- **It is carried forward.** When `check` or `update` produces a new version
  directory in a root that has no forwarder, the running bundled gupkg is
  copied into the new directory. The active version of a bundled install
  therefore always has a gupkg to run.
- **Uninstalling leaves only gupkg's data.** `uninstall --purge` removes the
  package. What remains is the root's `.gupkg\logs`.
- **An installed gupkg takes over later.** If gupkg is later installed into
  the same root, the launchers of existing bundled installs find the new
  forwarder and use it. `cleanup --bundled` can then drop their bundled
  copies (section 21.8).

### 21.5 Bootstrap: installing gupkg itself

Bootstrap is a bundled install of the `gupkg` package, plus the root data
directory that installing the `gupkg` package always creates.

1. **Obtain the release ZIP.** `gupkg-bootstrap.cmd`/`.ps1` downloads the
   newest release named in `RELEASES.md` and verifies it against that row's
   SHA-256 (issue 016). `-Source` and `-Sha256` work as today.
2. **Place it.**
   - **User mode:** extract to a staging folder.
   - **System mode:** the bootstrap script must be run elevated by the
     administrator. It creates the system root (`C:\opt`) if needed, sets an
     admin-only owner and permissions on it, or refuses if an existing
     `C:\opt` is not admin-only. It then extracts the release **directly
     into `C:\opt\gupkg\<version>\`**.
3. **Run `gupkg\<version>\install.cmd`**, mapping `-Scope` to `--mode`.
   - **User mode:** the launcher in staging finds no forwarder, so the bundled
     gupkg runs and copies the version directory into
     `%USERPROFILE%\opt\gupkg\<version>\`.
   - **System mode:** the elevated launcher in `C:\opt\gupkg\<version>\`
     verifies `C:\opt` and its own folder (section 21.3). It finds no
     forwarder yet, so it runs the bundled gupkg, which is inside the
     verified root.

   Either way, the `gupkg` package is installed like any other package. The
   install:
   - populates `App` by **copying the bundled tree** (`local = "gupkg"`,
     verified against `MANIFEST.sha256`);
   - creates `current`;
   - creates the shims `gupkg.exe` and `gupkg-manager.exe` pointing into
     `$App`, puts the bin directory on `PATH`, and adds the Start Menu entry;
   - creates the **root data directory** `<root>\.gupkg\`, with the forwarder
     and default settings.
4. **Done.** Launchers of packages in that root now find the installed
   gupkg through the forwarder. The bootstrap script has no install logic of
   its own beyond creating the system root safely. The same script can
   perform a bundled install of any distribution in user mode
   (`-Source <url> -Sha256 …`). Run that way, it installs only that program,
   not gupkg.

### 21.6 gupkg's own package in format 2

```toml
# gupkg\_check_updates\pkg.toml  (tracker; D10: no check code is ever copied into versions)
format = 2
description = "gupkg package manager"
modes = ["system", "user"]

[track]
provider = "github"
repo = "guraltsev/gupkg"
asset = "gupkg-${version}-win-amd64.zip"

[source]
local = "gupkg"                     # first install: copy the bundled tree into App
download_url = "https://github.com/guraltsev/gupkg/releases/download/v${version}/gupkg-${version}-win-amd64.zip"
subdir = "gupkg/${version}/gupkg"   # self-update and repair: the bundled tree inside the release ZIP
verify_manifest = "MANIFEST.sha256" # relative to the payload; checked on both routes

[source.checksum]                   # RELEASES.md: committed and reviewed (issue 016)
page_match = { url = "https://raw.githubusercontent.com/guraltsev/gupkg/main/RELEASES.md", match = '\| ${version} \| [0-9a-f]+ \| [^|]+ \| (?P<sha256>[0-9a-f]{64}) \|' }

[[shim]]
name = "gupkg"
target = "$App\\gupkg.cmd"

[[shim]]
name = "gupkg-manager"
target = "$App\\gupkg-manager.cmd"

[root_forwarder]                    # only valid in the gupkg package: writes <root>\.gupkg\gupkg.cmd and gupkg-manager.cmd
target = '$PackageRoot\current\App'
```

- **The pinned version inside the release ZIP.** The build writes the ZIP's
  `gupkg\<version>\pkg.toml` with every template expanded. It cannot contain
  the SHA-256 of the ZIP that contains it, so it keeps a *fixed*
  `[source.checksum]` page match on `RELEASES.md` for that exact version.
  §9.3 rule 2 allows a fixed URL. That page match is used only if the ZIP
  ever has to be downloaded again.
  - The bootstrap install downloads nothing (`local`). Its receipt records
    `checksum_decision = "verified-by-bootstrap"`: the script checked the
    ZIP against `RELEASES.md`, and the copy was checked against
    `MANIFEST.sha256`.
- **Self-update.**
  1. `gupkg self check` writes `<root>\gupkg\<new>\pkg.toml`, plus the two
     launchers as support files, but no bundled copy. For a system root it
     writes a proposal instead, which an elevated `self update` admits
     (section 9.3).
  2. `gupkg self update` finds no `local` tree, so it downloads the ZIP,
     verifies it against `RELEASES.md`, extracts the bundled tree into
     `<new>\App\`, verifies `MANIFEST.sha256`, and switches `current`.

  The forwarder already goes through `current`, so it needs no change. The
  running process keeps executing from the old version's `App`, which stays
  on disk until `cleanup`.
- **The checksum source.** `RELEASES.md` is preferred over the GitHub asset
  digest, because it is committed and reviewed. The digest can serve as a
  fallback, and issue 033 decides that.
- **`cleanup` drops the bundled copy** in gupkg's own version directories
  once `App` is populated, because the two are identical. `App` stays
  disposable afterwards: a rebuild falls back from `local` to `download_url`, verified
  against `RELEASES.md` and `MANIFEST.sha256`.

### 21.7 What the format and CLI gain

| Addition | Section | Purpose |
| --- | --- | --- |
| `[source] local = "<dir>"` | 7, 8.2 | Populate `App` by copying a verified folder shipped inside the version directory; fall back to `download_url`. |
| Reserved version-directory names `gupkg\`, `install.cmd`, `gupkg-manager.cmd` | 4 | The bundled runtime and the launchers. The engine never executes the bundled tree. |
| Carrying a bundled gupkg forward | 5.3, 7.4 | New version directories of a bundled install get a copy of the running bundle, so the active version can always manage itself. |
| `[root_forwarder]` (gupkg package only) | 7, 13.1, 21.3 | Writes `<root>\.gupkg\gupkg.cmd` and `gupkg-manager.cmd`, recorded in the install record and removed by uninstall. |
| `requires_gupkg = ">=1.2"` (optional) | 7 | The minimum gupkg version a package needs. An older gupkg exits 6. |
| Exit code **6** | 10.1 | "This package needs a newer gupkg." It drives the launcher fallback. |
| `gupkg install PATH --in-place` | 10.2, 21.4 | Install a distribution folder where it is (an ad-hoc root) instead of copying it into the mode's root. |
| `gupkg pkg bundle <version-dir> [--lite]` | 10.2 | Copy the *running* gupkg's verified bundle into `<version-dir>\gupkg\`, and write the two launchers. `--lite` leaves out `gupkg_manager` and Textual (open question 6). |
| `gupkg pkg export <version-dir> -o X.zip` | 10.2 | Write a distribution ZIP (`<name>\<version>\…`, plus `_check_updates` if present) and print its SHA-256. |

### 21.8 Size and cleanup

A bundled gupkg is roughly 40 MB: the embedded Python, Textual and Rich.

- **A bundled install keeps one bundled copy** in each version directory it
  carries forward to. `cleanup` of old versions removes old copies along with
  the old versions.
- **`gupkg cleanup --bundled`** removes bundled copies from the packages of
  a root only when that root has a forwarder to an installed gupkg of at
  least the same version. It never leaves an active version with no gupkg
  that its launchers can find. It is off by default (open question 5).
- **Registry packages do not ship a bundled gupkg.** Registry users already
  have gupkg. Bundling is for distributions outside the registry.
