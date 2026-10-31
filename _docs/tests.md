# Test policy

What a good test is, how test quality is measured, where a test goes, and which tier runs it. How the suite stays healthy
over time (temporary tests, retiring tests, automatic checks) is in [tests_hygiene.md](tests_hygiene.md).

Sections 1–6 are general and meant to work for any project. Section 7 binds them to this project.

## 1. Purpose

A test suite exists to tell you, quickly and truthfully, when a change breaks something a user or caller relies on. Every
test costs something every time it runs, and again every time someone (a person or an agent) has to read it, keep it
consistent or change it. So a test must earn its place, and **fewer, better tests beat many narrow ones**. Coverage, the
test count and how long a test is are not goals.

## 2. Measuring the quality of a test

### 2.1 The test of a test

A test is accepted only if every **must** below holds. Reviewers check each test against this list, and a failed **must**
is a blocking defect.

| #   | Question                                                                                                                                         | Level  |
| --- | :----------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| Q1  | **Behaviour.** Can you say in one sentence, in words a user or caller would understand, what is broken when this test fails?                     | must   |
| Q2  | **Proven.** Did a new test fail on the code without the change it protects, and pass with it? Both runs were seen.                               | must   |
| Q3  | **Owner.** Is this the layer that decides the rule? If not, is it the *one* case that shows the rule is wired in here?                           | must   |
| Q4  | **Unique.** Is it the only test that fails for exactly this reason?                                                                              | must   |
| Q5  | **Robust.** Would it still pass after a refactor that keeps the behaviour? (It uses public entry points and mocks only real boundaries.)         | must   |
| Q6  | **Deterministic.** Does it give the same result on every run, on every machine, in any order? (No real network, no real clock, no timing races.) | must   |
| Q7  | **Cheap.** Does it run in the lowest tier that can prove the behaviour (§5), and do no more work than the behaviour needs (§4.7)?                | should |
| Q8  | **Current.** Does it describe what the product does now, not what an old version did or which issue added it?                                    | should |
| Q9  | **Documented.** Does the name state the behaviour? Does the docstring explain it in the product's terms, assert everything it claims, and agree with the official documentation (§4.2)? | should |
| Q10 | **Readable.** Is the body short enough to check at a glance, with the action and the assertion visible?                                         | should |

A **should** that fails is debt. It is recorded (see [tests_hygiene.md](tests_hygiene.md) §5) and not added to.

### 2.2 Common failures, and what to do instead

| Smell                                                                                                | Why it is bad                                                      | Instead                                                                      |
| :--------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------- |
| The same rule tested at the validator, the planner, the executor, the CLI and the UI                 | Five tests to update for one change; none of them is "the" test    | Test thoroughly at the owner layer, and once elsewhere to show wiring (Q3)   |
| "X stays removed" tests (old commands, old keys, old file names)                                     | They test history, and they grow with every removal                | One test of the general rule: an unknown command, key or option is refused   |
| One case per instance of the same mistake (30 near-miss digits, 80 bad names)                        | The cases add run time and text and protect nothing more           | One case per *class* of input (§4.4)                                         |
| Calling private functions, counting calls, reading the source text                                   | They break on refactors and pass on real bugs                      | Drive the public entry point and observe the outcome                         |
| "Removing any single guard fails a test"                                                             | That is a mutation-testing goal, not a behaviour                   | Test each user-visible limit through the public entry point                  |
| One test file per issue or per task                                                                  | The next agent cannot find the existing test, so it writes another | One home per feature (§3)                                                    |
| Docstrings that restate the code, tell history, or claim checks the test does not make               | They explain nothing, and they mislead about what is protected     | A docstring that states the behaviour contract, every claim asserted, in agreement with the documentation (§4.2) |
| Tests that pass whatever happens (an assertion that cannot fail, a probe that any program satisfies) | False confidence                                                   | Make the test fail first (Q2)                                                |
| Tests built on a frozen copy of another repository's data                                            | They fail when the copy is refreshed, not when the code breaks     | Small fixtures written for the test; the other repository tests its own data |
| A real `sleep` or a real retry delay                                                                 | Minutes of waiting for nothing                                     | Inject the clock or the sleep                                                |

