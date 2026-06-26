"""Toolpath analysis and the soft-limit pre-flight (CLAUDE.md §7 M10, §8.2).

``analyze`` reduces a :class:`~cncctl.viz.simulate.Trace` to a bounding box,
travel/duration totals, and — critically — soft-limit violations. This is the
check M9 runs before streaming a program: SAFETY INVARIANT §8.2 (soft limits
validated host-side). Pure Python, no heavy dependency.
"""

from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise
from math import hypot

import msgspec

from cncctl.controller.messages import Position
from cncctl.viz.simulate import Trace, TracePoint


class SoftLimits(msgspec.Struct, frozen=True):
    """The machine's travel envelope per axis, as ``(lower, upper)`` mm.

    Built by the caller (M9/facade) from the machine config in the same
    coordinate frame the trace is simulated in.
    """

    x: tuple[float, float]
    y: tuple[float, float]
    z: tuple[float, float]


class AnalysisResult(msgspec.Struct, frozen=True):
    """The result of analyzing a trace."""

    min: Position
    max: Position
    total_travel_mm: float
    duration_s: float
    violations: tuple[str, ...]

    @property
    def in_bounds(self) -> bool:
        """True iff the toolpath stays within the soft limits (§8.2)."""
        return not self.violations

    @property
    def bounding_box(self) -> tuple[Position, Position]:
        """``(min_corner, max_corner)`` of the toolpath."""
        return self.min, self.max


def analyze(trace: Trace, limits: SoftLimits) -> AnalysisResult:
    """Bounding box, travel, duration, and soft-limit violations for ``trace``."""
    points = trace.points
    if not points:
        origin = Position(0.0, 0.0, 0.0)
        return AnalysisResult(origin, origin, 0.0, 0.0, ())

    lo = Position(min(p.x for p in points), min(p.y for p in points), min(p.z for p in points))
    hi = Position(max(p.x for p in points), max(p.y for p in points), max(p.z for p in points))
    return AnalysisResult(
        min=lo,
        max=hi,
        total_travel_mm=_total_travel(points),
        duration_s=trace.duration_s(),
        violations=tuple(_violations(lo, hi, limits)),
    )


def _total_travel(points: tuple[TracePoint, ...]) -> float:
    total = 0.0
    for a, b in pairwise(points):
        total += hypot(hypot(b.x - a.x, b.y - a.y), b.z - a.z)
    return total


def _violations(lo: Position, hi: Position, limits: SoftLimits) -> Iterable[str]:
    for axis, low, high, lower, upper in (
        ("X", lo.x, hi.x, limits.x[0], limits.x[1]),
        ("Y", lo.y, hi.y, limits.y[0], limits.y[1]),
        ("Z", lo.z, hi.z, limits.z[0], limits.z[1]),
    ):
        if low < lower:
            yield f"{axis} min {low:.3f} < soft limit {lower:.3f}"
        if high > upper:
            yield f"{axis} max {high:.3f} > soft limit {upper:.3f}"


__all__ = ["AnalysisResult", "SoftLimits", "analyze"]
