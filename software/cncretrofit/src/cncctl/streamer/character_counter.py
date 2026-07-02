"""Character-counting streamer.

Ports ioSender's character-counting sender (``reference/ioSender/CNC Controls/
CNC Controls/JobControl.xaml.cs`` — ``serialUsed``/``ACKPending`` accounting at
:1152 and the send condition at :1247). We follow the design's canonical
formulation, which is stricter than ioSender's:

* cost of a line = its exact byte length on the wire, terminator included
  (``len(encode_line(line))``), not character count;
* a line may be sent only while ``cost <= buffer_size - bytes_outstanding``
  (``<=``, against the *exact* verified RX buffer);
* no safety margin. ioSender sends against ``0.9 * buffer`` as a hardware-
  handshake high-water mark; the design forbids "probably fine" margins, so
  we account precisely against the real buffer size (verify via ``$I``,).

The accounting (:class:`BufferAccount`) is pure and synchronous so the
invariant — outstanding bytes never exceed the buffer — can be property-tested
exhaustively. :class:`CharacterCountingStreamer` wraps it with the asyncio
plumbing that waits for ack-driven room and drains on completion.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable

import msgspec

from cncctl.controller.errors import BufferOverflowError, StreamingError
from cncctl.transport.base import encode_line


def line_cost(line: str) -> int:
    """Exact wire-byte cost of ``line`` including its terminator."""
    return len(encode_line(line))


class BufferAccount:
    """Pure FIFO accounting of bytes outstanding in the device RX buffer.

    Tracks the unacknowledged lines in send order. ``acknowledge`` pops the
    oldest (each ``ok``/``error`` acks the oldest line,). Every method keeps
    the invariant ``bytes_outstanding <= buffer_size``.
    """

    __slots__ = ("_buffer_size", "_outstanding", "_pending")

    def __init__(self, buffer_size: int) -> None:
        if buffer_size <= 0:
            raise ValueError(f"buffer_size must be positive, got {buffer_size}")
        self._buffer_size = buffer_size
        self._pending: deque[tuple[str, int]] = deque()
        self._outstanding = 0

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @property
    def bytes_outstanding(self) -> int:
        return self._outstanding

    @property
    def free(self) -> int:
        """Bytes currently free in the RX buffer."""
        return self._buffer_size - self._outstanding

    @property
    def pending_count(self) -> int:
        """Number of sent-but-unacknowledged lines."""
        return len(self._pending)

    def fits_at_all(self, cost: int) -> bool:
        """Whether a line of ``cost`` bytes could *ever* fit in an empty buffer."""
        return cost <= self._buffer_size

    def can_send(self, cost: int) -> bool:
        """Whether a line of ``cost`` bytes fits right now."""
        return cost <= self.free

    def reserve(self, line: str, cost: int) -> None:
        """Account for ``line`` as sent.

        Raises:
            BufferOverflowError: if sending would exceed the buffer. With
                a prior ``can_send`` check this is unreachable; it is asserted
                anyway because a violation here would be a safety failure.
        """
        if cost > self.free:
            raise BufferOverflowError(
                f"sending {cost} bytes would exceed RX buffer "
                f"(outstanding={self._outstanding}, size={self._buffer_size})"
            )
        self._pending.append((line, cost))
        self._outstanding += cost

    def acknowledge(self) -> str:
        """Pop and return the oldest unacknowledged line, freeing its bytes.

        Raises:
            StreamingError: an ack arrived with no outstanding line.
        """
        if not self._pending:
            raise StreamingError("acknowledge with no outstanding line")
        line, cost = self._pending.popleft()
        self._outstanding -= cost
        return line

    def clear(self) -> None:
        """Drop all outstanding accounting (after a device reset / welcome,)."""
        self._pending.clear()
        self._outstanding = 0


class StreamProgress(msgspec.Struct, frozen=True):
    """Progress emitted as each line is sent (low-level; the controller maps
    this onto the operator-facing ``ProgramProgress`` in M9)."""

    sent: int
    acknowledged: int
    bytes_outstanding: int
    line: str


class CharacterCountingStreamer:
    """Streams lines to a send callback, never overflowing the RX buffer.

    The streamer does not read the device itself; whoever reads the inbound
    stream (the controller, M5) calls :meth:`acknowledge` on every ``ok``/
    ``error``. ``stream`` blocks sending until acks free enough room, then drains
    (waits for the final acks) before completing.
    """

    def __init__(
        self,
        *,
        buffer_size: int,
        send_line: Callable[[str], Awaitable[None]],
    ) -> None:
        self._account = BufferAccount(buffer_size)
        self._send_line = send_line
        self._cond = asyncio.Condition()

    @property
    def buffer_size(self) -> int:
        return self._account.buffer_size

    @property
    def bytes_outstanding(self) -> int:
        return self._account.bytes_outstanding

    @property
    def pending_count(self) -> int:
        return self._account.pending_count

    async def acknowledge(self) -> str:
        """Acknowledge the oldest outstanding line and wake any waiting sender.

        Raises:
            StreamingError: an ack arrived with no outstanding line.
        """
        async with self._cond:
            line = self._account.acknowledge()
            self._cond.notify_all()
            return line

    def reset(self) -> None:
        """Drop outstanding accounting after a device reset.

        Synchronous and intended to be called when no program is actively
        streaming (e.g. from the controller's welcome handler after a soft
        reset cancels a job). A subsequent ``stream`` starts from a clean buffer.
        """
        self._account.clear()

    async def stream(self, lines: AsyncIterable[str]) -> AsyncIterator[StreamProgress]:
        """Send every line in order, yielding progress as each is sent.

        Awaits room (ack-driven) before each send and drains all acks before
        returning, so completion means the device has acknowledged every line.
        Cancelling iteration stops sending but does not stop the machine — the
        caller must follow with a soft reset.

        Raises:
            BufferOverflowError: a single line is larger than the whole RX
                buffer and can never be sent.
        """
        sent = 0
        async for line in lines:
            cost = line_cost(line)
            if not self._account.fits_at_all(cost):
                raise BufferOverflowError(
                    f"line of {cost} bytes can never fit RX buffer "
                    f"{self._account.buffer_size}: {line!r}"
                )
            async with self._cond:
                while not self._account.can_send(cost):
                    await self._cond.wait()
                self._account.reserve(line, cost)
            await self._send_line(line)
            sent += 1
            yield StreamProgress(
                sent=sent,
                acknowledged=sent - self._account.pending_count,
                bytes_outstanding=self._account.bytes_outstanding,
                line=line,
            )
        async with self._cond:
            while self._account.pending_count != 0:
                await self._cond.wait()


__all__ = [
    "BufferAccount",
    "CharacterCountingStreamer",
    "StreamProgress",
    "line_cost",
]
