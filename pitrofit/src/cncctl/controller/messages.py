"""Typed messages exchanged across controller boundaries.

CLAUDE.md §3.4 forbids dict-of-strings between modules. Every inbound line
shape in §5.3 has a corresponding immutable type here, and the high-level
``ProgramProgress`` / ``Settings`` types ride on top. These are ``msgspec``
frozen Structs: cheap to build on the hot 10 Hz status path, immutable, and
directly (de)serializable for the future API/UI layer.

M1 defines the *shapes*. The M3 inbound parser is what populates the richer
fields from real device output; until then several fields stay optional with
sensible defaults so the ``FakeController`` can emit minimal-but-valid values.
"""

from __future__ import annotations

import enum

import msgspec

from cncctl.controller.state import MachineState


class Axis(enum.Enum):
    """A linear machine axis. The retrofit is 3-axis (CLAUDE.md §2).

    Values match the axis letters used in G-code and status reports. Rotary
    axes (A/B/C) are intentionally out of scope and not reserved here.
    """

    X = "X"
    Y = "Y"
    Z = "Z"


class Position(msgspec.Struct, frozen=True):
    """A 3-axis Cartesian position or offset, in millimeters."""

    x: float
    y: float
    z: float

    def value(self, axis: Axis) -> float:
        """Return the component for ``axis``."""
        return {Axis.X: self.x, Axis.Y: self.y, Axis.Z: self.z}[axis]


class Overrides(msgspec.Struct, frozen=True):
    """Feed / rapid / spindle override percentages (grbl ``Ov:`` field)."""

    feed: int = 100
    rapid: int = 100
    spindle: int = 100


class InputSignals(msgspec.Struct, frozen=True):
    """Decoded machine input pins — the ``Pn:`` status field ("switch logic").

    grblHAL appends one letter per *asserted* input to ``Pn:`` (e.g. ``Pn:XZP``
    means the X and Z limit switches and the probe are active). This is the typed
    decode of that string: the limit switches we actually have (3-axis), plus the
    control inputs. Any letter we do not name (extra-axis limits A/B/C/…, board-
    specific signals) is preserved in :attr:`other` so nothing is silently lost.

    Build it from a raw report with :meth:`from_pins`, or via
    :meth:`Status.signals`. Letter meanings follow grblHAL's realtime report and
    ioSender's signal decode (``reference/ioSender``).
    """

    limit_x: bool = False
    limit_y: bool = False
    limit_z: bool = False
    probe: bool = False  # P
    door: bool = False  # D — safety door
    estop: bool = False  # E
    reset: bool = False  # R
    feed_hold: bool = False  # H
    cycle_start: bool = False  # S
    other: frozenset[str] = frozenset()

    @property
    def any_limit(self) -> bool:
        """True if any axis limit switch is asserted."""
        return self.limit_x or self.limit_y or self.limit_z

    @property
    def active(self) -> bool:
        """True if any input at all is asserted."""
        return (
            self.any_limit
            or self.probe
            or self.door
            or self.estop
            or self.reset
            or self.feed_hold
            or self.cycle_start
            or bool(self.other)
        )

    @classmethod
    def from_pins(cls, pins: str) -> InputSignals:
        """Decode a ``Pn:`` letter string into typed signal state."""
        letters = set(pins)
        named = {"X", "Y", "Z", "P", "D", "E", "R", "H", "S"}
        return cls(
            limit_x="X" in letters,
            limit_y="Y" in letters,
            limit_z="Z" in letters,
            probe="P" in letters,
            door="D" in letters,
            estop="E" in letters,
            reset="R" in letters,
            feed_hold="H" in letters,
            cycle_start="S" in letters,
            other=frozenset(letters - named),
        )


# --- low-level line messages (§5.3) -----------------------------------------
class Ok(msgspec.Struct, frozen=True):
    """``ok`` — acknowledges the oldest unacknowledged line (§5.1)."""


class Error(msgspec.Struct, frozen=True):
    """``error:N`` — the device rejected a line. ``code`` is the grbl error number."""

    code: int
    message: str | None = None


