"""The automatic test-hygiene checks, run as a pytest plugin with every session.

The rules are in ``_docs/tests_hygiene.md`` section 6; the debt from before the policy is in
``tests/hygiene_baseline.txt`` (section 5). A violation is named ``rule|file|test``: ``file`` is relative to the
repository root with ``/`` separators, and ``test`` is empty for a rule about the whole module.

``H1`` no import of another test module or of ``tests._temp``.
``H2`` no import of a private name from the product, and no read of ``<product module>._name``.
``H3`` no issue, decision, task, review or old-version reference in a permanent test's name or docstring.
``H4`` every parametrised case has an explicit ``id``.
``H5`` every permanent test module and test function has a docstring that starts with a one-sentence summary.
``H6`` every ``tests/_temp/<ref>/`` has a complete README, and its ``ref`` is open.
``H7`` every ``tests/_conditional/<name>/`` has a complete README, and its ``expires`` date has not passed.

The files are read with ``ast`` and never imported.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

BASELINE_NAME = "hygiene_baseline.txt"

PRODUCT_PACKAGES = frozenset({"gupkg", "gupkg_ui", "gupkg_tui", "gupkg_manager", "gupkg_registry"})

# What each rule is, and where the policy explains it.
RULES = {
    "H1": ("a test module imports another test module or tests._temp", "_docs/tests.md section 3; _docs/tests_hygiene.md section 3.2"),
    "H2": ("a test uses a private name of the product", "_docs/tests.md section 4.5"),
    "H3": ("a permanent test's name or docstring refers to an issue, decision, task, review or old version", "_docs/tests.md sections 4.1 and 4.2"),
    "H4": ("a parametrised case has no explicit id", "_docs/tests.md section 4.4"),
    "H5": ("a permanent test module or test has no docstring that starts with a one-sentence summary", "_docs/tests.md section 4.2"),
    "H6": ("a temporary-test folder breaks its rules", "_docs/tests_hygiene.md sections 3.2, 3.3 and 8"),
    "H7": ("a conditional-test folder breaks its rules or has expired", "_docs/tests_hygiene.md sections 2.1 and 6"),
}

_HISTORY = (
    r"issue ?#?\d+",
    r"\bD\d{2}\b",
    r"\bT\d{2}\b",
    r"round \d",
    r"reviewer",
    r"(?<![\d.])0\.1[0-9]\b",
    r"\bQ\d+\b",
)
# Names are matched without case, with ``_`` read as a space: ``test_issue_99_fix`` says "issue 99".
_HISTORY_IN_NAME = re.compile("|".join(_HISTORY), re.IGNORECASE)
_HISTORY_IN_TEXT = re.compile("|".join(f"(?i:{p})" if p.startswith(("issue", "round", "reviewer")) else p for p in _HISTORY))

_NAMEDTUPLE_API = frozenset({"_asdict", "_replace", "_fields", "_make", "_field_defaults"})
_ABBREVIATIONS = re.compile(r"\b(?:e\.g|i\.e|etc|vs|cf|approx)\.", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class Violation:
    """One broken rule: where it is, and what exactly is wrong."""

    rule: str
    file: str
    test: str
    detail: str = ""

    @property
    def key(self) -> str:
        """The line that stands for this violation in the baseline."""
        return f"{self.rule}|{self.file}|{self.test}"

    def describe(self) -> str:
        """A line for a person: rule, file, test, what is wrong and where the policy is."""
        where = f"{self.file}::{self.test}" if self.test else self.file
        what, policy = RULES[self.rule]
        detail = f" ({self.detail})" if self.detail else ""
        return f"  {self.rule} {where}: {what}{detail}. Policy: {policy}."


# ---------------------------------------------------------------------------
# Module rules H1 to H5
# ---------------------------------------------------------------------------

def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _dotted(node: ast.AST) -> str | None:
    """Return ``a.b.c`` for a chain of names and attributes, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _tests_of(tree: ast.Module):
    """Yield ``(display name, node)`` for each test function and each test method of a ``Test*`` class."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name.startswith("test"):
                    yield f"{node.name}::{member.name}", member


def _import_targets(node: ast.stmt):
    """Yield the dotted module paths an import statement names (the module itself and each imported name)."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name.split(".")
    elif isinstance(node, ast.ImportFrom):
        module = node.module.split(".") if node.module else []
        yield module
        for alias in node.names:
            yield [*module, alias.name]


