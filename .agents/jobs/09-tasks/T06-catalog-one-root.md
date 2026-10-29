# T06: The catalog reads one root

Issue 035: §3.1 items 3–5 and 11, §4.3, §4.8, D50.

## Goal

`Catalog(scope)` scans exactly one root and joins exactly one registry cache. Ad-hoc roots and `--in-place` are
gone.

## Remove

From `src/gupkg/catalog/catalog.py`:

- `Catalog.from_settings`'s loop over modes and its reading of `state.toml` ad-hoc roots;
- `roots`, `data_roots`, `elevated`, `_trusted`, `_trusted_roots`, `_root_for_mode`, `_require_system_path`, the ad-hoc
  branch of `_root_of`;
- `AmbiguousSelector` and `_qualified`;
- the shadowing block of `list` (~438–448);
- the `preference` fallback of `_snapshots_for` (~798): one cache, `<root>\.gupkg\registry`;
- in `_view`, the two "install record says mode …" problems. They are replaced by the rule below;
- `ListFilter.modes`.

From `src/gupkg/model/state.py`: `Root.mode`, `Root.adhoc`, `Root.single`, `PackageView.mode`.

From `src/gupkg/catalog/local.py`: `read_adhoc_roots`.

From `src/gupkg/engine/stateio.py`: `register_adhoc`, `unregister_adhoc`, `[[adhoc]]`. `state.toml` keeps only the last
registry sync.

From the CLI: the `--in-place` option and the `--in-place` branch of `_copy_source`.

Also remove `resolve_package_path` callers' need for `elevated`.

## Add or change

1. **`Catalog(scope, system)`.** `describe`, `list` and `resolve` work on `scope.root`.
2. **A path selector** must lie inside `scope.root`. A path outside it is either:
   - a distribution folder: copied in, as today (design §21.4); or
   - `NotFound` with this message: `<path> is not inside this scope's root (<root>)`.
3. **A record whose `mode` is not `scope.mode`** is a broken reason:
   `the install record says <mode>; this package was moved between scopes — reinstall it`.
   New records write `mode = <mode>`.
4. **`list --json`:** remove `mode` from each row. Add `mode` and `root` once, at document level. Update
   `report/results.py`, `viewdata.py` and the contract test that pins the document. Do **not** bump the API version
   here: T07 does it once for all document changes. Record the change in your log for T07.
5. **Effect verification** (`_verify`): the `Mode` of an `env`/`PATH` effect is the run's `scope.mode`. The `self.elevated and scope is Mode.USER`
   special cases go.

## New tests (each must fail on `main`, then pass)

- [ ] Packages in two roots: a catalog of root A never lists, describes or resolves the package of root B, by name or
      by path. A path into root B gives the "not inside this scope's root" error.
- [ ] A user root whose registry cache was never synced has no registry entries. It does **not** see the system root's
      cache.
- [ ] A record with `mode = "system"` inside a user scope is `broken`, with the message above.
- [ ] A package folder with a `current` entry but no install record is `broken` with the reason `no install record
      (made by an older gupkg); reinstall with: gupkg install <name> --force`, and nothing in it changes (issue 035 §4.7).
- [ ] `list --available` in user mode lists a `["system"]` registry package marked `system only`, with the refusal
      text as the reason; in system mode a `["user"]` or portable one is marked `user only` (§4.6, Q14).
- [ ] `list --json` has `mode` and `root` at document level, and no `mode` per row.

## Done when

- [ ] `git grep -n "adhoc\|AmbiguousSelector\|_trusted_roots\|shadowing\|in_place\|--in-place\|Root(.*mode"` prints
      nothing in `src`.
- [ ] `src/gupkg/catalog/catalog.py` is at least 150 lines shorter than on `main`. Paste `wc -l` before and after.
- [ ] The full suite passes.
