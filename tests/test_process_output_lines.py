"""Cover how a child process's raw output bytes become reported lines.

The line splitter is driven with scripted byte chunks (as ``read1`` would
return them) so that boundaries can be placed exactly; a real child process
(``WindowsSystem.run``) checks the same behaviour end to end. Rendering of the
lines and the event stream are out of scope (see ``test_console_renderer.py``).
"""

from __future__ import annotations

import random
import sys
import time

import pytest

from gupkg.system.windows import WindowsSystem, _output_lines


class _Chunks:
    """A binary stream that returns the scripted chunks one ``read1`` at a time, then end of stream."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)

    def read1(self, size: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def _lines(*chunks: bytes) -> list[tuple[str, bool, bool]]:
    """Return what the splitter yields for *chunks*."""
    return list(_output_lines(_Chunks(*chunks)))


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        pytest.param((b"one\ntwo\n",), [("one", True, False), ("two", True, False)], id="line-feeds"),
        pytest.param((b"one\r\ntwo\r\n",), [("one", True, False), ("two", True, False)], id="crlf"),
        pytest.param((b"10%\r50%\r100%\n",), [("10%", True, False), ("50%", True, False), ("100%", True, False)], id="carriage-return-progress"),
        pytest.param((b"one\r", b"\ntwo\n"), [("one", True, False), ("two", True, False)], id="crlf-split-across-reads"),
        pytest.param((b"one\r", b"two\n"), [("one", True, False), ("two", True, False)], id="lone-cr-then-text-in-next-read"),
        pytest.param((b"one\r",), [("one", True, False)], id="trailing-lone-cr-at-end-of-stream"),
        pytest.param((b"par", b"tial"), [("partial", False, False)], id="unterminated-last-line"),
        pytest.param((b"a\n\nb\n",), [("a", True, False), ("", True, False), ("b", True, False)], id="blank-line"),
        pytest.param((), [], id="no-output"),
    ],
)
def test_lines_end_at_line_feed_crlf_or_carriage_return(chunks, expected) -> None:
    """Every line terminator, however the reads split it, ends exactly one line; an unterminated tail is still delivered."""
    assert _lines(*chunks) == expected


def test_utf8_characters_split_across_reads_are_decoded_whole() -> None:
    """A multi-byte character cut between two reads is not damaged."""
    encoded = "caf\u00e9 \u65e5\u672c\n".encode("utf-8")

    pieces = [encoded[:4], encoded[4:7], encoded[7:]]

    assert _lines(*pieces) == [("caf\u00e9 \u65e5\u672c", True, False)]


def test_bytes_that_are_not_utf8_fall_back_to_the_code_page_and_never_fail() -> None:
    """Output in a legacy code page (or with invalid bytes) is shown with replacement instead of raising."""
    [(text, terminated, continued)] = _lines(b"caf\xe9 \xff\xfe done\n")

    assert terminated and not continued
    assert text.startswith("caf") and text.endswith("done")


def test_a_very_long_line_without_a_newline_is_cut_into_bounded_pieces_quickly() -> None:
    """A 10 MB line with no terminator arrives in pieces of at most 64 KiB that rebuild the original, in time proportional to its size."""
    total = 10 * 1024 * 1024
    data = (b"abcdefgh" * (total // 8)) + b"\n"
    chunks = [data[i : i + 8192] for i in range(0, len(data), 8192)]

    started = time.monotonic()
    pieces = _lines(*chunks)
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert all(len(text) <= 64 * 1024 for text, _, _ in pieces)
    assert all(continued for _, terminated, continued in pieces[:-1]) and pieces[-1][1:] == (True, False)
    assert "".join(text for text, _, _ in pieces) == "abcdefgh" * (total // 8)


@pytest.mark.parametrize(
    ("character", "prefix"),
    [
        pytest.param(character, prefix, id=f"{len(character.encode())}-byte-char-after-{prefix}-ascii")
        for character in ("\u00e9", "\u65e5", "\U0001f600")
        for prefix in range(len(character.encode()))
    ],
)
def test_a_long_line_is_never_cut_inside_a_utf8_character(character: str, prefix: int) -> None:
    """Whatever offset of a 2-, 3- or 4-byte character the 64 KiB cut falls on, every piece is whole and the pieces rebuild the line exactly."""
    text = "a" * prefix + character * 40_000
    data = text.encode("utf-8") + b"\n"
    chunks = [data[i : i + 8191] for i in range(0, len(data), 8191)]

    pieces = _lines(*chunks)

    assert len(pieces) > 1, "the line was long enough to be cut"
    assert "".join(piece for piece, _, _ in pieces) == text
    assert all("\ufffd" not in piece for piece, _, _ in pieces)
    assert all(len(piece.encode("utf-8")) <= 64 * 1024 for piece, _, _ in pieces)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\x80" * 200_000, id="continuation-bytes"),
        pytest.param(b"\xc3" * 200_000, id="lead-bytes"),
        pytest.param(random.Random(7).randbytes(200_000).replace(b"\n", b"x").replace(b"\r", b"y"), id="random-bytes"),
    ],
)
def test_hostile_bytes_without_a_newline_are_cut_into_few_pieces_quickly(payload: bytes) -> None:
    """Output that is not UTF-8 at all (runs of continuation or lead bytes, random bytes) is cut at the cap in bounded time, not byte by byte."""
    chunks = [payload[i : i + 8192] for i in range(0, len(payload), 8192)]

    started = time.monotonic()
    pieces = _lines(*chunks)
    elapsed = time.monotonic() - started

    assert elapsed < 5
    assert len(pieces) <= 200_000 // (64 * 1024 - 3) + 2


@pytest.mark.windows_integration
def test_a_real_child_with_a_huge_line_and_progress_redraws_is_streamed_in_order() -> None:
    """A real child's progress redraws arrive as separate lines and its 1 MB line arrives as pieces with a continuation note."""
    program = (
        "import sys\n"
        "sys.stdout.write('step 1\\rstep 2\\rstep 3\\n')\n"
        "sys.stdout.write('x' * 1_000_000 + '\\n')\n"
    )
    received: list[str] = []

    result = WindowsSystem().run([sys.executable, "-c", program], on_line=received.append)

    assert result.returncode == 0
    assert received[:3] == ["step 1", "step 2", "step 3"]
    assert received[3].endswith(" [line continues]") and len(received) > 5
    assert result.stdout.splitlines()[:3] == ["step 1", "step 2", "step 3"]
    assert result.stdout.splitlines()[3] == "x" * 1_000_000
