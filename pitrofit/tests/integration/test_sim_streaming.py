"""Tier-2 integration: stream a program through the grblHAL simulator (M7).

Wires the real components together in-memory — CharacterCountingStreamer ->
SimulatedTransport -> GrblHalSimulator, with a reader that parses responses and
routes acks back to the streamer. This is the proto-form of M5's RealController
loop and proves the simulator carries a full program with correct character-
counting back-pressure. Runs on both OSes with no serial hardware.
"""

from __future__ import annotations

import asyncio

import pytest

from cncctl.controller.messages import Error, Ok
from cncctl.controller.state import MachineState
from cncctl.protocol.inbound import parse_line
from cncctl.streamer import line_source
from cncctl.streamer.character_counter import CharacterCountingStreamer
from tools.grblhal_sim import GrblHalSimulator, SimulatedTransport

pytestmark = pytest.mark.integration


async def test_stream_program_through_simulator() -> None:
    sim = GrblHalSimulator()
    # A small ack delay keeps several lines outstanding, exercising the
    # character-counting back-pressure against a deliberately small buffer.
    transport = SimulatedTransport(sim, ack_delay=0.0005)
    await transport.open("sim")
    streamer = CharacterCountingStreamer(buffer_size=64, send_line=transport.send_line)

    acks = 0

    async def reader() -> None:
        nonlocal acks
        async for raw in transport.read_lines():
            message = parse_line(raw)
            if isinstance(message, (Ok, Error)):
                await streamer.acknowledge()
                acks += 1

    reader_task = asyncio.create_task(reader())

    program = [f"G1 X{i}.000 Y{i}.000 F500" for i in range(100)]
    progress = [p async for p in streamer.stream(line_source.from_lines(program))]

    assert len(progress) == 100
    assert progress[-1].sent == 100
    assert sim.received_lines == program  # device received every line, in order
    assert streamer.bytes_outstanding == 0  # fully drained
    assert acks == 100
    assert sim.state is MachineState.IDLE

    reader_task.cancel()
    await transport.close()
