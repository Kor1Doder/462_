"""Tier-2 integration: the M9 file sender end-to-end through the simulator."""

from __future__ import annotations

from pathlib import Path

import pytest

from cncctl.controller.errors import SoftLimitError
from cncctl.controller.real import RealController
from cncctl.controller.state import MachineState
from cncctl.facade import Facade, MachineProfile
from cncctl.viz.analyze import SoftLimits
from cncctl.viz.simulate import Kinematics
from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

pytestmark = pytest.mark.integration


def _profile(*, x_max: float = 500.0) -> MachineProfile:
    return MachineProfile(
        soft_limits=SoftLimits(x=(-1.0, x_max), y=(-1.0, 500.0), z=(-110.0, 10.0)),
        kinematics=Kinematics(max_rate_mm_min=3000.0),
    )


async def test_send_program_file_through_real_controller(tmp_path: Path) -> None:
    sim = GrblHalSimulator()
    controller = RealController(SimulatedTransport(sim, ack_delay=0.0001), status_rate_hz=50)
    facade = Facade(controller, profile=_profile())
    await facade.connect("sim")

    path = tmp_path / "prog.nc"
    body = "\n".join(f"G1 X{i % 50}.0 Y{i % 40}.0 F600" for i in range(200))
    path.write_text("G90\n" + body + "\nM30\n", encoding="utf-8")

    progress = [p async for p in facade.send_program(path)]

    assert len(progress) == 202  # G90 + 200 moves + M30
    assert progress[-1].total == 202
    assert sim.received_lines[-1] == "M30"
    assert controller.state is MachineState.IDLE
    await facade.disconnect()


async def test_send_program_rejected_out_of_bounds_sends_nothing(tmp_path: Path) -> None:
    sim = GrblHalSimulator()
    controller = RealController(SimulatedTransport(sim))
    facade = Facade(controller, profile=_profile(x_max=10.0))
    await facade.connect("sim")

    path = tmp_path / "big.nc"
    path.write_text("G90\nG1 X100 F600\n", encoding="utf-8")

    before = list(sim.received_lines)
    with pytest.raises(SoftLimitError):
        _ = [p async for p in facade.send_program(path)]
    assert sim.received_lines == before  # no program line reached the device
    await facade.disconnect()
