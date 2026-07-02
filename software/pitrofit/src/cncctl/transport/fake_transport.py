"""In-memory, scriptable :class:`AsyncTransport` for tests.

Records everything written (lines and realtime bytes) for assertions, and lets
a test push inbound bytes/lines that ``read_lines`` will frame and yield. The
same :class:`LineAssembler` used by the real transport frames scripted bytes,
so partial-chunk framing is exercised through the fake too.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from cncctl.controller.errors import NotConnectedError
from cncctl.transport.base import LineAssembler, encode_realtime

if TYPE_CHECKING:
    from cncctl.transport.base import AsyncTransport


class FakeTransport:
    """A deterministic in-memory transport.

    Test helpers (not part of the protocol): :attr:`sent_lines`,
    :attr:`sent_realtime`, :attr:`opened_port`, and the ``feed_*`` /
    ``close_inbound`` scripting methods.
    """

    def __init__(self) -> None:
        self._open = False
        self.opened_port: str | None = None
        self.sent_lines: list[str] = []
        self.sent_realtime: list[int] = []
        self._assembler = LineAssembler()
        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()

    # -- AsyncTransport protocol ---------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self, port: str) -> None:
        self._open = True
        self.opened_port = port

    async def close(self) -> None:
        if not self._open:
            return
        self._open = False
        # Unblock any reader waiting on read_lines().
        self._inbound.put_nowait(None)

    async def send_line(self, line: str) -> None:
        self._require_open()
        self.sent_lines.append(line)

    async def send_realtime(self, byte: int) -> None:
        self._require_open()
        encode_realtime(byte)  # validates range, raises ValueError if out of range
        self.sent_realtime.append(byte)

    async def read_lines(self) -> AsyncIterator[bytes]:
        self._require_open()
        while True:
            item = await self._inbound.get()
            if item is None:  # close sentinel
                return
            yield item

    # -- scripting hooks (test setup, not part of the protocol) --------------
    def feed_bytes(self, data: bytes) -> None:
        """Push raw inbound bytes; complete (non-empty) lines become readable."""
        for line in self._assembler.feed(data):
            if line:
                self._inbound.put_nowait(line)

    def feed_line(self, line: str) -> None:
        """Push one inbound line (CRLF appended), as the device would send it."""
        self.feed_bytes(line.encode("utf-8") + b"\r\n")

    def close_inbound(self) -> None:
        """Signal EOF to a ``read_lines`` consumer without closing the port."""
        self._inbound.put_nowait(None)

    # -- internals -----------------------------------------------------------
    def _require_open(self) -> None:
        if not self._open:
            raise NotConnectedError("transport is not open")


if TYPE_CHECKING:
    # Static structural-conformance check against the transport protocol.
    _conforms: AsyncTransport = FakeTransport()


__all__ = ["FakeTransport"]
