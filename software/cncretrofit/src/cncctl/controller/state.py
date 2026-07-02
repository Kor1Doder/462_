"""Machine state model and legal-transition graph.

Ported from ioSender's ``GrblStates`` enum (``reference/ioSender/CNC Core/CNC
Core/Grbl.cs:154``) and the state handling in ``GrblViewModel.cs``
(``SetGRBLState``/``ParseStatus``). The string *value* of each member is the
exact token grblHAL emits as the first field of a status report
(``<State|...>`` or ``<State:Substate|...>``), so the M3 parser can map a
report token straight onto a member.

This module owns only the *coarse* state. The numeric substate (e.g. the ``1``
in ``Hold:1``, or alarm code ``11``) travels on the ``Status`` message, not in
the transition graph — keeping the graph tractable. See the design

The transition graph is our model of grbl's observable state changes. An
observed or requested edge that is not in the graph raises
``IllegalTransitionError`` rather than being silently accepted — the design:
"unexpected responses are typed exceptions ... Never assume the machine is
Idle." The benign-but-unenumerated edges are kept permissive; the
safety-critical edges (notably the stickiness of ``Alarm``) are kept strict.
"""

from __future__ import annotations

import enum

from cncctl.controller.errors import IllegalTransitionError


class MachineState(enum.Enum):
    """grblHAL machine states. Values match the status-report state token."""

    UNKNOWN = "Unknown"  # bootstrap, before the first status report
    IDLE = "Idle"
    RUN = "Run"
    HOLD = "Hold"
    JOG = "Jog"
    HOME = "Home"
    CHECK = "Check"
    ALARM = "Alarm"
    DOOR = "Door"
    SLEEP = "Sleep"
    TOOL = "Tool"


#: States in which a motion command must never be issued.
#:
#: SAFETY INVARIANT: no motion command is sent in ``Alarm`` or
#: ``Door`` state. The facade and the controllers both gate on this set before
#: anything reaches the streamer.
MOTION_BLOCKED_STATES: frozenset[MachineState] = frozenset({MachineState.ALARM, MachineState.DOOR})

#: States a soft reset / welcome may legitimately land in.
#: grbl comes up ``Idle``, or ``Alarm`` when homing is required first.
_RESET_TARGETS: frozenset[MachineState] = frozenset({MachineState.IDLE, MachineState.ALARM})

# Per-source explicit outgoing edges, *excluding* the universal edges added
# below (self-transition and -> Alarm). Each edge reflects real grbl behavior;
# the safety-critical entry is ALARM, which can only clear to IDLE (via $X /
# soft reset) or HOME (via $H) — never directly to a motion state.
_OUTGOING: dict[MachineState, frozenset[MachineState]] = {
    # Before the first status we know nothing; allow settling into any state.
    MachineState.UNKNOWN: frozenset(MachineState),
    MachineState.IDLE: frozenset(
        {
            MachineState.RUN,
            MachineState.JOG,
            MachineState.HOME,
            MachineState.CHECK,
            MachineState.SLEEP,
            MachineState.TOOL,
            MachineState.DOOR,
        }
    ),
    MachineState.RUN: frozenset(
        {MachineState.IDLE, MachineState.HOLD, MachineState.TOOL, MachineState.DOOR}
    ),
    MachineState.HOLD: frozenset(
        {MachineState.IDLE, MachineState.RUN, MachineState.SLEEP, MachineState.DOOR}
    ),
    MachineState.JOG: frozenset({MachineState.IDLE, MachineState.HOLD, MachineState.DOOR}),
    MachineState.HOME: frozenset({MachineState.IDLE, MachineState.DOOR}),
    MachineState.CHECK: frozenset({MachineState.IDLE, MachineState.RUN, MachineState.DOOR}),
    # Sticky: clears only to Idle (unlock/reset) or Home ($H). Never to motion.
    MachineState.ALARM: frozenset({MachineState.IDLE, MachineState.HOME}),
    MachineState.DOOR: frozenset({MachineState.IDLE, MachineState.RUN, MachineState.HOLD}),
    # Sleep exits only via a reset (modeled by StateMachine.reset()).
    MachineState.SLEEP: frozenset({MachineState.IDLE}),
    MachineState.TOOL: frozenset(
        {MachineState.IDLE, MachineState.RUN, MachineState.HOLD, MachineState.DOOR}
    ),
}


def _build_allowed() -> dict[MachineState, frozenset[MachineState]]:
    """Materialize the full transition table including the universal edges.

    Universal edges added to every source state:
      * self-transition (repeated status reports report the same state),
      * ``-> Alarm`` (a fault — hard limit, e-stop, etc. — can occur in any state).
    """
    allowed: dict[MachineState, frozenset[MachineState]] = {}
    for state in MachineState:
        explicit = _OUTGOING.get(state, frozenset())
        allowed[state] = explicit | {state, MachineState.ALARM}
    return allowed


#: ``ALLOWED[frm]`` is the set of states reachable from ``frm`` in one step.
ALLOWED: dict[MachineState, frozenset[MachineState]] = _build_allowed()


def is_legal(frm: MachineState, to: MachineState) -> bool:
    """Return whether ``frm -> to`` is a legal one-step transition."""
    return to in ALLOWED[frm]


class StateMachine:
    """Tracks the machine's coarse state and enforces the transition graph.

    This is a passive model: it does not talk to hardware. The controller feeds
    it observed states (from status reports / alarm lines), and consumers read
    :attr:`current`. Illegal observed transitions raise rather than being
    accepted, so a parser bug or a genuinely impossible report surfaces loudly.
    """

    __slots__ = ("_current",)

    def __init__(self, initial: MachineState = MachineState.UNKNOWN) -> None:
        self._current = initial

    @property
    def current(self) -> MachineState:
        """The most recently accepted state."""
        return self._current

    def apply(self, new_state: MachineState) -> bool:
        """Advance the model to ``new_state``.

        Returns ``True`` if the state actually changed, ``False`` for a
        self-transition (a repeated report of the same state).

        Raises:
            IllegalTransitionError: if ``current -> new_state`` is not in the
                transition graph.
        """
        if not is_legal(self._current, new_state):
            raise IllegalTransitionError(self._current, new_state)
        changed = new_state is not self._current
        self._current = new_state
        return changed

    def reset(self, to: MachineState = MachineState.IDLE) -> None:
        """Force the model to a post-reset state, bypassing the graph.

        Models a soft reset (``0x18``) or a ``Welcome`` line:
        a hard state reset reachable from *any* state. grbl lands in ``Idle``,
        or ``Alarm`` when homing is required first.

        Raises:
            ValueError: if ``to`` is not a legal reset target (programmer error).
        """
        if to not in _RESET_TARGETS:
            raise ValueError(f"reset target must be one of {_RESET_TARGETS}, got {to!r}")
        self._current = to


__all__ = [
    "ALLOWED",
    "MOTION_BLOCKED_STATES",
    "MachineState",
    "StateMachine",
    "is_legal",
]
