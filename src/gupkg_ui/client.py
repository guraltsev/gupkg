"""Run the sibling ``gupkg`` command line as a subprocess and use its machine interface (the only module of the UI programs that starts a program).

The UI programs never change the machine themselves (decision D44): every operation is a ``gupkg`` call through ``--json``,
``--plan``, ``--events-file`` and ``--no-input``. This module finds the ``gupkg.cmd`` launcher that sits in the same gupkg tree
as the running program (never one found on ``PATH``), builds the command line as an argument list that no shell parses, refuses
an argument that holds a character the launcher could misread, runs the call without ever blocking the caller's event loop,
follows the call's events file while it runs and returns one ``CallResult``. Everything that comes back from the call is untrusted:
the result document must be one JSON object with ``"api": 2``, the output is capped, and a call whose result cannot be read, whose
exit code disagrees with its document or whose events file has no ``run_end`` is a failure (``CallResult.problems``), never a success.

Usage and API
-------------
``sibling_launcher()`` returns the launcher of this gupkg tree. ``Client(command, root_options=...)`` is the handle that
runs calls: ``await client.query(args)`` runs a short read-only call (``list``, ``info``, ``--plan``) and returns its document;
``await client.call(args, events_name=..., events_dir=..., on_records=...)`` runs an operation, follows its events file and
returns the ``CallResult``. ``StopRequest.request()`` creates the stop marker that the run announced. Arguments after the
command word go through ``targets_after_options`` so that a target can never be mistaken for an option.

Implementation Approach
-----------------------
The call runs under ``asyncio`` subprocess support, standard input is closed (the CLI is always called with ``--no-input``),
standard output is collected with a hard cap and standard error keeps only its tail. The events file is polled by an
``EventStream`` between reads of the process, so the caller sees progress while the call runs and a final drain after it ended.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gupkg.report.events import API_VERSION
from gupkg.selfmgmt.location import RunningGupkg, locate_running_gupkg
from gupkg.system.port import unsafe_launcher_argument

from .follow import EventStream, read_run_end

# The largest result document that is read from standard output (anything longer is a failed call).
MAX_DOCUMENT_BYTES = 16 << 20
# The tail of standard error that is kept for the report of a failed call (the human text is also in the log files).
STDERR_TAIL_BYTES = 64 << 10
# Seconds between two looks at the events file while a call runs.
POLL_SECONDS = 0.1
# When a call ends abnormally (the program closes, a timeout), how long the CLI gets to stop after its current package before its whole process tree is killed.
STOP_GRACE_SECONDS = 5.0
# After a cancel request, how long a call may keep running before its process tree is killed.
FORCE_STOP_SECONDS = 30.0
# After ``run_end`` was written, how long the process may take to exit before its tree is killed (the result of ``run_end`` is still used).
EXIT_GRACE_SECONDS = 15.0
# Seconds without any output after which the owner of a call is told that it is still running (nothing is killed).
QUIET_SECONDS = 60.0


class ClientError(Exception):
    """Report that a call could not be made or its result cannot be trusted."""


# ---------------------------------------------------------------------------
# Finding the command and building command lines
# ---------------------------------------------------------------------------


def sibling_launcher(location: RunningGupkg | None = None) -> Path:
    """Return the ``gupkg.cmd`` launcher of the gupkg tree this program belongs to.

    Parameters
    ----------
    location : RunningGupkg, optional
        Where the running gupkg lives; found from the loaded packages by default.

    Returns
    -------
    Path
        The resolved launcher, a regular file below the tree's own folder.

    Raises
    ------
    ClientError
        If the tree has no ``gupkg.cmd`` or the file resolves outside the tree (a link planted to run something else).
    """
    where = location or locate_running_gupkg()
    tree = where.package_dir.parent.resolve()

    # A source checkout keeps the real launcher beside the sources, not the selector one level up (which searches PATH).
    folders = (where.package_dir,) if where.kind == "source" else where.launcher_dirs
    for folder in folders:
        candidate = Path(folder) / "gupkg.cmd"
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved.name.lower() != "gupkg.cmd" or not resolved.is_relative_to(tree):
            raise ClientError(f"{candidate} resolves to {resolved}, which is outside this gupkg tree {tree}")
        return resolved
    raise ClientError(f"gupkg.cmd was not found in this gupkg tree ({tree}); the UI programs run only their own gupkg")


def refuse_unsafe(arguments: Sequence[str]) -> None:
    """Refuse any argument that holds a character that must not reach a command line.

    Parameters
    ----------
    arguments : sequence of str
        The arguments that are about to be put on a command line for the ``.cmd`` launcher.

    Raises
    ------
    ClientError
        If an argument holds a shell or batch metacharacter (``&``, ``|``, ``<``, ``>``, ``^``, ``"``, ``%``, ``!``) or a control character.
    """
    # Check every argument before anything is started, so one unsafe name stops the whole call.
    for item in arguments:
        reason = unsafe_launcher_argument(item)
        if reason is not None:
            raise ClientError(f"refused: {reason} cannot be passed to gupkg safely; use a name without it")


def with_options(args: Sequence[str], options: Sequence[str]) -> list[str]:
    """Return *args* with *options* put before the ``--`` that starts the targets.

    Parameters
    ----------
    args : sequence of str
        The command word, its options and, after ``--``, its targets (everything after ``--`` is a target).
    options : sequence of str
        The options to add.

    Returns
    -------
    list of str
        *args* with *options* inserted before ``--`` (at the end when there is no ``--``).
    """
    items = list(args)
    split = items.index("--") if "--" in items else len(items)
    return [*items[:split], *options, *items[split:]]


def targets_after_options(command: str, options: Sequence[str], targets: Sequence[str]) -> tuple[str, ...]:
    """Return ``command options -- targets`` so that no target can be taken for an option.

    Parameters
    ----------
    command : str
        The command word.
    options : sequence of str
        The options, placed before ``--``.
    targets : sequence of str
        The targets, placed after ``--`` (no ``--`` at all when there are none).

    Returns
    -------
    tuple of str
        The arguments of the call.
    """
    return (command, *options, *(("--", *targets) if targets else ()))


# ---------------------------------------------------------------------------
# The result of a call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallResult:
    """What one ``gupkg`` call did, as far as it can be trusted.

    Methods
    -------
    entries
        Return the per-target entries of the call's document.
    errors
        Return the call-level errors of the call's document.

    Attributes
    ----------
    argv : tuple of str
        The command line that was run.
    exit_code : int or None
        The process exit code; ``None`` when the program could not be started.
    document : dict or None
        The ``--json`` result document of standard output (``api`` and head checked), else ``None``.
    stderr_tail : str
        The end of standard error, for the report of a failure.
    problems : tuple of str
        Reasons the result cannot be trusted: an unreadable document, an ``api`` mismatch, a missing ``run_end``, a crash.
    events_path : Path or None
        The global events file that was followed.
    stop_file : str or None
        The stop marker the run announced, when it announced one.
    warnings : tuple of str
        Things that do not make the result untrustworthy but that a person should read (a command that did not exit after finishing).
    ok : bool
        Whether the call succeeded: nothing to distrust, exit code 0 and a document that says ``ok``.
    """

    argv: tuple[str, ...]
    exit_code: int | None
    document: dict[str, Any] | None
    stderr_tail: str = ""
    problems: tuple[str, ...] = ()
    events_path: Path | None = None
    stop_file: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the call succeeded: nothing to distrust, exit code 0 and a document that says ``ok``."""
        return not self.problems and self.exit_code == 0 and bool(self.document and self.document.get("ok"))

    def entries(self) -> list[dict[str, Any]]:
        """Return the per-target entries of the call's document.

        Returns
        -------
        list of dict
            The ``results`` of the document that are objects; empty when there is no document.
        """
        return [entry for entry in (self.document or {}).get("results", ()) if isinstance(entry, dict)]

    def errors(self) -> list[dict[str, Any]]:
        """Return the call-level errors of the call's document.

        Returns
        -------
        list of dict
            The ``errors`` of the document that are objects (a usage error, a question that was not answered); empty when there is no document.
        """
        return [error for error in (self.document or {}).get("errors", ()) if isinstance(error, dict)]


def check_document(value: Any) -> dict[str, Any]:
    """Return *value* when it is a result document of this interface version, else raise ``ClientError``.

    A document is a JSON object with ``"api": 2``, a text ``command``, a boolean ``ok`` and an integer ``exit_code``; any other
    shape (or a newer ``api``) is refused, never guessed at.

    Parameters
    ----------
    value : object
        The decoded JSON of standard output.

    Returns
    -------
    dict
        *value* itself, once its head is checked.

    Raises
    ------
    ClientError
        If *value* is not a JSON object, speaks another ``api`` or lacks ``command``, ``ok`` or ``exit_code``.
    """
    # Check the shape from the outside in: object, interface version, then the head fields the callers rely on.
    if not isinstance(value, dict):
        raise ClientError("the result of gupkg is not a JSON object")
    if value.get("api") != API_VERSION:
        raise ClientError(f"gupkg speaks api {value.get('api')!r}; this program needs api {API_VERSION}: use the gupkg that came with it")
    if not isinstance(value.get("command"), str) or not isinstance(value.get("ok"), bool) or type(value.get("exit_code")) is not int:
        raise ClientError("the result of gupkg lacks command, ok or exit_code")
    return value


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class StopRequest:
    """Ask one running call to stop after its current package (decision D37), by creating the stop marker it announced.

    The marker is the file named by ``run_start.stop_file`` in the call's events file. Its name must be ``gupkg-stop-<32 hex>`` and
    its folder this program's temporary folder, so a hostile events file cannot make the UI create files elsewhere. A request made
    before the run announced its marker is carried out as soon as the announcement is read.

    Parameters
    ----------
    temp_dir : Path, optional
        The only folder where a marker may be created; the ``TEMP`` of the process environment by default.

    Methods
    -------
    request
        Record a stop request and carry it out when the marker is known.
    poll
        Carry out a recorded request once the run has announced its marker.

    Attributes
    ----------
    stream : EventStream or None
        The events stream whose ``run_start`` announces the marker; set by ``Client.call``.
    requested : bool
        Whether a stop was requested.
    created : bool
        Whether the request was decided (the marker created, already present or refused).
    requested_at : float
        The ``time.monotonic()`` of the first request.
    """

    def __init__(self, temp_dir: Path | None = None) -> None:
        self._temp_dir = temp_dir
        self.stream: EventStream | None = None
        self.requested = False
        self.created = False
        self.requested_at = 0.0

    def request(self) -> str:
        """Record the request, try to carry it out, and return what happened in words.

        Returns
        -------
        str
            A message for the person: the request was sent, is waiting for the marker, or could not be sent.
        """
        if not self.requested:
            self.requested_at = time.monotonic()
        self.requested = True
        return self.poll() or "The stop request is sent as soon as the run announces its stop marker."

    def poll(self) -> str:
        """Carry out a recorded request once the marker is known.

        Returns
        -------
        str
            A message when something was decided, else an empty string.
        """
        # Nothing to do until a stop was requested and the run announced its marker.
        stop = self.stream.stop_file if self.stream is not None else None
        if not self.requested or self.created or stop is None:
            return ""
        # Create only a marker with the exact name and folder this program expects, so a hostile events file cannot make it write elsewhere.
        path = Path(stop)
        folder = self._temp_dir or Path(os.environ.get("TEMP") or os.environ.get("TMP") or "")
        # The only name the stop marker of a run may have (the CLI creates it, the UI only creates a file where the run said).
        if not re.fullmatch(r"gupkg-stop-[0-9a-f]{32}", path.name) or not _same_folder(path.parent, folder):
            self.created = True
            return "The run announced a stop marker outside this program's temporary folder, so it cannot be cancelled from here."
        # Creating the file is the whole request; an existing file is an earlier request and counts as sent.
        try:
            with open(path, "x"):
                pass
        except FileExistsError:
            pass
        except OSError as error:
            self.created = True
            return f"The stop request could not be sent: {error}"
        self.created = True
        return "The stop request is sent: the run stops after the current package."


async def kill_tree(process: asyncio.subprocess.Process) -> None:
    """End *process* and everything it started, then wait for it.

    Parameters
    ----------
    process : asyncio.subprocess.Process
        The call to end; ``taskkill`` runs as an argument list, never through a shell.
    """
    argv = _taskkill_argv(process.pid)
    if argv is not None and process.returncode is None:
        try:
            killer = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(killer.wait(), 15)
        except (OSError, asyncio.TimeoutError):
            pass
    if process.returncode is None:
        process.kill()


def kill_tree_now(process: asyncio.subprocess.Process) -> None:
    """Run the blocking form of ``kill_tree``, for a program that is closing and cannot await.

    Parameters
    ----------
    process : asyncio.subprocess.Process
        The call to end; the work takes well under a second.
    """
    # Ask Windows to end the whole tree first, because the process held here is only the ``cmd.exe`` of the launcher.
    argv = _taskkill_argv(process.pid)
    if argv is not None:
        try:
            subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # If the parent is still there after that, kill it directly.
    if process.returncode is None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass


def end_abnormally(process: asyncio.subprocess.Process, events: EventStream | None, stop: StopRequest | None) -> None:
    """Leave no operation running headless when a call is abandoned (the program closes, a timeout).

    The CLI is first asked to stop after its current package through the stop marker (when it announced one), and given ``STOP_GRACE_SECONDS`` to write its
    ``run_end``; then the whole process tree is killed and the pipes are closed. Blocking is deliberate: this runs once, while the program is going away.

    Parameters
    ----------
    process : asyncio.subprocess.Process
        The abandoned call.
    events : EventStream or None
        Its events stream, polled while the CLI winds down.
    stop : StopRequest or None
        The request that carries the stop marker.
    """
    # Ask politely first and wait a bounded time for ``run_end``, so the package that is running is not cut off mid-step.
    if stop is not None and events is not None and process.returncode is None:
        stop.request()
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while events.run_end is None and time.monotonic() < deadline:
            events.poll()
            stop.poll()
            time.sleep(0.05)

    # Then kill the whole tree and close the pipe transport, so no handle outlives the program.
    kill_tree_now(process)
    transport = getattr(process, "_transport", None)
    if transport is not None:
        transport.close()


# --- supporting helpers ---


def _same_folder(first: Path, second: Path) -> bool:
    """Return whether two paths are the same existing folder, whatever their spelling (a short ``8.3`` name and the long one are the same folder)."""
    try:
        return second.is_dir() and os.path.samefile(first, second)
    except OSError:
        return False


def _taskkill_argv(pid: int) -> list[str] | None:
    """Return the command line that ends process *pid* and every process it started, or ``None`` when ``taskkill`` cannot be found safely.

    The launcher is a ``.cmd`` file, so the process this program holds is ``cmd.exe`` and the real command line is its child: killing only the parent would leave
    the operation running without anyone to read it. ``taskkill`` is taken from the system folder named by ``SYSTEMROOT``, never searched on ``PATH``.
    """
    base = os.environ.get("SYSTEMROOT")
    program = Path(base) / "System32" / "taskkill.exe" if base else None
    return [str(program), "/T", "/F", "/PID", str(pid)] if program is not None and program.is_file() else None


@dataclass
class Client:
    """The handle that runs ``gupkg`` calls.

    Parameters
    ----------
    command : sequence of str
        The program and any fixed leading arguments, normally ``(str(sibling_launcher()),)``.
    root_options : sequence of str, optional
        ``--settings PATH``, ``--root DIR`` and ``--sandbox DIR`` for the CLI, put before the command word of every call.
    env : mapping, optional
        The environment of the calls; ``os.environ`` by default.

    Methods
    -------
    argv
        Build the full command line of a call and refuse unsafe characters.
    query
        Run a short read-only call and return its checked document.
    call
        Run an operation, follow its events file and return the ``CallResult``.

    Attributes
    ----------
    command : tuple of str
        The program and its fixed leading arguments.
    root_options : tuple of str
        The root options put before every command word.
    env : mapping or None
        The environment of the calls.
    temp_dir : Path or None
        The temporary folder of the calls' environment, where a run puts its stop marker.
    """

    command: tuple[str, ...]
    root_options: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None

    @property
    def temp_dir(self) -> Path | None:
        """Return the temporary folder of the calls' environment, where a run puts its stop marker (``None`` when it names none)."""
        env = self.env if self.env is not None else os.environ
        text = env.get("TEMP") or env.get("TMP")
        return Path(text) if text else None

    def argv(self, args: Sequence[str]) -> list[str]:
        """Return the full command line for *args*, refusing unsafe characters.

        Parameters
        ----------
        args : sequence of str
            The command word and what follows.

        Returns
        -------
        list of str
            The launcher, the root options and *args*.

        Raises
        ------
        ClientError
            If an argument holds a character that must not reach a command line.
        """
        full = [*self.command, *self.root_options, *args]
        refuse_unsafe([*self.root_options, *args])
        return full

    async def query(self, args: Sequence[str], *, timeout: float = 120.0) -> dict[str, Any]:
        """Run a short read-only call (``list``, ``info``, ``--plan``) and return its checked document.

        The document is returned for every exit code that still produced one (a plan with a failed target is exit code 2);
        a call without a readable document raises.

        Parameters
        ----------
        args : sequence of str
            The command word, its options and its targets; ``--json`` and ``--no-input`` are added here.
        timeout : float
            Seconds after which the call is stopped and reported as failed.

        Returns
        -------
        dict
            The checked result document.

        Raises
        ------
        ClientError
            If the program cannot be started, takes longer than *timeout* seconds, or does not print one valid document.
        """
        outcome = await self._run(self.argv(with_options(args, ["--json", "--no-input"])), events=None, on_records=None, timeout=timeout)
        if outcome.document is None:
            raise ClientError(outcome.problems[0] if outcome.problems else "gupkg returned no result")
        return outcome.document

    async def call(
        self,
        args: Sequence[str],
        *,
        events_dir: Path | None,
        on_records: Callable[[list[dict[str, Any]]], None] | None = None,
        events_name: str | None = None,
        stop: StopRequest | None = None,
        on_idle: Callable[[float], None] | None = None,
    ) -> CallResult:
        """Run an operation with ``--json --no-input --events-file`` and follow its events while it runs.

        Parameters
        ----------
        args : sequence of str
            The command word, its options and (after ``--``) its targets; the machine options are added here.
        events_dir : Path, optional
            The ``.gupkg\\logs`` folder where the call writes its events file (the first target's root), or ``None`` to follow nothing.
        on_records : callable, optional
            Receives each batch of new event records on the event loop's thread.
        events_name : str, optional
            The events file name; a fresh unique one by default.
        stop : StopRequest, optional
            The request that stops this call after its current package; ``stop.request()`` may be called while the call runs. A call still running
            ``FORCE_STOP_SECONDS`` after the request is killed with its whole process tree and reported as failed.
        on_idle : callable, optional
            Called with the number of seconds when the call has written nothing for ``QUIET_SECONDS`` (and then every ``QUIET_SECONDS``); nothing is killed.

        Returns
        -------
        CallResult
            The trusted view of the call; check ``problems`` before believing ``document``.
        """
        # Name the events file, build the command line and prepare the follower before the process starts.
        name = events_name or f"gupkg-ui-{uuid.uuid4().hex}.jsonl"
        full = self.argv(with_options(args, ["--json", "--no-input", "--events-file", name]))
        stream = EventStream(Path(events_dir) / name) if events_dir is not None else None
        if stop is not None:
            stop.stream = stream
        return await self._run(full, events=stream, on_records=on_records, stop=stop, on_idle=on_idle, expect_events=events_dir is not None)

    # -- internals ---------------------------------------------------------------

    async def _run(
        self, argv: list[str], *, events: EventStream | None, on_records: Callable[[list[dict[str, Any]]], None] | None,
        timeout: float | None = None, stop: StopRequest | None = None, on_idle: Callable[[float], None] | None = None, expect_events: bool = False,
    ) -> CallResult:
        """Start the program, collect its output and follow its events file until it ends, and return the checked result."""
        # Start the process without a shell; a program that cannot even start is a failed result, not an exception.
        environment = dict(self.env) if self.env is not None else dict(os.environ)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment,
            )
        except OSError as error:
            return CallResult(tuple(argv), None, None, problems=(f"gupkg could not be started: {error}",))

        # Collect both pipes to their end, follow the events file, and wait for the process; an abandoned call is ended with its whole tree.
        flags = _Flags()
        stdout_task = asyncio.ensure_future(_read_capped(process.stdout, MAX_DOCUMENT_BYTES))
        stderr_task = asyncio.ensure_future(_read_tail(process.stderr, STDERR_TAIL_BYTES))
        follow_task = asyncio.ensure_future(_follow(process, events, on_records, stop, on_idle, flags))
        try:
            await asyncio.wait_for(asyncio.gather(process.wait(), stdout_task, stderr_task), timeout)
            await follow_task
        except asyncio.TimeoutError:
            # Too slow: end the operation and its tree, and report a failed call.
            follow_task.cancel()
            await asyncio.get_running_loop().run_in_executor(None, end_abnormally, process, events, stop)
            return CallResult(tuple(argv), None, None, problems=(f"gupkg did not finish within {timeout:.0f} seconds and was stopped",))
        except BaseException:
            # Cancelled (the program is closing): stop reading, then end the operation and everything it started.
            for task in (follow_task, stdout_task, stderr_task):
                task.cancel()
            end_abnormally(process, events, stop)
            raise

        # The process ended in time: judge its exit code, output and events file together.
        output, too_long = stdout_task.result()
        return _judge(argv, process.returncode, output, too_long, stderr_task.result(), events, flags, expect_events)


