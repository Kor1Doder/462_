"""2D render tests (M10): produces a sensible matplotlib Figure."""

from __future__ import annotations

from matplotlib.figure import Figure

from cncctl.gcode.parse import parse_string
from cncctl.viz.render import render
from cncctl.viz.simulate import Kinematics, Trace, simulate


def test_render_returns_a_figure_with_feed_and_rapid_layers() -> None:
    trace = simulate(
        parse_string("G90\nG0 X0 Y0\nG1 X10 Y10 F600\nG0 X0 Y0"),
        Kinematics(max_rate_mm_min=3000.0),
    )
    figure = render(trace)
    assert isinstance(figure, Figure)
    # two LineCollections: one for feeds, one for rapids
    assert len(figure.axes[0].collections) == 2
    assert figure.axes[0].get_xlabel() == "X (mm)"


def test_render_handles_empty_trace() -> None:
    figure = render(Trace(points=()))
    assert isinstance(figure, Figure)
