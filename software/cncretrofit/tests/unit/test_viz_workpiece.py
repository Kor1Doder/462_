"""Workpiece (material-removal) engine tests (M10).

Carves heightmaps from real toolpaths and asserts the geometry: slot depth/width,
flat-vs-ball cut profile, rapids leaving the stock untouched, cut-through clamped
to the stock bottom, the volume/depth metrics, the origin presets, and the CAM
collision warnings.
"""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from cncctl.gcode.parse import parse_string
from cncctl.viz.simulate import Kinematics, simulate
from cncctl.viz.workpiece import (
    CarveResult,
    HeightMapCarver,
    OriginPreset,
    Stock,
    Tool,
    cam_warnings,
    carve,
)

# Stock: 40x40x10, top face at Z=0, bottom at Z=-10 (front-left corner = G54 zero).
_STOCK = Stock.from_origin(40.0, 40.0, 10.0, OriginPreset.TOP_CORNER)

# Plunge to Z=-3 at X=20 then cut a slot up to Y=35; rapid in and out above the stock.
_SLOT = "G90\nG0 X20 Y5 Z5\nG1 Z-3 F100\nG1 Y35 F200\nG0 Z5"


def _carve(src: str, stock: Stock, tool: Tool, resolution: int = 120) -> CarveResult:
    trace = simulate(parse_string(src), Kinematics(max_rate_mm_min=3000.0))
    return carve(trace, stock, tool, resolution=resolution)


def _height_at(result: CarveResult, x: float, y: float) -> float:
    col = int(np.argmin(np.abs(result.xs - x)))
    row = int(np.argmin(np.abs(result.ys - y)))
    return float(result.heights[row, col])


# ----- stock / tool value types ---------------------------------------------


def test_stock_top_and_bottom() -> None:
    assert _STOCK.top == pytest.approx(0.0)
    assert _STOCK.bottom == pytest.approx(-10.0)


@pytest.mark.parametrize(
    ("preset", "x0", "y0", "z0"),
    [
        (OriginPreset.TOP_CORNER, 0.0, 0.0, -10.0),
        (OriginPreset.TOP_CENTER, -20.0, -20.0, -10.0),
        (OriginPreset.TOP_FRONT_CENTER, -20.0, 0.0, -10.0),
        (OriginPreset.BOTTOM_CORNER, 0.0, 0.0, 0.0),
    ],
)
def test_origin_presets_place_the_corner(
    preset: OriginPreset, x0: float, y0: float, z0: float
) -> None:
    stock = Stock.from_origin(40.0, 40.0, 10.0, preset)
    assert (stock.x0, stock.y0, stock.z0) == (x0, y0, z0)


def test_from_origin_rejects_nonpositive_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        Stock.from_origin(0.0, 40.0, 10.0, OriginPreset.TOP_CORNER)


def test_tool_radius() -> None:
    assert Tool(diameter=6.0).radius == pytest.approx(3.0)


# ----- carving geometry ------------------------------------------------------


def test_uncut_stock_stays_at_top() -> None:
    result = _carve("(nothing)", _STOCK, Tool(diameter=6.0))
    assert float(result.heights.min()) == pytest.approx(0.0)
    assert float(result.heights.max()) == pytest.approx(0.0)
    assert result.removed_volume_mm3 == pytest.approx(0.0)
    assert result.max_depth_mm == pytest.approx(0.0)


def test_rapids_above_the_stock_remove_nothing() -> None:
    result = _carve("G90\nG0 X20 Y20 Z5\nG0 X5 Y5 Z10", _STOCK, Tool(diameter=6.0))
    assert result.removed_volume_mm3 == pytest.approx(0.0)


def test_slot_is_cut_to_depth_and_leaves_the_rest() -> None:
    result = _carve(_SLOT, _STOCK, Tool(diameter=6.0))
    assert _height_at(result, 20.0, 20.0) == pytest.approx(-3.0, abs=1e-9)  # on the slot
    assert _height_at(result, 5.0, 20.0) == pytest.approx(0.0)  # far from the slot, untouched
    assert result.max_depth_mm == pytest.approx(3.0, abs=1e-6)
    assert result.removed_volume_mm3 > 0.0


def test_flat_tool_cuts_a_flat_floor_across_its_width() -> None:
    # 2 mm off the slot centreline is still under a 6 mm flat tool (radius 3).
    result = _carve(_SLOT, _STOCK, Tool(diameter=6.0, ball=False))
    assert _height_at(result, 22.0, 20.0) == pytest.approx(-3.0, abs=1e-9)