class _Flags:
    """What the follower decided about a call: it was killed after a cancel request, or it hung after ``run_end``."""

    def __init__(self) -> None:
        self.forced = False
        self.hung = False


async def _follow(
    process: asyncio.subprocess.Process, events: EventStream | None, on_records: Callable[[list[dict[str, Any]]], None] | None,
    stop: StopRequest | None, on_idle: Callable[[float], None] | None, flags: _Flags,
) -> None:
    """Deliver the events file's new records while the process runs, watch for a hang, then poll once more after it ended (the last lines)."""
    if events is None:
        return

    # Poll state: when output last arrived, when ``run_end`` was seen, and how many empty polls followed the exit.
    quiet_after_exit = 0
    last_output = time.monotonic()
    finished_at: float | None = None
    next_idle = QUIET_SECONDS
    while True:
        # Hand over whatever the run wrote since the last look; output resets the silence timer.
        records = events.poll()
        if records:
            last_output, next_idle = time.monotonic(), QUIET_SECONDS
            if on_records is not None:
                on_records(records)

        # A stop request made before the run announced its marker is carried out as soon as the marker is known.
        if stop is not None:
            stop.poll()

        # While the process runs, note when ``run_end`` appeared: the exit grace period is counted from it.
        now = time.monotonic()
        if process.returncode is None:
            if events.run_end is not None and finished_at is None:
                finished_at = now
            # Escalate in order: kill a call that ignored a cancel, kill one that hung after ``run_end``, else report a long silence.
            if stop is not None and stop.requested and now - stop.requested_at > FORCE_STOP_SECONDS:
                if not flags.forced:
                    flags.forced = True
                    await kill_tree(process)
            # A command that wrote ``run_end`` but did not exit is killed after a grace period; its result is still used.
            elif finished_at is not None and now - finished_at > EXIT_GRACE_SECONDS:
                if not flags.hung:
                    flags.hung = True
                    await kill_tree(process)
            elif on_idle is not None and now - last_output >= next_idle:
                on_idle(now - last_output)
                next_idle += QUIET_SECONDS

        # After the exit, two empty polls in a row mean the last lines were delivered.
        if process.returncode is not None and not records:
            quiet_after_exit += 1
            if quiet_after_exit >= 2:
                return
        await asyncio.sleep(0 if records else POLL_SECONDS)


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """Drain *stream* to its end, keeping at most *limit* bytes; the flag says whether more arrived."""
    kept: list[bytes] = []
    size = 0
    while stream is not None:
        data = await stream.read(1 << 16)
        if not data:
            break
        if size < limit:
            kept.append(data[: limit - size])
        size += len(data)
    return b"".join(kept), size > limit


