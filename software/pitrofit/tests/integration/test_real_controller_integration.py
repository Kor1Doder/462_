"""Tier-2 integration: the full M5 session against the grblHAL simulator.

the design / done-criteria: "connect -> soft reset -> settings
round-trip -> 1000-line program -> assert no buffer overflow, all acks received,
final state Idle, continuous status reports." Runs in-memory on both OSes.
"""

from __future__ import annotations

import pytest

from cncctl.controller.real import RealController
from cncctl.controller.state import MachineState
from cncctl.streamer import line_source
from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

pytestmark = pytest.mark.integration


async def test_full_session_through_simulator() -> None:
    sim = GrblHalSimulator()
    # A small ack delay keeps several lines outstanding (real character-counting
    # back-pressure); auto status reports flow throughout, as a real device does.
    transport = SimulatedTransport(sim, ack_delay=0.0001, status_interval=0.02)
    controller = RealController(transport, status_rate_hz=20)

    # 1. connect
    await controller.connect("sim")
    assert controller.is_connected
    assert controller.state is MachineState.IDLE

    # 2. soft reset
    await controller.soft_reset()
    assert controller.state is MachineState.IDLE

    # 3. settings round-trip (write is verified by re-reading $$)
    await controller.write_setting(100, "251.000")
    assert (await controller.read_settings()).get(100) == "251.000"

    # 4. a 1000-line program
    program = [f"G1 X{i % 100}.000 F600" for i in range(1000)]
    progress = [p async for p in controller.send_program(line_source.from_lines(program))]

    # 5. assertions: every line sent in order; send_program returns only after
    #    the streamer drains (pending_count == 0), so all 1000 lines were acked.
    #    No buffer overflow — the streamer raises BufferOverflowError if it ever
    #    would, so reaching here proves the invariant held throughout.
    assert len(progress) == 1000
    assert progress[-1].sent == 1000
    assert sim.received_lines[-1000:] == program  # all lines received, in order
    assert controller.state is MachineState.IDLE
    assert controller.last_status is not None  # status reports flowed during the run

    await controller.disconnect()
    assert not controller.is_connected
