"""Workpiece-simulation core: the pure parse -> simulate -> carve pipeline.

No Qt, no GPU — just the cncctl engine (``gcode.parse`` -> ``viz.simulate`` ->
``viz.workpiece.carve``) wrapped for the GUI, plus a headless matplotlib PNG
export. The interactive 3D widget (``workpiece_view.py``) and the headless
renderer both build on this, so the heavy GUI dependencies stay out of the
headless path: ``--render`` needs only the core library + matplotlib.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import msgspec

from cncctl.viz.analyze import SoftLimits, analyze
from cncctl.viz.simulate import Kinematics, Trace, simulate
from cncctl.gcode.parse import parse_string
from cncctl.viz.workpiece import CarveResult, OriginPreset, Stock, Tool, cam_warnings, carve

# Timing is geometry-independent here, so a fixed scalar cap is fine — the
# dry-run duration in the report is only a rough estimate.
_KINEMATICS = Kinematics(max_rate_mm_min=2000.0)

# Origin presets, in the order shown in the combo box. Mirrors the reference.
PRESETS: list[tuple[str, OriginPreset]] = [
    ("Top, front-left corner (X0 Y0 corner, Z0 top)", OriginPreset.TOP_CORNER),
    ("Top centre (X0 Y0 centre, Z0 top)", OriginPreset.TOP_CENTER),
    ("Top, front edge centre (X0 centre, Y0 front, Z0 top)", OriginPreset.TOP_FRONT_CENTER),
    ("Bottom, front-left corner (X0 Y0 corner, Z0 bottom)", OriginPreset.BOTTOM_CORNER),
]

# Shown on first launch so there is always something to look at: a small raster
# pocket (cleared by a 6 mm tool) plus a bored circle (an arc) on an 80x50x10 block.
DEMO_GCODE = """\
; cncctl workpiece-sim demo: raster pocket + bored circle
G21 G90
G0 Z5
G0 X15 Y15
G1 Z-3 F120
G1 Y35 F600
G1 X21
G1 Y15
G1 X27
G1 Y35
G1 X33
G1 Y15
G1 X39
G1 Y35
G1 X45
G1 Y15
G0 Z5
G0 X62 Y25
G1 Z-4 F120
G2 X62 Y25 I8 J0 F600
G0 Z5
M5
M2
"""


def mirror_trace(trace: Trace, *, mirror_x: bool = False, mirror_y: bool = False) -> Trace:
    """Flip a trace about its own bounding-box centre on X and/or Y.

    Preview helper for mirrored CAM output (e.g. a FlatCAM bottom-copper layer is
    exported mirrored for back-side milling, so it reads mirror-imaged top-down).
    Mirroring about the centre keeps the path in place but un-mirrors the image.
    """
    if not (mirror_x or mirror_y) or not trace.points:
        return trace
    cx = min(p.x for p in trace.points) + max(p.x for p in trace.points)
    cy = min(p.y for p in trace.points) + max(p.y for p in trace.points)
    points = tuple(
        msgspec.structs.replace(
            p,
            x=(cx - p.x) if mirror_x else p.x,
            y=(cy - p.y) if mirror_y else p.y,
        )
        for p in trace.points
    )
    return Trace(points=points)


def apply_engrave_depth(trace: Trace, depth: float) -> Trace:
    """Lower every cutting (feed) move by ``depth`` mm — a *preview-only* aid.

    Some CAM output engraves at the surface (``Z_Cut = 0``), so the tool grazes
    the top and removes nothing. Sinking the feed moves by a chosen depth lets the
    track pattern be previewed as carved grooves without editing/re-exporting the
    program. Rapids and the synthetic start point are left alone. ``depth <= 0``
    is a no-op.
    """
    if depth <= 0 or len(trace.points) < 2:
        return trace
    head, *rest = trace.points
    lowered = [head] + [
        msgspec.structs.replace(p, z=p.z - depth) if not p.rapid else p for p in rest
    ]
    return Trace(points=tuple(lowered))


def simulate_workpiece(
    gcode: str,
    *,
    size_x: float,
    size_y: float,
    size_z: float,
    preset: OriginPreset,
    diameter: float,
    ball: bool,
    resolution: int,
    mirror_x: bool = False,
    mirror_y: bool = False,
    fit: bool = False,
    margin: float = 5.0,
    engrave_depth: float = 0.0,
) -> tuple[Trace, CarveResult, str]:
    """Parse -> simulate -> (mirror) -> (engrave depth) -> carve; returns trace, result, report.

    With ``fit`` the stock is sized and placed to cover the toolpath's XY extent
    (plus ``margin`` mm), with its top at Z0 and ``size_z`` thickness below — so a
    program whose coordinates live anywhere (e.g. a PCB at negative Y) lands on a
    correctly-placed block instead of floating off a fixed origin stock.
    ``engrave_depth`` is a preview-only sink for surface-level cuts (see
    :func:`apply_engrave_depth`).

    Raises whatever the pipeline raises (parse errors, ``UnsupportedGcodeError``,
    ``ValueError`` for bad stock/tool) — the caller decides how to surface it.
    """
    program = parse_string(gcode)
    trace = mirror_trace(simulate(program, _KINEMATICS), mirror_x=mirror_x, mirror_y=mirror_y)
    trace = apply_engrave_depth(trace, engrave_depth)
    stock = _fit_stock(trace, size_z, margin) if (fit and trace.points) else (
        Stock.from_origin(size_x, size_y, size_z, preset)
    )
    tool = Tool(diameter=diameter, ball=ball)
    result = carve(trace, stock, tool, resolution=resolution)
    return trace, result, build_report(trace, stock, result)


def _fit_stock(trace: Trace, size_z: float, margin: float) -> Stock:
    """A stock covering the toolpath's XY bounding box (+margin), top at Z0."""
    xs = [p.x for p in trace.points]
    ys = [p.y for p in trace.points]
    x0, y0 = min(xs) - margin, min(ys) - margin
    size_x = max((max(xs) - min(xs)) + 2 * margin, 1.0)
    size_y = max((max(ys) - min(ys)) + 2 * margin, 1.0)
    return Stock(x0, y0, -size_z, size_x, size_y, size_z)


