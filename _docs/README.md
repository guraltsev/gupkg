# Documentation index

## For people using gupkg

Start at the top and stop when you have what you need.

1. [getting_started.md](getting_started.md) — install your first program and
   update it in five minutes.
2. [cookbook.md](cookbook.md) — recipes: user and system installs, portable packages,
   scheduled updates, rollback, uninstall, CI checks, adopting existing programs, repairs.
3. [troubleshooting.md](troubleshooting.md) — every error message with its fix.
4. [operations.md](operations.md) — the operator runbook: bootstrap, settings
   layers, registries and the registry tool, schedules, the health check, logs,
   sandbox runs, self-repair, and releasing.
5. [../README.md](../README.md) — the complete format 2 `pkg.toml` and command reference.

`gupkg --help` and `gupkg <command> --help` are always the authoritative list of
options for the version you have installed.

## For people changing gupkg

- [development_guide.md](development_guide.md) — the module map, the testing
  approach and the release build tools.
- [elevated-runtime-design.md](elevated-runtime-design.md) — the trust rules for
  elevated runs (the original design, with the current state in its section 9).
- [python_rules.md](python_rules.md) — Python implementation style, including
  code organization and block comments.
- [docstring_schema.md](docstring_schema.md) — docstring style.
- [tests.md](tests.md) — testing policy and what belongs in the suite.
- [tui_style_guide.md](tui_style_guide.md) — principles, interaction model, and
  visual style for the list-first terminal interfaces.
- [../tests/manual_smoke.md](../tests/manual_smoke.md) — Windows, elevation, and
  release smoke checks.
- [rehaul_design.md](rehaul_design.md) — **the design that gupkg 1.0
  implements**: package state model, one scope per run (system or user mode, portable packages), `pkg.toml`
  format 2, built-in services, checksum policy, unified CLI, events and logs, UI
  and testability, with the deviations listed at its top. Implemented by issues
  020–033, amended by issue 035 (one scope per run).
- [../_issues/_closed/](../_issues/_closed/) — design records and acceptance
  criteria for finished work, including the 1.0 epic
  ([issue 020](../_issues/_closed/issue020-rehaul-epic.md)).

`AGENTS.md` is only a pointer to the style and testing files. Put human-facing
rationale and style rules here.
