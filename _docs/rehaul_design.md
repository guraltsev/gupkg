# gupkg 1.0 rehaul: target design

Date: 2026-10-22
Status: **implemented** in gupkg 1.0 (issues 016–033; decisions D1–D46).
Tracking issue: `_issues/_closed/issue020-rehaul-epic.md`.
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

This document is the single description of the design that gupkg 1.0
implements. Issues 020–033 (now in `_issues/_closed/`) implemented it in
order. Where this document and older issues disagree, this document wins.
The user documentation (`README.md`, `_docs/*.md`) describes the behaviour of
1.0; this document gives the reasons.

**Deviations from the text below, found while implementing.** Where a
section's wording and the code differ, the code and the decision table win.

- **Check for updates and elevation (issue 030, D22).** `gupkg-manager
  check-updates --all` is one non-elevated call; a system package is only
  reported from a normal shell, and an administrator checks it from an
  elevated shell. The issue text that sent it "through the single elevated
  call" was wrong and was corrected.
- **The elevated relaunch waits (issue 029).** The elevated parent waits for
  the child and returns its exit code (clamped to 0–6, raw value in
  `child_raw_exit_code`); the events file is the progress channel. The
  design's "exits when the child starts" wording is superseded.
- **Where launchers sit (issue 033).** A version directory holds `install.cmd`
  and `gupkg-tui.cmd` in every root, and a tracker folder holds
  `check-updates.cmd` and never `install.cmd`; `MANIFEST.sha256` is verified
  for the bundled tree (`gupkg/` entries) only. The 1.x guard: gupkg's tracker
  refuses a release of a major version above the running gupkg's and names the
  bootstrap.
- **Registry (issue 031).** `_registry.toml` also carries `unpinned` and
  `unversioned` lists, and the registry tool refuses `--auto-push` of a hash
  it computed itself without `--allow-computed`.
- **Hook API.** Hooks declare `PKG_MODULE_API = 2` and receive a typed
  context (section 8.6), not the 0.17 dictionaries.
- **Not verified on real hardware:** Smart App Control (spike S2), and the
  manual checks listed in `tests/manual_smoke.md`.

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
15. The UI programs (`gupkg-tui`, `gupkg-manager`)
16. Testability
17. Registry 2
18. Migration
19. Decisions
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
      - `_check_updates` is a separate tracker. `gupkg check-updates` uses it
     to create new version directories, populated by default. Activating
     one of them is a separate operation.
6. **Strict checksums by default**, with an explicit, scoped way to skip them.
7. **Declarative services for recurring hook work.** Hooks remain an escape
   hatch, not the norm.
8. **A structured event stream** driving a readable console, an interactive UI,
   and standard log files.
9. **A system boundary behind one interface**, so that nearly all behaviour can
   be tested on a temporary directory without touching the real machine.
10. **Lifecycle completeness:** uninstall, switch/rollback, cleanup, and a
    health check (`gupkg check`).

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
| **Tracker** (`_check_updates`) | `<name>\_check_updates\`: a meta-package that discovers the newest upstream version and writes a *pinned version directory* for it, populated unless `--no-download` is given. That is all it does: it never activates anything and is never installed itself. Activating the new version is a separate `update` or `install`. Replaces `_bootstrap`. |
| **Check for updates** | The `gupkg check-updates` operation: run trackers, write new pinned version directories (downloaded and populated by default), report. The only operation that discovers new versions (section 9.3). |
| **Check** | The `gupkg check` operation: the health of installed packages (`App` against its fingerprint, `current`, integration effects, registry fingerprint). `--fix` repairs. Replaces 0.17's `doctor`. |
| **Populated / unpopulated** | Whether a version directory's `App\` exists and is non-empty. |
| **Populate script** | How a version's `App` is filled: the built-in download script, or a custom script in `pkg.local\` (section 4). |
| **Current** | The `current` junction naming the active version. A package is *installed* exactly when `current` is valid and its target is populated. |
| **Registry** | A read-only catalogue of package definitions (pinned versions and optional trackers), downloaded into a cache. Several registries may be configured; the official one is named `main`. |
| **Link** | Which registry entry a local package follows. By default it is the entry with the same name (D2); `gupkg link` points it elsewhere or opts out. Recorded in `<package>\.gupkg\package.toml`. |
| **Install record** | `<package>\.gupkg\package.toml`: mode, link, and the integration effects last applied (used by uninstall and stale-effect cleanup). |
| **Receipt** | `<package>\<version>\App.info` (owner decision after 1.0; before that `<package>\.gupkg\receipts\<version>.toml`, which is still read): what one install of one version did (source URL, observed SHA-256, checksum decision, log file, timestamps). |
| **Data directory** | `<package>\data\`: version-independent application state for portable mode (section 6.3). |
| **Service** | A built-in, declarative operation named in `pkg.toml` (fetch, extract, flatten, rename, discover versions, find checksums). |
| **Hook** | Trusted package-local code under `pkg.local\`, used only when no service fits: Python modules, and populate scripts, which may also be `.ps1`, `.cmd`/`.bat` or executables (section 4). |

## 4. On-disk layout

```text
<root>\                                  # one install mode per root
  <name>\                                # package root (identity = root + name)
    current\                             # junction -> active version
        _check_updates\                      # optional tracker (never installed)
      pkg.toml                           #   [track] + [source] templates
      pkg.local\                         #   optional hooks
      icons\ ...                         #   support files copied into new versions
    1.2.3\                               # pinned version
      pkg.toml                           #   [source] download_url + sha256
      App\                               #   payload; absent/empty until populated
      App.info                           #   the receipt: how the App was built, and for which mode
      icons\ ...
    1.2.4\                               # pinned, not yet populated ("available")
      pkg.toml
    data\                                # portable-mode persistent state (section 6.3)
    .gupkg\
      package.toml                       # install record: mode, link, applied effects
            state\track.toml                   # last tracker check result
      logs\                              # this package's run logs (section 14.3)
      locks\
      work\                              # disposable staging
```

The reserved names in a package root are `current`, `_check_updates`,
`data`, and anything starting with `.` or `_`. Inside a version directory,
`pkg.local\`, `gupkg\` (a bundled gupkg, section 21), `install.cmd` and
`gupkg-tui.cmd` (the package launchers) are reserved. 0.17's `_bootstrap`
gets no special treatment: it starts with `_`, so it is reserved and
ignored (D33).

Rules:

- **A version directory is any non-reserved child containing `pkg.toml`.**
  Version ordering is unchanged from 0.17: dotted segments, and `.local.` for a
  local revision. Reserved names never take part in ordering. This fixes
  `_bootstrap` sorting as newest.
- **Population is atomic.** `App` is built under `.gupkg\work` and renamed into
  place, so a version directory is never half-populated.
- **How `App` is populated** (owner's rules, 2026-10-22; D27). Every version
  says in `[source]` how its `App` is filled:

  ```toml
  [source]                          # 1. the built-in download script
  download_url = "https://…/tool-1.2.3.zip"
  sha256 = "…"                      # optional; the checksum rules of section 9 apply

  [source]                          # 2. a custom populate script in pkg.local\
  script = "pkg.local/populate.py"  # or .ps1, .cmd/.bat, or an executable

  [source]                          # 3. shipped: App is already in the version directory
  shipped = true
  ```

  | Kind | `[source]` | Meaning |
  | --- | --- | --- |
  | **Downloaded** | `download_url` | The built-in download script downloads the artifact, verifies it (section 9), unpacks it (section 8.2), and then runs the package's remaining local install steps (`[[prepare]]`, section 8.3) through a hook. `App` is **recoverable**: it can be deleted and rebuilt at any time. |
  | **Script-populated** | `script` | A custom populate script fills `App`. Python scripts are loaded as hooks; `.ps1`, `.cmd`/`.bat` and executables run as a subprocess and receive the context as JSON on standard input. The script can run the same `[[prepare]]` steps through its context. `App` is **as verified as the package itself** (section 9.2) and **recoverable while the script's inputs exist**. |
  | **Shipped** | `shipped = true` | `App` is included in the version directory, and nothing populates it. Examples are proprietary software, or a licensed or internally built payload distributed on a share. gupkg never drops or replaces it. |

  - **`shipped = true` with a `download_url`** is valid, and `shipped` takes
    precedence: gupkg never drops or replaces that `App`, and uses the URL
    **only** to recover an `App` that is missing (with the checksum rules of
    section 9).
  - **The default** for `shipped` is `false` when `[source]` has a
    `download_url` or a `script`, and `true` otherwise. `shipped = false`
    with neither is **invalid**, because nothing could ever populate `App`.
  - **One populate method per version.** `[source]` has a `download_url`
    or a `script`, not both. The one combination is `shipped = true` with
    a `download_url`. (Designer's default; it can be relaxed later.)
  - **gupkg's own tools always write the key explicitly:** `pkg new`,
    `pkg bundle`, the migration tool (section 18), and `check-updates`'
    `render_pinned`.
  - **Scripts may use the network** (D28). Python cannot sandbox trusted
    code, so gupkg does not try. What a version fetches is reviewable in its
    `pkg.toml` and its scripts.

  **Shipped is a first-class kind, not an exception.** For every
  `shipped = true` version:
  - `gupkg pkg check` requires a non-empty `App`. It accepts an
    `App\MANIFEST.sha256` (or `[source] verify_manifest`), which `check`
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
  `shipped`, or by re-running its populate script when the script's inputs
  are present. Otherwise `gupkg pkg check` and `install` fail with the error
  `App is missing and no way to recover it.` rather than trying to populate
  it.

  Several design choices follow from the rule:
  - **No state lives in `App`.** Portable data lives in `<package>\data`
    (section 6.3), and receipts and install records live in `.gupkg\`.
  - **Management lives outside `App`.** The bundled gupkg that manages a
    bundled install sits in `<version>\gupkg\` (section 21), so dropping a
    recoverable `App` never makes a package unmanageable.
  - **gupkg's own `App` is script-populated.** Its populate script copies
    the bundled tree (`<version>\gupkg\`) into `App`. gupkg's own package
    has no `download_url` (section 21.6).
  - **The lifecycle commands use it.** `cleanup` frees space by dropping
    recoverable, non-current `App`s. Repair (`install --refresh`,
    `check --fix`) rebuilds them.
  - **The active `App` is not dropped.** Removing it is valid on disk but
    leaves the package `broken` until repaired, so `cleanup` never removes
    the active `App`, nor gupkg's own running one.
  - **Every install records an `App` fingerprint** (D41). The receipt
    stores the SHA-256 of every file, and `check` reports modified, missing
    or extra files. A package with a very large tree (msys2 has about
    14,000 files) opts out with `record_manifest = false`; a global switch
    remains.

- **`current` is switched only after a successful population.** Today it is
  linked first (`gupkg.py:208-240`). That order is reversed.
- **Logs.** A package's run logs are kept in `<package>\.gupkg\logs\`. The
  root's `.gupkg\logs\` holds only global run information, with links to
  them (section 14.3, D26). The receipt points at the log file.

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

Every front end (the CLI, the UI programs, check, list, update planning)
consumes `PackageView`. No other code works out package state.

### 5.2 Derived status

One status is derived for display and filtering:

| Status | Definition |
| --- | --- |
| `available` | No local package root; the registry has the entry. |
| `local` | A local package root exists and holds at least one version directory or tracker, but there is no valid `current`. Nothing is installed; the definition is ready. |
| `installed` | `current` is valid and its target is populated. |
| `outdated` | Installed, and a newer version is *known*. Known means a newer local version directory (including one written by an earlier `check-updates`) or a newer pinned version in the linked registry entry. |
| `broken` | `current` is dangling or escapes the package root; the active `App` is missing or empty; or the install record and the system disagree (drift). |
| `invalid` | A version directory or tracker fails validation. Shown for that version only. It does not block other valid versions. Today one bad `_bootstrap` marks the whole package unhealthy. |

### 5.3 The cases the manager must handle

The owner listed four cases (registry-only, local versions only, local
tracker, registry plus local), each installed or not. Every combination has
one defined behaviour. A local package follows the registry entry with the
same name unless it opted out (D2); "linked" below means that it follows
one.

| Case | `list` shows | `check-updates <name>` | `install <name>` | `update <name>` |
| --- | --- | --- | --- | --- |
| **R** registry only | `available` (with `--available`) | Error: not local (hint: `install`). | Create `<root>\<name>`. Copy the newest registry pinned version, plus the registry tracker if there is one. Install. If the registry has only a tracker: copy it, and stop with "no version known yet; run `gupkg check-updates <name>`". | Error: not installed (hint: `install`). |
| **V** local versions, no tracker, not installed | `local` | Nothing to check (no tracker). | Install the newest valid local version. `name@ver` picks another. | Error: not installed. |
| **V** installed | `installed` / `outdated` | Nothing to check (no tracker). | Repair (re-apply integration; populate `App` if empty). | Activate the newest local version newer than `current`, populating it first if needed. Otherwise "up to date". |
| **T** local tracker (± versions), not installed | `local` | Run the tracker. If the result is newer than every local version, write the version directory and, unless `--no-download`, populate it. Activate nothing. | Install the newest valid local version. If there is none yet: "no version known yet; run `gupkg check-updates <name>`". The tracker is never run. | Error: not installed. |
| **T** installed | `installed` / `outdated` | As above. The package becomes `outdated` if a newer directory was written. | Repair. | Activate the newest local version newer than `current`. The tracker is never run. |
| **R+V/T, linked** (the default for the same name) | as V/T, with a `registry` column | Runs the local `_check_updates` (copied from the registry if absent, rule 2). | as V/T | Install the newest of: the local version directories, and the registry's pinned versions (copied in when chosen). |
| **R+V/T, opted out** (`gupkg link --unlink`) | as V/T, marked `same name in registry: main (not followed)` | as V/T | as V/T | Local sources only. `gupkg link <name>` follows the registry again. |
| **Broken** | `broken` | as V/T | Repair: re-populate `App` from the active version's `[source]`; re-apply integration. | Refuses until repaired (hint: `check --fix` / `install`). |

**Checking for updates and installing are separate operations** (owner's
decisions, 2026-10-22: D11, D21, D23). `check-updates` is the only
operation that runs a tracker. It writes the new version directories, by
default downloads and populates them, reports them, and stops. `install`
and `update` never run a tracker. They activate only versions that are
already known: local version directories, or the linked registry's pinned
versions. There is no flag that combines the two. A scheduled job runs
`gupkg-manager check-updates --all`, followed by
`gupkg-manager update --all --yes`. Between the two steps the new version
directories can be reviewed, edited or deleted.

Rules that apply to every case:

1. **Copying a version in.** A registry pinned version is copied into the
   local package root only when it is selected for install. If a local
   directory of the same name exists and its `pkg.toml` differs from the
   registry copy, the local one is kept and the package is reported as
   `diverged`. Local files are never overwritten. In a root with no root
   forwarder, the package's own bundled `gupkg\` folder is carried into the new
   directory (section 21.4, D32); the launchers are written fresh in every root (section 7.4).
2. **The registry tracker is copied in too.** When a linked package has no
   local `_check_updates` and the registry provides one, the registry's
   tracker is copied to `_check_updates` on install, check-updates or
   update, and refreshed when the registry revision changes, unless the
   local copy was edited. Edits are detected by recording the hash of what
   was copied.
3. **Tracker output is a normal version directory.** Its `pkg.toml` is fully
   pinned (section 7.4). After creation it is indistinguishable from a
   registry pinned version, and the tracker plays no further part in it.
4. **Downgrades are explicit.** `gupkg switch <name> <version>` is the way back. `install` and `update` of a version older than the
   active one ask first (owner decision, after 1.0); `--yes` or `--allow-downgrade` answers. Other version directories that are broken
   or not supported also ask, and `--yes` or `--allow-other-broken-versions` answers (`--no-input` makes either question exit code 5).
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
  section 21.4, or a portable package installed where it is, D42) from a
  path outside every configured root is added to the
  `adhoc` list in `state.toml` (section 11), so
  `list`, `check` and the manager's `update --all` still see it.
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

- **The default portable root is a scanned location, not a target.** The
  built-in `%USERPROFILE%\opt-portable` is where listings look for portable
  packages. `--mode portable` needs a *configured* portable root (a settings
  layer above the built-in defaults sets `[roots].portable`), a folder shaped
  `<name>\<version>\pkg.toml`, or an answer to a question (D42).
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
  shell. An elevated gupkg never acts on a user root, so an explicit
  `--mode user` in an elevated process is refused the same way (D31,
  section 9.6 rule 2). A portable root is user-writable too, so `--mode
  portable` (and a portable target) is refused under elevation the same way.
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

# Install-time steps per mode (owner decision after 1.0; [nonportable] is gone). Each is a list of
# [verb, argument...] steps run in order on the App being built: remove, create, rename, copy, run.
# What to remove when the package is NOT installed portable (keypirinha / mogan / systeminformer):
[user]
prepare = [["remove", "portable", ".portable", "*/SystemInformer.exe.settings.xml"]]

[system]
prepare = [["remove", "portable", ".portable", "*/SystemInformer.exe.settings.xml"]]
# [portable] takes the same `prepare` list beside persist, markers, args and env.
```

