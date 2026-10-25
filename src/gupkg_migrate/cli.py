"""The command line of ``gupkg-migrate``.

Call ``main(argv)`` from Python or run ``python -m gupkg_migrate``. The commands are
``package PATH`` (a version directory, a package folder or a whole registry; a package that 0.17
installed is also adopted: it gets an install record, see ``gupkg_migrate.adopt``), ``settings
CONFIG`` and ``shortcuts DIRECTORY``. Every command takes ``--check`` for a dry run that prints
a diff and changes nothing. Exit status is 0 when everything converted (or was already
converted) and 1 when anything failed validation or could not be read.

Implementation Approach
-----------------------
Each command first plans the whole conversion without writing, validates the result with
gupkg core, and writes only when the plan has no error and ``--check`` was not given. A plan
that fails validation therefore leaves the original files untouched.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from gupkg.system.port import System
from gupkg.system.windows import WindowsSystem

from .adopt import apply_adoption, plan_adoption
from .package import PackagePlan, apply_plan, plan_package
from .settings import apply_settings, plan_settings
from .shortcuts import apply_shortcuts, plan_shortcuts

_SKIPPED_NAMES = frozenset({"current", "data"})


def main(argv: Sequence[str] | None = None, *, system: System | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run one migration command and return its exit status.

    Parameters
    ----------
    argv : sequence of str, optional
        The command line without the program name; ``sys.argv[1:]`` by default.
    system : System, optional
        The machine boundary used to read shortcuts and adopt installs; ``WindowsSystem()`` by default.
    env : mapping, optional
        The environment used to expand the configured roots when adopting; ``os.environ`` by default.

    Returns
    -------
    int
        ``0`` when everything succeeded, ``1`` when a conversion failed.
    """
    parser = _parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    args.system = system if system is not None else WindowsSystem()
    args.env = env
    return int(args.run(args))


def find_package_dirs(path: Path) -> list[Path]:
    """List the directories below *path* that hold a ``pkg.toml``.

    Parameters
    ----------
    path : Path
        A version directory (itself returned), a package folder (its version directories
        and ``_bootstrap``) or a registry folder (the same, one level down).

    Returns
    -------
    list of Path
        Directories in name order. ``current`` and ``data``, hidden folders, links and
        junctions are skipped, so a version is never converted twice.
    """
    path = Path(path)
    if (path / "pkg.toml").is_file():
        return [path]

    def real_children(folder: Path) -> list[Path]:
        children = []
        for child in sorted(folder.iterdir()):
            if child.name.startswith(".") or child.name.casefold() in _SKIPPED_NAMES:
                continue
            if child.is_symlink() or os.path.isjunction(child) or not child.is_dir():
                continue
            children.append(child)
        return children

    found: list[Path] = []
    for child in real_children(path):
        if (child / "pkg.toml").is_file():
            found.append(child)
            continue
        found.extend(grandchild for grandchild in real_children(child) if (grandchild / "pkg.toml").is_file())
    return found


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _run_package(args: argparse.Namespace) -> int:
    """Convert every package directory below ``args.path``."""
    directories = find_package_dirs(args.path) if args.path.is_dir() else []
    if not directories:
        print(f"[error] no pkg.toml found at or below {args.path}", file=sys.stderr)
        return 1
    failed = 0
    for directory in directories:
        # One directory that cannot be read or written must not stop a whole registry run.
        try:
            plan = plan_package(directory)
            _report_package(plan, check=args.check, backup=args.backup)
            if not plan.ok:
                failed += 1
            elif not args.check and plan.changes_anything:
                for line in apply_plan(plan, backup=args.backup):
                    print(f"  {line}")
        except (OSError, ValueError) as error:
            print(f"[FAILED] {directory}: {error}", file=sys.stderr)
            failed += 1

    # Packages that 0.17 installed (they have `current`) are adopted once their versions are converted.
    if args.adopt:
        failed += _adopt_packages(args, directories)
    print(f"{len(directories)} directories, {failed} failed")
    return 1 if failed else 0


def _adopt_packages(args: argparse.Namespace, directories: list[Path]) -> int:
    """Adopt the package roots among the parents of *directories*; return how many failed."""
    roots = sorted({directory.parent for directory in directories if (directory.parent / "current").exists()
                    or os.path.islink(directory.parent / "current")})
    failed = 0
    env = os.environ if args.env is None else args.env
    for root in roots:
        try:
            plan = plan_adoption(root, system=args.system, env=env, mode=args.mode, settings_path=args.settings)
        except (OSError, ValueError) as error:
            print(f"[FAILED adopt] {root.name}: {error}", file=sys.stderr)
            failed += 1
            continue
        if plan.skipped:
            if not plan.skipped.startswith("no current"):
                print(f"[not adopted] {root.name}: {plan.skipped}")
            continue
        state = "already adopted" if plan.already_adopted else "FAILED" if not plan.ok else "would adopt" if args.check else "adopted"
        if args.check and not plan.ok and plan.version and any("convert it first" in error for error in plan.errors):
            state = "would adopt after its version is converted"
        print(f"[{state}] {root.name}" + (f" ({plan.mode}, {plan.version})" if plan.mode else ""))
        for error in plan.errors:
            if state != "would adopt after its version is converted":
                print(f"  error: {error}", file=sys.stderr)
        for note in plan.notes:
            print(f"  note: {note}")
        if not plan.ok and not args.check:
            failed += 1
        elif plan.ok and not args.check:
            try:
                for line in apply_adoption(plan):
                    print(f"  {line}")
            except (OSError, ValueError) as error:
                print(f"  error: {error}", file=sys.stderr)
                failed += 1
    return failed


