"""grblHAL simulator tests (M7): device behavior + every output parses.

These validate that the Tier-2 simulator speaks the exact dialect the M3 parser
understands, so the integration suite is testing the real protocol path.
"""

from __future__ import annotations

import pytest

from cncctl.controller.errors import NotConnectedError
from cncctl.controller.messages import Ok, SettingLine, Status, Welcome
from cncctl.controller.state import MachineState
from cncctl.protocol.inbound import parse_line
from cncctl.protocol.realtime import Realtime
from tools.grblhal_sim import (
    DEFAULT_SETTINGS,
    GrblHalSimulator,
    SimulatedTransport,
    SimulatorConfig,
)


# -- simulator core ----------------------------------------------------------
def test_settings_dump_ends_with_ok_and_parses() -> None:
    sim = GrblHalSimulator()
    response = sim.process_line("$$")
    assert response[-1] == "ok"
    assert len(response) - 1 == len(DEFAULT_SETTINGS)
    for line in response[:-1]:
        assert isinstance(parse_line(line), SettingLine)


def test_gcode_line_is_acked_and_recorded() -> None:
    sim = GrblHalSimulator()
    assert sim.process_line("G0 X1") == ["ok"]
    assert sim.received_lines == ["G0 X1"]


def test_setting_write_is_reflected_in_dump() -> None:
    sim = GrblHalSimulator()
    assert sim.process_line("$100=260.000") == ["ok"]
    assert "$100=260.000" in sim.process_line("$$")


def test_soft_reset_returns_welcome_and_idle() -> None:
    sim = GrblHalSimulator()
    response = sim.process_realtime(Realtime.SOFT_RESET)
    assert len(response) == 1
    assert isinstance(parse_line(response[0]), Welcome)
    assert sim.state is MachineState.IDLE


def test_homing_required_resets_to_alarm_and_locks_motion() -> None:
    sim = GrblHalSimulator(SimulatorConfig(homing_required=True))
    sim.process_realtime(Realtime.SOFT_RESET)
    assert sim.state is MachineState.ALARM
    assert sim.process_line("G0 X1") == ["error:9"]  # locked out
    assert sim.process_line("$X")[-1] == "ok"
    assert sim.state is MachineState.IDLE
    assert sim.process_line("G0 X1") == ["ok"]


def test_homing_clears_alarm() -> None:
    sim = GrblHalSimulator(SimulatorConfig(homing_required=True))
    sim.process_realtime(Realtime.SOFT_RESET)
    assert sim.process_line("$H") == ["ok"]
    assert sim.state is MachineState.IDLE


def test_status_report_parses_as_status() -> None:
    msg = parse_line(GrblHalSimulator().status_report())
    assert isinstance(msg, Status)
    assert msg.state is MachineState.IDLE


def test_error_injection() -> None:
    sim = GrblHalSimulator(SimulatorConfig(error_lines={"G0 X999": 2}))
    assert sim.process_line("G0 X999") == ["error:2"]


def test_blank_line_is_acked() -> None:
    sim = GrblHalSimulator()
    assert sim.process_line("   ") == ["ok"]
    assert sim.received_lines == []  # blanks are not recorded


def test_jog_sets_jog_state_then_cancels() -> None:
    sim = GrblHalSimulator()
    assert sim.process_line("$J=G91 G21 X10 F500") == ["ok"]
    assert sim.state is MachineState.JOG
    assert sim.process_realtime(Realtime.JOG_CANCEL) == []
    assert sim.state is MachineState.IDLE


def test_jog_rejected_in_alarm() -> None:
    sim = GrblHalSimulator(SimulatorConfig(homing_required=True))
    sim.process_realtime(Realtime.SOFT_RESET)
    assert sim.process_line("$J=G91 X1 F100") == ["error:9"]


