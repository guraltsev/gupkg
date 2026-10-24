"""Run the public ``gupkg`` command line: one grammar, one machine interface and the forwarders to the separate programs.

Every command takes explicit targets (names or paths, design section 10.1) and options after the command. The lifecycle
commands (``check-updates``, ``install``, ``update``, ``switch``, ``uninstall``, ``check``, ``cleanup``, ``link`` and the
``self`` aliases) call ``gupkg.engine.lifecycle``; ``list``, ``info``, ``search``, ``logs`` and ``shell`` read the catalog;
``registry``, ``settings`` and ``pkg`` work on registries, settings files and package definitions; ``tui``, ``manager`` and
``migrate`` start the separate programs of the same gupkg tree and pass their exit code through. The command line has no
interactive screen and no ``--all`` (decisions D11, D12): batches and interactive use belong to ``gupkg-manager``.

Usage and API
-------------
Call ``main(argv)`` from the console script or ``python -m gupkg``; it returns the exit code: 0 success, 2 user or
configuration error, 3 the operation failed, 4 internal error, 5 declined or input needed, 6 the package needs a newer gupkg.
``--json`` prints one result document on standard output (``"api": 1``) and sends human output to standard error;
``--plan`` plans without changing anything; ``--events-file NAME`` writes the event stream as JSON lines into the log
directories; ``--no-input`` never asks (a needed answer is exit code 5); ``--yes`` answers the consequential questions.

Implementation Approach
-----------------------
Argparse owns the grammar and nothing else: a parsed command is turned into ``Request`` objects, run through one session
(console and log reporters, the stop marker of the run, the engine) and rendered from the typed result. A call that needs an
elevated token hands the whole command line, rebuilt from the parsed options and never copied from the parent's input, to
one elevated child, which validates it again (``--relaunched``): the child accepts package names and paths inside the
system root and a fixed set of options, and refuses everything else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import authoring, settingsedit
from ._version import __version__
from .authoring import AuthoringError
from .catalog.catalog import Catalog, ListFilter
from .catalog.registry import cache_state, load_registry, registry_cache_dir, sync_registry
from .engine import lifecycle
from .engine.context import Engine, stop_marker
from .engine.errors import describe
from .engine.receipts import read_receipt, receipt_checksum
from .model.checksum import ChecksumInvalid, parse_skip_flag
from .model.mode import ModeContext, ModeRefused, build_mode_context, select_mode
from .model.plan import EXIT_FAILED, EXIT_INPUT, EXIT_OK, EXIT_USER, BatchResult, Request
from .model.settings import LAYER_DEFAULTS, LAYER_PORTABLE, LAYER_SYSTEM, LAYER_USER, Settings, SettingsError, expand_env
from .modes import SettingsLoader, default_folders, settings_loader
from .pinning import PinError, pin_hash
from .pkgcheck import check_directory
from .report import results
from .report.console import ConsoleReporter
from .report.events import Prompt, Step, Warning, emit
from .report.logfile import list_run_logs, running_gupkg_logs_base, validate_log_file_name
from .report.session import RunSession
from .selfmgmt.location import RunningGupkg, locate_running_gupkg
from .selfmgmt.status import self_status
from .system.port import ElevatedRun, unsafe_relaunch_argument
from .settingsload import settings_file
from .system.fake import FakeSystem
from .system.port import System, is_elevated_run
from .system.windows import WindowsSystem, wait_for_keypress
from .tls import enable_partial_chain_verification
from .viewdata import info_data, package_data

# The commands that are programs of their own: ``gupkg <word> ...`` starts the launcher in the same gupkg tree.
FORWARDERS = {"tui": "gupkg-tui.cmd", "manager": "gupkg-manager.cmd", "migrate": "gupkg-migrate.cmd"}

# The lifecycle commands, which run through ``gupkg.engine.lifecycle`` inside one session.
_ENGINE_COMMANDS = ("check-updates", "install", "update", "switch", "uninstall", "check", "cleanup", "link")


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    """What one invocation needs besides its parsed arguments: the machine, the environment and the settings loader."""

    args: argparse.Namespace
    system: System
    env: Mapping[str, str]
    location: RunningGupkg
    elevated: bool
    loader: SettingsLoader

    @property
    def json(self) -> bool:
        """Return whether the call asked for the machine document on standard output."""
        return bool(getattr(self.args, "json", False))


class UsageError(Exception):
    """Report a command line that the grammar refuses (exit code 2)."""


def main(
    argv: Sequence[str] | None = None,
    *,
    system: System | None = None,
    env: Mapping[str, str] | None = None,
    location: RunningGupkg | None = None,
) -> int:
    """Run one gupkg command line and return its exit code.

    Parameters
    ----------
    argv : sequence of str, optional
        The arguments without the program name; ``sys.argv[1:]`` by default.
    system : System, optional
        The machine boundary; the real one by default. ``--sandbox DIR`` wraps it in a sandbox.
    env : mapping, optional
        The environment used to expand ``%NAME%`` in settings and to find the temporary folder; ``os.environ`` by default.
    location : RunningGupkg, optional
        Where this gupkg lives; found from the loaded package by default. Tests pass a stand-in tree.

    Returns
    -------
    int
        0 success, 2 user or configuration error, 3 the operation failed, 4 internal error, 5 declined or input needed,
        6 the package needs a newer gupkg.
    """
    # Older bundled interpreters otherwise reject sites whose issuing intermediate, but not newest root, is trusted.
    enable_partial_chain_verification()
    _configure_streams()
    tokens = list(sys.argv[1:] if argv is None else argv)
    machine = system or WindowsSystem()
    where = location or locate_running_gupkg()
    environment = dict(os.environ) if env is None else dict(env)

    # The programs of their own take the rest of the command line untouched, so they are found before argparse sees it.
    forwarded = _forwarder(tokens)
    if forwarded is not None:
        return _forward(forwarded[0], forwarded[1], forwarded[2], machine, where)

    # Parse once; a usage error is exit code 2, with a JSON document when the caller asked for one.
    parser = _build_parser()
    try:
        args = parser.parse_args(_normalise(tokens))
    except UsageError as error:
        return _usage_error(tokens, str(error))
    except SystemExit as stop:
        return int(stop.code or 0)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USER
    return _run(args, tokens, machine, environment, where)


def _configure_streams() -> None:
    """Make writing to standard output and error unable to crash, and make redirected output UTF-8.

    The launcher runs ``python -I``, so ``PYTHONIOENCODING`` and ``PYTHONUTF8`` are ignored and a pipe or a file would use
    the ANSI code page. A redirected stream is therefore UTF-8 (the UI programs read it as UTF-8); a console keeps its own
    code page. Either way a character that cannot be encoded (a lone surrogate from a file name) is replaced, never raised.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _run(args: argparse.Namespace, tokens: list[str], machine: System, env: Mapping[str, str], where: RunningGupkg) -> int:
    """Run a parsed command on the real or sandboxed machine and turn what goes wrong into an exit code."""
    # The elevated child accepts a fixed set of options only: a standard user cannot steer it through the root options.
    if getattr(args, "relaunched", False) and (args.settings is not None or args.sandbox is not None):
        return _usage_error(tokens, "--settings and --sandbox are not accepted by the elevated child of a relaunch")
    if getattr(args, "relaunched", False):
        for token in tokens:
            reason = unsafe_relaunch_argument(token)
            if reason is not None:
                return _usage_error(tokens, f"the elevated child refuses {reason}: none of & | < > ^ \" % ! ` or control characters is accepted")
    try:
        parse_skip_flag(getattr(args, "skip_checksum", None))
    except ChecksumInvalid as error:
        return _usage_error(tokens, str(error))
    try:
        if args.sandbox is not None:
            args.sandbox.mkdir(parents=True, exist_ok=True)
            machine = FakeSystem.sandbox(args.sandbox, machine)
        elevated = is_elevated_run(machine)
        installed_root = where.root if where.kind == "installed" and machine.directory_is_admin_only(where.root) else None
        loader = settings_loader(
            system=machine, env=env, elevated=elevated, app_dir=where.app_dir, installed_system_root=installed_root,
            settings_path=args.settings,
        )
    except (OSError, ValueError) as error:
        return _usage_error(tokens, f"cannot set up the run{f' in --sandbox {args.sandbox}' if args.sandbox is not None else ''}: {error}")
    run = _Run(args, machine, env, where, elevated, loader)

    # Dispatch; a settings or usage problem is the caller's to fix, anything else unexpected is exit code 4.
    handler = _HANDLERS[args.command]
    try:
        code = handler(run)
    except Exception as error:  # noqa: BLE001 - the process boundary: nothing may escape as a traceback
        code = _fail(run, args.command, error)
    if getattr(args, "relaunched", False) and not getattr(args, "no_input", False) and sys.stdin is not None and sys.stdin.isatty():
        print("Press any key to continue...", file=sys.stderr)
        wait_for_keypress()
    return code