The receipt records which mode an App was prepared for (`[app] prepared_for`: the mode and a signature of its steps). Installing a
populated version in another mode asks and rebuilds the App from its verified download; an App that cannot be rebuilt is refused and
left alone, so steps never run on top of an App prepared for another mode.

**Rules:**

- **Where a portable package goes** (D42). `--mode portable` on a folder
  that already has the shape `<name>\<version>\pkg.toml` installs it in
  place, and the folder above `<name>` becomes the portable root.
  Otherwise the configured portable root is used, or gupkg asks; under
  `--no-input` that question is an error (exit 5).
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
  | `check_url` (`repo` / `repo_url` for the `github`, `gitlab` and `git` providers) | `[track]`, in `_check_updates` only | The page or API that is read to discover newer versions. | Yes, by `gupkg check-updates` only (section 9.3). |

  The engine keeps them apart by construction. `project_url` is never passed
  to the HTTP client, and a version directory has no `check_url` (D10).
- **`format = 2` is required.** Version 1 (0.17) files are read only by
  the separate migration tool (section 18).
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
sha256_from = "github-digest"     # provenance, informational: github-digest | vendor-sums | registry | manual
# kind is inferred from the file (zip, 7z, tar, sfx, nsis, msi, exe, file); override with kind = "…"
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

### 7.4 From tracker to version: `gupkg check-updates`

`gupkg check-updates firefox` runs these steps, and only these (D21–D23):

1. **Check.** Run `[track]` and get `version` plus provider captures (for
   example a GitHub asset URL or digest).
2. **Compare.** gupkg compares the result against `current` and the local
   versions. Hooks never decide "is it current?". That ends five
   inconsistent comparators and the `appReady` bugs.
3. **Write the version directory.** Write `<package>\<version>\` from the
   tracker:
   - copy the support files, but **strip all update-check code**. A `track`
     hook is never copied (section 9.3);
   - render `pkg.toml` with every template expanded;
   - remove `[track]`;
   - fill `[source].sha256` **only** from a checksum the tracker
     *discovered* (GitHub digest, sums file, sidecar), with `sha256_from`
     set accordingly;
   - write the version's own launchers, `install.cmd` and `gupkg-tui.cmd`,
     into the new directory **in every root**: whichever gupkg runs the
     check generates them from the one template of gupkg core (section
     21.3), so a double-click on the new version's `install.cmd` installs
     exactly that version. The tracker's own `check-updates.cmd` is never
     copied into a version;
   - in a root with no root forwarder (a bundled install), also copy the
     package's own bundled `gupkg\` folder into the new directory (section
     21.4, D32).
4. **Populate it, unless `--no-download`.** Run the version's populate
   script (section 4) into the new directory: download, verify (section 9),
   unpack and prepare. The checksum rules apply here exactly as in
   `install`. The new version is now ready, but **not active**. If the
   checksum rules refuse the download (no hash and no `--skip-checksum`),
   the version directory stays written without `App`, and the refusal is
   reported (D46).
5. **Report and stop.** The check reports the new directory, and the package
   shows as `outdated`. Nothing is activated or integrated: `current`,
   shortcuts, shims and the environment are unchanged.

`--no-download` stops after step 3 where the root is writable, and only
reports where it is not. Run from a normal shell, `check-updates` on a
system package implies `--no-download` and only reports. An administrator
runs it from an admin shell to write and populate the version in the
system root (D22). An elevated `check-updates` on a user package refuses
(D31).

The new directory is then an ordinary pinned version, which a later and
separate `gupkg update firefox` (or `install firefox@<version>`) activates,
populating it first if it has no `App` yet. That install validates it
again, like any other version directory.

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
| `index` | emacs, gimp, python, vlc, pidgin | `check_url`, ordered `[[track.level]]` with `match`, `select`, `key`, `next`, optional `probe` (descend until a file exists) |
| `redirect` | crystaldiskinfo, crystaldiskmark, geogebra, geogebra5, zotero | `check_url`, `match`, `unwrap_query` |
| `json` | firefox, julia, vscode, qbittorrent | `check_url`, `version` (JSONPath), `download`, `filter`, `records`, `match`, `checksum`, `captures` |
| `last_modified` | autoruns, processexplorer | `check_url`, `format = "%Y.%m.%d"` |
| `rss` | netio-gui | `check_url`, `match` |
| `git` | latex-toolkit, overleaf-sync, windebloattools | `repo_url`, `ref`; version = `YYYYMMDD-HHMMSS-<short sha>` (the commit is the integrity anchor) |
| `chain` | winscp, inkscape, shutter-encoder | ordered `[[track.fetch]]` steps; each step's captures feed the next URL; a step may be `optional = true` (skipped when it fails or matches nothing: inkscape's digest link) or `select = "each"` (try its matches in page order, at most 20, against the later steps and keep the first that gets through: shutter-encoder's links to several systems; one `each` step per chain, and the whole walk is bounded at 60 requests) |
| `module` | anything else | `pkg.local/track.py` → `track(ctx) -> TrackResult` |

Every provider:

- shares one HTTP client, with one User-Agent policy, one timeout, retries
  with backoff on 5xx and 429, TLS via `tls.py`, and the `GITHUB_TOKEN` header
  when it is set;
- returns `TrackResult(version, captures, checksum?)`;
- leaves version comparison to the manager.

**Provider details (issue 026).** The values of every `[track]` key are validated when the
manifest is parsed (`gupkg.model.trackspec`).

- **Captures.** A provider's named regex groups become captures, usable as `${name}` in
  `[source]` and `[[prepare]]`. Built in: `github` and `gitlab` give `tag`, `asset_name`,
  `asset_url` and `release_url`; `page`, `index`, `rss` and `chain` give `page_url`; `redirect`
  gives `final_url` and `file_name`; every URL provider gives `download_url` (the expanded
  `download` template or a `download_url` group); `git` gives `commit`, `short_commit`, `ref` and
  `commit_date`. `gupkg pkg check` rejects a `${name}` in a tracker's `[source]` that its provider
  never produces.
- **`version` and `download`** are templates over the captures (`version = "${major}.${minor}"`,
  `download = "${href}"`); the default `version` is `${version}`. Several matches are ordered with
  the one shared version comparison (`1.10` is newer than `1.3`); `select` is `max`, `first` or
  `unique`. `headers` (a table) set the User-Agent or other headers of the check; a `[source]`
  `headers` table does the same for the download (hwinfo needs a browser User-Agent and a Referer).
- **`github` / `gitlab`.** The version comes from the tag (`tag_prefix`, else a leading `v`
  dropped, or `tag_pattern` with named groups) or, with `version_from = "asset"`, from the asset
  name. The asset is the exact `asset` name or the full match of `asset_pattern`; both may contain
  `${version}`. `release = "newest-matching"` reads up to 100 releases and takes the newest that
  has a usable version and the asset; drafts are skipped and prereleases only with `prerelease =
  true`. A GitHub asset `digest` becomes the discovered checksum (`github-digest`).
- **`index`.** A level's `match` may contain `${name}` for what earlier levels captured (the value
  is escaped); `key` orders entries when the version is split over groups (`${major}.${minor}`);
  `next` is the page of the next level; `probe` is a file that must exist for an entry to count.
  The walk tries entries newest first and backtracks, within 60 requests.
- **`json`.** JSONPath subset: `$`, `.key`, `['key']`, `[n]`, `[*]`, `.*`, filters `[?a==b && c!=d]`
  (`==`, `!=`, `=~`), and a trailing `~` for keys. `records` selects the objects to look at, `filter`
  (one condition or an array) keeps some, and `version`, `download`, `checksum` and `captures` are
  read from each record; `match` extracts the version from the value (qbittorrent).
- **`last_modified`** formats the UTC `Last-Modified` date with `format` (default `%Y.%m.%d`;
  only `%Y %y %m %d %H %M %S %j` and literal characters allowed). **`rss`** matches the links,
  titles, ids and enclosure addresses of a feed (a document type declaration is refused).
  **`chain`** steps have `url`, `match`, `select`, `headers`, and `header` (match a response
  header such as `Content-Disposition` instead of the body); `${capture}` in a later step's `url`
  and `match` come from earlier steps, and a `sha256` group becomes the discovered checksum.
- **Safety.** Responses are data: a page over 8 MiB is refused. **The safety property for regular
  expressions is a deadline, not a check of the pattern.** Python's `re` backtracks and cannot be
  interrupted, so every match of a manifest pattern against fetched text (page, index, redirect, RSS and
  chain providers, `page_match`, `=~` filters on JSON values, tag and asset names) runs in a worker process
  (`gupkg/system/regex_worker.py`, started through `System.regex_worker` with the running interpreter's
  `-I -s -B -S`, a minimal environment holding only `SystemRoot`, and an empty temporary folder as its
  working directory). The parent (`gupkg.services.regexrun.RegexRunner`) sends the pattern, flags, prepared
  texts and operation as ASCII JSON on standard input and reads the matches (spans and groups, size-capped)
  from standard output. Each call has a deadline (2 s, counted from the worker's first line so a slow
  process start is not blamed on the pattern) and each provider run a total (10 s of matching); past the
  deadline the worker's whole process tree is killed (job object) and the call raises `RegexTimeout`
  naming the pattern and the address or field. The runner fails closed: a worker that crashes, prints
  something malformed or oversized, or is refused is an error, never "no match", so a hostile page cannot
  turn into "no version found". What it does not give: a memory cap (a worker can allocate during its
  deadline), and no network access is enforced by the operating system (the worker never opens a socket).
  The static guard is only a cheap first filter that refuses obviously dangerous shapes early with a clear
  message; it is **not** the safety property and a pattern that passes it can still be slow. It rejects
  patterns that repeat a repeated group, an empty-matching body, or overlapping alternatives inside a repeat
  of four or more, an unbounded `.` repeat next to another unbounded repeat (and more than one), more than
  three wildcard (`.`, `[^x]`) repeats, and more than 24 unbounded repeats or 6 in a row without a literal
  between them; groups are looked through at any depth, flags (`(?i)`, `re.X`) are honoured, and an
  iteration that holds an inner repeat is accepted only when it begins or ends with a required character
  that no inner repeat can consume (`(?:\d{1,3}\.){1,5}`, `\d+(?:[._]\d+)*`), judged by comparing character
  sets. Runs of word characters or of one character in a text are cut to 1024 and at most a mebibyte is sent.
  Redirects are followed by the client: HTTPS to HTTP and any non-http(s) target are refused, and
  `Authorization`, `Cookie` and any header whose name contains auth, key, token, secret, password,
  cookie or session are dropped when the host changes. A manifest `headers` table may not set
  `Host`, `Content-Length`, `Connection`, `Transfer-Encoding`, `Upgrade`, `TE`, `Proxy-*` and similar.
  Every request and download has a total deadline (120 s and one hour), not only an idle timeout.
  A body that ends before its `Content-Length`, a connection closed in the middle of the headers and
  a non-identity `Content-Encoding` are errors (a 204, 304, 1xx or HEAD answer has no body to check).
  A body with no length (HTTP/1.0 or `Connection: close`) cannot be verified and is accepted, like a
  browser does; the checksum rules cover integrity.
  `file:` URLs naming a network host or UNC share are refused unless the client is built with
  `allow_unc_file_urls` (Windows would authenticate to that server). `GITHUB_TOKEN` goes only to
  `api.github.com`, `github.com` and `uploads.github.com` over HTTPS and is read from an explicit
  mapping (dropped in an elevated run), never from `os.environ`; `git` runs with a scrubbed
  environment, null Git configuration files, no hooks, no credential helper and no prompts.

### 8.2 Unpacking (`[source]`)

| Field | Replaces | Meaning |
| --- | --- | --- |
| `kind` (inferred) | zip payload; 7zip/portablegit/qbittorrent unpack hooks | `zip`, `7z`, `tar` (`.tar.xz`, `.tar.gz`, …: two 7-Zip passes), `sfx`, `nsis`, `msi` (via `msiexec /a` into stage, or 7z), `exe`/`file` (copy as-is), `git`. There is no `inno` kind in 1.0 (D39). |
| `strip = N` | 6 flatten hooks and 15 `extract = [{src="x-*/", dest=""}]` rows | Remove N levels of a single wrapper folder. `strip = "auto"` strips while the root has exactly one directory. |
| `subdir` | `extractSubdir`, firefox `core` | Keep only this subtree. |
| `[[source.nested]]` | obsidian, mogan, pidgin | Extract an inner archive (`path`, `dest`, `replace`). |
| `[[source.extract]]` | tixati | Glob-mapped partial extraction (today's `extract`). Works for every kind. |
| `require = [...]` | 5 existence checks | Files that must exist after unpacking. |
| `verify_manifest = "<path>"` | gupkg's own `unpack_app` check (issue 016) | A `<sha256>  <relative path>` manifest shipped inside the artifact. Every unpacked file must match it, with no extra or missing files. Any package can use it; gupkg's own package is the first user. |

Every unpack service is safe-extracting: no paths escape, there are no reparse
points, and partial failures leave nothing behind. Every one works for every
archive kind; today `extract`/`rename` are limited to zip.

**Unpack details (issue 026).** The kind is read from the file's signature (a `.exe` is asked of
7-Zip: `Nsis` is `nsis`, an archive with a data offset is `sfx`, a plain executable is `exe`); a
declared `kind` is checked against it. `msi` is unpacked by 7-Zip, never by `msiexec`. The options
apply in this order, each addressing the tree as it is at that point: `nested`, `strip`,
`subdir`, `extract`, `require`, `verify_manifest`.

- `[[source.nested]]` unpacks the inner archive `path` and merges it at `dest` (an existing file is
  never overwritten); with `replace = true` the inner contents replace everything at `dest` (obsidian:
  `dest = ""` drops the rest of the installer). The inner archive file is deleted once unpacked.
- Names are judged before anything is written: absolute paths, drives, `..`, `:` (streams),
  Windows device names, a trailing dot or space, link entries, encryption and names that differ only by
  letter case are refused, and so are archives over 200,000 entries, 8 GiB in total or 4 GiB in one
  file (zip bombs); the limits are one budget shared by the artifact and every archive nested in it.
  Declared sizes are only a hint, so the limits are enforced while data is written: gzip, bzip2 and xz
  wrappers are decompressed in process with a byte cap (xz with a 256 MiB decoder memory limit), and
  every 7-Zip run is watched (the staging folder is measured about five times a second, junctions are
  not followed, and 7-Zip is killed within one poll of the limit being passed, not at the moment it is
  passed). A bomb that declares its size is caught earlier, by the check of the listing; the watcher
  is for archives that declare nothing or lie. Package code is trusted per file as well as per folder
  (script and parent chain), and a `sys.exit` at hook import or inside an entry point is a
  `HookError`. Residual: sibling modules that a hook imports from `pkg.local` are covered only by the
  whole-directory check, not one by one. Concatenated xz streams are all decompressed (zero padding
  in multiples of four bytes allowed); anything else after a stream is an error. The
  finished tree is walked again for links and special files. The tree appears under its final name by
  one rename, so a failed unpack leaves no `App` and no scratch folder.

`sfx` covers only self-extractors whose codecs the bundled 7-Zip supports.
Firefox and Zotero extract; MSYS2's `.sfx.exe` uses Zstandard and does not
(job 07, spike S3), so MSYS2 uses its `.tar.xz` release with the `tar`
kind (D39).

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

Details (issue 026): `remove` and `rename`/`copy` sources may be globs; `rename` and `copy` need
exactly one match, and `copy` never overwrites either; `remove` ignores a path that is not there and
deletes a link itself, never its target; `write` replaces a regular file but never writes through a
link. A path may not pass through a link or junction. If a step fails the staged `App` is deleted.

### 8.4 Vendor installers

`kind = "nsis"` extracts the installer without running it. Running vendor
code is a separate, explicitly marked step:

```toml
[source.installer]
run = ["/VERYSILENT", "/CURRENTUSER", "/NOICONS", "/DIR=$Stage"]
```

`list` and the install output label such packages "runs vendor installer", and
the setting `allow_vendor_installers` must not be `false`. GIMP, whose
installer is built with Inno Setup 7, is the only current user. No bundled
tool extracts it (job 07, spike S3), so 1.0 has no `inno` kind (D39).

The step runs only a downloaded `.exe` that matches a pinned sha256 (section 9.1, rule 6: never skippable), into the staged
`App` (`$Stage`), with a scrubbed environment, a timeout and arguments that may name only the stage; `require` is judged
after it ran. Section 13.4 has the rules and
the risk (vendor code runs with the rights of the gupkg that started it).

### 8.5 Checksum discovery (`[source.checksum]`)

The fields are `sidecar = "${download_url}.sha256"`, `sums_file`/`entry`,
`json`/`path`, and `page_match`. GitHub and GitLab digests are picked up
implicitly. This replaces discovery code in 13 hooks.

Details (issue 026): every address is a template over the discovered captures, `${download_url}`
being the `[source]` download address. `sidecar` and `sums_file` read `<digest>  <name>` and
`SHA256 (<name>) = <digest>` lines (a one-digest sidecar needs no name); `entry` must equal the listed
path exactly; `page_match` runs on the page the version came from (`${page_url}`) and takes the
`sha256` group; only a 64-digit hexadecimal token is ever accepted, and the provenance is
`vendor-sums`. A configured source that has no digest for the version is an error
(`ChecksumDiscoveryError`), not a silent skip: what a missing hash means is the checksum policy
(issue 027). A GitHub asset's `digest` is found by the `github` provider (`github-digest`). GitLab's
releases API publishes no digest today, so a GitLab link's `sha256` or `digest` field is used if it
ever appears, and otherwise the tracker declares `[source.checksum]`.

**`optional = true` (issue 031).** A vendor that publishes a digest for some builds only (GNU Emacs) is
declared with `optional = true`: when the file or the entry is missing the tracker finds the version
without a checksum, and the registry tool then computes one with `sha256_from = "registry"`, flagged
for review. Optional means the *source is absent*: a missing sums file or entry (`not found`, `not
listed`) or a fetch that failed (`fetch failed`) is waived, with a Warning event and a note in the
`TrackResult` that the registry tool prints next to `[REVIEW]`; a digest line that names the entry with
something that is not a SHA-256, and a regular-expression failure (a deadline, a crashed worker), are never
waived. A pinned version carries no `[source.checksum]`: `render_pinned` drops it and the package checker
refuses it in a version directory.

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
becomes `PKG_MODULE_API = 2`. Non-Python populate scripts receive the same
context as JSON on standard input (section 4).

The entry points are `track(ctx) -> TrackResult` (trackers only), `unpack(ctx)`, `prepare(ctx)` and
`populate(ctx)`. The context carries `package`, `version`, `system`, `http` (the shared client),
`paths` (`package_root`, `version_root`, `app`, `artifact`, `work`, `cache`), `options`,
`captures` and a `reporter`; the helpers of `gupkg.hooklib` take it as their first argument
(`fetch_text(ctx, url)`). A module that does not declare `PKG_MODULE_API = 2` is rejected like any
other invalid hook. **Trust:** `load_hook`, `run_custom_populate` and the `[[prepare]] module` step
take the `System` and, in an elevated run (or with `GUPKG_ELEVATED=1`), refuse a version directory or
`pkg.local` that is not admin-only before importing or starting anything (decision D19). Any
exception from a hook's import-time code is a `HookError`.

Every hook's context carries the shared HTTP client, so proxy, TLS,
retries and the GitHub token behave the same everywhere. Hooks may also
use the network by other means: Python cannot sandbox trusted code (D28).
`track` hooks return URLs, and the built-in download script downloads and
verifies them. A custom populate script may fetch what it needs itself;
its `App` is then as verified as the package (section 9.2).

## 9. Checksums and trust

### 9.1 Verification rule

When the built-in download script downloads a source artifact, during
`check-updates`, `install` or `update`:

1. **A SHA-256 in `[source]` is verified.** A **mismatch always fails.** No
   flag or setting turns a mismatch into a pass.
2. **When `[source]` has no SHA-256, the *missing-checksum policy* decides:**

   | Context | Policy |
   | --- | --- |
   | `gupkg` CLI (`check-updates`/`install`/`update`) | **refuse**, unless the command carries `--skip-checksum`. That flag applies to this invocation only and to missing hashes only. The CLI never prompts about checksums. |
   | `gupkg-manager` (section 15) | `[manager] checksum_missing` in the settings: `ask` (default) \| `refuse` \| `skip`. The manager reads the plan (`gupkg update … --plan --json`), which marks each target whose version has no SHA-256. With `ask`, it shows the package, version and URL and asks once per package. With `skip`, or after a yes, it calls the CLI for that target with `--skip-checksum`. With `refuse`, or after a no, it does not run that target. |
   | Non-interactive manager run (`gupkg-manager update --all --yes`, no TTY) | `ask` behaves as `refuse`. |

   The policy therefore lives entirely in the manager. The CLI only knows
   "refuse" and the explicit flag.

3. **Skipping is allowed for system installs too** (D24), including when
   the manager passes `--skip-checksum` to the elevated call, but **only
   for `https://` download URLs**. A plain-`http://` artifact without a
   hash is always refused. The run shows the skip in its Verify phase, the
   receipt records it, and `check` and `list --details` flag the install.
   This does not let a standard user gain rights: the URL comes from an
   admin-only `pkg.toml`, and the elevated side ignores the user's proxy
   and environment (section 9.6).

   Details (issue 027). "https" is judged on the parsed address: the
   text must start with `https://` and have a host, no user information
   (`https://good@evil`), no backslash and no space or control
   character, and every redirect hop of a skipped download must be
   https too (the download stops before the hop that is not). The manager
   relays its decision as `--skip-checksum=setting` or
   `--skip-checksum=user-approved`; a bare flag is `skipped-cli`; any other
   value is refused. A hash that is present is always verified, with or
   without the flag, and a malformed hash is an error, not a missing one.
