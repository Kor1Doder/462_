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
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QFont, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
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
from touch_input import (  # sibling module in examples/
    TapFeedback,
    attach_keyboard,
    attach_numpad,
    attach_numpad_spin,
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
from cncctl.controller.errors import (
    CommandRejectedError,
    ConfigError,
    ConnectionLostError,
    NotConnectedError,
)
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
from cncctl.controller.state import MachineState
from cncctl.facade import Facade, MachineProfile
from cncctl.transport.serial_transport import SerialTransport
from cncctl.viz.analyze import SoftLimits
from cncctl.viz.simulate import Kinematics

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
    (100, "X steps/mm"),
    (101, "Y steps/mm"),
    (102, "Z steps/mm"),
    (110, "X max rate"),
    (111, "Y max rate"),
    (112, "Z max rate"),
    (120, "X accel"),
    (121, "Y accel"),
    (122, "Z accel"),
    (130, "X travel"),
    (131, "Y travel"),
    (132, "Z travel"),
    (3, "Dir invert"),
    (23, "Homing dir"),
    (5, "Limit invert"),
    (20, "Soft limits"),
    (21, "Hard limits"),
    (22, "Homing enable"),
    (24, "Home feed"),
    (25, "Home seek"),
    (26, "Debounce"),
    (27, "Pull-off"),
)
# Conservative first-commissioning preset (limits off, homing on, NC switches).
_SAFE_HOMING = {20: "0", 21: "0", 22: "1", 5: "1", 24: "50.0", 25: "300.0", 26: "250", 27: "2.0"}

# "Run without limit switches" — the commissioning state when no limit/home
# switches are wired yet (CLAUDE.md §2). $22=0 so the controller boots Idle
# instead of into a homing-required Alarm; $21=0 (no hard limits) and $20=0 (no
# soft limits) so motion — including a G-code program — never trips an alarm on
# floating/absent switch pins. This is exactly the cure for "it keeps entering
# Alarm and I can't get out", at the cost of switch-based protection.
_NO_LIMITS = {20: "0", 21: "0", 22: "0"}

_SIGNAL_KEYS = ("X lim", "Y lim", "Z lim", "Probe", "Door", "E-Stop")


class SoftHomeError(RuntimeError):
    """A software homing pass could not complete safely (switch not found,
    e-stop/door asserted, alarm during the seek, or operator abort)."""


@dataclass(frozen=True)
class _SoftHomeParams:
    """Operator-tunable parameters for the software homing routine.

    ``dirs`` is the seek direction sign per axis (+1 toward positive, -1 toward
    negative). The machine seeks each switch at ``seek_feed``, backs off
    ``bounce`` mm at ``locate_feed``, and aborts an axis if the switch is not
    found within ``max_travel`` mm.
    """

    seek_feed: float
    locate_feed: float
    bounce: float
    max_travel: float
    dirs: dict[Axis, float]


# Root-owned helper that switches from the kiosk to the desktop (installed by
# deploy/install.sh). Absent when running the GUI by hand outside the appliance.
_DESKTOP_HANDOFF = "/usr/local/sbin/cncctl-to-desktop"

# USB vendor id of the Raspberry Pi Pico (RP2040) running grblHAL.
_PICO_VID = 0x2E8A
# Stable symlink the udev rule (deploy/) gives the Pico; preferred when present.
_GRBLHAL_SYMLINK = Path("/dev/grblhal")


def discover_ports() -> list[str]:
    """All plausible grblHAL serial ports, best candidate first.

    Order: the stable ``/dev/grblhal`` udev symlink, then USB devices whose VID is
    the Pico's (0x2e8a), then **every** ``/dev/ttyACM*`` and ``/dev/ttyUSB*`` in
    numeric order (ACM0, ACM1, USB0 …). The last group is the important fallback:
    the grblHAL build's USB VID may not be the Pico's, or the udev rule may be
    absent, so we still try each CDC port in turn instead of finding nothing.
    """
    import glob

    from serial.tools import list_ports

    found: list[str] = []

    def add(dev: str) -> None:
        if dev and dev not in found:
            found.append(dev)

    if _GRBLHAL_SYMLINK.exists():
        add(str(_GRBLHAL_SYMLINK))
    for info in list_ports.comports():
        if getattr(info, "vid", None) == _PICO_VID:
            add(info.device)
    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        for dev in sorted(glob.glob(pattern), key=_port_sort_key):
            add(dev)
    return found


def _port_sort_key(dev: str) -> tuple[str, int]:
    """Sort /dev/ttyACM10 after /dev/ttyACM2 (numeric suffix, not lexical)."""
    m = re.search(r"(\d+)$", dev)
    return (re.sub(r"\d+$", "", dev), int(m.group(1)) if m else 0)


