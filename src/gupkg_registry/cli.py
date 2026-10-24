"""The command line of the registry tool: ``update``, ``pin`` and ``validate``.

Exit status: 0 on success, 1 when a package failed or the registry has problems, 2 for a usage
or registry-file error. ``update`` never commits or pushes unless ``--auto-push`` is given.

Usage and API
-------------
``main(argv, system=None, environ=None)`` runs one command; ``python -m gupkg_registry`` calls it.
``update`` takes the package names to check (default: all), ``--registry PATH`` (default: the
current folder), ``--sandbox DIR`` (default: a temporary folder removed afterwards) and
``--auto-push``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from gupkg.system.port import System
from gupkg.system.windows import WindowsSystem

from .layout import RegistryFileError
from .publish import PublishError, commit_and_push
from .update import Outcome, pin_version, update_registry
from .validate import validate_registry


def main(argv: Sequence[str] | None = None, *, system: System | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Run one command and return its exit status (see the module docstring)."""
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    system = system if system is not None else WindowsSystem()
    environ = os.environ if environ is None else environ
    return int(args.run(args, system, environ))


def _sandbox(args: argparse.Namespace) -> tuple[Path, bool]:
    """Return the sandbox folder and whether this run created it (and so removes it)."""
    if args.sandbox is not None:
        return args.sandbox, False
    return Path(tempfile.mkdtemp(prefix="gupkg-registry-")), True


def _push_options(args: argparse.Namespace) -> dict[str, object]:
    """Return the keyword arguments of ``commit_and_push`` that the command line chose."""
    return {"allow_computed": args.allow_computed, "remote": args.remote, "branch": args.branch}


def _print(outcome: Outcome) -> None:
    """Print one outcome line."""
    flag = ("  [REVIEW]" if outcome.review else "") + ("  [UNVERSIONED]" if outcome.unversioned else "")
    print(f"{outcome.status:8} {outcome.package} {outcome.version or ''}: {outcome.message}{flag}".rstrip())


def _run_update(args: argparse.Namespace, system: System, environ: Mapping[str, str]) -> int:
    """Run the trackers; commit and push only with ``--auto-push``."""
    sandbox, temporary = _sandbox(args)
    try:
        report = update_registry(args.registry, system, sandbox=sandbox, names=args.packages or None, environ=environ)
    except RegistryFileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        if temporary:
            shutil.rmtree(sandbox, ignore_errors=True)
    for outcome in report.outcomes:
        _print(outcome)
    if args.auto_push and report.written:
        try:
            commit_and_push(system, args.registry, report.outcomes, **_push_options(args))
        except PublishError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print("committed and pushed")
    elif report.written:
        print("new versions are written but not committed: review them, then commit and push (or use --auto-push).")
    return 0 if report.ok else 1


def _run_pin(args: argparse.Namespace, system: System, environ: Mapping[str, str]) -> int:
    """Give a version its first hash; commit and push only with ``--auto-push``."""
    sandbox, temporary = _sandbox(args)
    try:
        outcome = pin_version(args.registry, system, args.version, sandbox=sandbox, environ=environ)
    except RegistryFileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    finally:
        if temporary:
            shutil.rmtree(sandbox, ignore_errors=True)
    _print(outcome)
    if outcome.status == "failed":
        return 1
    if args.auto_push:
        try:
            commit_and_push(system, args.registry, [outcome], **_push_options(args))
        except PublishError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    return 0


def _run_validate(args: argparse.Namespace, system: System, environ: Mapping[str, str]) -> int:
    """Validate the registry and print every problem."""
    report = validate_registry(args.registry)
    for problem in report.problems:
        print(f"problem: {problem}")
    print(
        f"{report.packages} packages, {report.versions} pinned versions, {len(report.hook_files)} hook files, "
        f"{len(report.unpinned)} unpinned: {'OK' if report.ok else str(len(report.problems)) + ' problems'}"
    )
    return 0 if report.ok else 1


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(prog="gupkg-registry", description="Keep a gupkg registry's pinned versions current.")
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--registry", type=Path, default=Path.cwd(), help="the registry checkout (default: current folder)")

    def network(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--sandbox", type=Path, metavar="DIR", help="scratch folder for downloads and staging (default: temporary)")
        sub.add_argument("--auto-push", action="store_true", help="commit the written versions and push (default: write only)")
        sub.add_argument("--allow-computed", action="store_true", help="with --auto-push, also publish hashes the tool computed itself (trust on first use)")
        sub.add_argument("--remote", help="with --auto-push, the remote to push to (default: the branch's upstream)")
        sub.add_argument("--branch", help="with --auto-push, the branch to push to (default: the branch's upstream)")

    update = commands.add_parser("update", help="run trackers and write new pinned versions")
    update.add_argument("packages", nargs="*", help="only these packages (default: all)")
    common(update)
    network(update)
    update.set_defaults(run=_run_update)

    pin = commands.add_parser("pin", help="give a version without a hash its first one (NAME/VERSION)")
    pin.add_argument("version", help="the version directory, as name/version")
    common(pin)
    network(pin)
    pin.set_defaults(run=_run_pin)

    validate = commands.add_parser("validate", help="check the whole registry (what CI runs)")
    common(validate)
    validate.set_defaults(run=_run_validate)
    return parser
