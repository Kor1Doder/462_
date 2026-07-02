"""Camera tab — live monitoring of the machine from a Pi camera or USB webcam.

**Monitoring only.** The tool position shown in the corner overlay comes from the
controller's status reports (`MPos`), *not* from the image — grblHAL already
knows where the tool is to step precision, far better than vision could estimate,
so the camera never closes any control loop. It is there to *watch* the cut
(spindle, axes, chips), not to measure.

Backends, auto-detected in this order:
  * **Picamera2** — Raspberry Pi CSI camera (Camera Module 3). Installed via apt
    on the Pi (`python3-picamera2`), so it is imported only if present.
  * **OpenCV** — any USB webcam via ``cv2.VideoCapture``. Ships aarch64 wheels;
    lives in the ``gui`` optional extra (``opencv-python-headless``).

If neither import is available the tab degrades to an install hint, exactly like
the 3D workpiece view does without its extra.
"""

from __future__ import annotations

import threading
import urllib.request
from typing import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:  # Pi CSI camera — only present on a Pi with python3-picamera2 installed
    from picamera2 import Picamera2

    _HAS_PICAMERA2 = True
except Exception:  # noqa: BLE001 - import can fail for more than ImportError off-Pi
    _HAS_PICAMERA2 = False

try:  # USB webcams (and the array<->QImage path needs numpy, which cv2 pulls in)
    import cv2
    import numpy as np

    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

CAMERA_AVAILABLE = _HAS_PICAMERA2 or _HAS_CV2


class _ClickLabel(QLabel):
    """A QLabel that reports where it was clicked (in its own pixel coords)."""

    clicked = Signal(int, int)

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802 - Qt override
        self.clicked.emit(int(ev.position().x()), int(ev.position().y()))


class _PicameraSource:
    """Pi CSI camera; ``read()`` returns an HxWx3 RGB uint8 array."""

    def __init__(self) -> None:
        self._cam = Picamera2()
        self._cam.configure(self._cam.create_preview_configuration(
            main={"format": "RGB888", "size": (1280, 720)}))
        self._cam.start()

    def read(self):  # noqa: ANN201
        return self._cam.capture_array()  # already RGB888

    def close(self) -> None:
        self._cam.stop()
        self._cam.close()


class _CvSource:
    """USB webcam via OpenCV; converts BGR -> RGB so colours are correct."""

    def __init__(self, index: int) -> None:
        self._cap = cv2.VideoCapture(index)
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    def read(self):  # noqa: ANN201
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def is_open(self) -> bool:
        return bool(self._cap.isOpened())

    def close(self) -> None:
        self._cap.release()


