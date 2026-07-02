"""cncctl desktop GUI (PySide6 / Qt) — an all-in-one operator panel.

Built entirely on the public Facade API. Layout (modelled on
``reference/gu_cnc_all_in_one.py`` but on our async core, with a real 3D view):

* a persistent **top bar** — connection + always-available emergency controls
  (Feed Hold / Resume / Unlock / Soft Reset), reachable from any tab;
* a persistent **dashboard** — live state, MPos, WPos, feed/spindle/overrides,
  and decoded input switches;
* tabs in workflow order: Guide, Control (jog + G54 work-zero), Program (send +
  pre-flight), Workpiece 3D, Switch Doctor, Settings (live grblHAL), Machine
  config (machine.toml + calibrate), Terminal.

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
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
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
from cncctl.controller.errors import ConfigError
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
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

# Raspberry Pi vendor id (RP2040 / Pico USB-CDC). grblHAL on the Pico enumerates
# under this VID; we also fall back to a name match for re-branded boards.
_PICO_VID = 0x2E8A


def discover_ports() -> list[Any]:
    """Available serial ports (pyserial ``ListPortInfo``), sorted by device."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return sorted(list_ports.comports(), key=lambda p: p.device)


def is_pico(info: Any) -> bool:
    """True if a port looks like a Raspberry Pi Pico / RP2040 (the grblHAL host)."""
    if getattr(info, "vid", None) == _PICO_VID:
        return True
    text = " ".join(
        str(getattr(info, attr, "") or "") for attr in ("description", "manufacturer", "product")
    ).lower()
    return any(key in text for key in ("pico", "rp2040", "raspberry"))

GUIDE_TEXT = """\
HOW THIS PANEL WORKS

The top bar is always visible: connect (or tick "Use simulator"), and the
emergency controls — Feed Hold, Resume, Unlock, Soft Reset — work from any tab.
The dashboard under it shows live state, position, and the input switches.

A SAFE FIRST RUN

  1.  Connect, or tick "Use simulator" to try without hardware.
  2.  Switch Doctor: jog each limit switch by hand, confirm it lights up on the
      correct axis. Get this right before enabling hard limits.
  3.  Machine config: fill in steps/mm, rates, accel, travel; Save. Settings:
      "Apply safe homing" then Home ($H).
  4.  Control: jog the tool to the part origin, then Zero XYZ (sets G54).
  5.  Program: choose a G-code file, Pre-flight (host-side soft-limit check),
      and watch it in Workpiece 3D first.
  6.  Air-cut above the stock before cutting for real. Keep a hand on the
      hardwired e-stop.
"""

SWITCH_WIRING_TEXT = """\
NORMALLY-CLOSED (NC) WIRING — recommended

Wire each limit switch normally-closed to ground, with the input pulled up:
a broken wire then reads as "triggered" and fails safe.

  3V3 --[10k]-- GPIO        (pull-up)
  GPIO -------- switch NC
  switch C ---- GND

With NC switches set $5=1 (limit invert). Rule of thumb, unpressed:

  Pn shows nothing. Press X -> Pn:X. Press Y -> Pn:Y. Press Z -> Pn:Z.

If a pin reads active while nothing is pressed: $5 is wrong, NC/NO are
swapped, or the line is floating/shorted. If pressing does nothing: wrong
GPIO, wired C/NO instead of C/NC, missing GND, or a broken wire.
"""

SETTINGS_HELP_TEXT = """\
LIVE grblHAL SETTINGS ($$)

$100-102  steps/mm per axis (from Machine config; verify against calibration).
$110-112  max rate (mm/min). Too high for the load -> stall (silent, high pitch).
$120-122  acceleration (mm/s^2). Lower if the motor stalls on direction changes.
$130-132  max travel (mm). Soft limits use this; keep it accurate.
$3   direction invert mask (axis runs the wrong way).
$23  homing direction mask.   $5  limit-pin invert (NC switches -> 1).
$20  soft limits (enable only once travel/homing are correct).
$21  hard limits (enable last, once wiring is stable).
$22  homing enable (1 for $H).  $24/$25 homing feed/seek.  $27 pull-off mm.

Read $$ to load, edit a field and Write to push (each write is verified by a
re-read). Backup writes the full set to cnc_backups/*.json first.
"""


