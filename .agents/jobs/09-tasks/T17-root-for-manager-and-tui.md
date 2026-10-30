# T17: `gupkg-manager` and `gupkg-tui` take `--root`

Added by the owner on 2026-10-29, after the release of 1.1.0 (answer: "Should the manager and TUI take `--root`? Yes").
Today the two programs take only `--settings` and `--sandbox`, and `gupkg --root DIR manager|tui` is refused (the
pass-through commands refuse `--settings`, `--root` and `--sandbox`). A user whose root is not the default has to write a
settings file just to open the manager on it.

## Goal

`--root DIR` names the root of the run for the manager and the TUI, with the same meaning as for every other command
(issue 035 §4.1: row 1 of the root chain, system mode needs an admin-only root).

## Rules

1. **Programs:** `gupkg-manager --root DIR` and `gupkg-tui --root DIR` (the entry points in `gupkg_manager/cli.py` and
   `gupkg_tui/cli.py`, and `gupkg_ui/startup.py`'s `build_environment`, which already builds the scope with
   `build_scope(root_option=...)`: read it first). The option is documented in their `--help`.
2. **Through `gupkg`:** `gupkg --root DIR manager` and `gupkg --root DIR tui` pass the option on to the program. `--settings`
   and `--sandbox` before `manager`/`tui` stay refused (they belong to the program: `gupkg-manager --settings PATH`), with
   the existing message, but the message must no longer mention `--root` as refused.
3. **Errors are visible:** a root that does not exist, is not a folder, is user-writable while the run is elevated
   (system mode), or is refused by `build_scope`, stops at startup with the scope's own message and exit code 2 (the
   existing `StartupError` path); nothing is created in that root.
4. **The screen shows it:** the status line already starts with `user · <root>` / `system · <root>`; with `--root` it
   shows that root (shortened as today). Render the manager and the TUI at 80x24 with `--root` and paste the screens
   (hint line and status line) in your log.
5. **Docs:** README.md and `_docs/troubleshooting.md` (the rows that say the programs take no `--root`),
   `_docs/rehaul_design.md` (the bullet that says they take none; §6.1 chain), `_docs/operations.md`, `src/gupkg/README.md`.
   `tests/manual_smoke.md`: add `035-T17-1` (start the manager with `--root` on a test root; elevated with a user-writable
   root is refused).
6. No new options other than `--root`; no change to how the other commands treat `--root`.

## New tests (each must fail on `main`, then pass)

- [ ] `build_environment(... root_option=<tmp>)` (the programs' argument parsing) gives a scope whose root is that folder
      and whose `root_source` says `--root`.
- [ ] `gupkg-manager --root <tmp> --help` lists the option; the same for `gupkg-tui`.
- [ ] `gupkg --root <tmp> manager` and `gupkg --root <tmp> tui` reach the program with the option (use the existing test
      double for the launch in `tests/test_cli_grammar.py` / `tests/test_manager_real_cli.py`); `gupkg --settings X manager`
      and `gupkg --sandbox D tui` are still refused.
- [ ] An elevated `FakeSystem` run with `--root` on a user-writable folder fails at startup with exit code 2 and creates
      nothing.
- [ ] The rendered manager and TUI (Textual pilot) show the given root in the status line.

## Done when

- [ ] The tests above pass, and each fails on `main` (both outputs in the log).
- [ ] The docs no longer say the programs take no `--root` (grep README.md and `_docs` for "no `--root`", "takes no",
      "refused" near `tui`/`manager`).
- [ ] The rendered screens are in the log (AGENTS rule 8).
- [ ] The full suite passes; `git diff --check main...HEAD` is clean.

## Do not

- Touch the registry repository or the owner's roots.
- Change `--settings`/`--sandbox` handling.
