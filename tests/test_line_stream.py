"""Cover how ``LineStream`` turns written text into committed lines and live progress.

``LineStream`` is exercised directly with plain callbacks. Terminals, Textual,
and real package operations are out of scope.
"""

from __future__ import annotations

from gupkg.commands import LineStream


def test_carriage_returns_become_transient_updates_until_a_newline_commits_the_last() -> None:
    """Carriage-return text is transient progress and the newline commits the latest one."""
    lines: list[str] = []
    transients: list[str] = []
    stream = LineStream(lines.append, on_transient=transients.append)

    stream.write("a\rb\r")
    assert transients == ["a", "b"]
    assert lines == []

    stream.write("\n")
    assert lines == ["b"]


def test_crlf_commits_one_line_and_no_line_keeps_a_carriage_return() -> None:
    """A CRLF ending commits one clean line with or without a transient callback."""
    with_transient: list[str] = []
    LineStream(with_transient.append, on_transient=lambda _text: None).write("x\r\n")
    assert with_transient == ["x"]

    plain: list[str] = []
    stream = LineStream(plain.append)
    stream.write("50%\r75%\r100%\r\ndone\n")
    assert plain == ["100%", "done"]
    assert not any("\r" in line for line in plain)


def test_stream_declares_live_progress_only_when_it_has_a_transient_callback() -> None:
    """Progress reporters can tell whether a stream will show transient text."""
    assert LineStream(lambda _line: None, on_transient=lambda _text: None).accepts_transient
    assert not LineStream(lambda _line: None).accepts_transient
