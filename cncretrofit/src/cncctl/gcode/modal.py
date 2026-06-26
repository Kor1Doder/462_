"""Modal-group accounting for the G-code wrapper (CLAUDE.md §4, §7 M6).

G-code is *modal*: a motion mode (``G0``/``G1``/...), distance mode
(``G90``/``G91``), units (``G20``/``G21``), plane, feed rate, and so on persist
across lines until changed. A bare-coordinate line like ``X30 Y40`` continues
the previously commanded motion. :class:`ModalState` carries that context so the
parser can resolve each line into a self-contained block.

Defaults follow grblHAL's power-up modal state: ``G0 G54 G17 G21 G90`` with no
feed/spindle yet commanded.
"""

from __future__ import annotations

import enum

import msgspec


class MotionMode(enum.Enum):
    """Modal group 1 — the active motion command."""

    RAPID = "G0"
    LINEAR = "G1"
    ARC_CW = "G2"
    ARC_CCW = "G3"


class DistanceMode(enum.Enum):
    """Modal group 3 — coordinate interpretation."""

    ABSOLUTE = "G90"
    INCREMENTAL = "G91"


class Units(enum.Enum):
    """Modal group 6 — length units."""

    INCH = "G20"
    MM = "G21"


class Plane(enum.Enum):
    """Modal group 2 — the arc/offset plane."""

    XY = "G17"
    ZX = "G18"
    YZ = "G19"


_MOTION_BY_G: dict[int, MotionMode] = {
    0: MotionMode.RAPID,
    1: MotionMode.LINEAR,
    2: MotionMode.ARC_CW,
    3: MotionMode.ARC_CCW,
}
_PLANE_BY_G: dict[int, Plane] = {17: Plane.XY, 18: Plane.ZX, 19: Plane.YZ}


class ModalContext(msgspec.Struct, frozen=True):
    """An immutable snapshot of the modal state in effect for one block."""

    motion: MotionMode
    distance: DistanceMode
    units: Units
    plane: Plane
    feed: float | None
    spindle: float | None
    wcs: str


class ModalState:
    """Mutable modal context, updated word-by-word as lines are parsed."""

    __slots__ = ("distance", "feed", "motion", "plane", "spindle", "units", "wcs")

    def __init__(self) -> None:
        self.motion = MotionMode.RAPID
        self.distance = DistanceMode.ABSOLUTE
        self.units = Units.MM
        self.plane = Plane.XY
        self.feed: float | None = None
        self.spindle: float | None = None
        self.wcs = "G54"

    def apply(self, letter: str, value: float) -> None:
        """Update the modal state from one G-code word, if it is modal.

        Non-modal words (axes, offsets, M-codes, line numbers, ...) are ignored
        here — the parser handles them per block.
        """
        if letter == "G":
            self._apply_g(int(value))
        elif letter == "F":
            self.feed = value
        elif letter == "S":
            self.spindle = value

    def _apply_g(self, code: int) -> None:
        if code in _MOTION_BY_G:
            self.motion = _MOTION_BY_G[code]
        elif code == 90:  # noqa: PLR2004 - canonical G-code numbers
            self.distance = DistanceMode.ABSOLUTE
        elif code == 91:  # noqa: PLR2004
            self.distance = DistanceMode.INCREMENTAL
        elif code == 20:  # noqa: PLR2004
            self.units = Units.INCH
        elif code == 21:  # noqa: PLR2004
            self.units = Units.MM
        elif code in _PLANE_BY_G:
            self.plane = _PLANE_BY_G[code]
        elif 54 <= code <= 59:  # noqa: PLR2004 - G54..G59 work coordinate systems
            self.wcs = f"G{code}"

    def snapshot(self) -> ModalContext:
        """Capture the current modal state as an immutable :class:`ModalContext`."""
        return ModalContext(
            motion=self.motion,
            distance=self.distance,
            units=self.units,
            plane=self.plane,
            feed=self.feed,
            spindle=self.spindle,
            wcs=self.wcs,
        )


__all__ = [
    "DistanceMode",
    "ModalContext",
    "ModalState",
    "MotionMode",
    "Plane",
    "Units",
]
