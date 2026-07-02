"""Outbound command construction (CLAUDE.md §4 ``outbound.py``, §7 M3).

Builds the *text* of grbl/grblHAL line commands with correct syntax — the part
that actually needs care. Turning a command string into wire bytes (charset +
terminator) is the transport's single responsibility (``transport.base.
encode_line``), so these builders return ``str`` and are not coupled to the
wire format. Realtime *bytes* live in ``protocol.realtime``.

System-command spellings are ported from ioSender's ``GrblConstants``
(``reference/ioSender/CNC Core/CNC Core/Grbl.cs:101``).
"""

from __future__ import annotations

from collections.abc import Iterable

from cncctl.controller.messages import Axis

# System commands (no arguments).
GET_SETTINGS = "$$"  # dump every $N=value (§5.6)
GET_BUILD_INFO = "$I"  # firmware version / options
GET_PARSER_STATE = "$G"  # active modal state -> [GC:...]
GET_NGC_PARAMETERS = "$#"  # work offsets / probe -> [G54:...] etc.
UNLOCK = "$X"  # clear alarm lock (§5.5)
HOME = "$H"  # homing cycle (all axes)
CHECK_MODE_TOGGLE = "$C"  # toggle G-code check mode


def _num(value: float) -> str:
    """Format a coordinate/feed as a compact fixed-point string (no sci notation)."""
    return f"{value:.4f}".rstrip("0").rstrip(".") or "0"


def format_home(axes: Iterable[Axis] | None = None) -> str:
    """Build a homing command: ``$H`` for all axes, or ``$HXZ`` for a subset."""
    if axes is None:
        return HOME
    return HOME + "".join(axis.value for axis in axes)


def format_jog(axis: Axis, distance_mm: float, feed_mm_min: float) -> str:
    """Build an incremental jog: ``$J=G91 G21 X10 F500`` (relative, millimeters).

    Raises:
        ValueError: if ``feed_mm_min`` is not positive.
    """
    if feed_mm_min <= 0:
        raise ValueError(f"jog feed must be > 0, got {feed_mm_min}")
    return f"$J=G91 G21 {axis.value}{_num(distance_mm)} F{_num(feed_mm_min)}"


def format_setting(key: int, value: str) -> str:
    """Build a setting write: ``$100=250.000`` (§5.6)."""
    return f"${key}={value}"


__all__ = [
    "CHECK_MODE_TOGGLE",
    "GET_BUILD_INFO",
    "GET_NGC_PARAMETERS",
    "GET_PARSER_STATE",
    "GET_SETTINGS",
    "HOME",
    "UNLOCK",
    "format_home",
    "format_jog",
    "format_setting",
]