def build_report(trace: Trace, stock: Stock, result: CarveResult) -> str:
    """A human-readable summary: extents, metrics, and CAM checks.

    The bounding box / travel / duration come from :func:`analyze` (with wide-open
    limits — retracting *above* the stock is normal, so the report's collision
    checks come from :func:`cam_warnings`, which is motion-aware, not from a naive
    bounding-box-vs-stock comparison).
    """
    wide = (float("-inf"), float("inf"))
    analysis = analyze(trace, SoftLimits(x=wide, y=wide, z=wide))
    stock_volume = stock.size_x * stock.size_y * stock.size_z
    pct = (result.removed_volume_mm3 / stock_volume * 100.0) if stock_volume else 0.0

    lo, hi = analysis.min, analysis.max
    lines = [
        "Toolpath extents (mm):",
        f"  X {lo.x:8.2f} .. {hi.x:8.2f}   stock {stock.x0:.2f} .. {stock.x0 + stock.size_x:.2f}",
        f"  Y {lo.y:8.2f} .. {hi.y:8.2f}   stock {stock.y0:.2f} .. {stock.y0 + stock.size_y:.2f}",
        f"  Z {lo.z:8.2f} .. {hi.z:8.2f}   stock {stock.bottom:.2f} .. {stock.top:.2f}",
        "",
        f"Total travel:     {analysis.total_travel_mm:10.1f} mm",
        f"Dry-run estimate: {analysis.duration_s:10.1f} s  (feed-limited, rough)",
        f"Material removed: {result.removed_volume_mm3:10.1f} mm^3  ({pct:.1f}% of stock)",
        f"Max cut depth:    {result.max_depth_mm:10.2f} mm  (stock is {stock.size_z:.2f} mm thick)",
        "",
        "Checks:",
    ]
    problems = list(cam_warnings(trace, stock))
    if problems:
        lines += [f"  ! {p}" for p in problems]
    else:
        lines.append("  OK - toolpath stays within the stock; no collisions detected.")
    return "\n".join(lines)


def render_png(
    out_path: Path,
    gcode: str,
    *,
    size_x: float,
    size_y: float,
    size_z: float,
    preset: OriginPreset,
    diameter: float,
    ball: bool,
    resolution: int,
    show_path: bool = True,
    mirror_x: bool = False,
    mirror_y: bool = False,
    fit: bool = False,
    margin: float = 5.0,
    engrave_depth: float = 0.0,
) -> str:
    """Render the carved workpiece to a PNG with matplotlib's Agg backend.

    Headless: no Qt and no display required (a Pi or CI box). Returns the report
    string. matplotlib is imported lazily so importing this module stays cheap.
    """
    import matplotlib
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.colors import LightSource
    from matplotlib.figure import Figure

    trace, result, report = simulate_workpiece(
        gcode,
        size_x=size_x,
        size_y=size_y,
        size_z=size_z,
        preset=preset,
        diameter=diameter,
        ball=ball,
        resolution=resolution,
        mirror_x=mirror_x,
        mirror_y=mirror_y,
        fit=fit,
        margin=margin,
        engrave_depth=engrave_depth,
    )
    figure = Figure(figsize=(8, 6), dpi=120)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot(projection="3d")
    x_grid, y_grid, z_grid = result.surface()
    stride = max(1, max(x_grid.shape) // 140)
    # Light the surface (relief) while keeping the depth colormap, matching the
    # GPU view's shading so the pockets/walls read as real geometry.
    stock = result.stock
    light = LightSource(azdeg=315, altdeg=45)
    vert_exag = 0.4 * max(stock.size_x, stock.size_y) / max(stock.size_z, 1e-3)
    shaded = light.shade(
        z_grid, cmap=matplotlib.colormaps["viridis"], vert_exag=vert_exag, blend_mode="soft"
    )
    axes.plot_surface(
        x_grid,
        y_grid,
        z_grid,
        facecolors=shaded,
        rstride=stride,
        cstride=stride,
        linewidth=0,
        antialiased=False,
        shade=False,
    )
    if show_path:
        axes.plot(
            [p.x for p in trace.points],
            [p.y for p in trace.points],
            [p.z for p in trace.points],
            color="crimson",
            linewidth=0.6,
        )
    axes.set_box_aspect((stock.size_x, stock.size_y, max(stock.size_z, 1e-3)))
    axes.set_xlabel("X (mm)")
    axes.set_ylabel("Y (mm)")
    axes.set_zlabel("Z (mm)")
    axes.set_title("Carved workpiece")
    figure.tight_layout()
    figure.savefig(out_path)
    return report
