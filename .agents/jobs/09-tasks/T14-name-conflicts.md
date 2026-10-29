# T14: A package cannot take over another package's bin names or shortcuts

Added by the owner on 2026-10-29, during job 09. It is not part of issue 035. It matters most for the `X` and
`X-portable` pairs of T03, which declare the same shim and shortcut names. It runs after T11 and before T12 and T13, so
that the docs and the acceptance run cover it.

## Goal

A name a package puts into a root's `bin` folder, a Start Menu shortcut or an environment variable belongs to **one
installed package**. When an install, update, switch or repair of package A would create or replace a name that a
**different installed package B** already provides, the operation stops **before any download or file change**, with a
message that names both packages, the name, and what to do. A different *version* of the same package is not a
conflict: a package always owns its own names.

Today `apply_effect` (`engine/effects.py`) writes over whatever is there and nothing checks who owns it.

## Rules

1. **What counts as a name** (the recorded effects of the install record, kind and identity):
   - `shim`: the shim in `<root>\bin`, by its file name, compared case-insensitively;
   - `file`: a wrapper file in `bin`, by its path, compared case-insensitively;
   - `shortcut`: the `.lnk` path in the Start Menu folder, compared case-insensitively;
   - `env`: the variable name in its scope, compared case-insensitively.

   A `path` effect is **not** a conflict: several packages may add the same folder to `PATH`, and the existing
   reference counting for that stays as it is.
2. **Who owns a name:** the recorded effects of every other installed package **in this scope's root** (the install
   records). Nothing outside the run's root is read (D50).
3. **When it is checked:** in the planner, before any download, unpack, hook or file change, for every operation that
   installs or activates a version: `install`, `update`, `switch`, `repair`, and `check-updates` with download. It also
   covers **two targets of the same command** that provide the same name (for example `gupkg install copyq
   copyq-portable`): the plan is refused, and the message names both targets.
4. **Never refused by this rule:** `uninstall`, `check` and `cleanup`, so that a conflicting package can always be
   removed. A package that is already installed and conflicting keeps working.
5. **A new version that introduces a conflict** is refused like a new install: the installed version keeps working,
   and the package is reported as `outdated` with the refusal text as the reason it cannot update (the same shape as
   D55 in issue 035 §4.6).
6. **The message** has the form (one sentence, then the way out):
   `<A> cannot be installed: it would provide the <shim|shortcut|wrapper|environment variable> '<name>' (<path or
   scope>), which the installed package '<B>' already provides. Uninstall '<B>' first, or use a package definition
   with a different name.`
   For two targets of one command: `<A> and <B> both provide ... Install them one at a time only if one of them
   changes the name.` Use plain words; no codes. The error code in the machine documents is `name-conflict`, exit code 2.
7. **Files that no gupkg package owns** (a file in `bin` or a shortcut that exists but is in no install record) keep the
   behaviour they have today. Do not change it in this task; list in your report what happens today and what a
   follow-up could do (open question for the owner, not blocking).
8. **`check`** reports a conflict that is already on disk (two install records that claim the same name) as a problem
   of both packages, with the same naming. It changes nothing.
9. **The manager** shows the refusal like any CLI failure: on the run screen, with the message. Add one UI test that
   the text is on the screen (AGENTS rule 8).

## Where

- the planner (`engine/planner.py`) and the catalog's view of installed effects (`catalog/catalog.py`): build the map
  *(kind, identity) → package* from the install records of the scope, once per plan;
- the refusal is a `PlanError` (or the existing refusal type the planner uses) with the new code `name-conflict`;
- `engine/health.py` / `check` for rule 8;
- `_docs/rehaul_design.md`: one short subsection (name ownership) in §5 and the error code in §10.4; `README.md`:
  one sentence. No other doc edits (T12 sweeps the rest).

## New tests (each must fail on `main`, then pass)

- [ ] Package A is installed and provides shim `tool`. Installing package B (a different package) that also provides
      shim `tool` is refused with the message. The package folder of B, `bin` and the records are unchanged.
- [ ] The same for a shortcut, a wrapper `file`, and an `env` variable.
- [ ] Updating A to a new version that still provides `tool` is **not** refused. Reinstalling A with `--force` is
      not refused.
- [ ] `gupkg install a b` where both provide `tool` is refused before anything is created; the message names both.
- [ ] An update of A whose new version takes a name that B provides is refused; the installed A still works and is
      reported `outdated` with the refusal as the reason.
- [ ] `uninstall`, `check` and `cleanup` of a conflicting package are not refused.
- [ ] `check` reports two records that claim the same name.
- [ ] Case-insensitivity: `Tool.exe` against `tool.EXE`.
- [ ] The registry fixtures: installing `copyq` and then `copyq-portable` (or the fixture pair the worker finds in
      `tests/fixtures/registry_100`: add a `-portable` twin if none exists) is refused with the message.
- [ ] The manager's run screen shows the refusal (Textual pilot, `export_text`; paste the screen in your log).

## Done when

- [ ] The tests above pass, and each fails on `main` (give both outputs).
- [ ] `list --json` and `info --json` are unchanged (no new keys), except the new error code in failure documents.
- [ ] The full suite passes.
- [ ] Manual check `035-T14-1` in `tests/manual_smoke.md`: on a test root, install a package, then a second package
      with the same shim name; see the refusal and an unchanged root.

## Do not

- Change what happens to a file that no package owns (rule 7).
- Add an override flag. A conflict is resolved by uninstalling or by renaming in the definition.
- Edit the registry (`gupkg-registry`). Whether the `X`/`X-portable` pairs should get different names is the owner's
  decision; this task makes the collision a clean refusal.
