"""Single-byte realtime command constants and helpers (CLAUDE.md §5.2).

grblHAL processes these bytes immediately, bypassing the line buffer, so they
work regardless of how full the RX buffer is — which is exactly why soft reset
is always available (§8.3). Values are ported from ioSender's ``GrblConstants``
(``reference/ioSender/CNC Core/CNC Core/Grbl.cs:65``).

§5.2 lists the legacy ASCII forms for status/cycle-start/feed-hold (``?``/``~``/
``!``); grblHAL also accepts binary equivalents (0x80-0x82). We send the ASCII
forms (universally accepted, human-readable in captures) and the binary forms
for everything that has no ASCII equivalent.
"""

from __future__ import annotations

import enum


class Realtime(enum.IntEnum):
    """Realtime command bytes. ``IntEnum`` so members pass straight to
    ``AsyncTransport.send_realtime`` (which takes an ``int``)."""

    SOFT_RESET = 0x18  # Ctrl-X
    STATUS_REPORT = 0x3F  # '?'
    CYCLE_START = 0x7E  # '~'
    FEED_HOLD = 0x21  # '!'
    SAFETY_DOOR = 0x84
    JOG_CANCEL = 0x85

    # Feed-rate overrides
    FEED_OVR_RESET = 0x90  # back to 100%
    FEED_OVR_COARSE_PLUS = 0x91  # +10%
    FEED_OVR_COARSE_MINUS = 0x92  # -10%
    FEED_OVR_FINE_PLUS = 0x93  # +1%
    FEED_OVR_FINE_MINUS = 0x94  # -1%

    # Rapid (G0) overrides
    RAPID_OVR_RESET = 0x95  # 100%
    RAPID_OVR_MEDIUM = 0x96  # 50%
    RAPID_OVR_LOW = 0x97  # 25%

    # Spindle-speed overrides
    SPINDLE_OVR_RESET = 0x99  # 100%
    SPINDLE_OVR_COARSE_PLUS = 0x9A  # +10%
    SPINDLE_OVR_COARSE_MINUS = 0x9B  # -10%
    SPINDLE_OVR_FINE_PLUS = 0x9C  # +1%
    SPINDLE_OVR_FINE_MINUS = 0x9D  # -1%
    SPINDLE_STOP = 0x9E  # toggle spindle stop during hold

    # Coolant toggles
    COOLANT_FLOOD_TOGGLE = 0xA0
    COOLANT_MIST_TOGGLE = 0xA1


#: Every recognized realtime byte, for fast membership checks.
REALTIME_BYTES: frozenset[int] = frozenset(member.value for member in Realtime)


def is_realtime(byte: int) -> bool:
    """Return whether ``byte`` is a recognized realtime command (§5.2)."""
    return byte in REALTIME_BYTES


def to_byte(command: Realtime) -> int:
    """Return the wire byte for ``command`` (its integer value)."""
    return command.value


__all__ = ["REALTIME_BYTES", "Realtime", "is_realtime", "to_byte"]
