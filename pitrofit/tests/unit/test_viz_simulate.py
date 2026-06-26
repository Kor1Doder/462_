"""Toolpath simulation tests (M10): geometry, modal carryover, arcs, timing."""

from __future__ import annotations

import pytest

from cncctl.controller.errors import UnsupportedGcodeError
from cncctl.gcode.parse import parse_string
from cncctl.viz.simulate import Kinematics, Trace, simulate


def _sim(src: str, max_rate: float = 3000.0) -> Trace:
    return simulate(parse_string(src), Kinematics(max_rate_mm_min=max_rate))


def test_empty_program_has_only_the_start_point() -> None:
    trace = _sim("(only a comment)")
    assert len(trace.points) == 1
    start = trace.points[0]
    assert (start.x, start.y, start.z) == (0.0, 0.0, 0.0)


def test_linear_moves_land_on_endpoints() -> None:
    points = _sim("G90\nG0 X10 Y20\nG1 Z-5 F100").points
    assert (points[1].x, points[1].y, points[1].z) == (10.0, 20.0, 0.0)
    assert points[1].rapid is True  # G0
    assert (points[2].x, points[2].y, points[2].z) == (10.0, 20.0, -5.0)
    assert points[2].rapid is False  # G1


def test_trace_points_carry_their_source_line() -> None:
    # line 0: G90, line 1: G0 X10, line 2: G1 Z-5
    points = _sim("G90\nG0 X10\nG1 Z-5 F100").points
    assert points[0].line == -1  # synthetic start point
    assert points[1].line == 1  # G0 X10
    assert points[2].line == 2  # G1 Z-5


def test_incremental_distance_mode() -> None:
    points = _sim("G91\nG1 X10 F100\nX5\nY-3").points
    assert (points[1].x, points[1].y) == (10.0, 0.0)
    assert (points[2].x, points[2].y) == (15.0, 0.0)  # +5 incremental
    assert (points[3].x, points[3].y) == (15.0, -3.0)


def test_arc_bulge_is_captured_exactly() -> None:
    # G2 CW upper semicircle (-10,0)->(10,0), center (0,0): Y peaks at exactly +10.
    trace = _sim("G90\nG0 X-10 Y0\nG2 X10 Y0 I10 J0 F600")
    assert max(p.y for p in trace.points) == pytest.approx(10.0, abs=1e-9)
    assert min(p.x for p in trace.points) == -10.0
    assert max(p.x for p in trace.points) == 10.0


def test_helical_arc_interpolates_z() -> None:
    trace = _sim("G90\nG0 X10 Y0 Z0\nG3 X0 Y10 I-10 J0 Z-5 F600")
    assert trace.points[-1].z == pytest.approx(-5.0)
    assert min(p.z for p in trace.points) == pytest.approx(-5.0)


def test_rapid_uses_max_rate_feed_uses_programmed() -> None:
    points = _sim("G90\nG0 X100\nG1 X200 F600", max_rate=3000.0).points
    assert points[1].feed_mm_min == 3000.0  # rapid
    assert points[2].feed_mm_min == 600.0  # programmed feed


def test_feed_is_capped_by_max_rate() -> None:
    points = _sim("G90\nG1 X100 F9000", max_rate=3000.0).points
    assert points[1].feed_mm_min == 3000.0


def test_duration_estimate() -> None:
    # 100 mm at 600 mm/min = 10 s.
    assert _sim("G90\nG1 X100 F600").duration_s() == pytest.approx(10.0, abs=0.01)


def test_feedless_move_falls_back_to_max_rate() -> None:
    points = _sim("G90\nG1 X10", max_rate=2500.0).points  # G1 with no F ever set
    assert points[1].feed_mm_min == 2500.0


def test_unsupported_plane_arc_raises() -> None:
    with pytest.raises(UnsupportedGcodeError, match="plane"):
        _sim("G90\nG18\nG0 X0 Y0 Z0\nG2 X10 Z10 I5 K0 F100")


def test_unsupported_r_form_arc_raises() -> None:
    with pytest.raises(UnsupportedGcodeError, match="R-form"):
        _sim("G90\nG0 X0 Y0\nG2 X10 Y0 R8 F100")


def test_arc_without_offsets_raises() -> None:
    with pytest.raises(UnsupportedGcodeError, match="without I/J"):
        _sim("G90\nG0 X0 Y0\nG2 X10 Y0 F100")


def test_clockwise_long_way_arc_runs() -> None:
    # raw sweep positive -> CW long-way branch
    trace = _sim("G90\nG0 X50 Y0\nG2 X60 Y10 I0 J10 F600")
    assert len(trace.points) > 2


def test_counterclockwise_long_way_arc_runs() -> None:
    # raw sweep negative -> CCW long-way branch
    trace = _sim("G90\nG0 X10 Y0\nG3 X0 Y-10 I-10 J0 F600")
    assert len(trace.points) > 2


def test_zero_radius_arc_does_not_crash() -> None:
    trace = _sim("G90\nG0 X5 Y5\nG2 X5 Y5 I0 J0 F100")  # I0 J0 -> radius 0
    assert trace.points[-1].x == pytest.approx(5.0)


def test_zero_arc_tolerance_is_handled() -> None:
    trace = simulate(
        parse_string("G90\nG0 X-10 Y0\nG2 X10 Y0 I10 J0 F600"),
        Kinematics(max_rate_mm_min=3000.0, arc_tolerance_mm=0.0),
    )
    assert len(trace.points) >= 2
