"""Calibration tests (M11): steps/mm math + flow, backlash, squaring."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from cncctl.calibration.backlash import GUIDE_STEPS, backlash_mm, run_backlash_guide
from cncctl.calibration.squaring import diagonal_difference_mm
from cncctl.calibration.steps_per_mm import (
    corrected_steps_per_mm,
    propose_steps,
    run_steps_calibration,
    setting_key_for_axis,
)
from cncctl.controller.errors import CalibrationError
from cncctl.controller.fake import FakeController
from cncctl.controller.messages import Axis
from cncctl.facade import Facade


# -- steps/mm math -----------------------------------------------------------
def test_corrected_steps_for_under_travel() -> None:
    # Commanded 100, measured 99.2 -> more steps/mm needed.
    assert corrected_steps_per_mm(100.0, 100.0, 99.2) == pytest.approx(100.80645, abs=1e-4)


def test_corrected_steps_for_over_travel() -> None:
    assert corrected_steps_per_mm(100.0, 100.0, 100.8) < 100.0


@pytest.mark.parametrize(
    ("current", "commanded", "measured"),
    [(0.0, 100.0, 99.0), (100.0, 0.0, 99.0), (100.0, 100.0, 0.0), (100.0, 100.0, -1.0)],
)
def test_corrected_steps_rejects_nonpositive(
    current: float, commanded: float, measured: float
) -> None:
    with pytest.raises(CalibrationError):
        corrected_steps_per_mm(current, commanded, measured)


def test_setting_keys_per_axis() -> None:
    assert setting_key_for_axis(Axis.X) == 100
    assert setting_key_for_axis(Axis.Y) == 101
    assert setting_key_for_axis(Axis.Z) == 102


# -- steps/mm flow against FakeController -------------------------------------
async def _facade(settings: Mapping[int, str]) -> tuple[Facade, FakeController]:
    controller = FakeController(settings=settings)
    facade = Facade(controller)
    await facade.connect("COM-TEST")
    return facade, controller


async def test_flow_applies_on_confirm() -> None:
    facade, _ = await _facade({100: "100.000"})
    out: list[str] = []
    applied = await run_steps_calibration(
        facade, Axis.X, commanded=100.0, measured=99.2, confirm=lambda _p: True, emit=out.append
    )
    assert applied
    assert (await facade.read_settings()).get(100) == "100.806"
    assert any("wrote and verified" in line for line in out)


async def test_flow_aborts_on_decline() -> None:
    facade, _ = await _facade({100: "100.000"})
    out: list[str] = []
    applied = await run_steps_calibration(
        facade, Axis.X, commanded=100.0, measured=99.2, confirm=lambda _p: False, emit=out.append
    )
    assert not applied
    assert (await facade.read_settings()).get(100) == "100.000"  # unchanged
    assert any("aborted" in line for line in out)


async def test_propose_steps_missing_setting_raises() -> None:
    facade, _ = await _facade({})
    with pytest.raises(CalibrationError, match="not present"):
        await propose_steps(facade, Axis.X, 100.0, 99.2)


async def test_propose_steps_non_numeric_raises() -> None:
    facade, _ = await _facade({100: "oops"})
    with pytest.raises(CalibrationError, match="numeric"):
        await propose_steps(facade, Axis.X, 100.0, 99.2)


async def test_proposal_value_formatting() -> None:
    facade, _ = await _facade({100: "250.000"})
    proposal = await propose_steps(facade, Axis.X, 100.0, 100.0)  # identity -> unchanged
    assert proposal.setting_key == 100
    assert proposal.value == "250.000"


# -- backlash ----------------------------------------------------------------
def test_backlash_mm_accepts_nonnegative() -> None:
    assert backlash_mm(0.05) == 0.05
    assert backlash_mm(0.0) == 0.0


def test_backlash_mm_rejects_negative() -> None:
    with pytest.raises(CalibrationError):
        backlash_mm(-0.1)


def test_run_backlash_guide_reports_measurement() -> None:
    out: list[str] = []
    value = run_backlash_guide(Axis.Y, measure=lambda: 0.08, emit=out.append)
    assert value == pytest.approx(0.08)
    assert len(out) == len(GUIDE_STEPS) + 2  # steps + result + apply note
    assert any("Y" in line for line in out)


# -- squaring ----------------------------------------------------------------
def test_diagonal_difference() -> None:
    assert diagonal_difference_mm(141.42, 141.42) == pytest.approx(0.0)
    assert diagonal_difference_mm(141.5, 141.4) == pytest.approx(0.1, abs=1e-9)


def test_diagonal_difference_rejects_nonpositive() -> None:
    with pytest.raises(CalibrationError):
        diagonal_difference_mm(0.0, 100.0)
