"""Pick one of a package's other local versions to switch to (the way back, decision D4).

The picker is one list of versions: Up and Down move, Enter chooses, Escape leaves without choosing. It is a dialog with the same key-hint line and status
line as every screen.

Usage and API
-------------
Push ``VersionPicker(package, versions)`` and take the chosen version (or ``None``) from the screen's result.

Implementation Approach
-----------------------
The list is a Textual ``OptionList`` built from ``Text`` options, so a version name is never parsed as markup.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from ..render import literal
from .frame import ModalFrame


class VersionPicker(ModalFrame):
    """Choose a version of a package; the result is the version name or ``None``.

    Parameters
    ----------
    package : str
        The package name, shown in the status line.
    versions : sequence of str
        The versions to choose from.

    Methods
    -------
    key_hints
        Return the keys of the picker.
    status_text
        Return what is being chosen.
    compose_body
        Yield the list of versions.
    on_mount
        Focus the list so Enter and the arrows work at once.
    on_option_list_option_selected
        Return the chosen version.
    action_cancel
        Leave without choosing.

    Attributes
    ----------
    BINDINGS : list of Binding
        The keys of the screen, as ``key`` (label): ``escape`` (Cancel), ``q`` (Cancel).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("q", "cancel", "Cancel")]

    def __init__(self, package: str, versions: Sequence[str]) -> None:
        super().__init__()
        self._package = literal(package, limit=80)
        self._versions = [literal(item, limit=80) for item in versions]

    def key_hints(self) -> str:
        """Return the keys of the picker.

        Returns
        -------
        str
            The text of the top line.
        """
        return "↑↓ move   Enter switch to this version   Esc cancel"

    def status_text(self) -> str:
        """Return what is being chosen.

        Returns
        -------
        str
            The text of the bottom line.
        """
        return f"switch {self._package}: choose the version to activate"

    def compose_body(self) -> ComposeResult:
        """Yield the list of versions."""
        yield OptionList(*(Option(Text(version), id=str(index)) for index, version in enumerate(self._versions)))

    def on_mount(self) -> None:
        """Focus the list so Enter and the arrows work at once."""
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Return the chosen version.

        Parameters
        ----------
        event : OptionList.OptionSelected
            The selection; its option id is the index of the chosen version.
        """
        self.dismiss(self._versions[int(event.option.id)] if event.option.id is not None else None)

    def action_cancel(self) -> None:
        """Leave without choosing."""
        self.dismiss(None)