# ---------------------------------------------------------------------------
# Output and errors
# ---------------------------------------------------------------------------


def _human(run: _Run, text: str) -> None:
    """Print human text: on standard output, or on standard error when standard output carries the JSON document."""
    print(text, file=sys.stderr if run.json else sys.stdout)


def _document(run: _Run, document: Mapping[str, Any]) -> None:
    """Print the machine document on standard output when ``--json`` was given."""
    if run.json:
        sys.stdout.write(results.dumps(document))
        sys.stdout.flush()


def _fail(run: _Run, command: str, error: BaseException) -> int:
    """Report *error* as a structured failure (text on standard error, a document with ``--json``) and return its exit code."""
    exit_code, data = describe(error)
    print(f"error: {data['message']}", file=sys.stderr)
    if data.get("hint"):
        print(f"hint: {data['hint']}", file=sys.stderr)
    _document(run, results.error_document(command, data["code"], data["message"], exit_code, data.get("hint")))
    return exit_code


def _usage_error(tokens: Sequence[str], message: str) -> int:
    """Report a usage error (exit code 2): the message on standard error and, when ``--json`` is among the arguments, a document."""
    print(f"gupkg: error: {message}", file=sys.stderr)
    if "--json" in tokens:
        command = next((item for item in tokens if not item.startswith("-")), "gupkg")
        sys.stdout.write(results.dumps(results.error_document(command, "usage", message, EXIT_USER)))
    return EXIT_USER


def _ask(question: str) -> bool:
    """Ask a yes-or-no question on the terminal (the question itself was already shown as a prompt event); no answer is no."""
    sys.stderr.write("[y/N] ")
    sys.stderr.flush()
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# The lifecycle commands
# ---------------------------------------------------------------------------


def _engine_command(run: _Run) -> int:
    """Run ``check-updates``, ``install``, ``update``, ``switch``, ``uninstall``, ``check``, ``cleanup`` or ``link``."""
    args, command = run.args, run.args.command
    return _lifecycle(run, command, _targets(run), _requests(args, command), sweep=_is_sweep(args))


def _lifecycle(run: _Run, command: str, targets: list[str], make: Callable[[str], Request], *, sweep: bool = False) -> int:
    """Plan or run *targets* through the engine inside one session, render the result and return its exit code."""
    args = run.args
    skip = parse_skip_flag(getattr(args, "skip_checksum", None))

    # A call with nothing to act on is not an error: say so, and report it in the document.
    if not targets:
        _human(run, "No packages.")
        _document(run, results.result_document(command, BatchResult()))
        return EXIT_OK

    stop_path = _stop_path(run)
    session = _session(run, command, stop_path)
    try:
        with session:
            # A plan changes nothing: it needs no question and no elevation.
            if getattr(args, "plan", False):
                engine = _engine(run, session, command, targets, stop_path, batch_confirmed=False)
                plans = lifecycle.plan(engine, [replace(make(item), skip=skip) for item in targets])
                document = results.plan_document(command, plans, targets)
                text = "\n".join(results.format_plan(entry) for entry in document["plans"])
                session.finish(text, document["exit_code"], document)
            else:
                code, document, text = _execute(run, session, command, targets, make, skip, stop_path, sweep)
                session.finish(text, code, document)
    finally:
        _remove_marker(stop_path)
    _document(run, document)
    return document["exit_code"]


def _execute(
    run: _Run, session: RunSession, command: str, targets: list[str], make: Callable[[str], Request],
    skip: str | None, stop_path: Path | None, sweep: bool,
) -> tuple[int, dict[str, Any], str]:
    """Ask the questions a consequential call needs, run it, and return its exit code, document and summary text."""
    args = run.args

    # Consequential: a plan with several targets is confirmed once (decision D25); --yes answers, --no-input is exit code 5.
    confirmed = bool(args.yes or getattr(args, "batch_confirmed", False))
    changes = command != "check" or getattr(args, "fix", False)
    if changes and len(targets) > 1:
        if not confirmed:
            question = f"{command} {len(targets)} packages ({', '.join(targets[:5])}{', ...' if len(targets) > 5 else ''})?"
            if args.no_input:
                return _declined(command, f"{question} Pass --yes to confirm. Nothing was changed.")
            emit(Prompt(question))
            if not _ask(question):
                return _declined(command, "Declined. Nothing was changed.")
            confirmed = True

    # Targets whose mode is known to need the elevated token are run apart from the rest (one elevation per call).
    merged: list[Any] = []
    children: list[BatchResult] = []
    relaunched = False
    for group in _groups(targets):
        engine = _engine(run, session, command, group, stop_path, batch_confirmed=confirmed and not args.yes)
        batch = lifecycle.run_batch(engine, [replace(make(item), skip=skip) for item in group])
        merged.extend(batch.results)
        relaunched = relaunched or batch.relaunched
        children.append(batch)
        if batch.relaunched:
            session.discard_log()
    child_code = next((item.child_exit_code for item in reversed(children) if item.child_exit_code is not None), None)
    combined = BatchResult(tuple(merged), relaunched=relaunched, child_exit_code=child_code)
    text = combined.summary()
    extra: dict[str, Any] = {}
    if relaunched:
        text += f"\nThe elevated gupkg ended with exit code {child_code} for the system targets."
        problem = _missing_run_end(run, args.events_file)
        if problem is not None:
            text += f"\nerror: {problem}"
            extra["errors"] = [{"code": "child-no-result", "message": problem, "hint": "The elevated run may have been killed; check the system root's logs."}]
    if sweep and not relaunched:
        extra["root_cleanup"] = _prune_roots(run, session, group_modes=_modes_of(targets), command=command)
    document = results.result_document(command, combined)
    document.update(extra)
    if "errors" in extra:
        document["ok"], document["exit_code"] = False, max(document["exit_code"], EXIT_FAILED)
    return document["exit_code"], document, text.strip()


def _declined(command: str, message: str) -> tuple[int, dict[str, Any], str]:
    """Return the outcome of a call whose confirmation was declined or could not be asked: exit code 5, nothing changed."""
    document = results.error_document(command, "input-needed", message, EXIT_INPUT)
    return EXIT_INPUT, document, message


def _requests(args: argparse.Namespace, command: str) -> Callable[[str], Request]:
    """Return the function that turns one target into the ``Request`` of *command* from the parsed options."""
    mode = getattr(args, "mode", None)
    if command == "check-updates":
        return lambda target: Request("check-updates", target, mode=mode, no_download=args.no_download)
    if command == "install":
        return lambda target: Request("install", target, mode=mode, refresh=args.refresh, in_place=args.in_place)
    if command == "update":
        return lambda target: Request("update", target, mode=mode)
    if command == "switch":
        return lambda target: Request("switch", target, mode=mode, version=args.version)
    if command == "uninstall":
        return lambda target: Request("uninstall", target, mode=mode, purge=args.purge)
    if command == "cleanup":
        return lambda target: Request("cleanup", target, mode=mode, keep=args.keep, bundled=args.bundled, sweep=not args.targets)
    if command == "check":
        return lambda target: Request("check", target, mode=mode, fix=args.fix)
    if command == "repair":
        return lambda target: Request("repair", target, mode=mode)
    return lambda target: Request("link", target, mode=mode, link_to=args.entry, unlink=args.unlink)


def _targets(run: _Run) -> list[str]:
    """Return the targets of the call; ``check`` and ``cleanup`` without targets act on every package of the trusted roots."""
    args = run.args
    named = list(getattr(args, "targets", None) or ([args.target] if getattr(args, "target", None) else []))
    if named or args.command not in ("check", "cleanup"):
        return named
    settings = run.loader().settings
    catalog = Catalog.from_settings(settings, run.system, run.env, elevated=run.elevated)
    modes = frozenset({args.mode}) if args.mode else None
    return [f"{view.mode}:{view.name}" for view in catalog.list(ListFilter(modes=modes)).views]


def _is_sweep(args: argparse.Namespace) -> bool:
    """Return whether the call is a root-wide ``cleanup`` (no target named), the only case that leaves a busy package alone."""
    return args.command == "cleanup" and not args.targets


