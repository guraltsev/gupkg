# T18: `gupkg.cmd` forwards its arguments to Python unchanged

Added on 2026-10-29 after the owner asked why `gupkg.cmd` splits arguments ("it should just forward them as is").
Found by T16: `--overwrite=orphaned-bin-files,orphaned-shortcuts` reaches Python as `--overwrite orphaned-bin-files
orphaned-shortcuts`; `--skip-checksum=cli` becomes `--skip-checksum` plus a target `cli` (pre-existing since before
job 09). T16 worked around the first one in `cli._normalise`.

## Cause (read, not yet reproduced)

`src/gupkg/gupkg.cmd` collects the arguments in a loop (`set "GUPKG_FORWARD_ARGS=%GUPKG_FORWARD_ARGS% "%~1""`, `shift`).
A batch file splits `%1` at spaces, `=`, `,` and `;` unless the caller quoted the whole argument, so the loop rebuilds the
command line from the pieces. The comment in the file says the loop exists because "cmd.exe /c quoting can leave a
leading quote on forwarded arguments when this launcher is started by the native shim"; normalising each argument
through `%~1` removes it. So the split is a side effect of a quoting workaround.

## Goal

Python receives exactly the arguments the caller passed: `=`, `,` and `;` inside an argument are kept, an empty
argument stays an empty argument, spaces and quotes survive. The native shim path (`gupkg.exe` / `gupkg-tui.exe` ... that
start the `.cmd` with `cmd /c`) keeps working. No argument becomes more dangerous than it is today (this launcher also
runs elevated).

## Steps

1. **Reproduce first** (AGENTS rule 1), with real `cmd.exe` in a temp folder: a fake version directory with
   the real bundled `python\python.exe` and a stub `bootstrap.py` that prints `json.dumps(sys.argv[1:])`; run `gupkg.cmd`
   with the arguments below and show what arrives today. Also reproduce the problem the loop was added for: start the
   launcher through the native shim (read `src/gupkg/shim/README-exe-shim.md` and `git log -S"Normalize every argument"
   -- src/gupkg/gupkg.cmd` for the history) and show the stray leading quote with a plain `%*` forward.
2. **Choose the forwarding** that passes every case below through unchanged AND works through the native shim. Likely
   `%*` (the raw text) with quoting handled where the shim builds its command line; read the shim source (Rust/C?) and the
   `.config.toml` `forward_arguments` handling. If the only way to make the shim path work is to change the shim, say so:
   do NOT rebuild or change the native shim binaries in this task: stop and report the options (the shim binaries are
   committed artefacts).
3. **Cases** (each must arrive unchanged): `--overwrite=a,b`, `--skip-checksum=cli`, `--root=C:\a b\c`, `--x=1;2`, an
   empty argument `""`, an argument with spaces, an argument ending in a backslash inside quotes, non-ASCII text, a
   100-character argument, `-- target`. And hostile arguments, compared with TODAY's behaviour (record it first): `"a&b"`,
   `"a|b"`, `"a>b"`, `a^b`, `"%PATH%"`, `!VAR!`, `"a""b"`. For each: arrives literally, or is refused as today; it must
   never execute a command and never be less safe than today (a canary file that a successful injection would create must
   never exist).
4. **Remove the T16 workaround** `cli._normalise` kind-joining for `--overwrite` once the arguments arrive intact (keep
   what direct runs need: `--overwrite` without a value, unknown kinds, empty items and duplicates are usage errors
   listing the kinds; an empty item `--overwrite=a,,b` now reaches Python and must be reported). Fix `--skip-checksum=cli`
   through `gupkg.cmd`: add a test.
5. **Tests** with real `cmd.exe` (see `tests/test_package_launcher.py` for how launchers are tested): every case above,
   through `gupkg.cmd` directly and through the native shim when the shim can be started in a test (otherwise a manual
   check `035-T18-1`). Each must fail on `main` where the case is broken today.
6. **Docs:** the RELEASES.md "Known limit" sentence of 1.1.1 stays as history; add no new release notes (no release in this
   task). Fix the docs/help that mention the limit (grep README.md, `_docs`, the `--overwrite` help text, the T16 log).
7. The elevated launcher chain (`package-launcher.cmd`, T11) and `gupkg-bootstrap.ps1` are not changed unless they forward
   arguments through the same pattern: grep for `%~1` / `shift` loops in `src/gupkg/*.cmd`, `src/gupkg/launchers/*.cmd`
   and `src/*.cmd`, list them, and fix the ones that split the same way (same test approach).

## Done when

- [ ] The reproduction (today's output) and the new output for every case are in the log.
- [ ] The tests above pass and fail on `main` where applicable.
- [ ] Hostile arguments: no worse than today, canary never created (log shows before/after).
- [ ] The native shim path works (a test or the manual check `035-T18-1`, with the steps run as far as possible).
- [ ] The full suite passes; `git diff --check main...HEAD` is clean (CRLF is by design for `.cmd`; `.gitattributes` decides).

## Do not

- Rebuild or change the native shim binaries.
- Bump the version, release, or push.
- Touch the owner's roots or the real machine.