class Bridge(QObject):
    """Runs an asyncio loop in a thread; marshals results to the Qt main thread."""

    invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.invoke.connect(self._run)
        self.error_handler: Any = None  # set by the window to log instead of pop a dialog

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
            except Exception as exc:  # report any failure to the user
                if on_err is not None:
                    self.post(lambda e=exc: on_err(e))
                elif self.error_handler is not None:
                    self.post(lambda e=exc: self.error_handler(e))
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


class MainWindow(QWidget):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.setWindowTitle("cncctl - EMCO retrofit")
        self.config_path = config_path
        self.bridge = Bridge()
        self.fields: dict[str, QLineEdit] = {}
        self.setting_fields: dict[int, QLineEdit] = {}
        self.dash_signals: dict[str, QLabel] = {}
        self.doctor_signals: dict[str, QLabel] = {}
        self.expected: dict[str, QCheckBox] = {}
        self.facade: Facade | None = None
        self.controller: RealController | None = None
        self.connected = False
        self._connecting = False
        self._ports: list[Any] = []
        self._port_devices: list[str] = []
        self.bridge.error_handler = lambda exc: self.log(f"error: {type(exc).__name__}: {exc}")

        root = QVBoxLayout(self)
        root.addWidget(self._topbar())
        root.addWidget(self._dashboard())

        tabs = QTabWidget()
        tabs.addTab(self._guide_tab(), "0) Guide")
        tabs.addTab(self._control_tab(), "1) Control")
        tabs.addTab(self._program_tab(), "2) Program")
        tabs.addTab(self._workpiece_tab(), "3) Workpiece 3D")
        tabs.addTab(self._switch_tab(), "4) Switch Doctor")
        tabs.addTab(self._settings_tab(), "5) Settings")
        tabs.addTab(self._config_tab(), "6) Machine config")
        tabs.addTab(self._terminal_tab(), "7) Terminal")
        self.tabs = tabs
        root.addWidget(tabs, 1)

        self._load_config()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_status)
        self.timer.start(150)
        # Port discovery + auto-(re)connect loop.
        self.scan_timer = QTimer(self)
        self.scan_timer.timeout.connect(self._auto_tick)
        self.scan_timer.start(1500)
        self._auto_tick()

    # ===================================================================== bars
    def _topbar(self) -> QWidget:
        box = QGroupBox("Connection / Emergency")
        row = QHBoxLayout(box)
        self.auto_check = QCheckBox("Auto-connect")
        self.auto_check.setChecked(True)
        self.auto_check.setToolTip("Find a Pico (or the simulator) and connect automatically.")
        row.addWidget(self.auto_check)
        self.sim_check = QCheckBox("Use simulator")
        row.addWidget(self.sim_check)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(220)
        self.port_combo.setToolTip("Discovered serial ports (auto-refreshed).")
        row.addWidget(self.port_combo)
        row.addWidget(_button("Connect", self._connect, kind="start"))
        row.addWidget(_button("Disconnect", self._disconnect))
        self.conn_status = QLabel("starting...")
        self.conn_status.setStyleSheet("color:#9aa;")
        row.addWidget(self.conn_status)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        row.addWidget(sep)
        row.addWidget(_button("Feed Hold !", lambda: self._guarded(lambda f: f.hold()), kind="danger"))
        row.addWidget(_button("Resume ~", lambda: self._guarded(lambda f: f.resume())))
        row.addWidget(_button("Unlock $X", lambda: self._guarded(lambda f: f.unlock())))
        row.addWidget(_button("Soft Reset", lambda: self._guarded(lambda f: f.reset()), kind="danger"))
        row.addStretch(1)
        return box

    def _dashboard(self) -> QWidget:
        box = QGroupBox("Status")
        grid = QGridLayout(box)
        self.state_label = QLabel("Disconnected")
        self.state_label.setFont(QFont("Consolas", 15, QFont.Weight.Bold))
        self.state_label.setStyleSheet("color:#facc15;")
        grid.addWidget(self.state_label, 0, 0, 2, 1)

        self.mpos_label = QLabel("MPos: -")
        self.wpos_label = QLabel("WPos: -")
        self.fs_label = QLabel("Feed/Spindle: -    Ov: -")
        for lbl in (self.mpos_label, self.wpos_label, self.fs_label):
            lbl.setFont(QFont("Consolas", 10))
        grid.addWidget(self.mpos_label, 0, 1)
        grid.addWidget(self.wpos_label, 0, 2)
        grid.addWidget(self.fs_label, 1, 1, 1, 2)

        inputs = QHBoxLayout()
        inputs.addWidget(QLabel("Inputs:"))
        for key in _SIGNAL_KEYS:
            lbl = QLabel(key)
            lbl.setFont(QFont("Consolas", 9))
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
    def _guide_tab(self) -> QWidget:
        tab = QWidget()
        outer = QHBoxLayout(tab)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(GUIDE_TEXT)
        text.setFont(QFont("Consolas", 10))
        outer.addWidget(text, 2)

        links = QGroupBox("Jump to")
        col = QVBoxLayout(links)
        for label, idx in (
            ("Switch Doctor", 4),
            ("Machine config", 6),
            ("Settings", 5),
            ("Control / jog & zero", 1),
            ("Program (send G-code)", 2),
            ("Workpiece 3D preview", 3),
        ):
            col.addWidget(_button(label, lambda _c=False, i=idx: self.tabs.setCurrentIndex(i)))
        col.addStretch(1)
        outer.addWidget(links, 1)
        return tab

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
        self.analysis.setFont(QFont("Consolas", 10))
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
            lbl.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
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
        self.doctor_diagnosis.setFont(QFont("Consolas", 10))
        dcol.addWidget(self.doctor_diagnosis, 1)
        body.addWidget(diag, 1)

        wiring = QGroupBox("Recommended wiring (NC)")
        wcol = QVBoxLayout(wiring)
        wt = QTextEdit()
        wt.setReadOnly(True)
        wt.setPlainText(SWITCH_WIRING_TEXT)
        wt.setFont(QFont("Consolas", 9))
        wcol.addWidget(wt)
        body.addWidget(wiring, 1)
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

        body = QHBoxLayout()
        form = QGroupBox("Live grblHAL settings")
        fgrid = QGridLayout(form)
        for idx, (num, label) in enumerate(_LIVE_SETTINGS):
            r, c = idx // 2, (idx % 2) * 2
            fgrid.addWidget(QLabel(f"${num} {label}"), r, c)
            edit = QLineEdit()
            self.setting_fields[num] = edit
            fgrid.addWidget(edit, r, c + 1)
        body.addWidget(form, 2)

        helpbox = QGroupBox("What these mean")
        hcol = QVBoxLayout(helpbox)
        ht = QTextEdit()
        ht.setReadOnly(True)
        ht.setPlainText(SETTINGS_HELP_TEXT)
        ht.setFont(QFont("Consolas", 9))
        hcol.addWidget(ht)
        body.addWidget(helpbox, 1)
        outer.addLayout(body, 1)
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
            ("transport.default_port_windows", "Port (Windows)", 90),
            ("transport.default_port_linux", "Port (Linux)", 120),
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
            header.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
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
        self.console.setFont(QFont("Consolas", 10))
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
    def _auto_tick(self) -> None:
        """Refresh the port list and, if enabled, auto-(re)connect to a Pico/sim.

        Runs on a timer. Never raises into the UI: connect failures are logged and
        retried on the next tick (the in-flight guard prevents overlapping tries).
        """
        self._refresh_ports()
        if self.connected or self._connecting or not self.auto_check.isChecked():
            if not self.connected and not self._connecting:
                self._set_conn_status("auto-connect off" if not self.auto_check.isChecked() else "")
            return
        if self.sim_check.isChecked():
            self._begin_connect_sim()
            return
        device = self._find_pico()
        if device is not None:
            self._begin_connect_serial(device)
        else:
            self._set_conn_status(f"searching for Pico... ({len(self._ports)} port(s))")

    def _refresh_ports(self) -> None:
        self._ports = discover_ports()
        devices = [p.device for p in self._ports]
        if devices == self._port_devices:
            return  # unchanged — don't disturb the combo (or the open dropdown)
        self._port_devices = devices
        keep = self.port_combo.currentData()
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        for info in self._ports:
            tag = "  [Pico]" if is_pico(info) else ""
            self.port_combo.addItem(f"{info.device} - {info.description}{tag}", info.device)
        if keep is not None:
            idx = self.port_combo.findData(keep)
            if idx >= 0:
                self.port_combo.setCurrentIndex(idx)
        self.port_combo.blockSignals(False)

    def _find_pico(self) -> str | None:
        for info in self._ports:
            if is_pico(info):
                return str(info.device)
        return None

    def _connect(self) -> None:
        """Manual Connect button: sim if ticked, else the selected (or a Pico) port."""
        if self.connected or self._connecting:
            return
        if self.sim_check.isChecked():
            self._begin_connect_sim()
            return
        device = self.port_combo.currentData() or self._find_pico()
        if not device:
            self._set_conn_status("no port selected")
            return
        self._begin_connect_serial(str(device))

    def _begin_connect_sim(self) -> None:
        from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

        self._begin_connect(SimulatedTransport(GrblHalSimulator(), status_interval=0.1), "sim")

    def _begin_connect_serial(self, device: str) -> None:
        self._begin_connect(SerialTransport(), device)

    def _begin_connect(self, transport: Any, target: str) -> None:
        self._connecting = True
        self._set_conn_status(f"connecting to {target}...")
        self.controller = RealController(
            transport,
            status_rate_hz=10,
            max_missed_status=50,   # tolerate ~5 s of silence (homing seeks block status reports)
            command_timeout=60.0,   # $H only acks when the whole homing cycle completes
        )
        self.facade = Facade(self.controller, profile=self._build_profile())
        self.log(f"connecting to {target} ...")
        self.bridge.submit(
            self.facade.connect(target),
            on_ok=lambda _r: self._mark_connected(target),
            on_err=lambda exc: self._connect_failed(target, exc),
        )

    def _mark_connected(self, target: str) -> None:
        self._connecting = False
        self.connected = True
        self.log(f"connected to {target}")
        self._set_conn_status(f"connected: {target}")

    def _connect_failed(self, target: str, exc: Exception) -> None:
        self._connecting = False
        self.connected = False
        self.controller = None
        self.facade = None
        self.log(f"connect to {target} failed: {type(exc).__name__}: {exc}")
        self._set_conn_status("connect failed - retrying" if self.auto_check.isChecked() else "connect failed")

    def _disconnect(self) -> None:
        # An explicit Disconnect also stops auto-connect, so it doesn't reconnect.
        self.auto_check.setChecked(False)
        if self.facade is not None:
            self.bridge.submit(self.facade.disconnect(), on_ok=lambda _r: self._mark_off())
        else:
            self._mark_off()

    def _mark_off(self) -> None:
        self.connected = False
        self._connecting = False
        self.controller = None
        self.facade = None
        self.log("disconnected")
        self._set_conn_status("disconnected")
        self._clear_dashboard()

    def _on_connection_lost(self) -> None:
        """The controller dropped on its own (unplugged / missed status)."""
        self.connected = False
        self._connecting = False
        self.controller = None
        self.facade = None
        self.log("connection lost")
        self._set_conn_status("connection lost - searching..." if self.auto_check.isChecked() else "connection lost")
        self._clear_dashboard()

    def _set_conn_status(self, text: str) -> None:
        if self.conn_status.text() != text:
            self.conn_status.setText(text)

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
        self.fields["transport.default_port_windows"].setText(cfg.transport.default_port_windows)
        self.fields["transport.default_port_linux"].setText(cfg.transport.default_port_linux)
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
                default_port_windows=self.fields["transport.default_port_windows"].text(),
                default_port_linux=self.fields["transport.default_port_linux"].text(),
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
        if line.replace(" ", "") == "$$":
            # $$ returns many "$n=value" lines that the controller parses into its
            # settings buffer rather than echoing to the console (so only the final
            # "ok" would show). Read them back and print them here.
            self.bridge.submit(
                self._facade().read_settings(),
                on_ok=self._dump_settings,
                on_err=lambda exc: self.log(f"  {type(exc).__name__}: {exc}"),
            )
            return
        self.bridge.submit(
            self._facade().run_line(line),
            on_ok=lambda _r: self.log("  ok"),
            on_err=lambda exc: self.log(f"  {type(exc).__name__}: {exc}"),
        )

    def _dump_settings(self, settings: Any) -> None:
        for key in sorted(settings.values):
            self.log(f"  ${key}={settings.values[key]}")
        self.log(f"  ok ({len(settings.values)} settings)")

    # =================================================================== status
    def _poll_status(self) -> None:
        if self.controller is None:
            return
        # The controller drops itself on unplug / missed status; notice it
        # here and let the auto-connect loop recover — no dialog, no stale screen.
        if self.connected and not self.controller.is_connected:
            self._on_connection_lost()
            return
        if not self.connected:
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

    def _clear_dashboard(self) -> None:
        self.state_label.setText("Disconnected")
        self.mpos_label.setText("MPos: -")
        self.wpos_label.setText("WPos: -")
        self.fs_label.setText("Feed/Spindle: -    Ov: -")
        self._update_signals(None)

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
            QMessageBox.warning(self, "Not connected", "Connect from the top bar first.")
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