def test_jog_moves_position_and_completes_on_status_poll() -> None:
    sim = GrblHalSimulator()
    sim.process_line("$J=G91 G21 X10 Y-4 F500")
    assert sim.state is MachineState.JOG
    # a status poll completes the (instantaneous) jog and reports the new position
    report = parse_line(sim.process_realtime(Realtime.STATUS_REPORT)[0])
    assert sim.state is MachineState.IDLE
    assert isinstance(report, Status)
    assert (report.mpos.x, report.mpos.y) == (10.0, -4.0)
    # incremental: a second jog adds to the first
    sim.process_line("$J=G91 G21 X-3 F500")
    report = parse_line(sim.process_realtime(Realtime.STATUS_REPORT)[0])
    assert isinstance(report, Status)
    assert report.mpos.x == 7.0


@pytest.mark.parametrize(
    "byte",
    [Realtime.FEED_HOLD, Realtime.CYCLE_START, Realtime.FEED_OVR_RESET],
)
def test_realtime_without_effect_returns_nothing(byte: Realtime) -> None:
    sim = GrblHalSimulator()
    assert sim.process_realtime(byte) == []


def test_safety_door_realtime_sets_door_state() -> None:
    sim = GrblHalSimulator()
    assert sim.process_realtime(Realtime.SAFETY_DOOR) == []
    assert sim.state is MachineState.DOOR


def test_feed_bytes_splits_realtime_from_lines() -> None:
    sim = GrblHalSimulator()
    data = b"G0 X1\n" + bytes([Realtime.STATUS_REPORT]) + b"G1 Y2\n"
    out = sim.feed_bytes(data)
    assert out[0] == "ok"
    assert out[1].startswith("<")  # the status report from '?'
    assert out[2] == "ok"
    assert sim.received_lines == ["G0 X1", "G1 Y2"]


def test_every_simulator_output_parses() -> None:
    sim = GrblHalSimulator()
    outputs = [
        sim.welcome,
        sim.status_report(),
        *sim.process_line("$$"),
        *sim.process_line("$I"),
        *sim.process_line("$G"),
        *sim.process_line("$X"),
    ]
    for line in outputs:
        parse_line(line)  # must not raise


# -- SimulatedTransport ------------------------------------------------------
async def test_transport_emits_welcome_on_open() -> None:
    transport = SimulatedTransport()
    await transport.open("sim")
    first = await anext(transport.read_lines())
    assert isinstance(parse_line(first), Welcome)
    await transport.close()


async def test_transport_settings_roundtrip() -> None:
    transport = SimulatedTransport()
    await transport.open("sim")
    reader = transport.read_lines()
    await anext(reader)  # welcome

    await transport.send_line("$$")
    settings: list[SettingLine] = []
    while True:
        msg = parse_line(await anext(reader))
        if isinstance(msg, Ok):
            break
        assert isinstance(msg, SettingLine)
        settings.append(msg)
    assert len(settings) == len(DEFAULT_SETTINGS)
    await transport.close()


async def test_transport_status_on_realtime() -> None:
    transport = SimulatedTransport()
    await transport.open("sim")
    reader = transport.read_lines()
    await anext(reader)  # welcome
    await transport.send_realtime(Realtime.STATUS_REPORT)
    assert isinstance(parse_line(await anext(reader)), Status)
    await transport.close()


async def test_transport_send_before_open_raises() -> None:
    transport = SimulatedTransport()
    with pytest.raises(NotConnectedError):
        await transport.send_line("x")


async def test_transport_callable_ack_delay() -> None:
    transport = SimulatedTransport(ack_delay=lambda: 0.0)
    await transport.open("sim")
    reader = transport.read_lines()
    await anext(reader)  # welcome
    await transport.send_line("G0 X1")
    assert isinstance(parse_line(await anext(reader)), Ok)
    await transport.close()


async def test_transport_auto_status_reports() -> None:
    transport = SimulatedTransport(status_interval=0.001)
    await transport.open("sim")
    reader = transport.read_lines()
    await anext(reader)  # welcome
    # the background status task emits reports on its own
    assert isinstance(parse_line(await anext(reader)), Status)
    await transport.close()


async def test_transport_close_is_idempotent() -> None:
    transport = SimulatedTransport()
    await transport.open("sim")
    await transport.close()
    await transport.close()  # no raise
    assert not transport.is_open