def _mode_prefix(target: str) -> str | None:
    """Return the mode a ``mode:name`` target names (``system``, ``user`` or ``portable``), else ``None``."""
    prefix, colon, rest = target.partition(":")
    return prefix.casefold() if colon and rest and prefix.casefold() in ("system", "user", "portable") and rest[0] not in "\\/" else None


def _groups(targets: list[str]) -> list[list[str]]:
    """Split targets that all name their mode into a normal group and a system group; any other call stays one group.

    A call whose targets were expanded from every package of the roots (or that qualified each target) may hold system
    and user packages. The system group needs the elevated token and is run apart (and last), so one elevation covers it
    and the other packages are not left out of an elevated child that would refuse them.
    """
    if not all(_mode_prefix(item) is not None for item in targets):
        return [targets]
    normal = [item for item in targets if _mode_prefix(item) != "system"]
    system = [item for item in targets if _mode_prefix(item) == "system"]
    return [group for group in (normal, system) if group]


def _modes_of(targets: Sequence[str]) -> set[str]:
    """Return the modes named by ``mode:name`` targets."""
    return {mode for mode in (_mode_prefix(item) for item in targets) if mode is not None}


def _prune_roots(run: _Run, session: RunSession, *, group_modes: set[str], command: str) -> dict[str, Any]:
    """Prune the logs and old registry trees of each root a root-wide ``cleanup`` covered (a system root only from an admin shell)."""
    done: dict[str, Any] = {}
    for mode in sorted(group_modes & {"system", "user"}):
        if mode == "system" and not run.elevated:
            emit(Warning("The system root's logs and registry trees are pruned from an administrator shell; they were left alone."))
            continue
        settings, context = _context(run, mode)
        engine = _engine(run, session, command, [], None, batch_confirmed=False)
        done[mode] = lifecycle.cleanup_root(engine, context, settings)
    return done


# ---------------------------------------------------------------------------
# Session, engine and the elevated child
# ---------------------------------------------------------------------------


_STOP_NAME = re.compile(r"gupkg-stop-[0-9a-f]{32}")


def _stop_path(run: _Run) -> Path | None:
    """Return a new, unique path for the run's stop marker, or ``None`` when no temporary folder is known (decision D37).

    The path is fresh for every run and announced in ``RunStart``: a caller creates a file there to stop the run after the
    package that is running. A file that already exists when the run starts is never a stop request, because the name
    is chosen after the caller could have created anything.
    """
    folder = run.env.get("TEMP") or run.env.get("TMP")
    if not folder or not Path(folder).is_dir():
        return None
    given = getattr(run.args, "stop_file", None)
    if given and getattr(run.args, "relaunched", False):
        # The parent's marker is accepted only as a fresh name inside this process's own temporary folder.
        path = Path(given)
        if _STOP_NAME.fullmatch(path.name) and os.path.normcase(os.path.abspath(path.parent)) == os.path.normcase(os.path.abspath(folder)):
            return path
    return Path(folder) / f"gupkg-stop-{uuid.uuid4().hex}"


def _missing_run_end(run: _Run, events_name: str | None) -> str | None:
    """Return a message when the elevated child left no ``run_end`` line in its events file, else ``None``.

    Only a call that named ``--events-file`` can be checked: the child writes that file into the system root's log folder.
    """
    if not events_name:
        return None
    try:
        _, context = _context(run, "system")
        lines = (context.log_dir / events_name).read_text(encoding="utf-8").splitlines()
    except (OSError, ModeRefused, SettingsError, ValueError):
        return f"The elevated run ended but its events file {events_name} could not be read, so how it ended is not known."
    last = next((line for line in reversed(lines) if line.strip()), "")
    try:
        ended = json.loads(last).get("event") == "run_end"
    except ValueError:
        ended = False
    return None if ended else f"The elevated run ended without a run_end line in its events file {events_name}: treat the run as failed."


def _remove_marker(path: Path | None) -> None:
    """Remove the stop marker when a caller created it (the run is over), ignoring a folder or a file that is in use."""
    if path is not None and os.path.lexists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def _session(run: _Run, command: str, stop_path: Path | None) -> RunSession:
    """Build the run's session: the console, the log files (retention from the settings) and the events file."""
    args = run.args
    settings = run.loader().settings
    console = ConsoleReporter(sys.stderr if run.json else None, problems_to_stderr=not run.json, color=settings["ui.color"])
    return RunSession(
        command=command,
        argv=_argv_text(run),
        mode=getattr(args, "mode", None) or "auto",
        elevated=run.elevated,
        version=__version__,
        reporters=[console],
        events_name=getattr(args, "events_file", None),
        keep_runs=settings["logs.keep_runs"],
        keep_days=settings["logs.keep_days"],
        fallback_base=running_gupkg_logs_base(),
        settings_layers=[item.name for item in run.loader().layers if item.status == "applied"],
        stop_file=str(stop_path) if stop_path is not None else None,
    )


def _argv_text(run: _Run) -> list[str]:
    """Return the command line recorded in the log header (the parsed command and its targets, never a secret)."""
    args = run.args
    named = list(getattr(args, "targets", None) or ([args.target] if getattr(args, "target", None) else []))
    return [args.command, *named]


def _engine(run: _Run, session: RunSession, command: str, targets: Sequence[str], stop_path: Path | None, *, batch_confirmed: bool) -> Engine:
    """Build the engine of one call: the machine, the questions it may ask, the stop marker and the elevated child's command line."""
    args = run.args
    log = session.log

    def log_path(package: Path) -> str | None:
        """Return the package's log file of this run, stored in its receipt (design 14.3)."""
        found = log.package_log(package) if log is not None else None
        return str(found) if found is not None else None

    return Engine(
        system=run.system, env=run.env, load=run.loader, confirm=_ask, yes=bool(args.yes), no_input=bool(args.no_input),
        cancel=stop_marker(stop_path) if stop_path is not None else (lambda: False), log_path=log_path,
        relaunched=bool(getattr(args, "relaunched", False)),
        argv=_child_argv(run, command, targets, batch_confirmed=batch_confirmed, stop_path=stop_path),
        gupkg_dir=run.location.version_dir, gupkg_root=run.location.root,
        request_stop=(lambda: stop_path.touch()) if stop_path is not None else None,
    )


def _child_argv(
    run: _Run, command: str, targets: Sequence[str], *, batch_confirmed: bool, stop_path: Path | None = None,
) -> tuple[str, ...]:
    """Return the command line of the elevated child, rebuilt from the parsed options (never copied from the input).

    Only the command, its targets and a fixed set of options travel: ``--settings`` and ``--sandbox`` never do, and the
    child validates all of it again. ``--yes`` travels only when the user gave it: a question the parent answered
    ("run this on three packages?") does not answer the ones the child asks ("delete the data folder?").
    """
    args = run.args
    argv: list[str] = [command]
    if command == "switch":
        argv += [targets[0], args.version]
    elif command == "link":
        argv += [targets[0], *([args.entry] if args.entry else [])]
    else:
        argv += list(targets)
    if getattr(args, "mode", None):
        argv += ["--mode", args.mode]
    for flag, option in (
        ("no_download", "--no-download"), ("refresh", "--refresh"), ("in_place", "--in-place"), ("purge", "--purge"),
        ("bundled", "--bundled"), ("fix", "--fix"), ("unlink", "--unlink"), ("plan", "--plan"), ("yes", "--yes"),
        ("no_input", "--no-input"),
    ):
        if getattr(args, flag, False):
            argv.append(option)
    if command == "cleanup":
        argv += ["--keep", str(args.keep)]
    skip = getattr(args, "skip_checksum", None)
    if skip is not None:
        argv.append("--skip-checksum" if skip == "" else f"--skip-checksum={skip}")
    if getattr(args, "events_file", None):
        argv += ["--events-file", args.events_file]
    if stop_path is not None and unsafe_relaunch_argument(str(stop_path)) is None:
        argv += ["--stop-file", str(stop_path)]
    argv.append("--relaunched")
    if batch_confirmed:
        argv.append("--batch-confirmed")
    return tuple(argv)


# ---------------------------------------------------------------------------
# Reading commands: list, info, search, logs, shell
# ---------------------------------------------------------------------------


def _catalog(run: _Run) -> Catalog:
    """Build the catalog of the trusted roots (only the system root in an elevated run)."""
    return Catalog.from_settings(run.loader().settings, run.system, run.env, elevated=run.elevated)


