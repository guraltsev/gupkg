"""Cover the manifest TOML writer that writes lists of tables as ``[[name]]`` blocks.

Behavior covered: every list of tables is written as ``[[name]]`` blocks (never as inline tables), a short list of plain
values stays on one line, a long one is wrapped, the text reads back to the same data, and the writer leaves the
``tomli_w`` module as it found it.
Real: the writer and ``tomllib``. No files.
"""

from __future__ import annotations

import tomllib

import tomli_w
from tomli_w import _writer

from gupkg.tomlio import dumps_tables

DATA = {
    "modes": ["system", "user"],
    "shim": [{"name": "emacs", "target": "$App/bin/emacs.exe"}, {"name": "runemacs", "target": "$App/bin/runemacs.exe", "type": "gui"}],
    "source": {"download_url": "https://example.com/a.zip", "require": ["a.exe", "b.exe"]},
}


def test_short_tables_in_a_list_are_written_as_double_bracket_blocks() -> None:
    """Two short shim tables become two ``[[shim]]`` blocks, not an inline array of tables."""
    text = dumps_tables(DATA)

    assert text.count("[[shim]]") == 2
    assert "shim = [" not in text and "{ name" not in text


def test_short_lists_of_plain_values_stay_on_one_line_and_long_ones_wrap() -> None:
    """``modes`` is one line; a list whose line would pass 80 characters is wrapped as ``tomli_w`` does."""
    long = [f"a-rather-long-file-name-{number}.exe" for number in range(4)]

    text = dumps_tables({**DATA, "require": long})

    assert 'modes = ["system", "user"]' in text
    assert 'require = [\n    "a-rather-long-file-name-0.exe",' in text


def test_the_text_reads_back_to_the_same_data() -> None:
    """The writer changes layout only: ``tomllib`` returns the data that went in."""
    assert tomllib.loads(dumps_tables(DATA)) == DATA


def test_the_writer_restores_tomli_w_even_when_it_fails() -> None:
    """A failing dump (a value TOML cannot hold) leaves the library's own functions in place."""
    table, array = _writer.is_suitable_inline_table, _writer.format_inline_array

    try:
        dumps_tables({"bad": object()})
    except TypeError:
        pass

    assert _writer.is_suitable_inline_table is table and _writer.format_inline_array is array
    assert tomli_w.dumps({"k": [{"a": 1}]}).startswith("k = [")
