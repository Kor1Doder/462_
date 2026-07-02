"""Inbound parser tests: a corpus of every shape plus property round-trips.

the design /: "Hypothesis round-trips of every well-formed line
shape" and "parser handles every example in the grblHAL docs and the test
corpus".
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cncctl.controller.errors import ParseError
from cncctl.controller.messages import (
    Alarm,
    BuildInfo,
    Error,
    Feedback,
    ModalState,
    Ok,
    Overrides,
    Position,
    ProbeResult,
    SettingLine,
    Status,
    WCSReport,
    Welcome,
)
from cncctl.controller.state import MachineState
from cncctl.protocol.inbound import parse_line


# -- simple acks / alarms ----------------------------------------------------
def test_ok() -> None:
    assert parse_line("ok") == Ok()


def test_ok_from_bytes() -> None:
    assert parse_line(b"ok") == Ok()


def test_error() -> None:
    assert parse_line("error:9") == Error(code=9)


def test_alarm() -> None:
    assert parse_line("ALARM:1") == Alarm(code=1)


# -- status reports ----------------------------------------------------------
def test_status_minimal() -> None:
    msg = parse_line("<Idle|MPos:0.000,0.000,0.000|FS:0,0>")
    assert isinstance(msg, Status)
    assert msg.state is MachineState.IDLE
    assert msg.mpos == Position(0.0, 0.0, 0.0)
    assert msg.feed == 0.0
    assert msg.spindle == 0.0


def test_status_with_substate() -> None:
    msg = parse_line("<Hold:0|MPos:0.000,0.000,0.000|FS:0,0>")
    assert isinstance(msg, Status)
    assert msg.state is MachineState.HOLD
    assert msg.substate == 0


def test_status_with_overrides_and_feed_spindle() -> None:
    msg = parse_line("<Run|MPos:1.000,2.000,3.000|FS:500,1000|Ov:100,90,110>")
    assert isinstance(msg, Status)
    assert msg.state is MachineState.RUN
    assert msg.mpos == Position(1.0, 2.0, 3.0)
    assert msg.feed == 500.0
    assert msg.spindle == 1000.0
    assert msg.overrides == Overrides(feed=100, rapid=90, spindle=110)


def test_status_work_position_and_wco() -> None:
    msg = parse_line("<Idle|WPos:1.000,2.000,3.000|WCO:0.500,0.000,0.000>")
    assert isinstance(msg, Status)
    assert msg.mpos is None  # stateless parser leaves MPos for the controller to derive
    assert msg.wpos == Position(1.0, 2.0, 3.0)
    assert msg.wco == Position(0.5, 0.0, 0.0)


def test_status_buffer_line_pins_accessory() -> None:
    msg = parse_line("<Idle|MPos:0.000,0.000,0.000|Bf:35,1024|Ln:99|Pn:XYZ|A:SF>")
    assert isinstance(msg, Status)
    assert msg.buffer_planner == 35
    assert msg.buffer_rx == 1024
    assert msg.line_number == 99
    assert msg.pins == "XYZ"
    assert msg.accessory == "SF"


def test_status_feed_only_field() -> None:
    msg = parse_line("<Jog|MPos:0.000,0.000,0.000|F:500>")
    assert isinstance(msg, Status)
    assert msg.feed == 500.0
    assert msg.spindle is None


@pytest.mark.parametrize("state", list(MachineState))
def test_status_parses_every_state(state: MachineState) -> None:
    msg = parse_line(f"<{state.value}|MPos:0.000,0.000,0.000>")
    assert isinstance(msg, Status)
    assert msg.state is state


# -- bracket messages --------------------------------------------------------
def test_feedback_message() -> None:
    assert parse_line("[MSG:Pgm End]") == Feedback(text="Pgm End")


def test_modal_state() -> None:
    msg = parse_line("[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]")
    assert isinstance(msg, ModalState)
    assert msg.modes[0] == "G0"
    assert "G54" in msg.modes


def test_wcs_report() -> None:
    assert parse_line("[G54:1.000,2.000,3.000]") == WCSReport(
        wcs="G54", offset=Position(1.0, 2.0, 3.0)
    )


def test_wcs_report_indexed_system() -> None:
    msg = parse_line("[G59.1:0.000,0.000,0.000]")
    assert isinstance(msg, WCSReport)
    assert msg.wcs == "G59.1"


def test_probe_result_success() -> None:
    assert parse_line("[PRB:0.000,0.000,1.234:1]") == ProbeResult(
        position=Position(0.0, 0.0, 1.234), success=True
    )


def test_probe_result_failure() -> None:
    msg = parse_line("[PRB:0.000,0.000,0.000:0]")
    assert isinstance(msg, ProbeResult)
    assert msg.success is False


def test_build_info_version() -> None:
    msg = parse_line("[VER:1.1f.20230920:]")
    assert isinstance(msg, BuildInfo)
    assert msg.version.startswith("1.1f")


def test_build_info_options() -> None:
    msg = parse_line("[OPT:VNMSL,35,254]")
    assert isinstance(msg, BuildInfo)
    assert msg.options == "VNMSL,35,254"


def test_unknown_bracket_falls_back_to_feedback() -> None:
    assert parse_line("[Plugin: SD card v1.0]") == Feedback(text="Plugin: SD card v1.0")


def test_bracket_without_colon_is_feedback() -> None:
    assert parse_line("[enabled]") == Feedback(text="enabled")


# -- settings and welcome ----------------------------------------------------
def test_setting_line() -> None:
    assert parse_line("$100=250.000") == SettingLine(key=100, value="250.000")


def test_welcome_grbl() -> None:
    msg = parse_line("Grbl 1.1f ['$' for help]")
    assert isinstance(msg, Welcome)
    assert msg.version == "1.1f"


def test_welcome_grblhal() -> None:
    msg = parse_line("GrblHAL 1.1f ['$' or '$HELP' for help]")
    assert isinstance(msg, Welcome)
    assert msg.version == "1.1f"


# -- error handling ----------------------------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "",
        "   ",
        "garbage",
        "<Idle|MPos:0.000,0.000>",  # position needs 3 components
        "<Frobnicate|MPos:0,0,0>",  # unknown state
        "error:notanumber",
        "ALARM:x",
        "$abc=1",  # non-numeric setting key
        "<Idle|Ov:100,100>",  # Ov needs 3
        "<Idle|Bf:35>",  # Bf needs 2
        "<Idle|MPos:a,b,c>",  # non-numeric position components
        "[PRB:0.000,0.000,0.000]",  # PRB missing :success
    ],
)
def test_malformed_lines_raise(line: str) -> None:
    with pytest.raises(ParseError):
        parse_line(line)


def test_status_ignores_field_without_colon() -> None:
    # A field with no colon is skipped leniently, not an error.
    msg = parse_line("<Idle|MPos:0.000,0.000,0.000|extrabareword>")
    assert isinstance(msg, Status)
    assert msg.mpos == Position(0.0, 0.0, 0.0)


def test_undecodable_bytes_raise() -> None:
    with pytest.raises(ParseError):
        parse_line(b"\xff\xfe")


# -- property round-trips ----------------------------------------------------
finite = st.floats(allow_nan=False, allow_infinity=False)


@given(x=finite, y=finite, z=finite)
def test_status_position_roundtrip(x: float, y: float, z: float) -> None:
    msg = parse_line(f"<Idle|MPos:{x!r},{y!r},{z!r}>")
    assert isinstance(msg, Status)
    assert msg.mpos == Position(x, y, z)


@given(code=st.integers(min_value=0, max_value=255))
def test_error_code_roundtrip(code: int) -> None:
    assert parse_line(f"error:{code}") == Error(code=code)


@given(code=st.integers(min_value=0, max_value=255))
def test_alarm_code_roundtrip(code: int) -> None:
    assert parse_line(f"ALARM:{code}") == Alarm(code=code)


@given(
    key=st.integers(min_value=0, max_value=999),
    value=st.text(alphabet="0123456789.-", min_size=1, max_size=8),
)
def test_setting_roundtrip(key: int, value: str) -> None:
    assert parse_line(f"${key}={value}") == SettingLine(key=key, value=value)
