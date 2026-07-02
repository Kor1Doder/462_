"""M9 HIL smoke: send a (motion-free) program through to the real machine.

Fulfills the M9 done-criterion "can send a real program on both OSes". Opt-in
and hardware-gated. The default program is intentionally
**motion-free** (units/comment/M30) so the smoke exercises the streaming + ack
path end-to-end without commanding the machine to move; point ``CNCCTL_PROGRAM``
at a real file to send an actual toolpath.

Run, e.g.:
  CNCCTL_HIL=1 CNCCTL_PORT=/dev/ttyACM0 uv run pytest tests/hil/test_send_program.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cncctl.controller.real import RealController
from cncctl.facade import Facade, MachineProfile
from cncctl.transport.serial_transport import SerialTransport
from cncctl.viz.analyze import SoftLimits
from cncctl.viz.simulate import Kinematics

pytestmark = pytest.mark.hil

_ABORT_SECONDS = 5
_MOTION_FREE_PROGRAM = "(cncctl HIL smoke)\nG21\nG90\nM30\n"


async def test_send_program_to_real_machine(tmp_path: Path) -> None:
    if os.environ.get("CNCCTL_HIL") != "1":
        pytest.skip("HIL tests require CNCCTL_HIL=1 and real hardware connected.")
    port = os.environ.get("CNCCTL_PORT")
    if not port:
        pytest.skip("Set CNCCTL_PORT to the grblHAL device (e.g. COM3 or /dev/ttyACM0).")

    program_path = Path(os.environ["CNCCTL_PROGRAM"]) if "CNCCTL_PROGRAM" in os.environ else None
    if program_path is None:
        program_path = tmp_path / "smoke.nc"
        program_path.write_text(_MOTION_FREE_PROGRAM, encoding="utf-8")

    print(
        f"\n*** HIL: sending {program_path} to {port} in {_ABORT_SECONDS}s — Ctrl-C to abort. ***"
    )
    await asyncio.sleep(_ABORT_SECONDS)

    # A generous envelope — the default program does not move, and a custom one
    # is the operator's responsibility.
    profile = MachineProfile(
        soft_limits=SoftLimits(x=(-1000.0, 1000.0), y=(-1000.0, 1000.0), z=(-1000.0, 1000.0)),
        kinematics=Kinematics(max_rate_mm_min=1000.0),
    )
    facade = Facade(RealController(SerialTransport()), profile=profile)
    await facade.connect(port)
    try:
        progress = [p async for p in facade.send_program(program_path)]
        assert progress
        assert progress[-1].sent == progress[-1].total
    finally:
        await facade.disconnect()
