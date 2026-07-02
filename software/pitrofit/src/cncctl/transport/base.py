"""Async transport abstraction, inbound line framing, and outbound encoding.

The transport is the lowest layer (CLAUDE.md §3.1, §4): a byte/line pipe to
grblHAL over USB-CDC. Everything above it (parser, streamer, controller) is
built on this protocol. Consumers above the controller never import this module.

Two write paths per CLAUDE.md §5.2:

* ``send_line``     — append the terminator, buffered write.
* ``send_realtime`` — a single byte, written immediately, bypassing the line queue.

Both must be safely callable concurrently, sharing the underlying writer with
byte-write-granular locking only. grbl's input handler plucks realtime bytes
out of the stream at the byte level, so a realtime byte interleaving between a
line's bytes is harmless; the lock only prevents two writes from corrupting
each other at the OS buffer.

Line framing is ported from ioSender's ``SerialStream.gp()``
(``reference/ioSender/CNC Core/CNC Core/SerialStream.cs:338``): split on LF,
strip a single trailing CR (grbl emits CRLF).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

#: Outgoing line terminator. CLAUDE.md §5.1 accounts for a single LF per line
#: ("len(line) + 1"). ioSender sends CR (``SerialStream.cs:289``); grblHAL
#: accepts either and counts one byte, so we follow §5.1 and use LF. The M4
#: streamer's character counting depends on this being a single byte.
LINE_TERMINATOR: bytes = b"\n"

#: Encoding for the wire. grbl is ASCII; UTF-8 is a superset and matches
#: ioSender's ``WriteCommand`` (``SerialStream.cs:290``).
WIRE_ENCODING = "utf-8"


def encode_line(line: str) -> bytes:
    """Encode a single G-code/command line for sending.

    Any trailing CR/LF on ``line`` is stripped before the canonical terminator
    is appended, so callers may pass lines with or without their own newline.
    """
    return line.rstrip("\r\n").encode(WIRE_ENCODING) + LINE_TERMINATOR


def encode_realtime(byte: int) -> bytes:
    """Encode a single realtime command byte (§5.2).

    Raises:
        ValueError: if ``byte`` is not in ``range(256)``.
    """
    if byte not in range(256):
        raise ValueError(f"realtime byte must be in range(256), got {byte}")
    return bytes((byte,))


class LineAssembler:
    """Frames a raw byte stream into complete protocol lines.

    Feed arbitrary byte chunks; receive zero or more complete lines per feed.
    Lines are split on LF; a single trailing CR is stripped. Bytes after the
    last LF are buffered until the next feed. Empty lines are returned as
    ``b""`` — the framer is pure; callers decide whether to drop them.
    """

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Append ``data`` and return every complete line it now yields."""
        self._buf.extend(data)
        lines: list[bytes] = []
        while (nl := self._buf.find(b"\n")) >= 0:
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            lines.append(line)
        return lines

    @property
    def pending(self) -> bytes:
        """Bytes buffered since the last complete line (no terminator yet)."""
        return bytes(self._buf)

    def clear(self) -> None:
        """Discard any buffered partial line (e.g. on reconnect/reset)."""
        self._buf.clear()


@runtime_checkable
class AsyncTransport(Protocol):
    """A byte/line pipe to grblHAL.

    ``runtime_checkable`` so tests can assert structural conformance. The typed
    contract is enforced statically by mypy.
    """

    @property
    def is_open(self) -> bool:
        """Whether the transport currently has an open connection."""
        ...

    async def open(self, port: str) -> None:
        """Open ``port`` (with reconnect/backoff for the real transport).

        Raises:
            TransportError: the port could not be opened.
        """
        ...

    async def close(self) -> None:
        """Close the connection and release the port. Idempotent."""
        ...

    async def send_line(self, line: str) -> None:
        """Send one line, terminator appended (§5.2 buffered path).

        Raises:
            NotConnectedError: the transport is not open.
        """
        ...

    async def send_realtime(self, byte: int) -> None:
        """Send a single realtime byte immediately (§5.2), bypassing the queue.

        Raises:
            NotConnectedError: the transport is not open.
            ValueError: ``byte`` is out of ``range(256)``.
        """
        ...

    def read_lines(self) -> AsyncIterator[bytes]:
        """Yield inbound lines (terminator-stripped, empties dropped).

        Ends when the connection closes (EOF). Cancellable at the await.
        """
        ...


__all__ = [
    "LINE_TERMINATOR",
    "WIRE_ENCODING",
    "AsyncTransport",
    "LineAssembler",
    "encode_line",
    "encode_realtime",
]