class _MjpegSource:
    """Network MJPEG stream (e.g. the Pi's camera over WiFi). Pure Qt/stdlib — no
    OpenCV — so it works from a PC even without a local camera backend.

    ``read()`` returns the latest frame as a ``QImage`` (Qt decodes the JPEG),
    hence the aligner (which needs a numpy array) is disabled in network mode;
    this is a monitoring view.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._latest: bytes | None = None
        self._lock = threading.Lock()
        self._stop = False
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            with urllib.request.urlopen(self._url, timeout=6) as resp:  # noqa: S310
                buf = b""
                while not self._stop:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    buf += chunk
                    while True:  # pull out every complete JPEG (FFD8…FFD9)
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2)
                        if start < 0 or end < 0:
                            break
                        with self._lock:
                            self._latest = buf[start : end + 2]
                        buf = buf[end + 2 :]
                    if len(buf) > 5_000_000:  # runaway guard (never got a full frame)
                        buf = b""
        except Exception as exc:  # noqa: BLE001 - any network/URL failure is non-fatal
            self.error = str(exc)

    def read(self) -> QImage | None:
        with self._lock:
            jpg = self._latest
        if not jpg:
            return None
        img = QImage()
        img.loadFromData(jpg)
        return None if img.isNull() else img

    def close(self) -> None:
        self._stop = True


class CameraWidget(QWidget):
    """Live camera view with an optional machine-position overlay.

    The host GUI calls :meth:`set_position` from its status poll so the live
    ``MPos`` is drawn over the video; nothing here reads position from pixels.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: _PicameraSource | _CvSource | _MjpegSource | None = None
        self._overlay = ""  # "MPos X.. Y.. Z.." pushed in by the GUI
        self._frame = None  # latest RGB ndarray, for the visual aligner
        self._target: tuple[float, float] | None = None  # clicked goal, frame px
        self._tool: tuple[float, float] | None = None  # tracked tool, frame px
        self._align_status = ""
        self._abort = False
        # The host GUI owns motion, so it provides the auto-align action.
        self.on_align: Callable[[], None] | None = None
        # Map from the displayed (scaled, centred) pixmap back to frame pixels.
        self._disp = (0.0, 0.0, 1.0, 1.0)  # ox, oy, scale_x, scale_y
        self._build_ui()

    # -- UI ------------------------------------------------------------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        controls = QHBoxLayout()
        self._start_btn = QPushButton("Start camera")
        self._start_btn.clicked.connect(self._toggle)
        controls.addWidget(self._start_btn)

        # USB device picker (CSI has a single fixed camera, so hide it there).
        self._device = QComboBox()
        for i in range(4):
            self._device.addItem(f"USB camera {i}", i)
        controls.addWidget(QLabel("Device:"))
        controls.addWidget(self._device)
        if _HAS_PICAMERA2:  # prefer the CSI camera; the index is irrelevant then
            self._device.setEnabled(False)
            self._device.hide()

        self._status = QLabel("idle")
        controls.addWidget(self._status, 1)
        outer.addLayout(controls)

        # Network camera: watch the Pi's camera from a PC over WiFi. Works with any
        # MJPEG stream (mjpg-streamer / motion / a libcamera-vid|ffmpeg pipe). No
        # local backend needed, so this is available even when CAMERA_AVAILABLE is
        # False — and it never errors when there is no camera (just says so).
        net = QHBoxLayout()
        net.addWidget(QLabel("Pi cam URL:"))
        self._net_url = QLineEdit("http://raspberrypi.local:8080/?action=stream")
        net.addWidget(self._net_url, 1)
        self._net_btn = QPushButton("View Pi cam (network)")
        self._net_btn.clicked.connect(self._toggle_network)
        net.addWidget(self._net_btn)
        outer.addLayout(net)

        self._view = _ClickLabel("Camera off")
        self._view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._view.setMinimumSize(480, 270)
        self._view.setStyleSheet("background:#111; color:#888;")
        self._view.clicked.connect(self._on_click)
        outer.addWidget(self._view, 1)

        # Experimental visual aligner: click a target, then let the tool drive
        # itself there as far as it reliably can; finish by hand. Motion runs in
        # the GUI (caps + abort + safety live there) via the on_align callback.
        align = QHBoxLayout()
        self._align_btn = QPushButton("Auto-align tool → target (experimental)")
        self._align_btn.clicked.connect(self._request_align)
        self._abort_btn = QPushButton("Abort")
        self._abort_btn.clicked.connect(self.request_abort)
        clear = QPushButton("Clear target")
        clear.clicked.connect(self._clear_target)
        self._align_label = QLabel("click the image to set a target")
        for w in (self._align_btn, self._abort_btn, clear):
            align.addWidget(w)
        align.addWidget(self._align_label, 1)
        outer.addLayout(align)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._grab)

        if not CAMERA_AVAILABLE:
            # No *local* backend — but the network Pi-cam viewer still works, and
            # the rest of the app is unaffected (camera is fully optional).
            self._start_btn.setEnabled(False)
            self._align_btn.setEnabled(False)
            self._view.setText(
                "No local camera backend — use 'View Pi cam (network)' above to\n"
                "watch the Pi's camera over WiFi.\n\n"
                "Local USB webcam:  uv sync --extra gui   (installs opencv)\n"
                "Pi CSI cam:  sudo apt install python3-picamera2")

    # -- start / stop --------------------------------------------------------
    def _toggle(self) -> None:
        if self._source is None:
            self._open()
        else:
            self._close()

    def _open(self) -> None:
        try:
            if _HAS_PICAMERA2:
                self._source = _PicameraSource()
                self._status.setText("CSI camera (Picamera2)")
            else:
                idx = int(self._device.currentData())
                src = _CvSource(idx)
                if not src.is_open():
                    src.close()
                    self._status.setText(f"could not open USB camera {idx}")
                    return
                self._source = src
                self._status.setText(f"USB camera {idx} (OpenCV)")
        except Exception as exc:  # noqa: BLE001 - surface any backend failure in-UI
            self._source = None
            self._status.setText(f"camera error: {exc}")
            return
        self._start_btn.setText("Stop camera")
        self._timer.start(33)  # ~30 fps

    def _toggle_network(self) -> None:
        if isinstance(self._source, _MjpegSource):
            self._close()
            return
        if self._source is not None:  # a local camera is running — stop it first
            self._close()
        url = self._net_url.text().strip()
        if not url:
            self._status.setText("enter the Pi camera URL first")
            return
        self._source = _MjpegSource(url)
        self._net_btn.setText("Stop Pi cam")
        self._status.setText(f"connecting to {url} …")
        self._timer.start(50)  # ~20 fps; network-bound anyway

    def _close(self) -> None:
        self._timer.stop()
        if self._source is not None:
            self._source.close()
            self._source = None
        self._start_btn.setText("Start camera")
        self._net_btn.setText("View Pi cam (network)")
        self._status.setText("idle")
        self._view.setText("Camera off")

    # -- per-frame -----------------------------------------------------------
    def _grab(self) -> None:
        src = self._source
        if src is None:
            return
        frame = src.read()
        if frame is None:  # no frame yet, or the network cam is unreachable
            if isinstance(src, _MjpegSource) and src.error:
                self._status.setText(f"no camera / unreachable: {src.error}")
            return
        if isinstance(frame, QImage):  # network MJPEG (no numpy array → aligner off)
            self._frame = None
            w, h = frame.width(), frame.height()
            pix = QPixmap.fromImage(frame)
            self._status.setText("Pi cam (network)")
        else:  # local backend: RGB ndarray
            self._frame = np.ascontiguousarray(frame)
            h, w, _ = self._frame.shape
            img = QImage(self._frame.data, w, h, 3 * w, QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(img.copy())  # copy() detaches from the numpy buffer
        pix = pix.scaled(self._view.size(), Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        # Record the frame->display mapping so clicks and markers line up.
        sx, sy = pix.width() / w, pix.height() / h
        self._disp = (0.0, 0.0, sx, sy)
        self._draw_overlay(pix, sx, sy)
        self._view.setPixmap(pix)

    def _draw_overlay(self, pix: QPixmap, sx: float, sy: float) -> None:
        painter = QPainter(pix)
        if self._overlay:  # live MPos in the corner
            painter.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
            rect = painter.fontMetrics().boundingRect(self._overlay)
            painter.fillRect(6, 6, rect.width() + 12, rect.height() + 8, QColor(0, 0, 0, 160))
            painter.setPen(QColor("#22c55e"))
            painter.drawText(12, 8 + rect.height(), self._overlay)
        if self._target is not None:  # goal crosshair (where the tool should go)
            self._cross(painter, self._target, sx, sy, QColor("#22c55e"), 12)
        if self._tool is not None:  # tracked tool position
            self._cross(painter, self._tool, sx, sy, QColor("#f97316"), 9)
        if self._align_status:
            painter.setPen(QColor("#fbbf24"))
            painter.setFont(QFont("Consolas", 12, QFont.Weight.Bold))
            painter.drawText(8, pix.height() - 10, self._align_status)
        painter.end()

    @staticmethod
    def _cross(painter: QPainter, pt: tuple[float, float], sx: float, sy: float,
               color: QColor, r: int) -> None:
        x, y = int(pt[0] * sx), int(pt[1] * sy)
        painter.setPen(QPen(color, 2))
        painter.drawLine(x - r, y, x + r, y)
        painter.drawLine(x, y - r, x, y + r)

    def _on_click(self, cx: int, cy: int) -> None:
        """Map a click on the (centred, scaled) view back to frame pixels."""
        if self._frame is None:
            return
        # The pixmap is centred in the label; undo the centring offset.
        pm = self._view.pixmap()
        if pm is None or pm.isNull():
            return
        ox = (self._view.width() - pm.width()) / 2.0
        oy = (self._view.height() - pm.height()) / 2.0
        _, _, sx, sy = self._disp
        fx, fy = (cx - ox) / sx, (cy - oy) / sy
        h, w, _ = self._frame.shape
        if 0 <= fx < w and 0 <= fy < h:
            self._target = (fx, fy)
            self._align_label.setText(f"target set @ ({fx:.0f}, {fy:.0f}) px")

    def _clear_target(self) -> None:
        self._target = None
        self._align_status = ""
        self._align_label.setText("click the image to set a target")

    def _request_align(self) -> None:
        if self._target is None:
            self._align_label.setText("set a target first — click the image")
            return
        self._abort = False
        if self.on_align is not None:
            self.on_align()

    # -- API used by the GUI's status poll and the align coroutine -----------
    def set_position(self, text: str) -> None:
        """Update the corner overlay with the controller's live MPos string."""
        self._overlay = text

    def latest_frame(self):  # noqa: ANN201 - ndarray | None
        """Most recent RGB frame (a copy is taken by the caller if needed)."""
        return self._frame

    def target_pixel(self) -> tuple[float, float] | None:
        return self._target

    def set_tool_marker(self, pt: tuple[float, float] | None) -> None:
        self._tool = pt

    def set_align_status(self, text: str) -> None:
        self._align_status = text
        self._align_label.setText(text)

    def request_abort(self) -> None:
        self._abort = True

    def align_aborted(self) -> bool:
        return self._abort

    def shutdown(self) -> None:
        """Release the camera (call on window close)."""
        self._close()
