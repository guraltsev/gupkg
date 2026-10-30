# T18b: the gupkg shims start the bundled Python, not a `.cmd`

Added 2026-10-30. Owner: "the shim should definitely not shim the cmd but the python call." Follows T18 part 1.

## Cause (reproduced by the T18 reviewer; the shim source was read by the controller)

`src/pkg.toml` declares the shims `gupkg`, `gupkg-tui`, `gupkg-manager` with `target = "$App\\gupkg.cmd"` etc. The native
shim (`C:\Users\guraltsev\Documents\devel\exe-shim\src\shim.cpp`, read only, do not edit it) quotes every argument
correctly and calls `CreateProcessW` on the target. For a `.cmd` target Windows itself runs `cmd.exe /c "<target>" "a"
"b"`, and cmd strips the outer quotes, so the batch file fails to start with any argument. The shim is not wrong; a
`.cmd` target is. A `.exe` target receives its arguments exactly.

## Goal

The three shims run `$App\python\python.exe` with fixed `[[argument]]`s (`-I -s -B -S $App\bootstrap.py`, and for tui /
manager `--gupkg-program=tui` / `--gupkg-program=manager` as the launchers do) and forward the caller's arguments
unchanged. Same flags as `gupkg.cmd`; read `gupkg.cmd`, `gupkg-tui.cmd`, `gupkg-manager.cmd` and keep every behaviour
that matters (a missing bundled Python gives a clear message today: say what the shim does instead and whether that is
acceptable).

## Steps

1. Reproduce first (AGENTS rule 1): in a temp folder (never the owner's roots) build a fake App with the real bundled
   `python\python.exe` and a stub `bootstrap.py` that prints `json.dumps(sys.argv[1:])`; copy the bundled `shim-console.exe`
   as `gupkg.exe` with the config of today (target `.cmd`) and show it failing with an argument; then with the new config
   show 15 arguments arriving exactly (`--overwrite=a,b`, `--x=1;2`, `""`, spaces, trailing backslash in quotes,
   non-ASCII, `"a&b"`, `"a|b"`, `a^b`, `"%PATH%"`, `!VAR!`, `"a""b"`, 100 chars, `-- target`) and exit codes propagating.
2. Change `src/pkg.toml` shim tables (check the `[[shim]]` schema: `arguments`, `forward_args`, `type`; `$App` expansion
   in arguments). Check every place that names the shim target: `launchers_find_gupkg` and the elevated launcher check
   (`engine/effects.py`, `launchers/package-launcher.cmd`), `selfmgmt` (`self repair` / `self status` must rewrite or
   report old shims whose config still points to a `.cmd`), `cleanup`, `engine/health.py` drift detection (an installed
   1.1.1 shim with the `.cmd` target must be reported as drift and fixed by `gupkg self repair`), and docs
   (`src/gupkg/shim/README-exe-shim.md` copy, README, `_docs`).
3. Elevated path: the elevated launcher's step-3 check must still verify the shim folder is admin-only; a shim that
   starts `python.exe` is checked the same way. Do not weaken it.
4. Tests (each fails on `main`, passes after): config text of the three shims; the real-shim argument test through
   `cmd.exe`-free execution above (skip with a clear reason if the bundled shim cannot run in the test environment);
   drift/repair of an old-style shim config; no remaining `.cmd` shim target.
5. Manual check `035-T18-2` in `tests/manual_smoke.md` for the owner: install into a test root, run `gupkg <hostile
   args>` from PowerShell and cmd, and `gupkg self repair` from a root installed with 1.1.1.

## Done when

- [ ] Reproduction output (before / after) in the log, including exit codes.
- [ ] The tests above pass and fail on `main`; the full suite passes; `git diff --check main...HEAD` is clean.
- [ ] Old 1.1.1 installs: what the user sees and the one command that repairs it, shown.
- [ ] Inconsistencies found are listed (AGENTS rule 13), not fixed unless the task covers them.

## Do not

- Edit the shim source repo or rebuild/replace the shim binaries.
- Touch `package-launcher.cmd`'s argument loop or the registry (decision pending), bump, release, push.
- Touch the owner's roots, `C:\opt`, `C:\bin`, the real PATH or Start Menu.