4. **Git sources** pin a commit; the commit hash is the integrity anchor.
5. **Local files** (`file:` URLs on a share) follow the same rules.
6. **A vendor installer is never skippable.** The skip rules above are for archives. A version with
   `[source.installer]` (section 8.4) runs only from a download that matches a pinned `sha256` (or a matching
   registry fingerprint): the plan refuses one with no hash before a byte is fetched, `run_installer` refuses a download
   whose decision is `skipped-cli`, `skipped-setting`, `user-approved` or `origin-not-verified`, and no flag, setting
   or approval overrides it, in a user or an elevated run. Pin a hash with `gupkg pkg pin`; the registry tool must
   publish a `sha256` for every version that runs an installer (GIMP).

`ignore_checksum` is removed from the format. Registry packages publish pinned
versions with hashes (section 17), so they need no bypass at all.

### 9.2 What gets recorded

The receipt records:

- `observed_sha256`
- `checksum_decision`: `verified`, `skipped-cli`, `skipped-setting` or
  `user-approved` for a download; for a version populated without a
  download (a custom populate script, a shipped `App`, or a distribution
  folder), `registry-fingerprint` or `origin-not-verified` (below)
- the URL, the final URL after redirects, and the size
- whether a distribution folder it was copied from carried the Mark of the
  Web (D40)

**Installs with no download** (D29). A distribution folder is installed by
copying it (section 21.4), and nothing is downloaded, so no trusted hash
exists. Such an install is labelled **origin not verified**, unless the
registry publishes a **fingerprint** for that package and version: one
SHA-256 over the version directory's files (`pkg.toml`, scripts and any
shipped files), computed by the registry tool (section 17). gupkg computes
the same fingerprint at install time, when the synced registry is on disk,
and again in `check`:

- a match labels the install **verified against registry `<name>`**;
- a **mismatch refuses the install**, like a checksum mismatch, and names
  the files that differ. On an installed package, `check` reports it;
- without a registry entry, or offline, the label stays *origin not
  verified* until a later `check` can compare.

An `App` filled by a custom populate script is **as verified as the
package itself** (D27): it carries the package's label.

Details (issue 028). The **fingerprint** of a version directory has one definition, one
implementation (`gupkg.services.fingerprint`) and one byte-exact contract, in section 17: the
registry tool, install and `check` all call it. It ignores the top-level `App` and `gupkg` (so a
shipped `App` is not part of it either) and refuses links. A match for a version that ships its `App` is therefore
labelled **verified (App not covered)**: the receipt's `[origin]` has `app_covered = false`, the run warns and `check` shows a
`verified-partly` note; the `App` is then compared only with the D41 install-time record. A local version that has
launchers (`install.cmd`, `gupkg-tui.cmd`) that the registry's copy lacks is compared without them. A **distribution folder** is copied, then judged: no
published fingerprint labels the install *origin not verified*, a match
*verified against registry `<name>`*, a mismatch refuses it, names the files that differ and
removes the copy this run created. The same comparison runs for a version copied from a registry
and, as a report only, for a version that was already in the root (the user's own edit wins,
design 5.3). An install that downloads records the download's own checksum record instead; a
script-populated version in a root of its own (no registry, no distribution) records no label,
because it makes no claim. The receipt's `[origin]` table has `kind` (`local`, `distribution` or
`registry`), `copied_from` and `source_marked`. A version's `App` fingerprint is the `[app]`
table of its receipt (`recorded`, `count`, `fingerprint` and the SHA-256 of every file); it is
written once and never overwritten by a repair, so a repair cannot bless a changed file.
`check` compares the files with it (skipping the persisted paths of a portable package) and
shows a stored `verified` as a self-report in user and portable mode, never as proof.

`list --details` and `check` show unverified installs.

gupkg never writes an observed hash into `pkg.toml` automatically.
`gupkg pkg pin <target>` copies a receipt's observed hash into the version's
`pkg.toml` with `sha256_from = "manual"`. It is a deliberate, reviewable act.

### 9.3 Update discovery lives only in `_check_updates`

The owner decided on 2026-10-22 that **a version directory never contains
update-check functionality** (D10). What a version installs is fixed in its
`pkg.toml` and its scripts, and can be reviewed there.

1. **A version directory has no `[track]` table.** Its `pkg.local\` holds no
   module that defines `track`. Validation (`gupkg pkg check`, the catalog,
   and the install planner) rejects a version directory that has either,
   and the error names `_check_updates`.
2. **Version-directory scripts may use the network** (D28). Populate
   scripts and other hooks are trusted code, and Python cannot sandbox
   trusted code, so gupkg does not restrict them. What they fetch is
   reviewable in the scripts.
3. **`_check_updates` is the only component that discovers new versions.**
   It decides what "newer" means, which URLs to probe and what to write.
   - Its output is a new pinned version directory. That directory goes
     through the same validation as any other version directory before
     anything is activated from it.
   - The output can always be reviewed. `check-updates` ends after writing
     (and by default populating) it, and activating it is a separate
     command.
4. **Updates never come from the installed version.** Today, `[update]` is
   copied forward from the *active* version (`updates.prepare_update`). That
   path is removed. Updates come only from `_check_updates` (local, or
   copied in from a linked registry) and from registry pinned versions.
5. **Who checks for updates** (D22).
   - **User and portable packages:** from a normal shell. An elevated
     `check-updates` on a user package refuses (D31).
   - **System packages:** an administrator runs `check-updates` from an
     admin shell. The tracker code it runs sits in the admin-only
     `<root>\<name>\_check_updates\`, and network responses are data, as
     with any download. It writes, and by default populates, the new
     version directly in the system root.
   - **From a normal shell, a system package** is checked with
     `--no-download` implied, and the result is only reported: the system
     root is not writable, and nothing is written elsewhere. There are no
     proposals.

### 9.4 Elevated runs

The trust rules in `docs/elevated-runtime-design.md` stay (issue 032 moves
the file to `_docs/`).

- **System-mode runs read only admin-only locations.** That covers the system
  root and its data directory `C:\opt\.gupkg\` (settings, registry cache,
  state, logs). The threat model behind this rule is section 9.6.
- **The user settings layer is ignored when elevated.**
- **An elevated gupkg never acts on a user root** (D31): not through the
  `auto` fallback, and not through an explicit `--mode user`. Portable roots
  are refused under elevation the same way.

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
- **Verifying a value gupkg wrote.** `check` verifies that a value listed in
  the install record is still present.

Rules:

- **No generic registry table.** Later releases may add *specific*
  declarative effects, such as file-type associations, `App Paths` or URL
  protocols. Each would be a named table with its own validation, recorded in
  the install record and removed on uninstall. None of them would be read
  back as information.
- **0.17 already complies.** Its only registry read is the read-modify-write
  of `PATH` in `components.py`. A behaviour test runs install, update and
  uninstall against `FakeSystem` and fails if its registry access log
  shows any read other than `PATH` (D38).

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
   Before it asks for elevation it also checks the files its child loads first (section 10.4); a
   check-then-launch race (TOCTOU) remains and is bounded by this very requirement.
2. **The elevated side resolves everything itself.** From the non-elevated
   side it accepts only a package name, or a path *inside* the admin-only
   system root, plus command options such as `--skip-checksum` (D24).
   Everything else comes from its own location: its root, that
   root's `.gupkg\settings.toml`, and package definitions. It ignores user
   settings, user roots and the user's environment.
3. **Launchers in admin-only roots reach only gupkg copies inside the same
   root** (section 21.3). Elevated launchers verify, with a trusted system
   tool and before running anything, that **everything they will run** is
   not user-writable: the root, the package and version directories, the
   root's `.gupkg\` and forwarder, and the whole gupkg and Python tree,
   recursively (D30). They refuse otherwise.
4. **Data from user-writable places is only ever data.** An example is a
   distribution folder an administrator installs system-wide (section
   21.4). It is copied and validated into
   admin-only locations before use. Code is never executed from there.
5. **Explicitly out of scope:**
   - malware already running as the user that requests UAC on its own;
   - an administrator who chooses to install an untrusted package;
   - an administrator account that is itself compromised.

A safety check placed in a *user-writable* launcher is a guard rail against
mistakes, not a security control: whoever can write the folder can remove
the check. The rules above never rely on such a check.

**`%SystemRoot%` is trusted** (D30). The elevated launcher finds PowerShell
through `%SystemRoot%`. A per-user override of that variable is a known way
to attack same-account elevation, but it needs malware already running as
the user who requests UAC, which rule 5 puts out of scope. When a standard
user calls a separate administrator account, the elevated process gets the
administrator's environment.

### 9.7 gupkg owns no files outside its roots

This is the owner's decision of 2026-10-22 (D20). gupkg's own files live in
the **root data directory** `<root>\.gupkg\` of the root they belong to:
`settings.toml`, `state.toml`, the global `logs\`, the registry cache, and the
root forwarder (section 21.1). Each package's own logs and records live in its
`<package>\.gupkg\` (section 4). gupkg **never** uses `%LOCALAPPDATA%`,
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
- **The UI programs are separate** (owner's decisions, 2026-10-22: D12, D34).
  `gupkg-tui` shows and acts on one package, and `gupkg-manager` is the
  general manager (section 15). Both may import gupkg core and any libraries
  for reading and reference, but every operation they perform is a `gupkg` CLI
  call (D44, section 10.4). Inventory browsing, `--all` batches, planning
  across packages, the missing-checksum policy, grouping operations under one
  elevation, and the interactive UIs all live there. The CLI acts on explicit
  targets: one or several names or paths.
- **Every separate tool is callable as `gupkg <command>`** (D45).
  `gupkg tui PATH` starts `gupkg-tui PATH`, `gupkg manager …` (including
  `gupkg manager tui`) starts `gupkg-manager …`, and `gupkg migrate …`
  starts `gupkg-migrate …`, all from the same gupkg tree. These
  forwarders are permanent. The CLI itself still has no UI.
- **Targets are names or paths.**
  - Selector grammar: `name`, `name@version`, `mode:name`, or a path. A path is
    anything containing `\` or `/`, or starting with `.`.
  - `registry/name` (for example `main/firefox`) selects a registry entry
    explicitly.
- **Options go after the command.** Root-only options are limited to
  `--version`, `--help`, `--settings PATH` and `--sandbox DIR` (section 16).
- **Exit codes are unchanged:** 0 ok, 2 user/config error, 3 operation
  failed, 4 internal error. New: 5 declined by the user or input needed, and
  6 this package needs a newer gupkg (it drives the launcher fallback,
  section 21.3).
- **Every mutating command takes `--yes`.** The CLI asks before
  consequential actions (`uninstall --purge`, a plan with several targets,
  anything else destructive) unless `--yes` is given. With `--no-input`, a
  question becomes exit code 5 instead. It never asks about checksums
  (D25).
- **Every command takes `--json` for machine output.** It replaces `--format
  toml`, and the output is one document on stdout. Human output goes to
  stderr.
- **No legacy code** (D33). The CLI has no 0.17 aliases. Old spellings other
  than the forwarders are unknown commands. Converting 0.17 data is the
  job of the separate migration tool (section 18).

### 10.2 Commands (`gupkg`)

```text
gupkg check-updates <target>... [--no-download] [--mode M] [--skip-checksum]   # run trackers; write and populate new version dirs; activate nothing
gupkg install  <target>... [--mode M] [--skip-checksum] [--refresh] [--in-place] [--yes]
gupkg update   <target>... [--mode M] [--skip-checksum] [--yes]   # activate the newest KNOWN version; never runs a tracker
gupkg switch   <target> <version>                         # activate another local version (rollback)
gupkg uninstall <target>... [--purge] [--yes]             # remove integration; --purge also deletes the package root and data
gupkg check    [<target>...] [--fix]                      # health: App fingerprint, current, drift, registry fingerprint (was doctor)
gupkg list     [--mode M] [--installed|--available|--outdated|--broken|--local] [--details] [--names]
gupkg info     <target>...                                # PackageView: versions, link, tracker, last check, receipts, log
gupkg search   <query> [--registry R] [--offline]
gupkg cleanup  [<target>...] [--keep N] [--bundled]       # old Apps, receipts, work, registry trees, logs (section 13.3); --bundled: section 21.8
gupkg link     <target> [<registry>/<name>] [--unlink]    # follow another registry entry, or stop following one (D2)
gupkg logs     [<target>] [--open | --path]
gupkg shell    [--mode M]                                 # print the PATH line for an integrate = "none" root (section 6.3)

