"""Line-framing and wire-encoding tests for the transport layer (M2).

The framer is the byte-level part that most needs to be bullet-proof, so it gets
explicit edge cases plus a property test: arbitrary chunking of a byte stream
must reassemble to exactly the same lines as feeding it whole.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cncctl.transport.base import (
    LINE_TERMINATOR,
    LineAssembler,
    encode_line,
    encode_realtime,
)

# Byte strings guaranteed to contain no CR or LF (i.e. valid single lines).
line_bytes = st.binary(max_size=40).map(lambda b: b.replace(b"\r", b"").replace(b"\n", b""))


def test_single_line_lf() -> None:
    assert LineAssembler().feed(b"ok\n") == [b"ok"]


def test_crlf_is_stripped() -> None:
    assert LineAssembler().feed(b"ok\r\n") == [b"ok"]


def test_multiple_lines_in_one_feed() -> None:
    assert LineAssembler().feed(b"ok\r\nerror:1\r\n") == [b"ok", b"error:1"]


def test_partial_line_is_buffered() -> None:
    a = LineAssembler()
    assert a.feed(b"<Idle|MPos:") == []
    assert a.pending == b"<Idle|MPos:"
    assert a.feed(b"0,0,0>\n") == [b"<Idle|MPos:0,0,0>"]
    assert a.pending == b""


def test_split_across_the_terminator() -> None:
    a = LineAssembler()
    assert a.feed(b"ok\r") == []  # CR buffered, LF not seen yet
    assert a.feed(b"\n") == [b"ok"]  # LF completes the line; CR stripped


def test_empty_lines_returned_as_empty_bytes() -> None:
    assert LineAssembler().feed(b"\n") == [b""]
    assert LineAssembler().feed(b"\r\n") == [b""]


def test_clear_discards_partial_line() -> None:
    a = LineAssembler()
    a.feed(b"partial")
    a.clear()
    assert a.pending == b""


@given(lines=st.lists(line_bytes), data=st.data())
def test_framing_is_chunk_invariant(lines: list[bytes], data: st.DataObject) -> None:
    stream = b"".join(line + b"\r\n" for line in lines)
    assembler = LineAssembler()
    out: list[bytes] = []
    i = 0
    while i < len(stream):
        n = data.draw(st.integers(min_value=1, max_value=len(stream) - i))
        out.extend(assembler.feed(stream[i : i + n]))
        i += n
    assert out == lines
    assert assembler.pending == b""


def test_encode_line_appends_terminator() -> None:
    assert encode_line("G0 X1") == b"G0 X1\n"


def test_encode_line_strips_existing_newline() -> None:
    assert encode_line("G0 X1\r\n") == b"G0 X1\n"
    assert encode_line("G0 X1\n") == b"G0 X1\n"


def test_encode_realtime_valid_bytes() -> None:
    assert encode_realtime(0x18) == b"\x18"
    assert encode_realtime(ord("?")) == b"?"


@pytest.mark.parametrize("value", [-1, 256, 1000])
def test_encode_realtime_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError, match="range"):
        encode_realtime(value)


def test_line_terminator_is_a_single_byte() -> None:
    # The M4 streamer's character counting depends on this (§5.1).
    assert LINE_TERMINATOR == b"\n"
    assert len(LINE_TERMINATOR) == 1
