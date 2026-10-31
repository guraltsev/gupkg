# T00: A tool that prints the suite's indicators

Issue 036 §2, §5 (indicators); `_docs/tests.md` §2.3; `_docs/tests_hygiene.md` §6, §8.

## Goal

`tools\test_report.py` prints the indicators of `tests.md` §2.3, so every later task can paste a before-and-after.
Nothing in the suite changes.

## Add

`tools/test_report.py`. Run as `.venv\Scripts\python tools\test_report.py [--json] [--timings <junit.xml>]`, it prints:

- **Size:** test files, lines and characters (with an estimate of 1 token per 4 characters) for `tests/**/*.py`, and the
  same for the source (`src/**/*.py`, excluding `src/gupkg/python/`), plus the ratio of the two.
- **Shape:**
  - test functions, and collected cases (from `pytest --collect-only -q`);
  - the median and maximum length of test names;
  - the share of docstrings in the test text.
- **Layout:**
  - files still flat in `tests/` compared with files in `tests/<area>/`;
  - folders in `tests/_temp/`, each with its `ref`, and whether that ref is open.
- **Hygiene:** the number of lines in `tests/hygiene_baseline.txt`, if the file exists.
- **Time:** with `--timings`, read a JUnit XML file (`pytest --junitxml`) and print:
  - the total time;
  - the time per tier, once tiers exist (until then, "no tiers");
  - the 10 slowest tests;
  - the share of the total taken by the slowest 200 tests.

- **Specification view:** with `--spec [<area>]`, print the module and test docstrings of the area (or of every area) as
  Markdown, one heading per feature file and one entry per test, without code. Docstrings are the behaviour
  documentation (`tests.md` §4.2), and this view lets a reviewer compare them with `README.md` and `_docs/`, and lets an
  agent read the behaviour without reading the tests' code.

The tool imports nothing from `gupkg`, needs no network and changes no file.

Add a short test of the tool in the release tier: it runs on a small fake tree under `tmp_path` and checks the counts.
Put the test in `tests/project/test_test_report.py`; tiers come only in T02, so until then the file has no marker.

## Done when

- [ ] `.venv\Scripts\python tools\test_report.py` prints every item above on today's `main`, and the output is in your log.
- [ ] With `--timings` on a JUnit file from a full run, it prints the slowest tests and the share of the slowest 200. The
      figures roughly match issue 036 §2.
- [ ] `--spec` prints the docstrings of one existing test file as Markdown; paste a short excerpt.
- [ ] `_docs/tests_hygiene.md` §8 needs no change (it already names the tool). If anything differs, report it.
- [ ] The full suite passes.