def local_ip() -> str:
    """This machine's primary IPv4 (the one used to reach the network).

    Uses the standard UDP-connect trick — no packet is actually sent, it just
    resolves which interface/address the OS would route through. Returns '?' if
    there is no network. On the Pi kiosk this is shown on-screen so the operator
    can point a PC at it (mypi@<ip>)."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "?"


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

    def submit(self, coro: Any, *, on_ok: Any = None, on_err: Any = None) -> Any:
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
                    self.post(
                        lambda e=exc: QMessageBox.critical(
                            None, "Error", f"{type(e).__name__}: {e}"
                        )
                    )
                return
            if on_ok is not None:
                self.post(lambda r=result: on_ok(r))

        future.add_done_callback(done)
        return future

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

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
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
        self._scan_queue: list[str] = []  # remaining ports to try this scan cycle
        self._send_future: Any = None  # the running send task, so Cancel can stop it
        self._soft_homing = False  # a software-homing pass is in progress
        self._soft_home_abort = False  # operator pressed Abort mid-homing
        self.sh_dir: dict[Axis, QComboBox] = {}  # per-axis seek-direction pickers
        # Cosmetic MPos origin captured at soft-home: the dashboard shows
        # (real MPos - this) so the panel reads 0 at home. Display only — grblHAL's
        # actual machine position is untouched (it can only be set by real $H).
        self._mpos_zero: dict[Axis, float] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)
        root.addWidget(self._topbar())
        root.addWidget(self._dashboard())

        tabs = QTabWidget()
        # Form-type tabs are wrapped so tall content is always reachable by
        # scrolling. Switch Doctor and Machine config already scroll internally;
        # the canvas tabs (CAD/PCB/Workpiece/Camera) fill the screen and must keep
        # their mouse interaction, so they are not wrapped.
        tabs.addTab(self._scrollable(self._control_tab()), "Control")
        tabs.addTab(self._scrollable(self._program_tab()), "Program")
        tabs.addTab(self._cad_tab(), "CAD/CAM")
        tabs.addTab(self._pcb_tab(), "PCB")
        tabs.addTab(self._workpiece_tab(), "Workpiece 3D")
        tabs.addTab(self._camera_tab(), "Camera")
        tabs.addTab(self._switch_tab(), "Switch Doctor")
        tabs.addTab(self._scrollable(self._settings_tab()), "Settings")
        tabs.addTab(self._config_tab(), "Machine config")
        tabs.addTab(self._scrollable(self._terminal_tab()), "Terminal")
        tabs.addTab(self._scrollable(self._transfer_tab()), "Transfer")
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

        # F11 toggles fullscreen anytime; Esc leaves it. ApplicationShortcut
        # context so it fires no matter which child widget has focus, and an
        # explicit .activated.connect (the constructor's activated= kwarg does
        # not reliably connect on PySide6). There is also a top-bar button.
        self._fs_sc = QShortcut(QKeySequence("F11"), self)
        self._fs_sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._fs_sc.activated.connect(self._toggle_fullscreen)
        self._esc_sc = QShortcut(QKeySequence("Escape"), self)
        self._esc_sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc_sc.activated.connect(self._exit_fullscreen)

    def _mono(self, delta: int = 0, *, bold: bool = False) -> QFont:
        """A monospace font sized relative to the app's responsive base point
        size (set from the screen resolution), so readouts scale with the UI."""
        weight = QFont.Weight.Bold if bold else QFont.Weight.Normal
        return QFont("Consolas", max(8, self.font().pointSize() + delta), weight)

    def _enable_touch_numpads(self) -> None:
        """Tap-to-keypad on every field (the touchscreen has no keyboard):
        numeric fields open a numpad, text fields a QWERTY keyboard."""
        # Jog / Z-zero / calibration inputs — all non-negative magnitudes.
        for edit in (self.step_edit, self.feed_edit, self.paper_edit, self.cal_cmd, self.cal_meas):
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
        for edit in (
            self.fields["machine.name"],
            self.fields["transport.default_port"],
            self.mdi_edit,
            self.file_edit,
        ):
            attach_keyboard(edit)
        # CAD/CAM and PCB spinboxes (incl. the per-shape editor) → touch numpad.
        for w in (getattr(self, "cad", None), getattr(self, "pcb", None)):
            if w is not None:
                w.enable_touch(attach_numpad_spin)

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
                subprocess.Popen(["sudo", "-n", _DESKTOP_HANDOFF])
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

        # This device's own IP — so the operator can read it off the touchscreen
        # and point a PC's Transfer tab at it (mypi@<ip>). Refreshed on each scan.
        self.ip_label = QLabel("IP …")
        self.ip_label.setStyleSheet("color:#38bdf8;")
        self.ip_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        flow.addWidget(self.ip_label)
        self._refresh_ip()

        self.sim_check = QCheckBox("Simulator")
        self.sim_check.setChecked(False)
        self.sim_check.toggled.connect(self._on_sim_toggled)
        flow.addWidget(self.sim_check)

        flow.addWidget(
            _button("Feed Hold !", lambda: self._guarded(lambda f: f.hold()), kind="danger")
        )
        flow.addWidget(_button("Resume ~", lambda: self._guarded(lambda f: f.resume())))
        flow.addWidget(_button("Unlock $X", lambda: self._guarded(lambda f: f.unlock())))
        flow.addWidget(_button("Clear Alarm", self._clear_alarm, kind="start"))
        flow.addWidget(
            _button("Soft Reset", lambda: self._guarded(lambda f: f.reset()), kind="danger")
        )
        flow.addWidget(_button("Full screen (F11)", self._toggle_fullscreen))
        flow.addWidget(_button("Quit → Desktop", self._quit_to_desktop))
        return box

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.showMaximized()  # fall back to maximized, not a tiny window
        else:
            self.showFullScreen()
            self.raise_()
            self.activateWindow()

    def _exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.showMaximized()

    def keyPressEvent(self, event: Any) -> None:
        # Direct handler in addition to the QShortcut, because on some
        # compositors the application-shortcut never reaches us. F11 toggles
        # fullscreen; Esc leaves it. Children propagate unhandled keys up here.
        key = event.key()
        if key == Qt.Key.Key_F11:
            self._toggle_fullscreen()
            event.accept()
            return
        if key == Qt.Key.Key_Escape and self.isFullScreen():
            self._exit_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def _scrollable(self, w: QWidget) -> QScrollArea:
        """Wrap a tab widget in a vertical scroll area so tall content stays
        reachable on short screens (the appliance touch panel). The inner widget
        tracks the viewport width and only scrolls when it is too tall to fit."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

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
            ("X-", Axis.X, -1.0, 0, 0),
            ("X+", Axis.X, 1.0, 0, 1),
            ("Y-", Axis.Y, -1.0, 1, 0),
            ("Y+", Axis.Y, 1.0, 1, 1),
            ("Z- down", Axis.Z, -1.0, 2, 0),
            ("Z+ up", Axis.Z, 1.0, 2, 1),
        ):
            pad.addWidget(_button(label, lambda _c=False, a=axis, s=sign: self._jog(a, s)), r, c)
        jcol.addLayout(pad)

        # Actions: three equal-width buttons.
        actions = QHBoxLayout()
        actions.addWidget(
            _button("Home $H", lambda: self._guarded(lambda f: f.home()), kind="danger"), 1
        )
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
        zgrid.addWidget(
            _button("Zero XYZ", lambda: self._zero_axes([Axis.X, Axis.Y, Axis.Z])), 2, 0, 1, 2
        )
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
        self.program_tab_widget = tab  # so a failed send can switch focus back here
        outer = QVBoxLayout(tab)
        row = QHBoxLayout()
        self.file_edit = QLineEdit()
        row.addWidget(self.file_edit, 1)
        row.addWidget(_button("Browse", self._browse))
        row.addWidget(_button("Pre-flight", self._preflight))
        row.addWidget(_button("Send", self._send_file, kind="start"))
        self.pause_btn = _button("Pause ⏸", self._toggle_pause)
        row.addWidget(self.pause_btn)
        row.addWidget(_button("Cancel (reset)", self._cancel_send, kind="danger"))
        outer.addLayout(row)
        self.progress = QProgressBar()
        outer.addWidget(self.progress)
        self.analysis = QPlainTextEdit()
        self.analysis.setReadOnly(True)
        self.analysis.setFont(self._mono())
        outer.addWidget(self.analysis, 1)
        return tab

    def _cad_tab(self) -> QWidget:
        """A small 2.5D CAD/CAM: draw shapes, set stock + origin, emit grblHAL
        G-code, and preview it in the 3D carve or push it to the sender."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from cad_cam import CadCamWidget

        self.cad = CadCamWidget()
        self.cad.on_preview = self._cad_preview
        self.cad.on_to_program = self._cad_to_program
        return self.cad

    def _pcb_tab(self) -> QWidget:
        """PCB front-copper isolation: draw traces/pads/board, emit grblHAL G-code."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from pcb import PcbWidget

        self.pcb = PcbWidget()
        self.pcb.on_preview = self._cad_preview
        self.pcb.on_to_program = self._cad_to_program
        return self.pcb

    def _cad_preview(self, gcode: str, label: str) -> None:
        wp = getattr(self, "workpiece", None)
        if wp is None:
            self.log("3D preview needs the GUI extra (pyqtgraph + PyOpenGL)")
            return
        wp.play_program(gcode, label)
        self.tabs.setCurrentWidget(wp)

    def _cad_to_program(self, gcode: str, label: str) -> None:
        from cad_cam import depo_dir

        fname = "pcb.nc" if label == "PCB" else "cadcam.nc"
        path = depo_dir() / fname  # keep it in depo so it persists and can be transferred
        path.write_text(gcode, encoding="utf-8")
        self.file_edit.setText(str(path))
        self.tabs.setCurrentWidget(self.program_tab_widget)
        self.log(f"{label} G-code saved to {path} — press Send")

    def _workpiece_tab(self) -> QWidget:
        """The GPU-accelerated carved-workpiece view (offline; no machine needed).

        Lazily imported so a missing 3D extra degrades to an install hint instead
        of breaking the whole window.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from workpiece_view import WorkpieceWidget
        except ImportError as exc:
            self.workpiece = None
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
        # Kept on the window so the Program-tab sender can drive it live (the
        # carve advances in step with the upload). Manual playback still works
        # whenever no upload is running.
        self.workpiece = WorkpieceWidget()
        return self.workpiece

    def _camera_tab(self) -> QWidget:
        """Live camera view for watching the cut; overlays the controller's real
        MPos (vision is never used to estimate position — see camera.py).

        Fully optional: if the camera module can't load for any reason, the tab
        degrades to a hint and the rest of the GUI runs normally."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from camera import CameraWidget

            self.camera = CameraWidget()
            self.camera.on_align = self._visual_align
            return self.camera
        except Exception as exc:
            self.camera = None
            tab = QWidget()
            lay = QVBoxLayout(tab)
            lbl = QLabel(f"Camera unavailable (optional): {type(exc).__name__}: {exc}")
            lbl.setWordWrap(True)
            lay.addWidget(lbl)
            lay.addStretch(1)
            return tab

    def _switch_tab(self) -> QWidget:
        # Wrap in a scroll area so the Soft Home panel at the bottom is always
        # reachable even on a short screen where the tab does not fully fit.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        tab = QWidget()
        scroll.setWidget(tab)
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
        lrow.addWidget(_button("Run w/o limits", self._disable_limits, kind="start"))
        lrow.addWidget(_button("Status ?", self._status_once))
        outer.addWidget(live)

        # Soft Home goes right under the live feedback so it is visible without
        # scrolling; the (secondary) Diagnosis panel sits below it.
        outer.addWidget(self._soft_home_group())

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
        # Bounded height so this secondary panel never eats the whole tab and
        # pushes the Soft Home controls off-screen.
        self.doctor_diagnosis.setMaximumHeight(140)
        outer.addLayout(body, 1)
        return scroll

    def _soft_home_group(self) -> QWidget:
        """Software homing (no grblHAL ``$H``): seek each limit switch, back off,
        and set G54 zero at the reference — order Z→Y→X, directions tunable.

        This is the DIY homing the operator asked for: we drive each axis toward
        its switch ourselves, watch the ``Pn:`` signal, stop on contact, pull off
        by the bounce distance, and zero. Every move is capped and abortable."""
        box = QGroupBox("Soft Home (yazılımsal referans — $H yok)")
        col = QVBoxLayout(box)
        hint = QLabel(
            "grblHAL homing yerine yazılımsal homing: her ekseni switch'ine sürer, "
            "değince durur, bounce kadar geri çekilir, G54'te sıfırlar. Sıra Z→Y→X. "
            "Limitler ($20/$21/$22) rutin boyunca otomatik kapatılıp sonra geri yüklenir."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#facc15;")
        col.addWidget(hint)

        # Tunables: feeds, bounce/pull-off, and the per-axis max-travel guard.
        prow = QHBoxLayout()
        self.sh_seek = QLineEdit("400")
        self.sh_locate = QLineEdit("120")
        self.sh_bounce = QLineEdit("3.0")
        self.sh_maxtravel = QLineEdit("400")
        for label, edit in (
            ("Seek mm/min", self.sh_seek),
            ("Locate mm/min", self.sh_locate),
            ("Bounce mm", self.sh_bounce),
            ("Max travel mm", self.sh_maxtravel),
        ):
            prow.addWidget(QLabel(label))
            edit.setMaximumWidth(80)
            prow.addWidget(edit)
        prow.addStretch(1)
        col.addLayout(prow)

        # Per-axis seek direction. Defaults: X toward X-, Y toward Y+, Z toward Z+ (up).
        drow = QHBoxLayout()
        drow.addWidget(QLabel("Yön:"))
        for axis, default in ((Axis.X, "-"), (Axis.Y, "+"), (Axis.Z, "+")):
            combo = QComboBox()
            combo.addItems(["-", "+"])
            combo.setCurrentText(default)
            self.sh_dir[axis] = combo
            drow.addWidget(QLabel(axis.value))
            drow.addWidget(combo)
        drow.addStretch(1)
        drow.addWidget(_button("Soft Home Z→Y→X", self._soft_home, kind="start"))
        drow.addWidget(_button("Abort", self._abort_soft_home, kind="danger"))
        col.addLayout(drow)
        return box

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
            grid.addWidget(
                _button(f"Compute {axis.upper()}", lambda _c=False, a=axis: self._compute_steps(a)),
                compute_row,
                col,
            )
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

    # ============================================================ transfer (WiFi)
    def _transfer_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        box = QGroupBox("Send this work to the Pi over WiFi (SSH) — both on the same network")
        form = QFormLayout(box)
        self.pi_host = QLineEdit("mypi@144.122.82.26")
        self.pi_dir = QLineEdit("~/cncctl")
        self.pi_pass = QLineEdit()
        self.pi_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.pi_pass.setPlaceholderText("blank if an SSH key is set up (recommended)")
        form.addRow("Pi SSH (user@host)", self.pi_host)
        form.addRow("Remote folder", self.pi_dir)
        form.addRow("Password (optional)", self.pi_pass)
        outer.addWidget(box)
        row = QHBoxLayout()
        row.addWidget(_button("Test connection", self._pi_test))
        row.addWidget(_button("Send current G-code →", self._pi_send_gcode, kind="start"))
        row.addWidget(_button("Send chosen files…", self._pi_send_files))
        row.addWidget(_button("Sync app code (deploy)", self._pi_sync_code))
        outer.addLayout(row)
        hint = QLabel(
            "Use the Pi's IP (shown top-left on the Pi's own screen) — '.local' needs mDNS and\n"
            "often won't resolve. For a password-free link, run once:  ssh-copy-id mypi@<pi-ip>\n"
            "'Send current G-code' copies the Program-tab file to the Pi; open it there and Send.\n"
            "'Sync app code' rsyncs this app to the Pi (updates the panel); then restart the kiosk."
        )
        hint.setWordWrap(True)
        outer.addWidget(hint)
        self.pi_log = QPlainTextEdit()
        self.pi_log.setReadOnly(True)
        self.pi_log.setFont(self._mono())
        outer.addWidget(self.pi_log, 1)
        for edit in (self.pi_host, self.pi_dir):
            attach_keyboard(edit)
        return tab

    def _has_sshpass(self) -> bool:
        return bool(self.pi_pass.text() and shutil.which("sshpass"))

    def _pi_prefix(self) -> list[str]:
        """sshpass wrapper if a password is given and available; else key auth."""
        if self._has_sshpass():
            return ["sshpass", "-p", self.pi_pass.text()]
        return []

    def _ssh_pairs(self) -> list[str]:
        pairs = ["StrictHostKeyChecking=accept-new", "ConnectTimeout=8"]
        if not self._has_sshpass():
            # No usable password → forbid the interactive prompt so ssh/scp FAIL
            # FAST with 'Permission denied' instead of hanging with no TTY.
            pairs.append("BatchMode=yes")
        return pairs

    def _ssh_opts(self) -> list[str]:
        opts: list[str] = []
        for p in self._ssh_pairs():
            opts += ["-o", p]
        return opts

    def _ssh_e(self) -> str:  # the -e string rsync uses to invoke ssh
        return "ssh " + " ".join(f"-o {p}" for p in self._ssh_pairs())

    def _pi_log_line(self, text: str) -> None:
        self.pi_log.appendPlainText(text)

    def _pi_run(self, argv: list[str], *, censor: str = "") -> None:
        if self.pi_pass.text() and not shutil.which("sshpass"):
            self._pi_log_line(
                "note: password is ignored — 'sshpass' is not installed. Use key auth "
                "(ssh-copy-id mypi@<ip>) or run: sudo apt install sshpass"
            )
        shown = " ".join("***" if censor and a == censor else a for a in argv)
        self._pi_log_line(f"$ {shown}")
        self.bridge.submit(
            self._run_proc(argv),
            on_ok=self._pi_log_line,
            on_err=lambda exc: self._pi_log_line(f"error: {type(exc).__name__}: {exc}"),
        )

    async def _run_proc(self, argv: list[str]) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            return f"missing command: {exc.filename} (e.g. sudo apt install rsync sshpass)"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=45)
        except TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return (
                "[timed out 45s] no reply — likely different subnet / firewall (METU "
                "isolates clients) or waiting for a password. Put both on one network "
                "(e.g. a phone hotspot) and set up ssh-copy-id."
            )
        text = out.decode(errors="replace").strip() or "(done, no output)"
        return f"[exit {proc.returncode}] {text}"

    def _pi_test(self) -> None:
        host = self.pi_host.text().strip()
        if not host:
            self._pi_log_line("enter the Pi address first (e.g. mypi@144.122.82.26)")
            return
        self._pi_run(
            self._pi_prefix() + ["ssh", *self._ssh_opts(), host, "echo cncctl-link-ok"],
            censor=self.pi_pass.text(),
        )

    def _pi_send_gcode(self) -> None:
        host = self.pi_host.text().strip()
        remote = self.pi_dir.text().strip() or "~"
        path = Path(self.file_edit.text())
        if not host or not path.name:
            self._pi_log_line("need a Pi address and a G-code file (Program tab).")
            return
        if not path.exists():
            self._pi_log_line(f"file not found: {path} — generate/save a program first.")
            return
        self._pi_run(
            self._pi_prefix() + ["scp", *self._ssh_opts(), str(path), f"{host}:{remote}/"],
            censor=self.pi_pass.text(),
        )

    def _pi_send_files(self) -> None:
        host = self.pi_host.text().strip()
        remote = self.pi_dir.text().strip() or "~"
        if not host:
            self._pi_log_line("enter the Pi address first.")
            return
        from cad_cam import depo_dir

        files, _ = QFileDialog.getOpenFileNames(
            self, "Choose files to send to the Pi", str(depo_dir()), "All files (*)"
        )
        if not files:
            return
        self._pi_run(
            self._pi_prefix() + ["scp", *self._ssh_opts(), *files, f"{host}:{remote}/"],
            censor=self.pi_pass.text(),
        )

    def _pi_sync_code(self) -> None:
        host = self.pi_host.text().strip()
        remote = self.pi_dir.text().strip() or "~/cncctl"
        if not host:
            self._pi_log_line("enter the Pi address first.")
            return
        local = Path(__file__).resolve().parent.parent  # the pitrofit/ project root
        # Exclude the heavy, machine-local, non-project trees. .vscode alone can be
        # >1 GB (VS Code's browse.vc.db IntelliSense cache), which over WiFi blows
        # past the SSH timeout and looks like a dead connection — it isn't.
        argv = self._pi_prefix() + [
            "rsync",
            "-az",
            "--exclude",
            ".venv",
            "--exclude",
            ".git",
            "--exclude",
            "__pycache__",
            "--exclude",
            ".vscode",
            "--exclude",
            ".mypy_cache",
            "--exclude",
            ".pytest_cache",
            "--exclude",
            ".ruff_cache",
            "--exclude",
            ".hypothesis",
            "-e",
            self._ssh_e(),
            f"{local}/",
            f"{host}:{remote}/",
        ]
        self._pi_run(argv, censor=self.pi_pass.text())

    # =============================================================== connection
    def _auto_scan(self) -> None:
        """Periodic: if nothing is connected, find a controller and connect.

        Simulator mode connects to the in-memory sim; otherwise we look for a
        Pico (``discover_ports``) and connect to the first one found. Failures
        are non-fatal — we back off briefly and the next scan retries.
        """
        self._refresh_ip()  # keep the on-screen IP current (cheap; no packets sent)
        if self._connecting or self.connected:
            return
        if time.monotonic() < self._retry_after:
            return
        if self.sim_check.isChecked():
            self._begin_connect("sim")
            return
        if not self._scan_queue:
            self._scan_queue = discover_ports()  # refresh candidates for a new cycle
        if self._scan_queue:
            target = self._scan_queue.pop(0)
            self.log(f"trying {target}…")
            self._begin_connect(target)
        else:
            self._set_conn_status("searching", None)
            self._retry_after = time.monotonic() + 1.0  # nothing plugged in; re-scan soon

    def _refresh_ip(self) -> None:
        ip = local_ip()
        self.ip_label.setText(f"IP {ip}")
        self.ip_label.setToolTip(f"This device — from a PC use  mypi@{ip}  in the Transfer tab")

    def _rescan(self) -> None:
        """Force an immediate, fresh (re)connect attempt over all ports."""
        self._retry_after = 0.0
        self._scan_queue = []  # re-discover ports from scratch
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
        # Short connect timeout so scanning past a wrong ACM port is quick.
        self.controller = RealController(transport, status_rate_hz=10, connect_timeout=3.0)
        self.facade = Facade(self.controller, profile=self._build_profile())
        self.bridge.submit(
            self.facade.connect(target),
            on_ok=lambda _r: self._on_connected(target),
            on_err=lambda exc: self._on_connect_failed(target, exc),
        )

    def _on_connected(self, target: str) -> None:
        self.connected = True
        self._connecting = False
        self._scan_queue = []  # found it; stop cycling candidates
        self._set_conn_status("connected", target)
        self.log(f"connected to {target}")

    def _on_connect_failed(self, target: str, exc: Exception) -> None:
        self._connecting = False
        self.connected = False
        self.controller = None
        self.facade = None
        self._set_conn_status("searching", None)
        self.log(f"{target} is not a grblHAL controller ({type(exc).__name__})")
        # Try the next candidate right away (ACM0 → ACM1 → …); only back off once
        # every port has been tried this cycle.
        if self._scan_queue and not self.sim_check.isChecked():
            self._auto_scan()
        else:
            self._retry_after = time.monotonic() + 3.0

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
                soft_limits=SoftLimits(
                    (-10000.0, 10000.0), (-10000.0, 10000.0), (-10000.0, 10000.0)
                ),
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

    # ============================================================ visual align
    def _visual_align(self) -> None:
        """Experimental: drive the tool onto the camera target as far as it can.

        Closed-loop visual servoing for a *fixed* camera (the tool moves in the
        image). It auto-calibrates pixels-per-mm with two small test jogs, then
        nudges X/Y to shrink the pixel error — stopping when it is close, stalls,
        or hits a cap, leaving the operator to finish by hand. Every move is XY
        only, step-capped, travel-capped, and abortable."""
        if not self._require_connected():
            return
        if self.camera.latest_frame() is None:
            QMessageBox.warning(self, "Visual align", "Start the camera first.")
            return
        if self.camera.target_pixel() is None:
            self.log("visual align: click the camera image to set a target first")
            return
        if (
            QMessageBox.question(
                self,
                "Visual align (experimental)",
                "The machine will JOG ITSELF in X/Y to drive the tool onto the clicked "
                "target, as far as it reliably can — then you finish by hand.\n\n"
                "• Spindle OFF (the tool is tracked by its appearance).\n"
                "• Keep a hand on the e-stop.\n"
                "• Z is never moved; each step is capped; Abort stops it.\n\nProceed?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.bridge.submit(self._visual_align_coro())

    async def _wait_idle(self, timeout: float = 8.0) -> bool:
        """Wait until the controller reports Idle again (a jog has settled)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        await asyncio.sleep(0.25)  # let the move actually start before testing Idle
        while loop.time() < deadline:
            ctrl = self.controller
            if ctrl is not None and ctrl.state.value.lower() == "idle":
                return True
            await asyncio.sleep(0.1)
        return False

    async def _visual_align_coro(self) -> None:
        import numpy as np

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import visual_align as va

        cam = self.camera
        fac = self._facade()
        feed, d_cal, gain = 600.0, 2.0, 0.5
        max_step, tol_px, max_iters, max_travel, min_score = 3.0, 8.0, 20, 40.0, 0.35

        def status(text: str) -> None:
            self.bridge.post(lambda: cam.set_align_status(text))

        def mark(pt: Any) -> None:
            self.bridge.post(lambda: cam.set_tool_marker(pt))

        def grab() -> Any:
            f = cam.latest_frame()
            return None if f is None else np.ascontiguousarray(f)

        async def jog_xy(dx: float, dy: float) -> None:
            if abs(dx) > 1e-3:
                await fac.jog(Axis.X, dx, self._cap_feed(Axis.X, feed))
                if not await self._wait_idle():
                    raise TimeoutError("X jog did not settle")
            if abs(dy) > 1e-3:
                await fac.jog(Axis.Y, dy, self._cap_feed(Axis.Y, feed))
                if not await self._wait_idle():
                    raise TimeoutError("Y jog did not settle")

        try:
            status("calibrating: measuring pixels-per-mm…")
            f0 = grab()
            if f0 is None:
                status("no camera frame — start the camera")
                return
            await jog_xy(d_cal, 0.0)
            fx = grab()
            await jog_xy(-d_cal, 0.0)
            await jog_xy(0.0, d_cal)
            fy = grab()
            await jog_xy(0.0, -d_cal)
            centroid = va.motion_centroid(f0, fx)
            if centroid is None:
                status("calibration failed: tool motion not seen (more light / bigger move?)")
                return
            template = va.grab_template(fx, centroid)
            p0, s0 = va.locate(f0, template)
            px, sx = va.locate(fx, template)
            py, sy = va.locate(fy, template)
            if min(s0, sx, sy) < min_score:
                status("calibration failed: tool not trackable (adjust lighting/contrast)")
                return
            jac = va.jacobian(p0, px, py, d_cal, d_cal)

            prev, travel = 1e9, 0.0
            for it in range(max_iters):
                if cam.align_aborted():
                    status("aborted — finish by hand")
                    return
                frame = grab()
                if frame is None:
                    status("lost camera frame")
                    return
                pos, score = va.locate(frame, template)
                mark(pos)
                if score < min_score:
                    status(f"lost the tool (match {score:.2f}) — finish by hand")
                    return
                target = cam.target_pixel()
                if target is None:
                    status("target cleared — stopped")
                    return
                ex, ey = target[0] - pos[0], target[1] - pos[1]
                err = (ex * ex + ey * ey) ** 0.5
                if err < tol_px:
                    status(f"aligned within {err:.0f}px — now finish by hand")
                    return
                if err > prev - 1.0:  # no meaningful progress: hand off to manual
                    status(f"stalled at {err:.0f}px — finish by hand")
                    return
                prev = err
                try:
                    dx, dy = va.mm_to_cancel(jac, (ex, ey))
                except np.linalg.LinAlgError:
                    status("degenerate calibration — finish by hand")
                    return
                dx, dy = dx * gain, dy * gain
                norm = (dx * dx + dy * dy) ** 0.5
                if norm > max_step:
                    dx, dy = dx * max_step / norm, dy * max_step / norm
                travel += abs(dx) + abs(dy)
                if travel > max_travel:
                    status(f"travel cap {max_travel:g}mm reached — finish by hand")
                    return
                status(f"step {it + 1}: error {err:.0f}px, moving X{dx:+.2f} Y{dy:+.2f}")
                await jog_xy(dx, dy)
            status(f"reached the {max_iters}-step limit — finish by hand")
        except Exception as exc:
            self.bridge.post(
                lambda e=exc: cam.set_align_status(f"align stopped: {type(e).__name__}: {e}")
            )
        finally:
            self.bridge.post(lambda: cam.set_tool_marker(None))

    def _zero_axes(self, axes: list[Axis]) -> None:
        if not self._require_connected():
            return
        names = "".join(a.value for a in axes)
        if (
            QMessageBox.question(self, "Work zero", f"Set current position as G54 {names}=0?")
            != QMessageBox.StandardButton.Yes
        ):
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
        if (
            QMessageBox.question(
                self, "Zero Z", f"Set current Z as G54 Z{paper:.3f}, then lift Z+5?"
            )
            != QMessageBox.StandardButton.Yes
        ):
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

    def _abort_send_task(self) -> None:
        """Stop the running send coroutine so its ``finally`` clears the
        controller's ``streaming`` flag. Without this, a mid-program soft reset
        leaves the streamer's ``stream()`` blocked, the flag stuck True, and every
        later ``$X``/command rejected with 'cannot send while streaming' — which
        is exactly why the alarm wouldn't clear until a reconnect."""
        fut = self._send_future
        self._send_future = None
        if fut is not None and not fut.done():
            fut.cancel()

    def _toggle_pause(self) -> None:
        """Toggle feed hold / resume for a running program.

        A feed hold (``!``) pauses motion without dropping the streamer, so a
        resume (``~``) picks up exactly where it left off — unlike Cancel, which
        soft-resets. The button label tracks the current state."""
        if not self._require_connected():
            return
        paused = getattr(self, "_paused", False)
        if paused:
            self._guarded(lambda f: f.resume())
            self.pause_btn.setText("Pause ⏸")
            self.log("resumed")
        else:
            self._guarded(lambda f: f.hold())
            self.pause_btn.setText("Resume ▶")
            self.log("feed hold — paused")
        self._paused = not paused

    def _cancel_send(self) -> None:
        """Cancel a running program and return to a usable state.

        Stops streaming, soft-resets the machine (which grblHAL leaves in Alarm
        because position was lost mid-motion), then unlocks ($X) so you land back
        in Idle instead of stuck in Alarm."""
        self._abort_send_task()
        self._paused = False
        self.pause_btn.setText("Pause ⏸")
        if not self._require_connected():
            return
        self.bridge.submit(
            self._cancel_coro(),
            on_ok=lambda _r: self.log("cancelled — reset + unlocked (Idle)"),
            on_err=lambda exc: self.log(f"cancel: {type(exc).__name__}: {exc}"),
        )

    async def _cancel_coro(self) -> None:
        await self._facade().cancel()  # feed hold + soft reset (stops the machine)
        await asyncio.sleep(0.4)  # let the welcome land and the send task unwind
        try:
            await self._facade().unlock()  # $X → back to Idle, no stuck alarm
        except Exception as exc:
            self.bridge.post(lambda e=exc: self.log(f"  $X after cancel: {e}"))

    def _clear_alarm(self) -> None:
        """Robustly get out of Alarm: stop any send, soft reset, then unlock ($X).

        A bare ``$X`` is refused while a limit pin still reads asserted (or while a
        cancelled program left the streamer stuck), so this aborts the send, resets
        to a clean state, and swallows a refusal instead of leaving a scary error.
        If it keeps coming back, the switches/settings are the cause — use 'Run w/o
        limits' in Switch Doctor."""
        self._abort_send_task()
        if not self._require_connected():
            return
        self.bridge.submit(
            self._clear_alarm_coro(),
            on_ok=lambda _r: self.log("clear alarm: reset + $X sent"),
            on_err=lambda exc: self.log(f"clear alarm: {type(exc).__name__}: {exc}"),
        )

    async def _clear_alarm_coro(self) -> None:
        await self._facade().reset()  # soft reset → clean slate
        await asyncio.sleep(0.3)  # let the welcome land and state settle
        try:
            await self._facade().unlock()  # $X clears the alarm lock
        except Exception as exc:
            self.bridge.post(lambda e=exc: self.log(f"  $X refused: {e} (check limit switches)"))

    def _disable_limits(self) -> None:
        """Write the no-switch preset ($20/$21/$22=0) and clear the alarm.

        This is the fix for a machine with no limit/home switches wired that keeps
        booting into / dropping to Alarm. Confirmed because it turns off the
        switch-based safety nets."""
        if not self._require_connected():
            return
        preset = ", ".join(f"${k}={v}" for k, v in _NO_LIMITS.items())
        if (
            QMessageBox.question(
                self,
                "Run without limit switches",
                "Turn OFF hard limits, soft limits and homing-required so the "
                f"machine stops entering Alarm?\n\n{preset}, then $X\n\n"
                "Only do this if no limit switches are wired.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.bridge.submit(
            self._disable_limits_coro(),
            on_ok=lambda _r: self.log("no-limits preset applied; alarm cleared"),
            on_err=lambda exc: self.log(f"disable limits: {type(exc).__name__}: {exc}"),
        )

    async def _disable_limits_coro(self) -> None:
        for num, value in _NO_LIMITS.items():
            await self._facade().write_setting(num, value)
            self.bridge.post(lambda n=num, v=value: self.log(f"  ${n}={v} (verified)"))
        await self._clear_alarm_coro()

    # ============================================================= soft homing
    def _abort_soft_home(self) -> None:
        """Ask a running soft-home pass to stop; the loop cancels the jog and
        unwinds at the next guard check (§8.3 soft-stop is always available)."""
        if self._soft_homing:
            self._soft_home_abort = True
            self.log("soft-home: abort requested")

    def _soft_home(self) -> None:
        """Kick off the DIY homing routine (Z→Y→X) from the Switch Doctor tab."""
        if not self._require_connected():
            return
        if self._soft_homing:
            self.log("soft-home already running")
            return
        try:
            params = _SoftHomeParams(
                seek_feed=float(self.sh_seek.text()),
                locate_feed=float(self.sh_locate.text()),
                bounce=float(self.sh_bounce.text()),
                max_travel=float(self.sh_maxtravel.text()),
                dirs={a: (1.0 if c.currentText() == "+" else -1.0) for a, c in self.sh_dir.items()},
            )
        except ValueError:
            QMessageBox.warning(self, "Soft Home", "Feeds, bounce and max travel must be numeric.")
            return
        if params.bounce <= 0 or params.max_travel <= 0 or params.seek_feed <= 0:
            QMessageBox.warning(self, "Soft Home", "Bounce, max travel and seek feed must be > 0.")
            return
        arrows = "  ".join(
            f"{a.value}{'+' if params.dirs[a] > 0 else '-'}" for a in (Axis.Z, Axis.Y, Axis.X)
        )
        if (
            QMessageBox.question(
                self,
                "Soft Home (kendi homing'imiz)",
                "Makine KENDİ KENDİNE her ekseni switch'ine sürecek, değince durup "
                f"{params.bounce:g} mm geri çekilip G54'te sıfırlayacak.\n\n"
                f"Sıra: Z → Y → X    Yön: {arrows}\n"
                f"Seek {params.seek_feed:g} mm/min, max {params.max_travel:g} mm.\n\n"
                "• E-stop'a elini yakın tut.  • Abort her an durdurur.\n"
                "• Limitler ($20/$21/$22) rutin boyunca geçici kapatılıp sonra "
                "geri yüklenir.\n\nBaşlansın mı?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._soft_homing = True
        self._soft_home_abort = False
        self.bridge.submit(
            self._soft_home_coro(params),
            on_ok=lambda _r: self.log("soft-home complete: G54 zero set at the switches (Z→Y→X)"),
            on_err=lambda exc: self.log(f"soft-home stopped: {type(exc).__name__}: {exc}"),
        )

    async def _soft_home_coro(self, p: _SoftHomeParams) -> None:
        """Home Z, then Y, then X against their limit switches, in software.

        Awaits each axis pass in turn. Any failure (switch not found, e-stop/door,
        alarm, abort) cancels the in-flight jog and re-raises so the on_err handler
        surfaces it. Clears ``_soft_homing`` on the way out no matter what."""
        f = self._facade()
        restore: dict[int, str] = {}
        try:
            # grblHAL IGNORES limit switches during its own homing cycle; we must do
            # the same. Otherwise the very first contact trips a hard-limit ALARM,
            # the machine never returns to Idle, the pull-off never runs, and the
            # pass dies after Z — exactly the "only Z, then nothing" symptom. So we
            # temporarily force $20/$21/$22 (soft limits / hard limits / homing-
            # required) to 0, remembering the old values to restore at the end.
            settings = await f.read_settings()
            for key in (20, 21, 22):
                old = settings.get(key)
                if old is not None and old != "0":
                    restore[key] = old
                    await f.write_setting(key, "0")
                    self.bridge.post(lambda k=key: self.log(f"soft-home: ${k}=0 (temporary)"))
            # A homing-required boot (or an earlier trip) can leave the machine in
            # Alarm; jog needs Idle, so unlock first.
            st = self.controller.last_status if self.controller is not None else None
            if st is not None and st.state is MachineState.ALARM:
                await f.unlock()
                await asyncio.sleep(0.3)
            # Sanity-check switch polarity BEFORE moving. With hard limits off and
            # the machine parked clear of the switches, every limit pin must read
            # 'off'. If one reads asserted at rest, $5 (NC/NO invert) is wrong — the
            # seek would 'hit' instantly and no back-off could ever release it (the
            # "won't release after 20 mm" symptom). Stop with the fix, don't thrash.
            await asyncio.sleep(0.2)  # let a fresh status arrive
            stuck = [a.value for a in (Axis.Z, Axis.Y, Axis.X) if self._sh_limit(a)]
            if stuck:
                raise SoftHomeError(
                    f"limit pin(s) {'+'.join(stuck)} read ASSERTED while idle and off "
                    "the switches — switch polarity ($5) is inverted. In Switch Doctor "
                    "flip $5 (1↔0) until every 'lim' shows OFF at rest, then retry. "
                    "(If an axis is truly parked on its switch, jog it off first.)"
                )
            for axis in (Axis.Z, Axis.Y, Axis.X):
                await self._soft_home_axis(f, axis, p.dirs[axis], p)
            # Homing done — auto-zero all axes in G54, exactly like pressing Zero XYZ
            # on the Control tab. The operator re-zeros at the workpiece by hand later.
            await self._sh_zero(f, [Axis.X, Axis.Y, Axis.Z])
            # Capture the current MPos as the cosmetic panel origin so the MPos
            # readout also shows 0 at home (display only — real MPos is untouched).
            st = self.controller.last_status if self.controller is not None else None
            if st is not None and st.mpos is not None:
                self._mpos_zero = {a: st.mpos.value(a) for a in (Axis.X, Axis.Y, Axis.Z)}
            self.bridge.post(lambda: self.log("soft-home: zeroed → X0 Y0 Z0 (panel MPos + G54)"))
        except BaseException:
            try:
                await f.cancel_jog()  # stop any motion in flight before unwinding
            except Exception:  # noqa: BLE001 - best-effort stop; original error wins
                pass
            raise
        finally:
            # Put the operator's limit settings back the way we found them. Every
            # axis has pulled off its switch by now, so re-enabling is safe.
            for key, val in restore.items():
                try:
                    await f.write_setting(key, val)
                    self.bridge.post(lambda k=key, v=val: self.log(f"soft-home: restored ${k}={v}"))
                except Exception as exc:  # noqa: BLE001 - a failed restore is logged, not fatal
                    self.bridge.post(
                        lambda k=key, e=exc: self.log(f"soft-home: could not restore ${k}: {e}")
                    )
            self._soft_homing = False

    async def _soft_home_axis(self, f: Facade, axis: Axis, sign: float, p: _SoftHomeParams) -> None:
        """Dead-simple per-axis homing: feed toward the switch, and the instant the
        pin goes active, back off a fixed amount the other way. That's it.

        We do NOT loop waiting for the pin to 'release' — we just seek, stop, back
        off once, and move on. Position (MPos) and pin state are logged at each step
        so what the machine actually does is visible."""
        seek = self._cap_feed(axis, p.seek_feed)
        slow = self._cap_feed(axis, p.locate_feed)
        self.bridge.post(
            lambda a=axis, fd=seek: self.log(f"soft-home {a.value}: seeking (feed {fd:g})…")
        )
        # SEEK toward the switch; the instant the pin asserts, stop.
        await f.jog(axis, sign * p.max_travel, seek)
        hit = await self._sh_wait_for_limit(axis)
        await f.cancel_jog()
        await self._sh_settle(f)  # let it stop; unlock if a limit tripped an alarm
        if not hit:
            raise SoftHomeError(
                f"{axis.value}: switch not found within {p.max_travel:g} mm (check direction)"
            )
        self.bridge.post(
            lambda a=axis, pos=self._sh_pos(axis): self.log(
                f"soft-home {a.value}: HIT at {pos} — backing off {p.bounce:g} mm"
            )
        )
        # BACK OFF once, the other way — and wait for the move to TRULY finish. A
        # 10 mm pull-off at 120 mm/min takes ~5 s; if we don't wait for the jog to
        # complete, the next command lands mid-move and grbl rejects it (error:9).
        await self._sh_jog_and_wait(f, axis, -sign * p.bounce, slow)
        self.bridge.post(
            lambda a=axis, pos=self._sh_pos(axis), on=self._sh_limit(axis): self.log(
                f"soft-home {a.value}: homed — now at {pos}, pin={'ON' if on else 'off'}"
            )
        )
        # Zeroing happens once at the very end, all axes together (see _soft_home_coro).

    async def _sh_jog_and_wait(self, f: Facade, axis: Axis, dist: float, feed: float) -> None:
        """Jog and block until the move actually completes: wait for motion to
        START (state → Jog), then for it to FINISH (state → Idle). Without this the
        next command can land while the jog is still running (grbl error:9), and a
        status read returns the pre-move position (the "MPos didn't change" mirage)."""
        await f.jog(axis, dist, feed)
        # Confirm motion started (best-effort — a very short move may skip the Jog
        # report at 10 Hz, so cap the wait and move on).
        for _ in range(40):  # ~1.2 s
            self._sh_guard()
            st = self.controller.last_status if self.controller is not None else None
            if st is not None and st.state is MachineState.JOG:
                break
            await asyncio.sleep(0.03)
        await self._sh_settle(f)  # now wait for it to come back to Idle

    async def _sh_zero(self, f: Facade, axes: list[Axis]) -> None:
        """Set the current position as G54 zero for ``axes`` — the same action as
        the Control tab's Zero XYZ. Retries on a transient error:9 (a G-code word
        rejected because the machine is momentarily still in Jog/Alarm)."""
        for attempt in range(6):
            await self._sh_confirm_idle(f)
            try:
                await f.set_work_zero(axes)
                return
            except CommandRejectedError as exc:
                if "error:9" in str(exc) and attempt < 5:
                    await asyncio.sleep(0.4)  # let the motion state clear, then retry
                    continue
                raise
        names = "".join(a.value for a in axes)
        raise SoftHomeError(f"{names}: could not set zero (machine stayed busy)")

    def _sh_pos(self, axis: Axis) -> str:
        """The axis' machine position from the latest status, for diagnostics."""
        st = self.controller.last_status if self.controller is not None else None
        if st is None or st.mpos is None:
            return "?"
        return f"{axis.value}{st.mpos.value(axis):.2f}"

    def _sh_limit(self, axis: Axis) -> bool:
        """Is ``axis``'s limit switch currently asserted (from the latest status)?"""
        st = self.controller.last_status if self.controller is not None else None
        if st is None:
            return False
        s = st.signals
        return {Axis.X: s.limit_x, Axis.Y: s.limit_y, Axis.Z: s.limit_z}[axis]

    def _sh_guard(self) -> None:
        """Raise if the pass must stop now: operator abort, e-stop, or door."""
        if self._soft_home_abort:
            raise SoftHomeError("aborted by operator")
        st = self.controller.last_status if self.controller is not None else None
        if st is not None:
            if st.signals.estop:
                raise SoftHomeError("E-STOP asserted")
            if st.signals.door or st.state is MachineState.DOOR:
                raise SoftHomeError("safety door open")

    async def _sh_wait_for_limit(self, axis: Axis, timeout: float = 180.0) -> bool:
        """Watch ``axis``'s switch during the seek. Return True on contact, False
        if the seek jog finishes without ever pressing it.

        Contact counts whether it shows up as the ``Pn:`` pin OR as a limit-alarm
        (with hard limits on, grblHAL trips its own alarm on contact) — either way
        the caller stops and backs off. Raises only on e-stop/door/abort (via the
        guard). We poll fast (30 ms) so a switch is never missed mid-motion."""
        # Wait for the jog to actually start so a pre-jog Idle is not mistaken for
        # a finished seek; contact during this window still counts as a hit.
        for _ in range(50):  # ~1.5 s
            self._sh_guard()
            if self._sh_limit(axis):
                return True
            st = self.controller.last_status if self.controller is not None else None
            if st is not None and st.state is MachineState.JOG:
                break
            await asyncio.sleep(0.03)
        for _ in range(int(timeout / 0.03)):
            self._sh_guard()
            if self._sh_limit(axis):
                return True
            st = self.controller.last_status if self.controller is not None else None
            if st is not None:
                if st.state is MachineState.ALARM:  # contact tripped a hard limit
                    return True
                if st.state is MachineState.IDLE:  # jog completed, no contact
                    return False
            await asyncio.sleep(0.03)
        return False

    async def _sh_settle(self, f: Facade, timeout: float = 8.0) -> None:
        """Let motion stop and make sure the next jog will be accepted.

        If a limit tripped an alarm, unlock ($X) so grblHAL will let us jog off the
        switch. Then wait briefly for Idle. Never raises on timeout — a genuinely
        stuck machine surfaces when the following jog is rejected; abort/e-stop/door
        still raise through the guard."""
        for _ in range(int(timeout / 0.05)):
            self._sh_guard()
            st = self.controller.last_status if self.controller is not None else None
            if st is not None:
                if st.state is MachineState.IDLE:
                    return
                if st.state is MachineState.ALARM:
                    await f.unlock()  # clear the limit-alarm so a jog-off is allowed
                    await asyncio.sleep(0.3)
            await asyncio.sleep(0.05)

    async def _sh_confirm_idle(self, f: Facade, timeout: float = 8.0) -> bool:
        """Wait for a *stable* Idle — two consecutive polls — so a following G-code
        word (G10 zero) is not rejected mid-transition as grbl error:9. Unlocks on
        alarm. Returns True once Idle is confirmed, False on timeout."""
        idle_seen = 0
        for _ in range(int(timeout / 0.05)):
            self._sh_guard()
            st = self.controller.last_status if self.controller is not None else None
            if st is not None and st.state is MachineState.IDLE:
                idle_seen += 1
                if idle_seen >= 2:
                    return True
            else:
                idle_seen = 0
                if st is not None and st.state is MachineState.ALARM:
                    await f.unlock()
                    await asyncio.sleep(0.3)
            await asyncio.sleep(0.05)
        return False

    # ================================================================== program
    def _browse(self) -> None:
        from cad_cam import depo_dir

        path, _ = QFileDialog.getOpenFileName(
            self, "Open G-code", str(depo_dir()), "G-code (*.nc *.gcode *.ngc *.txt);;All files (*)"
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
        path = Path(self.file_edit.text())
        # Fresh send: make sure the pause toggle starts in the un-paused state.
        self._paused = False
        self.pause_btn.setText("Pause ⏸")
        # A big file's soft-limit pre-flight can take a moment (it simulates the
        # whole toolpath off-thread); show that so Send doesn't look frozen.
        self.progress.setFormat("pre-flight / checking…")
        self.log(f"sending {path.name} — pre-flight…")
        # The live 3D preview is a nice-to-have; it must NEVER stop the actual
        # send. Any failure setting it up is logged and swallowed so the stream
        # below always starts.
        try:
            self._begin_live_sim(path)
        except Exception as exc:
            self.log(f"live sim unavailable: {type(exc).__name__}: {exc}")
        self._send_future = self.bridge.submit(
            self._stream_file(path),
            on_ok=lambda _r: self.log("send complete"),
            on_err=lambda exc: self._on_send_failed(exc),
        )

    def _on_send_failed(self, exc: Exception) -> None:
        """Make a refused/aborted send obvious — we may have switched to the 3D
        tab, so surface the reason on the Program tab instead of only logging it."""
        msg = f"SEND FAILED: {type(exc).__name__}: {exc}"
        self.log(msg)
        hint = ""
        if "alarm" in str(exc).lower() or type(exc).__name__ == "MachineNotReadyError":
            hint = (
                "\n\nMachine is not Idle (Alarm/limits?). Press 'Clear Alarm' in the\n"
                "top bar. If it keeps re-alarming and no limit switches are wired,\n"
                "use 'Run w/o limits' in the Switch Doctor tab, then Send again."
            )
        self.analysis.setPlainText(msg + hint)
        self.tabs.setCurrentWidget(self.program_tab_widget)

    def _begin_live_sim(self, path: Path) -> None:
        """Auto-run the cut simulation when the operator hits Send.

        Loads the program into the Workpiece 3D view, switches to it, and starts
        the animation right away — so pressing Send makes the simulation run, even
        if the machine is busy/alarmed and no lines flow yet. If the 3D extra is
        missing this is a no-op and the send still runs.
        """
        wp = getattr(self, "workpiece", None)
        if wp is None:
            return
        # Tolerant read: CAM posts are usually ASCII but may carry stray bytes;
        # never let a decode hiccup abort the preview (let alone the send).
        text = path.read_text(encoding="utf-8", errors="replace")
        wp.play_program(text, str(path))
        self.tabs.setCurrentWidget(wp)  # bring the running simulation into view

    async def _stream_file(self, path: Path) -> None:
        # The sender can emit thousands of progress events per second; posting
        # every one would flood the Qt queue. Coalesce progress-bar updates to
        # ~20 Hz. (The 3D simulation runs on its own timer, independent of this.)
        loop = asyncio.get_running_loop()
        last_post = 0.0
        latest = None
        async for progress in self._facade().send_program(path):
            latest = progress
            now = loop.time()
            if now - last_post >= 0.05:
                last_post = now
                self.bridge.post(
                    lambda s=progress.sent, t=progress.total or 0: self._show_progress(s, t)
                )
        if latest is not None:  # always show the final count
            self.bridge.post(lambda s=latest.sent, t=latest.total or 0: self._show_progress(s, t))

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

        # Feed the machine's real max travel ($130/$131/$132) into the CAD/CAM
        # fit-check so an oversized part is flagged against the actual envelope.
        def _f(n: int) -> float:
            v = settings.get(n)
            try:
                return float(v) if v is not None else 0.0
            except ValueError:
                return 0.0

        for w in (getattr(self, "cad", None), getattr(self, "pcb", None)):
            if w is not None:
                w.set_machine_travel(_f(130), _f(131), _f(132))
        self.log("read $$ settings")

    def _write_live_settings(self) -> None:
        if not self._require_connected():
            return
        edits = {
            num: e.text().strip() for num, e in self.setting_fields.items() if e.text().strip()
        }
        if (
            QMessageBox.question(
                self, "Write settings", f"Write {len(edits)} settings to the controller?"
            )
            != QMessageBox.StandardButton.Yes
        ):
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
        if (
            QMessageBox.question(
                self, "Safe homing", f"Write the first-commissioning preset?\n\n{preset}"
            )
            != QMessageBox.StandardButton.Yes
        ):
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
        self.fields[f"axes.{axis}.steps_per_mm"].setText(
            f"{_FULL_STEPS_PER_REV * microsteps / lead:.3f}"
        )

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

    def _apply_calibration(
        self, key: int, settings: Any, commanded: float, measured: float
    ) -> None:
        raw = settings.get(key)
        if raw is None:
            self.log(f"${key} not present")
            return
        value = f"{corrected_steps_per_mm(float(raw), commanded, measured):.3f}"
        if (
            QMessageBox.question(self, "Calibrate", f"Write ${key} = {value} (was {raw})?")
            != QMessageBox.StandardButton.Yes
        ):
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
        # If the machine alarms mid-send, the stream would otherwise just stall
        # (no more acks). Stop it and surface the reason instead of hanging.
        if state == "Alarm" and self._send_future is not None and not self._send_future.done():
            self._abort_send_task()
            msg = "ALARM during send — machine stopped mid-program."
            self.log(msg)
            self.analysis.setPlainText(
                msg + "\n\nUsually a hard/soft limit was hit (or an EMI/link glitch).\n"
                "Check Switch Doctor + machine travel, press 'Clear Alarm' (or\n"
                "'Run w/o limits' if no switches are wired), then Send again."
            )
            self.tabs.setCurrentWidget(self.program_tab_widget)
        if status is not None and status.mpos is not None:
            m = status.mpos
            # Subtract the soft-home origin so the panel reads 0 at home (cosmetic).
            dx = m.x - self._mpos_zero.get(Axis.X, 0.0)
            dy = m.y - self._mpos_zero.get(Axis.Y, 0.0)
            dz = m.z - self._mpos_zero.get(Axis.Z, 0.0)
            self.mpos_label.setText(f"MPos  X {dx:8.2f}  Y {dy:8.2f}  Z {dz:8.2f}")
            cam = getattr(self, "camera", None)
            if cam is not None:
                cam.set_position(f"{state}  X{dx:.2f} Y{dy:.2f} Z{dz:.2f}")
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
            signals = (
                self.controller.last_status.signals
                if (self.controller is not None and self.controller.last_status is not None)
                else None
            )
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
                lines.append(
                    "    -> $5 (limit invert) wrong, NC/NO swapped, or a floating/shorted line."
                )
            if expected - actual:
                lines.append(f"  expected but inactive:   {''.join(sorted(expected - actual))}")
                lines.append(
                    "    -> wrong GPIO, wired C/NO not C/NC, missing GND, or a broken wire."
                )
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

    def closeEvent(self, event: Any) -> None:
        # Release the camera device so it is not held after the window closes.
        cam = getattr(self, "camera", None)
        if cam is not None:
            cam.shutdown()
        # Disconnect cleanly (cancels the reader/poll tasks, closes the
        # transport) before stopping the loop, so no tasks are orphaned.
        if self.facade is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.facade.disconnect(), self.bridge.loop).result(
                    timeout=2.0
                )
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
    if args.fullscreen:
        window.resize(1180, 860)
        window.showFullScreen()
    else:
        # Windowed: never let the window grow taller/wider than the screen's usable
        # area — on a short panel an oversized window pushes its bottom (and the tab
        # scrollbars) off-screen, so you can neither see nor scroll to the lower
        # controls. (Not applied in fullscreen, where the window must fill the screen.)
        screen = app.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            window.setMaximumSize(avail.width(), avail.height())
            window.resize(min(1180, avail.width()), min(860, avail.height()))
        else:
            window.resize(1180, 860)
        window.showMaximized()  # big by default; F11 / the top-bar button go fullscreen
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
