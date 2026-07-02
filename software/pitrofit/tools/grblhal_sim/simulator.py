"""Protocol-level grblHAL device simulator.

A standalone model of grblHAL's *line-level* behavior — not a motion simulator
(that is grblHAL's job). It consumes commands and produces the exact line shapes
the M3 parser understands, so a ``RealController`` (M5) can talk to it through an
in-memory transport (:mod:`tools.grblhal_sim.loopback`) with no serial hardware.

Configurable/: welcome banner, settings dictionary,
alarm-on-reset (homing required), and per-line error injection. Ack timing and
status-report cadence are applied by the transport, which owns the clock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from cncctl.controller.state import MachineState
from cncctl.protocol.realtime import REALTIME_BYTES, Realtime

_SETTING_RE = re.compile(r"^\$(\d+)=(.*)$")
_JOG_WORD_RE = re.compile(r"([XYZ])(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_LINE_END = frozenset({0x0A, 0x0D})  # LF, CR

#: A small but realistic default settings map (steps/mm, rates, accel, travel).
DEFAULT_SETTINGS: dict[int, str] = {
    10: "511",  # status report mask
    11: "0.010",  # junction deviation
    100: "250.000",
    101: "250.000",
    102: "250.000",  # $100-$102 steps/mm
    110: "5000.000",
    111: "5000.000",
    112: "2500.000",  # $110-$112 max rate
    120: "100.000",
    121: "100.000",
    122: "100.000",  # $120-$122 acceleration
    130: "200.000",
    131: "200.000",
    132: "150.000",  # $130-$132 max travel
}

_DEFAULT_WELCOME = "GrblHAL 1.1f ['$' or '$HELP' for help]"
_ERROR_LOCKED = 9  # grbl error:9 — G-code locked out during alarm/jog


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    """Configuration for a :class:`GrblHalSimulator`."""

    welcome: str = _DEFAULT_WELCOME
    settings: Mapping[int, str] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    homing_required: bool = False  # if True, a reset lands in Alarm, not Idle
    error_lines: Mapping[str, int] = field(default_factory=dict)  # line -> error code
    persist_writes: bool = True  # if False, setting writes are acked but ignored
    # (fault injection for the settings-verify path,)


class GrblHalSimulator:
    """A line-level grblHAL device model.

    The methods are synchronous and side-effecting on the device state. The
    transport drives them and applies timing.
    """

    def __init__(self, config: SimulatorConfig | None = None) -> None:
        self._config = config or SimulatorConfig()
        self._settings: dict[int, str] = dict(self._config.settings)
        self._pos = (0.0, 0.0, 0.0)
        self._state = MachineState.IDLE
        self._alarm: int | None = None
        self._linebuf = bytearray()
        self.received_lines: list[str] = []  # every program line accepted (for assertions)
        self.reset()

    # -- introspection -------------------------------------------------------
    @property
    def state(self) -> MachineState:
        return self._state

    @property
    def welcome(self) -> str:
        return self._config.welcome

    def status_report(self) -> str:
        """Render the current state as a ``<...>`` status line."""
        x, y, z = self._pos
        return f"<{self._state.value}|MPos:{x:.3f},{y:.3f},{z:.3f}|FS:0,0|Ov:100,100,100>"

    # -- device behavior -----------------------------------------------------
    def reset(self) -> None:
        """Soft reset / power-up: land in Idle, or Alarm if homing is required."""
        if self._config.homing_required:
            self._state = MachineState.ALARM
            self._alarm = 1
        else:
            self._state = MachineState.IDLE
            self._alarm = None
        self._linebuf.clear()

    def process_line(self, line: str) -> list[str]:  # noqa: PLR0911
        """Process one inbound command line; return the response line(s)."""
        line = line.strip()
        if not line:
            return ["ok"]
        self.received_lines.append(line)

        if line in self._config.error_lines:
            return [f"error:{self._config.error_lines[line]}"]
        if line == "$$":
            return [f"${key}={self._settings[key]}" for key in sorted(self._settings)] + ["ok"]
        if line == "$I":
            return ["[VER:1.1f.20230920:]", "[OPT:VNMSL,35,254]", "ok"]
        if line == "$G":
            return ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]", "ok"]
        if line == "$X":
            self._state = MachineState.IDLE
            self._alarm = None
            return ["[MSG:Caution: Unlocked]", "ok"]
        if line == "$H":
            self._state = MachineState.IDLE
            self._alarm = None
            self._pos = (0.0, 0.0, 0.0)
            return ["ok"]
        if (match := _SETTING_RE.match(line)) is not None:
            if self._config.persist_writes:
                self._settings[int(match.group(1))] = match.group(2)
            return ["ok"]
        if line.startswith("$J="):
            if self._alarm is not None:
                return [f"error:{_ERROR_LOCKED}"]
            self._pos = _apply_jog(self._pos, line)
            self._state = MachineState.JOG
            return ["ok"]
        # Generic G/M-code: locked out while alarmed, otherwise accepted.
        if self._alarm is not None:
            return [f"error:{_ERROR_LOCKED}"]
        return ["ok"]

    def process_realtime(self, byte: int) -> list[str]:  # noqa: PLR0911
        """Process one realtime byte; return any immediate response line(s)."""
        if byte == Realtime.SOFT_RESET:
            self.reset()
            return [self.welcome]
        if byte == Realtime.STATUS_REPORT:
            if self._state is MachineState.JOG:
                self._state = MachineState.IDLE  # the (instantaneous) jog has completed
            return [self.status_report()]
        if byte == Realtime.FEED_HOLD:
            if self._state is MachineState.RUN:
                self._state = MachineState.HOLD
            return []
        if byte == Realtime.CYCLE_START:
            if self._state is MachineState.HOLD:
                self._state = MachineState.RUN
            return []
        if byte == Realtime.JOG_CANCEL:
            if self._state is MachineState.JOG:
                self._state = MachineState.IDLE
            return []
        if byte == Realtime.SAFETY_DOOR:
            self._state = MachineState.DOOR
            return []
        return []  # overrides and others produce no line response

    def feed_bytes(self, data: bytes) -> list[str]:
        """Process a raw byte stream, extracting realtime bytes from lines.

        Mirrors grbl's input handler: a realtime byte anywhere in the stream is
        acted on immediately; everything else accumulates into the current line
        until a CR/LF terminator.
        """
        out: list[str] = []
        for byte in data:
            if byte in REALTIME_BYTES:
                out.extend(self.process_realtime(byte))
            elif byte in _LINE_END:
                if self._linebuf:
                    out.extend(self.process_line(self._linebuf.decode("utf-8", "replace")))
                    self._linebuf.clear()
            else:
                self._linebuf.append(byte)
        return out


def _apply_jog(pos: tuple[float, float, float], line: str) -> tuple[float, float, float]:
    """Move ``pos`` by a ``$J=`` jog line. ``facade.jog`` emits G91 (incremental);
    a G90 jog is treated as absolute."""
    absolute = "G90" in line.upper()
    offsets = {m.group(1).upper(): float(m.group(2)) for m in _JOG_WORD_RE.finditer(line)}
    x, y, z = pos

    def coord(current: float, axis: str) -> float:
        if axis not in offsets:
            return current
        return offsets[axis] if absolute else current + offsets[axis]

    return coord(x, "X"), coord(y, "Y"), coord(z, "Z")


__all__ = ["DEFAULT_SETTINGS", "GrblHalSimulator", "SimulatorConfig"]
