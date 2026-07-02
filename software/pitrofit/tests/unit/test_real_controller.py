"""RealController unit tests, driven by the in-memory grblHAL simulator (M5).

These are fast and deterministic (no serial hardware), exercising the
composition, ack routing, settings verification, safety gates, and the
missed-status disconnect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from cncctl.controller.errors import (
    CommandRejectedError,
    MachineNotReadyError,
    NotConnectedError,
    SettingsMismatchError,
)
from cncctl.controller.messages import Axis, Position, Status
from cncctl.controller.protocol import Controller
from cncctl.controller.real import RealController
from cncctl.controller.state import MachineState
from cncctl.streamer import line_source
from tools.grblhal_sim import (
    DEFAULT_SETTINGS,
    GrblHalSimulator,
    SimulatedTransport,
    SimulatorConfig,
)


async def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not met within the timeout")
        await asyncio.sleep(0.005)


async def _connect(**sim_kwargs: object) -> tuple[RealController, SimulatedTransport]:
    sim = GrblHalSimulator(SimulatorConfig(**sim_kwargs))  # type: ignore[arg-type]
    transport = SimulatedTransport(sim)
    controller = RealController(transport, status_rate_hz=100)
    await controller.connect("sim")
    return controller, transport


def test_real_controller_satisfies_protocol() -> None:
    assert isinstance(RealController(SimulatedTransport()), Controller)


def test_wco_derivation_fills_missing_position() -> None:
    # The controller caches WCO and derives whichever of MPos/WPos a report omits
    #. _on_status is the sync entry point the read loop calls.
    controller = RealController(SimulatedTransport())

    controller._on_status(
        Status(state=MachineState.IDLE, mpos=Position(10.0, 20.0, 5.0), wco=Position(1.0, 2.0, 3.0))
    )
    derived = controller.last_status
    assert derived is not None
    assert derived.wpos == Position(9.0, 18.0, 2.0)  # MPos - WCO

    # A later report carrying only WPos still yields MPos from the cached WCO.
    controller._on_status(Status(state=MachineState.IDLE, wpos=Position(9.0, 18.0, 2.0)))
    again = controller.last_status
    assert again is not None
    assert again.mpos == Position(10.0, 20.0, 5.0)  # WPos + cached WCO
    assert again.wco == Position(1.0, 2.0, 3.0)  # cached WCO attached to the report


async def test_connect_populates_settings_and_idle() -> None:
    controller, _ = await _connect()
    assert controller.is_connected
    assert controller.state is MachineState.IDLE
    assert controller.settings.get(100) == DEFAULT_SETTINGS[100]
    await controller.disconnect()
    assert not controller.is_connected


async def test_read_settings_returns_full_map() -> None:
    controller, _ = await _connect()
    settings = await controller.read_settings()
    assert len(settings.values) == len(DEFAULT_SETTINGS)
    await controller.disconnect()


async def test_write_setting_verifies_round_trip() -> None:
    controller, _ = await _connect()
    await controller.write_setting(100, "260.000")
    assert (await controller.read_settings()).get(100) == "260.000"
    await controller.disconnect()


async def test_write_setting_mismatch_raises() -> None:
    # SAFETY: the device acks the write but does not persist it.
    controller, _ = await _connect(persist_writes=False)
    with pytest.raises(SettingsMismatchError):
        await controller.write_setting(100, "260.000")
    await controller.disconnect()


async def test_home_sends_command() -> None:
    controller, transport = await _connect()
    await controller.home()
    assert "$H" in transport.simulator.received_lines
    await controller.disconnect()


async def test_rejected_command_raises() -> None:
    controller, _ = await _connect(error_lines={"$H": 5})
    with pytest.raises(CommandRejectedError):
        await controller.home()
    await controller.disconnect()


async def test_jog_rejected_when_alarmed() -> None:
    # SAFETY: motion gated on the observed state.
    controller, _ = await _connect(homing_required=True)
    await _wait_until(lambda: controller.state is MachineState.ALARM)
    with pytest.raises(MachineNotReadyError):
        await controller.jog(Axis.X, 5.0, 500.0)
    await controller.disconnect()


async def test_jog_when_idle_sends_jog_command() -> None:
    controller, transport = await _connect()
    await controller.jog(Axis.X, 5.0, 500.0)
    assert any(line.startswith("$J=") for line in transport.simulator.received_lines)
    await controller.disconnect()


async def test_realtime_commands_do_not_raise() -> None:
    controller, _ = await _connect()
    await controller.cancel_jog()
    await controller.feed_hold()
    await controller.resume()
    assert controller.is_connected
    await controller.disconnect()


async def test_send_program_rejected_when_alarmed() -> None:
    # SAFETY: a program cannot start unless Idle.
    controller, _ = await _connect(homing_required=True)
    await _wait_until(lambda: controller.state is MachineState.ALARM)
    with pytest.raises(MachineNotReadyError):
        _ = [p async for p in controller.send_program(line_source.from_lines(["G0 X1"]))]
    await controller.disconnect()


async def test_run_line_sends_and_acks() -> None:
    controller, transport = await _connect()
    await controller.run_line("G0 X1 Y2")
    assert "G0 X1 Y2" in transport.simulator.received_lines
    await controller.disconnect()


async def test_run_line_rejected_raises() -> None:
    controller, _ = await _connect(error_lines={"G0 X9": 3})
    with pytest.raises(CommandRejectedError):
        await controller.run_line("G0 X9")
    await controller.disconnect()


async def test_soft_reset_returns_to_idle() -> None:
    controller, _ = await _connect()
    await controller.soft_reset()
    assert controller.state is MachineState.IDLE
    await controller.disconnect()


async def test_status_stream_yields_then_ends_on_disconnect() -> None:
    controller, _ = await _connect()
    stream = controller.status_stream()
    status = await asyncio.wait_for(anext(stream), timeout=1.0)
    assert isinstance(status, Status)
    assert status.state is MachineState.IDLE
    await controller.disconnect()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=1.0)


async def test_missed_status_reports_disconnect() -> None:
    # SAFETY: N consecutive missed status reports => disconnect.
    transport = SimulatedTransport(drop_status=True)
    controller = RealController(transport, status_rate_hz=200, max_missed_status=3)
    await controller.connect("sim")
    await _wait_until(lambda: not controller.is_connected, timeout_s=2.0)
    assert not controller.is_connected


async def test_commands_before_connect_raise() -> None:
    controller = RealController(SimulatedTransport())
    with pytest.raises(NotConnectedError):
        await controller.read_settings()
    with pytest.raises(NotConnectedError):
        await controller.home()
