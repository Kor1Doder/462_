"""Simple 2.5D CAD/CAM tab — draw shapes, set stock + origin, emit grblHAL G-code.

This is intentionally a *small* CAM, not a SolidWorks clone: you draw rectangles,
circles and polylines on a top-view (XY) canvas, place the work zero where you
want it, give each shape an operation (profile / pocket / engrave) and a cut
depth ("extrude" down into the stock), and it generates plain grblHAL G-code
(G21/G90, multi-pass Z, G0/G1) referenced to the origin you chose. The same
G-code can be previewed in the Workpiece 3D carve or pushed to the Program tab.

Pure Qt + math — no cncctl import — so it stays independent of the controller.
The host GUI wires two optional callbacks:

    widget.on_preview      = lambda gcode, label: ...   # show in the 3D carve
    widget.on_to_program   = lambda gcode, label: ...   # load into the sender
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


def depo_dir() -> Path:
    """The shared 'depo' folder where designs (.json) and G-code (.nc) are kept."""
    d = Path(__file__).resolve().parent.parent / "depo"  # pitrofit/depo
    d.mkdir(exist_ok=True)
    return d

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPaintEvent, QPen, QPolygonF, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

try:  # the 3D frame preview is optional (GUI extra: pyqtgraph + PyOpenGL)
    import numpy as np
    import pyqtgraph.opengl as gl
    from PySide6.QtGui import QVector3D

    _HAS_GL = True
except ImportError:  # pragma: no cover - depends on the optional extra
    _HAS_GL = False

# Operation kinds and their canvas colours.
OPS = {
    "profile_out": ("Profile (outside)", QColor("#38bdf8")),
    "profile_in": ("Profile (inside)", QColor("#a78bfa")),
    "pocket": ("Pocket (clear inside)", QColor("#f59e0b")),
    "engrave": ("Engrave (on the line)", QColor("#22c55e")),
    "cone": ("Conical hole (circle)", QColor("#f472b6")),
}

# Extra op tags used by the PCB tab (pcb.py) so the shared Canvas can colour them.
_EXTRA_COLORS = {
    "trace": QColor("#22c55e"), "pad": QColor("#fbbf24"), "board": QColor("#60a5fa"),
}


def color_for(op: str) -> QColor:
    """Canvas/3D colour for an op, tolerant of PCB ops not in OPS."""
    if op in OPS:
        return OPS[op][1]
    return _EXTRA_COLORS.get(op, QColor("#9ca3af"))


@dataclass
class Shape:
    """A drawn primitive in stock/world millimetres (X right, Y up)."""

    kind: str  # 'rect' | 'circle' | 'polygon' | 'poly'
    pts: list[tuple[float, float]] = field(default_factory=list)
    # rect: [(x0,y0),(x1,y1)] corners; circle/polygon: [(cx,cy),(edge)]; poly: vertices
    op: str = "profile_out"
    depth: float = 0.0  # per-shape cut depth (mm, >0); 0 = use the global default
    sides: int = 6  # regular-polygon edge count (polygon kind only)
    width: float = 0.0  # PCB trace/channel width (mm, >0); 0 = use the global default
    # Conical-hole ("cone" op): the drawn circle is the TOP diameter; these add the
    # bottom diameter and an optional straight (cylindrical) section before the taper.
    bot_dia: float = 0.0  # bottom diameter (di); 0 = half the top
    straight_mm: float = 0.0  # cylindrical section depth (L1) before the taper (L2 = depth)

    # -- geometry helpers ----------------------------------------------------
    def rect_bounds(self) -> tuple[float, float, float, float]:
        (x0, y0), (x1, y1) = self.pts[0], self.pts[1]
        return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)

    def circle(self) -> tuple[float, float, float]:
        (cx, cy), (ex, ey) = self.pts[0], self.pts[1]
        return cx, cy, math.hypot(ex - cx, ey - cy)

    def polygon(self) -> tuple[float, float, float, float]:
        """center x, y, radius, start-angle (the drawn handle is a vertex)."""
        (cx, cy), (ex, ey) = self.pts[0], self.pts[1]
        return cx, cy, math.hypot(ex - cx, ey - cy), math.atan2(ey - cy, ex - cx)

    def effective_depth(self, default: float) -> float:
        return self.depth if self.depth > 0 else default

    def outline(self) -> list[tuple[float, float]]:
        """Closed/open polyline of the raw drawn shape (for display + hit-test)."""
        if self.kind == "rect":
            x, y, w, h = self.rect_bounds()
            return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
        if self.kind == "circle":
            cx, cy, r = self.circle()
            return _circle_pts(cx, cy, r)
        if self.kind == "polygon":
            cx, cy, r, a0 = self.polygon()
            return _poly_ring(cx, cy, r, a0, self.sides)
        return list(self.pts)


def text_to_polylines(text: str, height_mm: float, x0: float, y0: float,
                     family: str = "DejaVu Sans",
                     spacing_mm: float = 0.0) -> list[list[tuple[float, float]]]:
    """Turn ``text`` into engrave polylines (glyph outlines) in world mm.

    Scaled so the cap height is ``height_mm``, bottom-left corner at (x0, y0), Y up.
    ``spacing_mm`` adds (or, if negative, removes) that much gap between letters.
    Each returned list is one closed contour (letters like O/A/e give several).
    """
    from PySide6.QtGui import QFont, QPainterPath

    font = QFont(family)
    font.setPixelSize(256)  # arbitrary internal size; we rescale to height_mm
    # Pass 1 (no spacing): the cap height sets the scale — spacing changes width,
    # not height, so this scale stays correct.
    probe = QPainterPath()
    probe.addText(0.0, 0.0, font, text)
    br0 = probe.boundingRect()
    if br0.height() <= 0:
        return []
    s = height_mm / br0.height()
    # Pass 2: apply the letter spacing (mm -> font px via the same scale).
    if spacing_mm:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing_mm / s)
    path = QPainterPath()
    path.addText(0.0, 0.0, font, text)
    br = path.boundingRect()
    out: list[list[tuple[float, float]]] = []
    for poly in path.toSubpathPolygons():
        pts = [((p.x() - br.left()) * s + x0, (br.bottom() - p.y()) * s + y0) for p in poly]
        if len(pts) >= 2:
            out.append(pts)
    return out


def _poly_ring(cx: float, cy: float, r: float, a0: float, n: int) -> list[tuple[float, float]]:
    n = max(3, int(n))
    pts = [(cx + r * math.cos(a0 + 2 * math.pi * i / n),
            cy + r * math.sin(a0 + 2 * math.pi * i / n)) for i in range(n)]
    pts.append(pts[0])  # close it
    return pts


def _circle_pts(cx: float, cy: float, r: float, chord: float = 0.2) -> list[tuple[float, float]]:
    if r <= 1e-6:
        return [(cx, cy)]
    n = max(16, min(720, int(2 * math.pi * r / max(0.05, chord))))
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]


# ============================================================ CAM (G-code gen)
@dataclass
class CamParams:
    origin_x: float = 0.0
    origin_y: float = 0.0
    tool_dia: float = 3.0
    stepover: float = 0.45  # fraction of tool dia between pocket passes
    cut_depth: float = 2.0  # total "extrude" depth into the stock (mm, positive)
    stepdown: float = 0.5  # max Z per pass
    feed: float = 300.0
    plunge: float = 100.0
    safe_z: float = 5.0
    retract: float = 1.0  # rapid clearance above the surface between moves


def _toolpaths(shape: Shape, p: CamParams) -> list[list[tuple[float, float]]]:
    """Offset/clear a shape into one or more cut polylines (world mm)."""
    r = p.tool_dia / 2.0
    step = max(0.2, p.tool_dia * p.stepover)
    if shape.kind == "rect":
        x, y, w, h = shape.rect_bounds()
        if shape.op == "engrave":
            return [_rect_loop(x, y, w, h)]
        if shape.op == "profile_out":
            return [_rect_loop(x - r, y - r, w + 2 * r, h + 2 * r)]
        if shape.op == "profile_in":
            if w - 2 * r <= 0 or h - 2 * r <= 0:
                return []
            return [_rect_loop(x + r, y + r, w - 2 * r, h - 2 * r)]
        return _pocket_rect(x, y, w, h, r, step)  # pocket
    if shape.kind == "circle":
        cx, cy, rad = shape.circle()
        if shape.op == "engrave":
            return [_circle_pts(cx, cy, rad)]
        if shape.op == "profile_out":
            return [_circle_pts(cx, cy, rad + r)]
        if shape.op == "profile_in":
            return [_circle_pts(cx, cy, rad - r)] if rad - r > 0 else []
        return _pocket_circle(cx, cy, rad, r, step)  # pocket
    if shape.kind == "polygon":
        cx, cy, rad, a0 = shape.polygon()
        n = max(3, shape.sides)
        dr = r / math.cos(math.pi / n)  # radius change that offsets the walls by r
        if shape.op == "engrave":
            return [_poly_ring(cx, cy, rad, a0, n)]
        if shape.op == "profile_out":
            return [_poly_ring(cx, cy, rad + dr, a0, n)]
        if shape.op == "profile_in":
            return [_poly_ring(cx, cy, rad - dr, a0, n)] if rad - dr > 0 else []
        rings, rr = [], rad - dr  # pocket: concentric rings inward
        rstep = max(0.2, step / math.cos(math.pi / n))
        while rr > rstep * 0.5:
            rings.append(_poly_ring(cx, cy, rr, a0, n)); rr -= rstep
        rings.append([(cx, cy)])
        return rings
    # polyline (arbitrary chain)
    pts = list(shape.pts)
    if shape.op == "pocket" and len(pts) >= 3:
        return _pocket_poly(pts, r, step)  # clear the inside of the closed outline
    # engrave / profile: follow the drawn line (no robust offset for open chains)
    return [pts] if len(pts) >= 2 else []


def _pocket_poly(pts, r, step) -> list[list[tuple[float, float]]]:  # noqa: ANN001
    """Clear the inside of a closed polyline by a boustrophedon raster fill.

    Scan-lines are intersected with the (auto-closed) outline and filled between
    even-odd pairs, so concave outlines work too; each span is inset by the tool
    radius ``r`` (and the Y range by ``r``) to keep the tool off the walls. One
    serpentine path, matching ``_pocket_rect``'s single-plunge raster."""
    poly = list(pts)
    if poly[0] != poly[-1]:
        poly.append(poly[0])  # close it
    ys = [y for _, y in poly]
    ymin, ymax = min(ys) + r, max(ys) - r
    if ymax <= ymin:
        return []
    raster: list[tuple[float, float]] = []
    yy, left = ymin, True
    while yy <= ymax + 1e-6:
        xints: list[float] = []
        for (x0, y0), (x1, y1) in zip(poly, poly[1:]):
            if (y0 <= yy < y1) or (y1 <= yy < y0):  # edge crosses this scan-line
                xints.append(x0 + (yy - y0) / (y1 - y0) * (x1 - x0))
        xints.sort()
        spans = [(a + r, b - r) for a, b in zip(xints[::2], xints[1::2]) if b - a > 2 * r]
        if not left:  # serpentine: reverse direction every row
            spans = [(b, a) for a, b in reversed(spans)]
        for a, b in spans:
            raster += [(a, yy), (b, yy)]
        yy += step
        left = not left
    return [raster] if raster else []


