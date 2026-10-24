"""Protect decision D44 for the UI programs: they read through gupkg core and perform every operation as a call to the ``gupkg`` command line.

Behaviour protected: no module of ``gupkg_ui``, ``gupkg_tui`` or ``gupkg_manager`` imports the engine, the services, the elevation or authoring code (an in-process operation would run with
the user's rights where the CLI would have asked for administrator rights, and would bypass the CLI's checks); none touches the registry, ``ctypes``, the network or a shell; and
only ``gupkg_ui/client.py`` starts a program. This is a security rule that no observable behaviour test can prove by absence, so it is checked on the imports and names of the sources.

Real: the source files. Mocked: nothing.
Out of scope: what the programs do at run time (the other ``test_ui_*`` and ``test_manager_*`` modules).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src"
PACKAGES = ("gupkg_ui", "gupkg_tui", "gupkg_manager")
FILES = sorted(path for package in PACKAGES for path in (SOURCE / package).rglob("*.py"))

# Core modules that perform operations (or ask for rights); the programs may import the rest of core for reading and reference.
FORBIDDEN_IMPORTS = (
    "gupkg.engine", "gupkg.services", "gupkg.relaunch", "gupkg.distcopy", "gupkg.authoring", "gupkg.pinning", "gupkg.settingsedit", "gupkg.cli",
    "gupkg.catalog.registry",
)
# Names that mean the registry, native calls, the network, a shell or starting a program, found in code (not in docstrings or comments).
FORBIDDEN_NAMES = frozenset({
    "winreg", "ctypes", "urlopen", "urllib.request", "os.system", "os.startfile", "ShellExecute", "http.client", "socket", "create_subprocess_shell", "Popen",
})


def code_names(path: Path) -> set[str]:
    """Return the dotted names the code uses (imports, names, attribute chains) and the keyword arguments it passes, without docstrings or comments."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{item.name}" for item in node.names)
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            parts, current = [node.attr], node.value
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                found.add(".".join([current.id, *reversed(parts)]))
            found.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(f"{node.arg}=")
    return found


def imported(path: Path) -> list[str]:
    """Return every module name the file imports (absolute names; relative imports are inside the same package)."""
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [item.name for item in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names += [node.module, *(f"{node.module}.{item.name}" for item in node.names)]
    return names


def test_the_programs_exist_and_are_scanned() -> None:
    """The scan covers real files of all three packages (an empty scan would prove nothing)."""
    assert len(FILES) >= 20 and {path.parts[-2] if path.parent.parent == SOURCE else path.parts[-3] for path in FILES} >= {"gupkg_ui", "gupkg_tui", "gupkg_manager"}


@pytest.mark.parametrize("path", FILES, ids=lambda path: str(path.relative_to(SOURCE)).replace("\\", "/"))
def test_no_operation_is_performed_in_process(path: Path) -> None:
    """The module imports no engine, service, elevation or authoring code, and no function-local import hides one."""
    bad = [name for name in imported(path) if any(name == banned or name.startswith(banned + ".") for banned in FORBIDDEN_IMPORTS)]

    assert bad == [], f"{path.name} imports {bad}: operations must be gupkg CLI calls (decision D44)"
    function_local = [node.lineno for function in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for node in ast.walk(function) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert function_local == [], f"{path.name} has function-local imports at lines {function_local}"


@pytest.mark.parametrize("path", FILES, ids=lambda path: str(path.relative_to(SOURCE)).replace("\\", "/"))
def test_no_registry_network_shell_or_program_start_outside_the_client(path: Path) -> None:
    """The module does not use the registry, ``ctypes``, the network or a shell, and only ``client.py`` starts a program (through ``asyncio``, with an argument list)."""
    names = code_names(path)

    assert sorted(names & FORBIDDEN_NAMES) == []
    assert "shell=" not in names
    starts = {"subprocess", "create_subprocess_exec"} & names
    if starts:
        assert path.name == "client.py" and path.parent.name == "gupkg_ui", f"{path} starts a program"
    if path.name == "client.py":
        assert "create_subprocess_exec" in names and "create_subprocess_shell" not in names
