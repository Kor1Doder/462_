"""Facade.send_program tests (M9): soft-limit pre-flight, streaming, cancel.

Against FakeController, so the focus is the pre-flight gate (§8.2) and the
cancel sequence — not real streaming timing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cncctl.config_io import (
    AxesConfig,
    AxisConfig,
    Config,
    MachineConfig,
    MotionConfig,
    TransportConfig,
)
from cncctl.controller.errors import ConfigError, SoftLimitError
from cncctl.controller.fake import FakeController
from cncctl.facade import Facade, MachineProfile
from cncctl.viz.analyze import SoftLimits
from cncctl.viz.simulate import Kinematics


def _profile(*, x_max: float = 200.0, y_max: float = 200.0, z_max: float = 50.0) -> MachineProfile:
    return MachineProfile(
        soft_limits=SoftLimits(x=(-1.0, x_max), y=(-1.0, y_max), z=(-110.0, z_max)),
        kinematics=Kinematics(max_rate_mm_min=3000.0),
    )


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "prog.nc"
    path.write_text(text, encoding="utf-8")
    return path


async def _facade(profile: MachineProfile | None) -> tuple[Facade, FakeController]:
    controller = FakeController()
    facade = Facade(controller, profile=profile)
    await facade.connect("COM-TEST")
    return facade, controller


async def test_send_program_streams_an_in_bounds_program(tmp_path: Path) -> None:
    facade, controller = await _facade(_profile())
    path = _write(tmp_path, "G90\nG0 X0 Y0\nG1 X50 Y50 F600\nG0 X0 Y0\nM30")
    progress = [p async for p in facade.send_program(path)]
    assert len(progress) == 5
    assert all(p.total == 5 for p in progress)
    assert controller.sent_lines == ["G90", "G0 X0 Y0", "G1 X50 Y50 F600", "G0 X0 Y0", "M30"]


async def test_send_program_refuses_out_of_bounds_and_sends_nothing(tmp_path: Path) -> None:
    # SAFETY §8.2: pre-flight refuses before any line is sent.
    facade, controller = await _facade(_profile(x_max=10.0))
    path = _write(tmp_path, "G90\nG1 X50 F600")
    with pytest.raises(SoftLimitError, match="X max"):
        _ = [p async for p in facade.send_program(path)]
    assert controller.sent_lines == []


async def test_send_program_requires_a_profile(tmp_path: Path) -> None:
    facade, _ = await _facade(profile=None)
    path = _write(tmp_path, "G0 X1")
    with pytest.raises(ConfigError):
        _ = [p async for p in facade.send_program(path)]


async def test_analyze_file_previews_without_sending(tmp_path: Path) -> None:
    facade, controller = await _facade(_profile())
    path = _write(tmp_path, "G90\nG1 X50 Y40 F600")
    result = await facade.analyze_file(path)
    assert result.in_bounds
    assert result.max.x == 50.0
    assert result.max.y == 40.0
    assert controller.sent_lines == []  # analysis only


async def test_analyze_file_requires_a_profile(tmp_path: Path) -> None:
    facade, _ = await _facade(profile=None)
    with pytest.raises(ConfigError):
        await facade.analyze_file(_write(tmp_path, "G0 X1"))


async def test_cancel_issues_feed_hold_then_soft_reset() -> None:
    facade, controller = await _facade(_profile())
    await facade.cancel()
    assert "!" in controller.commands
    assert "0x18" in controller.commands
    assert controller.commands.index("!") < controller.commands.index("0x18")


def test_machine_profile_from_config() -> None:
    axis = AxisConfig(
        microsteps=8,
        lead_screw_mm=4.0,
        steps_per_mm=320.0,
        max_rate_mm_min=3000.0,
        acceleration_mm_s2=100.0,
        soft_limit_mm=200.0,
    )
    config = Config(
        machine=MachineConfig(name="m"),
        transport=TransportConfig(default_port="/dev/ttyACM0"),
        axes=AxesConfig(x=axis, y=axis, z=axis),
        motion=MotionConfig(junction_deviation_mm=0.01),
    )
    profile = MachineProfile.from_config(config)
    assert profile.kinematics.max_rate_mm_min == 3000.0
    assert profile.soft_limits.x == (0.0, 200.0)
