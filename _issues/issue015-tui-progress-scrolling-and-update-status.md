# Issue 015: TUI progress, scrolling, and update-status feedback

Date: 2026-10-17
Priority: High (daily usability)
Change type: Bug fixes in `tui.py`, `manager_tui.py`, `commands.py`, `downloads.py`,
`core.py`, `manager.py`; no package-format or CLI-option changes

## Objective

Four problems make the interactive interface tiring to use:

1. Download progress adds a new line for every update instead of updating one line.
2. When output is longer than the screen, the view stays at the top. New output
   is written below the visible area and the user never sees it.
3. "Refresh update status" (and "Update all") shows nothing until every package
   has been checked.
4. Update-check results disappear after the user opens a package and comes back
   to the manager.

This issue records the root cause of each problem and a concrete fix. It also
covers one related latent bug found during the analysis: tool output is
rendered as Textual markup. That fix shares the same code path as fix 2.

Line numbers refer to commit `7fac9c1`.

## Summary

| # | Symptom | Root cause | Fix |
|---|---------|------------|-----|
| 1 | Progress writes new lines | `download_response` calls `log_info` (newline-terminated) for each MiB. `LineStream` and `ResultScreen` can only append lines. | Add a *transient line* protocol (`\r`-terminated). `LineStream` gets an `on_transient` callback. The TUI shows transient text on the status line. The CLI overwrites one line on a console and prints only the final line when output is redirected. |
| 2 | No auto-scroll | Output is a `Static` inside a `VerticalScroll`. Each update replaces the text, but nothing moves the scroll position. | One shared `OutputView` (anchored `VerticalScroll` plus a markup-free `Static`) used by every result screen. |
| 3 | No feedback while checking | `refresh_update_status` / `plan_updates` check every target in one blocking call. Each check's output is discarded. The screen updates only at the end. | Add an `on_progress` callback (same pattern as `execute_updates`). Stream "Checking i of N" and one result line per target. Allow cancelling between targets. |
| 4 | Check results erased | The manager app exits to open a package, then `refresh_inventory()` rebuilds every `ManagedTarget` with `update_status="unchecked"`. The status lives only in memory, although each check already writes it to disk. | Discovery reads `update_status` back from `<root>/.gupkg/state/update.toml`. Also reopen the browser where the user left it. |
| — | Output with `[...]` disappears or crashes | `Static.update(str)` parses Textual markup. | Render output with `markup=False` (part of `OutputView`). |

Recommended order: **4 → 2 (+ markup) → 3 → 1**. Each step can be released on its
own. Step 4 is the smallest and gives the most benefit. Step 2 creates the widget
that steps 3 and 1 use.

---

## 1. Download progress writes new lines instead of updating one

### Root cause

- `src/gupkg/downloads.py:41-70`: `download_response` reads in 1 MiB chunks
  (`_CHUNK_SIZE`, line 19). After each MiB it calls `log_info(...)`.
- `src/gupkg/core.py:244-259`: `log_info` goes through `_DynamicStdoutHandler`,
  which calls `print(...)`. Every report is a newline-terminated line. On the
  CLI, a 300 MiB download prints about 300 lines.
- `src/gupkg/commands.py:179-206`: `LineStream.write` splits only on `\n` and
  removes `\r`. It has no way to say "replace the last line".
- `src/gupkg/tui.py:357-360`: `ResultScreen._progress` appends every line to
  `self.lines` and redraws the whole text.

A second, smaller problem: reporting is tied to the 1 MiB chunk. On a slow
connection (100 KiB/s), nothing appears for about 10 seconds between updates,
and the first update after "total size" can take that long too.

### Fix design

Use carriage-return semantics, because terminals already understand them, and
teach the TUI stream to understand them too.

**a) A transient-progress primitive in `core.py`**

```python
def log_progress(message: str, *, final: bool = False) -> None:
    """Show one progress line that later progress replaces.

    On a live stream (an interactive console, or a stream that declares
    ``accepts_transient``), intermediate messages overwrite one line and the
    final message is committed with a newline. On any other stream (a
    redirected file, a pipe, test capture, ``_quiet``), intermediate messages
    are dropped and only the final message is written, as an ordinary line.
    """
```

Implementation notes:

