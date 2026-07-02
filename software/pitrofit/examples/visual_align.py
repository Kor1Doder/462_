"""Vision core for the experimental 'click a point, drive the tool there' aligner.

**Fixed-camera case:** the camera watches the table from a fixed spot and the
*tool* moves inside the image. We never trust vision for control precision — this
is an operator convenience to nudge the tool onto a clicked target, and every
motion it requests is capped and abortable in the GUI layer (see gui.py).

The loop is classic image-based visual servoing:

  1. **Calibrate** — jog a small known amount in +X then +Y, track how the tool
     moves in the image, and build a 2x2 Jacobian J (pixels per mm). This is the
     "1 mm = how many pixels, in which direction" mapping.
  2. **Servo** — locate the tool, take the pixel error to the clicked target,
     solve ``J · delta_mm = error_px`` for the millimetre jog that cancels it,
     move a fraction of it (gain < 1), and repeat until the error is tiny.

Tracking is template-matching on a patch grabbed during calibration: it does not
need to find the true tool *tip*, only a consistent point — the loop drives that
same tracked point onto the target, so consistency matters more than absolute
accuracy. Pure cv2 + numpy so it is unit-testable without a machine or a camera.
"""

from __future__ import annotations

import cv2
import numpy as np


def motion_centroid(before: np.ndarray, after: np.ndarray, *,
                    thresh: int = 20, min_area: float = 25.0) -> tuple[float, float] | None:
    """Centroid of the largest region that changed between two frames.

    With a fixed camera the only thing that moves is the tool, so the changed
    region localises it. Returns None if nothing moved enough (too small a jog,
    or the tool is out of frame)."""
    g1 = cv2.cvtColor(before, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(after, cv2.COLOR_RGB2GRAY)
    diff = cv2.GaussianBlur(cv2.absdiff(g1, g2), (5, 5), 0)
    _, mask = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    blob = max(contours, key=cv2.contourArea)
    if cv2.contourArea(blob) < min_area:
        return None
    m = cv2.moments(blob)
    if m["m00"] == 0:
        return None
    return (m["m10"] / m["m00"], m["m01"] / m["m00"])


def grab_template(frame: np.ndarray, center: tuple[float, float], half: int = 24) -> np.ndarray:
    """Square patch of ``frame`` centred on ``center`` (clamped to the edges)."""
    x, y = int(round(center[0])), int(round(center[1]))
    h, w = frame.shape[:2]
    x0, y0 = max(0, x - half), max(0, y - half)
    x1, y1 = min(w, x + half), min(h, y + half)
    return frame[y0:y1, x0:x1].copy()


def locate(frame: np.ndarray, template: np.ndarray) -> tuple[tuple[float, float], float]:
    """Best centre (x, y) of ``template`` in ``frame`` and its match score [0..1]."""
    res = cv2.matchTemplate(frame, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    th, tw = template.shape[:2]
    return (loc[0] + tw / 2.0, loc[1] + th / 2.0), float(score)


def jacobian(p0: tuple[float, float], px: tuple[float, float], py: tuple[float, float],
             dx_mm: float, dy_mm: float) -> np.ndarray:
    """Pixels-per-mm 2x2 from the tool pixel before/after a +X and a +Y jog.

    Columns are d(pixel)/dX and d(pixel)/dY, so ``J @ [dX, dY] = pixel shift``."""
    return np.array([
        [(px[0] - p0[0]) / dx_mm, (py[0] - p0[0]) / dy_mm],
        [(px[1] - p0[1]) / dx_mm, (py[1] - p0[1]) / dy_mm],
    ], dtype=float)


def mm_to_cancel(jac: np.ndarray, error_px: tuple[float, float]) -> tuple[float, float]:
    """Solve ``J · delta_mm = error_px`` -> the (dX, dY) mm that moves the tool by
    ``error_px``. Raises ``numpy.linalg.LinAlgError`` if the Jacobian is singular
    (degenerate calibration — e.g. the two jogs produced no measurable motion)."""
    dx, dy = np.linalg.solve(jac, np.array(error_px, dtype=float))
    return float(dx), float(dy)
