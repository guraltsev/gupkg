# Minimal list-first TUI guide

This guide defines the interaction, visual language, and implementation
boundaries for the two interactive programs of gupkg, `gupkg-tui` (one package)
and `gupkg-manager` (every package), which share the library `gupkg_ui`. It is
also intended as a reusable approach for small Python terminal applications:
apply the principles to the application's needs, rather than copying its
screens verbatim.

**What changed in the revision of issue 030.** The first version of this guide
banned headers, footers and persistent help. That ban is reversed for exactly
two lines: **every screen has one key-hint line at the top and one status line
at the bottom, and both are required.** Everything else stays frameless and
list-first.

## Purpose and fit

A small TUI is a decision surface, not a desktop application reproduced in a
terminal. The terminal already provides a window, cursor, scrolling, and
keyboard input. The application need only make the current context, available
choices, and result unambiguous.

Use this style when the application:

- has a modest number of operations and settings;
- is primarily keyboard-driven;
- must work in narrow, short, resized, or remote terminals (80x24 is the floor);
- exposes the same operations as an existing command line or Python API; and
- benefits from a little contextual status before an action.

The central rule is simple: present a short, well-ordered list of decisions,
between a line that says which keys work and a line that says what is going on.

## The fixed frame (required on every screen)

```text
 Enter open  / find  a avail  c check  u update  U all  s sync  ? help  q quit    <- key-hint line (1 line, always)
 Name   Version  Status            Registry                                      <- the content: frameless
 alpha  1.0.0    outdated → 2.0.0  main
 …
 user · C:\…\opt · 2 packages · 1 outdated · 0 broken · registry main @ 4f2a9c1    <- status line (1 line, always)
```

- **The key-hint line is the top line.** It lists the keys that work *on this
  screen now*, in the order of importance, and it differs between a running
  and a finished run view. It fits in 78 columns, so at 80x24 it is never
  clipped; `? help` and a way out (`q quit` or `Esc back`) are always on it. A
  key that exists but does not fit is explained by the help screen (`?`).
- **The status line is the bottom line.** It starts with the scope of the run
  (`user · <root>` or `system · <root>`, the root shortened in the middle so that the last folders stay), because a run has one scope
  and the person must always see which root is managed. It then says what the program knows and is
  doing: counts (outdated, broken), the registry revision and how long ago it
  was synchronised, the state of a run (`Running`, `Finished`, `Failed (exit
  code N)`), and the path of the latest log. The most important parts come
  first, because a narrow terminal clips the tail with an ellipsis.
- **Both lines are plain text on a quiet background.** They are not buttons,
  menus, banners or a title: no product name, no logo.
- **Dialogs carry the frame too.** A prompt is a full-screen dialog with its own
  hint line (`y yes   n no   Esc no`) and status line, so no screen ever
  lacks them. A dialog covers the whole terminal; nothing is drawn over the
  screen below it.
- **Both lines are checked by the test suite** at 80x24 and 200x50 for every
  screen of both programs.

## Visual language

### Use one list per decision

Show actions and settings in one selectable list; the selected row is the only
necessary indication of focus. Do not put the list inside a panel or add a
parallel row of buttons. The package browser is a table of rows (a table is a
list with columns); its actions are keys named on the hint line.

### Do not draw frames

Avoid the following unless a terminal protocol genuinely requires them:

- ASCII or Unicode borders;
- bordered form fields, cards, panels, dialogs, and tables;
- headers and footers *other than the two required lines*;
- button-like widgets for ordinary actions;
- large title banners, logos, or repeated product names; and
- spacer rows used only to make a screen feel less empty.

If a widget library draws borders, outlines, or background panels by default
(`DataTable`, `RichLog`, `OptionList`, `Input`), turn them off explicitly. The
shared stylesheet of `gupkg_ui` (`screens/frame.py`) does it once for all of them.

### Keep density intentional

Use one line per useful piece of information. Add a line only when it changes
the user's decision: object identity, installed version, warning, action,
setting, or command result.

Prefer words to status glyphs: use `on` and `off`, `user` and `system`, or
`unavailable`, rather than checkbox art. Color may reinforce meaning, but it
must never be the only signal: write `Warning:` and `Error:` even when the line
is colored, and end a failed phase with the word `failed`.

The run view uses the same palette as the console renderer (design 14.2):
phase headers bold cyan while running, then a green check (`✓`), a red cross
(`✗` and the word `failed`) or a dim dash (`–` and the word `skipped`); `+` and
`−` lines green and red; warnings yellow; errors bold red; child-process output
dim. Nothing else is colored. With `NO_COLOR` set the words and marks still carry
every state.

The test is simple: a copied, unstyled, monochrome rendering should still make
the hierarchy and every important state clear.

## Information hierarchy

### Put context before choices

When a screen operates on a package, show its identity before the rest:

```text
<name> <version>  Installed: <active version or not installed>
status: outdated → 2.0.0
Warning: metadata conflicts with the directory name.
```

Use the authoritative source for identity: the directory identity owns the
package name and version, and metadata is checked against it. Show a warning only
when the conflict exists.

### Reveal detail progressively

Decision-relevant state stays visible; explanatory detail is available on demand;
high-volume output gets its own view. The package view shows versions, registry
versions, tracker, link, data folder, last receipt and log. A package's log has
its own scrollable view (`l`).

### Text from outside is literal

