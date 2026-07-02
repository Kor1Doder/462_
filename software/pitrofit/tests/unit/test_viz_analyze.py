"""Toolpath analysis tests (M10): bounding box, travel, duration, soft limits.

The soft-limit checks are the safety pre-flight M9 depends on.
"""

from __future__ import annotations

import pytest

from cncctl.controller.messages import Position
from cncctl.gcode.parse import parse_string
from cncctl.viz.analyze import AnalysisResult, SoftLimits, analyze
from cncctl.viz.simulate import Kinematics, Trace, simulate

_WIDE = SoftLimits(x=(-1000.0, 1000.0), y=(-1000.0, 1000.0), z=(-1000.0, 1000.0))


def _analyze(src: str, limits: SoftLimits, max_rate: float = 3000.0) -> AnalysisResult:
    trace = simulate(parse_string(src), Kinematics(max_rate_mm_min=max_rate))
    return analyze(trace, limits)


def test_bounding_box_of_a_rectangle() -> None:
    result = _analyze("G90\nG0 X0 Y0\nG1 X100 F600\nY50\nX0\nY0", _WIDE)
    assert result.min == Position(0.0, 0.0, 0.0)
    assert result.max == Position(100.0, 50.0, 0.0)
    assert result.bounding_box == (Position(0.0, 0.0, 0.0), Position(100.0, 50.0, 0.0))


def test_total_travel() -> None:
    # rapid to origin (0) + 100 + 50 = 150 mm
    result = _analyze("G90\nG0 X0 Y0\nG1 X100 F600\nY50", _WIDE)
    assert result.total_travel_mm == pytest.approx(150.0)


def test_duration() -> None:
    assert _analyze("G90\nG1 X100 F600", _WIDE).duration_s == pytest.approx(10.0, abs=0.01)


def test_in_bounds_program_has_no_violations() -> None:
    result = _analyze("G90\nG1 X100 Y100 F600", SoftLimits((0, 200), (0, 200), (-50, 50)))
    assert result.in_bounds
    assert result.violations == ()


def test_out_of_bounds_x_is_caught() -> None:
    result = _analyze("G90\nG1 X100 F600", SoftLimits((0, 50), (-50, 50), (-50, 50)))
    assert not result.in_bounds
    assert any("X max" in v for v in result.violations)


def test_z_plunge_below_limit_is_caught() -> None:
    result = _analyze("G90\nG1 Z-10 F100", SoftLimits((-50, 50), (-50, 50), (-5, 5)))
    assert not result.in_bounds
    assert any("Z min" in v for v in result.violations)


def test_arc_bulge_violation_is_caught() -> None:
    # The semicircle bulges to Y=10, exceeding a Y limit of 5.
    result = _analyze(
        "G90\nG0 X-10 Y0\nG2 X10 Y0 I10 J0 F600", SoftLimits((-50, 50), (-5, 5), (-5, 5))
    )
    assert any("Y max" in v for v in result.violations)


def test_empty_trace_is_in_bounds() -> None:
    result = analyze(Trace(points=()), _WIDE)
    assert result.in_bounds
    assert result.total_travel_mm == 0.0
    assert result.duration_s == 0.0
