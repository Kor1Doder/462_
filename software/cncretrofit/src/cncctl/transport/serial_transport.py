"""Real serial transport over USB-CDC using ``pyserial-asyncio``.

Wraps an asyncio ``StreamReader``/``StreamWriter`` pair from
``serial_asyncio.open_serial_connection``. Provides the two write paths
sharing a single write lock, frames inbound bytes via :class:`LineAssembler`,
and opens with exponential backoff.

Note on policy vs mechanism: the backoff here is the *mechanism* for retrying a
flaky open. It does not auto-resume a program after a mid-stream disconnect —
that policy decision belongs to the controller/facade, and the design
forbids auto-resume. ``read_lines`` simply ends on EOF; reconnection is the
caller's explicit choice.

The real-port open and read path are validated by the HIL smoke test
(``tests/hil``, opt-in via ``CNCCTL_HIL=1``); the pure helpers (``backoff_delay``,
framing, encoding) and the abstraction (``FakeTransport``) carry the Tier-1
coverage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import serial_asyncio

from cncctl.controller.errors import ConnectionLostError, NotConnectedError, TransportError
from cncctl.log import get_logger
from cncctl.transport.base import LineAssembler, encode_line, encode_realtime

if TYPE_CHECKING:
    from cncctl.transport.base import AsyncTransport

_log = get_logger("transport.serial")

DEFAULT_BAUDRATE = 115200
DEFAULT_READ_CHUNK = 4096


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Exponential-backoff schedule for opening the port.

    Delay before attempt ``n`` (0-based) is ``min(base_delay * 2**n, max_delay)``.
    """

    base_delay: float = 0.5
    max_delay: float = 5.0
    max_attempts: int = 5


def backoff_delay(attempt: int, policy: ReconnectPolicy) -> float:
    """Return the delay (seconds) to wait *before* the 0-based ``attempt``."""
    if attempt < 0:
        raise ValueError(f"attempt must be >= 0, got {attempt}")
    return min(policy.base_delay * (2.0**attempt), policy.max_delay)


class SerialTransport:
    """USB-CDC serial transport backed by ``pyserial-asyncio``."""

    def __init__(
        self,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        reconnect: ReconnectPolicy | None = None,
        read_chunk: int = DEFAULT_READ_CHUNK,
    ) -> None:
        self._baudrate = baudrate
        self._reconnect = reconnect or ReconnectPolicy()
        self._read_chunk = read_chunk
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._assembler = LineAssembler()
        self._open = False
        self._port: str | None = None

    # -- AsyncTransport protocol ---------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self, port: str) -> None:
        """Open ``port`` with exponential backoff.

        Raises:
            TransportError: every attempt failed.
        """
        last_exc: OSError | None = None
        for attempt in range(self._reconnect.max_attempts):
            if attempt > 0:
                await asyncio.sleep(backoff_delay(attempt, self._reconnect))
            try:
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=port, baudrate=self._baudrate
                )
            except OSError as exc:  # serial.SerialException subclasses OSError
                last_exc = exc
                _log.warning(
                    "serial_open_failed",
                    port=port,
                    attempt=attempt,
                    max_attempts=self._reconnect.max_attempts,
                    error=str(exc),
                )
                continue
            self._assembler.clear()
            self._open = True
            self._port = port
            _log.info("serial_open", port=port, baudrate=self._baudrate)
            return
        raise TransportError(
            f"could not open {port} after {self._reconnect.max_attempts} attempt(s)"
        ) from last_exc

    async def close(self) -> None:
        """Close the writer and release the port. Idempotent."""
        writer = self._writer
        self._open = False
        self._reader = None
        self._writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError as exc:  # closing a port that already went away
                _log.warning("serial_close_error", port=self._port, error=str(exc))
        _log.info("serial_closed", port=self._port)

    async def send_line(self, line: str) -> None:
        """Send one line.

        Throughput note (the streaming bottleneck): we deliberately do **not**
        ``await writer.drain()`` here. ``drain()`` blocks until the line has
        physically left the host, and the character-counting streamer
        awaits this call before reserving and sending the *next* line — so a
        per-line drain serialises the pipeline to one round-trip per line and
        caps streaming at roughly one USB micro-frame per line (the symptom:
        "uploading to the Pico is very slow"). Without it, ``write`` only queues
        into the asyncio serial buffer and returns immediately, letting the
        streamer fill grblHAL's RX buffer (up to several lines back-to-back, the
        whole point of character counting) while the loop flushes the bytes in
        the background.

        This stays safe (no unbounded host-side buffering) because the streamer
        only sends a line when grblHAL's RX buffer has room, and that room is
        freed by ``ok``/``error`` acks — which the device emits only after it has
        *received* the earlier bytes. So the wire backpressure bounds the host
        buffer to the same ~``buffer_size`` (default 128 B) ceiling as the device
        buffer. Realtime bytes still flush immediately (see ``send_realtime``),
        and because they share ``_write_lock`` and the FIFO writer, draining one
        also pushes any line bytes queued ahead of it.
        """
        writer = self._require_writer()
        data = encode_line(line)
        async with self._write_lock:
            writer.write(data)

    async def send_realtime(self, byte: int) -> None:
        """Send a single realtime byte immediately.

        Realtime commands (``?``, ``!``, ``~``, soft reset, jog cancel) are
        latency-critical, so this path *does* drain: it flushes the byte — and,
        since the writer is a FIFO, any line bytes queued ahead of it — out to
        the host immediately rather than waiting for the next loop iteration.
        """
        writer = self._require_writer()
        data = encode_realtime(byte)  # validates range
        async with self._write_lock:
            writer.write(data)
            await writer.drain()

    async def read_lines(self) -> AsyncIterator[bytes]:
        """Yield inbound lines until EOF.

        Raises:
            NotConnectedError: the transport is not open.
            ConnectionLostError: the read failed mid-stream.
        """
        reader = self._reader
        if reader is None:
            raise NotConnectedError("transport is not open")
        while True:
            try:
                data = await reader.read(self._read_chunk)
            except OSError as exc:
                self._open = False
                raise ConnectionLostError(f"serial read failed on {self._port}") from exc
            if not data:  # EOF — the port closed
                self._open = False
                _log.info("serial_eof", port=self._port)
                return
            for line in self._assembler.feed(data):
                if line:
                    yield line

    # -- internals -----------------------------------------------------------
    def _require_writer(self) -> asyncio.StreamWriter:
        if self._writer is None or not self._open:
            raise NotConnectedError("transport is not open")
        return self._writer


if TYPE_CHECKING:
    # Static structural-conformance check against the transport protocol.
    _conforms: AsyncTransport = SerialTransport()


__all__ = [
    "DEFAULT_BAUDRATE",
    "ReconnectPolicy",
    "SerialTransport",
    "backoff_delay",
]