- Write directly to `sys.stdout`, not through the logger, because the logging
  handler always adds a newline. Look up `sys.stdout` when the function is called,
  as `_DynamicStdoutHandler` does, so that `redirect_stdout` still works.
- Live detection: `getattr(stream, "accepts_transient", False) or
  (stream.isatty() if hasattr(stream, "isatty") else False)`.
- Intermediate write: `message.ljust(previous_width) + "\r"`, then flush.
  Pad with spaces rather than the ANSI erase sequence `\x1b[K`. Plain Python
  does not turn on virtual-terminal processing in the Windows console, and
  without it legacy `conhost` prints the escape sequence as visible text.
  Remember the previous width in a module-level variable and reset it on `final`.
- Shorten intermediate messages to `shutil.get_terminal_size().columns - 1`. A
  line that wraps cannot be overwritten with `\r`.
- Final write: `message.ljust(previous_width) + "\n"`.

**b) Report by time, not by chunk, in `downloads.py`**

- Read in 64 KiB chunks. Call `log_progress` at most once every 0.2 s
  (`time.monotonic()`), and always on completion with `final=True`.
- Keep the `"<label>: total size …"` line as a normal `log_info` line. It is
  lasting, useful information.
- The final line is unchanged (`… / … (100%)` or `… received`), so the CLI
  output that users and tests depend on stays the same when output is redirected.

**c) `LineStream` understands `\r`** (`commands.py`)

Add an optional `on_transient: Callable[[str], None] | None = None`.
Set `accepts_transient = on_transient is not None`. The parser handles
three cases:

| Input | Action |
|-------|--------|
| text | append to `_pending` |
| `\r` | if `_pending`: `_transient = _pending`, call `on_transient(_pending)`, clear `_pending` |
| `\n` | emit `on_line(_pending or _transient or "")`, then clear both |

The `\n` row also handles `\r\n` from child-process output: the transient text
is committed as a normal line, which matches what a terminal shows. Without
`on_transient`, behaviour stays as it is now, except that a lone `\r` no longer
leaves `\r` characters inside lines.

**d) The TUI shows transient text on the status line** (`tui.py`, `ResultScreen`)

The style guide (`_docs/tui_style_guide.md:107-112`) allows only a command
summary, one status line, and scrollable output. The status line is the right
place for live progress:

```text
gupkg update --download-only C:\opt\Foo
Running: Downloading update: 12.0 MiB / 80.0 MiB (15%)
<output view>
```

`_run` builds `commands.LineStream(self._progress, on_transient=self._transient)`.
`_transient` calls the app from the worker thread
(`call_from_thread(status.update, f"Running: {text}")`). When the final line is
committed, the status goes back to `Running...` and the 100% line appears in
the output. The 0.2 s limit keeps calls from the worker thread infrequent.

**e) Unchanged on purpose**

- `--format toml` still discards progress (`cli.py:453-455`): `StringIO` is not
  live, so only final lines are produced, and those are discarded too.
- `manager._quiet` (`manager.py:794-797`) still discards everything.
- An elevated child whose output goes to a file or pipe is not live, so its
  log gets one final line per download instead of hundreds.

---

## 2. Long output does not scroll

### Root cause

Every result view has the same structure:

- `tui.py:342-343` (`ResultScreen`): `VerticalScroll` → `Static#output`
- `manager_tui.py:433-434` (`RefreshScreen`), `462-463` (`UpgradePlanScreen`),
  `542-543` (`ExecutionScreen`)

`Static.update(...)` replaces the text and the container's virtual height
grows, but nothing changes `scroll_y`. The view stays at the top, and new
lines (including the final outcome and its errors) are added out of sight.

Related problems in the same code:

- **Markup.** `Static` uses `markup=True` by default. I checked against the
  bundled Textual 8.2.8: `"ERROR: [bold] failed [b]"` renders as
  `"ERROR:  failed "`, and a stray closing tag such as `[/x]` raises
  `MarkupError`, which crashes the screen. Package output often contains TOML
  table names (`[update.check]`), paths, and hook output, so it is not safe to
  render as markup.
- **Quadratic redraw.** `ResultScreen` joins and re-renders all lines for every
  new line. This does not matter for tens of lines, but it gets noticeably slow
  with verbose hook output.

