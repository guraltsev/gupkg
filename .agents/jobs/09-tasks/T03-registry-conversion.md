# T03: Convert every registry package (in `gupkg-registry`)

Issue 035: §2 (the registry numbers), §4.6 "Registry" and "copyq". Owner answers Q2, Q2a and Q2b.

## Where

The folder `gupkg-registry` inside the gupkg workspace is a **separate Git repository**:

- work and commit inside it (`git -C gupkg-registry ...`), on the branch `i035/T03` created from its `main`;
- never push;
- validate it with the gupkg `main` that has T02 merged.

## Goal

Every `pkg.toml` in the registry is valid under T02's rules, and nothing a user could install before is lost.

## Steps

1. **Write a conversion script** `gupkg-registry/_tools/convert_035.py`. For every folder `<name>/<version>/` and
   `<name>/_check_updates/` with a `pkg.toml`:
   - **`modes = ["system", "user", "portable"]` (28 packages):**
     - make the package **non-portable**: `modes = ["system", "user"]`. Remove `[portable]`. Move the `prepare` steps of
       `[user]`/`[system]` into the plain `[[prepare]]` list (append, once, keeping their order). Remove `[user]` and
       `[system]`;
     - create **`<name>-portable`**: copy the package folder (versions and `_check_updates`) to `<name>-portable/`. In
       each copy: `modes = ["portable"]`, keep `[portable]` with only `persist` and `markers`, remove `[user]` and
       `[system]` and their steps (the portable variant must not remove the vendor's portable switch);
   - **`modes = ["portable"]` (sqlitebrowser, SumatraPDF-portable, tixati):** remove `[portable]` keys other than
     `persist` and `markers`, if there are any. Nothing else changes;
   - **`modes = ["system", "user"]`:** no change.

   The script rewrites files with the same TOML writer the registry tool uses. It must be idempotent: running it twice
   changes nothing the second time.
2. **copyq is special** (Q2b; the evidence is in issue 035 §4.6 "copyq"). After the script, check that:
   - `copyq/*/pkg.toml`: `modes = ["system", "user"]`, `[[prepare]]` contains `["create", "unins000.exe"]`, no
     `[portable]`;
   - `copyq-portable/*/pkg.toml`: `modes = ["portable"]`, `[portable] persist = ["config"]`, **no** `unins000.exe`
     step;
   - the comment block in `copyq/_check_updates/pkg.toml` that explains CopyQ's storage rule is kept in both variants
     and updated to describe each variant.
3. **Names in comments:** the description of each `-portable` copy ends with ` (portable)`.
4. **Launchers:** regenerate the launchers of the new folders with the registry tool's `launchers` command (read
   `_tools/registry_tool.py --help`). Do not hand-write `.cmd` files.
5. **Fingerprints and `_registry.toml`:**
   - recompute the fingerprints of every changed version with the registry tool (read its help);
   - add the new `-portable` entries the same way;
   - **never** invent a hash. If the tool needs the network to compute something, stop and ask.
6. **`PORTABLE-UNVERIFIED.md`:**
   - rename each row to its `-portable` package;
   - remove the mention of `[nonportable]`;
   - add a row for `copyq-portable`, citing upstream `src/platform/win/winplatform.cpp` `portableFolder()`, with
     confidence "high for the selection rule; persist of `config` through a junction untested".
7. **Run the checks:**
   - the registry's own tests: `<gupkg>\.venv\Scripts\python -m pytest gupkg-registry\_tests -q`;
   - gupkg's validator on the whole registry (read `_tools/pkg_check.py` and `gupkg pkg check --help` for how; use the
     gupkg source tree on `main`, not the installed gupkg).

## Done when

- [ ] Every `pkg.toml` in the registry passes gupkg's validation (paste the summary line).
- [ ] The registry's `_tests` pass.
- [ ] `Select-String -Path gupkg-registry\*\*\pkg.toml -Pattern '^\[user\]','^\[system\]','"system", "user", "portable"'`
      finds nothing.
- [ ] There are 28 new `*-portable` packages. The count is in your log, with the list.
- [ ] Running `convert_035.py` a second time changes no file (`git -C gupkg-registry status` is clean after it).
- [ ] The copyq checks of step 2 hold.

## Do not

- Push the registry.
- Change any `download_url`, `sha256` or tracker setting.
- Edit gupkg's own source.