def _rect_loop(x: float, y: float, w: float, h: float) -> list[tuple[float, float]]:
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]


def _pocket_rect(x, y, w, h, r, step) -> list[list[tuple[float, float]]]:  # noqa: ANN001
    ix0, iy0, ix1, iy1 = x + r, y + r, x + w - r, y + h - r
    if ix1 <= ix0 or iy1 <= iy0:
        return []
    raster: list[tuple[float, float]] = []
    yy = iy0
    left = True
    while yy <= iy1 + 1e-6:
        a, b = (ix0, ix1) if left else (ix1, ix0)
        raster += [(a, yy), (b, yy)]
        yy += step
        left = not left
    perimeter = _rect_loop(ix0, iy0, ix1 - ix0, iy1 - iy0)  # clean wall pass
    return [raster, perimeter]


def _pocket_circle(cx, cy, rad, r, step) -> list[list[tuple[float, float]]]:  # noqa: ANN001
    rings: list[list[tuple[float, float]]] = []
    rr = rad - r
    while rr > step * 0.5:
        rings.append(_circle_pts(cx, cy, rr))
        rr -= step
    rings.append([(cx, cy)])  # finish the very centre
    return rings


def toolpath_bounds(shapes: list[Shape], p: CamParams) -> tuple[float, float, float, float] | None:
    """Min/max X,Y the actual cut reaches (tool-offset included). None if empty."""
    xs: list[float] = []
    ys: list[float] = []
    for sh in shapes:
        for path in _toolpaths(sh, p):
            for x, y in path:
                xs.append(x); ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def fit_check(shapes: list[Shape], p: CamParams, stock: tuple[float, float, float],
              travel: tuple[float, float, float]) -> tuple[list[str], list[str]]:
    """Does the job fit the stock and the machine envelope? -> (errors, warnings).

    ``travel`` is the machine's per-axis max travel (grblHAL $130/$131/$132) — a
    property of the *machine*, in machine coordinates. The stock must fit inside
    it, and the toolpath must stay on the stock."""
    errors: list[str] = []
    warnings: list[str] = []
    sx, sy, sz = stock
    tx, ty, tz = travel
    def _total_depth(s: Shape) -> float:
        d = s.effective_depth(p.cut_depth)
        return d + s.straight_mm if s.op == "cone" else d

    maxd = max((_total_depth(s) for s in shapes), default=p.cut_depth)
    if tx > 0 and sx > tx:
        errors.append(f"Stock X {sx:g} mm exceeds machine X travel {tx:g} mm")
    if ty > 0 and sy > ty:
        errors.append(f"Stock Y {sy:g} mm exceeds machine Y travel {ty:g} mm")
    if tz > 0 and maxd > tz:
        errors.append(f"Cut depth {maxd:g} mm exceeds machine Z travel {tz:g} mm")
    if maxd > sz:
        errors.append(f"Cut depth {maxd:g} mm is deeper than the stock ({sz:g} mm)")
    b = toolpath_bounds(shapes, p)
    if b is not None:
        bx0, by0, bx1, by1 = b
        if bx0 < -1e-6 or by0 < -1e-6 or bx1 > sx + 1e-6 or by1 > sy + 1e-6:
            warnings.append(
                f"toolpath {bx0:.1f}..{bx1:.1f} x {by0:.1f}..{by1:.1f} mm runs off "
                f"the stock (0..{sx:g} x 0..{sy:g}) — outside profiles need margin")
    return errors, warnings