class Alarm(msgspec.Struct, frozen=True):
    """``ALARM:N`` — the device entered an alarm condition (§5.5).

    This is the *message*; :class:`cncctl.controller.errors.AlarmError` is the
    exception a layer raises in response.
    """

    code: int
    message: str | None = None


class Status(msgspec.Struct, frozen=True):
    """``<State|...>`` — an asynchronous status report (§5.3).

    The device sends machine position (``MPos``) or work position (``WPos``),
    not both; the other is derived via ``wco`` (work coordinate offset, which is
    only reported periodically). The line parser (M3) is stateless and fills
    whichever fields the line carried; the controller (M5) caches ``wco`` to
    derive the missing one. Hence both ``mpos`` and ``wpos`` are optional.
    """

    state: MachineState
    mpos: Position | None = None
    substate: int | None = None
    wpos: Position | None = None
    wco: Position | None = None
    feed: float | None = None
    spindle: float | None = None
    overrides: Overrides | None = None
    buffer_planner: int | None = None
    buffer_rx: int | None = None
    line_number: int | None = None
    pins: str | None = None
    accessory: str | None = None

    @property
    def signals(self) -> InputSignals:
        """The decoded input pins (limit switches, probe, door, e-stop, …).

        Decodes the raw :attr:`pins` field on access; empty/``None`` ``pins``
        gives an all-clear :class:`InputSignals`.
        """
        return InputSignals.from_pins(self.pins or "")


class Feedback(msgspec.Struct, frozen=True):
    """``[MSG:...]`` — a human-readable feedback message."""

    text: str


class ModalState(msgspec.Struct, frozen=True):
    """``[GC:...]`` — the active modal G/M words (§5.3, §5.4).

    Kept as the raw ordered words for M1; modal-group accounting lands in
    ``cncctl.gcode.modal`` if/when needed.
    """

    modes: tuple[str, ...] = ()


class WCSReport(msgspec.Struct, frozen=True):
    """``[G54:...]`` etc. — a work coordinate system offset."""

    wcs: str
    offset: Position


class ProbeResult(msgspec.Struct, frozen=True):
    """``[PRB:...]`` — the result of a probing cycle."""

    position: Position
    success: bool


class BuildInfo(msgspec.Struct, frozen=True):
    """``[VER:...]`` / ``[OPT:...]`` — firmware version and build options."""

    version: str
    options: str = ""
    raw: str = ""


class SettingLine(msgspec.Struct, frozen=True):
    """``$N=value`` — a single setting line from ``$$`` (§5.6)."""

    key: int
    value: str


class Welcome(msgspec.Struct, frozen=True):
    """``GrblHAL X.YY ...`` — a welcome banner.

    Reception is a hard state reset (CLAUDE.md §5.4): drop the ack queue, clear
    modal state, re-poll settings, emit a state-changed event.
    """

    raw: str
    version: str | None = None


# --- high-level aggregate messages ------------------------------------------
class Settings(msgspec.Struct, frozen=True):
    """The parsed ``$$`` settings map (§5.6).

    Cached after every connect and after every successful write; a re-read diff
    that does not match is an error (§8.7).
    """

    values: dict[int, str]

    def get(self, key: int) -> str | None:
        """Return the raw string value for ``key``, or ``None`` if unset."""
        return self.values.get(key)


class ProgramProgress(msgspec.Struct, frozen=True):
    """Progress of a running program, yielded by ``send_program`` (§7 M9).

    ``line`` is the 1-based index of the most recently *sent* line; ``sent`` and
    ``acknowledged`` are running byte/line counts the streamer tracks (§5.1).
    ``total`` is ``None`` when streaming an unbounded source; the file sender
    (M9) fills it from the program's line count.
    """

    line: int
    total: int | None
    sent: int
    acknowledged: int
    elapsed_s: float
    state: MachineState
    mpos: Position | None = None


__all__ = [
    "Alarm",
    "Axis",
    "BuildInfo",
    "Error",
    "Feedback",
    "InputSignals",
    "ModalState",
    "Ok",
    "Overrides",
    "Position",
    "ProbeResult",
    "ProgramProgress",
    "SettingLine",
    "Settings",
    "Status",
    "WCSReport",
    "Welcome",
]