def _h1(tree: ast.Module, file: str, test_stems: frozenset[str]) -> list[Violation]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        relative = isinstance(node, ast.ImportFrom) and node.level > 0
        for parts in _import_targets(node):
            if not parts:
                continue
            local = relative or parts[0] == "tests" or parts[0] in test_stems
            if local and any(part in test_stems or part in ("_temp", "_conditional") for part in parts):
                found.append(".".join(parts))
                break
    if found:
        return [Violation("H1", file, "", "imports " + ", ".join(sorted(set(found))))]
    return []


def _product_aliases(tree: ast.Module) -> set[str]:
    """Names bound by an import to a product module (or to the product package itself)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PRODUCT_PACKAGES:
                    names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in PRODUCT_PACKAGES:
                for alias in node.names:
                    # Lower-case names are modules or functions; CamelCase names are classes, whose
                    # ``_asdict`` and the like are public.
                    if alias.name[:1].islower():
                        names.add(alias.asname or alias.name)
    return names


def _private_target(text: str) -> bool:
    parts = text.split(".")
    return parts[0] in PRODUCT_PACKAGES and any(_is_private(part) for part in parts[1:])


def _h2(tree: ast.Module, file: str) -> list[Violation]:
    found: set[str] = set()
    aliases = _product_aliases(tree) | PRODUCT_PACKAGES
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in PRODUCT_PACKAGES:
                found.update(f"{node.module}.{a.name}" for a in node.names if _is_private(a.name))
        elif isinstance(node, ast.Attribute) and _is_private(node.attr) and node.attr not in _NAMEDTUPLE_API:
            base = _dotted(node.value)
            if base is not None and base.split(".")[0] in aliases:
                found.add(f"{base}.{node.attr}")
        elif isinstance(node, ast.Call) and node.args:
            callee = (_dotted(node.func) or "").split(".")[-1]
            first = node.args[0]
            if callee in {"setattr", "getattr", "delattr", "patch", "object"}:
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and _private_target(first.value):
                    found.add(first.value)
                elif len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    base = _dotted(first)
                    if base and base.split(".")[0] in aliases and _is_private(node.args[1].value):
                        found.add(f"{base}.{node.args[1].value}")
    if found:
        return [Violation("H2", file, "", "uses " + ", ".join(sorted(found)))]
    return []


def _h3(tree: ast.Module, file: str) -> list[Violation]:
    found = []
    for name, node in _tests_of(tree):
        words = name.split("::")[-1].replace("_", " ")
        doc = ast.get_docstring(node) or ""
        hit = _HISTORY_IN_NAME.search(words) or _HISTORY_IN_TEXT.search(doc)
        if hit:
            found.append(Violation("H3", file, name, f"mentions {hit.group(0)!r}"))
    return found


def _is_param_with_id(node: ast.AST) -> bool:
    if isinstance(node, ast.Starred):
        return False
    return (
        isinstance(node, ast.Call)
        and (_dotted(node.func) or "").split(".")[-1] == "param"
        and any(keyword.arg == "id" for keyword in node.keywords)
    )


def _values_have_ids(node: ast.AST, env: dict[str, ast.AST], depth: int = 0) -> bool:
    """Say whether every value in a parametrisation is a ``pytest.param(..., id=...)``."""
    if depth > 5:
        return False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(
            _values_have_ids(item.value, env, depth + 1) if isinstance(item, ast.Starred) else _is_param_with_id(item)
            for item in node.elts
        )
    if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        return _is_param_with_id(node.elt)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _values_have_ids(node.left, env, depth + 1) and _values_have_ids(node.right, env, depth + 1)
    if isinstance(node, ast.Name) and node.id in env:
        return _values_have_ids(env[node.id], env, depth + 1)
    return False


def _parametrisation_without_ids(call: ast.Call, env: dict[str, ast.AST]) -> bool:
    callee = _dotted(call.func) or ""
    last = callee.split(".")[-1]
    if last == "parametrize":
        values = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "argvalues"), None)
    elif last == "fixture":
        values = next((k.value for k in call.keywords if k.arg == "params"), None)
        if values is None:
            return False
    else:
        return False
    if any(keyword.arg == "ids" for keyword in call.keywords):
        return False
    return values is None or not _values_have_ids(values, env)


def _h4(tree: ast.Module, file: str) -> list[Violation]:
    env = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def decorators(node: ast.AST):
        return [d for d in getattr(node, "decorator_list", []) if isinstance(d, ast.Call)]

    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            calls = [n for n in ast.walk(node.value) if isinstance(n, ast.Call)]
            if any(_parametrisation_without_ids(call, env) for call in calls):
                found.append(Violation("H4", file, "", "pytestmark parametrisation without ids"))
        if isinstance(node, ast.ClassDef) and any(_parametrisation_without_ids(d, env) for d in decorators(node)):
            found.append(Violation("H4", file, node.name, "parametrisation without ids"))
        scopes = [node] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        if isinstance(node, ast.ClassDef):
            scopes = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for function in scopes:
            if any(_parametrisation_without_ids(d, env) for d in decorators(function)):
                prefix = f"{node.name}::" if isinstance(node, ast.ClassDef) else ""
                found.append(Violation("H4", file, f"{prefix}{function.name}", "parametrisation without ids"))
    return found


def _summary_problem(doc: str | None) -> str | None:
    """Return what is wrong with a docstring's opening, or ``None`` if it is a one-sentence summary."""
    if not doc or not doc.strip():
        return "no docstring"
    first_paragraph = " ".join(doc.strip().split("\n\n")[0].split())
    text = re.sub(r"`[^`]*`", "x", first_paragraph)
    text = _ABBREVIATIONS.sub("x", text)
    if not text.endswith((".", "?", "!")):
        return "the first paragraph does not end with a full stop"
    if re.search(r"[.?!]\s+\S", text):
        return "the first paragraph has more than one sentence"
    return None


