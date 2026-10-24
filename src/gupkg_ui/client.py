"""Run the sibling ``gupkg`` command line as a subprocess and read its machine interface (the only module of the UI programs that starts a program).

The UI programs never change the machine themselves (decision D44): every operation is a ``gupkg`` call through ``--json``,
``--plan``, ``--events-file`` and ``--no-input``. This module finds the ``gupkg.cmd`` launcher that sits in the same gupkg tree
as the running program (never one found on ``PATH``), builds the command line as an argument list that no shell reads, refuses
an argument that holds a character the launcher could misread, runs the call without ever blocking the caller's event loop,
follows the call's events file while it runs and returns one ``CallResult``. Everything it reads from the call is untrusted:
the result document must be one JSON object with ``"api": 1``, the output is capped, and a call whose result cannot be read, whose
exit code disagrees with its document or whose events file has no ``run_end`` is a failure (``CallResult.problems``), never a success.

Usage and API
-------------
``sibling_launcher()`` returns the launcher of this gupkg tree. ``Client(command, root_options=...)`` is the handle that
runs calls: ``await client.query(args)`` runs a short read-only call (``list``, ``info``, ``--plan``) and returns its document;
``await client.call(args, events_name=..., events_dir=..., on_records=...)`` runs an operation, follows its events file and
returns the ``CallResult``. ``StopRequest.request()`` creates the stop marker that the run announced. Arguments after the
command word go through ``targets_after_options`` so that a target can never be read as an option.

Implementation Approach
-----------------------
The call runs under ``asyncio`` subprocess support, standard input is closed (the CLI is always called with ``--no-input``),
standard output is read with a hard cap and standard error keeps only its tail. The events file is polled by an
``EventStream`` between reads of the process, so the caller sees progress while the call runs and a final drain after it ended.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gupkg.report.events import API_VERSION
from gupkg.selfmgmt.location import RunningGupkg, locate_running_gupkg
from gupkg.system.port import unsafe_relaunch_argument

from .follow import EventStream, read_run_end

# The largest result document that is read from standard output (anything longer is a failed call).
MAX_DOCUMENT_BYTES = 16 << 20
# The tail of standard error that is kept for the report of a failed call (the human text is also in the log files).
STDERR_TAIL_BYTES = 64 << 10
# Seconds between two looks at the events file while a call runs.
POLL_SECONDS = 0.1
# The only name the stop marker of a run may have (the CLI creates it, the UI only creates a file where the run said).
_STOP_NAME = re.compile(r"gupkg-stop-[0-9a-f]{32}")


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
    """Raise ``ClientError`` when an argument holds a character that must not reach a command line (``& | < > ^ " % ! ```, controls)."""
    for item in arguments:
        reason = unsafe_relaunch_argument(item)
        if reason is not None:
            raise ClientError(f"refused: {reason} cannot be passed to gupkg safely; use a name without it")


def with_options(args: Sequence[str], options: Sequence[str]) -> list[str]:
    """Return *args* with *options* put before the ``--`` that starts the targets (after it everything is a target)."""
    items = list(args)
    split = items.index("--") if "--" in items else len(items)
    return [*items[:split], *options, *items[split:]]


def targets_after_options(command: str, options: Sequence[str], targets: Sequence[str]) -> tuple[str, ...]:
    """Return ``command options -- targets``: the options first, the targets after ``--``, so no target can be read as an option."""
    return (command, *options, *(("--", *targets) if targets else ()))


# ---------------------------------------------------------------------------
# The result of a call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallResult:
    """What one ``gupkg`` call did, as far as it can be trusted.

    Attributes
    ----------
    argv : tuple of str
        The command line that was run.
    exit_code : int or None
        The process exit code; ``None`` when the program could not be started.
    document : dict or None
        The ``--json`` result document of standard output (``api`` and head checked), else ``None``.
    child : dict or None
        The result document of the elevated child (the ``run_end`` of the events file) when the call relaunched.
    stderr_tail : str
        The end of standard error, for the report of a failure.
    problems : tuple of str
        Reasons the result cannot be trusted: an unreadable document, an ``api`` mismatch, a missing ``run_end``, a crash.
    events_path : Path or None
        The global events file that was followed.
    stop_file : str or None
        The stop marker the run announced, when it announced one.
    """

    argv: tuple[str, ...]
    exit_code: int | None
    document: dict[str, Any] | None
    child: dict[str, Any] | None = None
    stderr_tail: str = ""
    problems: tuple[str, ...] = ()
    events_path: Path | None = None
    stop_file: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the call succeeded: nothing to distrust, exit code 0 and a document that says ``ok``."""
        return not self.problems and self.exit_code == 0 and bool(self.document and self.document.get("ok"))

    @property
    def relaunched(self) -> bool:
        """Whether the call started an elevated child for its system targets."""
        return bool(self.document and self.document.get("relaunched"))

    def entries(self) -> list[dict[str, Any]]:
        """Return the per-target entries: those of the call itself, then those of its elevated child."""
        found: list[dict[str, Any]] = []
        for document in (self.document, self.child):
            for entry in (document or {}).get("results", ()):
                if isinstance(entry, dict):
                    found.append(entry)
        return found

    def errors(self) -> list[dict[str, Any]]:
        """Return the call-level errors (a usage error, a question that was not answered) of the call and its child."""
        found: list[dict[str, Any]] = []
        for document in (self.document, self.child):
            for error in (document or {}).get("errors", ()):
                if isinstance(error, dict):
                    found.append(error)
        return found


def check_document(value: Any) -> dict[str, Any]:
    """Return *value* when it is a result document of this interface version, else raise ``ClientError``.

    A document is a JSON object with ``"api": 1``, a text ``command``, a boolean ``ok`` and an integer ``exit_code``; any other
    shape (or a newer ``api``) is refused, never guessed at.
    """
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
    """

    def __init__(self, temp_dir: Path | None = None) -> None:
        self._temp_dir = temp_dir
        self.stream: EventStream | None = None
        self.requested = False
        self.created = False

    def request(self) -> str:
        """Record the request, try to carry it out, and return what happened in words."""
        self.requested = True
        return self.poll() or "The stop request is sent as soon as the run announces its stop marker."

    def poll(self) -> str:
        """Carry out a recorded request once the marker is known; return a message when something was decided, else ``""``."""
        stop = self.stream.stop_file if self.stream is not None else None
        if not self.requested or self.created or stop is None:
            return ""
        path = Path(stop)
        folder = self._temp_dir or Path(os.environ.get("TEMP") or os.environ.get("TMP") or "")
        if not _STOP_NAME.fullmatch(path.name) or not folder.is_dir() or os.path.normcase(os.path.abspath(path.parent)) != os.path.normcase(os.path.abspath(folder)):
            self.created = True
            return "The run announced a stop marker outside this program's temporary folder, so it cannot be cancelled from here."
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


@dataclass
class Client:
    """The handle that runs ``gupkg`` calls.

    Parameters
    ----------
    command : sequence of str
        The program and any fixed leading arguments, normally ``(str(sibling_launcher()),)``.
    root_options : sequence of str, optional
        ``--settings PATH`` and ``--sandbox DIR`` for the CLI, put before the command word of every call.
    env : mapping, optional
        The environment of the calls; ``os.environ`` by default.
    """

    command: tuple[str, ...]
    root_options: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None

    @property
    def temp_dir(self) -> Path | None:
        """The temporary folder of the calls' environment: where a run puts its stop marker (``None`` when the environment names none)."""
        env = self.env if self.env is not None else os.environ
        text = env.get("TEMP") or env.get("TMP")
        return Path(text) if text else None

    def argv(self, args: Sequence[str]) -> list[str]:
        """Return the full command line for *args* (the command word and what follows), refusing unsafe characters."""
        full = [*self.command, *self.root_options, *args]
        refuse_unsafe([*self.root_options, *args])
        return full

    async def query(self, args: Sequence[str], *, timeout: float = 120.0) -> dict[str, Any]:
        """Run a short read-only call (``list``, ``info``, ``--plan``) and return its checked document.

        The document is returned for every exit code that still produced one (a plan with a failed target is exit code 2);
        a call without a readable document raises.

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
            The request that stops this call after its current package; ``stop.request()`` may be called while the call runs.

        Returns
        -------
        CallResult
            The trusted view of the call; check ``problems`` before believing ``document``.
        """
        name = events_name or f"gupkg-ui-{uuid.uuid4().hex}.jsonl"
        full = self.argv(with_options(args, ["--json", "--no-input", "--events-file", name]))
        stream = EventStream(Path(events_dir) / name) if events_dir is not None else None
        if stop is not None:
            stop.stream = stream
        return await self._run(full, events=stream, on_records=on_records, stop=stop)

    # -- internals ---------------------------------------------------------------

    async def _run(
        self, argv: list[str], *, events: EventStream | None, on_records: Callable[[list[dict[str, Any]]], None] | None,
        timeout: float | None = None, stop: StopRequest | None = None,
    ) -> CallResult:
        """Start the program, read it and its events file until it ends, and return the checked result."""
        environment = dict(self.env) if self.env is not None else dict(os.environ)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=environment,
            )
        except OSError as error:
            return CallResult(tuple(argv), None, None, problems=(f"gupkg could not be started: {error}",))

        # Read both pipes to their end, follow the events file, and wait for the process; a cancelled task kills the child.
        stdout_task = asyncio.ensure_future(_read_capped(process.stdout, MAX_DOCUMENT_BYTES))
        stderr_task = asyncio.ensure_future(_read_tail(process.stderr, STDERR_TAIL_BYTES))
        follow_task = asyncio.ensure_future(_follow(process, events, on_records, stop))
        try:
            await asyncio.wait_for(asyncio.gather(process.wait(), stdout_task, stderr_task), timeout)
            await follow_task
        except asyncio.TimeoutError:
            process.kill()
            follow_task.cancel()
            return CallResult(tuple(argv), None, None, problems=(f"gupkg did not finish within {timeout:.0f} seconds and was stopped",))
        except BaseException:
            if process.returncode is None:
                process.kill()
            follow_task.cancel()
            raise
        output, too_long = stdout_task.result()
        return _judge(argv, process.returncode, output, too_long, stderr_task.result(), events)