def _report_package(plan: PackagePlan, *, check: bool, backup: bool = True) -> None:
    """Print what happened to, or would happen to, one package directory."""
    name = f"{plan.directory.parent.name}/{plan.target.name}"
    if plan.already_converted:
        state = "already format 2" if plan.ok else "already format 2, INVALID"
    elif not plan.ok:
        state = "FAILED"
    else:
        state = "would convert" if check else "converted"
    renamed = f" (was {plan.directory.name})" if plan.target != plan.directory else ""
    print(f"[{state}] {name}{renamed}")
    for error in plan.errors:
        print(f"  error: {error}", file=sys.stderr)
    if plan.ok and not plan.already_converted:
        print(
            "  note: comments of the 0.17 file are not preserved in the converted file"
            + ("; pkg.toml.bak keeps them" if backup else " and --no-backup keeps no copy of them")
        )
    for note in plan.notes:
        print(f"  note: {note}")
    for source, destination in plan.moves:
        print(f"  move: {source} -> {destination}")
    for finding in plan.hooks:
        print(f"  hook: {finding.file} matches {finding.service}; use {finding.suggestion}")
    for other in plan.other_versions:
        print(f"  other version: {other.version}; recreate it with: {other.suggestion}")
    for launcher in plan.launchers:
        print(f"  launcher: {launcher}")
    for launcher in plan.stale_launchers:
        print(f"  remove launcher: {launcher} (a tracker carries only check-updates.cmd)")
    if check and plan.ok and not plan.already_converted:
        print(plan.diff(), end="" if plan.diff().endswith("\n") else "\n")


def _run_settings(args: argparse.Namespace) -> int:
    """Convert one manager configuration."""
    plan = plan_settings(args.config, output=args.output)
    for error in plan.errors:
        print(f"[error] {error}", file=sys.stderr)
    if not plan.ok:
        return 1
    for note in plan.notes:
        print(f"note: {note}")
    if args.check:
        print(f"[would write] {plan.output}")
        print(plan.text, end="")
        return 0
    try:
        written = apply_settings(plan, force=args.force)
    except (ValueError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(f"[written] {written}")
    return 0


def _run_shortcuts(args: argparse.Namespace) -> int:
    """Import the shortcut files of one version directory."""
    plan = plan_shortcuts(args.directory, args.system)
    for error in plan.errors:
        print(f"[error] {error}", file=sys.stderr)
    if not plan.ok:
        return 1
    print(f"{len(plan.shortcuts)} shortcuts: {', '.join(row['name'] for row in plan.shortcuts)}")
    if args.check:
        print(plan.new_text, end="")
        return 0
    try:
        for line in apply_shortcuts(plan, backup=args.backup):
            print(f"  {line}")
    except (ValueError, OSError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="gupkg-migrate", description="Migrate gupkg 0.17 packages, configuration and shortcuts to 1.0."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    package = commands.add_parser("package", help="convert a version directory, package folder or registry to format 2")
    package.add_argument("path", type=Path, help="a version directory, a package folder or a registry folder")
    package.add_argument("--check", action="store_true", help="print a diff and change nothing")
    package.add_argument("--no-backup", dest="backup", action="store_false", help="do not keep pkg.toml.bak")
    package.add_argument("--no-adopt", dest="adopt", action="store_false",
                         help="do not write install records for packages that 0.17 installed")
    package.add_argument("--mode", choices=("system", "user", "portable"),
                         help="the install mode of adopted packages when their root does not tell")
    package.add_argument("--settings", type=Path, help="a settings.toml that names the roots (used to find the install mode)")
    package.set_defaults(run=_run_package, env=None)

    settings = commands.add_parser("settings", help="convert gupkg-config.toml (schema 2) to settings.toml (schema 3)")
    settings.add_argument("config", type=Path, help="the gupkg-config.toml to convert")
    settings.add_argument("--output", type=Path, help="write here instead of <system root>\\.gupkg\\settings.toml")
    settings.add_argument("--check", action="store_true", help="print the result and change nothing")
    settings.add_argument("--force", action="store_true", help="replace an existing settings file")
    settings.set_defaults(run=_run_settings)

    shortcuts = commands.add_parser("shortcuts", help="turn .lnk files in _shortcuts into [[shortcut]] tables")
    shortcuts.add_argument("directory", type=Path, help="a format 2 version directory with a _shortcuts folder")
    shortcuts.add_argument("--check", action="store_true", help="print the new pkg.toml and change nothing")
    shortcuts.add_argument("--no-backup", dest="backup", action="store_false", help="do not keep pkg.toml.bak")
    shortcuts.set_defaults(run=_run_shortcuts)
    return parser