def _emit_cone(out: list[str], shape: Shape, p: CamParams,
               conv: Callable[[tuple[float, float]], tuple[float, float]],
               warnings: list[str], i: int) -> bool:
    """Mill a conical (and optionally cylindrical-then-conical) hole.

    The drawn circle is the TOP diameter ``do``; it tapers to ``bot_dia`` (di)
    over the taper depth (``depth``), after an optional straight section
    ``straight_mm`` (L1). At each Z step we clear a disc whose radius follows the
    cone wall — a stack of shrinking circle-pockets, so the part the cone mates
    with drops straight in. Flat-endmill stepped finish; smaller stepdown = smoother.
    """
    if shape.kind != "circle":
        warnings.append(f"shape {i + 1} cone: draw a circle for a conical hole — skipped")
        return False
    cx, cy, top_r = shape.circle()
    do = 2 * top_r
    di = shape.bot_dia if shape.bot_dia > 0 else do / 2
    straight = max(0.0, shape.straight_mm)
    taper = shape.depth if shape.depth > 0 else p.cut_depth
    total = straight + taper
    tr = p.tool_dia / 2
    step = max(0.2, p.tool_dia * p.stepover)
    if top_r - tr <= 0:
        warnings.append(f"shape {i + 1} cone: top dia {do:g} mm smaller than the tool — skipped")
        return False
    out.append(f"; --- shape {i + 1}: conical hole  top {do:g} / bottom {di:g} mm, "
               f"straight {straight:g} + taper {taper:g} mm ---")
    z = 0.0
    while z > -total + 1e-9:
        z = max(z - p.stepdown, -total)
        depth = -z
        if depth <= straight or taper <= 1e-9:
            wall_r = do / 2
        else:
            t = min(1.0, (depth - straight) / taper)
            wall_r = (do / 2) * (1 - t) + (di / 2) * t
        clear_r = wall_r - tr
        if clear_r <= 0:
            continue  # cone too narrow here to fit the tool
        ccx, ccy = conv((cx, cy))
        out.append(f"G0 X{ccx:.3f} Y{ccy:.3f}")
        out.append(f"G0 Z{p.retract:.3f}")
        out.append(f"G1 Z{z:.3f} F{p.plunge:.1f}")  # plunge at the centre
        rr = step
        while rr <= clear_r + 1e-9:
            for pt in _circle_pts(cx, cy, rr):
                gx, gy = conv(pt)
                out.append(f"G1 X{gx:.3f} Y{gy:.3f} F{p.feed:.1f}")
            rr += step
        for pt in _circle_pts(cx, cy, clear_r):  # final wall pass at exact radius
            gx, gy = conv(pt)
            out.append(f"G1 X{gx:.3f} Y{gy:.3f} F{p.feed:.1f}")
        out.append(f"G0 Z{p.safe_z:.3f}")
    return True


def generate_gcode(shapes: list[Shape], p: CamParams) -> tuple[str, list[str]]:
    """Return (gcode_text, warnings). Coordinates are relative to the origin."""
    warnings: list[str] = []
    out: list[str] = [
        "; cncctl CAD/CAM — grblHAL G-code",
        f"; tool dia {p.tool_dia:g} mm, depth {p.cut_depth:g} mm, stepdown {p.stepdown:g} mm",
        "G21", "G90", "G17", "G94",
        f"G0 Z{p.safe_z:.3f}",
    ]

    def conv(pt: tuple[float, float]) -> tuple[float, float]:
        return pt[0] - p.origin_x, pt[1] - p.origin_y

    cut = False
    for i, shape in enumerate(shapes):
        if shape.op == "cone":  # conical hole: handled in 3D, not via flat _toolpaths
            if _emit_cone(out, shape, p, conv, warnings, i):
                cut = True
            continue
        paths = _toolpaths(shape, p)
        if not paths:
            warnings.append(f"shape {i + 1} ({shape.kind}/{shape.op}): tool too big / empty — skipped")
            continue
        sdepth = shape.effective_depth(p.cut_depth)
        out.append(
            f"; --- shape {i + 1}: {shape.kind} / {OPS[shape.op][0]} / depth {sdepth:g} mm ---")
        # Depth passes: 0 -> -sdepth in <=stepdown steps.
        depths: list[float] = []
        z = 0.0
        while z > -sdepth + 1e-9:
            z = max(z - p.stepdown, -sdepth)
            depths.append(z)
        for path in paths:
            if len(path) < 2:
                continue
            sx, sy = conv(path[0])
            out.append(f"G0 X{sx:.3f} Y{sy:.3f}")
            out.append(f"G0 Z{p.retract:.3f}")
            for z in depths:
                out.append(f"G1 Z{z:.3f} F{p.plunge:.1f}")
                for pt in path[1:]:
                    gx, gy = conv(pt)
                    out.append(f"G1 X{gx:.3f} Y{gy:.3f} F{p.feed:.1f}")
                if path[0] != path[-1]:  # open path: lift between depth passes
                    out.append(f"G0 Z{p.retract:.3f}")
                    out.append(f"G0 X{sx:.3f} Y{sy:.3f}")
            out.append(f"G0 Z{p.safe_z:.3f}")
            cut = True
    out += ["M5", f"G0 Z{p.safe_z:.3f}", "G0 X0 Y0", "M2", ""]
    if not cut:
        warnings.append("nothing to cut — draw a shape and pick an operation")
    return "\n".join(out), warnings


