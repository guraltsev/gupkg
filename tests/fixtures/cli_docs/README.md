# Recorded documents of the real CLI

One document of every kind that the manager and `gupkg.report.documents` read, recorded from the real `gupkg.cmd` of this tree
(`--root`, `--settings`, `--sandbox` on a temporary folder; nothing of the real machine changes, nothing uses the network).
Nothing here is edited by hand: run `python tests/fixtures/cli_docs/record.py` to record them again (the temporary folder is
replaced by `@ROOT@`).

The world: a user root with `alpha` and `beta` installed at 1.0.0 (2.0.0 present, so both are outdated), `gamma` local, and a
registry cache `main` holding `copyq`, `drv` (declares `modes = ["system"]`) and `graphviz`.

| File | Command (after the root options `--root R --settings S --sandbox SB`) |
| --- | --- |
| `list.json` | `list --json` |
| `list_names.json` | `list --names --json` |
| `list_available.json` | `list --available --json` (`drv` is `system only` with its reason) |
| `info.json` | `info alpha --json` |
| `registry_status.json` | `registry status --json` |
| `check.json` | `check --json --no-input -- alpha` |
| `install_plan.json` | `install --plan --json --no-input -- copyq` (a download with a missing checksum) |
| `install_result.json` | `install --yes --json --no-input -- gamma@1.0.0` (success) |
| `install_failure.json` | `install --yes --json --no-input -- nothere` (failure, exit code 2) |

`tests/fixtures/ui/` is the other set of recorded documents: the world of the UI screen tests (two outdated packages, after a real
`update`), with the events files of that run, which these documents do not have. The two sets answer different questions (the screen
tests need a run's events; the reader tests need one document of every kind, including the registry and failures), so they are kept apart.
Both are recorded by a script and never edited by hand.
