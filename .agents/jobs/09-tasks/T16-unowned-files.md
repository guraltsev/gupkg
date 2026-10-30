# T16: A file or shortcut that no package owns is not overwritten silently

Added by the owner on 2026-10-29, after the release of 1.1.0. It completes T14 (rule 7 of T14: files that no gupkg
package owns kept their old behaviour: overwritten without a word). The owner's decision: **raise a helpful message, or
have a flag to overwrite.** Both: the default is to stop with a message; a flag overwrites.

## Goal

When an install, update, switch or repair would write a shim, a wrapper file or a Start Menu shortcut to a path where a
file **already exists that no installed package of this root owns**, the operation stops **before any download or file
change**, and the message says which path, why, and what to do. A flag `--overwrite` makes it replace the file and say
so.

## Rules

1. **What is checked** (the same names as T14, in the run's own root only):
   - `shim`: `<bin>\<name>.exe` and its `<name>.config.toml`;
   - `file`: a wrapper file in `bin`;
   - `shortcut`: the `.lnk` path in the Start Menu folder.

   `env` and `path` effects are out of scope (they are not files).
2. **Unowned** means: the path exists, and no install record in this scope's root provides that name (the T14
   ownership map says "no owner"). A name that **another package owns** stays the T14 refusal (`name-conflict`); a name
   that **this package already owns** (update, reinstall, switch, repair of the same package) is never unowned.
3. **An existing file that already equals what would be written is not a conflict.** Compare: a shim `.exe` by its
   bytes (or SHA-256) with the bundled shim, a `.config.toml` and a wrapper by content, a shortcut by reading it back
   and comparing target, arguments, working directory, icon, description and the `run_as_admin` flag. Reinstalling a
   package whose record was lost then works without the flag.
4. **When it is checked:** in the planner, right after the T14 name check, before any download, unpack, hook or file
   change, for `install`, `update`, `switch`, `repair`, and for two targets of the same command (each is checked
   against disk; targets that share a name are already refused by T14). `uninstall`, `check` (without `--fix`) and
   `cleanup` are never refused by this rule. `check --fix` does not overwrite an unowned file: it reports the problem
   and names the command that overwrites (`gupkg install <name> --overwrite=orphaned-bin-files`), and changes nothing.
5. **The message** (one sentence, then the way out; plain words, no codes):
   `<A> cannot be installed: '<path>' already exists and no installed package owns it. Move or delete that file, or run
   the command again with --overwrite=orphaned-bin-files to replace it.`
   For several paths of one package, list them all in one message (at most 5, then "and N more"). The error code in the
   machine documents is `file-exists`, exit code 2 (same as `name-conflict`).
6. **`--overwrite=KINDS`** (owner's change, 2026-10-29: a bare `--overwrite` is too generic; the flag names what it
   replaces). `KINDS` is a comma-separated list, and the value is required (`--overwrite` alone is a usage error that
   lists the valid kinds). Valid kinds today:
   - `orphaned-bin-files`: an unowned shim `.exe`, shim `.config.toml` or wrapper file in `bin`;
   - `orphaned-shortcuts`: an unowned shortcut in the Start Menu folder.

   Example: `gupkg install tool --overwrite=orphaned-bin-files,orphaned-shortcuts`. An unknown kind, an empty item or a
   duplicate is a usage error (exit code 2) that lists the valid kinds. There is no `all`. Each kind only allows its own
   replacements: a refusal for a shortcut is not lifted by `--overwrite=orphaned-bin-files`. The refusal message names the
   exact value that would lift it (only the kinds that are needed, in one flag), for example `--overwrite=orphaned-bin-files`
   or `--overwrite=orphaned-bin-files,orphaned-shortcuts`. The option applies to `install`, `update`, `switch` and `repair` (look in `cli.py` which of these exist as
   commands; `check --fix` does not take it). With the right kinds, the unowned files are replaced as today, the package becomes
   their owner (the effect records them as it records any file it wrote), and the run prints one line per replaced file
   (`replaced <path> (no installed package owned it)`). It never overrides a `name-conflict` with another package.
7. **The manager:** it shows the refusal on the run screen like any CLI failure. The message names the CLI flag; the
   manager does not get an overwrite key in this task.
8. **No new keys** in `list --json` or `info --json`; the documents keep their shape (API stays 2). The new text goes
   into the existing error/message fields. The typed reader must still read the failure documents.

## Where

`engine/planner.py` (the name check of T14 and the plan), `engine/effects.py` (how the planner can ask "would this
write overwrite an unowned file", and the comparison helpers), `engine/lifecycle.py` (pass the flag), `cli.py` (the
option, its help text), `engine/health.py` (`check --fix` report), docs: `_docs/rehaul_design.md` (§5.5 name
ownership: one paragraph; §10.4 error code `file-exists` and the option), `README.md` (one sentence),
`_docs/troubleshooting.md` (one row), `tests/manual_smoke.md` (`035-T16-1`).

## New tests (each must fail on `main`, then pass)

- [ ] An unowned `bin\tool.exe` (a plain file) exists; installing a package whose shim is `tool` is refused with the
      message naming the path; the package folder, `bin`, shortcuts and records are unchanged and nothing was fetched.
- [ ] The same for a wrapper file, the shim's `.config.toml`, and a shortcut.
- [ ] With `--overwrite=orphaned-bin-files` (or `orphaned-shortcuts` for the shortcut case) the same install succeeds, the file is replaced, one `replaced ...` line is printed, and the
      package owns the names afterwards (a second package with the same name now gets the T14 `name-conflict`).
- [ ] An existing file identical to what would be written is not a conflict (no flag needed).
- [ ] Update, reinstall and switch of a package that owns its names are not refused.
- [ ] `--overwrite=...` does not override a `name-conflict` with another installed package; `--overwrite=orphaned-shortcuts` does not lift a bin-file refusal and the other way round; `--overwrite` without a value, an unknown kind, an empty item and a duplicate are usage errors (exit 2) that list the valid kinds; the refusal message names the exact value that would lift it.
- [ ] `uninstall`, `check` and `cleanup` are not refused; `check --fix` reports the problem and the flag, and changes
      nothing.
- [ ] Case-insensitivity: `Tool.exe` on disk against a shim `tool`.
- [ ] The manager's run screen shows the refusal (Textual pilot, `export_text`, 80x24; paste the screen with hint
      line and status line in your log).
- [ ] The machine documents: a failed install document carries `file-exists` and the typed reader reads it.

## Done when

- [ ] The tests above pass, and each fails on `main` (both outputs in the log).
- [ ] `list --json` and `info --json` are unchanged.
- [ ] The full suite passes; `git diff --check main...HEAD` is clean.
- [ ] Manual check `035-T16-1` in `tests/manual_smoke.md`: a test root, a hand-made `bin\<name>.exe`, an install that is
      refused (the message names `--overwrite=orphaned-bin-files`), then the install with that flag.

## Do not

- Change `env`/`path` effects or what `uninstall` removes.
- Add an interactive question: the choice is the flag.
- Touch the registry repository or the owner's roots.
