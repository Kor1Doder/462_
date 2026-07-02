"""M11 HIL smoke: steps-per-mm calibration end-to-end on the real machine.

Fulfills the M11 done-criterion. Opt-in and hardware-gated (CLAUDE.md §6 Tier
3). To stay safe it uses ``commanded == measured`` (an identity calibration),
so it exercises the full read -> compute -> write -> verify flow while writing
the axis' existing ``$100`` value back unchanged. Persistence across power
cycles is the operator's manual check (re-read ``$$`` after a power cycle).

Run, e.g.:
  CNCCTL_HIL=1 CNCCTL_PORT=/dev/ttyACM0 uv run pytest tests/hil/test_calibrate_steps.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from cncctl.calibration.steps_per_mm import run_steps_calibration, setting_key_for_axis
from cncctl.controller.messages import Axis
from cncctl.controller.real import RealController
from cncctl.facade import Facade
from cncctl.transport.serial_transport import SerialTransport

pytestmark = pytest.mark.hil

_ABORT_SECONDS = 5


async def test_steps_calibration_round_trip() -> None:
    if os.environ.get("CNCCTL_HIL") != "1":
        pytest.skip("HIL tests require CNCCTL_HIL=1 and real hardware connected.")
    port = os.environ.get("CNCCTL_PORT")
    if not port:
        pytest.skip("Set CNCCTL_PORT to the grblHAL device (e.g. COM3 or /dev/ttyACM0).")

    print(f"\n*** HIL: steps calibration on {port} in {_ABORT_SECONDS}s — Ctrl-C to abort. ***")
    await asyncio.sleep(_ABORT_SECONDS)

    axis = Axis.X
    key = setting_key_for_axis(axis)
    facade = Facade(RealController(SerialTransport()))
    await facade.connect(port)
    try:
        before = (await facade.read_settings()).get(key)
        applied = await run_steps_calibration(
            facade,
            axis,
            commanded=100.0,
            measured=100.0,  # identity: writes the existing value back unchanged
            confirm=lambda _proposal: True,
            emit=print,
        )
        after = (await facade.read_settings()).get(key)
        assert applied
        assert after == before  # identity calibration leaves the value unchanged
        print(f"${key}: {before} -> {after}. Power-cycle and re-read $$ to confirm persistence.")
    finally:
        await facade.disconnect()
