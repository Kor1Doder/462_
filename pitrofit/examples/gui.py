"""cncctl desktop GUI (PySide6 / Qt) — an all-in-one operator panel.

Built entirely on the public Facade API. Layout (modelled on
``reference/gu_cnc_all_in_one.py`` but on our async core, with a real 3D view):

* a persistent **top bar** — connection + always-available emergency controls
  (Feed Hold / Resume / Unlock / Soft Reset), reachable from any tab;
* a persistent **dashboard** — live state, MPos, WPos, feed/spindle/overrides,
  and decoded input switches;
* tabs in workflow order: Control (jog + G54 work-zero), Program (send +
  pre-flight), Workpiece 3D, Switch Doctor, Settings (live grblHAL), Machine
  config (machine.toml + calibrate), Terminal.

Numeric fields are touch-driven: tapping one opens an on-screen numpad
(``touch_input.py``), since the appliance has a touch screen and no keyboard.

Qt is an optional extra — install it with::

    uv sync --extra gui

Run (tick "Use simulator" to try with no hardware)::

    uv run python examples/gui.py
    uv run python examples/gui.py --config config/machine.toml

SAFETY: the hardwired emergency stop is your real e-stop; the on-screen
buttons are conveniences over USB and depend on the PC/link being alive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cncctl.calibration.steps_per_mm import corrected_steps_per_mm, setting_key_for_axis
from cncctl.config_io import (
    AxesConfig,
    AxisConfig,
    Config,
    HomingConfig,
    MachineConfig,
    MotionConfig,
    TransportConfig,
    load_config,
    require_commissioned,
    save_config,
)
from cncctl.controller.errors import ConfigError, ConnectionLostError, NotConnectedError
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
from cncctl.facade import Facade, MachineProfile
from cncctl.transport.serial_transport import SerialTransport
from cncctl.viz.analyze import SoftLimits
from cncctl.viz.simulate import Kinematics
from touch_input import (  # sibling module in examples/
    TapFeedback,
    attach_keyboard,
    attach_numpad,
)

_FULL_STEPS_PER_REV = 200.0  # 1.8-degree motors (57BHH100)
_AXES = ("x", "y", "z")
_AXIS_FIELDS = (
    ("microsteps", "Microsteps"),
    ("lead_screw_mm", "Lead screw (mm/rev)"),
    ("steps_per_mm", "Steps/mm  ($100-102)"),
    ("max_rate_mm_min", "Max rate (mm/min, $110-112)"),
    ("acceleration_mm_s2", "Accel (mm/s^2, $120-122)"),
    ("soft_limit_mm", "Soft limit travel (mm, $130-132)"),
)

# Live grblHAL settings exposed on the Settings tab (number -> label).
_LIVE_SETTINGS: tuple[tuple[int, str], ...] = (
    (100, "X steps/mm"), (101, "Y steps/mm"), (102, "Z steps/mm"),
    (110, "X max rate"), (111, "Y max rate"), (112, "Z max rate"),
    (120, "X accel"), (121, "Y accel"), (122, "Z accel"),
    (130, "X travel"), (131, "Y travel"), (132, "Z travel"),
    (3, "Dir invert"), (23, "Homing dir"), (5, "Limit invert"),
    (20, "Soft limits"), (21, "Hard limits"), (22, "Homing enable"),
    (24, "Home feed"), (25, "Home seek"), (26, "Debounce"), (27, "Pull-off"),
)
# Conservative first-commissioning preset (limits off, homing on, NC switches).
_SAFE_HOMING = {20: "0", 21: "0", 22: "1", 5: "1", 24: "50.0", 25: "300.0", 26: "250", 27: "2.0"}

_SIGNAL_KEYS = ("X lim", "Y lim", "Z lim", "Probe", "Door", "E-Stop")

# Root-owned helper that switches from the kiosk to the desktop (installed by
# deploy/install.sh). Absent when running the GUI by hand outside the appliance.
_DESKTOP_HANDOFF = "/usr/local/sbin/cncctl-to-desktop"

# USB vendor id of the Raspberry Pi Pico (RP2040) running grblHAL.
_PICO_VID = 0x2E8A
# Stable symlink the udev rule (deploy/) gives the Pico; preferred when present.
_GRBLHAL_SYMLINK = Path("/dev/grblhal")


def discover_ports() -> list[str]:
    """Likely grblHAL serial ports, best candidate first.

    Prefers the stable ``/dev/grblhal`` udev symlink, then any USB-CDC device
    whose USB vendor id is the Pico's (0x2e8a). Returns an empty list if none
    are present (the UI then keeps searching).
    """
    from serial.tools import list_ports

    found: list[str] = []
    if _GRBLHAL_SYMLINK.exists():
        found.append(str(_GRBLHAL_SYMLINK))
    for info in list_ports.comports():
        if getattr(info, "vid", None) == _PICO_VID and info.device not in found:
            found.append(info.device)
    return found


class Bridge(QObject):
    """Runs an asyncio loop in a thread; marshals results to the Qt main thread."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.invoke.connect(self._run)

    @Slot(object)
    def _run(self, fn: Any) -> None:
        fn()

    def post(self, fn: Any) -> None:
        """Run ``fn`` on the Qt main thread (safe to call from any thread)."""
        self.invoke.emit(fn)

    def submit(self, coro: Any, *, on_ok: Any = None, on_err: Any = None) -> None:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def done(fut: Any) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # surface failures without crashing the UI
                if on_err is not None:
                    self.post(lambda e=exc: on_err(e))
                elif isinstance(exc, (ConnectionLostError, NotConnectedError)):
                    # Connection drops are handled by the poll loop (state +
                    # auto-reconnect); never raise a modal error screen for them.
                    pass
                else:
                    self.post(lambda e=exc: QMessageBox.critical(None, "Error", f"{type(e).__name__}: {e}"))
                return
            if on_ok is not None:
                self.post(lambda r=result: on_ok(r))

        future.add_done_callback(done)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)


def _button(text: str, slot: Any, *, kind: str = "") -> QPushButton:
    """A push button, optionally styled 'danger' (red) or 'start' (green)."""
    btn = QPushButton(text)
    btn.clicked.connect(slot)
    if kind:
        btn.setObjectName(kind)
    return btn