gupkg registry sync|status|list|add|remove
gupkg pkg new <name>[@<version>] [--track github:owner/repo] [--dir D]   # authoring: a starter tracker and/or pinned version
gupkg pkg check <path> [--install]                        # validate a package definition; --install runs a sandboxed install (section 16)
gupkg pkg pin <target>                                    # receipt hash -> pkg.toml (explicit)
gupkg pkg bundle <version-dir>                            # embed the running gupkg and write the version's launchers (section 21)
gupkg pkg export <version-dir> -o X.zip                   # write a distribution ZIP; print its SHA-256
gupkg settings show|path|init|set KEY VALUE
gupkg self status|repair|check-updates|update             # check-updates/update = `gupkg check-updates gupkg` / `gupkg update gupkg` (section 20)

gupkg tui <target>                                        # forwards to gupkg-tui (section 15)
gupkg manager ...                                         # forwards to gupkg-manager, e.g. `gupkg manager tui`
gupkg migrate ...                                         # forwards to gupkg-migrate, the migration tool (section 18)
```

The CLI has no `--all` and no interactive UI. Several targets in one call
run one after another, and the run continues past a failure. For scripts,
`gupkg update $(gupkg list --outdated --names)` is the batch form; with
planning and confirmation, the batch form is `gupkg-manager`. `check` and
`cleanup` with no target act on every package of the trusted roots (the
sweep: a package that another run holds is left alone, only there).

Readings that the issue 029 implementation settled:

- **Which commands take what.** `--yes`, `--no-input`, `--json` and `--events-file` belong to every command that runs
  through the engine (`check-updates`, `install`, `update`, `switch`, `uninstall`, `check`, `cleanup`, `link`, and
  `self repair|check-updates|update`) and to `registry sync`; `--plan` to every one of those except `check` and
  `registry sync`; the reading commands take `--json` and `--mode` only. `--skip-checksum[=cli|setting|user-approved]`
  belongs to `check-updates`, `install`, `update`, `self check-updates|update` and `pkg check`; a bare flag takes no word
  after it as its reason.
- **Confirmations (D25)** are asked for `uninstall --purge`, for any engine call with more than one target (including the
  expansion of `check --fix` and `cleanup` with no target) and for the elevation relaunch. `pkg pin` asks before it writes.
  The command line never asks about checksums.
- **`self`.** `self check-updates|update|repair` are `check-updates gupkg`, `update gupkg` and `repair gupkg` in the mode of
  the root this gupkg is installed in (`--mode` overrides); they produce the same result document as the long spelling.
  `self status` reports the interpreter and the `MANIFEST.sha256` check and exits 3 when either fails.
- **Forwarders** find `gupkg-tui.cmd`, `gupkg-manager.cmd` and `gupkg-migrate.cmd` in the gupkg tree they run from
  (never on `PATH`), pass the rest of the command line and the exit code, refuse an elevated shell (the UI programs call
  `gupkg` and ask for administrator rights themselves, once per batch) and refuse `--settings` and `--sandbox`, which they
  could not honour.
- **Settings, registries and logs.** `settings init|set` and `registry add|remove` write the settings file of the layer of
  `--mode` (the user layer unless elevated; the system layer only from an administrator shell) after the schema accepted
  the change; `registry sync|status|list` use the cache in the data directory of that mode's root; `logs` lists the run
  logs of a package (or of the root), `--path` prints the newest and `--open` opens it.

### 10.3 What changes for users

| 0.17 | 1.0 |
| --- | --- |
| `gupkg install C:\opt\pandoc` | unchanged |
| `gupkg manager install vscode` | `gupkg install vscode` (`gupkg manager …` now forwards to `gupkg-manager`) |
| `gupkg --scope user manager update --yes` | `gupkg manager update --all --mode user --yes`, which forwards to `gupkg-manager` |
| `gupkg update PATH --check-only` | `gupkg check-updates PATH --no-download`. This also writes the new version directory, but installs nothing. |
| `gupkg update PATH` (check + download + install) | `gupkg check-updates PATH` (finds, downloads and prepares), then `gupkg update PATH` (activates): two separate operations. |
| `gupkg install PATH --allow-downgrade` | unchanged (it asks first without the option); `gupkg switch PATH 1.2.3` is the explicit way back |
| `--no-checksum` | `--skip-checksum` (missing hashes only, `https://` URLs only) |
| `gupkg config-check` / `config-fix` | `gupkg pkg check` / `gupkg migrate` (section 18) |
| `gupkg manager list/doctor/search/registry` | `gupkg list/check/search/registry` |
| `gupkg manager self status/repair` | `gupkg self status/repair` |
| `gupkg manager self update` (which only repaired) | `gupkg self check-updates`, then `gupkg self update`: a real update to a new release |
| `gupkg manager self repair-deps/update-deps` | removed (issue 016); maintainers use `tools/update_deps.py` |
| `gupkg tui PATH` / `gupkg manager tui` | unchanged: they forward to `gupkg-tui PATH` and `gupkg-manager` |
| a 0.17 installation | re-run the bootstrap once, then convert packages and settings with the migration tool (section 18) |

There are no deprecated aliases (D33). `gupkg tui`, `gupkg manager` and
`gupkg migrate` are permanent forwarders (D45); every other 0.17 spelling
is an unknown command.

### 10.4 Machine interface (the contract the UI programs and other tools use)

The UI programs may import gupkg core for reading and reference (D44), but
they perform every operation by running `gupkg` as a subprocess through this
interface. Scripts and any other tool use it the same way, so it is a
public, versioned contract (`"api": 1`).

| Option | Meaning |
| --- | --- |
| `--json` | One result document on stdout: per target, the outcome, version, mode, receipt path and log path, plus structured errors such as `{"code": "checksum-missing", "url": …}`. Human output goes to stderr. |
| `--plan` | Accepted by `check-updates`, `install`, `update`, `uninstall`, `switch` and `cleanup`. Resolves and plans without changing anything. Per target it reports the chosen version, mode, whether elevation is required, `sha256: present \| missing` (a malformed declared hash makes the version invalid: that target has no plan but a failed entry with the stable error code `invalid-checksum` (plus `version` and `field`), exit code 2, which `--skip-checksum` never overrides), the source URL, and the effects to add or remove. |
| `--events-file NAME` | `NAME` is a plain file name, never a path (D26). The CLI writes the run's event stream (section 14.1) as JSON lines, flushed per event: global events to `<root>\.gupkg\logs\NAME`, which links to each package's events in `<root>\<name>\.gupkg\logs\NAME`. It ends with the result document. The caller follows those files. A file is used rather than a pipe because an elevated child started through `runas` cannot share pipes with a non-elevated parent, and a plain name means a non-elevated caller cannot steer an elevated write (section 9.6). |
| `--no-input` | Never prompts. Anything that would need an answer becomes a structured error with exit code 5. |

**Cancelling** (D37). A caller asks a running CLI to stop through a stop
request named after the run. The name is chosen by the CLI when the run
starts, unique to the run, and announced in `run_start.stop_file` (a path in
the temporary folder): the caller creates a file there. A file that exists
before the run starts is never a request, because the name is chosen after the
caller could have created anything. The CLI checks it between packages,
finishes the current package cleanly, reports the rest as skipped (exit code 5)
and removes the marker. For an elevated child the request is user-writable data
that can only make the run stop, which is safe under section 9.6.

**The documents.** Every document starts with `api`, `command`, `ok` and
`exit_code`. A lifecycle call adds `relaunched` and `results` (one entry per
target: `package`, `operation`, `ok`, `status`, `version`, `previous`, `mode`,
`exit_code`, `receipt`, `log`, `warnings`, and on failure `error` with `code`,
`message`, `hint` and the facts the code carries, such as the `url` of
`checksum-missing`). `--plan` adds `plan: true` and `plans`: one entry per
target with `target`, `ok`, `exit_code` and either `plan` (`operation`,
`package`, `path`, `mode`, `requires_elevation`, `version`, `previous`,
`effects`, `phases`, `notes`, `sha256`, `url`, and `populate` or `copy_in` when
the plan has them) or `result` (the failure, or "up to date"). A call that
failed before any target was handled (a usage error, a question that was not
answered) has `errors` with `code`, `message` and, when there is one, `hint`. The events file ends
with `run_end`, whose `result` is the same document. Contract tests pin all of
this.

