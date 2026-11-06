"""Reporters for the tests that watch the events of a run."""

from __future__ import annotations


class Collector:
    """Collect every event handed to it, in order.

    Methods
    -------
    handle
        Keeps one event.

    Attributes
    ----------
    events : list
        Events received so far, in order.
    """

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        """Keep one event.

        Parameters
        ----------
        event : object
            The event to keep.
        """
        self.events.append(event)