def test_ball_tool_cuts_deeper_at_the_centre_than_at_the_edge() -> None:
    result = _carve(_SLOT, _STOCK, Tool(diameter=6.0, ball=True))
    centre = _height_at(result, 20.0, 20.0)
    edge = _height_at(result, 22.0, 20.0)
    assert centre == pytest.approx(-3.0, abs=1e-9)  # tip touches at the centreline
    assert edge > centre + 0.5  # spherical underside: shallower off-axis


def test_cut_through_is_clamped_to_the_stock_bottom() -> None:
    result = _carve("G90\nG0 X20 Y20 Z5\nG1 Z-15 F100", _STOCK, Tool(diameter=6.0))
    assert float(result.heights.min()) == pytest.approx(-10.0)  # not -15
    assert result.max_depth_mm == pytest.approx(10.0)


def test_surface_arrays_are_grid_shaped() -> None:
    result = _carve(_SLOT, _STOCK, Tool(diameter=6.0))
    x_grid, y_grid, z_grid = result.surface()
    shape = (result.ys.shape[0], result.xs.shape[0])
    assert x_grid.shape == y_grid.shape == z_grid.shape == shape


# ----- carve guards ----------------------------------------------------------


def test_carve_rejects_nonpositive_stock() -> None:
    trace = simulate(parse_string("G90\nG1 X1 F100"), Kinematics(max_rate_mm_min=3000.0))
    bad = Stock(0.0, 0.0, 0.0, 0.0, 40.0, 10.0)
    with pytest.raises(ValueError, match="positive"):
        carve(trace, bad, Tool(diameter=6.0))


def test_carve_rejects_nonpositive_tool() -> None:
    trace = simulate(parse_string("G90\nG1 X1 F100"), Kinematics(max_rate_mm_min=3000.0))
    with pytest.raises(ValueError, match="diameter"):
        carve(trace, _STOCK, Tool(diameter=0.0))


# ----- incremental carver (drives the GUI playback) --------------------------


def test_incremental_carver_matches_one_shot_carve() -> None:
    # Carving segment-by-segment must reach the same heightmap as carve() at once.
    trace = simulate(parse_string(_SLOT), Kinematics(max_rate_mm_min=3000.0))
    one_shot = carve(trace, _STOCK, Tool(diameter=6.0), resolution=120)

    carver = HeightMapCarver(_STOCK, Tool(diameter=6.0), resolution=120)
    for prev, cur in pairwise(trace.points):
        carver.carve_segment((prev.x, prev.y, prev.z), (cur.x, cur.y, cur.z))

    assert np.allclose(carver.heights, one_shot.heights)
    assert carver.metrics()[1] == pytest.approx(one_shot.max_depth_mm)


def test_carver_reset_restores_flat_stock() -> None:
    carver = HeightMapCarver(_STOCK, Tool(diameter=6.0), resolution=60)
    carver.carve_segment((20.0, 5.0, -3.0), (20.0, 35.0, -3.0))
    assert carver.metrics()[0] > 0.0  # something removed
    carver.reset()
    assert float(carver.heights.min()) == pytest.approx(_STOCK.top)
    assert carver.metrics() == (pytest.approx(0.0), pytest.approx(0.0))


# ----- CAM warnings ----------------------------------------------------------


def test_clean_program_has_no_warnings() -> None:
    trace = simulate(parse_string(_SLOT), Kinematics(max_rate_mm_min=3000.0))
    assert cam_warnings(trace, _STOCK) == ()


def test_rapid_into_stock_is_flagged() -> None:
    # Plunge, then rapid sideways while still buried in the stock.
    src = "G90\nG0 X20 Y20 Z5\nG1 Z-3 F100\nG0 X10 Y10"
    trace = simulate(parse_string(src), Kinematics(max_rate_mm_min=3000.0))
    warnings = cam_warnings(trace, _STOCK)
    assert any("rapid" in w for w in warnings)


def test_cutting_outside_the_stock_is_flagged() -> None:
    src = "G90\nG0 X20 Y20 Z5\nG1 Z-3 F100\nG1 X100 Y100 F200"
    trace = simulate(parse_string(src), Kinematics(max_rate_mm_min=3000.0))
    warnings = cam_warnings(trace, _STOCK)
    assert any("footprint" in w for w in warnings)


def test_cut_through_bottom_is_flagged() -> None:
    src = "G90\nG0 X20 Y20 Z5\nG1 Z-15 F100"
    trace = simulate(parse_string(src), Kinematics(max_rate_mm_min=3000.0))
    warnings = cam_warnings(trace, _STOCK)
    assert any("bottom" in w for w in warnings)
