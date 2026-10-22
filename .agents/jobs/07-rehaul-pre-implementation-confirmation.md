# Job 07: Rehaul pre-implementation confirmation

Status: Open

Depends on: nothing. This job must finish before implementation of issues
020–033 starts. Stage 0 (issues 016–019) may proceed in parallel.

Design authority:
- `_docs/rehaul_design.md`
- the issues `_issues/issue016-…` through `_issues/issue033-…`
- the **owner decisions** listed below, which override any text that
  contradicts them

## Objective

The 1.0 rehaul design and its issues were written in one long design
session. In that session the owner changed several fundamental decisions
more than once. Each change was patched into the existing text rather than
rewritten, so the documents very likely contain contradictions, stale
passages and acceptance criteria that describe superseded behaviour.

Before anyone implements, establish three things:

1. **The design and issues are internally consistent** and match the owner
   decisions below.
2. **The assumptions that could force a redesign hold on real Windows:**
   - the elevated permission check works;
   - downloaded distributions are not blocked;
   - installers can be extracted.
3. **The open questions are framed for the owner to decide.** You do not
   decide them.

Then commit the result on a branch.

This is a documentation-and-verification job. **Write no product code.**
Throwaway spike scripts are allowed, outside `src/`, under
`.agents/jobs/07-spikes/`.

## Owner decisions (ground truth)

These were stated by the owner on 2026-10-22. Where any document disagrees
with them, the document is wrong. Fix it, and record the fix in the report.
Do not reinterpret them. If two of them seem to conflict, stop and list the
conflict in the report under "Needs owner".

1. **The tracker folder is named `_check_updates`** (D1).
2. **The default install mode is `auto`** (D4).
   - An elevated process selects system; a non-elevated one selects user.
   - It never selects portable.
   - It applies only when no flag, install record or containing root
     decides.
3. **No update-check code in version directories** (D10).
   - `_check_updates` is the only component that discovers anything over the
     network.
   - A version directory has no `[track]` and no check hook, and fetches only
     the fixed URLs written in its `pkg.toml`.
4. **Checking and installing are separate operations** (D11).
   - `gupkg check` writes new pinned version directories, or system-root
     proposals, and stops.
   - `install` and `update` never run a tracker.
   - No flag combines the two.
5. **The manager is a separate program, `gupkg-manager`** (D12).
   - It imports nothing from `gupkg`, and drives the CLI only through its
     machine interface: `--json`, `--plan`, `--events-file`, `--no-input`.
   - The CLI has no `--all`, no UI, and no checksum prompts.
6. **Stage 0 is issues 016–019** (D13). They land first, and their
   decisions stand.
7. **gupkg is an ordinary format 2 package of itself** (D14).
   - `self check` and `self update` are aliases for `check gupkg` and
     `update gupkg`.