async def _follow(
    process: asyncio.subprocess.Process, events: EventStream | None, on_records: Callable[[list[dict[str, Any]]], None] | None,
    stop: StopRequest | None,
) -> None:
    """Deliver the events file's new records while the process runs, then once more after it ended (the last lines)."""
    if events is None:
        return
    quiet_after_exit = 0
    while True:
        records = events.poll()
        if records and on_records is not None:
            on_records(records)

        # A stop request made before the run announced its marker is carried out as soon as the marker is known.
        if stop is not None:
            stop.poll()
        if process.returncode is not None and not records:
            quiet_after_exit += 1
            if quiet_after_exit >= 2:
                return
        await asyncio.sleep(0 if records else POLL_SECONDS)


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[bytes, bool]:
    """Read *stream* to its end, keeping at most *limit* bytes; the flag says whether more arrived."""
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
    """Read *stream* to its end and return the last *limit* bytes as text (a huge human log never fills memory)."""
    tail: deque[bytes] = deque()
    held = 0
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
    argv: list[str], code: int | None, output: bytes, too_long: bool, stderr: str, events: EventStream | None,
) -> CallResult:
    """Turn what a finished call left (exit code, output, events file) into a ``CallResult``, failing closed."""
    problems: list[str] = []
    document: dict[str, Any] | None = None

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
    if document is not None and code is not None and document["exit_code"] != code:
        problems.append(f"the exit code {code} disagrees with the result document ({document['exit_code']})")

    # The events file: when it exists it must end with ``run_end``; an elevated child's file is the only record of its work.
    child: dict[str, Any] | None = None
    path = events.global_path if events is not None else None
    end = events.run_end if events is not None and events.run_end is not None else (read_run_end(path) if path is not None else None)
    if document is not None and document.get("relaunched"):
        result = end.get("result") if end is not None else None
        if end is None or not isinstance(result, dict):
            problems.append("the elevated run left no run_end line in its events file: treat it as failed")
        else:
            try:
                child = check_document(result)
            except ClientError as error:
                problems.append(f"the elevated run's result is not usable: {error}")
    elif path is not None and os.path.lexists(path) and end is None:
        problems.append("the events file has no run_end line: the run did not finish")
    return CallResult(
        tuple(argv), code, document, child, stderr, tuple(problems), path, events.stop_file if events is not None else None,
    )