Every piece of text that comes from a package, the network, a hook or a log (names,
versions, descriptions, error messages, child-process lines) is shown **literally**:
as `rich.text.Text` objects (never as markup), with escape sequences, control
characters and bidirectional overrides removed and every line cut at a fixed length.
A name such as `[bold red]x[/]` is drawn as typed. In a `DataTable` pass `Text` cells
(a plain string is parsed as markup), and give `Static` widgets `markup=False`.

### The run view

An operation's output belongs in the run view, which contains:

1. an append-only log (`RichLog`) fed from the `--events-file` of the `gupkg` call, with
   the coloured phase headers, indented detail lines and warnings and errors;
2. one live progress line under the log for the transfer that runs now (a finished
   transfer leaves one lasting line in the log); and
3. the verdict and the summary, which are the **last lines of the log** and are repeated
   in the status line.

The log only appends: it never re-renders what is already in it, it keeps at most 5,000
lines (the log files keep everything), and it follows the tail until the person scrolls up
and resumes when they return to the bottom (`End` jumps there). Detail lines beyond a
limit are counted, and the count is shown once.

## Screen and interaction model

A new screen is justified when the user's mode of attention changes. Both programs are
**one application each**: opening a package, running an operation and coming back push and
pop screens, so the screen below keeps its filter, selection and scroll position. Nothing
exits and restarts to switch screens.

```text
browser (manager only) --Enter--> package view --c/u/r/x/h--> run view
        ^                              ^                          |
        +------------- Esc ------------+-------- Esc -------------+
```

### Prompts are dialogs, and they answer only to explicit keys

Confirmations (a plan, an uninstall, a purge), the checksum question and the cancel question
are dialogs. `y` answers yes; `n` and Escape answer no; **Enter never confirms**, and the
default is always the safe answer. The CLI is always called with `--no-input`: every
question is asked here, before the call, and the answer travels as `--yes` or as the
checksum decision.

### A running operation is protected

While a call runs, Escape (and `q`) ask "Cancel after the current package?" (decision D37).
Yes asks the running CLI to stop after the package it is working on, which finishes cleanly;
the rest is reported as skipped and no further call is started. The view can be left only
when the run has ended, so no operation is ever abandoned silently.

### Keep navigation uniform

Up and Down move through lists, Enter opens or activates the selected row, `/` filters, `?`
shows the keys of the program.

| Context | Escape | `q` |
| --- | --- | --- |
| Browser (manager) | Clear the filter, or exit the application | Exit the application |
| Package view (opened from the browser) | Back to the browser | Exit the application |
| Package view of `gupkg-tui` | Exit the application | Exit the application |
| Run view, finished | Back | Exit the application |
| Run view, running | Ask "Cancel after the current package?" | The same question |
| Dialog, log view, help | Back, answering no | (closes it) |

## Responsive behavior

Terminal dimensions are unstable: users resize windows, connect through SSH, change fonts,
and work in split panes. Build responsive behavior into the structure:

- use one vertical reading order;
- let the content take the remaining height between the two required lines and scroll;
- keep each line short and let the frame lines clip with an ellipsis (hints are written to
  fit 78 columns);
- never hide an action: if a terminal is too short, show fewer rows; the selected row must
  remain visible.

## Preserve application semantics

The programs are another way to invoke the application's behavior, not a second application.
**Every operation is a `gupkg` CLI call** through the machine interface (`--json`, `--plan`,
`--events-file`, `--no-input`; decision D44): the programs may import gupkg core to *read*
(package state, settings, the event vocabulary), but they never import the engine or run
anything in-process. They call the `gupkg.cmd` of their own gupkg tree (never one on `PATH`)
with an argument list and never through a shell, refuse a result whose `"api"` is not the one they were built for (2), and
treat an unreadable result, a missing `run_end` or a crash as a failure.

Keep framework code at the presentation edge:

```text
gupkg CLI
    validates inputs and performs operations (one call per group of targets; the run's one scope
    is the mode of the program's own process, and nothing in the screens switches it)

gupkg_ui.client / flows / session
    build the calls, follow the events files, apply the checksum policy

screens
    own temporary interaction state and dispatch user intent
```

## Long-running work

Filesystem, subprocess, and network work must not block the terminal event loop. A call is an
`asyncio` subprocess whose events file the screen follows, so child output never reaches the
terminal behind the interface and no widget is touched from another thread. Open the run view
before work starts, then show explicit text states (`Running`, `Finished`, `Failed`).

## Textual implementation notes

```tcss
Screen { padding: 0; }
KeyHintBar, StatusFooter { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
VerticalScroll, RichLog, DataTable, Input, OptionList { background: transparent; border: none; outline: none; }
```

- Use `DataTable` (rows, `Text` cells) for the browser, `RichLog` for the run view and logs,
  `OptionList` for choosing one item.
- Disable the command palette; it is one more chrome element.
- Tests drive the screens with Textual's pilot at 80x24 and 200x50 and assert on visible text
  and on reachable actions, not on widget identity.

## Review checklist

Before shipping, verify the following:

- Every screen, dialogs included, has the key-hint line on top and the status line at the
  bottom, complete at 80x24; no other header, footer, frame or button bar exists.
- The screen makes sense as plain monochrome text; no state depends on colour.
- Text from packages is literal; a name with brackets or escape characters cannot style or
  break the screen.
- Up/Down and Enter reach every row; Escape has the documented back/exit behaviour.
- A running operation cannot be left without the cancel question, and Enter never confirms a
  dialog.
- Every operation is a `gupkg` call; the program imports nothing that performs one.
- Long output is bounded: the log caps its lines and the view keeps answering keys.
