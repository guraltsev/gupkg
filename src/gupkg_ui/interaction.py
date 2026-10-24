"""Describe who can answer questions during a batch, so the same batch code runs under Textual, on the console and in a schedule.

The CLI is always called with ``--no-input``: every question a batch needs is asked by the UI program *before* the call and the answer travels as
``--yes`` or as the checksum decision. An ``Interaction`` carries the callbacks that ask (a modal screen, a console prompt) or, when there are none,
says that nobody can be asked, in which case the policy table of design 9.1 applies (``ask`` behaves as ``refuse``).

Usage and API
-------------
Build ``Interaction(confirm=..., ask_checksum=..., interactive=...)``; ``Job`` is the signature of the function a run screen executes.

Implementation Approach
-----------------------
The callbacks may be plain functions or coroutine functions; callers await the result when it is awaitable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

from .plans import PlannedTarget

if TYPE_CHECKING:
    from .session import BatchOutcome, BatchSession, Sink

# What the person is asked once for a whole plan: the lines of the plan table and the question. Returns ``True`` to run it.
Confirm = Callable[[list[Text], str], "bool | Awaitable[bool]"]
# What the person is asked for one package whose checksum is missing: returns ``True`` to install it once without a checksum.
AskChecksum = Callable[[PlannedTarget], "bool | Awaitable[bool]"]


@dataclass(frozen=True)
class Interaction:
    """Who can answer questions during a batch.

    Attributes
    ----------
    confirm : callable, optional
        Asked once with the plan; ``None`` means the plan is already confirmed (``--yes``) or nobody can be asked.
    ask_checksum : callable, optional
        Asked once per package whose checksum is missing and whose policy is ``ask``.
    interactive : bool
        Whether someone can answer at all; ``False`` makes ``ask`` behave as ``refuse`` (a schedule, ``--yes``, no terminal).
    """

    confirm: Confirm | None = None
    ask_checksum: AskChecksum | None = None
    interactive: bool = False


# The function a run screen executes: it gets the session that runs calls, the sink that shows output and the way to ask questions.
Job = Callable[["BatchSession", "Sink", Interaction], "Awaitable[BatchOutcome]"]