**The elevated child.** A call that needs the token starts one elevated child
of the same gupkg with a command line that the parent rebuilds from its parsed
options, never from its own input: the command, its targets, `--mode`,
`--no-download`, `--refresh`, `--in-place`, `--purge`, `--bundled`, `--fix`,
`--unlink`, `--keep`, `--plan`, `--yes` (only when the user gave it),
`--no-input`, `--skip-checksum[=reason]`, `--events-file NAME`, and the markers
`--relaunched` (always) and `--batch-confirmed` (the parent already asked "run
this on N packages"). `--settings`, `--sandbox` and `--json` never travel, and
the child refuses `--settings` and `--sandbox` itself. `--relaunched` makes the
child accept only package names and paths inside the system root. Targets that
name their mode are run in two groups (the others here, the system ones in the
child); a call whose targets only reveal their mode when they are resolved
reports each target the child cannot take as `not-run` (`mixed-modes`, exit
code 2) instead of dropping it. A root-wide `cleanup` (no target) hands the child no
targets either, so the child sweeps the system root itself.

*How the child is started (T1).* There is no shell in between: `ShellExecuteEx` with
the `runas` verb starts the bundled `python.exe -I -s -B -S bootstrap.py <arguments>`
of this gupkg, both paths taken from the location of the running gupkg after its
folders were checked admin-only; the arguments are quoted by the rules of
`CreateProcess`. In addition, the parent refuses (exit code 2, before any question)
every argument value that holds `& | < > ^ " % ! `` ` ``, a control character or a lone
surrogate, and the child refuses the same characters in its own command line.
A package whose name holds one of them is installed from an administrator shell. Every
Unicode category starting with `C` is refused (control characters including C1, bidi overrides
and isolates, zero-width joiners, lone surrogates, private-use and unassigned code points),
as are U+2028 and U+2029, and a target that starts with `-`. The options come first and the
targets follow `--`, so a folder named `--yes` cannot be read as an option. A command line over 30000
characters is refused (exit code 2), never reported as a declined prompt.

*What is checked before the prompt.* Before `ShellExecuteEx` the parent checks that
the files the child loads before any of its own code (`bundled_runtime_paths`: the package
folder and its parent, the bundled runtime folder, `Lib\site-packages`, `python.exe`,
`python*._pth`, `python*.dll` and `bootstrap.py`, the same list the child checks in
`enforce_bundled_runtime`) are admin-only; one that is not is exit code 2 and nothing is asked. A
check-then-launch race (TOCTOU) remains, bounded by the admin-only requirement itself: only an
administrator can change what the check accepted, and the child checks the same list again.

*The parent waits.* The parent keeps the process handle (`SEE_MASK_NOCLOSEPROCESS`),
waits for the child and exits with the **child's exit code** (the highest of it and
the codes of targets that were left out); the document has `relaunched: true` and
the same `exit_code`. A declined UAC prompt is exit code 5. The progress channel stays
the events file. When the call named `--events-file`, the parent reads the child's
last line afterwards: no `run_end` (a killed child, an unreadable file) is a failure,
exit code 3 with the error `child-no-result`. The child's process exit code is clamped
to gupkg's codes (`0`-`6` pass through, anything else is `4`); the raw code is the
`child_raw_exit_code` of the document. The call always gives the child an events file (its own
name when the caller named none) and prints the child's closing summary, so the output does not
vanish with the child's window; a scripted `--yes` or `--no-input` child never waits for a key. Ctrl-C while waiting creates the stop
marker (the parent passes its own marker to the child as the hidden `--stop-file`; the
child accepts only a fresh `gupkg-stop-<32 hex>` name inside its own temporary folder),
keeps waiting for 30 seconds, and then gives up with exit code 5, naming the marker and the events
file. When the user elevates through a *different* administrator account the child's temporary folder
differs, so the child ignores `--stop-file` and the stop never works: the parent waits 30 seconds and
gives up with exit code 5 (use the child's own `stop_file` from its events file instead). Scheduled and CI
callers use a unique `--events-file` name per run and treat a missing `run_end` as
failure; a multi-target call under `--no-input` needs `--yes` (the question "run this
on N packages?" is otherwise exit code 5).

*Text encoding.* The launcher runs `python -I`, which ignores `PYTHONIOENCODING`. A
redirected standard output or error is therefore UTF-8, a console keeps its code
page, and text that cannot be encoded is replaced, never raised. The `--json`
document is pure ASCII (non-ASCII characters are `\uXXXX` escapes), so it survives any
code page.

**Which `gupkg` the UI programs run.** Each always runs the `gupkg`
launcher next to it in the same gupkg tree. For an installed gupkg that is
`$App\gupkg.cmd`; for a bundled one, `<version>\gupkg\gupkg.cmd`. It never
uses a `gupkg` found on `PATH`, for the same reason that issue 018 forbids
interpreter probing. The UI programs and the CLI come from one bundle, so
their API versions always match. They still check the `"api"` field and
refuse a mismatch.

Exit codes follow section 10.1. One `check-updates`, `install`, `update` or
`uninstall` call may carry several targets. The manager uses that to group
every system-mode target into **one** elevated call, so the user sees a
single UAC prompt.

Contract tests pin the JSON shapes: the plan document, the result document
and every event type.

## 11. Settings file

There is one file format, `settings.toml`, with layered locations. The
separate migration tool converts 0.17's manager config `gupkg-config.toml`
(schema 2) (section 18).

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
   elevated and system-mode runs. In a portable run it is replaced by the
   portable root's layer, `<portable root>\.gupkg\settings.toml`, which
   is where `[integration.portable] integrate` is read from (section 6.1).
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
start_menu = '%APPDATA%\Microsoft\Windows\Start Menu\opt'
[integration.portable]
integrate = "user"            # user | none

[manager]                     # read only by gupkg-manager; the CLI ignores this table
checksum_missing = "ask"      # ask | refuse | skip
keep_screen = true            # print the last run's summary to the console after a UI program closes

[[registry]]
name = "main"
source = "https://github.com/guraltsev/gupkg-registry.git"

[logs]
keep_runs = 50
keep_days = 90

[cleanup]
auto = false                  # true: run cleanup after `gupkg-manager update --all` (D43)

[ui]
color = "auto"                # auto | always | never (NO_COLOR is honoured)

[hooks]
allow_vendor_installers = true
# There is no dependency-install switch. Per issue 016, hooks may import only
# the standard library, what the release bundle ships, and their own pkg.local\.

[install]
record_manifest = true        # false: no package records the SHA-256 of every App file (D41)
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
    mode.py         ModeContext, mode selection (6.2), context building (6.1)
    state.py        PackageView, LocalState, InstalledState, status derivation
    plan.py         InstallPlan, Phase, Effect (data only)
  catalog/      discovery and joins (reads disk, no mutation)
    local.py        scan roots, build LocalState
    record.py       the install record <package>\.gupkg\package.toml: read, and the one writer (called by engine)
    registry.py     registry cache, sync (writes only the cache), validation, per-revision index
    catalog.py      describe(), list(), resolve(), candidates(); the only place state is derived
  services/     built-in declarative operations
    http.py         shared client (UA, retry, TLS, token)
    track/          github, gitlab, page, index, redirect, json, last_modified, rss, git, chain, module
    populate/       the built-in download script; the custom-script runner (Python hook, or subprocess with JSON context)
    unpack/         zip, 7z, tar, sfx, nsis, msi, file, git; strip/subdir/nested/extract/require
    prepare.py      remove, rename, copy, write, module
    checksum.py     sidecar, sums_file, json, page; verification
    fingerprint.py  the fingerprint of a version directory and of a distribution copy (section 17), shared by the registry tool, install and check
    hooks.py        loader + HookContext + hooklib re-exports
  system/       THE side-effect boundary
    port.py         System protocol (fs links, env/PATH, shortcuts, broadcast, elevation, processes, http fetch)
    windows.py      real implementation
    fake.py         in-memory/tempdir implementation for tests and --sandbox
  engine/       planning and execution
    planner.py      PackageView + request -> InstallPlan (pure)
    executor.py     runs phases, emits events, writes receipts/install record, rollback
    appbuild.py     the built-in download script and the custom populate runner (atomic App)
    effects.py      declarations -> effects; apply, remove, verify (shortcuts, shims, env, PATH)
    portable.py     portable data hand-over, markers, the per-mode prepare steps and the App guard signature
    locks.py        the per-package lock (.gupkg\locks\package.lock)
    installer.py    vendor installers ([source.installer]): the guarded run into the stage
    fingerprint.py  the D41 App fingerprint (the version-directory fingerprint is services/fingerprint.py, section 17)
    receipts.py     receipt files; stateio.py: [[adhoc]] and track.toml
    checkupdates.py the check-updates plan: track, write, populate, stop
    health.py       what `check` reports
    lifecycle.py    check-updates/install/update/switch/uninstall/cleanup/check entry points
  report/       events and rendering
    events.py       event dataclasses + Reporter protocol (contextvar)
    console.py      Rich renderer
    logfile.py      text + JSONL log writer, retention
  modes.py      target -> mode: install record, containing root, portable target (D42), elevated admission (6.2, 9.6)
  relaunch.py   the up-front elevation step for system mode (9.6)
  distcopy.py   copy a distribution folder into a root, dropping the Mark of the Web (21.4)
  cli.py        argparse grammar -> lifecycle calls -> exit code; --json/--plan/--events-file/--no-input; the tui/manager/migrate forwarders
  viewdata.py   PackageView -> the plain data of list, info and --json (reads small files, prints nothing)
  settingsedit.py  create and edit one settings file (settings init/set, registry add/remove), validated by the schema first
  authoring.py  pkg new, pkg export, pkg bundle
  launchers/    the one template of the package launchers (install.cmd, gupkg-tui.cmd, check-updates.cmd) and the code that writes them; used by check-updates, authoring, the registry tool and the migration tool
  report/results.py  the result, plan and error documents of --json, and the text of list, info and --plan
  selfmgmt/     self status/repair: MANIFEST.sha256 check, bundled-runtime enforcement (issues 016, 018); self check-updates/update are aliases (section 20)
    location.py   where the running gupkg lives (installed, bundled, source): $App, its version directory and root, its sibling launchers
    status.py     what `gupkg self status` reports
  services/registrysource.py  the registry transports: trusted Git, the repository archive, the admin-only cache rule
```

```text
gupkg_ui/       shared by the two UI programs; may import gupkg core for reading (D44), never the engine
  client.py       the only module that starts a program: the sibling gupkg.cmd, argument lists, api check, stop request
  follow.py       follows --events-file files (global file + the package files it links to), bounded and strict
  render.py       event records -> phase-styled Rich text (literal, cleaned), verdict and summary lines
  plans.py        reads list/info/--plan documents; decides each planned target with model/manager_policy.py (ask/refuse/skip)
  steps.py        groups targets into calls: one non-elevated call and ONE call for all system targets
  session.py      runs the calls of a batch, combines their results, cancel through the stop marker
  flows.py        plan -> one confirmation -> policy -> grouped execution -> cleanup (shared by both programs)
  settings.py, startup.py, console.py, interaction.py
  screens/        frame (key-hint line, status line), run view, package view, log view, dialogs
gupkg_tui/      separate program (gupkg-tui launcher): one package's view and actions (cli.py, app.py)
gupkg_manager/  separate program (gupkg-manager launcher)
  batch.py        inventory, selection of what an --all batch acts on, the batch commands
  app/            Textual UI: the application and the browser
  cli.py          gupkg-manager [tui] | check-updates --all | update --all [--mode M] [--yes] | check
```

```text
gupkg_migrate/  separate, non-core tool (gupkg-migrate), built in issue 023 (section 18); may import gupkg core (D44)
                0.17 pkg.toml -> format 2, _bootstrap -> _check_updates,
                gupkg-config -> settings, adoption of 0.17 installs,
                shortcut import; ships in the release bundle;
                `gupkg migrate` forwards to it (D45)
gupkg_registry/ separate maintainer tool (issue 031, section 17); may import gupkg core (D44); not in the
                release bundle: update (run trackers, write pinned versions with sha256 and fingerprint),
                pin, validate
```

Dependency rules:

- **Inside `gupkg`:** `model` ← `catalog`, `services` ← `engine` ← `cli`.
- **`system`** is used only through the `System` protocol that `engine` and
  `services` receive.
- **No input or output below `cli`.** No module below it prints or reads
  input.
- **No hidden cycles.** There are no function-local imports to hide them.
- **The separate programs build on core** (D44). `gupkg_ui`, `gupkg_tui`,
  `gupkg_manager` and `gupkg_migrate` may import gupkg core and any
  libraries, and reusing core is preferred to reimplementing it. Core never
  imports them. The UI programs use core only for reading and reference:
  every operation they perform is a `gupkg` CLI call (section 10.4). The
  migration tool may use core to do its own work.
- **The migration tool is not part of core.** Core contains no code for
  0.17 data (D33).

This retires several of today's modules:

- **Split up:** `commands.py` (1,190 lines), `manager.py`, `gupkg.py`,
  `updates.py`, `origin.py` and `configuration.py`.
- **Merged:** the four hand-written TOML emitters become one writer,
  `tomli_w`. It is added to the runtime lock as a dependency in its own
  right, through the dev-tree lock tooling of issue 016
  (`tools/update_deps.py`). Its only copy used to be vendored inside pip, which
  issue 019 removed from the bundle; issue 019 locked `tomli_w` 1.2.0.
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
| **Prepare** | `[[prepare]]`, mode adjustments (`[portable]` markers, then the `prepare` steps of `[portable]` / `[user]` / `[system]`), `require` check; atomic rename into the version directory. |
| **Activate** | Portable data hand-over; switch `current`. |
| **Integrate** | Diff desired effects (shortcuts, env, PATH, shims) against the install record: create/update new ones, **remove stale ones**; broadcast environment change once. |
| **Finish** | Write receipt + install record; summary. |

An install plan never contains a tracker step. Its Fetch to Prepare phases
are skipped when the chosen version is already populated, for example by
`check-updates`. `check-updates` is a separate plan:

| Check-updates phase | Work |
| --- | --- |
| **Resolve** | Selector → `PackageView`; the package must have a tracker (local, or copied in from the linked registry). A system package needs an elevated run, or gets `--no-download` from a normal shell; a user package is never checked elevated (section 9.3). |
| **Track** | Run the provider; the catalog compares the result with the known versions. |
| **Write** | Render and validate the pinned `pkg.toml`, and write the version directory (section 7.4). With `--no-download` on a root that is not writable, report only. |
| **Fetch, Verify, Unpack, Prepare** | As above, into the new version directory, unless `--no-download`. |
| **Finish** | Record the result in `.gupkg\state\track.toml`; report the new versions. Nothing is activated or integrated. |

A failure before **Activate** leaves the system unchanged. A failure in
**Integrate** reports exactly which effects were applied, and `check --fix`
or a re-run converges on the declared state.

### 13.2 Batches

**In the engine.** A CLI call with several targets is a sequence of
per-package plans:

- the targets are resolved once;
- elevation is checked once for the call;
- the run continues past failures;
- per-package output is never discarded: it goes to the same event stream
  and log as any other run.

**In the UI programs.** Cross-package orchestration is `gupkg-manager`'s job
(section 15):

1. **Inventory:** `gupkg list --json`.
2. **Plan:** `gupkg update <targets> --plan --json`.
3. **Confirm.** The plan is confirmed once, and the checksum policy may ask
   per package.
4. **Run, grouped by mode.** One non-elevated `gupkg update <user targets>`
   call, then one elevated `gupkg update <system targets>` call.
5. **Report.** The manager shows the combined result.
6. **Cancel** (D37). Stopping takes effect after the current package: the
   running CLI finishes it cleanly and reports the rest as skipped.

The CLI's own questions are limited to consequential actions (D25). The UI
programs call it with `--no-input` and do their confirming themselves.
Today the CLI's `input()` and the TUI's confirmation are two separate
implementations.

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
- **`cleanup` runs only when asked.** `[cleanup] auto = true` runs it after
  `gupkg-manager update --all` (D43).
- **Bundled gupkg copies stay**, also in old version directories. Only
  `cleanup --bundled` removes them (section 21.8, D32).

### 13.4 Details of the engine (issue 028)

These readings settle what sections 13.1 to 13.3 leave open. They are what `gupkg.engine` does.

**Effects and the install record.** An effect is one of five kinds, each with the fields that
apply, remove and verify it: `shortcut` (`path`, `target`, `arguments`, `working_dir`,
`icon_location`, `description`), `shim` (`path`, `config`, `type`, `linkage`, `target`,
`arguments`, `forward_args`, `elevate`, `working_dir`, `config_sha256`), `file` (a content
wrapper: `path`, `content`, `sha256`), `env` (`scope`, `name`, `value`) and `path` (`scope`,
`entry`, `added`, `shared`). `$App`, `$VersionRoot` and the other variables expand against
`<package>\current`, so a link or shim keeps working when `current` moves. An effect is
identified by its path (an `env` by scope and name, a `PATH` entry by scope and normalised
entry): same identity with other fields is an *update*, a recorded effect that is no longer
declared is *stale* and is removed after the new ones are applied, and the environment change
is broadcast once. The `bin` folder's `PATH` entry (`shared`) belongs to the mode and is never
removed by one package; any other entry is removed only when this install added it
(`added`). The registry is only written: an environment value is set and deleted without being
read, `PATH` is read as part of its read-modify-write, and `check` reads exactly the values the
record lists (an elevated run does not read the user's). The install record always lists the
effects that are really on the machine: when one effect fails, the others are still applied, the
failure is reported, the record keeps the previous state of the failed effect, and `check --fix`
or a repeat converges. A recorded path outside the folder the mode owns is never acted on: it is
ignored with a warning and dropped from the record. A mode without a Start Menu or an
environment target (`integrate = "none"`) leaves those rows out and says so; `[portable]`
`args` are appended to shortcut and shim arguments and `[portable]` `env` becomes environment
values (set only when the mode touches the environment).

**Order and failure.** Resolve (including the copy-in) and every population phase come before
Activate, so a failure before Activate changes nothing outside the version directory (a copied-in
or check-updates-written directory is a valid version, not a leftover) and `<package>\.gupkg\work`.
A distribution folder that is refused (a fingerprint mismatch, a source that changed while it was
copied) leaves nothing of its copy behind. If the junction cannot be switched, the previous one is
put back. `App` is staged in `.gupkg\work` and renamed into place; with `--refresh` the old `App`
is moved aside first and restored if the new one cannot be put in place. The receipt
(`<version>\App.info`: `[source]`, `[checksum]`, `[origin]`, `[app]`, `[inputs]`,
`[run]` with the log path) and then the install record are written last; a download-only update
(`update` preparing a version) writes the receipt only.

**Repair.** `install` of an installed package, `repair` and `check --fix` rebuild a missing `App`
(from the `download_url`, or the script when its inputs exist, whatever `shipped` says) and apply
again every recorded effect that `check` finds missing; `--refresh` also rebuilds an `App` that can
be rebuilt and never a shipped one. A shipped `App` with neither URL nor script cannot be recovered:
`App is missing and no way to recover it.`.

**Cleanup.** For each package, `cleanup` drops the `App` of recoverable versions that are not
`current` beyond the newest `--keep N` older ones (default 1; with nothing installed, beyond the
newest N). It keeps shipped Apps, Apps of versions newer than `current` (an update `check-updates`
prepared), and Apps of script-populated versions whose recorded inputs (the top-level entries of the
version directory and the files of `pkg.local`, listed in the receipt) are no longer all there. It
removes receipts of versions that no longer exist and the entries of `.gupkg\work`. `cleanup_root`
prunes the root's logs and every package's logs beyond `[logs]` and the registry trees that are not
the active revision.

**Uninstall.** It removes every recorded effect and `current`, keeps the version directories (and
every `App`) and `data\`, and leaves the install record with its mode and link and no effects.
`--purge` asks first (the question names the package root, `data\` and every `App` that cannot be
rebuilt, `--no-input` or no way to ask is exit 5) and then deletes the package root; it refuses a root
that is a drive, the root itself or a folder that holds it.

**Portable.** `data\` holds what `[portable] persist` names. The first population moves the vendor's
defaults there; every activation copies the outgoing `App`'s persisted state back into `data\` and
then links it into the incoming `App` (a folder becomes a junction, a file is copied in), and saved
state wins over what a new `App` ships. Markers are created and the mode's `prepare` steps are run
only to an `App` that gupkg built, never to a shipped one. The `App` comparison of `check` skips the
persisted paths.

**Origin copies.** A registry pinned version is copied into the package root only when it is chosen
(into a root with no forwarder it carries the package's own bundled `gupkg\` folder from the
active version, like a `check-updates` directory),
and its tracker when the package follows the registry and has none or an unedited older one (the hash
and the revision recorded in the install record's `[link]` say whether the local copy was edited). A
distribution folder is copied by `copy_distribution` (with bounds on entries, bytes, depth and the
size of `pkg.toml`), the copy's `pkg.toml` must equal the one that was planned, and an existing
directory that differs from the source is never overwritten: the install is refused and says so.
An elevated run reads a distribution folder only while it copies it and plans from the admin-only
copy.

**Batches.** `run_batch` resolves every target once, asks `ensure_elevation` once for the whole call
(read-only operations never relaunch; a run that cannot name its own gupkg's folders never relaunches),
runs the targets in order and returns one typed result each. A stop request (a callable the front end
supplies; `stop_marker(path)` is true once a file exists, which is safe in an elevated run because it
can only stop the run) is checked between packages: the package that is running finishes, the rest are
`skipped`, the batch's exit code is 5.

**Trust in an elevated run.** `${NAME}` in a package's declarations sees only the machine's variables
(`SystemRoot`, `ProgramData`, `ProgramFiles`, `ALLUSERSPROFILE`, `COMSPEC`... never `USERPROFILE`,
`APPDATA` or `TEMP`), so the user's environment cannot steer what an elevated install writes. The download
cache, the package's work folder, the interpreter that
starts the regex worker, the worker script and 7-Zip must be in admin-only folders, judged before
anything starts; `git` is resolved only from admin-only folders of `PATH` and started by its full
path with a `PATH` that holds only such folders; hooks and populate scripts are judged by
`require_trusted_code`. `file:` URLs naming a network host stay refused (no setting enables them). A
hook of a version directory that exposes a name `track` once loaded is refused, whatever the static
scan saw. A package that needs a program the sandbox has no double for ends with the code
`needs-real-run`.

**Child processes of an elevated run.** `safe_process_environment(extra, system=...)` builds the environment from nothing the
process environment says: `PATH` is the admin-only, existing, absolute, de-duplicated folders of the process `PATH` (no `%`,
no UNC, no relative entry), `SYSTEMROOT`, `WINDIR` and `COMSPEC` come from the Windows folder only when `SYSTEMROOT` or `WINDIR` names an
existing admin-only folder (they are left out otherwise), `PATHEXT` is the fixed default, and `TEMP`/`TMP` are the work folder
the caller gives (`PROGRAMDATA` is left out). The installer and git both use it.

**Locks.** A run that changes a package (install, update, switch, repair, uninstall, cleanup,
`check-updates`, `check --fix`) first creates `<package>\.gupkg\locks\package.lock` exclusively; it holds the
process id, host, operation, start time and a random token. A second run on the same package fails with
the code `package-busy` (exit 3) and names the holder; `check`, `list` and `info` read only and take no
lock. A lock is stale, and reclaimed, when its process is not running (`System.process_is_running`), when
it is older than a day (no operation takes that long, and a recycled process id cannot keep a lock for
ever) or when its text is unreadable and not brand new; a lock of another host is judged by age only. A
lock is released on every exit, failure and cancel included. An elevated run locks only in an admin-only
`locks` folder. `cleanup` of a package the user names that another run holds fails with `package-busy` (exit 3); a
sweep over a whole root (`Request.sweep`) leaves it alone and reports it as `locked` (not a failure). Even when it holds
the lock, `cleanup` removes only the `.gupkg\work` entries older than 24 hours, so a run in flight keeps its staging folder.

**Vendor installers.** `[source.installer]` is implemented by `engine.installer` (section 8.4): the
verified, hashed-again `.exe` runs once into the staging folder. It needs `hooks.allow_vendor_installers`
not false (read from admin-only layers in an elevated run), the plan, the output, the receipt
(`[source] installer`) and `--plan --json` (`populate.label`) say `runs vendor installer`, and
`list --details` shows the same label from the version's manifest (`INSTALLER_LABEL`). The installer is
copied to the work folder under its published name, hashed once more and judged admin-only in an elevated
run; it gets a minimal environment (machine-level variables and a `TEMP` inside the work folder), no
standard input and `engine.installer_timeout` (30 minutes); in an elevated run its `PATH` holds only the admin-only
folders of the process `PATH`. `$Stage` expands to the staged `App`; after the expansion every value of an
option (`/DIR=...`, `-dir:...`, `-o...`) or bare argument must be the stage, a path below it, or a plain word: a value
that starts with a backslash, `/`, `~` or `%`, holds a `:` that is not in the stage path, a `..`, a separator
outside the stage or a Windows device name (`con`, `nul`, `com1`, also inside the stage) is refused before the
installer starts; a quoted `/D=` is refused (NSIS reads it unquoted and last). The validator accepts `[source.installer]` only with
`kind = "exe"` (or none) and no unpack option (`strip`, `subdir`, `nested`, `extract`, `verify_manifest`): they would be
silently ignored. After it ran, the
version's `require` paths must exist, and the staged `App` may hold no link or junction. A failing,
timed-out or refused installer removes the staging folder: nothing is activated. Under a sandbox an
installer runs only through an explicit process double; without one the result is `needs-real-run`. The risk:
an installer is vendor code that runs with the rights of the gupkg that started it (the administrator's in an
elevated run) and can change anything outside the stage (registry keys, other folders) that gupkg cannot see
or undo; the setting, the label and the warning in the output exist for that reason. A version that runs an
installer is populated by `check-updates` only when the setting allows it.

**`[[path]]` entries.** One entry is one absolute local folder (`X:\...`, or `%VAR%\...` for a user `PATH`): no `;`
(one declaration would become two entries and uninstall would not match them), no `..` segment (judged after
expansion, so `$App\..\..` is refused), no relative, UNC, `\\?\` or device path, no quote or control character. The
machine `PATH` takes only a path inside the package, the mode's `bin` folder or a machine-wide folder
(`ProgramFiles`, `SystemRoot`...), never a `%VAR%` prefix (a user can override it), and an existing folder that is a link or that
standard users can change is refused when the entry is applied, and so is an entry that does not exist yet unless it
lies inside the package (a standard user could create a missing folder inside a writable subfolder of `SystemRoot`, such as
`Temp`, later): for an entry inside the package the nearest existing ancestor is judged (admin-only, and no link or
junction between it and `current`). Removal skips these two rules, so an entry whose folder is gone can still be taken off the
`PATH`. Control, invisible and line-separator characters (the whole Unicode category C and `Zl`/`Zp`) are refused. The manifest validator, `desired_effects`,
`apply_effect` and `remove_effect` all check, so a hostile install record cannot make an uninstall change the `PATH`.

**Names a package may not take.** `[[env]]` and `[portable] env` may not name `Path` (it would replace the
whole `PATH`; `[[path]]` adds one entry the safe way) or a system variable (`PATHEXT`, `ComSpec`,
`SystemRoot`, `TEMP`, `USERPROFILE`, `APPDATA`, `PROCESSOR_*`...; any letter case), checked by the manifest
validator, by `desired_effects` and again when an effect is applied or removed, so an install record in a
user-writable root cannot make an uninstall delete them. Shortcut and shim names may not be device names
(`NUL`, `aux.lnk`, `COM1`), NTFS streams (`Tool:ads`) or end in a dot or a space, and are at most 255 characters;
an `[[env]]` name is at most 128 characters and holds no `%`, white space, `=` or invisible character.

**Secrets.** URLs are written to receipts, events, errors and plan data with user information and the values
of secret-looking query and fragment parameters (`token`, `signature`, `key`, `auth`, `policy`, `session`...,
separated by `&` or `;`) replaced by `***` (`gupkg.redact`). A secret in the URL *path* cannot be recognised and is not masked. A receipt larger than 1 MiB is not read (a user-mode receipt is user-writable); an `App`
of more than 4000 files records its count and one digest instead of the file list, and `check` then still
sees a change but cannot name the file.

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
gupkg update firefox  ·  user  ·  log: %USERPROFILE%\opt\firefox\.gupkg\logs\20261022T101500Z-update.log

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

| What | Location |
| --- | --- |
| a package's part of a run | `<root>\<name>\.gupkg\logs\` of that package (D26) |
| the run as a whole: global information, with links to the package logs | `<root>\.gupkg\logs\` of the root it acts on: `C:\opt\.gupkg\logs\` (admin-only) for system mode, `%USERPROFILE%\opt\.gupkg\logs\` for user mode, the portable or ad-hoc root's own for those |
| a bundled gupkg's run that involves no package (`search`, `registry status`) | the logs of the package the bundled copy belongs to |
| an installed gupkg's run that acts on no root | the data directory of the running gupkg's own root |

- **Two files per run and location.** Each location gets
  `<UTC yyyymmddThhmmssZ>-<command>.log` (plain text, flushed per line, no
  ANSI) and a `.jsonl` file with the same events. `latest.log` is a copy of
  the last run's text log.
- **Receipts point at their log.** Each receipt stores the log path, and
  `gupkg logs <target>` lists or opens a package's logs.
- **Retention** follows `[logs]`, and pruning happens when a run starts.
- **A logging failure never fails an operation.** It produces one warning.

## 15. The UI programs (`gupkg-tui`, `gupkg-manager`)

The owner decided on 2026-10-22 that the interactive programs are separate
from the CLI (D12) and from each other (D34). Both ship in the same bundle,
and both are specified in issue 030.

- **`gupkg-tui`** shows one package and acts on it: versions (local,
  registry, tracker), mode, link, data folder, the last receipt and the log,
  with check-updates, update, repair, switch, uninstall and logs on keys.
  `gupkg-tui.cmd`, the package launcher in each version folder, finds a
  gupkg as described in section 21.3, then runs `gupkg tui "%~dp0."`.
  `gupkg tui PATH` forwards to it.
- **`gupkg-manager`** is the general manager: the browser over every
  package, `--all` batches, planning and confirmation across packages, the
  `[manager] checksum_missing` policy (section 9.1), and grouping system
  targets into one elevated CLI call. `gupkg-manager` (or
  `gupkg manager tui`) opens the browser. The non-interactive commands for
  schedules are `gupkg-manager check-updates --all`,
  `gupkg-manager update --all [--mode M] [--yes]` and
  `gupkg-manager check`. `gupkg manager …` forwards to it.
- **They read through core and act through the CLI** (D44). They may
  import gupkg core and any libraries for reading and reference, such as
  package state, settings and the event renderer, but every operation they
  perform is a `gupkg` CLI call through the machine interface (section
  10.4). They share a small UI library of their own (`gupkg_ui`,
  section 12).
- **Rendering.** Both render the CLI's event stream (section 14.1), read
  from the files named by `--events-file`, with the same phase styling as
  the console renderer.
- **Scheduled runs.** A schedule typically runs
  `gupkg-manager check-updates --all`, then
  `gupkg-manager update --all --yes` (D11, D23 and section 5.3). These are
  two separate operations, so the new version folders can be reviewed in
  between.
- **Cancel** (D37). While a run is active, Esc asks "cancel after the
  current package?". It is never a silent leave (Ctrl-C asks the same
  question and never quits while a call runs). The running CLI finishes
  the current package cleanly and skips the rest; a program that is closed
  anyway first asks the CLI to stop, then ends its whole process tree (the
  launcher is a `.cmd`, so the process the program holds is only `cmd.exe`).
- **Scheduled runs are normal-user runs.** The UI programs refuse an
  elevated shell, so a task with "Run with highest privileges" is refused;
  run it as a normal user (system packages are then only reported, D22).

Textual stays for the interactive parts (D6). It is pinned and already
bundled. The manager's layout changes completely, and `gupkg-tui` uses the
same frame for one package:

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

- **One application, many screens.** Opening a package, running an
  operation and returning to the browser all happen inside one
  `gupkg-manager` application. It never exits and relaunches (today's
  `while True` loop between `GupkgApp` and `ManagerApp`).
- **The run view uses the console renderer's events.**
  - It shows the same coloured phase headers in an append-only `RichLog`,
    which ends the O(n²) re-render.
  - It keeps a progress bar under the active phase.
  - The verdict is written **at the end of the log**, and repeated in the
    footer.
- **Operations never block the UI.** Each runs as a CLI subprocess whose
  `--events-file` the UI follows, so the UI is never touched by
  child-process output and needs no process-global `redirect_stdout`.
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
- **Registry tests stay offline.** In the registry repository, `gupkg pkg
  check` validates every manifest and runs every tracker against recorded
  responses.
- **The registry rule is tested by behaviour** (D38). Install, update and
  uninstall are run against `FakeSystem` with a check of its registry
  access log. No test inspects imports or module layout.

## 17. Registry 2

Layout:

```text
gupkg-registry/
  <name>/
    _check_updates/        pkg.toml (+ pkg.local, support files)     # tracker (optional)
    <version>/      pkg.toml, fully pinned, no App; fingerprint     # every version is kept
  _registry.toml    format = 2, name, description, [fingerprints] "<name>/<version>" = "<sha256>"
```

- **The registry tool keeps the versions current** (D36). A maintainer runs
  it, by hand or on a schedule. For each new upstream version it:
  1. runs the package's `_check_updates` tracker;
  2. downloads the artifact;
  3. verifies it against the discovered checksum, or records its SHA-256
     with `sha256_from = "registry"` when the vendor publishes none (D8);
  4. runs the populate and prepare phases in a sandbox to prove the package
     installs;
  5. writes `<name>/<version>/` and its **fingerprint** (section 9.2).

  It does not commit. The maintainer reviews the new directories and pushes
  them. With `--auto-push`, the tool commits and pushes directly. The
  registry is therefore the place where a hash is computed and reviewed.
  Local installs never auto-populate hashes.
- **Every version is kept** (D36). A version is a small `pkg.toml`, and
  deletion is manual.
- **Fingerprints** let gupkg verify a package that is installed without a
  download, such as a distribution folder (section 9.2, D29).
  **The algorithm (issue 031, `gupkg.services.fingerprint`, version 1):** every regular
  file below the version directory except the top-level `App` and `gupkg` folders is
  described by a line `<size> <sha256 of its bytes> <relative path>` (`/` as separator,
  UTF-8); the lines are sorted by the UTF-8 bytes of the path; the fingerprint is the
  SHA-256 of `gupkg-fingerprint-1\n` followed by the lines. Timestamps and attributes are
  not part of it, empty directories have no effect, an empty file is covered, line endings
  are **not** normalised (so the registry stores every file byte for byte: `* -text` in its
  `.gitattributes`, launchers CRLF, everything else LF), and links, junctions and special
  files are refused rather than followed or skipped.
- **The registry tool** is `gupkg_registry` in the gupkg repository (`python -m gupkg_registry`
  `update`, `pin`, `validate`; the registry's `_scripts\registry-tool.cmd` runs it). It is a
  maintainer tool, separate from core and not shipped in the release bundle. It writes the
  `sha256` and `sha256_from` (`"registry"` for a hash it computed itself, over `https://` only,
  flagged `[REVIEW]` in its output and in the `--auto-push` commit message), the support files
  of the tracker without any update-check code, and the launchers generated from the template,
  then records the fingerprint in `_registry.toml`. It runs unpack and prepare in a sandbox
  folder and discards the staged `App`. A package is its unit of failure. Without `--auto-push`
  it starts no `git` process.
- **`unpinned`** (`_registry.toml`): `"name/version"` entries for legacy versions that still have no
  hash. Validation tolerates exactly those and nothing else; `registry-tool pin name/version`
  hashes one and removes the entry. It exists for the one pre-1.0 version (`qbittorrent/5.2.3`).
- **Unversioned download addresses** (`unversioned` in `_registry.toml`, a list of package names). Where the
  vendor serves every release at one address (Sysinternals' `Autoruns.zip`, ffmpeg's
  `ffmpeg-release-essentials.zip`, XnView MP, DontSleep, Process Explorer, Shutter Encoder), a pinned
  version keeps working only until the vendor replaces the file: afterwards its `sha256` fails for ever,
  so "every version is kept" does not make such a version installable. The registry keeps the directory
  anyway (deletion is manual), the tool prints `[UNVERSIONED]` and names these packages in the commit
  message, and a test compares the list with the fixtures. A maintainer who needs an old build must
  host it and change that version's `download_url` by hand.
- **Version names** are checked wherever a version directory is created or read (`is_version_name`):
  Windows device names (`CON`, `NUL`, `COM1`..., with or without an extension), control and bidirectional
  characters, `%`, `$`, `~<digit>`, a leading dash, surrounding spaces and a trailing dot are refused, so a
  hostile page cannot make the tool write a directory a Windows checkout cannot hold. Unicode control, format,
  separator, private-use, surrogate and unassigned characters, variation selectors and names over 128
  characters are refused too (a version a tracker discovers is capped at 64).
- **The tool's housekeeping and limits.** Leftover `.new-<8 hex>` staging folders are removed only when
  older than an hour (a live run keeps its own) and a folder that cannot be removed is a warning. The
  fingerprint walk refuses more than 10,000 files, 2,000 directories, 32 levels or 512 MiB. `git add` and
  `git commit` use `:(literal)` pathspecs; `--remote` and `--branch` follow the rules of `git
  check-ref-format` (a branch may not start with `refs/`).
- **Validation** (`gupkg_registry validate`, run by the registry's CI) requires: zero errors and zero
  warnings from the package checker for every tracker and version, explicit `modes`, a `sha256` and
  its provenance on every version, a fingerprint that matches the files, launchers equal to the
  template (none in a tracker), at most 13 hook files each with a `Why no service` note, no link,
  no `.bak`, LF endings in text files, a safe name for every version directory, and no stray directory,
  file or leftover `.new-*` staging folder in a package folder.
- **Every registry package installs under the strict CLI default** (no
  `--skip-checksum` needed). That is the acceptance bar for the migration.
- **Several registries.** `[[registry]]` entries can name several
  registries, for example the official one plus a private share. The
  selector `registry/name` disambiguates; otherwise the first registry that
  has the name wins, and `search` shows all of them. A local package follows
  that entry unless it opted out (D2).
- **Cache location.** The cache moves out of the gupkg version's `App` into
  the root data directory, `<root>\.gupkg\registry\`, so it survives
  self-updates. System runs use only the system root's cache. Old trees are
  pruned by `cleanup`.
- **Validation is per package.** One invalid package is reported and skipped.
  It no longer fails the whole sync.

## 18. Migration

Migration is done by a **separate, non-core tool**, `gupkg-migrate` (D33),
built in issue 023. It may import gupkg core (D44). It ships in the release
bundle and is called as `gupkg migrate …` (D45). 1.0 core contains no code for
0.17 data: no format 1 reader, no `_bootstrap` handling and no 0.17 aliases.

- **Upgrading gupkg itself.** 0.18 → 1.0 is a one-time re-run of the
  bootstrap script (section 21.5). 0.18's `self update` says so when it sees
  a 1.0 release.
- **`gupkg-migrate package <path>`** converts a version directory or
  `_bootstrap` to format 2:
  - `[origin]` and `[update.*]` become `[source]` and `[track]`. The
    overloaded `[origin] url` is split by meaning: a GitHub or project page
    becomes `project_url`, or `[track] repo`; a real artifact becomes
    `[source] download_url`. `homepage` becomes `project_url`;
  - a 0.17 origin script (`.ps1`, `.cmd`, `.bat` or an executable) becomes
    `[source] script` (section 4);
  - `[[origin.versions]]` is dissolved. The entry whose `version` matches the
    directory name becomes the single `[source]`. Every other entry is
    reported, with a suggested `gupkg pkg new <name>`, and dropped. Leftover
    `name`, `version` and `localVersion` keys are deleted;
  - camelCase keys become snake_case;
  - `only_portable` becomes `modes`;
  - `ignore_checksum` is dropped with a note;
  - `_bootstrap` is renamed to `_check_updates`;
  - hooks that match a known service pattern are reported with the suggested
    declarative replacement.

  It writes a backup by default and validates the result before replacing the
  file.
- **`gupkg-migrate settings <gupkg-config.toml>`** converts manager config
  schema 2 into `settings.toml`.
- **`gupkg-migrate shortcuts`** turns existing `.lnk` shortcuts into
  `[[shortcut]]` tables (0.17's `shortcuts_to_gupkg_toml.py`, D45).
- **Existing installs need no reinstall.** The migration tool *adopts*
  them. For each converted package that 0.17 installed, it writes
  `.gupkg\package.toml` from the existing `current` and the declared
  components, and marks those components "adopted". It reads old receipts
  and `.gupkg\state\update.toml` once, then removes them. Converting a
  package also writes its 1.0 launchers (`install.cmd`, `gupkg-tui.cmd`).
  `gupkg-migrate package <root-or-package>` does both (`--no-adopt` only converts;
  `--mode` and `--settings` decide the mode when the root does not): the effects of the
  record are the ones the engine would apply, each marked adopted, and a package whose
  active version is not converted yet is reported with the command that converts it.
- **gupkg's own package** (`src\pkg.toml`, `src\pkg.local\check_update.py`,
  `unpack_app.py`) is rewritten for format 2 as described in section 21.6:
  a custom tracker and a custom populate script.
- **The registry is converted with the migration tool** in the registry
  repository. The 62 hooks are then replaced package by package (issue
  031). Hooks left with no remaining purpose are deleted.
- **No compatibility window.** A format 1 manifest gets the generic
  "unsupported format" error; core does not mention the migration tool.

## 19. Decisions

D1, D3, D4 and D10–D20 are the owner's decisions of 2026-10-22. D21–D46
were taken by the owner in the review that followed job 07, on the same
day; they amend D2, D10, D11, D12, D14, D15 and D16 where the rows say so. D2
and D5–D9 were proposed by the designer and confirmed in that review, D2
in changed form.

| # | Decision | Alternatives considered |
| --- | --- | --- |
| D1 | Rename `_bootstrap` to **`_check_updates`** (owner's choice, 2026-10-22: the folder's only job is to check for a newer upstream version and write a new version directory). | `_latest`, `_track`, `_upstream`, `_channel` |
| D2 | **A local package follows the registry entry with the same name automatically** (Scoop-like; owner's change, 2026-10-22). It can opt out with `gupkg link --unlink`, or follow another entry with `gupkg link`. | An explicit link only (the earlier draft: it avoids merging a custom local package with an unrelated registry entry) |
| D3 | A checksum **mismatch is always fatal**; `--skip-checksum` covers only missing hashes. | Allow `--skip-checksum` to also bypass mismatches (Scoop's `--skip-hash`) |
| D4 | `default_mode = "auto"` (owner's decision, 2026-10-22). Elevated selects `system` (a package that doesn't support system is refused: an elevated gupkg never acts on a user root, section 9.6); non-elevated selects `user`; never `portable`. It applies only when no flag, install record or containing root decides, and it means the same in every command. | `default_mode = "user"`, with elevation never selecting system (the earlier draft) |
| D5 | Format 2 is strict, with an explicit migration tool; no dual-format reader. | Read both formats for one release |
| D6 | Keep Textual for the interactive UI (redesigned, in `gupkg-manager`); Rich for console runs. | Rich-only with inline prompts; prompt_toolkit |
| D7 | Portable mode persists declared paths in `<package>\data` and links/copies them into each `App`. | Keep everything in `App` and copy the whole old `App` forward on update |
| D8 | The registry tool computes hashes for vendors that publish none (`sha256_from = "registry"`), and the maintainer reviews them (D36). | Leave such versions unhashed and rely on the missing-checksum policy |
| D9 | `update` never installs a package that is not installed. | `update` installs missing packages |
| D10 | **Version directories contain no update-check code** (owner's decision, 2026-10-22). `_check_updates` is the only component that discovers anything over the network. Validation rejects `[track]` and `track` hooks in a version directory. Amended by D28: version-directory scripts may use the network (section 9.3). | Allow `[track]` in pinned versions so that a lone version directory can update itself |
| D11 | **Checking and installing are separate operations** (owner's decision, 2026-10-22). `gupkg check-updates` (D21) runs trackers, writes pinned version directories and by default populates them (D23), and stops. `install` and `update` install only already-known versions and never run a tracker. No flag combines the two. | The tracker hands the install off to the new version (the earlier draft); an `update --check` convenience flag |
| D12 | **The manager is a separate program, `gupkg-manager`** (owner's decision, 2026-10-22; with `gupkg-tui`, D34). Amended by D44: it may import gupkg core and any libraries for reading, but every operation is a CLI call through the versioned machine interface (`--json`, `--plan`, `--events-file`, `--no-input`). The CLI acts on explicit targets, with no `--all`, no UI and no checksum prompts. | A `manager` subcommand or mode inside the CLI (0.17); the manager importing the engine directly |
| D13 | **Stage 0 is issues 016–019, and they land first.** gupkg runs only on its bundled interpreter (`-I -s -B -S`). There is no runtime pip, dependency install or dependency check, and the bundle integrity is `MANIFEST.sha256`. The rehaul builds on these rather than redesigning them (section 20). | Fold them into the rehaul issues |
| D14 | **gupkg is an ordinary format 2 package of itself.** It has a custom `_check_updates` tracker and a custom populate script, and no `download_url` (D27). `self check-updates` and `self update` are aliases for `check-updates gupkg` and `update gupkg`, so D11 applies to gupkg too. | Keep self-update as special-cased hooks (`src/pkg.local`) |
| D15 | **Bundled mode** (owner's direction, 2026-10-22). Any version directory may carry a complete gupkg in `<version>\gupkg\`, so a program can be installed **without installing gupkg**. A bundled install keeps (and carries forward) its bundled copy to manage itself later, and never writes a root forwarder. gupkg's own package always carries a bundled gupkg, which its populate script copies into `App` (D27; the earlier `[source] local` is removed). The engine never executes a bundled tree (section 21). | A bundled install also installs gupkg (rejected by the owner: no extra infrastructure); gupkg's payload outside `App` (`into = "gupkg"`, an earlier draft) |
| D16 | **Package launchers find gupkg relative to themselves.** They try the root forwarder `%~dp0..\..\.gupkg\gupkg.cmd` first, then the bundled `%~dp0gupkg\gupkg.cmd`. They never use `PATH`, the registry, AppData or ProgramData. Elevated launchers first verify, with PowerShell `Get-Acl` from System32 and failing closed, that everything they will run is not user-writable (recursive, D30), and refuse otherwise. Exit code 6 triggers the fallback to the bundled copy (section 21.3). | Search `PATH` (rejected: a fake `gupkg.exe` cannot be trusted to refuse itself); registry registration; locator files in AppData or ProgramData |
| D17 | **A distribution folder outside every root is copied into the mode's root, then installed** (`--in-place` opts out). System-wide installs of a distribution are done only by the installed system gupkg from an admin shell, which copies the data and runs nothing from the source. A system bootstrap extracts straight into an admin-only `C:\opt` (section 21.4, 21.5). | Elevating a launcher in Downloads and copying with `robocopy` (an earlier draft) |
| D18 | **The Windows registry is a write target, never a source of information** (owner's decision, 2026-10-22). gupkg never reads it to learn anything: not its own location, not settings, not package state, not installed software. Reads happen only as part of a change (read-modify-write of `PATH`) or to verify a value gupkg wrote. There is no generic registry table; specific effects such as file types may come later (section 9.5). | A `Software\gupkg` registration for discovery (an earlier draft); a generic `[[winreg]]` table |
| D19 | **Threat model T1** (owner's decision, 2026-10-22). gupkg runs as administrator for system mode. A standard user must not gain administrator rights through gupkg: an elevated gupkg never runs code, and never reads inputs, that a standard user can modify. Only an admin-only gupkg elevates, and only by relaunching itself (section 9.6). | T0: treat elevation as a convenience only |
| D20 | **gupkg owns no files outside its roots** (owner's decision, 2026-10-22). Its settings, logs, registry cache, state and the root forwarder live in `<root>\.gupkg\`, and each package's own logs and records in `<package>\.gupkg\`. It never uses `%LOCALAPPDATA%`, `%APPDATA%` or `%ProgramData%` for its own data (section 9.7). | Per-user and machine-wide AppData/ProgramData folders (an earlier draft, taken from 0.17's embedded site-packages) |
| D21 | **Names.** Update discovery is `gupkg check-updates`; `gupkg check [--fix]` is the health check and replaces `doctor`; `gupkg pkg check` stays the authoring validator. | `gupkg check` for update discovery (the earlier draft) |
| D22 | **Who checks for updates.** For a system package an administrator runs `check-updates` from an admin shell, and it writes into the system root. From a normal shell it implies `--no-download` and, for a system package, only reports. No proposals (section 9.3). | Non-elevated checks writing proposals that an elevated `update` admits; an inbox folder with an `admit` command |
| D23 | **`check-updates` populates by default.** It writes the new version directory and downloads, verifies and unpacks it, but activates nothing. `--no-download` writes only the directory. `update` activates, populating first if needed. | Write `pkg.toml` only, download at `update` (the earlier draft) |
| D24 | **`--skip-checksum` for system installs** is allowed, also when relayed by the manager, but only for `https://` URLs (section 9.1). | Only from an admin shell; only through the system settings; never in system mode |
| D25 | **The CLI confirms consequential actions** unless `--yes`; `--no-input` turns a question into exit 5. Never about checksums. | The CLI never prompts |
| D26 | **Logs.** Per-package logs in `<package>\.gupkg\logs\`; the root's `.gupkg\logs\` holds global run information with links. `--events-file` takes a plain name. A bundled gupkg logs package-less runs into its own package (section 14.3). | Per-mode log directory only; a caller-chosen events path |
| D27 | **Populate scripts are first class** (section 4): the built-in download script, or a custom script (Python, `.ps1`, `.cmd`/`.bat`, executable). A custom-populated `App` is as verified as the package, and recoverable while the script's inputs exist. `[source] local` is removed. | `[source] local` copying a shipped folder (the earlier draft) |
| D28 | **Version-directory scripts may use the network.** Python cannot sandbox trusted code. D10 keeps only "no update-discovery code in version directories". | A contract that version-directory hooks get no HTTP client, with an import warning |
| D29 | **Installs with no download** are labelled *origin not verified*, unless a registry fingerprint matches (then *verified against registry*). A mismatch refuses the install (section 9.2). | `verified-by-bootstrap`; the bootstrap vouching for its ZIP |
| D30 | **Elevated launchers check everything they will run, recursively**, follow `current` safely, and trust `%SystemRoot%` (sections 9.6, 21.3). | Root and version directory only (job 07, spike S1 showed admin-only parents with user-writable children) |
| D31 | **An elevated gupkg never acts on a user root**, neither through the `auto` fallback nor an explicit `--mode user` (nor `--mode portable`: a portable root is user-writable too). | Fall back to user mode; drop elevation for user-mode work |
| D32 | **Which gupkg runs.** The installed gupkg through the root forwarder, unless `--no-system-gupkg=true`; otherwise the bundled copy. No version comparison. Carry-forward copies the package's own bundle; bundles stay until an explicit `cleanup --bundled`. | Copy the running gupkg, never an older one; drop old versions' bundles |
| D33 | **No legacy-specific code in core.** A separate migration tool (built in issue 023) converts formats, settings and `_bootstrap`, and adopts 0.17 installs. No 0.17 aliases. 0.18 → 1.0 is a bootstrap re-run (section 18). | `gupkg pkg migrate`, `settings init --from`, deprecated aliases and automatic adoption in core |
| D34 | **Two UI programs:** `gupkg-tui` (one package) and `gupkg-manager` (general), both in issue 030. `gupkg tui` and `gupkg manager` are permanent forwarders (section 15). | One `gupkg-manager` program with a package view |
| D35 | **Issue 017 lands strictly before issue 021.** | 017 may land after 021 |
| D36 | **The registry tool** creates new versions with hashes and fingerprints; a maintainer reviews and pushes, or `--auto-push` does. Every version is kept (section 17). | CI committing or opening pull requests; keeping only N versions |
| D37 | **Cancel stops after the current package** (sections 10.4, 13.2). | After the current phase; only between CLI calls |
| D38 | **The registry rule is tested by behaviour** (`FakeSystem`'s registry access log), not by inspecting code (section 16). Its UI half was dropped by D44. | Module-boundary tests; a CI lint step |
| D39 | **No `inno` kind in 1.0**; GIMP runs its vendor installer. A `tar` kind is added; MSYS2 uses its `.tar.xz` (section 8.2). | innoextract or innounp; bundling 7-Zip ZS; running MSYS2's `.sfx.exe` |
| D40 | **Copying a distribution into a root removes the Mark of the Web**, and the receipt records it (section 21.4). | Keep the marks and document `Unblock-File` |
| D41 | **Every install records an `App` fingerprint**; a package opts out with `record_manifest = false` (section 4). | Off by default; only for shipped `App`s |
| D42 | **Portable target.** A folder shaped `<name>\<version>\pkg.toml` installs in place; otherwise the configured portable root, or gupkg asks (section 6.3). | A fixed `%USERPROFILE%\opt-portable` default |
| D43 | **`cleanup` never runs automatically** (`[cleanup] auto` enables it). **No lite bundle** in 1.0. | Automatic cleanup; `pkg bundle --lite` |
| D44 | **The separate programs may import core.** `gupkg-tui` and `gupkg-manager` may import gupkg core and any libraries for reading and reference, but every operation they perform is a `gupkg` CLI call (sections 10.4, 12). The migration tool may import core and does its own work. Amends D12 and D38. | No imports at all (D12 as first written); UI programs doing operations in-process |
| D45 | **Every separate tool is callable as `gupkg <command>`.** `gupkg tui`, `gupkg manager` and `gupkg migrate` are permanent forwarders. The migration tool ships in the release bundle and includes the shortcut import (0.17's `shortcuts_to_gupkg_toml.py`) (sections 10.1, 18). | Separate commands only |
| D46 | **A refused download leaves the version without `App`.** When `check-updates` finds a version but the checksum rules refuse its download (no hash and no `--skip-checksum`), the version directory stays written without `App`, and the refusal is reported (section 7.4). | Write nothing |

There are no open questions. The six questions of the first draft were
answered by the owner on 2026-10-22: the default portable root (D42), the
number of registry versions (D36), automatic cleanup (D43), `App`
manifests (D41), `cleanup --bundled` (D32) and the lite bundle (D43).

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
| 018 | Exactly one interpreter, `<version>\gupkg\python\python.exe -I -s -B -S`, enforced on every start (`enforce_bundled_runtime`). Development uses `.venv`. | The same launch rule for `gupkg-tui`, `gupkg-manager` and the elevated relaunch (sections 9.6, 13.1). Subprocess tests start `gupkg.cmd`, never `sys.executable -m gupkg`. |
| 019 | No pip in the bundle or the lock; the build fails if pip is present; a build probe imports every locked module. | Adding `tomli_w` as a locked runtime dependency (section 12). The build probe must also import `gupkg_ui`, `gupkg_tui` and `gupkg_manager`. |

### 20.2 What the rehaul adds on top

1. **gupkg's own package always carries a bundled gupkg, which becomes its
   `App`** (D14, D15, D27, section 21). Its distribution is a version
   directory holding `pkg.toml`, the two package launchers and `gupkg\` (the
   bundled runtime).
   - gupkg's custom populate script copies the bundled tree into `App`, and
     the installed gupkg runs from `App`. gupkg's own package has no
     `download_url`, and its tracker is custom too.
   - This removes the 0.17 workaround in `src\pkg.local\unpack_app.py`,
     which keeps the tree at the version root and writes an
     `App\RELEASE.txt` marker only so that the engine counts the version as
     populated.
   - Issue 016's ZIP layout is the distribution layout of section 21.2.
2. **Self-updates follow D11.** `gupkg self check-updates` and
   `gupkg self update` are aliases for `gupkg check-updates gupkg` and
   `gupkg update gupkg`. In 1.0 the 0.17 hooks of issue 016 become a custom
   tracker and a custom populate script (section 21.6).
   - The registry may also publish pinned `gupkg\<version>\` directories,
     with fingerprints, so a linked install can update with no local check.
3. **`verify_manifest` is a general unpack service** (section 8.2). It checks
   the unpacked payload against a `<sha256>  <path>` manifest shipped in the
   artifact. gupkg's own bundle (`MANIFEST.sha256`) is the first user. Every
   install also records an `App` fingerprint (D41).
4. **`self status` and `check`.** Issue 016's "check the active version
   against `MANIFEST.sha256`" becomes `gupkg self status`. gupkg's own
   package also shows up in `gupkg check` like any other package.
5. **The UI programs run their sibling CLI** (section 10.4).
   They always use the `gupkg.cmd` next to them in the same bundle, never one
   found on `PATH`. This follows issue 018's rule against interpreter probing.
6. **The bootstrap script becomes thin** (section 21.5). `gupkg-bootstrap.ps1`
   downloads and verifies a release ZIP, extracts it, and runs its
   `install.cmd`. For system mode it first creates an admin-only `C:\opt`
   and extracts into it (section 21.5). Installing the bundled gupkg itself
   does the rest, including the root data directory with settings schema 3.
   That replaces `manager init`.
7. **0.18 → 1.0 is a bootstrap re-run** (D33). 0.18's `self update` points
   to the bootstrap when it sees a 1.0 release.
8. **One documentation pass.** Issues 016 and 018 update the README and
   `_docs` for the new distribution. Issue 032 rewrites those same pages for
   1.0 and must build on their text, not replace it.

### 20.3 Order

```text
stage 0:  016 ─► 018, 019        017                (all four land first: D13, D35)
stage 1+: 021 (System boundary; moves 017's code behind the port) ─► … ─► 032 (1.0)
```

Stage 0 needs no format change. It can ship as 0.18 while the rehaul issues
are worked on. Issue 021 starts only after all of stage 0, including 017,
has landed (D35).

## 21. Bundled mode, package launchers, and bootstrap

Bundled mode, as the owner defined it on 2026-10-22, is **a way to install a
package without installing extra infrastructure**. A program's distribution
can carry gupkg inside its own version directory, as `<version>\gupkg\`.
Someone unzips the folder anywhere and runs `install.cmd`. The program is
then installed with its shortcuts, shims, environment and PATH, and **gupkg
itself is not installed**: no gupkg package, no root forwarder, no gupkg
shims, and no gupkg `PATH` entry appear on the machine.

gupkg's own package uses the same mechanism. Its distribution *always*
carries a bundled gupkg. When the package is installed, its populate script
copies that bundled tree into `App`, and the installed gupkg runs from
`App`. gupkg is therefore an ordinary package in which `App` is the
payload, and the bundled copy is the means of getting it there.

Everything in this section follows the threat model of section 9.6 (T1), the
registry rule of section 9.5, and the rule that gupkg owns no files outside
its roots (section 9.7).

### 21.1 Terms

| Term | Meaning |
| --- | --- |
| **Root** | A folder that holds packages, one subfolder each, with one install mode (section 3). Default roots: `C:\opt` (system), `%USERPROFILE%\opt` (user). |
| **Root data directory** | `<root>\.gupkg\`: gupkg's own files for that root (settings, global logs, registry cache, state) and the **root forwarder**. gupkg owns no files outside its roots (section 9.7). |
| **Root forwarder** | `<root>\.gupkg\gupkg.cmd` and `gupkg-manager.cmd`: two-line scripts that call the installed gupkg (`…\gupkg\current\App\gupkg.cmd` and its sibling). Package launchers find an installed gupkg through it, at a fixed position relative to themselves (section 21.3). |
| **Bundled gupkg** | A complete gupkg runtime tree in `<version>\gupkg\`: the commands `gupkg.cmd`, `gupkg-tui.cmd` and `gupkg-manager.cmd`, the `gupkg` Python package with the `python\` of issue 018 inside it, the `gupkg_ui`, `gupkg_tui`, `gupkg_manager` and `gupkg_migrate` packages beside it, `VERSION`, `RELEASE.txt` and `MANIFEST.sha256` (issue 016; its paths are relative to the version directory and name only the files of the tree). It needs nothing installed. |
| **Installed gupkg** | The `current` version of the `gupkg` package in a root (`<root>\gupkg\current\App\`). Only installing the `gupkg` package creates one, and that install writes its root's forwarder. |
| **Bundled install** | A package installed by a bundled gupkg, with no installed gupkg involved. It writes no forwarder. The package keeps its bundled copy, so it can later be updated, repaired and uninstalled the same way (section 21.4). |
| **Distribution** | A ZIP of a package-root fragment (`<name>\<version>\…`, optionally with `<name>\_check_updates\`) whose version directory carries a bundled gupkg and the package launchers. |
| **Package launchers** | `install.cmd` and `gupkg-tui.cmd` in a version directory, and `check-updates.cmd` in a `_check_updates` folder (which only checks and never installs). gupkg core generates all of them from one template, whichever gupkg runs the operation. They find a gupkg to run (section 21.3). `gupkg-tui.cmd` keeps today's name and now starts the `gupkg-tui` program (section 15). |

### 21.2 Layouts

```text
C:\opt\                               a root (system mode, admin-only)
  .gupkg\                             root data directory (section 9.7)
        gupkg.cmd  gupkg-manager.cmd      root forwarder -> C:\opt\gupkg\current\App\...
    settings.toml  state.toml  logs\  registry\
  gupkg\                              gupkg's own package
    current\                          -> 1.0.0
    1.0.0\
      pkg.toml                        [source] script = "pkg.local/populate.py"; no download_url
      pkg.local\populate.py           copies the bundled tree into App
      App\                            the installed gupkg runs from here
        gupkg.cmd  gupkg-tui.cmd  gupkg-manager.cmd  gupkg\ (the package, with python\)  gupkg_ui\  gupkg_tui\  gupkg_manager\  gupkg_migrate\  MANIFEST.sha256  VERSION  RELEASE.txt
      gupkg\                          bundled copy: kept, it is the populate script's input
      install.cmd  gupkg-tui.cmd
    _check_updates\pkg.toml  pkg.local\track.py
  firefox\
    135.0.1\
      install.cmd  gupkg-tui.cmd       launchers find C:\opt\.gupkg\gupkg.cmd (two levels up)

MyApp-1.2.0.zip                       a distribution of another program
  myapp\
    1.2.0\
      pkg.toml                        ordinary format 2 pinned version
      App\                            empty (filled by its populate script) or shipped (section 4)
      gupkg\                          bundled gupkg (reserved name)
      install.cmd  gupkg-tui.cmd
    _check_updates\                   optional

gupkg-1.0.0-win-amd64.zip             gupkg's own distribution (issue 016)
  gupkg\
    1.0.0\  pkg.toml  pkg.local\  gupkg\  install.cmd  gupkg-tui.cmd
    _check_updates\pkg.toml  pkg.local\track.py
```

Rules:

- **Reserved names inside a version directory:** `gupkg\`, `install.cmd` and
  `gupkg-tui.cmd`, like `pkg.local`. The engine reads, verifies and copies a
  bundled gupkg, and never executes it. Only package launchers run a bundled
  gupkg.
- **Copying the bundled tree into `App`** is the job of gupkg's own
  populate script (section 21.6). Any other package that ships files to
  copy into `App` uses a populate script of its own (section 4).

### 21.3 Package launchers: which gupkg runs

`install.cmd`, `gupkg-tui.cmd` and the tracker's `check-updates.cmd` are generated from one template of gupkg core
(`gupkg\launchers\package-launcher.cmd`) by `check-updates` (new version directories, every root), `gupkg pkg new`,
`pkg bundle` and `pkg export`, the registry tool (`launchers`) and the migration tool. They are plain `cmd`.
A launcher lives at `<root>\<name>\<version>\`, so its root is `%~dp0..\..`.

**Which gupkg** (D16, D32):

1. `%~dp0..\..\.gupkg\gupkg.cmd`: the root forwarder, meaning the installed
   gupkg that manages this root. It is skipped when the launcher is given
   `--no-system-gupkg=true`.
2. `%~dp0gupkg\gupkg.cmd`: the bundled gupkg.
3. If neither exists, print how to get gupkg (the bootstrap URL) and exit
   9009.

The launcher never compares versions. The only fallback is the exit code 6
rule below.

**Elevated run.** The same lookup applies, but first the launcher must
establish that nothing it will run is user-writable. It **refuses to
proceed** otherwise.

- **What is checked** (D30): everything the launcher will run, recursively.
  That is the root (`%~dp0..\..`), the package directory (`%~dp0..`), the
  version directory (`%~dp0`), the root's `.gupkg\` with the forwarder, and
  the whole gupkg tree that will start, including its `python\`. Each must
  be owned by Administrators, SYSTEM or TrustedInstaller. No other security
  identifier may hold any write-type right on any of them: write, append,
  create files or folders, delete, delete-child, change permissions, or
  take ownership.
- **Why recursively.** Inheritance can be broken, and Windows itself does
  it: admin-only `C:\Windows` holds the user-writable `C:\Windows\Tasks`
  (job 07, spike S1). And `python.exe` and the standard library run before
  gupkg could check its own tree.
- **Through `current`.** When `%~dp0` is the `current` junction, the check
  resolves it, requires the target to be a version directory of the same
  package, and checks both the junction and the target. `Get-Acl` on a
  junction returns the junction's own permissions, not the target's (S1).
- **How it is checked.** With a tool that is not inside the location being
  checked, because a checker shipped there could have been replaced. The
  launcher calls
  `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile
  -NonInteractive` with a fixed `Get-Acl` script that compares security
  identifiers, so it does not depend on localized account names.
  - The script parses the SDDL text of `Get-Acl`'s `.Sddl` property and
    invokes no .NET methods, so it also runs under Constrained Language
    Mode, which blocks method calls such as `GetAccessRules(...)` (S1).
  - The script travels inside the launcher, after a marker line that `cmd`
    never reaches, and PowerShell reads it from `%~f0`. Passed as
    `-EncodedCommand` it would exceed `cmd`'s 8,191-character line limit
    (S1).
  - A null DACL (no `D:` part in the SDDL, so everyone has full access), an
    ACL that cannot be read, and an unknown ACE type or right all fail the
    check.
  - The check **fails closed**: if PowerShell is blocked or the result is
    unclear, the launcher refuses.
  - It costs about 0.6 s of PowerShell start-up, plus about 0.3–0.4 s for a
    whole gupkg tree (S1).
  - `%SystemRoot%` is trusted to locate PowerShell (section 9.6, D30).
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
  command with the bundled gupkg, if there is one. When elevated, the
  bundled gupkg has been checked with everything else.
- **The launchers:**
  - `check-updates.cmd` (in `_check_updates` only) runs `gupkg check-updates <name>`, the name being the folder's
    parent, and nothing else.
  - `install.cmd` runs `gupkg install "%~dp0."` and pauses on failure.
    - `gupkg-tui.cmd` runs `gupkg tui "%~dp0."` through the gupkg it found,
    which starts `gupkg-tui` and opens the package view: check-updates,
    update, repair, uninstall, logs.

  Both pass through `GUPKG_NO_PAUSE` as today, and accept
  `--no-system-gupkg=true`.

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
- **The copy drops the Mark of the Web** (D40). Explorer marks every file of
  a downloaded ZIP it extracts, and Windows then holds a double-clicked
  launcher behind a security warning (job 07, spike S2). The copies in the
  root carry no mark, and the receipt records that the source was marked.
- **Verification** (D29). Nothing is downloaded, so the install is labelled
  *origin not verified*, unless the registry publishes a fingerprint for
  that package and version and it matches. A mismatch refuses the install
  (section 9.2).
- **`--in-place` keeps the folder where it is.** It becomes an ad-hoc root
  (section 5.4). A portable install of a folder shaped
  `<name>\<version>\pkg.toml` stays in place without the flag (D42).
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
  version's `gupkg-tui.cmd` uses it for check-updates, update, repair and
  uninstall.
- **It is carried forward** (D32). When `check-updates` or `update` creates
  a new version directory in a root that has no forwarder, the package's
  own bundled gupkg is copied into it. The active version of a bundled
  install therefore always has a gupkg to run.
- **Uninstalling leaves only gupkg's data.** `uninstall --purge` removes the
  package. What remains is the root's `.gupkg\logs`.
- **An installed gupkg takes over later.** If gupkg is later installed into
  the same root, the launchers of existing bundled installs find the new
  forwarder and use it, unless they are given `--no-system-gupkg=true`.
  `cleanup --bundled` can then drop their bundled copies (section 21.8).

### 21.5 Bootstrap: installing gupkg itself

Bootstrap is a bundled install of the `gupkg` package, plus the root data
directory that installing the `gupkg` package always creates. Re-running it
is also how a 0.18 installation moves to 1.0 (D33).

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
     verifies everything it will run (section 21.3). It finds no forwarder
     yet, so it runs the bundled gupkg, which is inside the verified root.

   Either way, the `gupkg` package is installed like any other package. The
   install:
   - populates `App` by running gupkg's populate script, which **copies the
     bundled tree** and checks it against `MANIFEST.sha256`;
   - creates `current`;
   - creates the shims `gupkg.exe`, `gupkg-tui.exe` and `gupkg-manager.exe`
     pointing into `$App`, puts the bin directory on `PATH`, and adds the
     Start Menu entry;
   - creates the **root data directory** `<root>\.gupkg\`, with the forwarder
     and default settings;
   - labels the install like any distribution install: *verified against
     registry* when the registry's fingerprint matches, otherwise *origin
     not verified* (D29). The bootstrap's own ZIP check is not passed on.
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
provider = "module"                 # custom: pkg.local/track.py reads GitHub releases and RELEASES.md

[source]
script = "pkg.local/populate.py"    # custom populate script; gupkg has no download_url (D27)

[[shim]]
name = "gupkg"
target = "$App\\gupkg.cmd"

[[shim]]
name = "gupkg-tui"
target = "$App\\gupkg-tui.cmd"

[[shim]]
name = "gupkg-manager"
target = "$App\\gupkg-manager.cmd"

[root_forwarder]                    # only valid in the gupkg package: writes <root>\.gupkg\gupkg.cmd and gupkg-manager.cmd
target = '$PackageRoot\current\App'
```

- **The populate script** copies the bundled tree `<version>\gupkg\` into
  `App` and checks it against its `MANIFEST.sha256`. When a version
  directory has no bundled tree yet, as after a self-update check, the
  script first fetches that version's release ZIP, verifies it against the
  committed `RELEASES.md` row, and extracts the bundled tree into
  `<version>\gupkg\` (D28 allows the network). The tree is kept: it is the
  script's input, so `App` stays recoverable (D27).
- **The tracker** is a custom `track.py`. It reads the newest GitHub release
  and its `RELEASES.md` row. It is never copied into a version directory.
- **Verification.** gupkg's `App` is as verified as its version directory
  (D27). A version directory that the registry publishes is checked against
  its fingerprint (D29).
- **Self-update.**
  1. `gupkg self check-updates` writes `<root>\gupkg\<new>\` with
     `pkg.toml`, `pkg.local\populate.py` and the two launchers, and by
     default populates it, fetching the release as described above. For a
     system root it runs from an admin shell (D22).
  2. `gupkg self update` activates it by switching `current`.

  The forwarder already goes through `current`, so it needs no change. The
  running process keeps executing from the old version's `App`, which stays
  on disk until `cleanup`.
- **The bundled copy is kept** in gupkg's own version directories, because
  it is the populate script's input (D27, D32).

### 21.7 What the format and CLI gain

| Addition | Section | Purpose |
| --- | --- | --- |
| `[source] script = "<path>"` | 4, 8.6 | A custom populate script: Python, `.ps1`, `.cmd`/`.bat` or an executable (D27). |
| Reserved version-directory names `gupkg\`, `install.cmd`, `gupkg-tui.cmd` | 4 | The bundled runtime and the launchers. The engine never executes the bundled tree. |
| Carrying a bundled gupkg forward | 5.3, 7.4 | New version directories of a bundled install get a copy of the package's own bundle, so the active version can always manage itself (D32). |
| `[root_forwarder]` (gupkg package only) | 7, 13.1, 21.3 | Writes `<root>\.gupkg\gupkg.cmd` and `gupkg-manager.cmd`, recorded in the install record and removed by uninstall. |
| `requires_gupkg = ">=1.2"` (optional) | 7 | The minimum gupkg version a package needs. An older gupkg exits 6. |
| Exit code **6** | 10.1 | "This package needs a newer gupkg." It drives the launcher fallback. |
| `--no-system-gupkg=true` (launchers) | 21.3 | Use the bundled gupkg even when an installed one is reachable (D32). |
| `gupkg install PATH --in-place` | 10.2, 21.4 | Install a distribution folder where it is (an ad-hoc root) instead of copying it into the mode's root. |
| `gupkg pkg bundle <version-dir>` | 10.2 | Copy the *running* gupkg's verified bundle into `<version-dir>\gupkg\`. Issue 033 adds writing the two launchers (the template is theirs). There is no lite bundle in 1.0 (D43). |
| `gupkg pkg export <version-dir> -o X.zip` | 10.2 | Write a distribution ZIP (`<name>\<version>\…`, plus `_check_updates` if present) and print its SHA-256. |
| Registry fingerprints | 9.2, 17 | Verify a package that is installed without a download (D29). |

### 21.8 Size and cleanup

A bundled gupkg is roughly 40 MB: the embedded Python, Textual and Rich.

- **Bundled copies stay** in every version directory a bundled install
  carries forward to, old ones included (D32). Plain `cleanup` does not
  touch them, so switching back to an old version always finds its gupkg.
- **`gupkg cleanup --bundled`** removes bundled copies from the packages of
  a root only when that root has a forwarder to an installed gupkg. It never
  removes gupkg's own bundled copies, which are its populate script's input,
  and it runs only when asked (D32, D43). Afterwards, `--no-system-gupkg=true`
  no longer works for those packages.
- **Registry packages do not ship a bundled gupkg.** Registry users already
  have gupkg. Bundling is for distributions outside the registry.