def _apply_dark_theme(app: QApplication) -> None:
    """A calm dark theme; danger/start buttons are tinted via object names."""
    app.setStyle("Fusion")
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
        """
        QGroupBox { border: 1px solid #1f2937; border-radius: 6px; margin-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px;
                           color: #93c5fd; font-weight: bold; }
        QPushButton { background: #1f2937; padding: 6px 10px; border-radius: 4px; }
        QPushButton:hover { background: #334155; }
        QPushButton#danger { background: #7f1d1d; color: white; font-weight: bold; }
        QPushButton#danger:hover { background: #991b1b; }
        QPushButton#start { background: #14532d; color: white; font-weight: bold; }
        QPushButton#start:hover { background: #166534; }
        QLineEdit, QPlainTextEdit, QTextEdit, QComboBox { background: #020617;
                   border: 1px solid #1f2937; border-radius: 4px; padding: 2px; }
        QTabBar::tab { background: #111827; color: #e5e7eb; padding: 7px 13px; }
        QTabBar::tab:selected { background: #1f2937; color: #93c5fd; }
        QProgressBar { border: 1px solid #1f2937; border-radius: 4px; text-align: center; }
        QProgressBar::chunk { background: #2563eb; }
        """
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="cncctl desktop GUI (Qt)")
    parser.add_argument("--config", type=Path, default=Path("config/machine.toml"))
    args = parser.parse_args()

    app = QApplication(sys.argv)
    _apply_dark_theme(app)
    window = MainWindow(args.config)
    window.resize(1180, 860)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
