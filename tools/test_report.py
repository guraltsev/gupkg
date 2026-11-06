"""Print the indicators of the test suite (``_docs/tests.md`` section 2.3), or its docstrings as a specification.

This is a maintainer tool for the development tree; it is never shipped. It reads files and changes none.

Usage and API
-------------
``python tools/test_report.py [--json] [--timings JUNIT.XML] [--root DIR] [--no-collect]`` prints:

* size: test files, lines, characters and estimated tokens (1 token per 4 characters) of ``tests/**/*.py`` and of the
  source ``src/**/*.py`` (without ``src/gupkg/python/``), and their ratio;
* shape: test functions, collected cases (``pytest --collect-only -q``), the median and maximum length of test names
  and the share of docstrings in the test text;
* layout: test files still flat in ``tests/`` against files in ``tests/<area>/``, and the folders of ``tests/_temp/``
  with their ``ref`` and whether that reference is open;
* hygiene: the number of violation lines in ``tests/hygiene_baseline.txt`` (comment lines are not counted);
* time (with ``--timings``): the total, the time per tier, the 10 slowest tests and the share of the slowest 200.

``python tools/test_report.py --spec [AREA]`` prints the module and test docstrings of one area folder (or one test
file), or of every test file, as Markdown without code. ``report(root)``, ``timings(path)`` and ``spec(root, area)``
are the same results as functions.

Implementation Approach
-----------------------
The files are parsed with ``ast``, never imported. Only ``--collect-only`` starts a child process (pytest, in ``root``,
without a cache directory). The tool imports nothing from ``gupkg`` and uses no network. A tier is taken from a
``tier`` property on the test cases of the JUnit file; without one the report says "no tiers".
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Folders of tests/ that hold no feature files of an area.
NON_AREA_DIRS = {"_temp", "_conditional", "support", "fixtures", "__pycache__"}


def report(root: Path, collect: bool = True) -> dict:
    """Return the indicators of the suite below *root* as a dictionary.

    Parameters
    ----------
    root : Path
        Repository root that holds ``tests/`` and ``src/``.
    collect : bool, default=True
        Whether to run ``pytest --collect-only`` to count cases.

    Returns
    -------
    dict
        The ``size``, ``shape``, ``layout`` and ``hygiene`` indicators.
    """
    # Measure the test and source trees.
    tests_dir = root / "tests"
    test_files = _py_files(tests_dir)
    source_files = _py_files(root / "src", skip=(root / "src" / "gupkg" / "python",))
    tests_size, source_size = _size(test_files), _size(source_files)

    # Count test names and docstring characters over every parsable test file.
    names: list[str] = []
    docstring_chars = 0
    for path in test_files:
        tree = _parse(path)
        if tree is None:
            continue
        docstring_chars += _docstring_chars(tree)
        if path.name.startswith("test_"):
            names.extend(node.name for node in _test_functions(tree))
    lengths = [len(n) for n in names]

    # Layout: files still flat in tests/ against files in area folders.
    flat = [p for p in tests_dir.glob("test_*.py")]
    in_area = [
        p
        for p in tests_dir.glob("*/**/test_*.py")
        if p.relative_to(tests_dir).parts[0] not in NON_AREA_DIRS and "__pycache__" not in p.parts
    ]
    # Hygiene: count the violation lines of the baseline, if there is one.
    baseline = tests_dir / "hygiene_baseline.txt"
    baseline_lines = None
    if baseline.is_file():
        baseline_lines = sum(
            1 for line in baseline.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")
        )

    # Assemble the indicators into the report dictionary.
    return {
        "size": {
            "tests": tests_size,
            "source": source_size,
            "ratio_chars": round(tests_size["chars"] / source_size["chars"], 2) if source_size["chars"] else None,
        },
        # Shape: how many tests, cases and how long their names are.
        "shape": {
            "test_functions": len(names),
            "collected_cases": _collect_count(root) if collect else None,
            "name_length_median": statistics.median(lengths) if lengths else 0,
            "name_length_max": max(lengths, default=0),
            "docstring_share": round(docstring_chars / tests_size["chars"], 3) if tests_size["chars"] else 0,
        },
        # Layout and hygiene are small counts taken above.
        "layout": {"flat_files": len(flat), "area_files": len(in_area), "temp_folders": _temp_folders(root)},
        "hygiene": {"baseline_lines": baseline_lines},
    }


def timings(junit: Path) -> dict:
    """Return the total, per-tier and slowest-test times of a JUnit XML file written by ``pytest --junitxml``.

    Parameters
    ----------
    junit : Path
        JUnit XML file whose test cases carry optional ``tier`` properties.

    Returns
    -------
    dict
        Test count, total seconds, per-tier figures (``None`` without tiers) and the slowest tests.
    """
    # Read every test case with its time and optional tier.
    cases = []
    for case in ET.parse(junit).getroot().iter("testcase"):
        tier = None
        for prop in case.iter("property"):
            if prop.get("name") == "tier":
                tier = prop.get("value")
        cases.append((f"{case.get('classname', '')}::{case.get('name', '')}", float(case.get("time", 0)), tier))
    # Slowest first; the total and the per-tier sums come from the same list.
    cases.sort(key=lambda c: c[1], reverse=True)
    total = sum(c[1] for c in cases)
    per_tier: dict[str, dict] = {}
    for _, seconds, tier in cases:
        if tier:
            entry = per_tier.setdefault(tier, {"tests": 0, "seconds": 0.0})
            entry["tests"] += 1
            entry["seconds"] += seconds
    # Summarise the slowest tests and the share of the total they take.
    top200 = sum(c[1] for c in cases[:200])
    return {
        "tests": len(cases),
        "total_seconds": round(total, 1),
        "tiers": {k: {"tests": v["tests"], "seconds": round(v["seconds"], 1)} for k, v in per_tier.items()} or None,
        "slowest": [{"test": c[0], "seconds": round(c[1], 2)} for c in cases[:10]],
        "slowest_200_seconds": round(top200, 1),
        "slowest_200_share": round(top200 / total, 3) if total else 0,
    }


def spec(root: Path, area: str | None = None) -> str:
    """Return the docstrings of the test files of *area* (a folder of ``tests/`` or a file; all if ``None``) as Markdown.

    Parameters
    ----------
    root : Path
        Repository root that holds ``tests/``.
    area : str, optional
        Folder of ``tests/`` or a test file; every test file when omitted.

    Returns
    -------
    str
        Markdown with one section per test file and one per test.
    """
    # Choose the files: the named area (or file), or every test file outside ``_temp``.
    tests_dir = root / "tests"
    if area:
        target = tests_dir / area
        if not target.exists() and (root / area).exists():
            target = root / area
        files = [target] if target.is_file() else sorted(target.rglob("test_*.py"))
    else:
        files = sorted(p for p in tests_dir.rglob("test_*.py") if "_temp" not in p.relative_to(tests_dir).parts)
    # Emit the module docstring, then the docstring of each test, per file.
    out: list[str] = []
    for path in files:
        if "__pycache__" in path.parts:
            continue
        # Files that do not parse are left out of the specification.
        tree = _parse(path)
        if tree is None:
            continue
        out.append(f"## {path.relative_to(root).as_posix()}\n")
        out.append((ast.get_docstring(tree) or "*(no module docstring)*") + "\n")
        for node in _test_functions(tree):
            out.append(f"### {node.name}\n")
            out.append((ast.get_docstring(node) or "*(no docstring)*") + "\n")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return the process status.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments; ``sys.argv[1:]`` when omitted.

    Returns
    -------
    int
        Always ``0``; the report is informational.
    """
    # Describe the options of the report and the specification modes.
    parser = argparse.ArgumentParser(description="Print the indicators of the test suite, or its docstrings.")
    parser.add_argument("--json", action="store_true", help="print the indicators as JSON")
    parser.add_argument("--timings", type=Path, metavar="JUNIT.XML", help="add the time report from a JUnit XML file")
    parser.add_argument("--spec", nargs="?", const="", metavar="AREA", help="print the docstrings of AREA (or all) as Markdown")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="repository root")
    parser.add_argument("--no-collect", action="store_true", help="skip pytest --collect-only")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    # ``--spec`` prints the docstrings as UTF-8 Markdown and nothing else.
    if args.spec is not None:
        sys.stdout.reconfigure(encoding="utf-8")
        print(spec(root, args.spec or None))
        return 0
    # Otherwise print the indicators, with the time report when a JUnit file was given.
    data = report(root, collect=not args.no_collect)
    if args.timings:
        data["time"] = timings(args.timings)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    _print_report(data)
    if args.timings:
        _print_timings(data["time"])
    return 0