### 2.3 Measuring a whole suite

Watch these numbers over time. Every job that changes tests reports them before and after.

| Indicator | Healthy direction |
| --- | --- |
| Size of the test code compared with the source | Flat or falling. The test code rarely needs to be bigger than the code it tests |
| Wall time of the fast tier, run serially | Under 30 s |
| Number of hygiene violations ([tests_hygiene.md](tests_hygiene.md) §5) | Only ever falls |
| Temporary tests: how many, and whether each is linked to an open issue or job | Few, and every one linked |
| Behaviours tested more than once at the same layer (from reviews) | Falls |
| Dangerous workflows with no test (from reviews) | Falls. This matters more than any count |
| Disagreements between test docstrings and the official documentation (from reviews) | Zero. Each one found is resolved (§4.2) |

A falling test count is not a goal either. A suite can lose half its tests and gain protection.

## 3. Where tests live

```text
tests/
  conftest.py            suite-wide fixtures and the hygiene checks
  support/               helpers shared by several areas (fake machines, builders); never tests
  fixtures/<area>/       data files, small and written for the tests
  <area>/                one folder per real boundary of the code (a package or a program)
    conftest.py          the area's shared fixtures
    test_<feature>.py    one file per feature of that area
  _conditional/<name>/   tests that last while a stated condition holds, each folder with a condition and a date (tests_hygiene.md §2.1)
  _temp/<ref>/           temporary tests, each folder linked to an open issue or job (tests_hygiene.md §3)
  manual_smoke.md        checks that only a person on a real machine can run
```

Rules:

- **An area is one real boundary of the code:** a package, or a program, that has its own responsibility.
  - Areas mirror those boundaries, so the tests of a piece of code are found where the code is, and a change to one
    package touches one test area.
  - A boundary that exists only in the tests is not an area. Keep the areas few.
  - If no area fits some code, that says something about the code's structure. Raise it; do not invent an area.
- **A feature file is one behaviour of its area**, usually one module. Name it after what a user or caller sees:
  `test_install.py`, `test_checksum.py`, `test_launchers.py`.
- **Never name an area or a file after history:** an issue, a task, a review round, or a kind of hardening (`*_cases`,
  `*_security`, `*_hardening`, `*_issue_42`).
- **A new test goes into the existing file of its feature.** A new file needs a reason: no existing file fits. A new area
  needs the owner's agreement.
- **Test modules never import from each other.** Shared code goes into `support/` or a `conftest.py`.
- **Fixture data belongs to the repository that owns it.** Never copy another repository's real data into this one's
  tests. If the two must agree, that check runs in the other repository, or in the release tier against a real checkout
  of it.
- **Tests that need a person go into `manual_smoke.md`** as numbered entries, not into skipped tests.

## 4. Writing a test

### 4.1 Names

A test name is a statement of behaviour, in the present tense: `test_an_update_keeps_the_previous_version_until_the_new_one_works`.
Aim for under 70 characters, and do not go over 90. It names no issue, task or version.

### 4.2 Docstrings: the tests document the behaviour

Test docstrings are a specification of the product that a machine checks. Read together, the docstrings of a feature file
should be enough to rebuild the user documentation of that feature: what it does, its rules, its limits, its edge cases
and how it fails.

- **A module docstring** describes the feature as its user or caller sees it:
  - what the feature is for;
  - the rules it follows and the guarantees it gives;
  - what happens at the edges and on failure;
  - where the official documentation describes it (a link to the section);
  - in a last short paragraph, what the tests replace with fakes, if that is not obvious.
- **A test docstring** starts with a one-sentence summary of the behaviour. A paragraph follows when the behaviour needs
  it: the situation, what the user sees, and why it matters. It explains the behaviour in the product's terms, not the
  test's steps.
- **Every claim is asserted.**
  - A test docstring claims nothing its test does not check.
  - A module docstring claims nothing its file's tests do not check.
  - A rule that is documented but not tested is a gap: write the test, or move the sentence to the documentation.