# ============================================================ drawing canvas
class Canvas(QWidget):
    """Top-view (XY) drawing surface in millimetres, Y pointing up."""

    selectionChanged = Signal()
    mouseMoved = Signal(float, float)

    def __init__(self, get_state: Callable[[], dict[str, Any]]) -> None:
        super().__init__()
        self._get = get_state  # pulls stock size / origin / draw-mode / op from the widget
        self.shapes: list[Shape] = []
        self.selected: int | None = None
        self._mode = "select"
        self._scale = 4.0  # px per mm
        self._pan = QPointF(40, 40)  # screen offset of world (0,0)
        self._drag_start: tuple[float, float] | None = None
        self._preview: tuple[float, float] | None = None
        self._poly: list[tuple[float, float]] = []
        self._panning: QPointF | None = None
        self._user_view = False  # set once the user zooms/pans; until then we auto-fit
        self.setMouseTracking(True)
        self.setMinimumSize(480, 360)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_mode(self, mode: str) -> None:
        self._poly = []
        self._mode = mode
        self.update()

    # -- world<->screen ------------------------------------------------------
    def w2s(self, x: float, y: float) -> QPointF:
        return QPointF(self._pan.x() + x * self._scale, self.height() - (self._pan.y() + y * self._scale))

    def s2w(self, px: float, py: float) -> tuple[float, float]:
        return ((px - self._pan.x()) / self._scale,
                ((self.height() - py) - self._pan.y()) / self._scale)

    def fit(self) -> None:
        """Scale + centre so the stock/board fills the view. Re-arms auto-fit."""
        st = self._get()
        sx, sy = st["size_x"], st["size_y"]
        if sx <= 0 or sy <= 0:
            return
        m = 40
        self._scale = max(0.5, min((self.width() - 2 * m) / sx, (self.height() - 2 * m) / sy))
        # centre the stock in whatever space is left over
        self._pan = QPointF((self.width() - sx * self._scale) / 2,
                            (self.height() - sy * self._scale) / 2)
        self._user_view = False
        self.update()

    def resizeEvent(self, e: Any) -> None:
        # The window growing (e.g. fullscreen) must rescale the drawing, not leave
        # it tiny in a corner — auto-fit until the user has zoomed/panned themselves.
        super().resizeEvent(e)
        if not self._user_view:
            self.fit()

    # -- painting ------------------------------------------------------------
    def paintEvent(self, _e: QPaintEvent) -> None:
        st = self._get()
        qp = QPainter(self)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        qp.fillRect(self.rect(), QColor("#0f1115"))
        self._draw_grid(qp)
        self._draw_stock(qp, st)
        self._draw_origin(qp, st)
        for i, sh in enumerate(self.shapes):
            self._draw_shape(qp, sh, selected=(i == self.selected))
        self._draw_inprogress(qp)

    def _draw_grid(self, qp: QPainter) -> None:
        st = self._get()
        pen = QPen(QColor("#1b2027")); pen.setWidth(1); qp.setPen(pen)
        step = 10.0
        x = 0.0
        while x <= st["size_x"] + 1e-6:
            a, b = self.w2s(x, 0), self.w2s(x, st["size_y"])
            qp.drawLine(a, b); x += step
        y = 0.0
        while y <= st["size_y"] + 1e-6:
            a, b = self.w2s(0, y), self.w2s(st["size_x"], y)
            qp.drawLine(a, b); y += step

    def _draw_stock(self, qp: QPainter, st: dict[str, Any]) -> None:
        pen = QPen(QColor("#3b4250")); pen.setWidth(2); qp.setPen(pen)
        tl = self.w2s(0, st["size_y"]); br = self.w2s(st["size_x"], 0)
        qp.drawRect(QRectF(tl, br))

    def _draw_origin(self, qp: QPainter, st: dict[str, Any]) -> None:
        o = self.w2s(st["origin_x"], st["origin_y"])
        pen = QPen(QColor("#ef4444")); pen.setWidth(2); qp.setPen(pen)
        qp.drawLine(QPointF(o.x() - 10, o.y()), QPointF(o.x() + 10, o.y()))
        qp.drawLine(QPointF(o.x(), o.y() - 10), QPointF(o.x(), o.y() + 10))
        qp.setFont(QFont("monospace", 8))
        qp.drawText(QPointF(o.x() + 8, o.y() - 6), "0,0")

    def _draw_shape(self, qp: QPainter, sh: Shape, *, selected: bool) -> None:
        color = color_for(sh.op)
        st = self._get()
        # PCB trace: draw the copper channel at its real width plus a centreline.
        if sh.op == "trace" and len(sh.pts) >= 2:
            w_mm = sh.width if sh.width > 0 else (st.get("trace_width", 0.0) or st.get("tool_dia", 0.8))
            pts = QPolygonF([self.w2s(x, y) for x, y in sh.pts])
            band = QColor(color); band.setAlpha(110)
            bp = QPen(band); bp.setWidthF(max(2.0, w_mm * self._scale))
            bp.setCapStyle(Qt.PenCapStyle.RoundCap); bp.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            qp.setPen(bp); qp.drawPolyline(pts)
            cl = QPen(QColor("#ffffff") if selected else color); cl.setWidth(3 if selected else 1)
            qp.setPen(cl); qp.drawPolyline(pts)
            return
        # PCB pad: filled copper disc with the drill hole punched out.
        if sh.op == "pad":
            cx, cy, r = sh.circle()
            c = self.w2s(cx, cy); rpx = max(2.0, r * self._scale)
            qp.setPen(Qt.PenStyle.NoPen); qp.setBrush(color)
            qp.drawEllipse(c, rpx, rpx)
            hpx = max(1.5, (st.get("tool_dia", 0.8) / 2) * self._scale)
            qp.setBrush(QColor("#0f1115")); qp.drawEllipse(c, hpx, hpx)
            if selected:
                qp.setBrush(Qt.BrushStyle.NoBrush)
                sp = QPen(QColor("#ffffff")); sp.setWidth(2); qp.setPen(sp)
                qp.drawEllipse(c, rpx, rpx)
            return
        # Conical hole: top diameter solid, bottom diameter dashed (the taper).
        if sh.op == "cone":
            cx, cy, r = sh.circle()
            br = (sh.bot_dia / 2) if sh.bot_dia > 0 else r / 2
            c = self.w2s(cx, cy)
            tp = QPen(QColor("#ffffff") if selected else color); tp.setWidth(2)
            qp.setBrush(Qt.BrushStyle.NoBrush); qp.setPen(tp)
            qp.drawEllipse(c, r * self._scale, r * self._scale)
            dp = QPen(color); dp.setStyle(Qt.PenStyle.DashLine); qp.setPen(dp)
            qp.drawEllipse(c, br * self._scale, br * self._scale)
            return
        pen = QPen(QColor("#ffffff") if selected else color)
        pen.setWidth(3 if selected else 2)
        qp.setPen(pen)
        poly = QPolygonF([self.w2s(x, y) for x, y in sh.outline()])
        qp.drawPolyline(poly)

    def _draw_inprogress(self, qp: QPainter) -> None:
        pen = QPen(QColor("#94a3b8")); pen.setWidth(1); pen.setStyle(Qt.PenStyle.DashLine)
        qp.setPen(pen)
        if self._drag_start and self._preview:
            x0, y0 = self._drag_start; x1, y1 = self._preview
            if self._mode == "rect":
                qp.drawPolygon(QPolygonF([self.w2s(x0, y0), self.w2s(x1, y0),
                                          self.w2s(x1, y1), self.w2s(x0, y1)]))
            elif self._mode == "circle":
                r = math.hypot(x1 - x0, y1 - y0)
                qp.drawPolyline(QPolygonF([self.w2s(px, py) for px, py in _circle_pts(x0, y0, r)]))
            elif self._mode == "polygon":
                r = math.hypot(x1 - x0, y1 - y0); a0 = math.atan2(y1 - y0, x1 - x0)
                ring = _poly_ring(x0, y0, r, a0, int(self._get().get("sides", 6)))
                qp.drawPolyline(QPolygonF([self.w2s(px, py) for px, py in ring]))
        if self._poly:
            pts = self._poly + ([self._preview] if self._preview else [])
            qp.drawPolyline(QPolygonF([self.w2s(x, y) for x, y in pts]))

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, e: QMouseEvent) -> None:
        if e.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._panning = e.position(); return
        wx, wy = self.s2w(e.position().x(), e.position().y())
        if self._mode in ("rect", "circle", "polygon"):
            self._drag_start = (wx, wy)
        elif self._mode == "origin":
            st = self._get(); st["set_origin"](wx, wy); self.update()
        elif self._mode == "poly":
            self._poly.append((wx, wy)); self.update()
        elif self._mode == "select":
            self.selected = self._hit(wx, wy); self.selectionChanged.emit(); self.update()

    def mouseMoveEvent(self, e: QMouseEvent) -> None:
        wx, wy = self.s2w(e.position().x(), e.position().y())
        self.mouseMoved.emit(wx, wy)
        if self._panning is not None:
            d = e.position() - self._panning
            self._pan += QPointF(d.x(), -d.y()); self._panning = e.position()
            self._user_view = True; self.update(); return
        if self._drag_start or self._poly:
            self._preview = (wx, wy); self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:
        if e.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._panning = None; return
        if self._mode in ("rect", "circle", "polygon") and self._drag_start and self._preview:
            st = self._get()
            if math.hypot(self._preview[0] - self._drag_start[0],
                          self._preview[1] - self._drag_start[1]) > 0.5:
                sh = Shape(self._mode, [self._drag_start, self._preview], st["op"])
                if self._mode == "polygon":
                    sh.sides = int(st.get("sides", 6))
                self.shapes.append(sh)
                self.selected = len(self.shapes) - 1; self.selectionChanged.emit()
            self._drag_start = None; self._preview = None; self.update()

    def mouseDoubleClickEvent(self, _e: QMouseEvent) -> None:
        if self._mode == "poly" and len(self._poly) >= 2:
            self.shapes.append(Shape("poly", list(self._poly), self._get()["op"]))
            self.selected = len(self.shapes) - 1; self.selectionChanged.emit()
            self._poly = []; self._preview = None; self.update()

    def wheelEvent(self, e: QWheelEvent) -> None:
        wx, wy = self.s2w(e.position().x(), e.position().y())
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self._scale = max(0.3, min(60.0, self._scale * f))
        nx, ny = self.s2w(e.position().x(), e.position().y())
        self._pan += QPointF((wx - nx) * self._scale, (wy - ny) * self._scale)
        self._user_view = True
        self.update()

    def keyPressEvent(self, e: QMouseEvent) -> None:  # type: ignore[override]
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.selected is not None:
            self.shapes.pop(self.selected); self.selected = None
            self.selectionChanged.emit(); self.update()
        elif e.key() == Qt.Key.Key_Escape:
            self._poly = []; self._drag_start = None; self._preview = None; self.update()
        elif e.key() == Qt.Key.Key_F:  # re-fit the view to the stock/board
            self.fit()
        else:
            super().keyPressEvent(e)  # let F11 / others bubble up to the window

    def _hit(self, wx: float, wy: float) -> int | None:
        best, bestd = None, 4.0 / self._scale * 4
        for i, sh in enumerate(self.shapes):
            pts = sh.outline()
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                d = _seg_dist(wx, wy, ax, ay, bx, by)
                if d < bestd:
                    best, bestd = i, d
        return best


