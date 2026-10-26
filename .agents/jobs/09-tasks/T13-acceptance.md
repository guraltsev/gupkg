# T13: Acceptance

Issue 035: §9 (all items). AGENTS rules 3, 8, 11, 12.

## Where

Never on the owner's roots (issue 035 Q3). Use:

- **Test roots:** create `<tmp>\sys` (made admin-only with `icacls`, from an elevated shell) and `<tmp>\user`, with
  their own `bin` and Start Menu folders set in a `--settings` file or `$App\settings.toml` of a **copy** of the built
  tree;
- or `--sandbox <tmp>` for anything that would change the environment or the Start Menu;
- items that need a clean machine (the bootstrap, the real UAC shortcut): write them as manual checks for the owner.

## Steps

1. **Build a gupkg tree from this branch,** without the network. If the build script needs the network, use the source
   tree's `src\gupkg.cmd` instead, and say so.
2. **For each numbered item of issue 035 §9,** either:
   - run it, and paste the commands and the full output into `.agents/jobs/09-logs/T13.md`; or
   - if it needs UAC, a VM or a clean machine, write it as `035-T13-N` in `tests/manual_smoke.md`, with exact commands
     and the expected result.
3. **§9 item 1 (the poisoned user root).** Create in the user test root:
   - a `settings.toml` that is not valid TOML;
   - a `state.toml` with junk;
   - a package whose `pkg.local` hook writes a marker file;
   - a registry cache with another revision.

   Then run, elevated against the system test root: `list`, `check`, `install`, `update`, `cleanup`, `registry sync`,
   and the manager (pilot). Show that:
   - each succeeds;
   - no output mentions the user root;
   - the marker file does not exist.

   Swap the roles for the normal run.
4. **§9 item 2.** Run the four greps and paste the output.
5. **Counts.** Paste the test counts and the source and test line counts, before (from the progress file's baseline) and
   after.

## Done when

- [ ] Every §9 item is either shown with output or written as a manual check.
- [ ] Nothing outside the temporary folders changed. Show `git status`. If you ran anything not in a sandbox, list the
      folders you created and confirm they are deleted.
- [ ] The report states plainly which items passed, which failed (with the output), and which are owed to the owner.