def _cmd_list(run: _Run) -> int:
    """List the packages of the trusted roots, or the names for scripts, filtered by status and mode."""
    args = run.args
    chosen = next((name for name in ("installed", "available", "outdated", "broken", "local") if getattr(args, name)), None)
    filters = ListFilter(
        statuses=frozenset({chosen}) if chosen in ("outdated", "broken", "local", "available") else None,
        modes=frozenset({args.mode}) if args.mode else None, include_available=chosen == "available",
    )
    listing = _catalog(run).list(filters)
    views = [view for view in listing.views if chosen != "installed" or view.installed is not None]
    rows = [package_data(view, details=args.details) for view in views]
    for warning in listing.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.names:
        counts = {row["name"].casefold(): 0 for row in rows}
        for row in rows:
            counts[row["name"].casefold()] += 1
        names = [row["name"] if counts[row["name"].casefold()] == 1 else f"{row['mode']}:{row['name']}" for row in rows]
        if run.json:
            _document(run, results.envelope("list", ok=True, exit_code=EXIT_OK, names=names))
        else:
            print("\n".join(names))
        return EXIT_OK
    if run.json:
        _document(run, results.envelope("list", ok=True, exit_code=EXIT_OK, packages=rows, warnings=list(listing.warnings)))
    else:
        print(results.format_listing(rows, details=args.details))
    return EXIT_OK


def _cmd_info(run: _Run) -> int:
    """Show everything known about each named package: versions, link, tracker, last check, receipts and log."""
    args = run.args
    catalog = _catalog(run)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    code = EXIT_OK
    for target in args.targets:
        selector = _qualify(target, args.mode)
        try:
            rows.append(info_data(catalog.resolve(selector).view))
        except Exception as error:  # noqa: BLE001 - one unknown target must not hide the others
            exit_code, data = describe(error)
            code = max(code, exit_code)
            errors.append({"target": target, **data})
            print(f"error: {target}: {data['message']}", file=sys.stderr)
    if run.json:
        _document(run, results.envelope("info", ok=code == EXIT_OK, exit_code=code, packages=rows, errors=errors))
    else:
        print("\n\n".join(results.format_info(row) for row in rows))
    return code


def _cmd_search(run: _Run) -> int:
    """Find registry packages by name (refreshing a registry that was never synchronised unless ``--offline``)."""
    args = run.args
    settings, context = _context(run, args.mode)
    if not args.offline:
        _sync_missing(run, settings, context)
    catalog = Catalog.from_settings(settings, run.system, run.env, elevated=run.elevated)
    views = [
        view for view in catalog.list(ListFilter(query=args.query, registry=args.registry, include_available=True)).views
        if view.registry is not None
    ]
    rows = [
        {**package_data(view), "registry": view.registry.registry, "registry_version": view.registry.versions[-1].name if view.registry.versions else None}
        for view in views
    ]
    if run.json:
        _document(run, results.envelope("search", ok=True, exit_code=EXIT_OK, query=args.query, packages=rows))
    else:
        width = max((len(row["name"]) for row in rows), default=0)
        lines = [f"{row['name']:<{width}}  {row['registry']:<10} {row['registry_version'] or '-':<12} {row['status']}" for row in rows]
        print("\n".join(lines) if lines else f"No registry package matches '{args.query}'.")
    return EXIT_OK


def _qualify(target: str, mode: str | None) -> str:
    """Return *target* qualified with ``--mode`` when it is a plain package name (a path or a qualified name is left alone)."""
    plain = ":" not in target and "\\" not in target and "/" not in target and not target.startswith(".")
    return f"{mode}:{target}" if mode and plain else target


def _sync_missing(run: _Run, settings: Settings, context: ModeContext) -> None:
    """Synchronise the registries that were never synchronised into the root of *context*, so a first search finds something."""
    for registry in settings.registries:
        cache = registry_cache_dir(context.root, registry.name)
        try:
            known = cache_state(cache, run.system).tree_dir is not None
        except Exception:  # noqa: BLE001 - an untrusted cache is reported by the sync below
            known = False
        if not known and (context.mode != "system" or run.elevated):
            outcome = sync_registry(cache, run.system, name=registry.name, source=registry.source)
            if not outcome.ok:
                print(f"warning: registry {registry.name} could not be refreshed: {outcome.error}", file=sys.stderr)


def _cmd_logs(run: _Run) -> int:
    """List the run logs of a package (or of the root), print the newest path, or open the newest log."""
    args = run.args
    if args.target:
        found = _catalog(run).resolve(_qualify(args.target, args.mode))
        if found.view.path is None:
            return _fail(run, "logs", ModeRefused(f"'{args.target}' is not a local package, so it has no logs"))
        logs = list_run_logs(found.view.path)
        owner = found.view.name
    else:
        _, context = _context(run, args.mode)
        logs = list_run_logs(context.root)
        owner = f"the {context.mode} root"
    if (args.open or args.path) and not logs:
        print(f"error: {owner} has no run logs", file=sys.stderr)
        _document(run, results.error_document("logs", "no-logs", f"{owner} has no run logs", EXIT_USER))
        return EXIT_USER
    if args.open:
        run.system.open_path(logs[0])
    if run.json:
        _document(run, results.envelope("logs", ok=True, exit_code=EXIT_OK, logs=[str(item) for item in logs], opened=str(logs[0]) if args.open else None))
    elif args.path:
        print(logs[0])
    elif not args.open:
        print("\n".join(str(item) for item in logs) if logs else f"No run logs for {owner}.")
    return EXIT_OK


def _cmd_shell(run: _Run) -> int:
    """Print the ``PATH`` line for a root that does not touch the environment (``integrate = "none"``)."""
    _, context = _context(run, run.args.mode)
    line = context.path_line()
    if run.json:
        _document(run, results.envelope("shell", ok=True, exit_code=EXIT_OK, mode=context.mode, bin=str(context.bin_dir), path_line=line))
    elif line is None:
        print(f"{context.mode} mode adds {context.bin_dir} to PATH itself; there is nothing to print.", file=sys.stderr)
    else:
        print(line)
    return EXIT_OK


def _context(run: _Run, mode: str | None) -> tuple[Settings, ModeContext]:
    """Return the effective settings and the ``ModeContext`` of *mode* (``--mode``, else ``default_mode``) without a package.

    Raises
    ------
    ModeRefused
        If the mode is refused (an elevated run never uses a user or portable root) or portable has no configured root.
    """
    settings = run.loader().settings
    choice = select_mode(explicit=mode, elevated=run.elevated, default_mode=settings["default_mode"])
    if choice.mode == "portable":
        if settings.source_of("roots.portable") == LAYER_DEFAULTS:
            raise ModeRefused("No portable root is configured: set [roots].portable with `gupkg settings set roots.portable <folder>`.")
        settings = run.loader(portable_root=Path(expand_env(settings["roots.portable"], run.env))).settings
    elif choice.mode == "system" and not run.elevated:
        settings = run.loader(system_mode=True).settings
    return settings, build_mode_context(choice, settings, run.env, defaults=default_folders(run.system))


# ---------------------------------------------------------------------------
# registry, settings
# ---------------------------------------------------------------------------


def _cmd_registry(run: _Run) -> int:
    """Run ``registry sync|status|list|add|remove`` against the cache and the settings of the chosen root."""
    args = run.args
    settings, context = _context(run, args.mode)
    action = args.registry_command
    if action in ("sync", "add", "remove") and context.mode == "system" and not run.elevated:
        return _fail(run, f"registry {action}", ModeRefused(
            "The system root is written from an administrator shell: run this command there, or use --mode user."))
    if action == "sync":
        return _registry_sync(run, settings, context)
    if action == "add" or action == "remove":
        return _registry_edit(run, settings, context)
    rows = []
    for registry in settings.registries:
        cache = registry_cache_dir(context.root, registry.name)
        state = cache_state(cache, run.system)
        snapshot = load_registry(registry.name, cache, run.system) if action == "status" else None
        row = {"name": registry.name, "source": registry.source}
        if snapshot is not None:
            row.update({
                "revision": state.revision, "synchronized_at": state.synchronized_at, "last_failure": state.last_failure,
                "cache": str(cache), "packages": len(snapshot.entries), "problems": list(snapshot.problems),
            })
        rows.append(row)
    if run.json:
        _document(run, results.envelope(f"registry {action}", ok=True, exit_code=EXIT_OK, mode=context.mode, registries=rows))
    else:
        for row in rows:
            print(f"{row['name']}  {row['source']}")
            if action == "status":
                print(f"    revision {row['revision'] or 'not synchronised'}, synchronised {row['synchronized_at'] or 'never'}, {row['packages']} packages")
                if row["last_failure"]:
                    print(f"    last failure: {row['last_failure']}")
                for problem in row["problems"]:
                    print(f"    problem: {problem}")
        if not rows:
            print("No registries are configured.")
    return EXIT_OK