def _h5(tree: ast.Module, file: str) -> list[Violation]:
    found = []
    problem = _summary_problem(ast.get_docstring(tree))
    if problem:
        found.append(Violation("H5", file, "", f"module: {problem}"))
    for name, node in _tests_of(tree):
        problem = _summary_problem(ast.get_docstring(node))
        if problem:
            found.append(Violation("H5", file, name, problem))
    return found


def _test_stems(root: Path) -> frozenset[str]:
    """Return the names of the test modules below ``tests/``, such as ``test_install``."""
    return frozenset(p.stem for p in (root / "tests").rglob("test_*.py"))


def scan_module(path: Path, root: Path, test_stems: frozenset[str] | None = None) -> list[Violation]:
    """Return the violations of H1 to H5 in one permanent test module."""
    stems = _test_stems(root) if test_stems is None else test_stems
    file = _relative(path, root)
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return [*_h1(tree, file, stems), *_h2(tree, file), *_h3(tree, file), *_h4(tree, file), *_h5(tree, file)]


# ---------------------------------------------------------------------------
# Temporary folders, rule H6
# ---------------------------------------------------------------------------

_README_KEYS = ("ref", "kind", "purpose", "fate")


def temp_folders(root: Path) -> list[Path]:
    """Return the ``tests/_temp/<ref>/`` folders."""
    base = root / "tests" / "_temp"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and p.name != "__pycache__")