### Fix design

Add one small widget, used by all four live result screens. It can live in
`tui.py` and be imported by `manager_tui.py`, or both modules can build it from
a shared helper function, because Textual is imported lazily inside
`run_tui` / `run_manager_tui`:

```python
class OutputView(VerticalScroll):
    """Scrollable plain-text output that follows new lines until the user scrolls up."""

    def compose(self) -> ComposeResult:
        yield Static("", markup=False, id="output-text")

    def on_mount(self) -> None:
        self.anchor()

    def set_lines(self, lines: list[str]) -> None:
        self.query_one("#output-text", Static).update("\n".join(lines))
```

- `Widget.anchor()` exists in Textual 8.2.8 (`textual/widget.py:800`). It keeps
  the container at the bottom as content grows. It releases when the user
  scrolls up, so they can read earlier output, and it engages again when they
  scroll back to the bottom (`_check_anchor`). This is the behaviour people
  expect from a terminal.
- Focus the view in `on_mount` when the screen has no list, so that arrow
  keys, PgUp/PgDn, and Home/End scroll it without a mouse.
- Use `markup=False` for every `Static` that shows text from a package, the
  network, or a hook. That includes `TextScreen`, `DoctorScreen`, and the
  `DetailsScreen` diagnostics, which do not need anchoring.
- Built-in alternative: `textual.widgets.Log` (auto-scroll, cheap appends,
  plain text). It cannot replace a line in place, and `ExecutionScreen` needs
  that because it redraws per-target states. `OutputView` covers every screen
  with one approach. If verbose output turns out to be slow, change
  `set_lines` to append to the `Static` instead of rebuilding it. Callers do
  not change.
- CSS: the existing `VerticalScroll { background: transparent; border: none;
  height: 1fr; }` rule already applies, because `OutputView` is a subclass.

---

## 3. No feedback while checking for updates

### Root cause

- `manager_tui.py:440-447`: `RefreshScreen._refresh` runs one
  `asyncio.to_thread(commands.refresh_update_status, inventory)` and writes
  the output only after every target has been checked.
- `commands.py:573-578`: `refresh_update_status` loops over the targets one at a
  time. It has to, because each check redirects the process-wide `stdout`.
  It has no progress hook.
- `manager.py:794-797`: `_quiet` discards each check's output, so no partial
  text exists that could be streamed.
- `manager_tui.py:471-475`: `UpgradePlanScreen` has the same problem through
  `commands.plan_updates`, which also checks every installed target.
- In the package TUI, "Update: check" shows `Running...`, but the check path
  (`gupkg.py:_discover_update`, `updates.check_update`) logs nothing, so the
  output view stays empty until the outcome arrives.

With N packages, each needing a network round trip (GitHub API, git
ls-remote, module checkers), the screen shows "Checking packages..." without
change for tens of seconds. It looks the same as a hang.

### Fix design

**a) Progress callback in the command layer** (copy the existing
`execute_updates(..., on_progress=..., cancel_requested=...)` signature)

```python
def refresh_update_status(
    inventory: ManagerInventory,
    *,
    on_progress: Callable[[int, int, ManagedTarget, ActionResult | None], None] | None = None,
    cancel_requested: Callable[[], bool] = lambda: False,
) -> list[ActionResult]:
    """... ``on_progress(index, total, target, None)`` is called before each check and
    ``on_progress(index, total, target, result)`` after it."""
```

Make the same change to `plan_updates` (`manager.py:529` area: the boundary that
refreshes each target). The planner then reports "checking i of N" with the
same callback type.

**b) `RefreshScreen` streams as it works**

- Run the worker as a thread (`run_worker(self._refresh, thread=True)`), the
  same as `tui.ResultScreen`, and send updates with `app.call_from_thread`.
- Status line: `Checking 3 of 12: user:7zip`
- Output (`OutputView`): one line per finished target, written as soon as it
  finishes, using the existing `target_label` text, for example
  `user:7zip: Update Available v25.01.l1`. Errors go on their own line
  (`  Error: <first error>`).
- Final status: `Refresh complete: 2 available, 9 current, 1 error.`
- Escape sets `cancel_requested`. The loop stops before the next target, and
  the status reads `Cancelled after 4 of 12.` Results from finished checks
  are kept: they are saved to disk (see §4) and are already on the in-memory
  targets. At the moment, Escape pops the screen while the thread keeps
  running without any visible output.