# --- supporting helpers ---


def _py_files(folder: Path, skip: tuple[Path, ...] = ()) -> list[Path]:
    """Return the ``.py`` files below *folder* (sorted), without caches and the *skip* folders."""
    found = []
    for path in sorted(folder.rglob("*.py")):
        if "__pycache__" in path.parts or any(s in path.parents for s in skip):
            continue
        found.append(path)
    return found


def _size(files: list[Path]) -> dict:
    """Count files, lines, characters and estimated tokens of *files*."""
    lines = chars = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines += len(text.splitlines())
        chars += len(text)
    # One token is estimated as four characters.
    return {"files": len(files), "lines": lines, "chars": chars, "tokens": chars // 4}


def _parse(path: Path) -> ast.Module | None:
    """Parse *path*; a file that does not parse yields ``None``."""
    try:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None


def _test_functions(tree: ast.Module) -> list[ast.AST]:
    """Return the ``test_*`` functions of a module, including methods of ``Test*`` classes."""
    found: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            found.append(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                    found.append(item)
    return found


def _docstring_chars(tree: ast.Module) -> int:
    """Return the number of characters in the docstrings of a module, its classes and its functions."""
    total = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            total += len(ast.get_docstring(node, clean=False) or "")
    return total


def _collect_count(root: Path) -> int | None:
    """Return the number of cases ``pytest --collect-only -q`` finds in *root*, or ``None`` if it cannot tell."""
    command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", str(root / "tests")]
    try:
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    summary = re.search(r"(\d+)(?:/\d+)? tests? (?:collected|selected)", done.stdout)
    if summary:
        return int(summary.group(1))
    # Without a summary line (-q prints one id per line) count the node ids.
    ids = [line for line in done.stdout.splitlines() if "::" in line and not line.startswith(" ")]
    return len(ids) if ids else None


def _ref_is_open(root: Path, ref: str) -> bool | None:
    """Return whether the issue or job *ref* is open, or ``None`` if it names neither."""
    issue = re.fullmatch(r"issue(\d+)", ref)
    if issue:
        return bool(list((root / "_issues").glob(f"issue{issue.group(1)}-*.md")))
    job = re.fullmatch(r"job0*(\d+)", ref)
    if job:
        progress = list((root / ".agents" / "jobs").glob(f"{int(job.group(1)):02d}-*-progress.md"))
        if not progress:
            return False
        return not re.search(r"^\s*Status:\s*Closed", progress[0].read_text(encoding="utf-8"), re.M | re.I)
    return None


def _temp_folders(root: Path) -> list[dict]:
    """Describe the folders of ``tests/_temp/``: name, the ``ref:`` of the README and whether it is open."""
    folders = []
    temp = root / "tests" / "_temp"
    if not temp.is_dir():
        return folders
    # Read the optional ``ref:`` line of each folder's README and look up its state.
    for folder in sorted(p for p in temp.iterdir() if p.is_dir() and p.name != "__pycache__"):
        readme = folder / "README.md"
        ref = None
        if readme.is_file():
            found = re.search(r"^\s*(?:[-*]\s*)?`?ref:?`?:?\s*(.+?)\s*$", readme.read_text(encoding="utf-8"), re.M)
            ref = found.group(1) if found else None
        folders.append({"folder": folder.name, "ref": ref, "open": _ref_is_open(root, folder.name)})
    return folders


def _print_report(data: dict) -> None:
    """Print *data* from ``report`` as readable text."""
    size, shape, layout = data["size"], data["shape"], data["layout"]
    # Size and shape of the suite.
    print("Size")
    for label in ("tests", "source"):
        s = size[label]
        print(f"  {label:7} {s['files']:5} files {s['lines']:8} lines {s['chars']:9} chars  ~{s['tokens']} tokens")
    print(f"  ratio tests/source (chars): {size['ratio_chars']}")
    print("Shape")
    print(f"  test functions:        {shape['test_functions']}")
    print(f"  collected cases:       {shape['collected_cases']}")
    print(f"  test name length:      median {shape['name_length_median']}, max {shape['name_length_max']}")
    print(f"  docstring share:       {shape['docstring_share'] * 100:.1f}% of the test text")
    # Layout of the test files and the state of the temporary-test folders.
    print("Layout")
    print(f"  flat in tests/:        {layout['flat_files']} files")
    print(f"  in tests/<area>/:      {layout['area_files']} files")
    if not layout["temp_folders"]:
        print("  tests/_temp/:          no folders")
    for f in layout["temp_folders"]:
        state = {True: "open", False: "CLOSED or unknown", None: "unrecognised ref"}[f["open"]]
        print(f"  tests/_temp/{f['folder']}: ref {f['ref']} ({state})")
    # Hygiene closes the report.
    base = data["hygiene"]["baseline_lines"]
    print("Hygiene")
    print(f"  baseline lines:        {'no baseline file' if base is None else base}")


def _print_timings(data: dict) -> None:
    """Print *data* from ``timings`` as readable text."""
    # Totals and per-tier times, then the slowest tests.
    print("Time")
    print(f"  {data['tests']} tests, total {data['total_seconds']} s")
    if data["tiers"]:
        for tier, v in data["tiers"].items():
            print(f"  tier {tier}: {v['tests']} tests, {v['seconds']} s")
    else:
        print("  per tier: no tiers")
    # The ten slowest tests, then how much the slowest 200 contribute.
    print("  10 slowest:")
    for item in data["slowest"]:
        print(f"    {item['seconds']:8.2f} s  {item['test']}")
    print(f"  slowest 200: {data['slowest_200_seconds']} s = {data['slowest_200_share'] * 100:.1f}% of the total")


if __name__ == "__main__":
    raise SystemExit(main())