def _registry_sync(run: _Run, settings: Settings, context: ModeContext) -> int:
    """Synchronise the named registries (all of them by default) into the root's cache, inside a session."""
    args = run.args
    wanted = [item for item in settings.registries if not args.names or item.name.casefold() in {n.casefold() for n in args.names}]
    unknown = {n.casefold() for n in args.names} - {item.name.casefold() for item in wanted}
    if unknown:
        return _fail(run, "registry sync", ModeRefused(f"no registry is configured with the name {', '.join(sorted(unknown))}"))
    session = _session(run, "registry-sync", None)
    rows: list[dict[str, Any]] = []
    code = EXIT_OK
    with session:
        session.bind(context.root)
        for registry in wanted:
            outcome = sync_registry(
                registry_cache_dir(context.root, registry.name), run.system, name=registry.name, source=registry.source,
                offline=args.offline,
            )
            rows.append({
                "name": registry.name, "ok": outcome.ok, "status": outcome.status, "revision": outcome.revision,
                "problems": list(outcome.problems), "error": outcome.error,
            })
            if outcome.ok:
                emit(Step(f"registry {registry.name}: {outcome.status} (revision {(outcome.revision or '')[:12]})", "ok"))
                if outcome.problems:
                    emit(Warning(f"registry {registry.name}: {len(outcome.problems)} packages with problems were skipped"))
            else:
                code = EXIT_FAILED
                emit(Warning(f"registry {registry.name}: {outcome.error}"))
        document = results.envelope("registry sync", ok=code == EXIT_OK, exit_code=code, mode=context.mode, registries=rows)
        session.finish("\n".join(f"{row['name']}: {row['status'] or 'failed'}" for row in rows), code, document)
    _document(run, document)
    return code


def _registry_edit(run: _Run, settings: Settings, context: ModeContext) -> int:
    """Add or remove a ``[[registry]]`` entry in the settings file of the chosen layer."""
    args = run.args
    layer, path = _layer_file(context)
    try:
        if args.registry_command == "add":
            settingsedit.add_registry(path, layer, args.name, args.source, settings.registries)
        else:
            settingsedit.remove_registry(path, layer, args.name, settings.registries)
    except settingsedit.SettingsEditError as error:
        return _fail(run, f"registry {args.registry_command}", SettingsError(str(error)))
    _human(run, f"{args.registry_command}: {args.name} ({path})")
    _document(run, results.envelope(f"registry {args.registry_command}", ok=True, exit_code=EXIT_OK, file=str(path), name=args.name))
    return EXIT_OK


def _layer_file(context: ModeContext) -> tuple[str, Path]:
    """Return the settings layer and file that a write for *context*'s mode goes to (``<root>\\.gupkg\\settings.toml``)."""
    layer = {"system": LAYER_SYSTEM, "user": LAYER_USER, "portable": LAYER_PORTABLE}[context.mode]
    return layer, settings_file(context.root)


def _cmd_settings(run: _Run) -> int:
    """Run ``settings show|path|init|set``: read the layers, or change the file of one layer."""
    args = run.args
    action = args.settings_command
    loaded = run.loader()
    if action == "show":
        rows = [{"key": item.key, "value": item.value, "layer": item.layer} for item in loaded.settings.effective()]
        if run.json:
            _document(run, results.envelope("settings show", ok=True, exit_code=EXIT_OK, settings=rows))
        else:
            print("\n".join(f"{row['key']} = {row['value']!r}    ({row['layer']})" for row in rows))
        return EXIT_OK
    if action == "path":
        layers = [
            {"layer": item.name, "path": str(item.path) if item.path is not None else None, "status": item.status, "reason": item.reason}
            for item in loaded.layers
        ]
        if run.json:
            _document(run, results.envelope("settings path", ok=True, exit_code=EXIT_OK, layers=layers))
        else:
            print("\n".join(f"{row['layer']:<9} {row['path'] or '(built in)'}  [{row['status']}{': ' + row['reason'] if row['reason'] else ''}]" for row in layers))
        return EXIT_OK

    # init and set write one layer's file: the user's by default, the system's from an administrator shell.
    _, context = _context(run, args.mode)
    layer, path = _layer_file(context)
    if context.mode == "system" and not run.elevated:
        return _fail(run, f"settings {action}", ModeRefused("The system settings are written from an administrator shell."))
    try:
        if action == "init":
            settingsedit.init_settings(path, layer, force=args.force)
        else:
            settingsedit.set_setting(path, layer, args.key, args.value)
    except settingsedit.SettingsEditError as error:
        return _fail(run, f"settings {action}", SettingsError(str(error)))
    _human(run, f"{action}: {path}" + ("" if action == "init" else f" ({args.key} set; comments in the file are not kept)"))
    _document(run, results.envelope(f"settings {action}", ok=True, exit_code=EXIT_OK, file=str(path), layer=layer))
    return EXIT_OK


# ---------------------------------------------------------------------------
# pkg and self
# ---------------------------------------------------------------------------


def _cmd_pkg(run: _Run) -> int:
    """Run the authoring commands ``pkg new|check|pin|bundle|export``."""
    args = run.args
    action = args.pkg_command
    command = f"pkg {action}"
    try:
        if action == "new":
            name, _, version = args.name.partition("@")
            written = authoring.new_package(Path(args.dir) if args.dir else Path.cwd(), name, version or None, args.track)
            _human(run, "\n".join(f"wrote {item}" for item in written))
            _document(run, results.envelope(command, ok=True, exit_code=EXIT_OK, files=[str(item) for item in written]))
            return EXIT_OK
        if action == "check":
            return _pkg_check(run)
        if action == "pin":
            return _pkg_pin(run)
        if action == "export":
            exported = authoring.export_distribution(Path(args.directory), Path(args.output))
            print(exported.sha256, file=sys.stderr if run.json else sys.stdout)
            _document(run, results.envelope(
                command, ok=True, exit_code=EXIT_OK, file=str(exported.path), sha256=exported.sha256, entries=exported.entries))
            return EXIT_OK
        return _pkg_bundle(run)
    except (AuthoringError, PinError) as error:
        print(f"error: {error}", file=sys.stderr)
        _document(run, results.error_document(command, "refused", str(error), EXIT_USER))
        return EXIT_USER


def _pkg_check(run: _Run) -> int:
    """Validate a version directory, a tracker or a whole package root; ``--install`` also runs a sandboxed install."""
    args = run.args
    path = Path(args.path)
    folders = [path]
    if not (path / "pkg.toml").is_file() and path.is_dir():
        folders = sorted(item for item in path.iterdir() if item.is_dir() and (item / "pkg.toml").is_file())
    reports = [check_directory(folder) for folder in folders]
    rows = [
        {"path": str(item.path), "kind": item.kind.value if item.kind is not None else None, "ok": item.ok,
         "errors": list(item.errors), "warnings": list(item.warnings)}
        for item in reports
    ]
    code = EXIT_OK if reports and all(item.ok for item in reports) else EXIT_USER
    for item in reports:
        for line in item.errors:
            print(f"error: {item.path}: {line}", file=sys.stderr)
        for line in item.warnings:
            print(f"warning: {item.path}: {line}", file=sys.stderr)
    if not reports:
        print(f"error: {path} holds no pkg.toml", file=sys.stderr)
    extra: dict[str, Any] = {}
    if args.install and code == EXIT_OK:
        installed = _sandbox_install(run, reports[0].path)
        extra["install"] = installed
        code = max(code, installed["exit_code"])
    _human(run, "ok" if code == EXIT_OK else "failed")
    _document(run, results.envelope("pkg check", ok=code == EXIT_OK, exit_code=code, checks=rows, **extra))
    return code


