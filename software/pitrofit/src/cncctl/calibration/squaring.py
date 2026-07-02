"""Axis squaring helper (CLAUDE.md §4).

Squaring matters for gantry / dual-motor axes. The EMCO retrofit has one motor
per axis, so this is not used operationally — it is provided for the standard
diagonal-measurement method should it ever be needed.
"""

from __future__ import annotations

from cncctl.controller.errors import CalibrationError


def diagonal_difference_mm(diagonal_a_mm: float, diagonal_b_mm: float) -> float:
    """Difference between the two measured diagonals of a nominal square.

    Zero means the two axes are square; a non-zero, signed value indicates the
    lean direction and magnitude of the out-of-square.

    Raises:
        CalibrationError: either diagonal is not positive.
    """
    if diagonal_a_mm <= 0 or diagonal_b_mm <= 0:
        raise CalibrationError("diagonals must be positive")
    return diagonal_a_mm - diagonal_b_mm


__all__ = ["diagonal_difference_mm"]