async def _read_tail(stream: asyncio.StreamReader | None, limit: int) -> str:
    """Drain *stream* to its end and return the last *limit* bytes as text (a huge human log never fills memory)."""
    tail: deque[bytes] = deque()
    held = 0

    # Keep whole chunks only while they can still contribute to the last *limit* bytes.
    while stream is not None:
        data = await stream.read(1 << 16)
        if not data:
            break
        tail.append(data)
        held += len(data)
        while held - len(tail[0]) >= limit:
            held -= len(tail.popleft())
    return b"".join(tail)[-limit:].decode("utf-8", errors="replace")


def _judge(
    argv: list[str], code: int | None, output: bytes, too_long: bool, stderr: str, events: EventStream | None, flags: _Flags | None = None,
    expect_events: bool = False,
) -> CallResult:
    """Turn what a finished call left (exit code, output, events file) into a ``CallResult``, failing closed."""
    problems: list[str] = []
    warnings: list[str] = []
    document: dict[str, Any] | None = None
    flags = flags or _Flags()
    # The events file is the second source of truth: its ``run_end`` must agree with the process.
    path = events.global_path if events is not None else None
    end = events.run_end if events is not None and events.run_end is not None else (read_run_end(path) if path is not None else None)

    # A command that was killed after a cancel request left nothing to believe; one that hung after ``run_end`` still has its result there.
    if flags.forced:
        problems.append("the command did not stop after the cancel request and was stopped by force")
    if flags.hung:
        warnings.append("the command did not exit after finishing and was stopped; its result is the one it wrote at the end of its events file")
        code = None
        result = end.get("result") if end is not None else None
        output = json.dumps(result).encode("utf-8") if isinstance(result, dict) else b""

    # The document: exactly one JSON object of this api version on standard output.
    if too_long:
        problems.append("the result of gupkg is larger than the limit and was not read")
    else:
        try:
            document = check_document(json.loads(output.decode("utf-8", errors="replace")))
        except (ValueError, RecursionError):
            problems.append("gupkg printed no readable result document" + (f" (exit code {code})" if code is not None else ""))
        except ClientError as error:
            problems.append(str(error))

    # A command that hung after ``run_end`` takes its exit code from the document; otherwise the two must agree.
    if flags.hung and document is not None:
        code = document["exit_code"]
    if document is not None and code is not None and document["exit_code"] != code:
        problems.append(f"the exit code {code} disagrees with the result document ({document['exit_code']})")

    # The events file: when it exists it must end with ``run_end``.
    if path is not None and end is None and (os.path.lexists(path) or expect_events):
        # The UI always asks for an events file, so a result without its ``run_end`` is not believed when it claims success. A failure document is kept
        # (its entries say why it failed: a call whose every target failed to resolve writes no file), with a warning.
        note = f"the events file has no run_end line (exit code {code}): the run did not finish" if os.path.lexists(path) else f"no events file was written (exit code {code})"
        if document is None or document.get("ok") is True or os.path.lexists(path):
            problems.append(note)
        else:
            warnings.append(note)

    # Assemble the trusted view; ``problems`` makes the caller distrust the document.
    return CallResult(
        tuple(argv), code, document, stderr, tuple(problems), path, events.stop_file if events is not None else None, tuple(warnings),
    )
