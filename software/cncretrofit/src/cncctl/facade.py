"""Operator-facing facade over the ``Controller`` protocol.

The facade is the boundary the API/UI talk to. It uses *only* the
:class:`~cncctl.controller.protocol.Controller` interface — it never reaches
into transport/protocol/streamer — and it is where the safety invariants are
enforced and unit-tested.

Enforced here:
* no jog/program in ``Alarm``/``Door``; homing (the recovery from
  ``Alarm``) is blocked only by an open door. Rejected before reaching the
  controller, with :class:`MachineNotReadyError`.
* :meth:`reset` is always available.
* :meth:`bootstrap` re-reads ``$$`` and diffs after pushing settings.

The G-code *file sender* (``send_program``/``send_file``) lands in M9, where it
gains the soft-limit pre-flight that makes streaming a program safe; it
is intentionally absent here so nothing can stream a program unchecked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import msgspec

from cncctl.config_io import Config, require_commissioned, settings_from_config
from cncctl.controller.errors import (
    ConfigError,
    MachineNotReadyError,
    SettingsMismatchError,
    SoftLimitError,
)
from cncctl.controller.messages import Axis, ProgramProgress, Settings, Status
from cncctl.controller.protocol import Controller
from cncctl.controller.state import MOTION_BLOCKED_STATES, MachineState
from cncctl.gcode.parse import parse_string
from cncctl.log import get_logger
from cncctl.streamer import line_source
from cncctl.viz.analyze import AnalysisResult, SoftLimits, analyze
from cncctl.viz.simulate import Kinematics, simulate


class MachineProfile(msgspec.Struct, frozen=True):
    """The machine envelope + kinematics the file-sender pre-flight needs."""

    soft_limits: SoftLimits
    kinematics: Kinematics

    @classmethod
    def from_config(cls, config: Config) -> MachineProfile:
        """Derive a profile from the machine config.

        The rate cap is the slowest axis' max rate (a conservative scalar). The
        soft-limit envelope is ``[0, soft_limit_mm]`` per axis — CONVENTION:
        machine zero at the *minimum* corner (positive travel). A machine that
        homes to the maximum corner (negative machine coordinates) must supply
        :class:`SoftLimits` directly instead.
        """
        axes = config.axes
        rate = min(axes.x.max_rate_mm_min, axes.y.max_rate_mm_min, axes.z.max_rate_mm_min)
        limits = SoftLimits(
            x=(0.0, axes.x.soft_limit_mm),
            y=(0.0, axes.y.soft_limit_mm),
            z=(0.0, axes.z.soft_limit_mm),
        )
        return cls(soft_limits=limits, kinematics=Kinematics(max_rate_mm_min=rate))


class Facade:
    """High-level machine operations built on a :class:`Controller`."""

    def __init__(self, controller: Controller, *, profile: MachineProfile | None = None) -> None:
        self._controller = controller
        self._profile = profile
        self._log = get_logger("facade")

    @property
    def state(self) -> MachineState:
        """The machine's most recently observed state."""
        return self._controller.state

    # -- connection ----------------------------------------------------------
    async def connect(self, port: str) -> None:
        await self._controller.connect(port)

    async def disconnect(self) -> None:
        await self._controller.disconnect()

    # -- motion -------------------------------------------------------
    async def home(self, axes: Iterable[Axis] | None = None) -> None:
        """Run a homing cycle. Blocked only by an open door — homing is
        the recovery path out of ``Alarm`` and so is permitted there.

        Raises:
            MachineNotReadyError: the safety door is open.
        """
        if self._controller.state is MachineState.DOOR:
            raise MachineNotReadyError("cannot home with the safety door open")
        await self._controller.home(axes)

    async def jog(self, axis: Axis, distance_mm: float, feed_mm_min: float) -> None:
        """Jog an axis.

        Raises:
            MachineNotReadyError: the machine is in ``Alarm`` or ``Door``.
        """
        self._reject_if_motion_blocked("jog")
        await self._controller.jog(axis, distance_mm, feed_mm_min)

    async def cancel_jog(self) -> None:
        await self._controller.cancel_jog()

    # -- realtime control ----------------------------------------------------
    async def hold(self) -> None:
        """Feed hold."""
        await self._controller.feed_hold()

    async def resume(self) -> None:
        """Cycle start / resume."""
        await self._controller.resume()

    async def reset(self) -> None:
        """Soft reset — always available."""
        await self._controller.soft_reset()

    # -- settings ------------------------------------------------------------
    async def read_settings(self) -> Settings:
        return await self._controller.read_settings()

    async def write_setting(self, key: int, value: str) -> None:
        """Write one setting; the controller verifies it by re-reading ``$$``."""
        await self._controller.write_setting(key, value)

    def status_stream(self) -> AsyncIterator[Status]:
        return self._controller.status_stream()

    # -- program sending ---------------------------------------------
    async def analyze_file(self, path: Path) -> AnalysisResult:
        """Run the host-side pre-flight on a G-code file without sending it.

        Raises:
            ConfigError: no machine profile is configured.
            UnsupportedGcodeError: the simulator met a construct it cannot model.
        """
        if self._profile is None:
            raise ConfigError("no machine profile configured; cannot pre-flight a program")
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return await asyncio.to_thread(self._preflight, text)

    async def send_program(self, path: Path) -> AsyncIterator[ProgramProgress]:
        """Stream a G-code file after a soft-limit pre-flight.

        Reads and analyzes the file off the event loop; refuses to send if the
        toolpath would leave the soft limits. ``total`` on each yielded progress
        is the program's streamable line count.

        Raises:
            ConfigError: no machine profile is configured.
            SoftLimitError: the toolpath exceeds the soft limits.
            MachineNotReadyError / NotConnectedError: from the controller.
        """
        if self._profile is None:
            raise ConfigError("no machine profile configured; cannot pre-flight a program")
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        analysis = await asyncio.to_thread(self._preflight, text)
        if not analysis.in_bounds:
            raise SoftLimitError(
                f"program {path} exceeds soft limits: " + "; ".join(analysis.violations)
            )
        total = sum(1 for line in text.splitlines() if line.strip())
        self._log.info(
            "preflight_ok",
            path=str(path),
            lines=total,
            travel_mm=round(analysis.total_travel_mm, 1),
            duration_s=round(analysis.duration_s, 1),
        )
        async for progress in self._controller.send_program(line_source.from_string(text)):
            yield msgspec.structs.replace(progress, total=total)

    async def run_line(self, line: str) -> None:
        """Send one raw G-code / system line (MDI) and await its response.

        Does not pre-flight — callers that send motion should check soft limits
        themselves (the console does). Raises ``CommandRejectedError`` on
        ``error:N``.
        """
        await self._controller.run_line(line)

    async def unlock(self) -> None:
        """Clear an alarm (``$X``) so motion is allowed again.

        grblHAL ignores motion in ``Alarm`` until unlocked or homed; this is the
        unlock half. The operator must be sure the machine is safe to move first.
        Raises ``CommandRejectedError`` if the device refuses (e.g. an e-stop or
        limit is still asserted).
        """
        await self._controller.run_line("$X")

    async def set_work_zero(
        self, axes: Iterable[Axis], *, values: dict[Axis, float] | None = None
    ) -> None:
        """Set the current position as the G54 *work* zero for ``axes`` (``G10 L20 P1``).

        This is the probeless part-zero workflow: jog the tool to the desired
        origin, then zero it here. ``values`` overrides the coordinate assigned to
        an axis (default ``0``) — e.g. a paper-gauge thickness for Z. Switches and
        homing set the *machine* zero; this sets the *part* zero, independently.

        Raises:
            ValueError: ``axes`` is empty.
            CommandRejectedError: the device answered ``error:N``.
        """
        offsets = values or {}
        words = " ".join(f"{axis.value}{offsets.get(axis, 0.0):.4f}" for axis in axes)
        if not words:
            raise ValueError("set_work_zero requires at least one axis")
        await self._controller.run_line(f"G10 L20 P1 {words}")

    async def cancel(self) -> None:
        """Cancel a running program: feed hold, then soft reset."""
        await self._controller.feed_hold()
        await self._controller.soft_reset()

    def _preflight(self, text: str) -> AnalysisResult:
        assert self._profile is not None  # guarded by callers
        program = parse_string(text)
        trace = simulate(program, self._profile.kinematics)
        return analyze(trace, self._profile.soft_limits)

    # -- bootstrap ------------------------------------------------
    async def bootstrap(self, config: Config, port: str) -> None:
        """Boot the machine: validate config, connect, push settings, verify.

        Refuses an uncommissioned config (placeholder zeros,) before touching
        the machine, then pushes every derived ``$N=value`` and finally re-reads
        ``$$`` and diffs.

        Raises:
            ConfigError: the config is not commissioned.
            SettingsMismatchError: a pushed setting did not read back.
        """
        require_commissioned(config)
        desired = settings_from_config(config)
        await self._controller.connect(port)
        for key, value in desired.items():
            await self._controller.write_setting(key, value)
        settings = await self._controller.read_settings()
        mismatches = {
            key: (value, settings.get(key))
            for key, value in desired.items()
            if settings.get(key) != value
        }
        if mismatches:
            raise SettingsMismatchError(f"bootstrap settings did not verify: {mismatches}")
        self._log.info("bootstrap_complete", port=port, count=len(desired))

    # -- internals -----------------------------------------------------------
    def _reject_if_motion_blocked(self, action: str) -> None:
        state = self._controller.state
        if state in MOTION_BLOCKED_STATES:
            raise MachineNotReadyError(f"cannot {action} in {state.value} state")


__all__ = ["Facade", "MachineProfile"]
