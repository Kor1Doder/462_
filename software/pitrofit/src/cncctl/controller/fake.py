"""An in-memory, deterministic, scriptable :class:`Controller` for tests.

the design: the ``Controller`` protocol exists so a ``FakeController`` can
substitute the real one without a serial port or a machine. This fake models
grbl's *observable* behavior at the command level — it does not parse G-code or
plan motion (that is the real machine's / the simulator's job).

Design choices that keep tests deterministic:

* Commands complete instantly. A successful ``jog`` lands the model back in
  ``Idle`` with ``mpos`` updated; ``send_program`` materializes the line source
  so ``total`` is known, then yields one ``ProgramProgress`` per line.
* Non-happy-path states are reached via explicit scripting hooks
  (:meth:`inject_alarm`, :meth:`open_door`, :meth:`script_state`) rather than by
  timing, so a test can put the machine in ``Alarm``/``Door``/``Run`` and assert
  the safety behavior.
* Every issued command is appended to :attr:`commands` for assertions.

SAFETY: ``jog`` and ``send_program`` refuse to run unless the
machine is ``Idle``, raising :class:`MachineNotReadyError`. That covers the
``Alarm``/``Door`` lockout and the "machine already busy" case in one check.
``home`` (``$H``) is the alarm-recovery path, so it is permitted from ``Alarm``
as well as ``Idle`` but refused in every other state.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from typing import TYPE_CHECKING

import msgspec

from cncctl.controller.errors import MachineNotReadyError, NotConnectedError
from cncctl.controller.messages import Axis, Position, ProgramProgress, Settings, Status
from cncctl.controller.state import MachineState, StateMachine

_ORIGIN = Position(0.0, 0.0, 0.0)


class FakeController:
    """A deterministic in-memory implementation of the ``Controller`` protocol.

    Args:
        settings: initial ``$$`` map; copied, never aliased.
        reset_state: the state ``connect``/``soft_reset`` land in. ``Idle`` by
            default; pass ``MachineState.ALARM`` to simulate "homing required".
        status_interval: seconds awaited between ``status_stream`` yields.
            ``0.0`` (the default) keeps tests fast and deterministic.
        initial_state: the model's state before ``connect`` (``Unknown``).
    """

    def __init__(
        self,
        *,
        settings: Mapping[int, str] | None = None,
        reset_state: MachineState = MachineState.IDLE,
        status_interval: float = 0.0,
        initial_state: MachineState = MachineState.UNKNOWN,
    ) -> None:
        self._sm = StateMachine(initial_state)
        self._connected = False
        self._reset_state = reset_state
        self._status_interval = status_interval
        self._settings_map: dict[int, str] = dict(settings or {})
        self._mpos = _ORIGIN
        self.last_alarm: int | None = None
        self.commands: list[str] = []
        self.sent_lines: list[str] = []

    # -- introspection (test helpers, not part of the protocol) --------------
    @property
    def state(self) -> MachineState:
        """The model's current coarse state."""
        return self._sm.current

    @property
    def mpos(self) -> Position:
        """The model's current machine position (mm)."""
        return self._mpos

    @property
    def connected(self) -> bool:
        """Whether the fake is "connected"."""
        return self._connected

    # -- scripting hooks (test setup, not part of the protocol) --------------
    def script_state(self, state: MachineState) -> None:
        """Drive the model to ``state`` for test setup (must be a legal edge).

        Raises:
            IllegalTransitionError: if the edge is not legal — surfaces a test
                that set up an impossible scenario.
        """
        self._sm.apply(state)

    def inject_alarm(self, code: int) -> None:
        """Put the machine into ``Alarm`` with the given grbl alarm code."""
        self._sm.apply(MachineState.ALARM)
        self.last_alarm = code

    def open_door(self) -> None:
        """Open the safety door (-> ``Door``)."""
        self._sm.apply(MachineState.DOOR)

    # -- Controller protocol -------------------------------------------------
    async def connect(self, port: str) -> None:
        """Mark connected and reset the model to ``reset_state``."""
        self.commands.append(f"connect:{port}")
        self._connected = True
        self._sm.reset(self._reset_state)
        if self._reset_state is not MachineState.ALARM:
            self.last_alarm = None

    async def disconnect(self) -> None:
        """Mark disconnected; the model returns to ``Unknown``. Idempotent."""
        self.commands.append("disconnect")
        self._connected = False
        self._sm = StateMachine(MachineState.UNKNOWN)

    async def soft_reset(self) -> None:
        """Soft reset (``0x18``) — always available while connected.

        Works from *any* machine state, including ``Alarm``/``Run``/``Hold``;
        only a missing connection prevents it.
        """
        self._require_connected()
        self.commands.append("0x18")
        self._sm.reset(self._reset_state)
        if self._reset_state is not MachineState.ALARM:
            self.last_alarm = None

    async def home(self, axes: Iterable[Axis] | None = None) -> None:
        """Run a homing cycle (``$H``). Permitted only from ``Idle`` or ``Alarm``.

        Raises:
            NotConnectedError: no open connection.
            MachineNotReadyError: the machine is not in ``Idle``/``Alarm`` (this
                rejects ``Door`` and busy states, satisfying).
        """
        self._require_connected()
        if self._sm.current not in (MachineState.IDLE, MachineState.ALARM):
            raise MachineNotReadyError(f"cannot home from {self._sm.current.value}")
        letters = "".join(a.value for a in axes) if axes is not None else ""
        self.commands.append(f"$H{letters}")
        self._sm.apply(MachineState.HOME)
        self._sm.apply(MachineState.IDLE)
        self._mpos = _ORIGIN
        self.last_alarm = None

    async def jog(self, axis: Axis, distance_mm: float, feed_mm_min: float) -> None:
        """Jog ``axis`` by ``distance_mm`` at ``feed_mm_min``.

        Raises:
            NotConnectedError: no open connection.
            MachineNotReadyError: the machine is not ``Idle`` — covers the
                ``Alarm``/``Door`` lockout and the busy case.
        """
        self._require_connected()
        self._require_idle("jog")
        self.commands.append(f"jog:{axis.value}:{distance_mm}:{feed_mm_min}")
        self._sm.apply(MachineState.JOG)
        self._mpos = self._translate(axis, distance_mm)
        self._sm.apply(MachineState.IDLE)

    async def cancel_jog(self) -> None:
        """Cancel a jog (realtime ``0x85``). A no-op unless in ``Jog``."""
        self._require_connected()
        self.commands.append("0x85")
        if self._sm.current is MachineState.JOG:
            self._sm.apply(MachineState.IDLE)

    async def feed_hold(self) -> None:
        """Feed hold (realtime ``!``). ``Run`` -> ``Hold``; otherwise a no-op."""
        self._require_connected()
        self.commands.append("!")
        if self._sm.current is MachineState.RUN:
            self._sm.apply(MachineState.HOLD)

    async def resume(self) -> None:
        """Cycle start / resume (realtime ``~``). ``Hold`` -> ``Run``; else no-op."""
        self._require_connected()
        self.commands.append("~")
        if self._sm.current is MachineState.HOLD:
            self._sm.apply(MachineState.RUN)

    async def run_line(self, line: str) -> None:
        """Record a raw MDI line. The fake always accepts it (no error simulation)."""
        self._require_connected()
        self.commands.append(line)

    async def read_settings(self) -> Settings:
        """Return a snapshot copy of the cached ``$$`` map."""
        self._require_connected()
        self.commands.append("$$")
        return Settings(values=dict(self._settings_map))

    async def write_setting(self, key: int, value: str) -> None:
        """Write ``$key=value`` to the in-memory map.

        The fake's read-back always matches, so it never raises
        ``SettingsMismatchError``; the verify-and-diff step lives in the facade
 and is exercised there.
        """
        self._require_connected()
        self.commands.append(f"${key}={value}")
        self._settings_map[key] = value

    async def send_program(self, lines: AsyncIterable[str]) -> AsyncIterator[ProgramProgress]:
        """Stream a program, yielding one ``ProgramProgress`` per line.

        Refuses unless ``Idle`` (covers the ``Alarm``/``Door`` lockout,).
        The line source is materialized so ``total`` is known up front. The
        model is ``Run`` while yielding and returns to ``Idle`` when the source
        is exhausted.

        Raises:
            NotConnectedError: no open connection.
            MachineNotReadyError: the machine is not ``Idle``.
        """
        self._require_connected()
        self._require_idle("send_program")
        program = [line async for line in lines]
        total = len(program)
        if total == 0:
            return
        self._sm.apply(MachineState.RUN)
        for index, line in enumerate(program, start=1):
            self.sent_lines.append(line)
            yield ProgramProgress(
                line=index,
                total=total,
                sent=index,
                acknowledged=index,
                elapsed_s=0.0,
                state=MachineState.RUN,
                mpos=self._mpos,
            )
        self._sm.apply(MachineState.IDLE)

    async def status_stream(self) -> AsyncIterator[Status]:
        """Yield the current status snapshot indefinitely.

        Raises:
            NotConnectedError: no open connection (raised on first iteration).
        """
        self._require_connected()
        while True:
            yield self._snapshot()
            await asyncio.sleep(self._status_interval)

    # -- internals -----------------------------------------------------------
    def _snapshot(self) -> Status:
        substate = self.last_alarm if self._sm.current is MachineState.ALARM else None
        return Status(state=self._sm.current, mpos=self._mpos, substate=substate)

    def _translate(self, axis: Axis, delta: float) -> Position:
        component = {Axis.X: "x", Axis.Y: "y", Axis.Z: "z"}[axis]
        new_value = self._mpos.value(axis) + delta
        return msgspec.structs.replace(self._mpos, **{component: new_value})

    def _require_connected(self) -> None:
        if not self._connected:
            raise NotConnectedError("controller is not connected")

    def _require_idle(self, action: str) -> None:
        if self._sm.current is not MachineState.IDLE:
            raise MachineNotReadyError(
                f"cannot {action} from {self._sm.current.value}: machine must be Idle"
            )


if TYPE_CHECKING:
    from cncctl.controller.protocol import Controller

    # Static structural-conformance check: if FakeController ever drifts from
    # the Controller protocol, mypy --strict fails here.
    _conforms: Controller = FakeController()


__all__ = ["FakeController"]
