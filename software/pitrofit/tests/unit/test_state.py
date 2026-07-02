"""State-machine tests: exhaustive legal/illegal transitions and reset rules.

the design: "State machine: exhaustive transitions including illegal
ones (must raise)."
"""

from __future__ import annotations

import itertools

import pytest

from cncctl.controller.errors import IllegalTransitionError
from cncctl.controller.state import (
    ALLOWED,
    MOTION_BLOCKED_STATES,
    MachineState,
    StateMachine,
    is_legal,
)

ALL_STATES = list(MachineState)
ALL_PAIRS = list(itertools.product(ALL_STATES, ALL_STATES))


@pytest.mark.parametrize(("frm", "to"), ALL_PAIRS)
def test_every_transition_is_legal_or_raises(frm: MachineState, to: MachineState) -> None:
    sm = StateMachine(frm)
    if is_legal(frm, to):
        changed = sm.apply(to)
        assert sm.current is to
        assert changed is (frm is not to)
    else:
        with pytest.raises(IllegalTransitionError) as exc:
            sm.apply(to)
        assert exc.value.frm is frm
        assert exc.value.to is to
        assert sm.current is frm  # state unchanged after an illegal attempt


def test_self_transition_is_always_legal_and_reports_no_change() -> None:
    for state in ALL_STATES:
        sm = StateMachine(state)
        assert sm.apply(state) is False
        assert sm.current is state


def test_alarm_is_always_reachable() -> None:
    # A fault can occur in any state (hard limit, e-stop, ...).
    for state in ALL_STATES:
        assert MachineState.ALARM in ALLOWED[state]


def test_alarm_is_sticky_no_direct_motion() -> None:
    # the design: Alarm clears only via $X (-> Idle) or $H (-> Home).
    assert ALLOWED[MachineState.ALARM] == frozenset(
        {MachineState.IDLE, MachineState.HOME, MachineState.ALARM}
    )
    for forbidden in (MachineState.RUN, MachineState.JOG, MachineState.TOOL):
        assert forbidden not in ALLOWED[MachineState.ALARM]


def test_motion_blocked_states_are_alarm_and_door() -> None:
    # the design
    assert set(MOTION_BLOCKED_STATES) == {MachineState.ALARM, MachineState.DOOR}


def test_reset_lands_in_idle_by_default() -> None:
    sm = StateMachine(MachineState.RUN)
    sm.reset()
    assert sm.current is MachineState.IDLE


def test_reset_can_target_alarm_for_homing_required() -> None:
    sm = StateMachine(MachineState.SLEEP)
    sm.reset(MachineState.ALARM)
    assert sm.current is MachineState.ALARM


def test_reset_rejects_non_reset_targets() -> None:
    sm = StateMachine(MachineState.RUN)
    with pytest.raises(ValueError, match="reset target"):
        sm.reset(MachineState.RUN)


def test_reset_is_reachable_from_every_state() -> None:
    # SAFETY: soft reset is always available regardless of state.
    for state in ALL_STATES:
        sm = StateMachine(state)
        sm.reset()
        assert sm.current is MachineState.IDLE


def test_state_values_match_grbl_tokens() -> None:
    # The M3 parser maps a report token straight onto a member.
    assert MachineState.IDLE.value == "Idle"
    assert MachineState.ALARM.value == "Alarm"
    assert MachineState("Run") is MachineState.RUN
