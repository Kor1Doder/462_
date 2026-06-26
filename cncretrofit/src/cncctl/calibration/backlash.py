"""Backlash measurement — a guided protocol (CLAUDE.md §7 M11).

Backlash (lost motion at reversal) is measured with a dial indicator. This
module guides the operator through the measurement and reports the value; it
does not write a setting, because grblHAL's backlash compensation is build-
specific and CLAUDE.md §11 forbids guessing setting numbers.
"""

from __future__ import annotations

from collections.abc import Callable

from cncctl.controller.errors import CalibrationError
from cncctl.controller.messages import Axis

GUIDE_STEPS: tuple[str, ...] = (
    "1. Mount a dial indicator against the {axis} axis.",
    "2. Jog {axis}+ a few mm to take up slack, then zero the indicator.",
    "3. Jog {axis}- by the test distance, then jog {axis}+ back the same distance.",
    "4. Read the indicator: the residual offset from zero is the backlash.",
)


def backlash_mm(lost_motion_mm: float) -> float:
    """Validate and return the measured lost motion.

    Raises:
        CalibrationError: the measurement is negative.
    """
    if lost_motion_mm < 0:
        raise CalibrationError(f"backlash (lost motion) cannot be negative, got {lost_motion_mm}")
    return lost_motion_mm


def run_backlash_guide(
    axis: Axis,
    *,
    measure: Callable[[], float],
    emit: Callable[[str], None],
) -> float:
    """Print the guided measurement protocol, read the measurement, report it."""
    for step in GUIDE_STEPS:
        emit(step.format(axis=axis.value))
    value = backlash_mm(measure())
    emit(f"measured backlash on {axis.value}: {value:.3f} mm")
    emit("Apply it via your grblHAL build's backlash-compensation setting (build-specific).")
    return value


__all__ = ["GUIDE_STEPS", "backlash_mm", "run_backlash_guide"]