def _sandbox_install(run: _Run, version_dir: Path) -> dict[str, Any]:
    """Install *version_dir* into a throw-away root on a pretend machine and return the result document of that install.

    Files and downloads are real, the registry, Start Menu and ``PATH`` are fakes kept in a scratch folder (design section
    16), and the settings are a scratch file that names scratch roots, so nothing of the real machine changes. An elevated
    run refuses: the package's own code would run with an administrator's rights.
    """
    if run.elevated:
        raise ModeRefused("`pkg check --install` runs the package's own code: run it from a normal shell.")
    with run.system.scratch_directory("gupkg-pkgcheck-") as scratch:
        scratch = Path(scratch)
        roots = {name: scratch / name for name in ("system", "user", "portable")}
        text = (
            'schema = 3\ndefault_mode = "user"\nregistry = []\n'
            f"[roots]\nsystem = '{roots['system']}'\nuser = '{roots['user']}'\nportable = '{roots['portable']}'\n"
            f"[integration.user]\nbin = '{scratch / 'bin'}'\nstart_menu = '{scratch / 'menu'}'\n"
        )
        settings_path = scratch / "settings.toml"
        settings_path.write_text(text, encoding="utf-8")
        pretend = FakeSystem.sandbox(scratch / "machine", run.system)
        loader = settings_loader(
            system=pretend, env=run.env, elevated=False, settings_path=settings_path,
            read_text=lambda path: text if Path(path) == settings_path else None,
        )
        engine = Engine(system=pretend, env=run.env, load=loader, yes=True, no_input=True)
        batch = lifecycle.run_batch(engine, [Request("install", str(version_dir), mode="user", skip=parse_skip_flag(run.args.skip_checksum))])
    return results.result_document("install", batch)


def _pkg_pin(run: _Run) -> int:
    """Show where a receipt's hash came from and, once confirmed, copy it into the version's ``pkg.toml``."""
    args = run.args
    resolved = _catalog(run).resolve(_qualify(args.target, args.mode), for_mutation=True)
    view = resolved.view
    version = resolved.version or (view.installed.version if view.installed is not None else None)
    if view.local is None or version is None or view.local.version(version) is None:
        raise PinError(f"'{args.target}' is not a local version, so there is nothing to pin")
    folder = view.local.version(version).path
    receipt = read_receipt(view.local.path, version)
    record = receipt_checksum(receipt.data) if receipt.data is not None else None
    if record is None or record.observed_sha256 is None:
        raise PinError(f"{view.name} {version} has no receipt with an observed hash (it was verified, or never installed without a checksum)")

    # An install that was verified against a hash that pkg.toml declares has nothing to pin.
    if record.verified:
        _human(run, f"{view.name} {version} was verified against the hash that its pkg.toml declares: nothing to pin")
        _document(run, results.envelope(
            "pkg pin", ok=True, exit_code=EXIT_OK, package=view.name, version=version, changed=False, verified=True,
            sha256=record.observed_sha256, url=record.url, final_url=record.final_url, size=record.size,
        ))
        return EXIT_OK

    # Show the facts the user is about to rely on, and say plainly that the hash is only what one unverified download produced.
    saved = (receipt.data or {}).get("run", {})
    facts = [
        f"package {view.name} {version}",
        f"url:       {record.url or '-'}",
        f"final url: {record.final_url or '-'}",
        f"size:      {record.size if record.size is not None else '-'} bytes",
        f"date:      {saved.get('finished', '-') if isinstance(saved, dict) else '-'}",
        f"sha256:    {record.observed_sha256}",
        "This hash comes from a download that was NOT verified. Pinning it makes later installs fail if the file changes, "
        "but it proves nothing about the file you got the first time.",
    ]
    for line in facts:
        print(line, file=sys.stderr if run.json else sys.stdout)
    if not args.yes:
        question = f"Write this hash into {folder / 'pkg.toml'}?"
        if args.no_input:
            print(f"error: {question} Pass --yes to confirm. Nothing was changed.", file=sys.stderr)
            _document(run, results.error_document("pkg pin", "input-needed", question, EXIT_INPUT))
            return EXIT_INPUT
        print(question, file=sys.stderr)
        if not _ask(question):
            print("Declined. Nothing was changed.", file=sys.stderr)
            _document(run, results.error_document("pkg pin", "input-needed", "Declined. Nothing was changed.", EXIT_INPUT))
            return EXIT_INPUT
    outcome = pin_hash(folder, record)
    _human(run, "pinned" if outcome.changed else "already pinned")
    _document(run, results.envelope(
        "pkg pin", ok=True, exit_code=EXIT_OK, package=view.name, version=version, changed=outcome.changed, verified=False,
        sha256=record.observed_sha256, url=record.url, final_url=record.final_url, size=record.size,
    ))
    return EXIT_OK


def _pkg_bundle(run: _Run) -> int:
    """Copy the verified bundle of the running gupkg into a version directory (the launchers are written by issue 033)."""
    status = self_status(run.location)
    if status.version_root is None or status.integrity is None or not status.integrity.ok:
        raise AuthoringError(
            "the running gupkg has no verified bundle to copy: "
            + ("; ".join(status.integrity.problems()) if status.integrity is not None else "it is a source checkout or its manifest is unreadable")
        )
    copied = authoring.bundle_gupkg(Path(run.args.directory), Path(status.version_root) / "gupkg")
    _human(run, f"copied {copied} files into {Path(run.args.directory) / 'gupkg'}")
    _document(run, results.envelope("pkg bundle", ok=True, exit_code=EXIT_OK, files=copied, version=status.version))
    return EXIT_OK


def _cmd_self(run: _Run) -> int:
    """Run ``self status|repair|check-updates|update``; the last three are the lifecycle commands applied to ``gupkg``."""
    args = run.args
    action = args.self_command
    if action == "status":
        status = self_status(run.location)
        if run.json:
            _document(run, results.envelope("self status", ok=status.ok, exit_code=EXIT_OK if status.ok else EXIT_FAILED, **status.to_data()))
        else:
            print(f"gupkg {status.version} ({status.kind}) at {status.location}")
            print(f"interpreter: {status.interpreter} ({'bundled' if status.interpreter_bundled else 'NOT the bundled Python'})")
            for note in status.notes:
                print(f"note: {note}")
            for problem in status.problems():
                print(f"problem: {problem}")
            if status.ok:
                print("ok")
        return EXIT_OK if status.ok else EXIT_FAILED

    # The aliases are exactly the lifecycle commands on the package ``gupkg``, in the mode this gupkg lives in.
    command = {"repair": "repair", "check-updates": "check-updates", "update": "update"}[action]
    if args.mode is None and run.location.kind == "installed" and run.location.root is not None:
        args.mode = _mode_of_root(run, run.location.root)
    args.command = command
    return _lifecycle(run, command, ["gupkg"], _requests(args, command))


def _mode_of_root(run: _Run, root: Path) -> str | None:
    """Return the mode of the configured root that *root* is, or ``None`` when it is none of them."""
    settings = run.loader().settings
    for mode in ("system", "user", "portable"):
        if os.path.normcase(os.path.normpath(expand_env(settings[f"roots.{mode}"], run.env))) == os.path.normcase(os.path.normpath(root)):
            return mode
    return None


# ---------------------------------------------------------------------------
# The separate programs
# ---------------------------------------------------------------------------


def _forwarder(tokens: Sequence[str]) -> tuple[str, list[str], list[str]] | None:
    """Return ``(program, arguments, root options)`` when the command is ``tui``, ``manager`` or ``migrate``, else ``None``.

    The root options (``--settings PATH``, ``--sandbox DIR``) may come first; everything after the command word belongs to
    the program and is passed on unchanged, ``--help`` included.
    """
    index, root_options = 0, []
    while index < len(tokens):
        token = tokens[index]
        if token in ("--settings", "--sandbox"):
            root_options += tokens[index:index + 2]
            index += 2
        elif token.startswith(("--settings=", "--sandbox=")):
            root_options.append(token)
            index += 1
        else:
            break
    if index < len(tokens) and tokens[index] in FORWARDERS:
        return tokens[index], list(tokens[index + 1:]), root_options
    return None


def _forward(program: str, rest: list[str], root_options: list[str], system: System, location: RunningGupkg) -> int:
    """Start the program of the same gupkg tree with *rest* and return its exit code."""
    if root_options:
        print(f"gupkg: error: --settings and --sandbox are not passed on to gupkg-{program}", file=sys.stderr)
        return EXIT_USER
    # The UI programs ask for administrator rights themselves, through `gupkg`; the migration tool does its own work and
    # needs the token to adopt the system root, so only `tui` and `manager` refuse an elevated shell (D44, D45). It still
    # starts through its own launcher, which runs bootstrap's trust check before importing anything.
    if program != "migrate" and is_elevated_run(system):
        print(
            f"gupkg: error: gupkg {program} is not started from an elevated shell: the programs call `gupkg` and ask for "
            "administrator rights once themselves. Run it from a normal shell.", file=sys.stderr,
        )
        return EXIT_USER
    launcher = location.launcher(FORWARDERS[program])
    if launcher is None:
        print(
            f"gupkg: error: gupkg-{program} is not part of this gupkg ({FORWARDERS[program]} was not found beside "
            f"{location.package_dir}).", file=sys.stderr,
        )
        return EXIT_USER
    try:
        return system.run_foreground([str(launcher), *rest])
    except OSError as error:
        print(f"gupkg: error: cannot start {launcher}: {error}", file=sys.stderr)
        return EXIT_USER


# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


_HANDLERS: dict[str, Callable[[_Run], int]] = {
    **{name: _engine_command for name in _ENGINE_COMMANDS},
    "list": _cmd_list, "info": _cmd_info, "search": _cmd_search, "logs": _cmd_logs, "shell": _cmd_shell,
    "registry": _cmd_registry, "settings": _cmd_settings, "pkg": _cmd_pkg, "self": _cmd_self,
}

# The four spellings the issue keeps as usage errors that name the 1.0 way (they are not aliases, decision D33); every other
# removed 0.17 option is simply unknown to argparse.
_REMOVED_OPTIONS = {
    "--all": "gupkg has no --all (decisions D11, D12): the batch form is `gupkg-manager update --all`, or "
             "`gupkg update $(gupkg list --outdated --names)`",
    "--check": "`update` never looks for new versions: use `gupkg check-updates <target>` first, then `gupkg update <target>`",
    "--check-only": "use `gupkg check-updates <target> --no-download`",
    "--scope": "use --mode system|user|portable, after the command",
}


class _Parser(argparse.ArgumentParser):
    """An argument parser that raises ``UsageError`` instead of exiting, and refuses abbreviated options."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> Any:  # type: ignore[override]
        """Raise the usage error; ``main`` prints it and returns exit code 2."""
        raise UsageError(message)


class _Removed(argparse.Action):
    """A removed 0.17 option: seeing it is a usage error that says what to use now."""

    def __call__(self, parser: argparse.ArgumentParser, namespace: Any, values: Any, option_string: str | None = None) -> None:
        raise UsageError(f"{option_string} is not an option of this command: {_REMOVED_OPTIONS[option_string or '']}")


def _normalise(tokens: Sequence[str]) -> list[str]:
    """Spell a bare ``--skip-checksum`` as ``--skip-checksum=`` so that it never takes the next word (a target) as its reason."""
    return ["--skip-checksum=" if token == "--skip-checksum" else token for token in tokens]


def _events_file_name(text: str) -> str:
    """Accept only a plain, safe file name for ``--events-file``; anything else is a usage error and nothing is written."""
    try:
        return validate_log_file_name(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _options(
    parser: argparse.ArgumentParser, *, mutating: bool = False, plan: bool = False, mode: bool = True, skip: bool = False,
    events: bool = False, relaunch: bool = False,
) -> None:
    """Add the options a command shares: ``--mode``, ``--json``, ``--no-input``, ``--yes``, ``--plan``, ``--skip-checksum``.

    ``relaunch`` adds the two hidden markers of the elevated child (``--relaunched``, ``--batch-confirmed``) to the commands that
    run through the engine; no other command can be started as a relaunch.
    """
    if mode:
        parser.add_argument("--mode", choices=("system", "user", "portable"), help="the install mode (default: default_mode, auto)")
    parser.add_argument("--json", action="store_true", help="print one JSON result document on standard output; human text goes to standard error")
    parser.add_argument("--no-input", action="store_true", help="never ask: a question that would be asked is exit code 5")
    if mutating:
        parser.add_argument("--yes", action="store_true", help="answer the confirmations (several targets, --purge, elevation) with yes")
    else:
        parser.set_defaults(yes=False)
    if plan:
        parser.add_argument("--plan", action="store_true", help="resolve and plan without changing anything")
    if skip:
        parser.add_argument(
            "--skip-checksum", nargs="?", const="", default=None, metavar="REASON",
            help="install a download that has no SHA-256 (https:// URLs only); the manager relays --skip-checksum=setting "
                 "or =user-approved",
        )
    if events:
        parser.add_argument(
            "--events-file", metavar="NAME", type=_events_file_name,
            help="also write the run's events as JSON lines to the plain file NAME (not a path) in the run's log directories",
        )
    if relaunch:
        parser.add_argument("--relaunched", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--stop-file", default=None, help=argparse.SUPPRESS)
        parser.add_argument("--batch-confirmed", action="store_true", help=argparse.SUPPRESS)
    for option in _REMOVED_OPTIONS:
        parser.add_argument(option, action=_Removed, nargs=0, help=argparse.SUPPRESS)


HELP_EPILOG = """\
commands, in the order of a task:
  check-updates  find new versions and prepare them (activates nothing)
  install        install or repair a package
  update         activate the newest known version
  list           show the packages and their status
  info           show everything known about a package

maintenance:
  switch         activate another local version (rollback)
  uninstall      remove the integration (--purge deletes the package too)
  check          health check: drift, App fingerprint, registry fingerprint (--fix repairs)
  cleanup        remove old Apps, receipts, work folders and logs
  link           follow another registry entry, or none
  logs           list, print or open the logs of a package
  search         find registry packages
  shell          print the PATH line of an integrate = "none" root
  registry       sync | status | list | add | remove
  settings       show | path | init | set

authoring:
  pkg            new | check | pin | bundle | export

gupkg itself:
  self           status | repair | check-updates | update

separate programs of this gupkg:
  tui            gupkg tui PATH starts gupkg-tui
  manager        gupkg manager ... starts gupkg-manager
  migrate        gupkg migrate ... starts gupkg-migrate

