"""2D toolpath rendering (CLAUDE.md §7 M10).

Renders a :class:`~cncctl.viz.simulate.Trace` as an XY plot using matplotlib's
headless Agg backend (no GUI / display needed — suitable for a Pi or CI). Feed
moves are drawn solid, rapids dashed and faint. 3D / plotly arrive with the web
UI; this is the "2D first" deliverable.
"""

from __future__ import annotations

from itertools import pairwise

import matplotlib

matplotlib.use("Agg")  # headless: no display required (Pi / CI)

from matplotlib.collections import LineCollection
from matplotlib.figure import Figure

from cncctl.viz.simulate import Trace


def render(trace: Trace) -> Figure:
    """Render ``trace`` to a matplotlib :class:`Figure` (XY view)."""
    figure = Figure()
    axes = figure.subplots()

    feed_segments: list[list[tuple[float, float]]] = []
    rapid_segments: list[list[tuple[float, float]]] = []
    for a, b in pairwise(trace.points):
        segment = [(a.x, a.y), (b.x, b.y)]
        (rapid_segments if b.rapid else feed_segments).append(segment)

    axes.add_collection(LineCollection(feed_segments, colors="C0", linewidths=1.2))
    axes.add_collection(
        LineCollection(rapid_segments, colors="0.7", linewidths=0.8, linestyles="--")
    )

    axes.set_aspect("equal", "box")
    axes.autoscale_view()
    axes.margins(0.05)
    axes.set_xlabel("X (mm)")
    axes.set_ylabel("Y (mm)")
    axes.set_title("Toolpath (XY)")
    return figure


__all__ = ["render"]
