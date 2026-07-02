"""Interactive 3D workpiece view — a GPU-accelerated Qt component.

This is the embeddable widget the main GUI mounts as a tab (and it also runs
standalone). It renders the *carved* workpiece — not just the toolpath — as a
``pyqtgraph.opengl`` surface, so orbit/zoom is GPU-smooth (matplotlib's mplot3d
re-projects every face in Python on each mouse move and can't be made dynamic).

Responsiveness comes from two things:

* **The carve runs off the UI thread.** Editing a field schedules a debounced
  job on a ``QThreadPool``; only the latest result is drawn (a generation
  counter discards stale ones). The window never freezes mid-carve.
* **Orbit costs nothing.** Rotating just moves the GPU camera; the heightmap is
  uploaded once per carve, not recomputed per frame. View-only controls
  (Z-exaggeration, show-path) redraw from the cached result without re-carving.

Requires the GUI extra (``uv sync --extra gui``): PySide6 + pyqtgraph + PyOpenGL.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root -> cncctl
sys.path.insert(0, str(Path(__file__).resolve().parent))  # examples dir -> workpiece_core

import numpy as np
import pyqtgraph as pg
import pyqtgraph.opengl as gl
from PySide6.QtCore import QEvent, QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QSurfaceFormat, QVector3D
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from cncctl.viz.simulate import Trace
from cncctl.viz.workpiece import CarveResult, HeightMapCarver, Stock
from touch_input import attach_numpad_spin  # sibling module in examples/
from workpiece_core import DEMO_GCODE, PRESETS, simulate_workpiece

_DEBOUNCE_MS = 120

# Diffuse lighting for the carved surface: a unit light from the upper-front-left,
# plus an ambient floor so shadowed walls never go fully black. Baked into the
# per-vertex colours so depth (colormap) and relief (shading) both show at once.
_LIGHT = np.array([0.45, -0.5, 0.74], dtype=np.float64)
_LIGHT /= np.linalg.norm(_LIGHT)
_AMBIENT = 0.38


class _TouchGLView(gl.GLViewWidget):
    """GLViewWidget with two-finger pinch-to-zoom for touchscreens.

    pyqtgraph only zooms on the mouse wheel, which a touch panel has no way to
    send. We grab the standard pinch gesture and map its scale factor onto the
    camera distance. Single-finger orbit and right-drag pan keep working through
    Qt's touch->mouse synthesis (we never accept the raw touch events), so the
    "flawless otherwise" behaviour is untouched.
    """

    def __init__(self) -> None:
        super().__init__()
        # Receiving touch events is what feeds Qt's pinch recogniser.
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.grabGesture(Qt.GestureType.PinchGesture)

    def event(self, ev: QEvent) -> bool:
        if ev.type() == QEvent.Type.Gesture:
            pinch = ev.gesture(Qt.GestureType.PinchGesture)  # type: ignore[attr-defined]
            if pinch is not None:
                factor = pinch.scaleFactor()
                if factor > 0:
                    # Fingers apart (factor > 1) -> zoom in -> smaller distance.
                    distance = self.opts["distance"] / factor
                    self.setCameraPosition(distance=max(1.0, min(distance, 100_000.0)))
                ev.accept()
                return True
        return super().event(ev)


class _CarveSignals(QObject):
    """Carry a worker result back to the UI thread."""

    # (generation, payload) where payload is (trace, result, report) or an Exception.
    done = Signal(int, object)


class _CarveJob(QRunnable):
    """Run one parse->simulate->carve on a pool thread."""

    def __init__(self, generation: int, params: dict[str, object], signals: _CarveSignals) -> None:
        super().__init__()
        self._generation = generation
        self._params = params
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            payload: object = simulate_workpiece(**self._params)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001  (any pipeline error is reported to the UI)
            payload = exc
        self._signals.done.emit(self._generation, payload)


class WorkpieceWidget(QWidget):
    """3D carved-workpiece view with live, off-thread re-simulation."""

    def __init__(
        self, gcode: str | None = None, source: str | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._signals = _CarveSignals()
        self._signals.done.connect(self._on_carved)
        self._generation = 0
        self._gcode = gcode or DEMO_GCODE
        self._source = source or "(built-in demo)"
        self._last: tuple[Trace, CarveResult] | None = None
        self._view_framed = False
        self._axis_items: list[object] = []  # GL line + text labels of the ruler/axes

        # Playback state (the line-by-line animation).
        self._carver: HeightMapCarver | None = None
        self._anim_active = False  # True while showing a partial (animated) carve
        self._playing = False
        self._anim_seg = 0  # index of the last toolpath vertex reached
        self._anim_pos = (0.0, 0.0, 0.0)  # last carved tool position
        self._anim_time = 0.0  # simulated machining time reached (s)
        self._marker_pos = (0.0, 0.0, 0.0)
        self._gcode_lines = self._gcode.splitlines()

        # When the main GUI hits "Send", it pushes the program here and asks the
        # view to auto-run the cut animation (play_program). The carve mesh is
        # built off-thread, so we remember the request and start playing the
        # moment it is ready.
        self._autoplay_pending = False

        self._build_ui()
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._launch)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(40)  # ~25 fps playback
        self._anim_timer.timeout.connect(self._on_frame)
        self._schedule()  # initial carve of the demo / loaded file

    # -- construction --------------------------------------------------------
    def _build_ui(self) -> None:
        root = QHBoxLayout(self)

        # 4x MSAA for clean surface/edge anti-aliasing (set before the GL context).
        fmt = QSurfaceFormat.defaultFormat()
        if fmt.samples() < 4:
            fmt.setSamples(4)
            QSurfaceFormat.setDefaultFormat(fmt)

        self.view = _TouchGLView()
        self.view.setBackgroundColor(pg.mkColor(26, 29, 34))
        self.view.setCameraPosition(distance=160, elevation=28, azimuth=-62)
        self._grid = gl.GLGridItem()
        self._grid.setColor(pg.mkColor(70, 74, 82))
        self.view.addItem(self._grid)
        self._surface = gl.GLSurfacePlotItem(smooth=False, drawEdges=False)
        self.view.addItem(self._surface)
        self._box = gl.GLBoxItem()
        self._box.setColor(pg.mkColor(150, 155, 165, 160))
        self.view.addItem(self._box)
        self._path = gl.GLLinePlotItem(antialias=True, mode="line_strip")
        self.view.addItem(self._path)
        self._marker = gl.GLScatterPlotItem(
            pos=np.zeros((1, 3), dtype=np.float32), size=14.0, color=(1.0, 0.55, 0.1, 1.0)
        )
        self._marker.setVisible(False)
        self.view.addItem(self._marker)
        # A modest minimum so the 3D view can shrink instead of forcing the whole
        # window taller than the screen (the controls panel scrolls — see below).
        self.view.setMinimumSize(320, 240)
        root.addWidget(self.view, 1)

        panel = QVBoxLayout()

        setup = QGroupBox("Setup")
        form = QFormLayout(setup)
        self.file_label = QLabel(self._source)
        self.file_label.setWordWrap(True)
        load = QPushButton("Load G-code...")
        load.clicked.connect(self._choose_file)
        form.addRow(load)
        form.addRow("File", self.file_label)

        self.size_x = self._spin(1.0, 2000.0, 80.0, " mm")
        self.size_y = self._spin(1.0, 2000.0, 50.0, " mm")
        self.size_z = self._spin(0.1, 1000.0, 10.0, " mm")
        self.origin = QComboBox()
        self.origin.addItems([label for label, _ in PRESETS])
        self.tool = self._spin(0.1, 100.0, 6.0, " mm")
        self.ball = QCheckBox("Ball-nose tool")
        self.resolution = QSpinBox()
        self.resolution.setRange(16, 600)
        self.resolution.setValue(300)
        attach_numpad_spin(self.resolution)  # tap the field -> on-screen numpad
        form.addRow("Stock X", self.size_x)
        form.addRow("Stock Y", self.size_y)
        form.addRow("Stock Z", self.size_z)
        form.addRow("Origin", self.origin)
        form.addRow("Tool dia.", self.tool)
        form.addRow("", self.ball)
        form.addRow("Resolution", self.resolution)
        # Preview-only: sink surface-level (Z0) cuts so engrave files show grooves.
        self.engrave = self._spin(0.0, 50.0, 0.0, " mm", step=0.05)
        form.addRow("Engrave depth", self.engrave)
        # Mirror for back-side CAM output (e.g. FlatCAM bottom-copper layers come
        # mirrored for flipped-board milling; toggle to preview un-mirrored).
        self.mirror_x = QCheckBox("Mirror X")
        self.mirror_y = QCheckBox("Mirror Y")
        mirror_row = QHBoxLayout()
        mirror_row.addWidget(self.mirror_x)
        mirror_row.addWidget(self.mirror_y)
        mirror_row.addStretch(1)
        mirror_wrap = QWidget()
        mirror_wrap.setLayout(mirror_row)
        form.addRow("Mirror", mirror_wrap)
        # Fit the stock to wherever the toolpath actually lives (size + position),
        # so a program at arbitrary coordinates lands on the block instead of
        # floating off the fixed-origin stock. Overrides Stock X/Y + Origin.
        self.fit = QCheckBox("Fit stock to toolpath (overrides X/Y + origin)")
        form.addRow("", self.fit)
        panel.addWidget(setup)

        view_box = QGroupBox("View")
        view_form = QFormLayout(view_box)
        self.zexag = self._spin(1.0, 20.0, 3.0, "x", step=0.5)
        self.show_path = QCheckBox("Show toolpath")
        self.show_path.setChecked(True)
        self.show_axes = QCheckBox("Show axes & ruler")
        self.show_axes.setChecked(True)
        fit = QPushButton("Fit view")
        fit.clicked.connect(self._fit_view)
        view_form.addRow("Z exaggeration", self.zexag)
        view_form.addRow("", self.show_path)
        view_form.addRow("", self.show_axes)
        view_form.addRow(fit)
        panel.addWidget(view_box)

        # Line-by-line playback of the toolpath with live material removal.
        play_box = QGroupBox("Playback")
        play_v = QVBoxLayout(play_box)
        btn_row = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self._toggle_play)
        step_btn = QPushButton("Step")
        step_btn.clicked.connect(self._step)
        restart_btn = QPushButton("Restart")
        restart_btn.clicked.connect(self._restart)
        btn_row.addWidget(self.play_btn)
        btn_row.addWidget(step_btn)
        btn_row.addWidget(restart_btn)
        play_v.addLayout(btn_row)
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed (sim s/s)"))
        self.speed = self._spin(0.5, 2000.0, 10.0, "x", step=1.0)
        speed_row.addWidget(self.speed)
        speed_row.addStretch(1)
        play_v.addLayout(speed_row)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(1)
        self.slider.sliderPressed.connect(self._pause)
        self.slider.sliderReleased.connect(self._seek)
        play_v.addWidget(self.slider)
        self.playback_readout = QLabel("")
        self.playback_readout.setFont(QFont("Consolas", 9))
        play_v.addWidget(self.playback_readout)
        panel.addWidget(play_box)

        self.status = QLabel("ready")
        self.status.setStyleSheet("color: #9aa;")
        panel.addWidget(self.status)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        mono = self.report.font()
        mono.setFamily("Consolas")
        self.report.setFont(mono)
        panel.addWidget(self.report, 1)

        holder = QWidget()
        holder.setLayout(panel)
        # Scroll the controls instead of forcing the tab (and the whole window)
        # taller than the screen — otherwise entering this tab clips the others.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(holder)
        scroll.setMaximumWidth(440)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll)

        # Recompute on anything that changes the carve; redraw-only for view knobs.
        for spin in (self.size_x, self.size_y, self.size_z, self.tool):
            spin.valueChanged.connect(self._schedule)
        self.resolution.valueChanged.connect(self._schedule)
        self.engrave.valueChanged.connect(self._schedule)
        self.origin.currentIndexChanged.connect(self._schedule)
        self.ball.toggled.connect(self._schedule)
        self.mirror_x.toggled.connect(self._schedule)
        self.mirror_y.toggled.connect(self._schedule)
        self.fit.toggled.connect(self._on_fit_toggled)
        self.zexag.valueChanged.connect(self._render_only)
        self.show_path.toggled.connect(self._render_only)
        self.show_axes.toggled.connect(self._render_only)

    @staticmethod
    def _spin(
        low: float, high: float, value: float, suffix: str, *, step: float = 1.0
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setValue(value)
        spin.setSuffix(suffix)
        spin.setSingleStep(step)
        spin.setDecimals(2)
        spin.setKeyboardTracking(False)  # emit on commit, not on every digit
        attach_numpad_spin(spin)  # tap the field -> on-screen numpad (touchscreen)
        return spin

    # -- carve lifecycle -----------------------------------------------------
    def _schedule(self) -> None:
        self.status.setText("simulating...")
        self._debounce.start()

    def _on_fit_toggled(self) -> None:
        self._view_framed = False  # the stock just moved/resized — reframe the camera
        self._schedule()

    def _launch(self) -> None:
        params: dict[str, object] = {
            "gcode": self._gcode,
            "size_x": self.size_x.value(),
            "size_y": self.size_y.value(),
            "size_z": self.size_z.value(),
            "preset": PRESETS[self.origin.currentIndex()][1],
            "diameter": self.tool.value(),
            "ball": self.ball.isChecked(),
            "resolution": self.resolution.value(),
            "mirror_x": self.mirror_x.isChecked(),
            "mirror_y": self.mirror_y.isChecked(),
            "fit": self.fit.isChecked(),
            "engrave_depth": self.engrave.value(),
        }
        self._generation += 1
        self._pool.start(_CarveJob(self._generation, params, self._signals))

    @Slot(int, object)
    def _on_carved(self, generation: int, payload: object) -> None:
        if generation != self._generation:
            return  # a newer carve has superseded this one
        if isinstance(payload, Exception):
            self.status.setText(f"error: {type(payload).__name__}: {payload}")
            return
        trace, result, report = payload  # type: ignore[misc]
        self._last = (trace, result)
        self.report.setPlainText(report)
        self.status.setText("done")

        # Reset playback to a fresh, static (fully-carved) view.
        self._pause()
        self._anim_active = False
        self._carver = None
        self._anim_seg = 0
        n = len(trace.points)
        self.slider.blockSignals(True)
        self.slider.setMaximum(max(1, n - 1))
        self.slider.setValue(max(0, n - 1))
        self.slider.blockSignals(False)
        total = trace.duration_s()
        self.speed.blockSignals(True)
        self.speed.setValue(min(2000.0, max(1.0, total / 10.0)) if total > 0 else 10.0)
        self.speed.blockSignals(False)

        self._render()
        self._update_playback_readout()

        # A "Send" asked us to auto-run the cut; the mesh is ready now, so go.
        if self._autoplay_pending:
            self._autoplay_pending = False
            self._autostart_play()

    def _choose_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open G-code", "", "G-code (*.nc *.gcode *.ngc *.tap *.txt);;All files (*)"
        )
        if not name:
            return
        try:
            text = Path(name).read_text(encoding="utf-8")
        except OSError as exc:
            self.status.setText(f"read error: {exc}")
            return
        self.load_gcode(text, name)

    def load_gcode(self, text: str, source: str) -> None:
        """Load a G-code program (from a file chooser, or the live sender) and
        rebuild the carve. Public so the main GUI can push the program it is about
        to stream into this view."""
        self._gcode = text
        self._source = source
        self._gcode_lines = self._gcode.splitlines()
        self.file_label.setText(source)
        self._view_framed = False  # reframe the camera for the new part
        # Loaded files live at arbitrary coordinates; fit the stock so they land
        # on the block. (setChecked emits toggled -> _on_fit_toggled -> reschedule.)
        if not self.fit.isChecked():
            self.fit.setChecked(True)
        else:
            self._schedule()

    # -- playback (line-by-line animation) -----------------------------------
    def _toggle_play(self) -> None:
        if self._last is None:
            return
        if self._playing:
            self._pause()
            return
        if not self._anim_active or self._anim_seg >= len(self._last[0].points) - 1:
            self._start_animation()  # fresh run (or replay after finishing)
        self._playing = True
        self.play_btn.setText("Pause")
        self._anim_timer.start()

    def _pause(self) -> None:
        self._playing = False
        self.play_btn.setText("Play")
        self._anim_timer.stop()

    def _step(self) -> None:
        if self._last is None:
            return
        self._pause()
        if not self._anim_active or self._anim_seg >= len(self._last[0].points) - 1:
            self._start_animation()
        self._carve_to_segment(self._anim_seg + 1)
        self._render()
        self._update_playback_readout()
        self._update_slider()

    def _restart(self) -> None:
        if self._last is None:
            return
        self._pause()
        self._start_animation()
        self._render()
        self._update_playback_readout()
        self._update_slider()

    def _start_animation(self) -> None:
        assert self._last is not None
        trace, result = self._last
        self._carver = HeightMapCarver(
            result.stock, result.tool, resolution=self.resolution.value()
        )
        self._anim_active = True
        start = trace.points[0]
        self._anim_seg = 0
        self._anim_pos = (start.x, start.y, start.z)
        self._marker_pos = self._anim_pos
        self._anim_time = 0.0

    def _carve_to_segment(self, target: int) -> None:
        """Carve forward from the current vertex up to vertex ``target``."""
        assert self._last is not None and self._carver is not None
        points = self._last[0].points
        target = min(target, len(points) - 1)
        while self._anim_seg < target:
            nxt = points[self._anim_seg + 1]
            self._carver.carve_segment(self._anim_pos, (nxt.x, nxt.y, nxt.z))
            self._anim_pos = (nxt.x, nxt.y, nxt.z)
            self._anim_seg += 1
        self._marker_pos = self._anim_pos
        self._anim_time = points[self._anim_seg].t

    def _on_frame(self) -> None:
        if not self._playing or self._last is None or self._carver is None:
            return
        points = self._last[0].points
        last = len(points) - 1
        self._anim_time += self._anim_timer.interval() / 1000.0 * self.speed.value()

        # Complete every segment whose end time we have passed this frame.
        while self._anim_seg < last and points[self._anim_seg + 1].t <= self._anim_time:
            nxt = points[self._anim_seg + 1]
            self._carver.carve_segment(self._anim_pos, (nxt.x, nxt.y, nxt.z))
            self._anim_pos = (nxt.x, nxt.y, nxt.z)
            self._anim_seg += 1

        if self._anim_seg >= last:
            self._marker_pos = (points[last].x, points[last].y, points[last].z)
            self._pause()
        else:  # interpolate the tool within the current segment and carve up to it
            a, b = points[self._anim_seg], points[self._anim_seg + 1]
            span = b.t - a.t
            frac = min(1.0, max(0.0, (self._anim_time - a.t) / span)) if span > 1e-9 else 1.0
            marker = (a.x + (b.x - a.x) * frac, a.y + (b.y - a.y) * frac, a.z + (b.z - a.z) * frac)
            self._carver.carve_segment(self._anim_pos, marker)
            self._anim_pos = marker
            self._marker_pos = marker

        self._render()
        self._update_playback_readout()
        self._update_slider()

    def _seek(self) -> None:
        if self._last is None:
            return
        self._pause()
        if not self._anim_active:
            self._start_animation()
        assert self._carver is not None
        points = self._last[0].points
        target = min(self.slider.value(), len(points) - 1)
        if target < self._anim_seg:  # can't un-carve a heightmap: rebuild to here
            self._carver.reset()
            self._anim_seg = 0
            self._anim_pos = (points[0].x, points[0].y, points[0].z)
        self._carve_to_segment(target)
        self._render()
        self._update_playback_readout()

    def _update_slider(self) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(self._anim_seg)
        self.slider.blockSignals(False)

    # -- auto-run on Send (driven by the main GUI) ---------------------------
    def play_program(self, text: str, source: str) -> None:
        """Load ``text`` and start animating the cut automatically.

        Called when the operator hits "Send" in the main GUI: the simulation
        should just start running. The carve mesh is built off the UI thread, so
        we set a pending flag and :meth:`_on_carved` kicks off playback the moment
        it is ready (a second or two for a big file). Everything afterwards is the
        ordinary playback animation, so Play/Pause/Step/Seek keep working.
        """
        self._pause()
        self._autoplay_pending = True
        # load_gcode always (re)builds the carve off-thread; _on_carved starts the
        # animation when it lands. Going through that one path avoids a race where
        # an immediate start is then reset by the rebuild finishing.
        self.load_gcode(text, source)

    def _autostart_play(self) -> None:
        """Begin the cut animation from the start (programmatic Play)."""
        if self._last is None:
            return
        # Start at 1x so the simulation runs in real machining time — it tracks
        # the actual cut rather than racing ahead. The operator can still speed it
        # up with the Speed control afterwards.
        self.speed.blockSignals(True)
        self.speed.setValue(1.0)
        self.speed.blockSignals(False)
        self._start_animation()
        self._playing = True
        self.play_btn.setText("Pause")
        self._anim_timer.start()
        self._render()
        self._update_playback_readout()
        self._update_slider()

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = max(0.0, seconds)
        return f"{int(seconds) // 60:d}:{int(seconds) % 60:02d}"

    def _update_playback_readout(self) -> None:
        if self._last is None:
            return
        trace, result = self._last
        points = trace.points
        total = trace.duration_s()
        last = max(1, len(points) - 1)
        if self._anim_active and self._carver is not None:
            seg = self._anim_seg
            elapsed = min(self._anim_time, total)
            removed, depth = self._carver.metrics()
            marker = self._marker_pos
        else:
            seg = len(points) - 1
            elapsed = total
            removed, depth = result.removed_volume_mm3, result.max_depth_mm
            marker = (points[-1].x, points[-1].y, points[-1].z)

        line_idx = points[seg].line
        line_text = ""
        if 0 <= line_idx < len(self._gcode_lines):
            line_text = self._gcode_lines[line_idx].strip()[:38]
        mx, my, mz = marker
        self.playback_readout.setText(
            f"Line {line_idx + 1 if line_idx >= 0 else '-'}: {line_text}\n"
            f"Move {seg}/{last}    {seg / last * 100:4.0f}%\n"
            f"Time {self._fmt_time(elapsed)} / {self._fmt_time(total)}\n"
            f"Pos  X{mx:7.2f} Y{my:7.2f} Z{mz:7.2f}\n"
            f"Removed {removed:7.1f} mm^3   depth {depth:5.2f} mm"
        )

    # -- rendering -----------------------------------------------------------
    def _render_only(self) -> None:
        if self._last is not None:
            self._render()

    def _render(self) -> None:
        assert self._last is not None
        trace, result = self._last
        ze = self.zexag.value()
        stock = result.stock

        # During playback the surface follows the partially-carved heightmap.
        animating = self._anim_active and self._carver is not None
        heights = self._carver.heights if animating else result.heights

        z_disp = (heights.T * ze).astype(np.float32)  # (nx, ny) for the surface item
        dx = float(result.xs[1] - result.xs[0]) if result.xs.shape[0] > 1 else 1.0
        dy = float(result.ys[1] - result.ys[0]) if result.ys.shape[0] > 1 else 1.0
        colors = self._height_colors(heights.T, stock.top, stock.bottom)
        colors[..., :3] *= self._shade(z_disp, dx, dy)[..., np.newaxis]  # diffuse relief
        # GLSurfacePlotItem flattens its vertexes to (nx*ny, 3) but not the colors,
        # so per-vertex colours must be handed over pre-flattened to match the face
        # indices (otherwise it indexes past the grid's first axis and crashes).
        self._surface.setData(x=result.xs, y=result.ys, z=z_disp, colors=colors.reshape(-1, 4))

        # Toolpath: the full polyline statically, or revealed up to the tool live.
        if animating:
            revealed = [(p.x, p.y, p.z * ze) for p in trace.points[: self._anim_seg + 1]]
            mx, my, mz = self._marker_pos
            revealed.append((mx, my, mz * ze))
            path = np.array(revealed, dtype=np.float32)
            self._marker.setData(pos=np.array([[mx, my, mz * ze]], dtype=np.float32))
            self._marker.setVisible(True)
        else:
            path = np.array([(p.x, p.y, p.z * ze) for p in trace.points], dtype=np.float32)
            self._marker.setVisible(False)
        self._path.setData(pos=path, color=(0.95, 0.3, 0.3, 0.85), width=1.5)
        self._path.setVisible(self.show_path.isChecked())

        self._box.setSize(stock.size_x, stock.size_y, stock.size_z * ze)
        self._box.resetTransform()
        self._box.translate(stock.x0, stock.y0, stock.bottom * ze)

        self._grid.resetTransform()
        self._grid.setSize(stock.size_x * 1.5, stock.size_y * 1.5, 1.0)
        self._grid.setSpacing(10.0, 10.0, 10.0)
        self._grid.translate(
            stock.x0 + stock.size_x / 2, stock.y0 + stock.size_y / 2, stock.bottom * ze
        )

        self._build_axes(stock, ze)

        if not self._view_framed:
            self._fit_view()
            self._view_framed = True

    # -- geometry overlay (axes, ruler ticks, labels) ------------------------
    def _build_axes(self, stock: Stock, ze: float) -> None:
        """Draw labelled X/Y/Z axes with mm tick numbers along the stock edges.

        Rebuilt each render because tick positions follow the stock size and the
        Z exaggeration. Cheap (a few dozen GL items). Cleared when the toggle is
        off.
        """
        for item in self._axis_items:
            self.view.removeItem(item)
        self._axis_items = []
        if not self.show_axes.isChecked():
            return

        x0, y0 = stock.x0, stock.y0
        x1 = x0 + stock.size_x
        y1 = y0 + stock.size_y
        zb, zt = stock.bottom, stock.top
        span = max(stock.size_x, stock.size_y)
        tick = span * 0.022  # tick-mark / label offset length
        font = QFont("Helvetica", 9)
        ink = (210, 214, 222)

        segments: list[tuple[float, float, float]] = [
            (x0, y0, zb * ze), (x1, y0, zb * ze),  # X baseline
            (x0, y0, zb * ze), (x0, y1, zb * ze),  # Y baseline
            (x0, y0, zb * ze), (x0, y0, zt * ze),  # Z baseline
        ]
        for xv in self._ticks(x0, x1, self._nice_step(stock.size_x)):
            segments += [(xv, y0, zb * ze), (xv, y0 - tick, zb * ze)]
            self._add_label((xv, y0 - tick * 2.4, zb * ze), f"{xv:g}", ink, font)
        for yv in self._ticks(y0, y1, self._nice_step(stock.size_y)):
            segments += [(x0, yv, zb * ze), (x0 - tick, yv, zb * ze)]
            self._add_label((x0 - tick * 2.4, yv, zb * ze), f"{yv:g}", ink, font)
        for zv in self._ticks(zb, zt, self._nice_step(stock.size_z)):
            segments += [(x0, y0, zv * ze), (x0 - tick, y0, zv * ze)]
            self._add_label((x0 - tick * 2.6, y0, zv * ze), f"{zv:g}", ink, font)

        self._add_label(((x0 + x1) / 2, y0 - tick * 5.0, zb * ze), "X (mm)", ink, font)
        self._add_label((x0 - tick * 5.5, (y0 + y1) / 2, zb * ze), "Y (mm)", ink, font)
        self._add_label((x0 - tick * 2.6, y0, zt * ze + tick * 1.5), "Z (mm)", ink, font)

        line = gl.GLLinePlotItem(
            pos=np.array(segments, dtype=np.float32),
            mode="lines",
            color=(0.82, 0.84, 0.88, 0.9),
            width=1.0,
            antialias=True,
        )
        self.view.addItem(line)
        self._axis_items.append(line)

    def _add_label(
        self, pos: tuple[float, float, float], text: str, color: tuple[int, int, int], font: QFont
    ) -> None:
        item = gl.GLTextItem(pos=np.array(pos, dtype=float), text=text, color=color, font=font)
        self.view.addItem(item)
        self._axis_items.append(item)

    @staticmethod
    def _nice_step(span: float, target: int = 6) -> float:
        """A round tick interval (1/2/2.5/5 x 10^n) giving roughly ``target`` ticks."""
        if span <= 0:
            return 1.0
        raw = span / target
        magnitude = 10.0 ** math.floor(math.log10(raw))
        for multiple in (1.0, 2.0, 2.5, 5.0, 10.0):
            if raw <= multiple * magnitude:
                return multiple * magnitude
        return 10.0 * magnitude

    @staticmethod
    def _ticks(lo: float, hi: float, step: float) -> list[float]:
        first = math.ceil(lo / step - 1e-9)
        last = math.floor(hi / step + 1e-9)
        return [k * step for k in range(first, last + 1)]

    def _fit_view(self) -> None:
        if self._last is None:
            return
        stock = self._last[1].stock
        ze = self.zexag.value()
        centre = QVector3D(
            stock.x0 + stock.size_x / 2,
            stock.y0 + stock.size_y / 2,
            (stock.bottom + stock.size_z / 2) * ze,
        )
        self.view.setCameraPosition(
            pos=centre, distance=1.7 * max(stock.size_x, stock.size_y), elevation=28, azimuth=-62
        )

    @staticmethod
    def _height_colors(z2d: np.ndarray, top: float, bottom: float) -> np.ndarray:
        rng = max(top - bottom, 1e-6)
        norm = np.clip((z2d - bottom) / rng, 0.0, 1.0)
        cmap = pg.colormap.get("viridis")
        rgba = cmap.map(norm.ravel(), mode="float")
        return rgba.reshape((*z2d.shape, 4)).astype(np.float32)

    @staticmethod
    def _shade(z_disp: np.ndarray, dx: float, dy: float) -> np.ndarray:
        """Per-vertex diffuse lighting from the heightmap's surface normals.

        Computes the surface gradient, builds the unit normal ``(-dz/dx, -dz/dy,
        1)`` and returns Lambertian shade in ``[ambient, 1]``. Cut walls (steep
        gradient) turn away from the light and darken, giving the part real
        relief instead of a flat colour wash.
        """
        gx = np.gradient(z_disp, dx, axis=0)
        gy = np.gradient(z_disp, dy, axis=1)
        inv_len = 1.0 / np.sqrt(gx * gx + gy * gy + 1.0)
        diffuse = (-gx * _LIGHT[0] - gy * _LIGHT[1] + _LIGHT[2]) * inv_len
        shade = _AMBIENT + (1.0 - _AMBIENT) * np.clip(diffuse, 0.0, 1.0)
        return shade.astype(np.float32)


def main(argv: list[str] | None = None) -> int:
    """Run the widget as a standalone window."""
    from PySide6.QtWidgets import QApplication

    args = list(sys.argv if argv is None else argv)
    gcode: str | None = None
    source: str | None = None
    if len(args) > 1:
        path = Path(args[1])
        gcode = path.read_text(encoding="utf-8")
        source = str(path)

    app = QApplication.instance() or QApplication(args)
    window = WorkpieceWidget(gcode=gcode, source=source)
    window.setWindowTitle("cncctl - 3D workpiece simulator")
    window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    window.resize(1140, 740)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