- **A docstring agrees with the official documentation.** When they disagree, one of them is outdated. Find out which
  from the product's actual behaviour:
  - the product does what the test says: fix the documentation;
  - the product does what the documentation says: fix the test;
  - the product does neither: it is a bug, so report it.

  Never leave a disagreement in place. Drift between the two is the signal this rule exists for.
- **What a docstring never contains:**
  - how the code works internally;
  - a line-by-line restatement of the assertions;
  - history: the issue, decision, task or review that added the test, or what an old version did. Those belong in the
    commit message.

Detail is welcome when it explains behaviour. Length that only restates the code or tells history is the bloat this
policy removes.

### 4.3 Fixtures and helpers

- One fixture for one kind of world, defined once (in the area's `conftest.py`, or in `support/`). Do not copy it into
  each file.
- A helper may hide setup, but never the behaviour under test: the action and the assertion stay visible in the test.
- An autouse fixture that every test needs lives in the top `conftest.py`. Nothing else repeats it.

### 4.4 Parametrisation

- Parametrise over **classes** of input, not over instances of one class. Ten spellings of the same mistake are one
  case. A long list of hostile inputs is allowed only where each entry is a distinct attack, and then each one says
  which.
- Every case has a readable `id=`.
- If a parametrised test needs an `if` on its parameter, it is two tests.

### 4.5 Mocking

- Mock only real boundaries: the network, processes, the clock, the operating system, external services.
- Do not mock, patch or spy on the project's own functions. If a test can only observe something through a private
  function, the behaviour is either not observable (then document it, do not test it) or the design needs a seam.
- Prefer one fake of a boundary, shared by every test, to a mock per test. Test the fake against the real thing once
  (a contract test), so the two cannot drift.

### 4.6 Assertions

- Assert the outcome that a user or caller sees: the output, the files on disk, the exit code, the returned value, the
  error message a person reads.
- A user-visible message is asserted by its essential words, not byte for byte, unless the exact text is the contract
  (a machine-readable format, a documented message).
- Every assertion must be able to fail. Read it again after the test has failed once.

### 4.7 Cost

Cost is designed into a test when it is written and judged in review. It is not checked when the suite runs: a time
limit at run time would make results depend on the machine.

- **Do the least work that proves the behaviour.** Use the smallest fixture that shows the rule: two packages, not a
  copy of a hundred. Build only what the test reads.
- **Never wait for real time.** Inject the clock and the sleep. A retry delay or a timeout under test is a parameter set
  to zero.
- **Start a process only where the process is the behaviour.** Many cases that only need the logic go through one
  process, or none.
- **Share expensive setup that is read-only** (a built archive, a parsed document) at module or session scope. Share it
  only when no test changes it and no test depends on another's order.
- **The reviewer asks for each slow test:** what does it spend its time on, and does the behaviour need that?

### 4.8 Regression tests

A bug fix comes with a test that failed before the fix (Q2). But first look for the **existing test of that behaviour**.
Usually it should gain a case or an assertion, not a sibling. The test describes the behaviour, not the bug: name it after
what must hold, not after what went wrong or who found it.

## 5. Tiers

A tier is defined by **what a test touches**, never by how long it happens to take. So a test's tier can be checked by a
machine, and a test cannot drift into the wrong tier unnoticed.

| Tier | What a test in it may touch | Runs |
| --- | --- | --- |
| `unit` | Only the process itself: fakes of every boundary, small temporary files. No child process, real sleep, network, or system-wide state | After every edit |
| `process` | Real child processes, real operating-system objects (links, junctions, registry keys under a test name), loopback servers | Before saying a change is done |
| `release` | Builds, unpacks or verifies the shipped product, or runs another repository's checks against this one | Before a commit to the main branch, a merge or a release; after changing the build, install or self-update |
| manual | A person on a real machine | Before a release, as `manual_smoke.md` lists |

Rules:

- **Every test module declares its tier.** A module without a tier is an error.
- **In the `unit` tier, starting a process, sleeping or opening a socket fails the test** with a message naming the tier.
- **Speed is a design concern, not a run-time check** (§4.7).
  - No test fails for being slow.
  - Each tier's serial wall time and its slowest tests are reported (§2.3), so that slow tests are found and redesigned.
- **Report which tiers ran and their counts.** "Tests pass" without the tier names means nothing.
- **Parallel runs are allowed, as a convenience.** They are never the reason a tier is fast: its time is measured
  serially.

## 6. Testing work done by agents

Agents add tests easily and almost never remove them. They test whatever they just touched, at whatever layer they
touched it. These rules counter that:

- **Search before adding.** Find the existing test of the behaviour (by feature file, then by searching for the user-visible
  words). Extend it, or say why it cannot be extended.
- **Start from the behaviour contract, not the implementation.** Do not invent expected behaviour. Do not write tests to
  raise coverage. Do not preserve accidental behaviour.
- **Report what you added and what you removed,** with the tier and the count.
- **A finding from a review round is a case for the owner test,** not a new test named after the round.
- **Removing code removes its tests** (and their fixtures and helpers) in the same change.

## 7. This project (gupkg)

- **Areas:** one per real package of the code. A new area needs the owner's agreement.

  | Area | Code it covers |
  | --- | --- |
  | `model` | `src/gupkg/model/`, plus `tomlio`, `pkgcheck`, `authoring`, `pinning` (package definitions: format, checking, writing) |
  | `system` | `src/gupkg/system/`: the `System` port, `FakeSystem`, `WindowsSystem`, the regex worker process |
  | `services` | `src/gupkg/services/` (http, checksum, track, unpack, hooks, populate, prepare, regexrun), plus `tls`, `redact`, `hooklib`, `integrity`, `extractors` |
  | `catalog` | `src/gupkg/catalog/`, plus `targets`, `scope`, `settingsload`, `settingsedit`, `viewdata` (what is installed where, under which root and settings) |
  | `engine` | `src/gupkg/engine/`, plus `distcopy` |
  | `report` | `src/gupkg/report/`: console output, events, logs, result documents |
  | `self` | gupkg itself: `src/gupkg/selfmgmt/`, `src/gupkg/launchers/`, `bootstrap`, the shims, the bundled runtime, self-update |
  | `cli` | `src/gupkg/cli.py` |
  | `ui` | `gupkg_ui`, `gupkg_tui`, `gupkg_manager` |
  | `registry` | `gupkg_registry` (the registry tool) |
  | `project` | What is not in `src/`: `tools/`, `scripts/`, the release build, the documentation |

- **Boundaries:** the `System` port (`src/gupkg/system/port.py`). Its fake is `FakeSystem`; the contract tests run the same
  checks against `FakeSystem` and the real `WindowsSystem`. Inject `sleep` and the clock through the engine context and
  the HTTP client.
- **Commands:**

  | Command | What it runs |
  | --- | --- |
  | `scripts\test.cmd` | `unit` |
  | `scripts\test.cmd --process` | `unit` and `process` |
  | `scripts\test.cmd --all` | every tier |

  Set `GUPKG_NO_PAUSE=1` when a script or an agent runs them.
- **Release tier extras:** the registry's own tests (`gupkg-registry\_scripts\test.cmd`) run against this checkout. The
  registry's data is never copied into `tests/fixtures/`.
- **Manual checks:** `tests/manual_smoke.md`, with entries numbered `<issue>-<task>-<n>`.
- **Machine safety** (`AGENTS.md`): no test touches `C:\opt`, the user's real roots, the real `PATH`, the Start Menu or
  the Windows registry outside a `gupkg-test-*` key.
- **Status (2026-10-30):**
  - The suite is still flat in `tests/` and has no tiers; job 10 (`.agents/jobs/10-test-hygiene-controller.md`) migrates
    it.
  - Until then, legacy modules are recorded in the hygiene baseline ([tests_hygiene.md](tests_hygiene.md) §5).
  - New tests follow this policy from today.
