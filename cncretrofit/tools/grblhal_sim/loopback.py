"""In-memory transport that connects a controller to a GrblHalSimulator.

Implements :class:`cncctl.transport.base.AsyncTransport`, so a ``RealController``
(M5) can drive the simulator with no serial port. This is the vehicle for the
Tier-2 integration suite on both OSes (no com0com/socat needed).

Timing model (so the character-counting streamer is actually exercised): a
background *device task* pulls sent lines from an inbound queue and acks each
after a configurable delay, so multiple lines stay outstanding while the host
keeps sending. Realtime bytes bypass the queue and are processed immediately,
exactly as grbl handles them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from cncctl.controller.errors import NotConnectedError
from cncctl.protocol.realtime import Realtime

from .simulator import GrblHalSimulator

if TYPE_CHECKING:
    from cncctl.transport.base import AsyncTransport

#: A per-line ack delay: either a constant or a callable drawing from a
#: distribution (CLAUDE.md §6: "per-line ack delay").
AckDelay = float | Callable[[], float]


def _as_delay_fn(delay: AckDelay) -> Callable[[], float]:
    if callable(delay):
        return delay
    return lambda: delay


class SimulatedTransport:
    """An in-memory :class:`AsyncTransport` backed by a :class:`GrblHalSimulator`."""

    def __init__(
        self,
        simulator: GrblHalSimulator | None = None,
        *,
        ack_delay: AckDelay = 0.0,
        status_interval: float | None = None,
        drop_status: bool = False,
    ) -> None:
        self._sim = simulator or GrblHalSimulator()
        self._delay_fn = _as_delay_fn(ack_delay)
        self._status_interval = status_interval
        self._drop_status = drop_status  # ignore '?' (test the missed-status path)
        self._out: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._inbound: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._open = False

    @property
    def simulator(self) -> GrblHalSimulator:
        return self._sim

    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self, port: str) -> None:
        self._sim.reset()
        self._open = True
        self._emit(self._sim.welcome)  # device announces itself on connect (§5.4)
        self._tasks.append(asyncio.create_task(self._run_device()))
        if self._status_interval is not None:
            self._tasks.append(asyncio.create_task(self._run_status()))

    async def close(self) -> None:
        if not self._open:
            return
        self._open = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        self._out.put_nowait(None)  # unblock a pending read_lines

    async def send_line(self, line: str) -> None:
        self._require_open()
        self._inbound.put_nowait(line)

    async def send_realtime(self, byte: int) -> None:
        self._require_open()
        if self._drop_status and byte == Realtime.STATUS_REPORT:
            return
        for response in self._sim.process_realtime(byte):
            self._emit(response)

    async def read_lines(self) -> AsyncIterator[bytes]:
        self._require_open()
        while True:
            item = await self._out.get()
            if item is None:
                return
            yield item

    # -- device tasks --------------------------------------------------------
    async def _run_device(self) -> None:
        while True:
            line = await self._inbound.get()
            delay = self._delay_fn()
            if delay > 0:
                await asyncio.sleep(delay)
            for response in self._sim.process_line(line):
                self._emit(response)

    async def _run_status(self) -> None:
        assert self._status_interval is not None
        while True:
            await asyncio.sleep(self._status_interval)
            self._emit(self._sim.status_report())

    # -- internals -----------------------------------------------------------
    def _emit(self, line: str) -> None:
        # read_lines yields terminator-stripped line bytes (AsyncTransport contract).
        self._out.put_nowait(line.encode("utf-8"))

    def _require_open(self) -> None:
        if not self._open:
            raise NotConnectedError("simulated transport is not open")


if TYPE_CHECKING:
    _conforms: AsyncTransport = SimulatedTransport()


__all__ = ["AckDelay", "SimulatedTransport"]
