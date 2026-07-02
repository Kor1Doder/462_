"""Steps-per-mm calibration (CLAUDE.md §7 M11).

You command a known distance, measure the actual travel, and correct the axis'
``$100``/``$101``/``$102`` steps/mm. The flow is two-step (CLAUDE.md §7 M11):
propose the new value, require confirmation, then write and verify via ``$$``
(§8.7 — the controller's ``write_setting`` does the re-read/diff).

I/O is injected (``confirm`` / ``emit`` callables) so the flow is testable
without a terminal and the modules stay print-free (§9).
"""

from __future__ import annotations

from collections.abc import Callable

import msgspec

from cncctl.controller.errors import CalibrationError
from cncctl.controller.messages import Axis
from cncctl.facade import Facade

_SETTING_FOR_AXIS: dict[Axis, int] = {Axis.X: 100, Axis.Y: 101, Axis.Z: 102}


def setting_key_for_axis(axis: Axis) -> int:
    """Return the grbl steps/mm setting number for ``axis`` (``$100``/``$101``/``$102``)."""
    return _SETTING_FOR_AXIS[axis]


def corrected_steps_per_mm(current: float, commanded: float, measured: float) -> float:
    """Corrected steps/mm = ``current * commanded / measured``.

    Raises:
        CalibrationError: any input is not positive.
    """
    for name, value in (("current", current), ("commanded", commanded), ("measured", measured)):
        if value <= 0:
            raise CalibrationError(f"{name} must be > 0, got {value}")
    return current * commanded / measured


class StepsProposal(msgspec.Struct, frozen=True):
    """A proposed steps/mm correction for one axis."""

    axis: Axis
    setting_key: int
    current: float
    corrected: float
    commanded: float
    measured: float

    @property
    def value(self) -> str:
        """The proposed setting value as grbl would store it (3 decimals)."""
        return f"{self.corrected:.3f}"


async def propose_steps(
    facade: Facade, axis: Axis, commanded: float, measured: float
) -> StepsProposal:
    """Read the current steps/mm and compute the corrected proposal.

    Raises:
        CalibrationError: the setting is missing/non-numeric, or an input is invalid.
    """
    key = setting_key_for_axis(axis)
    raw = (await facade.read_settings()).get(key)
    if raw is None:
        raise CalibrationError(f"setting ${key} not present; cannot calibrate {axis.value}")
    try:
        current = float(raw)
    except ValueError as exc:
        raise CalibrationError(f"setting ${key} is not numeric: {raw!r}") from exc
    return StepsProposal(
        axis=axis,
        setting_key=key,
        current=current,
        corrected=corrected_steps_per_mm(current, commanded, measured),
        commanded=commanded,
        measured=measured,
    )


async def apply_steps(facade: Facade, proposal: StepsProposal) -> None:
    """Write the proposed setting (verified via ``$$`` by the controller, §8.7)."""
    await facade.write_setting(proposal.setting_key, proposal.value)


async def run_steps_calibration(
    facade: Facade,
    axis: Axis,
    *,
    commanded: float,
    measured: float,
    confirm: Callable[[StepsProposal], bool],
    emit: Callable[[str], None],
) -> bool:
    """Run the full two-step steps/mm calibration. Returns whether it was applied."""
    proposal = await propose_steps(facade, axis, commanded, measured)
    emit(f"axis {axis.value}: ${proposal.setting_key} current = {proposal.current:.3f} steps/mm")
    emit(f"  commanded {commanded:g} mm, measured {measured:g} mm")
    emit(f"  proposed  ${proposal.setting_key} = {proposal.value} steps/mm")
    if not confirm(proposal):
        emit("  aborted; no change written.")
        return False
    await apply_steps(facade, proposal)
    emit(f"  wrote and verified ${proposal.setting_key} = {proposal.value}")
    return True


__all__ = [
    "StepsProposal",
    "apply_steps",
    "corrected_steps_per_mm",
    "propose_steps",
    "run_steps_calibration",
    "setting_key_for_axis",
]