def parse_readme(folder: Path) -> dict[str, str] | None:
    """Return the ``ref``, ``kind``, ``purpose`` and ``fate`` entries of a folder's README, or ``None`` if it has none."""
    readme = folder / "README.md"
    if not readme.is_file():
        return None
    entries: dict[str, list[str]] = {}
    current = None
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?\**(ref|kind|purpose|fate|until|expires)\**\s*:\**\s*(.*)$", line, re.IGNORECASE)
        if match:
            current = match.group(1).lower()
            entries.setdefault(current, []).append(match.group(2))
        elif current and line.strip() and not line.startswith("#"):
            entries[current].append(line.strip())
    return {key: " ".join(v for v in values if v).strip() for key, values in entries.items()}


def readme_kind(folder: Path) -> str | None:
    """Return a folder's ``kind`` (``guard`` or ``report``), or ``None`` if the README does not say."""
    entries = parse_readme(folder)
    kind = (entries or {}).get("kind", "").strip("` ").lower()
    return kind or None


def _ref_state(root: Path, folder_name: str, ref: str) -> str | None:
    """Return why a reference is not open, or ``None`` if it is."""
    ref_path = root / ref.strip("` ")
    if not ref.strip("` ") or not ref_path.is_file():
        return f"ref {ref!r} is not a file; the issue or job is closed or the path is wrong"
    issue = re.fullmatch(r"issue(\d+)", folder_name)
    job = re.fullmatch(r"job(\d+)", folder_name)
    if issue:
        if not ref_path.name.startswith(f"issue{issue.group(1)}-") and ref_path.stem != f"issue{issue.group(1)}":
            return f"ref {ref!r} is not the file of {folder_name}"
        if "_closed" in ref_path.parts:
            return f"{folder_name} is closed (its file is in _issues/_closed)"
    elif job:
        number = job.group(1)
        if not ref_path.name.startswith(f"{number}-"):
            return f"ref {ref!r} is not the controller file of {folder_name}"
        if "_complete" in ref_path.parts:
            return f"{folder_name} is closed (its file is in _complete)"
        for progress in ref_path.parent.glob(f"{number}-*-progress.md"):
            if re.search(r"^Status:\s*Closed", progress.read_text(encoding="utf-8"), re.MULTILINE):
                return f"{folder_name} is closed (its progress file says Status: Closed)"
    else:
        return "the folder is not named issueNNN or jobNN"
    return None


def scan_temp(root: Path) -> list[Violation]:
    """Return the violations of H6 in every ``tests/_temp/<ref>/`` folder."""
    found = []
    for folder in temp_folders(root):
        file = _relative(folder, root)
        entries = parse_readme(folder)
        if entries is None:
            found.append(Violation("H6", file, "", "no README.md"))
            continue
        missing = [key for key in _README_KEYS if not entries.get(key)]
        if missing:
            found.append(Violation("H6", file, "", "README lacks " + ", ".join(missing)))
            continue
        if entries["kind"].strip("` ").lower() not in {"guard", "report"}:
            found.append(Violation("H6", file, "", "kind must be guard or report"))
            continue
        why = _ref_state(root, folder.name, entries["ref"])
        if why:
            found.append(Violation("H6", file, "", why))
    return found


_CONDITIONAL_KEYS = ("until", "expires", "purpose")


