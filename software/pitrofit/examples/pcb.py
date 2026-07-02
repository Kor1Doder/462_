"""PCB front-copper section — draw channels/traces, pads and a board outline,
emit grblHAL isolation G-code for a single-sided copper-clad board.

This is isolation milling (and light engraving): you draw on the copper top face
(z = 0), and the tool removes copper along the lines you draw.

* **Trace / channel** (polyline) — a single shallow pass along the path at the
  copper-clearing depth (V-bit / small end-mill). This is the groove that
  isolates / separates copper.
* **Pad / hole** (circle) — drilled at the centre (peck); if the circle is wider
  than the tool it is bored out to that radius.
* **Board outline** (rectangle) — profiled on the *outside* (offset by the tool
  radius) all the way through, to cut the finished board free.

Reuses the CAD/CAM canvas and helpers; only the G-code strategy is PCB-specific.
The host GUI wires ``on_preview`` / ``on_to_program`` exactly like the CAD tab.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

from cad_cam import Canvas, Shape, _circle_pts, depo_dir

import json
from dataclasses import asdict


class PcbParams:
    def __init__(self) -> None:
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.tool_dia = 0.8
        self.copper_depth = 0.15  # how deep the isolation groove goes (mm)
        self.channel_width = 0.0  # default channel width; 0 = a single centreline pass
        self.stepover = 0.6  # fraction of tool dia between parallel channel passes
        self.drill_depth = 2.0  # through-board hole depth
        self.board_depth = 1.8  # board thickness for the cutout
        self.stepdown = 0.5
        self.peck = 0.4
        self.feed = 120.0
        self.plunge = 40.0
        self.safe_z = 4.0
        self.retract = 0.6


def _offset_polyline(path: list[tuple[float, float]], d: float) -> list[tuple[float, float]]:
    """Offset an open polyline sideways by ``d`` (left normal). Approximate
    (per-vertex averaged normals) — fine for isolation channels."""
    if abs(d) < 1e-9 or len(path) < 2:
        return list(path)
    edges = []
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        ex, ey = bx - ax, by - ay
        ln = math.hypot(ex, ey) or 1.0
        edges.append((-ey / ln, ex / ln))
    out = []
    for i, (px, py) in enumerate(path):
        if i == 0:
            nx, ny = edges[0]
        elif i == len(path) - 1:
            nx, ny = edges[-1]
        else:
            nx, ny = edges[i - 1][0] + edges[i][0], edges[i - 1][1] + edges[i][1]
            ln = math.hypot(nx, ny) or 1.0
            nx, ny = nx / ln, ny / ln
        out.append((px + d * nx, py + d * ny))
    return out


def _channel_paths(path: list[tuple[float, float]], width: float, tool: float,
                   stepover: float) -> list[list[tuple[float, float]]]:
    """One centreline pass, or parallel passes covering ``width`` if it is wider
    than the tool."""
    if width <= tool + 1e-6:
        return [path]
    half = (width - tool) / 2.0
    step = max(0.1, tool * stepover)
    offs, o = [], -half
    while o <= half + 1e-9:
        offs.append(o); o += step
    if abs(offs[-1] - half) > 1e-6:
        offs.append(half)
    return [_offset_polyline(path, o) for o in offs]


def _depth_passes(total: float, step: float) -> list[float]:
    passes, z = [], 0.0
    while z > -total + 1e-9:
        z = max(z - step, -total)
        passes.append(z)
    return passes


def generate_pcb_gcode(shapes: list[Shape], p: PcbParams) -> tuple[str, list[str]]:
    warnings: list[str] = []
    out: list[str] = [
        "; cncctl PCB — grblHAL isolation G-code (copper top face)",
        f"; tool {p.tool_dia:g} mm, copper depth {p.copper_depth:g} mm",
        "G21", "G90", "G17", "G94", f"G0 Z{p.safe_z:.3f}",
    ]
    ox, oy = p.origin_x, p.origin_y

    def conv(pt: tuple[float, float]) -> tuple[float, float]:
        return pt[0] - ox, pt[1] - oy

    traces = [s for s in shapes if s.op == "trace"]
    pads = [s for s in shapes if s.op == "pad"]
    boards = [s for s in shapes if s.op == "board"]

    # 1) channels / traces — single shallow pass (stepped if copper_depth is deep)
    if traces:
        out.append("; --- traces / channels ---")
    for sh in traces:
        if len(sh.pts) < 2:
            continue
        d = sh.effective_depth(p.copper_depth)
        width = sh.width if sh.width > 0 else p.channel_width
        passes = _channel_paths(sh.pts, width, p.tool_dia, p.stepover)
        out.append(f"; trace: {width or p.tool_dia:g} mm wide, {d:g} mm deep, "
                   f"{len(passes)} pass(es)")
        for path in passes:
            sx, sy = conv(path[0])
            out.append(f"G0 X{sx:.3f} Y{sy:.3f}")
            out.append(f"G0 Z{p.retract:.3f}")
            for z in _depth_passes(d, p.stepdown):
                out.append(f"G1 Z{z:.3f} F{p.plunge:.1f}")
                for pt in path[1:]:
                    gx, gy = conv(pt)
                    out.append(f"G1 X{gx:.3f} Y{gy:.3f} F{p.feed:.1f}")
                out.append(f"G0 Z{p.retract:.3f}")
                out.append(f"G0 X{sx:.3f} Y{sy:.3f}")
            out.append(f"G0 Z{p.safe_z:.3f}")

    # 2) pads — peck-drill the centre, or bore the hole if it is wider than the tool
    if pads:
        out.append("; --- pads / holes ---")
    for sh in pads:
        cx, cy, rad = sh.circle()
        gx, gy = conv((cx, cy))
        out.append(f"G0 X{gx:.3f} Y{gy:.3f}")
        out.append(f"G0 Z{p.retract:.3f}")
        if rad <= p.tool_dia / 2 + 0.05:  # plunge a point hole, peck
            z = 0.0
            while z > -p.drill_depth + 1e-9:
                z = max(z - p.peck, -p.drill_depth)
                out.append(f"G1 Z{z:.3f} F{p.plunge:.1f}")
                out.append(f"G0 Z{p.retract:.3f}")
        else:  # bore: clear the whole disc (plunge centre, spiral out to the wall)
            br = rad - p.tool_dia / 2  # tool-centre radius at the wall
            step = max(0.2, p.tool_dia * p.stepover)
            for z in _depth_passes(p.drill_depth, p.stepdown):
                out.append(f"G0 X{gx:.3f} Y{gy:.3f}")  # back to the centre
                out.append(f"G1 Z{z:.3f} F{p.plunge:.1f}")  # plunge at the centre
                rr = step
                while rr <= br + 1e-9:  # widening rings clear the interior
                    for pt in _circle_pts(cx, cy, rr):
                        rx, ry = conv(pt)
                        out.append(f"G1 X{rx:.3f} Y{ry:.3f} F{p.feed:.1f}")
                    rr += step
                for pt in _circle_pts(cx, cy, br):  # final clean pass at the wall
                    rx, ry = conv(pt)
                    out.append(f"G1 X{rx:.3f} Y{ry:.3f} F{p.feed:.1f}")
        out.append(f"G0 Z{p.safe_z:.3f}")

    # 3) board outline — cut on the OUTSIDE (offset by the tool radius), full depth
    if boards:
        out.append("; --- board cutout ---")
    r = p.tool_dia / 2
    for sh in boards:
        x, y, w, h = sh.rect_bounds()
        loop = [(x - r, y - r), (x + w + r, y - r), (x + w + r, y + h + r),
                (x - r, y + h + r), (x - r, y - r)]
        sx, sy = conv(loop[0])
        out.append(f"G0 X{sx:.3f} Y{sy:.3f}")
        out.append(f"G0 Z{p.retract:.3f}")
        for z in _depth_passes(p.board_depth, p.stepdown):
            out.append(f"G1 Z{z:.3f} F{p.plunge:.1f}")
            for pt in loop[1:]:
                gx, gy = conv(pt)
                out.append(f"G1 X{gx:.3f} Y{gy:.3f} F{p.feed:.1f}")
        out.append(f"G0 Z{p.safe_z:.3f}")

    out += ["M5", f"G0 Z{p.safe_z:.3f}", "G0 X0 Y0", "M2", ""]
    if not (traces or pads or boards):
        warnings.append("nothing drawn — pick Trace / Pad / Board and draw on the copper")
    return "\n".join(out), warnings


class PcbWidget(QWidget):
    """Draw copper traces/pads/outline; emit isolation G-code for grblHAL."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.on_preview: Callable[[str, str], None] | None = None
        self.on_to_program: Callable[[str, str], None] | None = None
        self._origin = [0.0, 0.0]
        self._cur_op = "trace"
        self._gcode = ""
        self._travel = [200.0, 150.0, 100.0]
        self._ef: dict[str, Any] = {}
        self._editor_inner: QWidget | None = None
        self._building = False
        self._numpad: Callable[[Any], None] | None = None
        self._build_ui()
        self.canvas.fit()

    def _state(self) -> dict[str, Any]:
        return {
            "size_x": self.b_x.value(), "size_y": self.b_y.value(), "size_z": self.b_z.value(),
            "origin_x": self._origin[0], "origin_y": self._origin[1],
            "op": self._cur_op, "sides": 6, "default_depth": self.copper.value(),
            "trace_width": self.chanw.value(), "tool_dia": self.tool.value(),
            "set_origin": self._set_origin,
        }

    def _set_origin(self, x: float, y: float) -> None:
        self._origin = [round(x, 3), round(y, 3)]
        self.origin_lbl.setText(f"origin: X{x:.1f} Y{y:.1f} (G-code 0,0)")
        self.canvas.update()

    def set_machine_travel(self, x: float, y: float, z: float) -> None:
        self._travel = [x or self._travel[0], y or self._travel[1], z or self._travel[2]]

    def enable_touch(self, hook: Callable[[Any], None]) -> None:
        """Attach an on-screen numpad to every spinbox (touchscreen use)."""
        self._numpad = hook
        for sp in self.findChildren(QAbstractSpinBox):
            hook(sp)

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        self.canvas = Canvas(self._state)
        self.canvas.mouseMoved.connect(
            lambda x, y: self.pos_lbl.setText(f"X{x:7.2f} Y{y:7.2f} mm"))

        self.canvas.selectionChanged.connect(self._rebuild_editor)
        side = QVBoxLayout()
        side.addWidget(self._tools_box())
        side.addWidget(self._editor_box())
        side.addWidget(self._board_box())
        side.addWidget(self._cut_box())
        side.addWidget(self._out_box(), 1)
        panel = QWidget(); panel.setLayout(side)
        scroll = QScrollArea(); scroll.setWidget(panel); scroll.setWidgetResizable(True)
        scroll.setFixedWidth(350)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.canvas, 1)
        root.addWidget(scroll)

    def _tools_box(self) -> QWidget:
        box = QGroupBox("Copper layer — draw"); v = QVBoxLayout(box)
        grp = QButtonGroup(self); row = QHBoxLayout()
        tools = (("select", "Select", None), ("trace", "Trace", "poly"),
                 ("pad", "Pad/hole", "circle"), ("board", "Board", "rect"),
                 ("origin", "Set 0", "origin"))
        for op, text, mode in tools:
            b = QPushButton(text); b.setCheckable(True)
            b.clicked.connect(lambda _c=False, o=op, m=mode: self._pick_tool(o, m))
            grp.addButton(b); row.addWidget(b)
            if op == "trace":
                b.setChecked(True)
        v.addLayout(row)
        hint = QLabel("Trace = isolation channel · Pad = drilled hole · Board = outer cutout")
        hint.setWordWrap(True); v.addWidget(hint)
        self.origin_lbl = QLabel("origin: X0.0 Y0.0"); v.addWidget(self.origin_lbl)
        ep = QPushButton("Add pads at trace ends"); ep.clicked.connect(self._add_end_pads)
        v.addWidget(ep)
        drow = QHBoxLayout()
        d = QPushButton("Delete selected"); d.clicked.connect(self._delete)
        c = QPushButton("Clear all"); c.clicked.connect(self._clear)
        drow.addWidget(d); drow.addWidget(c); v.addLayout(drow)
        self.pos_lbl = QLabel("X 0 Y 0 mm"); self.pos_lbl.setFont(QFont("monospace", 9))
        v.addWidget(self.pos_lbl)
        return box

    def _pick_tool(self, op: str, mode: str | None) -> None:
        if op != "select":
            self._cur_op = op if op != "origin" else self._cur_op
        self.canvas.set_mode(mode if mode else "select")

    # -- per-element numeric editor ------------------------------------------
    def _editor_box(self) -> QWidget:
        box = QGroupBox("Selected element (mm, from 0)")
        self.editor_layout = QVBoxLayout(box)
        self._rebuild_editor()
        return box

    def _rebuild_editor(self) -> None:
        self._building = True
        if self._editor_inner is not None:
            self._editor_inner.deleteLater()
        inner = QWidget(); form = QFormLayout(inner); self._ef = {}
        i = self.canvas.selected; shapes = self.canvas.shapes
        if i is None or i >= len(shapes):
            form.addRow(QLabel("Select an element (Select tool, click it)\nto type its exact size."))
        else:
            sh = shapes[i]; ox, oy = self._origin

            def fld(val: float, lo: float = -1e5, hi: float = 1e5, step: float = 1.0) -> QDoubleSpinBox:
                sp = QDoubleSpinBox(); sp.setRange(lo, hi); sp.setDecimals(2)
                sp.setSingleStep(step); sp.setValue(val)
                sp.valueChanged.connect(self._apply_edits)
                if self._numpad:
                    self._numpad(sp)
                return sp

            if sh.op == "trace":
                form.addRow(QLabel("<b>Trace / channel</b>"))
                self._ef["width"] = fld(sh.width, 0, 50, 0.1)
                form.addRow(f"Width (0=global {self.chanw.value():g})", self._ef["width"])
                self._ef["depth"] = fld(sh.depth, 0, 10, 0.05)
                form.addRow(f"Depth (0=global {self.copper.value():g})", self._ef["depth"])
                for k, (x, y) in enumerate(sh.pts):
                    self._ef[f"x{k}"] = fld(x - ox); form.addRow(f"P{k + 1} X", self._ef[f"x{k}"])
                    self._ef[f"y{k}"] = fld(y - oy); form.addRow(f"P{k + 1} Y", self._ef[f"y{k}"])
            elif sh.op == "pad":
                cx, cy, r = sh.circle()
                form.addRow(QLabel("<b>Pad / hole</b>"))
                self._ef["cx"] = fld(cx - ox); form.addRow("Center X", self._ef["cx"])
                self._ef["cy"] = fld(cy - oy); form.addRow("Center Y", self._ef["cy"])
                self._ef["dia"] = fld(2 * r, 0.1, 50, 0.1); form.addRow("Hole diameter", self._ef["dia"])
                self._ef["depth"] = fld(sh.depth, 0, 20, 0.1)
                form.addRow(f"Drill depth (0=global {self.drilld.value():g})", self._ef["depth"])
            elif sh.op == "board":
                x, y, w, h = sh.rect_bounds()
                form.addRow(QLabel("<b>Board outline</b>"))
                self._ef["x"] = fld(x - ox); form.addRow("X from 0", self._ef["x"])
                self._ef["y"] = fld(y - oy); form.addRow("Y from 0", self._ef["y"])
                self._ef["w"] = fld(w, 0.1); form.addRow("Width", self._ef["w"])
                self._ef["h"] = fld(h, 0.1); form.addRow("Height", self._ef["h"])
        self.editor_layout.addWidget(inner)
        self._editor_inner = inner
        self._building = False

    def _apply_edits(self) -> None:
        if self._building:
            return
        i = self.canvas.selected
        if i is None or not self._ef:
            return
        sh = self.canvas.shapes[i]; ox, oy = self._origin; ef = self._ef
        if "depth" in ef:
            sh.depth = ef["depth"].value()
        if sh.op == "trace":
            sh.width = ef["width"].value()
            pts, k = [], 0
            while f"x{k}" in ef:
                pts.append((ef[f"x{k}"].value() + ox, ef[f"y{k}"].value() + oy)); k += 1
            if len(pts) >= 2:
                sh.pts = pts
        elif sh.op == "pad":
            cx = ef["cx"].value() + ox; cy = ef["cy"].value() + oy; r = ef["dia"].value() / 2
            sh.pts = [(cx, cy), (cx + r, cy)]
        elif sh.op == "board":
            x = ef["x"].value() + ox; y = ef["y"].value() + oy
            sh.pts = [(x, y), (x + ef["w"].value(), y + ef["h"].value())]
        self.canvas.update()

    def _board_box(self) -> QWidget:
        box = QGroupBox("Board (mm)"); v = QHBoxLayout(box)
        self.b_x = self._spin(5, 1000, 50); self.b_y = self._spin(5, 1000, 40)
        self.b_z = self._spin(0.2, 10, 1.6, 0.1)
        for lbl, sp in (("X", self.b_x), ("Y", self.b_y), ("Thick", self.b_z)):
            v.addWidget(QLabel(lbl)); v.addWidget(sp)
        for sp in (self.b_x, self.b_y):
            sp.valueChanged.connect(lambda *_: self.canvas.fit())
        return box

    def _cut_box(self) -> QWidget:
        box = QGroupBox("Cutting"); v = QVBoxLayout(box)
        self.tool = self._spin(0.05, 6, 0.8, 0.05)
        self.copper = self._spin(0.02, 2, 0.15, 0.01)
        self.chanw = self._spin(0.0, 20, 0.0, 0.1)
        self.padd = self._spin(0.3, 20, 1.5, 0.1)
        self.drilld = self._spin(0.2, 10, 2.0, 0.1)
        self.feed = self._spin(10, 1000, 120, 10)
        self.plunge = self._spin(5, 500, 40, 5)
        for sp in (self.tool, self.chanw):
            sp.valueChanged.connect(self.canvas.update)  # live channel-width preview
        for lbl, sp in (("Tool dia", self.tool), ("Copper depth", self.copper),
                        ("Channel width (0=1 pass)", self.chanw), ("Pad diameter", self.padd),
                        ("Drill depth", self.drilld), ("Feed", self.feed),
                        ("Plunge feed", self.plunge)):
            r = QHBoxLayout(); r.addWidget(QLabel(lbl)); r.addStretch(1); r.addWidget(sp)
            v.addLayout(r)
        return box

    def _out_box(self) -> QWidget:
        box = QGroupBox("G-code"); v = QVBoxLayout(box)
        gen = QPushButton("Generate G-code"); gen.setObjectName("start")
        gen.clicked.connect(self._generate); v.addWidget(gen)
        self.fit_lbl = QLabel("fit: —"); self.fit_lbl.setWordWrap(True); v.addWidget(self.fit_lbl)
        self.text = QPlainTextEdit(); self.text.setReadOnly(True)
        self.text.setFont(QFont("monospace", 9)); v.addWidget(self.text, 1)
        row = QHBoxLayout()
        for text, fn in (("Preview 3D", self._preview), ("→ Program", self._to_program),
                         ("Save .nc", self._save)):
            b = QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        v.addLayout(row)
        drow = QHBoxLayout()
        for text, fn in (("Save design (depo)", self._save_design), ("Open design", self._open_design)):
            b = QPushButton(text); b.clicked.connect(fn); drow.addWidget(b)
        v.addLayout(drow)
        return box

    def _save_design(self) -> None:
        data = {
            "type": "pcb",
            "board": [self.b_x.value(), self.b_y.value(), self.b_z.value()],
            "origin": list(self._origin),
            "shapes": [asdict(s) for s in self.canvas.shapes],
        }
        name, _ = QFileDialog.getSaveFileName(
            self, "Save PCB design", str(depo_dir() / "pcb.json"), "Design (*.json)")
        if name:
            Path(name).write_text(json.dumps(data, indent=1), encoding="utf-8")

    def _open_design(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open PCB design", str(depo_dir()), "Design (*.json)")
        if not name:
            return
        data = json.loads(Path(name).read_text(encoding="utf-8"))
        bx, by, bz = data.get("board", [50, 40, 1.6])
        self.b_x.setValue(bx); self.b_y.setValue(by); self.b_z.setValue(bz)
        self._origin = list(data.get("origin", [0.0, 0.0]))
        self.canvas.shapes = [
            Shape(**{k: ([tuple(p) for p in v] if k == "pts" else v) for k, v in d.items()})
            for d in data.get("shapes", [])
        ]
        self.canvas.selected = None
        self.canvas.fit()

    @staticmethod
    def _spin(lo: float, hi: float, val: float, step: float = 1.0) -> QDoubleSpinBox:
        s = QDoubleSpinBox(); s.setRange(lo, hi); s.setValue(val); s.setSingleStep(step)
        s.setDecimals(2 if step < 1 else 1); s.setMaximumWidth(90)
        return s

    def _params(self) -> PcbParams:
        p = PcbParams()
        p.origin_x, p.origin_y = self._origin
        p.tool_dia = self.tool.value(); p.copper_depth = self.copper.value()
        p.channel_width = self.chanw.value()
        p.drill_depth = self.drilld.value(); p.board_depth = self.b_z.value()
        p.feed = self.feed.value(); p.plunge = self.plunge.value()
        return p

    def _add_end_pads(self) -> None:
        """Drop a pad of the default diameter at every trace endpoint (deduped)."""
        r = self.padd.value() / 2.0
        existing = [s.circle()[:2] for s in self.canvas.shapes if s.op == "pad"]
        added = 0
        for sh in [s for s in self.canvas.shapes if s.op == "trace" and len(s.pts) >= 2]:
            for (px, py) in (sh.pts[0], sh.pts[-1]):
                if any(math.hypot(px - ex, py - ey) < r for ex, ey in existing):
                    continue
                self.canvas.shapes.append(Shape("circle", [(px, py), (px + r, py)], "pad"))
                existing.append((px, py)); added += 1
        self.canvas.update()
        self.fit_lbl.setText(f"added {added} end-pad(s)"); self.fit_lbl.setStyleSheet("color:#22c55e;")

    def _delete(self) -> None:
        i = self.canvas.selected
        if i is not None:
            self.canvas.shapes.pop(i); self.canvas.selected = None; self.canvas.update()

    def _clear(self) -> None:
        self.canvas.shapes.clear(); self.canvas.selected = None; self.canvas.update()

    def _generate(self) -> str:
        g, warns = generate_pcb_gcode(self.canvas.shapes, self._params())
        self._gcode = g
        bx, by = self.b_x.value(), self.b_y.value()
        errs = []
        if self._travel[0] and bx > self._travel[0]:
            errs.append(f"Board X {bx:g} > machine X travel {self._travel[0]:g} mm")
        if self._travel[1] and by > self._travel[1]:
            errs.append(f"Board Y {by:g} > machine Y travel {self._travel[1]:g} mm")
        self._errors = errs
        banner = "".join(f"; !! {e}\n" for e in errs) + "".join(f"; ! {w}\n" for w in warns)
        self.text.setPlainText(banner + g)
        if errs:
            self.fit_lbl.setText("✗ " + "; ".join(errs)); self.fit_lbl.setStyleSheet("color:#ef4444;")
        else:
            self.fit_lbl.setText("✓ board fits the machine"); self.fit_lbl.setStyleSheet("color:#22c55e;")
        return g

    def _blocked(self) -> bool:
        self._generate()
        if getattr(self, "_errors", []):
            QMessageBox.critical(self, "Does not fit", "\n".join(self._errors))
            return True
        return False

    def _preview(self) -> None:
        if not self._blocked() and self.on_preview and self._gcode.strip():
            self.on_preview(self._gcode, "PCB")

    def _to_program(self) -> None:
        if not self._blocked() and self.on_to_program and self._gcode.strip():
            self.on_to_program(self._gcode, "PCB")

    def _save(self) -> None:
        g = self._gcode or self._generate()
        name, _ = QFileDialog.getSaveFileName(
            self, "Save G-code", str(depo_dir() / "pcb.nc"), "G-code (*.nc)")
        if name:
            Path(name).write_text(g, encoding="utf-8")


__all__ = ["PcbWidget", "PcbParams", "generate_pcb_gcode"]
