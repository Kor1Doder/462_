"""Outbound command-builder tests (M3)."""

from __future__ import annotations

import pytest

from cncctl.controller.messages import Axis
from cncctl.protocol import outbound


def test_system_command_constants() -> None:
    assert outbound.GET_SETTINGS == "$$"
    assert outbound.UNLOCK == "$X"
    assert outbound.HOME == "$H"
    assert outbound.GET_BUILD_INFO == "$I"
    assert outbound.GET_PARSER_STATE == "$G"


def test_format_home_all_axes() -> None:
    assert outbound.format_home() == "$H"


def test_format_home_axis_subset() -> None:
    assert outbound.format_home([Axis.X, Axis.Z]) == "$HXZ"


def test_format_jog_basic() -> None:
    assert outbound.format_jog(Axis.X, 10.0, 500.0) == "$J=G91 G21 X10 F500"


def test_format_jog_negative_and_fractional() -> None:
    assert outbound.format_jog(Axis.Y, -3.25, 1000.0) == "$J=G91 G21 Y-3.25 F1000"


def test_format_jog_zero_distance() -> None:
    assert outbound.format_jog(Axis.Z, 0.0, 250.0) == "$J=G91 G21 Z0 F250"


def test_format_jog_rejects_nonpositive_feed() -> None:
    with pytest.raises(ValueError, match="feed"):
        outbound.format_jog(Axis.X, 5.0, 0.0)
    with pytest.raises(ValueError, match="feed"):
        outbound.format_jog(Axis.X, 5.0, -100.0)


def test_format_setting() -> None:
    assert outbound.format_setting(100, "250.000") == "$100=250.000"
    assert outbound.format_setting(11, "0.010") == "$11=0.010"
