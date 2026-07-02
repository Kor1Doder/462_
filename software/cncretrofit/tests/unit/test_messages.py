"""Message tests: msgspec round-trips (Hypothesis) and immutability.

the design: "Inbound parser: Hypothesis round-trips of every
well-formed line shape." The parser is M3; here we verify the *message types*
themselves serialize losslessly and are immutable.
"""

from __future__ import annotations

import msgspec
import pytest
from hypothesis import given
from hypothesis import strategies as st

from cncctl.controller.messages import (
    Axis,
    InputSignals,
    Overrides,
    Position,
    ProbeResult,
    ProgramProgress,
    SettingLine,
    Settings,
    Status,
)
from cncctl.controller.state import MachineState

# JSON-safe building blocks.
finite = st.floats(allow_nan=False, allow_infinity=False)
nonneg = st.floats(min_value=0.0, allow_nan=False, allow_infinity=False)
safe_text = st.text(st.characters(min_codepoint=1, blacklist_categories=("Cs",)), max_size=40)
setting_keys = st.integers(min_value=0, max_value=1000)

positions = st.builds(Position, x=finite, y=finite, z=finite)


def roundtrip[T](obj: T) -> T:
    """Encode to JSON and decode back into the same type."""
    return msgspec.json.decode(msgspec.json.encode(obj), type=type(obj))


@given(positions)
def test_position_roundtrip(obj: Position) -> None:
    assert roundtrip(obj) == obj


@given(st.builds(ProbeResult, position=positions, success=st.booleans()))
def test_probe_result_roundtrip(obj: ProbeResult) -> None:
    assert roundtrip(obj) == obj


@given(st.builds(SettingLine, key=setting_keys, value=safe_text))
def test_setting_line_roundtrip(obj: SettingLine) -> None:
    assert roundtrip(obj) == obj


@given(st.builds(Settings, values=st.dictionaries(setting_keys, safe_text)))
def test_settings_roundtrip_preserves_int_keys(obj: Settings) -> None:
    decoded = roundtrip(obj)
    assert decoded == obj
    assert all(isinstance(k, int) for k in decoded.values)


@given(
    st.builds(
        Status,
        state=st.sampled_from(MachineState),
        mpos=positions,
        substate=st.none() | st.integers(min_value=0, max_value=20),
        feed=st.none() | finite,
        overrides=st.none() | st.builds(Overrides),
    )
)
def test_status_roundtrip(obj: Status) -> None:
    assert roundtrip(obj) == obj


@given(
    st.builds(
        ProgramProgress,
        line=st.integers(min_value=0),
        total=st.integers(min_value=0),
        sent=st.integers(min_value=0),
        acknowledged=st.integers(min_value=0),
        elapsed_s=nonneg,
        state=st.sampled_from(MachineState),
        mpos=st.none() | positions,
    )
)
def test_program_progress_roundtrip(obj: ProgramProgress) -> None:
    assert roundtrip(obj) == obj


def test_position_value_indexes_by_axis() -> None:
    p = Position(1.0, 2.0, 3.0)
    assert p.value(Axis.X) == 1.0
    assert p.value(Axis.Y) == 2.0
    assert p.value(Axis.Z) == 3.0


def test_settings_get() -> None:
    s = Settings(values={100: "250.000"})
    assert s.get(100) == "250.000"
    assert s.get(999) is None


def test_messages_are_frozen() -> None:
    p = Position(1.0, 2.0, 3.0)
    with pytest.raises((AttributeError, TypeError)):
        p.x = 9.0  # type: ignore[misc]


def test_overrides_default_to_full_scale() -> None:
    ov = Overrides()
    assert (ov.feed, ov.rapid, ov.spindle) == (100, 100, 100)


# --- InputSignals: the Pn "switch logic" decode -----------------------------
def test_input_signals_empty_is_all_clear() -> None:
    sig = InputSignals.from_pins("")
    assert not sig.active
    assert not sig.any_limit
    assert sig.other == frozenset()


def test_input_signals_decodes_limits_and_controls() -> None:
    sig = InputSignals.from_pins("XZPE")
    assert sig.limit_x and sig.limit_z and not sig.limit_y
    assert sig.probe and sig.estop
    assert not sig.door
    assert sig.any_limit and sig.active


def test_input_signals_preserves_unknown_letters() -> None:
    sig = InputSignals.from_pins("XA")  # A = extra-axis limit, not named on a 3-axis mill
    assert sig.limit_x
    assert sig.other == frozenset({"A"})
    assert sig.active  # the unknown letter still counts as asserted


def test_input_signals_control_inputs() -> None:
    sig = InputSignals.from_pins("RHSD")
    assert sig.reset and sig.feed_hold and sig.cycle_start and sig.door
    assert not sig.any_limit


def test_status_signals_property_decodes_pins() -> None:
    assert Status(state=MachineState.ALARM, pins="Y").signals.limit_y
    assert not Status(state=MachineState.IDLE).signals.active  # pins is None -> all clear
