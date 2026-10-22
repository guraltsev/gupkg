# Issue 016: One self-contained release ZIP; runtime handling moves to the dev tree

Date: 2026-10-22
Priority: High
Change type: Distribution, bootstrap, launcher, self-update, release tooling;
removes CLI commands

## Objective

A gupkg release is a single ZIP that already contains everything gupkg needs
to run: the embedded CPython, the locked dependencies, the native shims and
the application. The build produces this ZIP, together with all the integrity
data used to verify it.

Decisions (approved 2026-10-22):

1. **No system Python, ever.** gupkg runs only on the interpreter inside its
   own version directory. No `PATH` probing, no `py` launcher, no
   `GUPKG_PYTHON` or `gupkg.python` override, not even for development.
2. **No runtime acquisition in user-facing code.** Downloading CPython,
   installing pip, installing gupkg's dependencies and updating the lock are
   build steps. They live in `tools\` / `scripts\` of the dev tree and are
   never shipped or reachable from `gupkg`.
3. **No dependency installs or dependency checks at run time.** This covers
   gupkg's own dependencies and package-local hook imports. gupkg does not
   probe imports, does not consult the lock, and never runs pip. The build
   guarantees the bundle is complete. An `ImportError` means a damaged
   install, and the manifest check finds it.
4. **Integrity data comes from the build.** Hashes are computed when the
   bundle is built and published with it. Installed code only *checks* them:
   no pinned URLs, no pinned hashes and no hash generation in user-facing code.

Split-out work: decision 1 is implemented by **issue 018** (remove system
Python autodetection). Removing pip from the bundle is **issue 019**.

A missing or damaged runtime is an installation error with one fix: install
the release ZIP again (`gupkg-bootstrap.cmd`, or `gupkg manager self repair`
with a ZIP).

## What has to go (user-facing code)

| Where | What it does today | Action |
|---|---|---|
| `src/gupkg/gupkg.cmd:63-91` | Tries `GUPKG_PYTHON`, `gupkg.python`, the bundled runtime, `python` and `py -3`. | Remove. Run `python\python.exe -I -s -B` only. |
| `src/gupkg/gupkg.cmd:21-24, 103-123` | Pinned `python.org` URL and hash. Downloads and extracts CPython through PowerShell. | Remove. |
| `src/gupkg/bootstrap.py:104-158` | Rewrites `._pth`. `_ensure_pip` downloads and runs unpinned `get-pip.py`. | Remove. The build writes `._pth`. The `--embedded` flag disappears. |
| `src/gupkg/dependencies.py` | `ensure_runtime_dependencies` (called from `cli.py:325, 404` before the TUIs) probes and pip-installs gupkg's own dependencies into `%LOCALAPPDATA%\gupkg\embedded\site-packages` or the bundled `site-packages`. `run_with_missing_dependencies` (used by `updates.py:51`) retries hooks after installing their missing imports. | Delete the module. The TUIs import Textual directly. Hooks run once, and an `ImportError` fails the hook with its module name. |
| `--allow-hook-dependency-install` (`cli.py:83, 345, 383, 421-443`, `commands.py:141-153`) | Opt-in for hook dependency installs. | Remove the option and its plumbing. Hooks may import only the standard library, what the bundle ships, and modules in their own `pkg.local\`. |
| Elevated-run trust check in `dependencies.py:205-230` | Before importing in an elevated run, confirms that the bundled `site-packages` is admin-only. | Keep the guarantee and move it. The elevated launch path checks the whole active version directory once with `require_trusted_directory` (`elevation.py`), which already covers `python\`. |
| `src/gupkg/locked_deps.py`, `cli.py:272-279, 364`, `commands.py:1068-1110` | `gupkg manager self repair-deps` / `update-deps`. | Move the logic to `tools/` (see below). Remove both commands. |
| `src/gupkg/dependency_notices.py`, `commands.py:1113-1125`, `cli.py:332, 393, 408` | Queries PyPI on start and prints "update-deps available" notices. | Remove. Dependency freshness is a maintainer concern (see `tools/check_deps.py`). |
| `gupkg-bootstrap.ps1:94-140` (`Install-BundledRuntime`) | Downloads CPython and the pip wheel, then installs the lock. | Remove. A payload without `gupkg\python\python.exe` is rejected. |
| `src/gupkg/pkg.toml` | `ignore_checksum = true` for self-update. | Remove. Self-update is verified (see below). |

## Dev tree (new or moved)

All of these run from a checkout and use the committed runtime
(`src/gupkg/python/`) or a developer's own Python. None of them is copied into
the bundle.

- `tools/fetch_runtime.py` (`scripts\fetch-runtime.cmd`): downloads the
  pinned embeddable CPython and checks it against the hash pinned in this
  script, the only place the URL and hash live. Writes `._pth`, installs pip
  from the pinned wheel, installs `gupkg-deps.lock` with `--require-hashes
  --only-binary=:all: --no-deps`. Replaces the three copies in `gupkg.cmd`,
  `gupkg-bootstrap.ps1` and `scripts\build-standalone.cmd`.
- `tools/update_deps.py`: today's `update_deps` / `repair_deps` logic from
  `locked_deps.py`. It resolves newer releases, shows the plan, rewrites
  `gupkg-deps.lock` and refreshes the committed runtime's `site-packages`.
  No elevation checks are needed, since it only touches the checkout.
- `tools/check_deps.py`: the PyPI comparison from `dependency_notices.py`, for
  maintainers.
- `gupkg-deps.lock` moves to the repo root or `tools/`. It is a build input,
  not part of the shipped package.

## Integrity: computed at build, checked at install

### Build (`tools/build_standalone.py`)

1. Verify the inputs: the CPython archive against the pinned hash in
   `fetch_runtime.py`, and every wheel through `--require-hashes`. A mismatch
   stops the build.
2. Assemble the version-directory tree.
3. Write `gupkg\MANIFEST.sha256`: one line `<sha256>  <relative path>` for
   every file in the tree except the manifest itself, sorted by path. The
   bundle's integrity is defined here, once.
4. Zip the tree as `gupkg-<version>-win-amd64.zip` and print its SHA-256.

### Release (`tools/release.py`)

- Build, upload the ZIP as the asset of GitHub release `v<version>`
  (`gh release create`), and add the ZIP's SHA-256 to `RELEASES.md`:
  `| version | commit | date | sha256 | notes |`.
- Refuse to release if the working tree is dirty or the ZIP was built from
  another commit (the commit is recorded in the ZIP as `gupkg\RELEASE.txt`).

### Install, self-update and repair (user-facing, check only)

- **Bootstrap** (`gupkg-bootstrap.ps1`): the default source is the asset URL
  from `RELEASES.md`, checked against the `sha256` in the same row. A `-Source`
  ZIP needs `-Sha256` or prints the existing warning. After extraction, check
  every file against `MANIFEST.sha256` and reject extra or missing files.
- **Self-update** (`src/pkg.local/check_update.py`, `unpack_app.py`): the
  candidate carries the asset URL and `sha256`, so gupkg's normal download
  verification applies. `unpack_app` checks the extracted tree against
  `MANIFEST.sha256` before the version directory is committed. The runtime
  comes from the new ZIP and is never carried over from the previous version.
- **`gupkg manager self status` / `manager doctor`**: check the active
  version against its `MANIFEST.sha256` and list modified, missing or extra
  files. Report only: the fix is reinstalling from the ZIP.

How `MANIFEST.sha256` itself is protected: the ZIP digest in `RELEASES.md`
covers it at download time. After installation it relies on the folder ACLs
the system-scope bootstrap already sets (admin-only write). Signing the
manifest is out of scope.

## Launcher after the change

```bat
set "GUPKG_PYTHON=%~dp0python\python.exe"
if not exist "%GUPKG_PYTHON%" (
    >&2 echo [gupkg] The bundled Python is missing: %GUPKG_PYTHON%
    >&2 echo [gupkg] Reinstall gupkg from a release ZIP.
    exit /b 1
)
"%GUPKG_PYTHON%" -I -s -B "%~dp0bootstrap.py" %GUPKG_FORWARD_ARGS%
```

The elevated/ordinary split in `gupkg.cmd` collapses, because both branches
now do the same thing. `fltmc` remains only if `bootstrap.py` still needs
`--elevated`.

## Repository

`src/gupkg/python/` stays committed for now, so a checkout runs without a
build step. Once two releases have shipped from the new pipeline, decide
whether `tools/fetch_runtime.py` should replace the committed copy.

`src/gupkg/digest.txt` is a 15,000-line source dump (gitingest output), not
integrity data. Remove it from the package and `.gitignore` it, so it does not
end up in `MANIFEST.sha256`.

## Tests

Behavior to protect:

- `gupkg.cmd` with no `python\python.exe` exits 1 with the reinstall message.
  With a `python.exe` on `PATH` and `GUPKG_PYTHON` set, neither is used.
- A gupkg run creates nothing under `%LOCALAPPDATA%\gupkg` and starts no
  `pip` process (monkeypatch `subprocess` to fail on any `pip` invocation).
- A hook importing a module that is not available fails the update with a
  message naming the module. The active version is unchanged, and nothing is
  installed.
- `gupkg manager self repair-deps` and `update-deps` are unknown commands, and
  `--allow-hook-dependency-install` is an unknown option.
- `build_standalone.py --skip-deps` on a fixture runtime writes a ZIP whose
  `MANIFEST.sha256` lists every file with the correct digest.
- `check_update` parses a `RELEASES.md` row with `sha256` and returns the
  asset URL and digest. A row without a digest is an error.
- Self-update with a ZIP whose digest does not match, or whose tree does not
  match its manifest, fails and leaves the active version unchanged.
- `manager self status` on a version with one edited file reports that file.

## Acceptance checklist

- [ ] `grep` over shipped files (`src/`, `gupkg-bootstrap.*`) finds no
      `python.org/ftp`, `get-pip`, `pip install`, `pypi.org`, `py -3`,
      `GUPKG_PYTHON=` outside the launcher snippet above, `Install-BundledRuntime`,
      `gupkg-deps.lock`, or `allow_hook_dependency_install`.
- [ ] `src/gupkg/dependencies.py`, `locked_deps.py` and
      `dependency_notices.py` no longer exist in the shipped tree.
- [ ] `tools/release.py` publishes `gupkg-<version>-win-amd64.zip` and writes
      its SHA-256 to `RELEASES.md`.
- [ ] Fresh Windows VM, no Python, user scope: `gupkg-bootstrap.cmd` installs
      and `gupkg --version` works. The only downloads are `RELEASES.md` and the
      ZIP.
- [ ] Same VM, elevated, `-Scope system`: same result.
- [ ] `gupkg manager self update` installs the next release, verified, with
      its own `python\`.
- [ ] Editing one file in the installed version makes `manager self status`
      report it.
- [ ] README "Installing gupkg", `_docs/operations.md`,
      `_docs/troubleshooting.md` and `_docs/development_guide.md` describe one
      distribution, the reinstall fix, and the dev-tree tools.

## Integration with the 1.0 rehaul

Added 2026-10-22. See `_docs/rehaul_design.md` §20 and epic issue 020.

This issue is **stage 0** of the rehaul (decision D13). It lands before issue
021, needs no package-format change, and its decisions stand unchanged.
Points of contact:

- **ZIP layout: bundled mode (decisions D15–D17; issue 033).** The release
  ZIP is a *distribution* of the gupkg package:
  - `gupkg\<version>\pkg.toml`;
  - `gupkg\<version>\gupkg\…`, the bundled tree (`python\`, the launchers,
    `MANIFEST.sha256`, `VERSION`);
  - `gupkg\<version>\install.cmd` and `gupkg-manager.cmd`;
  - `gupkg\_check_updates\pkg.toml`.

  **In 1.0, installing the package copies the bundled tree into `App`**
  (`[source] local = "gupkg"`), and the installed gupkg runs from `App`. The
  same bundled-tree layout lets any other program carry gupkg and be
  installed without gupkg being installed.
  - In 0.18 the 0.17 engine still needs the tree at the version root and the
    `App\RELEASE.txt` marker. Issue 033 removes both.
  - Existing installs need no file move: the first 1.0 self-update creates a
    new version directory whose `App` is extracted from the ZIP.
  - The bootstrap script shrinks to: download, verify against `RELEASES.md`,
    extract, run `install.cmd`.
- **Self-update hooks are interim.** The `src\pkg.local\check_update.py` and
  `unpack_app.py` described above are the 0.18 implementation. In 1.0,
  issue 032 replaces them with a format 2 `_check_updates` tracker
  (decision D14), using:
  - `[track] provider = "github"`;
  - `[source.checksum] page_match` on `RELEASES.md`;
  - `[source] local = "gupkg"` for bootstrap, a `url` for self-update, and
    `verify_manifest = "MANIFEST.sha256"`, a new general unpack service from
    issue 026.

  `self update` then becomes `gupkg check gupkg` followed by
  `gupkg update gupkg` (decision D11).
- **Command names.** `gupkg manager self status|repair|update` becomes
  `gupkg self status|repair|check|update` in issue 029. The commands that
  this issue removes (`repair-deps`, `update-deps`) are not reintroduced.
- **Manifest checks.** "`manager self status` / `manager doctor` check
  `MANIFEST.sha256`" becomes `gupkg self status` and `gupkg doctor` in 1.0.
  Open question 4 of the design proposes recording an `App` manifest for
  every package.
- **Trust model.** No runtime pip and no hook dependency installs is assumed
  by §8.6, §9.3 and §9.4 of the design. `gupkg-manager` (issue 030) imports
  Textual from the bundle directly.
- **Bootstrap script.** `gupkg-bootstrap.ps1` writes settings schema 3 and
  runs `gupkg settings init`, not `manager init`, from issue 032 on.
- **Documentation.** The README and `_docs` text written here is the base
  that issue 032 rewrites for 1.0.