class FlowLayout(QLayout):
    """A layout that lays widgets out left-to-right and wraps to a new row when
    it runs out of width — so a crowded button bar reflows onto extra rows on a
    small screen instead of overflowing or forcing the buttons smaller.

    Adapted from Qt's documented FlowLayout example.
    """

    def __init__(self, margin: int = 0, spacing: int = 6) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt override)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        line_height = 0
        space = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width()
            if next_x > right and line_height > 0:  # wrap to the next row
                x = rect.x() + margins.left()
                y = y + line_height + space
                next_x = x + hint.width()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x + space
            line_height = max(line_height, hint.height())
        return y + line_height + margins.bottom() - rect.y()


class MainWindow(QWidget):
    def __init__(self, config_path: Path, *, kiosk: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("cncctl - EMCO retrofit")
        self.config_path = config_path
        self.kiosk = kiosk
        self.bridge = Bridge()
        self.fields: dict[str, QLineEdit] = {}
        self.setting_fields: dict[int, QLineEdit] = {}
        self.dash_signals: dict[str, QLabel] = {}
        self.doctor_signals: dict[str, QLabel] = {}
        self.expected: dict[str, QCheckBox] = {}
        self.facade: Facade | None = None
        self.controller: RealController | None = None
        self.connected = False
        # Auto-connect state: we scan for a Pico and connect on our own.
        self._connecting = False
        self._current_port: str | None = None
        self._retry_after = 0.0  # monotonic time before which not to re-attempt

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        root.addWidget(self._topbar())
        root.addWidget(self._dashboard())

        tabs = QTabWidget()
        tabs.addTab(self._control_tab(), "Control")
        tabs.addTab(self._program_tab(), "Program")
        tabs.addTab(self._workpiece_tab(), "Workpiece 3D")
        tabs.addTab(self._switch_tab(), "Switch Doctor")
        tabs.addTab(self._settings_tab(), "Settings")
        tabs.addTab(self._config_tab(), "Machine config")
        tabs.addTab(self._terminal_tab(), "Terminal")
        self.tabs = tabs
        root.addWidget(tabs, 1)

        self._enable_touch_numpads()
        self._load_config()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_status)
        self.timer.start(150)
        # Auto-connect: scan for a controller and connect without manual input.
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._auto_scan)
        self.scan_timer.start(1500)
        self._auto_scan()

    def _mono(self, delta: int = 0, *, bold: bool = False) -> QFont:
        """A monospace font sized relative to the app's responsive base point
        size (set from the screen resolution), so readouts scale with the UI."""
        weight = QFont.Weight.Bold if bold else QFont.Weight.Normal
        return QFont("Consolas", max(8, self.font().pointSize() + delta), weight)

    def _enable_touch_numpads(self) -> None:
        """Tap-to-keypad on every field (the touchscreen has no keyboard):
        numeric fields open a numpad, text fields a QWERTY keyboard."""
        # Jog / Z-zero / calibration inputs — all non-negative magnitudes.
        for edit in (self.step_edit, self.feed_edit, self.paper_edit,
                     self.cal_cmd, self.cal_meas):
            attach_numpad(edit, allow_negative=False, allow_decimal=True)
        # Machine config fields (text ones get the QWERTY keyboard instead).
        text_keys = {"machine.name", "transport.default_port"}
        int_keys = {"transport.rx_buffer_bytes"}
        for key, edit in self.fields.items():
            if key in text_keys:
                continue
            decimal = not (key.endswith(".microsteps") or key in int_keys)
            attach_numpad(edit, allow_negative=False, allow_decimal=decimal)
        # Live grblHAL settings: raw $N values — allow sign and decimals.
        for edit in self.setting_fields.values():
            attach_numpad(edit, allow_negative=True, allow_decimal=True)
        # Text fields → on-screen QWERTY keyboard.
        for edit in (self.fields["machine.name"], self.fields["transport.default_port"],
                     self.mdi_edit, self.file_edit):
            attach_keyboard(edit)

    def _quit_to_desktop(self) -> None:
        """Close the panel and switch to the Raspberry Pi desktop (for debugging).

        Under the kiosk this hands off to a root-owned helper (installed by
        deploy/install.sh, run via a scoped NOPASSWD sudoers rule) that isolates
        graphical.target — stopping the kiosk service and starting the desktop.
        When the helper is absent (e.g. running the GUI by hand from a desktop),
        it just closes the window.
        """
        reply = QMessageBox.question(
            self,
            "Quit to desktop",
            "Close the CNC panel and switch to the Raspberry Pi desktop?\n\n"
            "This does NOT stop the machine — the hardwired e-stop is your real stop.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        # Mark this as a user-demanded quit so the kiosk relaunch loop stays down
        # for the session (a crash or frozen-then-killed GUI leaves no sentinel
        # and relaunches). The service clears it on its next start / on reboot.
        try:
            (Path.home() / ".cncctl-stop").touch()
        except OSError as exc:
            self.log(f"could not write stop sentinel: {exc}")
        if os.path.exists(_DESKTOP_HANDOFF):
            try:
                # Detached: the helper survives this process being torn down by
                # the target switch (it uses systemd-run internally).
                subprocess.Popen(["sudo", "-n", _DESKTOP_HANDOFF])  # noqa: S603, S607
            except OSError as exc:
                self.log(f"desktop handoff failed: {exc}")
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # ===================================================================== bars
    def _topbar(self) -> QWidget:
        box = QGroupBox("Connection / Emergency")
        # A wrapping layout so the buttons reflow onto a second row on a narrow
        # screen instead of overflowing — they keep their normal size.
        flow = FlowLayout(margin=6, spacing=6)
        box.setLayout(flow)
        policy = box.sizePolicy()
        policy.setHeightForWidth(True)
        box.setSizePolicy(policy)

        # Connection is automatic: we scan for the Pico and connect on our own.
        # The pill reports what the link is doing; Rescan forces an attempt now.
        self.conn_label = QLabel("Searching…")
        self.conn_label.setMinimumWidth(220)
        self._set_conn_status("searching", None)
        flow.addWidget(self.conn_label)
        flow.addWidget(_button("Rescan", self._rescan))

        self.sim_check = QCheckBox("Simulator")
        self.sim_check.setChecked(False)
        self.sim_check.toggled.connect(self._on_sim_toggled)
        flow.addWidget(self.sim_check)

        flow.addWidget(
            _button("Feed Hold !", lambda: self._guarded(lambda f: f.hold()), kind="danger")
        )
        flow.addWidget(_button("Resume ~", lambda: self._guarded(lambda f: f.resume())))
        flow.addWidget(_button("Unlock $X", lambda: self._guarded(lambda f: f.unlock())))
        flow.addWidget(
            _button("Soft Reset", lambda: self._guarded(lambda f: f.reset()), kind="danger")
        )
        flow.addWidget(_button("Quit → Desktop", self._quit_to_desktop))
        return box

    def _dashboard(self) -> QWidget:
        box = QGroupBox("Status")
        grid = QGridLayout(box)
        self.state_label = QLabel("Disconnected")
        self.state_label.setFont(self._mono(4, bold=True))
        self.state_label.setStyleSheet("color:#facc15;")
        grid.addWidget(self.state_label, 0, 0, 2, 1)

        self.mpos_label = QLabel("MPos: -")
        self.wpos_label = QLabel("WPos: -")
        self.fs_label = QLabel("Feed/Spindle: -    Ov: -")
        for lbl in (self.mpos_label, self.wpos_label, self.fs_label):
            lbl.setFont(self._mono())
        grid.addWidget(self.mpos_label, 0, 1)
        grid.addWidget(self.wpos_label, 0, 2)
        grid.addWidget(self.fs_label, 1, 1, 1, 2)

        inputs = QHBoxLayout()
        inputs.addWidget(QLabel("Inputs:"))
        for key in _SIGNAL_KEYS:
            lbl = QLabel(key)
            lbl.setFont(self._mono(-1))
            lbl.setStyleSheet("color:#64748b;")
            self.dash_signals[key] = lbl
            inputs.addWidget(lbl)
        inputs.addStretch(1)
        holder = QWidget()
        holder.setLayout(inputs)
        grid.addWidget(holder, 2, 0, 1, 3)
        grid.setColumnStretch(2, 1)
        return box

    # ===================================================================== tabs
    def _control_tab(self) -> QWidget:
        tab = QWidget()
        outer = QHBoxLayout(tab)

        jog = QGroupBox("Jog  (feed capped to each axis max rate)")
        jcol = QVBoxLayout(jog)

        # Inputs on their own row so the fields don't distort the button columns.
        inputs = QHBoxLayout()
        inputs.addWidget(QLabel("Step mm"))
        self.step_edit = QLineEdit("1.0")
        self.step_edit.setMaximumWidth(90)
        inputs.addWidget(self.step_edit)
        inputs.addSpacing(20)
        inputs.addWidget(QLabel("Feed mm/min"))
        self.feed_edit = QLineEdit("500")
        self.feed_edit.setMaximumWidth(90)
        inputs.addWidget(self.feed_edit)
        inputs.addStretch(1)
        jcol.addLayout(inputs)

        # Step presets: four equal-width buttons.
        presets = QHBoxLayout()
        for step in ("0.1", "1", "5", "10"):
            presets.addWidget(
                _button(f"{step} mm", lambda _c=False, s=step: self.step_edit.setText(s)), 1
            )
        jcol.addLayout(presets)

        # Jog pad: two equal columns so each -/+ pair matches.
        pad = QGridLayout()
        pad.setColumnStretch(0, 1)
        pad.setColumnStretch(1, 1)
        for label, axis, sign, r, c in (
            ("X-", Axis.X, -1.0, 0, 0), ("X+", Axis.X, 1.0, 0, 1),
            ("Y-", Axis.Y, -1.0, 1, 0), ("Y+", Axis.Y, 1.0, 1, 1),
            ("Z- down", Axis.Z, -1.0, 2, 0), ("Z+ up", Axis.Z, 1.0, 2, 1),
        ):
            pad.addWidget(_button(label, lambda _c=False, a=axis, s=sign: self._jog(a, s)), r, c)
        jcol.addLayout(pad)

        # Actions: three equal-width buttons.
        actions = QHBoxLayout()
        actions.addWidget(_button("Home $H", lambda: self._guarded(lambda f: f.home()), kind="danger"), 1)
        actions.addWidget(_button("Unlock $X", lambda: self._guarded(lambda f: f.unlock())), 1)
        actions.addWidget(_button("Status ?", self._status_once), 1)
        jcol.addLayout(actions)
        jcol.addStretch(1)
        outer.addWidget(jog, 1)

        zero = QGroupBox("Probeless G54 work-zero")
        zcol = QVBoxLayout(zero)
        hint = QLabel(
            "Jog the tool to the part origin, then zero it. The current position "
            "becomes 0 in G54 (the part zero). Homing/switches set the machine zero."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#facc15;")
        zcol.addWidget(hint)

        # Per-axis zero buttons: two equal columns.
        zgrid = QGridLayout()
        zgrid.setColumnStretch(0, 1)
        zgrid.setColumnStretch(1, 1)
        zgrid.addWidget(_button("Zero X", lambda: self._zero_axes([Axis.X])), 0, 0)
        zgrid.addWidget(_button("Zero Y", lambda: self._zero_axes([Axis.Y])), 0, 1)
        zgrid.addWidget(_button("Zero Z", lambda: self._zero_axes([Axis.Z])), 1, 0)
        zgrid.addWidget(_button("Zero XY", lambda: self._zero_axes([Axis.X, Axis.Y])), 1, 1)
        zgrid.addWidget(_button("Zero XYZ", lambda: self._zero_axes([Axis.X, Axis.Y, Axis.Z])), 2, 0, 1, 2)
        zcol.addLayout(zgrid)

        # Paper-gauge offset on its own row so the field doesn't widen a column.
        prow = QHBoxLayout()
        prow.addWidget(QLabel("Paper offset mm"))
        self.paper_edit = QLineEdit("0.00")
        self.paper_edit.setMaximumWidth(90)
        prow.addWidget(self.paper_edit)
        prow.addStretch(1)
        zcol.addLayout(prow)

        zcol.addWidget(_button("Zero Z (paper) + lift Z5", self._zero_z_paper))
        zcol.addStretch(1)
        outer.addWidget(zero, 1)
        return tab

    def _program_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        row.addWidget(self.file_edit, 1)
        row.addWidget(_button("Browse", self._browse))
        row.addWidget(_button("Pre-flight", self._preflight))
        row.addWidget(_button("Send", self._send_file, kind="start"))
        row.addWidget(_button("Cancel", lambda: self._guarded(lambda f: f.cancel()), kind="danger"))
        outer.addLayout(row)
        self.progress = QProgressBar()
        outer.addWidget(self.progress)
        self.analysis = QPlainTextEdit()
        self.analysis.setReadOnly(True)
        self.analysis.setFont(self._mono())
        outer.addWidget(self.analysis, 1)
        return tab

    def _workpiece_tab(self) -> QWidget:
        """The GPU-accelerated carved-workpiece view (offline; no machine needed).

        Lazily imported so a missing 3D extra degrades to an install hint instead
        of breaking the whole window.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from workpiece_view import WorkpieceWidget
        except ImportError as exc:
            tab = QWidget()
            layout = QVBoxLayout(tab)
            hint = QLabel(
                "The 3D workpiece view needs the GUI extra (pyqtgraph + PyOpenGL):\n\n"
                "    uv sync --extra gui\n\n"
                f"({exc})"
            )
            hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(hint)
            layout.addStretch(1)
            return tab
        return WorkpieceWidget()

    def _switch_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)

        live = QGroupBox("Live switch feedback")
        lrow = QHBoxLayout(live)
        for key in _SIGNAL_KEYS:
            lbl = QLabel(f"{key}\noff")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFont(self._mono(2, bold=True))
            lbl.setStyleSheet("color:#22c55e;")
            self.doctor_signals[key] = lbl
            lrow.addWidget(lbl)
        lrow.addWidget(_button("$5=1 (NC)", lambda: self._quick_set(5, "1")))
        lrow.addWidget(_button("$5=0 (NO)", lambda: self._quick_set(5, "0")))
        lrow.addWidget(_button("Status ?", self._status_once))
        outer.addWidget(live)

        body = QHBoxLayout()
        diag = QGroupBox("Diagnosis: which switches do you expect pressed?")
        dcol = QVBoxLayout(diag)
        erow = QHBoxLayout()
        for axis in ("X", "Y", "Z"):
            cb = QCheckBox(f"{axis} pressed")
            cb.toggled.connect(lambda *_: self._refresh_diagnosis())
            self.expected[axis] = cb
            erow.addWidget(cb)
        erow.addStretch(1)
        dcol.addLayout(erow)
        self.doctor_diagnosis = QPlainTextEdit()
        self.doctor_diagnosis.setReadOnly(True)
        self.doctor_diagnosis.setFont(self._mono())
        dcol.addWidget(self.doctor_diagnosis, 1)
        body.addWidget(diag, 1)
        outer.addLayout(body, 1)
        return tab

    def _settings_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        row = QHBoxLayout()
        row.addWidget(_button("Read $$", self._read_live_settings))
        row.addWidget(_button("Write changed", self._write_live_settings, kind="start"))
        row.addWidget(_button("Backup to file", self._backup_settings))
        row.addWidget(_button("Apply safe homing", self._apply_safe_homing))
        row.addStretch(1)
        outer.addLayout(row)

        form = QGroupBox("Live grblHAL settings")
        fgrid = QGridLayout(form)
        for idx, (num, label) in enumerate(_LIVE_SETTINGS):
            r, c = idx // 2, (idx % 2) * 2
            fgrid.addWidget(QLabel(f"${num} {label}"), r, c)
            edit = QLineEdit()
            self.setting_fields[num] = edit
            fgrid.addWidget(edit, r, c + 1)
        outer.addWidget(form, 1)
        return tab

    def _config_tab(self) -> QWidget:
        outer_tab = QScrollArea()
        outer_tab.setWidgetResizable(True)
        tab = QWidget()
        outer_tab.setWidget(tab)
        outer = QVBoxLayout(tab)

        meta = QGroupBox("Machine / transport")
        row = QHBoxLayout(meta)
        for key, label, width in (
            ("machine.name", "Name", 160),
            ("transport.default_port", "Port", 140),
            ("transport.rx_buffer_bytes", "RX buffer", 60),
        ):
            row.addWidget(QLabel(label))
            edit = QLineEdit()
            edit.setFixedWidth(width)
            self.fields[key] = edit
            row.addWidget(edit)
        row.addStretch(1)
        outer.addWidget(meta)

        axes = QGroupBox("Axes (machine.toml)")
        grid = QGridLayout(axes)
        for col, axis in enumerate(_AXES, start=1):
            header = QLabel(axis.upper())
            header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.setFont(self._mono(0, bold=True))
            grid.addWidget(header, 0, col)
        for r, (field, label) in enumerate(_AXIS_FIELDS, start=1):
            grid.addWidget(QLabel(label), r, 0)
            for col, axis in enumerate(_AXES, start=1):
                edit = QLineEdit()
                edit.setFixedWidth(90)
                self.fields[f"axes.{axis}.{field}"] = edit
                grid.addWidget(edit, r, col)
        compute_row = len(_AXIS_FIELDS) + 1
        grid.addWidget(QLabel("auto steps/mm:"), compute_row, 0)
        for col, axis in enumerate(_AXES, start=1):
            grid.addWidget(_button(f"Compute {axis.upper()}", lambda _c=False, a=axis: self._compute_steps(a)), compute_row, col)
        outer.addWidget(axes)

        motion = QGroupBox("Motion / homing")
        mrow = QHBoxLayout(motion)
        mrow.addWidget(QLabel("Junction deviation (mm, $11)"))
        jd = QLineEdit()
        jd.setFixedWidth(90)
        self.fields["motion.junction_deviation_mm"] = jd
        mrow.addWidget(jd)
        self.homing_check = QCheckBox("Homing enabled")
        mrow.addWidget(self.homing_check)
        mrow.addStretch(1)
        outer.addWidget(motion)

        buttons = QHBoxLayout()
        buttons.addWidget(_button("Reload", self._load_config))
        buttons.addWidget(_button("Save", self._save_config, kind="start"))
        buttons.addStretch(1)
        outer.addLayout(buttons)
        self.cfg_status = QLabel("")
        self.cfg_status.setWordWrap(True)
        outer.addWidget(self.cfg_status)

        cal = QGroupBox("Calibrate steps/mm  (measured vs commanded)")
        cgrid = QGridLayout(cal)
        cgrid.addWidget(QLabel("Axis"), 0, 0)
        self.cal_axis = QComboBox()
        self.cal_axis.addItems(["X", "Y", "Z"])
        cgrid.addWidget(self.cal_axis, 0, 1)
        cgrid.addWidget(QLabel("Commanded mm"), 0, 2)
        self.cal_cmd = QLineEdit("100.0")
        cgrid.addWidget(self.cal_cmd, 0, 3)
        cgrid.addWidget(QLabel("Measured mm"), 0, 4)
        self.cal_meas = QLineEdit("")
        cgrid.addWidget(self.cal_meas, 0, 5)
        cgrid.addWidget(_button("Compute & apply", self._calibrate), 0, 6)
        outer.addWidget(cal)
        outer.addStretch(1)
        return outer_tab

    def _terminal_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(self._mono())
        outer.addWidget(self.console, 1)
        row = QHBoxLayout()
        self.mdi_edit = QLineEdit()
        self.mdi_edit.setPlaceholderText("MDI: a G-code or $ command, then Enter")
        self.mdi_edit.returnPressed.connect(self._send_mdi)
        row.addWidget(self.mdi_edit, 1)
        row.addWidget(_button("Send", self._send_mdi))
        row.addWidget(_button("$$", self._read_live_settings))
        row.addWidget(_button("Clear", lambda: self.console.clear()))
        outer.addLayout(row)
        return tab

    # =============================================================== connection
    def _auto_scan(self) -> None:
        """Periodic: if nothing is connected, find a controller and connect.

        Simulator mode connects to the in-memory sim; otherwise we look for a
        Pico (``discover_ports``) and connect to the first one found. Failures
        are non-fatal — we back off briefly and the next scan retries.
        """
        if self._connecting or self.connected:
            return
        if time.monotonic() < self._retry_after:
            return
        if self.sim_check.isChecked():
            self._begin_connect("sim")
            return
        ports = discover_ports()
        if ports:
            self._begin_connect(ports[0])
        else:
            self._set_conn_status("searching", None)

    def _rescan(self) -> None:
        """Force an immediate (re)connect attempt."""
        self._retry_after = 0.0
        if not self.connected and not self._connecting:
            self._auto_scan()

    def _on_sim_toggled(self, _checked: bool) -> None:
        # Drop any current link; the scanner reconnects to the right target.
        self._teardown_link()
        self._retry_after = 0.0
        self._auto_scan()

    def _begin_connect(self, target: str) -> None:
        self._connecting = True
        self._current_port = target
        self._set_conn_status("connecting", target)
        if target == "sim":
            from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

            transport: Any = SimulatedTransport(GrblHalSimulator(), status_interval=0.1)
        else:
            transport = SerialTransport()
        self.controller = RealController(transport, status_rate_hz=10)
        self.facade = Facade(self.controller, profile=self._build_profile())
        self.bridge.submit(
            self.facade.connect(target),
            on_ok=lambda _r: self._on_connected(target),
            on_err=lambda exc: self._on_connect_failed(target, exc),
        )

    def _on_connected(self, target: str) -> None:
        self.connected = True
        self._connecting = False
        self._set_conn_status("connected", target)
        self.log(f"connected to {target}")

    def _on_connect_failed(self, target: str, exc: Exception) -> None:
        self._connecting = False
        self.connected = False
        self.controller = None
        self.facade = None
        self._retry_after = time.monotonic() + 4.0  # back off before retrying
        self._set_conn_status("searching", None)
        self.log(f"connect to {target} failed ({type(exc).__name__}); retrying")

    def _handle_link_lost(self) -> None:
        """The controller dropped the link (missed status / closed port)."""
        port = self._current_port
        self.connected = False
        self._connecting = False
        self.controller = None
        self.facade = None
        self._retry_after = time.monotonic() + 1.0
        self._set_conn_status("lost", port)
        self.log(f"connection to {port} lost — searching again")

    def _teardown_link(self) -> None:
        """Drop the current connection (best-effort) without surfacing errors."""
        facade = self.facade
        self.connected = False
        self._connecting = False
        self.controller = None
        self.facade = None
        self._current_port = None
        if facade is not None:
            self.bridge.submit(facade.disconnect())

    _CONN_STYLES = {
        "searching": ("● Searching for controller…", "#facc15"),
        "connecting": ("● Connecting {port}…", "#60a5fa"),
        "connected": ("● Connected {port}", "#22c55e"),
        "lost": ("● Link lost — searching…", "#ef4444"),
    }

    def _set_conn_status(self, phase: str, port: str | None) -> None:
        template, color = self._CONN_STYLES.get(phase, self._CONN_STYLES["searching"])
        shown = "simulator" if port == "sim" else (port or "")
        self.conn_label.setText(template.format(port=shown).replace("  ", " ").strip())
        self.conn_label.setStyleSheet(f"color:{color}; font-weight:bold;")

    def _build_profile(self) -> MachineProfile:
        try:
            cfg = self._config_from_fields()
            require_commissioned(cfg)
            return MachineProfile.from_config(cfg)
        except (ValueError, ConfigError):
            self.log("config not commissioned - using a generous soft-limit envelope")
            return MachineProfile(
                soft_limits=SoftLimits((-10000.0, 10000.0), (-10000.0, 10000.0), (-10000.0, 10000.0)),
                kinematics=Kinematics(max_rate_mm_min=1000.0),
            )

    # ================================================================== control
    def _jog(self, axis: Axis, sign: float) -> None:
        if not self._require_connected():
            return
        try:
            step = float(self.step_edit.text())
            feed = float(self.feed_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Jog", "Step and feed must be numeric.")
            return
        self._do(self._facade().jog(axis, sign * step, self._cap_feed(axis, feed)))

    def _cap_feed(self, axis: Axis, feed: float) -> float:
        try:
            axis_max = float(self.fields[f"axes.{axis.value.lower()}.max_rate_mm_min"].text())
        except ValueError:
            return feed
        if axis_max > 0 and feed > axis_max:
            self.log(f"feed {feed:g} capped to axis {axis.value} max rate {axis_max:g}")
            return axis_max
        return feed

    def _zero_axes(self, axes: list[Axis]) -> None:
        if not self._require_connected():
            return
        names = "".join(a.value for a in axes)
        if QMessageBox.question(self, "Work zero", f"Set current position as G54 {names}=0?") != QMessageBox.StandardButton.Yes:
            return
        self._do(self._facade().set_work_zero(axes))
        self.log(f"set G54 work zero: {names}")

    def _zero_z_paper(self) -> None:
        if not self._require_connected():
            return
        try:
            paper = float(self.paper_edit.text())
            feed = float(self.feed_edit.text())
        except ValueError:
            QMessageBox.warning(self, "Zero Z", "Paper offset and feed must be numeric.")
            return
        if QMessageBox.question(self, "Zero Z", f"Set current Z as G54 Z{paper:.3f}, then lift Z+5?") != QMessageBox.StandardButton.Yes:
            return
        self._do(self._zero_z_paper_coro(paper, self._cap_feed(Axis.Z, feed)))

    async def _zero_z_paper_coro(self, paper: float, feed: float) -> None:
        await self._facade().set_work_zero([Axis.Z], values={Axis.Z: paper})
        await self._facade().jog(Axis.Z, 5.0, feed)

    def _status_once(self) -> None:
        if not self._require_connected():
            return
        self._do(self._facade().run_line("?"))

    def _quick_set(self, num: int, value: str) -> None:
        if not self._require_connected():
            return
        self.bridge.submit(
            self._facade().write_setting(num, value),
            on_ok=lambda _r: self.log(f"  ${num}={value} (verified)"),
            on_err=lambda exc: self.log(f"  {type(exc).__name__}: {exc}"),
        )

    # ================================================================== program
    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open G-code", "", "G-code (*.nc *.gcode *.ngc *.txt);;All files (*)"
        )
        if path:
            self.file_edit.setText(path)

    def _preflight(self) -> None:
        if not self._require_connected():
            return
        self.bridge.submit(
            self._facade().analyze_file(Path(self.file_edit.text())), on_ok=self._show_analysis
        )

    def _show_analysis(self, result: Any) -> None:
        lo, hi = result.bounding_box
        lines = [
            f"bbox  X {lo.x:.1f}..{hi.x:.1f}   Y {lo.y:.1f}..{hi.y:.1f}   Z {lo.z:.1f}..{hi.z:.1f}",
            f"travel {result.total_travel_mm:.1f} mm    est {result.duration_s:.1f} s",
            f"in soft limits: {result.in_bounds}",
        ]
        lines += [f"  ! {v}" for v in result.violations]
        self.analysis.setPlainText("\n".join(lines))

    def _send_file(self) -> None:
        if not self._require_connected():
            return
        self.bridge.submit(
            self._stream_file(Path(self.file_edit.text())),
            on_ok=lambda _r: self.log("send complete"),
            on_err=lambda exc: self.log(f"send failed: {type(exc).__name__}: {exc}"),
        )

    async def _stream_file(self, path: Path) -> None:
        async for progress in self._facade().send_program(path):
            total = progress.total or 0
            sent = progress.sent
            self.bridge.post(lambda s=sent, t=total: self._show_progress(s, t))

    def _show_progress(self, sent: int, total: int) -> None:
        if total:
            self.progress.setMaximum(total)
            self.progress.setValue(sent)
        self.progress.setFormat(f"sent {sent}/{total or '?'} lines")

    # ============================================================ live settings
    def _read_live_settings(self) -> None:
        if not self._require_connected():
            return
        self.bridge.submit(self._facade().read_settings(), on_ok=self._apply_live_settings)

    def _apply_live_settings(self, settings: Any) -> None:
        for num, edit in self.setting_fields.items():
            value = settings.get(num)
            if value is not None:
                edit.setText(value)
        self.log("read $$ settings")

    def _write_live_settings(self) -> None:
        if not self._require_connected():
            return
        edits = {num: e.text().strip() for num, e in self.setting_fields.items() if e.text().strip()}
        if QMessageBox.question(self, "Write settings", f"Write {len(edits)} settings to the controller?") != QMessageBox.StandardButton.Yes:
            return
        self._do(self._write_settings_coro(edits))

    async def _write_settings_coro(self, edits: dict[int, str]) -> None:
        for num, value in edits.items():
            await self._facade().write_setting(num, value)
            self.bridge.post(lambda n=num, v=value: self.log(f"  ${n}={v} (verified)"))
        self.bridge.post(lambda: self.log("settings written"))

    def _backup_settings(self) -> None:
        if not self._require_connected():
            return
        self.bridge.submit(self._facade().read_settings(), on_ok=self._write_backup)

    def _write_backup(self, settings: Any) -> None:
        outdir = Path("cnc_backups")
        outdir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = outdir / f"grblhal_settings_{stamp}.json"
        payload = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "settings": {str(k): v for k, v in settings.values.items()},
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.log(f"backed up {len(settings.values)} settings to {path}")

    def _apply_safe_homing(self) -> None:
        if not self._require_connected():
            return
        preset = ", ".join(f"${k}={v}" for k, v in _SAFE_HOMING.items())
        if QMessageBox.question(self, "Safe homing", f"Write the first-commissioning preset?\n\n{preset}") != QMessageBox.StandardButton.Yes:
            return
        self._do(self._write_settings_coro(dict(_SAFE_HOMING)))

    # ============================================================ machine config
    def _compute_steps(self, axis: str) -> None:
        try:
            microsteps = float(self.fields[f"axes.{axis}.microsteps"].text())
            lead = float(self.fields[f"axes.{axis}.lead_screw_mm"].text())
        except ValueError:
            QMessageBox.warning(self, "Compute", "Enter numeric microsteps and lead screw first.")
            return
        if lead <= 0:
            QMessageBox.warning(self, "Compute", "Lead screw must be > 0.")
            return
        self.fields[f"axes.{axis}.steps_per_mm"].setText(f"{_FULL_STEPS_PER_REV * microsteps / lead:.3f}")

    def _load_config(self) -> None:
        try:
            cfg = load_config(self.config_path)
        except ConfigError as exc:
            self.cfg_status.setText(f"load failed: {exc}")
            return
        self.fields["machine.name"].setText(cfg.machine.name)
        self.fields["transport.default_port"].setText(cfg.transport.default_port)
        self.fields["transport.rx_buffer_bytes"].setText(str(cfg.transport.rx_buffer_bytes))
        for axis in _AXES:
            axis_cfg = getattr(cfg.axes, axis)
            for field, _ in _AXIS_FIELDS:
                self.fields[f"axes.{axis}.{field}"].setText(str(getattr(axis_cfg, field)))
        self.fields["motion.junction_deviation_mm"].setText(str(cfg.motion.junction_deviation_mm))
        self.homing_check.setChecked(cfg.homing.enabled)
        self._set_commissioned(cfg)

    def _config_from_fields(self) -> Config:
        def axis_cfg(axis: str) -> AxisConfig:
            f = self.fields
            return AxisConfig(
                microsteps=int(float(f[f"axes.{axis}.microsteps"].text())),
                lead_screw_mm=float(f[f"axes.{axis}.lead_screw_mm"].text()),
                steps_per_mm=float(f[f"axes.{axis}.steps_per_mm"].text()),
                max_rate_mm_min=float(f[f"axes.{axis}.max_rate_mm_min"].text()),
                acceleration_mm_s2=float(f[f"axes.{axis}.acceleration_mm_s2"].text()),
                soft_limit_mm=float(f[f"axes.{axis}.soft_limit_mm"].text()),
            )

        return Config(
            machine=MachineConfig(name=self.fields["machine.name"].text()),
            transport=TransportConfig(
                default_port=self.fields["transport.default_port"].text(),
                rx_buffer_bytes=int(float(self.fields["transport.rx_buffer_bytes"].text())),
            ),
            axes=AxesConfig(x=axis_cfg("x"), y=axis_cfg("y"), z=axis_cfg("z")),
            motion=MotionConfig(
                junction_deviation_mm=float(self.fields["motion.junction_deviation_mm"].text())
            ),
            homing=HomingConfig(enabled=self.homing_check.isChecked()),
        )

    def _save_config(self) -> None:
        try:
            cfg = self._config_from_fields()
        except ValueError as exc:
            QMessageBox.critical(self, "Save", f"Invalid value: {exc}")
            return
        save_config(cfg, self.config_path)
        self._set_commissioned(cfg)
        QMessageBox.information(self, "Save", f"Wrote {self.config_path}")

    def _set_commissioned(self, cfg: Config) -> None:
        try:
            require_commissioned(cfg)
        except ConfigError:
            self.cfg_status.setText(
                "NOT commissioned - fill in steps/mm, max rate, accel and soft limit "
                "(non-zero) for every axis before bootstrapping the machine."
            )
            self.cfg_status.setStyleSheet("color: #f87171;")
        else:
            self.cfg_status.setText("Commissioned OK.")
            self.cfg_status.setStyleSheet("color: #34d399;")

    def _calibrate(self) -> None:
        if not self._require_connected():
            return
        try:
            commanded = float(self.cal_cmd.text())
            measured = float(self.cal_meas.text())
        except ValueError:
            QMessageBox.warning(self, "Calibrate", "Commanded and measured must be numeric.")
            return
        key = setting_key_for_axis(Axis[self.cal_axis.currentText()])
        self.bridge.submit(
            self._facade().read_settings(),
            on_ok=lambda s: self._apply_calibration(key, s, commanded, measured),
        )

    def _apply_calibration(self, key: int, settings: Any, commanded: float, measured: float) -> None:
        raw = settings.get(key)
        if raw is None:
            self.log(f"${key} not present")
            return
        value = f"{corrected_steps_per_mm(float(raw), commanded, measured):.3f}"
        if QMessageBox.question(self, "Calibrate", f"Write ${key} = {value} (was {raw})?") != QMessageBox.StandardButton.Yes:
            return
        self.bridge.submit(
            self._facade().write_setting(key, value),
            on_ok=lambda _r: self.log(f"  wrote ${key}={value} (verified)"),
            on_err=lambda exc: self.log(f"  {type(exc).__name__}: {exc}"),
        )

    # ================================================================= terminal
    def _send_mdi(self) -> None:
        if not self._require_connected():
            return
        line = self.mdi_edit.text().strip()
        if not line:
            return
        self.mdi_edit.clear()
        self.log(f"> {line}")
        self.bridge.submit(
            self._facade().run_line(line),
            on_ok=lambda _r: self.log("  ok"),
            on_err=lambda exc: self.log(f"  {type(exc).__name__}: {exc}"),
        )

    # =================================================================== status
    def _poll_status(self) -> None:
        # Detect a link the controller dropped on its own (missed status reports
        # or a closed port) and fall back to "searching" cleanly — no error box.
        if self.connected and (self.controller is None or not self.controller.is_connected):
            self._handle_link_lost()
        if self.controller is None or not self.connected:
            self.state_label.setText("—")
            self.mpos_label.setText("MPos: -")
            self.wpos_label.setText("WPos: -")
            self.fs_label.setText("Feed/Spindle: -    Ov: -")
            self._update_signals(None)
            return
        status = self.controller.last_status
        state = self.controller.state.value
        self.state_label.setText(state)
        if status is not None and status.mpos is not None:
            m = status.mpos
            self.mpos_label.setText(f"MPos  X {m.x:8.2f}  Y {m.y:8.2f}  Z {m.z:8.2f}")
        else:
            self.mpos_label.setText("MPos: -")
        wpos = status.wpos if status is not None else None
        if wpos is not None:
            self.wpos_label.setText(f"WPos  X {wpos.x:8.2f}  Y {wpos.y:8.2f}  Z {wpos.z:8.2f}")
        else:
            self.wpos_label.setText("WPos: -")
        if status is not None and (status.feed is not None or status.spindle is not None):
            feed = "-" if status.feed is None else f"{status.feed:.0f}"
            spindle = "-" if status.spindle is None else f"{status.spindle:.0f}"
            ov = status.overrides
            ov_text = f"{ov.feed}/{ov.rapid}/{ov.spindle}%" if ov is not None else "-"
            self.fs_label.setText(f"Feed/Spindle: {feed} / {spindle}    Ov(f/r/s): {ov_text}")
        else:
            self.fs_label.setText("Feed/Spindle: -    Ov: -")
        self._update_signals(status.signals if status is not None else None)

    def _update_signals(self, signals: Any) -> None:
        active = self._signal_map(signals)
        for key, label in self.dash_signals.items():
            on = active[key]
            label.setText(f"{key}{'!' if on else ''}")
            label.setStyleSheet("color:#ef4444; font-weight:bold;" if on else "color:#64748b;")
        for key, label in self.doctor_signals.items():
            on = active[key]
            label.setText(f"{key}\n{'ACTIVE' if on else 'off'}")
            label.setStyleSheet("color:#ef4444;" if on else "color:#22c55e;")
        self._refresh_diagnosis(active)

    @staticmethod
    def _signal_map(signals: Any) -> dict[str, bool]:
        if signals is None:
            return dict.fromkeys(_SIGNAL_KEYS, False)
        return {
            "X lim": signals.limit_x,
            "Y lim": signals.limit_y,
            "Z lim": signals.limit_z,
            "Probe": signals.probe,
            "Door": signals.door,
            "E-Stop": signals.estop,
        }

    def _refresh_diagnosis(self, active_map: dict[str, bool] | None = None) -> None:
        if not self.expected:
            return
        if active_map is None:
            signals = self.controller.last_status.signals if (
                self.controller is not None and self.controller.last_status is not None
            ) else None
            active_map = self._signal_map(signals)
        actual = {a for a in ("X", "Y", "Z") if active_map[f"{a} lim"]}
        expected = {a for a, cb in self.expected.items() if cb.isChecked()}
        lines = [
            f"Reported limit pins: {''.join(sorted(actual)) or '-'}",
            f"You expect pressed:  {''.join(sorted(expected)) or '-'}",
            "",
        ]
        if actual == expected:
            lines.append("OK - the switch logic matches what you expect.")
        else:
            lines.append("MISMATCH:")
            if actual - expected:
                lines.append(f"  active but not expected: {''.join(sorted(actual - expected))}")
                lines.append("    -> $5 (limit invert) wrong, NC/NO swapped, or a floating/shorted line.")
            if expected - actual:
                lines.append(f"  expected but inactive:   {''.join(sorted(expected - actual))}")
                lines.append("    -> wrong GPIO, wired C/NO not C/NC, missing GND, or a broken wire.")
        self.doctor_diagnosis.setPlainText("\n".join(lines))

    # =================================================================== helpers
    def log(self, text: str) -> None:
        self.bridge.post(lambda: self.console.appendPlainText(text))

    def _facade(self) -> Facade:
        if self.facade is None:
            raise RuntimeError("not connected")
        return self.facade

    def _require_connected(self) -> bool:
        if not self.connected or self.facade is None:
            # No modal: auto-connect is in progress; just note it and flash the
            # connection pill so the operator sees why nothing happened.
            self.log("not connected yet — waiting for the controller")
            self._set_conn_status("searching", None)
            return False
        return True

    def _do(self, coro: Any) -> None:
        self.bridge.submit(coro, on_err=lambda exc: self.log(f"{type(exc).__name__}: {exc}"))

    def _guarded(self, make_coro: Any) -> None:
        if not self._require_connected():
            return
        self._do(make_coro(self.facade))

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        # Disconnect cleanly (cancels the reader/poll tasks, closes the
        # transport) before stopping the loop, so no tasks are orphaned.
        if self.facade is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.facade.disconnect(), self.bridge.loop
                ).result(timeout=2.0)
            except Exception:  # never block the window from closing
                pass
        self.bridge.stop()
        super().closeEvent(event)


def _responsive_base_pt(app: QApplication) -> int:
    """Pick a base UI point size from the screen height, so text is legible on a
    small touch panel without overflowing. Tighter padding (below) keeps widgets
    from growing much taller as the font grows."""
    screen = app.primaryScreen()
    height = screen.availableGeometry().height() if screen is not None else 720
    if height <= 480:
        return 11
    if height <= 600:
        return 12
    if height <= 800:
        return 13
    return 14


def _apply_dark_theme(app: QApplication, base_pt: int = 12) -> None:
    """A calm dark theme; danger/start buttons are tinted via object names.

    ``base_pt`` (from the screen size) sets the app font and is used to keep
    padding proportional so larger text stays compact and inside the screen."""
    app.setStyle("Fusion")
    font = app.font()
    font.setPointSize(base_pt)
    app.setFont(font)
    pad_v = max(2, base_pt // 4)
    pad_h = max(5, base_pt // 2)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0b1220"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e5e7eb"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#020617"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#111827"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e5e7eb"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1f2937"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e5e7eb"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#111827"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#e5e7eb"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2563eb"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(
        f"""
        QGroupBox {{ border: 1px solid #1f2937; border-radius: 6px;
                     margin-top: {base_pt}px; }}
        QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                           color: #93c5fd; font-weight: bold; }}
        QPushButton {{ background: #1f2937; padding: {pad_v}px {pad_h}px;
                       border-radius: 4px; }}
        QPushButton:hover {{ background: #334155; }}
        QPushButton#danger {{ background: #7f1d1d; color: white; font-weight: bold; }}
        QPushButton#danger:hover {{ background: #991b1b; }}
        QPushButton#start {{ background: #14532d; color: white; font-weight: bold; }}
        QPushButton#start:hover {{ background: #166534; }}
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {{ background: #020617;
                   border: 1px solid #1f2937; border-radius: 4px; padding: {pad_v}px; }}
        QTabBar::tab {{ background: #111827; color: #e5e7eb;
                        padding: {pad_v + 1}px {pad_h + 2}px; }}
        QTabBar::tab:selected {{ background: #1f2937; color: #93c5fd; }}
        QProgressBar {{ border: 1px solid #1f2937; border-radius: 4px;
                        text-align: center; }}
        QProgressBar::chunk {{ background: #2563eb; }}
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="cncctl desktop GUI (Qt)")
    parser.add_argument("--config", type=Path, default=Path("config/machine.toml"))
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="launch fullscreen (kiosk mode on the Raspberry Pi appliance)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    _apply_dark_theme(app, _responsive_base_pt(app))
    if args.fullscreen:
        # Kiosk on a touch panel: hide the mouse cursor and show a tap ripple on
        # whichever window is touched (main window or a numpad/keyboard dialog).
        app.setOverrideCursor(QCursor(Qt.CursorShape.BlankCursor))
        TapFeedback.install(app)
    window = MainWindow(args.config, kiosk=args.fullscreen)
    window.resize(1180, 860)
    if args.fullscreen:
        window.showFullScreen()
    else:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
