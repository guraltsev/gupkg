# T18: Remove the unused code in `manager_policy`

Issue 036 F10. **The owner approves this task before it is dispatched**, because it changes product code. The controller
asks.

## Goal

`gupkg.model.manager_policy.decide_targets` and `command_line` have no caller in `src`. The UI has its own `decide_targets`
in `src/gupkg_ui/plans.py`. Remove the unused functions and what only they use.

## Change

1. Confirm that nothing calls them: `git grep -n "decide_targets\|command_line" -- src` shows only the definitions in
   `manager_policy.py` and the UI's own `decide_targets`. Paste the output.
2. Remove the two functions, and any helper, type or constant that only they use. Update the module docstring:
   - it says "`decide_targets` does that for a batch";
   - check also `src/gupkg/README.md` and `_docs/`.
3. Remove what is left of their tests (T10 retired most of them).

## Done when

- [ ] `git grep -n "manager_policy.decide_targets\|manager_policy import.*command_line" -- src tests` prints nothing.
- [ ] The UI's checksum questions still work: the owner tests of `tests/ui/test_policy.py` and `tests/ui/test_flows.py`
      pass, and one manager flow that asks a checksum question is shown rendered (`AGENTS.md` rule 8).
- [ ] `scripts\test.cmd --all` passes.
