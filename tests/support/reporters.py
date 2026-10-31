"""Reporters for the tests that watch the events of a run."""

from __future__ import annotations


class Collector:
    """Collect every event handed to it, in order."""

    def __init__(self) -> None:
        self.events: list = []

    def handle(self, event) -> None:
        """Keep one event."""
        self.events.append(event)