**c) `UpgradePlanScreen`** uses the same status line (`Checking 3 of 7: …`)
before it shows the plan summary.

**d) The package-level check reports what it is doing**

In `gupkg._discover_update` (or `updates.check_update`), add one
`log_info` line before the network call and one after it:

```text
Checking for updates (github: owner/repo)...
Update available: v1.3.0.l1 (installed v1.2.0.l1)
```

Use a matching line for `current` / `not-configured`. These lines appear in
the CLI and the package TUI, and `_quiet` discards them in manager batches.

**Not in scope:** checking in parallel. Because each check redirects the
process-wide `stdout`, parallel checks would mix up their output unless
output were first routed per thread (a thread-local `sys.stdout` proxy). With
streamed progress, checking one at a time is acceptable. Reconsider if
refresh is still too slow.

---

## 4. Update-check results disappear after visiting a package

### Root cause

The status exists only in memory, and the inventory is rebuilt from scratch:

1. `DetailsScreen` → "Open package operations" calls `self.app.exit(self.target)`
   (`manager_tui.py:418`). This closes the whole manager app, including the
   browser screen and its filters.
2. The outer loop runs the package TUI and then calls `refresh_inventory()`
   (`manager_tui.py:679-686`).
3. `refresh_inventory` → `commands.discover_manager` →
   `manager._managed_target` (`manager.py:468-497`) creates each
   `ManagedTarget` with the dataclass default `update_status="unchecked"`
   and `candidate_version=None` (`manager.py:111-112`).
4. A new `ManagerApp()` starts at `HomeScreen`, not at the browser.

`refresh_inventory()` also runs after saving shim linkage
(`manager_tui.py:257`) and after initialization, and both clear the statuses
the same way.

The data is not actually lost. Every check, from the manager or from the
package TUI, goes through `gupkg._discover_update` (`gupkg.py:535-569`). That
function writes `lastStatus`, `lastCandidateVersion`, `lastSuccessfulCheck`,
and `lastError` to `<package root>/.gupkg/state/update.toml`
(`updates.update_paths`, `updates.write_update_state`). Discovery never reads
that file. `manager_update_target` already reads it, but only to fill in
`candidate_version` after a live check (`manager.py:679-683`).

### Fix design

**a) Discovery reads the saved status** (main fix; also helps
`gupkg manager list` and future sessions)

In `_managed_target`, after `installed_version` / `local_version` are known:

```python
status, candidate, checked_at = _saved_update_status(package.root, installed_version or local_version)
return ManagedTarget(..., update_status=status, candidate_version=candidate, checked_at=checked_at)
```

The rules for `_saved_update_status`:

| Saved state | Displayed `update_status` |
|-------------|---------------------------|
| file missing or unreadable | `unchecked` |
| `lastError` set (the last attempt failed) | `error` |
| `lastStatus` = `current` / `not-configured` | the same value |
| `lastStatus` = `available`, and the candidate is not newer than the active version (`compare_package_versions(active, candidate) >= 0`) | `current`. The update was installed after the check, and `full_package_upgrade` does not rewrite `lastStatus`. |
| `lastStatus` = `available`, and the candidate is newer | `available`, with `candidate_version = lastCandidateVersion` |

Requirements:

- **Discovery must not change files.** `updates.load_update_state` renames a
  corrupt state file and logs a warning (`updates.py:91-115`). Discovery must
  use a read-only parse (`read_toml_file` inside `try/except` → `unchecked`).
  Renaming is left to the update workflow, which owns that file.
- Check that `lastCandidateVersion` uses the same version-directory form that
  `compare_package_versions` expects (it comes from
  `_candidate_version` / `assignedVersion`, so it should). Add a fixture that
  covers it.
- Add `checked_at: str | None = None` to `ManagedTarget`. Show it in
  `DetailsScreen` (`Update: available v1.3.0.l1 (checked 2026-10-17 14:02)`)
  and in `target_record` for `manager list --format toml`. Keep the browser
  row as it is (one line per target, `_docs/tui_style_guide.md:67-72`).
