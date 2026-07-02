"""The real Controller: composes transport + parser + streamer.

``RealController`` is the only place that knows about the lower layers
(``transport`` / ``protocol`` / ``streamer``); everything above it sees only the
:class:`~cncctl.controller.protocol.Controller` interface.

Concurrency model:

* a **reader task** consumes ``transport.read_lines()``, parses each line, and
  dispatches it. Acks are routed in order: to the streamer while a program runs,
  to the oldest pending command otherwise. Status reports update the state model
  and fan out to ``status_stream`` subscribers.
* a **status-poll task** sends ``?`` at a fixed rate (default 10 Hz). Three
  consecutive missed reports are treated as a disconnect; it does not
  auto-resume.

Safety: motion commands are gated on the observed state before anything is sent
; soft reset is realtime and always available; settings writes are
verified by re-reading ``$$``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Iterable

import msgspec

from cncctl.controller.errors import (
    CommandRejectedError,
    CommandTimeoutError,
    ConnectionLostError,
    IllegalTransitionError,
    MachineNotReadyError,
    NotConnectedError,
    ProtocolError,
    SettingsMismatchError,
    StreamingError,
)
from cncctl.controller.messages import (
    Alarm,
    Axis,
    Error,
    Ok,
    Position,
    ProgramProgress,
    SettingLine,
    Settings,
    Status,
    Welcome,
)
from cncctl.controller.state import MachineState, StateMachine
from cncctl.log import get_logger
from cncctl.protocol import outbound
from cncctl.protocol.inbound import parse_line
from cncctl.protocol.realtime import Realtime
from cncctl.streamer.character_counter import CharacterCountingStreamer
from cncctl.transport.base import AsyncTransport

_DEFAULT_BUFFER_SIZE = 128
_DEFAULT_STATUS_RATE_HZ = 10.0
_DEFAULT_MAX_MISSED = 3
_DEFAULT_COMMAND_TIMEOUT = 5.0
_DEFAULT_CONNECT_TIMEOUT = 5.0


class RealController:
    """A :class:`Controller` backed by a real (or simulated) transport."""

    def __init__(
        self,
        transport: AsyncTransport,
        *,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
        status_rate_hz: float = _DEFAULT_STATUS_RATE_HZ,
        max_missed_status: int = _DEFAULT_MAX_MISSED,
        command_timeout: float = _DEFAULT_COMMAND_TIMEOUT,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self._transport = transport
        self._streamer = CharacterCountingStreamer(
            buffer_size=buffer_size, send_line=transport.send_line
        )
        self._status_period = 1.0 / status_rate_hz
        self._max_missed = max_missed_status
        self._command_timeout = command_timeout
        self._connect_timeout = connect_timeout
        self._log = get_logger("controller.real")

        self._sm = StateMachine()
        self._connected = False
        self._streaming = False
        self._pending_acks: deque[asyncio.Future[None]] = deque()
        self._settings_buffer: dict[int, str] = {}
        self._settings = Settings(values={})
        self._status_subscribers: list[asyncio.Queue[Status | None]] = []
        self._last_status: Status | None = None
        self._wco: Position | None = None  # cached work-coordinate offset
        self._missed = 0
        self._homing = False  # suspends the missed-status watchdog during a homing cycle
        self._welcome_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._poll_task: asyncio.Task[None] | None = None

    # -- introspection -------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def state(self) -> MachineState:
        return self._sm.current

    @property
    def last_status(self) -> Status | None:
        return self._last_status

    @property
    def settings(self) -> Settings:
        return self._settings

    # -- Controller protocol -------------------------------------------------
    async def connect(self, port: str) -> None:
        await self._transport.open(port)
        self._connected = True
        self._wco = None  # fresh session: forget any previous machine's offset
        self._welcome_event.clear()
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._welcome_event.wait(), self._connect_timeout)
        except TimeoutError as exc:
            await self._teardown("no welcome banner on connect")
            raise ProtocolError("device did not announce itself on connect") from exc
        self._settings = await self.read_settings()
        self._poll_task = asyncio.create_task(self._poll_status())
        self._log.info("connected", port=port, state=self._sm.current.value)

    async def disconnect(self) -> None:
        if not self._connected and self._reader_task is None:
            return
        await self._teardown("client disconnect")

    async def soft_reset(self) -> None:
        # SAFETY: always available while connected, from any state.
        self._require_connected()
        self._welcome_event.clear()
        await self._transport.send_realtime(Realtime.SOFT_RESET)
        # Best-effort confirmation; never block soft reset on a reply.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._welcome_event.wait(), self._command_timeout)

    async def home(self, axes: Iterable[Axis] | None = None) -> None:
        self._require_connected()
        if self._sm.current not in (MachineState.IDLE, MachineState.ALARM):
            raise MachineNotReadyError(f"cannot home from {self._sm.current.value}")
        # A homing cycle blocks the device for a long time and, on some drivers
        # (e.g. RP2040), it stops answering '?' while seeking. Suspend the
        # missed-status watchdog for the duration so a long, silent homing move
        # isn't mistaken for a lost connection (which would close the port
        # mid-cycle -> Windows ClearCommError 995).
        self._homing = True
        self._missed = 0
        try:
            # $H only acks when the whole cycle completes; allow plenty of time
            # for a slow/uncommissioned axis (the watchdog is suspended meanwhile).
            await self._send_command(outbound.format_home(axes), timeout=600.0)
        finally:
            self._homing = False
            self._missed = 0

    async def jog(self, axis: Axis, distance_mm: float, feed_mm_min: float) -> None:
        self._require_connected()
        self._require_idle("jog")  # covers the Alarm/Door lockout
        await self._send_command(outbound.format_jog(axis, distance_mm, feed_mm_min))

    async def cancel_jog(self) -> None:
        self._require_connected()
        await self._transport.send_realtime(Realtime.JOG_CANCEL)

    async def feed_hold(self) -> None:
        self._require_connected()
        await self._transport.send_realtime(Realtime.FEED_HOLD)

    async def resume(self) -> None:
        self._require_connected()
        await self._transport.send_realtime(Realtime.CYCLE_START)

    async def run_line(self, line: str) -> None:
        """Send one raw G-code/system line (MDI) and await its ok."""
        self._require_connected()
        await self._send_command(line)

    async def read_settings(self) -> Settings:
        self._require_connected()
        self._settings_buffer = {}
        await self._send_command(outbound.GET_SETTINGS)
        self._settings = Settings(values=dict(self._settings_buffer))
        return self._settings

    async def write_setting(self, key: int, value: str) -> None:
        self._require_connected()
        await self._send_command(outbound.format_setting(key, value))
        # SAFETY: verify by re-reading $$; a mismatch is an error.
        settings = await self.read_settings()
        actual = settings.get(key)
        if actual != value:
            raise SettingsMismatchError(f"${key}: wrote {value!r}, read back {actual!r}")

    async def send_program(self, lines: AsyncIterable[str]) -> AsyncIterator[ProgramProgress]:
        self._require_connected()
        if self._sm.current is not MachineState.IDLE:
            raise MachineNotReadyError(
                f"cannot start a program from {self._sm.current.value}: machine must be Idle"
            )
        started = time.monotonic()
        self._streaming = True
        try:
            async for progress in self._streamer.stream(lines):
                yield ProgramProgress(
                    line=progress.sent,
                    total=None,
                    sent=progress.sent,
                    acknowledged=progress.acknowledged,
                    elapsed_s=time.monotonic() - started,
                    state=self._sm.current,
                    mpos=self._last_status.mpos if self._last_status else None,
                )
        finally:
            self._streaming = False

    async def status_stream(self) -> AsyncIterator[Status]:
        queue: asyncio.Queue[Status | None] = asyncio.Queue()
        self._status_subscribers.append(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            with contextlib.suppress(ValueError):
                self._status_subscribers.remove(queue)

    # -- reader / dispatch ---------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            async for raw in self._transport.read_lines():
                try:
                    message = parse_line(raw)
                except ProtocolError as exc:
                    self._log.warning("parse_error", error=str(exc))
                    continue
                await self._dispatch(message)
        except ConnectionLostError as exc:
            self._log.warning("connection_lost", error=str(exc))
        if self._connected:
            self._mark_disconnected("transport closed")

    async def _dispatch(self, message: object) -> None:
        if isinstance(message, Status):
            self._on_status(message)
        elif isinstance(message, (Ok, Error)):
            await self._on_ack(message)
        elif isinstance(message, Welcome):
            self._on_welcome(message)
        elif isinstance(message, Alarm):
            self._log.warning("alarm", code=message.code)
        elif isinstance(message, SettingLine):
            self._settings_buffer[message.key] = message.value
        # Feedback / ModalState / BuildInfo / WCSReport / ProbeResult: ignored in M5.

    def _on_status(self, status: Status) -> None:
        self._missed = 0
        status = self._enrich(status)
        self._last_status = status
        try:
            self._sm.apply(status.state)
        except IllegalTransitionError as exc:
            # Surface the surprise but keep the last known state.
            self._log.warning("unexpected_transition", error=str(exc))
        for queue in self._status_subscribers:
            queue.put_nowait(status)

    def _enrich(self, status: Status) -> Status:
        """Fill the missing of MPos/WPos from the cached WCO.

        grblHAL reports either machine *or* work position (per ``$10``) plus the
        work-coordinate offset only periodically. We cache the latest WCO and use
        it to present both positions on every report, so consumers always have
        ``mpos`` and ``wpos``.
        """
        if status.wco is not None:
            self._wco = status.wco
        wco = self._wco
        if wco is None:
            return status
        changed: dict[str, Position] = {}
        if status.mpos is not None and status.wpos is None:
            changed["wpos"] = Position(
                status.mpos.x - wco.x, status.mpos.y - wco.y, status.mpos.z - wco.z
            )
        elif status.wpos is not None and status.mpos is None:
            changed["mpos"] = Position(
                status.wpos.x + wco.x, status.wpos.y + wco.y, status.wpos.z + wco.z
            )
        if status.wco is None:
            changed["wco"] = wco
        return msgspec.structs.replace(status, **changed) if changed else status

    async def _on_ack(self, message: Ok | Error) -> None:
        if self._streaming:
            try:
                await self._streamer.acknowledge()
            except StreamingError:
                # A stale ack for a line sent before a mid-program reset, arriving
                # after the welcome cleared the streamer's accounting. Harmless —
                # ignore it rather than letting it kill the reader loop.
                self._log.warning("stale_ack_after_reset")
                return
            if isinstance(message, Error):
                self._log.warning("error_during_program", code=message.code)
            return
        if not self._pending_acks:
            self._log.warning("unexpected_ack")
            return
        future = self._pending_acks.popleft()
        if future.done():
            return
        if isinstance(message, Error):
            future.set_exception(CommandRejectedError(message.code))
        else:
            future.set_result(None)

    def _on_welcome(self, welcome: Welcome) -> None:
        #: a welcome is a hard reset — drop the ack queue and the streamer's
        # outstanding-byte accounting, and reset state.
        self._sm.reset(MachineState.IDLE)
        self._missed = 0
        # A welcome means the device reset — any program in flight is aborted. Drop
        # the streaming flag so post-reset commands (e.g. $X to clear the alarm a
        # cancel leaves behind) are accepted instead of rejected with
        # "cannot send a command while a program is streaming".
        self._streaming = False
        self._streamer.reset()
        self._fail_pending(ConnectionLostError("device reset (welcome)"))
        self._welcome_event.set()
        self._log.info("welcome", version=welcome.version)

    # -- status polling ------------------------------------------------------
    async def _poll_status(self) -> None:
        while True:
            await asyncio.sleep(self._status_period)
            if not self._connected:
                return
            # While homing, the device may be silent for a long time; keep polling
            # but do not treat the silence as a lost connection.
            if not self._homing:
                self._missed += 1
                if self._missed >= self._max_missed:
                    await self._trigger_disconnect(f"{self._missed} consecutive missed status reports")
                    return
            with contextlib.suppress(NotConnectedError):
                await self._transport.send_realtime(Realtime.STATUS_REPORT)

    # -- command plumbing ----------------------------------------------------
    async def _send_command(self, line: str, timeout: float | None = None) -> None:
        if self._streaming:
            raise StreamingError("cannot send a command while a program is streaming")
        deadline = self._command_timeout if timeout is None else timeout
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._pending_acks.append(future)
        await self._transport.send_line(line)
        try:
            await asyncio.wait_for(future, deadline)
        except TimeoutError as exc:
            with contextlib.suppress(ValueError):
                self._pending_acks.remove(future)
            raise CommandTimeoutError(
                f"no response to {line!r} within {deadline}s"
            ) from exc

    # -- lifecycle -----------------------------------------------------------
    async def _trigger_disconnect(self, reason: str) -> None:
        """Internal disconnect (from a background task): tear down without
        cancelling the calling task."""
        self._mark_disconnected(reason)
        with contextlib.suppress(Exception):
            await self._transport.close()

    async def _teardown(self, reason: str) -> None:
        """Full, client-initiated teardown: cancel tasks and close."""
        self._mark_disconnected(reason)
        for task in (self._poll_task, self._reader_task):
            if task is not None:
                task.cancel()
        with contextlib.suppress(Exception):
            await self._transport.close()
        for task in (self._poll_task, self._reader_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._poll_task = None
        self._reader_task = None

    def _mark_disconnected(self, reason: str) -> None:
        if not self._connected:
            return
        self._connected = False
        self._fail_pending(ConnectionLostError(reason))
        for queue in self._status_subscribers:
            queue.put_nowait(None)
        self._log.info("disconnected", reason=reason)

    def _fail_pending(self, error: Exception) -> None:
        while self._pending_acks:
            future = self._pending_acks.popleft()
            if not future.done():
                future.set_exception(error)

    # -- guards --------------------------------------------------------------
    def _require_connected(self) -> None:
        if not self._connected:
            raise NotConnectedError("controller is not connected")

    def _require_idle(self, action: str) -> None:
        if self._sm.current is not MachineState.IDLE:
            raise MachineNotReadyError(
                f"cannot {action} from {self._sm.current.value}: machine must be Idle"
            )


__all__ = ["RealController"]