Targets are names (firefox, firefox@135.0, user:firefox, main/firefox) or paths. Options go after the command.
Exit status: 0 ok, 2 user or configuration error, 3 the operation failed, 4 internal error, 5 declined or input needed,
6 the package needs a newer gupkg. Add --json for scripts. Run 'gupkg <command> --help' for the options of one command."""


def _build_parser() -> argparse.ArgumentParser:
    """Build the whole grammar of design section 10.2 without looking at the machine."""
    parser = _Parser(
        prog="gupkg",
        description="Package manager for Windows applications that need no installer (gupkg).",
        epilog=HELP_EPILOG,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--settings", type=Path, metavar="PATH", help="read this settings file as the highest layer")
    parser.add_argument(
        "--sandbox", type=Path, metavar="DIR",
        help="run against a pretend machine kept in DIR: environment variables, PATH, shortcuts and elevation are recorded "
             "in DIR instead of the registry and Start Menu; files and downloads are real",
    )
    for option in ("--scope",):
        parser.add_argument(option, action=_Removed, nargs=0, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)

    def add(name: str, text: str, example: str, **options: Any) -> argparse.ArgumentParser:
        """Add one command with its description and an example."""
        command = commands.add_parser(name, description=text, epilog=f"example:\n  {example}", **options)
        return command

    sub = add("check-updates", "Run the trackers of the named packages and write (and by default populate) their new versions.",
              "gupkg check-updates firefox --no-download")
    sub.add_argument("targets", nargs="+", metavar="TARGET")
    sub.add_argument("--no-download", action="store_true", help="write the version directory only; download nothing")
    _options(sub, mutating=True, plan=True, skip=True, events=True, relaunch=True)

    sub = add("install", "Install (or repair) packages: download App if missing, then create shortcuts, PATH entries and shims.",
              "gupkg install firefox --mode user")
    sub.add_argument("targets", nargs="+", metavar="TARGET")
    sub.add_argument("--refresh", action="store_true", help="rebuild an App that can be rebuilt")
    sub.add_argument("--in-place", action="store_true", help="install a distribution folder where it is (an ad-hoc root)")
    _options(sub, mutating=True, plan=True, skip=True, events=True, relaunch=True)

    sub = add("update", "Activate the newest known version of each installed package; never runs a tracker.",
              "gupkg update firefox")
    sub.add_argument("targets", nargs="+", metavar="TARGET")
    _options(sub, mutating=True, plan=True, skip=True, events=True, relaunch=True)

    sub = add("switch", "Activate another local version of an installed package (the way back).",
              "gupkg switch firefox 134.0")
    sub.add_argument("targets", nargs=1, metavar="TARGET")
    sub.add_argument("version")
    _options(sub, mutating=True, plan=True, events=True, relaunch=True)

    sub = add("uninstall", "Remove the integration of packages; --purge also deletes the package folder and its data.",
              "gupkg uninstall firefox --purge")
    sub.add_argument("targets", nargs="+", metavar="TARGET")
    sub.add_argument("--purge", action="store_true", help="also delete the package folder, including its data")
    _options(sub, mutating=True, plan=True, events=True, relaunch=True)

    sub = add("check", "Health check: drift, App and registry fingerprints, unverified installs. Changes nothing without --fix.",
              "gupkg check firefox --fix")
    sub.add_argument("targets", nargs="*", metavar="TARGET", help="packages to check (default: every package)")
    sub.add_argument("--fix", action="store_true", help="re-apply the integration and rebuild a missing App")
    _options(sub, mutating=True, events=True, relaunch=True)

    sub = add("list", "List the packages of the roots with their status; nothing is checked over the network.",
              "gupkg list --outdated --names")
    kinds = sub.add_mutually_exclusive_group()
    for name, text in (("installed", "installed packages"), ("available", "registry packages that are not installed"),
                       ("outdated", "packages with a newer known version"), ("broken", "packages that need repair"),
                       ("local", "local packages that are not installed")):
        kinds.add_argument(f"--{name}", action="store_true", help=text)
    sub.add_argument("--details", action="store_true", help="versions, tracker, link and how each package was verified")
    sub.add_argument("--names", action="store_true", help="print only the names, one per line (for scripts)")
    _options(sub)

    sub = add("info", "Show everything known about each package: versions, link, tracker, last check, receipts and log.",
              "gupkg info firefox")
    sub.add_argument("targets", nargs="+", metavar="TARGET")
    _options(sub)

    sub = add("search", "Find registry packages by name.", "gupkg search editor")
    sub.add_argument("query", nargs="?", default="")
    sub.add_argument("--registry", metavar="NAME", help="only this registry")
    sub.add_argument("--offline", action="store_true", help="read the cache only; never use the network")
    _options(sub)

    sub = add("cleanup", "Remove old Apps, receipts, work folders, registry trees and logs; never runs by itself.",
              "gupkg cleanup --keep 1")
    sub.add_argument("targets", nargs="*", metavar="TARGET", help="packages to clean (default: every package of the roots)")
    sub.add_argument("--keep", type=int, default=1, metavar="N", help="older Apps to keep (default: 1)")
    sub.add_argument("--bundled", action="store_true", help="also drop bundled gupkg copies where a root forwarder exists")
    _options(sub, mutating=True, plan=True, events=True, relaunch=True)

    sub = add("link", "Follow another registry entry (registry/name), the entry of the same name, or none (--unlink).",
              "gupkg link firefox main/firefox-esr")
    sub.add_argument("target", metavar="TARGET")
    sub.add_argument("entry", nargs="?", metavar="REGISTRY/NAME")
    sub.add_argument("--unlink", action="store_true", help="follow no registry entry")
    _options(sub, mutating=True, plan=True, events=True, relaunch=True)

    sub = add("logs", "List the run logs of a package (or of the root); --path prints the newest, --open opens it.",
              "gupkg logs firefox --path")
    sub.add_argument("target", nargs="?", metavar="TARGET")
    group = sub.add_mutually_exclusive_group()
    group.add_argument("--open", action="store_true", help="open the newest log")
    group.add_argument("--path", action="store_true", help="print the path of the newest log")
    _options(sub)

    sub = add("shell", "Print the PATH line for a root that does not touch the environment (integrate = \"none\").",
              "gupkg shell --mode portable")
    _options(sub)

    sub = add("registry", "Synchronise, inspect and configure the registries.", "gupkg registry sync")
    actions = sub.add_subparsers(dest="registry_command", required=True, metavar="<action>", parser_class=_Parser)
    for name, text in (("sync", "download the registries"), ("status", "show each cache's revision"),
                       ("list", "list the configured registries"), ("add", "add a registry"), ("remove", "remove a registry")):
        leaf = actions.add_parser(name, description=text.capitalize() + ".", epilog=f"example:\n  gupkg registry {name}" + (" main" if name == "remove" else (" extra https://example.org/registry.zip" if name == "add" else "")))
        if name == "sync":
            leaf.add_argument("names", nargs="*", metavar="NAME")
            leaf.add_argument("--offline", action="store_true", help="report the cache without any request")
        if name == "add":
            leaf.add_argument("name")
            leaf.add_argument("source")
        if name == "remove":
            leaf.add_argument("name")
        _options(leaf, events=name == "sync")

    sub = add("settings", "Show, locate, create and change settings files.", "gupkg settings show")
    actions = sub.add_subparsers(dest="settings_command", required=True, metavar="<action>", parser_class=_Parser)
    for name, text in (("show", "show every effective setting and its layer"), ("path", "show the settings files of every layer"),
                       ("init", "create a settings file"), ("set", "set one value")):
        leaf = actions.add_parser(name, description=text.capitalize() + ".", epilog=f"example:\n  gupkg settings {name}" + (" logs.keep_runs 20" if name == "set" else ""))
        if name == "init":
            leaf.add_argument("--force", action="store_true", help="replace an existing file")
        if name == "set":
            leaf.add_argument("key")
            leaf.add_argument("value")
        _options(leaf, mode=name in ("init", "set"))

    sub = add("pkg", "Authoring: create, validate, pin, bundle and export package definitions.", "gupkg pkg check .")
    actions = sub.add_subparsers(dest="pkg_command", required=True, metavar="<action>", parser_class=_Parser)
    leaf = actions.add_parser("new", description="Write a starter package definition.", epilog="example:\n  gupkg pkg new mytool --track github:owner/mytool")
    leaf.add_argument("name", metavar="NAME[@VERSION]")
    leaf.add_argument("--track", metavar="github:OWNER/REPO", help="write a tracker that follows the releases of a GitHub repository")
    leaf.add_argument("--dir", metavar="FOLDER", help="the folder that receives the package (default: the current folder)")
    _options(leaf, mode=False)
    leaf = actions.add_parser("check", description="Validate a version directory, a tracker or a package folder.", epilog="example:\n  gupkg pkg check .\\mytool\\1.0 --install")
    leaf.add_argument("path")
    leaf.add_argument("--install", action="store_true", help="also run a sandboxed install (files and downloads are real)")
    _options(leaf, mode=False, skip=True)
    leaf = actions.add_parser("pin", description="Copy the hash of an install without a checksum into the version's pkg.toml.", epilog="example:\n  gupkg pkg pin mytool@1.0")
    leaf.add_argument("target", metavar="TARGET")
    _options(leaf, mutating=True)
    leaf = actions.add_parser("bundle", description="Copy the running gupkg's verified bundle into a version directory.", epilog="example:\n  gupkg pkg bundle .\\mytool\\1.0")
    leaf.add_argument("directory", metavar="VERSION-DIR")
    _options(leaf, mode=False)
    leaf = actions.add_parser("export", description="Write a distribution ZIP of a version directory and print its SHA-256.", epilog="example:\n  gupkg pkg export .\\mytool\\1.0 -o mytool-1.0.zip")
    leaf.add_argument("directory", metavar="VERSION-DIR")
    leaf.add_argument("-o", "--output", required=True, metavar="X.zip")
    _options(leaf, mode=False)

    sub = add("self", "Check and maintain gupkg itself; check-updates and update are `gupkg check-updates gupkg` and `gupkg update gupkg`.",
              "gupkg self status")
    actions = sub.add_subparsers(dest="self_command", required=True, metavar="<action>", parser_class=_Parser)
    leaf = actions.add_parser("status", description="Report the interpreter and the MANIFEST.sha256 check.", epilog="example:\n  gupkg self status")
    _options(leaf, mode=False)
    for name, text in (("repair", "re-apply gupkg's own integration"), ("check-updates", "look for a new gupkg release"),
                       ("update", "activate the newest known gupkg")):
        leaf = actions.add_parser(name, description=text.capitalize() + ".", epilog=f"example:\n  gupkg self {name}")
        if name == "check-updates":
            leaf.add_argument("--no-download", action="store_true", help="write the version directory only")
        _options(leaf, mutating=True, plan=True, skip=name != "repair", events=True, relaunch=True)
        if name != "check-updates":
            leaf.set_defaults(no_download=False)
        leaf.set_defaults(targets=None)

    # The separate programs appear in the help and are started before argparse sees their arguments.
    for word, program in FORWARDERS.items():
        commands.add_parser(word, description=f"Start {program.removesuffix('.cmd')} of this gupkg with the same arguments.",
                            epilog=f"example:\n  gupkg {word} ...", add_help=False)
    return parser