def _seg_dist(px, py, ax, ay, bx, by) -> float:  # noqa: ANN001
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# ============================================================ 3D frame preview
if _HAS_GL:

    class Preview3D(gl.GLViewWidget):
        """Top-down 3D view of the stock as a frame with the shapes on its top."""

        def __init__(self, get_state: Callable[[], dict[str, Any]],
                     get_shapes: Callable[[], list[Shape]]) -> None:
            super().__init__()
            self._get = get_state
            self._shapes = get_shapes
            self._items: list[Any] = []
            self.setBackgroundColor("#0f1115")
            self._framed = False

        def refresh(self) -> None:
            for it in self._items:
                self.removeItem(it)
            self._items = []
            st = self._get()
            sx, sy, sz = st["size_x"], st["size_y"], st["size_z"]

            box = gl.GLBoxItem(size=QVector3D(sx, sy, sz), color=(120, 140, 170, 255))
            box.translate(0, 0, -sz)  # top face sits at z=0
            self._add(box)
            grid = gl.GLGridItem(); grid.setSize(sx, sy); grid.setSpacing(10, 10)
            grid.translate(sx / 2, sy / 2, 0)
            self._add(grid)

            default_d = st.get("default_depth", 0.0)
            for sh in self._shapes():  # each shape as a recess: top + floor + walls
                pts = sh.outline()
                if len(pts) < 2:
                    continue
                c = color_for(sh.op)
                col = (c.redF(), c.greenF(), c.blueF(), 1.0)
                d = sh.effective_depth(default_d)
                top = np.array([(x, y, 0.0) for x, y in pts], dtype=np.float32)
                self._add(gl.GLLinePlotItem(pos=top, width=2, color=col, antialias=True))
                if d > 0:
                    floor = np.array([(x, y, -d) for x, y in pts], dtype=np.float32)
                    self._add(gl.GLLinePlotItem(pos=floor, width=2, color=col, antialias=True))
                    stepk = max(1, len(pts) // 12)  # a few vertical wall lines
                    for k in range(0, len(pts), stepk):
                        x, y = pts[k]
                        wall = np.array([(x, y, 0.0), (x, y, -d)], dtype=np.float32)
                        self._add(gl.GLLinePlotItem(pos=wall, width=1, color=col, antialias=True))

            ox, oy = st["origin_x"], st["origin_y"]
            cross = np.array([(ox - 5, oy, 0.1), (ox + 5, oy, 0.1),
                              (ox, oy, 0.1), (ox, oy - 5, 0.1), (ox, oy + 5, 0.1)],
                             dtype=np.float32)
            self._add(gl.GLLinePlotItem(pos=cross, width=3, color=(1, 0.2, 0.2, 1)))

            if not self._framed:  # keep the user's orbit on later refreshes
                self.opts["center"] = QVector3D(sx / 2, sy / 2, 0)
                # near-top, slightly tilted so the recess depths are visible
                self.setCameraPosition(distance=max(sx, sy) * 1.8, elevation=68, azimuth=-90)
                self._framed = True

        def _add(self, item: Any) -> None:
            self.addItem(item); self._items.append(item)


# ============================================================ the tab widget
class CadCamWidget(QWidget):
    """Draw shapes, set stock + origin, generate grblHAL G-code."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_preview: Callable[[str, str], None] | None = None
        self.on_to_program: Callable[[str, str], None] | None = None
        self._origin = [0.0, 0.0]
        self._gcode = ""
        self._errors: list[str] = []
        self._ef: dict[str, Any] = {}  # current shape-editor field widgets
        self._editor_inner: QWidget | None = None
        self._building_editor = False
        self._numpad: Callable[[Any], None] | None = None  # touch numpad attacher
        self._build_ui()
        self.canvas.fit()
        self._apply_zero_ref()  # default: bottom-left corner = G-code 0,0

    def _state(self) -> dict[str, Any]:
        return {
            "size_x": self.s_x.value(), "size_y": self.s_y.value(), "size_z": self.s_z.value(),
            "origin_x": self._origin[0], "origin_y": self._origin[1],
            "op": self.op_combo.currentData(),
            "sides": self.sides_spin.value(),
            "default_depth": self.depth.value(),
            "set_origin": self._set_origin,
        }

    def _set_origin_xy(self, x: float, y: float) -> None:
        self._origin = [round(x, 3), round(y, 3)]
        self.origin_lbl.setText(f"origin: X{x:.1f}  Y{y:.1f}  (G-code 0,0)")
        self.canvas.update()
        if self.preview3d is not None and self.stack.currentWidget() is self.preview3d:
            self.preview3d.refresh()

    def _set_origin(self, x: float, y: float) -> None:
        """From a canvas click in 'Set 0' mode → switch the preset to Custom."""
        self.zero_ref.blockSignals(True)
        self.zero_ref.setCurrentIndex(self.zero_ref.findData("custom"))
        self.zero_ref.blockSignals(False)
        self._set_origin_xy(x, y)

    def _apply_zero_ref(self) -> None:
        """Place the work origin at the chosen stock reference; G-code follows it."""
        key = self.zero_ref.currentData()
        sx, sy = self.s_x.value(), self.s_y.value()
        presets = {"bl": (0.0, 0.0), "center": (sx / 2, sy / 2),
                   "tl": (0.0, sy), "br": (sx, 0.0)}
        if key in presets:
            self._set_origin_xy(*presets[key])

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        self.canvas = Canvas(self._state)
        self.canvas.selectionChanged.connect(self._on_select)
        self.canvas.mouseMoved.connect(
            lambda x, y: self.pos_lbl.setText(f"X{x:7.2f}  Y{y:7.2f} mm"))

        # 2D editor + optional top-down 3D frame view, swapped by a toggle.
        self.stack = QStackedWidget()
        self.stack.addWidget(self.canvas)
        self.preview3d = Preview3D(self._state, lambda: self.canvas.shapes) if _HAS_GL else None
        if self.preview3d is not None:
            self.stack.addWidget(self.preview3d)

        vrow = QHBoxLayout()
        self.b2d = QPushButton("2D edit"); self.b3d = QPushButton("3D frame (top)")
        for b in (self.b2d, self.b3d):
            b.setCheckable(True)
        self.b2d.setChecked(True)
        grp = QButtonGroup(self); grp.addButton(self.b2d); grp.addButton(self.b3d)
        self.b2d.clicked.connect(lambda: self.stack.setCurrentWidget(self.canvas))
        self.b3d.clicked.connect(self._show_3d)
        vrow.addWidget(self.b2d); vrow.addWidget(self.b3d); vrow.addStretch(1)
        left = QVBoxLayout(); left.addLayout(vrow); left.addWidget(self.stack, 1)
        leftw = QWidget(); leftw.setLayout(left)

        side = QVBoxLayout()
        side.addWidget(self._tools_box())
        side.addWidget(self._editor_box())
        side.addWidget(self._stock_box())
        side.addWidget(self._machine_box())
        side.addWidget(self._cam_box())
        side.addWidget(self._out_box(), 1)
        panel = QWidget(); panel.setLayout(side)
        scroll = QScrollArea(); scroll.setWidget(panel); scroll.setWidgetResizable(True)
        scroll.setFixedWidth(350)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        root.addWidget(leftw, 1)
        root.addWidget(scroll)

    def _show_3d(self) -> None:
        if self.preview3d is None:
            self.b2d.setChecked(True)
            return
        self.preview3d.refresh()
        self.stack.setCurrentWidget(self.preview3d)

    def enable_touch(self, hook: Callable[[Any], None]) -> None:
        """Attach an on-screen numpad to every spinbox (touchscreen use). Also
        applied to the per-shape editor fields as they are (re)created."""
        self._numpad = hook
        for sp in self.findChildren(QAbstractSpinBox):
            hook(sp)

    def _tools_box(self) -> QWidget:
        box = QGroupBox("Draw"); v = QVBoxLayout(box)
        self.tool_group = QButtonGroup(self)
        row = QHBoxLayout()
        for mode, text in (("select", "Select"), ("rect", "Rect"), ("circle", "Circle"),
                           ("polygon", "Polygon"), ("poly", "Polyline"), ("origin", "Set 0")):
            b = QPushButton(text); b.setCheckable(True)
            b.clicked.connect(lambda _c=False, m=mode: self.canvas.set_mode(m))
            self.tool_group.addButton(b); row.addWidget(b)
            if mode == "select":
                b.setChecked(True)
        v.addLayout(row)
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Polygon sides:"))
        self.sides_spin = QSpinBox(); self.sides_spin.setRange(3, 24); self.sides_spin.setValue(6)
        srow.addWidget(self.sides_spin); srow.addStretch(1)
        v.addLayout(srow)
        orow = QHBoxLayout()
        orow.addWidget(QLabel("Operation:"))
        self.op_combo = QComboBox()
        for key, (label, _c) in OPS.items():
            self.op_combo.addItem(label, key)
        self.op_combo.currentIndexChanged.connect(self._apply_op_to_selection)
        orow.addWidget(self.op_combo, 1)
        v.addLayout(orow)

        # Part zero (work origin): where G-code coordinate 0,0 sits on the stock.
        # All generated G-code is referenced to this point.
        zrow = QHBoxLayout()
        zrow.addWidget(QLabel("Part zero:"))
        self.zero_ref = QComboBox()
        for label, key in (("Bottom-left corner", "bl"), ("Center", "center"),
                           ("Top-left corner", "tl"), ("Bottom-right corner", "br"),
                           ("Custom (click 'Set 0')", "custom")):
            self.zero_ref.addItem(label, key)
        self.zero_ref.currentIndexChanged.connect(self._apply_zero_ref)
        zrow.addWidget(self.zero_ref, 1)
        v.addLayout(zrow)
        self.origin_lbl = QLabel("origin: X0.0  Y0.0")
        v.addWidget(self.origin_lbl)
        txt = QPushButton("Add text (engrave)…"); txt.clicked.connect(self._add_text)
        v.addWidget(txt)
        drow = QHBoxLayout()
        d = QPushButton("Delete selected"); d.clicked.connect(self._delete_selected)
        c = QPushButton("Clear all"); c.clicked.connect(self._clear)
        drow.addWidget(d); drow.addWidget(c); v.addLayout(drow)
        self.pos_lbl = QLabel("X   0.00  Y   0.00 mm"); self.pos_lbl.setFont(QFont("monospace", 9))
        v.addWidget(self.pos_lbl)
        return box

    def _stock_box(self) -> QWidget:
        box = QGroupBox("Stock (mm)"); v = QHBoxLayout(box)
        self.s_x = self._spin(10, 1000, 80); self.s_y = self._spin(10, 1000, 50)
        self.s_z = self._spin(1, 200, 10)
        for lbl, sp in (("X", self.s_x), ("Y", self.s_y), ("Z", self.s_z)):
            v.addWidget(QLabel(lbl)); v.addWidget(sp)
        for sp in (self.s_x, self.s_y):
            sp.valueChanged.connect(self._on_stock_changed)
        return box

    def _on_stock_changed(self) -> None:
        self.canvas.fit()
        self._apply_zero_ref()  # keep a corner/center preset pinned as size changes
        if self.preview3d is not None and self.stack.currentWidget() is self.preview3d:
            self.preview3d.refresh()

    def _machine_box(self) -> QWidget:
        box = QGroupBox("Machine max travel (mm) — $130/131/132")
        v = QHBoxLayout(box)
        self.t_x = self._spin(0, 5000, 200); self.t_y = self._spin(0, 5000, 150)
        self.t_z = self._spin(0, 5000, 100)
        for lbl, sp in (("X", self.t_x), ("Y", self.t_y), ("Z", self.t_z)):
            v.addWidget(QLabel(lbl)); v.addWidget(sp)
        return box

    def set_machine_travel(self, x: float, y: float, z: float) -> None:
        """Push the connected machine's real $130/$131/$132 into the fit-check."""
        for sp, val in ((self.t_x, x), (self.t_y, y), (self.t_z, z)):
            if val > 0:
                sp.setValue(val)

    def _cam_box(self) -> QWidget:
        box = QGroupBox("Cutting"); v = QVBoxLayout(box)
        self.tool = self._spin(0.1, 50, 3.0, step=0.1)
        self.depth = self._spin(0.1, 200, 2.0, step=0.1)
        self.stepdown = self._spin(0.05, 50, 0.5, step=0.05)
        self.stepover = self._spin(5, 90, 45, step=5)
        self.feed = self._spin(10, 5000, 300, step=10)
        self.plunge = self._spin(5, 2000, 100, step=10)
        self.safez = self._spin(0.5, 50, 5.0, step=0.5)
        for lbl, sp in (("Tool dia", self.tool), ("Cut depth (extrude)", self.depth),
                        ("Z step-down", self.stepdown), ("Stepover %", self.stepover),
                        ("Feed", self.feed), ("Plunge feed", self.plunge), ("Safe Z", self.safez)):
            r = QHBoxLayout(); r.addWidget(QLabel(lbl)); r.addStretch(1); r.addWidget(sp)
            v.addLayout(r)
        return box

    def _out_box(self) -> QWidget:
        box = QGroupBox("G-code"); v = QVBoxLayout(box)
        gen = QPushButton("Generate G-code"); gen.setObjectName("start")
        gen.clicked.connect(self._generate); v.addWidget(gen)
        self.fit_lbl = QLabel("fit: —"); self.fit_lbl.setWordWrap(True)
        v.addWidget(self.fit_lbl)
        self.text = QPlainTextEdit(); self.text.setReadOnly(True)
        self.text.setFont(QFont("monospace", 9)); v.addWidget(self.text, 1)
        row = QHBoxLayout()
        for text, fn in (("Preview 3D", self._preview), ("→ Program", self._to_program),
                         ("Save .nc", self._save)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        v.addLayout(row)
        drow = QHBoxLayout()
        for text, fn in (("Save design (depo)", self._save_design),
                         ("Open design", self._open_design)):
            b = QPushButton(text); b.clicked.connect(fn); drow.addWidget(b)
        v.addLayout(drow)
        return box

    # -- design persistence (depo) -------------------------------------------
    def _design_dict(self) -> dict[str, Any]:
        return {
            "type": "cadcam",
            "stock": [self.s_x.value(), self.s_y.value(), self.s_z.value()],
            "origin": list(self._origin),
            "shapes": [asdict(s) for s in self.canvas.shapes],
        }

    def _save_design(self) -> None:
        name, _ = QFileDialog.getSaveFileName(
            self, "Save design", str(depo_dir() / "design.json"), "Design (*.json)")
        if name:
            Path(name).write_text(json.dumps(self._design_dict(), indent=1), encoding="utf-8")

    def _open_design(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open design", str(depo_dir()), "Design (*.json)")
        if not name:
            return
        data = json.loads(Path(name).read_text(encoding="utf-8"))
        sx, sy, sz = data.get("stock", [80, 50, 10])
        self.s_x.setValue(sx); self.s_y.setValue(sy); self.s_z.setValue(sz)
        self._origin = list(data.get("origin", [0.0, 0.0]))
        self.canvas.shapes = [
            Shape(**{k: ([tuple(p) for p in v] if k == "pts" else v) for k, v in d.items()})
            for d in data.get("shapes", [])
        ]
        self.canvas.selected = None
        self._rebuild_shape_editor()
        self.canvas.fit()

    @staticmethod
    def _spin(lo: float, hi: float, val: float, step: float = 1.0) -> QDoubleSpinBox:
        s = QDoubleSpinBox(); s.setRange(lo, hi); s.setValue(val); s.setSingleStep(step)
        s.setDecimals(2 if step < 1 else 1); s.setMaximumWidth(90)
        return s

    # -- actions -------------------------------------------------------------
    def _on_select(self) -> None:
        i = self.canvas.selected
        if i is not None:
            key = self.canvas.shapes[i].op
            self.op_combo.blockSignals(True)
            self.op_combo.setCurrentIndex(self.op_combo.findData(key))
            self.op_combo.blockSignals(False)
        self._rebuild_shape_editor()

    # -- per-shape numeric editor --------------------------------------------
    def _editor_box(self) -> QWidget:
        box = QGroupBox("Selected shape (mm, from 0)")
        self.editor_layout = QVBoxLayout(box)
        self._rebuild_shape_editor()
        return box

    def _rebuild_shape_editor(self) -> None:
        self._building_editor = True
        if self._editor_inner is not None:
            self._editor_inner.deleteLater()
        inner = QWidget(); form = QFormLayout(inner)
        self._ef = {}
        i = self.canvas.selected
        shapes = self.canvas.shapes
        if i is None or i >= len(shapes):
            form.addRow(QLabel("Select a shape (Select tool, click it)\nto edit its exact size."))
        else:
            sh = shapes[i]
            ox, oy = self._origin
            form.addRow(QLabel(f"<b>{sh.kind} · {OPS[sh.op][1].name()}</b>"))
            if sh.kind == "rect":
                x, y, w, h = sh.rect_bounds()
                self._add_field(form, "x", "X from 0", x - ox)
                self._add_field(form, "y", "Y from 0", y - oy)
                self._add_field(form, "w", "Width", w, lo=0.1)
                self._add_field(form, "h", "Height", h, lo=0.1)
            elif sh.kind in ("circle", "polygon"):
                cx, cy, r = (sh.circle() if sh.kind == "circle" else sh.polygon()[:3])
                self._add_field(form, "cx", "Center X from 0", cx - ox)
                self._add_field(form, "cy", "Center Y from 0", cy - oy)
                if sh.op == "cone":  # the circle is the top of the cone
                    self._add_field(form, "topdia", "Top dia do", 2 * r, lo=0.2)
                    botd = sh.bot_dia if sh.bot_dia > 0 else r
                    self._add_field(form, "botdia", "Bottom dia di", botd, lo=0.0)
                    self._add_field(form, "straight", "Straight depth L1", sh.straight_mm, lo=0.0)
                else:
                    self._add_field(form, "r", "Radius", r, lo=0.1)
                if sh.kind == "polygon":
                    ss = QSpinBox(); ss.setRange(3, 24); ss.setValue(sh.sides)
                    ss.valueChanged.connect(self._apply_shape_edits)
                    if self._numpad:
                        self._numpad(ss)
                    self._ef["sides"] = ss; form.addRow("Sides", ss)
            else:
                form.addRow(QLabel(f"polyline, {len(sh.pts)} points"))
            opc = QComboBox()
            for key, (label, _c) in OPS.items():
                opc.addItem(label, key)
            opc.setCurrentIndex(opc.findData(sh.op))
            opc.currentIndexChanged.connect(self._apply_shape_edits)
            self._ef["op"] = opc; form.addRow("Operation", opc)
            dlabel = "Taper depth L2" if sh.op == "cone" else f"Depth (0=global {self.depth.value():g})"
            self._add_field(form, "depth", dlabel, sh.depth, lo=0.0)
        self.editor_layout.addWidget(inner)
        self._editor_inner = inner
        self._building_editor = False

    def _add_field(self, form: QFormLayout, name: str, label: str, val: float,
                   lo: float = -100000.0, hi: float = 100000.0) -> None:
        sp = QDoubleSpinBox(); sp.setRange(lo, hi); sp.setDecimals(2); sp.setSingleStep(1.0)
        sp.setValue(val); sp.valueChanged.connect(self._apply_shape_edits)
        if self._numpad:
            self._numpad(sp)
        self._ef[name] = sp; form.addRow(label, sp)

    def _apply_shape_edits(self) -> None:
        if self._building_editor:
            return
        i = self.canvas.selected
        if i is None or not self._ef:
            return
        sh = self.canvas.shapes[i]
        ox, oy = self._origin
        ef = self._ef
        if "op" in ef:
            sh.op = ef["op"].currentData()
        if "depth" in ef:
            sh.depth = ef["depth"].value()
        if sh.kind == "rect":
            x = ef["x"].value() + ox; y = ef["y"].value() + oy
            sh.pts = [(x, y), (x + ef["w"].value(), y + ef["h"].value())]
        elif sh.kind in ("circle", "polygon"):
            cx = ef["cx"].value() + ox; cy = ef["cy"].value() + oy
            if "topdia" in ef:  # cone: radius from top diameter, plus di / L1
                r = ef["topdia"].value() / 2
                sh.bot_dia = ef["botdia"].value()
                sh.straight_mm = ef["straight"].value()
            else:
                r = ef["r"].value()
            a0 = sh.polygon()[3] if sh.kind == "polygon" else 0.0
            sh.pts = [(cx, cy), (cx + r * math.cos(a0), cy + r * math.sin(a0))]
            if sh.kind == "polygon" and "sides" in ef:
                sh.sides = ef["sides"].value()
        self.canvas.update()
        if self.preview3d is not None and self.stack.currentWidget() is self.preview3d:
            self.preview3d.refresh()

    def _apply_op_to_selection(self) -> None:
        i = self.canvas.selected
        if i is not None:
            self.canvas.shapes[i].op = self.op_combo.currentData()
            self.canvas.update()

    def _delete_selected(self) -> None:
        i = self.canvas.selected
        if i is not None:
            self.canvas.shapes.pop(i); self.canvas.selected = None; self.canvas.update()

    def _clear(self) -> None:
        self.canvas.shapes.clear(); self.canvas.selected = None; self.canvas.update()

    def _add_text(self) -> None:
        """Ask for a line of text and add it as engrave polylines on the stock."""
        dlg = QDialog(self); dlg.setWindowTitle("Add text (engrave)")
        form = QFormLayout(dlg)
        te = QLineEdit("TEXT")
        h = self._spin(1, 500, 10.0, step=1.0)
        gap = self._spin(-50, 50, 0.0, step=0.5)
        x = self._spin(-10000, 10000, 5.0, step=1.0)
        y = self._spin(-10000, 10000, 5.0, step=1.0)
        for lbl, wdg in (("Text", te), ("Height (mm)", h), ("Letter spacing (mm)", gap),
                         ("X from 0", x), ("Y from 0", y)):
            form.addRow(lbl, wdg)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted or not te.text().strip():
            return
        polys = text_to_polylines(te.text(), h.value(),
                                  x.value() + self._origin[0], y.value() + self._origin[1],
                                  spacing_mm=gap.value())
        if not polys:
            return
        for pts in polys:
            self.canvas.shapes.append(Shape("poly", pts, "engrave"))
        self.canvas.selected = None
        self.canvas.update()

    def _params(self) -> CamParams:
        return CamParams(
            origin_x=self._origin[0], origin_y=self._origin[1],
            tool_dia=self.tool.value(), stepover=self.stepover.value() / 100.0,
            cut_depth=self.depth.value(), stepdown=self.stepdown.value(),
            feed=self.feed.value(), plunge=self.plunge.value(), safe_z=self.safez.value(),
        )

    def _generate(self) -> str:
        p = self._params()
        stock = (self.s_x.value(), self.s_y.value(), self.s_z.value())
        travel = (self.t_x.value(), self.t_y.value(), self.t_z.value())
        self._errors, warns = fit_check(self.canvas.shapes, p, stock, travel)
        gcode, gwarn = generate_gcode(self.canvas.shapes, p)
        self._gcode = gcode
        banner = "".join(f"; !! ERROR: {e}\n" for e in self._errors)
        banner += "".join(f"; ! {w}\n" for w in warns + gwarn)
        self.text.setPlainText(banner + gcode)
        if self._errors:
            self.fit_lbl.setText("✗ DOES NOT FIT — " + "; ".join(self._errors))
            self.fit_lbl.setStyleSheet("color:#ef4444; font-weight:bold;")
        elif warns:
            self.fit_lbl.setText("⚠ " + "; ".join(warns))
            self.fit_lbl.setStyleSheet("color:#f59e0b;")
        else:
            self.fit_lbl.setText("✓ fits stock and machine envelope")
            self.fit_lbl.setStyleSheet("color:#22c55e;")
        return gcode

    def _blocked(self) -> bool:
        """Regenerate, and refuse if the job does not fit (hard errors)."""
        self._generate()
        if self._errors:
            QMessageBox.critical(self, "Does not fit",
                                 "This job does not fit:\n\n• " + "\n• ".join(self._errors))
            return True
        return False

    def _preview(self) -> None:
        if self._blocked():
            return
        if self.on_preview and self._gcode.strip():
            self.on_preview(self._gcode, "CAD/CAM")

    def _to_program(self) -> None:
        if self._blocked():
            return
        if self.on_to_program and self._gcode.strip():
            self.on_to_program(self._gcode, "CAD/CAM")

    def _save(self) -> None:
        g = self._gcode or self._generate()
        name, _ = QFileDialog.getSaveFileName(
            self, "Save G-code", str(depo_dir() / "drawing.nc"), "G-code (*.nc)")
        if name:
            Path(name).write_text(g, encoding="utf-8")


__all__ = ["CadCamWidget", "Shape", "CamParams", "generate_gcode"]