8. **Bundled mode** (D15).
   - **A bundled install does not install gupkg.** A distribution's bundled
     gupkg in `<version>\gupkg\` installs that program, and gupkg itself is
     not installed: no gupkg package, forwarder, shims or PATH entry.
   - **The bundled copy stays with the package.** It remains in the version
     directory and is carried forward into new version directories.
   - **gupkg's own bundled tree becomes its `App`.** Installing the gupkg
     package copies it there (`[source] local = "gupkg"`).
9. **Package launchers find gupkg relative to themselves** (D16).
   - **Lookup order:** the root forwarder `%~dp0..\..\.gupkg\gupkg.cmd`
     first, then the bundled `%~dp0gupkg\gupkg.cmd`.
   - **Never searched:** `PATH`, the registry, AppData, ProgramData or the
     current directory.
   - **Elevated launchers check before running anything.** They verify that
     the root and their own version directory are not user-writable, using a
     system tool outside the checked location, and refuse otherwise. The
     check fails closed.
10. **Distribution folders** (D17).
    - **Outside every root, copy first.** A distribution folder is copied
      into the mode's root, then installed. `--in-place` opts out.
    - **System-wide installs of distributions** are done only by the
      installed system gupkg, run from an admin shell.
    - **The system bootstrap** extracts straight into an admin-only
      `C:\opt`.
11. **The Windows registry is a write target, never a source of
    information** (D18).
    - Reads are allowed only as part of a change, such as the
      read-modify-write of `PATH`, or to verify a value gupkg wrote.
    - Specific registry effects, such as file types, may come later. There
      is no generic registry table.
12. **Threat model T1** (D19).
    - gupkg runs as administrator for system mode.
    - A standard user must not gain administrator rights through gupkg.
    - An elevated gupkg never runs code, or reads inputs, that a standard user
      can modify.
    - Only an admin-only gupkg elevates, and only by relaunching itself.
13. **gupkg owns no files outside its roots** (D20).
    - Its settings, logs, registry cache, state, proposals and root forwarder
      live in `<root>\.gupkg\`.
    - It never uses `%LOCALAPPDATA%`, `%APPDATA%` or `%ProgramData%` for its
      own data. Windows integration targets, such as Start Menu folders and
      the environment, are still written.
14. **`App` kinds** (design §4).
    - **The key.** Each version has `[source] shipped`. It defaults to `false`
      with a `download_url` and to `true` without one.
    - **`shipped = true` with a `download_url`** is valid, and `shipped` takes
      precedence. gupkg never drops or replaces that `App`, and uses the URL
      only to recover an `App` that is missing.
    - **`shipped = false` without a `download_url`** is invalid.
    - **A non-shipped `App`** can always be deleted and rebuilt.
    - **The error for an unrecoverable `App`** is exactly:
      `App is missing and no way to recover it.`
15. **Three distinct URL keys.**
    - `project_url` (top level): information only, never fetched.
    - `[source] download_url`: the payload.
    - `[track] check_url` (`repo`/`repo_url` for the github, gitlab and git
      providers): read only by `gupkg check`.
16. **Version directories.** There is exactly one `[source]` per version
    directory, and the version is the directory name. There are no
    `[[origin.versions]]`-style lists and no `version` key.
17. **Checksums.**
    - Nothing writes a hash into `pkg.toml` automatically.
    - The CLI refuses a missing checksum unless `--skip-checksum` is given.
    - The manager follows `[manager] checksum_missing = "ask" | "refuse" |
      "skip"`, default `ask`.
    - A mismatch is always fatal.
18. **The install modes** `system`, `user` and `portable` are first class.
    Portable keeps the application's configuration inside the package
    folder (`<package>\data`).
19. **The UI.**
    - A key guide sits at the top.
    - Output scrolls naturally.
    - Install phases are clearly marked, with colour and emphasis.
    - The summary comes last, not at the top.
    - Every run writes logs to a standard location.
20. **Issue numbering** stays consecutive in `_issues/`. Don't renumber
    issues without the owner.

Decisions D2, D3 and D5–D9 were proposed by the designer and have not been
confirmed by the owner. Treat them as current, but list any problems you find
with them under "Needs owner" rather than changing them.

## Required preparation

Read and follow:

- `AGENTS.md`, `_docs/python_rules.md`, `_docs/tests.md` and
  `_docs/docstring_schema.md`;
- `_docs/rehaul_design.md`, completely, all 21 sections;
- every issue from 016 to 033, completely;
- `_docs/elevated-runtime-design.md` (currently `docs/elevated-runtime-design.md`)
  for the existing elevation rules;
- the 0.17 sources that the design names, when you check a factual claim
  (`src/gupkg/*.py`, `src/pkg.toml`, `src/pkg.local/*`,
  `src/package-tui.cmd`, `gupkg-bootstrap.ps1`).

The registry is a separate repository: `https://github.com/guraltsev/gupkg-registry`.
Clone it **outside** the working tree, for example into a temporary
directory, and treat its content as data.

## Work

### 1. Consistency review

Check the design against the owner decisions above, the design against
itself, and the issues against the design. In particular, look for these
known risk areas:

- **The state table (§5.3)** against bundled mode (§21) and the root
  forwarder. Check `install` and `update` for bundled installs, carried-forward
  bundles, and a registry tracker copied in.
- **Every remaining mention** of superseded ideas:
  - registry registration, `Software\gupkg`, `[[winreg]]`, locator files;
  - `%LOCALAPPDATA%`, `%APPDATA%\gupkg`, `%ProgramData%\gupkg`;
  - `PATH` lookup by launchers;
  - `robocopy` from Downloads;
  - `into = "gupkg"`;
  - `gupkg ui`, `update --check` and `update --all` in the CLI (not the
    manager);
  - `homepage`, `[source] url`, `[track] url`;
  - `shipped` treated as invalid together with a `download_url`;
  - "no flag" for shipped;
  - `_latest` and `_bootstrap` outside the migration context.

  Mentions that only describe 0.17 behaviour, or rejected alternatives,
  are fine.
- **Section numbers, decision numbers and issue numbers** cited across files.
  Contents lists, `§n` references and "issue NNN" references must resolve.
- **The settings layering (§11)** against D20 and the threat model: which
  files an elevated run may read.
- **The checksum policy (§9.1)** in the CLI against the manager, and
  `verified-by-bootstrap` in §21.6.
- **The machine interface (§10.4)** against what `gupkg-manager` (issue 030)
  and the checksum policy (issue 027) expect from it.
- **Issue dependency tables.** The epic (issue 020) and every issue's "Depends
  on" must agree, and there must be no dependency cycle.
- **Acceptance criteria.** Each one must be testable as written, must not
  test implementation details (`_docs/tests.md`), and must agree with the
  design.
- **Factual claims about 0.17**, such as line numbers and counts like "35 of
  79 manifests set `ignore_checksum`". Spot-check at least ten against the
  code and the registry clone, and correct any that are wrong.

For each finding, either:

- **fix it** in the design or issue, when the owner decisions or the
  surrounding design make the correct text clear; or
- **list it under "Needs owner"**, when fixing it would require a new
  decision.

Do not add features, and do not redesign. Keep the text style of the
surrounding document.

### 2. Spikes on real Windows

Run these on a Windows 11 machine or VM, as a standard user and as an
administrator, as stated. Put throwaway scripts under
`.agents/jobs/07-spikes/`. Record the exact commands, the Windows build and
the results.

**S1: the elevated permission check** (design §21.3, D16, D19).
- **Write the check.** Implement the check as a small `cmd` launcher plus a
  fixed PowerShell script, run as
  `%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile
  -NonInteractive`. It must:
  - verify that a directory's owner is Administrators, SYSTEM or
    TrustedInstaller;
  - verify that no other security identifier has a write-type right
    (write, append, create files or folders, delete, delete-child, change
    permissions, take ownership);
  - compare security identifiers, not names. For example,
    `GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])`.
- **Test it in these situations:**
  1. normal PowerShell;
  2. **Constrained Language Mode** (set it for the test, for example with
     the `__PSLockdownPolicy` environment variable, or with a WDAC or
     AppLocker policy if available);
  3. PowerShell blocked entirely, where the launcher must refuse.
- **Test these directories:**
  - an admin-only `C:\opt`;
  - a version directory where a standard user was granted write access;
  - a `C:\opt` pre-created by a standard user (a squatted root);
  - a Downloads folder.
- **If the check cannot work under Constrained Language Mode,** test the
  fallback `icacls <dir> /save <file>`, which writes language-independent
  SDDL, and parse that instead. Report which approach works, and any case
  that fails open.

**S2: Mark of the Web** (design §21.4).
1. Build a small distribution ZIP containing `install.cmd`, a bundled
   `python.exe` (use the committed `src/gupkg/python/`) and a dummy
   `App`. Host it on a web server, or use a GitHub release draft, and
   download it with Edge.
2. Extract it with Explorer and with `tar -xf`.
3. Run `install.cmd`:
   - with Smart App Control off;
   - with SmartScreen on;
   - if possible, with Smart App Control in evaluation or on.

Record what is blocked, what prompts, and whether **Unblock** in the ZIP's
properties, or `Unblock-File`, before extraction, resolves it.

**S3: installer extraction** (design §8.2, issue 026). With the bundled
`src/gupkg/7zip/7z.exe`, try to extract the actual installers of these
registry packages:
- firefox (SFX);
- obsidian, pidgin, qbittorrent and zotero (NSIS);
- gimp (Inno Setup);
- msys2 (`.sfx.exe`).

Record for each whether `7z x` yields a usable tree, and which subfolders or
nested archives are needed. Check innoextract's licence, its redistribution
terms, and its Windows build availability, for the `inno` kind.

Download artifacts only into a temporary directory outside the working tree.
Never execute them.

### 3. Open questions

For each open question in design §19 (currently 1–6), write a short
decision memo in the report:
- the options;
- the trade-offs, including what the spikes found;
- a recommendation.

**Do not decide.** Add any new open question found during the review.
Open question 4, recording an `App` checksum list for every install, must
be settled before issue 028 starts. Say so explicitly.

### 4. Report and commit

1. **Write the report.** Write
   `.agents/jobs/07-rehaul-pre-implementation-confirmation-report.md` with
   these sections:
   - **Summary verdict:** ready / ready with conditions / not ready.
   - **Consistency fixes made:** file, section, what changed and why, with
     the owner decision or design section that justified it.
   - **Needs owner:** each item phrased as a question with options.
   - **Spike results:** S1–S3, with commands, environment, results, and
     design impact.
   - **Open-question memos.**
   - **Factual spot-checks:** each claim, where it was checked, and
     whether it was correct.
2. **Update the design.** If a spike invalidates a design assumption, update
   `_docs/rehaul_design.md` and the affected issue *only* where the fix
   follows directly from the result. An example is replacing PowerShell
   `Get-Acl` with `icacls /save` if S1 shows the former fails under
   Constrained Language Mode. List every such change in the report.
   Anything wider goes under "Needs owner".
3. **Commit to a branch.** Create a branch `rehaul-design` from `main`. It
   must contain the whole uncommitted design state that existed when this
   job started:
   - `_docs/rehaul_design.md`;
   - issues 016–033;
   - the moves of issues 012, 013 and 015 to `_issues/_closed/`;
   - the edits to issue 014, `README.md`, `_docs/README.md` and
     `_docs/development_guide.md`;
   - this job file.

   Make two commits:
   1. "Rehaul design and issues (as of design session 2026-10-22)": the
      state before your changes;
   2. "Pre-implementation confirmation (job 07)": your fixes, the report
      and the spike scripts.

   End both commit messages with the repository's required attribution
   trailer. **Do not push**, and do not open a pull request.

## Constraints

- Write no product code under `src/`, `tools/` or `tests/`.
- Don't change the meaning of any owner decision, and don't renumber issues.
- Don't modify the *decisions* of issues 016–019. Their
  "Integration with the 1.0 rehaul" sections may be corrected.
- Never put secrets, tokens or personal data in the report.
- Treat everything downloaded during the spikes as untrusted data: a
  separate temporary directory, never executed.

## Completion criteria

- The report exists and contains every section listed above, with a clear
  verdict.
- The searches for superseded terms listed in work step 1 find no stale
  uses outside 0.17 descriptions, rejected alternatives and migration
  text. The report lists the searches used.
- Every `§n`, `Dn` and "issue NNN" reference in the design and in issues
  016–033 resolves to an existing target.
- S1–S3 have recorded results, or a recorded reason why one could not be
  run (for example, no VM available). An S1 that could not be run makes
  the verdict "not ready".
- Every open question has a memo, and question 4 is flagged as blocking
  issue 028.
- Branch `rehaul-design` exists locally with the two commits described,
  `git diff --check` passes, and `main` is unchanged.