- Saved results do not expire automatically. The check time is shown and the
  user decides when to refresh. A max-age setting can come later if needed.
- After `manager_update_target` finishes a live check, also set `checked_at`
  so the in-memory and on-disk values match.

After this change the original workflow behaves as expected. A user who opens
a package from the browser, runs "Update: check" or "Update" there, and goes
back sees the new status, because the package TUI wrote it to the same
file that discovery now reads.

**b) Return to the same screen** (separate, smaller change)

Keep a small resume record in the `run_manager_tui` closure: the screen
(`browser`), `scope_filter`, `status_filter`, and the highlighted
`target_id`. `DetailsScreen` fills it in before `app.exit(target)`. When the
loop starts the next `ManagerApp`, `on_mount` pushes `HomeScreen` and then
`BrowserScreen(resume)`, which restores the filters and moves the highlight
to the target if it is still visible. Escape still goes back to Home.

**Not in scope:** running the package screens inside the manager app instead
of exiting and starting a new app. That would keep all screen state for free,
but it means combining two `App` subclasses, their bindings, and
`forced_scope` handling. Fix (b) gives the same result for much less work.

---

## Tests

Follow `_docs/tests.md`: test behaviour users can see, not internals. Suggested
permanent tests:

**Download progress** (`tests/test_download_progress.py`)
- On a stream that is not live (`capsys`), a known-size download prints the
  total-size line and one final `(100%)` line, and no intermediate progress
  lines. This keeps the current redirected CLI contract and stops output growing
  with download size.
- On a `LineStream` with `on_transient`, intermediate progress reaches the
  transient callback, and exactly one committed line contains `(100%)`.

**`LineStream` (public in `commands.__all__`)**
- `"a\rb\r"` gives transients `a`, `b` and no lines; then `"\n"` commits `b`.
- `"x\r\n"` commits `x` as one line. Without `on_transient`, no committed
  line contains `\r`.

**TUI scrolling and markup** (`tests/test_tui.py`, `run_test(size=(60, 8))`)
- Monkeypatch `commands.run_package_command` to write 50 lines to its `output`
  and return a failing outcome. After it finishes, the output view is scrolled
  to the end (`is_vertical_scroll_end`) and the failure text is visible.
- Output containing `[bold]` and `[/x]` appears literally and does not raise.
- During a download, the status line shows the transient progress text. It is
  not added to the output.

**Refresh progress** (`tests/test_manager_tui.py`)
- With `manager_update_target` monkeypatched to wait on a `threading.Event`, the
  status reads `Checking 1 of 2: …` while the first check is blocked. The first
  target's result line appears before the second check finishes.
- Escape during refresh stops before the next target and keeps results already
  obtained.

**Saved status** (`tests/test_gupkg_manager.py`)
- A package root whose `update.toml` says `available` with a newer
  candidate is found with `update_status == "available"` and that candidate.
- If the active version equals the saved candidate, the status is `current`.
- A corrupt `update.toml` gives `unchecked` and is **not** moved.
- Manager → open package → return: with `run_tui` monkeypatched to write an
  `available` state file, the browser shown after the return lists the target
  as `Update Available …`.

Also update `tests/manual_smoke.md` with the steps from the acceptance
checklist that need a real console (`\r` behaviour in `conhost` and
Windows Terminal).

## Acceptance checklist

- [ ] CLI download in a console: one progress line updates in place and ends
      at `(100%)`. Redirected to a file: the total-size and final lines only.
- [ ] TUI download: progress updates on the status line, and the output view
      gets one final line.
- [ ] Output longer than the screen follows new lines. Scrolling up stops the
      following, and scrolling back to the bottom starts it again. PgUp/PgDn
      work without a mouse.
- [ ] Output containing `[...]` shows literally and never crashes.
- [ ] Refresh: `Checking i of N: <target>` and per-target lines appear during
      the check. Escape stops after the current target.
- [ ] Update all: the planning phase shows the same progress.
- [ ] Package "Update: check" prints what it is checking and the result.
- [ ] Refresh, open a package, return: statuses are still shown, the browser
      reopens with the same filters and highlight, and a package updated
      meanwhile shows `Current`.
- [ ] A new `gupkg manager` session shows the saved statuses with check
      times in Details.
