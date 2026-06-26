"""The ``Controller`` protocol — the public interface of the controller layer.

This is the only surface that the facade, API, and UI are allowed to touch
(CLAUDE.md §4): consumers never import from ``transport/``, ``protocol/`` (the
wire protocol package), or ``streamer/``. ``RealController`` (M5) and
``FakeController`` (M1) both satisfy this protocol; the fake substitutes the
real one in tests without a serial port or a machine (CLAUDE.md §3.2).

Async contract (CLAUDE.md §9): every coroutine here is cancellable at any
``await``. Cancelling a command does not by itself leave the machine safe —
callers that need the machine stopped must follow cancellation with
``soft_reset`` (which is always available, §8.3).

Deviation from the §4 sketch: ``send_program`` is typed as a plain ``def``
returning ``AsyncIterator`` (an async generator), matching ``status_stream``.
That is the correct typing for an async generator and lets callers write
``async for progress in controller.send_program(...)`` directly. The §4 sketch
is explicitly labeled "not final"; this is the finalized signature.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from typing import Protocol, runtime_checkable

from cncctl.controller.messages import (
    Axis,
    ProgramProgress,
    Settings,
    Status,
)
from cncctl.controller.state import MachineState


@runtime_checkable
class Controller(Protocol):
    """Operator-meaningful control of a grblHAL machine.

    ``runtime_checkable`` so tests can assert structural conformance with
    ``isinstance``. Note that this only checks *method presence*, not
    signatures — the typed contract is enforced statically by mypy.
    """

    @property
    def state(self) -> MachineState:
        """The machine's most recently observed coarse state.

        ``Unknown`` before the first status report. Consumers (e.g. the facade)
        gate motion on this (§8.1).
        """
        ...

    async def connect(self, port: str) -> None:
        """Open the connection and bring the model to a known state.

        Awaits the transport opening and the device ``Welcome`` line, then a
        first settings read (§5.6).

        Raises:
            TransportError: the port could not be opened.
            ProtocolError: the device did not announce itself as expected.
        """
        ...

    async def disconnect(self) -> None:
        """Close the connection and stop background tasks.

        Idempotent: disconnecting an already-closed controller is a no-op.
        """
        ...

    async def soft_reset(self) -> None:
        """Issue a soft reset (``0x18``).

        SAFETY INVARIANT (CLAUDE.md §8.3): always available regardless of state
        or queue depth. Bypasses the streamer; clears the ack queue and modal
        state on the resulting ``Welcome`` (§5.4).
        """
        ...

    async def home(self, axes: Iterable[Axis] | None = None) -> None:
        """Run a homing cycle (``$H``), optionally for a subset of axes.

        Raises:
            MachineNotReadyError: the machine is in ``Door`` state (§8.1).
            NotConnectedError: no open connection.
        """
        ...

    async def jog(self, axis: Axis, distance_mm: float, feed_mm_min: float) -> None:
        """Jog ``axis`` by ``distance_mm`` at ``feed_mm_min``.

        Raises:
            MachineNotReadyError: the machine is in ``Alarm`` or ``Door`` (§8.1).
            NotConnectedError: no open connection.
        """
        ...

    async def cancel_jog(self) -> None:
        """Cancel an in-progress jog (realtime ``0x85``); a no-op if not jogging."""
        ...

    async def feed_hold(self) -> None:
        """Request a feed hold (realtime ``!``)."""
        ...

    async def resume(self) -> None:
        """Resume from a feed hold / cycle start (realtime ``~``)."""
        ...

    async def read_settings(self) -> Settings:
        """Read ``$$`` and return the parsed settings map (§5.6).

        Raises:
            NotConnectedError: no open connection.
        """
        ...

    async def write_setting(self, key: int, value: str) -> None:
        """Write ``$key=value`` and verify it by re-reading ``$$`` (§5.6, §8.7).

        Raises:
            SettingsMismatchError: the read-back value did not match (§8.7).
            NotConnectedError: no open connection.
        """
        ...

    def send_program(self, lines: AsyncIterable[str]) -> AsyncIterator[ProgramProgress]:
        """Stream a program, yielding progress as lines are sent and acked (§7 M9).

        The returned async iterator drives the character-counting streamer
        (§5.1); cancelling iteration stops sending but does not stop the
        machine — follow with ``soft_reset`` to halt (§8.3, §8.6).

        Raises:
            MachineNotReadyError: the machine is in ``Alarm`` or ``Door`` (§8.1).
            NotConnectedError: no open connection.
        """
        ...

    def status_stream(self) -> AsyncIterator[Status]:
        """Yield status reports as they arrive (default 10 Hz polling, §5, §8.5).

        Continues for the life of the connection. Loss of consecutive reports
        is surfaced by the controller as a disconnect (§8.5), not swallowed
        here.
        """
        ...

    async def run_line(self, line: str) -> None:
        """Send one raw G-code / system line (MDI) and await its acknowledgement.

        This is the manual-data-input primitive: it does *not* pre-flight the
        line (that is a caller concern, e.g. the console's soft-limit check).

        Raises:
            CommandRejectedError: the device answered ``error:N``.
            NotConnectedError: no open connection.
        """
        ...


__all__ = ["Controller"]
