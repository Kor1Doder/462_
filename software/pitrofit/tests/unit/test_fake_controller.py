"""FakeController tests — exercises every Controller method against the fake.

CLAUDE.md §7 M1 done-criteria: "Tier 1 tests exercise every method of the
protocol against the fake." Safety rejections per §8.1 are asserted here too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from cncctl.controller.errors import MachineNotReadyError, NotConnectedError
from cncctl.controller.fake import FakeController
from cncctl.controller.messages import Axis
from cncctl.controller.protocol import Controller
from cncctl.controller.state import MachineState


async def _aiter[T](items: list[T]) -> AsyncIterator[T]:
    for item in items:
        yield item


async def _connected(**kwargs: object) -> FakeController:
    fc = FakeController(**kwargs)  # type: ignore[arg-type]
    await fc.connect("COM-TEST")
    return fc


# -- conformance -------------------------------------------------------------
def test_fake_satisfies_controller_protocol() -> None:
    assert isinstance(FakeController(), Controller)


# -- connect / disconnect ----------------------------------------------------
async def test_connect_sets_idle_and_records() -> None:
    fc = FakeController()
    assert fc.state is MachineState.UNKNOWN
    assert not fc.connected
    await fc.connect("/dev/ttyACM0")
    assert fc.connected
    assert fc.state is MachineState.IDLE
    assert fc.commands == ["connect:/dev/ttyACM0"]


async def test_connect_can_land_in_alarm_when_homing_required() -> None:
    fc = FakeController(reset_state=MachineState.ALARM)
    await fc.connect("/dev/ttyACM0")
    assert fc.state is MachineState.ALARM


async def test_disconnect_returns_to_unknown() -> None:
    fc = await _connected()
    await fc.disconnect()
    assert not fc.connected
    assert fc.state is MachineState.UNKNOWN


# -- soft reset (always available, §8.3) -------------------------------------
async def test_soft_reset_works_from_run() -> None:
    fc = await _connected()
    fc.script_state(MachineState.RUN)
    await fc.soft_reset()
    assert fc.state is MachineState.IDLE
    assert "0x18" in fc.commands


async def test_soft_reset_clears_alarm() -> None:
    fc = await _connected()
    fc.inject_alarm(1)
    await fc.soft_reset()
    assert fc.state is MachineState.IDLE
    assert fc.last_alarm is None


async def test_soft_reset_requires_connection() -> None:
    fc = FakeController()
    with pytest.raises(NotConnectedError):
        await fc.soft_reset()


# -- home --------------------------------------------------------------------
async def test_home_from_idle() -> None:
    fc = await _connected()
    await fc.home()
    assert fc.state is MachineState.IDLE
    assert fc.mpos.x == 0.0
    assert "$H" in fc.commands


async def test_home_with_axis_subset_records_letters() -> None:
    fc = await _connected()
    await fc.home([Axis.X, Axis.Z])
    assert "$HXZ" in fc.commands


async def test_home_clears_alarm() -> None:
    fc = await _connected()
    fc.inject_alarm(11)
    await fc.home()
    assert fc.state is MachineState.IDLE
    assert fc.last_alarm is None


async def test_home_rejected_in_door() -> None:
    fc = await _connected()
    fc.open_door()
    with pytest.raises(MachineNotReadyError):
        await fc.home()


async def test_home_rejected_when_busy() -> None:
    fc = await _connected()
    fc.script_state(MachineState.RUN)
    with pytest.raises(MachineNotReadyError):
        await fc.home()


# -- jog ---------------------------------------------------------------------
async def test_jog_updates_mpos() -> None:
    fc = await _connected()
    await fc.jog(Axis.X, 10.0, 500.0)
    assert fc.mpos.x == 10.0
    assert fc.state is MachineState.IDLE
    await fc.jog(Axis.X, -3.0, 500.0)
    assert fc.mpos.x == 7.0
    assert fc.commands[-1] == "jog:X:-3.0:500.0"


async def test_jog_rejected_in_alarm() -> None:
    # SAFETY §8.1.
    fc = await _connected()
    fc.inject_alarm(1)
    with pytest.raises(MachineNotReadyError):
        await fc.jog(Axis.X, 5.0, 500.0)


async def test_jog_rejected_in_door() -> None:
    # SAFETY §8.1.
    fc = await _connected()
    fc.open_door()
    with pytest.raises(MachineNotReadyError):
        await fc.jog(Axis.Y, 5.0, 500.0)


async def test_jog_requires_connection() -> None:
    fc = FakeController()
    with pytest.raises(NotConnectedError):
        await fc.jog(Axis.X, 5.0, 500.0)


# -- cancel_jog --------------------------------------------------------------
async def test_cancel_jog_returns_to_idle() -> None:
    fc = await _connected()
    fc.script_state(MachineState.JOG)
    await fc.cancel_jog()
    assert fc.state is MachineState.IDLE
    assert "0x85" in fc.commands


async def test_cancel_jog_is_noop_when_idle() -> None:
    fc = await _connected()
    await fc.cancel_jog()
    assert fc.state is MachineState.IDLE


# -- feed_hold / resume ------------------------------------------------------
async def test_feed_hold_then_resume() -> None:
    fc = await _connected()
    fc.script_state(MachineState.RUN)
    await fc.feed_hold()
    assert fc.state is MachineState.HOLD
    await fc.resume()
    assert fc.state is MachineState.RUN
    assert "!" in fc.commands
    assert "~" in fc.commands


async def test_feed_hold_is_noop_when_idle() -> None:
    fc = await _connected()
    await fc.feed_hold()
    assert fc.state is MachineState.IDLE


# -- settings ----------------------------------------------------------------
async def test_read_settings_returns_snapshot() -> None:
    fc = await _connected(settings={100: "250.000"})
    settings = await fc.read_settings()
    assert settings.get(100) == "250.000"
    assert "$$" in fc.commands


async def test_write_setting_updates_map() -> None:
    fc = await _connected(settings={100: "250.000"})
    await fc.write_setting(100, "260.000")
    settings = await fc.read_settings()
    assert settings.get(100) == "260.000"
    assert "$100=260.000" in fc.commands


async def test_read_settings_requires_connection() -> None:
    fc = FakeController()
    with pytest.raises(NotConnectedError):
        await fc.read_settings()


# -- send_program ------------------------------------------------------------
async def test_send_program_yields_progress_per_line() -> None:
    fc = await _connected()
    lines = ["G0 X1", "G1 Y2 F100", "M30"]
    progress = [p async for p in fc.send_program(_aiter(lines))]
    assert len(progress) == 3
    assert all(p.total == 3 for p in progress)
    assert [p.line for p in progress] == [1, 2, 3]
    assert progress[-1].acknowledged == 3
    assert fc.sent_lines == lines
    assert fc.state is MachineState.IDLE  # back to Idle once exhausted


async def test_send_program_rejected_in_alarm() -> None:
    # SAFETY §8.1: refuses before sending anything.
    fc = await _connected()
    fc.inject_alarm(1)
    with pytest.raises(MachineNotReadyError):
        _ = [p async for p in fc.send_program(_aiter(["G0 X1"]))]
    assert fc.sent_lines == []


async def test_send_program_empty_source_is_noop() -> None:
    fc = await _connected()
    progress = [p async for p in fc.send_program(_aiter([]))]
    assert progress == []
    assert fc.state is MachineState.IDLE


async def test_send_program_requires_connection() -> None:
    fc = FakeController()
    with pytest.raises(NotConnectedError):
        _ = [p async for p in fc.send_program(_aiter(["G0 X1"]))]


# -- status_stream -----------------------------------------------------------
async def test_status_stream_reflects_current_state() -> None:
    fc = await _connected()
    stream = fc.status_stream()
    first = await anext(stream)
    assert first.state is MachineState.IDLE

    fc.inject_alarm(7)
    second = await anext(stream)
    assert second.state is MachineState.ALARM
    assert second.substate == 7
    await stream.aclose()


async def test_status_stream_requires_connection() -> None:
    fc = FakeController()
    stream = fc.status_stream()
    with pytest.raises(NotConnectedError):
        await anext(stream)
