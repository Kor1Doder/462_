"""Geometric toolpath simulation.

Walks a parsed :class:`~cncctl.gcode.parse.Program` into a typed :class:`Trace`
of positions over time. Pure Python (``math`` only) — no motion-planner
dependency (see ``viz/README.md`` for why).

Coverage and limits:

* Linear moves (``G0``/``G1``) with absolute (``G90``) / incremental (``G91``)
  distance modes.
* ``G2``/``G3`` arcs in the **XY plane (G17)**, ``I``/``J`` center form, with
  linear Z interpolation (helical), sampled to a chord tolerance so the
  bounding box captures the arc's bulge.
* Arcs in the G18/G19 planes and the ``R`` radius form raise
  :class:`UnsupportedGcodeError` rather than silently mis-bounding the path.

Timing is a feed-limited estimate (``length / velocity``); acceleration and
junction deviation are not modelled — duration is a lower bound, fine for a
dry-run estimate.
"""

from __future__ import annotations

from math import acos, atan2, ceil, cos, hypot, pi, sin

import msgspec

from cncctl.controller.errors import UnsupportedGcodeError
from cncctl.gcode.modal import DistanceMode, MotionMode, Plane
from cncctl.gcode.parse import Motion, Program

_Point = tuple[float, float, float]
_MAX_ARC_SEGMENTS = 4096


class Kinematics(msgspec.Struct, frozen=True):
    """The kinematic inputs the simulator needs.

    ``max_rate_mm_min`` is a conservative scalar cap (e.g. the slowest axis'
    max rate) used for rapids and to clamp programmed feeds.
    """

    max_rate_mm_min: float
    arc_tolerance_mm: float = 0.02


class TracePoint(msgspec.Struct, frozen=True):
    """One point on the toolpath polyline."""

    t: float  # cumulative time (s)
    x: float
    y: float
    z: float
    rapid: bool  # the segment ending at this point is a rapid (G0)
    feed_mm_min: float  # velocity of that segment
    line: int = -1  # 0-based source G-code line of the move (-1 for the start point)


class Trace(msgspec.Struct, frozen=True):
    """A toolpath as a polyline of :class:`TracePoint`s, starting at the origin."""

    points: tuple[TracePoint, ...]

    def duration_s(self) -> float:
        """Estimated dry-run duration (s)."""
        return self.points[-1].t if self.points else 0.0


def simulate(
    program: Program,
    kinematics: Kinematics,
    *,
    start: _Point = (0.0, 0.0, 0.0),
) -> Trace:
    """Simulate ``program`` into a :class:`Trace` in the program's coordinate frame.

    Raises:
        UnsupportedGcodeError: an arc construct the simulator does not model.
    """
    pos = start
    elapsed = 0.0
    points = [TracePoint(0.0, pos[0], pos[1], pos[2], rapid=False, feed_mm_min=0.0)]

    for motion in program.motions():
        target = _resolve_target(pos, motion)
        if motion.mode in (MotionMode.RAPID, MotionMode.LINEAR):
            segment = [target]
        else:
            segment = _sample_arc(pos, target, motion, kinematics.arc_tolerance_mm)
        velocity = _velocity(motion, kinematics)
        rapid = motion.mode is MotionMode.RAPID
        for point in segment:
            length = _distance(pos, point)
            elapsed += (length / velocity * 60.0) if velocity > 0 else 0.0
            points.append(
                TracePoint(
                    elapsed, point[0], point[1], point[2], rapid, velocity, motion.line_index
                )
            )
            pos = point

    return Trace(points=tuple(points))


def _resolve_target(pos: _Point, motion: Motion) -> _Point:
    incremental = motion.context.distance is DistanceMode.INCREMENTAL
    words = motion.words

    def axis(current: float, letter: str) -> float:
        if letter not in words:
            return current
        return current + words[letter] if incremental else words[letter]

    return axis(pos[0], "X"), axis(pos[1], "Y"), axis(pos[2], "Z")


def _velocity(motion: Motion, kinematics: Kinematics) -> float:
    if motion.mode is MotionMode.RAPID:
        return kinematics.max_rate_mm_min
    feed = motion.context.feed
    if feed is None or feed <= 0:
        return kinematics.max_rate_mm_min  # feed-less cut (grbl would error); estimate anyway
    return min(feed, kinematics.max_rate_mm_min)


def _sample_arc(start: _Point, end: _Point, motion: Motion, tolerance_mm: float) -> list[_Point]:
    if motion.context.plane is not Plane.XY:
        raise UnsupportedGcodeError(
            f"arcs in plane {motion.context.plane.value} are not yet supported (G17/XY only)"
        )
    words = motion.words
    if "R" in words and "I" not in words and "J" not in words:
        raise UnsupportedGcodeError("R-form arcs are not yet supported; use I/J center offsets")
    if "I" not in words and "J" not in words:
        raise UnsupportedGcodeError("arc without I/J center offsets")

    sx, sy, sz = start
    ex, ey, ez = end
    cx = sx + words.get("I", 0.0)
    cy = sy + words.get("J", 0.0)
    radius = hypot(sx - cx, sy - cy)
    start_angle = atan2(sy - cy, sx - cx)
    end_angle = atan2(ey - cy, ex - cx)
    sweep = _arc_sweep(start_angle, end_angle, clockwise=motion.mode is MotionMode.ARC_CW)
    segments = _arc_segments(radius, abs(sweep), tolerance_mm)

    points: list[_Point] = []
    for fraction in _arc_fractions(start_angle, sweep, segments):
        angle = start_angle + sweep * fraction
        points.append(
            (cx + radius * cos(angle), cy + radius * sin(angle), sz + (ez - sz) * fraction)
        )
    return points


def _arc_fractions(start_angle: float, sweep: float, segments: int) -> list[float]:
    """Sample fractions along the arc: uniform steps PLUS the exact cardinal
    angles (0, ±90°, 180°) that fall inside the sweep.

    Forcing a sample exactly at each cardinal angle makes the bounding box exact
    rather than under-reporting an arc's bulge by up to the chord tolerance — the
    soft-limit pre-flight must not under-report.
    """
    fractions = {index / segments for index in range(1, segments + 1)}  # uniform; includes end
    for quarter in range(-8, 9):
        fraction = (quarter * (pi / 2.0) - start_angle) / sweep
        if 0.0 < fraction < 1.0:
            fractions.add(fraction)
    return sorted(fractions)


def _arc_sweep(start_angle: float, end_angle: float, *, clockwise: bool) -> float:
    sweep = end_angle - start_angle
    if clockwise and sweep >= 0:
        sweep -= 2 * pi  # also turns a coincident start/end into a full -360 circle
    elif not clockwise and sweep <= 0:
        sweep += 2 * pi
    return sweep


def _arc_segments(radius: float, sweep_abs: float, tolerance_mm: float) -> int:
    if radius <= 0 or sweep_abs <= 0:
        return 1
    ratio = max(-1.0, min(1.0, 1.0 - tolerance_mm / radius))
    max_step = 2.0 * acos(ratio)  # max segment angle for the chord tolerance
    if max_step <= 0:
        return 1
    return min(_MAX_ARC_SEGMENTS, max(1, ceil(sweep_abs / max_step)))


def _distance(a: _Point, b: _Point) -> float:
    return hypot(hypot(b[0] - a[0], b[1] - a[1]), b[2] - a[2])


__all__ = ["Kinematics", "Trace", "TracePoint", "simulate"]
