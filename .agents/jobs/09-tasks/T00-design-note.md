# T00: Design note (documentation only)

## Goal

Make `_docs/rehaul_design.md` describe the new model of issue 035 **before** any code changes. The owner then confirms
the new decisions D47–D55. This task changes **no** Python, `.cmd` or test file.

## Read

- Issue 035, all of it. It is the source of every sentence you write.
- `_docs/rehaul_design.md`, all of it.
- `_docs/elevated-runtime-design.md`.

## Change

Only these two files:

- `_docs/rehaul_design.md`
- `_docs/elevated-runtime-design.md`

## Steps

1. **Rewrite §6** as "Installations and package kinds", from issue 035 §4.1, §4.2 and §4.6:
   - a run belongs to one installation (side, root); the side comes from the process token;
   - the root fallback chain, as a table, copied from issue 035 §4.1;
   - settings layers (issue 035 §4.2);
   - the package kinds (portable or not, `modes` with the meaning of issue 035 §4.6);
   - the side restriction (D55);
   - copyq as the worked example (issue 035 §4.6).
2. **Update §3 (Vocabulary):** add "Installation", "Side" and "Portable package". Remove "Root forwarder" and "ad-hoc
   root". Change "Root" (one root per run).
3. **Update §4:** the `gupkg-location` file in `<root>\.gupkg\` (issue 035 §4.9). Remove the `data\` wording "for
   portable mode" and write "for portable packages".
4. **§5.3:** replace rule 5 ("Each scope is a separate package") with "A name is unique within one installation". Remove
   the `mode:name` selectors and the shadowing text.
5. **§5.4:** remove ad-hoc roots. A path is inside the run's root, or a distribution folder that is copied in.
6. **§9.4, §9.6:** an elevated run is the system side. Remove the relaunch from rule 1. Rule 2 becomes: the system side
   names no user location (structural). Keep rules 3–5.
7. **§10.1–10.4:**
   - remove `--mode`, `mode:` selectors, `--in-place`, `--relaunched`, `--batch-confirmed`, `--stop-file`, "The elevated
     child" and `relaunched` in documents;
   - add `--root DIR`;
   - the API becomes `"api": 2`;
   - remove the `migrate` forwarder and the 0.17 rows of the §10.3 table.
8. **§11:** settings schema 4 (issue 035 §4.2). `[roots]` keeps `system` and `user` only. One `[integration]` table.
   Layers: defaults, app, root, `--settings` (user side only).
9. **§12:** remove `relaunch.py`, `gupkg_migrate`, `modes.py` mode selection, and `gupkg_ui/steps.py` grouping. Add the
   installation module.
10. **§13.2, §15:** one manager scope per run. An elevated manager is allowed. No grouping by mode.
11. **§18 (Migration):** delete the section's content and leave one line: "Removed by issue 035 (D54): the repository
    contains no migration code."
12. **§19:** add D47–D55, copied from issue 035 §6.1. In each superseded row (D4, D16, D17, D20, D22, D24, D31, D32, D33,
    D37, D42, D44, D45), add at the start: "**Superseded by D4x (issue 035).**" Keep the old text after it.
13. **§21:** forwarder text → the `gupkg-location` file and the launcher order of issue 035 §4.9. Remove
    `--no-system-gupkg`.
14. **`_docs/elevated-runtime-design.md`:**
    - remove the relaunch;
    - an elevated process runs only an admin-only gupkg tree, and acts only on the admin-only system root;
    - launcher `PATH` rule (issue 035 §4.9, owner's answer Q7 (b)).

## Done when

- [ ] `git diff --stat main` lists only the two docs files (and your log file).
- [ ] `Select-String -Path _docs\rehaul_design.md -Pattern 'default_mode','--relaunched','gupkg migrate','--no-system-gupkg','adhoc'`
      finds matches only in superseded decision rows, or none at all.
- [ ] §19 has rows D47 to D55.
- [ ] Every rule of issue 035 §4 appears in the design. List in your log each §4.x with the design section that now
      holds it.

## Do not

- Change code, tests, other docs or the issue.
- Invent rules that issue 035 does not state. If the design needs a sentence the issue does not support, stop and ask.
