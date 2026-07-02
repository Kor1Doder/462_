"""Material-removal ("workpiece") simulation (CLAUDE.md §7 M10).

The :mod:`~cncctl.viz.simulate` / :mod:`~cncctl.viz.analyze` pair tells you where
the *tool* goes and whether it stays in bounds. This module answers the next
question an operator actually asks looking at a preview: **what does the finished
part look like, and where does the tool dig in?**

It models the stock as a Z-heightmap (a depth map: one "remaining material" Z per
XY grid cell) and lowers the map wherever the tool sweeps over it. For a 3-axis
mill with no undercuts this is the standard, cheap simulation — far lighter than a
voxel grid, and it produces a real carved top surface to render in 3D.

This is *geometry*, like the rest of ``viz``; it does not talk to the machine.
The reference 2.5D previewer (``reference/gcode_workpiece_simulator_pyqt6.py``)
only draws the toolpath over a stock box — it never removes material. ``carve``
is the piece that makes this a workpiece simulator rather than a path viewer.

Inputs are a :class:`~cncctl.viz.simulate.Trace` (so arcs, modal moves and
incremental distance are already resolved upstream) plus a :class:`Stock` and a
:class:`Tool`. :func:`cam_warnings` adds the geometric collision checks the
reference performed (rapid into stock, cutting outside the stock, cutting through
the bottom).
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from itertools import pairwise

import msgspec
import numpy as np
import numpy.typing as npt

from cncctl.viz.simulate import Trace

_FloatArray = npt.NDArray[np.float64]

#: Grid cells along the stock's longer XY side. Clamped to ``[_MIN, _MAX]``.
DEFAULT_RESOLUTION = 220
_MIN_RESOLUTION = 16
_MAX_RESOLUTION = 600
_EPS = 1e-6


class OriginPreset(enum.Enum):
    """Where the program's zero (G54) sits on the physical stock.

    Maps a human choice to the stock's min corner in *program* coordinates so
    the stock box and the trace share one frame. Mirrors the reference's origin
    presets.
    """

    TOP_CENTER = "top-center"  # X0/Y0 at the stock centre, Z0 at the top face
    TOP_CORNER = "top-corner"  # X0/Y0 at the front-left corner, Z0 at the top
    TOP_FRONT_CENTER = "top-front-center"  # X0 centred, Y0 at the front edge, Z0 top
    BOTTOM_CORNER = "bottom-corner"  # X0/Y0 at the front-left corner, Z0 at the bottom


class Stock(msgspec.Struct, frozen=True):
    """A rectangular block of raw material, as a box in program coordinates.

    The box spans ``[x0, x0+size_x] x [y0, y0+size_y] x [z0, z0+size_z]``. Build
    it from sizes + an :class:`OriginPreset` with :meth:`from_origin`, or pass the
    corner directly.
    """

    x0: float
    y0: float
    z0: float
    size_x: float
    size_y: float
    size_z: float

    @property
    def top(self) -> float:
        """Z of the (uncut) top face."""
        return self.z0 + self.size_z

    @property
    def bottom(self) -> float:
        """Z of the bottom face — material is never removed below this."""
        return self.z0

    @classmethod
    def from_origin(
        cls, size_x: float, size_y: float, size_z: float, preset: OriginPreset
    ) -> Stock:
        """Build a stock of the given size with ``preset`` deciding where G54 zero lands."""
        if size_x <= 0 or size_y <= 0 or size_z <= 0:
            raise ValueError(f"stock dimensions must be positive: {size_x}x{size_y}x{size_z}")
        if preset is OriginPreset.TOP_CENTER:
            return cls(-size_x / 2.0, -size_y / 2.0, -size_z, size_x, size_y, size_z)
        if preset is OriginPreset.TOP_CORNER:
            return cls(0.0, 0.0, -size_z, size_x, size_y, size_z)
        if preset is OriginPreset.TOP_FRONT_CENTER:
            return cls(-size_x / 2.0, 0.0, -size_z, size_x, size_y, size_z)
        return cls(0.0, 0.0, 0.0, size_x, size_y, size_z)  # BOTTOM_CORNER


class Tool(msgspec.Struct, frozen=True):
    """The cutting tool. ``ball=True`` models a ball-nose end mill; otherwise flat."""

    diameter: float
    ball: bool = False

    @property
    def radius(self) -> float:
        return self.diameter / 2.0


@dataclass(frozen=True)
class CarveResult:
    """The carved stock: a heightmap plus volume/depth metrics.

    ``heights[r, c]`` is the remaining-material Z at grid cell ``(ys[r], xs[c])``;
    uncut cells equal :attr:`Stock.top`. ``xs``/``ys`` are the cell centre
    coordinates (program frame).
    """

    xs: _FloatArray
    ys: _FloatArray
    heights: _FloatArray
    stock: Stock
    tool: Tool
    removed_volume_mm3: float
    max_depth_mm: float

    def surface(self) -> tuple[_FloatArray, _FloatArray, _FloatArray]:
        """``(X, Y, Z)`` meshgrid arrays for a 3D surface plot of the carved top."""
        x_grid, y_grid = np.meshgrid(self.xs, self.ys)
        return x_grid, y_grid, self.heights


class HeightMapCarver:
    """Stateful, incremental carve over a fixed stock/tool grid.

    :func:`carve` builds one and runs a whole trace; the GUI playback reuses it to
    carve segment-by-segment (calling :meth:`carve_segment` as the animated tool
    advances) so material removal can be shown live without re-carving the whole
    path every frame. The heightmap (:attr:`heights`) is mutated in place.

    Raises:
        ValueError: non-positive stock dimensions or tool diameter.
    """

    def __init__(self, stock: Stock, tool: Tool, *, resolution: int = DEFAULT_RESOLUTION) -> None:
        if stock.size_x <= 0 or stock.size_y <= 0 or stock.size_z <= 0:
            raise ValueError("stock dimensions must be positive")
        if tool.diameter <= 0:
            raise ValueError(f"tool diameter must be positive: {tool.diameter}")

        resolution = max(_MIN_RESOLUTION, min(_MAX_RESOLUTION, resolution))
        cell = max(stock.size_x, stock.size_y) / resolution
        nx = max(2, round(stock.size_x / cell) + 1)
        ny = max(2, round(stock.size_y / cell) + 1)
        self.stock = stock
        self.tool = tool
        self.xs = np.linspace(stock.x0, stock.x0 + stock.size_x, nx, dtype=np.float64)
        self.ys = np.linspace(stock.y0, stock.y0 + stock.size_y, ny, dtype=np.float64)
        self.heights: _FloatArray = np.full((ny, nx), stock.top, dtype=np.float64)
        self._dx = stock.size_x / (nx - 1)
        self._dy = stock.size_y / (ny - 1)

    def reset(self) -> None:
        """Restore the stock to a flat, uncut block."""
        self.heights[:] = self.stock.top

    def carve_segment(
        self, start: tuple[float, float, float], end: tuple[float, float, float]
    ) -> None:
        """Lower the heightmap along one straight tool move from ``start`` to ``end``."""
        _carve_segment(
            self.heights,
            self.xs,
            self.ys,
            self._dx,
            self._dy,
            start,
            end,
            self.tool.radius,
            self.tool.ball,
            self.stock.bottom,
        )

    def metrics(self) -> tuple[float, float]:
        """Return ``(removed_volume_mm3, max_depth_mm)`` for the current state."""
        removed = float(np.sum(self.stock.top - self.heights)) * self._dx * self._dy
        max_depth = float(self.stock.top - self.heights.min())
        return removed, max_depth

    def snapshot(self) -> CarveResult:
        """Capture the current heightmap and metrics as a :class:`CarveResult`."""
        removed, max_depth = self.metrics()
        return CarveResult(
            self.xs, self.ys, self.heights, self.stock, self.tool, removed, max_depth
        )


def carve(
    trace: Trace, stock: Stock, tool: Tool, *, resolution: int = DEFAULT_RESOLUTION
) -> CarveResult:
    """Carve ``stock`` with ``tool`` following ``trace`` and return the result.

    The heightmap starts flush with the stock top and is lowered wherever the
    tool sweeps below it. Rapids that stay above the stock leave it untouched
    (the tool tip never reaches the material). Material is never removed below
    :attr:`Stock.bottom`.

    Raises:
        ValueError: non-positive stock dimensions or tool diameter.
    """
    carver = HeightMapCarver(stock, tool, resolution=resolution)
    for prev, cur in pairwise(trace.points):
        carver.carve_segment((prev.x, prev.y, prev.z), (cur.x, cur.y, cur.z))
    return carver.snapshot()


def _carve_segment(
    heights: _FloatArray,
    xs: _FloatArray,
    ys: _FloatArray,
    dx: float,
    dy: float,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
    ball: bool,
    bottom: float,
) -> None:
    """Stamp the tool along one straight segment, sampled finely enough to overlap."""
    sx, sy, sz = start
    ex, ey, ez = end
    spacing = min(dx, dy)
    xy_len = math.hypot(ex - sx, ey - sy)
    steps = max(1, math.ceil(xy_len / spacing)) if spacing > 0 else 1
    for k in range(steps + 1):
        t = k / steps
        _stamp(
            heights,
            xs,
            ys,
            dx,
            dy,
            sx + (ex - sx) * t,
            sy + (ey - sy) * t,
            sz + (ez - sz) * t,
            radius,
            ball,
            bottom,
        )


def _stamp(
    heights: _FloatArray,
    xs: _FloatArray,
    ys: _FloatArray,
    dx: float,
    dy: float,
    cx: float,
    cy: float,
    cz: float,
    radius: float,
    ball: bool,
    bottom: float,
) -> None:
    """Lower every cell within ``radius`` of ``(cx, cy)`` to the tool's underside.

    Operates on the sub-grid bounding the tool disc (so cost is O(disc), not
    O(grid)). Flat tools cut a flat disc at ``cz``; ball tools cut the spherical
    underside. Nothing is removed where the tool sits above the current surface
    (``np.minimum``), so rapids above the stock are free.
    """
    nx = xs.shape[0]
    ny = ys.shape[0]
    col_lo = max(0, math.floor((cx - radius - xs[0]) / dx))
    col_hi = min(nx - 1, math.ceil((cx + radius - xs[0]) / dx))
    row_lo = max(0, math.floor((cy - radius - ys[0]) / dy))
    row_hi = min(ny - 1, math.ceil((cy + radius - ys[0]) / dy))
    if col_lo > col_hi or row_lo > row_hi:
        return  # tool entirely off the stock in XY

    sub_x = xs[col_lo : col_hi + 1]
    sub_y = ys[row_lo : row_hi + 1]
    ddx = sub_x[np.newaxis, :] - cx
    ddy = sub_y[:, np.newaxis] - cy
    dist2 = ddx * ddx + ddy * ddy
    within = dist2 <= radius * radius
    if ball and radius > 0:
        # Ball centre rides one radius above the tip; underside = centre - sqrt(r^2 - d^2).
        cut = (cz + radius) - np.sqrt(np.maximum(0.0, radius * radius - dist2))
    else:
        cut = np.full_like(dist2, cz)
    cut = np.maximum(cut, bottom)
    block = heights[row_lo : row_hi + 1, col_lo : col_hi + 1]
    np.minimum(block, np.where(within, cut, np.inf), out=block)


def cam_warnings(trace: Trace, stock: Stock) -> tuple[str, ...]:
    """Geometric pre-machining checks against the stock (the reference's CAM checks).

    Reports, with counts:

    * rapids (``G0``) travelling below the stock top *inside* the stock footprint
      — a likely collision (rapids do not slow for material);
    * cutting moves leaving the stock in XY while below the top face;
    * any move reaching below the stock bottom (cut-through).

    Checks are evaluated at trace vertices (arcs are already sampled upstream), so
    a move that only clips a corner *between* vertices may be missed — a
    conservative, fast screen, not a guarantee.
    """
    x_min, x_max = stock.x0, stock.x0 + stock.size_x
    y_min, y_max = stock.y0, stock.y0 + stock.size_y
    rapid_into = cut_outside = through_bottom = 0
    for p in trace.points[1:]:
        in_x = (x_min - _EPS) <= p.x <= (x_max + _EPS)
        in_y = (y_min - _EPS) <= p.y <= (y_max + _EPS)
        within_xy = in_x and in_y
        below_top = p.z < stock.top - _EPS
        if p.rapid and within_xy and below_top:
            rapid_into += 1
        if not p.rapid and below_top and not within_xy:
            cut_outside += 1
        if within_xy and p.z < stock.bottom - _EPS:
            through_bottom += 1

    warnings: list[str] = []
    if rapid_into:
        warnings.append(
            f"{rapid_into} rapid move(s) travel below the stock top inside the stock "
            "- collision risk"
        )
    if cut_outside:
        warnings.append(f"{cut_outside} cutting move(s) leave the stock footprint in XY")
    if through_bottom:
        warnings.append(
            f"{through_bottom} move(s) reach below the stock bottom "
            f"(Z < {stock.bottom:.3f}) - cut-through"
        )
    return tuple(warnings)


__all__ = [
    "DEFAULT_RESOLUTION",
    "CarveResult",
    "HeightMapCarver",
    "OriginPreset",
    "Stock",
    "Tool",
    "cam_warnings",
    "carve",
]