def scan_conditional(root: Path, today: date) -> list[Violation]:
    """Return the violations of H7 in every ``tests/_conditional/<name>/`` folder.

    A folder needs a README with ``until`` (the condition that ends the tests), ``expires`` (a ``YYYY-MM-DD`` date by which the
    condition is checked again) and ``purpose``. A date before *today* fails the session until someone confirms the condition
    and moves the date, or removes the tests.
    """
    base = root / "tests" / "_conditional"
    if not base.is_dir():
        return []
    found = []
    for folder in sorted(p for p in base.iterdir() if p.is_dir() and p.name != "__pycache__"):
        file = _relative(folder, root)
        entries = parse_readme(folder)
        if entries is None:
            found.append(Violation("H7", file, "", "no README.md"))
            continue
        missing = [key for key in _CONDITIONAL_KEYS if not entries.get(key)]
        if missing:
            found.append(Violation("H7", file, "", "README lacks " + ", ".join(missing)))
            continue
        try:
            expires = date.fromisoformat(entries["expires"].strip("` "))
        except ValueError:
            found.append(Violation("H7", file, "", "expires must be a date written YYYY-MM-DD"))
            continue
        if expires < today:
            found.append(Violation("H7", file, "", f"expired on {expires}: confirm the condition and set a later date, or retire the tests"))
    return found


# ---------------------------------------------------------------------------
# The baseline and the verdict
# ---------------------------------------------------------------------------

def load_baseline(path: Path) -> set[str]:
    """Return the violation keys listed in a baseline file (none if the file does not exist)."""
    if not path.is_file():
        return set()
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line for line in lines if line and not line.startswith("#")}


def check(root: Path, modules: list[Path], baseline: Path | None = None, today: date | None = None) -> str | None:
    """Check *modules* (permanent and conditional test modules), the temporary and the conditional folders of *root*; return the failure text, or ``None``.

    *today* is the date the ``expires`` of a conditional folder is compared with (the current date by default).

    Only the baseline lines of the files that were checked, and of files that no longer exist, can be stale, so a run of
    one file does not report the rest of the baseline.
    """
    baseline_path = baseline or root / "tests" / BASELINE_NAME
    listed = load_baseline(baseline_path)
    files = {_relative(path, root): path for path in modules}
    violations = scan_temp(root) + scan_conditional(root, today or date.today())
    stems = _test_stems(root)
    for path in sorted(files.values()):
        violations.extend(scan_module(path, root, stems))
    present = {v.key for v in violations}
    new = [v for v in sorted(violations) if v.key not in listed]
    stale = []
    for line in sorted(listed - present):
        rule, _, rest = line.partition("|")
        file = rest.partition("|")[0]
        if rule in ("H6", "H7") or file in files or not (root / file).exists():
            stale.append(line)
    if not new and not stale:
        return None
    parts = []
    if new:
        parts.append(f"Test hygiene: {len(new)} new violation(s). Fix the test; never add a line to {BASELINE_NAME}.")
        parts.extend(v.describe() for v in new)
    if stale:
        parts.append(
            f"Test hygiene: {len(stale)} baseline line(s) no longer match a violation. "
            f"Delete them from tests/{BASELINE_NAME} (the baseline only shrinks)."
        )
        parts.extend(f"  {line}" for line in stale)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The pytest plugin
# ---------------------------------------------------------------------------

def _named_on_command_line(config: pytest.Config, folder: Path) -> bool:
    for arg in config.invocation_params.args:
        text = str(arg)
        if text.startswith("-"):
            continue
        path = (config.invocation_params.dir / text.split("::")[0]).resolve()
        if path == folder.resolve() or folder.resolve() in path.parents:
            return True
    return False


def pytest_ignore_collect(collection_path: Path, config: pytest.Config):
    """Leave a ``kind: report`` temporary folder out of the run unless a command-line argument names it."""
    root = config.rootpath
    base = (root / "tests" / "_temp").resolve()
    path = collection_path.resolve()
    if base not in path.parents:
        return None
    folder = base / path.relative_to(base).parts[0]
    if readme_kind(folder) == "report" and not _named_on_command_line(config, folder):
        return True
    return None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(session: pytest.Session, config: pytest.Config, items: list[pytest.Item]) -> None:
    """Fail the session when a test file or a temporary folder breaks a rule that the baseline does not list."""
    root = config.rootpath
    temp = (root / "tests" / "_temp").resolve()
    modules = sorted({item.path for item in items if temp not in item.path.resolve().parents})
    message = check(root, modules)
    if message:
        raise pytest.UsageError(message)
