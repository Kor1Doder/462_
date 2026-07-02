"""Facade tests (M8): operator ops + §8 safety invariants, against FakeController."""

from __future__ import annotations

from pathlib import Path

import pytest

from cncctl.config_io import (
    AxesConfig,
    AxisConfig,
    Config,
    HomingConfig,
    MachineConfig,
    MotionConfig,
    TransportConfig,
    load_config,
)
from cncctl.controller.errors import ConfigError, MachineNotReadyError, SettingsMismatchError
from cncctl.controller.fake import FakeController
from cncctl.controller.messages import Axis
from cncctl.controller.state import MachineState
from cncctl.facade import Facade


def _commissioned_config() -> Config:
    axis = AxisConfig(
        microsteps=8,
        lead_screw_mm=4.0,
        steps_per_mm=320.0,
        max_rate_mm_min=3000.0,
        acceleration_mm_s2=100.0,
        soft_limit_mm=200.0,
    )
    return Config(
        machine=MachineConfig(name="test"),
        transport=TransportConfig(default_port="/dev/ttyACM0"),
        axes=AxesConfig(x=axis, y=axis, z=axis),
        motion=MotionConfig(junction_deviation_mm=0.01),
        homing=HomingConfig(enabled=False),
    )


async def _facade() -> tuple[Facade, FakeController]:
    controller = FakeController()
    facade = Facade(controller)
    await facade.connect("COM-TEST")
    return facade, controller


# -- connection / passthrough ------------------------------------------------
async def test_connect_disconnect() -> None:
    controller = FakeController()
    facade = Facade(controller)
    await facade.connect("COM-TEST")
    assert controller.connected
    await facade.disconnect()
    assert not controller.connected


async def test_jog_forwards_when_idle() -> None:
    facade, controller = await _facade()
    await facade.jog(Axis.X, 10.0, 500.0)
    assert any(c.startswith("jog:") for c in controller.commands)


async def test_hold_then_resume() -> None:
    facade, controller = await _facade()
    controller.script_state(MachineState.RUN)
    await facade.hold()
    assert controller.state is MachineState.HOLD
    await facade.resume()
    assert controller.state is MachineState.RUN


async def test_status_stream() -> None:
    facade, _ = await _facade()
    stream = facade.status_stream()
    status = await anext(stream)
    assert status.state is MachineState.IDLE
    await stream.aclose()


async def test_state_property_reflects_controller() -> None:
    facade, controller = await _facade()
    assert facade.state is MachineState.IDLE
    controller.inject_alarm(1)
    assert facade.state is MachineState.ALARM


async def test_run_line_passthrough() -> None:
    facade, controller = await _facade()
    await facade.run_line("G0 X1")
    assert "G0 X1" in controller.commands


async def test_unlock_sends_kill_alarm() -> None:
    facade, controller = await _facade()
    await facade.unlock()
    assert "$X" in controller.commands


async def test_set_work_zero_emits_g10() -> None:
    facade, controller = await _facade()
    await facade.set_work_zero([Axis.X, Axis.Y])
    assert "G10 L20 P1 X0.0000 Y0.0000" in controller.commands


async def test_set_work_zero_honours_offset() -> None:
    facade, controller = await _facade()
    await facade.set_work_zero([Axis.Z], values={Axis.Z: 0.1})
    assert "G10 L20 P1 Z0.1000" in controller.commands


async def test_set_work_zero_requires_axes() -> None:
    facade, _ = await _facade()
    with pytest.raises(ValueError, match="at least one axis"):
        await facade.set_work_zero([])


async def test_cancel_jog_passthrough() -> None:
    facade, controller = await _facade()
    controller.script_state(MachineState.JOG)
    await facade.cancel_jog()
    assert controller.state is MachineState.IDLE


async def test_settings_passthrough() -> None:
    facade, _ = await _facade()
    await facade.write_setting(100, "250.000")
    settings = await facade.read_settings()
    assert settings.get(100) == "250.000"


# -- §8.1: motion gated at the facade ----------------------------------------
async def test_jog_blocked_in_alarm_before_reaching_controller() -> None:
    facade, controller = await _facade()
    controller.inject_alarm(1)
    before = list(controller.commands)
    with pytest.raises(MachineNotReadyError):
        await facade.jog(Axis.X, 5.0, 500.0)
    assert controller.commands == before  # nothing reached the controller


async def test_jog_blocked_in_door() -> None:
    facade, controller = await _facade()
    controller.open_door()
    with pytest.raises(MachineNotReadyError):
        await facade.jog(Axis.Y, 5.0, 500.0)


async def test_home_allowed_in_alarm_is_recovery() -> None:
    facade, controller = await _facade()
    controller.inject_alarm(1)
    await facade.home()  # homing is the recovery path; must not raise
    assert controller.state is MachineState.IDLE


async def test_home_blocked_with_open_door() -> None:
    facade, controller = await _facade()
    controller.open_door()
    with pytest.raises(MachineNotReadyError):
        await facade.home()


# -- §8.3: soft reset always available ---------------------------------------
async def test_reset_works_even_in_alarm() -> None:
    facade, controller = await _facade()
    controller.inject_alarm(1)
    await facade.reset()
    assert controller.state is MachineState.IDLE
    assert "0x18" in controller.commands


# -- bootstrap (§2, §8.7) ----------------------------------------------------
async def test_bootstrap_pushes_and_verifies() -> None:
    controller = FakeController()
    facade = Facade(controller)
    await facade.bootstrap(_commissioned_config(), "COM-TEST")
    settings = await controller.read_settings()
    assert settings.get(100) == "320.000"
    assert settings.get(11) == "0.010"


async def test_bootstrap_rejects_uncommissioned_before_connecting() -> None:
    controller = FakeController()
    facade = Facade(controller)
    placeholder = load_config(Path("config/machine.toml"))
    with pytest.raises(ConfigError):
        await facade.bootstrap(placeholder, "COM-TEST")
    assert not controller.connected  # refused before touching the machine


async def test_bootstrap_detects_settings_mismatch() -> None:
    class DroppingController(FakeController):
        async def write_setting(self, key: int, value: str) -> None:
            return  # ack but do not persist -> read-back will differ (§8.7)

    controller = DroppingController()
    facade = Facade(controller)
    with pytest.raises(SettingsMismatchError):
        await facade.bootstrap(_commissioned_config(), "COM-TEST")
